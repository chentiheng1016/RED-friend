"""operation_sops：影片 SOP 寫入 + 查詢工具的單元測試。"""
import unittest
from unittest import mock

from agent_core import operation_sops as ops


class IngestTests(unittest.TestCase):
    def _fake_access(self):
        return {"rag_source": "sop", "owner_color": "red",
                "department": "倉庫", "access_red": True}

    def test_chunks_and_upserts_with_correct_metadata(self):
        store = mock.MagicMock()
        long_text = "步驟一：開啟收櫃資料建立功能，勾選資料並執行入庫。" * 40
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store), \
                mock.patch.object(ops, "metadata_access_fields",
                                  return_value=self._fake_access()):
            r = ops.ingest_operation_sop("vid123", "收櫃資料建立.mp4",
                                         long_text, department="倉庫")
        self.assertTrue(r["ok"])
        self.assertEqual(r["doc_id"], "sop_vid123")
        self.assertGreater(r["chunks"], 1)  # 夠長 → 多 chunk
        store.upsert_batch.assert_called_once()
        # 迴歸：重灌 chunks 變少時舊高編號 chunk 會殘留 — 每次 ingest 都要清尾
        store.delete_stale_chunks.assert_called_once_with(
            "sop_vid123", store.upsert_batch.call_args[0][0].__len__())
        ids, docs, metas = store.upsert_batch.call_args[0]
        self.assertEqual(len(ids), len(docs))
        self.assertEqual(len(ids), len(metas))
        self.assertTrue(ids[0].endswith("__c0"))
        self.assertTrue(all(m["doc_id"] == "sop_vid123" for m in metas))
        self.assertTrue(all(m["access_red"] is True for m in metas))   # owner 看得到
        self.assertTrue(all(m["video_id"] == "vid123" for m in metas))  # 連回來源影片
        self.assertTrue(all(m["sync_complete"] is True for m in metas))  # 不被當未完成清掉
        self.assertEqual(metas[0]["chunk_index"], 0)

    def test_uses_stable_doc_id_for_idempotent_reingest(self):
        store = mock.MagicMock()
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store), \
                mock.patch.object(ops, "metadata_access_fields",
                                  return_value=self._fake_access()):
            r1 = ops.ingest_operation_sop("vidX", "a.mp4", "步驟。" * 30)
            r2 = ops.ingest_operation_sop("vidX", "a.mp4", "步驟。" * 30)
        # 同影片 → 同 doc_id 與同 chunk_id → 重跑是覆蓋不是堆積
        self.assertEqual(r1["doc_id"], r2["doc_id"], "sop_vidX")
        ids1 = store.upsert_batch.call_args_list[0][0][0]
        ids2 = store.upsert_batch.call_args_list[1][0][0]
        self.assertEqual(ids1, ids2)

    def test_rejects_empty(self):
        self.assertFalse(ops.ingest_operation_sop("", "", "")["ok"])
        self.assertFalse(ops.ingest_operation_sop("v", "n", "   ")["ok"])
        self.assertFalse(ops.ingest_operation_sop("", "n", "text")["ok"])


class SearchTests(unittest.TestCase):
    def test_empty_collection_message(self):
        store = mock.MagicMock()
        store.count.return_value = 0
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            out = ops.search_operation_sops("收櫃怎麼建")
        self.assertIn("還是空的", out)

    def test_formats_hits_with_provenance(self):
        store = mock.MagicMock()
        store.count.return_value = 10
        store.query.return_value = [{
            "text": "步驟一：在 FTE_570 畫面勾選資料列，點收櫃入庫(F3)。",
            "metadata": {"title": "[操作SOP] 收櫃資料建立.mp4", "department": "倉庫",
                         "video_id": "vid123", "chunk_index": 1},
            "distance": 0.2,
        }]
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store), \
                mock.patch.object(ops, "access_where", return_value=None), \
                mock.patch.object(ops, "current_request_caller", return_value="red"), \
                mock.patch.object(ops, "log_rag_access_event"), \
                mock.patch.object(ops, "current_request_trace_id", return_value=""):
            out = ops.search_operation_sops("收櫃怎麼建", k=3)
        self.assertIn("收櫃資料建立", out)
        self.assertIn("FTE_570", out)        # 螢幕細節有進到可查內容
        self.assertIn("sim=0.80", out)        # distance 0.2 → similarity 0.80
        self.assertIn("部門=倉庫", out)
        self.assertIn("影片id=vid123", out)

    def test_no_hits_message(self):
        store = mock.MagicMock()
        store.count.return_value = 10
        store.query.return_value = []
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store), \
                mock.patch.object(ops, "access_where", return_value=None), \
                mock.patch.object(ops, "current_request_caller", return_value="red"), \
                mock.patch.object(ops, "log_rag_access_event"), \
                mock.patch.object(ops, "current_request_trace_id", return_value=""):
            out = ops.search_operation_sops("不存在的東西")
        self.assertIn("沒找到", out)

    def test_empty_query(self):
        self.assertIn("不能為空", ops.search_operation_sops(""))


class ToolRegistrationTests(unittest.TestCase):
    def test_registered_in_catalog(self):
        from agent_core import tool_registry_catalog as cat
        names = {getattr(t, "__name__", "") for t in cat.BASE_BUILTIN_TOOLS}
        self.assertIn("search_operation_sops", names)


if __name__ == "__main__":
    unittest.main()
