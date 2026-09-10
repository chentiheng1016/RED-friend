"""Permanent Drive extraction failures get a skip marker so the daemon stops
re-downloading + re-extracting them on every run.

Before this, an encrypted PDF (pypdf FileNotDecryptedError) or a corrupt /
truncated PDF (PdfStreamError) surfaced as a retryable `error` with no marker —
so each daily sync re-downloaded and re-parsed the same ~800 unprocessable
files forever. These failures are deterministic in the file's bytes, so we mark
them (keyed on modifiedTime) and skip before download next time; a re-uploaded
(e.g. decrypted) copy bumps modifiedTime and is retried. Transient failures
(network/API) must still bubble up as a retryable `error`.
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


ENCRYPTED_MSG = (
    "Drive file text fid failed: pypdf.errors.FileNotDecryptedError: "
    "File has not been decrypted"
)


# ── classifier ──────────────────────────────────────────────────────

class PermanentExtractErrorClassifierTests(unittest.TestCase):
    def test_encrypted_pdf_is_permanent(self):
        from agent_core.ingest import drive_sync
        exc = RuntimeError(ENCRYPTED_MSG)
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "FileNotDecryptedError",
        )

    def test_corrupt_and_bomb_signatures_are_permanent(self):
        from agent_core.ingest import drive_sync
        for sig in ("PdfStreamError", "PdfReadError", "LimitReachedError",
                    "BadZipFile", "EmptyFileError"):
            exc = RuntimeError(f"Drive file text fid failed: pypdf.errors.{sig}: boom")
            self.assertTrue(drive_sync._is_permanent_extract_error(exc), sig)
            self.assertEqual(drive_sync._permanent_extract_error_signature(exc), sig)

    def test_encrypted_xls_is_permanent(self):
        # xlrd.biffh.XLRDError("Workbook is encrypted") — xlrd can never
        # decrypt, so the extract pool surfaces the same failure every night.
        from agent_core.ingest import drive_sync
        exc = RuntimeError(
            "Drive file text fid failed: xlrd.biffh.XLRDError: Workbook is encrypted"
        )
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "Workbook is encrypted",
        )

    def test_non_standard_pdf_encryption_is_permanent(self):
        # pypdf NotImplementedError for non-Standard security handlers.
        from agent_core.ingest import drive_sync
        exc = RuntimeError(
            "Drive file text fid failed: NotImplementedError: "
            "only Standard PDF encryption handler is available"
        )
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "only Standard PDF encryption handler is available",
        )

    def test_openpyxl_invalid_file_is_permanent(self):
        # openpyxl.utils.exceptions.InvalidFileException — bytes aren't an
        # OOXML zip at all (misnamed legacy .xls / .xlsb / corrupt container).
        from agent_core.ingest import drive_sync
        exc = RuntimeError(
            "Drive file text fid failed: "
            "openpyxl.utils.exceptions.InvalidFileException: openpyxl does not "
            "support the old .xls file format"
        )
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "InvalidFileException",
        )

    def test_xls_without_workbook_stream_is_permanent(self):
        # xlrd.biffh.XLRDError for an OLE2 container with no Workbook stream
        # (Office-encrypted .xls / non-Excel OLE2 misnamed .xls).
        from agent_core.ingest import drive_sync
        exc = RuntimeError(
            "Drive file text fid failed: xlrd.biffh.XLRDError: "
            "Can't find workbook in OLE2 compound document"
        )
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "Can't find workbook in OLE2 compound document",
        )

    def test_xlsx_bytes_misnamed_xls_is_permanent(self):
        # xlrd.biffh.XLRDError — xlrd ≥ 2.0 refuses OOXML bytes outright.
        from agent_core.ingest import drive_sync
        exc = RuntimeError(
            "Drive file text fid failed: xlrd.biffh.XLRDError: "
            "Excel xlsx file; not supported"
        )
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "Excel xlsx file; not supported",
        )

    def test_openpyxl_invalid_xml_last_line_is_permanent(self):
        # openpyxl wraps invalid-XML parse failures in a three-line
        # ValueError; the extract pool keeps only the LAST traceback line, so
        # the surfaced message carries neither class name nor first line.
        from agent_core.ingest import drive_sync
        exc = RuntimeError(
            "Drive file text fid failed: "
            "Please see the exception for more details."
        )
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "Please see the exception for more details.",
        )

    def test_openpyxl_corrupt_style_range_is_permanent(self):
        # openpyxl descriptor validation TypeError on corrupt style ranges —
        # class name is a bare "TypeError", so the message is the signature.
        from agent_core.ingest import drive_sync
        exc = RuntimeError(
            "Drive file text fid failed: TypeError: "
            "expected <class 'openpyxl.worksheet.cell_range.MultiCellRange'>"
        )
        self.assertTrue(drive_sync._is_permanent_extract_error(exc))
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(exc),
            "expected <class 'openpyxl.worksheet.cell_range.MultiCellRange'>",
        )

    def test_transient_errors_are_not_permanent(self):
        from agent_core.ingest import drive_sync
        for msg in (
            "Drive file text fid failed: ConnectionError: timed out",
            "503 Service Unavailable",
            "BrokenPipeError: [Errno 32] Broken pipe",
            # A non-openpyxl TypeError must stay retryable — the corrupt-style
            # signature is pinned to the full MultiCellRange message.
            "Drive file text fid failed: TypeError: expected <class 'int'>",
        ):
            self.assertFalse(
                drive_sync._is_permanent_extract_error(RuntimeError(msg)), msg
            )
        # Unknown exception → signature falls back to the class name.
        self.assertEqual(
            drive_sync._permanent_extract_error_signature(ValueError("weird")),
            "ValueError",
        )


# ── _skip_marker_matches honors extract_error markers ───────────────

class SkipMarkerMatchesExtractErrorTests(unittest.TestCase):
    @staticmethod
    def _marker():
        return {
            "reason": "extract_error: FileNotDecryptedError",
            "modified_time": "2026-05-07T09:00:00.000Z",
            "folder_id": "f1", "drive_id": "0AX",
        }

    def test_matches_when_modified_time_unchanged(self):
        from agent_core.ingest import drive_sync
        self.assertTrue(drive_sync._skip_marker_matches(
            self._marker(), "2026-05-07T09:00:00.000Z", "f1", "0AX"))

    def test_rejected_when_modified_time_changed(self):
        from agent_core.ingest import drive_sync
        # File re-uploaded (e.g. decrypted) → newer modifiedTime → must retry.
        self.assertFalse(drive_sync._skip_marker_matches(
            self._marker(), "2026-06-01T10:00:00.000Z", "f1", "0AX"))

    def test_rejected_without_modified_time(self):
        from agent_core.ingest import drive_sync
        self.assertFalse(drive_sync._skip_marker_matches(
            self._marker(), "", "f1", "0AX"))


# ── sync_file integration ───────────────────────────────────────────

class SyncFileExtractErrorTests(unittest.TestCase):
    def setUp(self):
        from agent_core.ingest import drive_sync
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            drive_sync, "_SKIP_STATE_FILE",
            os.path.join(self._tmp.name, "drive_sync_skip_state.json"),
        )
        self._patch.start()
        drive_sync._SKIP_STATE_CACHE = None

    def tearDown(self):
        from agent_core.ingest import drive_sync
        drive_sync._SKIP_STATE_CACHE = None
        self._patch.stop()
        self._tmp.cleanup()

    @staticmethod
    def _store():
        # synced_at older than the file's modifiedTime so the freshness gate
        # does NOT fast-skip — sync_file must proceed to extraction.
        from agent_core.rag_gateway import metadata_access_fields
        store = mock.MagicMock()
        meta = {
            "synced_at": "2026-05-06T09:00:00+00:00",
            "folder_id": "p1", "drive_id": "0AX",
            "content_hash": "HASHv1xxxxxx", "title": "", "sync_complete": True,
        }
        meta.update(metadata_access_fields(
            "drive", file_id="fid", folder_id="p1", drive_id="0AX"))
        store.get_doc_metadata.return_value = meta
        return store

    @staticmethod
    def _pdf_service():
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid", "name": "secret.pdf", "mimeType": "application/pdf",
            "parents": ["p1"], "modifiedTime": "2026-05-07T09:00:00.000Z",
        }
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        service = mock.MagicMock()
        service.files.return_value = files_obj
        return service, files_obj

    def test_encrypted_pdf_records_marker_and_skips(self):
        from agent_core.ingest import drive_sync
        store = self._store()
        service, _ = self._pdf_service()
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_export_file_text_for_sync",
                               side_effect=RuntimeError(ENCRYPTED_MSG)):
            r = drive_sync.sync_file(
                "fid", folder_id="p1", drive_id="0AX",
                modified_time="2026-05-07T09:00:00.000Z",
            )
        self.assertTrue(r["skipped"])
        self.assertEqual(r["reason"], "extract_error: FileNotDecryptedError")
        store.upsert_batch.assert_not_called()  # no chunks written
        marker = drive_sync._get_matching_skip_marker(
            "fid", modified_time="2026-05-07T09:00:00.000Z",
            folder_id="p1", drive_id="0AX")
        self.assertIsNotNone(marker)
        self.assertEqual(marker["reason"], "extract_error: FileNotDecryptedError")

    def test_second_run_fast_skips_without_drive_or_extract(self):
        """The whole point: once marked, the next sync must NOT touch Drive
        or re-extract — it short-circuits on the marker before download."""
        from agent_core.ingest import drive_sync
        store = self._store()
        service, _ = self._pdf_service()
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_export_file_text_for_sync",
                               side_effect=RuntimeError(ENCRYPTED_MSG)):
            drive_sync.sync_file("fid", folder_id="p1", drive_id="0AX",
                                 modified_time="2026-05-07T09:00:00.000Z")

        export_mock = mock.MagicMock()
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service") as gs2, \
             mock.patch.object(drive_sync, "_export_file_text_for_sync", export_mock):
            r = drive_sync.sync_file("fid", folder_id="p1", drive_id="0AX",
                                     modified_time="2026-05-07T09:00:00.000Z")
        self.assertTrue(r["skipped"])
        self.assertEqual(r["reason"], "extract_error: FileNotDecryptedError")
        gs2.assert_not_called()          # Drive API never built
        export_mock.assert_not_called()  # no re-extraction

    def test_transient_error_reraises_and_records_no_marker(self):
        from agent_core.ingest import drive_sync
        store = self._store()
        service, _ = self._pdf_service()
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_export_file_text_for_sync",
                               side_effect=RuntimeError("ConnectionError: timed out")):
            with self.assertRaises(RuntimeError):
                drive_sync.sync_file("fid", folder_id="p1", drive_id="0AX",
                                     modified_time="2026-05-07T09:00:00.000Z")
        marker = drive_sync._get_matching_skip_marker(
            "fid", modified_time="2026-05-07T09:00:00.000Z",
            folder_id="p1", drive_id="0AX")
        self.assertIsNone(marker)  # transient → retried next run, not marked


if __name__ == "__main__":
    unittest.main()
