"""員工 persona 精簡（2026-08-04 成本查帳）。

部門員工每則訊息的 prompt 底盤裡，persona 佔 19,680 字元 ≈ 同量 token
（中文約 1 token/字元）。桌面自動化 / shell / 瀏覽器 / 排程那幾段講的工具
一顆都不在部門白名單裡，是純浪費。

這個檔案的重點不是「有沒有砍」，而是**砍得安全**：
  - 被砍段落必須真的存在（改名了要紅，否則精簡靜默失效只是變貴）
  - 防幻覺守則等必留段落一定要活著
  - 被砍段落提到的工具，必須沒有任何部門色拿得到

注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown。
"""
import os
import sys
import unittest

os.environ.setdefault("AGENT_DAEMON_MODE", "1")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class PersonaTrimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent_core.persona import build_persona_text
        cls.full = build_persona_text("（測試用記憶佔位）")

    def _trim(self, text=None):
        from agent_core.persona import trim_persona_for_employee
        return trim_persona_for_employee(self.full if text is None else text)

    def test_dropped_sections_actually_exist_today(self):
        """改名 persona 標題會讓精簡靜默失效（只是變貴、不會報錯）——這裡擋住。"""
        from agent_core.persona import _EMPLOYEE_DROP_SECTIONS
        missing = [s for s in _EMPLOYEE_DROP_SECTIONS if s not in self.full]
        self.assertEqual(
            missing, [],
            f"這些段落標題已不在 persona 裡，精簡會靜默失效：{missing}")

    def test_dropped_sections_are_gone_after_trim(self):
        from agent_core.persona import _EMPLOYEE_DROP_SECTIONS
        slim = self._trim()
        for section in _EMPLOYEE_DROP_SECTIONS:
            self.assertNotIn(section, slim)

    def test_guardrail_sections_survive(self):
        """🛑 員工正是最需要防幻覺守則的通道，這些絕不能被砍掉。"""
        slim = self._trim()
        for keep in ("【事實準確守則】", "【互動特別守則】", "【結論】",
                     "【詳細分析】", "【風險與建議】", "【依據資料】",
                     "【🧠 長期記憶 / 向量 RAG 使用守則】"):
            self.assertIn(keep, slim, f"必留段落被砍了：{keep}")
        # 防幻覺三道閘的關鍵字也要在
        for keep in ("citation_guard", "verify_claim", "禁止編造業務事實"):
            self.assertIn(keep, slim)

    def test_dropped_tools_are_unreachable_by_every_color(self):
        """被砍段落提到的工具，必須沒有任何部門色拿得到——否則等於砍掉說明書
        卻留著工具，LLM 會亂用。"""
        from agent_core.agents.permission_matrix import Agent
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        reachable = set()
        for agent in Agent:
            if agent.value == "red":
                continue
            reachable |= allowed_tool_names_for_color(agent.value)
        for tool in ("run_shell", "click_screen", "type_text", "press_keys",
                     "analyze_screen", "open_application", "correct_mistake",
                     "list_mistakes", "add_scheduled_task", "telegram_push"):
            self.assertNotIn(
                tool, reachable,
                f"{tool} 員工拿得到，就不該把它的使用守則從 persona 砍掉")

    def test_trim_actually_saves_a_meaningful_chunk(self):
        slim = self._trim()
        saved = len(self.full) - len(slim)
        self.assertGreater(saved, 8000, "省不到 8k 字元，值不值得做要重新評估")
        self.assertLess(len(slim), len(self.full))

    def test_fail_open_on_unrecognised_persona(self):
        """persona 全改寫（標題都對不上）→ 原樣回傳，不是回空字串。"""
        other = "完全不同的 persona，沒有任何段落標題。"
        self.assertEqual(self._trim(other), other)

    def test_empty_input_is_safe(self):
        self.assertEqual(self._trim(""), "")

    def test_trim_is_idempotent(self):
        once = self._trim()
        self.assertEqual(self._trim(once), once)

    def test_no_giant_blank_gaps_left_behind(self):
        self.assertNotIn("\n\n\n", self._trim())


if __name__ == "__main__":
    unittest.main()
