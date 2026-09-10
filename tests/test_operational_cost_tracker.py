from __future__ import annotations

import os
import types
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_COST_TRACKER_BACKEND": "postgres",
}


def _usage():
    return types.SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=50,
        cached_content_token_count=7,
        thoughts_token_count=3,
        tool_use_prompt_token_count=11,
        total_token_count=171,
    )


class OperationalCostTrackerTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker

        cost_tracker._PG_COST_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_cost_switch(self):
        from agent_core import operational_cost_tracker as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_record_call_writes_postgres_backend(self):
        from agent_core import cost_tracker

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_cost_tracker.write_cost_entry",
                ) as write_entry:
            cost_tracker.record_call(
                "gemini-flash-latest",
                _usage(),
                duration_ms=12.34,
                caller="telegram_chat",
            )

        write_entry.assert_called_once()
        entry = write_entry.call_args.args[0]
        self.assertEqual(entry["model"], "gemini-flash-latest")
        self.assertEqual(entry["prompt_tokens"], 100)
        self.assertEqual(entry["output_tokens"], 50)
        self.assertEqual(entry["thinking_tokens"], 3)
        self.assertEqual(entry["cached_tokens"], 7)
        self.assertEqual(entry["tool_use_tokens"], 11)
        self.assertEqual(entry["total_tokens"], 171)
        self.assertGreater(entry["cost_usd"], 0)
        self.assertEqual(entry["duration_ms"], 12.3)
        self.assertEqual(entry["caller"], "telegram_chat")

    def test_record_api_error_writes_postgres_backend(self):
        from agent_core import cost_tracker

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_cost_tracker.write_api_error",
                ) as write_error:
            cost_tracker.record_api_error(
                "gemini",
                "503",
                model="gemini-flash-latest",
                detail="service unavailable",
            )

        write_error.assert_called_once()
        entry = write_error.call_args.args[0]
        self.assertEqual(entry["service"], "gemini")
        self.assertEqual(entry["status"], "503")
        self.assertEqual(entry["model"], "gemini-flash-latest")
        self.assertEqual(entry["detail"], "service unavailable")

    def test_load_entries_prefers_postgres_backend(self):
        from agent_core import cost_tracker

        rows = [{"ts": "2026-06-24T12:00:00", "cost_usd": 0.01}]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_cost_tracker.load_cost_entries",
                    return_value=rows,
                ) as load_entries:
            self.assertEqual(cost_tracker._load_entries(hours=6), rows)

        load_entries.assert_called_once_with(hours=6)

    def test_api_error_stats_reads_postgres_backend(self):
        from agent_core import cost_tracker

        successes = [
            {"ts": "2026-06-24T12:00:00", "model": "gemini-flash-latest"}
            for _ in range(8)
        ]
        errors = [
            {
                "ts": "2026-06-24T12:01:00",
                "status": "503",
                "model": "gemini-flash-latest",
            },
            {
                "ts": "2026-06-24T12:02:00",
                "status": "429",
                "model": "gemini-3-flash-preview",
            },
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_cost_tracker.load_cost_entries",
                    return_value=successes,
                ) as load_entries, \
                mock.patch(
                    "agent_core.operational_cost_tracker.load_api_errors",
                    return_value=errors,
                ) as load_errors:
            stats = cost_tracker.api_error_stats(hours=6)

        self.assertEqual(stats["errors"], 2)
        self.assertEqual(stats["successes"], 8)
        self.assertEqual(stats["total"], 10)
        self.assertEqual(stats["error_rate_pct"], 20.0)
        self.assertEqual(stats["by_status"], {"503": 1, "429": 1})
        self.assertEqual(
            stats["by_model"]["gemini-flash-latest"],
            {"errors": 1, "successes": 8, "total": 9, "error_rate_pct": 11.1},
        )
        self.assertEqual(
            stats["by_model"]["gemini-3-flash-preview"],
            {"errors": 1, "successes": 0, "total": 1, "error_rate_pct": 100.0},
        )
        load_entries.assert_called_once_with(hours=6)
        load_errors.assert_called_once_with(hours=6)


if __name__ == "__main__":
    unittest.main()
