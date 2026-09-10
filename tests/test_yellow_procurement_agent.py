from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class FakeSupplyChainManager:
    def __init__(self):
        today = datetime(2026, 5, 21)
        self.suppliers = [
            SimpleNamespace(
                id="SUP001",
                name="Foam Supplier",
                category="raw_materials",
                contact_info={"email": "foam@example.com"},
                performance_score=0.92,
                last_evaluation=None,
            ),
            SimpleNamespace(
                id="SUP002",
                name="Thread Vendor",
                category="components",
                contact_info={"email": "thread@example.com"},
                performance_score=0.81,
                last_evaluation=None,
            ),
        ]
        self.purchase_orders = [
            SimpleNamespace(
                id="PO100",
                supplier_id="SUP001",
                items=[{"name": "EVA foam", "quantity": 500, "unit_price": 2.5}],
                order_date=today - timedelta(days=2),
                expected_delivery=today + timedelta(days=5),
                actual_delivery=None,
                status="confirmed",
                total_value=1250.0,
            ),
            SimpleNamespace(
                id="PO200",
                supplier_id="SUP002",
                items=[{"name": "Yellow thread", "quantity": 1000, "unit_price": 0.3}],
                order_date=today - timedelta(days=12),
                expected_delivery=today - timedelta(days=1),
                actual_delivery=None,
                status="delayed",
                total_value=300.0,
            ),
        ]
        self.created = []
        self.updated = []

    def evaluate_supplier_performance(self, supplier_id):
        return {
            "supplier_id": supplier_id,
            "supplier_name": "Foam Supplier",
            "performance_score": 0.93,
            "total_orders": 4,
            "delivered_orders": 3,
            "on_time_delivery_rate": 0.75,
            "average_delay_days": 1.5,
        }

    def check_inventory_levels(self):
        return [{
            "item_id": "INV001",
            "item_name": "EVA foam",
            "message": "EVA foam 庫存低於補貨點，需要補貨",
        }]

    def get_supply_chain_risks(self):
        return [{
            "risk_type": "delivery_delays",
            "severity": "medium",
            "recommendation": "審查延遲訂單",
        }]

    def generate_supply_chain_report(self):
        return "供應鏈管理報告"

    def create_purchase_order(self, supplier_id, items, expected_delivery_days=7):
        order = SimpleNamespace(
            id="PO300",
            supplier_id=supplier_id,
            items=items,
            order_date=datetime(2026, 5, 21),
            expected_delivery=datetime(2026, 5, 21) + timedelta(days=expected_delivery_days),
            actual_delivery=None,
            status="pending",
            total_value=sum(item["quantity"] * item["unit_price"] for item in items),
        )
        self.created.append(order)
        self.purchase_orders.append(order)
        return order

    def update_order_status(self, order_id, status, actual_delivery=None):
        self.updated.append((order_id, status, actual_delivery))
        return order_id == "PO100"


class YellowProcurementAgentTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import AgentRegistry, PermissionMiddleware
        from agent_core.agents.permission_matrix import Agent
        from agent_core.agents.yellow_procurement import YellowProcurementAgent

        self.Agent = Agent
        self.fake = FakeSupplyChainManager()
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.registry.register(YellowProcurementAgent())
        patcher = mock.patch(
            "agent_core.agents.yellow_procurement.procurement._manager",
            return_value=self.fake,
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def _dispatch(self, intent, payload=None, caller=None):
        from agent_core.agents import AgentRequest

        return self.middleware.dispatch(AgentRequest(
            caller=caller or self.Agent.RED,
            target=self.Agent.YELLOW,
            intent=intent,
            payload=payload or {},
        ))

    def test_profile_lists_yellow_intents(self):
        result = self._dispatch("query.profile")

        profile = result["profile"]
        self.assertEqual(profile["color"], "yellow")
        self.assertIn("query.procurement_eta", profile["query_intents"])
        self.assertIn("command.create_purchase_order", profile["command_intents"])

    def test_list_purchase_orders_filters_delayed(self):
        result = self._dispatch(
            "query.list_purchase_orders",
            {"status": "delayed"},
        )

        self.assertNotEqual(result.get("status"), "stub")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["orders"][0]["id"], "PO200")
        self.assertIn("PO200", result["text"])

    def test_purchase_order_detail(self):
        result = self._dispatch("query.purchase_order", {"order_id": "PO100"})

        self.assertTrue(result["found"])
        self.assertEqual(result["order"]["supplier_name"], "Foam Supplier")
        self.assertIn("EVA foam", result["text"])

    def test_procurement_eta_returns_eta_for_gray_cross_query(self):
        result = self._dispatch(
            "query.procurement_eta",
            {"order_id": "PO100"},
            caller=self.Agent.GRAY,
        )

        self.assertEqual(result["eta"], "2026-05-26")
        self.assertEqual(result["order_id"], "PO100")
        self.assertIn("ETA 2026-05-26", result["text"])

    def test_suppliers_and_alerts(self):
        suppliers = self._dispatch("query.suppliers", {"min_score": 0.9})
        alerts = self._dispatch("query.inventory_alerts")
        risks = self._dispatch("query.risks")

        self.assertEqual(suppliers["total"], 1)
        self.assertEqual(suppliers["suppliers"][0]["id"], "SUP001")
        self.assertEqual(alerts["total"], 1)
        self.assertEqual(risks["total"], 1)

    def test_commands_route_to_manager(self):
        created = self._dispatch(
            "command.create_purchase_order",
            {
                "supplier_id": "SUP001",
                "items": [{"name": "Foam", "quantity": 10, "unit_price": 2}],
                "expected_delivery_days": 9,
            },
        )
        updated = self._dispatch(
            "command.update_order_status",
            {"order_id": "PO100", "status": "shipped"},
        )

        self.assertTrue(created["ok"])
        self.assertEqual(created["order"]["id"], "PO300")
        self.assertTrue(updated["ok"])
        self.assertEqual(self.fake.updated[0][0], "PO100")

    def test_procurement_records_uses_email_timeline(self):
        with mock.patch(
            "agent_core.email_timeline.query_po_timeline",
            return_value="PO timeline",
        ) as timeline:
            result = self._dispatch(
                "query.procurement_records",
                {"po_number": "JF001", "max_events": 5},
            )

        self.assertEqual(result["text"], "PO timeline")
        timeline.assert_called_once_with("JF001", max_events=5)

    def test_telegram_shortcut_routes_purchase_eta(self):
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()
        self.addCleanup(telegram_command._reset_for_test)

        result = telegram_command.handle_yellow_procurement_command(
            "/purchase eta PO100",
            caller=self.Agent.YELLOW,
        )

        self.assertIn("yellow / query.procurement_eta", result)
        self.assertIn("ETA 2026-05-26", result)

    def test_unknown_intent_raises(self):
        with self.assertRaises(ValueError):
            self._dispatch("query.nope")


if __name__ == "__main__":
    unittest.main()
