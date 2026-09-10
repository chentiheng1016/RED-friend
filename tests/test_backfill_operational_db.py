from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backfill_operational_db as backfill


class FakeCursor:
    def __init__(self, claimed: set[tuple[str, str]], calls: list[tuple[str, object]]):
        self._claimed = claimed
        self._calls = calls
        self._fetchone = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))
        if "INSERT INTO red_backfill_events" not in sql:
            self._fetchone = None
            return
        source, source_hash = params[0], params[1]
        key = (source, source_hash)
        if key in self._claimed:
            self._fetchone = None
            return
        self._claimed.add(key)
        self._fetchone = (1,)

    def fetchone(self):
        return self._fetchone


class FakeConnection:
    def __init__(self, claimed: set[tuple[str, str]], calls: list[tuple[str, object]]):
        self._claimed = claimed
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return FakeCursor(self._claimed, self._calls)


class BackfillOperationalDBTests(unittest.TestCase):
    def test_iter_jsonl_entries_keeps_original_line_numbers_under_limit(self):
        with tempfile.TemporaryDirectory(prefix="red_backfill_test_") as tmp:
            path = Path(tmp) / "cost.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps({"seq": seq}, ensure_ascii=False)
                    for seq in (1, 2, 3)
                )
                + "\n",
                encoding="utf-8",
            )

            entries, exists = backfill._iter_jsonl_entries(str(path), limit=2)

        self.assertTrue(exists)
        self.assertEqual([entry.line_no for entry in entries], [2, 3])
        self.assertEqual([entry.row["seq"] for entry in entries], [2, 3])

    def test_cost_event_backfill_is_idempotent_on_rerun(self):
        from agent_core import cost_tracker

        claimed: set[tuple[str, str]] = set()
        calls: list[tuple[str, object]] = []

        def _connect():
            return FakeConnection(claimed, calls)

        with tempfile.TemporaryDirectory(prefix="red_backfill_test_") as tmp:
            path = Path(tmp) / "cost.jsonl"
            rows = [
                {"ts": "2026-06-24T12:00:00+00:00", "model": "gemini", "total_tokens": 10},
                {"ts": "2026-06-24T12:00:01+00:00", "model": "gemini", "total_tokens": 20},
            ]
            path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )

            with mock.patch.object(cost_tracker, "_COST_LOG", str(path)), \
                    mock.patch.object(backfill.operational_db, "ensure_schema"), \
                    mock.patch.object(backfill.operational_db, "connect", side_effect=_connect):
                first = backfill._backfill_cost_events(dry_run=False, limit=0)
                second = backfill._backfill_cost_events(dry_run=False, limit=0)

        self.assertEqual((first.seen, first.written, first.skipped), (2, 2, 0))
        self.assertEqual((second.seen, second.written, second.skipped), (2, 0, 2))
        target_inserts = [
            sql
            for sql, _params in calls
            if "INSERT INTO red_cost_events" in sql
        ]
        self.assertEqual(len(target_inserts), 2)

    def test_json_payload_includes_totals_and_schema(self):
        results = [
            backfill.Result("cost_events", "/tmp/cost.jsonl", seen=2, written=1, skipped=1),
            backfill.Result("edge_tasks", "/tmp/edge.json", status="missing"),
        ]

        payload = backfill._json_payload(
            results=results,
            dry_run=True,
            only="cost_events,edge_tasks",
            selected=["cost_events", "edge_tasks"],
            limit=2,
            failed=False,
        )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["totals"], {"seen": 2, "written": 1, "skipped": 1})
        self.assertEqual(payload["results"][0]["source"], "cost_events")
        self.assertEqual(payload["schema_expected"], backfill.operational_db.SCHEMA_VERSION)

    def test_json_mode_reports_missing_db_as_structured_error(self):
        stdout = StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(stdout):
            code = backfill.main(["--json", "--only", "cost_events"])

        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertFalse(payload["db_configured"])
        self.assertIn("RED_OPERATIONAL_DB_URL", payload["error"])


if __name__ == "__main__":
    unittest.main()
