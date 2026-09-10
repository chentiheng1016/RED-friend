"""Phase 3c — OrangeSalesAgent + 4 個 shim 測試.

驗證：
  - 4 條舊 import path（agent_core.{quote, quote_batch, quote_gen,
    customer_intel}）仍可用
  - Shim 與真實模組共用同一個 function object
  - OrangeSalesAgent 接得到（透過 wire.build_default_registry）
  - 已知 query.* 路由到正確函式（mock 真實函式以隔離 IO）
  - 矩陣可達性：Yellow / Green / Blue / Indigo / Purple / Gray / Black 都可查 Orange
  - 未知 intent → ValueError
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class TestOrangeShims(unittest.TestCase):
    """確保 4 條舊 import path 不破。"""

    def test_quote_old_path(self):
        from agent_core import quote as old
        for name in (
            "_QUOTE_DIR", "_QUOTE_CSV", "_QUOTE_EXTRACTED_IDS",
            "_parse_quote_json", "_extract_quote_from_thread",
            "extract_quote_from_email", "build_quote_history",
            "query_quote_history",
        ):
            self.assertTrue(hasattr(old, name), f"shim 缺 {name}")

    def test_quote_batch_old_path(self):
        from agent_core import quote_batch as old
        self.assertTrue(callable(old.batch_extract_quotes_from_parquet))

    def test_quote_gen_old_path(self):
        from agent_core import quote_gen as old
        self.assertTrue(callable(old.generate_quote))

    def test_customer_intel_old_path(self):
        from agent_core import customer_intel as old
        for name in ("customer_360", "list_active_customers", "customer_alerts"):
            self.assertTrue(hasattr(old, name), f"shim 缺 {name}")

    def test_shim_and_real_share_state(self):
        from agent_core import quote as via_shim
        from agent_core.agents.orange_sales import quote as via_real
        self.assertIs(via_shim.query_quote_history, via_real.query_quote_history)
        self.assertIs(via_shim._QUOTE_CSV, via_real._QUOTE_CSV)


class TestOrangeAgent(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import build_default_registry, Agent
        self.Agent = Agent
        self.registry, self.middleware = build_default_registry()

    def _dispatch(self, caller, intent, payload=None):
        from agent_core.agents import AgentRequest
        return self.middleware.dispatch(AgentRequest(
            caller=caller, target=self.Agent.ORANGE,
            intent=intent, payload=payload if payload is not None else {},
        ))

    # ---- registry / 矩陣 ----

    def test_orange_in_registry(self):
        self.assertIn(self.Agent.ORANGE, self.registry)
        from agent_core.agents.orange_sales import OrangeSalesAgent
        self.assertIsInstance(
            self.registry.get(self.Agent.ORANGE),
            OrangeSalesAgent,
        )

    def test_real_agent_does_not_return_stub_status(self):
        # query.active_customers 是純讀，但會觸碰 Gmail — mock 一下
        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.list_active_customers",
            return_value="[mocked active customers]",
        ):
            result = self._dispatch(
                self.Agent.RED, "query.active_customers",
                payload={"days": 30, "min_emails": 1},
            )
        self.assertNotEqual(result.get("status"), "stub")
        self.assertEqual(result["text"], "[mocked active customers]")

    # ---- 矩陣允許的 caller ----

    def test_callers_allowed_by_matrix(self):
        # Yellow / Green / Blue / Indigo / Purple / Gray / Black 矩陣皆含 Orange
        from agent_core.agents import QUERY_MATRIX
        for caller in (self.Agent.YELLOW, self.Agent.GREEN, self.Agent.BLUE,
                       self.Agent.INDIGO, self.Agent.PURPLE, self.Agent.GRAY,
                       self.Agent.BLACK):
            self.assertIn(self.Agent.ORANGE, QUERY_MATRIX[caller],
                          f"規格：{caller.value} 矩陣應含 Orange")

        # 真打一發（mock 函式）
        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.customer_360",
            return_value="[mock 360]",
        ):
            for caller in (self.Agent.YELLOW, self.Agent.PURPLE, self.Agent.GRAY):
                result = self._dispatch(
                    caller, "query.customer_360",
                    payload={"customer": "Decathlon"},
                )
                self.assertEqual(result["text"], "[mock 360]")

    def test_white_cannot_call_orange(self):
        # White 矩陣為空 — 不能呼叫 Orange
        from agent_core.agents import AgentRequest, PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.middleware.dispatch(AgentRequest(
                caller=self.Agent.WHITE, target=self.Agent.ORANGE,
                intent="query.active_customers", payload={},
            ))

    # ---- intent 路由 ----

    def test_query_quote_history_routes(self):
        # 真接到實際 query_quote_history（不 mock），讓 signature mismatch
        # 在測試階段就被 TypeError 抓到 — 之前用 mock 漏掉了 Codex P1。
        with mock.patch(
            "agent_core.agents.orange_sales.quote.query_quote_history",
            wraps=__import__(
                "agent_core.agents.orange_sales.quote",
                fromlist=["query_quote_history"],
            ).query_quote_history,
        ) as m:
            # CSV 不存在也 OK，我們只在乎 signature 對得上、不 raise TypeError
            self._dispatch(
                self.Agent.RED, "query.quote_history",
                payload={"customer": "Richter", "recent_months": 12},
            )
        m.assert_called_once_with(
            customer="Richter", sku="", direction="",
            recent_months=12, expand_aliases=True,
        )

    def test_query_customer_360_routes(self):
        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.customer_360",
            return_value="[mock 360]",
        ) as m:
            result = self._dispatch(
                self.Agent.RED, "query.customer_360",
                payload={"customer": "PAX", "days": 60},
            )
        self.assertEqual(result["text"], "[mock 360]")
        m.assert_called_once_with(customer="PAX", days=60, push_telegram=False)

    def test_query_customer_alerts_routes(self):
        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.customer_alerts",
            return_value="[mock alerts]",
        ) as m:
            result = self._dispatch(
                self.Agent.RED, "query.customer_alerts",
                payload={"days": 7},
            )
        self.assertEqual(result["text"], "[mock alerts]")
        m.assert_called_once_with(days=7)

    def test_command_extract_quote_email_routes(self):
        with mock.patch(
            "agent_core.agents.orange_sales.quote.extract_quote_from_email",
            return_value={"ok": True},
        ) as m:
            result = self._dispatch(
                self.Agent.RED, "command.extract_quote_email",
                payload={"message_id": "msg-123"},
            )
        self.assertEqual(result["result"], {"ok": True})
        m.assert_called_once_with("msg-123")

    def test_command_generate_quote_forwards_kwargs(self):
        with mock.patch(
            "agent_core.agents.orange_sales.quote_gen.generate_quote",
            return_value="quote.xlsx",
        ) as m:
            result = self._dispatch(
                self.Agent.RED, "command.generate_quote",
                payload={"customer": "PAX", "shoe_model": "XR-100",
                         "items": [{"sku": "A1", "qty": 100}]},
            )
        self.assertEqual(result["result"], "quote.xlsx")
        m.assert_called_once_with(
            customer="PAX", shoe_model="XR-100",
            items=[{"sku": "A1", "qty": 100}],
        )

    def test_unknown_intent_raises(self):
        from agent_core.agents import AgentRequest
        with self.assertRaises(ValueError):
            self.middleware.dispatch(AgentRequest(
                caller=self.Agent.RED, target=self.Agent.ORANGE,
                intent="totally.unknown", payload={},
            ))


class TestOrangeSalesAgentGmailRAG(unittest.TestCase):
    """query.search_emails + command.sync_gmail — mock Gmail/ChromaDB。"""

    def setUp(self):
        from agent_core.agents import AgentRegistry, PermissionMiddleware
        from agent_core.agents.orange_sales import OrangeSalesAgent
        from agent_core.agents.permission_matrix import Agent

        self.Agent = Agent
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.registry.register(OrangeSalesAgent())

    def _dispatch(self, intent, payload, caller=None):
        from agent_core.agents import AgentRequest
        return self.middleware.dispatch(AgentRequest(
            caller=caller or self.Agent.RED, target=self.Agent.ORANGE,
            intent=intent, payload=payload,
        ))

    def test_search_emails_empty_query_returns_error(self):
        result = self._dispatch("query.search_emails", {"query": ""})
        self.assertIn("error", result)

    def test_search_emails_calls_vector_store(self):
        from unittest.mock import MagicMock, patch

        fake_store = MagicMock()
        fake_store.query.return_value = [
            {"text": "報價 PAX 2026", "metadata": {"subject": "Quote PAX"}, "distance": 0.08}
        ]
        with patch("agent_core.ingest.vector_store.get_store", return_value=fake_store):
            result = self._dispatch("query.search_emails", {"query": "PAX 報價", "n_results": 3})

        # 大王沒有 ACL 限制，但語意檢索一律排除小紅自產內容（排程報表）。
        fake_store.query.assert_called_once_with(
            "PAX 報價", n_results=3, where={"generated_by_red": {"$ne": True}},
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["text"], "報價 PAX 2026")

    def test_search_emails_department_caller_gets_acl_filter(self):
        from unittest.mock import MagicMock, patch

        fake_store = MagicMock()
        fake_store.query.return_value = []
        with patch("agent_core.ingest.vector_store.get_store", return_value=fake_store):
            result = self._dispatch(
                "query.search_emails",
                {"query": "PAX 報價", "n_results": 3},
                caller=self.Agent.ORANGE,
            )

        fake_store.query.assert_called_once_with(
            "PAX 報價",
            n_results=3,
            where={"$and": [
                {"generated_by_red": {"$ne": True}},
                {"access_orange": {"$eq": True}},
            ]},
        )
        self.assertEqual(result["total"], 0)

    def test_sync_gmail_thread_id_calls_sync_thread(self):
        from unittest.mock import patch

        with patch("agent_core.ingest.gmail_sync.sync_thread") as mock_sync:
            mock_sync.return_value = {"thread_id": "t123", "chunks": 3}
            result = self._dispatch("command.sync_gmail", {"thread_id": "t123"})

        mock_sync.assert_called_once_with("t123")
        self.assertEqual(result["chunks"], 3)

    def test_sync_gmail_no_thread_calls_sync_query(self):
        from unittest.mock import patch

        with patch("agent_core.ingest.gmail_sync.sync_query") as mock_sync:
            mock_sync.return_value = {"query": "newer_than:180d", "synced": 50}
            result = self._dispatch("command.sync_gmail", {"max_threads": 50})

        mock_sync.assert_called_once_with("newer_than:180d", 50)
        self.assertEqual(result["synced"], 50)


if __name__ == "__main__":
    unittest.main()
