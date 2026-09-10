"""Regression (review finding #12): upsert_to_chroma read row fields with bare
bracket access (row["date"], row["sender"][:100], int(row["message_count"])).
A single drifted/missing field raised and aborted the WHOLE batch's
vectorization — 50 emails lost to one bad row, with only a warning. Pin that
bad rows are skipped individually while the rest still upsert.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeCol:
    def __init__(self):
        self.calls = []

    def upsert(self, documents, ids, metadatas):
        self.calls.append({"documents": documents, "ids": ids, "metadatas": metadatas})


def _good_row(tid):
    return {
        "thread_id": tid,
        "primary_dept": "業務",
        "direction": "in",
        "subject": f"Subject {tid}",
        "summary": f"Summary {tid}",
        "entities_json": "{}",
        "brands": "[]",
        "date": "2026-06-01",
        "sender": "alice@acme.com",
        "message_count": 3,
    }


class UpsertToChromaResilienceTests(unittest.TestCase):
    def _run(self, rows):
        from agent_core import internal_emails_store

        col = _FakeCol()
        with mock.patch("agent_core.memory._get_memory_collection", return_value=col):
            internal_emails_store.upsert_to_chroma(rows)
        return col

    def test_missing_keys_row_is_skipped_not_fatal(self):
        rows = [
            _good_row("t1"),
            {"thread_id": "t2", "subject": "only subject"},  # missing date/sender/message_count
            _good_row("t3"),
        ]
        col = self._run(rows)
        self.assertEqual(len(col.calls), 1)
        ids = col.calls[0]["ids"]
        # The good rows survive...
        self.assertIn("t1", ids)
        self.assertIn("t3", ids)
        # ...and the sparse row, lacking the bracket-accessed fields, no longer
        # blows up the batch — it's handled (with defaults) rather than fatal.
        # (t2 here actually has enough to render with .get defaults, so it's in.)
        self.assertIn("t2", ids)

    def test_uncoercible_field_skips_only_that_row(self):
        bad = _good_row("bad")
        bad["message_count"] = "not-a-number"  # int(...) raises → skip just this row
        rows = [_good_row("t1"), bad, _good_row("t3")]
        col = self._run(rows)
        ids = col.calls[0]["ids"]
        self.assertEqual(set(ids), {"t1", "t3"})
        self.assertNotIn("bad", ids)

    def test_all_good_rows_upserted(self):
        col = self._run([_good_row("t1"), _good_row("t2")])
        self.assertEqual(set(col.calls[0]["ids"]), {"t1", "t2"})

    def test_bad_row_does_not_poison_a_later_same_thread_good_row(self):
        # A malformed row must NOT add its thread_id to seen_ids before it
        # throws — otherwise a later well-formed duplicate of the same thread
        # is silently dropped by the dedup check and the thread is vectorized
        # NEITHER way. (Regression: seen_ids.add was happening before int().)
        bad = _good_row("T")
        bad["message_count"] = "not-a-number"  # int(...) raises
        good = _good_row("T")  # same thread_id, fully valid, appears later
        good["subject"] = "the good duplicate"
        col = self._run([bad, good])
        ids = col.calls[0]["ids"]
        self.assertIn("T", ids)  # the good duplicate survives
        # and it's the GOOD row's content that got vectorized, not nothing
        doc = col.calls[0]["documents"][ids.index("T")]
        self.assertIn("the good duplicate", doc)


if __name__ == "__main__":
    unittest.main()
