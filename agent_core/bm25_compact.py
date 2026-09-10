"""記憶體緊湊版 Okapi BM25 — 取代 rank_bm25.BM25Okapi 的 in-RAM 索引。

為什麼存在：telegram daemon 的 recall 把 22K+ 筆記憶餵給 BM25Okapi，其
doc_freqs（每文件一個 dict）在 CJK 逐字 tokenize 下吃掉 ~180MB 常駐，且
每次 memory collection count 變動就整座重建（新舊雙份並存），RSS 一路
棘輪上爬（2026-06-11 觀測 246→476→696MB）。本模組把同一份索引壓進
numpy CSR postings（token → (doc_idx, tf) 切片），同語料 <1/5 記憶體。

分數與 rank_bm25.BM25Okapi **完全同值**（同 k1=1.5/b=0.75/epsilon=0.25、
同負 idf→eps*average_idf 下限、重複 query token 同樣重複計分、缺詞回 0），
tests/test_bm25_compact.py 直接拿 rank_bm25 當 oracle 驗證。

鐵則：只 import numpy 與 stdlib（葉模組，誰都能拉）。
"""
from __future__ import annotations

import math
from array import array
from collections import Counter
from typing import Iterable, Sequence

import numpy as np


class CompactBM25:
    """CSR-postings Okapi BM25。

    用 from_corpus_iter() 建立；直接建構式僅供內部/測試使用。
    """

    __slots__ = (
        "corpus_size",
        "avgdl",
        "_k1",
        "_vocab",
        "_indptr",
        "_doc_idx",
        "_tf",
        "_idf",
        "_denom_base",
    )

    def __init__(
        self,
        *,
        corpus_size: int,
        avgdl: float,
        k1: float,
        vocab: dict,
        indptr: np.ndarray,
        doc_idx: np.ndarray,
        tf: np.ndarray,
        idf: np.ndarray,
        denom_base: np.ndarray,
    ):
        self.corpus_size = corpus_size
        self.avgdl = avgdl
        self._k1 = k1
        self._vocab = vocab
        self._indptr = indptr
        self._doc_idx = doc_idx
        self._tf = tf
        self._idf = idf
        self._denom_base = denom_base

    @classmethod
    def from_corpus_iter(
        cls,
        corpus_iter: Iterable[Sequence[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        epsilon: float = 0.25,
    ) -> "CompactBM25":
        """從「已 tokenize 的文件」iterator 建索引。

        接受 iterator 而非 list，讓呼叫端能逐頁從 chroma 拉文件、tokenize 完
        就丟棄原文 — 建索引峰值只剩 postings 陣列本身，不再是整批 docs。
        """
        vocab: dict = {}
        tid_buf = array("i")
        did_buf = array("i")
        tf_buf = array("i")
        doc_len_buf = array("i")

        corpus_size = 0
        total_tokens = 0
        for tokens in corpus_iter:
            doc_len_buf.append(len(tokens))
            total_tokens += len(tokens)
            for tok, cnt in Counter(tokens).items():
                tid = vocab.setdefault(tok, len(vocab))
                tid_buf.append(tid)
                did_buf.append(corpus_size)
                tf_buf.append(cnt)
            corpus_size += 1

        if corpus_size == 0:
            raise ValueError("empty corpus")

        avgdl = total_tokens / corpus_size

        # array("i") 是 C int → np.intc；後續 fancy-index / astype 都產生自有
        # 記憶體的複本，不會殘留對 buffer 的 view。
        tid_arr = np.frombuffer(tid_buf, dtype=np.intc) if tid_buf else np.empty(0, np.intc)
        did_arr = np.frombuffer(did_buf, dtype=np.intc) if did_buf else np.empty(0, np.intc)
        tf_arr = np.frombuffer(tf_buf, dtype=np.intc) if tf_buf else np.empty(0, np.intc)
        doc_len = np.frombuffer(doc_len_buf, dtype=np.intc).astype(np.float64)

        # 依 token id 重排成 CSR：indptr[tid]..indptr[tid+1] 為該 token 的 postings
        order = np.argsort(tid_arr, kind="stable")
        doc_idx = np.ascontiguousarray(did_arr[order]).astype(np.int32)
        tf = np.ascontiguousarray(tf_arr[order]).astype(np.float64)
        n_vocab = len(vocab)
        df = np.bincount(tid_arr, minlength=n_vocab).astype(np.int64)
        indptr = np.zeros(n_vocab + 1, dtype=np.int64)
        np.cumsum(df, out=indptr[1:])

        # idf 與 BM25Okapi._calc_idf 完全同式：
        #   idf = ln(N - df + 0.5) - ln(df + 0.5)
        #   負 idf → epsilon * average_idf（平均含負值、不 clamp）
        # average_idf 用 Python 循序加總（同 BM25Okapi 的 idf_sum += ...），
        # 避免 np.sum pairwise 加總在最後幾個 ulp 上偏離 oracle。
        idf = np.empty(n_vocab, dtype=np.float64)
        if n_vocab:
            for tid in range(n_vocab):
                idf[tid] = math.log(corpus_size - df[tid] + 0.5) - math.log(df[tid] + 0.5)
            average_idf = sum(idf.tolist()) / n_vocab
            eps = epsilon * average_idf
            idf[idf < 0] = eps

        # 分母底項 k1*(1 - b + b*dl/avgdl) 對 query 不變，預先算好
        if avgdl > 0:
            denom_base = k1 * (1 - b + b * doc_len / avgdl)
        else:
            # 全空文件的退化語料：BM25Okapi 在這裡會除以零；我們直接讓所有
            # 分數為 0（query 反正配不到任何 token）。
            denom_base = np.full(corpus_size, k1, dtype=np.float64)

        return cls(
            corpus_size=corpus_size,
            avgdl=avgdl,
            k1=k1,
            vocab=vocab,
            indptr=indptr,
            doc_idx=doc_idx,
            tf=tf,
            idf=idf,
            denom_base=denom_base,
        )

    def get_scores(self, query: Sequence[str]) -> np.ndarray:
        """回傳全語料分數陣列（float64, len=corpus_size）。

        語義對齊 BM25Okapi.get_scores：重複 token 重複累分；不在 vocab 的
        token 貢獻 0。
        """
        score = np.zeros(self.corpus_size, dtype=np.float64)
        for q in query:
            tid = self._vocab.get(q)
            if tid is None:
                continue
            lo = self._indptr[tid]
            hi = self._indptr[tid + 1]
            if lo == hi:
                continue
            docs = self._doc_idx[lo:hi]
            tf = self._tf[lo:hi]
            score[docs] += self._idf[tid] * (
                tf * (self._k1 + 1) / (tf + self._denom_base[docs])
            )
        return score

    def nbytes(self) -> int:
        """索引主體（numpy 陣列）位元組數 — 給 dashboard / 診斷用。"""
        return int(
            self._indptr.nbytes
            + self._doc_idx.nbytes
            + self._tf.nbytes
            + self._idf.nbytes
            + self._denom_base.nbytes
        )
