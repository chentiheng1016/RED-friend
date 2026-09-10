"""Postgres-backed policy decision log."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = os.environ.get("RED_POLICY_ENGINE_BACKEND", "").strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_POLICY_ENGINE_POSTGRES", False))


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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def write_decision(record: Mapping[str, Any]) -> None:
    payload = dict(record)
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_policy_decisions (
                    ts, caller, tool, channel, decision, reason_layer, reason,
                    risk_score, forced_dry_run, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _parse_ts(payload.get("at") or payload.get("ts")),
                    str(payload.get("caller") or payload.get("user") or ""),
                    str(payload.get("tool") or ""),
                    str(payload.get("channel") or ""),
                    "allow" if payload.get("allow") else "refuse",
                    str(payload.get("reason_layer") or ""),
                    str(payload.get("reason") or ""),
                    _as_int(payload.get("risk_score")),
                    _as_bool(payload.get("forced_dry_run")),
                    _jsonb(payload),
                ),
            )


def _row_dict(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    keys = (
        "ts", "caller", "tool", "channel", "decision", "reason_layer",
        "reason", "risk_score", "forced_dry_run", "payload",
    )
    return dict(zip(keys, row))


def _decision_from_row(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    data = _row_dict(row)
    payload = data.get("payload") or {}
    out = dict(payload) if isinstance(payload, dict) else {}
    decision = str(data.get("decision") or "")
    allow = decision == "allow" if decision else _as_bool(out.get("allow"))
    out.update({
        "at": _iso_local(data.get("ts")),
        "caller": str(data.get("caller") or ""),
        "tool": str(data.get("tool") or ""),
        "channel": str(data.get("channel") or ""),
        "allow": allow,
        "reason_layer": str(data.get("reason_layer") or ""),
        "reason": str(data.get("reason") or ""),
        "risk_score": _as_int(data.get("risk_score")),
        "forced_dry_run": _as_bool(data.get("forced_dry_run")),
    })
    return out


def load_decisions(hours: int | None = None, limit: int = 5000) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if hours is not None:
        cutoff = datetime.now().astimezone() - timedelta(hours=max(1, int(hours or 1)))
        clauses.append("ts >= %s")
        params.append(cutoff)
    lim = max(1, min(int(limit or 5000), 50000))
    params.append(lim)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    ts, caller, tool, channel, decision, reason_layer, reason,
                    risk_score, forced_dry_run, payload
                FROM red_policy_decisions
                {where}
                ORDER BY ts DESC, id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    return [_decision_from_row(row) for row in reversed(rows)]
