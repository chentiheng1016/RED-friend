"""Postgres-backed task_memory storage."""
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
    backend = os.environ.get("RED_TASK_MEMORY_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_TASK_MEMORY_POSTGRES", False))


def namespace() -> str:
    return os.environ.get("RED_TASK_MEMORY_NAMESPACE", "default").strip() or "default"


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
            try:
                dt = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _task_payload(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        payload = row.get("payload")
    else:
        payload = row[0] if row else None
    return dict(payload) if isinstance(payload, dict) else {}


def _load_locked(cur, ns: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT payload
        FROM red_task_memory
        WHERE namespace = %s
        ORDER BY created_at ASC NULLS LAST, id ASC
        """,
        (ns,),
    )
    return {"version": 1, "tasks": [_task_payload(row) for row in cur.fetchall()]}


def load_tasks() -> dict[str, Any]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            return _load_locked(cur, ns)


def _task_values(ns: str, task: Mapping[str, Any]) -> tuple[Any, ...] | None:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return None
    priority = 5
    try:
        priority = int(task.get("priority") or 5)
    except (TypeError, ValueError):
        priority = 5
    return (
        ns,
        task_id,
        str(task.get("status") or ""),
        priority,
        str(task.get("title") or "")[:200],
        _parse_ts(task.get("created_at")),
        str(task.get("deadline") or ""),
        _parse_ts(task.get("deadline")),
        str(task.get("next_reminder_at") or ""),
        _parse_ts(task.get("next_reminder_at")),
        str(task.get("linked_customer") or ""),
        _jsonb(task),
    )


def _replace_locked(cur, ns: str, data: Mapping[str, Any]) -> None:
    tasks = [t for t in data.get("tasks", []) if isinstance(t, Mapping)]
    ids = [str(t.get("id") or "").strip() for t in tasks if str(t.get("id") or "").strip()]
    if ids:
        cur.execute(
            "DELETE FROM red_task_memory WHERE namespace = %s AND id <> ALL(%s)",
            (ns, ids),
        )
    else:
        cur.execute("DELETE FROM red_task_memory WHERE namespace = %s", (ns,))

    for task in tasks:
        values = _task_values(ns, task)
        if values is None:
            continue
        cur.execute(
            """
            INSERT INTO red_task_memory (
                namespace, id, status, priority, title_preview, created_at,
                deadline, deadline_at, next_reminder_at, next_reminder_at_ts,
                linked_customer, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, id) DO UPDATE SET
                status = EXCLUDED.status,
                priority = EXCLUDED.priority,
                title_preview = EXCLUDED.title_preview,
                created_at = EXCLUDED.created_at,
                deadline = EXCLUDED.deadline,
                deadline_at = EXCLUDED.deadline_at,
                next_reminder_at = EXCLUDED.next_reminder_at,
                next_reminder_at_ts = EXCLUDED.next_reminder_at_ts,
                linked_customer = EXCLUDED.linked_customer,
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
            cur.execute("LOCK TABLE red_task_memory IN EXCLUSIVE MODE")
            _replace_locked(cur, ns, data)


@contextlib.contextmanager
def locked_tasks() -> Iterator[dict[str, Any]]:
    """Yield all tasks under a transaction lock and save on clean exit."""
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE red_task_memory IN EXCLUSIVE MODE")
                data = _load_locked(cur, ns)
                data.setdefault("tasks", [])
                yield data
                _replace_locked(cur, ns, data)
