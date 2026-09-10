"""Postgres-backed shared Gemini circuit breaker state."""
from __future__ import annotations

import os
import time
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import (
    connect,
    enabled as db_enabled,
    ensure_schema_once as ensure_schema,
)

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = os.environ.get("RED_GEMINI_CIRCUIT_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_GEMINI_CIRCUIT_POSTGRES", False))


def _row_dict(row: Mapping[str, Any] | tuple[Any, ...], keys: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(keys, row))


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def open_state(model: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Return the active open circuit state for `model`, if any.

    Expired open states are deleted so the next failure starts a fresh window,
    matching the in-process circuit breaker's half-open behavior.
    """
    key = (model or "?").strip() or "?"
    ts = time.time() if now is None else float(now)
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT opened_until, reason
                FROM red_gemini_circuit_state
                WHERE model = %s
                """,
                (key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            data = _row_dict(row, ("opened_until", "reason"))
            opened_until = _as_float(data.get("opened_until"))
            if opened_until > ts:
                return {
                    "model": key,
                    "opened_until": opened_until,
                    "reason": str(data.get("reason") or "recent Gemini failures"),
                }
            if opened_until:
                cur.execute(
                    "DELETE FROM red_gemini_circuit_state WHERE model = %s",
                    (key,),
                )
            return None


def record_failure(
    model: str,
    *,
    reason: str,
    now: float | None = None,
    threshold: int,
    window_s: int,
    open_s: int,
) -> dict[str, Any]:
    """Record one final transient failure and return the updated circuit state."""
    key = (model or "?").strip() or "?"
    ts = time.time() if now is None else float(now)
    threshold = max(1, int(threshold))
    window_s = max(1, int(window_s))
    open_s = max(1, int(open_s))
    opened_until_if_threshold = ts + open_s
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_gemini_circuit_state (
                    model, failures, first_failure_at, last_failure_at,
                    opened_until, reason, updated_at
                )
                VALUES (
                    %s, 1, %s, %s,
                    CASE WHEN 1 >= %s THEN %s ELSE 0 END,
                    %s, now()
                )
                ON CONFLICT (model) DO UPDATE SET
                    failures = CASE
                        WHEN EXCLUDED.last_failure_at
                             - red_gemini_circuit_state.last_failure_at > %s
                        THEN 1
                        ELSE red_gemini_circuit_state.failures + 1
                    END,
                    first_failure_at = CASE
                        WHEN EXCLUDED.last_failure_at
                             - red_gemini_circuit_state.last_failure_at > %s
                        THEN EXCLUDED.first_failure_at
                        ELSE red_gemini_circuit_state.first_failure_at
                    END,
                    last_failure_at = EXCLUDED.last_failure_at,
                    opened_until = CASE
                        WHEN (
                            CASE
                                WHEN EXCLUDED.last_failure_at
                                     - red_gemini_circuit_state.last_failure_at > %s
                                THEN 1
                                ELSE red_gemini_circuit_state.failures + 1
                            END
                        ) >= %s
                        THEN %s
                        ELSE 0
                    END,
                    reason = EXCLUDED.reason,
                    updated_at = now()
                RETURNING
                    failures, first_failure_at, last_failure_at,
                    opened_until, reason
                """,
                (
                    key,
                    ts,
                    ts,
                    threshold,
                    opened_until_if_threshold,
                    reason,
                    window_s,
                    window_s,
                    window_s,
                    threshold,
                    opened_until_if_threshold,
                ),
            )
            row = cur.fetchone()
    data = _row_dict(
        row or (),
        ("failures", "first_failure_at", "last_failure_at", "opened_until", "reason"),
    )
    return {
        "model": key,
        "failures": _as_int(data.get("failures")),
        "first_failure_at": _as_float(data.get("first_failure_at")),
        "last_failure_at": _as_float(data.get("last_failure_at")),
        "opened_until": _as_float(data.get("opened_until")),
        "reason": str(data.get("reason") or ""),
    }


def clear_state(model: str) -> None:
    key = (model or "?").strip() or "?"
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM red_gemini_circuit_state WHERE model = %s",
                (key,),
            )
