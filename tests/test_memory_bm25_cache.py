"""BM25 cache 記憶體紀律：串流重建、緊湊欄位、fetch-on-hit。

2026-06-11 telegram daemon RSS 棘輪事故的回歸防護：
  - cache 只准存 {count, bm25, ids, sources, dates} — 全量 docs/metas 進
    cache 就是 ~250MB 常駐 + 每小時重建雙份並存的根因，永遠不准回來。
  - 重建走 _iter_collection_pages 逐頁串流。
  - 命中文件全文由 _fetch_docs_by_ids 對 top-N 補抓。
"""
from __future__ import annotations

import logging
import types
import unittest
from unittest import mock

from agent_core import memory as mem_mod
from agent_core import memory_ops


class _FakeCollection:
    """模擬 chroma collection：分頁 get(include=...) 與 ids get(ids=...)。"""

    def __init__(self, rows):
        # rows: list of (id, doc, meta)
        self.rows = list(rows)
        self.page_get_calls = 0
        self.ids_get_calls = 0

    def count(self):
        return len(self.rows)

    def get(self, *, ids=None, include=None, limit=None, offset=None, where=None):
        if ids is not None:
            self.ids_get_calls += 1
            picked = [r for r in self.rows if r[0] in set(ids)]
            return {
                "ids": [r[0] for r in picked],
                "documents": [r[1] for r in picked],
                "metadatas": [r[2] for r in picked],
            }
        self.page_get_calls += 1
        lo = int(offset or 0)
        hi = lo + int(limit or 1000)
        picked = self.rows[lo:hi]
        return {
            "ids": [r[0] for r in picked],
            "documents": [r[1] for r in picked],
            "metadatas": [r[2] for r in picked],
        }


def _rows(n, source="email"):
    return [
        (f"id-{i}", f"文件 {i} shoe order token{i}", {"source": source, "date": f"2026-06-{i % 28 + 1:02d}"})
        for i in range(n)
    ]


class _CacheStateMixin(unittest.TestCase):
    """conftest autouse 在 unittest discover 下不生效 — 隔離放 setUp/tearDown。"""

    def setUp(self):
        super().setUp()
        self._saved = dict(mem_mod._bm25_cache)
        self._reset_cache()

    def tearDown(self):
        mem_mod._bm25_cache.clear()
        mem_mod._bm25_cache.update(self._saved)
        super().tearDown()

    @staticmethod
    def _reset_cache():
        mem_mod._bm25_cache.clear()
        mem_mod._bm25_cache.update(
            {"count": -1, "bm25": None, "ids": None, "sources": None, "dates": None}
        )


class BuildBm25IndexTests(_CacheStateMixin):
    def test_cache_holds_compact_fields_only(self):
        col = _FakeCollection(_rows(5))
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=col):
            idx = mem_mod._build_bm25_index()

        self.assertIsNotNone(idx)
        self.assertEqual(
            set(idx.keys()), {"count", "bm25", "ids", "sources", "dates"},
            "cache 出現未預期欄位 — 全量 docs/metas 不准回到 cache（記憶體紀律）",
        )
        self.assertEqual(idx["count"], 5)
        self.assertEqual(idx["ids"], [f"id-{i}" for i in range(5)])
        self.assertEqual(idx["sources"], ["email"] * 5)
        self.assertEqual(idx["dates"][0], "2026-06-01")
        self.assertEqual(idx["bm25"].corpus_size, 5)

    def test_streaming_pages_and_cache_hit(self):
        col = _FakeCollection(_rows(2500))  # 3 頁（1000/批）
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=col):
            first = mem_mod._build_bm25_index()
            pages_after_build = col.page_get_calls
            second = mem_mod._build_bm25_index()

        self.assertEqual(pages_after_build, 3)
        self.assertEqual(col.page_get_calls, 3, "count 沒變不應重拉")
        self.assertIs(first["bm25"], second["bm25"])

    def test_count_change_triggers_rebuild(self):
        col = _FakeCollection(_rows(3))
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=col):
            first = mem_mod._build_bm25_index()
            col.rows.append(("id-new", "新文件 booking", {"source": "note", "date": ""}))
            second = mem_mod._build_bm25_index()

        self.assertEqual(first["count"], 3)
        self.assertEqual(second["count"], 4)
        self.assertEqual(second["bm25"].corpus_size, 4)
        self.assertEqual(second["ids"][-1], "id-new")

    def test_empty_collection_returns_none(self):
        col = _FakeCollection([])
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=col):
            self.assertIsNone(mem_mod._build_bm25_index())


class FetchDocsByIdsTests(unittest.TestCase):
    def test_returns_mapping_and_skips_missing(self):
        col = _FakeCollection(_rows(3))
        out = mem_mod._fetch_docs_by_ids(col, ["id-1", "id-404"])
        self.assertEqual(set(out.keys()), {"id-1"})
        doc, meta = out["id-1"]
        self.assertIn("文件 1", doc)
        self.assertEqual(meta["source"], "email")

    def test_collection_error_degrades_to_empty(self):
        col = types.SimpleNamespace(get=mock.Mock(side_effect=RuntimeError("server down")))
        self.assertEqual(mem_mod._fetch_docs_by_ids(col, ["id-1"]), {})

    def test_empty_ids_short_circuits(self):
        col = types.SimpleNamespace(get=mock.Mock())
        self.assertEqual(mem_mod._fetch_docs_by_ids(col, []), {})
        col.get.assert_not_called()


class Bm25TopHitsTests(_CacheStateMixin):
    def _build_idx(self, col):
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=col):
            return mem_mod._build_bm25_index()

    def test_source_filter_applies_before_truncation(self):
        # 25 筆 email 全部命中 "shoe"；2 筆 note 完全不含 query token（0 分）。
        # source="note" 過濾在截斷前 → 即使 0 分、全域排名墊底，note 兩筆都要在。
        rows = _rows(25, source="email") + [
            ("note-1", "完全無關內容甲", {"source": "note", "date": ""}),
            ("note-2", "完全無關內容乙", {"source": "note", "date": ""}),
        ]
        col = _FakeCollection(rows)
        idx = self._build_idx(col)
        hits = memory_ops.bm25_top_hits(
            idx,
            ["shoe"],
            col=col,
            limit=20,
            source="note",
            fetch_docs_by_ids_fn=mem_mod._fetch_docs_by_ids,
        )
        self.assertEqual([h[0] for h in hits], ["note-1", "note-2"])

    def test_hits_sorted_by_score_and_fetch_only_top(self):
        rows = _rows(30)
        # 讓 id-7 額外多次出現 query token → 分數最高
        rows[7] = ("id-7", "shoe shoe shoe shoe", {"source": "email", "date": ""})
        col = _FakeCollection(rows)
        idx = self._build_idx(col)
        hits = memory_ops.bm25_top_hits(
            idx,
            ["shoe"],
            col=col,
            limit=10,
            fetch_docs_by_ids_fn=mem_mod._fetch_docs_by_ids,
        )
        self.assertEqual(len(hits), 10)
        self.assertEqual(hits[0][0], "id-7")
        self.assertEqual(hits[0][1], "shoe shoe shoe shoe")
        scores = [h[3] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(col.ids_get_calls, 1, "全文補抓只准對 top-N 打一次")

    def test_deleted_id_between_build_and_fetch_is_skipped(self):
        rows = _rows(5)
        col = _FakeCollection(rows)
        idx = self._build_idx(col)
        col.rows = [r for r in col.rows if r[0] != "id-0"]  # build 後被刪
        hits = memory_ops.bm25_top_hits(
            idx,
            ["shoe"],
            col=col,
            limit=20,
            fetch_docs_by_ids_fn=mem_mod._fetch_docs_by_ids,
        )
        self.assertNotIn("id-0", [h[0] for h in hits])
        self.assertEqual(len(hits), 4)


class RecallHybridWiringTests(_CacheStateMixin):
    def test_recall_hybrid_end_to_end_with_compact_cache(self):
        """memory.recall → memory_ops.recall → bm25_top_hits → _fetch_docs_by_ids
        全鏈路（向量端故障 → 純 BM25 fallback）。"""
        col = _FakeCollection(_rows(6))
        col.query = mock.Mock(side_effect=RuntimeError("vector down"))
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=col):
            out = mem_mod.recall("shoe order", mode="hybrid", k=3)
        self.assertIn("找到 3 筆", out)
        self.assertIn("id-", out)

    def test_recall_bm25_mode_fetch_failure_degrades_gracefully(self):
        """補抓回空（例如 chroma get 失敗被吞成 {}）→ 回「沒找到」而非炸掉。"""
        col = _FakeCollection(_rows(4))
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=col):
            idx = mem_mod._build_bm25_index()
        out = memory_ops.recall(
            "shoe",
            mode="bm25",
            get_memory_collection_fn=lambda: col,
            build_bm25_index_fn=lambda: idx,
            simple_tokenize_fn=mem_mod._simple_tokenize,
            fetch_docs_by_ids_fn=lambda c, ids: {},
            logger_obj=logging.getLogger(__name__),
        )
        self.assertIn("沒找到", out)


if __name__ == "__main__":
    unittest.main()
