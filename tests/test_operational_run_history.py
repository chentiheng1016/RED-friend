from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_RUN_HISTORY_BACKEND": "postgres",
}


class OperationalRunHistoryTests(unittest.TestCase):
    def setUp(self):
        from agent_core import run_history

        run_history._PG_RUN_HISTORY_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_run_history_switch(self):
        from agent_core import operational_run_history as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_audited_write_routes_to_postgres_backend(self):
        from agent_core import run_history

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.write_run_record",
                ) as write_record:
            @run_history.audited()
            def send_gmail(to, subject):
                return f"sent to {to}: {subject}"

            self.assertEqual(send_gmail("a@b.c", "hello"), "sent to a@b.c: hello")

        write_record.assert_called_once()
        record = write_record.call_args.args[0]
        self.assertEqual(record["tool"], "send_gmail")
        self.assertEqual(record["status"], "success")
        self.assertIn("sent to", record["result"])

    def test_list_runs_prefers_postgres_backend(self):
        from agent_core import run_history

        rows = [{
            "id": "run-1",
            "tool": "send_gmail",
            "started_at": "2026-06-24T12:00:00",
            "status": "success",
            "elapsed_sec": 0.1,
            "short_result": "sent",
        }]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.list_entries",
                    return_value=rows,
                ) as list_entries:
            out = run_history.list_runs(tool_name="send_gmail", status="success", since_hours=6, limit=5)

        self.assertIn("run-1", out)
        self.assertIn("send_gmail", out)
        list_entries.assert_called_once_with(
            tool_name="send_gmail",
            status="success",
            since_hours=6,
            limit=5,
        )

    def test_find_past_actions_prefers_postgres_backend(self):
        from agent_core import run_history

        rows = [{
            "id": "run-2",
            "tool": "create_event",
            "started_at": "2026-06-24T12:00:00",
            "status": "success",
            "elapsed_sec": 0.2,
            "short_result": "客戶A 會議",
            "kwargs": '{"title":"客戶A 會議"}',
        }]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.find_entries",
                    return_value=rows,
                ) as find_entries:
            out = run_history.find_past_actions("客戶A", days=3, limit=5)

        self.assertIn("run-2", out)
        self.assertIn("客戶A", out)
        find_entries.assert_called_once_with(query="客戶A", days=3, limit=5)

    def test_show_run_reads_postgres_backend(self):
        from agent_core import run_history

        record = {
            "id": "run-3",
            "tool": "send_gmail",
            "status": "success",
            "started_at": "2026-06-24T12:00:00",
            "ended_at": "2026-06-24T12:00:01",
            "elapsed_sec": 1.0,
            "args": "[]",
            "kwargs": "{}",
            "result": "sent",
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.read_run",
                    return_value=record,
                ) as read_run:
            out = run_history.show_run("run-3")

        self.assertIn("Run run-3", out)
        self.assertIn("sent", out)
        read_run.assert_called_once_with("run-3")

    def test_stats_reads_postgres_backend(self):
        from agent_core import run_history

        stats = {
            "total": 3,
            "success": 2,
            "error": 1,
            "tool_counts": [("send_gmail", 2), ("create_event", 1)],
            "error_tools": [("send_gmail", 1)],
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.stats",
                    return_value=stats,
                ):
            out = run_history.run_history_stats()

        self.assertIn("總 run 數: 3", out)
        self.assertIn("成功: 2", out)
        self.assertIn("send_gmail", out)

    def test_prune_routes_to_postgres_backend(self):
        from agent_core import run_history

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_run_history.prune_old_runs",
                    return_value=4,
                ) as prune:
            out = run_history.prune_old_runs(days=30)

        self.assertIn("刪除 4 筆 Postgres run history", out)
        prune.assert_called_once_with(30)


if __name__ == "__main__":
    unittest.main()
