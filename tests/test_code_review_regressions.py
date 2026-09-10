"""Regression coverage for PR review findings."""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class GeminiGenerateSignatureTests(unittest.TestCase):
    def test_progress_report_uses_model_contents_signature(self):
        from agent_core import progress_manager

        manager = progress_manager.ProgressManager.__new__(progress_manager.ProgressManager)
        overall = {
            "total_samples": 2,
            "completed_samples": 1,
            "completion_rate": 0.5,
            "delayed_samples": ["S-1"],
            "delayed_count": 1,
            "last_updated": "2026-05-13T00:00:00",
        }
        with mock.patch.object(manager, "calculate_overall_progress", return_value=overall), \
             mock.patch.object(
                 progress_manager,
                 "_gemini_generate",
                 return_value=types.SimpleNamespace(text="進度摘要"),
             ) as generate:
            report = manager.generate_progress_report()

        self.assertIn("進度摘要", report)
        self.assertEqual(generate.call_args.kwargs["model"], progress_manager.GEMINI_MODEL)
        self.assertIsInstance(generate.call_args.kwargs["contents"], list)
        self.assertNotIn("max_tokens", generate.call_args.kwargs)


class GuardPerformanceTests(unittest.TestCase):
    def test_is_dry_run_does_not_parse_state_file_on_every_call(self):
        from agent_core import dry_run

        old_state = {
            "enabled": dry_run._STATE["enabled"],
            "enabled_at": dry_run._STATE["enabled_at"],
            "simulated_calls": dry_run._STATE["simulated_calls"],
        }
        old_refreshed_at = dry_run._STATE_REFRESHED_AT
        old_mtime = dry_run._STATE_FILE_MTIME
        old_ttl = dry_run._REFRESH_TTL_S
        try:
            dry_run._STATE["enabled"] = False
            dry_run._STATE["enabled_at"] = None
            dry_run._STATE["simulated_calls"].clear()
            dry_run._STATE_REFRESHED_AT = 0.0
            dry_run._STATE_FILE_MTIME = None
            dry_run._REFRESH_TTL_S = 1.0

            with mock.patch.object(dry_run.os.path, "getmtime", return_value=123.0), \
                 mock.patch.object(
                     dry_run,
                     "_load_persisted_state",
                     return_value={"enabled": True, "enabled_at": "now", "simulated_calls": []},
                 ) as load_state, \
                 mock.patch.object(dry_run.time, "monotonic", side_effect=[10.0, 10.1]):
                self.assertTrue(dry_run.is_dry_run())
                self.assertTrue(dry_run.is_dry_run())

            self.assertEqual(load_state.call_count, 1)
        finally:
            dry_run._STATE["enabled"] = old_state["enabled"]
            dry_run._STATE["enabled_at"] = old_state["enabled_at"]
            dry_run._STATE["simulated_calls"] = old_state["simulated_calls"]
            dry_run._STATE_REFRESHED_AT = old_refreshed_at
            dry_run._STATE_FILE_MTIME = old_mtime
            dry_run._REFRESH_TTL_S = old_ttl

    def test_vector_store_metadata_segment_uses_short_ttl_cache(self):
        from agent_core.ingest import vector_store

        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        store._collection_name = "drive_docs"
        store._metadata_segment_cache_id = ""
        store._metadata_segment_cache_until = 0.0

        with mock.patch.object(vector_store, "_METADATA_SEGMENT_CACHE_TTL_S", 1.0), \
             mock.patch.object(vector_store.time, "monotonic", side_effect=[10.0, 10.1, 11.2]), \
             mock.patch.object(
                 store,
                 "_load_metadata_segment_id",
                 side_effect=["seg-1", "seg-2"],
             ) as load_segment:
            self.assertEqual(store._metadata_segment(), "seg-1")
            self.assertEqual(store._metadata_segment(), "seg-1")
            self.assertEqual(store._metadata_segment(), "seg-2")

        self.assertEqual(load_segment.call_count, 2)


if __name__ == "__main__":
    unittest.main()
