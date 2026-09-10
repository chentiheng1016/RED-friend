"""Tests for drive_sync._chunk_text — chunking + title prefix.

The title prefix is a precision lever: chunks read by the embedder gain
document-level context, so queries like '客戶 ABC 的保固' match against
chunks of ABC's contract even when the chunk itself only mentions '保固'.
Without the prefix, the contract chunk and a generic warranty FAQ look
embedding-equivalent.
"""
from __future__ import annotations

import os
import sys
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ChunkTextTests(unittest.TestCase):
    def test_no_title_falls_back_to_unprefixed_chunks(self):
        from agent_core.ingest import drive_sync
        chunks = drive_sync._chunk_text("hello world")
        self.assertEqual(chunks, ["hello world"])

    def test_title_prefixed_to_every_chunk(self):
        from agent_core.ingest import drive_sync
        # Force multi-chunk output by using text > CHUNK_SIZE.
        text = "x" * (drive_sync.CHUNK_SIZE * 2)
        chunks = drive_sync._chunk_text(text, title="ABC合約.docx")
        self.assertGreaterEqual(len(chunks), 2)
        for c in chunks:
            self.assertTrue(c.startswith("[ABC合約.docx] "), c[:50])

    def test_payload_size_shrinks_to_keep_total_under_chunk_size(self):
        """Without budget adjustment, prefix + payload would push past
        CHUNK_SIZE and could exceed Gemini's per-input token budget."""
        from agent_core.ingest import drive_sync
        text = "x" * (drive_sync.CHUNK_SIZE * 3)
        title = "AAAAA"  # 5 chars + brackets + space = 8 char prefix
        chunks = drive_sync._chunk_text(text, title=title)
        for c in chunks:
            self.assertLessEqual(len(c), drive_sync.CHUNK_SIZE)

    def test_pathologically_long_title_keeps_payload_nonzero(self):
        """A 200-char filename shouldn't reduce payload to 0 chars."""
        from agent_core.ingest import drive_sync
        long_title = "X" * 500
        text = "abc" * 1000
        chunks = drive_sync._chunk_text(text, title=long_title)
        self.assertTrue(chunks)
        # Title was truncated to keep the prefix from dominating CHUNK_SIZE.
        for c in chunks:
            # Each chunk is prefix + ≥1 char payload — never just prefix.
            self.assertGreater(len(c), len("[" + long_title[:80] + "] "))

    def test_empty_text_returns_empty_list(self):
        from agent_core.ingest import drive_sync
        self.assertEqual(drive_sync._chunk_text("", title="anything"), [])
        self.assertEqual(drive_sync._chunk_text("   ", title="anything"), [])

    def test_overlap_between_consecutive_chunks(self):
        """Chunk N's tail overlaps with chunk N+1's head — preserves context
        across boundaries so a sentence split across chunks still retrieves."""
        from agent_core.ingest import drive_sync
        # Use unique tokens at known positions so we can verify overlap.
        text = "".join(f"#{i:03d}" for i in range(500))  # 4 chars per token
        chunks = drive_sync._chunk_text(text)  # no title → prefix-free
        self.assertGreater(len(chunks), 1)
        # Last 80 chars of chunk[0] (overlap) must appear at start of chunk[1].
        tail = chunks[0][-drive_sync.CHUNK_OVERLAP:]
        self.assertIn(tail[:20], chunks[1][:100])  # at least the leading tokens overlap


if __name__ == "__main__":
    unittest.main()
