"""Postgres-backed task queue backend for cloud RED workers."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from agent_core.env_utils import env_bool
from agent_core.operational_db import connect, enabled as db_enabled, ensure_schema

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_DEAD = "dead"
STATE_CANCELLED = "cancelled"


def enabled() -> bool:
    backend = os.environ.get("RED_TASK_QUEUE_BACKEND", "").strip().lower()
    explicit = backend in {"postgres", "postgresql", "operational_db", "db"}
    return db_enabled() and (explicit or env_bool("RED_TASK_QUEUE_POSTGRES", False))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _jsonb(value: Mapping[str, Any]):
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(dict(value))
    except Exception:
        return dict(value)


def _task_from_row(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        keys = (
            "id", "tool", "kwargs", "priority", "state", "submitted_at",
            "started_at", "ended_at", "attempts", "max_retries",
            "timeout_sec", "mutex_group", "next_run_at", "last_error",
            "cancel_requested", "worker_id", "result_preview",
        )
        row = dict(zip(keys, row))
    return {
        "id": str(row.get("id") or ""),
        "tool": str(row.get("tool") or ""),
        "kwargs": dict(row.get("kwargs") or {}),
        "priority": int(row.get("priority") or 5),
        "state": str(row.get("state") or ""),
        "submitted_at": _iso(row.get("submitted_at")),
        "started_at": _iso(row.get("started_at")),
        "ended_at": _iso(row.get("ended_at")),
        "attempts": int(row.get("attempts") or 0),
        "max_retries": int(row.get("max_retries") or 0),
        "timeout_sec": int(row.get("timeout_sec") or 300),
        "mutex_group": str(row.get("mutex_group") or ""),
        "next_run_at": _iso(row.get("next_run_at")),
        "last_error": row.get("last_error"),
        "cancel_requested": bool(row.get("cancel_requested") or False),
        "worker_id": str(row.get("worker_id") or ""),
        "result_preview": str(row.get("result_preview") or ""),
    }


def _task_columns() -> str:
    return (
        "id, tool, kwargs, priority, state, submitted_at, started_at, ended_at, "
        "attempts, max_retries, timeout_sec, mutex_group, next_run_at, "
        "last_error, cancel_requested, worker_id, result_preview"
    )


def _backoff_seconds(attempt: int, base_sec: int) -> int:
    return int(base_sec) * (2 ** max(0, int(attempt) - 1))


def _dead_payload(task: Mapping[str, Any], *, moved_at: datetime) -> dict[str, Any]:
    payload = dict(task)
    payload["state"] = STATE_DEAD
    payload["moved_to_dlq_at"] = moved_at.isoformat(timespec="seconds")
    return payload


def _insert_dead_letter(cur, task: Mapping[str, Any], *, moved_at: datetime) -> None:
    cur.execute(
        """
        INSERT INTO red_task_dead_letters (
            id, tool, payload, moved_to_dlq_at, last_error
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            payload = EXCLUDED.payload,
            moved_to_dlq_at = EXCLUDED.moved_to_dlq_at,
            last_error = EXCLUDED.last_error
        """,
        (
            str(task.get("id") or ""),
            str(task.get("tool") or ""),
            _jsonb(_dead_payload(task, moved_at=moved_at)),
            moved_at,
            str(task.get("last_error") or ""),
        ),
    )


def reap_stale_running(
    *,
    stale_grace_sec: int,
    backoff_base_sec: int,
    dlq_retain_days: int,
) -> int:
    if not enabled():
        return 0
    ensure_schema()
    now = _now()
    reaped = 0
    cutoff = now - timedelta(days=int(dlq_retain_days))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_task_columns()}
                FROM red_task_queue
                WHERE state = %s
                FOR UPDATE SKIP LOCKED
                """,
                (STATE_RUNNING,),
            )
            rows = cur.fetchall()
            for row in rows:
                task = _task_from_row(row)
                started = task.get("started_at")
                if not started:
                    continue
                try:
                    started_dt = datetime.fromisoformat(str(started))
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                deadline = started_dt + timedelta(
                    seconds=int(task.get("timeout_sec") or 60) + int(stale_grace_sec)
                )
                if now <= deadline:
                    continue
                reaped += 1
                attempts = int(task.get("attempts") or 0)
                max_retries = int(task.get("max_retries") or 0)
                if attempts >= max_retries + 1:
                    task["ended_at"] = now.isoformat(timespec="seconds")
                    task["last_error"] = (
                        "worker died while RUNNING (reaped stale) — retries exhausted"
                    )
                    _insert_dead_letter(cur, task, moved_at=now)
                    cur.execute("DELETE FROM red_task_queue WHERE id = %s", (task["id"],))
                else:
                    wait = _backoff_seconds(attempts, backoff_base_sec)
                    cur.execute(
                        """
                        UPDATE red_task_queue
                        SET state = %s,
                            started_at = NULL,
                            next_run_at = %s,
                            last_error = %s,
                            cancel_requested = false,
                            worker_id = '',
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            STATE_PENDING,
                            now + timedelta(seconds=wait),
                            "worker died while RUNNING (reaped stale) — re-queued",
                            task["id"],
                        ),
                    )
            cur.execute(
                "DELETE FROM red_task_dead_letters WHERE moved_to_dlq_at < %s",
                (cutoff,),
            )
    return reaped


def enqueue_task(
    task: Mapping[str, Any],
    *,
    queue_max_size: int,
    stale_grace_sec: int,
    backoff_base_sec: int,
    dlq_retain_days: int,
) -> dict[str, Any]:
    ensure_schema()
    reap_stale_running(
        stale_grace_sec=stale_grace_sec,
        backoff_base_sec=backoff_base_sec,
        dlq_retain_days=dlq_retain_days,
    )
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("LOCK TABLE red_task_queue IN EXCLUSIVE MODE")
            cur.execute(
                """
                SELECT count(*)
                FROM red_task_queue
                WHERE state IN (%s, %s)
                """,
                (STATE_PENDING, STATE_RUNNING),
            )
            live = int((cur.fetchone() or (0,))[0] or 0)
            if live >= int(queue_max_size):
                return {"ok": False, "reason": "full", "live": live}
            cur.execute(
                """
                INSERT INTO red_task_queue (
                    id, tool, kwargs, priority, state, submitted_at, started_at,
                    ended_at, attempts, max_retries, timeout_sec, mutex_group,
                    next_run_at, last_error
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    task["id"], task["tool"], _jsonb(task.get("kwargs") or {}),
                    int(task.get("priority") or 5), STATE_PENDING,
                    task.get("submitted_at"), task.get("started_at"),
                    task.get("ended_at"), int(task.get("attempts") or 0),
                    int(task.get("max_retries") or 0),
                    int(task.get("timeout_sec") or 300),
                    str(task.get("mutex_group") or ""),
                    task.get("next_run_at"), task.get("last_error"),
                ),
            )
    return {"ok": True, "task_id": task["id"]}


def cancel_task(task_id: str) -> dict[str, Any]:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_task_columns()}
                FROM red_task_queue
                WHERE id = %s
                FOR UPDATE
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "reason": "not_found"}
            task = _task_from_row(row)
            if task["state"] == STATE_PENDING:
                cur.execute(
                    """
                    UPDATE red_task_queue
                    SET state = %s, ended_at = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (STATE_CANCELLED, _now(), task_id),
                )
                return {"ok": True, "previous_state": STATE_PENDING}
            if task["state"] == STATE_RUNNING:
                cur.execute(
                    """
                    UPDATE red_task_queue
                    SET cancel_requested = true, updated_at = now()
                    WHERE id = %s
                    """,
                    (task_id,),
                )
                return {"ok": True, "previous_state": STATE_RUNNING}
            return {"ok": False, "reason": "invalid_state", "state": task["state"]}


def get_task(task_id: str) -> dict[str, Any] | None:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_task_columns()} FROM red_task_queue WHERE id = %s",
                (task_id,),
            )
            row = cur.fetchone()
            if row:
                return _task_from_row(row)
            cur.execute("SELECT payload FROM red_task_dead_letters WHERE id = %s", (task_id,))
            row = cur.fetchone()
            if row and isinstance(row[0], dict):
                return dict(row[0])
    return None


def list_tasks(state: str = "", limit: int = 200) -> list[dict[str, Any]]:
    ensure_schema()
    lim = max(1, min(int(limit or 200), 1000))
    with connect() as conn:
        with conn.cursor() as cur:
            if state:
                cur.execute(
                    f"""
                    SELECT {_task_columns()}
                    FROM red_task_queue
                    WHERE state = %s
                    ORDER BY
                        CASE state
                            WHEN %s THEN 0 WHEN %s THEN 1 WHEN %s THEN 2
                            WHEN %s THEN 3 WHEN %s THEN 4 ELSE 9
                        END,
                        priority ASC,
                        submitted_at ASC
                    LIMIT %s
                    """,
                    (
                        state, STATE_RUNNING, STATE_PENDING, STATE_FAILED,
                        STATE_DONE, STATE_CANCELLED, lim,
                    ),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {_task_columns()}
                    FROM red_task_queue
                    ORDER BY
                        CASE state
                            WHEN %s THEN 0 WHEN %s THEN 1 WHEN %s THEN 2
                            WHEN %s THEN 3 WHEN %s THEN 4 ELSE 9
                        END,
                        priority ASC,
                        submitted_at ASC
                    LIMIT %s
                    """,
                    (
                        STATE_RUNNING, STATE_PENDING, STATE_FAILED,
                        STATE_DONE, STATE_CANCELLED, lim,
                    ),
                )
            return [_task_from_row(row) for row in cur.fetchall()]


def find_live_task(tool: str, mutex_group: str = "") -> dict[str, Any] | None:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            if mutex_group:
                cur.execute(
                    f"""
                    SELECT {_task_columns()}
                    FROM red_task_queue
                    WHERE tool = %s AND mutex_group = %s AND state IN (%s, %s)
                    ORDER BY submitted_at ASC
                    LIMIT 1
                    """,
                    (tool, mutex_group, STATE_PENDING, STATE_RUNNING),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {_task_columns()}
                    FROM red_task_queue
                    WHERE tool = %s AND state IN (%s, %s)
                    ORDER BY submitted_at ASC
                    LIMIT 1
                    """,
                    (tool, STATE_PENDING, STATE_RUNNING),
                )
            row = cur.fetchone()
            return _task_from_row(row) if row else None


def list_dead_letters(limit: int = 20) -> list[dict[str, Any]]:
    ensure_schema()
    lim = max(1, min(int(limit or 20), 500))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM red_task_dead_letters
                ORDER BY moved_to_dlq_at DESC
                LIMIT %s
                """,
                (lim,),
            )
            out = []
            for row in cur.fetchall():
                payload = row[0] if isinstance(row, (tuple, list)) else row
                if isinstance(payload, dict):
                    out.append(dict(payload))
            return out


def requeue_dead_letter(task_id: str, new_task_id: str) -> dict[str, Any]:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM red_task_dead_letters
                WHERE id = %s
                FOR UPDATE
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if not row or not isinstance(row[0], dict):
                return {"ok": False, "reason": "not_found"}
            task = dict(row[0])
            cur.execute("DELETE FROM red_task_dead_letters WHERE id = %s", (task_id,))
            task.update({
                "id": new_task_id,
                "state": STATE_PENDING,
                "attempts": 0,
                "last_error": None,
                "next_run_at": _now().isoformat(timespec="seconds"),
                "started_at": None,
                "ended_at": None,
            })
            cur.execute(
                """
                INSERT INTO red_task_queue (
                    id, tool, kwargs, priority, state, submitted_at, started_at,
                    ended_at, attempts, max_retries, timeout_sec, mutex_group,
                    next_run_at, last_error
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    task["id"], task["tool"], _jsonb(task.get("kwargs") or {}),
                    int(task.get("priority") or 5), STATE_PENDING,
                    task.get("submitted_at") or task.get("next_run_at"),
                    None, None, 0, int(task.get("max_retries") or 0),
                    int(task.get("timeout_sec") or 300),
                    str(task.get("mutex_group") or ""),
                    task.get("next_run_at"), None,
                ),
            )
    return {"ok": True, "task": task}


def claim_task(worker_id: str) -> dict[str, Any] | None:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_task_columns()}
                FROM red_task_queue q
                WHERE q.state = %s
                  AND q.next_run_at <= now()
                  AND (
                    q.mutex_group = ''
                    OR NOT EXISTS (
                        SELECT 1
                        FROM red_task_queue r
                        WHERE r.state = %s
                          AND r.mutex_group = q.mutex_group
                    )
                  )
                ORDER BY q.priority ASC, q.submitted_at ASC
                FOR UPDATE OF q SKIP LOCKED
                LIMIT 1
                """,
                (STATE_PENDING, STATE_RUNNING),
            )
            row = cur.fetchone()
            if not row:
                return None
            task = _task_from_row(row)
            cur.execute(
                """
                UPDATE red_task_queue
                SET state = %s,
                    started_at = %s,
                    attempts = attempts + 1,
                    cancel_requested = false,
                    worker_id = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (STATE_RUNNING, _now(), worker_id, task["id"]),
            )
            task["state"] = STATE_RUNNING
            task["started_at"] = _now().isoformat(timespec="seconds")
            task["attempts"] = int(task.get("attempts") or 0) + 1
            task["cancel_requested"] = False
            task["worker_id"] = worker_id
            return task


def complete_task(
    task_id: str,
    *,
    success: bool,
    error: str = "",
    result_preview: str = "",
    backoff_base_sec: int,
    dlq_retain_days: int,
) -> None:
    ensure_schema()
    now = _now()
    cutoff = now - timedelta(days=int(dlq_retain_days))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_task_columns()}
                FROM red_task_queue
                WHERE id = %s
                FOR UPDATE
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            task = _task_from_row(row)
            cancel_requested = bool(task.get("cancel_requested"))
            if cancel_requested:
                cur.execute(
                    """
                    UPDATE red_task_queue
                    SET state = %s,
                        ended_at = %s,
                        last_error = %s,
                        result_preview = %s,
                        worker_id = '',
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        STATE_CANCELLED, now, error or "cancelled by user",
                        result_preview[:500], task_id,
                    ),
                )
            elif success:
                cur.execute(
                    """
                    UPDATE red_task_queue
                    SET state = %s,
                        ended_at = %s,
                        last_error = NULL,
                        result_preview = %s,
                        worker_id = '',
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (STATE_DONE, now, result_preview[:500], task_id),
                )
            else:
                task["last_error"] = error
                task["ended_at"] = now.isoformat(timespec="seconds")
                attempts = int(task.get("attempts") or 0)
                max_retries = int(task.get("max_retries") or 0)
                if attempts >= max_retries + 1:
                    _insert_dead_letter(cur, task, moved_at=now)
                    cur.execute("DELETE FROM red_task_queue WHERE id = %s", (task_id,))
                else:
                    wait = _backoff_seconds(attempts, backoff_base_sec)
                    cur.execute(
                        """
                        UPDATE red_task_queue
                        SET state = %s,
                            ended_at = %s,
                            next_run_at = %s,
                            last_error = %s,
                            result_preview = %s,
                            cancel_requested = false,
                            worker_id = '',
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            STATE_PENDING, now, now + timedelta(seconds=wait),
                            error, result_preview[:500], task_id,
                        ),
                    )
            cur.execute(
                """
                DELETE FROM red_task_queue
                WHERE state IN (%s, %s)
                  AND ended_at IS NOT NULL
                  AND ended_at < %s
                """,
                (STATE_DONE, STATE_CANCELLED, now - timedelta(days=7)),
            )
            cur.execute(
                "DELETE FROM red_task_dead_letters WHERE moved_to_dlq_at < %s",
                (cutoff,),
            )


def queue_summary() -> dict[str, Any]:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT state, count(*)
                FROM red_task_queue
                GROUP BY state
                """
            )
            counts = {str(state): int(count) for state, count in cur.fetchall()}
            cur.execute("SELECT count(*) FROM red_task_dead_letters")
            dlq = int((cur.fetchone() or (0,))[0] or 0)
            cur.execute(
                """
                SELECT mutex_group, id
                FROM red_task_queue
                WHERE state = %s AND mutex_group <> ''
                """,
                (STATE_RUNNING,),
            )
            holders = {str(group): str(tid) for group, tid in cur.fetchall()}
    return {
        "pending": counts.get(STATE_PENDING, 0),
        "running": counts.get(STATE_RUNNING, 0),
        "done": counts.get(STATE_DONE, 0),
        "failed": counts.get(STATE_FAILED, 0),
        "cancelled": counts.get(STATE_CANCELLED, 0),
        "dlq": dlq,
        "mutex_holders": holders,
        "backend": "postgres",
    }
