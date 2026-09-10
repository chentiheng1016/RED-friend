"""Black agent — 出納 / Cashier.

Black v1 is read-only. It focuses on cash-in/cash-out visibility by reusing
the accounting email lake extraction layer. Payment execution remains outside
Telegram and outside this agent.
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents._payload import pint, pstr
from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core.agents.black_cashier import cashier as _cash


class BlackCashierAgent(BaseAgent):
    identity = Agent.BLACK

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        if intent == "query.profile":
            return {"profile": _cash.get_profile()}

        if intent in {"query.list_transactions", "query.cash_records"}:
            return _cash.cash_records(
                counterparty=pstr(payload, "counterparty", "customer", "supplier"),
                po_number=pstr(payload, "po_number", "po_id"),
                keyword=pstr(payload, "keyword", "query"),
                direction=pstr(payload, "direction", "cash_direction"),
                days_back=pint(payload, "days_back", "days", default=365),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.cash_payments":
            return _cash.cash_payments(
                counterparty=pstr(payload, "counterparty", "customer", "supplier"),
                po_number=pstr(payload, "po_number", "po_id"),
                keyword=pstr(payload, "keyword", "query"),
                days_back=pint(payload, "days_back", "days", default=365),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.cash_receipts":
            return _cash.cash_receipts(
                counterparty=pstr(payload, "counterparty", "customer", "supplier"),
                keyword=pstr(payload, "keyword", "query"),
                days_back=pint(payload, "days_back", "days", default=365),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.cash_alerts":
            return _cash.cash_alerts(
                days_back=pint(payload, "days_back", "days", default=90),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.cash_summary":
            return _cash.cash_summary(
                days_back=pint(payload, "days_back", "days", default=30),
                limit=pint(payload, "limit", default=5),
            )

        raise ValueError(f"BlackCashierAgent: unknown intent {intent!r}")
