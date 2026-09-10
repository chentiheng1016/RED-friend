"""Postgres-backed tool budget counters."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = os.environ.get("RED_TOOL_BUDGETS_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_TOOL_BUDGETS_POSTGRES", False))


def _jsonb(value: Mapping[str, Any]):
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(dict(value))
    except Exception:
        return dict(value)


def _day(value: str | None = None) -> date:
    text = str(value or "").strip()
    if text:
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    return date.today()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _row_record(row: Mapping[str, Any] | tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    if not isinstance(row, Mapping):
        keys = ("tool", "daily", "hour", "hour_count", "last_at", "by_caller")
        row = dict(zip(keys, row))
    rec = {
        "daily": int(row.get("daily") or 0),
        "hour": str(row.get("hour") or ""),
        "hour_count": int(row.get("hour_count") or 0),
        "last_at": _iso(row.get("last_at")) or "—",
    }
    by_caller = row.get("by_caller") or {}
    if isinstance(by_caller, dict) and by_caller:
        rec["by_caller"] = by_caller
    return str(row.get("tool") or ""), rec


def load_day(day_str: str | None = None) -> dict[str, Any]:
    ensure_schema()
    day = _day(day_str)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tool, daily, hour, hour_count, last_at, by_caller
                FROM red_tool_budget_usage
                WHERE day = %s
                """,
                (day,),
            )
            rows = cur.fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        tool, rec = _row_record(row)
        if tool:
            out[tool] = rec
    return out


def _select_for_update(cur, *, day: date, tool_name: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT tool, daily, hour, hour_count, last_at, by_caller
        FROM red_tool_budget_usage
        WHERE day = %s AND tool = %s
        FOR UPDATE
        """,
        (day, tool_name),
    )
    row = cur.fetchone()
    if not row:
        return {"daily": 0, "hour": "", "hour_count": 0}
    _tool, rec = _row_record(row)
    return rec


def _upsert_record(
    cur,
    *,
    day: date,
    tool_name: str,
    rec: Mapping[str, Any],
) -> None:
    last_at_raw = str(rec.get("last_at") or "")
    last_at = None
    if last_at_raw and last_at_raw != "—":
        try:
            last_at = datetime.fromisoformat(last_at_raw)
        except ValueError:
            last_at = None
    cur.execute(
        """
        INSERT INTO red_tool_budget_usage (
            day, tool, daily, hour, hour_count, last_at, by_caller
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (day, tool) DO UPDATE SET
            daily = EXCLUDED.daily,
            hour = EXCLUDED.hour,
            hour_count = EXCLUDED.hour_count,
            last_at = EXCLUDED.last_at,
            by_caller = EXCLUDED.by_caller,
            updated_at = now()
        """,
        (
            day,
            tool_name,
            int(rec.get("daily") or 0),
            str(rec.get("hour") or ""),
            int(rec.get("hour_count") or 0),
            last_at,
            _jsonb(rec.get("by_caller") or {}),
        ),
    )


def record_use(
    tool_name: str,
    *,
    caller_key: str = "",
    cur_hour: str,
    now_iso: str,
    retain_days: int = 30,
) -> None:
    ensure_schema()
    day = _day()
    cutoff = day - timedelta(days=max(1, int(retain_days)))
    with connect() as conn:
        with conn.cursor() as cur:
            rec = _select_for_update(cur, day=day, tool_name=tool_name)
            rec["daily"] = int(rec.get("daily") or 0) + 1
            if rec.get("hour") != cur_hour:
                rec["hour"] = cur_hour
                rec["hour_count"] = 0
            rec["hour_count"] = int(rec.get("hour_count") or 0) + 1
            rec["last_at"] = now_iso

            if caller_key:
                by_caller = rec.get("by_caller") or {}
                cr = by_caller.get(caller_key) or {
                    "daily": 0,
                    "hour": "",
                    "hour_count": 0,
                }
                cr["daily"] = int(cr.get("daily") or 0) + 1
                if cr.get("hour") != cur_hour:
                    cr["hour"] = cur_hour
                    cr["hour_count"] = 0
                cr["hour_count"] = int(cr.get("hour_count") or 0) + 1
                cr["last_at"] = now_iso
                by_caller[caller_key] = cr
                rec["by_caller"] = by_caller

            _upsert_record(cur, day=day, tool_name=tool_name, rec=rec)
            cur.execute("DELETE FROM red_tool_budget_usage WHERE day < %s", (cutoff,))


def reset_budget(tool_name: str) -> bool:
    ensure_schema()
    day = _day()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM red_tool_budget_usage WHERE day = %s AND tool = %s",
                (day, tool_name),
            )
            count = getattr(cur, "rowcount", 0)
    return bool(count)
