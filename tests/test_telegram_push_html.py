"""主動推播路徑（telegram_push → _send_text_to_resolved_chat → sendMessage）的
``` 圍欄 HTML <pre> 等寬渲染。

接續 test_telegram_format.py（鎖 markdown_to_telegram_html helper 本身）與
test_telegram_text_retry.py（鎖 502 重試）：這裡鎖的是把 helper 接進**推播**送出
路徑後的整合行為，比照互動回覆 daemon_telegram.tg_send：
  - 單段且含 ``` → payload 帶 parse_mode=HTML、text 是 <pre> 渲染版；
  - Telegram 永久性拒絕 HTML（400 bad entities）→ 自動退回無 parse_mode 的純文字重送；
  - 不含 ``` → payload 完全不變（零行為改變）；
  - 跨段（>4096，切過 <pre> 會壞）→ 不啟用 HTML，純文字分段；
  - 暫時性 5xx（HTML 本身有效）→ 保留 HTML 重試，不提早退純文字（防雙送）。

隔離：全程 mock `_telegram_call`（不連 Telegram）、patch `_resolve_chat_id` 與
time.sleep。不 hardcode 任何 /Users/... 路徑。
"""
import unittest
from unittest import mock

from agent_core import telegram as tg

_BAD_ENTITIES = "Telegram API 錯誤：HTTP 400: Bad Request: can't parse entities: ..."


class TelegramPushHtmlRenderTests(unittest.TestCase):
    def _push(self, message, fake_call):
        """跑 telegram_push，回 (out, payloads)；payloads 是每次 sendMessage 的 payload。"""
        payloads = []

        def fake(method, payload, timeout=10, **kwargs):
            payloads.append(dict(payload))
            return fake_call(len(payloads))

        with mock.patch.object(tg, "_resolve_chat_id", return_value=("123456", "")), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRY_SLEEP_S", 0), \
             mock.patch.object(tg.time, "sleep"), \
             mock.patch.object(tg, "_telegram_call", side_effect=fake):
            out = tg.telegram_push(message)
        return out, payloads

    def test_fence_uses_html_parse_mode(self):
        out, payloads = self._push(
            "庫存：\n```\nGL02  100\nGL03  200\n```",
            lambda n: ({"message_id": 1}, None),
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].get("parse_mode"), "HTML")  # 帶 HTML
        self.assertIn("<pre>", payloads[0]["text"])              # 渲染成 <pre>
        self.assertIn("GL02  100", payloads[0]["text"])          # 內容仍在
        self.assertIn("已推送", out)

    def test_html_rejected_falls_back_to_plain(self):
        # 第一次（HTML）被 Telegram entities 解析器拒絕（400）→ 退回純文字重送
        out, payloads = self._push(
            "庫存：\n```\nGL02  100\n```",
            lambda n: (None, _BAD_ENTITIES) if n == 1 else ({"message_id": 1}, None),
        )
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0].get("parse_mode"), "HTML")  # 第一次帶 HTML
        self.assertNotIn("parse_mode", payloads[1])              # 退回：無 parse_mode
        self.assertNotIn("<pre>", payloads[1]["text"])           # 退回：純文字
        self.assertIn("```", payloads[1]["text"])                # 純文字保留原圍欄字面
        self.assertIn("已推送", out)                             # 最終成功

    def test_no_fence_payload_unchanged(self):
        out, payloads = self._push(
            "每日生產回報：DECA.26 800 雙、LURCHI 趕產中。",
            lambda n: ({"message_id": 1}, None),
        )
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("parse_mode", payloads[0])              # 不含 ``` → 零行為改變
        self.assertEqual(
            payloads[0],
            {
                "chat_id": "123456",
                "text": "每日生產回報：DECA.26 800 雙、LURCHI 趕產中。",
                "disable_web_page_preview": True,
            },
        )
        self.assertIn("已推送", out)

    def test_multipart_fence_not_rendered_as_html(self):
        # >4096 會被切成多段，切過 <pre> 會壞，故跨段不啟用 HTML（同 tg_send）。
        message = "```\n" + ("A" * 5000) + "\n```"
        out, payloads = self._push(message, lambda n: ({"message_id": 1}, None))
        self.assertEqual(len(payloads), 2)                       # 切成 2 段
        for p in payloads:
            self.assertNotIn("parse_mode", p)                    # 兩段都純文字
            self.assertNotIn("<pre>", p["text"])
        self.assertIn("已推送", out)

    def test_html_transient_502_retries_with_html_not_plain(self):
        # 暫時性 5xx 的 HTML 是有效的 → 應保留 HTML 重試，不提早退純文字（避免雙送）。
        out, payloads = self._push(
            "庫存：\n```\nGL02  100\n```",
            lambda n: (None, "Telegram API 錯誤：HTTP 502: Bad Gateway")
            if n == 1 else ({"message_id": 1}, None),
        )
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0].get("parse_mode"), "HTML")  # 第一次 HTML（502）
        self.assertEqual(payloads[1].get("parse_mode"), "HTML")  # 重試仍 HTML，非純文字
        self.assertIn("已推送", out)


if __name__ == "__main__":
    unittest.main()
