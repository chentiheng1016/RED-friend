"""Tests for the two resilience red-lines added to dashboard_alerts:

  - _check_chroma_mode_consistency: shared Chroma server reachability
  - _check_external_api_health: Gemini API error rate

Both feed alert_pusher → Telegram. The underlying probes (chroma_backend.
preflight, cost_tracker.api_error_stats) are mocked so the checks are tested
in isolation from live server / cost-log state.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core import dashboard_alerts
from tests.awake_clock_isolation import AwakeClockIsolationMixin


class ChromaModeConsistencyCheckTests(unittest.TestCase):
    def _patch_preflight(self, status):
        return mock.patch("agent_core.chroma_backend.preflight",
                          return_value=status)

    def test_http_server_down_is_crit(self):
        status = {"mode": "http", "http_url": "http://127.0.0.1:8000",
                  "server_alive": False, "ok": False}
        with self._patch_preflight(status):
            alerts = dashboard_alerts._check_chroma_mode_consistency()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["id"], "chroma_server_down")
        self.assertEqual(alerts[0]["level"], "crit")

    def test_http_server_up_is_silent(self):
        status = {"mode": "http", "http_url": "http://127.0.0.1:8000",
                  "server_alive": True, "ok": True}
        with self._patch_preflight(status):
            self.assertEqual(dashboard_alerts._check_chroma_mode_consistency(), [])

    def test_direct_mode_silent_even_when_server_alive(self):
        """direct = dev/CI/offline; build_chroma_client's own guard handles
        the dangerous case, so this check stays quiet to avoid false alarms."""
        status = {"mode": "direct", "http_url": "", "server_alive": True,
                  "ok": False}
        with self._patch_preflight(status):
            self.assertEqual(dashboard_alerts._check_chroma_mode_consistency(), [])

    def test_unknown_mode_is_silent(self):
        status = {"mode": "unknown", "server_alive": None, "ok": True}
        with self._patch_preflight(status):
            self.assertEqual(dashboard_alerts._check_chroma_mode_consistency(), [])

    def test_preflight_failure_is_silent(self):
        with mock.patch("agent_core.chroma_backend.preflight",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(dashboard_alerts._check_chroma_mode_consistency(), [])


class ExternalApiHealthCheckTests(unittest.TestCase):
    def _patch_stats(self, stats):
        return mock.patch("agent_core.cost_tracker.api_error_stats",
                          return_value=stats)

    def _stats(self, errors, successes, by_status=None, by_model=None):
        total = errors + successes
        rate = round(errors / total * 100.0, 1) if total else 0.0
        return {"window_hours": 6, "errors": errors, "successes": successes,
                "total": total, "error_rate_pct": rate,
                "by_status": by_status or {}, "by_model": by_model or {}}

    def test_low_sample_is_silent(self):
        with self._patch_stats(self._stats(5, 3)):  # total 8 < min 20
            self.assertEqual(dashboard_alerts._check_external_api_health(), [])

    def test_healthy_rate_is_silent(self):
        with self._patch_stats(self._stats(2, 98, {"503": 2})):  # 2%
            self.assertEqual(dashboard_alerts._check_external_api_health(), [])

    def test_warn_rate(self):
        with self._patch_stats(self._stats(30, 70, {"503": 30})):  # 30%
            alerts = dashboard_alerts._check_external_api_health()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["id"], "external_api_error_rate")
        self.assertEqual(alerts[0]["level"], "warn")

    def test_crit_rate(self):
        by_model = {
            "gemini-flash-latest": {
                "errors": 50, "successes": 40, "total": 90,
                "error_rate_pct": 55.6,
            },
            "gemini-3-flash-preview": {
                "errors": 10, "successes": 0, "total": 10,
                "error_rate_pct": 100.0,
            },
        }
        with self._patch_stats(
            self._stats(60, 40, {"503": 50, "429": 10}, by_model)
        ):  # 60%
            alerts = dashboard_alerts._check_external_api_health()
        self.assertEqual(alerts[0]["level"], "crit")
        self.assertIn("主要模型：gemini-flash-latest 50/90", alerts[0]["detail"])
        self.assertEqual(alerts[0]["metric"]["by_model"], by_model)

    def test_stats_failure_is_silent(self):
        with mock.patch("agent_core.cost_tracker.api_error_stats",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(dashboard_alerts._check_external_api_health(), [])

    # ── 瞬時爆發要講出來（2026-08-14 DNS 斷線那次的教訓）──────────────
    def test_clustered_errors_are_flagged_as_a_burst(self):
        """12 筆全在 2 秒內＝網路/DNS 斷線的形狀，不是模型壞掉。

        那次面板只顯示「26% 錯誤率」＋主要模型，看的人會先去懷疑 Gemini；
        真因是本機 DNS 斷線，網路回來就自己好了。
        """
        stats = self._stats(12, 35, {"other": 12})
        stats["error_span_sec"] = 1.0
        with self._patch_stats(stats):
            alerts = dashboard_alerts._check_external_api_health()
        self.assertIn("瞬時爆發", alerts[0]["detail"])
        self.assertIn("1 秒內", alerts[0]["detail"])
        self.assertEqual(alerts[0]["metric"]["error_span_sec"], 1.0)

    def test_sustained_errors_are_not_flagged_as_a_burst(self):
        """攤在整個視窗上的錯誤是持續故障，不能貼「瞬時爆發」標籤。"""
        stats = self._stats(30, 70, {"503": 30})
        stats["error_span_sec"] = 5 * 3600.0
        with self._patch_stats(stats):
            alerts = dashboard_alerts._check_external_api_health()
        self.assertNotIn("瞬時爆發", alerts[0]["detail"])

    def test_missing_span_does_not_crash(self):
        """舊格式的 stats（沒有 error_span_sec）不能讓告警整條掛掉。"""
        with self._patch_stats(self._stats(30, 70, {"503": 30})):
            alerts = dashboard_alerts._check_external_api_health()
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertIsNone(alerts[0]["metric"]["error_span_sec"])


class EmbeddingApiHealthCheckTests(unittest.TestCase):
    """embedding 路徑自己的錯誤率紅線。

    在這條之前，embedding 失敗**完全沒有結構化紀錄**（只 print 到 rag_sync log），
    而它是最大宗 Gemini 消費者。假警報普查 E 把 embedding 的成功從生成路徑的分母
    剔掉之後，這條把它們配上自己的分子 —— 兩條各自成對，誰也不稀釋誰。
    """

    def _patch_stats(self, errors, successes, by_status=None):
        total = errors + successes
        rate = round(errors / total * 100.0, 1) if total else 0.0
        return mock.patch(
            "agent_core.cost_tracker.embed_error_stats",
            return_value={"window_hours": 6, "errors": errors,
                          "successes": successes, "total": total,
                          "error_rate_pct": rate,
                          "by_status": by_status or {}})

    def test_registered_in_all_checks(self):
        """沒掛進 _ALL_CHECKS 就永遠不會跑。"""
        self.assertIn(dashboard_alerts._check_embedding_api_health,
                      dashboard_alerts._ALL_CHECKS)

    def test_zero_and_a_couple_of_failures_stay_silent(self):
        """0～4 批：還在雜訊區。"""
        with self._patch_stats(0, 5000):
            self.assertEqual(dashboard_alerts._check_embedding_api_health(), [])
        with self._patch_stats(4, 5000, {"429": 4}):
            self.assertEqual(dashboard_alerts._check_embedding_api_health(), [])

    def test_five_failures_warn_even_at_a_tiny_ratio(self):
        """🚨 這條是本次修正的核心。

        歷史基線是**零**（2026-07-27→08-15 四份 rag_sync log，「giving up」0 次），
        所以 5 批重試用盡 = 前所未見，就算只佔 0.1% 也該講。初版訂「≥200 批且
        ≥30%」在零基線上等於一條永遠不會響的鈴 —— 跟普查 C 同型的錯。
        """
        with self._patch_stats(5, 5000, {"429": 5}):      # 0.1%
            alerts = dashboard_alerts._check_embedding_api_health()
        self.assertEqual(alerts[0]["id"], "embedding_api_error_rate")
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertEqual(alerts[0]["metric"]["errors"], 5)

    def test_majority_failing_escalates_to_crit(self):
        """比例的用途只剩「整條掛掉」的升級。"""
        with self._patch_stats(700, 300, {"403": 700}):   # 70%
            crit = dashboard_alerts._check_embedding_api_health()
        self.assertEqual(crit[0]["level"], "crit")
        self.assertIn("403×700", crit[0]["detail"])

    def test_high_ratio_but_too_few_failures_is_still_silent(self):
        """3 批全掛 = 100%，但絕對量還在雜訊區 —— 絕對筆數是主閘。"""
        with self._patch_stats(3, 0, {"timeout": 3}):
            self.assertEqual(dashboard_alerts._check_embedding_api_health(), [])

    def test_stats_failure_is_silent(self):
        with mock.patch("agent_core.cost_tracker.embed_error_stats",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(dashboard_alerts._check_embedding_api_health(), [])


class RunsErrorAbsoluteGateTests(unittest.TestCase):
    """判準＝絕對筆數 ≥3 **且** 比例超標。

    背景（假警報普查 C）：runs/index.jsonl 只記 wrap_sensitive_tool 級的敏感
    工具，實測真實流量**每日中位數 3 筆**。在這種量級上比例沒有統計意義 ——
    舊判準 `2/5 = 40%` 就 warn、`3/5 = 60%` 就 crit，已誤報三次。
    """

    def _trend(self, total, err):
        pct = (err / total * 100) if total else 0.0
        return mock.patch("agent_core.dashboard_trends.runs_trend",
                          return_value={"today": {"total": total, "err": err},
                                        "today_err_pct": pct})

    def _alerts(self):
        return dashboard_alerts._check_runs_errors()

    def test_two_failures_out_of_five_is_noise_not_an_alert(self):
        """舊判準這裡會報 warn（40%）—— 正是三次誤報的形狀。"""
        with self._trend(5, 2):
            self.assertEqual(self._alerts(), [])

    def test_one_failure_never_alerts_however_bad_the_ratio(self):
        with self._trend(1, 1):          # 100% 但只有 1 筆
            self.assertEqual(self._alerts(), [])

    def test_three_failures_out_of_three_still_fires(self):
        """舊的 `total < 5` 樣本閘會把這種**真的該看**的日子擋掉。"""
        with self._trend(3, 3):
            alerts = self._alerts()
        self.assertEqual([a["level"] for a in alerts], ["crit"])
        self.assertEqual(alerts[0]["metric"]["errors"], 3)

    def test_many_failures_but_low_ratio_stays_silent(self):
        """比例仍是第二條件 —— 100 次跑 5 次失敗（5%）不該吵。"""
        with self._trend(100, 5):
            self.assertEqual(self._alerts(), [])

    def test_detail_shows_absolute_count_not_just_percent(self):
        with self._trend(10, 4):
            detail = self._alerts()[0]["detail"]
        self.assertIn("4 筆失敗 / 10 次", detail)

    def test_missing_err_key_falls_back_to_deriving_from_pct(self):
        """其他 backend 的 shape 沒有 `err` 時要能從比例回推，不是直接崩。"""
        with mock.patch("agent_core.dashboard_trends.runs_trend",
                        return_value={"today": {"total": 10}, "today_err_pct": 50.0}):
            alerts = self._alerts()
        self.assertEqual([a["level"] for a in alerts], ["warn"])
        self.assertEqual(alerts[0]["metric"]["errors"], 5)


class ErrorLogRatioBaselineTests(unittest.TestCase):
    """ratio 判準要有真實基線才做 —— 否則基線越乾淨越容易炸。

    修前寫的是 `avg7 = trend["7d_avg"] or 1`：7 天平均為 0（乾淨的一週）時被
    換成 1，於是 10 條錯誤 / 1 = 10× → crit「今日 log 錯誤暴衝」。可是在一個
    乾淨的基線上，10 條 log 錯誤根本不是暴衝，就只是 10 條。
    """

    def _trend(self, today, avg7):
        return mock.patch("agent_core.dashboard_trends.errors_trend",
                          return_value={"today": today, "7d_avg": avg7})

    def _ids(self):
        return [a["id"] for a in dashboard_alerts._check_error_log()]

    def test_zero_baseline_does_not_manufacture_a_spike(self):
        """這就是修掉的那個 bug：乾淨的一週 + 10 條錯誤 ≠ 10× 暴衝。"""
        with self._trend(10, 0):
            self.assertNotIn("errors_ratio_crit", self._ids())

    def test_thin_baseline_below_min_is_ignored(self):
        # 平均 2 條/天、今天 40 條＝20×，但基線太薄，倍數沒有意義
        with self._trend(40, 2):
            self.assertNotIn("errors_ratio_crit", self._ids())

    def test_real_baseline_still_catches_a_real_spike(self):
        """護欄不能把真的暴衝一起擋掉 —— 基線夠厚時照報。"""
        with self._trend(200, 15):          # 13× on a 15/day baseline
            self.assertIn("errors_ratio_crit", self._ids())

    def test_absolute_count_still_fires_without_any_baseline(self):
        """ratio 靜默不影響絕對值那條 —— 爆量還是抓得到。"""
        with self._trend(600, 0):
            ids = self._ids()
        self.assertIn("errors_log", ids)            # 600 ≥ crit 500
        self.assertNotIn("errors_ratio_crit", ids)


class DashboardAlertSectionTests(unittest.TestCase):
    def test_alert_section_keeps_model_breakdown(self):
        from agent_core import dashboard

        detail = (
            "最近 6h：295 失敗 / 297 次呼叫 = 99%（crit 門檻 50%）。"
            "主要狀態：503×268, 504×18, quota_depleted×9；"
            "主要模型：gemini-flash-latest 286/288 (99%)"
        )
        alert = {
            "id": "external_api_error_rate",
            "level": "crit",
            "title": "Gemini API 錯誤率異常高",
            "detail": detail,
        }
        with mock.patch(
            "agent_core.dashboard_alerts.check_alerts",
            return_value=[alert],
        ):
            text = dashboard._section_alerts()

        self.assertIn("主要模型", text)
        self.assertIn("gemini-flash-latest", text)


class CheckAlertsKillSwitchTests(unittest.TestCase):
    """RED_DISABLE_HEALTH_ALERTS：plumbing 測試隔離 kill-switch。

    守在 check_alerts() 這個唯一咽喉點，讓「讀 live var/（cost.jsonl 算 Gemini
    錯誤率）與 launchctl」的所有 check 一次回空 —— RPC/worker E2E 測試靠它在
    子程序裡不依賴 live fleet 健康（見 tests/test_tool_rpc_worker.py）。正式
    daemon 不該設（會讓 alert_pusher 誤判恢復、致盲監控）。
    """

    def setUp(self):
        # 每個測試自己設 env 值；snapshot+還原避免洩漏，並清乾淨起點。
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("RED_DISABLE_HEALTH_ALERTS", None)

    def test_kill_switch_short_circuits_and_skips_checks(self):
        os.environ["RED_DISABLE_HEALTH_ALERTS"] = "1"
        fake = mock.Mock(return_value=[{"id": "x", "level": "crit"}])
        with mock.patch.object(dashboard_alerts, "_ALL_CHECKS", [fake]):
            self.assertEqual(dashboard_alerts.check_alerts(), [])
        fake.assert_not_called()  # 短路：連個別 check 都不跑

    def test_default_off_runs_checks(self):
        fake = mock.Mock(return_value=[{"id": "x", "level": "crit"}])
        with mock.patch.object(dashboard_alerts, "_ALL_CHECKS", [fake]):
            out = dashboard_alerts.check_alerts()
        self.assertEqual(out, [{"id": "x", "level": "crit"}])
        fake.assert_called_once()

    def test_falsey_env_values_do_not_trigger(self):
        # env_bool 語義：0/false/off/空 一律不觸發 kill-switch。
        falsey = ("0", "false", "off", "")
        fake = mock.Mock(return_value=[])
        for val in falsey:
            os.environ["RED_DISABLE_HEALTH_ALERTS"] = val
            with mock.patch.object(dashboard_alerts, "_ALL_CHECKS", [fake]):
                dashboard_alerts.check_alerts()
        self.assertEqual(fake.call_count, len(falsey))


class ErpMirrorStaleCheckTests(AwakeClockIsolationMixin, unittest.TestCase):
    """_check_erp_mirror_stale：ERP 鏡像新鮮度（manifest ts）+ 異常表數。"""

    def setUp(self):
        # age 判準走 _observed_age_h（扣睡眠），tick 檔要隔離，否則讀到 live
        # var/state 的空窗會把 30h 扣成 24h → 26h 門檻不觸發而空手而歸。
        self._awake_clock_iso_setup()
        self.addCleanup(self._awake_clock_iso_teardown)

    def _write_manifest(self, entries: dict) -> str:
        import json
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        return path

    def _ts(self, hours_ago: float) -> str:
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def test_missing_manifest_silent(self):
        self.assertEqual(
            dashboard_alerts._check_erp_mirror_stale("/nonexistent/manifest.json"), [])

    def test_fresh_no_alert(self):
        p = self._write_manifest({"SC00.T": {"status": "done", "ts": self._ts(3)}})
        self.assertEqual(dashboard_alerts._check_erp_mirror_stale(p), [])

    def test_stale_warn_then_crit(self):
        p = self._write_manifest({"SC00.T": {"status": "done", "ts": self._ts(30)}})
        alerts = dashboard_alerts._check_erp_mirror_stale(p)
        self.assertEqual([a["level"] for a in alerts], ["warn"])
        self.assertEqual(alerts[0]["id"], "erp_mirror_stale")
        p2 = self._write_manifest({"SC00.T": {"status": "done", "ts": self._ts(60)}})
        self.assertEqual(dashboard_alerts._check_erp_mirror_stale(p2)[0]["level"], "crit")

    def test_error_tables_reported(self):
        p = self._write_manifest({
            "SC00.A": {"status": "done", "ts": self._ts(2)},
            "SC00.B": {"status": "error", "ts": self._ts(2)},
            "SC00.C": {"status": "count_mismatch", "ts": self._ts(2)},
        })
        alerts = dashboard_alerts._check_erp_mirror_stale(p)
        self.assertEqual([a["id"] for a in alerts], ["erp_mirror_table_errors"])
        self.assertEqual(alerts[0]["metric"]["bad_tables"], 2)

    def test_corrupt_manifest_silent(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(dashboard_alerts._check_erp_mirror_stale(path), [])

    def test_pseudo_entry_not_counted_as_bad_table(self):
        # "_item_alias" 偽條目失敗有專屬檢查，不該再算成「表刷新異常」
        p = self._write_manifest({
            "SC00.A": {"status": "done", "ts": self._ts(3)},
            "_item_alias": {"status": "error", "ts": self._ts(3)},
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_stale(p), [])

    def test_pseudo_entry_does_not_mask_staleness(self):
        # 手動補跑對照成功（偽條目 ts 新）不代表鏡像本體有刷
        p = self._write_manifest({
            "SC00.A": {"status": "done", "ts": self._ts(30)},
            "_item_alias": {"status": "done", "ts": self._ts(1)},
        })
        alerts = dashboard_alerts._check_erp_mirror_stale(p)
        self.assertEqual([a["id"] for a in alerts], ["erp_mirror_stale"])


class ErpMirrorSlowCheckTests(AwakeClockIsolationMixin, unittest.TestCase):
    """_check_erp_mirror_slow：跑批時長（02:00 → 最後落筆）超過門檻即 warn。"""

    def setUp(self):
        # 同 ErpMirrorStaleCheckTests：>26h 靜默那條走 _observed_age_h。
        self._awake_clock_iso_setup()
        self.addCleanup(self._awake_clock_iso_teardown)

    def _write_manifest(self, entries: dict) -> str:
        import json
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        return path

    def _run_ending(self, minutes_after_two: float, hours_ago: float = 1.0):
        """造一輪「今天 02:00 起跑、跑 N 分鐘」的 manifest，並把 now 對齊到收工後。

        _check_erp_mirror_slow 讀 datetime.now() 算 age_h（>26h 靜默），所以測試
        用固定基準日 + patch 時鐘，避免真實 now 落在 02:00 前造成 end 是未來時刻。
        """
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        base = _dt(2026, 8, 1, 2, 0, 0)
        end = base + _td(minutes=minutes_after_two)
        now = end + _td(hours=hours_ago)
        return end, now

    def _patch_now(self, now):
        """只改 dashboard_alerts 看到的 datetime.now()，strptime 走真品。"""
        from datetime import datetime as real

        class _Clock(real):
            @classmethod
            def now(cls, tz=None):
                return now

        p = mock.patch.object(dashboard_alerts, "datetime", _Clock)
        p.start()
        self.addCleanup(p.stop)

    def _manifest_for(self, end, secs_by_table=None):
        stamp = end.strftime("%Y-%m-%d %H:%M:%S")
        entries = {"SC00.T": {"status": "done", "ts": stamp, "full_ts": stamp,
                              "secs": 12.0}}
        for name, secs in (secs_by_table or {}).items():
            entries[name] = {"status": "done", "ts": stamp, "full_ts": stamp,
                             "secs": secs}
        return self._write_manifest(entries)

    def test_missing_manifest_silent(self):
        self.assertEqual(
            dashboard_alerts._check_erp_mirror_slow("/nonexistent/manifest.json"), [])

    def test_normal_duration_no_alert(self):
        end, now = self._run_ending(75)          # 75 分：實測基線內
        self._patch_now(now)
        self.assertEqual(dashboard_alerts._check_erp_mirror_slow(self._manifest_for(end)), [])

    def test_slow_run_warns(self):
        end, now = self._run_ending(110)         # 110 分 > 預設門檻 100、< deadline 120
        self._patch_now(now)
        alerts = dashboard_alerts._check_erp_mirror_slow(self._manifest_for(end))
        self.assertEqual([a["id"] for a in alerts], ["erp_mirror_slow"])
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertEqual(alerts[0]["metric"]["duration_min"], 110.0)

    def test_manual_rerun_silent(self):
        # 手動補跑（09:09 收工）＝ 02:00 起算 429 分 > deadline 120 → 差值無意義，靜默
        end, now = self._run_ending(429)
        self._patch_now(now)
        self.assertEqual(dashboard_alerts._check_erp_mirror_slow(self._manifest_for(end)), [])

    def test_stale_mirror_silent(self):
        # 鏡像 >26h 沒刷：root cause 交給 erp_mirror_stale，這裡不疊
        end, now = self._run_ending(110, hours_ago=30)
        self._patch_now(now)
        self.assertEqual(dashboard_alerts._check_erp_mirror_slow(self._manifest_for(end)), [])

    def test_slowest_table_from_this_run_only(self):
        # 預檢跳過的表 secs 是上次殘值（full_ts 舊）→ 不該被選成「本輪最慢」
        end, now = self._run_ending(110)
        self._patch_now(now)
        stamp = end.strftime("%Y-%m-%d %H:%M:%S")
        p = self._write_manifest({
            "SC00.FRESH": {"status": "done", "ts": stamp, "full_ts": stamp, "secs": 300.0},
            "SC00.SKIPPED": {"status": "done", "ts": stamp, "precheck": "unchanged",
                             "full_ts": "2026-07-20 02:30:00", "secs": 9999.0},
        })
        alerts = dashboard_alerts._check_erp_mirror_slow(p)
        self.assertEqual(alerts[0]["metric"]["slowest_table"], "SC00.FRESH")

    def test_pseudo_entry_does_not_extend_run(self):
        # _item_alias 落筆在最後，但偽條目不算表 → 不許墊高 end 把時長灌成告警
        end, now = self._run_ending(75)
        self._patch_now(now)
        from datetime import timedelta as _td
        stamp = end.strftime("%Y-%m-%d %H:%M:%S")
        late = (end + _td(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
        p = self._write_manifest({
            "SC00.T": {"status": "done", "ts": stamp, "full_ts": stamp, "secs": 12.0},
            "_item_alias": {"status": "done", "ts": late},
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_slow(p), [])

    def test_corrupt_manifest_silent(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(dashboard_alerts._check_erp_mirror_slow(path), [])

    def test_threshold_env_tunable(self):
        end, now = self._run_ending(110)
        self._patch_now(now)
        p = self._manifest_for(end)
        prev = os.environ.get("RED_ERP_REFRESH_SLOW_WARN_MIN")
        os.environ["RED_ERP_REFRESH_SLOW_WARN_MIN"] = "115"
        try:
            self.assertEqual(dashboard_alerts._check_erp_mirror_slow(p), [])
        finally:
            if prev is None:
                os.environ.pop("RED_ERP_REFRESH_SLOW_WARN_MIN", None)
            else:
                os.environ["RED_ERP_REFRESH_SLOW_WARN_MIN"] = prev


class ErpMirrorPairSkewCheckTests(unittest.TestCase):
    """_check_erp_mirror_pair_skew：單頭/明細一半 error、另一半刷新 → JOIN 靜默漏資料。

    時鐘一律 patch 掉 `_observed_age_h`（真品會扣機器睡眠時數，實機睡多久會讓
    「40 小時前」算成 20 小時 → 測試隨主機睡眠史 flaky）。這裡用原始 wall-clock
    年齡，判準本身才是被測的東西。
    """

    def setUp(self):
        p = mock.patch.object(
            dashboard_alerts, "_observed_age_h",
            lambda ts: max(0.0, (time.time() - ts) / 3600.0))
        p.start()
        self.addCleanup(p.stop)

    def _write_manifest(self, entries: dict) -> str:
        import json
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        return path

    def _ts(self, hours_ago: float) -> str:
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def _done(self, hours_ago: float) -> dict:
        stamp = self._ts(hours_ago)
        return {"status": "done", "ts": stamp, "full_ts": stamp}

    def test_missing_manifest_silent(self):
        self.assertEqual(
            dashboard_alerts._check_erp_mirror_pair_skew("/nonexistent/manifest.json"),
            [])

    def test_all_done_no_alert(self):
        p = self._write_manifest({
            "MK00.SF_TRANS_HEADER": self._done(3),
            "MK00.SF_TRANS_WK": self._done(3),
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_pair_skew(p), [])

    def test_error_half_with_fresh_sibling_warns(self):
        """2026-08-15 實例：明細 SSH 斷線失敗、單頭同輪刷成功 → v_production 漏整個生產日。"""
        p = self._write_manifest({
            "MK00.SF_TRANS_HEADER": self._done(3),
            "MK00.SF_TRANS_WK": {"status": "error", "ts": self._ts(3),
                                 "error": "exit 255 … Broken pipe"},
        })
        alerts = dashboard_alerts._check_erp_mirror_pair_skew(p)
        self.assertEqual([a["id"] for a in alerts], ["erp_mirror_pair_skew"])
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertEqual(alerts[0]["metric"]["skewed_groups"], 1)
        self.assertEqual(alerts[0]["metric"]["groups"][0]["failed"],
                         ["MK00.SF_TRANS_WK"])
        self.assertEqual(alerts[0]["metric"]["groups"][0]["fresh"],
                         ["MK00.SF_TRANS_HEADER"])
        # 兩張表名都要出現在文字裡——維運的人要能直接知道補刷哪張
        self.assertIn("MK00.SF_TRANS_WK", alerts[0]["detail"])
        self.assertIn("MK00.SF_TRANS_HEADER", alerts[0]["detail"])

    def test_count_mismatch_is_not_skew(self):
        """count_mismatch 的資料是載入過的（mirror 先 load 再對帳）＝沒有舊版本殘留。"""
        p = self._write_manifest({
            "MK00.SF_TRANS_HEADER": self._done(3),
            "MK00.SF_TRANS_WK": {"status": "count_mismatch", "ts": self._ts(3),
                                 "db_rows": 100, "streamed": 102, "loaded": 102},
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_pair_skew(p), [])

    def test_stale_sibling_is_not_skew(self):
        """兄弟表 full_ts 是上週的（本輪被 precheck 跳過）＝沒人跑贏它，不齊不起來。"""
        p = self._write_manifest({
            "MK00.SF_TRANS_HEADER": self._done(200),
            "MK00.SF_TRANS_WK": {"status": "error", "ts": self._ts(3)},
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_pair_skew(p), [])

    def test_precheck_skipped_sibling_is_not_skew(self):
        """預檢跳過只更新 ts、不動 full_ts：ts 很新但 full_ts 舊 → 不算刷新過。"""
        p = self._write_manifest({
            "MK00.SF_TRANS_HEADER": {"status": "done", "ts": self._ts(3),
                                     "full_ts": self._ts(200),
                                     "precheck": "unchanged"},
            "MK00.SF_TRANS_WK": {"status": "error", "ts": self._ts(3)},
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_pair_skew(p), [])

    def test_whole_group_failed_is_not_skew(self):
        """全組都失敗＝全是舊版本、彼此仍一致；報 table_errors 就夠，不疊這則。"""
        p = self._write_manifest({
            "MK00.SF_TRANS_HEADER": {"status": "error", "ts": self._ts(3)},
            "MK00.SF_TRANS_WK": {"status": "error", "ts": self._ts(3)},
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_pair_skew(p), [])

    def test_unpaired_table_error_silent(self):
        """不在成對清單裡的表失敗（如 SE_CUST 維度表）→ 沒有 JOIN 對面，靜默。"""
        p = self._write_manifest({
            "SC00.SE_CUST": {"status": "error", "ts": self._ts(3)},
            "MK00.SF_TRANS_HEADER": self._done(3),
            "MK00.SF_TRANS_WK": self._done(3),
        })
        self.assertEqual(dashboard_alerts._check_erp_mirror_pair_skew(p), [])

    def test_multiple_groups_reported(self):
        p = self._write_manifest({
            "MK00.SF_TRANS_HEADER": self._done(3),
            "MK00.SF_TRANS_WK": {"status": "error", "ts": self._ts(3)},
            "GL00.AP_APPLY_M": self._done(3),
            "GL00.AP_APPLY_D": {"status": "error", "ts": self._ts(3)},
        })
        alerts = dashboard_alerts._check_erp_mirror_pair_skew(p)
        self.assertEqual(alerts[0]["metric"]["skewed_groups"], 2)

    def test_pair_groups_are_all_hot_tables(self):
        """清單成員必須全在 HOT_TABLES —— 熱表增刪時這份不會靜默過期。

        不在熱表的表不會每晚重刷，也就不會產生「一半新一半舊」的不齊；留在清單裡
        只會讓判準對著永遠不動的表空轉。
        """
        from agent_core.erp_mirror import HOT_TABLES
        hot = set(HOT_TABLES)
        for group in dashboard_alerts._ERP_PAIR_GROUPS:
            for key in group:
                self.assertIn(key, hot, f"{key} 不在 HOT_TABLES（成對清單過期了？）")

    def test_registered_in_checks(self):
        self.assertIn(dashboard_alerts._check_erp_mirror_pair_skew,
                      dashboard_alerts._ALL_CHECKS)


class ErpItemAliasCheckTests(AwakeClockIsolationMixin, unittest.TestCase):
    """_check_erp_item_alias：庫存編號（舊短碼）對照空缺 + 同步連續失敗。

    照 tests/test_erp_mirror.py SyncItemAliasTests 慣例：fixture DuckDB 建在
    tempdir，不碰真鏡像 var/data/erp_mirror。
    """

    def setUp(self):
        import tempfile
        # 74h 寬限只比 fixture 的 80h 多 6 小時 —— live tick 時間軸上任何 >6h 的
        # 空窗都會把它扣到門檻底下，所以 tick 檔一定要隔離。
        self._awake_clock_iso_setup()
        self.addCleanup(self._awake_clock_iso_teardown)
        d = tempfile.mkdtemp(prefix="alias_alert_test_")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.db = os.path.join(d, "erp_full.duckdb")
        self.man = os.path.join(d, "manifest.json")

    def _make_db(self, rows=(), create_table=True):
        import duckdb
        con = duckdb.connect(self.db)
        if create_table:
            con.execute('CREATE TABLE "_item_alias"(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)')
            if rows:
                con.executemany('INSERT INTO "_item_alias" VALUES (?, ?)',
                                [list(r) for r in rows])
        con.close()

    def _write_manifest(self, entries):
        import json
        with open(self.man, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)

    def _ts(self, hours_ago):
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def _check(self):
        return dashboard_alerts._check_erp_item_alias(self.db, self.man)

    def test_missing_db_silent(self):
        # CI / dev clone 沒有鏡像 → 靜默
        self._write_manifest({"_item_alias": {"status": "error", "ts": self._ts(1)}})
        self.assertEqual(self._check(), [])

    def test_table_missing_warns(self):
        self._make_db(create_table=False)
        self._write_manifest({"SC00.T": {"status": "done", "ts": self._ts(3)}})
        alerts = self._check()
        self.assertEqual([a["id"] for a in alerts], ["erp_item_alias_missing"])
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertIn("不存在", alerts[0]["detail"])

    def test_empty_table_warns(self):
        self._make_db(rows=())
        self._write_manifest({"SC00.T": {"status": "done", "ts": self._ts(3)}})
        alerts = self._check()
        self.assertEqual([a["id"] for a in alerts], ["erp_item_alias_missing"])
        self.assertEqual(alerts[0]["metric"]["rows"], 0)

    def test_healthy_silent(self):
        self._make_db(rows=[("SFIXI0500T003600400-A010", "SF24")])
        self._write_manifest({
            "SC00.T": {"status": "done", "ts": self._ts(3)},
            "_item_alias": {"status": "done", "ts": self._ts(3),
                            "last_done_ts": self._ts(3), "rows": 1},
        })
        self.assertEqual(self._check(), [])

    def test_consecutive_failures_warn_after_grace(self):
        self._make_db(rows=[("A", "SF24")])
        # 距上次成功 80h（連三晚失敗）→ warn
        self._write_manifest({
            "SC00.T": {"status": "done", "ts": self._ts(3)},
            "_item_alias": {"status": "error", "ts": self._ts(3),
                            "last_done_ts": self._ts(80)},
        })
        alerts = self._check()
        self.assertEqual([a["id"] for a in alerts], ["erp_item_alias_stale"])
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertEqual(alerts[0]["metric"]["status"], "error")

    def test_single_night_failure_silent(self):
        # 單晚失敗（如 SSH 瞬斷）在 74h 寬限內自癒 → 不吵
        self._make_db(rows=[("A", "SF24")])
        self._write_manifest({
            "SC00.T": {"status": "done", "ts": self._ts(3)},
            "_item_alias": {"status": "empty", "ts": self._ts(3),
                            "last_done_ts": self._ts(30)},
        })
        self.assertEqual(self._check(), [])

    def test_never_succeeded_anchors_first_bad(self):
        self._make_db(rows=[("A", "SF24")])
        self._write_manifest({
            "SC00.T": {"status": "done", "ts": self._ts(3)},
            "_item_alias": {"status": "error", "ts": self._ts(3),
                            "first_bad_ts": self._ts(80)},
        })
        alerts = self._check()
        self.assertEqual([a["id"] for a in alerts], ["erp_item_alias_stale"])

    def test_mirror_globally_stale_defers(self):
        # 鏡像本體 stale → root cause 交給 erp_mirror_stale，這裡不疊
        self._make_db(create_table=False)
        self._write_manifest({
            "SC00.T": {"status": "done", "ts": self._ts(60)},
            "_item_alias": {"status": "error", "ts": self._ts(60),
                            "last_done_ts": self._ts(90)},
        })
        self.assertEqual(self._check(), [])

    def test_no_manifest_silent_when_table_ok(self):
        # 舊 manifest（還沒有偽條目）+ 對照表健在 → 靜默
        self._make_db(rows=[("A", "SF24")])
        self.assertEqual(self._check(), [])

    def test_registered_in_all_checks(self):
        self.assertIn(dashboard_alerts._check_erp_item_alias,
                      dashboard_alerts._ALL_CHECKS)


if __name__ == "__main__":
    unittest.main()
