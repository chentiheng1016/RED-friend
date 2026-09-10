"""美金對台幣即時匯率（agent_core/fx_rates.py）—— 每日 09:00／14:00 推播的資料面。

這份報價會直接送到四個部門主管手上、且完全不經 LLM，所以測的重點全在「數字不會
騙人」：合理區間閘擋掉壞來源、兩來源差太多要標記、全部抓不到就明講失敗（絕不用
歷史值假裝是今天的價）、漲跌只在真的有可比紀錄時才寫。

全部離線 —— 不打任何外部 API，歷史檔一律指到 tmpdir（主 checkout 的 var/ 不能被
測試碰到）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import fx_rates  # noqa: E402


def _fake_requests(payload=None, *, status_exc=None, json_exc=None):
    """最小的 requests 替身，只實作 fx_rates 用到的三個面。"""
    def _get(url, timeout=None):
        resp = types.SimpleNamespace()
        resp.raise_for_status = (lambda: (_ for _ in ()).throw(status_exc)) if status_exc else (lambda: None)
        resp.json = (lambda: (_ for _ in ()).throw(json_exc)) if json_exc else (lambda: payload)
        return resp
    return types.SimpleNamespace(get=_get)


class ParserTests(unittest.TestCase):
    def test_fxratesapi(self):
        ts = 1785862740
        rate, quoted = fx_rates._parse_fxratesapi(
            {"timestamp": ts, "base": "USD", "rates": {"TWD": 32.4420062862}}
        )
        self.assertAlmostEqual(rate, 32.4420062862)
        expected = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(quoted, expected)

    def test_coinbase_has_no_quote_time(self):
        rate, quoted = fx_rates._parse_coinbase(
            {"data": {"currency": "USD", "rates": {"TWD": "32.410434", "JPY": "150"}}}
        )
        self.assertAlmostEqual(rate, 32.410434)
        self.assertIsNone(quoted)

    def test_er_api(self):
        rate, quoted = fx_rates._parse_er_api(
            {"time_last_update_unix": 1785801751, "rates": {"TWD": 32.337189}}
        )
        self.assertAlmostEqual(rate, 32.337189)
        self.assertIsInstance(quoted, datetime)

    def test_rter(self):
        rate, quoted = fx_rates._parse_rter(
            {"USDTWD": {"Exrate": 32.4, "UTC": "2026-08-04 16:00:02"},
             "USDJPY": {"Exrate": 150.0, "UTC": "2026-08-04 16:00:02"}}
        )
        self.assertAlmostEqual(rate, 32.4)
        self.assertIsInstance(quoted, datetime)

    def test_rter_bad_timestamp_degrades_to_none(self):
        # 時間欄壞掉不該讓整筆報價作廢 —— 數字還是好的，只是沒有報價時間。
        rate, quoted = fx_rates._parse_rter({"USDTWD": {"Exrate": 32.4, "UTC": "not-a-time"}})
        self.assertAlmostEqual(rate, 32.4)
        self.assertIsNone(quoted)


class FetchOneTests(unittest.TestCase):
    """單一來源的三道閘：HTTP 壞、格式壞、數字不合理。"""

    def test_good_payload(self):
        fake = _fake_requests({"timestamp": 1785862740, "rates": {"TWD": 32.44}})
        with mock.patch.object(fx_rates, "requests", fake):
            reading, err = fx_rates._fetch_one("X", "http://x", fx_rates._parse_fxratesapi)
        self.assertEqual(err, "")
        self.assertAlmostEqual(reading.rate, 32.44)
        self.assertEqual(reading.source, "X")

    def test_inverted_rate_is_rejected_by_sanity_band(self):
        # 1/32.4 = 0.0309 —— 來源把方向搞反時最典型的樣子。放行就會推出
        # 「1 USD = 0.03 TWD」這種顯然錯、但格式完全正常的數字。
        fake = _fake_requests({"timestamp": 1, "rates": {"TWD": 0.030864}})
        with mock.patch.object(fx_rates, "requests", fake):
            reading, err = fx_rates._fetch_one("X", "http://x", fx_rates._parse_fxratesapi)
        self.assertIsNone(reading)
        self.assertIn("不在合理區間", err)

    def test_absurdly_high_rate_is_rejected(self):
        fake = _fake_requests({"timestamp": 1, "rates": {"TWD": 3240.0}})
        with mock.patch.object(fx_rates, "requests", fake):
            reading, err = fx_rates._fetch_one("X", "http://x", fx_rates._parse_fxratesapi)
        self.assertIsNone(reading)
        self.assertIn("不在合理區間", err)

    def test_http_error_becomes_error_string_not_exception(self):
        fake = _fake_requests(status_exc=RuntimeError("503 Server Error"))
        with mock.patch.object(fx_rates, "requests", fake):
            reading, err = fx_rates._fetch_one("X", "http://x", fx_rates._parse_fxratesapi)
        self.assertIsNone(reading)
        self.assertIn("X:", err)

    def test_unexpected_shape_becomes_error_string(self):
        # 來源改版（欄位不見）不該炸掉 dispatcher，只該讓這個來源出局。
        fake = _fake_requests({"unexpected": True})
        with mock.patch.object(fx_rates, "requests", fake):
            reading, err = fx_rates._fetch_one("X", "http://x", fx_rates._parse_fxratesapi)
        self.assertIsNone(reading)
        self.assertIn("回傳格式非預期", err)


class FetchReadingsTests(unittest.TestCase):
    def test_stops_after_wanted_count(self):
        calls = []

        def fake_fetch_one(name, url, parser):
            calls.append(name)
            return fx_rates.Reading(source=name, rate=32.4, quoted_at=None), ""

        with mock.patch.object(fx_rates, "_fetch_one", side_effect=fake_fetch_one):
            readings, errors = fx_rates.fetch_readings(want=2)

        self.assertEqual(len(readings), 2)
        self.assertEqual(errors, [])
        # 前兩個來源都活著 → 不該再多打第三、第四支 API。
        self.assertEqual(len(calls), 2)

    def test_falls_through_dead_sources(self):
        def fake_fetch_one(name, url, parser):
            if name in ("FXRatesAPI", "Coinbase"):
                return None, f"{name}: down"
            return fx_rates.Reading(source=name, rate=32.4, quoted_at=None), ""

        with mock.patch.object(fx_rates, "_fetch_one", side_effect=fake_fetch_one):
            readings, errors = fx_rates.fetch_readings(want=2)

        self.assertEqual([r.source for r in readings], ["ExchangeRate-API", "rter.info"])
        self.assertEqual(len(errors), 2)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fx_rates_test_")
        self.history = os.path.join(self.tmpdir, "usd_twd_rate_history.json")
        patcher = mock.patch.object(fx_rates, "_HISTORY_FILE", self.history)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = datetime(2026, 8, 5, 9, 0, 0)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _brief(self, readings, errors=(), **kwargs):
        with mock.patch.object(fx_rates, "fetch_readings",
                               return_value=(list(readings), list(errors))):
            return fx_rates.usd_twd_brief(now=self.now, **kwargs)

    def _seed_history(self, rows):
        with open(self.history, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "readings": rows}, fh)

    def test_all_sources_dead_says_so_and_prints_no_number(self):
        text = self._brief([], errors=["FXRatesAPI: timeout", "Coinbase: 503"])
        self.assertTrue(text.startswith("❌"))
        self.assertIn("不猜", text)
        self.assertIn("FXRatesAPI: timeout", text)
        self.assertNotIn("1 USD =", text)

    def test_all_sources_dead_never_reuses_history(self):
        # 最重要的一條：昨天有紀錄、今天全掛，也不准把昨天的數字端出來當今天的價。
        self._seed_history([{"at": (self.now - timedelta(days=1)).isoformat(),
                             "rate": 32.38, "source": "FXRatesAPI"}])
        text = self._brief([], errors=["all down"])
        self.assertNotIn("32.38", text)

    def test_happy_path_shows_rate_and_cross_check(self):
        text = self._brief([
            fx_rates.Reading("FXRatesAPI", 32.442, self.now - timedelta(minutes=1)),
            fx_rates.Reading("Coinbase", 32.410, None),
        ])
        self.assertIn("1 USD = 32.44 TWD", text)
        self.assertIn("資料來源：FXRatesAPI", text)
        self.assertIn("交叉核對 Coinbase 32.41", text)
        self.assertNotIn("⚠️", text)
        self.assertIn("即期買入", text)  # 牌告口徑的說明一定要在

    def test_divergent_sources_are_flagged(self):
        text = self._brief([
            fx_rates.Reading("FXRatesAPI", 32.44, self.now),
            fx_rates.Reading("Coinbase", 33.60, None),  # 差 3.5%
        ])
        self.assertIn("⚠️", text)
        self.assertIn("再確認", text)

    def test_single_source_says_not_cross_checked(self):
        text = self._brief([fx_rates.Reading("FXRatesAPI", 32.44, self.now)])
        self.assertIn("未交叉核對", text)

    def test_stale_quote_is_flagged_as_market_closed(self):
        # 週一早上推播時報價會是週五收盤 —— 不講清楚會被當成當下的價。
        text = self._brief([
            fx_rates.Reading("FXRatesAPI", 32.44, self.now - timedelta(hours=50)),
        ])
        self.assertIn("市場休市中", text)

    def test_fresh_quote_not_flagged(self):
        text = self._brief([
            fx_rates.Reading("FXRatesAPI", 32.44, self.now - timedelta(minutes=3)),
        ])
        self.assertNotIn("市場休市中", text)

    def test_change_vs_previous_reading(self):
        self._seed_history([{"at": (self.now - timedelta(hours=19)).isoformat(),
                             "rate": 32.380, "source": "FXRatesAPI"}])
        text = self._brief([fx_rates.Reading("FXRatesAPI", 32.442, self.now)])
        self.assertIn("📈", text)
        self.assertIn("+0.062", text)
        self.assertIn("32.380", text)

    def test_change_downwards(self):
        self._seed_history([{"at": (self.now - timedelta(hours=19)).isoformat(),
                             "rate": 32.500, "source": "FXRatesAPI"}])
        text = self._brief([fx_rates.Reading("FXRatesAPI", 32.400, self.now)])
        self.assertIn("📉", text)
        self.assertIn("-0.100", text)

    def test_too_recent_reading_is_not_used_as_baseline(self):
        # 手動試跑一次不該把 09:00 vs 14:00 的比較洗成「較上次 +0.001」。
        self._seed_history([{"at": (self.now - timedelta(minutes=5)).isoformat(),
                             "rate": 32.441, "source": "FXRatesAPI"}])
        text = self._brief([fx_rates.Reading("FXRatesAPI", 32.442, self.now)])
        self.assertNotIn("較上次", text)

    def test_no_history_means_no_change_line(self):
        text = self._brief([fx_rates.Reading("FXRatesAPI", 32.442, self.now)])
        self.assertNotIn("較上次", text)

    def test_corrupt_history_does_not_break_the_push(self):
        with open(self.history, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        text = self._brief([fx_rates.Reading("FXRatesAPI", 32.442, self.now)])
        self.assertIn("1 USD = 32.44 TWD", text)

    def test_record_appends_history(self):
        self._brief([fx_rates.Reading("FXRatesAPI", 32.442, self.now)])
        with open(self.history, encoding="utf-8") as fh:
            rows = json.load(fh)["readings"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["rate"], 32.442)
        self.assertEqual(rows[0]["source"], "FXRatesAPI")

    def test_record_false_does_not_write(self):
        self._brief([fx_rates.Reading("FXRatesAPI", 32.442, self.now)], record=False)
        self.assertFalse(os.path.exists(self.history))

    def test_history_is_capped(self):
        self._seed_history([
            {"at": (self.now - timedelta(days=i + 1)).isoformat(), "rate": 32.0, "source": "S"}
            for i in range(fx_rates._HISTORY_KEEP + 10)
        ])
        self._brief([fx_rates.Reading("FXRatesAPI", 32.442, self.now)])
        with open(self.history, encoding="utf-8") as fh:
            rows = json.load(fh)["readings"]
        self.assertEqual(len(rows), fx_rates._HISTORY_KEEP)

    def test_failed_fetch_is_not_recorded(self):
        self._brief([], errors=["all down"])
        self.assertFalse(os.path.exists(self.history))

    def test_result_differs_between_pushes_so_dedup_never_suppresses_it(self):
        # dispatcher 會用結果的 hash 去重（remember_dispatcher_result）。早上與下午
        # 報價剛好一樣時，若訊息完全相同，下午那封會被安靜吃掉。
        reading = [fx_rates.Reading("FXRatesAPI", 32.44, self.now)]
        with mock.patch.object(fx_rates, "fetch_readings", return_value=(reading, [])):
            am = fx_rates.usd_twd_brief(now=datetime(2026, 8, 5, 9, 0), record=False)
            pm = fx_rates.usd_twd_brief(now=datetime(2026, 8, 5, 14, 0), record=False)
        self.assertNotEqual(am, pm)


class SkillShellTests(unittest.TestCase):
    def test_tool_is_background_safe_and_delegates(self):
        import skills.fx_rates as skill

        # background_safe = safe_tools 放行背景排程的第二條路徑；沒標就靜默失效。
        self.assertTrue(getattr(skill.usd_twd_rate_brief, "background_safe", False))
        with mock.patch.object(fx_rates, "usd_twd_brief", return_value="OK") as m:
            self.assertEqual(skill.usd_twd_rate_brief(), "OK")
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
