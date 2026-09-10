"""Cloud operational database helpers.

This module is the first Postgres-backed state layer for RED.  It is optional:
local launchd/dev runs keep using the existing file stores unless
RED_OPERATIONAL_DB_URL or RED_DATABASE_URL is configured.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from agent_core.env_utils import env_int
from agent_core.logging_and_paths import logger

SCHEMA_VERSION = 16

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_FAILURE_UNTIL = 0.0
_POOL_LOCK = threading.Lock()
_POOL = None
_POOL_CONFIG: tuple[str, int, int, int] | None = None
_POOL_WARNING_UNTIL = 0.0


def database_url() -> str:
    """Return the configured Postgres URL, if any."""
    return (
        os.environ.get("RED_OPERATIONAL_DB_URL")
        or os.environ.get("RED_DATABASE_URL")
        or ""
    ).strip()


def enabled() -> bool:
    return bool(database_url())


def _connect_timeout() -> int:
    return env_int(
        "RED_OPERATIONAL_DB_CONNECT_TIMEOUT_SEC",
        2,
        min_value=1,
        max_value=30,
    )


def _failure_cooldown() -> int:
    return env_int(
        "RED_OPERATIONAL_DB_FAILURE_COOLDOWN_SEC",
        30,
        min_value=0,
        max_value=3600,
    )


def _pool_enabled() -> bool:
    return _truthy(os.environ.get("RED_OPERATIONAL_DB_POOL"))


def _pool_min_size() -> int:
    return env_int(
        "RED_OPERATIONAL_DB_POOL_MIN_SIZE",
        1,
        min_value=0,
        max_value=50,
    )


def _pool_max_size() -> int:
    min_size = _pool_min_size()
    return env_int(
        "RED_OPERATIONAL_DB_POOL_MAX_SIZE",
        max(4, min_size),
        min_value=max(1, min_size),
        max_value=200,
    )


def _parse_timestamptz(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _jsonb(value: Mapping[str, Any]):
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(dict(value))
    except Exception:
        return dict(value)


def _warn_pool_fallback(exc: Exception) -> None:
    global _POOL_WARNING_UNTIL
    now = time.monotonic()
    if now < _POOL_WARNING_UNTIL:
        return
    _POOL_WARNING_UNTIL = now + 60
    logger.warning("operational DB pool unavailable; using direct connections: %s", exc)


class _PooledConnection:
    """Small adapter so pooled connections behave like psycopg connections."""

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def __getattr__(self, name: str):
        if self._conn is None:
            raise RuntimeError("pooled connection is closed")
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        conn = self._conn
        try:
            if conn is not None:
                if exc_type is None:
                    conn.commit()
                else:
                    conn.rollback()
        finally:
            self.close()
        return False

    def close(self) -> None:
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        self._pool.putconn(conn)


def _get_pool(url: str):
    global _POOL, _POOL_CONFIG
    timeout = _connect_timeout()
    min_size = _pool_min_size()
    max_size = _pool_max_size()
    config = (url, timeout, min_size, max_size)
    with _POOL_LOCK:
        if _POOL is not None and _POOL_CONFIG == config:
            return _POOL
        if _POOL is not None:
            try:
                _POOL.close()
            except Exception:
                pass
            _POOL = None
            _POOL_CONFIG = None
        try:
            from psycopg_pool import ConnectionPool
        except ModuleNotFoundError as exc:
            _warn_pool_fallback(exc)
            return None
        try:
            _POOL = ConnectionPool(
                conninfo=url,
                min_size=min_size,
                max_size=max_size,
                kwargs={"connect_timeout": timeout},
                open=True,
            )
            _POOL_CONFIG = config
            return _POOL
        except Exception as exc:  # noqa: BLE001 - fall back to direct connect
            _warn_pool_fallback(exc)
            return None


def connect():
    """Open a psycopg connection.

    Import psycopg lazily so local environments without Postgres support keep
    importing RED modules normally.
    """
    url = database_url()
    if not url:
        raise RuntimeError("RED operational database is not configured")
    if _pool_enabled():
        pool = _get_pool(url)
        if pool is not None:
            return _PooledConnection(pool)
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "psycopg is required for RED_OPERATIONAL_DB_URL/RED_DATABASE_URL"
        ) from exc
    return psycopg.connect(url, connect_timeout=_connect_timeout())


def ensure_schema(conn=None) -> None:
    """Create/upgrade the operational schema.

    The DDL is deliberately additive and idempotent so many Cloud Run instances
    can race safely during rollout.
    """
    own_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        description TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_audit_events (
                        id BIGSERIAL PRIMARY KEY,
                        logged_at TIMESTAMPTZ NOT NULL,
                        event TEXT NOT NULL,
                        status TEXT NOT NULL,
                        chat_id TEXT NOT NULL DEFAULT '',
                        update_id TEXT NOT NULL DEFAULT '',
                        command TEXT NOT NULL DEFAULT '',
                        actor_color TEXT NOT NULL DEFAULT '',
                        actor_source TEXT NOT NULL DEFAULT '',
                        actor_label TEXT NOT NULL DEFAULT '',
                        actor_is_owner BOOLEAN NOT NULL DEFAULT false,
                        chat_type TEXT NOT NULL DEFAULT '',
                        from_id TEXT NOT NULL DEFAULT '',
                        from_username TEXT NOT NULL DEFAULT '',
                        message_id TEXT NOT NULL DEFAULT '',
                        text_preview TEXT NOT NULL DEFAULT '',
                        reply_preview TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_audit_events_logged_at_idx
                    ON red_audit_events (logged_at DESC, id DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_audit_events_chat_idx
                    ON red_audit_events (chat_id, logged_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_audit_events_command_idx
                    ON red_audit_events (command, logged_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_task_queue (
                        id TEXT PRIMARY KEY,
                        tool TEXT NOT NULL,
                        kwargs JSONB NOT NULL DEFAULT '{}'::jsonb,
                        priority SMALLINT NOT NULL DEFAULT 5,
                        state TEXT NOT NULL,
                        submitted_at TIMESTAMPTZ NOT NULL,
                        started_at TIMESTAMPTZ,
                        ended_at TIMESTAMPTZ,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 3,
                        timeout_sec INTEGER NOT NULL DEFAULT 300,
                        mutex_group TEXT NOT NULL DEFAULT '',
                        next_run_at TIMESTAMPTZ NOT NULL,
                        last_error TEXT,
                        cancel_requested BOOLEAN NOT NULL DEFAULT false,
                        worker_id TEXT NOT NULL DEFAULT '',
                        result_preview TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_task_queue_ready_idx
                    ON red_task_queue (state, next_run_at, priority, submitted_at)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_task_queue_mutex_idx
                    ON red_task_queue (state, mutex_group)
                    WHERE mutex_group <> ''
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_task_dead_letters (
                        id TEXT PRIMARY KEY,
                        tool TEXT NOT NULL,
                        payload JSONB NOT NULL,
                        moved_to_dlq_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_error TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_task_dead_letters_moved_idx
                    ON red_task_dead_letters (moved_to_dlq_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_telegram_private_approvals (
                        namespace TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        telegram_user_id TEXT NOT NULL DEFAULT '',
                        username TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL DEFAULT '',
                        color TEXT NOT NULL DEFAULT '',
                        email TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'approved',
                        approved_by TEXT NOT NULL DEFAULT '',
                        approved_at TIMESTAMPTZ,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, chat_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_telegram_private_approvals_status_idx
                    ON red_telegram_private_approvals (namespace, status)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_telegram_join_requests (
                        namespace TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        telegram_user_id TEXT NOT NULL DEFAULT '',
                        username TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL DEFAULT '',
                        chat_type TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        requested_at TIMESTAMPTZ,
                        last_seen_at TIMESTAMPTZ,
                        last_seen_ts DOUBLE PRECISION NOT NULL DEFAULT 0,
                        last_text TEXT NOT NULL DEFAULT '',
                        update_id TEXT NOT NULL DEFAULT '',
                        last_notified_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                        last_notified_at_iso TIMESTAMPTZ,
                        resolved_at TIMESTAMPTZ,
                        resolved_by TEXT NOT NULL DEFAULT '',
                        approved_color TEXT NOT NULL DEFAULT '',
                        employee_email TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, chat_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_telegram_join_requests_pending_idx
                    ON red_telegram_join_requests (namespace, status, last_seen_ts DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_tg_auth_state (
                        namespace TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        confirm_ts DOUBLE PRECISION NOT NULL DEFAULT 0,
                        dangerous_confirm_ts DOUBLE PRECISION NOT NULL DEFAULT 0,
                        confirm_history JSONB NOT NULL DEFAULT '[]'::jsonb,
                        message_history JSONB NOT NULL DEFAULT '[]'::jsonb,
                        lockout_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, chat_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_tg_auth_state_updated_idx
                    ON red_tg_auth_state (updated_at)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_tool_budget_usage (
                        day DATE NOT NULL,
                        tool TEXT NOT NULL,
                        daily INTEGER NOT NULL DEFAULT 0,
                        hour TEXT NOT NULL DEFAULT '',
                        hour_count INTEGER NOT NULL DEFAULT 0,
                        last_at TIMESTAMPTZ,
                        by_caller JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (day, tool)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_tool_budget_usage_updated_idx
                    ON red_tool_budget_usage (updated_at)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_cost_events (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        thinking_tokens INTEGER NOT NULL DEFAULT 0,
                        cached_tokens INTEGER NOT NULL DEFAULT 0,
                        tool_use_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
                        duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                        caller TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_cost_events_ts_idx
                    ON red_cost_events (ts DESC, id DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_cost_events_caller_idx
                    ON red_cost_events (caller, ts DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_api_error_events (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL,
                        service TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        detail TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_api_error_events_ts_idx
                    ON red_api_error_events (ts DESC, id DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_run_history (
                        id TEXT PRIMARY KEY,
                        tool TEXT NOT NULL DEFAULT '',
                        started_at TIMESTAMPTZ NOT NULL,
                        ended_at TIMESTAMPTZ,
                        status TEXT NOT NULL DEFAULT '',
                        elapsed_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
                        ok BOOLEAN,
                        error_code TEXT NOT NULL DEFAULT '',
                        recoverable BOOLEAN,
                        short_result TEXT NOT NULL DEFAULT '',
                        args_text TEXT NOT NULL DEFAULT '',
                        kwargs_text TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_run_history_started_idx
                    ON red_run_history (started_at DESC, id DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_run_history_tool_status_idx
                    ON red_run_history (tool, status, started_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_task_memory (
                        namespace TEXT NOT NULL,
                        id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT '',
                        priority SMALLINT NOT NULL DEFAULT 5,
                        title_preview TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ,
                        deadline TEXT NOT NULL DEFAULT '',
                        deadline_at TIMESTAMPTZ,
                        next_reminder_at TEXT NOT NULL DEFAULT '',
                        next_reminder_at_ts TIMESTAMPTZ,
                        linked_customer TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_task_memory_status_idx
                    ON red_task_memory (namespace, status, priority, deadline_at)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_task_memory_reminder_idx
                    ON red_task_memory (namespace, next_reminder_at_ts)
                    WHERE next_reminder_at_ts IS NOT NULL
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_task_memory_customer_idx
                    ON red_task_memory (namespace, linked_customer)
                    WHERE linked_customer <> ''
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_edge_devices (
                        namespace TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        department TEXT NOT NULL DEFAULT '',
                        employee_email TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        last_seen_at TIMESTAMPTZ,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, device_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_edge_devices_department_idx
                    ON red_edge_devices (namespace, department, status)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_edge_tasks (
                        namespace TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        department TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        target_device_id TEXT NOT NULL DEFAULT '',
                        employee_email TEXT NOT NULL DEFAULT '',
                        claimed_by_device_id TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ,
                        updated_at_ts TIMESTAMPTZ,
                        completed_at TIMESTAMPTZ,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, task_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_edge_tasks_claim_idx
                    ON red_edge_tasks (
                        namespace, status, department, target_device_id,
                        employee_email, created_at
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_edge_tasks_device_idx
                    ON red_edge_tasks (namespace, claimed_by_device_id, status)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_policy_decisions (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL,
                        caller TEXT NOT NULL DEFAULT '',
                        tool TEXT NOT NULL DEFAULT '',
                        channel TEXT NOT NULL DEFAULT '',
                        decision TEXT NOT NULL DEFAULT '',
                        reason_layer TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        risk_score INTEGER NOT NULL DEFAULT 0,
                        forced_dry_run BOOLEAN NOT NULL DEFAULT false,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_policy_decisions_ts_idx
                    ON red_policy_decisions (ts DESC, id DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_policy_decisions_decision_idx
                    ON red_policy_decisions (decision, ts DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_policy_decisions_tool_idx
                    ON red_policy_decisions (tool, ts DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_work_modes (
                        namespace TEXT PRIMARY KEY,
                        mode TEXT NOT NULL DEFAULT 'normal',
                        set_at TIMESTAMPTZ,
                        expires_at TIMESTAMPTZ,
                        set_by TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_work_modes_updated_idx
                    ON red_work_modes (updated_at)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_work_mode_history (
                        id BIGSERIAL PRIMARY KEY,
                        namespace TEXT NOT NULL,
                        at TIMESTAMPTZ NOT NULL,
                        from_mode TEXT NOT NULL DEFAULT '',
                        to_mode TEXT NOT NULL DEFAULT '',
                        set_by TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        duration_minutes INTEGER NOT NULL DEFAULT 0,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_work_mode_history_at_idx
                    ON red_work_mode_history (namespace, at DESC, id DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_dry_run_state (
                        namespace TEXT PRIMARY KEY,
                        enabled BOOLEAN NOT NULL DEFAULT false,
                        enabled_at TIMESTAMPTZ,
                        enabled_at_text TEXT NOT NULL DEFAULT '',
                        simulated_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_dry_run_state_updated_idx
                    ON red_dry_run_state (updated_at)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_web_domain_policies (
                        namespace TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        policy TEXT NOT NULL,
                        note TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, domain)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_web_domain_policies_policy_idx
                    ON red_web_domain_policies (namespace, policy)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_intent_classifications (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL,
                        intent TEXT NOT NULL DEFAULT '',
                        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                        method TEXT NOT NULL DEFAULT '',
                        text_preview TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_intent_classifications_ts_idx
                    ON red_intent_classifications (ts DESC, id DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_intent_classifications_intent_idx
                    ON red_intent_classifications (intent, ts DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_alert_push_state (
                        namespace TEXT NOT NULL,
                        alert_id TEXT NOT NULL,
                        first_seen_at TIMESTAMPTZ,
                        first_seen_at_text TEXT NOT NULL DEFAULT '',
                        last_pushed_at TIMESTAMPTZ,
                        last_pushed_at_text TEXT NOT NULL DEFAULT '',
                        title TEXT NOT NULL DEFAULT '',
                        level TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, alert_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_alert_push_state_level_idx
                    ON red_alert_push_state (namespace, level)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_alert_push_state_pushed_idx
                    ON red_alert_push_state (namespace, last_pushed_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_gemini_circuit_state (
                        model TEXT PRIMARY KEY,
                        failures INTEGER NOT NULL DEFAULT 0,
                        first_failure_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                        last_failure_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                        opened_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                        reason TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_gemini_circuit_open_idx
                    ON red_gemini_circuit_state (opened_until)
                    WHERE opened_until > 0
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS red_backfill_events (
                        source TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        target_table TEXT NOT NULL DEFAULT '',
                        source_path TEXT NOT NULL DEFAULT '',
                        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        PRIMARY KEY (source, source_hash)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS red_backfill_events_seen_idx
                    ON red_backfill_events (first_seen_at DESC)
                    """
                )
                cur.execute(
                    """
                    INSERT INTO red_schema_migrations (version, description)
                    VALUES (%s, %s)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (
                        SCHEMA_VERSION,
                        "operational audit, task, auth, budget, cost, run history, task memory, edge task, policy decision, work mode, dry-run, web domain policy, intent classification, alert push, gemini circuit, backfill idempotency schema",
                    ),
                )
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def _ensure_schema_once() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        ensure_schema()
        _SCHEMA_READY = True


def ensure_schema_once() -> None:
    _ensure_schema_once()


def _audit_insert_payload(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _parse_timestamptz(record.get("logged_at")),
        str(record.get("event") or ""),
        str(record.get("status") or ""),
        str(record.get("chat_id") or ""),
        str(record.get("update_id") or ""),
        str(record.get("command") or ""),
        str(record.get("actor_color") or ""),
        str(record.get("actor_source") or ""),
        str(record.get("actor_label") or ""),
        _truthy(record.get("actor_is_owner")),
        str(record.get("chat_type") or ""),
        str(record.get("from_id") or ""),
        str(record.get("from_username") or ""),
        str(record.get("message_id") or ""),
        str(record.get("text_preview") or ""),
        str(record.get("reply_preview") or ""),
        str(record.get("reason") or ""),
        _jsonb(record),
    )


def write_audit_event(record: Mapping[str, Any]) -> bool:
    """Best-effort central audit write.

    Returns True only when Postgres accepted the event.  Failures are swallowed
    and put behind a short cooldown so Telegram handling does not block on a
    broken database.
    """
    global _FAILURE_UNTIL
    if not enabled():
        return False
    now = time.monotonic()
    if _FAILURE_UNTIL and now < _FAILURE_UNTIL:
        return False
    try:
        _ensure_schema_once()
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO red_audit_events (
                        logged_at, event, status, chat_id, update_id, command,
                        actor_color, actor_source, actor_label, actor_is_owner,
                        chat_type, from_id, from_username, message_id,
                        text_preview, reply_preview, reason, payload
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    _audit_insert_payload(record),
                )
        _FAILURE_UNTIL = 0.0
        return True
    except Exception as exc:  # noqa: BLE001 - audit must not affect handling
        cooldown = _failure_cooldown()
        _FAILURE_UNTIL = time.monotonic() + cooldown if cooldown else 0.0
        logger.warning("operational DB audit write failed: %s", exc)
        return False


def read_recent_audit_events(limit: int = 50) -> list[dict[str, Any]]:
    """Read recent audit events from Postgres, oldest-to-newest."""
    if not enabled():
        return []
    lim = max(1, min(int(limit or 50), 500))
    try:
        _ensure_schema_once()
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload
                    FROM red_audit_events
                    ORDER BY logged_at DESC, id DESC
                    LIMIT %s
                    """,
                    (lim,),
                )
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = row[0] if isinstance(row, (tuple, list)) else row
            if isinstance(payload, dict):
                out.append(payload)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("operational DB audit read failed: %s", exc)
        return []


def health_status() -> dict[str, Any]:
    """Return a small status object for health checks."""
    if not enabled():
        return {"enabled": False, "ok": True, "schema_version": None, "error": ""}
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.execute("SELECT max(version) FROM red_schema_migrations")
                row = cur.fetchone()
        version = row[0] if row else None
        return {
            "enabled": True,
            "ok": True,
            "schema_version": version,
            "schema_expected": SCHEMA_VERSION,
            "pool_enabled": _pool_enabled(),
            "pool_active": _POOL is not None,
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": True,
            "ok": False,
            "schema_version": None,
            "schema_expected": SCHEMA_VERSION,
            "pool_enabled": _pool_enabled(),
            "pool_active": _POOL is not None,
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
