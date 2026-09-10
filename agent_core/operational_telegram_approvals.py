"""Postgres-backed Telegram private approvals and join requests."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}

_APPROVAL_COLUMNS = (
    "chat_id",
    "telegram_user_id",
    "username",
    "name",
    "color",
    "email",
    "status",
    "approved_by",
    "approved_at",
)
_JOIN_COLUMNS = (
    "chat_id",
    "telegram_user_id",
    "username",
    "name",
    "chat_type",
    "status",
    "requested_at",
    "last_seen_at",
    "last_seen_ts",
    "last_text",
    "update_id",
    "last_notified_at",
    "last_notified_at_iso",
    "resolved_at",
    "resolved_by",
    "approved_color",
    "employee_email",
)
_APPROVAL_SELECT = ", ".join(_APPROVAL_COLUMNS)
_JOIN_SELECT = ", ".join(_JOIN_COLUMNS)


def enabled() -> bool:
    backend = (
        os.environ.get("RED_TELEGRAM_APPROVALS_BACKEND")
        or os.environ.get("RED_TELEGRAM_JOIN_REQUESTS_BACKEND")
        or ""
    ).strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (
        explicit or env_bool("RED_TELEGRAM_APPROVALS_POSTGRES", False)
    )


def _namespace(value: str) -> str:
    return str(value or "").strip() or "red"


def _jsonb(value: Mapping[str, Any]):
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(dict(value))
    except Exception:
        return dict(value)


def _row_map(row: Mapping[str, Any] | tuple[Any, ...], columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(columns, row))


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: Any) -> str:
    dt = _as_datetime(value)
    return dt.isoformat() if dt else ""


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _approval_actor(row: Mapping[str, Any], *, namespace: str, default_color: str) -> dict[str, str]:
    color = str(row.get("color") or default_color or "").strip()
    if not color:
        return {}
    chat_id = str(row.get("chat_id") or "").strip()
    if not chat_id:
        return {}
    return {
        "chat_id": chat_id,
        "telegram_user_id": str(row.get("telegram_user_id") or ""),
        "color": color,
        "email": str(row.get("email") or ""),
        "name": str(row.get("name") or f"{color} Telegram user"),
        "source": f"telegram_private_approval:{namespace}",
        "is_owner": "false",
    }


def _join_record(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    data = _row_map(row, _JOIN_COLUMNS)
    record = {
        "chat_id": str(data.get("chat_id") or ""),
        "telegram_user_id": str(data.get("telegram_user_id") or ""),
        "username": str(data.get("username") or ""),
        "name": str(data.get("name") or ""),
        "chat_type": str(data.get("chat_type") or ""),
        "status": str(data.get("status") or "pending"),
        "requested_at": _iso(data.get("requested_at")),
        "last_seen_at": _iso(data.get("last_seen_at")),
        "last_seen_ts": _float(data.get("last_seen_ts")),
        "last_text": str(data.get("last_text") or ""),
        "update_id": str(data.get("update_id") or ""),
        "last_notified_at": _float(data.get("last_notified_at")),
        "last_notified_at_iso": _iso(data.get("last_notified_at_iso")),
        "resolved_at": _iso(data.get("resolved_at")),
        "resolved_by": str(data.get("resolved_by") or ""),
        "approved_color": str(data.get("approved_color") or ""),
        "employee_email": str(data.get("employee_email") or ""),
    }
    return {key: value for key, value in record.items() if value not in ("", None)}


def _write_join_record(cur, *, namespace: str, record: Mapping[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO red_telegram_join_requests (
            namespace, chat_id, telegram_user_id, username, name, chat_type,
            status, requested_at, last_seen_at, last_seen_ts, last_text,
            update_id, last_notified_at, last_notified_at_iso, resolved_at,
            resolved_by, approved_color, employee_email, payload
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (namespace, chat_id) DO UPDATE SET
            telegram_user_id = EXCLUDED.telegram_user_id,
            username = EXCLUDED.username,
            name = EXCLUDED.name,
            chat_type = EXCLUDED.chat_type,
            status = EXCLUDED.status,
            requested_at = EXCLUDED.requested_at,
            last_seen_at = EXCLUDED.last_seen_at,
            last_seen_ts = EXCLUDED.last_seen_ts,
            last_text = EXCLUDED.last_text,
            update_id = EXCLUDED.update_id,
            last_notified_at = EXCLUDED.last_notified_at,
            last_notified_at_iso = EXCLUDED.last_notified_at_iso,
            resolved_at = EXCLUDED.resolved_at,
            resolved_by = EXCLUDED.resolved_by,
            approved_color = EXCLUDED.approved_color,
            employee_email = EXCLUDED.employee_email,
            payload = EXCLUDED.payload,
            updated_at = now()
        """,
        (
            _namespace(namespace),
            str(record.get("chat_id") or ""),
            str(record.get("telegram_user_id") or ""),
            str(record.get("username") or ""),
            str(record.get("name") or ""),
            str(record.get("chat_type") or ""),
            str(record.get("status") or "pending"),
            _as_datetime(record.get("requested_at")),
            _as_datetime(record.get("last_seen_at")),
            _float(record.get("last_seen_ts")),
            str(record.get("last_text") or ""),
            str(record.get("update_id") or ""),
            _float(record.get("last_notified_at")),
            _as_datetime(record.get("last_notified_at_iso")),
            _as_datetime(record.get("resolved_at")),
            str(record.get("resolved_by") or ""),
            str(record.get("approved_color") or ""),
            str(record.get("employee_email") or ""),
            _jsonb(record),
        ),
    )


def list_approved_actors(
    *,
    namespace: str,
    default_color: str = "",
) -> dict[str, dict[str, str]]:
    ensure_schema()
    ns = _namespace(namespace)
    actors: dict[str, dict[str, str]] = {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_APPROVAL_SELECT}
                FROM red_telegram_private_approvals
                WHERE namespace = %s AND status = %s
                """,
                (ns, "approved"),
            )
            rows = cur.fetchall()
    for row in rows:
        actor = _approval_actor(
            _row_map(row, _APPROVAL_COLUMNS),
            namespace=ns,
            default_color=default_color,
        )
        if actor:
            actors[actor["chat_id"]] = actor
    return actors


def record_join_request(
    *,
    namespace: str,
    chat_id: str,
    telegram_user_id: str,
    username: str,
    name: str,
    chat_type: str,
    text: str,
    update_id: str | int,
    now_ts: float,
    notify_interval_s: int,
) -> dict[str, Any]:
    ensure_schema()
    ns = _namespace(namespace)
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        return {}
    now_dt = datetime.fromtimestamp(float(now_ts), timezone.utc)
    now_iso = now_dt.isoformat()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_JOIN_SELECT}
                FROM red_telegram_join_requests
                WHERE namespace = %s AND chat_id = %s
                FOR UPDATE
                """,
                (ns, chat_id),
            )
            row = cur.fetchone()
            existing = _join_record(row) if row else {}
            last_notified = _float(existing.get("last_notified_at"))
            should_notify = (
                str(existing.get("status") or "pending") != "pending"
                or float(now_ts) - last_notified >= int(notify_interval_s)
            )
            record = {
                **existing,
                "chat_id": chat_id,
                "telegram_user_id": str(telegram_user_id or "").strip(),
                "username": str(username or "").strip(),
                "name": str(name or ""),
                "chat_type": str(chat_type or ""),
                "status": "pending",
                "requested_at": existing.get("requested_at") or now_iso,
                "last_seen_at": now_iso,
                "last_seen_ts": float(now_ts),
                "last_text": str(text or ""),
                "update_id": str(update_id or ""),
            }
            if should_notify:
                record["last_notified_at"] = float(now_ts)
                record["last_notified_at_iso"] = now_iso
            _write_join_record(cur, namespace=ns, record=record)
    return {**record, "should_notify": should_notify}


def pending_join_requests(*, namespace: str, cutoff_ts: float) -> list[dict[str, Any]]:
    ensure_schema()
    ns = _namespace(namespace)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_JOIN_SELECT}
                FROM red_telegram_join_requests
                WHERE namespace = %s
                    AND status = %s
                    AND last_seen_ts >= %s
                ORDER BY last_seen_ts DESC
                """,
                (ns, "pending", float(cutoff_ts)),
            )
            rows = cur.fetchall()
    return [_join_record(row) for row in rows]


def save_private_approval(
    *,
    namespace: str,
    chat_id: str,
    color: str,
    name: str,
    email: str,
    telegram_user_id: str = "",
    username: str = "",
    approved_by: str = "",
) -> str:
    ensure_schema()
    ns = _namespace(namespace)
    now_dt = datetime.now(timezone.utc)
    record = {
        "namespace": ns,
        "chat_id": str(chat_id or "").strip(),
        "telegram_user_id": str(telegram_user_id or "").strip(),
        "username": str(username or "").strip(),
        "name": str(name or ""),
        "color": str(color or ""),
        "email": str(email or ""),
        "status": "approved",
        "approved_by": str(approved_by or "").strip(),
        "approved_at": now_dt.isoformat(),
    }
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_telegram_private_approvals (
                    namespace, chat_id, telegram_user_id, username, name,
                    color, email, status, approved_by, approved_at, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (namespace, chat_id) DO UPDATE SET
                    telegram_user_id = EXCLUDED.telegram_user_id,
                    username = EXCLUDED.username,
                    name = EXCLUDED.name,
                    color = EXCLUDED.color,
                    email = EXCLUDED.email,
                    status = EXCLUDED.status,
                    approved_by = EXCLUDED.approved_by,
                    approved_at = EXCLUDED.approved_at,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                """,
                (
                    ns,
                    record["chat_id"],
                    record["telegram_user_id"],
                    record["username"],
                    record["name"],
                    record["color"],
                    record["email"],
                    record["status"],
                    record["approved_by"],
                    now_dt,
                    _jsonb(record),
                ),
            )
    return record["email"]


def update_join_status(
    *,
    namespace: str,
    chat_id: str,
    status: str,
    color: str = "",
    email: str = "",
    approved_by: str = "",
) -> dict[str, Any]:
    ensure_schema()
    ns = _namespace(namespace)
    chat_id = str(chat_id or "").strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_JOIN_SELECT}
                FROM red_telegram_join_requests
                WHERE namespace = %s AND chat_id = %s
                FOR UPDATE
                """,
                (ns, chat_id),
            )
            row = cur.fetchone()
            record = _join_record(row) if row else {"chat_id": chat_id}
            record.update({
                "chat_id": chat_id,
                "status": str(status or ""),
                "resolved_at": now_iso,
                "resolved_by": str(approved_by or "").strip(),
            })
            if color:
                record["approved_color"] = str(color or "")
            if email:
                record["employee_email"] = str(email or "")
            _write_join_record(cur, namespace=ns, record=record)
    return record
