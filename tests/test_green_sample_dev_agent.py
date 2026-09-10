"""Phase 2 — GreenSampleDevAgent + shim 測試.

驗證：
  - 舊 import path（agent_core.sample_tracker / daemon_sample_check）仍可用
  - GreenSampleDevAgent 註冊到 registry，被 middleware 路由
  - 各 intent 走得通（透過 mock sample_tracker 狀態檔）
  - permission matrix 仍生效（PURPLE/BLACK/YELLOW 等不能查 GREEN）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class TestBackwardCompatShim(unittest.TestCase):
    """確保舊 import path 不破。"""

    def test_sample_tracker_old_path_still_works(self):
        from agent_core import sample_tracker as old
        # 確認所有 public symbol 都還在
        for name in (
            "track_sample", "list_tracked_samples", "update_sample_status",
            "close_sample", "delete_tracked_sample", "check_sample_deadlines",
            "_load_sample_tracker", "_save_sample_tracker",
            "_SAMPLE_TRACKER_PATH",
        ):
            self.assertTrue(hasattr(old, name), f"shim 缺 {name}")

    def test_daemon_sample_check_old_path_still_works(self):
        from agent_core import daemon_sample_check as old
        self.assertTrue(callable(old.task_sample_check))

    def test_shim_and_real_module_share_state(self):
        # 兩條 import path 拿到的 _load_sample_tracker 應該是同一個 function
        from agent_core import sample_tracker as via_shim
        from agent_core.agents.green_sample_dev import sample_tracker as via_real
        self.assertIs(via_shim._load_sample_tracker, via_real._load_sample_tracker)
        self.assertIs(via_shim.track_sample, via_real.track_sample)


class TestGreenSampleDevAgent(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import (
            Agent, AgentRegistry, PermissionMiddleware,
        )
        from agent_core.agents.green_sample_dev import GreenSampleDevAgent

        self.Agent = Agent
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.green = GreenSampleDevAgent()
        self.registry.register(self.green)

        # 把 sample_tracker 的 state 檔導向 tmp，避免污染真實 state
        self._tmpdir = tempfile.mkdtemp(prefix="red_green_test_")
        self._tmpfile = os.path.join(self._tmpdir, "sample_tracker.json")
        self._edge_file = os.path.join(self._tmpdir, "edge_tasks.json")
        self._edge_env = mock.patch.dict(os.environ, {"RED_EDGE_TASKS_FILE": self._edge_file}, clear=False)
        self._edge_env.start()
        from agent_core.agents.green_sample_dev import sample_tracker as st
        self._orig_path = st._SAMPLE_TRACKER_PATH
        st._SAMPLE_TRACKER_PATH = self._tmpfile

    def tearDown(self):
        from agent_core.agents.green_sample_dev import sample_tracker as st
        st._SAMPLE_TRACKER_PATH = self._orig_path
        self._edge_env.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_query_sample_status_not_found(self):
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.sample_status", payload={"sample_id": "S-NOT-EXIST"},
        ))
        self.assertFalse(result["found"])
        self.assertIsNone(result["entry"])

    def test_query_sample_status_found(self):
        # 先塞一筆
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump({
                "samples": {
                    "S-2026-01": {
                        "sample_id": "S-2026-01", "customer": "Decathlon",
                        "status": "open", "expected_feedback_date": "2026-05-10",
                    },
                },
                "updated_at": "2026-04-30T10:00:00",
            }, f)

        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.ORANGE, target=self.Agent.GREEN,
            intent="query.sample_status", payload={"sample_id": "S-2026-01"},
        ))
        self.assertTrue(result["found"])
        self.assertEqual(result["entry"]["customer"], "Decathlon")

    def test_query_list_samples(self):
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.list_samples", payload={"status": "open"},
        ))
        # 空 state → 預期回 prompt 字串
        self.assertIn("text", result)
        self.assertIn("沒有追蹤中樣品", result["text"])

    def test_query_profile_exposes_green_rpa_contract(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.profile", payload={},
        ))

        profile = result["profile"]
        self.assertEqual(profile["color"], "green")
        self.assertEqual(profile["department"], "樣品室")
        self.assertIn("sample_master", {m["id"] for m in profile["erp_modules"]})
        self.assertIn("刪除樣品主檔", profile["forbidden_actions"])

    def test_query_rpa_recipes_lists_development_recipes(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.rpa_recipes", payload={},
        ))

        recipe_ids = {recipe["id"] for recipe in result["recipes"]}
        self.assertIn("green_create_sample_record", recipe_ids)
        self.assertIn("green_update_sample_status", recipe_ids)

    def test_query_rpa_recipe_returns_recipe_and_required_fields(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.rpa_recipe",
            payload={"recipe_id": "green_create_sample_record"},
        ))

        self.assertTrue(result["found"])
        recipe = result["recipe"]
        self.assertTrue(recipe["confirmation"]["before_submit"])
        self.assertIn("sample_id", recipe["required_fields"])
        self.assertIn("before_submit_screenshot", recipe["audit"])

    def test_query_rpa_recipe_missing_recipe_id(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.rpa_recipe",
            payload={},
        ))

        self.assertEqual(result["error"], "missing recipe_id")

    def test_query_validate_rpa_payload_accepts_complete_create_sample(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.validate_rpa_payload",
            payload={
                "recipe_id": "green_create_sample_record",
                "data": {
                    "sample_id": " S-DEV-1 ",
                    "customer": "Jaifung",
                    "description": "New outsole sample",
                    "sent_date": "2026-05-20",
                    "expected_feedback_date": "2026-05-27",
                    "notes": "first trial",
                },
            },
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(result["normalized_payload"]["sample_id"], "S-DEV-1")
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["unknown_fields"], [])
        self.assertEqual(result["invalid_fields"], [])

    def test_query_validate_rpa_payload_reports_missing_fields(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.validate_rpa_payload",
            payload={
                "recipe_id": "green_create_sample_record",
                "data": {
                    "sample_id": "S-DEV-2",
                    "sent_date": "2026-05-20",
                },
            },
        ))

        self.assertFalse(result["ok"])
        self.assertIn("customer", result["missing_fields"])
        self.assertIn("description", result["missing_fields"])
        self.assertIn("expected_feedback_date", result["missing_fields"])

    def test_query_validate_rpa_payload_rejects_bad_status(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.validate_rpa_payload",
            payload={
                "recipe_id": "green_update_sample_status",
                "sample_id": "S-DEV-1",
                "status": "cancelled",
            },
        ))

        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_fields"], [])
        self.assertIn("status", {item["field"] for item in result["invalid_fields"]})

    def test_query_validate_rpa_payload_rejects_unknown_fields(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.validate_rpa_payload",
            payload={
                "recipe_id": "green_update_sample_status",
                "data": {
                    "sample_id": "S-DEV-1",
                    "status": "closed",
                    "price": "999",
                },
            },
        ))

        self.assertFalse(result["ok"])
        self.assertEqual(result["unknown_fields"], ["price"])

    def test_query_edge_task_draft_returns_safe_draft(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.edge_task_draft",
            payload={
                "recipe_id": "green_update_sample_status",
                "data": {
                    "sample_id": "S-DEV-1",
                    "status": " Feedback_Received ",
                    "note": "客戶已回饋",
                },
                "employee_email": "dev@company.example",
                "device_id": "mac-dev-01",
                "requested_by": "owner@company.example",
            },
        ))

        self.assertTrue(result["ok"])
        task = result["task"]
        self.assertEqual(task["type"], "edge_rpa")
        self.assertEqual(task["department"], "green")
        self.assertEqual(task["status"], "draft")
        self.assertEqual(task["recipe_id"], "green_update_sample_status")
        self.assertTrue(task["requires_confirmation"]["before_submit"])
        self.assertEqual(task["payload"]["status"], "feedback_received")
        self.assertEqual(task["employee_email"], "dev@company.example")

    def test_command_enqueue_edge_task_then_query_queue(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="command.enqueue_edge_task",
            payload={
                "recipe_id": "green_update_sample_status",
                "data": {
                    "sample_id": "S-DEV-1",
                    "status": "closed",
                },
                "employee_email": "dev@company.example",
                "device_id": "mac-dev-01",
                "requested_by": "owner@company.example",
            },
        ))

        self.assertTrue(result["ok"])
        task = result["task"]
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["department"], "green")
        self.assertEqual(task["target_device_id"], "mac-dev-01")

        listed = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.edge_tasks",
            payload={"status": "queued"},
        ))
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["tasks"][0]["task_id"], task["task_id"])

    def test_query_edge_task_requires_task_id(self):
        from agent_core.agents import AgentRequest

        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.edge_task",
            payload={},
        ))

        self.assertEqual(result["error"], "missing task_id")

    def test_command_track_sample_then_query(self):
        from agent_core.agents import AgentRequest

        # 用 command.track_sample 塞一筆
        track_result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="command.track_sample",
            payload={
                "sample_id": "S-TEST-1",
                "customer": "Richter",
                "description": "Black PU sneaker, US 9",
            },
        ))
        self.assertIn("✅", track_result["text"])

        # 再用 query.sample_status 確認查得到
        q = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.GREEN,
            intent="query.sample_status", payload={"sample_id": "S-TEST-1"},
        ))
        self.assertTrue(q["found"])
        self.assertEqual(q["entry"]["customer"], "Richter")

    def test_unknown_intent_raises(self):
        from agent_core.agents import AgentRequest
        with self.assertRaises(ValueError):
            self.middleware.dispatch(AgentRequest(
                caller=self.Agent.RED, target=self.Agent.GREEN,
                intent="totally.unknown", payload={},
            ))

    def test_command_check_deadlines_no_samples(self):
        # mock telegram_push so 不真的打 API（即使 push_telegram=False 也保險）
        from agent_core.agents import AgentRequest
        with mock.patch(
            "agent_core.agents.green_sample_dev.sample_tracker.telegram_push",
            return_value=None,
        ):
            result = self.middleware.dispatch(AgentRequest(
                caller=self.Agent.RED, target=self.Agent.GREEN,
                intent="command.check_deadlines",
                payload={"auto_draft_followup": False, "push_telegram": False},
            ))
        self.assertIn("✅", result["text"])

    # ---- 矩陣防線：不允許的 caller 必須被擋 ----

    def test_blue_cannot_query_green(self):
        # 規格：Blue → Orange, Yellow, Indigo, Purple, Gray, White（不含 Green）
        from agent_core.agents import AgentRequest, PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.middleware.dispatch(AgentRequest(
                caller=self.Agent.BLUE, target=self.Agent.GREEN,
                intent="query.sample_status", payload={"sample_id": "x"},
            ))

    def test_yellow_cannot_query_green(self):
        from agent_core.agents import AgentRequest, PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.middleware.dispatch(AgentRequest(
                caller=self.Agent.YELLOW, target=self.Agent.GREEN,
                intent="query.sample_status", payload={"sample_id": "x"},
            ))

    def test_orange_can_query_green(self):
        # 規格：Orange → Green 在矩陣內 → 應通過 permission；空 state 拿到 found=False
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.ORANGE, target=self.Agent.GREEN,
            intent="query.sample_status", payload={"sample_id": "S-x"},
        ))
        self.assertFalse(result["found"])

    def test_indigo_can_query_green(self):
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.INDIGO, target=self.Agent.GREEN,
            intent="query.sample_status", payload={"sample_id": "S-x"},
        ))
        self.assertFalse(result["found"])


if __name__ == "__main__":
    unittest.main()
