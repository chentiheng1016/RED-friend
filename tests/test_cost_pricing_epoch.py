"""Regression: cost.jsonl 裡混著多個計價紀元的列，跨紀元加總沒有意義。

2026-08-05 10:50 的假告警實錄 —— #353 把 `_PRICING` 從（誤標成 USD 的）新台幣
改成官方美元牌價、門檻也一起換算，但 daemon 艦隊到 10:50:24 才 redeploy。重啟
後的 process 拿到**新的 USD 門檻**（crit US$37），去加總當天 00:00–10:50 那 911
筆**還是舊台幣口徑**寫進去的列，得到 US$77.83 就報了 crit。換算回美元其實只有
US$2.40，離門檻還很遠。

治法有兩條路：回填 cost.jsonl，或在讀取端重算。選讀取端是因為
  1. 寫入是 open(_COST_LOG, "a") 每次開關檔、只有 process 內鎖，6+ 支 daemon
     同時 append，rename + 重寫有資料遺失窗口；
  2. `cost_usd` 是衍生值且三個紀元都算錯過，raw token 才是真相且逐列完整保存。

這個檔釘住三件事：新列要帶版本、舊列讀出來要被重算、門檻不能再跟牌價脫鉤。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# 2026-08-05 帳本裡的真實列（caller=memory.embed_document，跨切換點前後各一）。
# 兩列 token 數同量級，但舊列的 cost_usd 是台幣口徑 → 差 ~32 倍。
_TWD_EPOCH_ROW = {
    "ts": "2026-08-05T10:50:02", "model": "gemini-3.6-flash",
    "prompt_tokens": 1342, "output_tokens": 94, "thinking_tokens": 1484,
    "cached_tokens": 0, "total_tokens": 2920,
    "cost_usd": 0.4413,  # ← 台幣刻度，沒有 pricing_ver
    "caller": "email_classify._classify_email_for_lake",
}


class NormaliseEntryCostsTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker

        self.ct = cost_tracker

    def test_row_without_version_is_recomputed_from_raw_tokens(self):
        row = dict(_TWD_EPOCH_ROW)
        (fixed,) = self.ct._normalize_entry_costs([row])
        expected = self.ct.compute_cost_usd(
            "gemini-3.6-flash", 1342, 94 + 1484, cached_tokens=0)
        self.assertAlmostEqual(fixed["cost_usd"], expected, places=9)
        # 台幣列被縮回美元刻度：0.4413 → ~0.0138，約 32 倍
        self.assertLess(fixed["cost_usd"], 0.05)
        self.assertGreater(_TWD_EPOCH_ROW["cost_usd"] / fixed["cost_usd"], 25)

    def test_current_version_row_is_left_untouched(self):
        """當前紀元的列直接信任，不重算（省掉整月十萬列的無謂計算）。"""
        row = {
            "ts": "2026-08-05T11:02:37", "model": "gemini-3.6-flash",
            "prompt_tokens": 1372, "output_tokens": 105, "thinking_tokens": 1279,
            "cached_tokens": 0, "cost_usd": 999.0,  # 故意給個荒謬值
            "pricing_ver": self.ct._PRICING_VERSION,
        }
        (out,) = self.ct._normalize_entry_costs([row])
        self.assertEqual(out["cost_usd"], 999.0)

    def test_row_without_raw_tokens_keeps_original_value(self):
        """沒有 raw token 可依據時保留原值 —— 寧可沿用舊數字也不要低報成 0。"""
        row = {"ts": "2026-07-01T00:00:00", "model": "gemini-3.6-flash",
               "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
               "cost_usd": 1.23}
        (out,) = self.ct._normalize_entry_costs([row])
        self.assertEqual(out["cost_usd"], 1.23)

    def test_broken_row_does_not_break_the_whole_ledger(self):
        rows = [{"model": None, "prompt_tokens": "not-a-number"}, dict(_TWD_EPOCH_ROW)]
        out = self.ct._normalize_entry_costs(rows)
        self.assertEqual(len(out), 2)
        self.assertLess(out[1]["cost_usd"], 0.05)  # 好的那列照樣被修

    def test_mixed_epoch_ledger_sums_on_one_scale(self):
        """本案重演：同一份帳本混兩個紀元，加總必須是正規化後的值。"""
        twd_rows = [dict(_TWD_EPOCH_ROW) for _ in range(10)]
        usd_row = {
            "ts": "2026-08-05T11:02:37", "model": "gemini-3.6-flash",
            "prompt_tokens": 1372, "output_tokens": 105, "thinking_tokens": 1279,
            "cached_tokens": 0, "cost_usd": 0.0124,
        }
        raw_total = sum(r["cost_usd"] for r in twd_rows) + usd_row["cost_usd"]
        self.assertGreater(raw_total, 4.4)  # 混著加 = 4.42，數字沒有意義

        out = self.ct._normalize_entry_costs(twd_rows + [usd_row])
        self.assertLess(sum(r["cost_usd"] for r in out), 0.2)


class PricingVersionStampTests(unittest.TestCase):
    def test_record_call_stamps_current_pricing_version(self):
        from agent_core import cost_tracker

        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "cost.jsonl")
            with mock.patch.object(cost_tracker, "_COST_LOG", log), \
                    mock.patch.object(cost_tracker, "_pg_cost_store", lambda: None):
                cost_tracker.record_call(
                    model="gemini-3.6-flash",
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=1000, candidates_token_count=50,
                        cached_content_token_count=0, total_token_count=1050),
                    caller="test",
                )
            with open(log, encoding="utf-8") as f:
                entry = json.loads(f.readline())

        self.assertEqual(entry["pricing_ver"], cost_tracker._PRICING_VERSION)
        # 有了戳記，這列讀回來就不該再被重算
        before = entry["cost_usd"]
        (out,) = cost_tracker._normalize_entry_costs([dict(entry)])
        self.assertEqual(out["cost_usd"], before)


class PricingMagnitudeGuardTests(unittest.TestCase):
    """量級守門 —— 這次 bug 的本質是「一個數字大了 32 倍，但整條路徑上沒有任何
    斷言擋它」。釘住量級就能在 CI 抓到下一次幣別／單位寫錯。"""

    def setUp(self):
        from agent_core import cost_tracker

        self.ct = cost_tracker

    def test_typical_flash_call_costs_cents_not_dollars(self):
        # 一次典型的分類呼叫 ~3k token：美元刻度 ≈ $0.014，台幣刻度會是 ~$0.44
        cost = self.ct.compute_cost_usd("gemini-3.6-flash", 1342, 1578)
        self.assertLess(cost, 0.05, "3k token 的 flash 呼叫要 $0.05 以上 = 單位錯了")
        self.assertGreater(cost, 0.001, "太便宜 = 掉到 lite 價或牌價被清空")

    def test_every_priced_model_stays_in_usd_order_of_magnitude(self):
        """整張表逐條掃：沒有任何模型的 1M input token 該超過 US$100。

        （TWD 刻度的 3.6-flash input 是 48.56、output 242.82；output 那條是這裡
        唯一會超過 100 的，所以只釘 input 就足以抓到整張表被換成台幣。）
        """
        for model, (inp, out) in self.ct._PRICING.items():
            with self.subTest(model=model):
                self.assertLess(inp, 100.0, f"{model} input 牌價疑似非美元刻度")
                self.assertGreaterEqual(inp, 0.0)
                self.assertLess(out, 200.0, f"{model} output 牌價疑似非美元刻度")


class ThresholdTrackPricingTests(unittest.TestCase):
    """門檻與牌價同源 —— 這次假告警的根因就是兩份各自寫死的數字沒有東西保證同步。"""

    def setUp(self):
        from agent_core import cost_tracker, dashboard_alerts

        self.ct = cost_tracker
        self.da = dashboard_alerts

    def test_thresholds_are_multiples_of_the_pricing_table_rate(self):
        rate = self.ct._PRICING["gemini-3.6-flash"][0]
        self.assertAlmostEqual(self.da._DEFAULTS["cost_today_warn_usd"], 17 * rate, places=6)
        self.assertAlmostEqual(self.da._DEFAULTS["cost_today_crit_usd"], 25 * rate, places=6)
        self.assertAlmostEqual(self.da._DEFAULTS["cost_recent_crit_usd"], 2 * rate, places=6)

    def test_threshold_follows_a_pricing_unit_change(self):
        """牌價換單位（例如又被寫成台幣）時，門檻必須跟著走。

        這正是 08-05 沒發生、才會報假警報的那件事。
        """
        twd_table = dict(self.ct._PRICING)
        twd_table["gemini-3.6-flash"] = (48.56, 242.82)
        with mock.patch.object(self.ct, "_PRICING", twd_table):
            self.assertAlmostEqual(self.da._flash_input_rate(), 48.56, places=4)
            # 若門檻仍寫死 37.0，台幣口徑的日常花費（~$360）會天天報 crit
            self.assertGreater(25 * self.da._flash_input_rate(), 1000.0)

    def test_broken_pricing_table_falls_back_instead_of_zeroing_the_redline(self):
        """牌價被改成 0 時不能讓門檻歸零（會變成天天狂告警）。"""
        broken = dict(self.ct._PRICING)
        broken["gemini-3.6-flash"] = (0.0, 0.0)
        with mock.patch.object(self.ct, "_PRICING", broken):
            self.assertEqual(self.da._flash_input_rate(), self.da._FLASH_INPUT_RATE_FALLBACK)

    def test_defaults_are_plain_floats(self):
        """_DEFAULTS 必須留成 str -> float，既有讀取端（測試、env override）才不會壞。"""
        for key, value in self.da._DEFAULTS.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, (int, float))


if __name__ == "__main__":
    unittest.main()
