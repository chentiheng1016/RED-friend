"""RAG_GMAIL_STRIP_QUOTES：回信引用鏈剝除（quote chain 治本 Phase 1）。

釘住：gate 預設關；首封永不剝；「>」行與各語系歸屬標記後整段被剝；
轉寄標記命中即整封保留；剝空的回信不進 thread 文本。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.ingest import gmail_sync as gs


class StripReplyQuotesTests(unittest.TestCase):
    def test_gt_lines_removed_body_kept(self):
        body = "收到，週五出貨。\n> 請問 LOT 318 何時出？\n> 數量 1200 雙"
        self.assertEqual(gs._strip_reply_quotes(body), "收到，週五出貨。")

    def test_gmail_en_attribution_cuts_tail(self):
        body = "OK will do.\nOn Mon, Jul 7, 2026 at 3:00 PM UserC Wei wrote:\n舊內容一大串"
        self.assertEqual(gs._strip_reply_quotes(body), "OK will do.")

    def test_gmail_zh_attribution_cuts_tail(self):
        body = "好的，已安排。\n在 2026年7月7日 週一 下午3:00 佳桀UserC 寫道：\n前文引用"
        self.assertEqual(gs._strip_reply_quotes(body), "好的，已安排。")

    def test_outlook_original_message_cuts_tail(self):
        body = "請參考附件。\n-----Original Message-----\nFrom: x@y.com\n舊信全文"
        self.assertEqual(gs._strip_reply_quotes(body), "請參考附件。")

    def test_outlook_zh_header_block_cuts_tail(self):
        body = "數量更正為 800。\n寄件者: owner@company.example\n主旨: RE: PO\n舊信全文"
        self.assertEqual(gs._strip_reply_quotes(body), "數量更正為 800。")

    def test_vietnamese_attribution_cuts_tail(self):
        body = "Đã nhận, cảm ơn.\nVào Th 2, 7 thg 7, 2026 lúc 15:00 UserC đã viết:\ncũ"
        self.assertEqual(gs._strip_reply_quotes(body), "Đã nhận, cảm ơn.")

    def test_forward_marker_preserves_whole_body(self):
        body = "轉給你參考。\n---------- Forwarded message ----------\n外部原信全文（本 thread 唯一正本）"
        self.assertEqual(gs._strip_reply_quotes(body), body)

    def test_pure_quote_reply_strips_to_empty(self):
        body = "> 只有引用\n> 沒有新話"
        self.assertEqual(gs._strip_reply_quotes(body), "")


class ThreadTextGateTests(unittest.TestCase):
    def _fake_thread(self):
        def payload(text):
            return {"__text__": text}
        return {
            "historyId": "h1",
            "messages": [
                {"id": "m0", "payload": payload("原始詢價：LOT 318 交期？"),
                 "snippet": "snip"},
                {"id": "m1", "payload": payload(
                    "週五出貨。\nOn Mon wrote 不完整標記不剝\n> 請問 LOT 318 何時出？")},
            ],
        }

    def _run_thread_text(self):
        fake = self._fake_thread()
        req = mock.MagicMock()
        with mock.patch.object(gs, "_execute", return_value=fake), \
             mock.patch.object(gs, "_get_headers", return_value={
                 "Subject": "LOT 318", "From": "a@b", "Date": "d"}), \
             mock.patch.object(gs, "_extract_text",
                               side_effect=lambda p, *a, **k: p.get("__text__", "")):
            svc = mock.MagicMock()
            svc.users.return_value.threads.return_value.get.return_value = req
            return gs._thread_text(svc, "t1")[0]

    def test_gate_off_keeps_quotes(self):
        with mock.patch.object(gs, "_STRIP_QUOTES", False):
            text = self._run_thread_text()
        self.assertIn("> 請問 LOT 318 何時出？", text)

    def test_gate_on_strips_reply_quotes_but_not_first_message(self):
        with mock.patch.object(gs, "_STRIP_QUOTES", True):
            text = self._run_thread_text()
        self.assertIn("原始詢價：LOT 318 交期？", text)   # 首封原樣
        self.assertIn("週五出貨。", text)                  # 回信新內容保留
        self.assertNotIn("> 請問", text)                   # 引用行剝掉


if __name__ == "__main__":
    unittest.main()
