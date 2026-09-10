"""RAG_DRIVE_DEDUP_SCOPE：content-hash 去重的 scope（folder=歷史行為 / drive=清複本文化）。

釘住：①預設 folder 模式跨 folder 不合併（行為不變）；②drive 模式同 drive 跨
folder 合併、跨 drive 永不合併、空 drive_id（plain folder 目標）退回 folder 等值；
③search_drive_docs folder 級零命中自動放寬退路。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _info(**docs):
    """{'docA': ('drive1', 'folderX', True), ...} → _pick_same_scope_canonical 的 info。"""
    return {
        d: {"drive_id": dr, "folder_id": fo, "complete": comp}
        for d, (dr, fo, comp) in docs.items()
    }


class PickCanonicalFolderScopeTests(unittest.TestCase):
    """預設 folder 模式＝歷史行為。"""

    def _pick(self, info, drive_id, folder_id):
        from agent_core.ingest import vector_store as vs
        with mock.patch.object(vs, "_DEDUP_SCOPE", "folder"):
            return vs.VectorStore._pick_same_scope_canonical(
                info, list(info), drive_id, folder_id)

    def test_same_folder_matches(self):
        info = _info(docB=("d1", "f1", True))
        self.assertEqual(self._pick(info, "d1", "f1"), "docB")

    def test_cross_folder_not_merged(self):
        info = _info(docB=("d1", "f2", True))
        self.assertIsNone(self._pick(info, "d1", "f1"))

    def test_incomplete_candidate_excluded(self):
        info = _info(docB=("d1", "f1", False))
        self.assertIsNone(self._pick(info, "d1", "f1"))

    def test_smallest_doc_id_wins(self):
        info = _info(docC=("d1", "f1", True), docA=("d1", "f1", True))
        self.assertEqual(self._pick(info, "d1", "f1"), "docA")


class PickCanonicalDriveScopeTests(unittest.TestCase):
    """drive 模式＝同 drive 即合併。"""

    def _pick(self, info, drive_id, folder_id):
        from agent_core.ingest import vector_store as vs
        with mock.patch.object(vs, "_DEDUP_SCOPE", "drive"):
            return vs.VectorStore._pick_same_scope_canonical(
                info, list(info), drive_id, folder_id)

    def test_cross_folder_same_drive_merges(self):
        info = _info(docB=("d1", "f99", True))
        self.assertEqual(self._pick(info, "d1", "f1"), "docB")

    def test_cross_drive_never_merges(self):
        info = _info(docB=("d2", "f1", True))
        self.assertIsNone(self._pick(info, "d1", "f1"))

    def test_empty_drive_falls_back_to_folder_equality(self):
        # plain folder 目標（Meet Recordings 等）drive_id 空：跨 folder 不得誤併。
        info = _info(docB=("", "meetFolder", True))
        self.assertIsNone(self._pick(info, "", "otherFolder"))
        self.assertEqual(self._pick(info, "", "meetFolder"), "docB")

    def test_incomplete_candidate_still_excluded(self):
        info = _info(docB=("d1", "f99", False))
        self.assertIsNone(self._pick(info, "d1", "f1"))


class FolderZeroHitFallbackTests(unittest.TestCase):
    """search_drive_docs：folder 級零命中 → 自動放寬到 drive／全庫重查。"""

    def setUp(self):
        import agent_core.citation_feedback as _cf
        p = mock.patch.object(_cf, "LEDGER_FILE", os.path.join(
            os.path.sep, "tmp", f"cite_test_{os.getpid()}.jsonl"))
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(_cf, "RECENT_KEYS_FILE", os.path.join(
            os.path.sep, "tmp", f"cite_keys_test_{os.getpid()}.json"))
        p2.start()
        self.addCleanup(p2.stop)

    def _hit(self, folder="fOther"):
        return {
            "text": "越南文 SOP 內容。",
            "metadata": {"title": "QUI TRÌNH SX.pdf", "doc_id": "fCanon",
                         "drive_id": "d1", "folder_id": folder, "chunk_index": 0},
            "distance": 0.2,
        }

    def _store(self, per_call_hits):
        store = mock.MagicMock()
        store.count.return_value = 100
        store.is_empty.return_value = False
        store.query.side_effect = per_call_hits
        return store

    def test_folder_miss_falls_back_to_drive_and_notes_it(self):
        from agent_core.ingest import drive_search
        store = self._store([[], [self._hit()]])
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            r = drive_search.search_drive_docs("SOP", drive_id="d1", folder_id="f1")
        self.assertIn("放寬", r)
        self.assertIn("QUI TRÌNH SX.pdf", r)
        self.assertEqual(store.query.call_count, 2)
        # 第二次查詢的 where 不得再帶 folder_id 條件。
        second_where = str(store.query.call_args_list[1])
        self.assertNotIn("f1", second_where)

    def test_folder_miss_and_wider_miss_reports_no_hits(self):
        from agent_core.ingest import drive_search
        store = self._store([[], []])
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            r = drive_search.search_drive_docs("SOP", folder_id="f1")
        self.assertIn("沒找到", r)

    def test_no_folder_filter_never_double_queries(self):
        from agent_core.ingest import drive_search
        store = self._store([[]])
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            r = drive_search.search_drive_docs("SOP", drive_id="d1")
        self.assertIn("沒找到", r)
        self.assertEqual(store.query.call_count, 1)


if __name__ == "__main__":
    unittest.main()
