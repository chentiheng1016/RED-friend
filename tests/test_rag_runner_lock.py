"""Tests for rag_sync's process lock hygiene."""
from __future__ import annotations

import fcntl
import os
import tempfile
import unittest
from unittest import mock


class RagRunnerLockTests(unittest.TestCase):
    def test_run_sync_clears_lock_file_after_success(self):
        from agent_core.ingest import rag_runner
        import agent_core.logging_and_paths as paths

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(paths, "STATE_DIR", tmpdir), \
             mock.patch.object(rag_runner, "_run_sync_locked", return_value={"ok": True}):
            result = rag_runner.run_sync()

            self.assertEqual(result, {"ok": True})
            with open(os.path.join(tmpdir, "rag_sync.lock"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "")

    def test_run_sync_returns_locked_when_another_writer_has_lock(self):
        from agent_core.ingest import rag_runner
        import agent_core.logging_and_paths as paths

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.object(paths, "STATE_DIR", tmpdir), \
             mock.patch.object(rag_runner, "_run_sync_locked") as sync_mock:
            lock_path = os.path.join(tmpdir, "rag_sync.lock")
            with open(lock_path, "a+", encoding="utf-8") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = rag_runner.run_sync()

            self.assertTrue(result["locked"])
            self.assertEqual(result["drive"], [])
            sync_mock.assert_not_called()


class RagRunnerLogSummaryTests(unittest.TestCase):
    def test_compact_sync_result_counts_reasons_without_details(self):
        from agent_core.ingest import rag_runner

        result = {
            "drive_id": "0A-test",
            "total": 5,
            "synced": 1,
            "skipped": 4,
            "purged": 0,
            "listing_complete": True,
            "errors": ["top-level errors are not part of per-drive compaction"],
            "details": [
                {"file_id": "a", "chunks": 2},
                {"file_id": "b", "skipped": True, "reason": "unchanged"},
                {"file_id": "c", "skipped": True, "reason": "empty_text"},
                {"file_id": "d", "skipped": True, "reason": "too_large: 99 bytes"},
                {"file_id": "e", "title": "broken.pdf", "skipped": True, "reason": "error: boom"},
            ],
        }

        compact = rag_runner._compact_sync_result(result)

        self.assertNotIn("details", compact)
        self.assertNotIn("errors", compact)
        self.assertEqual(compact["total"], 5)
        self.assertEqual(compact["files_with_chunks"], 1)
        self.assertEqual(
            compact["skip_reasons"],
            {"empty_text": 1, "error": 1, "too_large": 1, "unchanged": 1},
        )
        self.assertEqual(
            compact["error_samples"],
            [{"file_id": "e", "title": "broken.pdf", "reason": "error: boom"}],
        )

    def test_skip_reason_bucket_collapses_dynamic_suffixes(self):
        """帶動態尾巴（例外訊息/大小/檔名）的 reason 要歸併成一鍵 — 之前
        extract_error/timeout/embedding_unavailable 每檔一鍵，summary 爆量。"""
        from agent_core.ingest import rag_runner

        cases = {
            "error: boom": "error",
            "too_large: 99 bytes > 10": "too_large",
            "extract_too_large: 78643200 bytes > extract limit": "extract_too_large",
            "extract_error: FileNotDecryptedError": "extract_error",
            "timeout: Drive file text fid exceeded 300s": "timeout",
            "embedding_unavailable: monthly spending cap": "embedding_unavailable",
            "image_ocr_unavailable: daily budget exhausted": "image_ocr_unavailable",
            "media_transcription_unavailable: budget": "media_transcription_unavailable",
            # 無動態尾巴的 reason 原樣保留。
            "unchanged": "unchanged",
            "empty_text": "empty_text",
            "duplicate_content": "duplicate_content",
        }
        for reason, bucket in cases.items():
            self.assertEqual(rag_runner._skip_reason_bucket(reason), bucket, reason)

    def test_skip_reason_bucket_groups_distinct_tails_together(self):
        from agent_core.ingest import rag_runner
        from collections import Counter

        reasons = Counter(
            rag_runner._skip_reason_bucket(r) for r in (
                "extract_error: FileNotDecryptedError",
                "extract_error: BadZipFile",
                "timeout: file A exceeded 300s",
                "timeout: file B exceeded 300s",
            )
        )
        self.assertEqual(reasons, {"extract_error": 2, "timeout": 2})


if __name__ == "__main__":
    unittest.main()
