"""長訊息分段上限（健檢 High）— 兩條送出路徑的整合 regression。

Telegram sendMessage 的 4096 上限以 **UTF-16 code unit** 計（emoji 佔 2）；
多段時兩條路徑都會給每段加 `[i/n]\n` 前綴。舊 bug：
  - daemon_telegram.tg_send / telegram._telegram_text_chunks 都用 code point
    切滿 4096 → 加前綴後 4103+，該段被 Telegram 400 打回（超長回覆前段丟失）；
  - emoji 訊息 4096 個 code point 實際 8192 units，整則被打回。

這裡鎖住兩條路徑實際送出的每個 payload["text"]（含前綴）都 ≤ 4096 UTF-16 units、
且重組後內容無損。隔離：mock _telegram_call / 注入 fake requests stub，不連網。
"""
import unittest
from unittest import mock

from agent_core import telegram as tg
from agent_core.telegram_format import utf16_len


class PushPathChunkLimitTests(unittest.TestCase):
    """telegram_push → _send_text_to_resolved_chat（推播路徑）。"""

    def _push(self, message):
        payloads = []

        def fake(method, payload, timeout=10, **kwargs):
            payloads.append(dict(payload))
            return {"message_id": len(payloads)}, None

        with mock.patch.object(tg, "_resolve_chat_id", return_value=("123456", "")), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRY_SLEEP_S", 0), \
             mock.patch.object(tg.time, "sleep"), \
             mock.patch.object(tg, "_telegram_call", side_effect=fake):
            out = tg.telegram_push(message)
        return out, payloads

    def test_long_ascii_message_every_payload_within_limit(self):
        out, payloads = self._push("A" * 9000)
        self.assertIn("已推送", out)
        self.assertGreater(len(payloads), 1)
        for p in payloads:
            self.assertLessEqual(utf16_len(p["text"]), 4096, p["text"][:40])
        # [i/n]\n 前綴仍在、內容無損
        self.assertTrue(payloads[0]["text"].startswith(f"[1/{len(payloads)}]\n"))
        body = "".join(p["text"].split("\n", 1)[1] for p in payloads)
        self.assertEqual(body, "A" * 9000)

    def test_emoji_message_counted_in_utf16_units(self):
        # 4000 emoji = 8000 UTF-16 units：舊 code-point 邏輯會當單段直送（被打回）
        msg = "😀" * 4000
        out, payloads = self._push(msg)
        self.assertIn("已推送", out)
        self.assertGreater(len(payloads), 1)
        for p in payloads:
            self.assertLessEqual(utf16_len(p["text"]), 4096)
        body = "".join(p["text"].split("\n", 1)[1] for p in payloads)
        self.assertEqual(body, msg)

    def test_short_message_single_payload_no_prefix(self):
        out, payloads = self._push("hello 大王")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["text"], "hello 大王")  # 單段零行為改變


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"ok": True}


class _FakeRequestsStub:
    """無 .Session 屬性 → _get_tg_session 原樣回傳（見 daemon_telegram 註解）。"""

    def __init__(self):
        self.payloads = []

    def post(self, url, json=None, timeout=None):
        self.payloads.append(dict(json or {}))
        return _FakeResponse()


class TgSendChunkLimitTests(unittest.TestCase):
    """daemon_telegram.tg_send（互動回覆路徑）。"""

    def _send(self, text):
        from agent_core import daemon_telegram as dt
        stub = _FakeRequestsStub()
        ok = dt.tg_send("tok", "999", text, requests_module=stub)
        return ok, stub.payloads

    def test_long_reply_every_payload_within_limit(self):
        ok, payloads = self._send("B" * 9000)
        self.assertTrue(ok)
        self.assertGreater(len(payloads), 1)
        for p in payloads:
            self.assertLessEqual(utf16_len(p["text"]), 4096)
        self.assertTrue(payloads[0]["text"].startswith(f"[1/{len(payloads)}]\n"))
        body = "".join(p["text"].split("\n", 1)[1] for p in payloads)
        self.assertEqual(body, "B" * 9000)

    def test_emoji_reply_counted_in_utf16_units(self):
        msg = "🔥" * 4000  # 8000 units — 舊邏輯單段直送會被 Telegram 打回
        ok, payloads = self._send(msg)
        self.assertTrue(ok)
        self.assertGreater(len(payloads), 1)
        for p in payloads:
            self.assertLessEqual(utf16_len(p["text"]), 4096)

    def test_single_part_fence_still_rendered_as_html(self):
        # 保留 tg_send 單段 <pre> 渲染語意（len(parts)==1 判斷）
        ok, payloads = self._send("庫存：\n```\nGL02  100\n```")
        self.assertTrue(ok)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].get("parse_mode"), "HTML")
        self.assertIn("<pre>", payloads[0]["text"])

    def test_multipart_fence_not_rendered_as_html(self):
        ok, payloads = self._send("```\n" + "C" * 5000 + "\n```")
        self.assertTrue(ok)
        self.assertGreater(len(payloads), 1)
        for p in payloads:
            self.assertNotIn("parse_mode", p)
            self.assertNotIn("<pre>", p["text"])

    def test_empty_reply_placeholder_kept(self):
        ok, payloads = self._send("")
        self.assertTrue(ok)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["text"], "(空回覆)")


if __name__ == "__main__":
    unittest.main()
