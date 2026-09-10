"""CFO 財務長模式 —— 仿 test_security_work_mode 的模式註冊/持久化測試。

不碰 live var/：mode 狀態檔導到 tempdir。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest


class TestCfoWorkMode(unittest.TestCase):
    def setUp(self):
        from agent_core import mode_manager

        self.mode_manager = mode_manager
        self._tmpdir = tempfile.mkdtemp(prefix="red_cfo_mode_")
        self._orig_mode_file = mode_manager._MODE_FILE
        self._orig_history_file = mode_manager._HISTORY_FILE
        mode_manager._MODE_FILE = os.path.join(self._tmpdir, "work_mode.json")
        mode_manager._HISTORY_FILE = os.path.join(
            self._tmpdir, "work_mode_history.jsonl"
        )

    def tearDown(self):
        self.mode_manager._MODE_FILE = self._orig_mode_file
        self.mode_manager._HISTORY_FILE = self._orig_history_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_cfo_mode_can_be_selected_and_listed(self):
        from agent_core.mode_manager import get_current_mode, set_work_mode
        from agent_core.mode_policy import known_modes
        from agent_core.persona_profiles import list_known_modes

        result = set_work_mode("cfo")

        self.assertTrue(result.ok)
        self.assertEqual(get_current_mode(), "cfo")
        self.assertIn("cfo", known_modes())
        self.assertIn("cfo", list_known_modes())

    def test_cfo_persona_names_the_deterministic_tools(self):
        from agent_core.persona_profiles import persona_for

        addendum = persona_for("cfo")
        # 三本帳的查詢工具都要點名 —— persona 沒點名的工具，模型不會主動用。
        for tool in (
            "income_statement", "profit_trend", "expense_breakdown",
            "cash_position", "cash_flow_monthly", "payment_pressure",
            "cash_outlook", "material_price_watch", "overpriced_purchases",
            "expense_anomaly",
        ):
            self.assertIn(tool, addendum, f"CFO persona 缺工具 {tool}")

    def test_cfo_persona_keeps_honest_boundaries(self):
        from agent_core.persona_profiles import persona_for

        addendum = persona_for("cfo")
        self.assertIn("福群", addendum)          # 單體帳邊界
        self.assertIn("不是預測", addendum)       # 月均=歷史算術
        self.assertIn("不給投資建議", addendum)   # 證券/理財不碰
        self.assertIn("照表唸", addendum)         # 零幻覺
        self.assertIn("警語", addendum)           # 工具警語要轉述

    def test_cfo_mode_does_not_narrow_tools(self):
        from agent_core.mode_policy import filter_tools_by_mode, get_mode_rules

        rules = get_mode_rules("cfo")
        self.assertIsNone(rules["tool_names"])
        self.assertEqual(rules["blocked_tiers"], [])

        def fake_tool():
            return ""

        fake_tool.__name__ = "whatever_tool"
        self.assertEqual(filter_tools_by_mode([fake_tool], "cfo"), [fake_tool])

    def test_cfo_mode_auto_expires_back_to_normal(self):
        from agent_core.mode_manager import get_current_mode, set_work_mode

        result = set_work_mode("cfo", duration_minutes=1)
        self.assertTrue(result.ok)
        self.assertEqual(get_current_mode(), "cfo")


if __name__ == "__main__":
    unittest.main()
