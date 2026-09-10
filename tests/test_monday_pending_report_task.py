"""週一會議未完成事項總報告（monday_meeting_pending_report）＋ weekdays 排程閘門。

需求（2026-08-17，大王）：每週一 10:00 全公司例行會議，週一 06:00 先寄一份
近 21 天未完成事項總報告給 owner@ 與 gm@。

守的事：
  1. dispatcher 的 should_run_task 要認得 weekdays —— 閘門壞掉的兩種死法都要抓：
     「週一沒跑」（報告開天窗）與「不是週一也跑」（變成每天寄）。
  2. 註冊腳本冪等：重跑不得洗掉 last_run_at / run_count / dedup_hashes。
  3. 任務 prompt 點名的工具全部進得了背景工具集（靜默少一段的前科 #350）——
     這裡直接拿 register 腳本裡的 _TASK 過 audit_task_tool_refs，等於部署前
     預演 health_check 會看到什麼。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import scripts.register_monday_pending_report_task as reg  # noqa: E402
from agent_core.daemon_dispatcher import should_run_task  # noqa: E402

# 2026-08-17 是週一（iso=1）、08-16 週日、08-18 週二。用固定日期，不吃 wall clock。
_MON_0602 = datetime(2026, 8, 17, 6, 2)
_MON_0930 = datetime(2026, 8, 17, 9, 30)
_SUN_0602 = datetime(2026, 8, 16, 6, 2)
_TUE_0602 = datetime(2026, 8, 18, 6, 2)


def _fresh_task(**over) -> dict:
    task = dict(reg._TASK)
    task.update({"last_run_at": None, "run_count": 0, "dedup_hashes": [],
                 "last_error": None})
    task.update(over)
    return task


class WeekdayGateTests(unittest.TestCase):
    def test_runs_on_monday_inside_window(self):
        self.assertTrue(should_run_task(_fresh_task(), _MON_0602))

    def test_does_not_run_on_monday_outside_window(self):
        self.assertFalse(should_run_task(_fresh_task(), _MON_0930))

    def test_does_not_run_on_other_days_even_inside_window(self):
        """閘門另一半：不是週一絕不能跑 —— 壞掉就是「每天 06:00 都寄」。"""
        self.assertFalse(should_run_task(_fresh_task(), _SUN_0602))
        self.assertFalse(should_run_task(_fresh_task(), _TUE_0602))

    def test_interval_still_applies_within_the_window(self):
        """視窗內已跑過 → 同一視窗不再觸發（interval=視窗寬度的既有機制）。"""
        ran = _fresh_task(last_run_at=datetime(2026, 8, 17, 6, 1).isoformat())
        self.assertFalse(should_run_task(ran, datetime(2026, 8, 17, 6, 40)))

    def test_next_monday_runs_again(self):
        ran = _fresh_task(last_run_at=_MON_0602.isoformat())
        self.assertTrue(should_run_task(ran, datetime(2026, 8, 24, 6, 2)))

    def test_force_run_bypasses_the_weekday_gate(self):
        """run_scheduled_task_now 的一次性手動觸發不受星期限制（維運/驗收用）。"""
        forced = _fresh_task(next_force_run=True)
        self.assertTrue(should_run_task(forced, _SUN_0602))

    def test_tasks_without_weekdays_are_untouched(self):
        """既有任務（沒 weekdays 欄位）行為不變 —— 這條紅了代表改壞全艦隊排程。"""
        daily = _fresh_task()
        daily.pop("weekdays")
        self.assertTrue(should_run_task(daily, _SUN_0602))

    def test_garbage_weekdays_do_not_raise(self):
        for bad in ("monday", 1, {"a": 1}, [None, "x"], []):
            with self.subTest(bad=bad):
                should_run_task(_fresh_task(weekdays=bad), _MON_0602)


class TaskDefinitionTests(unittest.TestCase):
    """任務設定本身：排程、收件人、以及「一律完整輸出」的宣告。"""

    def test_schedule_is_monday_six_am_once(self):
        self.assertEqual(reg._TASK["weekdays"], [1])
        self.assertEqual(reg._TASK["start_hour"], 6)
        self.assertEqual(reg._TASK["end_hour"], 7)
        self.assertEqual(reg._TASK["interval_minutes"], 60)

    def test_recipients_are_dylan_and_spencer_email_only(self):
        self.assertEqual(reg._TASK["notify_emails"],
                         ["owner@company.example", "gm@company.example"])
        self.assertNotIn("notify_channel", reg._TASK)

    def test_prompt_declares_full_output_and_21_day_window(self):
        """dispatcher 看到「(無新發現)」會整封不寄 —— 固定報表必須明講一律完整輸出。"""
        self.assertIn("一律完整輸出", reg._TASK["prompt"])
        self.assertIn("21", reg._TASK["prompt"])
        # 報告期間寫實際日期 → 兩週內容再像，雜湊也不同 → dedup 不會吃掉週報。
        self.assertIn("報告期間", reg._TASK["prompt"])

    def test_prompt_tools_all_reachable_for_background_dispatcher(self):
        """預演 health_check / LiveTaskFileDrift：prompt 點名的工具缺一不可。"""
        from agent_core.daemon_dispatcher import audit_task_tool_refs
        from agent_core.tool_registry import tools_list
        problems = audit_task_tool_refs([_fresh_task()], tools_list)
        self.assertEqual(problems, [], "任務點名了進不了背景工具集的工具：\n  "
                         + "\n  ".join(problems))


class RegisterScriptTests(unittest.TestCase):
    """冪等：新增 → 更新（保留 runtime 狀態）→ 刪除。"""

    def _run_seed(self, data: dict) -> list[str]:
        from unittest import mock
        with mock.patch.object(reg, "update_daemon_tasks",
                               side_effect=lambda fn: (fn(data), data)[1]):
            return reg.seed()

    def test_seed_adds_task_once(self):
        data = {"version": 1, "tasks": []}
        actions = self._run_seed(data)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertTrue(actions and actions[0].startswith("➕"))
        task = data["tasks"][0]
        self.assertEqual(task["name"], reg.TASK_NAME)
        self.assertEqual(task["run_count"], 0)
        self.assertIsNotNone(task["created_at"])

    def test_seed_update_preserves_runtime_state(self):
        existing = _fresh_task(
            last_run_at="2026-08-11T06:03:00", run_count=3,
            dedup_hashes=["aaa"], created_at="2026-08-17T12:00:00",
            prompt="舊版 prompt", interval_minutes=999)
        data = {"version": 1, "tasks": [existing]}
        actions = self._run_seed(data)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertTrue(actions and actions[0].startswith("♻️"))
        task = data["tasks"][0]
        # 設定被更新
        self.assertEqual(task["prompt"], reg._PROMPT)
        self.assertEqual(task["interval_minutes"], 60)
        # 跑出來的狀態一個都不能洗掉
        self.assertEqual(task["last_run_at"], "2026-08-11T06:03:00")
        self.assertEqual(task["run_count"], 3)
        self.assertEqual(task["dedup_hashes"], ["aaa"])
        self.assertEqual(task["created_at"], "2026-08-17T12:00:00")

    def test_remove_deletes_only_this_task(self):
        from unittest import mock
        other = {"name": "email_pending_tracker"}
        data = {"version": 1, "tasks": [other, _fresh_task()]}
        with mock.patch.object(reg, "update_daemon_tasks",
                               side_effect=lambda fn: (fn(data), data)[1]):
            reg.remove()
        self.assertEqual(data["tasks"], [other])

    def test_dry_run_does_not_write(self):
        from unittest import mock
        with mock.patch.object(reg, "update_daemon_tasks") as upd:
            reg.seed(dry_run=True)
            reg.remove(dry_run=True)
        upd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
