from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock


class TelegramAgentConfigTests(unittest.TestCase):
    def test_env_agent_chat_bindings_parse_multiple_colors(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.telegram_agent_config import env_agent_chat_bindings

        bindings = env_agent_chat_bindings("green:111|222, orange=333 ; bad:999")

        self.assertEqual(bindings[Agent.GREEN], {"111", "222"})
        self.assertEqual(bindings[Agent.ORANGE], {"333"})
        self.assertNotIn("bad", bindings)

    def test_employee_registry_telegram_ids_become_actors(self):
        from agent_core.web_server import employee_registry
        from agent_core.telegram_agent_config import actor_for_chat_id, authorized_inbound_chat_ids

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            registry_path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": registry_path}):
                employee_registry.register_employee(
                    "alice@example.com",
                    "Alice",
                    "green",
                    telegram_user_id="12345",
                )

                actor = actor_for_chat_id("12345", owner_chat_id="999")
                authorized = authorized_inbound_chat_ids(owner_chat_id="999")

        self.assertEqual(actor["color"], "green")
        self.assertEqual(actor["email"], "alice@example.com")
        self.assertIn("12345", authorized)
        self.assertIn("999", authorized)

    def test_chat_ids_for_agent_uses_env_and_owner_for_red(self):
        from agent_core import telegram_agent_config as cfg
        from agent_core.telegram_agent_config import chat_ids_for_agent

        # live checkout 的 employee registry 把 gm(9990000003) 綁成 red，
        # 會混進 red 的 chat_ids 讓斷言失敗（CI 沒這份 registry 才會過）。本測
        # 只驗 env binding + owner 的合併邏輯，把 registry 來源中性化（與
        # backend / 檔案路徑無關），讓 live 與 CI 都穩定。
        with mock.patch.dict(
            os.environ,
            {"RED_TELEGRAM_AGENT_CHATS": "green:111|222,red:333"},
            clear=False,
        ), mock.patch.object(cfg, "employee_telegram_actors", return_value={}):
            self.assertEqual(chat_ids_for_agent("green", owner_chat_id="999"), {"111", "222"})
            self.assertEqual(chat_ids_for_agent("red", owner_chat_id="999"), {"333", "999"})
            self.assertEqual(
                chat_ids_for_agent("red", owner_chat_id="999", include_owner_for_red=False),
                {"333"},
            )

    def test_binding_diagnostics_reports_duplicates_and_invalid_env_ids(self):
        from agent_core.web_server import employee_registry
        from agent_core.telegram_agent_config import telegram_binding_diagnostics

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(os.environ, {}, clear=True):
            registry_path = os.path.join(tmpdir, "employee_registry.json")
            with mock.patch.dict(
                os.environ,
                {
                    "RED_EMPLOYEE_REGISTRY_FILE": registry_path,
                    "RED_TELEGRAM_AGENT_CHATS": "green:111,orange:abc,white:222",
                },
            ):
                employee_registry.register_employee(
                    "alice@example.com",
                    "Alice",
                    "white",
                    telegram_user_id="111",
                )
                diagnostics = telegram_binding_diagnostics(owner_chat_id="999")

        warnings = "\n".join(diagnostics["warnings"])
        self.assertIn("111 is configured multiple times", warnings)
        self.assertIn("invalid format: abc", warnings)
        statuses = {(row["chat_id"], row["source"]): row["status"] for row in diagnostics["bindings"]}
        self.assertEqual(statuses[("111", "employee_registry")], "active")
        self.assertEqual(statuses[("111", "RED_TELEGRAM_AGENT_CHATS")], "shadowed")
        self.assertEqual(statuses[("abc", "RED_TELEGRAM_AGENT_CHATS")], "invalid")


if __name__ == "__main__":
    unittest.main()
