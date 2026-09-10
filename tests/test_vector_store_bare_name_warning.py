"""裸名誤連警告：手動 session 忘帶 RED_EMBED_DIM 開到裸名 3072 collection、
而 production 資料在 _<dim> 兄弟時要印一次警告（warn-once）；production
suffixed 路徑零觸發。"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import embedding_config  # noqa: E402
from agent_core.ingest import vector_store  # noqa: E402


class _FakeClient:
    """get_collection 只認 existing 集合，其他名字 raise（模擬 chroma 行為）。"""

    def __init__(self, existing: set[str]):
        self.existing = existing
        self.calls: list[str] = []

    def get_collection(self, name: str):
        self.calls.append(name)
        if name not in self.existing:
            raise ValueError(f"Collection {name} does not exist.")
        return object()


class SiblingCollectionNamesTests(unittest.TestCase):
    def test_at_full_dim_returns_suffixed_names(self):
        # 兄弟清單涵蓋跨 backend 組合（_768 與 bge 的 _bge1024）。
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RED_EMBED_DIM", None)
            os.environ.pop("RED_EMBED_BACKEND", None)
            self.assertEqual(
                embedding_config.sibling_collection_names("drive_docs"),
                ["drive_docs_768", "drive_docs_bge1024"],
            )

    def test_at_768_returns_bare_name(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_DIM": "768"}):
            os.environ.pop("RED_EMBED_BACKEND", None)
            self.assertEqual(
                embedding_config.sibling_collection_names("drive_docs"),
                ["drive_docs", "drive_docs_bge1024"],
            )


class BareNameWarningTests(unittest.TestCase):
    def setUp(self):
        vector_store._BARE_NAME_SIBLING_WARNED.clear()
        self.addCleanup(vector_store._BARE_NAME_SIBLING_WARNED.clear)

    def _run(self, client, logical, physical) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            os_env = {}
            with mock.patch.dict(os.environ, os_env, clear=False):
                os.environ.pop("RED_EMBED_DIM", None)
                vector_store._warn_if_bare_name_shadows_suffixed(
                    client, logical, physical
                )
        return out.getvalue()

    def test_suffixed_physical_never_checks_or_warns(self):
        client = _FakeClient(existing={"drive_docs_768"})
        out = self._run(client, "drive_docs", "drive_docs_768")
        self.assertEqual(out, "")
        self.assertEqual(client.calls, [])  # production 路徑零 round-trip

    def test_bare_name_with_live_sibling_warns_once(self):
        client = _FakeClient(existing={"drive_docs_768"})
        out = self._run(client, "drive_docs", "drive_docs")
        self.assertIn("drive_docs_768", out)
        self.assertIn("RED_EMBED_DIM=768", out)
        # warn-once：第二次同 logical 靜默
        out2 = self._run(client, "drive_docs", "drive_docs")
        self.assertEqual(out2, "")

    def test_bare_name_without_sibling_is_silent(self):
        client = _FakeClient(existing=set())
        out = self._run(client, "drive_docs", "drive_docs")
        self.assertEqual(out, "")

    def test_sibling_probe_error_is_swallowed(self):
        class _ExplodingClient:
            def get_collection(self, name):
                raise RuntimeError("server down")

        out = self._run(_ExplodingClient(), "drive_docs", "drive_docs")
        self.assertEqual(out, "")  # 警告是 best-effort，絕不擋開 collection


class BareNameWarningSqliteTests(unittest.TestCase):
    """SQL fast-path 版：_metadata_segment 直讀 sqlite、不經 _open_collection，
    警告要在 _load_metadata_segment_id 的同一條連線上也觸發（Codex review P2）。"""

    def setUp(self):
        vector_store._BARE_NAME_SIBLING_WARNED.clear()
        self.addCleanup(vector_store._BARE_NAME_SIBLING_WARNED.clear)

    def _conn(self, collection_names: list[str]):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("create table collections (id text, name text)")
        conn.executemany(
            "insert into collections values (?, ?)",
            [(f"id-{n}", n) for n in collection_names],
        )
        return conn

    def _run(self, conn, logical, physical) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            vector_store._warn_if_bare_name_shadows_suffixed_sqlite(
                conn, logical, physical
            )
        return out.getvalue()

    def test_bare_name_with_sibling_row_warns(self):
        conn = self._conn(["drive_docs", "drive_docs_768"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RED_EMBED_DIM", None)
            out = self._run(conn, "drive_docs", "drive_docs")
        self.assertIn("drive_docs_768", out)
        self.assertIn("RED_EMBED_DIM=768", out)

    def test_suffixed_physical_is_silent_and_skips_query(self):
        conn = self._conn(["drive_docs"])  # 就算有裸名資料也不警告
        out = self._run(conn, "drive_docs", "drive_docs_768")
        self.assertEqual(out, "")

    def test_no_sibling_row_is_silent(self):
        conn = self._conn(["drive_docs"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RED_EMBED_DIM", None)
            out = self._run(conn, "drive_docs", "drive_docs")
        self.assertEqual(out, "")

    def test_missing_table_is_swallowed(self):
        import sqlite3

        conn = sqlite3.connect(":memory:")  # 沒有 collections 表
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RED_EMBED_DIM", None)
            out = self._run(conn, "drive_docs", "drive_docs")
        self.assertEqual(out, "")

    def test_warn_once_shared_with_http_probe(self):
        conn = self._conn(["drive_docs", "drive_docs_768"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RED_EMBED_DIM", None)
            first = self._run(conn, "drive_docs", "drive_docs")
            self.assertIn("drive_docs_768", first)
            # 同 process 內 HTTP 版不再重複警告
            client = _FakeClient(existing={"drive_docs_768"})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                vector_store._warn_if_bare_name_shadows_suffixed(
                    client, "drive_docs", "drive_docs"
                )
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
