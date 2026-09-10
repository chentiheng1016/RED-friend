"""Tests for sync_config — mutator semantics + cross-process race.

Previously all mutators were the textbook load → mutate → save lost-update
pattern. After the locked_json refactor, multiple concurrent
add_drive_folder calls all survive.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")


def _concurrent_add_worker(config_file: str, folder_id: str) -> None:
    """Worker: import fresh, point _CONFIG_FILE at our temp, add a folder."""
    from agent_core.ingest import sync_config as sc
    sc._CONFIG_FILE = config_file
    time.sleep(0.01)  # widen the race window
    sc.add_drive_folder(folder_id)


class SyncConfigBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.path = os.path.join(self.tmpdir, "rag_sync_targets.json")
        from agent_core.ingest import sync_config as sc
        self._patcher = mock.patch.object(sc, "_CONFIG_FILE", self.path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_drive_folder_idempotent(self):
        from agent_core.ingest import sync_config as sc
        sc.add_drive_folder("fid-1")
        sc.add_drive_folder("fid-1")  # second add must not duplicate
        t = sc.load_targets()
        self.assertEqual(t["drive_folder_ids"], ["fid-1"])

    def test_add_drive_folder_rejects_empty(self):
        from agent_core.ingest import sync_config as sc
        with self.assertRaises(ValueError):
            sc.add_drive_folder("   ")

    def test_remove_drive_folder_also_clears_recursive_flag(self):
        from agent_core.ingest import sync_config as sc
        sc.enable_recursive_folder("fid-1")
        sc.remove_drive_folder("fid-1")
        t = sc.load_targets()
        self.assertNotIn("fid-1", t["drive_folder_ids"])
        self.assertNotIn("fid-1", t["recursive_folder_ids"])

    def test_enable_recursive_adds_to_both_lists(self):
        from agent_core.ingest import sync_config as sc
        sc.enable_recursive_folder("fid-1")
        t = sc.load_targets()
        self.assertIn("fid-1", t["drive_folder_ids"])
        self.assertIn("fid-1", t["recursive_folder_ids"])

    def test_enable_then_disable_all_drives(self):
        from agent_core.ingest import sync_config as sc
        sc.enable_all_drives()
        self.assertTrue(sc.load_targets()["all_drives"])
        sc.disable_all_drives()
        self.assertFalse(sc.load_targets()["all_drives"])


class SyncConfigConcurrencyTests(unittest.TestCase):
    """The lost-update fix: 6 concurrent add_drive_folder calls all persist."""

    def test_concurrent_adds_all_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "rag_sync_targets.json")
            ctx = mp.get_context("spawn")
            folder_ids = [f"folder-{i}" for i in range(6)]
            procs = [
                ctx.Process(target=_concurrent_add_worker, args=(path, fid))
                for fid in folder_ids
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=15)
                self.assertEqual(p.exitcode, 0, f"worker crashed: {p}")
            with open(path) as f:
                targets = json.load(f)
            self.assertEqual(
                sorted(targets["drive_folder_ids"]), sorted(folder_ids),
                f"lost update! expected {sorted(folder_ids)}, got "
                f"{sorted(targets['drive_folder_ids'])}"
            )


if __name__ == "__main__":
    unittest.main()
