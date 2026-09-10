from __future__ import annotations

import unittest


class CorrectionDetectorTests(unittest.TestCase):
    def test_matches_user_independent_check_phrase(self):
        """The actual phrase that triggered the Jalas sycophancy event."""
        from agent_core.correction_detector import detect_correction

        result = detect_correction("我查過jalas沒有用防水膜,你再確認一下")
        self.assertTrue(result.is_correction)
        self.assertEqual(result.matched_pattern, "user_independent_check")
        # The regex captures "我查過...沒有"; allow either negation variant.
        self.assertTrue(
            any(neg in result.matched_text for neg in ("沒有", "不是", "沒用"))
        )

    def test_matches_direct_error_assertion(self):
        from agent_core.correction_detector import detect_correction

        for phrase in ("你搞錯了", "你弄錯了", "你錯了", "你搞混了"):
            with self.subTest(phrase=phrase):
                result = detect_correction(f"{phrase}，那不是 Sympatex")
                self.assertTrue(result.is_correction)
                self.assertEqual(result.matched_pattern, "user_direct_error_assertion")

    def test_matches_recheck_demand(self):
        from agent_core.correction_detector import detect_correction

        for phrase in ("你再確認一下", "你再查一次", "麻煩你再核對", "再次確認"):
            with self.subTest(phrase=phrase):
                self.assertTrue(detect_correction(phrase).is_correction)

    def test_short_denial_only_matches_with_fact_keyword(self):
        """『不對』alone is ambiguous; require a material/spec keyword nearby
        so we don't flag every casual disagreement."""
        from agent_core.correction_detector import detect_correction

        self.assertTrue(detect_correction("不對，BOM 規格不是這樣").is_correction)
        self.assertTrue(detect_correction("這不對，料號弄錯了").is_correction)
        # No fact keyword nearby — should NOT match the short denial rule.
        # (May still match other patterns, but the short-denial one alone
        # should not fire.)
        out = detect_correction("不對，今天天氣不好")
        # We don't strictly require False here — but the matched_pattern
        # must NOT be the short-denial one.
        self.assertNotEqual(out.matched_pattern, "user_short_denial_with_fact")

    def test_ignores_innocuous_recheck_for_meeting_time(self):
        """『我會再確認會議時間』 isn't a correction — bias toward precision."""
        from agent_core.correction_detector import detect_correction

        result = detect_correction("我會再確認會議時間然後跟你說")
        self.assertFalse(result.is_correction)

    def test_ignores_completely_unrelated_chat(self):
        from agent_core.correction_detector import detect_correction

        for s in (
            "幫我查一下 Jalas 最近的報價",
            "明天早上有什麼會議？",
            "大王好",
            "",
            "   ",
        ):
            with self.subTest(s=s):
                self.assertFalse(detect_correction(s).is_correction)

    def test_skips_giant_pastes(self):
        """Forwarded emails / log dumps shouldn't be scanned."""
        from agent_core.correction_detector import detect_correction

        huge = "你錯了" + "x" * 3000
        result = detect_correction(huge)
        self.assertFalse(result.is_correction)

    def test_hint_includes_reanchored_workflow_steps(self):
        from agent_core.correction_detector import detect_correction

        result = detect_correction("我查過jalas沒有用防水膜,你再確認一下")
        hint = result.hint()
        self.assertIn("反翻供守則", hint)
        self.assertIn("query_bom", hint)
        self.assertIn("禁止「您說得對」", hint)

    def test_hint_is_empty_when_no_correction(self):
        from agent_core.correction_detector import detect_correction

        result = detect_correction("幫我查 Jalas 的報價")
        self.assertEqual(result.hint(), "")
        self.assertEqual(result.hint(is_owner=False), "")

    def test_employee_hint_reverify_without_owner_tools(self):
        """員工版 hint：同樣要求重查（最高指導原則），但不可出現大王稱謂、
        owner-only 工具建議、或 remember_correction_rule（owner-only 固化）。"""
        from agent_core.correction_detector import detect_correction

        result = detect_correction("庫存數量不對，你再確認一下")
        hint = result.hint(is_owner=False)
        self.assertIn("最高指導原則", hint)
        self.assertIn("員工", hint)
        self.assertIn("重查", hint)
        self.assertIn("查不到能確認的資料", hint)
        self.assertIn("禁止「您說得對」", hint)
        self.assertNotIn("大王", hint)
        self.assertNotIn("query_bom", hint)
        self.assertNotIn("remember_correction_rule", hint)


if __name__ == "__main__":
    unittest.main()
