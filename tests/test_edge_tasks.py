from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock


class EdgeTasksTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {"RED_EDGE_TASKS_FILE": os.path.join(self._tmpdir.name, "edge_tasks.json")},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _draft(self):
        from agent_core.agents.green_sample_dev import rpa_tasks

        return rpa_tasks.build_edge_task_draft(
            "green_update_sample_status",
            {"sample_id": "S-DEV-1", "status": "closed"},
            employee_email="dev@company.example",
            device_id="mac-dev-01",
            requested_by="owner@company.example",
        )

    def test_enqueue_claim_and_complete_task(self):
        from agent_core import edge_tasks

        device = edge_tasks.register_device(
            device_id="mac-dev-01",
            department="green",
            employee_email="dev@company.example",
            device_name="開發 Mac",
            capabilities=["erp.sample_master"],
        )
        self.assertTrue(device["ok"])

        queued = edge_tasks.enqueue_edge_task(self._draft(), created_by="owner@company.example")
        task_id = queued["task"]["task_id"]
        self.assertEqual(queued["task"]["status"], "queued")

        claimed = edge_tasks.claim_next_task(
            device_id="mac-dev-01",
            department="green",
            employee_email="dev@company.example",
        )
        self.assertEqual(claimed["task"]["task_id"], task_id)
        self.assertEqual(claimed["task"]["status"], "running")

        done = edge_tasks.update_task_status(
            task_id=task_id,
            device_id="mac-dev-01",
            status="done",
            message="completed",
            result={"screenshots": 2},
        )
        self.assertEqual(done["task"]["status"], "done")
        self.assertEqual(done["task"]["result"], {"screenshots": 2})

    def test_claim_respects_department_and_employee(self):
        from agent_core import edge_tasks

        edge_tasks.enqueue_edge_task(self._draft())

        wrong_department = edge_tasks.claim_next_task(
            device_id="mac-dev-01",
            department="orange",
            employee_email="dev@company.example",
        )
        self.assertIsNone(wrong_department["task"])

        wrong_employee = edge_tasks.claim_next_task(
            device_id="mac-dev-01",
            department="green",
            employee_email="other@company.example",
        )
        self.assertIsNone(wrong_employee["task"])

    def test_registered_device_cannot_claim_other_department(self):
        from agent_core import edge_tasks

        # green 任務 + 註冊在 green 的裝置改傳 department=orange → 不得認領（IDOR 防護）。
        edge_tasks.register_device(
            device_id="mac-dev-01",
            department="green",
            employee_email="dev@company.example",
        )
        edge_tasks.enqueue_edge_task(self._draft())
        spoofed = edge_tasks.claim_next_task(
            device_id="mac-dev-01",
            department="orange",
            employee_email="dev@company.example",
        )
        self.assertIsNone(spoofed["task"])
        # 用註冊部門 green 認領則成功。
        ok = edge_tasks.claim_next_task(
            device_id="mac-dev-01",
            department="green",
            employee_email="dev@company.example",
        )
        self.assertIsNotNone(ok["task"])

    def test_assigned_task_cannot_be_completed_by_other_device(self):
        from agent_core import edge_tasks

        queued = edge_tasks.enqueue_edge_task(self._draft())

        with self.assertRaises(PermissionError):
            edge_tasks.update_task_status(
                task_id=queued["task"]["task_id"],
                device_id="other-mac",
                status="done",
            )


class EdgeApiTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "RED_EDGE_TASKS_FILE": os.path.join(self._tmpdir.name, "edge_tasks.json"),
                "RED_EDGE_AGENT_TOKEN": "edge-token",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmpdir.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module

        return TestClient(app_module.app)

    def test_edge_api_requires_token(self):
        client = self._client()

        response = client.post("/api/edge/register", json={})

        self.assertEqual(response.status_code, 401)

    def test_edge_api_register_poll_and_status(self):
        from agent_core import edge_tasks

        queued = edge_tasks.enqueue_edge_task(
            self._draft(),
            target_device_id="mac-dev-01",
        )
        task_id = queued["task"]["task_id"]
        client = self._client()
        headers = {"x-edge-token": "edge-token"}

        register = client.post(
            "/api/edge/register",
            headers=headers,
            json={
                "device_id": "mac-dev-01",
                "department": "green",
                "employee_email": "dev@company.example",
            },
        )
        self.assertEqual(register.status_code, 200)
        self.assertTrue(register.json()["ok"])

        poll = client.post(
            "/api/edge/poll",
            headers=headers,
            json={
                "device_id": "mac-dev-01",
                "department": "green",
                "employee_email": "dev@company.example",
            },
        )
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["task"]["task_id"], task_id)

        status = client.post(
            f"/api/edge/tasks/{task_id}/status",
            headers=headers,
            json={"device_id": "mac-dev-01", "status": "done", "message": "ok"},
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["task"]["status"], "done")

    def _draft(self):
        from agent_core.agents.green_sample_dev import rpa_tasks

        return rpa_tasks.build_edge_task_draft(
            "green_update_sample_status",
            {"sample_id": "S-DEV-1", "status": "closed"},
            employee_email="dev@company.example",
            device_id="mac-dev-01",
        )


if __name__ == "__main__":
    unittest.main()
