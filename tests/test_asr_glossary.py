"""asr_glossary：收割、排序、轉錄修正（確定性＋LLM 約束式）的單元測試。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_core import asr_glossary as ag


def _resp(text: str):
    r = mock.MagicMock()
    r.text = text
    return r


class GlossaryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(ag, "_GLOSSARY_PATH",
                              os.path.join(self._tmp.name, "asr_glossary.json")),
            mock.patch.object(ag, "_EXTRA_PATH",
                              os.path.join(self._tmp.name, "extra.txt")),
            mock.patch.object(ag, "_ERP_SCHEMA_PATH",
                              os.path.join(self._tmp.name, "erp.md")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_add_terms_and_priority_upgrade(self):
        self.assertEqual(ag.add_terms(["針車", "大底", ""], priority=ag._P_HARVESTED), 2)
        # 同詞用更高優先級再加 → 升級不降級
        ag.add_terms(["針車"], priority=ag._P_MANUAL)
        terms = ag.load_glossary()
        self.assertEqual(terms["針車"]["priority"], ag._P_MANUAL)
        self.assertEqual(terms["針車"]["count"], 2)

    def test_top_terms_priority_then_count(self):
        ag.add_terms(["harvested_hot"], priority=ag._P_HARVESTED)
        ag.add_terms(["harvested_hot"], priority=ag._P_HARVESTED)
        ag.add_terms(["harvested_cold"], priority=ag._P_HARVESTED)
        ag.add_terms(["manual_term"], priority=ag._P_MANUAL)
        out = ag.top_terms(2)
        self.assertEqual(out[0], "manual_term")       # 人工優先
        self.assertEqual(out[1], "harvested_hot")     # 同級比 count
        self.assertEqual(len(ag.top_terms(100)), 3)

    def test_mine_code_tokens(self):
        counts = ag.mine_code_tokens([
            "開 FTE_570 畫面，join key 是 SE_ID；FTE_570 再點一次",
            "BQ_SE_ORDITEM 是訂單主檔。lower_case 不算，ABC 單段也不算",
        ])
        self.assertEqual(counts["FTE_570"], 2)
        self.assertEqual(counts["SE_ID"], 1)
        self.assertIn("BQ_SE_ORDITEM", counts)
        self.assertNotIn("ABC", counts)

    def test_harvest_merges_all_sources(self):
        with open(ag._EXTRA_PATH, "w", encoding="utf-8") as f:
            f.write("# 註解行\n收櫃入庫\n針車\n")
        with open(ag._ERP_SCHEMA_PATH, "w", encoding="utf-8") as f:
            f.write("| 9 | **SE_ID** | VARCHAR2(20) | N | 訂單單號 |\n")
        with mock.patch.object(ag, "_correction_terms",
                               return_value=["Mixbus 不對，是收櫃"]):
            r = ag.harvest_glossary(extra_texts=["畫面代碼 FTE_570"])
        self.assertTrue(r["ok"])
        terms = ag.load_glossary()
        self.assertEqual(terms["收櫃入庫"]["priority"], ag._P_MANUAL)
        self.assertEqual(terms["Mixbus 不對，是收櫃"]["priority"], ag._P_CORRECTION)
        self.assertEqual(terms["SE_ID"]["priority"], ag._P_HARVESTED)
        self.assertIn("FTE_570", terms)

    def test_harvest_cap(self):
        many = " ".join(f"TOK_{i:03d}" for i in range(50))
        with mock.patch.object(ag, "_correction_terms", return_value=[]), \
                mock.patch.object(ag, "_HARVEST_CAP", 10):
            ag.harvest_glossary(extra_texts=[many])
        self.assertEqual(len(ag.load_glossary()), 10)

    def test_load_corrupt_file(self):
        with open(ag._GLOSSARY_PATH, "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertEqual(ag.load_glossary(), {})


class CorrectTranscriptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(ag, "_GLOSSARY_PATH",
                              os.path.join(self._tmp.name, "asr_glossary.json")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_empty_text(self):
        self.assertFalse(ag.correct_transcript("  ")["ok"])

    def test_deterministic_only_when_llm_off(self):
        with mock.patch.object(
                ag, "_apply_ledger_corrections",
                side_effect=lambda t: (t.replace("誤聽詞", "正確詞"),
                                       ["誤聽詞→正確詞"])):
            r = ag.correct_transcript("這裡有誤聽詞。", use_llm=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "這裡有正確詞。")
        self.assertFalse(r["llm_applied"])
        self.assertEqual(r["original"], "這裡有誤聽詞。")
        self.assertEqual(r["corrections_applied"], ["誤聽詞→正確詞"])

    def test_no_glossary_skips_llm(self):
        with mock.patch.object(ag, "_apply_ledger_corrections",
                               side_effect=lambda t: (t, [])), \
                mock.patch("agent_core.gemini_client._gemini_generate") as gen:
            r = ag.correct_transcript("一段話。", use_llm=True)
        gen.assert_not_called()
        self.assertFalse(r["llm_applied"])

    def test_llm_correction_applied(self):
        ag.add_terms(["收櫃"], priority=ag._P_MANUAL)
        base = "先開米克斯巴士畫面，然後點收貴入庫。" * 3
        fixed = base.replace("收貴", "收櫃")
        with mock.patch.object(ag, "_apply_ledger_corrections",
                               side_effect=lambda t: (t, [])), \
                mock.patch("agent_core.gemini_client._gemini_generate",
                           return_value=_resp(fixed)) as gen:
            r = ag.correct_transcript(base, use_llm=True)
        self.assertTrue(r["llm_applied"])
        self.assertEqual(r["text"], fixed)
        self.assertEqual(r["deterministic"], base)
        self.assertEqual(gen.call_args.kwargs["caller"], "asr_glossary.correct")

    def test_wrapper_tags_stripped_from_llm_output(self):
        # 模型常把 wrap_as_untrusted 的標籤回顯 — 必須剝掉，
        # 不得漏進最終逐字稿（會進 SOP 素材與 RAG）。
        ag.add_terms(["收櫃"], priority=ag._P_MANUAL)
        base = "先開收貴畫面再入庫。" * 5
        echoed = "<asr_transcript>\n" + base.replace("收貴", "收櫃") + "\n</asr_transcript>"
        with mock.patch.object(ag, "_apply_ledger_corrections",
                               side_effect=lambda t: (t, [])), \
                mock.patch("agent_core.gemini_client._gemini_generate",
                           return_value=_resp(echoed)):
            r = ag.correct_transcript(base, use_llm=True)
        self.assertTrue(r["llm_applied"])
        self.assertNotIn("asr_transcript", r["text"])
        self.assertIn("收櫃", r["text"])

    def test_llm_rewrite_guard(self):
        # 修正後長度掉 20% 以上 = 越權改寫 → 退回確定性版本
        ag.add_terms(["收櫃"], priority=ag._P_MANUAL)
        base = "很長的一段轉錄稿。" * 20
        with mock.patch.object(ag, "_apply_ledger_corrections",
                               side_effect=lambda t: (t, [])), \
                mock.patch("agent_core.gemini_client._gemini_generate",
                           return_value=_resp("被大幅改寫的短摘要")):
            r = ag.correct_transcript(base, use_llm=True)
        self.assertFalse(r["llm_applied"])
        self.assertEqual(r["text"], base)

    def test_llm_error_falls_back(self):
        ag.add_terms(["收櫃"], priority=ag._P_MANUAL)
        with mock.patch.object(ag, "_apply_ledger_corrections",
                               side_effect=lambda t: (t, [])), \
                mock.patch("agent_core.gemini_client._gemini_generate",
                           side_effect=RuntimeError("503")):
            r = ag.correct_transcript("一段話。", use_llm=True)
        self.assertTrue(r["ok"])
        self.assertFalse(r["llm_applied"])

    def test_oversize_skips_llm(self):
        ag.add_terms(["收櫃"], priority=ag._P_MANUAL)
        with mock.patch.object(ag, "_apply_ledger_corrections",
                               side_effect=lambda t: (t, [])), \
                mock.patch.object(ag, "_LLM_CORRECT_MAX_CHARS", 10), \
                mock.patch("agent_core.gemini_client._gemini_generate") as gen:
            r = ag.correct_transcript("超過十個字的轉錄稿內容啦啦啦", use_llm=True)
        gen.assert_not_called()
        self.assertTrue(r["ok"])


class LedgerCorrectionFilterTests(unittest.TestCase):
    """mistake_ledger 糾正規則套用在轉錄稿的過濾層（過濾在 asr_glossary 端做，
    不動 mistake_ledger）：短 wrong key 跳過＋實際套用的規則可追溯。"""

    def _with_rules(self, rules: dict):
        import agent_core.mistake_ledger as ml

        patches = [
            mock.patch.object(ml, "_ensure_ledger_loaded", lambda: None),
            mock.patch.object(ml, "_mistake_ledger",
                              {"corrections": rules, "log": []}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_short_wrong_key_skipped_long_applied(self):
        # 聊天域的單字規則（「書→輸」這類）無詞邊界套轉錄稿會大面積誤傷。
        self._with_rules({"書": "輸", "米克斯巴士": "Mixbus"})
        fixed, applied = ag._apply_ledger_corrections(
            "打開書面資料，切到米克斯巴士畫面。")
        self.assertIn("書面資料", fixed)          # 單字規則不套
        self.assertIn("Mixbus", fixed)            # ≥2 字規則照套
        self.assertEqual(applied, ["米克斯巴士→Mixbus"])

    def test_applied_rules_surface_in_result(self):
        self._with_rules({"收貴": "收櫃"})
        r = ag.correct_transcript("先開收貴畫面。", use_llm=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "先開收櫃畫面。")
        self.assertEqual(r["corrections_applied"], ["收貴→收櫃"])

    def test_unapplied_rules_not_reported(self):
        self._with_rules({"收貴": "收櫃"})
        fixed, applied = ag._apply_ledger_corrections("完全沒有誤聽的句子。")
        self.assertEqual(fixed, "完全沒有誤聽的句子。")
        self.assertEqual(applied, [])

    def test_ledger_read_failure_returns_original(self):
        import agent_core.mistake_ledger as ml

        with mock.patch.object(ml, "_ensure_ledger_loaded",
                               side_effect=RuntimeError("boom")):
            fixed, applied = ag._apply_ledger_corrections("原文不動。")
        self.assertEqual(fixed, "原文不動。")
        self.assertEqual(applied, [])


class GlossaryJsonShapeTests(unittest.TestCase):
    def test_saved_shape_is_human_editable(self):
        tmp = tempfile.TemporaryDirectory()
        with mock.patch.object(ag, "_GLOSSARY_PATH",
                               os.path.join(tmp.name, "g.json")):
            ag.add_terms(["針車"])
            with open(ag._GLOSSARY_PATH, encoding="utf-8") as f:
                data = json.load(f)
        tmp.cleanup()
        self.assertIn("terms", data)
        self.assertIn("updated_at", data)
        self.assertEqual(data["terms"]["針車"]["priority"], 0)


if __name__ == "__main__":
    unittest.main()
