"""Tests for the content-hash gate in drive_sync.

Drive's modifiedTime bumps on metadata-only edits (share / permission /
label changes), not just content edits. Without this gate, every such bump
triggered a full re-embed of every chunk — pure waste. The gate compares
sha256 of the freshly-extracted text to the stored hash; matches refresh
synced_at without re-embedding.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _hash16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


class ContentHashGateTests(unittest.TestCase):
    """sync_file's second-line defense — runs AFTER text is fetched (so we
    saw a bump big enough to slip the modifiedTime gate) but BEFORE chunking
    + embedding. Matches refresh synced_at and exit early."""

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
    def _drive_service_for(text: str, mime: str = "text/plain"):
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid", "name": "x.txt",
            "mimeType": mime, "parents": ["sub1"],
        }
        media_req = mock.MagicMock()
        media_req.execute.return_value = text.encode("utf-8")
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        files_obj.get_media.return_value = media_req
        service = mock.MagicMock()
        service.files.return_value = files_obj
        return service

    def test_identical_content_skips_embed_and_refreshes_metadata(self):
        """Drive bumped modifiedTime (we got past the time gate), but the
        bytes are identical to last sync. We MUST NOT re-embed; we just
        refresh synced_at (so tomorrow's modifiedTime gate fires cleanly)
        and stamp the doc's modified_time alongside."""
        from agent_core.ingest import drive_sync

        body = "保固期 12 個月，自簽收日起計。"
        existing_hash = _hash16(body)

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            # synced_at < modifiedTime → time gate falls through.
            "synced_at": "2026-05-06T08:00:00+00:00",
            "folder_id": "sub1",
            "drive_id":  "0AABCDEF",
            "content_hash": existing_hash,  # but content is the same
            "title": "x.txt",  # matches Drive meta.name from _drive_service_for
        }
        service = self._drive_service_for(body)

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertEqual(r["reason"], "content_unchanged")
        self.assertTrue(r["skipped"])
        # Critical guarantees: NO re-embed, BUT synced_at was bumped so the
        # next run's time gate fast-skips without falling through here again.
        store.upsert_batch.assert_not_called()
        store.set_doc_metadata_fields.assert_called_once()
        called_with = store.set_doc_metadata_fields.call_args
        self.assertEqual(called_with.args[0], "fid")
        fields = called_with.args[1]
        # synced_at is the new (now) timestamp — the important property is
        # just that it's truthy and not the old one.
        self.assertTrue(fields["synced_at"])
        self.assertNotEqual(fields["synced_at"], "2026-05-06T08:00:00+00:00")
        # The doc's own date rides along so retrieval can rank by recency.
        self.assertEqual(fields["modified_time"], "2026-05-07T10:00:00.000Z")

    def test_identical_content_refreshes_access_fields_metadata_only(self):
        """改 rag_access.json 後：fast-skip 的 metadata_access_matches 不過 →
        重下載 → 內容未變走到這裡。refresh 必須把最新 access 欄位一併寫回
        （metadata-only、免重 embed），否則新 ACL 永不生效、明晚又重下載。"""
        from agent_core.ingest import drive_sync
        from agent_core.rag_gateway import metadata_access_fields

        body = "保固期 12 個月，自簽收日起計。"
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "2026-05-06T08:00:00+00:00",
            "folder_id": "sub1",
            "drive_id":  "0AABCDEF",
            "content_hash": _hash16(body),
            "title": "x.txt",
            # 故意帶「過時」的 ACL 欄位（與現行 rag_access 設定無關的值）。
            "access_red": False,
        }
        service = self._drive_service_for(body)

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertEqual(r["reason"], "content_unchanged")
        store.upsert_batch.assert_not_called()  # metadata-only,不重 embed
        fields = store.set_doc_metadata_fields.call_args.args[1]
        expected_access = metadata_access_fields(
            "drive", file_id="fid", folder_id="sub1", drive_id="0AABCDEF",
        )
        for key, value in expected_access.items():
            self.assertIn(key, fields)
            self.assertEqual(fields[key], value)

    def test_different_content_falls_through_to_full_embed(self):
        from agent_core.ingest import drive_sync

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at":    "2026-05-06T08:00:00+00:00",
            "folder_id":    "sub1",
            "drive_id":     "0AABCDEF",
            "content_hash": "STALEHASHxxxxxx",  # mismatch
            "title":        "x.txt",
        }
        service = self._drive_service_for("changed body text")

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertNotIn(r.get("reason", ""), {"content_unchanged", "unchanged"})
        store.upsert_batch.assert_called_once()
        store.set_doc_metadata_fields.assert_not_called()

    def test_hard_embedding_quota_skips_before_drive_content_download(self):
        from agent_core.ingest import drive_sync

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "2026-05-06T08:00:00+00:00",
            "folder_id": "sub1",
            "drive_id": "0AABCDEF",
            "content_hash": "STALEHASHxxxxxx",
            "title": "x.txt",
        }

        service = self._drive_service_for("body")
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch.object(
                drive_sync,
                "get_embedding_hard_quota_message",
                return_value="monthly spending cap",
             ), \
             mock.patch("agent_core.google_auth.get_service", return_value=service) as get_service:
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertTrue(r["skipped"])
        self.assertIn("embedding_unavailable", r["reason"])
        get_service.assert_called_once()
        get_service.return_value.files.return_value.get_media.assert_not_called()
        store.upsert_batch.assert_not_called()

    def test_large_file_chunk_count_is_capped_but_hash_uses_full_text(self):
        from agent_core.ingest import drive_sync

        body = "A" * (drive_sync.CHUNK_SIZE * 5)
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at":    "2026-05-06T08:00:00+00:00",
            "folder_id":    "sub1",
            "drive_id":     "0AABCDEF",
            "content_hash": "STALEHASHxxxxxx",
            "title":        "x.txt",
        }
        service = self._drive_service_for(body)

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_DRIVE_MAX_CHUNKS_PER_FILE", 2):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertEqual(r["chunks"], 2)
        self.assertTrue(r["truncated"])
        store.upsert_batch.assert_called_once()
        ids, docs, metas = store.upsert_batch.call_args.args
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(m["truncated"] for m in metas))
        self.assertTrue(all(m["source_chunk_count"] == r["source_chunks"] for m in metas))
        self.assertTrue(all(m["content_hash"] == _hash16(body) for m in metas))
        self.assertTrue(all(m["sync_complete"] is False for m in metas))
        # Doc date stamped on every chunk (from the listing's modifiedTime
        # when files().get didn't return one) — retrieval recency needs it.
        self.assertTrue(all(m["modified_time"] == "2026-05-07T10:00:00.000Z" for m in metas))
        store.delete_stale_chunks.assert_called_once_with("fid", 2)
        store.mark_doc_sync_complete.assert_called_once_with("fid")

    def test_incomplete_previous_upsert_blocks_modified_time_fast_skip(self):
        from agent_core.ingest import drive_sync

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "2026-05-07T09:00:00+00:00",
            "folder_id": "sub1",
            "drive_id": "0AABCDEF",
            "content_hash": "HASHv1xxxxxx",
            "title": "x.txt",
            "sync_complete": False,
        }
        service = self._drive_service_for("ignored")
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "fid", "name": "x.bin",
            "mimeType": "application/x-not-supported", "parents": ["sub1"],
        }

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-06T08:00:00.000Z",
            )

        self.assertEqual(r["reason"], "unsupported mime_type")
        service.files.return_value.get.assert_called_once()

    def test_incomplete_previous_upsert_blocks_content_hash_skip(self):
        from agent_core.ingest import drive_sync

        body = "same body"
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "2026-05-06T08:00:00+00:00",
            "folder_id": "sub1",
            "drive_id": "0AABCDEF",
            "content_hash": _hash16(body),
            "title": "x.txt",
            "sync_complete": False,
        }
        service = self._drive_service_for(body)

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertEqual(r.get("chunks"), 1)
        store.upsert_batch.assert_called_once()
        store.touch_synced_at.assert_not_called()
        store.mark_doc_sync_complete.assert_called_once_with("fid")

    def test_embedding_failure_does_not_mark_partial_upsert_complete(self):
        from agent_core.ingest import drive_sync
        from agent_core.ingest.vector_store import GeminiHardQuotaError

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "2026-05-06T08:00:00+00:00",
            "folder_id": "sub1",
            "drive_id": "0AABCDEF",
            "content_hash": "STALEHASHxxxxxx",
            "title": "x.txt",
        }
        store.upsert_batch.side_effect = GeminiHardQuotaError("monthly spending cap")
        service = self._drive_service_for("changed body text")

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertTrue(r["skipped"])
        self.assertIn("embedding_unavailable", r["reason"])
        store.delete_stale_chunks.assert_not_called()
        store.mark_doc_sync_complete.assert_not_called()
        metas = store.upsert_batch.call_args.args[2]
        self.assertTrue(all(m["sync_complete"] is False for m in metas))


    def test_legacy_doc_without_stored_hash_falls_through(self):
        """Doc indexed before the hash was added — existing.content_hash is
        empty. We must NOT mistake empty == empty as 'content unchanged' and
        skip; first re-sync rewrites the metadata with a hash for next time."""
        from agent_core.ingest import drive_sync

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at":    "2026-05-06T08:00:00+00:00",
            "folder_id":    "sub1",
            "drive_id":     "0AABCDEF",
            "content_hash": "",   # legacy
            "title":        "x.txt",
        }
        service = self._drive_service_for("any content")

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        # Full embed path taken; hash gets stamped on the new chunks.
        store.upsert_batch.assert_called_once()
        store.touch_synced_at.assert_not_called()
        self.assertEqual(r.get("chunks"), 1)

    def test_metadata_mismatch_blocks_hash_skip_too(self):
        """Even if content_hash matches, a folder_id/drive_id mismatch
        forces a full re-sync so the metadata is corrected. Otherwise the
        per-drive purge contract breaks for migrating chunks (codex finding
        from PR #24 — same logic must apply at this gate)."""
        from agent_core.ingest import drive_sync

        body = "same body"
        same_hash = _hash16(body)

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at":    "2026-05-06T08:00:00+00:00",
            "folder_id":    "sub1",
            "drive_id":     "",            # legacy: no drive_id stamped
            "content_hash": same_hash,
            "title":        "x.txt",
        }
        service = self._drive_service_for(body)

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",   # caller wants this stamped
                modified_time="2026-05-07T10:00:00.000Z",
            )

        # MUST do a full re-embed to write drive_id onto the chunks.
        store.upsert_batch.assert_called_once()
        store.touch_synced_at.assert_not_called()
        self.assertNotEqual(r.get("reason"), "content_unchanged")


    def test_rename_with_unchanged_content_forces_re_embed(self):
        """Codex finding on PR #26: R2 bakes [filename] into every chunk's
        text, so a rename without content change still demands a re-embed
        — otherwise indexed chunks keep the stale '[old-name]' prefix and
        title-prefixed retrieval breaks for the new filename."""
        from agent_core.ingest import drive_sync

        body = "保固期 12 個月"
        same_hash = _hash16(body)

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at":    "2026-05-06T08:00:00+00:00",
            "folder_id":    "sub1",
            "drive_id":     "0AABCDEF",
            "content_hash": same_hash,    # content identical
            "title":        "old-name.docx",  # but title changed
        }
        # Drive now serves the file under its new name.
        service = self._drive_service_for(body)
        service.files().get().execute.return_value = {
            "id": "fid", "name": "new-name.docx",  # ← renamed
            "mimeType": "text/plain", "parents": ["sub1"],
        }

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        # MUST do a full re-embed so chunk text + metadata.title both
        # reflect the new filename.
        store.upsert_batch.assert_called_once()
        store.touch_synced_at.assert_not_called()
        self.assertNotEqual(r.get("reason"), "content_unchanged")


class DuplicateContentGateTests(unittest.TestCase):
    """Cross-file dedup: identical bytes already indexed under a different,
    fully-synced doc_id must skip the embed and leave a self-healing marker."""

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
    def _drive_service_for(text: str, name: str = "copy.txt"):
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid", "name": name,
            "mimeType": "text/plain", "parents": ["sub1"],
        }
        media_req = mock.MagicMock()
        media_req.execute.return_value = text.encode("utf-8")
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        files_obj.get_media.return_value = media_req
        service = mock.MagicMock()
        service.files.return_value = files_obj
        return service

    def test_duplicate_content_skips_embed_and_records_marker(self):
        from agent_core.ingest import drive_sync

        body = "報價單：保固 12 個月，單價 USD 3.20"
        store = mock.MagicMock()
        # This file is not yet indexed (first time we see this copy)...
        store.get_doc_metadata.return_value = {
            "synced_at": "", "folder_id": "sub1", "drive_id": "0AABCDEF",
            "content_hash": "", "title": "", "sync_complete": True,
        }
        # ...but the identical bytes already live under canonical_fid.
        store.find_duplicate_doc_id.return_value = "canonical_fid"
        service = self._drive_service_for(body)

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertTrue(r["skipped"])
        self.assertEqual(r["reason"], "duplicate_content")
        self.assertEqual(r["canonical_doc_id"], "canonical_fid")
        # No embedding work for the duplicate copy.
        store.upsert_batch.assert_not_called()
        store.touch_synced_at.assert_not_called()
        # Any stale chunks this file carried are dropped — it's a pure pointer.
        store.delete_by_doc_id.assert_called_once_with("fid")
        # find_duplicate_doc_id excludes this file's own id and is scope-aware.
        store.find_duplicate_doc_id.assert_called_once()
        called = store.find_duplicate_doc_id.call_args.args
        self.assertEqual(called[0], _hash16(body))  # content_hash
        self.assertEqual(called[1], "fid")           # exclude self
        self.assertEqual(called[2], "0AABCDEF")      # drive_id
        self.assertEqual(called[3], "sub1")          # folder_id

    def test_no_duplicate_falls_through_to_full_embed(self):
        from agent_core.ingest import drive_sync

        body = "unique content nobody else has"
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "", "folder_id": "sub1", "drive_id": "0AABCDEF",
            "content_hash": "", "title": "", "sync_complete": True,
        }
        store.find_duplicate_doc_id.return_value = None
        service = self._drive_service_for(body)

        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid",
                folder_id="sub1",
                drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        self.assertEqual(r.get("chunks"), 1)
        store.upsert_batch.assert_called_once()
        store.delete_by_doc_id.assert_not_called()

    def test_duplicate_marker_skips_next_run_before_drive_download(self):
        """The recorded marker must fast-skip the next pass before any Drive
        RPC — that's where the download + embed cost is actually saved."""
        from agent_core.ingest import drive_sync

        body = "shared body"

        def make_store(canonical):
            store = mock.MagicMock()
            store.get_doc_metadata.return_value = {
                "synced_at": "", "folder_id": "sub1", "drive_id": "0AABCDEF",
                "content_hash": "", "title": "", "sync_complete": True,
            }
            store.find_duplicate_doc_id.return_value = canonical
            return store

        service = self._drive_service_for(body)
        # First pass: records the duplicate_content marker.
        with mock.patch.object(drive_sync, "get_store", return_value=make_store("canonical_fid")), \
             mock.patch("agent_core.google_auth.get_service", return_value=service) as get_service:
            first = drive_sync.sync_file(
                "fid", folder_id="sub1", drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )
        self.assertEqual(first["reason"], "duplicate_content")
        get_service.assert_called_once()

        # Second pass: canonical still present → marker matches → no Drive call.
        with mock.patch.object(drive_sync, "get_store", return_value=make_store("canonical_fid")), \
             mock.patch("agent_core.google_auth.get_service") as get_service:
            second = drive_sync.sync_file(
                "fid", folder_id="sub1", drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )
        self.assertEqual(second["reason"], "duplicate_content")
        get_service.assert_not_called()

    def test_duplicate_marker_self_clears_when_canonical_gone(self):
        """If the canonical copy later vanishes from the index, the marker must
        stop matching so the file gets re-embedded — content is never orphaned."""
        from agent_core.ingest import drive_sync

        body = "shared body"
        service = self._drive_service_for(body)
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "", "folder_id": "sub1", "drive_id": "0AABCDEF",
            "content_hash": "", "title": "", "sync_complete": True,
        }
        store.find_duplicate_doc_id.return_value = "canonical_fid"
        with mock.patch.object(drive_sync, "get_store", return_value=store), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            drive_sync.sync_file(
                "fid", folder_id="sub1", drive_id="0AABCDEF",
                modified_time="2026-05-07T10:00:00.000Z",
            )

        # Canonical now gone → find_duplicate_doc_id returns None.
        store.find_duplicate_doc_id.return_value = None
        marker = drive_sync._get_matching_skip_marker(
            "fid",
            modified_time="2026-05-07T10:00:00.000Z",
            folder_id="sub1",
            drive_id="0AABCDEF",
        )
        self.assertIsNone(marker)


class EmptyTextSkipMarkerTests(unittest.TestCase):
    def test_empty_text_marker_skips_next_unchanged_run_before_drive_download(self):
        from agent_core.ingest import drive_sync

        def make_store():
            store = mock.MagicMock()
            store.get_doc_metadata.return_value = {
                "synced_at": "",
                "folder_id": "sub1",
                "drive_id": "0AABCDEF",
                "content_hash": "",
                "title": "",
                "sync_complete": True,
            }
            return store

        meta = {
            "id": "fid",
            "name": "blank.pdf",
            "mimeType": "text/plain",
            "parents": ["sub1"],
            "modifiedTime": "2026-05-07T10:00:00.000Z",
        }
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = meta
        media_req = mock.MagicMock()
        media_req.execute.return_value = b"   "
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        files_obj.get_media.return_value = media_req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            drive_sync._SKIP_STATE_CACHE = None
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "get_store", return_value=make_store()), \
                     mock.patch("agent_core.google_auth.get_service", return_value=service) as get_service:
                    first = drive_sync.sync_file(
                        "fid",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                        modified_time=meta["modifiedTime"],
                    )
                self.assertEqual(first["reason"], "empty_text")
                get_service.assert_called_once()

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "get_store", return_value=make_store()), \
                     mock.patch("agent_core.google_auth.get_service") as get_service:
                    second = drive_sync.sync_file(
                        "fid",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                        modified_time=meta["modifiedTime"],
                    )
                self.assertEqual(second["reason"], "empty_text")
                get_service.assert_not_called()
            finally:
                drive_sync._SKIP_STATE_CACHE = None

    def test_unsupported_mime_marker_skips_next_unchanged_run_before_drive_metadata(self):
        from agent_core.ingest import drive_sync

        def make_store():
            store = mock.MagicMock()
            store.get_doc_metadata.return_value = {
                "synced_at": "",
                "folder_id": "sub1",
                "drive_id": "0AABCDEF",
                "content_hash": "",
                "title": "",
                "sync_complete": True,
            }
            return store

        meta = {
            "id": "fid",
            "name": "raw.bin",
            "mimeType": "application/x-not-supported",
            "parents": ["sub1"],
            "modifiedTime": "2026-05-07T10:00:00.000Z",
        }
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = meta
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            drive_sync._SKIP_STATE_CACHE = None
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "get_store", return_value=make_store()), \
                     mock.patch("agent_core.google_auth.get_service", return_value=service) as get_service:
                    first = drive_sync.sync_file(
                        "fid",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                        modified_time=meta["modifiedTime"],
                    )
                self.assertEqual(first["reason"], "unsupported mime_type")
                get_service.assert_called_once()

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "get_store", return_value=make_store()), \
                     mock.patch("agent_core.google_auth.get_service") as get_service:
                    second = drive_sync.sync_file(
                        "fid",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                        modified_time=meta["modifiedTime"],
                    )
                self.assertEqual(second["reason"], "unsupported mime_type")
                get_service.assert_not_called()

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "_DIRECT_TEXT", {"application/x-not-supported"}):
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time=meta["modifiedTime"],
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                    )
                self.assertIsNone(marker)
            finally:
                drive_sync._SKIP_STATE_CACHE = None

    def test_too_large_marker_skips_next_unchanged_run_before_drive_metadata(self):
        from agent_core.ingest import drive_sync

        def make_store():
            store = mock.MagicMock()
            store.get_doc_metadata.return_value = {
                "synced_at": "",
                "folder_id": "sub1",
                "drive_id": "0AABCDEF",
                "content_hash": "",
                "title": "",
                "sync_complete": True,
            }
            return store

        meta = {
            "id": "fid",
            "name": "huge.pdf",
            "mimeType": "application/pdf",
            "parents": ["sub1"],
            "modifiedTime": "2026-05-07T10:00:00.000Z",
            "size": "99",
        }
        meta_req = mock.MagicMock()
        meta_req.execute.return_value = meta
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            drive_sync._SKIP_STATE_CACHE = None
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "_DRIVE_MAX_DOWNLOAD_BYTES", 10), \
                     mock.patch.object(drive_sync, "get_store", return_value=make_store()), \
                     mock.patch("agent_core.google_auth.get_service", return_value=service) as get_service:
                    first = drive_sync.sync_file(
                        "fid",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                        modified_time=meta["modifiedTime"],
                    )
                self.assertTrue(first["reason"].startswith("too_large:"))
                get_service.assert_called_once()
                with open(state_file, encoding="utf-8") as fh:
                    skip_state = json.load(fh)
                marker = skip_state["files"]["fid"]
                self.assertEqual(marker["size_bytes"], 99)
                self.assertEqual(marker["limit_bytes"], 10)

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "_DRIVE_MAX_DOWNLOAD_BYTES", 10), \
                     mock.patch.object(drive_sync, "get_store", return_value=make_store()), \
                     mock.patch("agent_core.google_auth.get_service") as get_service:
                    second = drive_sync.sync_file(
                        "fid",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                        modified_time=meta["modifiedTime"],
                    )
                self.assertTrue(second["reason"].startswith("too_large:"))
                get_service.assert_not_called()

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "_DRIVE_MAX_DOWNLOAD_BYTES", 100):
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time=meta["modifiedTime"],
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                    )
                self.assertIsNone(marker)
            finally:
                drive_sync._SKIP_STATE_CACHE = None

    def test_legacy_too_large_xlsx_marker_clears_when_spreadsheet_cap_allows(self):
        from agent_core.ingest import drive_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            with open(state_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "version": 1,
                    "files": {
                        "fid": {
                            "reason": "too_large: 78643200 bytes > 52428800",
                            "modified_time": "2026-05-07T10:00:00.000Z",
                            "folder_id": "sub1",
                            "drive_id": "0AABCDEF",
                            "title": "Danh sách nhân sự 1.xlsx",
                        },
                    },
                }, fh)

            drive_sync._SKIP_STATE_CACHE = None
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "_DRIVE_MAX_DOWNLOAD_BYTES", 50 * 1024 * 1024), \
                     mock.patch.object(drive_sync, "_DRIVE_SPREADSHEET_MAX_DOWNLOAD_BYTES", 100 * 1024 * 1024):
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time="2026-05-07T10:00:00.000Z",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                    )
            finally:
                drive_sync._SKIP_STATE_CACHE = None

        self.assertIsNone(marker)

    def test_unsupported_marker_clears_when_mime_becomes_ignored(self):
        from agent_core.ingest import drive_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            with open(state_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "version": 1,
                    "files": {
                        "fid": {
                            "reason": "unsupported mime_type",
                            "modified_time": "2026-05-07T10:00:00.000Z",
                            "folder_id": "sub1",
                            "drive_id": "0AABCDEF",
                            "title": "driver.dll",
                            "mime_type": "application/x-msdownload",
                        },
                    },
                }, fh)

            drive_sync._SKIP_STATE_CACHE = None
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file):
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time="2026-05-07T10:00:00.000Z",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                    )
            finally:
                drive_sync._SKIP_STATE_CACHE = None

        self.assertIsNone(marker)

    def test_extract_too_large_marker_is_honored_next_sync(self):
        """An extract_too_large skip marker must match on the next sync so the
        oversized file is NOT re-downloaded + re-crashed (PR #81 review)."""
        from agent_core.ingest import drive_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            with open(state_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "version": 1,
                    "files": {
                        "fid": {
                            "reason": "extract_too_large: Drive file text fid: "
                                      "78643200 bytes > extract limit 31457280 (30 MB)",
                            "modified_time": "2026-06-01T10:00:00.000Z",
                            "folder_id": "sub1",
                            "drive_id": "0AABCDEF",
                            "title": "Danh sách nhân sự 1.xlsx",
                            "size_bytes": 78643200,
                            "limit_bytes": 31457280,
                        },
                    },
                }, fh)

            drive_sync._SKIP_STATE_CACHE = None
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "_DRIVE_EXTRACT_MAX_BYTES", 30 * 1024 * 1024):
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time="2026-06-01T10:00:00.000Z",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                    )
            finally:
                drive_sync._SKIP_STATE_CACHE = None

        self.assertIsNotNone(marker, "extract_too_large marker must be honored")

    def test_extract_too_large_marker_clears_when_limit_raised(self):
        """Raising RAG_DRIVE_EXTRACT_MAX_MB above the file size re-enables it."""
        from agent_core.ingest import drive_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            with open(state_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "version": 1,
                    "files": {
                        "fid": {
                            "reason": "extract_too_large: 78643200 bytes > extract limit 31457280",
                            "modified_time": "2026-06-01T10:00:00.000Z",
                            "folder_id": "sub1",
                            "drive_id": "0AABCDEF",
                            "title": "Danh sách nhân sự 1.xlsx",
                            "size_bytes": 78643200,
                        },
                    },
                }, fh)

            drive_sync._SKIP_STATE_CACHE = None
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(drive_sync, "_DRIVE_EXTRACT_MAX_BYTES", 100 * 1024 * 1024):
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time="2026-06-01T10:00:00.000Z",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                    )
            finally:
                drive_sync._SKIP_STATE_CACHE = None

        self.assertIsNone(marker, "raised limit must clear the marker")

    def test_deferred_skip_state_writes_flush_once(self):
        from agent_core.ingest import drive_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            drive_sync._SKIP_STATE_CACHE = None
            drive_sync._SKIP_STATE_DEFER_DEPTH = 0
            drive_sync._SKIP_STATE_DIRTY = False
            drive_sync._SKIP_STATE_SAVE_GENERATION = 0
            drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0
            try:
                real_write = drive_sync._write_skip_state_snapshot

                def checked_write(*args):
                    self.assertFalse(drive_sync._SKIP_STATE_LOCK._is_owned())
                    return real_write(*args)

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(
                         drive_sync,
                         "_write_skip_state_snapshot",
                         side_effect=checked_write,
                     ) as write_state:
                    with drive_sync._defer_skip_state_writes():
                        drive_sync._record_skip_marker(
                            "a",
                            reason="empty_text",
                            modified_time="2026-05-07T10:00:00.000Z",
                            folder_id="sub1",
                            drive_id="0AABCDEF",
                            title="a.txt",
                        )
                        drive_sync._record_skip_marker(
                            "b",
                            reason="empty_text",
                            modified_time="2026-05-07T10:00:00.000Z",
                            folder_id="sub1",
                            drive_id="0AABCDEF",
                            title="b.txt",
                        )
                    self.assertEqual(write_state.call_count, 1)
            finally:
                drive_sync._SKIP_STATE_CACHE = None
                drive_sync._SKIP_STATE_DEFER_DEPTH = 0
                drive_sync._SKIP_STATE_DIRTY = False
                drive_sync._SKIP_STATE_SAVE_GENERATION = 0
                drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0

    def test_immediate_skip_state_write_runs_after_lock_release(self):
        from agent_core.ingest import drive_sync

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            drive_sync._SKIP_STATE_CACHE = None
            drive_sync._SKIP_STATE_DEFER_DEPTH = 0
            drive_sync._SKIP_STATE_DIRTY = False
            drive_sync._SKIP_STATE_SAVE_GENERATION = 0
            drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0
            try:
                real_write = drive_sync._write_skip_state_snapshot

                def checked_write(*args):
                    self.assertFalse(drive_sync._SKIP_STATE_LOCK._is_owned())
                    return real_write(*args)

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(
                         drive_sync,
                         "_write_skip_state_snapshot",
                         side_effect=checked_write,
                     ) as write_state:
                    drive_sync._record_skip_marker(
                        "a",
                        reason="empty_text",
                        modified_time="2026-05-07T10:00:00.000Z",
                        folder_id="sub1",
                        drive_id="0AABCDEF",
                        title="a.txt",
                    )
                    self.assertEqual(write_state.call_count, 1)
            finally:
                drive_sync._SKIP_STATE_CACHE = None
                drive_sync._SKIP_STATE_DEFER_DEPTH = 0
                drive_sync._SKIP_STATE_DIRTY = False
                drive_sync._SKIP_STATE_SAVE_GENERATION = 0
                drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0

    def test_deferred_skip_state_write_failure_preserves_body_exception(self):
        from agent_core.ingest import drive_sync

        class BodyError(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            drive_sync._SKIP_STATE_CACHE = None
            drive_sync._SKIP_STATE_DEFER_DEPTH = 0
            drive_sync._SKIP_STATE_DIRTY = False
            drive_sync._SKIP_STATE_SAVE_GENERATION = 0
            drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0
            try:
                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file), \
                     mock.patch.object(
                         drive_sync,
                         "_write_skip_state_snapshot",
                         side_effect=OSError("disk full"),
                     ), \
                     self.assertLogs("agent_core.ingest.drive_sync", level="ERROR") as logs:
                    with self.assertRaisesRegex(BodyError, "primary failure"):
                        with drive_sync._defer_skip_state_writes():
                            drive_sync._record_skip_marker(
                                "a",
                                reason="empty_text",
                                modified_time="2026-05-07T10:00:00.000Z",
                                folder_id="sub1",
                                drive_id="0AABCDEF",
                                title="a.txt",
                            )
                            raise BodyError("primary failure")
                self.assertIn("failed to persist Drive skip state", logs.output[0])
            finally:
                drive_sync._SKIP_STATE_CACHE = None
                drive_sync._SKIP_STATE_DEFER_DEPTH = 0
                drive_sync._SKIP_STATE_DIRTY = False
                drive_sync._SKIP_STATE_SAVE_GENERATION = 0
                drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0

    def test_skip_state_stale_snapshot_cannot_overwrite_newer_generation(self):
        from agent_core.ingest import drive_sync

        marker = {
            "reason": "empty_text",
            "modified_time": "2026-05-07T10:00:00.000Z",
            "folder_id": "sub1",
            "drive_id": "0AABCDEF",
            "title": "a.txt",
            "seen_at": "2026-05-07T10:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "drive_sync_skip_state.json")
            drive_sync._SKIP_STATE_CACHE = None
            drive_sync._SKIP_STATE_DEFER_DEPTH = 0
            drive_sync._SKIP_STATE_DIRTY = False
            drive_sync._SKIP_STATE_SAVE_GENERATION = 0
            drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0
            try:
                state = {"version": 1, "files": {"a": dict(marker)}}
                with drive_sync._SKIP_STATE_LOCK:
                    older = drive_sync._save_or_defer_skip_state_unlocked(state)
                    state["files"]["b"] = {**marker, "title": "b.txt"}
                    newer = drive_sync._save_or_defer_skip_state_unlocked(state)

                with mock.patch.object(drive_sync, "_SKIP_STATE_FILE", state_file):
                    drive_sync._write_skip_state_snapshot(*newer)
                    drive_sync._write_skip_state_snapshot(*older)

                with open(state_file, encoding="utf-8") as fh:
                    saved = json.load(fh)
                self.assertEqual(set(saved["files"]), {"a", "b"})
            finally:
                drive_sync._SKIP_STATE_CACHE = None
                drive_sync._SKIP_STATE_DEFER_DEPTH = 0
                drive_sync._SKIP_STATE_DIRTY = False
                drive_sync._SKIP_STATE_SAVE_GENERATION = 0
                drive_sync._SKIP_STATE_LAST_WRITTEN_GENERATION = 0


class TouchSyncedAtTests(unittest.TestCase):
    """The metadata-only update path that the content-hash gate relies on —
    must NOT trigger an embed call (that would defeat the purpose)."""

    def test_updates_metadata_for_all_chunks_without_re_embedding(self):
        from agent_core.ingest import vector_store
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        col = mock.MagicMock()
        col.get.return_value = {
            "ids": ["fid__c0", "fid__c1"],
            "metadatas": [
                {"synced_at": "old1", "title": "x.docx"},
                {"synced_at": "old2", "title": "x.docx"},
            ],
        }
        store._col = col

        store.touch_synced_at("fid", "2026-05-07T10:00:00+00:00")

        # ChromaDB's update() with metadatas only does NOT recompute embeddings.
        col.update.assert_called_once()
        kwargs = col.update.call_args.kwargs
        self.assertEqual(kwargs["ids"], ["fid__c0", "fid__c1"])
        # Other fields preserved; only synced_at bumped.
        for m in kwargs["metadatas"]:
            self.assertEqual(m["synced_at"], "2026-05-07T10:00:00+00:00")
            self.assertEqual(m["title"], "x.docx")

    def test_no_op_when_doc_not_indexed(self):
        from agent_core.ingest import vector_store
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        col = mock.MagicMock()
        col.get.return_value = {"ids": [], "metadatas": []}
        store._col = col

        store.touch_synced_at("missing", "2026-05-07T10:00:00+00:00")
        col.update.assert_not_called()


class MarkDocSyncCompleteTests(unittest.TestCase):
    def test_marks_all_chunks_complete_without_re_embedding(self):
        from agent_core.ingest import vector_store
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        col = mock.MagicMock()
        col.get.return_value = {
            "ids": ["fid__c0", "fid__c1"],
            "metadatas": [
                {"doc_id": "fid", "sync_complete": False, "title": "x.docx"},
                {"doc_id": "fid", "sync_complete": False, "title": "x.docx"},
            ],
        }
        store._col = col

        store.mark_doc_sync_complete("fid")

        col.update.assert_called_once()
        kwargs = col.update.call_args.kwargs
        self.assertEqual(kwargs["ids"], ["fid__c0", "fid__c1"])
        for m in kwargs["metadatas"]:
            self.assertIs(m["sync_complete"], True)
            self.assertEqual(m["title"], "x.docx")


class FindDuplicateDocIdTests(unittest.TestCase):
    """vector_store.find_duplicate_doc_id HTTP-fallback path (no sqlite segment).
    The sqlite fast-path is exercised end-to-end in production; here we pin the
    Python-side filtering: exclude self, drop incomplete copies, pick canonical."""

    def _store_with_collection_get(self, metadatas):
        from agent_core.ingest import vector_store
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        store._collection_name = "drive_docs"
        col = mock.MagicMock()
        col.get.return_value = {"metadatas": metadatas}
        store._col = col
        # Force the HTTP/fallback branch: pretend there's no sqlite segment.
        store._metadata_segment = lambda: ""
        return store

    def test_returns_other_complete_doc_id(self):
        store = self._store_with_collection_get([
            {"doc_id": "other", "sync_complete": True},
        ])
        self.assertEqual(store.find_duplicate_doc_id("h", "self"), "other")

    def test_excludes_self(self):
        store = self._store_with_collection_get([
            {"doc_id": "self", "sync_complete": True},
        ])
        self.assertIsNone(store.find_duplicate_doc_id("h", "self"))

    def test_skips_incomplete_copies(self):
        store = self._store_with_collection_get([
            {"doc_id": "halfwritten", "sync_complete": False},
        ])
        self.assertIsNone(store.find_duplicate_doc_id("h", "self"))

    def test_picks_smallest_doc_id_as_canonical(self):
        store = self._store_with_collection_get([
            {"doc_id": "zzz", "sync_complete": True},
            {"doc_id": "aaa", "sync_complete": True},
            {"doc_id": "mmm", "sync_complete": True},
        ])
        self.assertEqual(store.find_duplicate_doc_id("h", "self"), "aaa")

    def test_empty_hash_returns_none_without_querying(self):
        store = self._store_with_collection_get([
            {"doc_id": "other", "sync_complete": True},
        ])
        self.assertIsNone(store.find_duplicate_doc_id("", "self"))
        store._col.get.assert_not_called()

    def test_legacy_chunk_without_sync_complete_treated_as_complete(self):
        store = self._store_with_collection_get([
            {"doc_id": "legacy"},  # no sync_complete key → defaults True
        ])
        self.assertEqual(store.find_duplicate_doc_id("h", "self"), "legacy")


class SameScopeDedupTests(unittest.TestCase):
    """find_duplicate_doc_id only collapses copies in the SAME (drive_id,
    folder_id) scope. Cross-folder / cross-drive copies are legitimately
    separate entries because search_drive_docs filters on those fields."""

    def _store(self, metadatas):
        from agent_core.ingest import vector_store
        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        store._collection_name = "drive_docs"
        col = mock.MagicMock()
        col.get.return_value = {"metadatas": metadatas}
        store._col = col
        store._metadata_segment = lambda: ""  # force HTTP/Python fallback
        return store

    def test_same_scope_is_deduped(self):
        store = self._store([
            {"doc_id": "canon", "sync_complete": True,
             "drive_id": "D1", "folder_id": "F1"},
        ])
        self.assertEqual(
            store.find_duplicate_doc_id("h", "self", "D1", "F1"), "canon"
        )

    def test_different_folder_not_deduped(self):
        store = self._store([
            {"doc_id": "canon", "sync_complete": True,
             "drive_id": "D1", "folder_id": "OTHER"},
        ])
        self.assertIsNone(
            store.find_duplicate_doc_id("h", "self", "D1", "F1")
        )

    def test_different_drive_not_deduped(self):
        store = self._store([
            {"doc_id": "canon", "sync_complete": True,
             "drive_id": "OTHER", "folder_id": "F1"},
        ])
        self.assertIsNone(
            store.find_duplicate_doc_id("h", "self", "D1", "F1")
        )

    def test_picks_smallest_among_same_scope_only(self):
        store = self._store([
            {"doc_id": "aaa", "sync_complete": True,  # wrong scope
             "drive_id": "OTHER", "folder_id": "F1"},
            {"doc_id": "mmm", "sync_complete": True,  # right scope
             "drive_id": "D1", "folder_id": "F1"},
            {"doc_id": "zzz", "sync_complete": True,  # right scope
             "drive_id": "D1", "folder_id": "F1"},
        ])
        # 'aaa' is smallest overall but wrong scope → 'mmm' wins.
        self.assertEqual(
            store.find_duplicate_doc_id("h", "self", "D1", "F1"), "mmm"
        )

    def test_no_scope_matches_no_scope(self):
        """Legacy chunks with no drive_id/folder_id (== '') dedup against a
        scopeless query, preserving the original same-folder behaviour."""
        store = self._store([
            {"doc_id": "canon", "sync_complete": True},
        ])
        self.assertEqual(store.find_duplicate_doc_id("h", "self"), "canon")

    def test_gate_passes_scope_to_finder(self):
        """sync_file must forward drive_id/folder_id so the gate is scope-aware."""
        from agent_core.ingest import drive_sync

        body = "scoped body text"
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "", "folder_id": "F1", "drive_id": "D1",
            "content_hash": "", "title": "", "sync_complete": True,
        }
        store.find_duplicate_doc_id.return_value = None  # no same-scope dup

        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid", "name": "copy.txt",
            "mimeType": "text/plain", "parents": ["F1"],
        }
        media_req = mock.MagicMock()
        media_req.execute.return_value = body.encode("utf-8")
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        files_obj.get_media.return_value = media_req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                drive_sync, "_SKIP_STATE_FILE",
                os.path.join(tmp, "s.json"),
            ), mock.patch.object(drive_sync, "get_store", return_value=store), \
               mock.patch("agent_core.google_auth.get_service", return_value=service):
                drive_sync._SKIP_STATE_CACHE = None
                drive_sync.sync_file(
                    "fid", folder_id="F1", drive_id="D1",
                    modified_time="2026-05-07T10:00:00.000Z",
                )
                drive_sync._SKIP_STATE_CACHE = None

        store.find_duplicate_doc_id.assert_called_once()
        args = store.find_duplicate_doc_id.call_args.args
        self.assertEqual(args[0], _hash16(body))  # content_hash
        self.assertEqual(args[1], "fid")           # exclude self
        self.assertEqual(args[2], "D1")            # drive_id
        self.assertEqual(args[3], "F1")            # folder_id


if __name__ == "__main__":
    unittest.main()
