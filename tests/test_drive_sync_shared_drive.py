"""Tests for Shared-Drive recursive sync (Option A).

Covers:
  - is_shared_drive_id() heuristic
  - _list_drive_files() builds the right Drive API call (corpora=drive, driveId,
    supportsAllDrives, includeItemsFromAllDrives) and aggregates pages
  - _list_drive_files() flips listing_complete=False on incompleteSearch
  - sync_shared_drive() ingests files across multiple subfolders, stamps drive_id
  - sync_shared_drive() skips purge when listing_complete=False
  - sync_shared_drive() purges when listing_complete=True
  - rag_runner dispatches 0A* IDs to sync_shared_drive, others to sync_folder
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── helpers ─────────────────────────────────────────────────────────

def _build_fake_service(pages: list[dict]):
    """Build a Drive-service mock whose files().list(...).execute() returns
    the supplied pages in order. Each call to list() pops the next page."""
    page_iter = iter(pages)
    last_kwargs: dict = {}

    def _list(**kwargs):
        last_kwargs.clear()
        last_kwargs.update(kwargs)
        page = next(page_iter)
        req = mock.MagicMock()
        req.execute.return_value = page
        return req

    files_obj = mock.MagicMock()
    files_obj.list = _list
    service = mock.MagicMock()
    service.files.return_value = files_obj
    return service, last_kwargs


# ── tests ───────────────────────────────────────────────────────────

class IsSharedDriveIdTests(unittest.TestCase):
    def test_classic_shared_drive_id(self):
        from agent_core.ingest import drive_sync
        # Real-shape Shared Drive root IDs from the user's config.
        for sid in [
            "0AF1sBHRGfyhkUk9PVA",
            "0ANb85sbGWOKAUk9PVA",
            "0AEYnVMdnwfFQUk9PVA",
        ]:
            self.assertTrue(drive_sync.is_shared_drive_id(sid), sid)

    def test_regular_folder_id_rejected(self):
        from agent_core.ingest import drive_sync
        # Real-shape folder/file IDs (33+ chars, start with '1').
        for fid in [
            "17jocxZmUI5NnwwdKmlA1yGjjzskap-FVKG10POd4Roo",
            "1Kf8X2Rc481N6e2RzCwHDaif-mLfLtzz5W3lJTyBHsu8",
        ]:
            self.assertFalse(drive_sync.is_shared_drive_id(fid), fid)

    def test_empty_and_whitespace(self):
        from agent_core.ingest import drive_sync
        self.assertFalse(drive_sync.is_shared_drive_id(""))
        self.assertFalse(drive_sync.is_shared_drive_id("   "))


class ListDriveFilesTests(unittest.TestCase):
    def test_request_kwargs_target_single_drive(self):
        from agent_core.ingest import drive_sync
        service, last_kwargs = _build_fake_service([
            {"files": [{"id": "f1", "name": "a", "mimeType": "text/plain", "parents": ["sub1"]}]},
        ])
        files, complete = drive_sync._list_drive_files(service, "0ABCDEF1234567890")
        self.assertEqual([f["id"] for f in files], ["f1"])
        self.assertTrue(complete)
        self.assertEqual(last_kwargs["corpora"], "drive")
        self.assertEqual(last_kwargs["driveId"], "0ABCDEF1234567890")
        self.assertTrue(last_kwargs["includeItemsFromAllDrives"])
        self.assertTrue(last_kwargs["supportsAllDrives"])
        # Must NOT scope by parents — we want recursive across all subfolders.
        self.assertNotIn("'sub1' in parents", last_kwargs.get("q", ""))

    def test_paginates_and_aggregates(self):
        from agent_core.ingest import drive_sync
        service, _ = _build_fake_service([
            {"files": [{"id": "f1", "name": "a", "mimeType": "text/plain", "parents": ["sub1"]}],
             "nextPageToken": "tok"},
            {"files": [{"id": "f2", "name": "b", "mimeType": "text/plain", "parents": ["sub2"]}]},
        ])
        files, complete = drive_sync._list_drive_files(service, "0ABCDEF1234567890")
        self.assertEqual([f["id"] for f in files], ["f1", "f2"])
        self.assertTrue(complete)

    def test_incomplete_search_flag_propagates(self):
        from agent_core.ingest import drive_sync
        service, _ = _build_fake_service([
            {"files": [{"id": "f1", "name": "a", "mimeType": "text/plain", "parents": ["sub1"]}],
             "incompleteSearch": True},
        ])
        files, complete = drive_sync._list_drive_files(service, "0ABCDEF1234567890")
        self.assertEqual(len(files), 1)
        self.assertFalse(complete)

    def test_user_rate_limit_is_retried(self):
        from agent_core.ingest import drive_sync

        class _Resp(dict):
            status = 403

        class _RateLimitError(Exception):
            resp = _Resp()

            def __str__(self):
                return "User rate limit exceeded."

        req = mock.MagicMock()
        req.execute.side_effect = [
            _RateLimitError(),
            {"files": [{"id": "f1", "name": "a", "mimeType": "text/plain", "parents": ["sub1"]}]},
        ]
        files_obj = mock.MagicMock()
        files_obj.list.return_value = req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_MAX_ATTEMPTS", 2), \
             mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_BASE_SLEEP_S", 0):
            files, complete = drive_sync._list_drive_files(service, "0ABCDEF1234567890")

        self.assertEqual([f["id"] for f in files], ["f1"])
        self.assertTrue(complete)
        self.assertEqual(req.execute.call_count, 2)

    def test_timeout_is_retried(self):
        from agent_core.ingest import drive_sync

        first_req = mock.MagicMock()
        first_req.execute.side_effect = TimeoutError("socket timeout")
        second_req = mock.MagicMock()
        second_req.execute.return_value = {
            "files": [{"id": "f1", "name": "a", "mimeType": "text/plain", "parents": ["sub1"]}],
        }
        files_obj = mock.MagicMock()
        files_obj.list.side_effect = [first_req, second_req]
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_MAX_ATTEMPTS", 2), \
             mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_BASE_SLEEP_S", 0):
            files, complete = drive_sync._list_drive_files(service, "0ABCDEF1234567890")

        self.assertEqual([f["id"] for f in files], ["f1"])
        self.assertTrue(complete)
        first_req.execute.assert_called_once()
        second_req.execute.assert_called_once()
        self.assertEqual(files_obj.list.call_count, 2)

    def test_wall_clock_timeout_is_not_retried(self):
        from agent_core.ingest import drive_sync

        req = mock.MagicMock()
        factory_calls = 0

        def request_factory():
            nonlocal factory_calls
            factory_calls += 1
            return req

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_MAX_ATTEMPTS", 2), \
             mock.patch.object(
                 drive_sync,
                 "_run_with_timeout",
                 side_effect=drive_sync._WallClockTimeoutError("Drive list exceeded 150s"),
             ) as run_with_timeout:
            with self.assertRaises(drive_sync._WallClockTimeoutError):
                drive_sync._execute_drive_request(request_factory, "Drive list")

        self.assertEqual(factory_calls, 1)
        run_with_timeout.assert_called_once()

    def test_connection_error_is_retried(self):
        from agent_core.ingest import drive_sync

        req = mock.MagicMock()
        req.execute.side_effect = [
            ConnectionError("temporary network drop"),
            {"files": [{"id": "f1", "name": "a", "mimeType": "text/plain", "parents": ["sub1"]}]},
        ]
        files_obj = mock.MagicMock()
        files_obj.list.return_value = req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_MAX_ATTEMPTS", 2), \
             mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_BASE_SLEEP_S", 0):
            files, complete = drive_sync._list_drive_files(service, "0ABCDEF1234567890")

        self.assertEqual([f["id"] for f in files], ["f1"])
        self.assertTrue(complete)
        self.assertEqual(req.execute.call_count, 2)


class ListFolderRecursiveTests(unittest.TestCase):
    def test_folder_children_query_includes_supported_files_and_folders(self):
        from agent_core.ingest import drive_sync
        service, last_kwargs = _build_fake_service([
            {"files": [
                {"id": "sub1", "name": "Sub", "mimeType": drive_sync._FOLDER_MIME},
                {"id": "file1", "name": "Doc", "mimeType": "text/plain"},
            ]},
        ])

        files, complete = drive_sync._list_folder_with_children(service, "root-folder")

        self.assertEqual([f["id"] for f in files], ["sub1", "file1"])
        self.assertTrue(complete)
        self.assertIn("'root-folder' in parents", last_kwargs["q"])
        self.assertIn(drive_sync._FOLDER_MIME, last_kwargs["q"])
        self.assertIn("text/plain", last_kwargs["q"])
        # incompleteSearch must be requested so purge can honor partial results;
        # size rides along so _prepare_file can skip the per-file files().get.
        self.assertIn("incompleteSearch", last_kwargs["fields"])
        self.assertIn("size", last_kwargs["fields"])

    def test_list_folder_flags_incomplete_search(self):
        from agent_core.ingest import drive_sync
        service, last_kwargs = _build_fake_service([
            {"files": [{"id": "f1", "name": "a", "mimeType": "text/plain"}],
             "incompleteSearch": True},
        ])
        files, complete = drive_sync._list_folder(service, "folder1")
        self.assertEqual([f["id"] for f in files], ["f1"])
        self.assertFalse(complete)
        self.assertIn("incompleteSearch", last_kwargs["fields"])
        self.assertIn("size", last_kwargs["fields"])

    def test_recursive_plain_folder_walks_child_folders(self):
        from agent_core.ingest import drive_sync

        def fake_children(_service, folder_id):
            return {
                "root": [
                    {"id": "sub1", "name": "Sub", "mimeType": drive_sync._FOLDER_MIME},
                    {"id": "file1", "name": "Root doc", "mimeType": "text/plain"},
                ],
                "sub1": [
                    {"id": "sub2", "name": "Nested", "mimeType": drive_sync._FOLDER_MIME},
                    {"id": "file2", "name": "Child doc", "mimeType": "text/plain"},
                ],
                "sub2": [
                    {"id": "file3", "name": "Deep doc", "mimeType": "text/plain"},
                ],
            }.get(folder_id, []), True

        with mock.patch.object(drive_sync, "_list_folder_with_children", side_effect=fake_children):
            files, complete = drive_sync._list_folder_recursive(mock.MagicMock(), "root")

        self.assertEqual([f["id"] for f in files], ["file1", "file2", "file3"])
        self.assertTrue(complete)

    def test_recursive_incomplete_child_listing_flags_whole_walk(self):
        """One incomplete subfolder page must poison the whole recursive walk
        — the root-scoped purge would otherwise delete that subtree."""
        from agent_core.ingest import drive_sync

        def fake_children(_service, folder_id):
            if folder_id == "root":
                return [
                    {"id": "sub1", "name": "Sub", "mimeType": drive_sync._FOLDER_MIME},
                    {"id": "file1", "name": "Root doc", "mimeType": "text/plain"},
                ], True
            return [], False  # sub1 listing came back incomplete

        with mock.patch.object(drive_sync, "_list_folder_with_children", side_effect=fake_children):
            files, complete = drive_sync._list_folder_recursive(mock.MagicMock(), "root")

        self.assertEqual([f["id"] for f in files], ["file1"])
        self.assertFalse(complete)

    def test_retry_loop_uses_logging_module(self):
        from agent_core.ingest import drive_sync

        first_req = mock.MagicMock()
        first_req.execute.side_effect = ConnectionError("temporary network drop")
        second_req = mock.MagicMock()
        second_req.execute.return_value = {"ok": True}
        requests = iter([first_req, second_req])

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_MAX_ATTEMPTS", 2), \
             mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_BASE_SLEEP_S", 0), \
             self.assertLogs("agent_core.ingest.drive_sync", level="WARNING") as logs:
            result = drive_sync._execute_drive_request(lambda: next(requests), "Drive list")

        self.assertEqual(result, {"ok": True})
        self.assertIn("Drive RPC retry 1/2 Drive list", logs.output[0])

    def test_retry_backoff_adds_jitter(self):
        from agent_core.ingest import drive_sync

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_BASE_SLEEP_S", 15), \
             mock.patch.object(drive_sync.random, "uniform", return_value=0.25):
            sleep_s = drive_sync._drive_retry_sleep_seconds(ConnectionError("drop"), attempt=2)

        self.assertEqual(sleep_s, 30.25)

    def test_retry_after_header_is_capped(self):
        from agent_core.ingest import drive_sync

        class _Resp(dict):
            status = 429

        class _RateLimitError(Exception):
            resp = _Resp({"retry-after": "7200"})

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_MAX_SLEEP_S", 3600):
            sleep_s = drive_sync._drive_retry_sleep_seconds(_RateLimitError(), attempt=1)

        self.assertEqual(sleep_s, 3600.0)

    def test_retry_backoff_zero_base_skips_jitter(self):
        from agent_core.ingest import drive_sync

        with mock.patch.object(drive_sync, "_DRIVE_RPC_RETRY_BASE_SLEEP_S", 0), \
             mock.patch.object(drive_sync.random, "uniform") as jitter:
            sleep_s = drive_sync._drive_retry_sleep_seconds(ConnectionError("drop"), attempt=1)

        self.assertEqual(sleep_s, 0.0)
        jitter.assert_not_called()


class OfficeLockFileFilterTests(unittest.TestCase):
    """MS Office writes hidden owner files like ~$report.xlsx that have an
    Office MIME type but aren't valid OOXML zips. The Drive API returns them
    in listings; the listing layer must drop them before they reach the
    extractor (where openpyxl/python-docx would BadZipFile-error)."""

    def test_predicate_recognises_lock_prefix(self):
        from agent_core.ingest import drive_sync
        self.assertTrue(drive_sync._is_office_lock_file("~$report.xlsx"))
        self.assertTrue(drive_sync._is_office_lock_file("~$contract.docx"))
        self.assertTrue(drive_sync._is_office_lock_file("~$"))
        self.assertFalse(drive_sync._is_office_lock_file("report.xlsx"))
        self.assertFalse(drive_sync._is_office_lock_file("contains~$inside.xlsx"))
        self.assertFalse(drive_sync._is_office_lock_file(""))

    def test_junk_predicate_recognises_appledouble_and_desktop_artifacts(self):
        from agent_core.ingest import drive_sync
        self.assertTrue(drive_sync._is_drive_junk_file("._photo.jpg"))
        self.assertTrue(drive_sync._is_drive_junk_file(".DS_Store"))
        self.assertTrue(drive_sync._is_drive_junk_file("Thumbs.db"))
        self.assertTrue(drive_sync._is_drive_junk_file("desktop.ini"))
        self.assertTrue(drive_sync._is_drive_junk_file("~$report.xlsx"))
        self.assertFalse(drive_sync._is_drive_junk_file("photo.jpg"))

    def test_list_drive_files_drops_lock_files(self):
        from agent_core.ingest import drive_sync
        service, _ = _build_fake_service([
            {"files": [
                {"id": "f1", "name": "real.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                {"id": "f2", "name": "~$real.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                {"id": "f3", "name": "~$other.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
                {"id": "f4", "name": "._photo.jpg", "mimeType": "image/jpeg"},
                {"id": "f5", "name": ".DS_Store", "mimeType": "text/plain"},
            ]},
        ])
        files, complete = drive_sync._list_drive_files(service, "0AABCDEF")
        self.assertEqual([f["id"] for f in files], ["f1"])
        self.assertTrue(complete)


class SyncSharedDriveTests(unittest.TestCase):
    def _patch_env(self, files: list[dict], listing_complete: bool, indexed_ids: set[str]):
        """Wire up the mocks shared by sync_shared_drive tests."""
        store = mock.MagicMock()
        store.list_doc_ids_by_drive.return_value = indexed_ids
        ds = sys.modules["agent_core.ingest.drive_sync"]
        return (
            mock.patch.object(ds, "get_store", return_value=store),
            mock.patch.object(ds, "_list_drive_files", return_value=(files, listing_complete)),
            mock.patch("agent_core.google_auth.get_service", return_value=mock.MagicMock()),
            store,
        )

    def test_recurses_across_subfolders_and_stamps_drive_id(self):
        from agent_core.ingest import drive_sync
        files = [
            {"id": "fa", "name": "A", "mimeType": "text/plain", "parents": ["sub1"]},
            {"id": "fb", "name": "B", "mimeType": "text/plain", "parents": ["sub2"]},
            {"id": "fc", "name": "C", "mimeType": "text/plain", "parents": ["sub2/nested"]},
        ]
        p1, p2, p3, store = self._patch_env(files, listing_complete=True, indexed_ids=set())
        sync_calls = []
        def fake_sync_file(file_id, folder_id="", drive_id="", modified_time=""):
            sync_calls.append((file_id, folder_id, drive_id))
            return {"file_id": file_id, "title": "x", "chunks": 1}

        with p1, p2, p3, mock.patch.object(drive_sync, "sync_file", side_effect=fake_sync_file):
            result = drive_sync.sync_shared_drive("0ABCDEF1234567890")

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["synced"], 3)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["drive_id"], "0ABCDEF1234567890")
        self.assertTrue(result["listing_complete"])
        # Each file gets its parent stamped as folder_id, plus the shared drive_id.
        self.assertEqual(
            sync_calls,
            [
                ("fa", "sub1", "0ABCDEF1234567890"),
                ("fb", "sub2", "0ABCDEF1234567890"),
                ("fc", "sub2/nested", "0ABCDEF1234567890"),
            ],
        )

    def test_skips_purge_when_listing_incomplete(self):
        from agent_core.ingest import drive_sync
        files = [{"id": "fa", "name": "A", "mimeType": "text/plain", "parents": ["sub1"]}]
        # Stale doc 'old' is indexed but not in current listing. With
        # incompleteSearch=True the purge MUST be skipped — partial results
        # must never trigger deletes.
        p1, p2, p3, store = self._patch_env(files, listing_complete=False, indexed_ids={"fa", "old"})

        with p1, p2, p3, mock.patch.object(drive_sync, "sync_file", return_value={"file_id": "fa", "chunks": 1}):
            result = drive_sync.sync_shared_drive("0ABCDEF1234567890")

        self.assertFalse(result["listing_complete"])
        self.assertEqual(result["purged"], 0)
        store.delete_by_doc_id.assert_not_called()

    def test_purges_when_listing_complete(self):
        from agent_core.ingest import drive_sync
        files = [{"id": "fa", "name": "A", "mimeType": "text/plain", "parents": ["sub1"]}]
        p1, p2, p3, store = self._patch_env(files, listing_complete=True, indexed_ids={"fa", "old"})

        with p1, p2, p3, mock.patch.object(drive_sync, "sync_file", return_value={"file_id": "fa", "chunks": 1}):
            result = drive_sync.sync_shared_drive("0ABCDEF1234567890")

        self.assertEqual(result["purged"], 1)
        store.delete_by_doc_id.assert_called_once_with("old")

    def test_sync_file_exception_marks_skipped(self):
        from agent_core.ingest import drive_sync
        files = [{"id": "fa", "name": "A", "mimeType": "text/plain", "parents": ["sub1"]}]
        p1, p2, p3, _ = self._patch_env(files, listing_complete=True, indexed_ids=set())

        with p1, p2, p3, mock.patch.object(drive_sync, "sync_file", side_effect=RuntimeError("boom")):
            result = drive_sync.sync_shared_drive("0ABCDEF1234567890")

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("error: boom", result["details"][0]["reason"])


class SyncPlainFolderRecursiveTests(unittest.TestCase):
    def test_recursive_folder_stamps_root_folder_and_purges_by_root(self):
        from agent_core.ingest import drive_sync

        files = [
            {
                "id": "child-file",
                "name": "Child",
                "mimeType": "text/plain",
                "parents": ["subfolder"],
                "modifiedTime": "2026-05-17T00:00:00Z",
            },
        ]
        store = mock.MagicMock()
        store.list_doc_ids_by_folder.return_value = {"child-file", "old-file"}
        calls = []

        def fake_sync_file(file_id, folder_id="", drive_id="", modified_time=""):
            calls.append((file_id, folder_id, drive_id, modified_time))
            return {"file_id": file_id, "chunks": 1}

        with mock.patch("agent_core.google_auth.get_service", return_value=mock.MagicMock()), \
             mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch.object(drive_sync, "_list_folder_recursive", return_value=(files, True)), \
             mock.patch.object(drive_sync, "sync_file", side_effect=fake_sync_file):
            result = drive_sync.sync_folder("root-folder", recursive=True)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["purged"], 1)
        self.assertTrue(result["listing_complete"])
        store.delete_by_doc_id.assert_called_once_with("old-file")
        self.assertEqual(
            calls,
            [("child-file", "root-folder", "", "2026-05-17T00:00:00Z")],
        )

    def test_sync_folder_skips_purge_when_listing_incomplete(self):
        """incompleteSearch on a plain-folder listing must block the purge —
        the same partial-listing contract sync_shared_drive already honors."""
        from agent_core.ingest import drive_sync

        files = [
            {"id": "child-file", "name": "Child", "mimeType": "text/plain",
             "modifiedTime": "2026-05-17T00:00:00Z"},
        ]
        store = mock.MagicMock()
        store.list_doc_ids_by_folder.return_value = {"child-file", "old-file"}

        with mock.patch("agent_core.google_auth.get_service", return_value=mock.MagicMock()), \
             mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch.object(drive_sync, "_list_folder", return_value=(files, False)), \
             mock.patch.object(drive_sync, "sync_file",
                               return_value={"file_id": "child-file", "chunks": 1}):
            result = drive_sync.sync_folder("plain-folder-id")

        self.assertEqual(result["purged"], 0)
        self.assertFalse(result["listing_complete"])
        store.delete_by_doc_id.assert_not_called()
        store.bulk_delete_by_doc_ids.assert_not_called()


class SyncAllDrivesPurgeGuardTests(unittest.TestCase):
    """sync_all_drives 的 purge 必須走 _purge_absent_docs（比例閘）— 2026-06-28
    倉庫誤刪 700 檔事故的同型入口之前在這裡是裸刪。"""

    def _run(self, indexed_ids, files, listing_complete=True):
        from agent_core.ingest import drive_sync
        store = mock.MagicMock()
        store.list_doc_ids.return_value = indexed_ids
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch.object(drive_sync, "_list_all_files",
                               return_value=(files, listing_complete)), \
             mock.patch("agent_core.google_auth.get_service", return_value=mock.MagicMock()), \
             mock.patch.object(drive_sync, "sync_file",
                               side_effect=lambda fid, **kw: {"file_id": fid, "chunks": 1}):
            result = drive_sync.sync_all_drives()
        return result, store

    def test_suspicious_mass_purge_is_skipped(self):
        from agent_core.ingest import drive_sync
        # 1000 indexed、Drive 短回 100 → 要刪 900（>50% 且 ≥50）→ 比例閘擋下。
        files = [{"id": f"f{i}", "name": f"n{i}", "mimeType": "text/plain",
                  "modifiedTime": "t"} for i in range(100)]
        indexed = {f"f{i}" for i in range(100)} | {f"stale{i}" for i in range(900)}
        with mock.patch.object(drive_sync, "_PURGE_MAX_FRACTION", 0.5), \
             mock.patch.object(drive_sync, "_PURGE_MIN_ABS", 50):
            result, store = self._run(indexed, files)
        self.assertEqual(result["purged"], 0)
        store.delete_by_doc_id.assert_not_called()
        store.bulk_delete_by_doc_ids.assert_not_called()

    def test_normal_small_purge_still_deletes(self):
        files = [{"id": "fa", "name": "A", "mimeType": "text/plain", "modifiedTime": "t"}]
        result, store = self._run({"fa", "old"}, files)
        self.assertEqual(result["purged"], 1)

    def test_incomplete_listing_skips_purge_entirely(self):
        files = [{"id": "fa", "name": "A", "mimeType": "text/plain", "modifiedTime": "t"}]
        result, store = self._run({"fa", "old"}, files, listing_complete=False)
        self.assertEqual(result["purged"], 0)
        store.list_doc_ids.assert_not_called()


class RagRunnerDispatchTests(unittest.TestCase):
    """Confirm rag_runner picks sync_shared_drive vs sync_folder by ID shape."""

    def test_dispatches_by_id_shape(self):
        from agent_core.ingest import rag_runner, drive_sync
        import agent_core.logging_and_paths as paths

        targets = {
            "all_drives":       False,
            "drive_folder_ids": [
                "0AF1sBHRGfyhkUk9PVA",                         # shared drive
                "1Kf8X2Rc481N6e2RzCwHDaif-mLfLtzz5W3lJTyBHsu8", # plain folder
            ],
            "gmail_query":       "",
            "gmail_max_threads": 200,
        }

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(paths, "STATE_DIR", tmpdir), \
             mock.patch("agent_core.ingest.sync_config.load_targets", return_value=targets), \
             mock.patch.object(drive_sync, "sync_shared_drive",
                               return_value={"total": 1, "synced": 1, "skipped": 0,
                                             "purged": 0, "listing_complete": True}) as sd_mock, \
             mock.patch.object(drive_sync, "sync_folder",
                               return_value={"total": 1, "synced": 1, "skipped": 0,
                                             "purged": 0, "details": []}) as sf_mock:
            rag_runner.run_sync()

        sd_mock.assert_called_once_with("0AF1sBHRGfyhkUk9PVA")
        sf_mock.assert_called_once_with(
            "1Kf8X2Rc481N6e2RzCwHDaif-mLfLtzz5W3lJTyBHsu8",
            recursive=False,
        )

    def test_dispatches_recursive_plain_folder_from_config(self):
        from agent_core.ingest import rag_runner, drive_sync
        import agent_core.logging_and_paths as paths

        targets = {
            "all_drives": False,
            "drive_folder_ids": ["plain-folder-id"],
            "recursive_folder_ids": ["plain-folder-id"],
            "gmail_query": "",
            "gmail_max_threads": 200,
        }

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(paths, "STATE_DIR", tmpdir), \
             mock.patch("agent_core.ingest.sync_config.load_targets", return_value=targets), \
             mock.patch.object(drive_sync, "sync_folder",
                               return_value={"total": 1, "synced": 1, "skipped": 0,
                                             "purged": 0, "details": []}) as sf_mock:
            rag_runner.run_sync()

        sf_mock.assert_called_once_with("plain-folder-id", recursive=True)


if __name__ == "__main__":
    unittest.main()
