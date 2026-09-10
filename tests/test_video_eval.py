"""video_eval：評測題庫儲存、草擬、批改計分、歷次紀錄的單元測試。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_core import video_eval as ve


def _resp(text: str):
    r = mock.MagicMock()
    r.text = text
    return r


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(ve, "_EVAL_DIR", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_upsert_dedups_by_question_and_assigns_ids(self):
        qs = [
            {"kind": "param", "question": "數量欄填多少？", "expected": "120"},
            {"kind": "step", "question": "先做什麼？", "expected": "開 FTE_570"},
            {"kind": "param", "question": "數量欄填多少？", "expected": "重複題"},
            {"kind": "bogus", "question": "分類亂寫？", "expected": "x"},
            {"question": "沒答案的題", "expected": ""},
        ]
        r = ve.upsert_questions("vid1", "收櫃.mp4", qs, source="auto_draft")
        self.assertEqual(r["added"], 3)  # 重複與空答案被擋
        es = ve.load_eval_set("vid1")
        self.assertEqual(es["video_name"], "收櫃.mp4")
        self.assertEqual([q["id"] for q in es["questions"]], ["q01", "q02", "q03"])
        self.assertEqual(es["questions"][2]["kind"], "other")  # 亂寫的分類正規化
        self.assertTrue(all(q["status"] == "draft" for q in es["questions"]))

    def test_verify_all_and_subset(self):
        ve.upsert_questions("vid2", "n", [
            {"kind": "param", "question": "Q1", "expected": "A1"},
            {"kind": "step", "question": "Q2", "expected": "A2"},
        ], source="auto_draft")
        r = ve.verify_questions("vid2", ["q01"])
        self.assertEqual(r["verified"], 1)
        self.assertEqual(r["total_verified"], 1)
        r = ve.verify_questions("vid2")
        self.assertEqual(r["total_verified"], 2)

    def test_verify_missing_set(self):
        self.assertFalse(ve.verify_questions("nope")["ok"])

    def test_safe_id_sanitizes_path_hostile_video_id(self):
        path = ve._eval_path("../../etc/passwd")
        self.assertTrue(os.path.basename(path).startswith("______etc_passwd"))
        self.assertEqual(os.path.dirname(path), self._tmp.name)

    def test_qid_survives_manual_deletion(self):
        # 題庫開放人工開檔刪爛題 — 刪 q02 後再加題不得撞號（len+1 舊 bug）。
        ve.upsert_questions("vidZ", "n", [
            {"kind": "param", "question": f"Q{i}", "expected": f"A{i}"}
            for i in range(1, 5)
        ], source="auto_draft")
        es = ve.load_eval_set("vidZ")
        es["questions"] = [q for q in es["questions"] if q["id"] != "q02"]
        ve.save_eval_set(es)
        ve.upsert_questions("vidZ", "n", [
            {"kind": "term", "question": "新題", "expected": "新答"}],
            source="auto_draft")
        ids = [q["id"] for q in ve.load_eval_set("vidZ")["questions"]]
        self.assertEqual(len(ids), len(set(ids)))  # 無重複
        self.assertEqual(ids[-1], "q05")           # max+1，不是 len+1 的 q04

    def test_list_eval_sets(self):
        ve.upsert_questions("vidA", "甲", [
            {"kind": "param", "question": "Q", "expected": "A"}], source="human")
        sets = ve.list_eval_sets()
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0]["video_id"], "vidA")
        self.assertEqual(sets[0]["questions"], 1)


class DraftTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(ve, "_EVAL_DIR", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_draft_parses_llm_json_and_marks_draft(self):
        payload = json.dumps([
            {"kind": "param", "question": "數量填多少？", "expected": "120"},
            {"kind": "term", "question": "FTE_570 是什麼？", "expected": "收櫃畫面"},
        ], ensure_ascii=False)
        with mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=_resp(payload)) as gen:
            r = ve.draft_eval_questions("vid9", "收櫃.mp4", "教學內容" * 50, n=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["added"], 2)
        es = ve.load_eval_set("vid9")
        self.assertTrue(all(q["status"] == "draft" for q in es["questions"]))
        self.assertEqual(gen.call_args.kwargs["caller"], "video_eval.draft")

    def test_draft_unparseable_output(self):
        with mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=_resp("我不會輸出 JSON")):
            r = ve.draft_eval_questions("vid9", "n", "text")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "llm_output_unparseable")

    def test_draft_rejects_empty(self):
        self.assertFalse(ve.draft_eval_questions("", "n", "text")["ok"])
        self.assertFalse(ve.draft_eval_questions("vid", "n", "  ")["ok"])


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(ve, "_EVAL_DIR", self._tmp.name)
        self._patch.start()
        ve.upsert_questions("vidJ", "教學.mp4", [
            {"kind": "param", "question": "Q1", "expected": "A1"},
            {"kind": "step", "question": "Q2", "expected": "A2"},
            {"kind": "term", "question": "Q3", "expected": "A3"},
            {"kind": "causal", "question": "Q4", "expected": "A4"},
        ], source="human")
        ve.verify_questions("vidJ")

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _judged(self):
        return json.dumps([
            {"id": "q01", "verdict": "correct", "note": "ok"},
            {"id": "q02", "verdict": "partial", "note": "缺值"},
            {"id": "q03", "verdict": "wrong", "note": "矛盾"},
            {"id": "q04", "verdict": "亂寫", "note": ""},  # 未知 verdict → not_covered
        ])

    def test_scoring_math(self):
        es = ve.load_eval_set("vidJ")
        with mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=_resp(self._judged())) as gen:
            r = ve.judge_answers(es, "候選產出文字")
        self.assertTrue(r["ok"])
        # (1 + 0.5 + 0 + 0) / 4
        self.assertAlmostEqual(r["score"], 0.375)
        self.assertAlmostEqual(r["coverage"], 0.75)  # not_covered 1/4
        self.assertEqual(r["verdicts"]["correct"], 1)
        self.assertEqual(r["verdicts"]["not_covered"], 1)
        self.assertEqual(gen.call_args.kwargs["caller"], "video_eval.judge")

    def test_only_verified_gate(self):
        ve.upsert_questions("vidD", "n", [
            {"kind": "param", "question": "草稿題", "expected": "A"}], source="auto_draft")
        es = ve.load_eval_set("vidD")
        r = ve.judge_answers(es, "文字")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_verified_questions")

    def test_empty_candidate(self):
        es = ve.load_eval_set("vidJ")
        self.assertFalse(ve.judge_answers(es, "  ")["ok"])

    def test_run_eval_persists_and_report_shows_trend(self):
        with mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=_resp(self._judged())):
            r = ve.run_eval("vidJ", "候選產出文字", label="baseline")
        self.assertTrue(r["ok"])
        self.assertIn("run_file", r)
        runs_dir = ve._runs_dir()
        self.assertTrue(os.path.exists(os.path.join(runs_dir, r["run_file"])))
        report = ve.format_eval_report("vidJ")
        self.assertIn("score=0.38", report)
        self.assertIn("[baseline]", report)

    def test_run_eval_without_set(self):
        self.assertFalse(ve.run_eval("nope", "text")["ok"])

    def test_judge_unparseable_fails_instead_of_zero_score(self):
        # judge 輸出解析失敗 ≠ 全題 not_covered — 默默給 score=0 會把
        # 走勢圖畫成管線大退化（其實是 judge 壞了）。
        es = ve.load_eval_set("vidJ")
        for garbage in ("我不會輸出 JSON", "", "[]"):
            with mock.patch("agent_core.gemini_client._gemini_generate",
                            return_value=_resp(garbage)):
                r = ve.judge_answers(es, "候選產出文字")
            self.assertFalse(r["ok"], garbage)
            self.assertEqual(r["reason"], "judge_output_unparseable")

    def test_run_eval_unparseable_judge_leaves_no_run_file(self):
        with mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=_resp("不是 JSON")):
            r = ve.run_eval("vidJ", "候選產出文字", label="broken-judge")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "judge_output_unparseable")
        runs = ve._runs_dir()
        self.assertTrue(not os.path.isdir(runs) or not os.listdir(runs))


if __name__ == "__main__":
    unittest.main()
