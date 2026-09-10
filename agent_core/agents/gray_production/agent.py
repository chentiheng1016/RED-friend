"""Gray agent — 生產管理 / Production Management.

Gray Trigger (Special Logic):
  When a production anomaly is reported, Gray automatically:
    1. Cross-queries Yellow (採購 ETA), Indigo (庫存), White (合規替代方案)
    2. Calculates new ECD (Estimated Completion Date)
    3. Notifies Orange (業務) of the delay
    4. Submits a Decision Support Report to Red (GM) via Telegram

Intents:
  query.*
    query.profile                  returns agent capability profile
    query.production_status        payload={"recent_n"?: int}
    query.list_production_orders   alias → query.production_status (UI back-compat)
    query.anomaly_history          payload={"recent_n"?: int}

  command.*
    command.report_anomaly    payload={
      "order_id": str,
      "product": str,
      "customer": str,
      "original_ecd": str,   # ISO date e.g. "2026-05-20"
      "reason": str,
      "severity": "low"|"medium"|"high"
    }
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core.agents.gray_production import anomaly as _an
from agent_core.agents.gray_production import production_tracker as _pt


def _parse_recent_n(value: object, default: int) -> int:
    """Safely coerce *value* to a positive int, falling back to *default*.

    Handles None, non-numeric strings, and floats from JSON payloads without
    raising TypeError or ValueError to the caller.
    """
    if value is None:
        return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


class GrayProductionAgent(BaseAgent):
    identity = Agent.GRAY

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:

        if intent == "query.profile":
            return {
                "profile": {
                    "color": "gray",
                    "department": "生產管理 / Production Management",
                    "query_intents": [
                        "query.profile",
                        "query.production_status",
                        "query.list_production_orders",
                        "query.anomaly_history",
                    ],
                    "command_intents": [
                        "command.report_anomaly",
                    ],
                    "notes": (
                        "Gray tracks production anomalies, recalculates ECD, "
                        "and coordinates Yellow/Indigo/White/Orange/Red notices."
                    ),
                }
            }

        # query.list_production_orders — legacy intent sent by the UI quick-action
        # before this handler existed (stub swallowed it silently).  Alias it to
        # query.production_status so clicking 「查生產進度」 still works.
        if intent in ("query.production_status", "query.list_production_orders"):
            recent_n = _parse_recent_n(payload.get("recent_n"), default=10)
            records = _pt.list_anomalies(recent_n)
            return {
                "count": len(records),
                "anomalies": records,
            }

        if intent == "query.anomaly_history":
            recent_n = _parse_recent_n(payload.get("recent_n"), default=20)
            records = _pt.list_anomalies(recent_n)
            return {
                "count": len(records),
                "anomalies": records,
            }

        if intent == "command.report_anomaly":
            # Reject None explicitly before str() coercion: str(None) == "None"
            # which is non-empty and would pass the checks below with a bogus value.
            for field in ("order_id", "product", "customer", "original_ecd", "reason"):
                if payload.get(field) is None:
                    return {"error": f"{field} 不能為 null"}

            order_id     = str(payload.get("order_id", "")).strip()
            product      = str(payload.get("product", "")).strip()
            customer     = str(payload.get("customer", "")).strip()
            original_ecd = str(payload.get("original_ecd", "")).strip()
            reason       = str(payload.get("reason", "")).strip()
            severity     = str(payload.get("severity", "medium")).strip().lower()

            if not order_id:
                return {"error": "order_id 不能為空"}
            if not product:
                return {"error": "product 不能為空"}
            if not customer:
                return {"error": "customer 不能為空"}
            if not original_ecd:
                return {"error": "original_ecd 不能為空（格式：YYYY-MM-DD）"}
            if not reason:
                return {"error": "reason 不能為空"}
            if severity not in ("low", "medium", "high"):
                return {"error": "severity 必須是 low / medium / high"}

            try:
                return _an.trigger_gray(
                    agent=self,
                    order_id=order_id,
                    product=product,
                    customer=customer,
                    original_ecd=original_ecd,
                    reason=reason,
                    severity=severity,
                    trace_id=trace_id,
                )
            except ValueError as exc:
                return {"error": str(exc)}

        raise ValueError(f"GrayProductionAgent: unknown intent {intent!r}")
