"""_purge_absent_docs 防呆閘測試：擋 Drive 短回清單造成的 purge 誤刪（2026-06-28 倉庫事故）。"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_core.ingest import drive_sync


class PurgeGuardTests(unittest.TestCase):
    def _run(self, indexed, current, complete=True):
        with mock.patch.object(drive_sync, "_delete_removed_docs") as dele, \
             mock.patch.object(drive_sync, "_PURGE_MAX_FRACTION", 0.5), \
             mock.patch.object(drive_sync, "_PURGE_MIN_ABS", 50):
            removed = drive_sync._purge_absent_docs(
                object(), "test", set(indexed), set(current), len(set(current)), complete,
            )
        return removed, dele

    def test_normal_small_purge_deletes(self):
        # 100 indexed, 95 listed → 刪 5（< 50 絕對）→ 正常刪
        removed, dele = self._run(range(100), range(95))
        self.assertEqual(len(removed), 5)
        dele.assert_called_once()

    def test_suspicious_mass_purge_skipped(self):
        # 1000 indexed, 100 listed → 刪 900（>50% 且 ≥50）→ 跳過、不刪
        removed, dele = self._run(range(1000), range(100))
        self.assertEqual(removed, set())
        dele.assert_not_called()

    def test_倉庫_like_case_skipped(self):
        # 倉庫情境：~3998 indexed、短回 ~1098 listed → 刪 ~2900 → 跳過
        removed, dele = self._run(range(3998), range(1098))
        self.assertEqual(removed, set())
        dele.assert_not_called()

    def test_incomplete_listing_skips(self):
        removed, dele = self._run(range(100), range(10), complete=False)
        self.assertEqual(removed, set())
        dele.assert_not_called()

    def test_small_drive_high_fraction_still_deletes(self):
        # 10 indexed, 2 listed → 刪 8（>50% 但 < 50 絕對）→ 仍刪（小硬碟真刪不擋）
        removed, dele = self._run(range(10), range(2))
        self.assertEqual(len(removed), 8)
        dele.assert_called_once()

    def test_empty_indexed_no_error(self):
        removed, dele = self._run([], range(5))
        self.assertEqual(removed, set())
        dele.assert_called_once()  # 空 indexed → _delete_removed_docs(空) no-op


if __name__ == "__main__":
    unittest.main()
