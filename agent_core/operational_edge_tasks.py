"""Postgres-backed Edge Agent task queue state."""
from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime
from typing import Any, Iterator, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = os.environ.get("RED_EDGE_TASKS_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_EDGE_TASKS_POSTGRES", False))


def namespace() -> str:
    return os.environ.get("RED_EDGE_TASKS_NAMESPACE", "default").strip() or "default"


def _json_safe(value: Any) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"_raw": str(value)}


def _jsonb(value: Mapping[str, Any]):
    value = _json_safe(value)
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    except Exception:
        return value


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _default_state() -> dict[str, Any]:
    return {"version": 1, "devices": {}, "tasks": []}


def _device_payload(row: Mapping[str, Any] | tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    if isinstance(row, Mapping):
        device_id = str(row.get("device_id") or "")
        payload = row.get("payload")
    else:
        device_id = str(row[0] or "") if row else ""
        payload = row[1] if len(row) > 1 else None
    return device_id, dict(payload) if isinstance(payload, dict) else {}


def _task_payload(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        payload = row.get("payload")
    else:
        payload = row[0] if row else None
    return dict(payload) if isinstance(payload, dict) else {}


def _load_locked(cur, ns: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT device_id, payload
        FROM red_edge_devices
        WHERE namespace = %s
        ORDER BY device_id ASC
        """,
        (ns,),
    )
    devices = {}
    for row in cur.fetchall():
        device_id, payload = _device_payload(row)
        if device_id:
            devices[device_id] = payload

    cur.execute(
        """
        SELECT payload
        FROM red_edge_tasks
        WHERE namespace = %s
        ORDER BY created_at ASC NULLS LAST, task_id ASC
        """,
        (ns,),
    )
    return {"version": 1, "devices": devices, "tasks": [_task_payload(row) for row in cur.fetchall()]}


def load_state() -> dict[str, Any]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            return _load_locked(cur, ns)


def _device_values(ns: str, device_id: str, device: Mapping[str, Any]) -> tuple[Any, ...] | None:
    did = str(device_id or device.get("device_id") or "").strip()
    if not did:
        return None
    return (
        ns,
        did,
        str(device.get("department") or ""),
        str(device.get("employee_email") or ""),
        str(device.get("status") or ""),
        _parse_ts(device.get("last_seen_at")),
        _jsonb(device),
    )


def _task_values(ns: str, task: Mapping[str, Any]) -> tuple[Any, ...] | None:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return None
    return (
        ns,
        task_id,
        str(task.get("department") or ""),
        str(task.get("status") or ""),
        str(task.get("target_device_id") or ""),
        str(task.get("employee_email") or ""),
        str(task.get("claimed_by_device_id") or ""),
        _parse_ts(task.get("created_at")),
        _parse_ts(task.get("updated_at")),
        _parse_ts(task.get("completed_at")),
        _jsonb(task),
    )


def _replace_locked(cur, ns: str, data: Mapping[str, Any]) -> None:
    devices = data.get("devices") if isinstance(data.get("devices"), Mapping) else {}
    device_ids = [str(k or "").strip() for k in devices if str(k or "").strip()]
    if device_ids:
        cur.execute(
            "DELETE FROM red_edge_devices WHERE namespace = %s AND device_id <> ALL(%s)",
            (ns, device_ids),
        )
    else:
        cur.execute("DELETE FROM red_edge_devices WHERE namespace = %s", (ns,))

    for device_id, device in devices.items():
        if not isinstance(device, Mapping):
            continue
        values = _device_values(ns, str(device_id), device)
        if values is None:
            continue
        cur.execute(
            """
            INSERT INTO red_edge_devices (
                namespace, device_id, department, employee_email, status,
                last_seen_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, device_id) DO UPDATE SET
                department = EXCLUDED.department,
                employee_email = EXCLUDED.employee_email,
                status = EXCLUDED.status,
                last_seen_at = EXCLUDED.last_seen_at,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            values,
        )

    tasks = [t for t in data.get("tasks", []) if isinstance(t, Mapping)]
    task_ids = [str(t.get("task_id") or "").strip() for t in tasks if str(t.get("task_id") or "").strip()]
    if task_ids:
        cur.execute(
            "DELETE FROM red_edge_tasks WHERE namespace = %s AND task_id <> ALL(%s)",
            (ns, task_ids),
        )
    else:
        cur.execute("DELETE FROM red_edge_tasks WHERE namespace = %s", (ns,))

    for task in tasks:
        values = _task_values(ns, task)
        if values is None:
            continue
        cur.execute(
            """
            INSERT INTO red_edge_tasks (
                namespace, task_id, department, status, target_device_id,
                employee_email, claimed_by_device_id, created_at, updated_at_ts,
                completed_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, task_id) DO UPDATE SET
                department = EXCLUDED.department,
                status = EXCLUDED.status,
                target_device_id = EXCLUDED.target_device_id,
                employee_email = EXCLUDED.employee_email,
                claimed_by_device_id = EXCLUDED.claimed_by_device_id,
                created_at = EXCLUDED.created_at,
                updated_at_ts = EXCLUDED.updated_at_ts,
                completed_at = EXCLUDED.completed_at,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            values,
        )


def replace_all(data: Mapping[str, Any]) -> None:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("LOCK TABLE red_edge_devices, red_edge_tasks IN EXCLUSIVE MODE")
            _replace_locked(cur, ns, data)


@contextlib.contextmanager
def locked_state() -> Iterator[dict[str, Any]]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE red_edge_devices, red_edge_tasks IN EXCLUSIVE MODE")
                data = _load_locked(cur, ns)
                data.setdefault("devices", {})
                data.setdefault("tasks", [])
                yield data
                _replace_locked(cur, ns, data)
