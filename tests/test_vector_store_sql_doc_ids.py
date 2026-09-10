"""_sql_doc_ids 的 filter 變體查詢計畫 + 慢路徑 fallback 記錄。

2026-07 深檢：filter 變體（list_doc_ids_by_drive/folder）沒有 INDEXED BY 提示，
SQLite planner 從 doc_id 側起手、range-scan 全 DB 的 embedding_metadata
（live 54GB 實測 87.9s/次）；改成 filter-key 先行（同 _sql_find_duplicate_doc_id
2026-06-27 的同病同修）後 1.76s。這裡用最小 fixture 釘住：

  1. 帶正式 index（同 chromadb migration 00004 的 partial index 定義）時，
     filter 變體回正確結果。
  2. index 不存在（INDEXED BY 直接報錯）→ 回 None + 印出 fallback 記錄，
     caller 走 HTTP paged scan。
  3. 沒有 sqlite metadata segment → 同樣回 None + 記錄（之前完全無 log，
     慢路徑靜默拖垮夜跑無從診斷）。
"""
from __future__ import annotations

import contextlib
import io
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


def _build_chroma_sqlite(db_path: str, with_index: bool = True) -> None:
    """兩個 doc 的最小 chroma.sqlite3：docA(folder F1)、docB(folder F2)，
    同一 Shared Drive D1。index 定義照抄 chromadb migration
    00004-metadata-indices（partial、(key, string_value)）。"""
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
            INSERT INTO embeddings  VALUES (2, 'seg1');
            INSERT INTO embedding_metadata VALUES (1, 'doc_id',    'docA', NULL, NULL, NULL);
            INSERT INTO embedding_metadata VALUES (1, 'folder_id', 'F1',   NULL, NULL, NULL);
            INSERT INTO embedding_metadata VALUES (1, 'drive_id',  'D1',   NULL, NULL, NULL);
            INSERT INTO embedding_metadata VALUES (2, 'doc_id',    'docB', NULL, NULL, NULL);
            INSERT INTO embedding_metadata VALUES (2, 'folder_id', 'F2',   NULL, NULL, NULL);
            INSERT INTO embedding_metadata VALUES (2, 'drive_id',  'D1',   NULL, NULL, NULL);
            """
        )
        if with_index:
            conn.execute(
                "CREATE INDEX embedding_metadata_string_value "
                "ON embedding_metadata (key, string_value) "
                "WHERE string_value IS NOT NULL"
            )
        conn.commit()
    finally:
        conn.close()


class _FixtureBase(unittest.TestCase):
    with_index = True

    def setUp(self):
        # 隔離 RED_EMBED_DIM，理由同 test_vector_store_sql_fastpath_timeout。
        self._embed_dim_patch = mock.patch.dict(os.environ, {"RED_EMBED_DIM": ""})
        self._embed_dim_patch.start()
        self.tmp = tempfile.mkdtemp(prefix="vs_sqldocids_")
        _build_chroma_sqlite(
            os.path.join(self.tmp, "chroma.sqlite3"), with_index=self.with_index
        )
        self._path_patch = mock.patch.object(vector_store, "_CHROMA_PATH", self.tmp)
        self._path_patch.start()
        self.vs = vector_store.VectorStore("drive_docs")

    def tearDown(self):
        self._path_patch.stop()
        self._embed_dim_patch.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class SqlDocIdsFilterVariantTests(_FixtureBase):
    def test_folder_filter_returns_scoped_doc_ids(self):
        self.assertEqual(self.vs._sql_doc_ids("folder_id", "F1"), {"docA"})
        self.assertEqual(self.vs._sql_doc_ids("folder_id", "F2"), {"docB"})

    def test_drive_filter_returns_scoped_doc_ids(self):
        self.assertEqual(self.vs._sql_doc_ids("drive_id", "D1"), {"docA", "docB"})

    def test_unmatched_filter_returns_empty_set_not_none(self):
        # 權威的「沒有」— caller 不可誤判成 fast-path 失敗而走 HTTP。
        self.assertEqual(self.vs._sql_doc_ids("folder_id", "NOPE"), set())

    def test_unfiltered_listing_unchanged(self):
        self.assertEqual(self.vs._sql_doc_ids(), {"docA", "docB"})


class SqlDocIdsFallbackLoggingTests(_FixtureBase):
    with_index = False  # INDEXED BY 找不到 index → OperationalError → fallback

    def test_missing_index_returns_none_and_logs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self.vs._sql_doc_ids("folder_id", "F1")
        self.assertIsNone(result)
        out = buf.getvalue()
        self.assertIn("SQL fast-path unavailable", out)
        self.assertIn("folder_id=F1", out)
        self.assertIn("paged scan", out)

    def test_no_metadata_segment_returns_none_and_logs(self):
        # fixture 只有 drive_docs collection → gmail_threads 找不到 segment。
        other = vector_store.VectorStore("gmail_threads")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = other._sql_doc_ids()
        self.assertIsNone(result)
        out = buf.getvalue()
        self.assertIn("no sqlite metadata segment", out)
        self.assertIn("all docs", out)


class ListDocIdsHttpFallbackTests(unittest.TestCase):
    def test_list_doc_ids_by_folder_falls_back_to_paged_metadatas(self):
        """fast-path 回 None 時 caller 走 HTTP _paged_metadatas，行為不變。"""
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        store._collection_name = "drive_docs"
        store._sql_doc_ids = lambda *a, **k: None  # 模擬 fast-path 失敗
        col = mock.MagicMock()
        col.get.return_value = {"metadatas": [{"doc_id": "x"}]}
        store._col = col
        self.assertEqual(store.list_doc_ids_by_folder("F1"), {"x"})
        col.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
