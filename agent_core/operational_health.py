"""Operational Postgres backend health catalog."""
from __future__ import annotations

import importlib
import os
from typing import Any

from agent_core.env_utils import env_bool
from agent_core import operational_db

_POSTGRES_VALUES = {"postgres", "postgresql", "operational_db", "db"}

_BACKEND_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "audit",
        "module": "agent_core.operational_db",
        "backend_envs": ("RED_OPERATIONAL_DB_URL", "RED_DATABASE_URL"),
        "bool_envs": (),
        "role": "Telegram audit mirror",
    },
    {
        "name": "task_queue",
        "module": "agent_core.operational_task_queue",
        "backend_envs": ("RED_TASK_QUEUE_BACKEND",),
        "bool_envs": ("RED_TASK_QUEUE_POSTGRES",),
        "role": "background task queue and DLQ",
    },
    {
        "name": "telegram_approvals",
        "module": "agent_core.operational_telegram_approvals",
        "backend_envs": ("RED_TELEGRAM_APPROVALS_BACKEND",),
        "bool_envs": ("RED_TELEGRAM_APPROVALS_POSTGRES",),
        "role": "Telegram approvals and join requests",
    },
    {
        "name": "telegram_auth",
        "module": "agent_core.operational_tg_auth_state",
        "backend_envs": ("RED_TELEGRAM_AUTH_BACKEND", "RED_TG_AUTH_BACKEND"),
        "bool_envs": ("RED_TELEGRAM_AUTH_POSTGRES",),
        "role": "Telegram confirmation and rate-limit state",
    },
    {
        "name": "tool_budgets",
        "module": "agent_core.operational_tool_budgets",
        "backend_envs": ("RED_TOOL_BUDGETS_BACKEND",),
        "bool_envs": ("RED_TOOL_BUDGETS_POSTGRES",),
        "role": "daily/hourly sensitive tool budgets",
    },
    {
        "name": "cost_tracker",
        "module": "agent_core.operational_cost_tracker",
        "backend_envs": ("RED_COST_TRACKER_BACKEND",),
        "bool_envs": ("RED_COST_TRACKER_POSTGRES",),
        "role": "Gemini cost and external API errors",
    },
    {
        "name": "run_history",
        "module": "agent_core.operational_run_history",
        "backend_envs": ("RED_RUN_HISTORY_BACKEND",),
        "bool_envs": ("RED_RUN_HISTORY_POSTGRES",),
        "role": "sensitive tool run history",
    },
    {
        "name": "task_memory",
        "module": "agent_core.operational_task_memory",
        "backend_envs": ("RED_TASK_MEMORY_BACKEND",),
        "bool_envs": ("RED_TASK_MEMORY_POSTGRES",),
        "role": "commitment/task memory",
    },
    {
        "name": "edge_tasks",
        "module": "agent_core.operational_edge_tasks",
        "backend_envs": ("RED_EDGE_TASKS_BACKEND",),
        "bool_envs": ("RED_EDGE_TASKS_POSTGRES",),
        "role": "Edge Agent devices and ERP tasks",
    },
    {
        "name": "policy_engine",
        "module": "agent_core.operational_policy_engine",
        "backend_envs": ("RED_POLICY_ENGINE_BACKEND",),
        "bool_envs": ("RED_POLICY_ENGINE_POSTGRES",),
        "role": "tool policy decisions",
    },
    {
        "name": "work_mode",
        "module": "agent_core.operational_work_mode",
        "backend_envs": ("RED_WORK_MODE_BACKEND",),
        "bool_envs": ("RED_WORK_MODE_POSTGRES",),
        "role": "current work mode and history",
    },
    {
        "name": "dry_run",
        "module": "agent_core.operational_dry_run",
        "backend_envs": ("RED_DRY_RUN_BACKEND",),
        "bool_envs": ("RED_DRY_RUN_POSTGRES",),
        "role": "global dry-run guard state",
    },
    {
        "name": "web_domain_policy",
        "module": "agent_core.operational_web_domain_policy",
        "backend_envs": (
            "RED_WEB_DOMAIN_POLICY_BACKEND",
            "RED_WEB_ACCESS_POLICY_BACKEND",
        ),
        "bool_envs": ("RED_WEB_DOMAIN_POLICY_POSTGRES",),
        "role": "web domain access policy",
    },
    {
        "name": "intent_router",
        "module": "agent_core.operational_intent_router",
        "backend_envs": ("RED_INTENT_ROUTER_BACKEND", "RED_INTENT_LOG_BACKEND"),
        "bool_envs": ("RED_INTENT_ROUTER_POSTGRES",),
        "role": "intent classification log",
    },
    {
        "name": "alert_push",
        "module": "agent_core.operational_alert_pusher",
        "backend_envs": ("RED_ALERT_PUSH_BACKEND", "RED_ALERT_PUSHER_BACKEND"),
        "bool_envs": ("RED_ALERT_PUSH_POSTGRES",),
        "role": "alert push dedupe and recovery state",
    },
    {
        "name": "gemini_circuit",
        "module": "agent_core.operational_gemini_circuit",
        "backend_envs": ("RED_GEMINI_CIRCUIT_BACKEND",),
        "bool_envs": ("RED_GEMINI_CIRCUIT_POSTGRES",),
        "role": "shared Gemini outage circuit breaker",
    },
)


def _backend_env_value(spec: dict[str, Any]) -> str:
    for name in spec.get("backend_envs", ()):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _configured(spec: dict[str, Any]) -> bool:
    value = _backend_env_value(spec)
    if value:
        if spec["name"] == "audit":
            return operational_db.enabled()
        return value.lower() in _POSTGRES_VALUES
    return any(env_bool(name, False) for name in spec.get("bool_envs", ()))


def _module_enabled(module_name: str) -> tuple[bool, str]:
    try:
        module = importlib.import_module(module_name)
        enabled_fn = getattr(module, "enabled", None)
        if callable(enabled_fn):
            return bool(enabled_fn()), ""
        return False, "module has no enabled()"
    except Exception as exc:  # noqa: BLE001 - diagnostic path
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def backend_statuses() -> list[dict[str, Any]]:
    """Return one status row per optional operational backend."""
    db_on = operational_db.enabled()
    rows: list[dict[str, Any]] = []
    for spec in _BACKEND_SPECS:
        configured = _configured(spec)
        enabled, error = _module_enabled(spec["module"])
        if spec["name"] == "audit":
            enabled = db_on
        status = "enabled" if enabled else "local_fallback"
        if configured and not enabled:
            status = "configured_but_inactive"
        reason = error
        if configured and not enabled and not reason:
            reason = "database URL missing" if not db_on else "backend switch not accepted"
        rows.append({
            "name": spec["name"],
            "role": spec["role"],
            "status": status,
            "enabled": enabled,
            "configured": configured,
            "backend_env_value": _backend_env_value(spec),
            "reason": reason,
        })
    return rows


def health_report() -> dict[str, Any]:
    db = operational_db.health_status()
    backends = backend_statuses()
    version = db.get("schema_version")
    schema_expected = db.get("schema_expected", operational_db.SCHEMA_VERSION)
    schema_ok = (
        not db.get("enabled")
        or (isinstance(version, int) and version >= schema_expected)
    )
    return {
        "operational_db": db,
        "schema_ok": schema_ok,
        "backends": backends,
        "enabled_backends": [b["name"] for b in backends if b["enabled"]],
        "configured_backends": [b["name"] for b in backends if b["configured"]],
        "local_fallback_backends": [b["name"] for b in backends if not b["enabled"]],
    }


def health_issues(*, cloud_runtime: bool = False) -> list[dict[str, Any]]:
    report = health_report()
    db = report["operational_db"]
    issues: list[dict[str, Any]] = []
    if cloud_runtime and not db.get("enabled"):
        issues.append({
            "severity": "warning",
            "area": "operational_db",
            "msg": "Cloud runtime 未設定 RED_OPERATIONAL_DB_URL/RED_DATABASE_URL；多人/多 agent 狀態會退回本機 fallback",
        })
    if db.get("enabled") and not db.get("ok"):
        issues.append({
            "severity": "error",
            "area": "operational_db",
            "msg": f"Postgres operational DB 不可用：{db.get('error') or 'unknown'}",
        })
    elif db.get("enabled") and not report.get("schema_ok"):
        issues.append({
            "severity": "warning",
            "area": "operational_db/schema",
            "msg": f"schema version {db.get('schema_version')} < expected {db.get('schema_expected')}",
        })
    for item in report["backends"]:
        if item["configured"] and not item["enabled"]:
            issues.append({
                "severity": "warning",
                "area": f"operational_db/{item['name']}",
                "msg": f"{item['role']} 已設定 Postgres backend 但未啟用：{item.get('reason') or 'unknown'}",
            })
    return issues


def format_backend_status() -> str:
    report = health_report()
    db = report["operational_db"]
    lines = ["Operational DB backend status"]
    lines.append(
        "db="
        + ("enabled" if db.get("enabled") else "disabled")
        + f" ok={bool(db.get('ok'))}"
        + f" schema={db.get('schema_version')}/{db.get('schema_expected')}"
        + f" pool={bool(db.get('pool_active'))}"
    )
    for item in report["backends"]:
        lines.append(f"- {item['name']}: {item['status']} ({item['role']})")
    return "\n".join(lines)
