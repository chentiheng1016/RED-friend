"""telegram_send_photo 對暫時性錯誤（含 Telegram 5xx 閘道）的重試行為。

回歸事故 2026-06-16：一張甘特圖 PNG 在傳送時遇到 `Telegram API 錯誤：HTTP 502:
Bad Gateway`，但 (1) sendPhoto 當時完全沒重試（只有 sendDocument 有），(2) 502/5xx
也不在「可重試」名單 —— 於是一個瞬間的 Telegram 閘道故障讓整張圖永久漏掉，小紅退而
求其次做成 Excel 表格（沒有視覺甘特圖）。本測試鎖住修補後的行為。

隔離：全程 mock `_telegram_call_multipart`（不連 Telegram）、patch `_resolve_chat_id`
與 `_validate_send_path`（聚焦重試邏輯，不碰路徑/授權規則）。不 hardcode 任何
/Users/... 路徑。
"""
import os
import tempfile
import unittest
from unittest import mock

from agent_core import telegram as tg


class RetryableClassifierTests(unittest.TestCase):
    def test_502_bad_gateway_retryable(self):
        # 正是 2026-06-16 漏掉甘特圖的那個錯誤字串
        self.assertTrue(tg._telegram_upload_error_retryable(
            "Telegram API 錯誤：HTTP 502: Bad Gateway"))

    def test_503_504_retryable(self):
        self.assertTrue(tg._telegram_upload_error_retryable(
            "Telegram API 錯誤：HTTP 503: Service Unavailable"))
        self.assertTrue(tg._telegram_upload_error_retryable(
            "Telegram API 錯誤：HTTP 504: Gateway Time-out"))

    def test_timeout_and_ratelimit_still_retryable(self):
        # 既有行為不可被破壞
        self.assertTrue(tg._telegram_upload_error_retryable("上傳超時（>180s）"))
        self.assertTrue(tg._telegram_upload_error_retryable(
            "Telegram 上傳失敗：ConnectionError: write operation timed out"))
        self.assertTrue(tg._telegram_upload_error_retryable(
            "Telegram API 錯誤：HTTP 429: Too Many Requests"))

    def test_genuine_api_error_not_retryable(self):
        # 永久性錯誤不該被無謂重試
        self.assertFalse(tg._telegram_upload_error_retryable(
            "Telegram API 錯誤：HTTP 400: Bad Request: chat not found"))
        self.assertFalse(tg._telegram_upload_error_retryable(
            "Telegram API 錯誤：HTTP 403: Forbidden: bot was blocked by the user"))

    def test_empty_not_retryable(self):
        self.assertFalse(tg._telegram_upload_error_retryable(""))
        self.assertFalse(tg._telegram_upload_error_retryable(None))


class SendPhotoRetryTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".png")
        os.write(fd, b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _run(self, fake_call, retries=3):
        with mock.patch.object(tg, "_validate_send_path", return_value=(True, self.path)), \
             mock.patch.object(tg, "_resolve_chat_id", return_value=("123456", "")), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRIES", retries), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRY_SLEEP_S", 0), \
             mock.patch.object(tg.time, "sleep"), \
             mock.patch.object(tg, "_telegram_call_multipart", side_effect=fake_call):
            return tg.telegram_send_photo(self.path, caption="x")

    def test_retries_502_then_succeeds(self):
        calls = []

        def fake(method, files, data, timeout=60, **kwargs):
            calls.append(method)
            if len(calls) == 1:
                return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"
            return {"message_id": 7}, None

        out = self._run(fake)
        self.assertTrue(out.ok, str(out))
        self.assertEqual(len(calls), 2)            # 第一次 502、第二次成功
        self.assertEqual(calls, ["sendPhoto", "sendPhoto"])

    def test_persistent_502_fails_after_retries(self):
        calls = []

        def fake(method, files, data, timeout=60, **kwargs):
            calls.append(method)
            return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"

        out = self._run(fake)
        self.assertFalse(out.ok)
        self.assertEqual(len(calls), 3)            # 用滿 3 次才放棄
        self.assertIn("已重試", str(out))

    def test_no_retry_on_genuine_error(self):
        calls = []

        def fake(method, files, data, timeout=60, **kwargs):
            calls.append(method)
            return None, "Telegram API 錯誤：HTTP 400: Bad Request: chat not found"

        out = self._run(fake)
        self.assertFalse(out.ok)
        self.assertEqual(len(calls), 1)            # 永久錯誤：只試一次

    def test_success_first_try_no_retry(self):
        calls = []

        def fake(method, files, data, timeout=60, **kwargs):
            calls.append(method)
            return {"message_id": 1}, None

        out = self._run(fake)
        self.assertTrue(out.ok, str(out))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
