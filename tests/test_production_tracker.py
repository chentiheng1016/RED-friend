"""Tests for production_tracker — append/list semantics and cross-process race.

This module had zero coverage before the locked_json refactor. The append
path is now exercised end-to-end (incl. multiprocess race), and the
shape-recovery branch is documented as a hard error so silent data loss
isn't possible.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")


def _concurrent_append_worker(data_dir: str, label: str) -> None:
    """Subprocess worker: import fresh + append one record."""
    import importlib
    os.environ["RED_DATA_DIR"] = data_dir  # unused — _STORE path is module-level
    # Patch the module-level _STORE because it's bound at import time from DATA_DIR.
    from agent_core.agents.gray_production import production_tracker
    importlib.reload(production_tracker)
    production_tracker._STORE = Path(data_dir) / "production_anomalies.json"
    time.sleep(0.01)  # widen the race window
    production_tracker.append_anomaly({"label": label})


class ProductionTrackerBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # Repoint the module's _STORE at our tmpdir.
        from agent_core.agents.gray_production import production_tracker as pt
        self._patcher = mock.patch.object(
            pt, "_STORE", Path(self.tmpdir) / "production_anomalies.json"
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_append_then_list_round_trips(self):
        from agent_core.agents.gray_production import production_tracker as pt
        pt.append_anomaly({"kind": "scrap", "qty": 3})
        records = pt.list_anomalies(recent_n=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "scrap")
        self.assertEqual(records[0]["qty"], 3)
        self.assertIn("logged_at", records[0])

    def test_list_clamps_recent_n_to_one_at_minimum(self):
        from agent_core.agents.gray_production import production_tracker as pt
        for i in range(5):
            pt.append_anomaly({"i": i})
        # recent_n=0 should clamp to 1, not return everything (negative-slice bug).
        self.assertEqual(len(pt.list_anomalies(recent_n=0)), 1)
        self.assertEqual(len(pt.list_anomalies(recent_n=-5)), 1)

    def test_list_clamps_recent_n_to_max(self):
        from agent_core.agents.gray_production import production_tracker as pt
        # Don't actually write 1000 records; just verify the clamp doesn't crash
        # and returns whatever exists, capped.
        for i in range(3):
            pt.append_anomaly({"i": i})
        self.assertEqual(len(pt.list_anomalies(recent_n=999999)), 3)

    def test_wrong_shape_file_raises_loudly(self):
        """If something writes a dict to the file (manual edit / bad migration),
        we must crash rather than silently lose new appends. The previous
        version's `records = []` rebind would silently drop the write."""
        from agent_core.agents.gray_production import production_tracker as pt
        pt._STORE.parent.mkdir(parents=True, exist_ok=True)
        pt._STORE.write_text('{"not": "a list"}', encoding="utf-8")
        with self.assertRaises(TypeError):
            pt.append_anomaly({"kind": "scrap"})


class ProductionTrackerConcurrencyTests(unittest.TestCase):
    """Cross-process: 8 subprocesses each append; lock must keep all 8."""

    def test_concurrent_appends_all_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = mp.get_context("spawn")
            procs = [
                ctx.Process(target=_concurrent_append_worker,
                            args=(tmpdir, f"event-{i}"))
                for i in range(8)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=15)
                self.assertEqual(p.exitcode, 0, f"worker crashed: {p}")
            with open(os.path.join(tmpdir, "production_anomalies.json")) as f:
                records = json.load(f)
            self.assertEqual(
                len(records), 8,
                f"lost update! expected 8, got {len(records)}: "
                f"{[r.get('label') for r in records]}"
            )


if __name__ == "__main__":
    unittest.main()
