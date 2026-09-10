"""Gmail RAG sync incremental skip tests."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


_TEST_MAILBOX = "boss@example.com"


def _gmail_service_for_list(threads):
    list_req = mock.MagicMock()
    list_req.execute.return_value = {"threads": threads}
    threads_obj = mock.MagicMock()
    threads_obj.list.return_value = list_req
    # _mailbox_email() calls users().getProfile(userId="me").execute() —
    # return a real dict so it produces a usable string instead of a
    # MagicMock that would mismatch the access_fields check.
    profile_req = mock.MagicMock()
    profile_req.execute.return_value = {"emailAddress": _TEST_MAILBOX}
    users_obj = mock.MagicMock()
    users_obj.threads.return_value = threads_obj
    users_obj.getProfile.return_value = profile_req
    service = mock.MagicMock()
    service.users.return_value = users_obj
    return service, threads_obj


def _access_fields_for_mailbox(mailbox_email: str = _TEST_MAILBOX) -> dict:
    """Build the access fields gmail_sync stamps on chunks, so test fixtures
    match what the new rag_gateway ACL check expects to see."""
    from agent_core.rag_gateway import metadata_access_fields
    return metadata_access_fields("gmail", mailbox_email=mailbox_email)


class GmailIncrementalSyncTests(unittest.TestCase):
    def test_sync_query_skips_thread_when_history_id_matches(self):
        from agent_core.ingest import gmail_sync

        service, threads_obj = _gmail_service_for_list([
            {"id": "t1", "historyId": "h1"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {
            "t1": {"history_id": "h1", "synced_at": "old",
                   **_access_fields_for_mailbox()},
        }

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped"], 1)
        threads_obj.get.assert_not_called()
        store.upsert_batch.assert_not_called()

    def test_prefetch_failure_falls_back_to_per_thread_metadata(self):
        """bulk_get_doc_metadata 炸掉 ≠ 整信箱重抓重 embed：_prepare_thread 要
        退回 store.get_doc_metadata 逐封判 unchanged（比照 drive_sync 的逐檔
        fallback）。"""
        from agent_core.ingest import gmail_sync

        service, threads_obj = _gmail_service_for_list([
            {"id": "t1", "historyId": "h1"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.side_effect = RuntimeError("chroma hiccup")
        store.get_doc_metadata.return_value = {
            "history_id": "h1", "synced_at": "old",
            **_access_fields_for_mailbox(),
        }

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["synced"], 0)
        store.get_doc_metadata.assert_called_once_with("t1")
        threads_obj.get.assert_not_called()   # 沒重抓 thread 本體
        store.upsert_batch.assert_not_called()  # 沒重 embed

    def test_prefetch_failure_still_resyncs_changed_thread(self):
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([
            {"id": "t1", "historyId": "h2"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.side_effect = RuntimeError("chroma hiccup")
        store.get_doc_metadata.return_value = {
            "history_id": "h1", "synced_at": "old",  # 舊 → 需重同步
            **_access_fields_for_mailbox(),
        }

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": "h2",
                 }),
             ):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["synced"], 1)
        store.upsert_batch.assert_called_once()

    def test_per_thread_metadata_lookup_failure_degrades_to_resync(self):
        """fallback 的 get_doc_metadata 也炸 → 該 thread 照舊重同步（不摧毀
        整輪、也不 raise）。"""
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([
            {"id": "t1", "historyId": "h1"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.side_effect = RuntimeError("chroma hiccup")
        store.get_doc_metadata.side_effect = RuntimeError("still down")

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": "h1",
                 }),
             ):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["synced"], 1)
        store.upsert_batch.assert_called_once()

    def test_threads_list_page_size_capped_at_api_max(self):
        """threads.list 一頁最多 500（API 上限）；之前 100 一頁要多打 5 倍
        list RPC。"""
        from agent_core.ingest import gmail_sync

        service, threads_obj = _gmail_service_for_list([])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {}

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service):
            gmail_sync.sync_query("newer_than:180d", max_threads=1000)

        kwargs = threads_obj.list.call_args.kwargs
        self.assertEqual(kwargs["maxResults"], 500)

    def test_sync_query_reindexes_thread_when_history_id_changes(self):
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([
            {"id": "t1", "historyId": "h2"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {
            "t1": {"history_id": "h1", "synced_at": "old",
                   **_access_fields_for_mailbox()},
        }

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": "h2",
                 }),
             ):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["synced"], 1)
        store.upsert_batch.assert_called_once()
        metas = store.upsert_batch.call_args.args[2]
        self.assertEqual(metas[0]["history_id"], "h2")
        self.assertTrue(store.delete_stale_chunks.called)


class _FakeStore:
    """In-memory store mimicking vector_store 的關鍵語意，讓測試能重現
    「upsert_batch 第 k 子批失敗、前 k-1 批已落地」的半寫入狀態：

    - upsert_batch 內部 32 筆一子批，可注入第 k 子批丟例外（前面子批已落地）。
    - bulk_get_doc_metadata 聚合 chunk metas：metas[0]（最低 id = chunk 0）
      提供 history_id 等欄位；sync_complete 缺省=True、任一 chunk False →
      整 doc False（對齊 vector_store._reduce_doc_metadata）。
    - mark_doc_sync_complete 翻正該 doc 每個 chunk 的 sync_complete。
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}  # chunk_id -> metadata（插入序 = chunk 序）
        self.fail_at_subbatch: int | None = None  # 1-based；炸一次後自動復原
        self.upsert_calls = 0
        self.mark_complete_calls: list[str] = []

    def upsert_batch(self, ids, docs, metas, _batch_size=32):
        self.upsert_calls += 1
        for b, i in enumerate(range(0, len(ids), _batch_size), start=1):
            if self.fail_at_subbatch is not None and b >= self.fail_at_subbatch:
                self.fail_at_subbatch = None
                raise RuntimeError("ChromaOpTimeout: sub-batch write timed out")
            for cid, meta in zip(ids[i:i + _batch_size], metas[i:i + _batch_size]):
                self.rows[cid] = dict(meta)

    def bulk_get_doc_metadata(self, doc_ids):
        by_doc: dict[str, list[dict]] = {}
        for meta in self.rows.values():
            did = meta.get("doc_id")
            if did in doc_ids:
                by_doc.setdefault(did, []).append(meta)
        out = {}
        for did, metas in by_doc.items():
            reduced = dict(metas[0])
            reduced["sync_complete"] = all(
                m.get("sync_complete", True) for m in metas
            )
            out[did] = reduced
        return out

    def delete_stale_chunks(self, doc_id, chunk_count):
        stale = [
            cid for cid, m in self.rows.items()
            if m.get("doc_id") == doc_id
            and int(m.get("chunk_index", 0)) >= chunk_count
        ]
        for cid in stale:
            del self.rows[cid]

    def mark_doc_sync_complete(self, doc_id):
        self.mark_complete_calls.append(doc_id)
        for m in self.rows.values():
            if m.get("doc_id") == doc_id:
                m["sync_complete"] = True


class GmailSyncCompleteTests(unittest.TestCase):
    """sync_complete 兩段式標記：半寫入 thread 下一輪必重抽（不 fast-skip）、
    舊資料（無欄位）照常 skip、正常路徑 mark complete 後照常 skip。"""

    @staticmethod
    def _forty_chunks(text, subject="", context=""):
        return [f"[{subject}] chunk {i}" for i in range(40)]

    def _run_sync(self, store, history_id="h2"):
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([
            {"id": "t1", "historyId": history_id},
        ])
        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(gmail_sync, "_chunk_text", self._forty_chunks), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": history_id,
                 }),
             ):
            return gmail_sync.sync_query("newer_than:180d", max_threads=1)

    def test_flush_midway_failure_not_skipped_next_run_then_self_heals(self):
        """(a) flush 中途失敗（第二子批丟例外、第一子批已落地）→ 半寫入
        thread 前段 chunk 帶新 history_id，但 sync_complete=False 擋住 skip
        閘；下一輪重抽、成功後 mark complete；第三輪才照常 skip。"""
        store = _FakeStore()

        # 第一輪：40 chunks、第二子批(32..39)炸 → 只有 chunk 0-31 落地。
        store.fail_at_subbatch = 2
        result = self._run_sync(store)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("t1__c0", store.rows)          # 前段已落地…
        self.assertNotIn("t1__c39", store.rows)      # …後段沒寫入
        self.assertEqual(store.rows["t1__c0"]["history_id"], "h2")  # 且帶新 history_id
        self.assertIs(store.rows["t1__c0"]["sync_complete"], False)
        self.assertEqual(store.mark_complete_calls, [])  # 失敗邊界後不得 mark

        # 第二輪：history_id 沒變（h2==h2），舊碼會誤判 unchanged 跳過；
        # 新碼靠 sync_complete=False 拒絕 skip、重抽自癒。
        result = self._run_sync(store)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len([m for m in store.rows.values()
                              if m.get("doc_id") == "t1"]), 40)
        self.assertEqual(store.mark_complete_calls, ["t1"])
        self.assertTrue(all(m["sync_complete"] is True for m in store.rows.values()))

        # 第三輪：已 complete + history_id 相同 → 照常 skip、不再重 embed。
        upserts_before = store.upsert_calls
        result = self._run_sync(store)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(store.upsert_calls, upserts_before)

    def test_legacy_metadata_without_sync_complete_field_still_skips(self):
        """(b) 舊資料沒有 sync_complete 欄位 → 缺省視為 complete、照常 skip
        （否則升級當晚 15 萬 threads 全量重抓）。"""
        from agent_core.ingest import gmail_sync

        service, threads_obj = _gmail_service_for_list([
            {"id": "t1", "historyId": "h1"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {
            "t1": {"history_id": "h1", "synced_at": "old",  # 無 sync_complete 欄位
                   **_access_fields_for_mailbox()},
        }

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["synced"], 0)
        threads_obj.get.assert_not_called()
        store.upsert_batch.assert_not_called()

    def test_incomplete_metadata_blocks_skip_on_prefetch_path(self):
        """prefetch 路徑：sync_complete=False（新碼寫的殘缺標記）→ 即使
        history_id 相同也不准 skip，重抽 + mark complete。"""
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([
            {"id": "t1", "historyId": "h1"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {
            "t1": {"history_id": "h1", "synced_at": "old",
                   "sync_complete": False,
                   **_access_fields_for_mailbox()},
        }

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": "h1",
                 }),
             ):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["synced"], 1)
        store.upsert_batch.assert_called_once()
        # 新寫入的 chunk 一律先標 False，等 finalize 才翻 True。
        metas = store.upsert_batch.call_args.args[2]
        self.assertTrue(all(m["sync_complete"] is False for m in metas))
        store.mark_doc_sync_complete.assert_called_once_with("t1")
        # 順序：upsert → delete_stale → mark_complete。
        names = [c[0] for c in store.method_calls
                 if c[0] in ("upsert_batch", "delete_stale_chunks",
                             "mark_doc_sync_complete")]
        self.assertEqual(
            names, ["upsert_batch", "delete_stale_chunks", "mark_doc_sync_complete"])

    def test_incomplete_metadata_blocks_skip_on_per_thread_fallback(self):
        """逐筆 fallback 路徑（bulk 炸掉走 get_doc_metadata）也要看
        sync_complete。"""
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([
            {"id": "t1", "historyId": "h1"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.side_effect = RuntimeError("chroma hiccup")
        store.get_doc_metadata.return_value = {
            "history_id": "h1", "synced_at": "old",
            "sync_complete": False,
            **_access_fields_for_mailbox(),
        }

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": "h1",
                 }),
             ):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["synced"], 1)
        store.upsert_batch.assert_called_once()
        store.mark_doc_sync_complete.assert_called_once_with("t1")

    def test_delete_stale_failure_leaves_thread_unmarked(self):
        """delete_stale 失敗 → 不得 mark complete（下一輪重抽修復，而不是
        fast-skip 一個 stale 尾巴沒清乾淨的 doc）；也不炸掉整輪。"""
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([
            {"id": "t1", "historyId": "h2"},
        ])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {}
        store.delete_stale_chunks.side_effect = RuntimeError("delete timeout")

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": "h2",
                 }),
             ):
            result = gmail_sync.sync_query("newer_than:180d", max_threads=1)

        self.assertEqual(result["synced"], 1)  # upsert 已落地，計 synced
        store.mark_doc_sync_complete.assert_not_called()

    def test_sync_thread_marks_complete_after_upsert_and_delete_stale(self):
        """單封入口 sync_thread 也要走兩段式，否則手動同步的 thread 永遠
        殘缺 → 每輪 sync_query 都重抓。"""
        from agent_core.ingest import gmail_sync

        service, _ = _gmail_service_for_list([])
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {}

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", return_value=service), \
             mock.patch.object(
                 gmail_sync,
                 "_thread_text",
                 return_value=("hello body", {
                     "subject": "Subject A",
                     "sender": "a@example.com",
                     "date": "today",
                     "history_id": "h9",
                 }),
             ):
            result = gmail_sync.sync_thread("t9")

        self.assertEqual(result["chunks"], 1)
        store.upsert_batch.assert_called_once()
        store.delete_stale_chunks.assert_called_once_with("t9", 1)
        store.mark_doc_sync_complete.assert_called_once_with("t9")


if __name__ == "__main__":
    unittest.main()
