"""Postgres-backed web domain access policies."""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

_BACKENDS = {"postgres", "postgresql", "operational_db", "db"}
_DOMAIN_POLICIES = frozenset({
    "allowed",
    "needs_api",
    "requires_manual_login",
    "blocked",
})


def enabled() -> bool:
    backend = (
        os.environ.get("RED_WEB_DOMAIN_POLICY_BACKEND")
        or os.environ.get("RED_WEB_ACCESS_POLICY_BACKEND")
        or ""
    ).strip().lower()
    explicit = backend in _BACKENDS
    return db_enabled() and (explicit or env_bool("RED_WEB_DOMAIN_POLICY_POSTGRES", False))


def namespace() -> str:
    return os.environ.get("RED_WEB_DOMAIN_POLICY_NAMESPACE", "default").strip() or "default"


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


def _row_policy(row: Mapping[str, Any] | tuple[Any, ...]) -> tuple[str, dict[str, str]]:
    if isinstance(row, Mapping):
        domain = str(row.get("domain") or "")
        policy = str(row.get("policy") or "")
        note = str(row.get("note") or "")
    else:
        domain = str(row[0] or "") if row else ""
        policy = str(row[1] or "") if len(row) > 1 else ""
        note = str(row[2] or "") if len(row) > 2 else ""
    return domain, {"policy": policy, "note": note}


def load_policies() -> dict[str, dict[str, str]]:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT domain, policy, note
                FROM red_web_domain_policies
                WHERE namespace = %s
                ORDER BY domain ASC
                """,
                (namespace(),),
            )
            rows = cur.fetchall()
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        domain, entry = _row_policy(row)
        if domain and entry.get("policy") in _DOMAIN_POLICIES:
            out[domain] = entry
    return out


def set_policy(domain: str, policy: str, note: str = "") -> None:
    record = {
        "domain": domain,
        "policy": policy,
        "note": note,
    }
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO red_web_domain_policies (
                    namespace, domain, policy, note, payload
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (namespace, domain) DO UPDATE SET
                    policy = EXCLUDED.policy,
                    note = EXCLUDED.note,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                """,
                (namespace(), domain, policy, note, _jsonb(record)),
            )


def clear_policy(domain: str) -> bool:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM red_web_domain_policies
                WHERE namespace = %s AND domain = %s
                """,
                (namespace(), domain),
            )
            count = int(getattr(cur, "rowcount", 0) or 0)
    return count > 0
