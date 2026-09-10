#!/usr/bin/env python3
"""Backfill local RED operational JSON/JSONL state into Postgres."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core import operational_db


@dataclass
class Result:
    name: str
    path: str
    seen: int = 0
    written: int = 0
    skipped: int = 0
    status: str = "ok"
    error: str = ""


@dataclass(frozen=True)
class JsonlEntry:
    line_no: int
    row: dict[str, Any]


def _load_json(path: str, default: Any) -> tuple[Any, bool]:
    if not path or not os.path.isfile(path):
        return default, False
    with open(path, encoding="utf-8") as handle:
        return json.load(handle), True


def _iter_jsonl_entries(path: str, *, limit: int = 0) -> tuple[list[JsonlEntry], bool]:
    if not path or not os.path.isfile(path):
        return [], False
    window: deque[tuple[int, str]] | list[tuple[int, str]]
    window = deque(maxlen=limit) if limit > 0 else []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if isinstance(window, deque):
                window.append((line_no, line))
            else:
                window.append((line_no, line))
    out: list[JsonlEntry] = []
    for line_no, line in window:
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict):
            out.append(JsonlEntry(line_no=line_no, row=item))
    return out, True


def _iter_jsonl(path: str, *, limit: int = 0) -> tuple[list[dict[str, Any]], bool]:
    entries, exists = _iter_jsonl_entries(path, limit=limit)
    return [entry.row for entry in entries], exists


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


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _jsonb(value: Mapping[str, Any]):
    return operational_db._jsonb(value)


def _json_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _entry_hash(entry: JsonlEntry) -> str:
    return _json_hash({"line_no": entry.line_no, "row": entry.row})


def _claim_backfill_event(
    cur,
    *,
    source: str,
    source_hash: str,
    target_table: str,
    source_path: str,
    payload: Mapping[str, Any],
) -> bool:
    cur.execute(
        """
        INSERT INTO red_backfill_events (
            source, source_hash, target_table, source_path, payload
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (source, source_hash) DO NOTHING
        RETURNING 1
        """,
        (source, source_hash, target_table, source_path, _jsonb(payload)),
    )
    return cur.fetchone() is not None


def _write_idempotent_entries(
    result: Result,
    *,
    entries: list[JsonlEntry],
    target_table: str,
    write_row: Callable[[Any, Mapping[str, Any]], None],
) -> None:
    operational_db.ensure_schema()
    with operational_db.connect() as conn:
        with conn.cursor() as cur:
            for entry in entries:
                source_hash = _entry_hash(entry)
                claimed = _claim_backfill_event(
                    cur,
                    source=result.name,
                    source_hash=source_hash,
                    target_table=target_table,
                    source_path=result.path,
                    payload={"line_no": entry.line_no, "row": entry.row},
                )
                if not claimed:
                    result.skipped += 1
                    continue
                write_row(cur, entry.row)
                result.written += 1


def _backfill_task_memory(*, dry_run: bool, limit: int) -> Result:
    from agent_core import task_memory
    from agent_core import operational_task_memory as store

    path = task_memory._TASK_FILE
    data, exists = _load_json(path, {"version": 1, "tasks": []})
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    result = Result("task_memory", path, seen=len(tasks))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result
    store.replace_all(data)
    result.written = len(tasks)
    return result


def _backfill_edge_tasks(*, dry_run: bool, limit: int) -> Result:
    from agent_core import edge_tasks
    from agent_core import operational_edge_tasks as store

    path = edge_tasks._edge_tasks_file()
    data, exists = _load_json(path, {"version": 1, "devices": {}, "tasks": []})
    devices = data.get("devices", {}) if isinstance(data, dict) else {}
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    result = Result("edge_tasks", path, seen=len(devices) + len(tasks))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result
    store.replace_all(data)
    result.written = len(devices) + len(tasks)
    return result


def _backfill_dry_run(*, dry_run: bool, limit: int) -> Result:
    from agent_core import dry_run as dry_run_mod
    from agent_core import operational_dry_run as store

    path = dry_run_mod._state_path()
    data, exists = _load_json(path, {})
    calls = data.get("simulated_calls", []) if isinstance(data, dict) else []
    result = Result("dry_run", path, seen=1 if exists else 0)
    if not exists:
        result.status = "missing"
        return result
    result.seen = 1 + len(calls)
    if dry_run:
        return result
    store.save_state(data)
    result.written = result.seen
    return result


def _backfill_work_mode(*, dry_run: bool, limit: int) -> Result:
    from agent_core import mode_manager
    from agent_core import operational_work_mode as store

    mode_data, mode_exists = _load_json(mode_manager._MODE_FILE, {})
    history, hist_exists = _iter_jsonl_entries(mode_manager._HISTORY_FILE, limit=limit)
    result = Result(
        "work_mode",
        f"{mode_manager._MODE_FILE};{mode_manager._HISTORY_FILE}",
        seen=(1 if mode_exists else 0) + len(history),
    )
    if not mode_exists and not hist_exists:
        result.status = "missing"
        return result
    if dry_run:
        return result
    if isinstance(mode_data, dict) and mode_exists:
        store.save_mode_state(mode_data)
        result.written += 1
    if history:
        def _write_history(cur, row: Mapping[str, Any]) -> None:
            cur.execute(
                """
                INSERT INTO red_work_mode_history (
                    namespace, at, from_mode, to_mode, set_by, reason,
                    duration_minutes, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    store.namespace(),
                    _parse_ts(row.get("at")) or datetime.now().astimezone(),
                    str(row.get("from_mode") or ""),
                    str(row.get("to_mode") or ""),
                    str(row.get("set_by") or ""),
                    str(row.get("reason") or ""),
                    _as_int(row.get("duration_minutes")),
                    _jsonb(row),
                ),
            )

        history_result = Result(
            "work_mode_history",
            mode_manager._HISTORY_FILE,
            seen=len(history),
        )
        _write_idempotent_entries(
            history_result,
            entries=history,
            target_table="red_work_mode_history",
            write_row=_write_history,
        )
        result.written += history_result.written
        result.skipped += history_result.skipped
    return result


def _backfill_web_domain_policy(*, dry_run: bool, limit: int) -> Result:
    from agent_core import web_access_guard
    from agent_core import operational_web_domain_policy as store

    path = web_access_guard._DOMAIN_POLICY_FILE
    data, exists = _load_json(path, {})
    policies = data if isinstance(data, dict) else {}
    result = Result("web_domain_policy", path, seen=len(policies))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result
    for domain, entry in policies.items():
        norm = web_access_guard._normalize_domain(str(domain))
        if not norm:
            result.skipped += 1
            continue
        if isinstance(entry, str):
            policy = entry
            note = ""
        elif isinstance(entry, dict):
            policy = str(entry.get("policy") or "")
            note = str(entry.get("note") or "")
        else:
            result.skipped += 1
            continue
        if policy not in web_access_guard._DOMAIN_POLICIES:
            result.skipped += 1
            continue
        store.set_policy(norm, policy, note)
        result.written += 1
    return result


def _backfill_alert_push(*, dry_run: bool, limit: int) -> Result:
    from agent_core import alert_pusher
    from agent_core import operational_alert_pusher as store

    path = alert_pusher._PUSH_STATE_FILE
    data, exists = _load_json(path, {})
    state = data if isinstance(data, dict) else {}
    result = Result("alert_push", path, seen=len(state))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result
    store.replace_state(state)
    result.written = len(state)
    return result


def _backfill_policy_decisions(*, dry_run: bool, limit: int) -> Result:
    from agent_core import policy_engine

    entries, exists = _iter_jsonl_entries(policy_engine._LOG_FILE, limit=limit)
    result = Result("policy_decisions", policy_engine._LOG_FILE, seen=len(entries))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result

    def _write_decision(cur, row: Mapping[str, Any]) -> None:
        cur.execute(
            """
            INSERT INTO red_policy_decisions (
                ts, caller, tool, channel, decision, reason_layer, reason,
                risk_score, forced_dry_run, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _parse_ts(row.get("at") or row.get("ts")) or datetime.now().astimezone(),
                str(row.get("caller") or row.get("user") or ""),
                str(row.get("tool") or ""),
                str(row.get("channel") or ""),
                "allow" if row.get("allow") else "refuse",
                str(row.get("reason_layer") or ""),
                str(row.get("reason") or ""),
                _as_int(row.get("risk_score")),
                _as_bool(row.get("forced_dry_run")),
                _jsonb(row),
            ),
        )

    _write_idempotent_entries(
        result,
        entries=entries,
        target_table="red_policy_decisions",
        write_row=_write_decision,
    )
    return result


def _backfill_intent_classifications(*, dry_run: bool, limit: int) -> Result:
    from agent_core import intent_router

    entries, exists = _iter_jsonl_entries(intent_router._LOG_FILE, limit=limit)
    result = Result("intent_classifications", intent_router._LOG_FILE, seen=len(entries))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result

    def _write_classification(cur, row: Mapping[str, Any]) -> None:
        cur.execute(
            """
            INSERT INTO red_intent_classifications (
                ts, intent, confidence, method, text_preview, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                _parse_ts(row.get("at") or row.get("ts")) or datetime.now().astimezone(),
                str(row.get("intent") or ""),
                _as_float(row.get("confidence")),
                str(row.get("method") or ""),
                str(row.get("text_preview") or "")[:200],
                _jsonb(row),
            ),
        )

    _write_idempotent_entries(
        result,
        entries=entries,
        target_table="red_intent_classifications",
        write_row=_write_classification,
    )
    return result


def _backfill_run_history(*, dry_run: bool, limit: int) -> Result:
    from agent_core import run_history
    from agent_core import operational_run_history as store

    rows, exists = _iter_jsonl(run_history.RUNS_INDEX, limit=limit)
    result = Result("run_history", run_history.RUNS_INDEX, seen=len(rows))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result
    for row in rows:
        if not row.get("id"):
            result.skipped += 1
            continue
        store.write_run_record(row)
        result.written += 1
    return result


def _backfill_cost_events(*, dry_run: bool, limit: int) -> Result:
    from agent_core import cost_tracker

    entries, exists = _iter_jsonl_entries(cost_tracker._COST_LOG, limit=limit)
    result = Result("cost_events", cost_tracker._COST_LOG, seen=len(entries))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result

    def _write_cost(cur, row: Mapping[str, Any]) -> None:
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
                _parse_ts(row.get("ts")) or datetime.now().astimezone(),
                str(row.get("model") or ""),
                _as_int(row.get("prompt_tokens")),
                _as_int(row.get("output_tokens")),
                _as_int(row.get("thinking_tokens")),
                _as_int(row.get("cached_tokens")),
                _as_int(row.get("tool_use_tokens")),
                _as_int(row.get("total_tokens")),
                _as_float(row.get("cost_usd")),
                _as_float(row.get("duration_ms")),
                str(row.get("caller") or ""),
                _jsonb(row),
            ),
        )

    _write_idempotent_entries(
        result,
        entries=entries,
        target_table="red_cost_events",
        write_row=_write_cost,
    )
    return result


def _backfill_api_errors(*, dry_run: bool, limit: int) -> Result:
    from agent_core import cost_tracker

    entries, exists = _iter_jsonl_entries(cost_tracker._API_ERROR_LOG, limit=limit)
    result = Result("api_errors", cost_tracker._API_ERROR_LOG, seen=len(entries))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result

    def _write_api_error(cur, row: Mapping[str, Any]) -> None:
        cur.execute(
            """
            INSERT INTO red_api_error_events (
                ts, service, status, model, detail, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                _parse_ts(row.get("ts")) or datetime.now().astimezone(),
                str(row.get("service") or ""),
                str(row.get("status") or ""),
                str(row.get("model") or ""),
                str(row.get("detail") or ""),
                _jsonb(row),
            ),
        )

    _write_idempotent_entries(
        result,
        entries=entries,
        target_table="red_api_error_events",
        write_row=_write_api_error,
    )
    return result


def _backfill_telegram_audit(*, dry_run: bool, limit: int) -> Result:
    from agent_core import telegram_audit

    path = telegram_audit.audit_file_path()
    entries, exists = _iter_jsonl_entries(path, limit=limit)
    result = Result("telegram_audit", path, seen=len(entries))
    if not exists:
        result.status = "missing"
        return result
    if dry_run:
        return result

    def _write_audit(cur, row: Mapping[str, Any]) -> None:
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
            operational_db._audit_insert_payload(row),
        )

    _write_idempotent_entries(
        result,
        entries=entries,
        target_table="red_audit_events",
        write_row=_write_audit,
    )
    return result


def _backfill_tool_budgets(*, dry_run: bool, limit: int) -> Result:
    from agent_core import tool_budgets

    root = tool_budgets._BUDGET_DIR
    result = Result("tool_budgets", root)
    if not os.path.isdir(root):
        result.status = "missing"
        return result
    files = sorted(name for name in os.listdir(root) if name.endswith(".json"))
    if limit > 0:
        files = files[-limit:]
    rows: list[tuple[date, str, dict[str, Any]]] = []
    for name in files:
        day = _parse_date(name[:-5])
        if day is None:
            result.skipped += 1
            continue
        data, _exists = _load_json(os.path.join(root, name), {})
        if not isinstance(data, dict):
            result.skipped += 1
            continue
        for tool_name, record in data.items():
            if isinstance(record, dict):
                rows.append((day, str(tool_name), record))
    result.seen = len(rows)
    if dry_run:
        return result
    operational_db.ensure_schema()
    with operational_db.connect() as conn:
        with conn.cursor() as cur:
            for day, tool_name, record in rows:
                last_at = _parse_ts(record.get("last_at"))
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
                        _as_int(record.get("daily")),
                        str(record.get("hour") or ""),
                        _as_int(record.get("hour_count")),
                        last_at,
                        _jsonb(record.get("by_caller") or {}),
                    ),
                )
                result.written += 1
    return result


def _backfill_task_queue(*, dry_run: bool, limit: int) -> Result:
    from agent_core import task_queue

    queue, q_exists = _load_json(task_queue._QUEUE_FILE, {"tasks": []})
    dlq, dlq_exists = _load_json(task_queue._DLQ_FILE, {"tasks": []})
    tasks = [t for t in queue.get("tasks", []) if isinstance(t, dict)] if isinstance(queue, dict) else []
    dead = [t for t in dlq.get("tasks", []) if isinstance(t, dict)] if isinstance(dlq, dict) else []
    if limit > 0:
        tasks = tasks[-limit:]
        dead = dead[-limit:]
    result = Result(
        "task_queue",
        f"{task_queue._QUEUE_FILE};{task_queue._DLQ_FILE}",
        seen=len(tasks) + len(dead),
    )
    if not q_exists and not dlq_exists:
        result.status = "missing"
        return result
    if dry_run:
        return result
    operational_db.ensure_schema()
    with operational_db.connect() as conn:
        with conn.cursor() as cur:
            for task in tasks:
                task_id = str(task.get("id") or "").strip()
                if not task_id:
                    result.skipped += 1
                    continue
                submitted = _parse_ts(task.get("submitted_at")) or datetime.now().astimezone()
                next_run = _parse_ts(task.get("next_run_at")) or submitted
                cur.execute(
                    """
                    INSERT INTO red_task_queue (
                        id, tool, kwargs, priority, state, submitted_at,
                        started_at, ended_at, attempts, max_retries,
                        timeout_sec, mutex_group, next_run_at, last_error,
                        cancel_requested, worker_id, result_preview
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        tool = EXCLUDED.tool,
                        kwargs = EXCLUDED.kwargs,
                        priority = EXCLUDED.priority,
                        state = EXCLUDED.state,
                        submitted_at = EXCLUDED.submitted_at,
                        started_at = EXCLUDED.started_at,
                        ended_at = EXCLUDED.ended_at,
                        attempts = EXCLUDED.attempts,
                        max_retries = EXCLUDED.max_retries,
                        timeout_sec = EXCLUDED.timeout_sec,
                        mutex_group = EXCLUDED.mutex_group,
                        next_run_at = EXCLUDED.next_run_at,
                        last_error = EXCLUDED.last_error,
                        cancel_requested = EXCLUDED.cancel_requested,
                        worker_id = EXCLUDED.worker_id,
                        result_preview = EXCLUDED.result_preview,
                        updated_at = now()
                    """,
                    (
                        task_id,
                        str(task.get("tool") or ""),
                        _jsonb(task.get("kwargs") or {}),
                        _as_int(task.get("priority"), 5),
                        str(task.get("state") or task_queue.STATE_PENDING),
                        submitted,
                        _parse_ts(task.get("started_at")),
                        _parse_ts(task.get("ended_at")),
                        _as_int(task.get("attempts")),
                        _as_int(task.get("max_retries"), 3),
                        _as_int(task.get("timeout_sec"), 300),
                        str(task.get("mutex_group") or ""),
                        next_run,
                        task.get("last_error"),
                        _as_bool(task.get("cancel_requested")),
                        str(task.get("worker_id") or ""),
                        str(task.get("result_preview") or "")[:500],
                    ),
                )
                result.written += 1
            for task in dead:
                task_id = str(task.get("id") or "").strip()
                if not task_id:
                    result.skipped += 1
                    continue
                moved_at = _parse_ts(task.get("moved_to_dlq_at")) or datetime.now().astimezone()
                cur.execute(
                    """
                    INSERT INTO red_task_dead_letters (
                        id, tool, payload, moved_to_dlq_at, last_error
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        tool = EXCLUDED.tool,
                        payload = EXCLUDED.payload,
                        moved_to_dlq_at = EXCLUDED.moved_to_dlq_at,
                        last_error = EXCLUDED.last_error
                    """,
                    (
                        task_id,
                        str(task.get("tool") or ""),
                        _jsonb(task),
                        moved_at,
                        str(task.get("last_error") or ""),
                    ),
                )
                result.written += 1
    return result


SOURCES: dict[str, Callable[..., Result]] = {
    "task_queue": _backfill_task_queue,
    "task_memory": _backfill_task_memory,
    "edge_tasks": _backfill_edge_tasks,
    "dry_run": _backfill_dry_run,
    "work_mode": _backfill_work_mode,
    "web_domain_policy": _backfill_web_domain_policy,
    "alert_push": _backfill_alert_push,
    "policy_decisions": _backfill_policy_decisions,
    "intent_classifications": _backfill_intent_classifications,
    "run_history": _backfill_run_history,
    "cost_events": _backfill_cost_events,
    "api_errors": _backfill_api_errors,
    "telegram_audit": _backfill_telegram_audit,
    "tool_budgets": _backfill_tool_budgets,
}


def _selected_sources(raw: str) -> list[str]:
    if not raw or raw.strip().lower() == "all":
        return list(SOURCES)
    selected: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name:
            selected.append(name)
    unknown = [name for name in selected if name not in SOURCES]
    if unknown:
        raise SystemExit(f"unknown --only source(s): {', '.join(unknown)}")
    return selected


def _print_results(results: list[Result]) -> None:
    print("source,status,seen,written,skipped,path,error")
    for item in results:
        print(
            ",".join(
                [
                    item.name,
                    item.status,
                    str(item.seen),
                    str(item.written),
                    str(item.skipped),
                    json.dumps(item.path, ensure_ascii=False),
                    json.dumps(item.error, ensure_ascii=False),
                ]
            )
        )


def _json_payload(
    *,
    results: list[Result],
    dry_run: bool,
    only: str,
    selected: list[str],
    limit: int,
    failed: bool,
) -> dict[str, Any]:
    return {
        "status": "failed" if failed else "ok",
        "dry_run": dry_run,
        "only": only,
        "selected_sources": selected,
        "limit": limit,
        "db_configured": operational_db.enabled(),
        "schema_expected": operational_db.SCHEMA_VERSION,
        "totals": {
            "seen": sum(item.seen for item in results),
            "written": sum(item.written for item in results),
            "skipped": sum(item.skipped for item in results),
        },
        "results": [_result_to_dict(item) for item in results],
    }


def _result_to_dict(item: Result) -> dict[str, Any]:
    return {
        "source": item.name,
        "status": item.status,
        "seen": item.seen,
        "written": item.written,
        "skipped": item.skipped,
        "path": item.path,
        "error": item.error,
    }


def _print_json_results(
    *,
    results: list[Result],
    dry_run: bool,
    only: str,
    selected: list[str],
    limit: int,
    failed: bool,
) -> None:
    print(
        json.dumps(
            _json_payload(
                results=results,
                dry_run=dry_run,
                only=only,
                selected=selected,
                limit=limit,
                failed=failed,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count local records without writing to Postgres",
    )
    parser.add_argument(
        "--only",
        default="all",
        help="comma-separated source list, or all",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="only backfill the last N JSONL rows/files per source (0 = all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of CSV",
    )
    args = parser.parse_args(argv)

    selected = _selected_sources(args.only)
    if not args.dry_run and not operational_db.enabled():
        message = "RED_OPERATIONAL_DB_URL or RED_DATABASE_URL is required"
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "dry_run": False,
                        "only": args.only,
                        "selected_sources": selected,
                        "limit": max(0, args.limit),
                        "db_configured": False,
                        "schema_expected": operational_db.SCHEMA_VERSION,
                        "error": message,
                        "results": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(message, file=sys.stderr)
        return 2
    if not args.dry_run:
        operational_db.ensure_schema()

    results: list[Result] = []
    failed = False
    for name in selected:
        try:
            results.append(SOURCES[name](dry_run=args.dry_run, limit=max(0, args.limit)))
        except Exception as exc:  # noqa: BLE001 - keep reporting other sources
            failed = True
            results.append(Result(name, "", status="error", error=f"{type(exc).__name__}: {exc}"))
    if args.json:
        _print_json_results(
            results=results,
            dry_run=args.dry_run,
            only=args.only,
            selected=selected,
            limit=max(0, args.limit),
            failed=failed,
        )
    else:
        _print_results(results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
