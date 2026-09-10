from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_INTENT_ROUTER_BACKEND": "postgres",
}


class OperationalIntentRouterTests(unittest.TestCase):
    def setUp(self):
        from agent_core import intent_router

        intent_router._PG_INTENT_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_intent_switch(self):
        from agent_core import operational_intent_router as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_classify_writes_postgres_backend(self):
        from agent_core import intent_router

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_intent_router.write_classification",
                ) as write_classification:
            result = intent_router.classify("幫我寄信給客戶", allow_llm=False)

        self.assertEqual(result.intent, "write_email")
        write_classification.assert_called_once()
        record = write_classification.call_args.args[0]
        self.assertEqual(record["intent"], "write_email")
        self.assertEqual(record["method"], "heuristic")
        self.assertIn("寄信", record["text_preview"])

    def test_intent_recent_reads_postgres_backend(self):
        from agent_core import intent_router

        rows = [
            {
                "at": "2026-06-24T12:02:00",
                "intent": "write_email",
                "confidence": 0.9,
                "method": "heuristic",
                "text_preview": "幫我寄信",
            },
            {
                "at": "2026-06-24T12:01:00",
                "intent": "query_data",
                "confidence": 0.8,
                "method": "heuristic",
                "text_preview": "今天信箱",
            },
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_intent_router.load_classifications",
                    return_value=rows,
                ) as load_classifications:
            out = intent_router.intent_recent(hours=6, limit=5)

        self.assertIn("write_email", out)
        self.assertIn("query_data", out)
        self.assertIn("分布", out)
        load_classifications.assert_called_once_with(hours=6, limit=50000)

    def test_intent_summary_reads_postgres_backend(self):
        from agent_core import intent_router

        rows = [
            {"intent": "write_email", "method": "heuristic"},
            {"intent": "write_email", "method": "heuristic"},
            {"intent": "chat", "method": "llm"},
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_intent_router.load_classifications",
                    return_value=rows,
                ):
            summary = intent_router.intent_summary(hours=12)

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_intent"], {"write_email": 2, "chat": 1})
        self.assertEqual(summary["by_method"], {"heuristic": 2, "llm": 1})

    def test_intent_routing_status_reads_postgres_backend(self):
        from agent_core import intent_router

        rows = [
            {"intent": "write_email", "confidence": 0.9, "method": "heuristic"},
            {"intent": "chat", "confidence": 0.95, "method": "heuristic"},
            {"intent": "unknown", "confidence": 0.2, "method": "llm"},
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_intent_router.load_classifications",
                    return_value=rows,
                ) as load_classifications:
            out = intent_router.intent_routing_status()

        self.assertIn("Intent Routing 狀態", out)
        self.assertIn("分類 3 次", out)
        self.assertIn("可路由", out)
        self.assertIn("write_email", out)
        load_classifications.assert_called_once_with(hours=24, limit=50000)


if __name__ == "__main__":
    unittest.main()
