"""Blue agent — 船務 / Shipping.

船務查詢：
  - query.profile
  - query.list_shipments
  - query.shipment
  - query.shipping_eta
  - query.shipping_records
  - query.shipping_alerts
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents._payload import pint, pstr
from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core.agents.blue_shipping import shipping as _ship


class BlueShippingAgent(BaseAgent):
    identity = Agent.BLUE

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        if intent == "query.profile":
            return {"profile": _ship.get_profile()}

        if intent == "query.list_shipments":
            return _ship.list_shipments(
                customer=pstr(payload, "customer"),
                po_number=pstr(payload, "po_number", "po_id"),
                keyword=pstr(payload, "keyword", "query"),
                status=pstr(payload, "status"),
                days_back=pint(payload, "days_back", "days", default=180),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.shipment":
            return _ship.get_shipment(
                pstr(
                    payload,
                    "identifier",
                    "shipment_id",
                    "thread_id",
                    "container_number",
                    "awb",
                    "po_number",
                ),
                days_back=pint(payload, "days_back", default=3650),
            )

        if intent == "query.shipping_eta":
            return _ship.shipping_eta(
                identifier=pstr(payload, "identifier", "shipment_id"),
                po_number=pstr(payload, "po_number", "po_id"),
                customer=pstr(payload, "customer"),
                keyword=pstr(payload, "keyword", "query"),
                days_back=pint(payload, "days_back", "days", default=365),
                limit=pint(payload, "limit", default=10),
            )

        if intent == "query.shipping_records":
            return _ship.shipping_records(
                po_number=pstr(payload, "po_number", "po_id"),
                customer=pstr(payload, "customer"),
                keyword=pstr(payload, "keyword", "query"),
                days_back=pint(payload, "days_back", "days", default=365),
                max_events=pint(payload, "max_events", "limit", default=20),
            )

        if intent == "query.shipping_alerts":
            return _ship.shipping_alerts(
                days_back=pint(payload, "days_back", "days", default=60),
                limit=pint(payload, "limit", default=20),
            )

        raise ValueError(f"BlueShippingAgent: unknown intent {intent!r}")
