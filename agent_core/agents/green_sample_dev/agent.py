"""Green agent — 樣品室部門 agent.

intent 列表（Phase 2 起步，未來可加）:
  - query.profile              payload={}
  - query.rpa_recipes          payload={}
  - query.rpa_recipe           payload={"recipe_id": "green_create_sample_record"}
  - query.validate_rpa_payload payload={"recipe_id": "...", "data": {...}}
  - query.edge_task_draft      payload={"recipe_id": "...", "data": {...}}
  - query.edge_tasks           payload={"status": "queued", "device_id": "..."}
  - query.edge_task            payload={"task_id": "..."}
  - query.sample_status        payload={"sample_id": "S-2026-04"}
  - query.list_samples          payload={"status": "open" | "delayed" | "closed" | "all"}
  - query.erp_sample           payload={"keyword": "PU468" | "SR2511001" | "JA1065"}
  - command.enqueue_edge_task   payload={"recipe_id": "...", "data": {...}}
  - command.track_sample        payload=track_sample 的所有參數
  - command.update_status       payload={"sample_id", "status", "note"}
  - command.check_deadlines     payload={"auto_draft_followup": bool, "push_telegram": bool}

呼叫者透過 PermissionMiddleware.dispatch 進來，矩陣決定誰能 query。
依矩陣：Orange / Indigo / Red 可以查 Green；其他色不行。
"""
from __future__ import annotations

from collections.abc import Mapping as MappingABC
from typing import Any, Mapping

from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core import edge_tasks as _edge_tasks
from agent_core.agents.green_sample_dev import profile as _profile
from agent_core.agents.green_sample_dev import rpa_tasks as _rpa
from agent_core.agents.green_sample_dev import sample_tracker as _st


_RPA_META_FIELDS = frozenset({
    "recipe_id",
    "employee_email",
    "device_id",
    "requested_by",
    "trace_id",
    "data",
})


def _extract_rpa_data(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    data = payload.get("data")
    if data is None:
        return {
            key: value
            for key, value in payload.items()
            if key not in _RPA_META_FIELDS
        }, None
    if not isinstance(data, MappingABC):
        return {}, "data must be a JSON object"
    return dict(data), None


class GreenSampleDevAgent(BaseAgent):
    identity = Agent.GREEN

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        if intent == "query.profile":
            return {"profile": _profile.get_profile()}

        if intent == "query.rpa_recipes":
            return {
                "department": "green",
                "recipes": _profile.list_rpa_recipes(),
            }

        if intent == "query.rpa_recipe":
            recipe_id = str(payload.get("recipe_id", "")).strip()
            if not recipe_id:
                return {"error": "missing recipe_id"}
            recipe = _profile.get_rpa_recipe(recipe_id)
            return {
                "recipe_id": recipe_id,
                "found": recipe is not None,
                "recipe": recipe,
            }

        if intent == "query.validate_rpa_payload":
            data, error = _extract_rpa_data(payload)
            if error:
                return {"ok": False, "error": error}
            return _rpa.validate_rpa_payload(
                recipe_id=str(payload.get("recipe_id", "")),
                payload=data,
            )

        if intent == "query.edge_task_draft":
            data, error = _extract_rpa_data(payload)
            if error:
                return {"ok": False, "error": error}
            return _rpa.build_edge_task_draft(
                recipe_id=str(payload.get("recipe_id", "")),
                payload=data,
                employee_email=str(payload.get("employee_email") or ""),
                device_id=str(payload.get("device_id") or ""),
                requested_by=str(payload.get("requested_by") or ""),
                trace_id=str(payload.get("trace_id") or trace_id),
            )

        if intent == "query.edge_tasks":
            status = str(payload.get("status") or "").strip()
            statuses = status if status else None
            return _edge_tasks.list_tasks(
                department="green",
                device_id=str(payload.get("device_id") or ""),
                employee_email=str(payload.get("employee_email") or ""),
                statuses=statuses,
                limit=int(payload.get("limit", 20)),
            )

        if intent == "query.edge_task":
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                return {"error": "missing task_id"}
            return _edge_tasks.get_task(task_id)

        if intent == "query.sample_status":
            sample_id = str(payload.get("sample_id", "")).strip()
            if not sample_id:
                return {"error": "missing sample_id"}
            data = _st._load_sample_tracker()
            entry = data.get("samples", {}).get(sample_id)
            return {"sample_id": sample_id, "found": entry is not None, "entry": entry}

        if intent == "query.list_samples":
            status = str(payload.get("status", "open"))
            return {"text": _st.list_tracked_samples(status=status)}

        if intent == "query.erp_sample":
            # 確定性樣品單查詢（零 LLM）：料號/庫存編號→SR 樣品單反查／
            # 樣品單號·鞋款→樣品 BOM 照表念（2026-07-29 UserC PU468 案：
            # SP00 樣品域是 freeform 盲區，被幻覺成「ERP 無獨立 SR 樣品單」）。
            from agent_core.agents._payload import pstr
            from agent_core.erp_stock_query import erp_sample_lookup
            return {"text": erp_sample_lookup(
                pstr(payload, "keyword", "料號", "item", "q", "樣品單",
                     "sample", "款", "model"),
            )}

        if intent == "command.track_sample":
            return {"text": _st.track_sample(
                sample_id=str(payload.get("sample_id", "")),
                customer=str(payload.get("customer", "")),
                description=str(payload.get("description", "")),
                contact_email=str(payload.get("contact_email", "")),
                sent_date=str(payload.get("sent_date", "")),
                expected_feedback_date=str(payload.get("expected_feedback_date", "")),
                notes=str(payload.get("notes", "")),
            )}

        if intent == "command.enqueue_edge_task":
            data, error = _extract_rpa_data(payload)
            if error:
                return {"ok": False, "error": error}
            draft = _rpa.build_edge_task_draft(
                recipe_id=str(payload.get("recipe_id", "")),
                payload=data,
                employee_email=str(payload.get("employee_email") or ""),
                device_id=str(payload.get("device_id") or payload.get("target_device_id") or ""),
                requested_by=str(payload.get("requested_by") or ""),
                trace_id=str(payload.get("trace_id") or trace_id),
            )
            if not draft["ok"]:
                return draft
            return _edge_tasks.enqueue_edge_task(
                draft,
                target_device_id=str(payload.get("target_device_id") or payload.get("device_id") or ""),
                employee_email=str(payload.get("employee_email") or ""),
                created_by=str(payload.get("requested_by") or ""),
            )

        if intent == "command.update_status":
            return {"text": _st.update_sample_status(
                sample_id=str(payload.get("sample_id", "")),
                status=str(payload.get("status", "")),
                note=str(payload.get("note", "")),
            )}

        if intent == "command.check_deadlines":
            return {"text": _st.check_sample_deadlines(
                auto_draft_followup=bool(payload.get("auto_draft_followup", True)),
                push_telegram=bool(payload.get("push_telegram", False)),
            )}

        raise ValueError(f"GreenSampleDevAgent: unknown intent {intent!r}")
