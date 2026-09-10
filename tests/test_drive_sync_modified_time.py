"""Tests for modifiedTime-based fast-skip in drive_sync.

Without this gate, every daily run re-downloads + re-embeds every file in the
index — that hits Gemini's quota wall on a 20k-file index. The gate skips
files whose stored modified_time EQUALS the listing's modifiedTime. Equality
(not the old `synced_at >= modifiedTime`) matters: an edit landing between the
download and the synced_at stamp made the >=-watermark skip that edit forever.
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


# ── _stored_modified_time_matches ───────────────────────────────────

class StoredModifiedTimeMatchesTests(unittest.TestCase):
    def test_identical_strings_match(self):
        from agent_core.ingest import drive_sync
        self.assertTrue(drive_sync._stored_modified_time_matches(
            "2026-05-07T08:00:00.000Z",
            "2026-05-07T08:00:00.000Z",
        ))

    def test_newer_listing_time_does_not_match(self):
        from agent_core.ingest import drive_sync
        self.assertFalse(drive_sync._stored_modified_time_matches(
            "2026-05-07T08:00:00.000Z",
            "2026-05-07T09:00:00.000Z",
        ))

    def test_older_listing_time_does_not_match(self):
        """Equality, not >= — a stored watermark 'fresher' than the listing is
        still a mismatch and must trigger a re-check (content-hash gate keeps
        the re-embed away)."""
        from agent_core.ingest import drive_sync
        self.assertFalse(drive_sync._stored_modified_time_matches(
            "2026-05-07T09:00:00.000Z",
            "2026-05-07T08:00:00.000Z",
        ))

    def test_empty_stored_returns_false(self):
        # Legacy chunks without the field must take one full pass to stamp it.
        from agent_core.ingest import drive_sync
        self.assertFalse(drive_sync._stored_modified_time_matches(
            "", "2026-05-07T09:00:00.000Z"))

    def test_empty_listing_hint_returns_false(self):
        from agent_core.ingest import drive_sync
        self.assertFalse(drive_sync._stored_modified_time_matches(
            "2026-05-07T09:00:00.000Z", ""))

    def test_handles_z_vs_plus_zero_zero_at_same_instant(self):
        """Z vs +00:00 suffix — same instant, different spelling → match."""
        from agent_core.ingest import drive_sync
        self.assertTrue(drive_sync._stored_modified_time_matches(
            "2026-05-07T09:00:00+00:00",
            "2026-05-07T09:00:00Z",
        ))

    def test_garbage_input_returns_false(self):
        from agent_core.ingest import drive_sync
        self.assertFalse(drive_sync._stored_modified_time_matches(
            "not-a-timestamp", "also-bad"))


# ── _stored_metadata_matches helper ─────────────────────────────────

class StoredMetadataMatchesTests(unittest.TestCase):
    @staticmethod
    def _existing(folder="", drive=""):
        return {"synced_at": "2026-05-07T09:00:00+00:00",
                "folder_id": folder, "drive_id": drive}

    def test_both_match_returns_true(self):
        from agent_core.ingest import drive_sync
        e = self._existing(folder="f1", drive="0AX")
        self.assertTrue(drive_sync._stored_metadata_matches(e, "f1", "0AX"))

    def test_drive_id_missing_in_store_returns_false(self):
        from agent_core.ingest import drive_sync
        e = self._existing(folder="f1", drive="")
        self.assertFalse(drive_sync._stored_metadata_matches(e, "f1", "0AX"))

    def test_folder_id_changed_returns_false(self):
        from agent_core.ingest import drive_sync
        e = self._existing(folder="old_sub", drive="0AX")
        self.assertFalse(drive_sync._stored_metadata_matches(e, "new_sub", "0AX"))

    def test_empty_caller_hints_treated_as_dont_care(self):
        from agent_core.ingest import drive_sync
        e = self._existing(folder="anything", drive="anything")
        self.assertTrue(drive_sync._stored_metadata_matches(e, "", ""))

    def test_only_drive_id_hint_ignores_folder(self):
        from agent_core.ingest import drive_sync
        e = self._existing(folder="legacy_folder", drive="0AX")
        # Caller cares about drive_id, not folder_id → folder mismatch is fine.
        self.assertTrue(drive_sync._stored_metadata_matches(e, "", "0AX"))


# ── sync_file fast-skip path ────────────────────────────────────────

class SyncFileFastSkipTests(unittest.TestCase):
    """When up-to-date, sync_file must NOT call get_service or any Drive API.
    That's the whole point of the gate — zero cost on unchanged files."""

    def setUp(self):
        from agent_core.ingest import drive_sync

        self._skip_state_tmp = tempfile.TemporaryDirectory()
        self._skip_state_patch = mock.patch.object(
            drive_sync,
            "_SKIP_STATE_FILE",
            os.path.join(self._skip_state_tmp.name, "drive_sync_skip_state.json"),
        )
        self._skip_state_patch.start()
        drive_sync._SKIP_STATE_CACHE = None

    def tearDown(self):
        from agent_core.ingest import drive_sync

        drive_sync._SKIP_STATE_CACHE = None
        self._skip_state_patch.stop()
        self._skip_state_tmp.cleanup()

    @staticmethod
    def _store(synced_at="", folder_id="", drive_id="",
               content_hash="HASHv1xxxxxx", title="", modified_time=""):
        # content_hash defaults to a non-empty marker so existing tests still
        # exercise the fast-skip path; tests that need the migration scenario
        # pass content_hash="" explicitly.
        #
        # access fields populated from rag_gateway so the fast-skip path's
        # metadata_access_matches() check sees them in the existing-chunk
        # metadata. Without these, every fast-skip test would think the access
        # config changed and re-sync, defeating the test's purpose.
        from agent_core.rag_gateway import metadata_access_fields
        store = mock.MagicMock()
        meta = {
            "synced_at": synced_at, "folder_id": folder_id,
            "drive_id": drive_id, "content_hash": content_hash,
            "title": title, "sync_complete": True,
            "modified_time": modified_time,
        }
        meta.update(metadata_access_fields("drive", file_id="fid",
                                            folder_id=folder_id,
                                            drive_id=drive_id))
        store.get_doc_metadata.return_value = meta
        return store

    @staticmethod
    def _service_with_unsupported_mime(parents):
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid", "name": "x.bin",
            "mimeType": "application/x-not-supported", "parents": parents,
        }
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        service = mock.MagicMock()
        service.files.return_value = files_obj
        return service, files_obj

    def test_unchanged_file_skips_without_calling_drive_or_gemini(self):
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2026-05-07T09:00:00+00:00",
            folder_id="folder1",
            modified_time="2026-05-06T08:00:00.000Z",  # == listing hint
        )
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service") as gs_mock:
            r = drive_sync.sync_file(
                "fid",
                folder_id="folder1",
                modified_time="2026-05-06T08:00:00.000Z",
            )
        self.assertEqual(r, {"file_id": "fid", "skipped": True, "reason": "unchanged"})
        # Crucial: Drive API was never built — no network, no quota burn.
        gs_mock.assert_not_called()
        # And the consolidated metadata fetch ran exactly once (one Chroma
        # round-trip, not three).
        self.assertEqual(store.get_doc_metadata.call_count, 1)
        # No metadata write on a clean skip — 50k no-op updates per night is
        # the failure mode.
        store.set_doc_metadata_fields.assert_not_called()

    def test_missing_stored_modified_time_blocks_fast_skip(self):
        """Legacy chunks without a stored modified_time must NOT fast-skip on
        synced_at freshness: the synced_at watermark hides edits that land
        between the download and the stamp. They take one full pass (the
        content-hash gate prevents a re-embed) which stamps the field."""
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2026-05-07T09:00:00+00:00",  # newer than the hint
            folder_id="folder1",
        )  # modified_time defaults to "" (legacy)
        service, files_obj = self._service_with_unsupported_mime(parents=["folder1"])
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="folder1",
                modified_time="2026-05-06T08:00:00.000Z",
            )
        self.assertNotEqual(r.get("reason"), "unchanged")
        files_obj.get.assert_called_once()  # full path WAS taken

    def test_stale_stored_modified_time_blocks_fast_skip(self):
        """THE watermark bug: file edited between the download and the
        synced_at stamp → stored modified_time is older than the listing's,
        yet synced_at (stamped after) looks fresher. The old >=-gate skipped
        this edit forever; the equality gate must fall through."""
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2026-05-07T09:00:00+00:00",   # newer than the hint...
            folder_id="folder1",
            modified_time="2026-05-06T07:00:00.000Z",  # ...but stale watermark
        )
        service, files_obj = self._service_with_unsupported_mime(parents=["folder1"])
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="folder1",
                modified_time="2026-05-06T08:00:00.000Z",
            )
        self.assertNotEqual(r.get("reason"), "unchanged")
        files_obj.get.assert_called_once()  # full path WAS taken

    def test_modified_file_proceeds_with_full_sync(self):
        from agent_core.ingest import drive_sync
        store = self._store(synced_at="2026-05-06T09:00:00+00:00")
        service, files_obj = self._service_with_unsupported_mime(parents=["p1"])
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="p1",
                modified_time="2026-05-07T09:00:00.000Z",
            )
        self.assertEqual(r["reason"], "unsupported mime_type")
        files_obj.get.assert_called_once()  # full path WAS taken

    def test_export_timeout_preserves_existing_chunks(self):
        from agent_core.ingest import drive_sync
        store = self._store(synced_at="2026-05-06T09:00:00+00:00")
        service, files_obj = self._service_with_unsupported_mime(parents=["p1"])
        files_obj.get.return_value.execute.return_value = {
            "id": "fid",
            "name": "slow.pdf",
            "mimeType": "application/pdf",
            "parents": ["p1"],
        }

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_export_file_text_for_sync", side_effect=TimeoutError("slow export")):
            r = drive_sync.sync_file(
                "fid",
                folder_id="p1",
                modified_time="2026-05-07T09:00:00.000Z",
            )

        self.assertTrue(r["skipped"])
        self.assertIn("timeout", r["reason"])
        files_obj.get.assert_called_once()
        store.delete_by_doc_id.assert_not_called()
        store.upsert_batch.assert_not_called()

    def test_large_binary_file_is_skipped_without_download(self):
        from agent_core.ingest import drive_sync
        store = self._store(synced_at="2026-05-06T09:00:00+00:00")
        service, files_obj = self._service_with_unsupported_mime(parents=["p1"])
        files_obj.get.return_value.execute.return_value = {
            "id": "fid",
            "name": "huge.pdf",
            "mimeType": "application/pdf",
            "parents": ["p1"],
            "size": "99",
        }

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_DRIVE_MAX_DOWNLOAD_BYTES", 10), \
             mock.patch.object(drive_sync, "_export_file_text") as export_mock:
            r = drive_sync.sync_file(
                "fid",
                folder_id="p1",
                modified_time="2026-05-07T09:00:00.000Z",
            )

        self.assertTrue(r["skipped"])
        self.assertIn("too_large", r["reason"])
        export_mock.assert_not_called()
        store.delete_by_doc_id.assert_not_called()
        store.upsert_batch.assert_not_called()

    def test_large_xlsx_uses_spreadsheet_specific_download_limit(self):
        from agent_core.ingest import drive_sync
        store = self._store(synced_at="2026-05-06T09:00:00+00:00")
        service, files_obj = self._service_with_unsupported_mime(parents=["p1"])
        files_obj.get.return_value.execute.return_value = {
            "id": "fid",
            "name": "Danh sách nhân sự 1.xlsx",
            "mimeType": drive_sync._XLSX_MIME,
            "parents": ["p1"],
            "modifiedTime": "2026-05-07T09:00:00.000Z",
            "size": str(75 * 1024 * 1024),
        }

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_DRIVE_MAX_DOWNLOAD_BYTES", 50 * 1024 * 1024), \
             mock.patch.object(drive_sync, "_DRIVE_SPREADSHEET_MAX_DOWNLOAD_BYTES", 100 * 1024 * 1024), \
             mock.patch.object(drive_sync, "_export_file_text_for_sync", return_value="name\tdepartment") as export_mock:
            r = drive_sync.sync_file(
                "fid",
                folder_id="p1",
                modified_time="2026-05-07T09:00:00.000Z",
            )

        self.assertEqual(r["chunks"], 1)
        export_mock.assert_called_once()
        store.upsert_batch.assert_called_once()

    def test_default_spreadsheet_download_limit_covers_large_workbooks(self):
        from agent_core.ingest import drive_sync

        self.assertGreaterEqual(
            drive_sync._DRIVE_SPREADSHEET_MAX_DOWNLOAD_BYTES,
            350 * 1024 * 1024,
        )

    def test_drive_id_mismatch_blocks_fast_skip(self):
        """Migration scenario flagged by code review: a file previously indexed
        via sync_all_drives or the old sync_folder path has no drive_id stored.
        sync_shared_drive must NOT fast-skip it, otherwise list_doc_ids_by_drive
        could never find it and per-drive purge would leak stale chunks."""
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2026-05-07T09:00:00+00:00",
            folder_id="subfolder1",
            drive_id="",  # legacy: no drive_id stamped
            modified_time="2026-05-06T08:00:00.000Z",  # matches — mismatch decides
        )
        service, files_obj = self._service_with_unsupported_mime(parents=["subfolder1"])
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="subfolder1",
                drive_id="0AABCDEF",  # caller wants this stamped
                modified_time="2026-05-06T08:00:00.000Z",
            )
        files_obj.get.assert_called_once()
        self.assertNotEqual(r.get("reason"), "unchanged")

    def test_folder_id_mismatch_blocks_fast_skip(self):
        """File moved between subfolders: stored folder_id is stale. Fast-skip
        would leave the index pointing at the old subfolder, breaking the
        per-folder purge contract."""
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2026-05-07T09:00:00+00:00",
            folder_id="old_subfolder",
            drive_id="0AABCDEF",
            modified_time="2026-05-06T08:00:00.000Z",  # matches — mismatch decides
        )
        service, files_obj = self._service_with_unsupported_mime(parents=["new_subfolder"])
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="new_subfolder",
                drive_id="0AABCDEF",
                modified_time="2026-05-06T08:00:00.000Z",
            )
        files_obj.get.assert_called_once()
        self.assertNotEqual(r.get("reason"), "unchanged")

    def test_metadata_matches_with_no_caller_hints_still_fast_skips(self):
        """Direct sync_file('fid', modified_time=...) with no folder/drive
        context (caller doesn't care) must still fast-skip on freshness alone
        — empty hints shouldn't block the gate."""
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2026-05-07T09:00:00+00:00",
            folder_id="anything", drive_id="anything",
            modified_time="2026-05-06T08:00:00.000Z",
        )
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service") as gs_mock:
            r = drive_sync.sync_file(
                "fid",
                modified_time="2026-05-06T08:00:00.000Z",
            )
        self.assertEqual(r.get("reason"), "unchanged")
        gs_mock.assert_not_called()

    def test_legacy_doc_without_content_hash_falls_through(self):
        """Codex finding on PR #26: docs indexed before R2/R3 have no
        content_hash and no [title] prefix in their chunk text. Their
        synced_at is also newer than Drive's modifiedTime (file hasn't
        changed since), so the time gate would fast-skip them and the
        title-prefix lift would never reach existing production indexes.

        Treat the missing content_hash as a migration marker — fall through
        to one full re-embed, which backfills both the prefix and the hash."""
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2026-05-07T09:00:00+00:00",  # newer than modifiedTime
            folder_id="sub1",
            drive_id="0AABCDEF",
            content_hash="",  # legacy: predates R3
            title="legacy.docx",
            modified_time="2026-05-06T08:00:00.000Z",  # matches — hash decides
        )
        service, files_obj = self._service_with_unsupported_mime(parents=["sub1"])
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-06T08:00:00.000Z",  # older than synced_at
            )
        # Time gate would normally fast-skip; the migration marker prevents it.
        files_obj.get.assert_called_once()
        self.assertNotEqual(r.get("reason"), "unchanged")

    def test_no_modified_time_hint_proceeds_with_full_sync(self):
        """Direct sync_file('fid') from the white_legal command has no hint
        and must NOT fast-skip — user explicitly asked for re-sync."""
        from agent_core.ingest import drive_sync
        store = self._store(
            synced_at="2099-01-01T00:00:00+00:00",  # very fresh
            folder_id="p1",
        )
        service, files_obj = self._service_with_unsupported_mime(parents=["p1"])
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            drive_sync.sync_file("fid")  # no modified_time
        files_obj.get.assert_called_once()  # full path WAS taken


# ── caller threads modifiedTime through ─────────────────────────────

class CallerThreadsModifiedTimeTests(unittest.TestCase):
    def test_sync_shared_drive_passes_modified_time(self):
        from agent_core.ingest import drive_sync
        files = [
            {"id": "fa", "name": "A.pdf", "mimeType": "application/pdf",
             "parents": ["sub1"], "modifiedTime": "2026-05-01T10:00:00.000Z"},
            {"id": "fb", "name": "B.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "parents": ["sub2"], "modifiedTime": "2026-05-02T11:00:00.000Z"},
        ]
        captured: list[dict] = []
        def fake_sync_file(file_id, folder_id="", drive_id="", modified_time=""):
            captured.append({
                "file_id": file_id, "folder_id": folder_id,
                "drive_id": drive_id, "modified_time": modified_time,
            })
            return {"file_id": file_id, "chunks": 1}

        store = mock.MagicMock()
        store.list_doc_ids_by_drive.return_value = set()
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch.object(drive_sync, "_list_drive_files", return_value=(files, True)), \
             mock.patch("agent_core.google_auth.get_service", return_value=mock.MagicMock()), \
             mock.patch.object(drive_sync, "sync_file", side_effect=fake_sync_file):
            drive_sync.sync_shared_drive("0AABCDEF")

        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["modified_time"], "2026-05-01T10:00:00.000Z")
        self.assertEqual(captured[1]["modified_time"], "2026-05-02T11:00:00.000Z")
        # Drive ID + folder ID still threaded through correctly.
        self.assertEqual(captured[0]["drive_id"], "0AABCDEF")
        self.assertEqual(captured[0]["folder_id"], "sub1")

    def test_sync_folder_passes_modified_time(self):
        from agent_core.ingest import drive_sync
        files = [
            {"id": "fa", "name": "A.txt", "mimeType": "text/plain",
             "modifiedTime": "2026-04-15T08:00:00.000Z"},
        ]
        captured: list[dict] = []
        def fake_sync_file(file_id, folder_id="", drive_id="", modified_time=""):
            captured.append({"file_id": file_id, "modified_time": modified_time})
            return {"file_id": file_id, "chunks": 1}

        store = mock.MagicMock()
        store.list_doc_ids_by_folder.return_value = set()
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch.object(drive_sync, "_list_folder", return_value=(files, True)), \
             mock.patch("agent_core.google_auth.get_service", return_value=mock.MagicMock()), \
             mock.patch.object(drive_sync, "sync_file", side_effect=fake_sync_file):
            drive_sync.sync_folder("plain-folder-id")

        self.assertEqual(captured[0]["modified_time"], "2026-04-15T08:00:00.000Z")


# ── vector_store.get_doc_metadata ───────────────────────────────────

class GetDocMetadataTests(unittest.TestCase):
    @staticmethod
    def _store_with_metas(metas):
        from agent_core.ingest import vector_store
        # Bypass __init__ — directly stub the chroma collection.
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        col = mock.MagicMock()
        col.get.return_value = {"metadatas": metas}
        store._col = col
        return store, col

    def test_synced_at_takes_latest_across_chunks(self):
        store, _ = self._store_with_metas([
            {"synced_at": "2026-05-01T10:00:00+00:00", "folder_id": "f1", "drive_id": "0AX"},
            {"synced_at": "2026-05-03T10:00:00+00:00", "folder_id": "f1", "drive_id": "0AX"},  # latest
            {"synced_at": "2026-05-02T10:00:00+00:00", "folder_id": "f1", "drive_id": "0AX"},
        ])
        meta = store.get_doc_metadata("doc1")
        self.assertEqual(meta["synced_at"], "2026-05-03T10:00:00+00:00")
        self.assertEqual(meta["folder_id"], "f1")
        self.assertEqual(meta["drive_id"], "0AX")

    def test_no_chunks_returns_empty_strings(self):
        store, _ = self._store_with_metas([])
        meta = store.get_doc_metadata("doc1")
        self.assertEqual(
            meta,
            {"synced_at": "", "folder_id": "", "drive_id": "",
             "content_hash": "", "title": "", "history_id": "",
             "subject": "", "date": "", "modified_time": "",
             "sync_complete": True},
        )

    def test_sync_complete_false_if_any_chunk_incomplete(self):
        store, _ = self._store_with_metas([
            {"synced_at": "2026-05-01T10:00:00+00:00", "content_hash": "h", "sync_complete": True},
            {"synced_at": "2026-05-01T10:00:00+00:00", "content_hash": "h", "sync_complete": False},
        ])
        meta = store.get_doc_metadata("doc1")
        self.assertFalse(meta["sync_complete"])

    def test_single_round_trip_to_chroma(self):
        """Fast-skip path budget assumes ONE _col.get() per file. Pin it."""
        store, col = self._store_with_metas([
            {"synced_at": "2026-05-01T10:00:00+00:00", "folder_id": "f", "drive_id": "0A"}
        ])
        store.get_doc_metadata("doc1")
        self.assertEqual(col.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
