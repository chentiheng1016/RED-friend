"""BlackCashierAgent read-only implementation tests."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class BlackCashierAgentTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import Agent, AgentRegistry, PermissionMiddleware
        from agent_core.agents.black_cashier import BlackCashierAgent

        self.Agent = Agent
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.registry.register(BlackCashierAgent())

    def _dispatch(self, intent: str, payload: dict | None = None):
        from agent_core.agents import AgentRequest

        return self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED,
            target=self.Agent.BLACK,
            intent=intent,
            payload=payload or {},
        ))

    def test_profile_is_real_agent(self):
        result = self._dispatch("query.profile")

        self.assertEqual(result["profile"]["color"], "black")
        self.assertIn("Cashier", result["profile"]["department"])
        self.assertNotEqual(result.get("status"), "stub")

    def test_cash_records_delegates_to_cashier_helper(self):
        with mock.patch(
            "agent_core.agents.black_cashier.cashier.cash_records",
            return_value={"text": "[records]", "total": 1},
        ) as helper:
            result = self._dispatch(
                "query.cash_records",
                {"direction": "outbound", "keyword": "Fulltide", "days_back": 30, "limit": 7},
            )

        helper.assert_called_once_with(
            counterparty="",
            po_number="",
            keyword="Fulltide",
            direction="outbound",
            days_back=30,
            limit=7,
        )
        self.assertEqual(result["text"], "[records]")

    def test_list_transactions_alias(self):
        with mock.patch(
            "agent_core.agents.black_cashier.cashier.cash_records",
            return_value={"text": "[transactions]", "total": 1},
        ) as helper:
            result = self._dispatch("query.list_transactions", {"keyword": "第一銀行"})

        helper.assert_called_once()
        self.assertEqual(result["text"], "[transactions]")

    def test_unknown_intent_raises_value_error(self):
        from agent_core.agents import AgentRequest

        with self.assertRaises(ValueError):
            self.middleware.dispatch(AgentRequest(
                caller=self.Agent.RED,
                target=self.Agent.BLACK,
                intent="query.unknown",
                payload={},
            ))


if __name__ == "__main__":
    unittest.main()
