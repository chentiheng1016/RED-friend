"""Postgres-backed run history storage."""
from __future__ import annotations

import os
import json
from datetime import datetime, timedelta
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = os.environ.get("RED_RUN_HISTORY_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_RUN_HISTORY_POSTGRES", False))


def _jsonb(value: Mapping[str, Any]):
    value = _json_safe(value)
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(value)
    except Exception:
        return value


def _json_safe(value: Any) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"_raw": str(value)}


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


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _short_result(record: Mapping[str, Any]) -> str:
    return str(record.get("short_result") or record.get("result") or "")[:200]


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def write_run_record(record: Mapping[str, Any]) -> None:
    payload = dict(record)
    started_at = _parse_ts(payload.get("started_at")) or datetime.now().astimezone()
    ended_at = _parse_ts(payload.get("ended_at"))
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_run_history (
                    id, tool, started_at, ended_at, status, elapsed_sec, ok,
                    error_code, recoverable, short_result, args_text,
                    kwargs_text, payload
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    tool = EXCLUDED.tool,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    status = EXCLUDED.status,
                    elapsed_sec = EXCLUDED.elapsed_sec,
                    ok = EXCLUDED.ok,
                    error_code = EXCLUDED.error_code,
                    recoverable = EXCLUDED.recoverable,
                    short_result = EXCLUDED.short_result,
                    args_text = EXCLUDED.args_text,
                    kwargs_text = EXCLUDED.kwargs_text,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                """,
                (
                    str(payload.get("id") or ""),
                    str(payload.get("tool") or ""),
                    started_at,
                    ended_at,
                    str(payload.get("status") or ""),
                    _as_float(payload.get("elapsed_sec")),
                    _bool_or_none(payload.get("ok")),
                    str(payload.get("error_code") or ""),
                    _bool_or_none(payload.get("recoverable")),
                    _short_result(payload),
                    str(payload.get("args") or ""),
                    str(payload.get("kwargs") or ""),
                    _jsonb(payload),
                ),
            )


def _entry_from_row(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        keys = (
            "id", "tool", "started_at", "ended_at", "status", "elapsed_sec",
            "short_result", "ok", "error_code", "recoverable", "args_text",
            "kwargs_text",
        )
        row = dict(zip(keys, row))
    out: dict[str, Any] = {
        "id": str(row.get("id") or ""),
        "tool": str(row.get("tool") or ""),
        "started_at": _iso_local(row.get("started_at")),
        "ended_at": _iso_local(row.get("ended_at")),
        "status": str(row.get("status") or ""),
        "elapsed_sec": _as_float(row.get("elapsed_sec")),
        "short_result": str(row.get("short_result") or ""),
        "args": str(row.get("args_text") or ""),
        "kwargs": str(row.get("kwargs_text") or ""),
    }
    if row.get("ok") is not None:
        out["ok"] = bool(row.get("ok"))
    if row.get("error_code"):
        out["error_code"] = str(row.get("error_code") or "")
    if row.get("recoverable") is not None:
        out["recoverable"] = bool(row.get("recoverable"))
    return out


def _select_columns() -> str:
    return (
        "id, tool, started_at, ended_at, status, elapsed_sec, short_result, "
        "ok, error_code, recoverable, args_text, kwargs_text"
    )


def list_entries(
    *,
    tool_name: str = "",
    status: str = "",
    since_hours: int = 24,
    limit: int = 20,
) -> list[dict[str, Any]]:
    cutoff = datetime.now().astimezone() - timedelta(hours=max(1, int(since_hours)))
    clauses = ["started_at >= %s"]
    params: list[Any] = [cutoff]
    if tool_name:
        clauses.append("tool = %s")
        params.append(tool_name)
    if status:
        clauses.append("status = %s")
        params.append(status)
    params.append(max(1, min(int(limit or 20), 200)))
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_select_columns()}
                FROM red_run_history
                WHERE {' AND '.join(clauses)}
                ORDER BY started_at DESC, id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    return [_entry_from_row(row) for row in rows]


def load_metrics_entries(*, hours: int = 24, limit: int = 5000) -> list[dict[str, Any]]:
    cutoff = datetime.now().astimezone() - timedelta(hours=max(1, int(hours)))
    lim = max(1, min(int(limit or 5000), 50000))
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_select_columns()}
                FROM red_run_history
                WHERE started_at >= %s
                ORDER BY started_at ASC, id ASC
                LIMIT %s
                """,
                (cutoff, lim),
            )
            rows = cur.fetchall()
    return [_entry_from_row(row) for row in rows]


def find_entries(*, query: str, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
    cutoff = datetime.now().astimezone() - timedelta(days=max(1, int(days)))
    pattern = f"%{query.lower()}%"
    lim = max(1, min(int(limit or 20), 200))
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_select_columns()}
                FROM red_run_history
                WHERE started_at >= %s
                  AND (
                    lower(tool) LIKE %s
                    OR lower(args_text) LIKE %s
                    OR lower(kwargs_text) LIKE %s
                    OR lower(short_result) LIKE %s
                  )
                ORDER BY started_at DESC, id DESC
                LIMIT %s
                """,
                (cutoff, pattern, pattern, pattern, pattern, lim),
            )
            rows = cur.fetchall()
    return [_entry_from_row(row) for row in rows]


def read_run(run_id: str) -> dict[str, Any] | None:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM red_run_history WHERE id = %s",
                (str(run_id or ""),),
            )
            row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, Mapping):
        payload = row.get("payload")
    else:
        payload = row[0] if isinstance(row, (tuple, list)) else row
    return dict(payload) if isinstance(payload, dict) else None


def stats() -> dict[str, Any]:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (WHERE status = 'success'),
                    count(*) FILTER (WHERE status = 'error')
                FROM red_run_history
                """
            )
            counts = cur.fetchone() or (0, 0, 0)
            cur.execute(
                """
                SELECT tool, count(*)
                FROM red_run_history
                GROUP BY tool
                ORDER BY count(*) DESC, tool ASC
                LIMIT 10
                """
            )
            tool_counts = cur.fetchall()
            cur.execute(
                """
                SELECT tool, count(*)
                FROM red_run_history
                WHERE status = 'error'
                GROUP BY tool
                ORDER BY count(*) DESC, tool ASC
                LIMIT 5
                """
            )
            error_tools = cur.fetchall()
    return {
        "total": int(counts[0] or 0),
        "success": int(counts[1] or 0),
        "error": int(counts[2] or 0),
        "tool_counts": [(str(row[0] or ""), int(row[1] or 0)) for row in tool_counts],
        "error_tools": [(str(row[0] or ""), int(row[1] or 0)) for row in error_tools],
    }


def prune_old_runs(days: int) -> int:
    cutoff = datetime.now().astimezone() - timedelta(days=max(1, int(days)))
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM red_run_history WHERE started_at < %s",
                (cutoff,),
            )
            count = int(getattr(cur, "rowcount", 0) or 0)
    return count
