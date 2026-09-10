"""telegram_push / 外送文字對暫時性錯誤（含 Telegram 502）的重試行為。

接續 photo retry（test_telegram_photo_retry.py）：互動「回覆」走 daemon_telegram.tg_send
（本來就有 5xx/429 重試），但 agent_core.telegram 的「推播」路徑（telegram_push →
_send_text_to_resolved_chat → sendMessage）先前**完全沒重試** —— 無人值守的每日生產
回報 / 告警 / briefing 一遇 `HTTP 502: Bad Gateway` 就靜默漏掉、沒人會發現。本測試
鎖住補上的重試（與 photo/document 共用 _TG_RETRYABLE_TOKENS）。

隔離：全程 mock `_telegram_call`（不連 Telegram）、patch `_resolve_chat_id`。
不 hardcode 任何 /Users/... 路徑。
"""
import unittest
from unittest import mock

from agent_core import telegram as tg


class SendMessageWithRetriesTests(unittest.TestCase):
    def _run(self, fake_call, retries=3):
        with mock.patch.object(tg, "_TG_UPLOAD_RETRIES", retries), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRY_SLEEP_S", 0), \
             mock.patch.object(tg.time, "sleep"), \
             mock.patch.object(tg, "_telegram_call", side_effect=fake_call):
            return tg._send_message_with_retries("123456", "hello")

    def test_retries_502_then_succeeds(self):
        calls = []

        def fake(method, payload, timeout=10, **kwargs):
            calls.append(method)
            if len(calls) == 1:
                return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"
            return {"message_id": 1}, None

        result, err = self._run(fake)
        self.assertIsNone(err)
        self.assertEqual(len(calls), 2)              # 502 後重試成功
        self.assertEqual(calls, ["sendMessage", "sendMessage"])

    def test_persistent_502_fails_after_retries(self):
        calls = []

        def fake(method, payload, timeout=10, **kwargs):
            calls.append(method)
            return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"

        result, err = self._run(fake)
        self.assertIsNotNone(err)
        self.assertEqual(len(calls), 3)              # 用滿 3 次
        self.assertIn("已重試", err)

    def test_no_retry_on_genuine_error(self):
        calls = []

        def fake(method, payload, timeout=10, **kwargs):
            calls.append(method)
            return None, "Telegram API 錯誤：HTTP 400: Bad Request: chat not found"

        result, err = self._run(fake)
        self.assertIsNotNone(err)
        self.assertEqual(len(calls), 1)              # 永久錯誤不重試

    def test_timeout_forwarded(self):
        seen = {}

        def fake(method, payload, timeout=10, **kwargs):
            seen["timeout"] = timeout
            return {"message_id": 1}, None

        with mock.patch.object(tg, "_telegram_call", side_effect=fake):
            tg._send_message_with_retries("123456", "hi", timeout=15)
        self.assertEqual(seen["timeout"], 15)


class TelegramPushRetryTests(unittest.TestCase):
    """端到端：公開的 telegram_push 推播工具在 502 後自動恢復。"""

    def test_push_recovers_from_transient_502(self):
        calls = []

        def fake(method, payload, timeout=10, **kwargs):
            calls.append(method)
            if len(calls) == 1:
                return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"
            return {"message_id": 1}, None

        with mock.patch.object(tg, "_resolve_chat_id", return_value=("123456", "")), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRY_SLEEP_S", 0), \
             mock.patch.object(tg.time, "sleep"), \
             mock.patch.object(tg, "_telegram_call", side_effect=fake):
            out = tg.telegram_push("每日生產回報：DECA.26 800 雙…")

        self.assertIn("已推送", out)                  # 推播在重試後成功
        self.assertEqual(len(calls), 2)

    def test_push_persistent_502_reports_partial_fail(self):
        def fake(method, payload, timeout=10, **kwargs):
            return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"

        with mock.patch.object(tg, "_resolve_chat_id", return_value=("123456", "")), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRY_SLEEP_S", 0), \
             mock.patch.object(tg.time, "sleep"), \
             mock.patch.object(tg, "_telegram_call", side_effect=fake):
            out = tg.telegram_push("告警：磁碟快滿")

        self.assertIn("失敗", out)                    # 用滿重試仍失敗 → 回報部分失敗


if __name__ == "__main__":
    unittest.main()
