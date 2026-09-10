"""Regression: embedding token 估算長期低估 1.8 倍。

背景（2026-08-12）：對帳時發現帳本記的 embedding token 只有 GCP 實際計費的
一半上下。追下去不是漏記呼叫，是 `estimate_embed_tokens` 的係數錯了 ——
它的 docstring 寫「語料以 zh-TW + 英文混排為主」，但實際語料**只有 4–6% 是
CJK 字元**（drive 4.0%／gmail 6.2%／chat 6.3%），剩下九成多是料號、單號、
數字、表格、越南文，斷詞密度遠高於舊公式假設的「4 個字元 1 token」。

校準方法（要重做時照這個跑）：拿 `count_tokens(model="gemini-embedding-001")`
當真值，對 ChromaDB 真實語料抽樣，對 `actual ≈ A×CJK + B×其他` 最小平方擬合。
驗收看**總量**（記帳要的是加總對），用不同種子的獨立樣本：
舊公式估/實 0.56、新公式 1.01–1.02。

這個檔釘住方向與量級，不打真 API（純算術）。
"""
from __future__ import annotations

import os
import sys
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class CoefficientTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker

        self.ct = cost_tracker

    def test_non_cjk_is_two_chars_per_token_not_four(self):
        """舊值 0.25（4 字元 1 token）是這次低估的主因。"""
        self.assertAlmostEqual(self.ct._EMBED_TOKENS_PER_OTHER_CHAR, 0.50, places=3)
        self.assertGreater(self.ct._EMBED_TOKENS_PER_OTHER_CHAR, 0.25,
                           "掉回 4 字元/token 會讓 embedding 成本再度低估近一倍")

    def test_cjk_slightly_under_one_token_per_char(self):
        self.assertAlmostEqual(self.ct._EMBED_TOKENS_PER_CJK_CHAR, 0.82, places=3)
        self.assertLessEqual(self.ct._EMBED_TOKENS_PER_CJK_CHAR, 1.0)

    def test_cjk_costs_more_tokens_per_char_than_latin(self):
        self.assertGreater(self.ct._EMBED_TOKENS_PER_CJK_CHAR,
                           self.ct._EMBED_TOKENS_PER_OTHER_CHAR)


class EstimateTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker

        self.ct = cost_tracker

    def test_latin_text_uses_two_chars_per_token(self):
        # 600 字元純 ASCII → 約 300 tokens（舊公式會給 150）
        est = self.ct.estimate_embed_tokens("a" * 600)
        self.assertAlmostEqual(est, 300, delta=2)

    def test_cjk_text_uses_the_cjk_coefficient(self):
        est = self.ct.estimate_embed_tokens("中" * 600)
        self.assertAlmostEqual(est, 492, delta=2)  # 600 × 0.82

    def test_mixed_text_adds_both_parts(self):
        est = self.ct.estimate_embed_tokens("中" * 100 + "a" * 100)
        self.assertAlmostEqual(est, 100 * 0.82 + 100 * 0.50, delta=2)

    def test_realistic_chunk_lands_near_measured_density(self):
        """真實語料實測 1.5–2.4 字元/token；600 字元的 chunk 應落在這個帶內。"""
        chunk = ("PO#JF0P26080022 DECATHLON qty 1,200 pairs ETA 2026-09-15 "
                 "工廠 FUCHUN 出貨通知 ") * 8
        est = self.ct.estimate_embed_tokens(chunk[:600])
        self.assertGreater(est, 600 / 2.6)
        self.assertLess(est, 600 / 1.3)

    def test_batch_is_summed(self):
        one = self.ct.estimate_embed_tokens("a" * 200)
        three = self.ct.estimate_embed_tokens(["a" * 200] * 3)
        self.assertEqual(three, one * 3)

    def test_per_input_cap_still_applies(self):
        """單筆封頂不能被係數改動弄丟 —— 模型本身就只吃 2048 token。"""
        est = self.ct.estimate_embed_tokens("a" * 100_000)
        self.assertEqual(est, self.ct._EMBED_MAX_TOKENS_PER_INPUT)

    def test_empty_and_weird_inputs_are_safe(self):
        self.assertEqual(self.ct.estimate_embed_tokens(""), 0)
        self.assertEqual(self.ct.estimate_embed_tokens([]), 0)
        self.assertEqual(self.ct.estimate_embed_tokens(None), 0)
        self.assertGreaterEqual(self.ct.estimate_embed_tokens(["", "a"]), 1)

    def test_single_char_never_rounds_to_zero(self):
        """0.5 token 四捨五入後仍必須 ≥ 1，否則短查詢整批記成 0。"""
        self.assertGreaterEqual(self.ct.estimate_embed_tokens("a"), 1)

    def test_new_formula_beats_old_on_latin_heavy_corpus(self):
        """語料是 latin-heavy（實測 CJK 只佔 4–6%），舊公式必然低估。"""
        text = "a" * 570 + "中" * 30           # ≈ 5% CJK，貼近實際語料組成
        new = self.ct.estimate_embed_tokens(text)
        cjk, other = 30, 570
        old = cjk + (other + 3) // 4
        self.assertGreater(new, old * 1.5, "新公式應明顯高於舊公式（舊的低估 1.8×）")


if __name__ == "__main__":
    unittest.main()
