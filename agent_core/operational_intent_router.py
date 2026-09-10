"""Postgres-backed intent classification log."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}


def enabled() -> bool:
    backend = (
        os.environ.get("RED_INTENT_ROUTER_BACKEND")
        or os.environ.get("RED_INTENT_LOG_BACKEND")
        or ""
    ).strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_INTENT_ROUTER_POSTGRES", False))


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


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def write_classification(record: Mapping[str, Any]) -> None:
    payload = dict(record)
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_intent_classifications (
                    ts, intent, confidence, method, text_preview, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    _parse_ts(payload.get("at") or payload.get("ts")),
                    str(payload.get("intent") or ""),
                    _as_float(payload.get("confidence")),
                    str(payload.get("method") or ""),
                    str(payload.get("text_preview") or "")[:200],
                    _jsonb(payload),
                ),
            )


def _row_dict(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    keys = ("ts", "intent", "confidence", "method", "text_preview", "payload")
    return dict(zip(keys, row))


def _classification_from_row(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    data = _row_dict(row)
    payload = data.get("payload") or {}
    out = dict(payload) if isinstance(payload, dict) else {}
    out.update({
        "at": _iso_local(data.get("ts")),
        "intent": str(data.get("intent") or ""),
        "confidence": round(_as_float(data.get("confidence")), 2),
        "method": str(data.get("method") or ""),
        "text_preview": str(data.get("text_preview") or ""),
    })
    return out


def load_classifications(hours: int | None = None, limit: int = 5000) -> list[dict[str, Any]]:
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
                SELECT ts, intent, confidence, method, text_preview, payload
                FROM red_intent_classifications
                {where}
                ORDER BY ts DESC, id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    return [_classification_from_row(row) for row in rows]
