from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class CostAlertCurrentBurnTests(unittest.TestCase):
    def test_today_over_budget_is_warning_when_recent_burn_is_quiet(self):
        from agent_core import dashboard_alerts

        # 金額 fixture 對齊 2026-08-05 幣別修正後的預設門檻
        # （warn US$25 / crit US$37 / recent_crit US$3/30min）。
        # 舊 fixture 的 1300/900/5/150 是 TWD 刻度（見 cost_tracker._PRICING
        # 幣別註解），整組 ÷32.37 換成等效美元。
        with mock.patch(
            "agent_core.dashboard_trends.cost_trend",
            return_value={"today_usd": 40.0, "7d_avg_usd": 28.0},
        ), mock.patch.object(dashboard_alerts, "_recent_cost_usd", return_value=0.15):
            alerts = dashboard_alerts._check_cost()

        # 健檢 Medium：warn/crit 收斂成單一穩定 id "cost_today"（level 表達嚴重
        # 度），alert_pusher 的 warn→crit _escalated 升級才會生效。
        self.assertEqual(alerts[0]["id"], "cost_today")
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertIn("目前未持續暴衝", alerts[0]["title"])

    def test_today_over_budget_stays_critical_when_recent_burn_is_active(self):
        from agent_core import dashboard_alerts

        with mock.patch(
            "agent_core.dashboard_trends.cost_trend",
            return_value={"today_usd": 40.0, "7d_avg_usd": 28.0},
        ), mock.patch.object(dashboard_alerts, "_recent_cost_usd", return_value=4.5):
            alerts = dashboard_alerts._check_cost()

        self.assertEqual(alerts[0]["id"], "cost_today")
        self.assertEqual(alerts[0]["level"], "crit")

    def test_warn_and_crit_levels_share_one_stable_id(self):
        """warn 檔（超 warn 未達 crit）與 crit 檔必須用同一個 id "cost_today"
        — 否則惡化時 pusher 先推舊 id 的「✅ 已恢復」再推新 id。"""
        from agent_core import dashboard_alerts

        with mock.patch(
            "agent_core.dashboard_trends.cost_trend",
            return_value={"today_usd": 28.0, "7d_avg_usd": 26.0},
        ), mock.patch.object(dashboard_alerts, "_recent_cost_usd", return_value=0.0):
            alerts = dashboard_alerts._check_cost()

        self.assertEqual(alerts[0]["id"], "cost_today")
        self.assertEqual(alerts[0]["level"], "warn")

    def test_ratio_alerts_share_one_stable_id(self):
        from agent_core import dashboard_alerts

        with mock.patch(
            "agent_core.dashboard_trends.cost_trend",
            return_value={"today_usd": 1.0, "7d_avg_usd": 0.1},
        ), mock.patch.object(dashboard_alerts, "_recent_cost_usd", return_value=0.0):
            alerts = dashboard_alerts._check_cost()

        ratio_alerts = [a for a in alerts if a["id"] == "cost_ratio"]
        self.assertEqual(len(ratio_alerts), 1)
        self.assertEqual(ratio_alerts[0]["level"], "crit")  # 10× ≥ 5× crit 門檻

    # ── 假警報普查 D：暴衝告警要看得出是不是 embedding 背填 ──────────
    def test_ratio_alert_surfaces_embedding_share(self):
        """cost_ratio 觸發時要指出 embedding 佔多少。

        普查 D 原本的假設是「#388 換代讓 ratio 虛高 7 天」，但實測 16 天帳本
        推翻了：embedding 只佔日金額 2–10%，補 1.8× 後 ratio 只從 0.50× 動到
        0.48×，對 2.5× 門檻差一個數量級。真正有用的是把「誰在燒」寫進 detail
        —— 背填/重建那天 embedding 佔比會從個位數跳到七成以上。
        """
        from agent_core import dashboard_alerts

        with mock.patch(
            "agent_core.dashboard_trends.cost_trend",
            return_value={"today_usd": 1.0, "7d_avg_usd": 0.1,
                          "today_embedding_usd": 0.75},
        ), mock.patch.object(dashboard_alerts, "_recent_cost_usd", return_value=0.0):
            alerts = dashboard_alerts._check_cost()

        ratio = [a for a in alerts if a["id"] == "cost_ratio"][0]
        self.assertIn("embedding US$0.7500", ratio["detail"])
        self.assertIn("75%", ratio["detail"])
        self.assertEqual(ratio["metric"]["today_embedding_usd"], 0.75)

    def test_ratio_alert_without_embedding_has_no_note(self):
        """沒有 embedding 花費就不要多印一段（trend 缺這個鍵也不能炸）。"""
        from agent_core import dashboard_alerts

        with mock.patch(
            "agent_core.dashboard_trends.cost_trend",
            return_value={"today_usd": 1.0, "7d_avg_usd": 0.1},
        ), mock.patch.object(dashboard_alerts, "_recent_cost_usd", return_value=0.0):
            alerts = dashboard_alerts._check_cost()

        ratio = [a for a in alerts if a["id"] == "cost_ratio"][0]
        self.assertNotIn("embedding", ratio["detail"])
        self.assertEqual(ratio["metric"]["today_embedding_usd"], 0.0)


class HealthScoreCurrentRiskTests(unittest.TestCase):
    def test_low_24h_success_small_sample_no_today_failures_does_not_penalize(self):
        from agent_core import status_center

        overview = {
            "alerts": {"crit_count": 0, "warn_count": 0},
            "daemons": {"last_exit_nonzero": []},
            "metrics": {"total_calls": 12, "success_pct": 83.3},
            "queue": {"dlq": 0},
            "task_memory": {"overdue": 0},
        }
        with mock.patch.object(status_center, "system_overview", return_value=overview), \
             mock.patch(
                 "agent_core.dashboard_trends.runs_trend",
                 return_value={"today": {"total": 0}},
             ):
            result = status_center.health_score()

        self.assertEqual(result["score"], 100)
        self.assertEqual(result["reasons"], [])

    def test_low_24h_success_large_sample_still_penalizes(self):
        from agent_core import status_center

        overview = {
            "alerts": {"crit_count": 0, "warn_count": 0},
            "daemons": {"last_exit_nonzero": []},
            "metrics": {"total_calls": 50, "success_pct": 83.3},
            "queue": {"dlq": 0},
            "task_memory": {"overdue": 0},
        }
        with mock.patch.object(status_center, "system_overview", return_value=overview), \
             mock.patch(
                 "agent_core.dashboard_trends.runs_trend",
                 return_value={"today": {"total": 2}},
             ):
            result = status_center.health_score()

        self.assertEqual(result["score"], 90)
        self.assertIn("success_pct", result["reasons"][0])


class SystemAlertsInputCoercionTests(unittest.TestCase):
    def test_dict_level_filters_to_critical_alerts(self):
        from agent_core import dashboard_alerts

        alerts = [
            {"id": "warn_one", "level": "warn", "title": "Warn", "detail": "w"},
            {"id": "crit_one", "level": "crit", "title": "Crit", "detail": "c"},
        ]
        with mock.patch.object(dashboard_alerts, "check_alerts", return_value=alerts):
            text = dashboard_alerts.system_alerts({"level": "crit"})
        self.assertIn("Crit", text)
        self.assertNotIn("Warn", text)

    def test_bad_level_shape_does_not_raise(self):
        from agent_core import dashboard_alerts

        with mock.patch.object(dashboard_alerts, "check_alerts", return_value=[]):
            text = dashboard_alerts.system_alerts({"unexpected": "shape"})
        self.assertIn("目前無", text)


if __name__ == "__main__":
    unittest.main()
