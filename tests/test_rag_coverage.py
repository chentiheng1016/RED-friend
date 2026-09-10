"""Tests for the RAG (Drive/Gmail/Chat) coverage report tool.

Replaces the one-off measurement scripts written repeatedly this session
(distinct doc_id grouping, drive_id→department resolution, chroma counts)
with a reusable, LLM-callable tool. Tests pin: dept-label resolution
(shared-drive vs plain-folder fallback), live-count failure degrades to
N/A rather than crashing, percentage math, and tool registration.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── _resolve_target_label ─────────────────────────────────────────────

class ResolveTargetLabelTests(unittest.TestCase):
    def test_shared_drive_resolves_via_drives_get(self):
        from agent_core import rag_coverage
        svc = mock.MagicMock()
        svc.drives.return_value.get.return_value.execute.return_value = {"name": "採購部門"}
        label, is_drive = rag_coverage._resolve_target_label(svc, "0AABCDEF")
        self.assertEqual(label, "採購部門")
        self.assertTrue(is_drive)

    def test_plain_folder_falls_back_to_files_get(self):
        from agent_core import rag_coverage
        svc = mock.MagicMock()
        svc.drives.return_value.get.return_value.execute.side_effect = RuntimeError("not a drive")
        svc.files.return_value.get.return_value.execute.return_value = {"name": "Meet Recordings"}
        label, is_drive = rag_coverage._resolve_target_label(svc, "1MsvKHF")
        self.assertEqual(label, "Meet Recordings")
        self.assertFalse(is_drive)

    def test_both_fail_falls_back_to_raw_id(self):
        from agent_core import rag_coverage
        svc = mock.MagicMock()
        svc.drives.return_value.get.return_value.execute.side_effect = RuntimeError("nope")
        svc.files.return_value.get.return_value.execute.side_effect = RuntimeError("nope either")
        label, is_drive = rag_coverage._resolve_target_label(svc, "deadbeef")
        self.assertEqual(label, "deadbeef")
        self.assertFalse(is_drive)


# ── _live_file_count ──────────────────────────────────────────────────

class LiveFileCountTests(unittest.TestCase):
    def test_shared_drive_paginates_and_sums(self):
        from agent_core import rag_coverage
        svc = mock.MagicMock()
        pages = [
            {"files": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
            {"files": [{"id": "c"}]},
        ]
        svc.files.return_value.list.return_value.execute.side_effect = pages
        n = rag_coverage._live_file_count(svc, "0AABCDEF", is_shared_drive=True)
        self.assertEqual(n, 3)

    def test_plain_folder_uses_parents_query(self):
        from agent_core import rag_coverage
        svc = mock.MagicMock()
        svc.files.return_value.list.return_value.execute.return_value = {"files": [{"id": "a"}]}
        n = rag_coverage._live_file_count(svc, "folder1", is_shared_drive=False)
        self.assertEqual(n, 1)
        kwargs = svc.files.return_value.list.call_args.kwargs
        self.assertIn("folder1", kwargs["q"])
        self.assertNotIn("corpora", kwargs)

    def test_api_failure_returns_none_not_raise(self):
        from agent_core import rag_coverage
        svc = mock.MagicMock()
        svc.files.return_value.list.return_value.execute.side_effect = RuntimeError("403")
        n = rag_coverage._live_file_count(svc, "0AABCDEF", is_shared_drive=True)
        self.assertIsNone(n)


# ── rag_coverage_report ───────────────────────────────────────────────

class RagCoverageReportTests(unittest.TestCase):
    def _store(self, doc_ids):
        store = mock.MagicMock()
        store.list_doc_ids.return_value = set(doc_ids)
        store.list_doc_ids_by_drive.side_effect = (
            lambda drive_id: {d for d in doc_ids if d.startswith(drive_id)}
        )
        return store

    def test_reports_pct_and_grand_total(self):
        from agent_core import rag_coverage

        drive_store = self._store({"0A1_x", "0A1_y", "0A2_z"})
        gmail_store = self._store({"t1", "t2", "t3"})
        chat_store = self._store({"spaces/a"})

        def _get_store(name):
            return {
                "drive_docs": drive_store,
                "gmail_threads": gmail_store,
                "google_chat_messages": chat_store,
            }[name]

        drive_svc = mock.MagicMock()
        drive_svc.drives.return_value.get.return_value.execute.return_value = {"name": "採購部門"}

        with mock.patch.object(rag_coverage, "load_targets",
                                return_value={"drive_folder_ids": ["0A1"]}), \
             mock.patch("agent_core.ingest.vector_store.get_store", side_effect=_get_store), \
             mock.patch("agent_core.dashboard._ensure_chroma_endpoint"), \
             mock.patch("agent_core.google_auth.get_service", return_value=drive_svc), \
             mock.patch.object(rag_coverage, "_live_file_count", return_value=4):
            report = rag_coverage.rag_coverage_report()

        self.assertIn("採購部門", report)
        self.assertIn("索引", report)
        self.assertIn("50.0%", report)  # 2 indexed / 4 total
        self.assertIn("Gmail", report)
        self.assertIn("已索引 threads：3", report)
        self.assertIn("Google Chat", report)
        self.assertIn("已索引 space 數：1", report)

    def test_unknown_live_total_shows_na_not_crash(self):
        from agent_core import rag_coverage

        drive_store = self._store({"0A1_x"})
        stores = {
            "drive_docs": drive_store,
            "gmail_threads": self._store(set()),
            "google_chat_messages": self._store(set()),
        }
        drive_svc = mock.MagicMock()
        drive_svc.drives.return_value.get.return_value.execute.return_value = {"name": "開發部門"}

        with mock.patch.object(rag_coverage, "load_targets",
                                return_value={"drive_folder_ids": ["0A1"]}), \
             mock.patch("agent_core.ingest.vector_store.get_store", side_effect=lambda n: stores[n]), \
             mock.patch("agent_core.dashboard._ensure_chroma_endpoint"), \
             mock.patch("agent_core.google_auth.get_service", return_value=drive_svc), \
             mock.patch.object(rag_coverage, "_live_file_count", return_value=None):
            report = rag_coverage.rag_coverage_report()

        self.assertIn("N/A", report)
        self.assertNotIn("Traceback", report)

    def test_no_drive_targets_configured(self):
        from agent_core import rag_coverage

        stores = {
            "gmail_threads": self._store(set()),
            "google_chat_messages": self._store(set()),
        }
        with mock.patch.object(rag_coverage, "load_targets", return_value={"drive_folder_ids": []}), \
             mock.patch("agent_core.ingest.vector_store.get_store", side_effect=lambda n: stores[n]), \
             mock.patch("agent_core.dashboard._ensure_chroma_endpoint"):
            report = rag_coverage.rag_coverage_report()
        self.assertIn("未設定同步目標", report)


# ── tool registration ─────────────────────────────────────────────────

class ToolRegistrationTests(unittest.TestCase):
    def test_in_global_tools_list(self):
        from agent_core.tool_registry import tools_list
        names = {getattr(t, "__name__", "") for t in tools_list}
        self.assertIn("rag_coverage_report", names)

    def test_in_query_data_intent_bucket(self):
        from agent_core import intent_router
        bucket = intent_router._TOOL_BUCKETS[intent_router.INTENT_QUERY_DATA]
        self.assertIn("rag_coverage_report", bucket)

    def test_tier_is_safe(self):
        """A read-only report must not demand a +確認 token in Telegram."""
        from agent_core.tool_tiers import get_tier, TIER_SAFE
        self.assertEqual(get_tier("rag_coverage_report"), TIER_SAFE)


if __name__ == "__main__":
    unittest.main()
