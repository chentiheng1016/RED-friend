"""Postgres-backed alert push de-dupe state."""
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
    backend = (
        os.environ.get("RED_ALERT_PUSH_BACKEND")
        or os.environ.get("RED_ALERT_PUSHER_BACKEND")
        or ""
    ).strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_ALERT_PUSH_POSTGRES", False))


def namespace() -> str:
    return os.environ.get("RED_ALERT_PUSH_NAMESPACE", "default").strip() or "default"


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


def _iso_local(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def _row_state(row: Mapping[str, Any] | tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = dict(
            zip(
                (
                    "alert_id", "first_seen_at", "first_seen_at_text",
                    "last_pushed_at", "last_pushed_at_text", "title",
                    "level", "reason", "payload",
                ),
                row,
            )
        )
    alert_id = str(data.get("alert_id") or "")
    payload = data.get("payload") or {}
    out = dict(payload) if isinstance(payload, dict) else {}
    out.update({
        "first_seen_at": (
            str(data.get("first_seen_at_text") or "")
            or _iso_local(data.get("first_seen_at"))
        ),
        "last_pushed_at": (
            str(data.get("last_pushed_at_text") or "")
            or _iso_local(data.get("last_pushed_at"))
        ),
        "title": str(data.get("title") or ""),
        "level": str(data.get("level") or ""),
        "reason": str(data.get("reason") or ""),
    })
    return alert_id, out


def _load_locked(cur, ns: str) -> dict[str, dict[str, Any]]:
    cur.execute(
        """
        SELECT
            alert_id, first_seen_at, first_seen_at_text,
            last_pushed_at, last_pushed_at_text, title, level, reason, payload
        FROM red_alert_push_state
        WHERE namespace = %s
        ORDER BY alert_id ASC
        """,
        (ns,),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        alert_id, state = _row_state(row)
        if alert_id:
            out[alert_id] = state
    return out


def _values(ns: str, alert_id: str, state: Mapping[str, Any]) -> tuple[Any, ...] | None:
    aid = str(alert_id or "").strip()
    if not aid:
        return None
    first_seen = str(state.get("first_seen_at") or "")
    last_pushed = str(state.get("last_pushed_at") or "")
    return (
        ns,
        aid,
        _parse_ts(first_seen),
        first_seen,
        _parse_ts(last_pushed),
        last_pushed,
        str(state.get("title") or ""),
        str(state.get("level") or ""),
        str(state.get("reason") or ""),
        _jsonb(state),
    )


def _replace_locked(cur, ns: str, state: Mapping[str, Any]) -> None:
    alert_ids = [str(k or "").strip() for k in state if str(k or "").strip()]
    if alert_ids:
        cur.execute(
            "DELETE FROM red_alert_push_state WHERE namespace = %s AND alert_id <> ALL(%s)",
            (ns, alert_ids),
        )
    else:
        cur.execute("DELETE FROM red_alert_push_state WHERE namespace = %s", (ns,))

    for alert_id, info in state.items():
        if not isinstance(info, Mapping):
            continue
        values = _values(ns, str(alert_id), info)
        if values is None:
            continue
        cur.execute(
            """
            INSERT INTO red_alert_push_state (
                namespace, alert_id, first_seen_at, first_seen_at_text,
                last_pushed_at, last_pushed_at_text, title, level, reason,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, alert_id) DO UPDATE SET
                first_seen_at = EXCLUDED.first_seen_at,
                first_seen_at_text = EXCLUDED.first_seen_at_text,
                last_pushed_at = EXCLUDED.last_pushed_at,
                last_pushed_at_text = EXCLUDED.last_pushed_at_text,
                title = EXCLUDED.title,
                level = EXCLUDED.level,
                reason = EXCLUDED.reason,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            values,
        )


def load_state() -> dict[str, dict[str, Any]]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            return _load_locked(cur, ns)


def replace_state(state: Mapping[str, Any]) -> None:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("LOCK TABLE red_alert_push_state IN EXCLUSIVE MODE")
            _replace_locked(cur, ns, state)


@contextlib.contextmanager
def locked_state() -> Iterator[dict[str, dict[str, Any]]]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE red_alert_push_state IN EXCLUSIVE MODE")
                data = _load_locked(cur, ns)
                yield data
                _replace_locked(cur, ns, data)
