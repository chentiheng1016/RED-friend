from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import rehearse_operational_db as rehearse


class RehearseOperationalDBTests(unittest.TestCase):
    def test_main_json_summary_uses_machine_readable_sections(self):
        dry_payload = {
            "status": "ok",
            "dry_run": True,
            "results": [{"source": "cost_events", "status": "ok", "seen": 2, "written": 0}],
        }
        write_payload = {
            "status": "ok",
            "dry_run": False,
            "results": [{"source": "cost_events", "status": "ok", "seen": 2, "written": 2}],
        }
        calls: list[list[str]] = []

        def fake_run(cmd, *, env=None, quiet=False, capture=False):
            del env, quiet
            calls.append([str(part) for part in cmd])
            command_text = " ".join(str(part) for part in cmd)
            if "migrate_operational_db.py" in command_text:
                return "RED operational DB schema is ready at version 16\n"
            if "backfill_operational_db.py" in command_text and "--dry-run" in cmd:
                return json.dumps(dry_payload) if capture else ""
            if "backfill_operational_db.py" in command_text:
                return json.dumps(write_payload) if capture else ""
            return ""

        with tempfile.TemporaryDirectory(prefix="red_rehearse_test_") as tmp:
            tmp_root = str(Path(tmp) / "cluster")
            with mock.patch.object(rehearse, "_find_postgres_bin", return_value=Path("/tmp/pg/bin")), \
                    mock.patch.object(rehearse, "_free_port", return_value=6543), \
                    mock.patch.object(rehearse.tempfile, "mkdtemp", return_value=tmp_root), \
                    mock.patch.object(rehearse, "_run", side_effect=fake_run), \
                    mock.patch.object(
                        rehearse,
                        "_health_report",
                        return_value={"schema_ok": True, "enabled_backend_count": 16, "issues": []},
                    ), \
                    mock.patch.object(
                        rehearse,
                        "_table_counts",
                        return_value={
                            "schema_migrations": {"max_version": 16, "rows": 1},
                            "table_count": 2,
                            "tables": {"red_backfill_events": 2},
                        },
                    ), \
                    mock.patch.object(rehearse.subprocess, "run"):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = rehearse.main([
                        "--json",
                        "--dry-run-limit",
                        "2",
                        "--write-limit",
                        "2",
                    ])

        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["port"], 6543)
        self.assertEqual(summary["migrate"]["stdout"], "RED operational DB schema is ready at version 16")
        self.assertEqual(summary["health"]["enabled_backend_count"], 16)
        self.assertEqual(summary["dry_run"]["results"][0]["seen"], 2)
        self.assertEqual(summary["write_backfill"]["results"][0]["written"], 2)
        self.assertEqual(summary["table_counts"]["tables"]["red_backfill_events"], 2)
        self.assertFalse(stdout.getvalue().startswith("postgres_bin="))
        self.assertTrue(any("createdb" in item for call in calls for item in call))


if __name__ == "__main__":
    unittest.main()
