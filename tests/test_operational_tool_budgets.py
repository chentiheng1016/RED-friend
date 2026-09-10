from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_TOOL_BUDGETS_BACKEND": "postgres",
}


class OperationalToolBudgetTests(unittest.TestCase):
    def test_backend_requires_explicit_budget_switch(self):
        from agent_core import operational_tool_budgets as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_load_today_routes_to_postgres_backend(self):
        from agent_core import tool_budgets

        rows = {"send_gmail": {"daily": 2, "hour": "h", "hour_count": 1}}
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_tool_budgets.load_day",
                    return_value=rows,
                ) as load_day:
            self.assertEqual(tool_budgets._load_today(), rows)

        load_day.assert_called_once()

    def test_check_budget_reads_postgres_state(self):
        from agent_core import tool_budgets

        with mock.patch.dict(
            os.environ,
            {**_DB_ENV, "RED_BUDGET_SEND_GMAIL_DAILY": "2"},
            clear=True,
        ), \
                mock.patch(
                    "agent_core.operational_tool_budgets.load_day",
                    return_value={"send_gmail": {"daily": 2, "hour": "", "hour_count": 0}},
                ):
            ok, reason = tool_budgets.check_budget("send_gmail")

        self.assertFalse(ok)
        self.assertIn("上限 2", reason)

    def test_record_use_writes_postgres_backend(self):
        from agent_core import tool_budgets

        with mock.patch.dict(
            os.environ,
            {**_DB_ENV, "RED_BUDGET_TEST_TOOL_DAILY": "10"},
            clear=True,
        ), \
                mock.patch(
                    "agent_core.operational_tool_budgets.record_use",
                ) as record_use:
            tool_budgets.record_use("test_tool", caller_id="team/a")

        record_use.assert_called_once()
        self.assertEqual(record_use.call_args.args[0], "test_tool")
        self.assertEqual(
            record_use.call_args.kwargs["caller_key"],
            tool_budgets._normalize_caller_id("team/a"),
        )
        self.assertEqual(record_use.call_args.kwargs["retain_days"], 30)

    def test_reset_budget_deletes_postgres_backend(self):
        from agent_core import tool_budgets

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_tool_budgets.reset_budget",
                    return_value=True,
                ) as reset_budget:
            self.assertTrue(tool_budgets.reset_budget("send_gmail"))

        reset_budget.assert_called_once_with("send_gmail")


if __name__ == "__main__":
    unittest.main()
