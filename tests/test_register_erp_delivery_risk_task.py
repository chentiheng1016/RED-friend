"""register_erp_delivery_risk_task：把既有排程改成 deterministic_tool 的冪等腳本。

守的是三件會出事的事：
  1. 不准動大王排好的送信視窗（這支任務是既有的，腳本只該接管 deterministic_tool）
  2. 原本的 LLM prompt 要備份得回得去（--revert），且重跑不可以把備份覆蓋成說明文字
  3. 跑出來的狀態（last_run_at / run_count / dedup_hashes）一個都不能洗掉
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import scripts.register_erp_delivery_risk_task as reg  # noqa: E402

_LIVE_PROMPT = "1) 呼叫 erp_delivery_risk_alert() 掃交期風險；2) 整理成表格回覆。"


def _live_task(**over) -> dict:
    """線上那支任務的樣子：LLM 路徑 + 已經跑過一陣子的狀態。"""
    task = {
        "name": "erp_delivery_risk_daily",
        "prompt": _LIVE_PROMPT,
        "interval_minutes": 1440,
        "start_hour": 8,
        "end_hour": 10,
        "enabled": True,
        "notify_emails": [],
        "last_run_at": "2026-08-06T08:33:59",
        "run_count": 45,
        "dedup_hashes": ["abc123", "def456"],
        "last_error": None,
        "created_at": "2026-06-01T09:00:00",
    }
    task.update(over)
    return task


def _args(**over) -> argparse.Namespace:
    ns = argparse.Namespace(start_hour=None, end_hour=None, interval=None,
                            dry_run=False, revert=False)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _run(data: dict, *, revert: bool = False, dry_run: bool = False, **arg_over) -> str:
    """跑一次 register()，回 stdout。update_daemon_tasks 換成直接在 data 上 mutate。"""
    def fake_update(mutate_fn):
        mutate_fn(data)
        return data

    buf = io.StringIO()
    with mock.patch.object(reg, "update_daemon_tasks", fake_update), \
         mock.patch.object(reg, "_load_daemon_tasks", return_value=data), \
         mock.patch.object(reg, "_check_tool_reachable", return_value=""), \
         contextlib.redirect_stdout(buf):
        rc = reg.register(dry_run=dry_run, revert=revert, args=_args(**arg_over))
    assert rc == 0, f"register() 回 {rc}"
    return buf.getvalue()


class ConvertTests(unittest.TestCase):
    def test_sets_deterministic_tool_and_stashes_prompt(self):
        data = {"tasks": [_live_task()]}
        out = _run(data)
        task = data["tasks"][0]
        self.assertEqual(task["deterministic_tool"], "erp_delivery_risk_alert")
        self.assertEqual(task[reg._STASH_KEY], _LIVE_PROMPT)
        self.assertEqual(task["prompt"], reg._PROMPT)
        self.assertIn("deterministic_tool", out)

    def test_keeps_live_schedule_and_runtime_state(self):
        data = {"tasks": [_live_task()]}
        _run(data)
        task = data["tasks"][0]
        # 送信視窗/間隔/通知對象：一個都不准動
        self.assertEqual((task["start_hour"], task["end_hour"]), (8, 10))
        self.assertEqual(task["interval_minutes"], 1440)
        self.assertEqual(task["notify_emails"], [])
        # 跑出來的狀態：一個都不准洗（洗掉 dedup 會讓同一份報表再寄一次）
        self.assertEqual(task["last_run_at"], "2026-08-06T08:33:59")
        self.assertEqual(task["run_count"], 45)
        self.assertEqual(task["dedup_hashes"], ["abc123", "def456"])
        self.assertEqual(task["created_at"], "2026-06-01T09:00:00")

    def test_idempotent_second_run_reports_no_change_and_keeps_stash(self):
        data = {"tasks": [_live_task()]}
        _run(data)
        out = _run(data)
        self.assertIn("無變更", out)
        # 重跑不可以把備份覆蓋成說明文字（覆蓋掉就再也回不去 LLM 路徑）
        self.assertEqual(data["tasks"][0][reg._STASH_KEY], _LIVE_PROMPT)

    def test_schedule_overrides_only_when_given(self):
        data = {"tasks": [_live_task()]}
        _run(data, start_hour=8, end_hour=9, interval=60)
        task = data["tasks"][0]
        self.assertEqual((task["start_hour"], task["end_hour"], task["interval_minutes"]),
                         (8, 9, 60))

    def test_creates_task_when_missing(self):
        data = {"tasks": []}
        out = _run(data)
        self.assertEqual(len(data["tasks"]), 1)
        task = data["tasks"][0]
        self.assertEqual(task["name"], "erp_delivery_risk_daily")
        self.assertEqual(task["deterministic_tool"], "erp_delivery_risk_alert")
        self.assertEqual((task["start_hour"], task["end_hour"]), (8, 9))
        self.assertIn("新增", out)

    def test_dry_run_reports_without_touching_the_file(self):
        data = {"tasks": [_live_task()]}
        buf = io.StringIO()
        # 真實環境 _load_daemon_tasks 回的是剛從檔案讀出來的新 dict，dry-run 改的是
        # 那份副本；這裡照樣給副本，並讓 update_daemon_tasks 一被呼叫就炸。
        with mock.patch.object(reg, "_load_daemon_tasks",
                               return_value={"tasks": [_live_task()]}), \
             mock.patch.object(reg, "_check_tool_reachable", return_value=""), \
             mock.patch.object(reg, "update_daemon_tasks",
                               side_effect=AssertionError("dry-run 不該寫入")), \
             contextlib.redirect_stdout(buf):
            rc = reg.register(dry_run=True, revert=False, args=_args())
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        self.assertNotIn("已寫入", out)
        self.assertIn("deterministic_tool", out)          # 仍要印出會做的變更
        self.assertNotIn("deterministic_tool", data["tasks"][0])


class RevertTests(unittest.TestCase):
    def test_revert_restores_prompt_and_drops_deterministic_tool(self):
        data = {"tasks": [_live_task()]}
        _run(data)
        _run(data, revert=True)
        task = data["tasks"][0]
        self.assertNotIn("deterministic_tool", task)
        self.assertNotIn(reg._STASH_KEY, task)
        self.assertEqual(task["prompt"], _LIVE_PROMPT)
        self.assertEqual(task["run_count"], 45)      # 還原也不動狀態

    def test_revert_without_stash_warns_instead_of_silently_keeping_note(self):
        data = {"tasks": [_live_task(prompt=reg._PROMPT,
                                     deterministic_tool="erp_delivery_risk_alert")]}
        out = _run(data, revert=True)
        self.assertIn("⚠️", out)
        self.assertIn("沒有備份", out)

    def test_revert_on_missing_task_is_noop(self):
        data = {"tasks": []}
        out = _run(data, revert=True)
        self.assertEqual(data["tasks"], [])
        self.assertIn("沒東西可還原", out)


class ToolReachabilityGuardTests(unittest.TestCase):
    def test_unreachable_tool_aborts_before_writing(self):
        data = {"tasks": [_live_task()]}
        buf = io.StringIO()
        with mock.patch.object(reg, "_check_tool_reachable", return_value="❌ 不可達"), \
             mock.patch.object(reg, "update_daemon_tasks",
                               side_effect=AssertionError("不該寫入")), \
             contextlib.redirect_stdout(buf):
            rc = reg.register(dry_run=False, revert=False, args=_args())
        self.assertEqual(rc, 1)
        self.assertNotIn("deterministic_tool", data["tasks"][0])


if __name__ == "__main__":
    unittest.main()
