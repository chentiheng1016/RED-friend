from __future__ import annotations

import unittest


class ExtractiveModeTests(unittest.TestCase):
    def test_recognises_canonical_jalas_lookup(self):
        """The exact question that triggered the original hallucination
        must now route through extractive mode."""
        from agent_core.extractive_mode import is_fact_lookup, extractive_addendum

        text = "jalas這個客戶有用防水膜嗎"
        self.assertTrue(is_fact_lookup(text))
        addendum = extractive_addendum(text)
        self.assertIn("萃取模式", addendum)
        self.assertIn("query_bom", addendum)
        self.assertIn("verify_claim", addendum)
        self.assertIn("禁止", addendum)

    def test_recognises_price_lookups(self):
        from agent_core.extractive_mode import is_fact_lookup

        for q in (
            "1155 的報價是多少？",
            "PU468 單價是多少",
            "報價多少 USD",
            "Jalas 1055 多少錢",
        ):
            with self.subTest(q=q):
                self.assertTrue(is_fact_lookup(q))

    def test_recognises_material_and_supplier_lookups(self):
        from agent_core.extractive_mode import is_fact_lookup

        for q in (
            "Jalas 用什麼材料",
            "1155 的料號是什麼",
            "華峰 PU468 的供應商是誰",
            "查一下 1055 的 BOM",
            "Jalas 的規格",
            "1155 的料號",
        ):
            with self.subTest(q=q):
                self.assertTrue(is_fact_lookup(q))

    def test_recognises_english_lookups(self):
        from agent_core.extractive_mode import is_fact_lookup

        for q in (
            "What material does Jalas use?",
            "What is the BOM for 1155?",
            "What's the price for PU468",
            "What supplier provides DRI-LEX?",
        ):
            with self.subTest(q=q):
                self.assertTrue(is_fact_lookup(q))

    def test_does_not_trigger_on_casual_chat(self):
        from agent_core.extractive_mode import is_fact_lookup, extractive_addendum

        for q in (
            "大王好",
            "明天會議幫我加個提醒",
            "幫我寄信給 UserAng",
            "Jalas 的會議我幫您加上了",
            "麻煩你寄出這份報價單",
            "",
            "   ",
        ):
            with self.subTest(q=q):
                self.assertFalse(is_fact_lookup(q))
                self.assertEqual(extractive_addendum(q), "")

    def test_skips_giant_pastes(self):
        from agent_core.extractive_mode import is_fact_lookup

        forwarded_email = "Jalas 用什麼材料" + "\n論述細節" * 500
        self.assertFalse(is_fact_lookup(forwarded_email))


if __name__ == "__main__":
    unittest.main()
