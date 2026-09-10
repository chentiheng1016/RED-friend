"""排程任務的「不經 LLM」路徑（``deterministic_tool``）。

有一類排程的訊息內容就是某顆唯讀工具的輸出（匯率推播、固定數字報表）。讓 Gemini
轉述一次只是多一層可能改寫數字的風險，還多燒一次 quota —— 〈員工零幻覺〉在這種
任務上最好的做法就是完全不讓 LLM 碰。

這裡守三件事：
  1. 帶 deterministic_tool 時真的一次 Gemini 都不打（client factory 不被呼叫）。
  2. 工具名一律從 safe_tools 解析 —— 排程設定不能指名背景工具集以外的東西。
  3. 找不到工具要 raise（由 dispatch loop 記成 last_error），不是安靜回空字串。
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import daemon_dispatcher as dd  # noqa: E402


def usd_twd_rate_brief():
    """假的匯率工具（名字與真工具一致，標了 background_safe）。"""
    return "  1 USD = 32.44 TWD  "


usd_twd_rate_brief.background_safe = True


def send_gmail():
    """有副作用的工具：沒列名單、沒標 background_safe → 不該被 deterministic 路徑碰到。"""
    return "sent"


class RunDeterministicTaskTests(unittest.TestCase):
    def test_calls_the_tool_and_strips(self):
        out = dd.run_deterministic_task(
            "usd_twd_rate_brief", tools_list=[usd_twd_rate_brief, send_gmail]
        )
        self.assertEqual(out, "1 USD = 32.44 TWD")

    def test_unknown_tool_raises(self):
        with self.assertRaises(ValueError) as ctx:
            dd.run_deterministic_task("no_such_tool", tools_list=[usd_twd_rate_brief])
        self.assertIn("no_such_tool", str(ctx.exception))

    def test_tool_outside_safe_set_is_refused(self):
        # send_gmail 在 tools_list 裡，但不是背景唯讀工具 → safe_tools 濾掉 → raise。
        with self.assertRaises(ValueError):
            dd.run_deterministic_task("send_gmail", tools_list=[usd_twd_rate_brief, send_gmail])

    def test_tool_exception_propagates(self):
        def broken_tool():
            raise RuntimeError("來源全掛")

        broken_tool.background_safe = True
        with self.assertRaises(RuntimeError):
            dd.run_deterministic_task("broken_tool", tools_list=[broken_tool])


class RunOneDispatcherTaskRoutingTests(unittest.TestCase):
    def _exploding_factory(self):
        raise AssertionError("deterministic 任務不該建立 Gemini client")

    def test_deterministic_task_never_touches_gemini(self):
        out = dd.run_one_dispatcher_task(
            {"name": "usd_twd_rate_am", "prompt": "（不使用）",
             "deterministic_tool": "usd_twd_rate_brief"},
            tools_list=[usd_twd_rate_brief],
            gemini_model="gemini-flash-latest",
            agent_client_factory=self._exploding_factory,
            agent_types_factory=self._exploding_factory,
        )
        self.assertEqual(out, "1 USD = 32.44 TWD")

    def test_blank_deterministic_tool_falls_through_to_llm_path(self):
        # 空字串/缺欄位 = 一般任務，行為必須跟以前一模一樣（會去建 Gemini client）。
        for task in ({"name": "t", "prompt": "p", "deterministic_tool": "  "},
                     {"name": "t", "prompt": "p"}):
            with self.assertRaises(AssertionError):
                dd.run_one_dispatcher_task(
                    task,
                    tools_list=[],
                    gemini_model="gemini-flash-latest",
                    agent_client_factory=self._exploding_factory,
                    agent_types_factory=self._exploding_factory,
                )


class RealFxToolIsReachableTests(unittest.TestCase):
    """回歸守門：真工具要真的進得了背景工具集。

    〈排程 prompt 點名的工具進不了背景工具集〉那批靜默失效（#350）就是這個形態 ——
    設定寫得好好的、工具卻不在 safe_tools 裡，任務每天安靜地失敗。
    """

    def test_usd_twd_rate_brief_passes_safe_tools(self):
        import skills.fx_rates as skill

        passed = dd.safe_tools([skill.usd_twd_rate_brief])
        self.assertEqual([t.__name__ for t in passed], ["usd_twd_rate_brief"])


if __name__ == "__main__":
    unittest.main()
