from __future__ import annotations

import logging
import types
import unittest
from unittest import mock


def _fake_collection(ids: list[str], docs: list[str], distances: list[float]):
    """Build a chromadb-like collection.query() stub."""
    def query(query_texts, n_results, where):
        return {
            "ids": [ids],
            "documents": [docs],
            "metadatas": [[{"source": "email", "ts": "2026-05-20T10:00:00"} for _ in ids]],
            "distances": [distances],
        }
    return types.SimpleNamespace(query=query)


def _call_recall(collection, *, mode="vector", min_score=0.0, k=5):
    from agent_core import memory_ops

    return memory_ops.recall(
        query="jalas 防水膜",
        k=k,
        source="",
        mode=mode,
        min_score=min_score,
        get_memory_collection_fn=lambda: collection,
        build_bm25_index_fn=lambda: None,
        simple_tokenize_fn=lambda s: s.split(),
        fetch_docs_by_ids_fn=lambda col, ids: {},
        logger_obj=logging.getLogger(__name__),
    )


class RecallConfidenceTiersTests(unittest.TestCase):
    def test_high_medium_weak_tiers_are_labelled_in_output(self):
        # distances: 0.05 → sim 0.95 (🟢), 0.40 → sim 0.60 (🟡), 0.65 → sim 0.35 (🔴)
        coll = _fake_collection(
            ids=["t1", "t2", "t3"],
            docs=["strong match", "weakish match", "co-occurrence only"],
            distances=[0.05, 0.40, 0.65],
        )

        out = _call_recall(coll, mode="vector", k=5)

        self.assertIn("🟢 高信心 1", out)
        self.assertIn("🟡 中信心 1", out)
        self.assertIn("🔴 弱信心 1", out)
        # Per-row emoji prefix so the LLM can spot weak hits at a glance.
        self.assertIn("🟢 🆔 [thread_id=t1]", out)
        self.assertIn("🔴 🆔 [thread_id=t3]", out)

    def test_weak_only_results_include_the_not_evidence_warning(self):
        coll = _fake_collection(
            ids=["w1", "w2"],
            docs=["weak 1", "weak 2"],
            distances=[0.7, 0.8],  # → sim 0.3, 0.2 (both 🔴)
        )

        out = _call_recall(coll, mode="vector", k=5)

        self.assertIn("🔴 弱信心 2", out)
        self.assertIn("不是事實證據", out)
        self.assertIn("query_bom", out)

    def test_min_score_filters_weak_hits_out(self):
        coll = _fake_collection(
            ids=["t1", "t2", "t3"],
            docs=["a", "b", "c"],
            distances=[0.05, 0.40, 0.65],
        )

        out = _call_recall(coll, mode="vector", k=5, min_score=0.5)

        # t1 (sim 0.95) and t2 (sim 0.60) pass; t3 (sim 0.35) is dropped.
        self.assertIn("找到 2 筆", out)
        self.assertIn("thread_id=t1", out)
        self.assertIn("thread_id=t2", out)
        self.assertNotIn("thread_id=t3", out)

    def test_min_score_can_filter_everything_out(self):
        coll = _fake_collection(
            ids=["w1"],
            docs=["weakish"],
            distances=[0.7],
        )

        out = _call_recall(coll, mode="vector", k=5, min_score=0.5)

        self.assertIn("沒找到相似度", out)
        self.assertIn("0.50", out)
        self.assertIn("query_email_lake", out)

    def test_no_filter_default_returns_all_hits_with_warning_only(self):
        coll = _fake_collection(
            ids=["s1", "w1"],
            docs=["strong", "weak"],
            distances=[0.05, 0.8],
        )

        out = _call_recall(coll, mode="vector", k=5)  # default min_score=0

        self.assertIn("thread_id=s1", out)
        self.assertIn("thread_id=w1", out)

    def test_existing_hybrid_path_still_says_found_n(self):
        """Backwards-compat — the old test_agent_lazy_paths assertion
        on '找到 2 筆' must keep working."""
        fake_collection = types.SimpleNamespace(
            query=mock.Mock(side_effect=RuntimeError("vector down")),
        )
        fake_bm25 = types.SimpleNamespace(get_scores=lambda _t: [3.0, 1.0])
        # 新版緊湊 cache 形狀：無 docs/metas，全文靠 fetch_docs_by_ids_fn 補抓
        fake_idx = {
            "bm25": fake_bm25,
            "ids": ["id-1", "id-2"],
            "sources": ["email", "note"],
            "dates": ["", ""],
        }
        fake_store = {
            "id-1": ("shoe order abc", {"source": "email", "ts": "2026-04-19T10:00:00"}),
            "id-2": ("other memory", {"source": "note", "ts": "2026-04-18T09:00:00"}),
        }

        from agent_core import memory_ops
        out = memory_ops.recall(
            "shoe abc",
            k=5,
            source="",
            mode="hybrid",
            min_score=0.0,
            get_memory_collection_fn=lambda: fake_collection,
            build_bm25_index_fn=lambda: fake_idx,
            simple_tokenize_fn=lambda s: s.split(),
            fetch_docs_by_ids_fn=lambda col, ids: {
                i: fake_store[i] for i in ids if i in fake_store
            },
            logger_obj=logging.getLogger(__name__),
        )

        self.assertIn("找到 2 筆", out)
        self.assertIn("id-1", out)


class RecallDefaultMinScoreTests(unittest.TestCase):
    def test_env_var_overrides_default_min_score(self):
        """RED_RECALL_DEFAULT_MIN_SCORE pre-filters weak hits without callers
        having to pass min_score explicitly."""
        import os
        from agent_core import memory as mem_mod

        fake_collection = _fake_collection(
            ids=["s1", "w1"],
            docs=["strong", "weak"],
            distances=[0.05, 0.8],  # → sim 0.95, 0.20
        )

        with mock.patch.object(mem_mod, "_get_memory_collection",
                               return_value=fake_collection), \
             mock.patch.dict(os.environ,
                             {"RED_RECALL_DEFAULT_MIN_SCORE": "0.5"}):
            out = mem_mod.recall("x", k=5, mode="vector")

        # The 0.20 hit must have been filtered out at daemon level.
        self.assertIn("thread_id=s1", out)
        self.assertNotIn("thread_id=w1", out)

    def test_explicit_min_score_overrides_env_default(self):
        import os
        from agent_core import memory as mem_mod

        fake_collection = _fake_collection(
            ids=["s1", "w1"],
            docs=["strong", "weak"],
            distances=[0.05, 0.8],
        )
        with mock.patch.object(mem_mod, "_get_memory_collection",
                               return_value=fake_collection), \
             mock.patch.dict(os.environ,
                             {"RED_RECALL_DEFAULT_MIN_SCORE": "0.5"}):
            # Caller-supplied 0.0 must beat the env-default 0.5.
            out = mem_mod.recall("x", k=5, mode="vector", min_score=0.0)
        self.assertIn("thread_id=s1", out)
        self.assertIn("thread_id=w1", out)

    def test_invalid_env_value_falls_back_to_zero(self):
        import os
        from agent_core import memory as mem_mod

        with mock.patch.dict(os.environ,
                             {"RED_RECALL_DEFAULT_MIN_SCORE": "not-a-number"}):
            self.assertEqual(mem_mod._recall_default_min_score(), 0.0)


if __name__ == "__main__":
    unittest.main()
