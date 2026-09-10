from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock


class TelegramPreflightTests(unittest.TestCase):
    def test_preflight_reports_missing_token_and_owner(self):
        from agent_core import telegram_preflight

        with mock.patch("agent_core.telegram._get_telegram_token", return_value=""), \
             mock.patch("agent_core.telegram._get_telegram_chat_id", return_value=""), \
             mock.patch.dict(os.environ, {}, clear=True):
            result = telegram_preflight.run_preflight()

        self.assertFalse(result["ok"])
        checks = {item["name"]: item for item in result["checks"]}
        self.assertEqual(checks["bot_token"]["status"], "fail")
        self.assertEqual(checks["owner_chat"]["status"], "fail")
        self.assertEqual(checks["telegram_api"]["status"], "skip")

    def test_preflight_passes_with_owner_and_department_chat(self):
        from agent_core import telegram_preflight

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("agent_core.telegram._get_telegram_token", return_value="123:abc"), \
             mock.patch("agent_core.telegram._get_telegram_chat_id", return_value="999"), \
             mock.patch.dict(
                 os.environ,
                 {
                     "RED_TELEGRAM_AGENT_CHATS": "green:111",
                     "RED_TELEGRAM_AUDIT_FILE": os.path.join(tmpdir, "telegram_audit.jsonl"),
                     "RED_TELEGRAM_BOT_USERNAME": "RedAgent",
                 },
                 clear=True,
             ):
            result = telegram_preflight.run_preflight()

        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(result["ok"])
        self.assertEqual(checks["bot_token"]["status"], "pass")
        self.assertEqual(checks["owner_chat"]["status"], "pass")
        self.assertEqual(checks["department_chats"]["status"], "pass")

    def test_preflight_allows_department_bot_default_private_actor(self):
        from agent_core import telegram_preflight

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("agent_core.telegram._get_telegram_token", return_value="123:abc"), \
             mock.patch("agent_core.telegram._get_telegram_chat_id", return_value=""), \
             mock.patch.dict(
                 os.environ,
                 {
                     "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS": "1",
                     "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "orange",
                     "RED_TELEGRAM_AUDIT_FILE": os.path.join(tmpdir, "telegram_audit.jsonl"),
                     "RED_TELEGRAM_BOT_USERNAME": "OrangeAgent",
                 },
                 clear=True,
             ):
            result = telegram_preflight.run_preflight()

        checks = {item["name"]: item for item in result["checks"]}
        self.assertTrue(result["ok"])
        self.assertEqual(checks["owner_chat"]["status"], "pass")
        self.assertEqual(checks["default_private_actor"]["status"], "pass")
        self.assertEqual(checks["inbound_actors"]["status"], "pass")
        self.assertEqual(checks["department_chats"]["status"], "pass")
        self.assertIn("orange", checks["department_chats"]["detail"])

    def test_preflight_surfaces_binding_warnings(self):
        from agent_core import telegram_preflight
        from agent_core.web_server import employee_registry

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("agent_core.telegram._get_telegram_token", return_value="123:abc"), \
             mock.patch("agent_core.telegram._get_telegram_chat_id", return_value="999"), \
             mock.patch.dict(
                 os.environ,
                 {
                     "RED_EMPLOYEE_REGISTRY_FILE": os.path.join(tmpdir, "employees.json"),
                     "RED_TELEGRAM_AGENT_CHATS": "green:111,orange:notnumeric",
                     "RED_TELEGRAM_AUDIT_FILE": os.path.join(tmpdir, "telegram_audit.jsonl"),
                 },
                 clear=True,
             ):
            employee_registry.register_employee(
                "alice@example.com",
                "Alice",
                "white",
                telegram_user_id="111",
            )
            result = telegram_preflight.run_preflight()

        warning_details = "\n".join(
            item["detail"] for item in result["checks"]
            if item["status"] == "warn"
        )
        self.assertIn("111 is configured multiple times", warning_details)
        self.assertIn("invalid format: notnumeric", warning_details)

    def test_check_api_warns_on_username_mismatch(self):
        from agent_core import telegram_preflight

        class FakeResponse:
            def json(self):
                return {"ok": True, "result": {"username": "ActualBot"}}

        fake_requests = mock.Mock()
        fake_requests.get.return_value = FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("agent_core.telegram._get_telegram_token", return_value="123:abc"), \
             mock.patch("agent_core.telegram._get_telegram_chat_id", return_value="999"), \
             mock.patch.dict(
                 os.environ,
                 {
                     "RED_TELEGRAM_BOT_USERNAME": "ConfiguredBot",
                     "RED_TELEGRAM_AUDIT_FILE": os.path.join(tmpdir, "telegram_audit.jsonl"),
                 },
                 clear=True,
             ):
            result = telegram_preflight.run_preflight(
                check_api=True,
                requests_module=fake_requests,
            )

        checks = [item for item in result["checks"] if item["name"] == "telegram_api"]
        self.assertEqual(checks[-1]["status"], "warn")
        self.assertIn("does not match", checks[-1]["detail"])

    def test_missing_audit_parent_is_warning_when_nearest_parent_writable(self):
        from agent_core import telegram_preflight

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch("agent_core.telegram._get_telegram_token", return_value="123:abc"), \
             mock.patch("agent_core.telegram._get_telegram_chat_id", return_value="999"), \
             mock.patch.dict(
                 os.environ,
                 {
                     "RED_TELEGRAM_AUDIT_FILE": os.path.join(
                         tmpdir,
                         "missing",
                         "telegram_audit.jsonl",
                     ),
                 },
                 clear=True,
             ):
            result = telegram_preflight.run_preflight()

        checks = [item for item in result["checks"] if item["name"] == "audit_path"]
        self.assertEqual(checks[-1]["status"], "warn")
        self.assertIn("does not exist yet", checks[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
