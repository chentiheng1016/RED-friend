"""健檢 Medium regression: force-run 是一次性手動觸發、非 retry-until-success。
失敗也要清 next_force_run，否則確定性失敗的 forced task 每 dispatcher tick(~5min)
重跑、24h 不停燒 Gemini 配額（should_run_task 在 flag 上先 return True、繞過 interval）。
"""
import unittest
from datetime import datetime, timedelta

from agent_core import daemon_dispatcher as dd


class DispatcherForceRunTests(unittest.TestCase):
    def test_force_run_cleared_on_failure(self):
        task = {"name": "t", "next_force_run": True, "interval_minutes": 60,
                "last_run_at": "2026-06-21T13:50:00"}
        dd.mark_dispatcher_task_failed(task, RuntimeError("boom"), datetime(2026, 6, 21, 14, 0, 0))
        self.assertNotIn("next_force_run", task)  # one-shot trigger cleared on failure
        self.assertEqual(task["last_error"], "boom")

    def test_force_run_still_cleared_on_success(self):
        # regression guard: success path unchanged (still pops it)
        task = {"name": "t", "next_force_run": True}
        dd.mark_dispatcher_task_succeeded(task, datetime(2026, 6, 21, 14, 0, 0))
        self.assertNotIn("next_force_run", task)

    def test_failed_force_run_does_not_refire_every_tick(self):
        now = datetime(2026, 6, 21, 14, 0, 0)
        task = {"name": "t", "enabled": True, "next_force_run": True,
                "interval_minutes": 60, "start_hour": 0, "end_hour": 24}
        self.assertTrue(dd.should_run_task(task, now))  # force flag fires it once
        dd.mark_dispatcher_task_failed(task, RuntimeError("boom"), now)
        # next dispatcher tick ~5min later: flag gone, 60m interval not elapsed → no re-run
        self.assertFalse(dd.should_run_task(task, now + timedelta(minutes=5)))


if __name__ == "__main__":
    unittest.main()
