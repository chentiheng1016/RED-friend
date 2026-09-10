from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_RUN_HISTORY_BACKEND": "postgres",
}


class OperationalMetricsTests(unittest.TestCase):
    def setUp(self):
        from agent_core import metrics

        metrics._PG_METRICS_WARNING_UNTIL = 0.0

    def test_metrics_summary_reads_postgres_run_history(self):
        from agent_core import metrics

        rows = [
            {
                "id": "r1",
                "tool": "send_gmail",
                "started_at": "2026-06-24T12:00:00",
                "status": "success",
                "elapsed_sec": 0.1,
                "short_result": "sent",
            },
            {
                "id": "r2",
                "tool": "send_gmail",
                "started_at": "2026-06-24T12:01:00",
                "status": "error",
                "elapsed_sec": 0.3,
                "short_result": "boom",
                "error_code": "network",
            },
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.load_metrics_entries",
                    return_value=rows,
                ) as load_entries:
            summary = metrics.metrics_summary(hours=6)

        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["total_success"], 1)
        self.assertEqual(summary["total_error"], 1)
        self.assertEqual(summary["success_pct"], 50.0)
        self.assertEqual(summary["error_codes"], {"network": 1})
        load_entries.assert_called_once_with(hours=6)

    def test_tool_metrics_reads_postgres_run_history(self):
        from agent_core import metrics

        rows = [
            {
                "id": "r1",
                "tool": "create_event",
                "started_at": "2026-06-24T12:00:00",
                "status": "success",
                "elapsed_sec": 0.1,
                "short_result": "ok",
            },
            {
                "id": "r2",
                "tool": "create_event",
                "started_at": "2026-06-24T12:02:00",
                "status": "error",
                "elapsed_sec": 0.5,
                "short_result": "calendar down",
                "error_code": "network",
            },
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.load_metrics_entries",
                    return_value=rows,
                ):
            result = metrics.tool_metrics(hours=6)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "create_event")
        self.assertEqual(result[0]["calls"], 2)
        self.assertEqual(result[0]["error"], 1)
        self.assertEqual(result[0]["last_error"], "calendar down")


if __name__ == "__main__":
    unittest.main()
