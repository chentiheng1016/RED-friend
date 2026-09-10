"""Embedding 記帳（2026-08-01 帳單稽核）。

稽核實錘：gemini-embedding-001 是月燒最大宗（6 月 27.4 億 tokens = $12,929、
7 月 17.2 億 = $8,236），但 vector_store / memory 的 embed_content 完全沒進
cost_tracker——month_to_date_usd / 月度 cap 預警對最大宗支出全盲。

鎖三件事：
1. estimate_embed_tokens 的啟發式（係數見 cost_tracker，每筆封頂 2048 = 模型
   輸入截斷上限）。⚠️ 2026-08-12 起係數改成實測值（CJK 0.82／其他 0.50），
   舊的「CJK 1.0／其他 0.25」低估總量 1.8 倍，詳見
   tests/test_embed_token_estimate.py 與 cost_tracker 的校準註解。
2. record_embed_call 寫出的 cost.jsonl entry 用官方 USD 牌價（$0.15/M）計價。
3. 兩個咽喉點（vector_store._gemini_embed_raw、memory._gemini_embed）成功
   回應後都會記帳，caller 依 task_type 拆 document/query。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class EstimateEmbedTokensTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker as ct
        self.ct = ct

    def test_ascii_two_chars_per_token(self):
        # 實測非 CJK ≈ 2 字元/token（舊值 4 是這次低估的主因）
        self.assertEqual(self.ct.estimate_embed_tokens(["abcdefgh"]), 4)

    def test_cjk_slightly_under_one_token_per_char(self):
        # 7 字 × 0.82 = 5.74 → 6
        self.assertEqual(self.ct.estimate_embed_tokens(["工廠出貨排程表"]), 6)

    def test_mixed_text(self):
        # "PO123 " = 6 非 CJK → 3 tokens；"出貨" = 2 CJK → 1.64 → 合計 4.64 → 5
        self.assertEqual(self.ct.estimate_embed_tokens(["PO123 出貨"]), 5)

    def test_caps_at_model_input_truncation_limit(self):
        # gemini-embedding-001 每筆輸入截斷在 2048 tokens，超過不處理也不計費
        self.assertEqual(self.ct.estimate_embed_tokens(["廠" * 5000]), 2048)

    def test_batch_sums_per_text(self):
        # 8 非 CJK → 4；2 CJK → 1.64 → 2；合計 6
        self.assertEqual(self.ct.estimate_embed_tokens(["abcdefgh", "廠廠"]), 6)

    def test_bare_string_accepted(self):
        self.assertEqual(self.ct.estimate_embed_tokens("abcdefgh"), 4)

    def test_empty_inputs(self):
        self.assertEqual(self.ct.estimate_embed_tokens([]), 0)
        self.assertEqual(self.ct.estimate_embed_tokens(["", "a"]), 1)


class RecordEmbedCallTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker as ct
        self.ct = ct
        self.tmp = tempfile.mkdtemp(prefix="red_embed_cost_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = os.path.join(self.tmp, "cost.jsonl")
        for p in (
            mock.patch.object(ct, "_COST_LOG", self.log),
            mock.patch.object(ct, "_pg_cost_store", lambda: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _entries(self):
        if not os.path.isfile(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_writes_entry_with_billed_rate(self):
        self.ct.record_embed_call(
            model="gemini-embedding-001",
            texts=["廠" * 1000, "廠" * 1000],
            duration_ms=12.5,
            caller="vector_store.embed_document",
        )
        entries = self._entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["model"], "gemini-embedding-001")
        # 每筆 1000 個 CJK 字 × 0.82 = 820 tokens，兩筆 = 1640
        self.assertEqual(e["prompt_tokens"], 1640)
        self.assertEqual(e["output_tokens"], 0)
        self.assertEqual(e["caller"], "vector_store.embed_document")
        # 官方 USD 牌價 $0.15/M（2026-08-05 幣別修正；舊值 4.75 是 TWD）
        self.assertAlmostEqual(e["cost_usd"], 1640 / 1_000_000 * 0.15, places=6)

    def test_empty_texts_records_nothing(self):
        self.ct.record_embed_call(model="gemini-embedding-001", texts=[])
        self.assertEqual(self._entries(), [])

    def test_never_raises(self):
        # texts 給個會讓估算炸掉的東西也不能拖累主流程
        self.ct.record_embed_call(model="gemini-embedding-001", texts=object())


class VectorStoreEmbedRecordingTests(unittest.TestCase):
    """_gemini_embed_raw 成功回應後記帳；caller 依 task_type 拆 document/query。"""

    def _embed(self, task_type):
        from agent_core.ingest import vector_store as vs

        fake_emb = SimpleNamespace(values=[0.1, 0.2, 0.3])
        fake_client = mock.MagicMock()
        fake_client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[fake_emb]
        )
        with mock.patch(
            "agent_core.gemini_client._get_embed_client", return_value=fake_client
        ), mock.patch("agent_core.cost_tracker.record_embed_call") as rec:
            vs._gemini_embed_raw(["hi 出貨"], task_type)
        return rec

    def test_document_embed_records_with_document_caller(self):
        rec = self._embed("RETRIEVAL_DOCUMENT")
        rec.assert_called_once()
        kwargs = rec.call_args.kwargs
        self.assertEqual(kwargs["model"], "gemini-embedding-001")
        self.assertEqual(kwargs["texts"], ["hi 出貨"])
        self.assertEqual(kwargs["caller"], "vector_store.embed_document")

    def test_query_embed_records_with_query_caller(self):
        rec = self._embed("RETRIEVAL_QUERY")
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["caller"], "vector_store.embed_query")

    def test_recorder_failure_does_not_break_embed(self):
        from agent_core.ingest import vector_store as vs

        fake_emb = SimpleNamespace(values=[0.1, 0.2])
        fake_client = mock.MagicMock()
        fake_client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[fake_emb]
        )
        with mock.patch(
            "agent_core.gemini_client._get_embed_client", return_value=fake_client
        ), mock.patch(
            "agent_core.cost_tracker.record_embed_call",
            side_effect=RuntimeError("boom"),
        ):
            out = vs._gemini_embed_raw(["hi"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(len(out), 1)


class MemoryEmbedRecordingTests(unittest.TestCase):
    """memory._gemini_embed（xiaohong_memory collection 的獨立咽喉點）也要記帳。"""

    def _embed(self, task_type):
        from agent_core import memory

        fake_emb = SimpleNamespace(values=[0.1, 0.2, 0.3])
        fake_client = mock.MagicMock()
        fake_client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[fake_emb]
        )
        fake_types = mock.MagicMock()
        with mock.patch.object(
            memory, "_get_gemini_client", return_value=fake_client
        ), mock.patch.object(
            memory, "_get_genai_types", return_value=fake_types
        ), mock.patch("agent_core.cost_tracker.record_embed_call") as rec:
            memory._gemini_embed(["記憶"], task_type=task_type)
        return rec

    def test_document_embed_records(self):
        rec = self._embed("RETRIEVAL_DOCUMENT")
        rec.assert_called_once()
        kwargs = rec.call_args.kwargs
        self.assertEqual(kwargs["model"], "gemini-embedding-001")
        self.assertEqual(kwargs["caller"], "memory.embed_document")

    def test_query_embed_records(self):
        rec = self._embed("RETRIEVAL_QUERY")
        self.assertEqual(rec.call_args.kwargs["caller"], "memory.embed_query")


if __name__ == "__main__":
    unittest.main()
