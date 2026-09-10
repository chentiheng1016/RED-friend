"""email_ingest 的 Gemini 分類呼叫：故障期單封 wedge 不該拖垮整輪。

背景：2026-06-18 Gemini broad-outage 期間，email_ingest 每輪卡在 _classify_email_for_lake
的 Gemini 呼叫（ssl.recv 半開 TCP wedge），看門狗 run_with_deadline 到 1200s 砍掉整輪
（exit 75）。根因之一＝該呼叫沿用 client 預設 600s timeout + _gemini_generate 預設 5 次重試，
單封就能燒掉整輪預算。修法＝分類呼叫帶「緊 timeout + 少重試」的 per-call config。

這些測試驗證 wiring（離線、mock gmail/Gemini），不打真 API。
"""
import unittest
from unittest import mock

from agent_core import email_classify


class ClassifyTimeoutConfigTests(unittest.TestCase):
    def test_config_builder_returns_tight_per_request_timeout(self):
        # 只釘 timeout 這一項（本檔要保護的就是它）。config 還帶著 thinking 意圖
        # 等其他鍵，用整份 dict 相等去比，每加一個鍵就假性紅一次。
        self.assertEqual(
            email_classify._classify_gen_config()["http_options"],
            {"timeout": email_classify._CLASSIFY_TIMEOUT_MS},
        )
        self.assertEqual(
            email_classify._lake_gen_config()["http_options"],
            {"timeout": email_classify._CLASSIFY_TIMEOUT_MS},
        )

    def test_defaults_tighter_than_client_baseline(self):
        # client 預設 600_000ms / _gemini_generate 預設 5 次重試；分類要明顯更緊。
        self.assertLessEqual(email_classify._CLASSIFY_TIMEOUT_MS, 120_000)
        self.assertGreaterEqual(email_classify._CLASSIFY_MAX_ATTEMPTS, 1)
        self.assertLess(email_classify._CLASSIFY_MAX_ATTEMPTS, 5)


class LakeClassifyWiringTests(unittest.TestCase):
    @staticmethod
    def _fake_service(sender="vendor@example.com", subject="PO update"):
        msg = {"threadId": "t1", "payload": {"headers": [
            {"name": "From", "value": sender},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Tue, 17 Jun 2026 09:00:00 +0800"},
        ]}}
        svc = mock.MagicMock()
        svc.users.return_value.messages.return_value.get.return_value.execute.return_value = msg
        return svc

    def test_lake_classify_passes_tight_timeout_and_fewer_retries(self):
        captured = {}

        def fake_gen(model=None, contents=None, config=None, max_attempts=5, **kw):
            captured["config"] = config
            captured["max_attempts"] = max_attempts
            return mock.Mock(text="{}")

        # _parse_lake_json→None 讓函式在捕獲呼叫參數後乾淨退出（不依賴回傳 schema）。
        with mock.patch.object(email_classify, "get_service", return_value=self._fake_service()), \
             mock.patch.object(email_classify, "_extract_body", return_value="body text"), \
             mock.patch.object(email_classify, "_list_attachments", return_value=[]), \
             mock.patch.object(email_classify, "_parse_lake_json", return_value=None), \
             mock.patch.object(email_classify, "_gemini_generate", side_effect=fake_gen):
            result = email_classify._classify_email_for_lake("mid-123")

        self.assertIsNone(result)
        self.assertEqual(captured["config"],
                         {"http_options": {"timeout": email_classify._CLASSIFY_TIMEOUT_MS}})
        self.assertEqual(captured["max_attempts"], email_classify._CLASSIFY_MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
