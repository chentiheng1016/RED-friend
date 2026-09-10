from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts import ci_operational_db_smoke as smoke


def _good_summary() -> dict[str, object]:
    return {
        "health": {
            "schema_ok": True,
            "issues": [],
            "operational_db": {
                "enabled": True,
                "ok": True,
                "schema_version": 16,
                "schema_expected": 16,
                "error": "",
            },
        },
        "dry_run": {
            "results": [
                {"source": "cost_events", "status": "ok", "seen": 2},
                {"source": "edge_tasks", "status": "missing", "seen": 0},
            ],
        },
        "table_counts": {
            "schema_migrations": {"max_version": 16, "rows": 1},
            "table_count": 3,
            "tables": {
                "red_schema_migrations": 1,
                "red_backfill_events": 0,
                "red_gemini_circuit_state": 0,
            },
        },
    }


class CiOperationalDbSmokeTests(unittest.TestCase):
    def test_validate_summary_accepts_good_rehearsal(self):
        self.assertEqual(smoke._validate_summary(_good_summary()), [])

    def test_validate_summary_reports_schema_and_table_issues(self):
        summary = _good_summary()
        health = summary["health"]
        self.assertIsInstance(health, dict)
        db = health["operational_db"]
        self.assertIsInstance(db, dict)
        db["schema_version"] = 15
        counts = summary["table_counts"]
        self.assertIsInstance(counts, dict)
        tables = counts["tables"]
        self.assertIsInstance(tables, dict)
        tables.pop("red_backfill_events")

        issues = smoke._validate_summary(summary)

        self.assertTrue(any("schema version mismatch" in issue for issue in issues))
        self.assertTrue(any("red_backfill_events" in issue for issue in issues))

    def test_main_skips_when_postgres_missing_unless_required(self):
        with mock.patch.object(smoke, "_postgres_available", return_value=(False, "missing")):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(smoke.main([]), 0)
            self.assertIn("skipped", stdout.getvalue())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(smoke.main(["--require-postgres"]), 2)
            self.assertIn("skipped", stdout.getvalue())

    def test_main_runs_rehearsal_and_prints_ok(self):
        summary = _good_summary()
        completed = subprocess.CompletedProcess(
            args=["rehearse"],
            returncode=0,
            stdout=json.dumps(summary),
            stderr="",
        )
        with mock.patch.object(smoke, "_postgres_available", return_value=(True, "")), \
                mock.patch.object(smoke.subprocess, "run", return_value=completed) as run:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = smoke.main(["--dry-run-limit", "2"])

        self.assertEqual(code, 0)
        self.assertIn("operational DB smoke: ok schema=16/16 tables=3", stdout.getvalue())
        cmd = run.call_args.args[0]
        self.assertIn("--skip-write", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("--dry-run-limit", cmd)

    def test_main_returns_failure_for_invalid_summary(self):
        completed = subprocess.CompletedProcess(
            args=["rehearse"],
            returncode=0,
            stdout=json.dumps({"health": {}}),
            stderr="",
        )
        with mock.patch.object(smoke, "_postgres_available", return_value=(True, "")), \
                mock.patch.object(smoke.subprocess, "run", return_value=completed):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = smoke.main([])

        self.assertEqual(code, 1)
        self.assertIn("operational DB smoke: failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
