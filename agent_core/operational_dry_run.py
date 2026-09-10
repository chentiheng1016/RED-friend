"""Postgres-backed dry-run mode state."""
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
    backend = os.environ.get("RED_DRY_RUN_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_DRY_RUN_POSTGRES", False))


def namespace() -> str:
    return os.environ.get("RED_DRY_RUN_NAMESPACE", "default").strip() or "default"


def _default_state() -> dict[str, Any]:
    return {"enabled": False, "enabled_at": None, "simulated_calls": []}


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"_raw": str(value)}


def _jsonb(value: Any):
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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)][-200:]


def _state_from_row(row: Mapping[str, Any] | tuple[Any, ...] | None) -> dict[str, Any]:
    if not row:
        return _default_state()
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = dict(
            zip(
                (
                    "enabled", "enabled_at", "enabled_at_text",
                    "simulated_calls", "payload",
                ),
                row,
            )
        )
    payload = data.get("payload") or {}
    out = _default_state()
    if isinstance(payload, dict):
        out.update(payload)
    out["enabled"] = _as_bool(data.get("enabled"))
    out["enabled_at"] = (
        str(data.get("enabled_at_text") or "")
        or _iso_local(data.get("enabled_at"))
        or None
    )
    out["simulated_calls"] = _calls(
        data.get("simulated_calls") or out.get("simulated_calls")
    )
    return out


def _state_values(ns: str, state: Mapping[str, Any]) -> tuple[Any, ...]:
    payload = {
        "enabled": _as_bool(state.get("enabled")),
        "enabled_at": state.get("enabled_at"),
        "simulated_calls": _calls(state.get("simulated_calls")),
    }
    return (
        ns,
        payload["enabled"],
        _parse_ts(payload.get("enabled_at")),
        str(payload.get("enabled_at") or ""),
        _jsonb(payload["simulated_calls"]),
        _jsonb(payload),
    )


def _save_locked(cur, ns: str, state: Mapping[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO red_dry_run_state (
            namespace, enabled, enabled_at, enabled_at_text,
            simulated_calls, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (namespace) DO UPDATE SET
            enabled = EXCLUDED.enabled,
            enabled_at = EXCLUDED.enabled_at,
            enabled_at_text = EXCLUDED.enabled_at_text,
            simulated_calls = EXCLUDED.simulated_calls,
            payload = EXCLUDED.payload,
            updated_at = now()
        """,
        _state_values(ns, state),
    )


def load_state() -> dict[str, Any]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled, enabled_at, enabled_at_text, simulated_calls, payload
                FROM red_dry_run_state
                WHERE namespace = %s
                """,
                (ns,),
            )
            return _state_from_row(cur.fetchone())


def save_state(state: Mapping[str, Any]) -> None:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            _save_locked(cur, ns, state)


@contextlib.contextmanager
def locked_state() -> Iterator[dict[str, Any]]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE red_dry_run_state IN EXCLUSIVE MODE")
                cur.execute(
                    """
                    SELECT
                        enabled, enabled_at, enabled_at_text,
                        simulated_calls, payload
                    FROM red_dry_run_state
                    WHERE namespace = %s
                    """,
                    (ns,),
                )
                data = _state_from_row(cur.fetchone())
                yield data
                _save_locked(cur, ns, data)
