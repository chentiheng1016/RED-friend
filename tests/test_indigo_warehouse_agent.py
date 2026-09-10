"""IndigoWarehouseAgent real implementation tests."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class FakeWarehouseManager:
    def __init__(self):
        self.suppliers = [
            SimpleNamespace(id="SUP001", name="Foam Supplier"),
            SimpleNamespace(id="SUP002", name="Thread Vendor"),
        ]
        self.inventory = [
            SimpleNamespace(
                id="INV-EVA",
                name="EVA foam",
                category="foam",
                current_stock=6,
                reorder_point=10,
                max_stock=100,
                unit_cost=2.5,
                supplier_id="SUP001",
                last_updated=datetime(2026, 5, 21),
            ),
            SimpleNamespace(
                id="INV-EVA-ALT",
                name="EVA foam alt",
                category="foam",
                current_stock=80,
                reorder_point=10,
                max_stock=100,
                unit_cost=2.7,
                supplier_id="SUP001",
                last_updated=datetime(2026, 5, 21),
            ),
            SimpleNamespace(
                id="INV-THREAD",
                name="Yellow thread",
                category="thread",
                current_stock=500,
                reorder_point=50,
                max_stock=600,
                unit_cost=0.3,
                supplier_id="SUP002",
                last_updated=datetime(2026, 5, 21),
            ),
        ]

    def check_inventory_levels(self):
        return [{
            "item_id": "INV-EVA",
            "item_name": "EVA foam",
            "alert_type": "reorder",
            "message": "EVA foam 庫存低於補貨點，需要補貨",
        }]


def _warehouse_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "thread_id": "thread-wh-1",
            "first_message_id": "msg-1",
            "date": "2026-04-17",
            "last_message_date": "2026-04-17",
            "sender": '"井戶良枝" <warehouse-mgr@company.example>',
            "recipients": "twsales@company.example",
            "subject": "2026 Boots Materials - Stock Wrong Part 3",
            "primary_dept": "倉庫",
            "all_depts": json.dumps(["倉庫", "業務"], ensure_ascii=False),
            "direction": "internal",
            "brands": "LURCHI",
            "summary": "修正 2026 靴款庫存數量錯誤，並處理料件報廢與補貨事宜。",
            "entities_json": json.dumps({
                "people": ["UserY"],
                "products": ["NY276", "NY250", "2026 Boots"],
                "customers": ["Lurchi", "Richter", "Pax"],
                "suppliers": ["Jaifung"],
                "amounts": ["7y", "20Y"],
                "dates_mentioned": ["2026-04-17", "ETD5/17"],
                "po_numbers": [],
                "actions": ["取消 NY276 分配並報廢 7y 庫存", "追蹤補貨 ETD"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 3,
            "raw_body_preview": "Stock wrong: cancel allocation and scrap 7y NY276, replenish ETD5/17.",
        },
        {
            "thread_id": "thread-wh-2",
            "first_message_id": "msg-2",
            "date": "2026-04-14",
            "last_message_date": "2026-04-14",
            "sender": '"井戶良枝" <warehouse-mgr@company.example>',
            "recipients": "twpurchase2@company.example",
            "subject": "Jalas [ G604 ZS FIBERGLASS 54800305 (SF23) ] - Received Qty",
            "primary_dept": "倉庫",
            "all_depts": json.dumps(["倉庫"], ensure_ascii=False),
            "direction": "internal",
            "brands": "JALAS",
            "summary": "確認收到 800 雙樣品替換品，但因品質不佳被客戶拒收。",
            "entities_json": json.dumps({
                "people": ["UserY", "UserA"],
                "products": ["G604 ZS FIBERGLASS 54800305 (SF23)"],
                "customers": ["Jalas", "Ejendals"],
                "suppliers": ["ISCO"],
                "amounts": ["800PR"],
                "dates_mentioned": ["2026-04-14"],
                "po_numbers": ["JA251201-02"],
                "actions": ["確認收到 800 雙替換品", "處理品質不佳且不可接受的問題"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 2,
            "raw_body_preview": "Received Qty 800PR, customer rejected due to quality issue.",
        },
        {
            "thread_id": "thread-system-1",
            "first_message_id": "msg-3",
            "date": "2026-04-22",
            "last_message_date": "2026-04-22",
            "sender": "owner@company.example",
            "recipients": "owner@company.example",
            "subject": "【小紅新信】📦 倉庫摘要",
            "primary_dept": "倉庫",
            "all_depts": json.dumps(["倉庫"], ensure_ascii=False),
            "direction": "internal",
            "brands": "",
            "summary": "系統摘要，不能當作真正倉庫信",
            "entities_json": json.dumps({"products": ["SYSTEM-STOCK"]}),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": "Stock received SYSTEM-STOCK",
        },
    ])


class IndigoWarehouseAgentTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import Agent, AgentRegistry, PermissionMiddleware
        from agent_core.agents.indigo_warehouse import IndigoWarehouseAgent

        self.Agent = Agent
        self.fake = FakeWarehouseManager()
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.registry.register(IndigoWarehouseAgent())
        self.manager = mock.patch(
            "agent_core.agents.indigo_warehouse.warehouse._manager",
            return_value=self.fake,
        )
        self.load_df = mock.patch(
            "agent_core.agents.indigo_warehouse.warehouse._load_df",
            return_value=_warehouse_df(),
        )
        self.manager.start()
        self.load_df.start()

    def tearDown(self):
        self.load_df.stop()
        self.manager.stop()

    def _dispatch(self, intent: str, payload: dict | None = None, caller=None):
        from agent_core.agents import AgentRequest

        return self.middleware.dispatch(AgentRequest(
            caller=caller or self.Agent.RED,
            target=self.Agent.INDIGO,
            intent=intent,
            payload=payload or {},
        ))

    def test_profile_lists_indigo_intents(self):
        result = self._dispatch("query.profile")

        profile = result["profile"]
        self.assertEqual(profile["color"], "indigo")
        self.assertIn("倉庫", profile["department"])
        self.assertIn("query.stock_availability", profile["query_intents"])

    def test_list_inventory_filters_low_stock(self):
        result = self._dispatch("query.list_inventory", {"low_stock_only": True})

        self.assertNotEqual(result.get("status"), "stub")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], "INV-EVA")
        self.assertIn("reorder", result["text"])

    def test_stock_availability_returns_alternatives_for_gray(self):
        result = self._dispatch(
            "query.stock_availability",
            {"product": "EVA foam", "alternatives": True},
            caller=self.Agent.GRAY,
        )

        self.assertTrue(result["in_stock"])
        self.assertEqual(result["quantity"], 6)
        self.assertEqual(result["status"], "reorder")
        self.assertIn("EVA foam alt", result["alternatives"])

    def test_stock_availability_not_found_uses_warehouse_records(self):
        result = self._dispatch(
            "query.stock_availability",
            {"product": "NY276", "alternatives": True},
        )

        self.assertFalse(result["in_stock"])
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["warehouse_records"][0]["thread_id"], "thread-wh-1")
        self.assertIn("NY276", result["text"])

    def test_inventory_alerts_from_manager(self):
        result = self._dispatch("query.inventory_alerts")

        self.assertEqual(result["total"], 1)
        self.assertIn("EVA foam", result["text"])

    def test_warehouse_records_filter_and_exclude_system_summary(self):
        result = self._dispatch("query.warehouse_records", {
            "customer": "Jalas",
            "days_back": 3650,
        })

        self.assertEqual(result["total"], 1)
        self.assertIn("JA251201-02", result["text"])
        self.assertNotIn("SYSTEM-STOCK", result["text"])


class IndigoWarehouseTelegramShortcutTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()
        self.fake = FakeWarehouseManager()
        self.manager = mock.patch(
            "agent_core.agents.indigo_warehouse.warehouse._manager",
            return_value=self.fake,
        )
        self.load_df = mock.patch(
            "agent_core.agents.indigo_warehouse.warehouse._load_df",
            return_value=_warehouse_df(),
        )
        self.manager.start()
        self.load_df.start()

    def tearDown(self):
        from agent_core.agents import telegram_command

        self.load_df.stop()
        self.manager.stop()
        telegram_command._reset_for_test()

    def test_is_indigo_warehouse_command(self):
        from agent_core.agents.telegram_command import is_indigo_warehouse_command

        self.assertTrue(is_indigo_warehouse_command("/warehouse stock EVA foam"))
        self.assertTrue(is_indigo_warehouse_command("/indigo inventory"))
        self.assertTrue(is_indigo_warehouse_command("/倉庫 alerts"))
        self.assertFalse(is_indigo_warehouse_command("/dept indigo query.profile"))

    def test_warehouse_stock_shortcut_routes_to_indigo(self):
        from agent_core.agents.telegram_command import handle_indigo_warehouse_command

        result = handle_indigo_warehouse_command("/warehouse stock EVA foam")

        self.assertIn("[indigo / query.stock_availability]", result)
        self.assertIn("EVA foam alt", result)

    def test_warehouse_records_shortcut_routes_to_indigo(self):
        from agent_core.agents.telegram_command import handle_indigo_warehouse_command

        result = handle_indigo_warehouse_command("/warehouse records customer Jalas 3650")

        self.assertIn("[indigo / query.warehouse_records]", result)
        self.assertIn("JA251201-02", result)


if __name__ == "__main__":
    unittest.main()
