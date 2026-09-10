"""PurpleAccountingAgent real implementation tests."""
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


def _accounting_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "thread_id": "thread-acct-1",
            "first_message_id": "msg-1",
            "date": "2026-04-10",
            "last_message_date": "2026-04-10",
            "sender": "fx-desk@bank.example",
            "recipients": "accounting-vn@company.example",
            "subject": "第一銀行 國外匯入匯款通知",
            "primary_dept": "會計",
            "all_depts": json.dumps(["會計"], ensure_ascii=False),
            "direction": "inbound",
            "brands": "JALAS",
            "summary": "收到第一銀行 USD 10,983.40 匯款通知，生效日期為 2026/04/14",
            "entities_json": json.dumps({
                "people": ["Owner"],
                "products": [],
                "customers": ["JAI JYE CORPORATION"],
                "suppliers": ["EJENDALS SUOMI OY", "第一銀行"],
                "amounts": ["10,983.40 USD"],
                "dates_mentioned": ["2026/04/10", "2026/04/14"],
                "po_numbers": ["JL250326A"],
                "actions": ["收到匯款通知"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": "Forex inward remittance notice USD 10,983.40 JL250326A",
        },
        {
            "thread_id": "thread-acct-2",
            "first_message_id": "msg-2",
            "date": "2026-04-07",
            "last_message_date": "2026-04-07",
            "sender": '"張苑蘭" <accounting-vn@company.example>',
            "recipients": "owner@company.example",
            "subject": "COSMO & FULLTITE 預付款",
            "primary_dept": "會計",
            "all_depts": json.dumps(["會計", "採購"], ensure_ascii=False),
            "direction": "internal",
            "brands": "DECATHLON",
            "summary": "Cosmostar 和 Fulltide 預付款項支付時間協調。",
            "entities_json": json.dumps({
                "people": ["張苑蘭", "Owner"],
                "products": ["Cosmostar", "Fulltide"],
                "customers": ["Decathlon"],
                "suppliers": ["Fulltide"],
                "amounts": ["13,464.96 USD", "20,771.61 USD"],
                "dates_mentioned": ["4/9", "4/15"],
                "po_numbers": ["JFPP2603009", "JF0P26020009"],
                "actions": ["協調付款時間", "提供匯款單", "匯款"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 3,
            "raw_body_preview": "Prepayment for Fulltide, arrange payment and remittance form.",
        },
        {
            "thread_id": "thread-acct-3",
            "first_message_id": "msg-3",
            "date": "2026-04-14",
            "last_message_date": "2026-04-14",
            "sender": '"中華電信電子帳單" <cht_ebpp@cht.com.tw>',
            "recipients": "owner@company.example",
            "subject": "中華電信115年4月電信費用通知單[郵件編號:437571750]",
            "primary_dept": "會計",
            "all_depts": json.dumps(["會計"], ensure_ascii=False),
            "direction": "inbound",
            "brands": "",
            "summary": "中華電信115年4月電信帳單通知。",
            "entities_json": json.dumps({
                "people": [],
                "products": ["電信服務"],
                "customers": ["中華電信"],
                "suppliers": ["中華電信"],
                "amounts": ["1,234元"],
                "dates_mentioned": ["115年4月"],
                "po_numbers": ["437571750"],
                "actions": ["開啟附加檔案瀏覽本期帳單"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": "billing notice invoice 1,234元",
        },
        {
            "thread_id": "thread-acct-4",
            "first_message_id": "msg-4",
            "date": "2026-04-08",
            "last_message_date": "2026-04-08",
            "sender": "service@bank.example",
            "recipients": "accounting-vn@company.example",
            "subject": "銀行告知帳戶名稱有誤",
            "primary_dept": "會計",
            "all_depts": json.dumps(["會計"], ensure_ascii=False),
            "direction": "inbound",
            "brands": "",
            "summary": "付款資料帳戶名稱有誤，需修正後重送。",
            "entities_json": json.dumps({
                "customers": ["鼎匯"],
                "suppliers": ["銀行"],
                "amounts": ["50,000 USD"],
                "po_numbers": [],
                "actions": ["修正帳戶名稱"],
            }, ensure_ascii=False),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": "account name error, payment rejected",
        },
        {
            "thread_id": "thread-system-1",
            "first_message_id": "msg-5",
            "date": "2026-04-22",
            "last_message_date": "2026-04-22",
            "sender": "owner@company.example",
            "recipients": "owner@company.example",
            "subject": "【小紅新信】會計摘要",
            "primary_dept": "會計",
            "all_depts": json.dumps(["會計"], ensure_ascii=False),
            "direction": "internal",
            "brands": "",
            "summary": "系統摘要，不能當作真正會計信",
            "entities_json": json.dumps({"amounts": ["999 USD"], "po_numbers": ["SYSTEM-PO"]}),
            "topic_tags": "",
            "state": "",
            "message_count": 1,
            "raw_body_preview": "invoice payment remittance",
        },
    ])


class PurpleAccountingAgentTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import Agent, AgentRegistry, PermissionMiddleware
        from agent_core.agents.purple_accounting import PurpleAccountingAgent

        self.Agent = Agent
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.registry.register(PurpleAccountingAgent())
        self.load_df = mock.patch(
            "agent_core.agents.purple_accounting.accounting._load_df",
            return_value=_accounting_df(),
        )
        self.load_df.start()

    def tearDown(self):
        self.load_df.stop()

    def _dispatch(self, intent: str, payload: dict | None = None, caller=None):
        from agent_core.agents import AgentRequest

        return self.middleware.dispatch(AgentRequest(
            caller=caller or self.Agent.RED,
            target=self.Agent.PURPLE,
            intent=intent,
            payload=payload or {},
        ))

    def test_profile_lists_purple_intents(self):
        result = self._dispatch("query.profile")

        profile = result["profile"]
        self.assertEqual(profile["color"], "purple")
        self.assertIn("會計", profile["department"])
        self.assertIn("query.payment_records", profile["query_intents"])

    def test_list_accounts_alias_is_real_and_excludes_system_summary(self):
        result = self._dispatch("query.list_accounts", {"days_back": 3650})

        self.assertNotEqual(result.get("status"), "stub")
        self.assertEqual(result["total"], 4)
        self.assertIn("10,983.40 USD", result["text"])
        self.assertNotIn("SYSTEM-PO", result["text"])

    def test_remittance_records(self):
        result = self._dispatch("query.remittance_records", {
            "keyword": "JL250326A",
            "days_back": 3650,
        })

        self.assertEqual(result["total"], 1)
        self.assertIn("received", result["text"])
        self.assertIn("10,983.40 USD", result["text"])

    def test_payment_records_by_po(self):
        result = self._dispatch("query.payment_records", {
            "po_number": "JFPP2603009",
            "days_back": 3650,
        })

        self.assertEqual(result["total"], 1)
        self.assertIn("Fulltide", result["text"])
        self.assertIn("outbound", result["text"])

    def test_invoice_records(self):
        result = self._dispatch("query.invoice_records", {
            "keyword": "中華電信",
            "days_back": 3650,
        })

        self.assertEqual(result["total"], 1)
        self.assertIn("437571750", result["text"])
        self.assertIn("invoice", result["text"])

    def test_accounting_alerts_include_attention(self):
        result = self._dispatch("query.accounting_alerts", {"days_back": 3650})

        self.assertGreaterEqual(result["total"], 1)
        self.assertIn("帳戶名稱有誤", result["text"])


class PurpleAccountingTelegramShortcutTests(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()
        self.load_df = mock.patch(
            "agent_core.agents.purple_accounting.accounting._load_df",
            return_value=_accounting_df(),
        )
        self.load_df.start()

    def tearDown(self):
        from agent_core.agents import telegram_command

        self.load_df.stop()
        telegram_command._reset_for_test()

    def test_is_purple_accounting_command(self):
        from agent_core.agents.telegram_command import is_purple_accounting_command

        self.assertTrue(is_purple_accounting_command("/accounting summary"))
        self.assertTrue(is_purple_accounting_command("/purple alerts"))
        self.assertTrue(is_purple_accounting_command("/invoice 中華電信"))
        self.assertTrue(is_purple_accounting_command("/會計 records"))
        self.assertFalse(is_purple_accounting_command("/dept purple query.profile"))

    def test_accounting_invoice_shortcut_routes_to_purple(self):
        from agent_core.agents.telegram_command import handle_purple_accounting_command

        result = handle_purple_accounting_command("/accounting invoices 中華電信")

        self.assertIn("[purple / query.invoice_records]", result)
        self.assertIn("437571750", result)

    def test_payment_direct_shortcut_routes_to_purple(self):
        from agent_core.agents.telegram_command import handle_purple_accounting_command

        result = handle_purple_accounting_command("/payment JFPP2603009")

        self.assertIn("[purple / query.payment_records]", result)
        self.assertIn("Fulltide", result)


if __name__ == "__main__":
    unittest.main()
