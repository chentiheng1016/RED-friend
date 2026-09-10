"""Postgres-backed cost and external API error logs."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = os.environ.get("RED_COST_TRACKER_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_COST_TRACKER_POSTGRES", False))


def _jsonb(value: Mapping[str, Any]):
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(dict(value))
    except Exception:
        return dict(value)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if text:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now().astimezone()
        else:
            dt = datetime.now().astimezone()
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


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def write_cost_entry(entry: Mapping[str, Any]) -> None:
    record = dict(entry)
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_cost_events (
                    ts, model, prompt_tokens, output_tokens, thinking_tokens,
                    cached_tokens, tool_use_tokens, total_tokens, cost_usd,
                    duration_ms, caller, payload
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    _parse_ts(record.get("ts")),
                    str(record.get("model") or ""),
                    _as_int(record.get("prompt_tokens")),
                    _as_int(record.get("output_tokens")),
                    _as_int(record.get("thinking_tokens")),
                    _as_int(record.get("cached_tokens")),
                    _as_int(record.get("tool_use_tokens")),
                    _as_int(record.get("total_tokens")),
                    _as_float(record.get("cost_usd")),
                    _as_float(record.get("duration_ms")),
                    str(record.get("caller") or ""),
                    _jsonb(record),
                ),
            )


def write_api_error(entry: Mapping[str, Any]) -> None:
    record = dict(entry)
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_api_error_events (
                    ts, service, status, model, detail, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    _parse_ts(record.get("ts")),
                    str(record.get("service") or ""),
                    str(record.get("status") or ""),
                    str(record.get("model") or ""),
                    str(record.get("detail") or ""),
                    _jsonb(record),
                ),
            )


def _hours_clause(hours: int | None) -> tuple[str, tuple[Any, ...]]:
    if hours is None:
        return "", ()
    h = max(1, int(hours or 1))
    cutoff = datetime.now().astimezone() - timedelta(hours=h)
    return "WHERE ts >= %s", (cutoff,)


def _row_dict(row: Mapping[str, Any] | tuple[Any, ...], keys: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(keys, row))


def _cost_row(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "ts", "model", "prompt_tokens", "output_tokens", "thinking_tokens",
        "cached_tokens", "tool_use_tokens", "total_tokens", "cost_usd",
        "duration_ms", "caller", "payload",
    )
    data = _row_dict(row, keys)
    payload = data.get("payload") or {}
    out = dict(payload) if isinstance(payload, dict) else {}
    out.update({
        "ts": _iso_local(data.get("ts")),
        "model": str(data.get("model") or ""),
        "prompt_tokens": _as_int(data.get("prompt_tokens")),
        "output_tokens": _as_int(data.get("output_tokens")),
        "thinking_tokens": _as_int(data.get("thinking_tokens")),
        "cached_tokens": _as_int(data.get("cached_tokens")),
        "tool_use_tokens": _as_int(data.get("tool_use_tokens")),
        "total_tokens": _as_int(data.get("total_tokens")),
        "cost_usd": _as_float(data.get("cost_usd")),
        "duration_ms": _as_float(data.get("duration_ms")),
        "caller": str(data.get("caller") or ""),
    })
    return out


def _api_error_row(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    keys = ("ts", "service", "status", "model", "detail", "payload")
    data = _row_dict(row, keys)
    payload = data.get("payload") or {}
    out = dict(payload) if isinstance(payload, dict) else {}
    out.update({
        "ts": _iso_local(data.get("ts")),
        "service": str(data.get("service") or ""),
        "status": str(data.get("status") or ""),
        "model": str(data.get("model") or ""),
        "detail": str(data.get("detail") or ""),
    })
    return out


def load_cost_entries(hours: int | None = None) -> list[dict[str, Any]]:
    ensure_schema()
    where, params = _hours_clause(hours)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    ts, model, prompt_tokens, output_tokens, thinking_tokens,
                    cached_tokens, tool_use_tokens, total_tokens, cost_usd,
                    duration_ms, caller, payload
                FROM red_cost_events
                {where}
                ORDER BY ts ASC, id ASC
                """,
                params,
            )
            rows = cur.fetchall()
    return [_cost_row(row) for row in rows]


def load_api_errors(hours: int | None = None) -> list[dict[str, Any]]:
    ensure_schema()
    where, params = _hours_clause(hours)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ts, service, status, model, detail, payload
                FROM red_api_error_events
                {where}
                ORDER BY ts ASC, id ASC
                """,
                params,
            )
            rows = cur.fetchall()
    return [_api_error_row(row) for row in rows]
