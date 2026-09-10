"""Tests for runtime housekeeping."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_spec = importlib.util.spec_from_file_location(
    "housekeeping",
    os.path.join(_REPO_ROOT, "launchd", "scripts", "housekeeping.py"),
)
housekeeping = importlib.util.module_from_spec(_spec)
sys.modules["housekeeping"] = housekeeping
_spec.loader.exec_module(housekeeping)


class SmokeLogRetentionTests(unittest.TestCase):
    def test_prune_smoke_logs_deletes_old_json_records(self):
        old_runs_dir = housekeeping.RUNS_DIR
        with tempfile.TemporaryDirectory(prefix="red_runs_") as tmp:
            smoke_dir = Path(tmp) / "post_deploy_smoke"
            smoke_dir.mkdir()
            old_record = smoke_dir / "post_deploy_smoke-old.json"
            new_record = smoke_dir / "post_deploy_smoke-new.json"
            old_record.write_text("{}", encoding="utf-8")
            new_record.write_text("{}", encoding="utf-8")
            old_time = time.time() - 90 * 86400
            os.utime(old_record, (old_time, old_time))

            try:
                housekeeping.RUNS_DIR = tmp
                summary = housekeeping._prune_smoke_logs(days=60)
            finally:
                housekeeping.RUNS_DIR = old_runs_dir

            self.assertFalse(old_record.exists())
            self.assertTrue(new_record.exists())
            self.assertIn("刪除 1", summary)


if __name__ == "__main__":
    unittest.main()
