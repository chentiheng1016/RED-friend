"""RPA task contracts for the Green / sample room agent.

The Green agent owns the ERP-facing business rules. Edge Agents should receive
validated task drafts from here, then execute locally with human confirmation.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from agent_core.agents.green_sample_dev import profile as _profile


_DATE_FIELDS = frozenset({"sent_date", "expected_feedback_date"})
_TEXT_FIELD_TYPES = (int, float, str)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: Any) -> str | None:
    """Normalize ERP text values; return None for values we refuse to coerce."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return None
    if isinstance(value, _TEXT_FIELD_TYPES):
        return str(value).strip()
    return None


def _validate_iso_date(field: str, value: str) -> dict[str, str] | None:
    if not value:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return {
            "field": field,
            "reason": "must be YYYY-MM-DD",
        }
    return None


def validate_rpa_payload(recipe_id: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize an ERP/RPA payload for a Green recipe.

    Unknown fields are rejected. This keeps the future Edge Agent from executing
    values that the department agent did not explicitly approve.
    """
    recipe_id = str(recipe_id or "").strip()
    if not recipe_id:
        return {
            "ok": False,
            "error": "missing recipe_id",
            "recipe_id": "",
            "found": False,
            "missing_fields": [],
            "unknown_fields": [],
            "invalid_fields": [],
            "normalized_payload": {},
            "required_fields": [],
            "optional_fields": [],
        }

    recipe = _profile.get_rpa_recipe(recipe_id)
    if recipe is None:
        return {
            "ok": False,
            "error": "unknown recipe_id",
            "recipe_id": recipe_id,
            "found": False,
            "missing_fields": [],
            "unknown_fields": [],
            "invalid_fields": [],
            "normalized_payload": {},
            "required_fields": [],
            "optional_fields": [],
        }

    raw_payload = dict(payload or {})
    required_fields = list(recipe.get("required_fields", []))
    optional_fields = list(recipe.get("optional_fields", []))
    allowed_fields = set(required_fields) | set(optional_fields)

    unknown_fields = sorted(field for field in raw_payload if field not in allowed_fields)
    invalid_fields: list[dict[str, str]] = []
    normalized: dict[str, str] = {}

    for field in required_fields + optional_fields:
        if field not in raw_payload:
            continue
        cleaned = _clean_text(raw_payload.get(field))
        if cleaned is None:
            invalid_fields.append({
                "field": field,
                "reason": "must be text, number, or empty",
            })
            continue
        if field == "status":
            cleaned = cleaned.lower()
        if cleaned:
            normalized[field] = cleaned

    missing_fields = [field for field in required_fields if not normalized.get(field)]

    for field in _DATE_FIELDS:
        if field in normalized:
            date_error = _validate_iso_date(field, normalized[field])
            if date_error is not None:
                invalid_fields.append(date_error)

    allowed_status = recipe.get("allowed_status")
    if allowed_status and "status" in normalized:
        if normalized["status"] not in set(allowed_status):
            invalid_fields.append({
                "field": "status",
                "reason": f"must be one of: {', '.join(allowed_status)}",
            })

    return {
        "ok": not missing_fields and not unknown_fields and not invalid_fields,
        "recipe_id": recipe_id,
        "found": True,
        "missing_fields": missing_fields,
        "unknown_fields": unknown_fields,
        "invalid_fields": invalid_fields,
        "normalized_payload": normalized,
        "required_fields": required_fields,
        "optional_fields": optional_fields,
    }


def build_edge_task_draft(
    recipe_id: str,
    payload: Mapping[str, Any] | None,
    *,
    employee_email: str = "",
    device_id: str = "",
    requested_by: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """Build a safe Edge Agent task draft without enqueueing or executing it."""
    validation = validate_rpa_payload(recipe_id, payload)
    if not validation["ok"]:
        return {
            "ok": False,
            "validation": validation,
        }

    recipe = _profile.get_rpa_recipe(validation["recipe_id"])
    if recipe is None:
        return {
            "ok": False,
            "validation": validation,
        }

    task = {
        "task_id": f"draft-{uuid4().hex}",
        "type": "edge_rpa",
        "department": "green",
        "status": "draft",
        "recipe_id": validation["recipe_id"],
        "recipe_label": recipe["label"],
        "erp_module": recipe["erp_module"],
        "risk": recipe["risk"],
        "mode": recipe["mode"],
        "requires_confirmation": deepcopy(recipe["confirmation"]),
        "employee_email": str(employee_email or "").strip(),
        "device_id": str(device_id or "").strip(),
        "requested_by": str(requested_by or "").strip(),
        "trace_id": str(trace_id or "").strip(),
        "payload": validation["normalized_payload"],
        "preconditions": deepcopy(recipe.get("preconditions", [])),
        "steps": deepcopy(recipe.get("steps", [])),
        "audit_required": deepcopy(recipe.get("audit", [])),
        "rollback": recipe.get("rollback", ""),
        "created_at": _utc_now_iso(),
    }

    return {
        "ok": True,
        "task": task,
        "validation": validation,
    }
