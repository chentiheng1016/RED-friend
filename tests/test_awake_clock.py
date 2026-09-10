"""age-based 告警扣掉「沒在觀測」的時間。

守的核心不變量：**機器睡著的那段不算 age**。這台是 MacBook，闔上蓋子睡 10 小時
會讓 email_ingest / erp_mirror / item_alias / rdp 巡檢的 age 同時超標，醒來瞬間
噴一整排告警而系統從頭到尾都好的（2026-07-07 實際發生過）。

第二個不變量：**不確定時不消音**。沒有 tick 歷史（第一次部署、檔案壞掉）一律回
「全程在線」＝維持加這層之前的行為。寧可誤報也不漏報 —— 尤其 RDP 巡檢那條，
靜音等於把資安旗標蓋掉。

隔離：tick 檔路徑逐測 patch 到 tmpdir（模組常數在 import time 就從 STATE_DIR
算好，只 patch STATE_DIR 沒用，見 tests/run_history_isolation.py 同款教訓）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

HOUR = 3600.0


class UnobservedWindowTests(unittest.TestCase):
    """純函式：給定 tick 時間軸，算出沒在觀測的秒數。"""

    def setUp(self):
        from agent_core import awake_clock
        self.ac = awake_clock

    def _ticks(self, start: float, count: int, step: float = 300.0) -> list[float]:
        return [start + i * step for i in range(count)]

    def test_continuous_ticks_mean_fully_observed(self):
        now = 100_000.0
        ticks = self._ticks(now - 4 * HOUR, 48)        # 4 小時、每 5 分鐘一顆
        self.assertEqual(
            self.ac.unobserved_seconds_since(now - 4 * HOUR, now, ticks), 0.0)
        self.assertAlmostEqual(
            self.ac.observed_age_hours(now - 4 * HOUR, now, ticks), 4.0, places=2)

    def test_sleep_gap_is_subtracted(self):
        """睡 10 小時 → 12 小時的 raw age 只剩約 2 小時觀測時數。"""
        now = 1_000_000.0
        awake_before = self._ticks(now - 12 * HOUR, 12)       # 睡前 1 小時
        awake_after = self._ticks(now - 1 * HOUR, 12)         # 醒後 1 小時
        ticks = awake_before + awake_after
        observed = self.ac.observed_age_hours(now - 12 * HOUR, now, ticks)
        self.assertLess(observed, 3.0, "睡掉的 10 小時不該算進 age")
        self.assertGreater(observed, 1.5, "醒著的那兩段要算")

    def test_no_history_means_fully_observed(self):
        """沒有 tick = 不知道 → 維持原行為，不幫告警消音。"""
        now = 500_000.0
        self.assertEqual(self.ac.unobserved_seconds_since(now - 30 * HOUR, now, []), 0.0)
        self.assertAlmostEqual(
            self.ac.observed_age_hours(now - 30 * HOUR, now, []), 30.0, places=2)

    def test_period_before_the_record_began_is_unknown_not_unobserved(self):
        """🚨 比最舊 tick 更早的那段是**未知**，不能當成「沒觀測」。

        否則新部署（tick 紀錄才剛開始）的頭幾小時會把所有 age-based 告警一起
        靜音 —— 那比誤報更糟。這條測試就是為了釘住這個方向。
        """
        now = 900_000.0
        ticks = self._ticks(now - 300.0, 2)        # 紀錄只涵蓋最近 5 分鐘
        unobs = self.ac.unobserved_seconds_since(now - 10 * HOUR, now, ticks)
        self.assertEqual(unobs, 0.0)
        # → age 維持原始值，該報的照報
        self.assertAlmostEqual(
            self.ac.observed_age_hours(now - 10 * HOUR, now, ticks), 10.0, places=2)

    def test_gap_between_two_ticks_still_counts_after_the_clamp(self):
        """睡眠會落在兩顆 tick 之間 —— 上面那條護欄不能把它一起擋掉。"""
        now = 900_000.0
        ticks = [now - 20 * HOUR] + self._ticks(now - 300.0, 2)
        unobs = self.ac.unobserved_seconds_since(now - 20 * HOUR, now, ticks)
        self.assertGreater(unobs, 19 * HOUR)

    def test_jitter_within_factor_is_not_a_gap(self):
        """排程抖動（遲到一兩個 interval）是正常的，不能當空窗。

        間隔 340–700s 全都在 3×300=900s 門檻內 → 整段視為連續在線。
        """
        now = 700_000.0
        ticks = [now - 3600, now - 3000, now - 2300, now - 1700,
                 now - 1000, now - 400, now - 60]
        self.assertEqual(self.ac.unobserved_seconds_since(now - 3600, now, ticks), 0.0)
        self.assertAlmostEqual(self.ac.observed_age_hours(now - 3600, now, ticks), 1.0, places=2)

    def test_future_since_ts_does_not_go_negative(self):
        now = 400_000.0
        self.assertEqual(self.ac.unobserved_seconds_since(now + HOUR, now, [now]), 0.0)
        self.assertEqual(self.ac.observed_age_hours(now + HOUR, now, [now]), 0.0)


class TickPersistenceTests(unittest.TestCase):
    def setUp(self):
        from agent_core import awake_clock
        self.ac = awake_clock
        self._tmp = tempfile.mkdtemp(prefix="awake_ticks_")
        self._orig = awake_clock._TICKS_FILE
        awake_clock._TICKS_FILE = os.path.join(self._tmp, "awake_ticks.json")

    def tearDown(self):
        import shutil
        self.ac._TICKS_FILE = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_record_then_read_round_trip(self):
        self.ac.record_tick(1000.0)
        self.ac.record_tick(1300.0)
        self.assertEqual(self.ac._read_ticks(), [1000.0, 1300.0])

    def test_old_ticks_are_pruned(self):
        self.ac.record_tick(1000.0)
        self.ac.record_tick(1000.0 + 8 * 86400)       # 8 天後
        self.assertEqual(self.ac._read_ticks(), [1000.0 + 8 * 86400])

    def test_corrupt_file_degrades_to_no_history(self):
        with open(self.ac._TICKS_FILE, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(self.ac._read_ticks(), [])
        self.ac.record_tick(2000.0)                    # 壞檔也要能繼續寫
        self.assertEqual(self.ac._read_ticks(), [2000.0])

    def test_unwritable_path_does_not_raise(self):
        self.ac._TICKS_FILE = "/nonexistent-root-dir/x/awake_ticks.json"
        self.ac.record_tick(3000.0)                    # 不該炸


class AlertIntegrationTests(unittest.TestCase):
    """告警端真的有扣掉睡眠，而且降級時不消音。"""

    def setUp(self):
        from agent_core import dashboard_alerts
        self.da = dashboard_alerts

    def test_email_ingest_stale_silent_when_the_gap_was_sleep(self):
        """heartbeat 20 小時沒寫，但那 20 小時機器都沒在線 → 不該報。"""
        from agent_core.logging_and_paths import STATE_DIR
        with tempfile.TemporaryDirectory() as d:
            hb = os.path.join(d, "email_ingest_heartbeat.json")
            with open(hb, "w", encoding="utf-8") as fh:
                fh.write("{}")
            old = os.path.getmtime(hb) - 20 * HOUR
            os.utime(hb, (old, old))
            with mock.patch.object(self.da, "STATE_DIR", d, create=True), \
                    mock.patch("agent_core.logging_and_paths.STATE_DIR", d), \
                    mock.patch.object(self.da, "_observed_age_h", return_value=0.2):
                self.assertEqual(self.da._check_email_ingest_stale(), [])
            self.assertTrue(os.path.isdir(d) and STATE_DIR)  # 只是確認沒動到真的

    def test_observed_age_falls_back_to_raw_when_clock_breaks(self):
        """awake_clock 壞掉 → 回原始 age，不是回 0（回 0 等於幫告警消音）。"""
        import time
        with mock.patch("agent_core.awake_clock.observed_age_hours",
                        side_effect=RuntimeError("boom")):
            got = self.da._observed_age_h(time.time() - 5 * HOUR)
        self.assertAlmostEqual(got, 5.0, places=1)

    def test_check_alerts_records_a_tick(self):
        with mock.patch("agent_core.awake_clock.record_tick") as rec, \
                mock.patch.object(self.da, "_ALL_CHECKS", []):
            self.da.check_alerts()
        rec.assert_called_once()

    def test_kill_switch_does_not_record_a_tick(self):
        """告警關掉時我們本來就沒在觀測，不該記成有。"""
        with mock.patch.dict(os.environ, {"RED_DISABLE_HEALTH_ALERTS": "1"}), \
                mock.patch("agent_core.awake_clock.record_tick") as rec:
            self.assertEqual(self.da.check_alerts(), [])
        rec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
