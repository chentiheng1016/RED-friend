"""Postgres-backed work mode state and history."""
from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime, timedelta
from typing import Any, Iterator, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}
DEFAULT_MODE = "normal"


def enabled() -> bool:
    backend = os.environ.get("RED_WORK_MODE_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_WORK_MODE_POSTGRES", False))


def namespace() -> str:
    return os.environ.get("RED_WORK_MODE_NAMESPACE", "default").strip() or "default"


def _default_state() -> dict[str, Any]:
    return {"mode": DEFAULT_MODE, "set_at": "", "expires_at": "", "set_by": ""}


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


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _state_from_row(row: Mapping[str, Any] | tuple[Any, ...] | None) -> dict[str, Any]:
    if not row:
        return _default_state()
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = dict(zip(("mode", "set_at", "expires_at", "set_by", "payload"), row))
    payload = data.get("payload") or {}
    out = _default_state()
    if isinstance(payload, dict):
        out.update(payload)
    if data.get("mode"):
        out["mode"] = str(data.get("mode") or DEFAULT_MODE)
    if not out.get("set_at"):
        out["set_at"] = _iso_local(data.get("set_at"))
    if not out.get("expires_at"):
        out["expires_at"] = _iso_local(data.get("expires_at"))
    if data.get("set_by"):
        out["set_by"] = str(data.get("set_by") or "")
    return out


def _history_from_row(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = dict(
            zip(
                (
                    "at", "from_mode", "to_mode", "set_by", "reason",
                    "duration_minutes", "payload",
                ),
                row,
            )
        )
    payload = data.get("payload") or {}
    out = dict(payload) if isinstance(payload, dict) else {}
    out.update({
        "at": _iso_local(data.get("at")),
        "from_mode": str(data.get("from_mode") or ""),
        "to_mode": str(data.get("to_mode") or ""),
        "set_by": str(data.get("set_by") or ""),
        "reason": str(data.get("reason") or ""),
        "duration_minutes": _as_int(data.get("duration_minutes")),
    })
    return out


def _state_values(ns: str, state: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        ns,
        str(state.get("mode") or DEFAULT_MODE),
        _parse_ts(state.get("set_at")),
        _parse_ts(state.get("expires_at")),
        str(state.get("set_by") or ""),
        _jsonb(state),
    )


def _save_locked(cur, ns: str, state: Mapping[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO red_work_modes (
            namespace, mode, set_at, expires_at, set_by, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (namespace) DO UPDATE SET
            mode = EXCLUDED.mode,
            set_at = EXCLUDED.set_at,
            expires_at = EXCLUDED.expires_at,
            set_by = EXCLUDED.set_by,
            payload = EXCLUDED.payload,
            updated_at = now()
        """,
        _state_values(ns, state),
    )


def load_mode_state() -> dict[str, Any]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mode, set_at, expires_at, set_by, payload
                FROM red_work_modes
                WHERE namespace = %s
                """,
                (ns,),
            )
            return _state_from_row(cur.fetchone())


def save_mode_state(state: Mapping[str, Any]) -> None:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            _save_locked(cur, ns, state)


def append_history(entry: Mapping[str, Any]) -> None:
    record = dict(entry)
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_work_mode_history (
                    namespace, at, from_mode, to_mode, set_by, reason,
                    duration_minutes, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    namespace(),
                    _parse_ts(record.get("at")) or datetime.now().astimezone(),
                    str(record.get("from_mode") or ""),
                    str(record.get("to_mode") or ""),
                    str(record.get("set_by") or ""),
                    str(record.get("reason") or ""),
                    _as_int(record.get("duration_minutes")),
                    _jsonb(record),
                ),
            )


def load_history(*, hours: int = 24, limit: int = 5000) -> list[dict[str, Any]]:
    cutoff = datetime.now().astimezone() - timedelta(hours=max(1, int(hours or 1)))
    lim = max(1, min(int(limit or 5000), 50000))
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    at, from_mode, to_mode, set_by, reason,
                    duration_minutes, payload
                FROM red_work_mode_history
                WHERE namespace = %s AND at >= %s
                ORDER BY at DESC, id DESC
                LIMIT %s
                """,
                (namespace(), cutoff, lim),
            )
            rows = cur.fetchall()
    return [_history_from_row(row) for row in reversed(rows)]


@contextlib.contextmanager
def locked_mode_state() -> Iterator[dict[str, Any]]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE red_work_modes IN EXCLUSIVE MODE")
                cur.execute(
                    """
                    SELECT mode, set_at, expires_at, set_by, payload
                    FROM red_work_modes
                    WHERE namespace = %s
                    """,
                    (ns,),
                )
                data = _state_from_row(cur.fetchone())
                yield data
                _save_locked(cur, ns, data)
