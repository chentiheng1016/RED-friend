"""帳本按 API key 拆帳：存指紋、不存金鑰。

起因：大王問「三把 key 分別花多少」，查下去發現本機完全答不了 ——
`cost.jsonl` 只有 ts/model/tokens/cost_usd/caller，沒有任何 key 或 project 維度，
而帳單是照 GCP project 出的。加一個不可逆的短指紋就能把兩端接起來。

兩條紅線：
  ① **金鑰本身絕不進 cost.jsonl** —— 那是明文檔，dashboard / red-web 都會讀。
  ② **取指紋不可有副作用** —— `_get_gemini_api_key()` 在金鑰缺失或格式錯時會
     `sys.exit(1)`，而指紋是在記帳路徑上取的；記帳是 best-effort，絕不該能終止
     整個 daemon。所以只讀已載入的全域，不觸發載入。
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

_FAKE_A = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE123"
_FAKE_B = "AIzaSyOTHEROTHEROTHEROTHEROTHEROTHER999"


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        from agent_core import gemini_client

        self.gc = gemini_client
        self._orig = gemini_client._gemini_api_key
        self.addCleanup(setattr, gemini_client, "_gemini_api_key", self._orig)

    def test_fingerprint_is_stable_and_distinguishes_keys(self):
        self.gc._gemini_api_key = _FAKE_A
        a1 = self.gc.api_key_fingerprint()
        a2 = self.gc.api_key_fingerprint()
        self.gc._gemini_api_key = _FAKE_B
        b = self.gc.api_key_fingerprint()
        self.assertEqual(a1, a2, "同一把 key 的指紋必須穩定，否則拆帳會散開")
        self.assertNotEqual(a1, b)
        self.assertEqual(len(a1), 8)

    def test_fingerprint_never_leaks_key_material(self):
        self.gc._gemini_api_key = _FAKE_A
        fp = self.gc.api_key_fingerprint()
        self.assertNotIn("AIza", fp)
        self.assertNotIn(_FAKE_A[-8:], fp, "不可包含金鑰尾段")
        self.assertNotIn(fp, _FAKE_A, "指紋不得是金鑰的子字串")
        self.assertRegex(fp, r"^[0-9a-f]{8}$")

    def test_no_key_loaded_returns_empty_without_side_effects(self):
        """關鍵：不可觸發 _get_gemini_api_key（那顆會 sys.exit(1)）。"""
        self.gc._gemini_api_key = None
        with mock.patch.object(self.gc, "_get_gemini_api_key",
                               side_effect=AssertionError("不該被呼叫")) as loader:
            self.assertEqual(self.gc.api_key_fingerprint(), "")
        loader.assert_not_called()


class LedgerStampTests(unittest.TestCase):
    def setUp(self):
        from agent_core import gemini_client

        self.gc = gemini_client
        self._orig = gemini_client._gemini_api_key
        self.addCleanup(setattr, gemini_client, "_gemini_api_key", self._orig)

    @staticmethod
    def _usage():
        return SimpleNamespace(prompt_token_count=1000, candidates_token_count=50,
                               cached_content_token_count=0, total_token_count=1050)

    def _record(self, key, tmp):
        from agent_core import cost_tracker

        self.gc._gemini_api_key = key
        log = os.path.join(tmp, "cost.jsonl")
        with mock.patch.object(cost_tracker, "_COST_LOG", log), \
                mock.patch.object(cost_tracker, "_pg_cost_store", lambda: None):
            cost_tracker.record_call(model="gemini-3.6-flash",
                                     usage_metadata=self._usage(), caller="t")
        with open(log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_record_call_stamps_the_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._record(_FAKE_A, tmp)
        self.assertEqual(rows[-1]["key_fp"], self.gc.api_key_fingerprint())

    def test_ledger_line_contains_no_key_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.gc._gemini_api_key = _FAKE_A
            rows = self._record(_FAKE_A, tmp)
        raw = json.dumps(rows[-1], ensure_ascii=False)
        self.assertNotIn(_FAKE_A, raw)
        self.assertNotIn("AIza", raw)

    def test_fingerprint_failure_does_not_break_accounting(self):
        """取指紋失敗只能讓欄位空著，不能讓整筆記帳掉。"""
        from agent_core import cost_tracker

        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "cost.jsonl")
            with mock.patch.object(cost_tracker, "_COST_LOG", log), \
                    mock.patch.object(cost_tracker, "_pg_cost_store", lambda: None), \
                    mock.patch.object(self.gc, "api_key_fingerprint",
                                      side_effect=RuntimeError("keyring 掛了")):
                cost_tracker.record_call(model="gemini-3.6-flash",
                                         usage_metadata=self._usage(), caller="t")
            with open(log, encoding="utf-8") as f:
                row = json.loads(f.readline())
        self.assertEqual(row["key_fp"], "")
        self.assertGreater(row["cost_usd"], 0, "記帳本身必須照常完成")


class CostByKeyTests(unittest.TestCase):
    def _report(self, rows, **kw):
        from agent_core import cost_tracker

        with mock.patch.object(cost_tracker, "_load_entries", return_value=rows):
            return cost_tracker.cost_by_key(**kw)

    @staticmethod
    def _row(fp, cost, caller="a.b", model="gemini-3.6-flash"):
        return {"ts": "2026-08-07T10:00:00", "model": model, "prompt_tokens": 10,
                "output_tokens": 5, "thinking_tokens": 0, "cached_tokens": 0,
                "total_tokens": 15, "cost_usd": cost, "caller": caller, "key_fp": fp}

    def test_splits_cost_across_keys(self):
        out = self._report([self._row("aaaa1111", 1.0), self._row("aaaa1111", 2.0),
                            self._row("bbbb2222", 0.5)])
        self.assertIn("aaaa1111", out)
        self.assertIn("bbbb2222", out)
        self.assertIn("$3.0000", out)   # 同一把 key 要加總
        self.assertIn("$0.5000", out)
        self.assertIn("85.7%", out)     # 3.0 / 3.5

    def test_rows_without_fingerprint_are_labelled_not_dropped(self):
        rows = [self._row("aaaa1111", 1.0)]
        rows.append({k: v for k, v in self._row("x", 4.0).items() if k != "key_fp"})
        out = self._report(rows)
        self.assertIn("(未記錄)", out)
        self.assertIn("$4.0000", out, "沒有指紋的舊列不能被丟掉，只能標記")

    def test_all_legacy_rows_get_an_explanation(self):
        """全是舊列時要說明原因，別讓人誤判成「某把 key 沒用量」。

        ⚠️ 只驗有沒有解釋，**不驗日期字面**。上一版這裡斷言訊息含 "2026-08-07"，
        把一個當時猜的上線日鎖進測試；實際部署是 08-12，於是那個錯日期被測試
        保護著、一路留在使用者看得到的訊息裡。訊息現在改成描述機制、不帶日期。
        """
        rows = [{k: v for k, v in self._row("x", 1.0).items() if k != "key_fp"}]
        out = self._report(rows)
        self.assertIn("沒有 key 指紋", out)
        self.assertIn("不是", out, "要明講這不代表某把 key 沒用量")
        self.assertNotRegex(out, r"20\d\d-\d\d-\d\d",
                            "訊息不該寫死日期——會走鐘且誤導對帳")

    def test_marks_which_key_is_currently_active(self):
        from agent_core import cost_tracker

        with mock.patch.object(cost_tracker, "_api_key_fingerprint", return_value="bbbb2222"):
            out = self._report([self._row("aaaa1111", 1.0), self._row("bbbb2222", 2.0)])
        self.assertIn("← 目前這把", out)
        line = next(x for x in out.splitlines() if "← 目前這把" in x)
        self.assertIn("bbbb2222", line)

    def test_empty_ledger_is_not_a_crash(self):
        self.assertIn("沒有任何", self._report([]))


if __name__ == "__main__":
    unittest.main()
