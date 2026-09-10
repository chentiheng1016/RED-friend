"""健檢 Low batch 1（correctness）：
- gemini 暫時性錯誤分類改字界/片語匹配（'rate' 不再吃 'generate'、'500' 不再吃 JSON 內數字）
- alert 閾值 env 拒 nan/±inf（走 env_float）
- date-only deadline 不在 00:00 誤報 🚨 OVERDUE
"""
import os
import unittest
from unittest import mock


class GeminiTransientClassifierTests(unittest.TestCase):
    def test_generate_content_errors_not_transient(self):
        from agent_core.gemini_client import _is_transient_error
        self.assertFalse(_is_transient_error("AttributeError in generate_content"))
        self.assertFalse(_is_transient_error("400 INVALID_ARGUMENT see ai.google.dev/api/generate-content"))
        self.assertFalse(_is_transient_error("404 model not found for generateContent"))
        self.assertFalse(_is_transient_error("400 bad request; promptTokenCount: 1500"))

    def test_real_transient_still_detected(self):
        from agent_core.gemini_client import _is_transient_error
        for m in ("503 UNAVAILABLE", "429 rate limit exceeded", "500 internal error",
                  "deadline exceeded", "model is overloaded", "high demand"):
            self.assertTrue(_is_transient_error(m), m)

    def test_classify_uses_word_boundary(self):
        from agent_core.gemini_client import _classify_api_error
        self.assertEqual(_classify_api_error("503 UNAVAILABLE"), "503")
        self.assertEqual(_classify_api_error("400 bad arg tokens=1500"), "other")


class AlertEnvOverrideTests(unittest.TestCase):
    def test_nan_inf_rejected_to_default(self):
        from agent_core import dashboard_alerts as da
        for bad in ("nan", "-inf", "inf", "NaN"):
            with mock.patch.dict(os.environ, {"RED_ALERT_COST_MONTHLY_CAP_USD": bad}):
                self.assertEqual(da._env_override("cost_monthly_cap_usd", 250.0), 250.0, bad)
        with mock.patch.dict(os.environ, {"RED_ALERT_COST_MONTHLY_CAP_USD": "300"}):
            self.assertEqual(da._env_override("cost_monthly_cap_usd", 250.0), 300.0)


class DeadlineDateOnlyTests(unittest.TestCase):
    def test_date_only_padded_to_end_of_day(self):
        from agent_core.task_memory import _deadline_cmp
        self.assertEqual(_deadline_cmp("2026-06-21"), "2026-06-21T23:59:59")
        self.assertEqual(_deadline_cmp("2026-06-21T08:00:00"), "2026-06-21T08:00:00")
        self.assertEqual(_deadline_cmp(""), "")

    def test_date_only_not_overdue_at_midnight(self):
        from agent_core.task_memory import _deadline_cmp
        d = "2026-06-21"  # due end of the 21st
        self.assertFalse(_deadline_cmp(d) < "2026-06-21T00:00:01")  # NOT overdue at 00:00
        self.assertTrue(_deadline_cmp(d) < "2026-06-22T00:00:01")   # overdue next day


if __name__ == "__main__":
    unittest.main()
