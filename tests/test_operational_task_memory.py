from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_TASK_MEMORY_BACKEND": "postgres",
}


class OperationalTaskMemoryTests(unittest.TestCase):
    def setUp(self):
        from agent_core import task_memory

        task_memory._PG_TASK_MEMORY_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_task_memory_switch(self):
        from agent_core import operational_task_memory as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_load_tasks_reads_postgres_backend(self):
        from agent_core import task_memory

        rows = {"version": 1, "tasks": [{"id": "task_1", "status": "pending"}]}
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_task_memory.load_tasks",
                    return_value=rows,
                ) as load_tasks:
            self.assertEqual(task_memory._load_tasks(), rows)

        load_tasks.assert_called_once()

    def test_add_task_writes_postgres_backend(self):
        from agent_core import task_memory

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_tasks():
            data = {"version": 1, "tasks": []}
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_task_memory.locked_tasks",
                    side_effect=fake_locked_tasks,
                ) as locked_tasks:
            out = task_memory.add_task("回客戶 PO", linked_customer="客戶 A")

        self.assertTrue(out.ok, out)
        self.assertEqual(len(captured["tasks"]), 1)
        self.assertEqual(captured["tasks"][0]["title"], "回客戶 PO")
        self.assertEqual(captured["tasks"][0]["linked_customer"], "客戶 A")
        locked_tasks.assert_called_once()

    def test_update_status_writes_postgres_backend(self):
        from agent_core import task_memory

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_tasks():
            data = {
                "version": 1,
                "tasks": [{"id": "task_1", "title": "x", "status": "pending", "log": []}],
            }
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_task_memory.locked_tasks",
                    side_effect=fake_locked_tasks,
                ):
            out = task_memory.update_task_status("task_1", "done", "finished")

        self.assertTrue(out.ok, out)
        self.assertEqual(captured["tasks"][0]["status"], "done")
        self.assertEqual(captured["tasks"][0]["next_reminder_at"], "")
        self.assertTrue(captured["tasks"][0].get("completed_at"))

    def test_task_summary_reads_postgres_backend(self):
        from agent_core import task_memory

        rows = {
            "version": 1,
            "tasks": [
                {
                    "id": "task_1",
                    "title": "x",
                    "status": "pending",
                    "deadline": "2020-01-01",
                    "next_reminder_at": "2026-06-25",
                },
                {"id": "task_2", "title": "y", "status": "done"},
            ],
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_task_memory.load_tasks",
                    return_value=rows,
                ):
            summary = task_memory.task_summary()

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_status"]["pending"], 1)
        self.assertEqual(summary["by_status"]["done"], 1)
        self.assertEqual(summary["overdue"], 1)
        self.assertEqual(summary["with_reminder"], 1)


if __name__ == "__main__":
    unittest.main()
