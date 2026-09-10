from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_EDGE_TASKS_BACKEND": "postgres",
}


class OperationalEdgeTasksTests(unittest.TestCase):
    def setUp(self):
        from agent_core import edge_tasks

        edge_tasks._PG_EDGE_WARNING_UNTIL = 0.0

    def _draft(self):
        from agent_core.agents.green_sample_dev import rpa_tasks

        return rpa_tasks.build_edge_task_draft(
            "green_update_sample_status",
            {"sample_id": "S-DEV-1", "status": "closed"},
            employee_email="dev@company.example",
            device_id="mac-dev-01",
            requested_by="owner@company.example",
        )

    def test_backend_requires_explicit_edge_switch(self):
        from agent_core import operational_edge_tasks as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_register_device_writes_postgres_backend(self):
        from agent_core import edge_tasks

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_state():
            data = {"version": 1, "devices": {}, "tasks": []}
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch("agent_core.operational_edge_tasks.locked_state", side_effect=fake_locked_state) as locked:
            result = edge_tasks.register_device(
                device_id="mac-dev-01",
                department="green",
                employee_email="dev@company.example",
            )

        self.assertTrue(result["ok"])
        self.assertIn("mac-dev-01", captured["devices"])
        self.assertEqual(captured["devices"]["mac-dev-01"]["department"], "green")
        locked.assert_called_once()

    def test_enqueue_claim_and_complete_use_postgres_backend(self):
        from agent_core import edge_tasks

        state = {
            "version": 1,
            "devices": {
                "mac-dev-01": {
                    "device_id": "mac-dev-01",
                    "department": "green",
                    "employee_email": "dev@company.example",
                    "status": "active",
                },
            },
            "tasks": [],
        }

        @contextlib.contextmanager
        def fake_locked_state():
            yield state

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch("agent_core.operational_edge_tasks.locked_state", side_effect=fake_locked_state):
            queued = edge_tasks.enqueue_edge_task(self._draft(), created_by="owner@company.example")
            task_id = queued["task"]["task_id"]
            claimed = edge_tasks.claim_next_task(
                device_id="mac-dev-01",
                department="green",
                employee_email="dev@company.example",
            )
            done = edge_tasks.update_task_status(
                task_id=task_id,
                device_id="mac-dev-01",
                status="done",
                message="completed",
                result={"screenshots": 2},
            )

        self.assertEqual(claimed["task"]["task_id"], task_id)
        self.assertEqual(claimed["task"]["status"], "running")
        self.assertEqual(done["task"]["status"], "done")
        self.assertEqual(state["tasks"][0]["status"], "done")
        self.assertEqual(state["tasks"][0]["result"], {"screenshots": 2})

    def test_load_state_reads_postgres_backend(self):
        from agent_core import edge_tasks

        rows = {
            "version": 1,
            "devices": {"mac-dev-01": {"device_id": "mac-dev-01"}},
            "tasks": [{"task_id": "edge_1", "status": "queued"}],
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_edge_tasks.locked_state",
                    return_value=contextlib.nullcontext(rows),
                ) as locked:
            self.assertEqual(edge_tasks.load_state(), rows)

        locked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
