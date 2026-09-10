from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_POLICY_ENGINE_BACKEND": "postgres",
}


class OperationalPolicyEngineTests(unittest.TestCase):
    def setUp(self):
        from agent_core import policy_engine

        policy_engine._PG_POLICY_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_policy_switch(self):
        from agent_core import operational_policy_engine as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_evaluate_policy_writes_postgres_backend(self):
        from agent_core import policy_engine

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_policy_engine.write_decision",
                ) as write_decision:
            decision = policy_engine.evaluate_policy(
                "send_gmail",
                channel="telegram",
                source="sub_agent",
                user="green-agent",
                kwargs={"to": "x@y.com", "subject": "s", "body": "b"},
            )

        self.assertTrue(decision.allow)
        write_decision.assert_called_once()
        record = write_decision.call_args.args[0]
        self.assertEqual(record["tool"], "send_gmail")
        self.assertEqual(record["channel"], "telegram")
        self.assertEqual(record["source"], "sub_agent")
        self.assertEqual(record["user"], "green-agent")
        self.assertTrue(record["allow"])

    def test_policy_summary_reads_postgres_backend(self):
        from agent_core import policy_engine

        rows = [
            {"tool": "send_gmail", "allow": True, "reason_layer": "tier"},
            {"tool": "set_vault_secret", "allow": False, "reason_layer": "tier"},
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_policy_engine.load_decisions",
                    return_value=rows,
                ) as load_decisions:
            summary = policy_engine.policy_summary(hours=6)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["allowed"], 1)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["by_layer"], {"tier": 1})
        self.assertEqual(summary["by_tool"], {"send_gmail": 1, "set_vault_secret": 1})
        load_decisions.assert_called_once_with(hours=6, limit=50000)

    def test_policy_recent_reads_postgres_backend(self):
        from agent_core import policy_engine

        rows = [
            {
                "at": "2026-06-24T12:00:00",
                "tool": "set_vault_secret",
                "allow": False,
                "reason_layer": "tier",
                "reason": "LOCKED tool",
            },
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_policy_engine.load_decisions",
                    return_value=rows,
                ) as load_decisions:
            out = policy_engine.policy_recent(hours=6, limit=5)

        self.assertIn("Policy 決策", out)
        self.assertIn("set_vault_secret", out)
        self.assertIn("LOCKED tool", out)
        load_decisions.assert_called_once_with(hours=6, limit=50000)


if __name__ == "__main__":
    unittest.main()
