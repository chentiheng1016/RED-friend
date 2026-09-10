"""rag_gap_detector：漏抽 = listing − indexed − skip-marker − 政策忽略。
驗三關過濾各自生效、listing 不完整保守低估、多 drive 彙總與 plain folder 略過。"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import rag_gap_detector as gap  # noqa: E402


def _f(fid, name="doc.pdf", mime="application/pdf", mtime="2026-07-10T00:00:00Z", parents=None):
    return {
        "id": fid, "name": name, "mimeType": mime,
        "modifiedTime": mtime, "parents": parents or ["p1"],
    }


class DetectDriveGapsTests(unittest.TestCase):
    def _patch(self, listing, complete, indexed, skipped):
        """patch drive_sync 三 helper + vector_store.get_store。"""
        store = mock.Mock()
        store.list_doc_ids_by_drive.return_value = set(indexed)
        p_list = mock.patch(
            "agent_core.ingest.drive_sync._list_drive_files",
            return_value=(listing, complete),
        )
        p_skip = mock.patch(
            "agent_core.ingest.drive_sync._load_skip_state",
            return_value={"version": 1, "files": {fid: {} for fid in skipped}},
        )
        p_store = mock.patch(
            "agent_core.ingest.vector_store.get_store", return_value=store
        )
        return p_list, p_skip, p_store

    def _run(self, listing, complete=True, indexed=(), skipped=()):
        p_list, p_skip, p_store = self._patch(listing, complete, indexed, skipped)
        with p_list, p_skip, p_store:
            return gap.detect_drive_gaps("drive-1", service=object())

    def test_indexed_files_are_not_gaps(self):
        r = self._run([_f("a"), _f("b")], indexed=["a", "b"])
        self.assertEqual(r["gap_count"], 0)

    def test_skip_marked_files_are_not_gaps(self):
        # b 有 skip-marker（extract 失敗在案）→ 不算漏抽
        r = self._run([_f("a"), _f("b")], indexed=["a"], skipped=["b"])
        self.assertEqual(r["gap_count"], 0)

    def test_policy_ignored_mime_not_gap_even_without_marker(self):
        # .dll（ignored 副檔名）新檔、沒 marker、沒索引 → 政策忽略、非漏抽
        r = self._run([_f("a", name="setup.dll", mime="application/x-msdownload")])
        self.assertEqual(r["gap_count"], 0)

    def test_true_gap_surfaces(self):
        # c：有檔、不在索引、無 marker、非忽略 MIME → 真漏抽
        r = self._run([_f("a"), _f("b"), _f("c")], indexed=["a"], skipped=["b"])
        self.assertEqual(r["gap_count"], 1)
        self.assertEqual(r["gaps"][0]["file_id"], "c")

    def test_gaps_sorted_by_mtime_desc(self):
        listing = [
            _f("old", mtime="2026-01-01T00:00:00Z"),
            _f("new", mtime="2026-07-01T00:00:00Z"),
        ]
        r = self._run(listing)
        self.assertEqual([g["file_id"] for g in r["gaps"]], ["new", "old"])

    def test_incomplete_listing_flagged_and_conservative(self):
        # listing 不完整：現存但沒列到的檔不會被誤報；旗標要傳出去
        r = self._run([_f("a")], complete=False, indexed=[])
        self.assertFalse(r["listing_complete"])
        self.assertEqual(r["gap_count"], 1)  # 只報列到的，沒列到的不誤報

    def test_missing_id_skipped(self):
        r = self._run([{"name": "no-id.pdf", "mimeType": "application/pdf"}])
        self.assertEqual(r["gap_count"], 0)

    def test_counts_reported(self):
        r = self._run([_f("a"), _f("b"), _f("c")], indexed=["a"], skipped=["b"])
        self.assertEqual(r["total_listed"], 3)
        self.assertEqual(r["indexed_count"], 1)
        self.assertEqual(r["skipped_count"], 1)


class DetectAllDriveGapsTests(unittest.TestCase):
    def test_plain_folder_targets_skipped_shared_drives_scanned(self):
        def fake_resolve(_svc, tid):
            # d1 = shared drive；folderX = plain 資料夾
            return ({"d1": ("會計", True), "folderX": ("Meet Recordings", False)}[tid])

        with mock.patch("agent_core.dashboard._ensure_chroma_endpoint"), \
             mock.patch("agent_core.google_auth.get_service", return_value=object()), \
             mock.patch.object(
                 gap, "load_targets",
                 return_value={"drive_folder_ids": ["d1", "folderX"]},
             ), \
             mock.patch("agent_core.rag_coverage._resolve_target_label", side_effect=fake_resolve), \
             mock.patch.object(
                 gap, "detect_drive_gaps",
                 return_value={
                     "drive_id": "d1", "listing_complete": True, "total_listed": 5,
                     "indexed_count": 3, "skipped_count": 1, "gap_count": 1,
                     "gaps": [{"file_id": "z", "name": "x.pdf", "mime_type": "application/pdf",
                               "modified_time": "2026-07-01T00:00:00Z", "parents": []}],
                 },
             ):
            summary = gap.detect_all_drive_gaps()

        self.assertEqual(summary["skipped_targets"], ["Meet Recordings"])
        self.assertIn("會計", summary["per_drive"])
        self.assertEqual(summary["total_gap_count"], 1)
        self.assertFalse(summary["any_incomplete"])


class RagGapReportTests(unittest.TestCase):
    def test_report_lists_gaps_and_notes(self):
        fake_summary = {
            "per_drive": {
                "會計": {
                    "label": "會計", "listing_complete": True, "total_listed": 100,
                    "indexed_count": 98, "skipped_count": 1, "gap_count": 2,
                    "gaps": [
                        {"file_id": "f1", "name": "缺的合約.pdf", "mime_type": "application/pdf",
                         "modified_time": "2026-07-10T00:00:00Z", "parents": []},
                        {"file_id": "f2", "name": "缺的報價.xlsx", "mime_type": "x",
                         "modified_time": "2026-07-09T00:00:00Z", "parents": []},
                    ],
                },
            },
            "skipped_targets": ["Meet Recordings"],
            "total_gap_count": 2,
            "any_incomplete": False,
        }
        with mock.patch.object(gap, "detect_all_drive_gaps", return_value=fake_summary):
            out = gap.rag_gap_report()
        self.assertIn("會計", out)
        self.assertIn("缺的合約.pdf", out)
        self.assertIn("f1", out)
        self.assertIn("合計漏抽 2", out)
        self.assertIn("Meet Recordings", out)

    def test_report_truncates_to_sample(self):
        gaps = [
            {"file_id": f"f{i}", "name": f"d{i}.pdf", "mime_type": "application/pdf",
             "modified_time": "2026-07-10T00:00:00Z", "parents": []}
            for i in range(20)
        ]
        fake_summary = {
            "per_drive": {"庫": {"label": "庫", "listing_complete": True,
                                 "total_listed": 100, "indexed_count": 80,
                                 "skipped_count": 0, "gap_count": 20, "gaps": gaps}},
            "skipped_targets": [], "total_gap_count": 20, "any_incomplete": False,
        }
        with mock.patch.object(gap, "detect_all_drive_gaps", return_value=fake_summary):
            out = gap.rag_gap_report(sample=5)
        self.assertIn("另有 15 筆", out)

    def test_empty_targets(self):
        with mock.patch.object(
            gap, "detect_all_drive_gaps",
            return_value={"per_drive": {}, "skipped_targets": [],
                          "total_gap_count": 0, "any_incomplete": False},
        ):
            out = gap.rag_gap_report()
        self.assertIn("未設定同步目標", out)


if __name__ == "__main__":
    unittest.main()
