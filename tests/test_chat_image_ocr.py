"""Tests for Google Chat image-attachment OCR enrichment (Phase 1).

Chat messages often carry ERP screenshots / report images; by default only the
filename placeholder is indexed (content unsearchable). enrich_messages_with_
image_ocr downloads the bytes and runs macOS Vision OCR (free) into
attachment['_ocr_text'], which _message_line then folds into the embedded line.

These tests pin: the gate defaults off, the OCR text surfaces in the message
line, non-image / failed / out-of-bounds attachments are skipped gracefully,
and the per-space cap holds — all without touching the real Google download
machinery or Vision (both patched).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── _message_line folds OCR text in ──────────────────────────────────

class MessageLineOcrTests(unittest.TestCase):
    def test_ocr_text_appears_in_line(self):
        from agent_core.ingest import chat_sync
        msg = {
            "createTime": "2026-06-30T01:00:00Z",
            "sender": {"displayName": "UserS"},
            "text": "",
            "attachment": [{
                "contentName": "截圖.png",
                "contentType": "image/png",
                "_ocr_text": "LOT 208 交期 2026-07-15 數量 1200",
            }],
        }
        line = chat_sync._message_line(msg)
        self.assertIn("附件OCR", line)
        self.assertIn("LOT 208 交期 2026-07-15", line)
        self.assertIn("截圖.png", line)

    def test_without_ocr_is_filename_only(self):
        from agent_core.ingest import chat_sync
        msg = {
            "createTime": "2026-06-30T01:00:00Z",
            "sender": {"displayName": "UserS"},
            "text": "",
            "attachment": [{"contentName": "x.png", "contentType": "image/png"}],
        }
        line = chat_sync._message_line(msg)
        self.assertIn("「附件：x.png」", line)
        self.assertNotIn("附件OCR", line)


# ── enrich_messages_with_image_ocr ───────────────────────────────────

class EnrichOcrTests(unittest.TestCase):
    def _img_msg(self, name="a.png", ct="image/png", res="res1"):
        return {"attachment": [{
            "contentName": name, "contentType": ct,
            "attachmentDataRef": {"resourceName": res},
        }]}

    def test_gate_off_by_default_does_nothing(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg()]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", False), \
             mock.patch.object(chat_sync, "_download_attachment_bytes") as dl:
            n = chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(n, 0)
        dl.assert_not_called()
        self.assertNotIn("_ocr_text", msgs[0]["attachment"][0])

    def test_gate_on_ocrs_image(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg()]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_download_attachment_bytes",
                               return_value=b"x" * 50_000), \
             mock.patch("agent_core.ingest.drive_sync._extract_image_vision",
                        return_value="OCR 出來的字"):
            n = chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(n, 1)
        self.assertEqual(msgs[0]["attachment"][0]["_ocr_text"], "OCR 出來的字")

    def test_non_image_attachment_skipped(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg(name="doc.pdf", ct="application/pdf")]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_download_attachment_bytes") as dl:
            n = chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(n, 0)
        dl.assert_not_called()

    def test_download_failure_is_swallowed(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg()]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_download_attachment_bytes",
                               side_effect=RuntimeError("boom")):
            n = chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(n, 0)
        self.assertNotIn("_ocr_text", msgs[0]["attachment"][0])

    def test_too_small_image_skipped(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg()]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_CHAT_IMG_MIN_BYTES", 5 * 1024), \
             mock.patch.object(chat_sync, "_download_attachment_bytes",
                               return_value=b"tiny"), \
             mock.patch("agent_core.ingest.drive_sync._extract_image_vision") as ocr:
            n = chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(n, 0)
        ocr.assert_not_called()

    def test_empty_ocr_text_not_stored(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg()]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_CHAT_IMAGE_CAPTION", False), \
             mock.patch.object(chat_sync, "_download_attachment_bytes",
                               return_value=b"x" * 50_000), \
             mock.patch("agent_core.ingest.drive_sync._extract_image_vision",
                        return_value=None):
            n = chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(n, 0)
        self.assertNotIn("_ocr_text", msgs[0]["attachment"][0])

    def test_per_space_cap_enforced(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg(res="r1"), self._img_msg(res="r2")]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_CHAT_IMG_OCR_PER_SPACE_CAP", 1), \
             mock.patch.object(chat_sync, "_download_attachment_bytes",
                               return_value=b"x" * 50_000), \
             mock.patch("agent_core.ingest.drive_sync._extract_image_vision",
                        return_value="text"):
            n = chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(n, 1)

    def test_ocr_text_capped(self):
        from agent_core.ingest import chat_sync
        msgs = [self._img_msg()]
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_CHAT_OCR_TEXT_CAP", 10), \
             mock.patch.object(chat_sync, "_download_attachment_bytes",
                               return_value=b"x" * 50_000), \
             mock.patch("agent_core.ingest.drive_sync._extract_image_vision",
                        return_value="A" * 999):
            chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)
        self.assertEqual(len(msgs[0]["attachment"][0]["_ocr_text"]), 10)


# ── Gemini caption fallback（Phase 2：OCR 無字才打） ─────────────────

class CaptionFallbackTests(unittest.TestCase):
    """OCR 抽不出字的純照片走 Gemini 描述退路；有字/關 gate/失敗都不打。"""

    def _img_msg(self, name="photo.jpg", ct="image/jpeg", res="res1"):
        return {"attachment": [{
            "contentName": name, "contentType": ct,
            "attachmentDataRef": {"resourceName": res},
        }]}

    def _enrich(self, msgs, *, caption_gate, ocr_result, caption_mock):
        from agent_core.ingest import chat_sync
        with mock.patch.object(chat_sync, "_CHAT_IMAGE_OCR", True), \
             mock.patch.object(chat_sync, "_CHAT_IMAGE_CAPTION", caption_gate), \
             mock.patch.object(chat_sync, "_download_attachment_bytes",
                               return_value=b"x" * 50_000), \
             mock.patch("agent_core.ingest.drive_sync._extract_image_vision",
                        return_value=ocr_result), \
             mock.patch.object(chat_sync, "caption_image_bytes", caption_mock):
            return chat_sync.enrich_messages_with_image_ocr(mock.MagicMock(), msgs)

    def test_caption_gate_off_by_default_no_gemini(self):
        cap = mock.MagicMock(return_value="不該被叫到")
        msgs = [self._img_msg()]
        n = self._enrich(msgs, caption_gate=False, ocr_result="", caption_mock=cap)
        self.assertEqual(n, 0)
        cap.assert_not_called()
        self.assertNotIn("_caption_text", msgs[0]["attachment"][0])

    def test_ocr_empty_falls_back_to_caption(self):
        cap = mock.MagicMock(return_value="棕色反毛皮低筒安全鞋樣品照")
        msgs = [self._img_msg()]
        n = self._enrich(msgs, caption_gate=True, ocr_result="", caption_mock=cap)
        self.assertEqual(n, 1)
        self.assertEqual(
            msgs[0]["attachment"][0]["_caption_text"], "棕色反毛皮低筒安全鞋樣品照")
        cap.assert_called_once()

    def test_ocr_text_wins_no_caption_call(self):
        cap = mock.MagicMock(return_value="不該被叫到")
        msgs = [self._img_msg()]
        n = self._enrich(msgs, caption_gate=True, ocr_result="LOT 208", caption_mock=cap)
        self.assertEqual(n, 1)
        cap.assert_not_called()
        self.assertNotIn("_caption_text", msgs[0]["attachment"][0])

    def test_caption_failure_swallowed(self):
        cap = mock.MagicMock(side_effect=RuntimeError("gemini down"))
        msgs = [self._img_msg()]
        n = self._enrich(msgs, caption_gate=True, ocr_result="", caption_mock=cap)
        self.assertEqual(n, 0)
        self.assertNotIn("_caption_text", msgs[0]["attachment"][0])

    def test_caption_per_space_cap(self):
        from agent_core.ingest import chat_sync
        cap = mock.MagicMock(return_value="描述")
        msgs = [self._img_msg(res="r1"), self._img_msg(res="r2")]
        with mock.patch.object(chat_sync, "_CHAT_IMG_CAPTION_PER_SPACE_CAP", 1):
            n = self._enrich(msgs, caption_gate=True, ocr_result="", caption_mock=cap)
        self.assertEqual(n, 1)
        self.assertEqual(cap.call_count, 1)

    def test_failed_captions_also_consume_cap(self):
        # 保險絲數「嘗試」不數「成功」：Gemini 持續故障時不該打到 OCR cap 的量。
        from agent_core.ingest import chat_sync
        cap = mock.MagicMock(return_value="")
        msgs = [self._img_msg(res=f"r{k}") for k in range(5)]
        with mock.patch.object(chat_sync, "_CHAT_IMG_CAPTION_PER_SPACE_CAP", 2):
            n = self._enrich(msgs, caption_gate=True, ocr_result="", caption_mock=cap)
        self.assertEqual(n, 0)
        self.assertEqual(cap.call_count, 2)

    def test_caption_text_capped(self):
        from agent_core.ingest import chat_sync
        cap = mock.MagicMock(return_value="長" * 999)
        msgs = [self._img_msg()]
        with mock.patch.object(chat_sync, "_CHAT_CAPTION_TEXT_CAP", 10):
            self._enrich(msgs, caption_gate=True, ocr_result="", caption_mock=cap)
        self.assertEqual(len(msgs[0]["attachment"][0]["_caption_text"]), 10)

    def test_caption_appears_in_message_line(self):
        from agent_core.ingest import chat_sync
        msg = {
            "createTime": "2026-07-27T01:00:00Z",
            "sender": {"displayName": "UserY"},
            "text": "",
            "attachment": [{
                "contentName": "sample.jpg",
                "contentType": "image/jpeg",
                "_caption_text": "棕色反毛皮低筒安全鞋樣品照",
            }],
        }
        line = chat_sync._message_line(msg)
        self.assertIn("附件描述", line)
        self.assertIn("棕色反毛皮低筒安全鞋樣品照", line)
        self.assertIn("sample.jpg", line)


if __name__ == "__main__":
    unittest.main()
