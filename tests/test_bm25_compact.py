"""CompactBM25 與 rank_bm25.BM25Okapi 的分數一致性（oracle 對照）。

CompactBM25 是 2026-06 telegram daemon 記憶體棘輪事故的修復：BM25Okapi 的
doc_freqs（每文件一個 dict）在 22K CJK 語料上吃 ~180MB；緊湊版必須在
**完全同分** 的前提下換掉它 — 這裡直接拿 rank_bm25 當 oracle 驗證。
"""
from __future__ import annotations

import random
import unittest

import numpy as np

from agent_core.bm25_compact import CompactBM25
from agent_core.memory import _simple_tokenize


def _oracle(corpus_tokens):
    from rank_bm25 import BM25Okapi

    return BM25Okapi(corpus_tokens)


_CJK_SNIPPETS = [
    "大王要求報價單附運費估算",
    "客戶說防水膜規格不對要重出",
    "六月船期已訂艙等待裝櫃",
    "供應商回覆樣品下週寄出",
    "這批工作靴鞋底開模費用另計",
]
_LATIN_SNIPPETS = [
    "PO-2026-0418 confirmed qty 2694 prs",
    "Blåkläder safety boot AF-1 Pro quote",
    "RICHTER booking request HCM - HAM 01X40HC",
    "invoice 1176843 paid via T/T",
    "jalas waterproof membrane spec sheet",
]


def _build_mixed_corpus(n_docs: int, seed: int) -> list[list[str]]:
    rng = random.Random(seed)
    corpus = []
    for i in range(n_docs):
        parts = []
        for _ in range(rng.randint(1, 4)):
            pool = _CJK_SNIPPETS if rng.random() < 0.5 else _LATIN_SNIPPETS
            parts.append(rng.choice(pool))
        text = " ".join(parts)
        if i % 17 == 0:
            text = ""  # 空文件也要對得上
        corpus.append(_simple_tokenize(text))
    return corpus


class CompactBM25ParityTests(unittest.TestCase):
    def _assert_parity(self, corpus_tokens, queries):
        compact = CompactBM25.from_corpus_iter(iter(corpus_tokens))
        oracle = _oracle(corpus_tokens)
        for q in queries:
            got = compact.get_scores(q)
            want = oracle.get_scores(q)
            self.assertIsInstance(got, np.ndarray)
            self.assertEqual(got.shape, (len(corpus_tokens),))
            np.testing.assert_allclose(
                got, want, rtol=1e-9, atol=1e-12,
                err_msg=f"query={q!r} 分數偏離 oracle",
            )

    def test_mixed_cjk_latin_corpus_matches_oracle(self):
        corpus = _build_mixed_corpus(300, seed=42)
        queries = [
            _simple_tokenize("richter 七月船 booking"),
            _simple_tokenize("PO-2026-0418 數量"),
            _simple_tokenize("Blåkläder 防水膜"),
            _simple_tokenize("jalas"),
            ["缺", "字", "oov-token-xyz"],          # 部分 OOV
            ["totally", "absent", "everywhere"],     # 全 OOV → 全 0
            ["船", "船", "船"],                      # 重複 token 重複累分
            [],                                       # 空 query → 全 0
        ]
        self._assert_parity(corpus, queries)

    def test_negative_idf_floor_matches_oracle(self):
        # "the" 出現在 >半數文件 → idf < 0 → 落到 epsilon * average_idf 下限。
        corpus = [
            ["the", "quick", "fox"],
            ["the", "lazy", "dog"],
            ["the", "old", "fox"],
            ["rare", "term"],
        ]
        self._assert_parity(corpus, [["the"], ["the", "fox"], ["rare", "the"]])

    def test_single_doc_corpus_matches_oracle(self):
        corpus = [_simple_tokenize("單一文件 corpus edge case AF-1")]
        self._assert_parity(corpus, [["af-1"], ["單"], ["missing"]])

    def test_empty_corpus_raises_value_error(self):
        with self.assertRaises(ValueError):
            CompactBM25.from_corpus_iter(iter([]))

    def test_all_empty_docs_returns_zero_scores(self):
        # 退化語料：BM25Okapi 會除以零；緊湊版定義為全 0 分（query 配不到
        # 任何 token），不噴例外。
        compact = CompactBM25.from_corpus_iter(iter([[], [], []]))
        scores = compact.get_scores(["anything"])
        np.testing.assert_array_equal(scores, np.zeros(3))

    def test_accepts_generator_and_consumes_lazily(self):
        corpus = _build_mixed_corpus(50, seed=7)
        compact = CompactBM25.from_corpus_iter(tokens for tokens in corpus)
        self.assertEqual(compact.corpus_size, 50)

    def test_nbytes_reports_positive_size(self):
        corpus = _build_mixed_corpus(50, seed=7)
        compact = CompactBM25.from_corpus_iter(iter(corpus))
        self.assertGreater(compact.nbytes(), 0)


if __name__ == "__main__":
    unittest.main()
