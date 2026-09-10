"""Edge Agent task queue.

This queue is for department-owned RPA tasks that must be executed on an
employee computer. It is separate from ``task_queue.py`` because Edge tasks have
device assignment, human confirmation, and ERP audit requirements instead of
daemon-tool retry semantics.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text, logger


STATUS_DRAFT = "draft"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WAITING_CONFIRM = "waiting_confirm"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_WAITING_CONFIRM})
FINAL_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED})
VALID_STATUSES = frozenset({
    STATUS_DRAFT,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_WAITING_CONFIRM,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_CANCELLED,
})

_state_lock = threading.Lock()
_MAX_TASKS = 1000
_PG_EDGE_WARNING_UNTIL = 0.0
_PG_ACTIVE_STATES: list[dict[str, Any]] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_task_id() -> str:
    return "edge_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(3)


def _edge_tasks_file() -> str:
    explicit = os.environ.get("RED_EDGE_TASKS_FILE", "").strip()
    if explicit:
        return explicit
    object_root = os.environ.get("RED_OBJECT_STORAGE_DIR", "").strip()
    if object_root:
        return os.path.join(object_root, "data", "edge_tasks.json")
    return os.path.join(DATA_DIR, "edge_tasks.json")


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "devices": {},
        "tasks": [],
    }


def _normalize_state(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _default_state()
    data.setdefault("version", 1)
    data.setdefault("devices", {})
    data.setdefault("tasks", [])
    if not isinstance(data["devices"], dict):
        data["devices"] = {}
    if not isinstance(data["tasks"], list):
        data["tasks"] = []
    return data


def _warn_pg_edge_fallback(exc: Exception) -> None:
    global _PG_EDGE_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_EDGE_WARNING_UNTIL:
        return
    _PG_EDGE_WARNING_UNTIL = now + 30
    logger.warning("Postgres edge_tasks failed; falling back to file: %s", exc)


def _pg_edge_store():
    try:
        from agent_core import operational_edge_tasks as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - edge tasks should stay usable
        _warn_pg_edge_fallback(exc)
    return None


@contextlib.contextmanager
def _queue_lock():
    store = _pg_edge_store()
    if store:
        pg_data: dict[str, Any] | None = None
        body_failed = False
        try:
            with store.locked_state() as data:
                pg_data = data
                _PG_ACTIVE_STATES.append(data)
                try:
                    yield
                except BaseException:
                    body_failed = True
                    raise
                finally:
                    _PG_ACTIVE_STATES.pop()
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            if body_failed:
                raise
            _warn_pg_edge_fallback(exc)
            if pg_data is not None:
                _save_state_file(pg_data)
                return

    import fcntl

    _state_lock.acquire()
    lock_fd = None
    try:
        path = _edge_tasks_file()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            lock_fd = open(path + ".lock", "w")
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        except Exception as exc:
            print(f"[edge_tasks] cross-process lock failed ({exc}); using thread lock only")
            if lock_fd is not None:
                try:
                    lock_fd.close()
                except Exception:
                    pass
                lock_fd = None
        yield
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fd.close()
            except Exception:
                pass
        _state_lock.release()


def _load_state_unlocked() -> dict[str, Any]:
    if _PG_ACTIVE_STATES:
        return _normalize_state(_PG_ACTIVE_STATES[-1])
    path = _edge_tasks_file()
    if not os.path.exists(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return _normalize_state(json.load(handle))
    except Exception:
        return _default_state()


def _save_state_file(data: dict[str, Any]) -> None:
    path = _edge_tasks_file()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _atomic_write_text(path, json.dumps(_normalize_state(data), ensure_ascii=False, indent=2))


def _save_state_unlocked(data: dict[str, Any]) -> None:
    if _PG_ACTIVE_STATES:
        state = _PG_ACTIVE_STATES[-1]
        if data is state:
            _normalize_state(state)
            return
        snapshot = deepcopy(_normalize_state(data))
        state.clear()
        state.update(snapshot)
        return
    _save_state_file(data)


def load_state() -> dict[str, Any]:
    with _queue_lock():
        return deepcopy(_load_state_unlocked())


def _validate_department(department: str) -> str:
    from agent_core.agents.permission_matrix import Agent

    value = str(department or "").strip().lower()
    Agent(value)
    if value == "red":
        raise ValueError("red is not an Edge Agent execution department")
    return value


def _normalize_capabilities(capabilities: Any) -> list[str]:
    if capabilities is None:
        return []
    if not isinstance(capabilities, list):
        raise ValueError("capabilities must be a list")
    out = []
    for item in capabilities:
        value = str(item or "").strip()
        if value:
            out.append(value)
    return sorted(set(out))


def register_device(
    *,
    device_id: str,
    department: str,
    employee_email: str,
    device_name: str = "",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Register or refresh an Edge Agent device."""
    normalized_device_id = str(device_id or "").strip()
    if not normalized_device_id:
        raise ValueError("device_id is required")
    normalized_department = _validate_department(department)
    normalized_email = str(employee_email or "").strip().lower()
    if not normalized_email:
        raise ValueError("employee_email is required")
    normalized_capabilities = _normalize_capabilities(capabilities)

    with _queue_lock():
        data = _load_state_unlocked()
        now = _now_iso()
        existing = data["devices"].get(normalized_device_id, {})
        record = {
            "device_id": normalized_device_id,
            "department": normalized_department,
            "employee_email": normalized_email,
            "device_name": str(device_name or existing.get("device_name") or normalized_device_id).strip(),
            "capabilities": normalized_capabilities,
            "status": "active",
            "registered_at": existing.get("registered_at") or now,
            "last_seen_at": now,
        }
        data["devices"][normalized_device_id] = record
        _save_state_unlocked(data)
        return {"ok": True, "device": deepcopy(record)}


def _task_from_draft(edge_task: Mapping[str, Any]) -> dict[str, Any]:
    task = deepcopy(dict(edge_task))
    if task.get("type") != "edge_rpa":
        raise ValueError("edge task draft must have type=edge_rpa")
    department = _validate_department(str(task.get("department") or ""))
    recipe_id = str(task.get("recipe_id") or "").strip()
    if not recipe_id:
        raise ValueError("edge task draft missing recipe_id")
    payload = task.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("edge task draft payload must be an object")
    task["department"] = department
    return task


def enqueue_edge_task(
    draft_result: Mapping[str, Any],
    *,
    target_device_id: str = "",
    employee_email: str = "",
    created_by: str = "",
) -> dict[str, Any]:
    """Turn a validated Edge task draft into a queued task."""
    if not isinstance(draft_result, Mapping) or not draft_result.get("ok"):
        raise ValueError("draft_result must be an ok edge task draft result")
    draft_task = draft_result.get("task")
    if not isinstance(draft_task, Mapping):
        raise ValueError("draft_result missing task")
    edge_task = _task_from_draft(draft_task)

    normalized_device_id = str(target_device_id or edge_task.get("device_id") or "").strip()
    normalized_email = str(employee_email or edge_task.get("employee_email") or "").strip().lower()
    now = _now_iso()

    with _queue_lock():
        data = _load_state_unlocked()
        if len(data["tasks"]) >= _MAX_TASKS:
            raise RuntimeError("edge task queue is full")
        queue_task = {
            "task_id": _new_task_id(),
            "type": "edge_rpa",
            "department": edge_task["department"],
            "recipe_id": edge_task["recipe_id"],
            "recipe_label": edge_task.get("recipe_label", ""),
            "status": STATUS_QUEUED,
            "target_device_id": normalized_device_id,
            "employee_email": normalized_email,
            "created_by": str(created_by or "").strip(),
            "created_at": now,
            "updated_at": now,
            "claimed_at": "",
            "claimed_by_device_id": "",
            "completed_at": "",
            "last_error": "",
            "edge_task": edge_task,
            "events": [
                {
                    "at": now,
                    "status": STATUS_QUEUED,
                    "message": "queued",
                },
            ],
        }
        data["tasks"].append(queue_task)
        _save_state_unlocked(data)
        return {"ok": True, "task": deepcopy(queue_task)}


def _status_filter(statuses: list[str] | tuple[str, ...] | set[str] | str | None) -> set[str]:
    if statuses is None or statuses == "":
        return set()
    raw = [statuses] if isinstance(statuses, str) else list(statuses)
    normalized = {str(status or "").strip().lower() for status in raw if str(status or "").strip()}
    unknown = normalized - VALID_STATUSES
    if unknown:
        raise ValueError(f"unknown edge task status: {', '.join(sorted(unknown))}")
    return normalized


def _matches_task(
    task: Mapping[str, Any],
    *,
    department: str = "",
    device_id: str = "",
    employee_email: str = "",
    statuses: set[str] | None = None,
) -> bool:
    if department and task.get("department") != department:
        return False
    if statuses and task.get("status") not in statuses:
        return False
    target_device = str(task.get("target_device_id") or "").strip()
    if device_id and target_device and target_device != device_id:
        return False
    task_employee = str(task.get("employee_email") or "").strip().lower()
    if employee_email and task_employee and task_employee != employee_email:
        return False
    return True


def list_tasks(
    *,
    department: str = "",
    device_id: str = "",
    employee_email: str = "",
    statuses: list[str] | tuple[str, ...] | set[str] | str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    normalized_department = _validate_department(department) if department else ""
    normalized_device = str(device_id or "").strip()
    normalized_email = str(employee_email or "").strip().lower()
    status_set = _status_filter(statuses)
    limit = max(1, min(int(limit or 50), 200))

    with _queue_lock():
        data = _load_state_unlocked()
        tasks = [
            deepcopy(task)
            for task in reversed(data["tasks"])
            if _matches_task(
                task,
                department=normalized_department,
                device_id=normalized_device,
                employee_email=normalized_email,
                statuses=status_set,
            )
        ][:limit]
        return {"ok": True, "tasks": tasks, "total": len(tasks)}


def get_task(task_id: str) -> dict[str, Any]:
    needle = str(task_id or "").strip()
    if not needle:
        raise ValueError("task_id is required")
    with _queue_lock():
        data = _load_state_unlocked()
        for task in data["tasks"]:
            if task.get("task_id") == needle:
                return {"ok": True, "found": True, "task": deepcopy(task)}
    return {"ok": True, "found": False, "task": None}


def claim_next_task(
    *,
    device_id: str,
    department: str,
    employee_email: str = "",
) -> dict[str, Any]:
    normalized_device = str(device_id or "").strip()
    if not normalized_device:
        raise ValueError("device_id is required")
    normalized_department = _validate_department(department)
    normalized_email = str(employee_email or "").strip().lower()

    with _queue_lock():
        data = _load_state_unlocked()
        now = _now_iso()
        rec = data["devices"].get(normalized_device)
        if rec is not None:
            rec["last_seen_at"] = now
            # 已註冊裝置只能認領自己註冊部門的任務。所有 edge 裝置共用一把
            # RED_EDGE_AGENT_TOKEN，若只信 request body 的 department，註冊在 A 部門
            # 的裝置改傳 department=B 就能搶 B 部門的 RPA 任務（IDOR）。以持久化的
            # device→department 綁定為準。（未註冊裝置維持原本 graceful no-match。）
            if rec.get("department") != normalized_department:
                logger.warning(
                    "edge claim 部門不符：device=%s registered=%s requested=%s",
                    normalized_device, rec.get("department"), normalized_department,
                )
                _save_state_unlocked(data)
                return {"ok": True, "task": None}
        for task in data["tasks"]:
            if task.get("status") != STATUS_QUEUED:
                continue
            if not _matches_task(
                task,
                department=normalized_department,
                device_id=normalized_device,
                employee_email=normalized_email,
                statuses={STATUS_QUEUED},
            ):
                continue
            task["status"] = STATUS_RUNNING
            task["claimed_at"] = now
            task["claimed_by_device_id"] = normalized_device
            task["updated_at"] = now
            task.setdefault("events", []).append({
                "at": now,
                "status": STATUS_RUNNING,
                "device_id": normalized_device,
                "message": "claimed",
            })
            _save_state_unlocked(data)
            return {"ok": True, "task": deepcopy(task)}
        _save_state_unlocked(data)
        return {"ok": True, "task": None}


def update_task_status(
    *,
    task_id: str,
    device_id: str,
    status: str,
    message: str = "",
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    needle = str(task_id or "").strip()
    normalized_device = str(device_id or "").strip()
    normalized_status = str(status or "").strip().lower()
    if not needle:
        raise ValueError("task_id is required")
    if not normalized_device:
        raise ValueError("device_id is required")
    if normalized_status not in VALID_STATUSES - {STATUS_DRAFT, STATUS_QUEUED}:
        raise ValueError("status must be running, waiting_confirm, done, failed, or cancelled")
    if result is not None and not isinstance(result, Mapping):
        raise ValueError("result must be an object")

    with _queue_lock():
        data = _load_state_unlocked()
        now = _now_iso()
        for task in data["tasks"]:
            if task.get("task_id") != needle:
                continue
            target_device = str(task.get("target_device_id") or "").strip()
            claimed_device = str(task.get("claimed_by_device_id") or "").strip()
            if target_device and target_device != normalized_device:
                raise PermissionError("task is assigned to a different device")
            if claimed_device and claimed_device != normalized_device:
                raise PermissionError("task was claimed by a different device")
            task["status"] = normalized_status
            task["updated_at"] = now
            if normalized_status in FINAL_STATUSES:
                task["completed_at"] = now
            if normalized_status == STATUS_FAILED:
                task["last_error"] = str(message or "").strip()
            if result is not None:
                task["result"] = deepcopy(dict(result))
            task.setdefault("events", []).append({
                "at": now,
                "status": normalized_status,
                "device_id": normalized_device,
                "message": str(message or "").strip(),
            })
            _save_state_unlocked(data)
            return {"ok": True, "task": deepcopy(task)}
    return {"ok": False, "error": "task not found"}
