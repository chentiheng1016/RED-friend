"""Tests for the wall-clock timeout guard around Chroma HTTP ops.

ChromaDB's HttpClient has no per-request timeout. A bloated collection that
cache-misses + reloads its HNSW segment on every upsert froze the whole nightly
sync for ~55min on a single collection_upsert (2026-06-15) — the main thread
parked in socket.recv() with nothing to break it. _run_chroma_op_with_timeout
bounds each op so the caller defers the batch and the sync keeps moving instead
of hanging forever (the same guarantee _gemini_embed_with_timeout gives embeds
and _SQL_FASTPATH_TIMEOUT_S gives the direct-SQLite path).
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class RunChromaOpWithTimeoutTests(unittest.TestCase):
    def test_fast_op_returns_value(self):
        from agent_core.ingest import vector_store
        sentinel = {"ids": ["a"]}
        result = vector_store._run_chroma_op_with_timeout(
            lambda col: sentinel, object(), "drive_docs"
        )
        self.assertIs(result, sentinel)

    def test_hanging_op_raises_chroma_op_timeout(self):
        """The core guarantee: a wedged Chroma call aborts instead of hanging
        the sync forever."""
        from agent_core.ingest import vector_store

        with mock.patch.object(vector_store, "_CHROMA_OP_TIMEOUT_S", 0.1):
            never_returns = threading.Event()
            self.addCleanup(never_returns.set)  # release the leaked daemon thread

            def hang(col):
                never_returns.wait(timeout=10)  # blocks; safety net
                return "late"

            with self.assertRaises(vector_store.ChromaOpTimeout):
                vector_store._run_chroma_op_with_timeout(hang, object(), "gmail_messages")

    def test_inner_exception_propagates(self):
        from agent_core.ingest import vector_store

        def boom(col):
            raise ValueError("xyz")

        with self.assertRaises(ValueError) as cm:
            vector_store._run_chroma_op_with_timeout(boom, object(), "drive_docs")
        self.assertIn("xyz", str(cm.exception))

    def test_zero_timeout_runs_inline(self):
        """0 disables the guard — the op runs on the caller thread, no deadline,
        no spawned daemon thread."""
        from agent_core.ingest import vector_store
        ran_on = []

        def op(col):
            ran_on.append(threading.current_thread())
            return 42

        with mock.patch.object(vector_store, "_CHROMA_OP_TIMEOUT_S", 0):
            result = vector_store._run_chroma_op_with_timeout(op, object(), "drive_docs")
        self.assertEqual(result, 42)
        self.assertEqual(ran_on, [threading.current_thread()])

    def test_chroma_op_timeout_is_ordinary_exception(self):
        """Callers defer on `except Exception` (e.g. gmail_sync._flush) — the
        timeout must be an ordinary Exception, not BaseException, or a stalled
        upsert would escape the handler and crash the run."""
        from agent_core.ingest import vector_store
        self.assertTrue(issubclass(vector_store.ChromaOpTimeout, Exception))
        self.assertTrue(issubclass(vector_store.ChromaOpTimeout, TimeoutError))


class WithCollectionTimeoutTests(unittest.TestCase):
    def _store(self):
        from agent_core.ingest import vector_store
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        store._collection_name = "gmail_messages"
        store._col = mock.MagicMock()
        return store, vector_store

    def test_passes_through_value(self):
        store, _vs = self._store()
        self.assertEqual(store._with_collection(lambda col: "ok"), "ok")

    def test_times_out_without_treating_it_as_a_stale_collection(self):
        """A hung op surfaces as ChromaOpTimeout. It must NOT be mistaken for a
        stale-collection handle (which would refresh + immediately re-run the op,
        re-hanging on the same wedged collection)."""
        store, vector_store = self._store()
        refreshed = []
        store._refresh_collection = lambda: refreshed.append(True)

        with mock.patch.object(vector_store, "_CHROMA_OP_TIMEOUT_S", 0.1):
            never_returns = threading.Event()
            self.addCleanup(never_returns.set)

            def hang(col):
                never_returns.wait(timeout=10)
                return None

            with self.assertRaises(vector_store.ChromaOpTimeout):
                store._with_collection(hang)

        self.assertEqual(refreshed, [])  # not retried


if __name__ == "__main__":
    unittest.main()
