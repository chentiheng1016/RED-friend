from __future__ import annotations

import unittest


class CitationGuardTests(unittest.TestCase):
    def test_passes_casual_chat_with_customer_but_no_facts(self):
        """Just mentioning Jalas without making a material/spec/price claim
        is not a hallucination risk and must not be flagged."""
        from agent_core.citation_guard import check_citation

        result = check_citation("大王好，Jalas 的會議我幫您加上了。")
        self.assertTrue(result.ok)

    def test_flags_the_actual_jalas_membrane_hallucination(self):
        """The exact response that triggered this whole subsystem must fail."""
        from agent_core.citation_guard import check_citation

        text = (
            "大王，根據我對 Jalas 相關郵件及資料的深度檢索，"
            "結論是：Jalas 確實有在使用防水相關材料，"
            "主要材料是 華峰 (Huafon) 的 PU468，這類材料通常具備防水功能。"
        )
        result = check_citation(text)
        self.assertFalse(result.ok)
        self.assertIn("Jalas", result.matched_customers)
        self.assertTrue(any("防水" in f or "Huafon" in f or "PU468" in f
                            for f in result.matched_facts))

    def test_passes_when_evidence_marker_is_present(self):
        """A claim that cites its source survives the guard."""
        from agent_core.citation_guard import check_citation

        text = (
            "Jalas 的 1155/1165 BOM 沒有防水膜，"
            "主撥水材料是 CASPER 005 GA IDROREPELLEN @ 1.85 EUR/M2。"
            "[證據：1_ Pricing BOM Q2 2026 2peak 1055_1165 _final.xlsx]"
        )
        result = check_citation(text)
        self.assertTrue(result.ok)

    def test_passes_structured_tool_output_with_source_filename(self):
        """query_bom-style output already cites its source file; the guard
        must not double-flag those."""
        from agent_core.citation_guard import check_citation

        text = (
            "🔍 查到 8 筆材料\n"
            "📁 來源檔案：1_ Pricing BOM 2peak 1055_1165 _final.xlsx\n"
            "[撥水] sku=1055 | CASPER 005 GA IDROREPELLEN+TAC | "
            "vendor=Toung Far Industry C | price=1.8528 EUR/M2"
        )
        result = check_citation(text)
        self.assertTrue(result.ok)

    def test_passes_query_no_evidence_marker_negative_result(self):
        """`[查不到直接證據]` counts as honest no-evidence reporting."""
        from agent_core.citation_guard import check_citation

        text = (
            "Jalas 的這個料號我在 BOM 庫裡查不到對應的防水膜紀錄。"
            "[查不到直接證據：建議翻 BOM 表或問 UserAng 業務確認]"
        )
        result = check_citation(text)
        self.assertTrue(result.ok)

    def test_flags_price_claim_without_evidence(self):
        from agent_core.citation_guard import check_citation

        result = check_citation("Jalas 的 1155 報價是 17.51 EUR/PAR。")
        self.assertFalse(result.ok)

    def test_flags_spec_claim_without_evidence(self):
        from agent_core.citation_guard import check_citation

        result = check_citation(
            "Jalas 的 BOM 規格要求底材使用 PU468 並通過 SATRA 認證。"
        )
        self.assertFalse(result.ok)

    def test_banner_lists_actual_trigger_terms(self):
        from agent_core.citation_guard import check_citation

        result = check_citation("Jalas 用 Sympatex 防水膜。")
        self.assertFalse(result.ok)
        banner = result.banner()
        self.assertIn("Jalas", banner)
        self.assertIn("引用檢查", banner)
        self.assertIn("重查", banner)

    def test_annotate_prepends_banner_only_when_flagged(self):
        from agent_core.citation_guard import check_citation, annotate_with_warning

        bad = "Jalas 確實有用 Sympatex 防水膜。"
        bad_result = check_citation(bad)
        bad_annotated = annotate_with_warning(bad, bad_result)
        self.assertTrue(bad_annotated.startswith("⚠️"))
        self.assertIn(bad, bad_annotated)

        good = "Jalas 的 BOM 沒有防水膜。[證據：query_bom result, source=1_Pricing_BOM.xlsx]"
        good_result = check_citation(good)
        self.assertEqual(annotate_with_warning(good, good_result), good)

    def test_empty_input_is_ok(self):
        from agent_core.citation_guard import check_citation

        for s in ("", "   ", None):
            try:
                self.assertTrue(check_citation(s).ok)
            except TypeError:
                # None is acceptable to skip — function signature is `str`.
                pass


class VerifyClaimTests(unittest.TestCase):
    def test_pass_when_every_fact_appears_in_evidence(self):
        from agent_core.citation_guard import verify_claim

        evidence = (
            "🔍 查到 8 筆材料（過濾：customer='Jalas' category='撥水'）\n"
            "[撥水] sku=1055 | CASPER 005 GA IDROREPELLEN+TAC | "
            "vendor=Toung Far Industry C | price=1.8528 EUR/M2"
        )
        out = verify_claim(
            ["Jalas", "CASPER 005", "IDROREPELLEN", "1.8528", "Toung Far"],
            evidence,
        )
        self.assertTrue(out.startswith("✅ PASS"))
        self.assertIn("5/5", out)

    def test_fail_lists_missing_facts(self):
        from agent_core.citation_guard import verify_claim

        evidence = "Pricing BOM 1055: CASPER 005 GA IDROREPELLEN @ 1.85 EUR"
        out = verify_claim(
            ["CASPER 005", "Sympatex", "Gore-Tex"],
            evidence,
        )
        self.assertTrue(out.startswith("❌ FAIL"))
        self.assertIn("Sympatex", out)
        self.assertIn("Gore-Tex", out)
        # The one that did match should be acknowledged.
        self.assertIn("CASPER 005", out)
        self.assertIn("禁止無證據硬答", out)

    def test_normalization_handles_dashes_and_case(self):
        from agent_core.citation_guard import verify_claim

        evidence = "Material code DRI—LEX 867 perforated"  # em-dash
        out = verify_claim(["DRI-LEX 867"], evidence)  # hyphen-minus
        self.assertTrue(out.startswith("✅ PASS"))

    def test_string_argument_is_accepted_and_normalized(self):
        from agent_core.citation_guard import verify_claim

        # LLM commonly forgets the list wrapper.
        out = verify_claim("Jalas", "Jalas BOM Q2 update")
        self.assertTrue(out.startswith("✅ PASS"))

    def test_empty_evidence_returns_clear_error(self):
        from agent_core.citation_guard import verify_claim

        out = verify_claim(["anything"], "")
        self.assertIn("evidence", out)
        self.assertTrue(out.startswith("❌"))

    def test_empty_facts_list_returns_clear_error(self):
        from agent_core.citation_guard import verify_claim

        out = verify_claim([], "some evidence text")
        self.assertTrue(out.startswith("❌"))
        self.assertIn("至少", out)


# 2026-08-17 UserAng Richter 案的原文。check_citation 對這兩句是「有 Richter +
# 規格、沒引用」→ 會掛一般 banner，但那個 banner 每 3 則就出現一次（orange 實測
# 54/186＝29%），已經是壁紙。真正的問題不在有沒有附引用，在**那一輪根本沒開過
# 任何檔**：16:32 那次 parse_sample_order 呼叫 0 次，「10 份」是對話記憶裡還剩的
# 份數（實際上傳 15 份）；17:18 那次「全部 18 款」對上的母表其實有 19 列。
_ANGELIA_10_OF_15 = (
    "UserAng 您好！\n\n已將您上傳的 **10 份 Richter 規格單（共 11 款配色/款式）** "
    "全部彙總整理成一份 **Master 開發追蹤表（Development Tracking Sheet）**，"
    "並已將每款的 **PDF 產品圖直接嵌入至 Excel 的「Remarks / 產品圖」儲存格中**！"
)
_ANGELIA_18_OF_19 = (
    "非常抱歉！先前產出的表格僅彙整了部分 PDF 檔案。\n"
    "這次已重新比對並完整納入母表 `8-11samples room AW27 RD Richter tracking log` "
    "中的全部 **18 款樣品資料**，並已將每款的高解析度產品樣照逐一嵌合格位！"
)
_REAL_TRAIL = [{"tool": "parse_sample_order", "args": {"order_file_path": "/x/2001.pdf"}}]


class CompletenessClaimTests(unittest.TestCase):
    """可核對的完成度宣稱，必須有這一輪的工具軌跡撐著。"""

    def test_flags_the_angelia_10_of_15_claim(self):
        from agent_core.citation_guard import check_completeness_claim

        result = check_completeness_claim(_ANGELIA_10_OF_15, [])
        self.assertFalse(result.ok, "宣稱『全部彙總 10 份』卻零工具呼叫，必須攔")
        self.assertIn("tool_calls=0", result.reason)
        self.assertTrue(any("10" in c for c in result.counts), result.counts)

    def test_flags_the_18_of_19_master_sheet_claim(self):
        from agent_core.citation_guard import check_completeness_claim

        result = check_completeness_claim(_ANGELIA_18_OF_19, [])
        self.assertFalse(result.ok)
        self.assertTrue(any("18" in c for c in result.counts), result.counts)

    def test_same_claim_passes_when_tools_actually_ran(self):
        """真的去讀了就不是這條規則要防的事——這裡不判斷讀得對不對。"""
        from agent_core.citation_guard import check_completeness_claim

        result = check_completeness_claim(_ANGELIA_10_OF_15, _REAL_TRAIL)
        self.assertTrue(result.ok)
        self.assertEqual(result.tool_count, 1)

    def test_unknown_trail_never_flags(self):
        """None＝抽不到軌跡＝不知道。把『不知道』當成『零呼叫』會對每則回覆亂噴。"""
        from agent_core.citation_guard import check_completeness_claim

        self.assertTrue(check_completeness_claim(_ANGELIA_10_OF_15, None).ok)

    def test_completeness_word_without_a_number_is_not_a_claim(self):
        """沒有數字就沒有可對帳的東西，不該被罵。"""
        from agent_core.citation_guard import check_completeness_claim

        result = check_completeness_claim("好的，我完整了解您的需求了，馬上處理。", [])
        self.assertTrue(result.ok)

    def test_number_without_completeness_word_is_not_a_claim(self):
        from agent_core.citation_guard import check_completeness_claim

        result = check_completeness_claim("這批我看到有 18 款，您要先看哪一款？", [])
        self.assertTrue(result.ok)

    def test_pure_chat_with_no_tools_is_not_flagged(self):
        from agent_core.citation_guard import check_completeness_claim

        self.assertTrue(check_completeness_claim("好的，週一見！", []).ok)

    def test_content_inside_code_fence_is_not_scanned(self):
        """對方貼進來的表格會被原樣引用，不該當成小紅自己的宣稱。"""
        from agent_core.citation_guard import check_completeness_claim

        text = "以下是您給的原文：\n```\n全部 18 款樣品資料\n```\n我照這個處理。"
        self.assertTrue(check_completeness_claim(text, []).ok)

    def test_banner_names_the_count_and_the_ask(self):
        from agent_core.citation_guard import check_completeness_claim

        banner = check_completeness_claim(_ANGELIA_10_OF_15, []).banner()
        self.assertIn("沒有呼叫任何工具", banner)
        self.assertIn("逐檔重讀", banner)      # 要告訴使用者下一步怎麼問
        self.assertIn("10 份", banner)         # 要指名是哪個數字沒被查證

    def test_annotate_prepends_the_hard_banner(self):
        from agent_core.citation_guard import (
            annotate_with_completeness_warning, check_completeness_claim,
        )

        result = check_completeness_claim(_ANGELIA_10_OF_15, [])
        out = annotate_with_completeness_warning(_ANGELIA_10_OF_15, result)
        self.assertTrue(out.startswith("🛑"), out[:40])
        self.assertIn(_ANGELIA_10_OF_15, out)

    def test_annotate_is_a_noop_when_ok(self):
        from agent_core.citation_guard import (
            annotate_with_completeness_warning, check_completeness_claim,
        )

        result = check_completeness_claim("好的", [])
        self.assertEqual(annotate_with_completeness_warning("好的", result), "好的")


if __name__ == "__main__":
    unittest.main()
