"""Hallucination regression suite — golden Q&A pairs that exercise every
defense layer at once.

What this guards against: as new tools, models, and persona edits land,
nothing should silently undo the protections we built up. The suite picks
the exact failure patterns that bit us (Jalas 防水膜, paraphrased PU →
membrane jump, sycophantic flip on user pushback) and asserts that each
defense layer still flags them.

Why this lives alongside the other unit tests instead of as a CRON: the
defense layers themselves are deterministic — citation_guard, recall
tiering, correction_detector, extractive_mode, verify_claim. Running them
on a fixed golden set gives us regression coverage without needing a live
Gemini call. The LLM behaviour itself is still observed in production
through the daemon's stdout (the [citation_guard] / [correction] /
[extractive] log lines).

Each test below names the exact incident or attack pattern it locks down,
so a future maintainer who breaks a check sees in the failure message
which historical bug just regressed.
"""
from __future__ import annotations

import unittest


# ─── Golden incident corpus ────────────────────────────────────────────
# Each entry is (incident_id, user_question, model_reply, notes).
# These are real or representative snippets we want every layer to keep
# flagging. The dict is keyed for easy referencing in failure messages.

HALLUCINATION_INCIDENTS: dict[str, dict[str, str]] = {
    "jalas_membrane_2026_05_20": {
        "user_q": "jalas這個客戶有用防水膜嗎",
        "bad_reply": (
            "大王，根據我對 Jalas 相關郵件及資料的深度檢索，"
            "Jalas 確實有在使用防水相關材料，主要材料是華峰 (Huafon) 的 PU468。"
        ),
        "good_reply": (
            "Jalas 的 1055/1065/1155/1165 BOM 沒有任何防水膜（Sympatex / "
            "Gore-Tex 類）。主撥水材料是 CASPER 005 GA IDROREPELLEN+TAC "
            "@ 1.85 EUR/M2 from Toung Far。"
            "[證據：1_ Pricing BOM Q2 2026 2peak 1055_1165 _final.xlsx]"
        ),
        "user_correction": "我查過jalas沒有用防水膜,你再確認一下",
    },
    "blaklader_sympatex_invented": {
        "user_q": "Blaklader 有用 Sympatex 嗎？",
        "bad_reply": (
            "Blaklader 在 2026 年的訂單中確實採用 Sympatex W55 防水膜，"
            "用於高階雪鞋系列。"
        ),
        "good_reply": (
            "BOM 庫裡查不到 Blaklader 的 Sympatex 紀錄。"
            "[查不到直接證據：建議翻 BOM 表或問 UserAng 業務確認]"
        ),
        "user_correction": "Blaklader 沒有用 Sympatex 啊，你搞錯了",
    },
    "fabricated_price": {
        "user_q": "1155 報價多少？",
        "bad_reply": "1155 的單價是 25 EUR/PAR，主要是材料漲價拉高了 BOM 成本。",
        "good_reply": (
            "1155 BOM 總成本為 13.51 EUR/PAR。"
            "[證據：1_ Pricing BOM Q2 2026 2peak 1055_1165 _final.xlsx, sheet 1155]"
        ),
        "user_correction": "1155 報價不對，再確認一下",
    },
}


class CitationGuardRegressionTests(unittest.TestCase):
    """The pre-send banner must still flag every historical bad reply."""

    def test_every_bad_reply_is_flagged_as_missing_citation(self):
        from agent_core.citation_guard import check_citation

        for incident_id, incident in HALLUCINATION_INCIDENTS.items():
            with self.subTest(incident=incident_id):
                result = check_citation(incident["bad_reply"])
                self.assertFalse(
                    result.ok,
                    msg=(
                        f"REGRESSION: citation_guard no longer flags the "
                        f"{incident_id} bad reply. Either the trigger keywords"
                        f" moved or evidence detection got too lenient."
                    ),
                )

    def test_every_good_reply_passes(self):
        from agent_core.citation_guard import check_citation

        for incident_id, incident in HALLUCINATION_INCIDENTS.items():
            with self.subTest(incident=incident_id):
                result = check_citation(incident["good_reply"])
                self.assertTrue(
                    result.ok,
                    msg=(
                        f"REGRESSION: good reply for {incident_id} now fails "
                        f"the citation guard. The detector may be too strict. "
                        f"Reason: {result.reason}"
                    ),
                )


class CorrectionDetectorRegressionTests(unittest.TestCase):
    """Every recorded user-correction phrase must still trigger the detector."""

    def test_every_recorded_correction_is_detected(self):
        from agent_core.correction_detector import detect_correction

        for incident_id, incident in HALLUCINATION_INCIDENTS.items():
            with self.subTest(incident=incident_id):
                result = detect_correction(incident["user_correction"])
                self.assertTrue(
                    result.is_correction,
                    msg=(
                        f"REGRESSION: correction_detector missed "
                        f"{incident_id} user correction: "
                        f"{incident['user_correction']!r}"
                    ),
                )


class ExtractiveModeRegressionTests(unittest.TestCase):
    """Every recorded user question must route through extractive mode."""

    def test_every_recorded_question_triggers_extractive_mode(self):
        from agent_core.extractive_mode import is_fact_lookup

        for incident_id, incident in HALLUCINATION_INCIDENTS.items():
            with self.subTest(incident=incident_id):
                self.assertTrue(
                    is_fact_lookup(incident["user_q"]),
                    msg=(
                        f"REGRESSION: extractive_mode no longer recognises "
                        f"{incident_id} as a fact-lookup question: "
                        f"{incident['user_q']!r}"
                    ),
                )


class VerifyClaimRegressionTests(unittest.TestCase):
    """verify_claim must catch fabricated facts not present in evidence."""

    def test_jalas_bad_reply_claims_fail_against_real_bom_evidence(self):
        """The fabricated 'PU468 防水膜' answer must fail verify_claim
        against the actual BOM tool output."""
        from agent_core.citation_guard import verify_claim

        # Simulate what query_bom would actually return for Jalas + 撥水.
        real_evidence = (
            "🔍 查到 8 筆材料（過濾：customer='Jalas' category='撥水'）\n"
            "📁 來源檔案：1_ Pricing BOM Q2 2026 2peak 1055_1165 _final.xlsx\n"
            "[撥水] sku=1055 | CASPER 005 GA IDROREPELLEN+TAC | "
            "vendor=Toung Far Industry C | price=1.8528 EUR/M2"
        )

        # The bad reply claimed Jalas uses 防水膜 + Huafon PU468 + Sympatex.
        # None of those terms appear in the actual evidence.
        fabricated_facts = ["防水膜", "Sympatex", "Huafon", "PU468"]
        out = verify_claim(fabricated_facts, real_evidence)
        self.assertTrue(out.startswith("❌ FAIL"))
        for term in fabricated_facts:
            self.assertIn(term, out)

    def test_real_jalas_facts_pass_against_evidence(self):
        from agent_core.citation_guard import verify_claim

        real_evidence = (
            "[撥水] sku=1055 | CASPER 005 GA IDROREPELLEN+TAC | "
            "vendor=Toung Far Industry C | price=1.8528 EUR/M2"
        )
        out = verify_claim(
            ["CASPER 005", "IDROREPELLEN", "1.8528", "Toung Far"],
            real_evidence,
        )
        self.assertTrue(out.startswith("✅ PASS"))


class FullPipelineRegressionTest(unittest.TestCase):
    """At least one defense layer must catch every historical incident.

    This is the meta-assertion: even if no single layer is perfect, the
    combined stack of (extractive_mode, citation_guard, correction_detector,
    verify_claim) must engage on every recorded bad-reply scenario.
    """

    def test_at_least_one_layer_flags_each_incident(self):
        from agent_core.citation_guard import check_citation
        from agent_core.correction_detector import detect_correction
        from agent_core.extractive_mode import is_fact_lookup

        for incident_id, incident in HALLUCINATION_INCIDENTS.items():
            with self.subTest(incident=incident_id):
                triggered = []
                if is_fact_lookup(incident["user_q"]):
                    triggered.append("extractive_mode")
                if not check_citation(incident["bad_reply"]).ok:
                    triggered.append("citation_guard")
                if detect_correction(incident["user_correction"]).is_correction:
                    triggered.append("correction_detector")
                self.assertTrue(
                    triggered,
                    msg=(
                        f"REGRESSION: NO defense layer engaged on "
                        f"{incident_id}. The anti-hallucination stack has "
                        f"a hole — review what changed in extractive_mode, "
                        f"citation_guard, or correction_detector."
                    ),
                )


if __name__ == "__main__":
    unittest.main()
