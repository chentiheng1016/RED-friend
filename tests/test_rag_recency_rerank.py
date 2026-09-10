"""Tests for recency-aware reranking of RAG hits (recency.py + tool wiring).

純語意檢索不管文件新舊；recency.rerank_by_recency 把候選池用
blended = (1-w)·sim + w·freshness 重排。這些測試釘住：衰減數學、壞值降級、
重排確實能讓「相關但較舊」讓位給「稍不相關但很新」，以及 prefer_recent 旗標
正確接進 search_drive_docs / search_google_chat（且預設關＝行為不變）。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TODAY = date(2026, 7, 1)


# ── recency math ─────────────────────────────────────────────────────

class FreshnessScoreTests(unittest.TestCase):
    def test_today_is_full_fresh(self):
        from agent_core.ingest import recency
        s = recency.freshness_score(
            {"modified_time": "2026-07-01T00:00:00Z"}, "modified_time", 180.0, today=_TODAY)
        self.assertAlmostEqual(s, 1.0, places=6)

    def test_half_life_is_half(self):
        from agent_core.ingest import recency
        # 180 天前的文件，半衰期 180 → freshness ≈ 0.5
        s = recency.freshness_score(
            {"modified_time": "2026-01-02"}, "modified_time", 180.0, today=_TODAY)
        self.assertAlmostEqual(s, 0.5, places=2)

    def test_missing_date_is_zero(self):
        from agent_core.ingest import recency
        self.assertEqual(
            recency.freshness_score({}, "modified_time", 180.0, today=_TODAY), 0.0)

    def test_unparseable_date_is_zero(self):
        from agent_core.ingest import recency
        self.assertEqual(
            recency.freshness_score(
                {"modified_time": "(unknown)"}, "modified_time", 180.0, today=_TODAY),
            0.0,
        )

    def test_future_date_clamped_to_full_fresh(self):
        from agent_core.ingest import recency
        s = recency.freshness_score(
            {"modified_time": "2099-01-01"}, "modified_time", 180.0, today=_TODAY)
        self.assertEqual(s, 1.0)


class SimilarityTests(unittest.TestCase):
    def test_distance_flipped_to_similarity(self):
        from agent_core.ingest import recency
        self.assertAlmostEqual(recency.similarity({"distance": 0.13}), 0.87, places=6)

    def test_none_distance_is_farthest(self):
        from agent_core.ingest import recency
        self.assertEqual(recency.similarity({"distance": None}), 0.0)

    def test_distance_over_one_clamped_nonnegative(self):
        from agent_core.ingest import recency
        self.assertEqual(recency.similarity({"distance": 1.7}), 0.0)


# ── rerank_by_recency ────────────────────────────────────────────────

class RerankByRecencyTests(unittest.TestCase):
    def _hits(self):
        # A: very relevant (sim 0.9) but 2 years old → stale
        # B: less relevant (sim 0.6) but updated today → fresh
        return [
            {"text": "A", "metadata": {"title": "old", "modified_time": "2024-07-01"},
             "distance": 0.1},
            {"text": "B", "metadata": {"title": "new", "modified_time": "2026-07-01"},
             "distance": 0.4},
        ]

    def test_recency_weight_promotes_fresh_doc(self):
        from agent_core.ingest import recency
        out = recency.rerank_by_recency(
            self._hits(), "modified_time", 2,
            half_life_days=180.0, recency_weight=0.5, today=_TODAY)
        # 時間加權後，今天更新的 B 應排到相關但老舊的 A 前面
        self.assertEqual(out[0]["text"], "B")
        self.assertEqual(out[1]["text"], "A")

    def test_zero_weight_keeps_pure_semantic_order(self):
        from agent_core.ingest import recency
        out = recency.rerank_by_recency(
            self._hits(), "modified_time", 2,
            half_life_days=180.0, recency_weight=0.0, today=_TODAY)
        # w=0 → 純語意，最相關的 A 仍在最前
        self.assertEqual(out[0]["text"], "A")

    def test_truncates_to_k(self):
        from agent_core.ingest import recency
        out = recency.rerank_by_recency(
            self._hits(), "modified_time", 1,
            half_life_days=180.0, recency_weight=0.5, today=_TODAY)
        self.assertEqual(len(out), 1)

    def test_attaches_recency_breakdown_without_mutating_input(self):
        from agent_core.ingest import recency
        hits = self._hits()
        out = recency.rerank_by_recency(
            hits, "modified_time", 2,
            half_life_days=180.0, recency_weight=0.5, today=_TODAY)
        self.assertIn("_recency", out[0])
        self.assertIn("blended", out[0]["_recency"])
        # 原輸入 dict 不被就地汙染（淺拷貝）
        self.assertNotIn("_recency", hits[0])

    def test_empty_hits_returns_empty(self):
        from agent_core.ingest import recency
        self.assertEqual(recency.rerank_by_recency([], "modified_time", 5), [])


# ── prefer_recent wiring into the search tools ───────────────────────

class _StoreMixin:
    @staticmethod
    def _store(count, hits):
        store = mock.MagicMock()
        store.count.return_value = count
        store.is_empty.return_value = (count == 0)
        store.query.return_value = hits
        return store


class DriveSearchPreferRecentTests(_StoreMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # citation feedback（Phase 3）讀 var/state ledger——測試導到 tmp，
        # 免得活機器的引用記錄改變排序（tests immune to live var/ state）。
        import tempfile
        from agent_core import citation_feedback as _cf
        self._cf_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cf_tmp.cleanup)
        _p = mock.patch.object(_cf, "LEDGER_FILE",
                               os.path.join(self._cf_tmp.name, "cf.json"))
        _p.start()
        self.addCleanup(_p.stop)
        _p2 = mock.patch.object(_cf, "RECENT_KEYS_FILE",
                                os.path.join(self._cf_tmp.name, "cf_recent.json"))
        _p2.start()
        self.addCleanup(_p2.stop)

    def _hits(self):
        return [
            {"text": "舊報價", "metadata": {"title": "quote_2024", "modified_time": "2024-01-01"},
             "distance": 0.1},
            {"text": "新報價", "metadata": {"title": "quote_2026", "modified_time": "2026-06-30"},
             "distance": 0.35},
        ]

    def test_default_is_pure_semantic_no_reorder_note(self):
        from agent_core.ingest import drive_search
        store = self._store(100, self._hits())
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            r = drive_search.search_drive_docs("報價", k=5)
        self.assertNotIn("時間加權", r)
        # 預設只撈 n 筆（不放大候選池）
        self.assertEqual(store.query.call_args.kwargs["n_results"], 5)

    def test_prefer_recent_enlarges_pool_and_reranks(self):
        from agent_core.ingest import drive_search, recency
        store = self._store(100, self._hits())
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            r = drive_search.search_drive_docs("報價", k=2, prefer_recent=True)
        # 候選池放大到 pool_size(2)（預設 20）
        self.assertEqual(store.query.call_args.kwargs["n_results"], recency.pool_size(2))
        # 輸出標注時間加權，且帶新鮮度欄位
        self.assertIn("時間加權", r)
        self.assertIn("新鮮度=", r)
        # 新報價（2026）應排在舊報價（2024）之前
        self.assertLess(r.index("新報價"), r.index("舊報價"))


class ChatSearchPreferRecentTests(_StoreMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # citation feedback（Phase 3）讀 var/state ledger——測試導到 tmp，
        # 免得活機器的引用記錄改變排序（tests immune to live var/ state）。
        import tempfile
        from agent_core import citation_feedback as _cf
        self._cf_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cf_tmp.cleanup)
        _p = mock.patch.object(_cf, "LEDGER_FILE",
                               os.path.join(self._cf_tmp.name, "cf.json"))
        _p.start()
        self.addCleanup(_p.stop)
        _p2 = mock.patch.object(_cf, "RECENT_KEYS_FILE",
                                os.path.join(self._cf_tmp.name, "cf_recent.json"))
        _p2.start()
        self.addCleanup(_p2.stop)

    def _hits(self):
        return [
            {"text": "舊討論", "metadata": {"display_name": "群A", "last_message_time": "2024-01-01"},
             "distance": 0.1},
            {"text": "新討論", "metadata": {"display_name": "群B", "last_message_time": "2026-06-30"},
             "distance": 0.35},
        ]

    def test_default_is_pure_semantic(self):
        from agent_core.ingest import chat_search
        store = self._store(100, self._hits())
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store), \
             mock.patch("agent_core.ingest.chat_search.access_where", return_value=None):
            r = chat_search.search_google_chat("交期", k=5)
        self.assertNotIn("時間加權", r)
        self.assertEqual(store.query.call_args.kwargs["n_results"], 5)

    def test_prefer_recent_reranks_by_last_message_time(self):
        from agent_core.ingest import chat_search, recency
        store = self._store(100, self._hits())
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store), \
             mock.patch("agent_core.ingest.chat_search.access_where", return_value=None):
            r = chat_search.search_google_chat("交期", k=2, prefer_recent=True)
        self.assertEqual(store.query.call_args.kwargs["n_results"], recency.pool_size(2))
        self.assertIn("時間加權", r)
        self.assertLess(r.index("新討論"), r.index("舊討論"))


if __name__ == "__main__":
    unittest.main()
