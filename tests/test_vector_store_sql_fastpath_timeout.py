"""The direct-SQLite fast-path must be time-bounded.

Root cause of the 2026-06-13 rag_sync hang: chroma.sqlite3 grew to 34GB, and a
fast-path query (a raw `sqlite3` read that bypasses the chroma HTTP server)
degraded into a full table scan. sqlite3's connect `timeout` only bounds lock
waits, not statement execution, so the query blocked the whole sync for 23h.

The fix bounds these queries with a progress-handler deadline: on overshoot the
query aborts with OperationalError, which every `_sql_*` method already maps to
`None` → the caller falls back to the (slower but bounded) HTTP path. These
tests pin both halves: a healthy query still works, and a query whose deadline
has passed aborts to `None` instead of hanging.
"""
from __future__ import annotations

import itertools
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.ingest import vector_store  # noqa: E402


def _build_min_chroma_sqlite(db_path: str) -> None:
    """Create the smallest chroma.sqlite3 the fast-path queries read: one
    collection, one METADATA segment, one embedding tagged doc_id='docA'."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (id TEXT, name TEXT);
            CREATE TABLE segments (id TEXT, collection TEXT, scope TEXT);
            CREATE TABLE embeddings (id INTEGER, segment_id TEXT);
            CREATE TABLE embedding_metadata (
                id INTEGER, key TEXT, string_value TEXT,
                int_value INTEGER, float_value REAL, bool_value INTEGER
            );
            INSERT INTO collections VALUES ('c1', 'drive_docs');
            INSERT INTO segments    VALUES ('seg1', 'c1', 'METADATA');
            INSERT INTO embeddings  VALUES (1, 'seg1');
            INSERT INTO embedding_metadata VALUES (1, 'doc_id', 'docA', NULL, NULL, NULL);
            INSERT INTO embedding_metadata VALUES (1, 'title',  'Hello', NULL, NULL, NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()


class SqlFastpathTimeoutTests(unittest.TestCase):
    def setUp(self):
        # 固定跑在 full-dim（bare collection 名，同下面 fixture 的 'drive_docs'）
        # 不管環境跑過什麼——某些測試（例如 briefing_preview 走 real
        # system_status() → _ensure_chroma_endpoint()）會把 RED_EMBED_DIM=768
        # 留在 process 環境沒清乾淨，同一輪 discover 後面的測試若不主動隔離就會
        # 讓 physical_collection_name() 誤解成要找 'drive_docs_768'，這裡的 fixture
        # 只有裸名 collection，segment 找不到、_sql_doc_ids() 全部回 None。
        self._embed_dim_patch = mock.patch.dict(os.environ, {"RED_EMBED_DIM": ""})
        self._embed_dim_patch.start()
        self.tmp = tempfile.mkdtemp(prefix="vs_sqlfastpath_")
        _build_min_chroma_sqlite(os.path.join(self.tmp, "chroma.sqlite3"))
        self._path_patch = mock.patch.object(vector_store, "_CHROMA_PATH", self.tmp)
        self._path_patch.start()
        self.vs = vector_store.VectorStore("drive_docs")

    def tearDown(self):
        self._path_patch.stop()
        self._embed_dim_patch.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_healthy_query_returns_data(self):
        """A normal fast-path read works (the bound never trips for a fast query)."""
        self.assertEqual(self.vs._sql_doc_ids(), {"docA"})

    def test_fastpath_aborts_to_none_when_deadline_passed(self):
        """When the deadline is exceeded the query aborts and the method returns
        None — the signal every caller uses to fall back to the HTTP path,
        instead of hanging on a multi-GB scan."""
        # Pre-warm the segment cache so the forced abort targets the main query,
        # not the (also-bounded) segment lookup.
        self.vs._metadata_segment_cache_id = "seg1"
        self.vs._metadata_segment_cache_until = float("inf")
        # monotonic jumps far past the deadline on the first handler check, and
        # the handler fires every opcode, so the query aborts immediately.
        with mock.patch.object(vector_store, "_SQL_FASTPATH_TIMEOUT_S", 30.0), \
             mock.patch.object(vector_store, "_SQL_FASTPATH_PROGRESS_OPS", 1), \
             mock.patch.object(vector_store.time, "monotonic",
                               side_effect=itertools.count(0.0, 1_000_000.0)):
            self.assertIsNone(self.vs._sql_doc_ids())

    def test_sqlite_connect_aborts_a_runaway_query(self):
        """The connection from _sqlite_connect actually interrupts a long query
        (real clock) — proving the mechanism on the exact shape that hung: a
        scan-like workload that never returns on its own."""
        with mock.patch.object(vector_store, "_SQL_FASTPATH_TIMEOUT_S", 0.2):
            conn = self.vs._sqlite_connect()
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    # Unbounded recursive CTE: only the deadline can stop it.
                    conn.execute(
                        "WITH RECURSIVE c(x) AS "
                        "(SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                        "SELECT count(*) FROM c"
                    ).fetchone()
            finally:
                conn.close()

    def test_disabled_when_timeout_zero(self):
        """Timeout 0 disables the bound entirely (no progress handler installed)
        — the healthy path still works, opting out cleanly."""
        with mock.patch.object(vector_store, "_SQL_FASTPATH_TIMEOUT_S", 0.0):
            self.assertEqual(self.vs._sql_doc_ids(), {"docA"})


if __name__ == "__main__":
    unittest.main()
