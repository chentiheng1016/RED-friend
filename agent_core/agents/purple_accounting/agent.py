"""Purple agent — 會計 / Accounting.

會計查詢：
  - query.profile
  - query.list_accounts
  - query.list_accounting_records
  - query.accounting_record
  - query.invoice_records
  - query.payment_records
  - query.remittance_records
  - query.accounting_alerts
  - query.accounting_summary
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents._payload import pint, pstr
from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core.agents.purple_accounting import accounting as _acct


class PurpleAccountingAgent(BaseAgent):
    identity = Agent.PURPLE

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        if intent == "query.profile":
            return {"profile": _acct.get_profile()}

        if intent in {"query.list_accounts", "query.list_accounting_records"}:
            return _acct.list_accounting_records(
                kind=pstr(payload, "kind", "type"),
                status=pstr(payload, "status"),
                counterparty=pstr(payload, "counterparty", "customer", "supplier"),
                po_number=pstr(payload, "po_number", "po_id"),
                keyword=pstr(payload, "keyword", "query"),
                days_back=pint(payload, "days_back", "days", default=365),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.accounting_record":
            return _acct.get_accounting_record(
                pstr(
                    payload,
                    "identifier",
                    "record_id",
                    "thread_id",
                    "po_number",
                ),
                days_back=pint(payload, "days_back", default=3650),
            )

        if intent == "query.invoice_records":
            return _acct.invoice_records(
                counterparty=pstr(payload, "counterparty", "customer", "supplier"),
                po_number=pstr(payload, "po_number", "po_id"),
                keyword=pstr(payload, "keyword", "query"),
                days_back=pint(payload, "days_back", "days", default=365),
                max_events=pint(payload, "max_events", "limit", default=20),
            )

        if intent == "query.payment_records":
            return _acct.payment_records(
                counterparty=pstr(payload, "counterparty", "customer", "supplier"),
                po_number=pstr(payload, "po_number", "po_id"),
                keyword=pstr(payload, "keyword", "query"),
                cash_direction=pstr(payload, "cash_direction", "direction"),
                days_back=pint(payload, "days_back", "days", default=365),
                max_events=pint(payload, "max_events", "limit", default=20),
            )

        if intent == "query.remittance_records":
            return _acct.remittance_records(
                counterparty=pstr(payload, "counterparty", "customer", "supplier"),
                keyword=pstr(payload, "keyword", "query"),
                days_back=pint(payload, "days_back", "days", default=365),
                max_events=pint(payload, "max_events", "limit", default=20),
            )

        if intent == "query.accounting_alerts":
            return _acct.accounting_alerts(
                days_back=pint(payload, "days_back", "days", default=90),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.accounting_summary":
            return _acct.accounting_summary(
                days_back=pint(payload, "days_back", "days", default=30),
                limit=pint(payload, "limit", default=5),
            )

        raise ValueError(f"PurpleAccountingAgent: unknown intent {intent!r}")
