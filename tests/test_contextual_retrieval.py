"""Tests for Phase 1 Contextual Retrieval (design: docs/contextual_retrieval_design.md).

三塊：
  1. ChunkTextContextTests — 三個 sync 的 _chunk_text 外掛式前綴：flag 關逐字沿用
     舊 [標題] 行為、flag 開（context 有值）payload 固定 CHUNK_SIZE 不縮，且 chunk
     邊界與 context 長度無關（保證重嵌時 __c{i} 對齊）。
  2. ContextualizeHelperTests — gen_doc_context / ctx_ver_satisfied 各分支。
  3. ContextVersionGateTests — sync_file 整合：content-hash gate 的 ctx_ver 閘（flag
     關忽略 ctx_ver 照舊 skip、flag 開落後版本強制重嵌並寫入新 context 前綴 + ctx_ver）。
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _hash16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


class ChunkTextContextTests(unittest.TestCase):
    """_chunk_text 是純函式、不讀 env——只看 context 參數決定前綴模式。"""

    def _modules(self):
        from agent_core.ingest import chat_sync, drive_sync, gmail_sync

        return drive_sync, gmail_sync, chat_sync

    def test_flag_off_verbatim_title_prefix_and_shrunk_payload(self):
        """context="" → 逐字沿用今日行為：[標題] 前綴、payload 扣前綴長，
        第一個 chunk 總長恰為 CHUNK_SIZE。"""
        drive_sync, _, _ = self._modules()
        text = "x" * 2000
        chunks = drive_sync._chunk_text(text, title="MyDoc")  # context 預設 ""
        prefix = "[MyDoc] "
        self.assertTrue(chunks and all(c.startswith(prefix) for c in chunks))
        # payload = CHUNK_SIZE - len(prefix)；前綴 + payload = CHUNK_SIZE
        self.assertEqual(len(chunks[0]), drive_sync.CHUNK_SIZE)

    def test_context_mode_full_payload_not_shrunk(self):
        """context 有值 → 前綴外掛，payload 維持整個 CHUNK_SIZE（不扣前綴）。"""
        drive_sync, _, _ = self._modules()
        text = "x" * 2000
        chunks = drive_sync._chunk_text(text, title="MyDoc", context="脈絡")
        prefix = "脈絡\n\n"
        self.assertTrue(all(c.startswith(prefix) for c in chunks))
        self.assertEqual(len(chunks[0]), len(prefix) + drive_sync.CHUNK_SIZE)

    def test_context_boundaries_independent_of_context_length(self):
        """關鍵不變性：context 模式下 chunk 邊界只由正文決定，與 context 長短無關
        → 不同 ctx_ver（不同 context 文字）產生相同 chunk 數與相同 payload 切分，
        重嵌時 __c{i} 對齊、upsert 乾淨覆蓋（設計 §3.2）。三個 sync 一致。"""
        for mod in self._modules():
            text = "字" * 2000
            short_ctx, long_ctx = "短", "非常冗長的文件脈絡描述" * 4
            c_short = mod._chunk_text(text, context=short_ctx)
            c_long = mod._chunk_text(text, context=long_ctx)
            self.assertEqual(
                len(c_short), len(c_long),
                f"{mod.__name__}: chunk 數應與 context 長度無關",
            )
            payloads_short = [c[len(short_ctx) + 2:] for c in c_short]  # +2 = "\n\n"
            payloads_long = [c[len(long_ctx) + 2:] for c in c_long]
            self.assertEqual(
                payloads_short, payloads_long,
                f"{mod.__name__}: 去前綴後 payload 切分應完全相同",
            )

    def test_empty_text_returns_empty_both_modes(self):
        for mod in self._modules():
            self.assertEqual(mod._chunk_text("", context="x"), [])
            self.assertEqual(mod._chunk_text("   \n\n  ", context="x"), [])


class ContextualizeHelperTests(unittest.TestCase):
    def setUp(self):
        from agent_core.ingest import contextualize

        self.contextualize = contextualize

    def test_disabled_returns_empty(self):
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "0"}):
            self.assertEqual(self.contextualize.gen_doc_context("some text", "t"), "")

    def test_enabled_empty_text_returns_empty(self):
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "1"}):
            self.assertEqual(self.contextualize.gen_doc_context("   ", "t"), "")
            self.assertEqual(self.contextualize.gen_doc_context("", "t"), "")

    def test_enabled_generates_and_normalizes(self):
        fake = mock.Mock()
        fake.text = "  `這是\nABC 客戶的\n合約`  "
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "1"}), \
             mock.patch(
                 "agent_core.gemini_client._gemini_generate", return_value=fake
             ):
            out = self.contextualize.gen_doc_context("doc body", "ABC合約")
        # 壓成單行、去 markdown 圍欄殘留
        self.assertEqual(out, "這是 ABC 客戶的 合約")

    def test_enabled_failure_falls_back_to_empty(self):
        """生成失敗（LLM 例外）一律回 ""——呼叫端 fallback 回 [標題]，不阻塞夜跑。"""
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "1"}), \
             mock.patch(
                 "agent_core.gemini_client._gemini_generate",
                 side_effect=RuntimeError("boom"),
             ):
            self.assertEqual(self.contextualize.gen_doc_context("body", "t"), "")

    def test_output_capped(self):
        fake = mock.Mock()
        fake.text = "很長" * 500  # 遠超 _MAX_CONTEXT_CHARS
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "1"}), \
             mock.patch(
                 "agent_core.gemini_client._gemini_generate", return_value=fake
             ):
            out = self.contextualize.gen_doc_context("body", "t")
        self.assertLessEqual(len(out), self.contextualize._MAX_CONTEXT_CHARS)

    def test_ctx_ver_satisfied_flag_off_always_true(self):
        """flag 關：一律 True——維持今日行為，絕不因 ctx_ver 觸發重嵌。"""
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "0"}):
            self.assertTrue(self.contextualize.ctx_ver_satisfied({}))
            self.assertTrue(self.contextualize.ctx_ver_satisfied({"ctx_ver": 0}))

    def test_ctx_ver_satisfied_flag_on(self):
        ver = self.contextualize.CONTEXTUAL_VER
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "1"}):
            self.assertFalse(self.contextualize.ctx_ver_satisfied({}))  # 缺 = 0
            self.assertFalse(self.contextualize.ctx_ver_satisfied({"ctx_ver": 0}))
            self.assertTrue(self.contextualize.ctx_ver_satisfied({"ctx_ver": ver}))
            self.assertTrue(
                self.contextualize.ctx_ver_satisfied({"ctx_ver": ver + 1})
            )

    def test_ctx_ver_satisfied_malformed_value(self):
        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "1"}):
            self.assertFalse(
                self.contextualize.ctx_ver_satisfied({"ctx_ver": "not-an-int"})
            )


class ContextVersionGateTests(unittest.TestCase):
    """sync_file 整合：content-hash gate 的 ctx_ver 閘。metadata 刻意不含
    modified_time，讓 modtime gate fall through 到 content-hash gate（沿用
    test_drive_sync_content_hash 的手法）。"""

    def setUp(self):
        from agent_core.ingest import drive_sync

        self._skip_state_tmp = tempfile.TemporaryDirectory()
        self._skip_state_patch = mock.patch.object(
            drive_sync,
            "_SKIP_STATE_FILE",
            os.path.join(self._skip_state_tmp.name, "drive_sync_skip_state.json"),
        )
        self._skip_state_patch.start()
        drive_sync._SKIP_STATE_CACHE = None

    def tearDown(self):
        from agent_core.ingest import drive_sync

        drive_sync._SKIP_STATE_CACHE = None
        self._skip_state_patch.stop()
        self._skip_state_tmp.cleanup()

    @staticmethod
    def _drive_service_for(text: str, mime: str = "text/plain"):
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid", "name": "x.txt", "mimeType": mime, "parents": ["sub1"],
        }
        media_req = mock.MagicMock()
        media_req.execute.return_value = text.encode("utf-8")
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        files_obj.get_media.return_value = media_req
        service = mock.MagicMock()
        service.files.return_value = files_obj
        return service

    def _store_with(self, body: str, extra_meta: dict):
        store = mock.MagicMock()
        meta = {
            "synced_at": "2026-05-06T08:00:00+00:00",
            "folder_id": "sub1",
            "drive_id": "0AABCDEF",
            "content_hash": _hash16(body),  # 內容相同
            "title": "x.txt",  # 與 _drive_service_for 的 name 一致
        }
        meta.update(extra_meta)
        store.get_doc_metadata.return_value = meta
        store.find_duplicate_doc_id.return_value = None  # 不觸發 dedup gate
        return store

    def _run_sync(self, store, service, env):
        from agent_core.ingest import drive_sync

        with mock.patch.dict(os.environ, env), \
             mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            return drive_sync.sync_file(
                "fid", folder_id="sub1", drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

    def test_flag_off_ignores_stale_ctx_ver_and_skips(self):
        """flag 關 + 內容相同 + 無 ctx_ver → 照舊 content_unchanged skip，
        不因 ctx_ver 落後而重嵌（今日行為零改變）。"""
        body = "保固期 12 個月，自簽收日起計。"
        store = self._store_with(body, {})  # 無 ctx_ver
        service = self._drive_service_for(body)
        r = self._run_sync(store, service, {"RAG_CONTEXTUAL_RETRIEVAL": "0"})
        self.assertEqual(r["reason"], "content_unchanged")
        store.upsert_batch.assert_not_called()

    def test_flag_on_current_ctx_ver_still_skips(self):
        """flag 開 + 內容相同 + ctx_ver 達標 → 仍 skip（不重複做白工）。"""
        from agent_core.ingest import contextualize

        body = "保固期 12 個月，自簽收日起計。"
        store = self._store_with(body, {"ctx_ver": contextualize.CONTEXTUAL_VER})
        service = self._drive_service_for(body)
        r = self._run_sync(store, service, {"RAG_CONTEXTUAL_RETRIEVAL": "1"})
        self.assertEqual(r["reason"], "content_unchanged")
        store.upsert_batch.assert_not_called()

    def test_flag_on_stale_ctx_ver_forces_reembed_with_context(self):
        """flag 開 + 內容相同 + ctx_ver 落後 → 繞過 fast-skip 重嵌：chunk 帶新
        LLM context 前綴、metadata 寫入 ctx_ver=CONTEXTUAL_VER。"""
        from agent_core.ingest import contextualize

        body = "保固期 12 個月，自簽收日起計。"
        store = self._store_with(body, {})  # 無 ctx_ver = 0 < CONTEXTUAL_VER
        service = self._drive_service_for(body)
        fake = mock.Mock()
        fake.text = "ABC 客戶合約的保固條款"

        from agent_core.ingest import drive_sync

        with mock.patch.dict(os.environ, {"RAG_CONTEXTUAL_RETRIEVAL": "1"}), \
             mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch(
                 "agent_core.gemini_client._gemini_generate", return_value=fake
             ):
            r = drive_sync.sync_file(
                "fid", folder_id="sub1", drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertNotIn(r.get("reason"), ("content_unchanged", "unchanged"))
        store.upsert_batch.assert_called_once()
        ids, docs, metas = store.upsert_batch.call_args.args[:3]
        self.assertTrue(docs[0].startswith("ABC 客戶合約的保固條款\n\n"))
        self.assertEqual(metas[0]["ctx_ver"], contextualize.CONTEXTUAL_VER)


if __name__ == "__main__":
    unittest.main()
