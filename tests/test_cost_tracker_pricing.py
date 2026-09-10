"""Regression (review finding #14): _price_for_model fell back to the cheapest
flash-LITE rate for any model whose prefix wasn't in _PRICING. The documented
background-fleet model `gemini-flash-latest` (and `gemini-pro-latest`) matched
no prefix → billed at flash-lite, under-reporting spend several-fold and
starving the monthly-cap alert. Pin the family-based fallback.
"""
from __future__ import annotations

import os
import sys
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class PriceForModelTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker

        self.ct = cost_tracker

    def test_flash_latest_uses_billed_36_flash_rate_not_lite(self):
        # fleet 的 gemini-flash-latest 實際結在 3.6 flash SKU，_PRICING 直接掛
        # 顯式別名，不再靠家族 fallback 掉到便宜的 lite 價。
        # （費率 2026-08-05 由 TWD 改為官方 USD 1.50/7.50，見幣別註解。）
        rate = self.ct._price_for_model("gemini-flash-latest")
        self.assertEqual(rate, self.ct._PRICING["gemini-3.6-flash"])
        self.assertNotEqual(rate, self.ct._PRICING["gemini-2.5-flash-lite"])

    def test_unknown_flash_alias_falls_back_to_billed_36_rate(self):
        # 家族 fallback 也走帳單實收價：未知 flash 別名多半解析到最新版。
        self.assertEqual(
            self.ct._price_for_model("gemini-flash-preview-9.9"),
            self.ct._PRICING["gemini-3.6-flash"],
        )

    def test_pro_latest_uses_pro_rate(self):
        self.assertEqual(
            self.ct._price_for_model("gemini-pro-latest"),
            self.ct._PRICING["gemini-2.5-pro"],
        )

    def test_flash_lite_alias_still_lite(self):
        # An alias that explicitly says flash-lite must keep the lite rate.
        self.assertEqual(
            self.ct._price_for_model("gemini-flash-lite-latest"),
            self.ct._PRICING["gemini-2.5-flash-lite"],
        )

    def test_known_exact_prefixes_unchanged(self):
        self.assertEqual(self.ct._price_for_model("gemini-2.5-pro"), self.ct._PRICING["gemini-2.5-pro"])
        self.assertEqual(
            self.ct._price_for_model("gemini-2.5-flash-lite"),
            self.ct._PRICING["gemini-2.5-flash-lite"],
        )

    def test_truly_unknown_falls_back_to_lite(self):
        self.assertEqual(
            self.ct._price_for_model("some-other-vendor-model"),
            self.ct._PRICING["gemini-2.5-flash-lite"],
        )

    def test_flash_latest_cost_is_higher_than_old_lite_estimate(self):
        # 1M output tokens: the bug undercounted these by ~3x.
        lite = self.ct.compute_cost_usd("gemini-2.5-flash-lite", 0, 1_000_000)
        latest = self.ct.compute_cost_usd("gemini-flash-latest", 0, 1_000_000)
        self.assertGreater(latest, lite)

    def test_embedding_uses_official_usd_rate(self):
        # 🚨 2026-08-05：舊值 4.75 其實是**新台幣**（4.86 TWD/M）。官方 USD
        # 牌價 EmbedContent gemini-embedding-001 = 0.15 USD/M、output 免費。
        self.assertEqual(
            self.ct._price_for_model("gemini-embedding-001"),
            self.ct._PRICING["gemini-embedding"],
        )
        self.assertAlmostEqual(
            self.ct.compute_cost_usd("gemini-embedding-001", 1_000_000, 0),
            0.15,
        )


class CachedInputPricingTests(unittest.TestCase):
    """cached input token 的折扣係數（2026-08-04 成本查帳）。

    預設 0.10 來自 GCP Cloud Billing Catalog API 上同一模型的兩條 SKU：
    gemini 3.6 flash text 一般 input 1.50 USD/M vs cached input 0.15 USD/M
    ＝正好十分之一（TWD 檔為 48.56 vs 4.86，比值相同）。_PRICING 存的是**未
    快取**那條的價，折扣沒有被吸收進去 —— 帳本過去把 cached 全額計價是純粹
    高估對話類 caller。
    """

    def setUp(self):
        from agent_core import cost_tracker
        self.ct = cost_tracker

    def test_default_multiplier_matches_official_sku_ratio(self):
        """預設 0.10 = GCP Catalog API 上 cached / 非 cached 兩條 SKU 的比值
        （4.86 / 48.56 TWD/M gemini 3.6 flash text），不是拍腦袋的業界值。"""
        self.assertAlmostEqual(self.ct._CACHED_INPUT_MULTIPLIER, 0.10, places=4)

    def test_zero_cached_is_identical_to_old_signature(self):
        # 沒有快取命中時，新舊簽名必須算出一模一樣的數字
        plain = self.ct.compute_cost_usd("gemini-3.6-flash", 100_000, 1_000)
        with_zero = self.ct.compute_cost_usd(
            "gemini-3.6-flash", 100_000, 1_000, cached_tokens=0)
        self.assertEqual(plain, with_zero)

    def test_cached_portion_is_ten_times_cheaper(self):
        rate = self.ct._PRICING["gemini-3.6-flash"][0]
        cached_only = self.ct.compute_cost_usd(
            "gemini-3.6-flash", 1_000_000, 0, cached_tokens=1_000_000)
        fresh_only = self.ct.compute_cost_usd("gemini-3.6-flash", 1_000_000, 0)
        self.assertAlmostEqual(fresh_only, rate, places=6)
        self.assertAlmostEqual(cached_only, rate * 0.10, places=6)

    def test_discount_applies_only_to_cached_portion(self):
        from unittest import mock
        with mock.patch.object(self.ct, "_CACHED_INPUT_MULTIPLIER", 0.25):
            cost = self.ct.compute_cost_usd(
                "gemini-3.6-flash", 100_000, 0, cached_tokens=80_000)
        rate = self.ct._PRICING["gemini-3.6-flash"][0]
        expected = (20_000 / 1e6) * rate + (80_000 / 1e6) * rate * 0.25
        self.assertAlmostEqual(cost, round(expected, 6), places=6)

    def test_full_cache_hit_at_zero_multiplier_costs_only_output(self):
        from unittest import mock
        with mock.patch.object(self.ct, "_CACHED_INPUT_MULTIPLIER", 0.0):
            cost = self.ct.compute_cost_usd(
                "gemini-3.6-flash", 50_000, 1_000, cached_tokens=50_000)
        out_rate = self.ct._PRICING["gemini-3.6-flash"][1]
        self.assertAlmostEqual(cost, round((1_000 / 1e6) * out_rate, 6), places=6)

    def test_cached_greater_than_prompt_is_clamped_not_negative(self):
        # API 偶爾回報 cached > prompt；夾不住的話 fresh 變負數＝負成本
        from unittest import mock
        with mock.patch.object(self.ct, "_CACHED_INPUT_MULTIPLIER", 0.0):
            cost = self.ct.compute_cost_usd(
                "gemini-3.6-flash", 1_000, 0, cached_tokens=9_999_999)
        self.assertEqual(cost, 0.0)

    def test_negative_and_none_inputs_are_safe(self):
        self.assertEqual(
            self.ct.compute_cost_usd("gemini-3.6-flash", -5, -5, cached_tokens=-5), 0.0)
        self.assertEqual(
            self.ct.compute_cost_usd("gemini-3.6-flash", None, None, cached_tokens=None),
            0.0)

    def test_record_call_passes_cached_through(self):
        """record_call 要把 cached 傳下去，否則係數改了也不生效。"""
        from types import SimpleNamespace
        from unittest import mock
        seen = {}

        def fake_compute(model, prompt, output, cached_tokens=0):
            seen.update(model=model, prompt=prompt, output=output,
                        cached=cached_tokens)
            return 0.0

        usage = SimpleNamespace(
            prompt_token_count=100, candidates_token_count=10,
            cached_content_token_count=77, thoughts_token_count=5,
            tool_use_prompt_token_count=0, total_token_count=115,
        )
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # 導到暫存檔，別汙染主 checkout 的 var/data/cost/cost.jsonl
            with mock.patch.object(self.ct, "compute_cost_usd", fake_compute), \
                    mock.patch.object(self.ct, "_COST_LOG",
                                      os.path.join(tmp, "cost.jsonl")):
                self.ct.record_call("gemini-3.6-flash", usage, caller="t")
        self.assertEqual(seen.get("cached"), 77)
        self.assertEqual(seen.get("output"), 15)   # candidates + thinking


class CurrencyIsUsdTests(unittest.TestCase):
    """🚨 幣別回歸（2026-08-05）。

    2026-08-01 那次校正把 GCP 帳單上的**新台幣**金額當成美元寫進 _PRICING
    （該帳單帳戶 currencyCode="TWD"），整本帳被放大 ~32 倍、所有告警門檻也跟著
    訂在 TWD 刻度。這組測試釘住「表內數字是美元」，避免再被同樣的方式改回去。

    對照值全部來自 GCP Cloud Billing Catalog API（services/AEFD-7695-64FA
    「Gemini API」）2026-08-05 實查的官方牌價。
    """

    # (model, input_usd_per_M, output_usd_per_M, 對應的 TWD 值)
    OFFICIAL = [
        ("gemini-3.6-flash", 1.50, 7.50, 48.56),
        ("gemini-flash-latest", 1.50, 7.50, 48.56),
        ("gemini-embedding", 0.15, 0.0, 4.86),
        ("gemini-2.5-pro", 1.25, 10.00, None),
        ("gemini-2.5-flash", 0.30, 2.50, None),
        ("gemini-2.5-flash-lite", 0.10, 0.40, None),
    ]

    def setUp(self):
        from agent_core import cost_tracker
        self.ct = cost_tracker

    def test_rates_match_official_usd_catalog(self):
        for model, want_in, want_out, _twd in self.OFFICIAL:
            got_in, got_out = self.ct._PRICING[model]
            self.assertAlmostEqual(got_in, want_in, places=4, msg=f"{model} input")
            self.assertAlmostEqual(got_out, want_out, places=4, msg=f"{model} output")

    def test_no_entry_is_still_on_the_twd_scale(self):
        """任何一條掉回 TWD 刻度（≈32× 美元牌價）都要立刻紅。"""
        for model, want_in, _want_out, twd in self.OFFICIAL:
            if twd is None:
                continue
            got_in = self.ct._PRICING[model][0]
            self.assertNotAlmostEqual(
                got_in, twd, places=1,
                msg=f"{model} 的 input 費率是新台幣值 {twd}，不是美元 {want_in}")

    def test_flash_input_is_cheaper_than_pro_input(self):
        """結構性 sanity check：flash 的 input 不可能比 pro 貴。
        舊表 flash 47.80 > pro 40.00 正是幣別混用（flash 是 TWD、pro 也是 TWD
        但取自不同檔位）留下的破綻。"""
        flash_in = self.ct._PRICING["gemini-3.6-flash"][0]
        pro_in = self.ct._PRICING["gemini-2.5-pro"][0]
        self.assertLessEqual(flash_in, pro_in * 2)

    def test_daily_budget_default_is_usd_scale(self):
        import inspect
        default = inspect.signature(self.ct.cost_alert).parameters["daily_budget_usd"].default
        self.assertLess(default, 100.0, "每日預算預設仍是 TWD 刻度（舊值 700）")
        self.assertGreater(default, 1.0)

    def test_alert_thresholds_are_usd_scale(self):
        from agent_core import dashboard_alerts
        warn = dashboard_alerts._DEFAULTS["cost_today_warn_usd"]
        crit = dashboard_alerts._DEFAULTS["cost_today_crit_usd"]
        self.assertLess(warn, 100.0, "warn 門檻仍是 TWD 刻度（舊值 800）")
        self.assertLess(crit, 200.0, "crit 門檻仍是 TWD 刻度（舊值 1200）")
        self.assertLess(warn, crit)

    def test_trend_cutover_is_off_now_that_rows_are_normalised(self):
        """cutover 日期過濾已由讀取端正規化取代，預設關閉。

        原本這個測試釘的是「cutover 要 ≥ 2026-08-05」，用來把 TWD 刻度的舊列
        擋在均值外。現在 _normalize_entry_costs 會把每列重算成當前紀元，歷史列
        不再有刻度差；繼續把 cutover 往後推只會讓 7d/30d 均值失去所有基準日
        （實測 avg7 = avg30 = 0.0），連帶讓 cost_ratio 紅線靜默失效。
        """
        from agent_core import dashboard_trends
        self.assertEqual(dashboard_trends._PRICING_CUTOVER_DEFAULT, "")


if __name__ == "__main__":
    unittest.main()
