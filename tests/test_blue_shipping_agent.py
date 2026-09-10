"""BlueShippingAgent real implementation tests."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _shipping_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "thread_id": "thread-ship-1",
            "first_message_id": "msg-1",
            "date": "2026-04-21",
            "last_message_date": "2026-04-21",
            "sender": "UserA <twpurchase2@company.example>",
            "recipients": "shipping@company.example",
            "subject": (
                "28 PACKAGES -- LOT 101-2026 FOR LURCHI/RICHTER, "
                "久靈快遞 AWB: 902165975, ETD: 4/20, ETA(FU CHUN): 4/28"
            ),
            "primary_dept": "船務",
            "all_depts": json.dumps(["船務", "採購"], ensure_ascii=False),
            "direction": "outbound",
            "brands": "LURCHI",
            "summary": "出貨通知：LOT 101-2026 已發貨，預計 4/28 到貨",
            "entities_json": json.dumps({
                "people": ["UserA"],
                "products": ["LOT 101-2026", "28 PACKAGES"],
                "customers": ["LURCHI/RICHTER", "FU CHUN"],
                "dates_mentioned": ["4/20", "4/28"],
                "po_numbers": ["LJF26040067"],
                "actions": ["參考隨附貨運單據"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": (
                "收料單: LJF26040067 28 PACKAGES AWB: 902165975, "
                "ETD: 4/20, ETA(FU CHUN): 4/28"
            ),
        },
        {
            "thread_id": "thread-ship-2",
            "first_message_id": "msg-2",
            "date": "2026-04-18",
            "last_message_date": "2026-04-18",
            "sender": "shipping@company.example",
            "recipients": "sales@company.example",
            "subject": "Container WHLU1234567 delayed for JALAS PO JF0P26040054",
            "primary_dept": "船務",
            "all_depts": json.dumps(["船務"], ensure_ascii=False),
            "direction": "outbound",
            "brands": "JALAS",
            "summary": "櫃號 WHLU1234567 延誤，尚未取得新版 ETA",
            "entities_json": json.dumps({
                "people": ["Shipping"],
                "products": ["bulk shoes"],
                "customers": ["JALAS"],
                "dates_mentioned": ["4/18"],
                "po_numbers": ["JF0P26040054"],
                "actions": ["等待 forwarder 更新 ETA"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 2,
            "raw_body_preview": "Container WHLU1234567 delayed. ETD: 4/18. ETA pending.",
        },
        {
            "thread_id": "thread-purchase-1",
            "first_message_id": "msg-3",
            "date": "2026-04-17",
            "last_message_date": "2026-04-17",
            "sender": "buyer@example.com",
            "recipients": "twpurchase@company.example",
            "subject": "採購單確認",
            "primary_dept": "採購",
            "all_depts": json.dumps(["採購"], ensure_ascii=False),
            "direction": "inbound",
            "brands": "",
            "summary": "供應商確認採購單",
            "entities_json": json.dumps({"po_numbers": ["PO-ONLY-PURCHASE"]}),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": "purchase only",
        },
        {
            "thread_id": "thread-system-1",
            "first_message_id": "msg-4",
            "date": "2026-04-22",
            "last_message_date": "2026-04-22",
            "sender": "owner@company.example",
            "recipients": "owner@company.example",
            "subject": "【小紅新信】🔴1 急件 + 📦1 業務",
            "primary_dept": "船務",
            "all_depts": json.dumps(["船務"], ensure_ascii=False),
            "direction": "internal",
            "brands": "",
            "summary": "系統摘要，不能當作真正船務信",
            "entities_json": json.dumps({"po_numbers": ["SYSTEM-PO"]}),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": "ETD: 4/22 ETA: 4/29",
        },
    ])


class BlueShippingAgentTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import Agent, AgentRegistry, PermissionMiddleware
        from agent_core.agents.blue_shipping import BlueShippingAgent

        self.Agent = Agent
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.registry.register(BlueShippingAgent())
        self.load_df = mock.patch(
            "agent_core.agents.blue_shipping.shipping._load_df",
            return_value=_shipping_df(),
        )
        self.load_df.start()

    def tearDown(self):
        self.load_df.stop()

    def _dispatch(self, intent: str, payload: dict | None = None):
        from agent_core.agents import AgentRequest

        return self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED,
            target=self.Agent.BLUE,
            intent=intent,
            payload=payload or {},
        ))

    def test_profile_lists_blue_shipping_intents(self):
        result = self._dispatch("query.profile")

        profile = result["profile"]
        self.assertEqual(profile["color"], "blue")
        self.assertIn("船務", profile["department"])
        self.assertIn("query.list_shipments", profile["query_intents"])

    def test_list_shipments_uses_shipping_rows_and_parses_eta_awb(self):
        result = self._dispatch("query.list_shipments", {"days_back": 3650})

        self.assertNotEqual(result.get("status"), "stub")
        self.assertEqual(result["total"], 2)
        self.assertIn("LJF26040067", result["text"])
        self.assertIn("ETA 4/28", result["text"])
        first = next(row for row in result["shipments"] if row["thread_id"] == "thread-ship-1")
        self.assertEqual(first["awb_numbers"], ["902165975"])
        self.assertEqual(first["eta"], "4/28")

    def test_shipping_eta_by_po(self):
        result = self._dispatch("query.shipping_eta", {
            "po_number": "LJF26040067",
            "days_back": 3650,
        })

        self.assertEqual(result["eta"], "4/28")
        self.assertIn("LJF26040067", result["text"])

    def test_get_shipment_by_awb(self):
        result = self._dispatch("query.shipment", {"identifier": "902165975"})

        self.assertTrue(result["found"])
        self.assertIn("AWB: 902165975", result["text"])

    def test_shipping_alerts_include_delayed_container(self):
        result = self._dispatch("query.shipping_alerts", {"days_back": 3650})

        self.assertEqual(result["total"], 1)
        self.assertIn("WHLU1234567", result["text"])

    def test_shipping_records_by_customer(self):
        result = self._dispatch("query.shipping_records", {
            "customer": "JALAS",
            "days_back": 3650,
        })

        self.assertEqual(result["total"], 1)
        self.assertIn("JF0P26040054", result["text"])


class BlueShippingTelegramShortcutTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()
        self.load_df = mock.patch(
            "agent_core.agents.blue_shipping.shipping._load_df",
            return_value=_shipping_df(),
        )
        self.load_df.start()

    def tearDown(self):
        from agent_core.agents import telegram_command

        self.load_df.stop()
        telegram_command._reset_for_test()

    def test_is_blue_shipping_command(self):
        from agent_core.agents.telegram_command import is_blue_shipping_command

        self.assertTrue(is_blue_shipping_command("/shipping eta LJF26040067"))
        self.assertTrue(is_blue_shipping_command("/blue shipments"))
        self.assertTrue(is_blue_shipping_command("/船務 alerts"))
        self.assertFalse(is_blue_shipping_command("/dept blue query.profile"))

    def test_shipping_eta_shortcut_routes_to_blue(self):
        from agent_core.agents.telegram_command import handle_blue_shipping_command

        result = handle_blue_shipping_command("/shipping eta LJF26040067")

        self.assertIn("[blue / query.shipping_eta]", result)
        self.assertIn("ETA 4/28", result)

    def test_shipping_records_shortcut_routes_to_blue(self):
        from agent_core.agents.telegram_command import handle_blue_shipping_command

        result = handle_blue_shipping_command("/shipping records customer JALAS 3650")

        self.assertIn("[blue / query.shipping_records]", result)
        self.assertIn("JF0P26040054", result)


if __name__ == "__main__":
    unittest.main()
