"""scripts/backfill_bge_collections.py — 資料變換與安全閘。

不碰 live chroma / embed server / 網路：EAV 串流與批次規劃用臨時 sqlite 與
純函式測；dry-run main 用假 sqlite 檔跑到「印計畫就返回」。腳本 import 時會
setdefault RED_EMBED_BACKEND/RED_EMBED_DIM — 一律包在 mock.patch.dict 裡載入，
避免 env 洩漏到其他測試（tests-immune-to-live-env 鐵則）。
"""
import contextlib
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from agent_core.path_safety import _REPO_ROOT


def _load_script():
    path = os.path.join(_REPO_ROOT, "scripts", "backfill_bge_collections.py")
    spec = importlib.util.spec_from_file_location("backfill_bge_collections", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_src_sqlite(path, coll_name, rows):
    """最小 chroma EAV：rows = [(eid, doc, {key: value}), ...]"""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE collections (id TEXT, name TEXT);
        CREATE TABLE segments (id TEXT, collection TEXT, scope TEXT);
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT);
        CREATE TABLE embedding_metadata (
            id INTEGER, key TEXT,
            string_value TEXT, int_value INTEGER,
            float_value REAL, bool_value INTEGER);
    """)
    con.execute("INSERT INTO collections VALUES ('c1', ?)", (coll_name,))
    con.execute("INSERT INTO segments VALUES ('seg1', 'c1', 'METADATA')")
    for i, (eid, doc, meta) in enumerate(rows, start=1):
        con.execute("INSERT INTO embeddings VALUES (?, 'seg1', ?)", (i, eid))
        con.execute(
            "INSERT INTO embedding_metadata (id, key, string_value) "
            "VALUES (?, 'chroma:document', ?)", (i, doc))
        for k, v in meta.items():
            if isinstance(v, bool):
                con.execute("INSERT INTO embedding_metadata (id, key, bool_value)"
                            " VALUES (?, ?, ?)", (i, k, int(v)))
            elif isinstance(v, int):
                con.execute("INSERT INTO embedding_metadata (id, key, int_value)"
                            " VALUES (?, ?, ?)", (i, k, v))
            elif isinstance(v, float):
                con.execute("INSERT INTO embedding_metadata (id, key, float_value)"
                            " VALUES (?, ?, ?)", (i, k, v))
            else:
                con.execute("INSERT INTO embedding_metadata (id, key, string_value)"
                            " VALUES (?, ?, ?)", (i, k, v))
    con.commit()
    con.close()


class BackfillBgeTests(unittest.TestCase):
    def setUp(self):
        # 載入腳本（隔離其 import-time env setdefault）
        self.env = mock.patch.dict(os.environ, {
            "RED_EMBED_BACKEND": "bge", "RED_EMBED_DIM": "768",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mod = _load_script()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def test_iter_rows_groups_and_restores_types(self):
        db = os.path.join(self.tmp, "src.sqlite3")
        _make_src_sqlite(db, "drive_docs_768", [
            ("id1", "文件一", {"chunk_index": 3, "access_red": True,
                               "title": "報表.pdf", "score": 0.5}),
            ("id2", "doc two", {"chunk_index": 0}),
        ])
        con = sqlite3.connect(db)
        seg = self.mod._metadata_segment_id(con, "drive_docs_768")
        rows = list(self.mod._iter_rows(con, seg))
        self.assertEqual(len(rows), 2)
        eid, meta = rows[0]
        self.assertEqual(eid, "id1")
        self.assertEqual(meta["chroma:document"], "文件一")
        self.assertIs(meta["access_red"], True)
        self.assertEqual(meta["chunk_index"], 3)
        self.assertEqual(meta["score"], 0.5)
        self.assertEqual(meta["title"], "報表.pdf")

    def test_iter_pending_batches_skips_and_packs(self):
        rows = [
            ("a", {"chroma:document": "甲", "k": 1}),
            ("b", {"chroma:document": "乙", "k": 2}),
            ("done1", {"chroma:document": "丙", "k": 3}),
            ("empty", {"chroma:document": "   ", "k": 4}),
            ("nodoc", {"k": 5}),
            ("c", {"chroma:document": "丁", "k": 6}),
        ]
        batches = list(self.mod.iter_pending_batches(iter(rows), {"done1"}, 2))
        self.assertEqual(len(batches), 2)
        ids0, docs0, metas0 = batches[0]
        self.assertEqual(ids0, ["a", "b"])
        self.assertEqual(docs0, ["甲", "乙"])
        # chroma:document 已 pop 掉、不能寫回 metadata
        self.assertEqual(metas0, [{"k": 1}, {"k": 2}])
        self.assertEqual(batches[1][0], ["c"])  # 殘批

    def test_src_dst_naming(self):
        self.assertEqual(self.mod._gemini_physical("drive_docs"), "drive_docs_768")
        with mock.patch.dict(os.environ, {"RED_EMBED_DIM": "3072"}):
            self.assertEqual(self.mod._gemini_physical("gmail_threads"),
                             "gmail_threads")

    def test_dry_run_prints_plan_and_stops(self):
        db = os.path.join(self.tmp, "src.sqlite3")
        _make_src_sqlite(db, "gmail_threads_768",
                         [("x", "內容", {"k": 1})])
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BGE_BACKFILL_SRC_DB": db}), \
                mock.patch.object(sys, "argv",
                                  ["backfill", "--logical", "gmail_threads"]), \
                contextlib.redirect_stdout(out):
            rc = self.mod.main()
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("src=gmail_threads_768", text)
        self.assertIn("dst=gmail_threads_bge1024", text)
        self.assertIn("dry-run", text)

    def test_execute_requires_embed_server(self):
        db = os.path.join(self.tmp, "src.sqlite3")
        _make_src_sqlite(db, "gmail_threads_768", [("x", "內容", {})])
        with mock.patch.dict(os.environ, {"BGE_BACKFILL_SRC_DB": db}), \
                mock.patch.object(sys, "argv",
                                  ["backfill", "--logical", "gmail_threads",
                                   "--execute"]), \
                mock.patch.object(self.mod, "server_alive", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main()
        self.assertIn("embed server", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
