"""排程任務健康判讀 + 失敗/停擺告警。

背景（2026-08-06）：dispatcher 執行失敗只把 last_error 寫回 daemon_tasks.json
就結束，全 repo 沒有任何東西讀它；red-status 的 ✅ 又是 `enabled` 旗標而非健康
狀態，所以一支連續失敗一週的任務顯示得跟正常的一模一樣。這批任務有一半直接寄信
給同事（採購晨報 / 生產回報 / 倉庫通知），壞掉時第一個發現的是收件人。

停擺門檻的重點在 expected_gap_minutes：任務只在 start_hour–end_hour 內會被觸發，
所以「每 60 分鐘」配 09–10 視窗實際是一天一次。拿 interval 當門檻會天天誤報 ——
這是本檔最該守住的不變量。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

NOW = datetime(2026, 8, 6, 18, 0, 0)


def _task(**kw):
    base = {"name": "t", "interval_minutes": 60, "start_hour": 0,
            "end_hour": 24, "enabled": True}
    base.update(kw)
    return base


class ExpectedGapTests(unittest.TestCase):
    def setUp(self):
        from agent_core import scheduler
        self.s = scheduler

    def test_all_day_window_is_just_the_interval(self):
        self.assertEqual(self.s.expected_gap_minutes(
            _task(interval_minutes=30, start_hour=0, end_hour=24)), 30.0)

    def test_narrow_window_accounts_for_the_wait_until_it_reopens(self):
        """09-10 視窗＋60 分 interval 實際是一天一次 —— 門檻要涵蓋視窗外的等待。"""
        gap = self.s.expected_gap_minutes(
            _task(interval_minutes=60, start_hour=9, end_hour=10))
        self.assertEqual(gap, 23 * 60 + 60)          # 23 小時視窗外 + interval

    def test_garbage_fields_fall_back_instead_of_raising(self):
        self.assertEqual(self.s.expected_gap_minutes(
            _task(interval_minutes="x", start_hour=None, end_hour="y")), 60.0)

    def test_inverted_window_treated_as_all_day(self):
        self.assertEqual(self.s.expected_gap_minutes(
            _task(interval_minutes=15, start_hour=20, end_hour=4)), 15.0)


class WeekdayScheduleTests(unittest.TestCase):
    """weekdays 週排程：解析 + 停擺門檻。門檻不認得週節奏 = 每週固定誤報 stalled。"""

    def setUp(self):
        from agent_core import scheduler
        self.s = scheduler

    def test_weekday_parsing_sorts_dedups_and_drops_garbage(self):
        self.assertEqual(self.s.task_weekdays(_task(weekdays=[5, 1, 1, "3"])), (1, 3, 5))
        # 單一垃圾值只丟那一個，不整欄作廢（手改設定打錯一個數字，任務不該默默變每天跑）。
        self.assertEqual(self.s.task_weekdays(_task(weekdays=[1, "x", 0, 8, None])), (1,))

    def test_no_weekdays_field_means_every_day(self):
        self.assertEqual(self.s.task_weekdays(_task()), ())
        self.assertEqual(self.s.task_weekdays(_task(weekdays="1")), ())  # 整欄不是 list

    def test_monday_only_window_task_gap_is_one_week(self):
        """每週一 06–07 視窗＋60 分 interval ＝ 一週一次，門檻要涵蓋整週。"""
        gap = self.s.expected_gap_minutes(
            _task(interval_minutes=60, start_hour=6, end_hour=7, weekdays=[1]))
        self.assertEqual(gap, 7 * 24 * 60.0)          # 6 天整 + 23 小時視窗外 + interval

    def test_two_weekdays_use_the_longest_leg(self):
        """週一＋週四：最長的一段是週四→下週一的 4 天。"""
        gap = self.s.expected_gap_minutes(
            _task(interval_minutes=60, start_hour=6, end_hour=7, weekdays=[1, 4]))
        self.assertEqual(gap, (3 * 24 + 23) * 60.0 + 60)

    def test_all_seven_weekdays_behave_like_daily(self):
        self.assertEqual(
            self.s.expected_gap_minutes(
                _task(interval_minutes=30, start_hour=0, end_hour=24,
                      weekdays=[1, 2, 3, 4, 5, 6, 7])),
            30.0)

    def test_weekly_task_health_does_not_false_alarm_mid_week(self):
        """週一跑過、週四檢查（NOW=週四）—— 正常等待中，不可誤報 stalled。"""
        h = self.s.task_health(
            _task(start_hour=6, end_hour=7, weekdays=[1],
                  last_run_at=(NOW - timedelta(days=3)).isoformat()), NOW)
        self.assertEqual(h["state"], "ok")

    def test_weekly_task_stalled_after_two_missed_mondays(self):
        h = self.s.task_health(
            _task(start_hour=6, end_hour=7, weekdays=[1],
                  last_run_at=(NOW - timedelta(days=15)).isoformat()), NOW)
        self.assertEqual(h["state"], "stalled")


class TaskHealthTests(unittest.TestCase):
    def setUp(self):
        from agent_core import scheduler
        self.s = scheduler

    def _health(self, **kw):
        return self.s.task_health(_task(**kw), NOW)

    def test_recent_run_is_ok(self):
        h = self._health(last_run_at=(NOW - timedelta(minutes=10)).isoformat())
        self.assertEqual(h["state"], "ok")

    def test_last_error_is_reported_even_when_it_ran_just_now(self):
        """跑了但拋例外 —— last_run_at 是新的，不能因此判成健康。"""
        h = self._health(last_run_at=NOW.isoformat(), last_error="KeyError: boom")
        self.assertEqual(h["state"], "error")
        self.assertIn("boom", h["detail"])

    def test_disabled_is_not_a_problem(self):
        h = self._health(enabled=False, last_run_at=None)
        self.assertEqual(h["state"], "disabled")

    def test_stalled_when_older_than_threshold(self):
        h = self._health(start_hour=9, end_hour=10,
                         last_run_at=(NOW - timedelta(days=3)).isoformat())
        self.assertEqual(h["state"], "stalled")

    def test_daily_window_task_not_stalled_after_one_day(self):
        """晨報昨天跑過、今天視窗還沒到 —— 這是正常的，不可誤報。"""
        h = self._health(start_hour=9, end_hour=10,
                         last_run_at=(NOW - timedelta(hours=25)).isoformat())
        self.assertEqual(h["state"], "ok")

    def test_never_run_and_old_enough_is_flagged(self):
        h = self._health(created_at=(NOW - timedelta(days=5)).isoformat(),
                         last_run_at=None)
        self.assertEqual(h["state"], "never")

    def test_just_created_is_not_flagged(self):
        h = self._health(created_at=(NOW - timedelta(minutes=5)).isoformat(),
                         last_run_at=None)
        self.assertEqual(h["state"], "ok")

    def test_never_run_without_created_at_is_unknown_not_alarming(self):
        """分不出新舊時保持沉默，別對既有任務製造一批假警報。"""
        h = self._health(last_run_at=None)
        self.assertEqual(h["state"], "unknown")

    def test_unparseable_timestamps_do_not_raise(self):
        for bad in ("", "not-a-date", 12345, None):
            with self.subTest(bad=bad):
                self.s.task_health(_task(last_run_at=bad, created_at=bad), NOW)


class ScheduledTaskAlertTests(unittest.TestCase):
    def setUp(self):
        from agent_core import dashboard_alerts
        self.da = dashboard_alerts

    def _alerts(self, tasks):
        from agent_core import scheduler
        with mock.patch.object(scheduler, "_load_daemon_tasks",
                               return_value={"tasks": tasks}):
            return self.da._check_scheduled_tasks()

    def test_healthy_fleet_produces_no_alerts(self):
        self.assertEqual(self._alerts([
            _task(name="a", last_run_at=datetime.now().isoformat())]), [])

    def test_failing_task_that_emails_colleagues_is_crit(self):
        alerts = self._alerts([_task(
            name="ashley_daily_brief_am", last_error="boom",
            last_run_at=datetime.now().isoformat(),
            notify_emails=["twpurchase2@company.example"])])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "crit")
        self.assertIn("twpurchase2@company.example", alerts[0]["detail"])
        self.assertIn("ashley_daily_brief_am", alerts[0]["id"])

    def test_failing_owner_only_task_is_warn(self):
        alerts = self._alerts([_task(name="ponder", last_error="boom",
                                     last_run_at=datetime.now().isoformat())])
        self.assertEqual(alerts[0]["level"], "warn")

    def test_telegram_delivery_also_counts_as_outbound(self):
        alerts = self._alerts([_task(
            name="daily_warehouse_report", last_error="boom",
            last_run_at=datetime.now().isoformat(),
            notify_channel="telegram", notify_agent_color="indigo")])
        self.assertEqual(alerts[0]["level"], "crit")
        self.assertIn("indigo", alerts[0]["detail"])

    def test_stalled_task_alerts_with_actionable_advice(self):
        alerts = self._alerts([_task(
            name="amanda_todo_pm", start_hour=15, end_hour=16,
            last_run_at=(datetime.now() - timedelta(days=4)).isoformat(),
            notify_emails=["vnpurchase2@company.example"])])
        self.assertEqual(len(alerts), 1)
        self.assertIn("停擺", alerts[0]["title"])
        self.assertIn("start_hour", alerts[0]["advice"])

    def test_disabled_task_is_silent(self):
        self.assertEqual(self._alerts([
            _task(name="off", enabled=False, last_error="boom")]), [])

    def test_unreadable_task_file_does_not_raise(self):
        from agent_core import scheduler
        with mock.patch.object(scheduler, "_load_daemon_tasks",
                               side_effect=OSError("gone")):
            self.assertEqual(self.da._check_scheduled_tasks(), [])

    def test_registered_in_all_checks(self):
        """漏掛進 _ALL_CHECKS 的話這條檢查永遠不會跑 —— 正是本案要修的靜默失效。"""
        self.assertIn(self.da._check_scheduled_tasks, self.da._ALL_CHECKS)


class DashboardSectionTests(unittest.TestCase):
    """red-status 的圖示必須表達健康，不是 enabled 旗標。"""

    def setUp(self):
        from agent_core import dashboard
        self.d = dashboard

    def _render(self, tasks):
        from agent_core import scheduler
        with mock.patch.object(scheduler, "_load_daemon_tasks",
                               return_value={"tasks": tasks}):
            return self.d._section_scheduled_tasks()

    def test_failing_task_does_not_render_as_green_tick(self):
        out = self._render([_task(name="broken", last_error="boom",
                                  last_run_at=datetime.now().isoformat())])
        self.assertIn("❌", out)
        self.assertNotIn("✅ broken", out)
        self.assertIn("boom", out)

    def test_healthy_task_still_green(self):
        out = self._render([_task(name="fine",
                                  last_run_at=datetime.now().isoformat())])
        self.assertIn("✅", out)
        self.assertIn("全部正常", out)

    def test_bad_tasks_float_to_the_top_of_the_truncated_list(self):
        """面板只列前 10 支 —— 壞的排第 11 位就等於看不到。"""
        tasks = [_task(name=f"ok{i}", last_run_at=datetime.now().isoformat())
                 for i in range(12)]
        tasks.append(_task(name="broken_last", last_error="boom",
                           last_run_at=datetime.now().isoformat()))
        out = self._render(tasks)
        self.assertIn("broken_last", out)
        self.assertIn("1 支異常", out)

    def test_empty_and_unreadable_are_handled(self):
        self.assertIn("沒有排程任務", self._render([]))
        from agent_core import scheduler
        with mock.patch.object(scheduler, "_load_daemon_tasks",
                               side_effect=OSError("gone")):
            self.assertIn("⚠️", self.d._section_scheduled_tasks())


if __name__ == "__main__":
    unittest.main()
