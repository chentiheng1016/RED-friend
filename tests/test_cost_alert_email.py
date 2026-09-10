"""成本告警 → 並行 email 通道 + 均值基準線的舊費率過濾。

兩件事一起測（同一個需求的兩半，2026-08-04 大王指定）：

1. **email 是並行通道、不是 fallback**：alert_pusher 原本只有「telegram 掛了
   才寄信」。成本暴衝恰恰發生在 telegram 活得好好的時候，那條路等於永遠不寄。
   現在 id 以 cost 開頭的告警一律照寄 RED_ALERT_COST_EMAIL_TO。

2. **均值要濾掉 #328 之前的舊費率列**：cutover(2026-08-01) 前的 cost.jsonl 列
   低估約 100×，混進 7d avg 會把基準線壓低 → cost_ratio 一路假警報。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock


class CostAlertRecipientTests(unittest.TestCase):
    """收件人解析——設定錯了就整條通道靜默失效，值得單獨釘住。"""

    def setUp(self):
        self._saved = os.environ.get("RED_ALERT_COST_EMAIL_TO")

    def tearDown(self):
        # 主 checkout 的 process env 會洩漏到同 suite 的其他測試，一定要還原。
        if self._saved is None:
            os.environ.pop("RED_ALERT_COST_EMAIL_TO", None)
        else:
            os.environ["RED_ALERT_COST_EMAIL_TO"] = self._saved

    def _recipients(self, raw):
        from agent_core import alert_pusher
        if raw is None:
            os.environ.pop("RED_ALERT_COST_EMAIL_TO", None)
        else:
            os.environ["RED_ALERT_COST_EMAIL_TO"] = raw
        return alert_pusher._cost_alert_recipients()

    def test_unset_means_channel_off(self):
        self.assertEqual(self._recipients(None), [])
        self.assertEqual(self._recipients(""), [])
        self.assertEqual(self._recipients("   "), [])

    def test_separators_and_junk_filtering(self):
        self.assertEqual(self._recipients("owner@company.example"), ["owner@company.example"])
        self.assertEqual(
            self._recipients("owner@company.example, a@b.com;c@d.com"),
            ["owner@company.example", "a@b.com", "c@d.com"],
        )
        # 不像 email 的 token 直接丟掉，不要拿去 send_gmail 撞 API 錯誤
        self.assertEqual(self._recipients("owner@company.example, 忘了打, x@y.com"),
                         ["owner@company.example", "x@y.com"])

    def test_is_cost_alert_matches_whole_family(self):
        from agent_core import alert_pusher
        for alert_id in ("cost_today", "cost_ratio", "cost_monthly_cap", "cost_high"):
            self.assertTrue(alert_pusher._is_cost_alert({"id": alert_id}), alert_id)
        for alert_id in ("daemon_down", "email_ingest_stale", "", None):
            self.assertFalse(alert_pusher._is_cost_alert({"id": alert_id}), repr(alert_id))


class CostAlertEmailPushTests(unittest.TestCase):
    """_push_alert 的通道編排。"""

    def setUp(self):
        self._saved = os.environ.get("RED_ALERT_COST_EMAIL_TO")
        os.environ["RED_ALERT_COST_EMAIL_TO"] = "owner@company.example"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RED_ALERT_COST_EMAIL_TO", None)
        else:
            os.environ["RED_ALERT_COST_EMAIL_TO"] = self._saved

    _COST_ALERT = {
        "id": "cost_ratio",
        "level": "warn",
        "title": "今日成本明顯高於最近平均",
        "detail": "今日 US$347.0193 = 2.9× 7-day avg US$118.6587",
        "advice": "對比昨日 cost_by_tool 看差別",
    }

    def test_email_sent_even_when_telegram_succeeds(self):
        """這是整個需求的核心：telegram 成功也要寄。舊碼會在這裡 early-return。"""
        from agent_core import alert_pusher

        with mock.patch.object(alert_pusher, "_try_telegram_push",
                               return_value=(True, "")) as tg, \
             mock.patch("agent_core.gmail.send_gmail_internal", return_value="已寄出") as send:
            ok, errors = alert_pusher._push_alert(dict(self._COST_ALERT))

        self.assertTrue(ok)
        self.assertEqual(errors, [])
        tg.assert_called_once()
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["to"], "owner@company.example")
        self.assertIn("今日成本明顯高於最近平均", kwargs["subject"])
        # 內文要帶得動判斷的數字，不能只有標題
        self.assertIn("2.9×", kwargs["body"])

    def test_non_cost_alert_does_not_email(self):
        from agent_core import alert_pusher

        alert = {"id": "daemon_down", "level": "crit", "title": "daemon 掛了",
                 "detail": "telegram daemon 沒心跳"}
        with mock.patch.object(alert_pusher, "_try_telegram_push",
                               return_value=(True, "")), \
             mock.patch("agent_core.gmail.send_gmail_internal") as send:
            ok, errors = alert_pusher._push_alert(alert)

        self.assertTrue(ok)
        send.assert_not_called()

    def test_channel_off_when_env_unset(self):
        from agent_core import alert_pusher

        os.environ.pop("RED_ALERT_COST_EMAIL_TO", None)
        with mock.patch.object(alert_pusher, "_try_telegram_push",
                               return_value=(True, "")), \
             mock.patch("agent_core.gmail.send_gmail_internal") as send:
            ok, _ = alert_pusher._push_alert(dict(self._COST_ALERT))

        self.assertTrue(ok)
        send.assert_not_called()

    def test_email_failure_does_not_break_telegram_success(self):
        """email 掛掉不該讓 alert 被判定成沒推出去（會被重試洗版），但要留下痕跡。"""
        from agent_core import alert_pusher

        with mock.patch.object(alert_pusher, "_try_telegram_push",
                               return_value=(True, "")), \
             mock.patch("agent_core.gmail.send_gmail_internal",
                        side_effect=RuntimeError("SMTP boom")):
            ok, errors = alert_pusher._push_alert(dict(self._COST_ALERT))

        self.assertTrue(ok, "telegram 成功就該回 True")
        self.assertEqual([ch for ch, _ in errors], ["cost_email"])
        self.assertIn("SMTP boom", errors[0][1])

    def test_send_gmail_string_failure_is_detected(self):
        """gmail_ops.send_gmail 失敗是回字串不是 raise——別把它當成功。"""
        from agent_core import alert_pusher

        with mock.patch.object(alert_pusher, "_try_telegram_push",
                               return_value=(True, "")), \
             mock.patch("agent_core.gmail.send_gmail_internal",
                        return_value="發信失敗：invalid grant"):
            ok, errors = alert_pusher._push_alert(dict(self._COST_ALERT))

        self.assertTrue(ok)
        self.assertEqual([ch for ch, _ in errors], ["cost_email"])
        self.assertIn("invalid grant", errors[0][1])

    def test_email_still_sent_when_telegram_dead(self):
        """telegram 掛掉時不能因為已經寄過 cost email 就跳過原本的 fallback。"""
        from agent_core import alert_pusher

        with mock.patch.object(alert_pusher, "_try_telegram_push",
                               return_value=(False, "timeout")), \
             mock.patch.object(alert_pusher, "_try_email_fallback",
                               return_value=(True, "")) as fb, \
             mock.patch("agent_core.gmail.send_gmail_internal", return_value="已寄出") as send:
            ok, errors = alert_pusher._push_alert(dict(self._COST_ALERT))

        self.assertTrue(ok)
        send.assert_called_once()          # 並行那封
        fb.assert_called_once()            # 原本的 fallback 照走
        self.assertEqual([ch for ch, _ in errors], ["telegram"])


class PricingCutoverBaselineTests(unittest.TestCase):
    """7d/30d 均值要濾掉 #328 cutover 之前的舊費率列。"""

    def setUp(self):
        self._saved = os.environ.get("RED_COST_PRICING_CUTOVER_DATE")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RED_COST_PRICING_CUTOVER_DATE", None)
        else:
            os.environ["RED_COST_PRICING_CUTOVER_DATE"] = self._saved

    def _make_log(self, tmpdir):
        """今天 + 兩天新費率($300/日) + 兩天舊費率($0.30/日)。"""
        path = os.path.join(tmpdir, "cost.jsonl")
        now = datetime.now()
        rows = []
        for days, usd in ((0, 300.0), (1, 300.0), (2, 300.0), (5, 0.30), (6, 0.30)):
            rows.append({"ts": (now - timedelta(days=days)).isoformat(timespec="seconds"),
                         "cost_usd": usd, "model": "m"})
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return path

    def _trend(self, cutover_env):
        from agent_core import cost_tracker, dashboard_trends
        if cutover_env is None:
            os.environ.pop("RED_COST_PRICING_CUTOVER_DATE", None)
        else:
            os.environ["RED_COST_PRICING_CUTOVER_DATE"] = cutover_env
        with tempfile.TemporaryDirectory() as d:
            path = self._make_log(d)
            with mock.patch.object(cost_tracker, "_COST_LOG", path):
                return dashboard_trends.cost_trend()

    def test_old_pricing_days_excluded_from_avg(self):
        cutover = (datetime.now() - timedelta(days=3)).date().isoformat()
        trend = self._trend(cutover)
        # 只剩 today-1 / today-2 兩天（今天本來就不列入）→ 均值 = 300
        self.assertAlmostEqual(trend["7d_avg_usd"], 300.0, places=2)
        # 比值回到同量級 → 不該再有假警報
        self.assertLess(trend["today_usd"] / trend["7d_avg_usd"], 2.5)

    def test_without_cutover_old_rows_drag_baseline_down(self):
        """反證：不過濾的話均值被壓成 150、比值 2.0× —— 就是假警報的成因。"""
        trend = self._trend("")
        self.assertAlmostEqual(trend["7d_avg_usd"], (300 + 300 + 0.30 + 0.30) / 4, places=2)

    def test_malformed_cutover_falls_back_to_no_filter(self):
        """env 打錯不該讓整個 trend 爆掉或靜默算錯——退回不過濾。"""
        from agent_core import dashboard_trends
        os.environ["RED_COST_PRICING_CUTOVER_DATE"] = "not-a-date"
        self.assertIsNone(dashboard_trends._pricing_cutover())

    def test_default_cutover_is_off_superseded_by_read_time_normalisation(self):
        """預設不再過濾 —— 讀取端正規化已經讓歷史列同刻度。

        原本這裡釘的是 "2026-08-05"（把 TWD 刻度的列擋在均值外）。改用
        cost_tracker._normalize_entry_costs 逐列重算之後，刻度差不存在了，而
        繼續把 cutover 往後推有實害：均值會失去所有基準日（實測 avg7 = avg30
        = 0.0），`if avg7 > 0.01` 那道守門讓整條 cost_ratio 紅線靜默失效。
        機制本身保留，env 仍可臨時設日期（見上面幾個測試）。
        """
        from agent_core import dashboard_trends
        os.environ.pop("RED_COST_PRICING_CUTOVER_DATE", None)
        self.assertIsNone(dashboard_trends._pricing_cutover())


if __name__ == "__main__":
    unittest.main()
