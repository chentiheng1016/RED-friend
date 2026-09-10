"""Postgres-backed Telegram confirmation and rate-limit state."""
from __future__ import annotations

import os
import re
from typing import Any

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = (
        os.environ.get("RED_TELEGRAM_AUTH_BACKEND")
        or os.environ.get("RED_TG_AUTH_BACKEND")
        or ""
    ).strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (
        explicit or env_bool("RED_TELEGRAM_AUTH_POSTGRES", False)
    )


def namespace() -> str:
    raw = (
        os.environ.get("RED_TELEGRAM_AUTH_NAMESPACE")
        or os.environ.get("RED_TELEGRAM_STATE_SUFFIX")
        or os.environ.get("RED_TELEGRAM_DEFAULT_ACTOR_COLOR")
        or "red"
    )
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw or "")).strip("-_.").lower()
    return safe or "red"


def _jsonb(value: Any):
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    except Exception:
        return value


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _history(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _row_map(row: Any) -> dict[str, Any]:
    keys = (
        "confirm_ts",
        "dangerous_confirm_ts",
        "confirm_history",
        "message_history",
        "lockout_until",
    )
    if not row:
        return {
            "confirm_ts": 0.0,
            "dangerous_confirm_ts": 0.0,
            "confirm_history": [],
            "message_history": [],
            "lockout_until": 0.0,
        }
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(keys, row))


def _select_state(cur, *, ns: str, chat_id: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT confirm_ts, dangerous_confirm_ts, confirm_history,
               message_history, lockout_until
        FROM red_tg_auth_state
        WHERE namespace = %s AND chat_id = %s
        FOR UPDATE
        """,
        (ns, chat_id),
    )
    return _row_map(cur.fetchone())


def _write_state(cur, *, ns: str, chat_id: str, state: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO red_tg_auth_state (
            namespace, chat_id, confirm_ts, dangerous_confirm_ts,
            confirm_history, message_history, lockout_until
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (namespace, chat_id) DO UPDATE SET
            confirm_ts = EXCLUDED.confirm_ts,
            dangerous_confirm_ts = EXCLUDED.dangerous_confirm_ts,
            confirm_history = EXCLUDED.confirm_history,
            message_history = EXCLUDED.message_history,
            lockout_until = EXCLUDED.lockout_until,
            updated_at = now()
        """,
        (
            ns,
            chat_id,
            _float(state.get("confirm_ts")),
            _float(state.get("dangerous_confirm_ts")),
            _jsonb(_history(state.get("confirm_history"))),
            _jsonb(_history(state.get("message_history"))),
            _float(state.get("lockout_until")),
        ),
    )


def _with_state(chat_id: str, callback):
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            state = _select_state(cur, ns=ns, chat_id=chat_id)
            result = callback(state)
            _write_state(cur, ns=ns, chat_id=chat_id, state=state)
            return result


def mark_confirmed(
    chat_id: str,
    *,
    now: float,
    rate_window_sec: int,
    rate_max_confirms: int,
    rate_lockout_sec: int,
) -> bool:
    def update(state: dict[str, Any]) -> bool:
        lock_until = _float(state.get("lockout_until"))
        if lock_until > now:
            return False
        hist = [
            ts for ts in _history(state.get("confirm_history"))
            if now - ts <= rate_window_sec
        ]
        if len(hist) >= rate_max_confirms:
            state["lockout_until"] = now + rate_lockout_sec
            state["confirm_history"] = hist
            return False
        hist.append(now)
        state["confirm_history"] = hist
        state["confirm_ts"] = now
        return True

    return bool(_with_state(chat_id, update))


def is_locked_out(chat_id: str, *, now: float) -> tuple[bool, float]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT lockout_until
                FROM red_tg_auth_state
                WHERE namespace = %s AND chat_id = %s
                """,
                (ns, chat_id),
            )
            row = cur.fetchone()
    lock_until = _float(row[0] if row else 0.0)
    remaining = lock_until - now
    return remaining > 0, max(0.0, remaining)


def check_confirmed(
    chat_id: str,
    *,
    now: float,
    window_sec: int,
) -> tuple[bool, float]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT confirm_ts
                FROM red_tg_auth_state
                WHERE namespace = %s AND chat_id = %s
                """,
                (ns, chat_id),
            )
            row = cur.fetchone()
    ts = _float(row[0] if row else 0.0)
    if ts <= 0:
        return False, -1.0
    elapsed = now - ts
    return elapsed <= window_sec, elapsed


def revoke_after_use(chat_id: str) -> None:
    def update(state: dict[str, Any]) -> None:
        state["confirm_ts"] = 0.0
        state["dangerous_confirm_ts"] = 0.0

    _with_state(chat_id, update)


def mark_dangerous_confirmed(chat_id: str, *, now: float) -> bool:
    def update(state: dict[str, Any]) -> bool:
        if _float(state.get("lockout_until")) > now:
            return False
        state["dangerous_confirm_ts"] = now
        return True

    return bool(_with_state(chat_id, update))


def check_dangerous_confirmed(
    chat_id: str,
    *,
    now: float,
    window_sec: int,
) -> tuple[bool, float]:
    ensure_schema()
    ns = namespace()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT dangerous_confirm_ts
                FROM red_tg_auth_state
                WHERE namespace = %s AND chat_id = %s
                """,
                (ns, chat_id),
            )
            row = cur.fetchone()
    ts = _float(row[0] if row else 0.0)
    if ts <= 0:
        return False, -1.0
    elapsed = now - ts
    return elapsed <= window_sec, elapsed


def check_message_rate_limit(
    chat_id: str,
    *,
    now: float,
    window_sec: int,
    max_per_window: int,
) -> tuple[bool, float, int]:
    def update(state: dict[str, Any]) -> tuple[bool, float, int]:
        hist = [
            ts for ts in _history(state.get("message_history"))
            if now - ts <= window_sec
        ]
        count = len(hist)
        if count >= max_per_window:
            retry_after = max(0.0, window_sec - (now - hist[0]))
            state["message_history"] = hist
            return False, retry_after, count
        hist.append(now)
        state["message_history"] = hist
        return True, 0.0, count + 1

    return _with_state(chat_id, update)
