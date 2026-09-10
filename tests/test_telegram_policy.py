from __future__ import annotations

import unittest


class TelegramPolicyTests(unittest.TestCase):
    def test_policy_rows_include_admin_employee_and_group_rules(self):
        from agent_core.telegram_policy import telegram_command_policy_rows

        rows = telegram_command_policy_rows()
        by_actor: dict[str, list[str]] = {}
        for row in rows:
            by_actor.setdefault(row["actor"], []).append(row["commands"])

        self.assertTrue(any("/ingest" in command for command in by_actor["red"]))
        self.assertTrue(any("/dev enqueue" in command for command in by_actor["green"]))
        self.assertTrue(any("/shipping" in command for command in by_actor["blue"]))
        self.assertTrue(any("/sales" in command for command in by_actor["orange"]))
        self.assertTrue(any("/purchase" in command for command in by_actor["yellow"]))
        self.assertTrue(any("/warehouse" in command for command in by_actor["indigo"]))
        self.assertTrue(any("/accounting" in command for command in by_actor["purple"]))
        self.assertTrue(any("/production" in command for command in by_actor["gray"]))
        self.assertTrue(any("/cashier" in command for command in by_actor["black"]))
        self.assertTrue(any("/legal" in command for command in by_actor["white"]))
        self.assertTrue(any("/dept" in command for command in by_actor["white"]))
        self.assertTrue(any("@BotName" in command for command in by_actor["group chats"]))


if __name__ == "__main__":
    unittest.main()
