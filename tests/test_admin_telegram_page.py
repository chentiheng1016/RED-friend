from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock


class AdminTelegramPageTests(unittest.TestCase):
    def _client(self, user: dict[str, str]):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module

        self._patches = [
            mock.patch.object(app_module, "get_secret_key", return_value="x" * 32),
            mock.patch.object(app_module, "_get_session_user", return_value=user),
            # _require_login 現在會對 registry 重驗 color/is_boss；回一個與 session
            # 同 color 的員工，讓重驗結果等於此測試模擬的身分。
            mock.patch.object(app_module, "get_employee", return_value={
                "email": user.get("email", ""),
                "name": user.get("name", ""),
                "color": user.get("color", "green"),
            }),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop_patches)
        return TestClient(app_module.app)

    def _stop_patches(self):
        for patch in getattr(self, "_patches", []):
            patch.stop()

    def test_admin_telegram_page_shows_audit_and_policy(self):
        from agent_core.telegram_audit import log_telegram_event

        user = {
            "email": "boss@example.com",
            "name": "Boss",
            "color": "red",
            "is_boss": "True",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = os.path.join(tmpdir, "telegram_audit.jsonl")
            registry_path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(
                os.environ,
                {
                    "RED_TELEGRAM_AUDIT_FILE": audit_path,
                    "RED_EMPLOYEE_REGISTRY_FILE": registry_path,
                    "RED_TELEGRAM_AGENT_CHATS": "green:12345",
                },
            ), mock.patch("agent_core.telegram._get_telegram_chat_id", return_value="999"):
                log_telegram_event(
                    event="telegram_update",
                    status="ok",
                    chat_id="12345",
                    text="/whoami",
                    reply="identity",
                    actor={"color": "green", "name": "Alice"},
                    message={
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 12345, "username": "alice"},
                    },
                )
                client = self._client(user)
                response = client.get("/admin/telegram?limit=10")

        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("Telegram 管理", body)
        self.assertIn("綁定診斷", body)
        self.assertIn("指令白名單", body)
        self.assertIn("/dept &lt;color&gt; query.*", body)
        self.assertIn("12345", body)
        self.assertIn("green", body)

    def test_admin_telegram_page_rejects_non_boss(self):
        user = {
            "email": "alice@example.com",
            "name": "Alice",
            "color": "green",
            "is_boss": "False",
        }
        client = self._client(user)

        response = client.get("/admin/telegram")

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
