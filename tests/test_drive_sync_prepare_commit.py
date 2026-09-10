"""Phase 0 結構契約：sync_file 拆成 _prepare_file（讀/網路/CPU、不寫）+
_commit_file（主執行緒、唯一 writer）。

既有 test_drive_sync_* 已驗證端到端行為等價；這檔額外鎖定切分本身的不變量，
防未來在 prepare 誤加 store 寫（會破壞日後平行化的單 writer 保證），並釘住
_commit_file 的寫順序與 hard-quota 退路。
"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_core.ingest import drive_sync


class PrepareFileNoWriteTests(unittest.TestCase):
    """_prepare_file 只能讀，不能寫 store / skip-marker。"""

    def test_fast_skip_unchanged_returns_plan_without_writes(self):
        store = mock.MagicMock()
        existing = {
            "synced_at": "2026-06-26T00:00:00+00:00",
            "content_hash": "abc", "title": "t",
            "folder_id": "F", "drive_id": "D", "sync_complete": True,
            "modified_time": "2026-06-25",
        }
        with mock.patch.object(drive_sync, "_sync_complete_allows_skip", return_value=True), \
                mock.patch.object(drive_sync, "_stored_modified_time_matches", return_value=True), \
                mock.patch.object(drive_sync, "_stored_metadata_matches", return_value=True), \
                mock.patch.object(drive_sync, "metadata_access_matches", return_value=True), \
                mock.patch.object(drive_sync, "_record_skip_marker") as rec:
            plan = drive_sync._prepare_file(
                "fid", folder_id="F", drive_id="D", modified_time="2026-06-25",
                prefetched_metadata=existing, store=store,
            )
        self.assertIsInstance(plan, drive_sync._FilePlan)
        self.assertEqual(plan.result["reason"], "unchanged")
        # 乾淨 skip 不做任何 metadata 寫
        self.assertIsNone(plan.set_metadata)
        # prepare 絕不可寫
        store.set_doc_metadata_fields.assert_not_called()
        store.delete_by_doc_id.assert_not_called()
        store.upsert_batch.assert_not_called()
        rec.assert_not_called()

    def test_fast_skip_gate_consults_stored_modified_time(self):
        """watermark 換成等值比對後，gate 必須把「儲存的 modified_time」與
        listing 的 modifiedTime 一起交給 _stored_modified_time_matches。"""
        store = mock.MagicMock()
        existing = {
            "synced_at": "2026-06-26T00:00:00+00:00",
            "content_hash": "abc", "title": "t",
            "folder_id": "F", "drive_id": "D", "sync_complete": True,
            "modified_time": "2026-06-20",  # ≠ listing → 不可 fast-skip
        }
        with mock.patch.object(drive_sync, "_sync_complete_allows_skip", return_value=True), \
                mock.patch.object(drive_sync, "_stored_modified_time_matches",
                                  wraps=drive_sync._stored_modified_time_matches) as gate, \
                mock.patch.object(drive_sync, "_stored_metadata_matches", return_value=True), \
                mock.patch.object(drive_sync, "metadata_access_matches", return_value=True), \
                mock.patch.object(drive_sync, "_get_matching_skip_marker",
                                  return_value={"title": "t", "reason": "empty_text"}):
            plan = drive_sync._prepare_file(
                "fid", folder_id="F", drive_id="D", modified_time="2026-06-25",
                prefetched_metadata=existing, store=store,
            )
        gate.assert_called_once_with("2026-06-20", "2026-06-25")
        self.assertNotEqual(plan.result["reason"], "unchanged")

    def test_skip_marker_hit_returns_plan_without_writes(self):
        store = mock.MagicMock()
        existing = {
            "synced_at": "", "content_hash": "", "title": "",
            "folder_id": "", "drive_id": "", "sync_complete": True,
        }
        with mock.patch.object(drive_sync, "_get_matching_skip_marker",
                               return_value={"title": "x", "reason": "duplicate_content"}):
            plan = drive_sync._prepare_file(
                "fid", prefetched_metadata=existing, store=store,
            )
        self.assertEqual(plan.result["reason"], "duplicate_content")
        store.delete_by_doc_id.assert_not_called()
        store.upsert_batch.assert_not_called()


class CommitFileTests(unittest.TestCase):
    """_commit_file 依 _FilePlan 執行所有寫，順序對齊原 sync_file。"""

    def test_plain_skip_writes_nothing(self):
        store = mock.MagicMock()
        with mock.patch.object(drive_sync, "_record_skip_marker") as rec, \
                mock.patch.object(drive_sync, "_clear_skip_marker") as clr:
            out = drive_sync._commit_file(
                store, drive_sync._FilePlan("fid", {"file_id": "fid", "skipped": True,
                                                    "reason": "unchanged"}))
        self.assertEqual(out["reason"], "unchanged")
        store.delete_by_doc_id.assert_not_called()
        store.set_doc_metadata_fields.assert_not_called()
        store.upsert_batch.assert_not_called()
        rec.assert_not_called()
        clr.assert_not_called()

    def test_delete_plus_marker(self):
        store = mock.MagicMock()
        with mock.patch.object(drive_sync, "_record_skip_marker") as rec:
            out = drive_sync._commit_file(store, drive_sync._FilePlan(
                "fid", {"file_id": "fid", "skipped": True, "reason": "empty_text"},
                delete_doc=True, skip_marker={"reason": "empty_text", "title": "t"}))
        store.delete_by_doc_id.assert_called_once_with("fid")
        rec.assert_called_once_with("fid", reason="empty_text", title="t")
        self.assertEqual(out["reason"], "empty_text")

    def test_set_metadata_only(self):
        store = mock.MagicMock()
        drive_sync._commit_file(store, drive_sync._FilePlan(
            "fid", {"skipped": True}, set_metadata={"synced_at": "now"}))
        store.set_doc_metadata_fields.assert_called_once_with("fid", {"synced_at": "now"})
        store.upsert_batch.assert_not_called()

    def test_index_order_upsert_then_delete_stale_then_mark_then_clear(self):
        store = mock.MagicMock()
        calls = []
        store.upsert_batch.side_effect = lambda *a, **k: calls.append("upsert")
        store.delete_stale_chunks.side_effect = lambda *a, **k: calls.append("delete_stale")
        store.mark_doc_sync_complete.side_effect = lambda *a, **k: calls.append("mark")
        with mock.patch.object(drive_sync, "_clear_skip_marker",
                               side_effect=lambda *a: calls.append("clear")):
            out = drive_sync._commit_file(store, drive_sync._FilePlan(
                "fid", {"file_id": "fid", "title": "t", "chunks": 2}, title="t",
                upsert=(["fid__c0", "fid__c1"], ["a", "b"], [{}, {}], 2)))
        self.assertEqual(calls, ["upsert", "delete_stale", "mark", "clear"])
        store.delete_stale_chunks.assert_called_once_with("fid", 2)
        self.assertEqual(out["chunks"], 2)

    def test_index_hard_quota_returns_skip_and_skips_delete_stale(self):
        store = mock.MagicMock()
        store.upsert_batch.side_effect = drive_sync.GeminiHardQuotaError("quota gone")
        out = drive_sync._commit_file(store, drive_sync._FilePlan(
            "fid", {"file_id": "fid", "title": "t", "chunks": 2}, title="t",
            upsert=(["fid__c0"], ["a"], [{}], 1)))
        self.assertTrue(out["skipped"])
        self.assertIn("embedding_unavailable", out["reason"])
        self.assertEqual(out["title"], "t")
        store.delete_stale_chunks.assert_not_called()  # upsert 失敗不可往下走
        store.mark_doc_sync_complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
