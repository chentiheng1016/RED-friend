"""Task memory — 「誰交代了什麼、什麼時候要做、現在進度到哪、關聯哪封信 / 哪個會議」。

跟 RAG 互補：
  RAG（chroma_db）  事實型記憶 — 客戶報過什麼價、產品規格、流程文件
                   query 是「請告訴我關於 X 的資料」
  task_memory       承諾型記憶 — 大王/客戶交代過什麼還沒做完
                   query 是「我答應誰要在何時前完成什麼？」

為什麼分兩個 store：
  - RAG embedding 適合語意相似搜尋，但不擅長「截止日 < 今天 + 2 天」這種條件
  - 事實會穩定（規格不變），承諾會變化（status 改、deadline 延、note 更新）
  - 承諾要主動 fire reminder（時間驅動）；RAG 是 query 驅動

Schema（每筆 task）：
  id, title, description, owner_requested_by, owner_assigned_to,
  created_at, deadline, status, priority,
  linked_email_threads / linked_email_messages / linked_calendar_events /
  linked_customer / linked_quote / linked_sample,
  next_reminder_at, reminder_history,
  log（每次 update 一筆 audit）

存儲：var/state/task_memory.json（fcntl-locked R-M-W via agent_core.state_io.locked_json
       — cross-process safe across all 11 telegram daemons + the foreground REPL）

安全：
  - 寫類 tool（add / update / link / set_reminder）= CONFIRM tier，要 +確認
  - delete_task = DANGEROUS（刪 commitment 是高風險記憶遺失）
  - 讀類 tool（list / detail / find / due_today）= SAFE
  - title / description 顯示時過 log_redact（避免歷史含 inline secret）
  - linked_* IDs 用 regex 驗證格式（防 path-traversal 樣式）
  - 上限 _MAX_TASKS=2000，超過拒新增（防 LLM 爆量）
  - reminder_at 不能 > 1 年後（防 typo 設成 9999 年）

Reminder firing：
  fire_due_reminders() → 掃所有 task，next_reminder_at <= now 的：
    1. 透過 telegram_push 通知
    2. 把這次 reminder ts 推到 reminder_history
    3. 清掉 next_reminder_at（避免重複觸發）
  由 queue_bootstrap.maybe_drain_after_tick 順便呼叫（tick daemon 跑完即可）。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Iterator

from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger
from agent_core.state_io import locked_json


# ────────────────────────────────────────────────────────────────────
# 常數 / 路徑
# ────────────────────────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
_VALID_STATUSES = frozenset({
    STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_BLOCKED,
    STATUS_DONE, STATUS_CANCELLED,
})
_OPEN_STATUSES = frozenset({STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_BLOCKED})

PRIORITY_HIGH = 1
PRIORITY_NORMAL = 5
PRIORITY_LOW = 9

_TASK_FILE = os.path.join(STATE_DIR, "task_memory.json")
_PG_TASK_MEMORY_WARNING_UNTIL = 0.0
_MAX_TASKS = 2000
_MAX_TITLE_LEN = 200
_MAX_DESC_LEN = 4000
_MAX_NOTE_LEN = 1000
_MAX_LOG_ENTRIES = 50  # 每個 task 保留最後 50 筆 log
_MAX_FUTURE_DAYS = 365  # deadline / reminder 不能設超過 1 年後

# linked id 格式驗證（保守：英數 / 底線 / 短槓 / 點 / @）
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_\-.@]{1,128}$")

# ────────────────────────────────────────────────────────────────────
# Locked R-M-W helper
# ────────────────────────────────────────────────────────────────────
# Why this exists: 11 telegram daemons + the foreground REPL all add /
# update / delete tasks via the public tools. Without a cross-process lock,
# two concurrent writers can both load v1, both mutate to v2, last writer
# wins — silent lost update of a user's task. The fix is fcntl.flock around
# the whole read-modify-write window (state_io.locked_json), so writers
# serialize at the OS level.
#
# We keep the thin _locked_tasks wrapper around locked_json so call sites
# don't repeat the default= and shape guard (locked_json yields the raw
# default {} on missing file; we want {"version": 1, "tasks": []}).
def _warn_pg_task_memory_fallback(exc: Exception) -> None:
    global _PG_TASK_MEMORY_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_TASK_MEMORY_WARNING_UNTIL:
        return
    _PG_TASK_MEMORY_WARNING_UNTIL = now + 30
    logger.warning("Postgres task_memory failed; falling back to file: %s", exc)


def _pg_task_store():
    try:
        from agent_core import operational_task_memory as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - task memory should stay usable
        _warn_pg_task_memory_fallback(exc)
    return None


def _save_tasks_file(data: dict) -> None:
    try:
        _atomic_write_text(_TASK_FILE,
                            json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


@contextlib.contextmanager
def _locked_tasks() -> Iterator[dict]:
    """Yield the parsed task_memory.json under an fcntl lock; auto-saved on
    normal exit (NOT on exception). Use for all read-modify-write paths.
    For read-only callers, plain _load_tasks() is fine — writes are atomic
    so readers never see a torn file."""
    store = _pg_task_store()
    if store:
        pg_data: dict | None = None
        body_failed = False
        try:
            with store.locked_tasks() as data:
                pg_data = data
                data.setdefault("tasks", [])
                try:
                    yield data
                except BaseException:
                    body_failed = True
                    raise
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            if body_failed:
                raise
            _warn_pg_task_memory_fallback(exc)
            if pg_data is not None:
                _save_tasks_file(pg_data)
                return

    with locked_json(_TASK_FILE, default={"version": 1, "tasks": []}) as data:
        data.setdefault("tasks", [])  # locked_json bypasses _load_tasks's shape guard
        yield data


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _deadline_cmp(deadline: str) -> str:
    """Normalize a deadline for lexicographic overdue comparison. A date-only
    deadline ('2026-06-21') means END of that day, but as a bare 10-char string it
    sorts BEFORE any same-day datetime → a task due today fires a spurious 🚨
    OVERDUE at 00:00（健檢 Low；tasks_due_today 已用 .startswith，這裡對齊）。Pad to
    T23:59:59 so it only counts overdue once the day is actually over."""
    d = (deadline or "").strip()
    if len(d) == 10 and d[4:5] == "-" and d[7:8] == "-":  # YYYY-MM-DD, no time part
        return d + "T23:59:59"
    return d


def _new_task_id() -> str:
    return "task_" + datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(3)


def _ensure_dir() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception:
        pass


def _load_tasks() -> dict:
    store = _pg_task_store()
    if store:
        try:
            data = store.load_tasks()
            data.setdefault("tasks", [])
            return data
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_task_memory_fallback(exc)

    _ensure_dir()
    if not os.path.isfile(_TASK_FILE):
        return {"version": 1, "tasks": []}
    try:
        with open(_TASK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("tasks", [])
        return data
    except Exception:
        return {"version": 1, "tasks": []}


def _save_tasks(data: dict) -> None:
    store = _pg_task_store()
    if store:
        try:
            store.replace_all(data)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_task_memory_fallback(exc)
    _save_tasks_file(data)


def _parse_when(when: str | None) -> datetime | None:
    """寬容解析 ISO datetime / YYYY-MM-DD / YYYY-MM-DD HH:MM。

    回 None = 不合法或空。
    """
    if not when:
        return None
    s = str(when).strip()
    if not s:
        return None
    # 試 ISO（帶 T）
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    # YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    # YYYY-MM-DD HH:MM
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        pass
    return None


def _validate_future_date(when: str, label: str) -> tuple[bool, str]:
    """deadline / reminder 必須在過去 1 年到未來 1 年之間。"""
    if not when:
        return True, ""  # 空字串 = 不設
    dt = _parse_when(when)
    if dt is None:
        return False, f"{label} 格式無法解析（請用 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS）"
    now = datetime.now()
    if dt > now + timedelta(days=_MAX_FUTURE_DAYS):
        return False, f"{label} 超過 {_MAX_FUTURE_DAYS} 天（{dt.isoformat()[:19]}）— 應該是 typo？"
    return True, ""


def _validate_link_id(link_id: str, label: str) -> tuple[bool, str]:
    if not link_id:
        return True, ""
    if not isinstance(link_id, str):
        return False, f"{label} 必須是字串"
    if not _VALID_ID_RE.match(link_id):
        return False, f"{label} 含非法字元（只允許 A-Z/a-z/0-9/_/-/.//@）"
    return True, ""


def _redact_text(text: str) -> str:
    try:
        from agent_core.log_redact import redact_log_line
        return redact_log_line(text or "")
    except Exception:
        return text or ""


def _find_task(tasks: list, task_id: str) -> dict | None:
    for t in tasks:
        if t.get("id") == task_id:
            return t
    return None


def _append_log(task: dict, note: str, by: str = "self") -> None:
    if not note:
        return
    log = task.setdefault("log", [])
    log.append({"at": _now_iso(), "by": by, "note": note[:_MAX_NOTE_LEN]})
    # 只留最後 _MAX_LOG_ENTRIES 筆
    if len(log) > _MAX_LOG_ENTRIES:
        task["log"] = log[-_MAX_LOG_ENTRIES:]


# ────────────────────────────────────────────────────────────────────
# Public API — Write
# ────────────────────────────────────────────────────────────────────
def add_task(title: str, description: str = "",
             owner_requested_by: str = "", owner_assigned_to: str = "self",
             deadline: str = "", priority: int = PRIORITY_NORMAL,
             linked_email_thread: str = "",
             linked_email_message: str = "",
             linked_calendar_event: str = "",
             linked_customer: str = "",
             linked_quote: str = "",
             linked_sample: str = "",
             reminder_at: str = "") -> Any:
    """🟢 加一個 task 進記憶。

    Args:
        title: 一句話描述（必填，<200 字元）
        description: 細節（可空）
        owner_requested_by: 誰交代的（客戶名 / 大王 / 自己想到的）
        owner_assigned_to: 誰執行（預設 'self' = 大王/小紅）
        deadline: 截止日 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS（可空）
        priority: 1=高 5=正常 9=低
        linked_*: 關聯資源 id（gmail thread/message id / calendar event id /
                  客戶名 / quote 編號 / sample 編號）
        reminder_at: 下次提醒時間（fire_due_reminders 會掃到）

    Returns:
        ToolResult — success.data 含 task_id；failure 含 error_code
    """
    from agent_core.tool_result import ToolResult, ErrorCode

    if not title or not isinstance(title, str):
        return ToolResult.failure("title 必填且須為字串",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    if len(title) > _MAX_TITLE_LEN:
        return ToolResult.failure(
            f"title 太長（{len(title)} > {_MAX_TITLE_LEN}）",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False)
    if description and len(description) > _MAX_DESC_LEN:
        description = description[:_MAX_DESC_LEN]

    # 驗證日期
    for label, val in (("deadline", deadline), ("reminder_at", reminder_at)):
        ok, err = _validate_future_date(val, label)
        if not ok:
            return ToolResult.failure(err,
                                       error_code=ErrorCode.INVALID_INPUT,
                                       recoverable=False)
    # 驗證 linked id
    for label, val in (("linked_email_thread", linked_email_thread),
                       ("linked_email_message", linked_email_message),
                       ("linked_calendar_event", linked_calendar_event),
                       ("linked_quote", linked_quote),
                       ("linked_sample", linked_sample)):
        ok, err = _validate_link_id(val, label)
        if not ok:
            return ToolResult.failure(err,
                                       error_code=ErrorCode.INVALID_INPUT,
                                       recoverable=False)
    # 範圍
    priority = max(1, min(9, int(priority)))

    task = {
        "id": _new_task_id(),
        "title": title,
        "description": description or "",
        "owner_requested_by": owner_requested_by or "",
        "owner_assigned_to": owner_assigned_to or "self",
        "created_at": _now_iso(),
        "deadline": deadline or "",
        "status": STATUS_PENDING,
        "priority": priority,
        "linked_email_threads": [linked_email_thread] if linked_email_thread else [],
        "linked_email_messages": [linked_email_message] if linked_email_message else [],
        "linked_calendar_events": [linked_calendar_event] if linked_calendar_event else [],
        "linked_customer": linked_customer or "",
        "linked_quote": linked_quote or "",
        "linked_sample": linked_sample or "",
        "next_reminder_at": reminder_at or "",
        "reminder_history": [],
        "log": [],
    }
    _append_log(task, f"created (priority={priority}, deadline={deadline or '—'})")

    with _locked_tasks() as data:
        # 上限：開放中的 task 超過 _MAX_TASKS 拒
        live = [t for t in data["tasks"] if t.get("status") in _OPEN_STATUSES]
        if len(live) >= _MAX_TASKS:
            return ToolResult.failure(
                f"task_memory 已滿（{len(live)} 個未完成 ≥ {_MAX_TASKS} 上限）",
                error_code=ErrorCode.RATE_LIMITED,
                recoverable=True,
                suggested_fix="先完成或取消舊 task 再加新的",
            )
        data["tasks"].append(task)
    return ToolResult.success(
        f"✅ 已加 task: {task['id']}  「{title[:30]}...」"
        if len(title) > 30 else f"✅ 已加 task: {task['id']}  「{title}」",
        data={"task_id": task["id"], "title": title, "deadline": deadline,
              "priority": priority},
        artifacts=[task["id"]],
    )


def update_task_status(task_id: str, status: str, note: str = "") -> Any:
    """🟢 改 task 狀態（pending / in_progress / blocked / done / cancelled）。"""
    from agent_core.tool_result import ToolResult, ErrorCode
    if status not in _VALID_STATUSES:
        return ToolResult.failure(
            f"status '{status}' 無效。有效值：{', '.join(sorted(_VALID_STATUSES))}",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False)
    with _locked_tasks() as data:
        task = _find_task(data["tasks"], task_id)
        if task is None:
            return ToolResult.failure(f"找不到 task {task_id}",
                                       error_code=ErrorCode.NOT_FOUND,
                                       recoverable=False)
        old = task.get("status")
        task["status"] = status
        if status in (STATUS_DONE, STATUS_CANCELLED):
            task["completed_at"] = _now_iso()
            task["next_reminder_at"] = ""  # 清掉 reminder
        _append_log(task, f"status: {old} → {status}" + (f"  note: {note}" if note else ""))
    return ToolResult.success(f"✅ task {task_id} 狀態 {old} → {status}",
                               data={"task_id": task_id, "status": status})


def complete_task(task_id: str, note: str = "") -> Any:
    """🟢 標記 task 完成（捷徑 = update_status(done, note)）。"""
    return update_task_status(task_id, STATUS_DONE, note)


def link_to_email(task_id: str, gmail_id: str, kind: str = "thread") -> Any:
    """🟢 把 task 跟 gmail thread / message 關聯。

    Args:
        task_id: task 編號
        gmail_id: thread_id 或 message_id
        kind: 'thread' 或 'message'
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    if kind not in ("thread", "message"):
        return ToolResult.failure("kind 只能是 'thread' 或 'message'",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    ok, err = _validate_link_id(gmail_id, "gmail_id")
    if not ok:
        return ToolResult.failure(err, error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    with _locked_tasks() as data:
        task = _find_task(data["tasks"], task_id)
        if task is None:
            return ToolResult.failure(f"找不到 task {task_id}",
                                       error_code=ErrorCode.NOT_FOUND,
                                       recoverable=False)
        key = "linked_email_threads" if kind == "thread" else "linked_email_messages"
        lst = task.setdefault(key, [])
        if gmail_id not in lst:
            lst.append(gmail_id)
            _append_log(task, f"link {kind}: {gmail_id}")
            return ToolResult.success(f"✅ task {task_id} 加 {kind}={gmail_id}",
                                       data={"task_id": task_id})
        return ToolResult.success(f"task {task_id} 已含此 {kind} link",
                                   data={"task_id": task_id, "duplicate": True})


def link_to_calendar(task_id: str, event_id: str) -> Any:
    """🟢 把 task 跟 calendar event 關聯。"""
    from agent_core.tool_result import ToolResult, ErrorCode
    ok, err = _validate_link_id(event_id, "event_id")
    if not ok:
        return ToolResult.failure(err, error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    with _locked_tasks() as data:
        task = _find_task(data["tasks"], task_id)
        if task is None:
            return ToolResult.failure(f"找不到 task {task_id}",
                                       error_code=ErrorCode.NOT_FOUND,
                                       recoverable=False)
        lst = task.setdefault("linked_calendar_events", [])
        if event_id not in lst:
            lst.append(event_id)
            _append_log(task, f"link calendar event: {event_id}")
            return ToolResult.success(f"✅ task {task_id} 加 calendar event={event_id}",
                                       data={"task_id": task_id})
        return ToolResult.success(f"task {task_id} 已含此 event link",
                                   data={"task_id": task_id, "duplicate": True})


def set_task_reminder(task_id: str, reminder_at: str) -> Any:
    """🟢 設定下次提醒時間（fire_due_reminders 會掃到）。

    reminder_at: YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS。空字串 = 取消提醒。
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    ok, err = _validate_future_date(reminder_at, "reminder_at")
    if not ok:
        return ToolResult.failure(err, error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    with _locked_tasks() as data:
        task = _find_task(data["tasks"], task_id)
        if task is None:
            return ToolResult.failure(f"找不到 task {task_id}",
                                       error_code=ErrorCode.NOT_FOUND,
                                       recoverable=False)
        task["next_reminder_at"] = reminder_at
        _append_log(task, f"reminder set: {reminder_at or '(cleared)'}")
    return ToolResult.success(
        f"✅ task {task_id} 提醒 → {reminder_at or '(已取消)'}",
        data={"task_id": task_id, "next_reminder_at": reminder_at})


def delete_task(task_id: str) -> Any:
    """🔴 刪 task（DANGEROUS — 失去 commitment 記憶）。"""
    from agent_core.tool_result import ToolResult, ErrorCode
    with _locked_tasks() as data:
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        if len(data["tasks"]) == before:
            return ToolResult.failure(f"找不到 task {task_id}",
                                       error_code=ErrorCode.NOT_FOUND,
                                       recoverable=False)
    return ToolResult.success(f"✅ task {task_id} 已刪除",
                               data={"task_id": task_id})


# ────────────────────────────────────────────────────────────────────
# Public API — Read
# ────────────────────────────────────────────────────────────────────
def list_tasks(status: str = "open", customer: str = "",
               due_within_days: int = 0, limit: int = 30) -> str:
    """🟢 列 task。

    Args:
        status: 'open'（pending+in_progress+blocked，預設）/ 'all' / 單一 status
        customer: 過濾 linked_customer
        due_within_days: 0 = 不過濾；>0 = 只列 N 天內到期
        limit: 顯示上限
    """
    data = _load_tasks()
    tasks = data["tasks"]
    if status == "open":
        tasks = [t for t in tasks if t.get("status") in _OPEN_STATUSES]
    elif status and status != "all":
        tasks = [t for t in tasks if t.get("status") == status]
    if customer:
        tasks = [t for t in tasks if t.get("linked_customer") == customer]
    if due_within_days > 0:
        cutoff = (datetime.now() + timedelta(days=due_within_days)).isoformat()
        tasks = [t for t in tasks
                 if t.get("deadline") and t["deadline"] <= cutoff]
    # 排序：priority → deadline（早的優先）→ created_at
    tasks.sort(key=lambda t: (t.get("priority", 5),
                               t.get("deadline") or "9999",
                               t.get("created_at") or ""))
    out = [f"📋 task memory — {len(tasks)} 個" +
           (f"（status={status}）" if status != "open" else "（未完成）")]
    out.append("─" * 60)
    if not tasks:
        out.append("  ✅ 沒有符合條件的 task")
        return "\n".join(out)
    icons = {STATUS_PENDING: "⏳", STATUS_IN_PROGRESS: "🔄",
             STATUS_BLOCKED: "🚧", STATUS_DONE: "✅", STATUS_CANCELLED: "🚫"}
    now_iso = _now_iso()
    for t in tasks[:limit]:
        ic = icons.get(t.get("status"), "?")
        title = _redact_text(t.get("title", ""))[:50]
        deadline = (t.get("deadline") or "")[:16]
        overdue = deadline and _deadline_cmp(deadline) < now_iso
        deadline_mark = "⚠️ " if overdue else ""
        owner = t.get("owner_requested_by", "—")[:12]
        out.append(f"  {ic} pri={t.get('priority', 5)} {t['id']} "
                   f"  {title}")
        out.append(f"      由 {owner} 交代  {deadline_mark}截止 {deadline or '—'}"
                   + (f"  客戶 {t['linked_customer']}" if t.get("linked_customer") else ""))
    if len(tasks) > limit:
        out.append(f"  ...（還有 {len(tasks)-limit} 個未顯示）")
    out.append("")
    out.append("💡 task_detail('<id>') 看詳情；update_task_status / complete_task 改狀態")
    return "\n".join(out)


def task_detail(task_id: str) -> str:
    """🟢 看單個 task 細節（含 log）。"""
    data = _load_tasks()
    task = _find_task(data["tasks"], task_id)
    if task is None:
        return f"❌ 找不到 task {task_id}"
    out = [
        f"📋 {task['id']}",
        f"  title       : {_redact_text(task['title'])}",
        f"  status      : {task.get('status')}",
        f"  priority    : {task.get('priority')}",
        f"  由誰交代    : {task.get('owner_requested_by') or '—'}",
        f"  指派給      : {task.get('owner_assigned_to') or 'self'}",
        f"  截止        : {task.get('deadline') or '—'}",
        f"  下次提醒    : {task.get('next_reminder_at') or '—'}",
        f"  創建        : {task.get('created_at')}",
    ]
    if task.get("description"):
        out.append(f"  description : {_redact_text(task['description'])[:300]}")
    # 關聯
    links = []
    for k, label in (("linked_email_threads", "Gmail threads"),
                      ("linked_email_messages", "Gmail messages"),
                      ("linked_calendar_events", "Calendar events")):
        v = task.get(k) or []
        if v:
            links.append(f"    {label}: {', '.join(v[:5])}")
    for k, label in (("linked_customer", "客戶"),
                      ("linked_quote", "Quote"),
                      ("linked_sample", "Sample")):
        v = task.get(k)
        if v:
            links.append(f"    {label}: {v}")
    if links:
        out.append("  關聯：")
        out.extend(links)
    # log
    log = task.get("log") or []
    if log:
        out.append(f"  log（最後 {min(len(log), 8)} 筆）：")
        for e in log[-8:]:
            note_safe = _redact_text(e.get("note", ""))[:80]
            out.append(f"    [{e.get('at', '')[:16]}] {e.get('by', '?')} — {note_safe}")
    return "\n".join(out)


def find_tasks_by(query: str, limit: int = 15) -> str:
    """🟢 在 title / description / owner / linked_customer 找含 query 字串的 task。"""
    if not query or not isinstance(query, str):
        return "❌ query 必填（字串）"
    q = query.lower()
    data = _load_tasks()
    matches = []
    for t in data["tasks"]:
        haystack = " ".join([
            t.get("title", ""), t.get("description", ""),
            t.get("owner_requested_by", ""), t.get("linked_customer", ""),
        ]).lower()
        if q in haystack:
            matches.append(t)
    matches.sort(key=lambda t: (t.get("priority", 5), t.get("created_at") or ""))
    if not matches:
        return f"  （沒找到含「{query}」的 task）"
    out = [f"🔍 含「{query}」的 task — {len(matches)} 筆"]
    icons = {STATUS_PENDING: "⏳", STATUS_IN_PROGRESS: "🔄",
             STATUS_BLOCKED: "🚧", STATUS_DONE: "✅", STATUS_CANCELLED: "🚫"}
    for t in matches[:limit]:
        ic = icons.get(t.get("status"), "?")
        title = _redact_text(t.get("title", ""))[:60]
        out.append(f"  {ic} {t['id']}  {title}")
    return "\n".join(out)


def tasks_due_today() -> str:
    """🟢 今天 (date) 截止的 task。"""
    today = datetime.now().strftime("%Y-%m-%d")
    data = _load_tasks()
    tasks = [t for t in data["tasks"]
             if t.get("status") in _OPEN_STATUSES
             and (t.get("deadline") or "").startswith(today)]
    tasks.sort(key=lambda t: t.get("priority", 5))
    if not tasks:
        return "  ✅ 今天沒有截止的 task"
    out = [f"📅 今日截止 — {len(tasks)} 個 task"]
    for t in tasks:
        title = _redact_text(t.get("title", ""))[:50]
        out.append(f"  ⏳ pri={t.get('priority')} {t['id']}  {title}")
        owner = t.get("owner_requested_by", "—")
        out.append(f"      由 {owner} 交代")
    return "\n".join(out)


def tasks_overdue() -> str:
    """🟢 已逾期但 status 還在 open 的 task。"""
    now_iso = _now_iso()
    data = _load_tasks()
    tasks = [t for t in data["tasks"]
             if t.get("status") in _OPEN_STATUSES
             and t.get("deadline") and _deadline_cmp(t["deadline"]) < now_iso]
    tasks.sort(key=lambda t: t.get("deadline") or "")
    if not tasks:
        return "  ✅ 沒有逾期 task"
    out = [f"🔴 逾期 — {len(tasks)} 個 task"]
    for t in tasks[:20]:
        title = _redact_text(t.get("title", ""))[:50]
        out.append(f"  ⚠️ pri={t.get('priority')} {t['id']}  {title}")
        out.append(f"      截止 {t.get('deadline', '')[:16]}  "
                   f"由 {t.get('owner_requested_by', '—')} 交代")
    return "\n".join(out)


def tasks_for_email(gmail_thread_or_msg_id: str) -> str:
    """🟢 找關聯到某個 gmail thread / message 的 task（給 reply 時帶 context）。"""
    if not gmail_thread_or_msg_id:
        return "❌ 必須提供 gmail thread / message id"
    data = _load_tasks()
    matches = [t for t in data["tasks"]
               if gmail_thread_or_msg_id in (t.get("linked_email_threads") or [])
               or gmail_thread_or_msg_id in (t.get("linked_email_messages") or [])]
    if not matches:
        return f"  （沒有 task 關聯 {gmail_thread_or_msg_id}）"
    out = [f"📧 關聯 {gmail_thread_or_msg_id} 的 task — {len(matches)} 筆"]
    for t in matches:
        title = _redact_text(t.get("title", ""))[:60]
        out.append(f"  • {t['id']}  [{t.get('status')}]  {title}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Reminder firing — 由 daemon tick 呼叫
# ────────────────────────────────────────────────────────────────────
# Deadline escalation：逾期後再 fire 一次「🚨 OVERDUE」（每個 task 只一次，
# 用 escalation_fired_at 旗標避免重複 spam）
_DEADLINE_ESCALATION_DELAY_HOURS = 0  # 0 = deadline 一過就立刻 escalate


def _compute_next_recurring(task: dict, now: datetime) -> str:
    """recurring task 算下次 reminder_at。回 ISO 字串，若 recurrence 無效或 max 用完回空字串。"""
    rec = task.get("recurrence") or {}
    interval_days = int(rec.get("interval_days", 0))
    interval_hours = int(rec.get("interval_hours", 0))
    if interval_days <= 0 and interval_hours <= 0:
        return ""  # 不是 recurring
    max_repeats = int(rec.get("max_repeats", 0))
    repeat_count = int(rec.get("repeat_count", 0)) + 1
    rec["repeat_count"] = repeat_count
    task["recurrence"] = rec
    # 達上限 → 不再排
    if max_repeats > 0 and repeat_count >= max_repeats:
        return ""
    next_dt = now + timedelta(days=interval_days, hours=interval_hours)
    return next_dt.isoformat(timespec="seconds")


def fire_due_reminders() -> int:
    """掃所有 task，fire 兩種 reminder：

    1. **next_reminder_at <= now** — 用戶設的提醒時間到
       - 若 task 有 recurrence → 自動排下次，repeat_count++
       - 若沒 recurrence → 一次性 fire 後清掉 next_reminder_at
    2. **deadline_escalation** — deadline 已過 + 仍 open + 還沒 escalate 過
       → fire 一次 🚨 OVERDUE（每個 task 只一次，用 escalation_fired_at 防重）

    回傳：實際 fire 的 reminder 總數（兩種加起來）。
    """
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    escalation_threshold = (
        now - timedelta(hours=_DEADLINE_ESCALATION_DELAY_HOURS)
    ).isoformat(timespec="seconds")
    fired = 0
    notifications: list[tuple[dict, str]] = []  # (task_snapshot, msg_kind)
    # Parallel list (same index as notifications): how to undo the "fired" state
    # if the Telegram push fails. The state mutation is committed on lock exit
    # BEFORE the push — so without this, a failed push would clear
    # next_reminder_at / set escalation_fired_at permanently and the reminder
    # would be lost forever. On push failure we roll back → re-fires next tick.
    rollbacks: list[dict] = []

    with _locked_tasks() as data:
        for t in data["tasks"]:
            if t.get("status") not in _OPEN_STATUSES:
                continue

            # ── (1) next_reminder_at fire ──
            r = t.get("next_reminder_at")
            if r and r <= now_iso:
                prev_recurrence = dict(t["recurrence"]) if t.get("recurrence") else None
                prev_history = list(t.get("reminder_history", []))
                t.setdefault("reminder_history", []).append(now_iso)
                t["reminder_history"] = t["reminder_history"][-20:]
                # recurring → 自動排下次
                next_iso = _compute_next_recurring(t, now)
                if next_iso:
                    t["next_reminder_at"] = next_iso
                    _append_log(t, f"reminder fired; next at {next_iso[:16]}")
                else:
                    t["next_reminder_at"] = ""
                    _append_log(t, "reminder fired (one-shot)")
                notifications.append((dict(t), "reminder"))
                rollbacks.append({
                    "id": t["id"],
                    "expect": ("next_reminder_at", t["next_reminder_at"]),
                    "restore": {
                        "next_reminder_at": r,
                        "recurrence": prev_recurrence,
                        "reminder_history": prev_history,
                    },
                })

            # ── (2) deadline escalation ──
            deadline = t.get("deadline") or ""
            if (deadline and _deadline_cmp(deadline) < escalation_threshold
                    and not t.get("escalation_fired_at")):
                t["escalation_fired_at"] = now_iso
                _append_log(t, f"OVERDUE escalation fired (deadline {deadline[:16]})")
                notifications.append((dict(t), "overdue"))
                rollbacks.append({
                    "id": t["id"],
                    "expect": ("escalation_fired_at", now_iso),
                    "restore": {"escalation_fired_at": ""},
                })

    # push 在 lock 外（避免 telegram_push 慢拖住所有寫入）
    failed_idx: list[int] = []
    for idx, (t, kind) in enumerate(notifications):
        try:
            from agent_core.telegram import telegram_push
            title = _redact_text(t.get("title", ""))[:80]
            owner = t.get("owner_requested_by", "")
            deadline = t.get("deadline", "")
            if kind == "overdue":
                header = f"🚨 OVERDUE：{title}"
                detail = (f"截止 {deadline[:16]}（已過）"
                          + (f"  由 {owner} 交代" if owner else ""))
            else:
                header = f"📌 任務提醒：{title}"
                rec = t.get("recurrence") or {}
                if rec.get("interval_days"):
                    header += f" (每 {rec['interval_days']} 天)"
                detail = ((f"由 {owner} 交代  " if owner else "")
                          + (f"截止 {deadline[:16]}" if deadline else ""))
            msg = (f"{header}\n"
                   + (detail + "\n" if detail else "")
                   + f"  task_id: {t['id']}\n"
                   + f"  task_detail('{t['id']}') 看詳情")
            telegram_push(msg)
            fired += 1
        except Exception:
            failed_idx.append(idx)  # roll back below so it re-fires next tick

    # 推播失敗 → 回滾「已 fire」狀態（state 已在 lock 退出時寫入）。只在「我們剛
    # 寫的值還在」時才回滾，避免蓋掉這段期間使用者另外設的新提醒。
    if failed_idx:
        try:
            with _locked_tasks() as data:
                by_id = {t["id"]: t for t in data["tasks"]}
                for idx in failed_idx:
                    rb = rollbacks[idx]
                    t = by_id.get(rb["id"])
                    if t is None:
                        continue
                    exp_key, exp_val = rb["expect"]
                    if t.get(exp_key) != exp_val:
                        continue  # 被別處改過，別蓋
                    for key, val in rb["restore"].items():
                        if key == "recurrence" and val is None:
                            t.pop("recurrence", None)
                        else:
                            t[key] = val
                    _append_log(t, "reminder push 失敗 → 已回滾，下次 tick 重試")
        except Exception:
            pass  # 回滾本身失敗也不該炸 daemon tick
    return fired


def set_recurring_reminder(task_id: str, *,
                            interval_days: int = 0,
                            interval_hours: int = 0,
                            max_repeats: int = 0) -> Any:
    """🟢 把 task 設成週期性 reminder（每 N 天 / N 小時 fire 一次）。

    Args:
        task_id: 任務 id
        interval_days: 每幾天 repeat（與 hours 擇一或兩者都用）
        interval_hours: 每幾小時 repeat
        max_repeats: 0 = 無限；>0 = fire 滿 N 次自動停

    使用情境：每週一檢查 backlog、每天提醒 daily standup、每月 invoice 月結。
    第一次 reminder 在 next_reminder_at 設好的時間，之後自動加 interval。
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    if interval_days < 0 or interval_hours < 0:
        return ToolResult.failure("interval 不能負數",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    if interval_days == 0 and interval_hours == 0:
        return ToolResult.failure("至少要設 interval_days 或 interval_hours 其一",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    if max_repeats < 0:
        return ToolResult.failure("max_repeats 不能負數",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    with _locked_tasks() as data:
        task = _find_task(data["tasks"], task_id)
        if task is None:
            return ToolResult.failure(f"找不到 task {task_id}",
                                       error_code=ErrorCode.NOT_FOUND,
                                       recoverable=False)
        task["recurrence"] = {
            "interval_days": interval_days,
            "interval_hours": interval_hours,
            "max_repeats": max_repeats,
            "repeat_count": 0,
        }
        # 若沒設 next_reminder_at，立刻設成第一次（now + interval）
        if not task.get("next_reminder_at"):
            now = datetime.now()
            first = now + timedelta(days=interval_days, hours=interval_hours)
            task["next_reminder_at"] = first.isoformat(timespec="seconds")
        rec_str = (f"每 {interval_days}d {interval_hours}h"
                   + (f" (max {max_repeats} 次)" if max_repeats else ""))
        _append_log(task, f"recurring set: {rec_str}")
    return ToolResult.success(
        f"✅ task {task_id} 設成 recurring：{rec_str}",
        data={"task_id": task_id, "interval_days": interval_days,
              "interval_hours": interval_hours, "max_repeats": max_repeats},
    )


def clear_recurring_reminder(task_id: str) -> Any:
    """🟢 把 task 從 recurring 改回一次性（清掉 recurrence）。"""
    from agent_core.tool_result import ToolResult, ErrorCode
    with _locked_tasks() as data:
        task = _find_task(data["tasks"], task_id)
        if task is None:
            return ToolResult.failure(f"找不到 task {task_id}",
                                       error_code=ErrorCode.NOT_FOUND,
                                       recoverable=False)
        if not task.get("recurrence"):
            return ToolResult.success(f"task {task_id} 本來就不是 recurring",
                                       data={"task_id": task_id})
        task.pop("recurrence", None)
        _append_log(task, "recurring cleared")
    return ToolResult.success(f"✅ task {task_id} 不再 recurring",
                               data={"task_id": task_id})


# ────────────────────────────────────────────────────────────────────
# Auto-link & reverse-hint helpers — 給 daemon / LLM 用
# ────────────────────────────────────────────────────────────────────
def link_last_sent_email_to_task(task_id: str) -> Any:
    """🟢 從 runs/index.jsonl 找最近一次 send_gmail / reply_gmail 成功的 thread_id，
    自動連到指定 task。

    使用情境：LLM 剛幫大王回信，回信後接著呼叫此 helper，不用手動傳 thread_id。
    這個 helper 從 audit log 裡撈，所以 send_gmail 不用改。
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    from agent_core.logging_and_paths import RUNS_DIR
    import os
    # 先確認 task 存在 (read-only check — no lock needed; atomic writes mean
    # we always see a consistent snapshot, and the actual link is done below
    # under _locked_tasks)
    data = _load_tasks()
    if _find_task(data["tasks"], task_id) is None:
        return ToolResult.failure(f"找不到 task {task_id}",
                                   error_code=ErrorCode.NOT_FOUND,
                                   recoverable=False)
    # 從 runs/index.jsonl 倒讀，找最近一筆 send_gmail / reply_gmail 成功
    idx_path = os.path.join(RUNS_DIR, "index.jsonl")
    if not os.path.isfile(idx_path):
        return ToolResult.failure("沒有 runs/index.jsonl，無從找近期寄信紀錄",
                                   error_code=ErrorCode.NOT_FOUND,
                                   recoverable=False)
    target_tools = {"send_gmail", "reply_gmail", "create_draft"}
    candidate = None
    try:
        with open(idx_path, "r", encoding="utf-8") as f:
            for ln in reversed(f.readlines()):
                try:
                    r = json.loads(ln)
                except (ValueError, TypeError):
                    continue
                if r.get("tool") in target_tools and r.get("status") == "success":
                    candidate = r
                    break
    except Exception as e:
        return ToolResult.failure(f"讀 runs/index 失敗：{e}",
                                   error_code=ErrorCode.INTERNAL,
                                   recoverable=True)
    if not candidate:
        return ToolResult.failure(
            "近期沒有 send_gmail / reply_gmail 成功紀錄",
            error_code=ErrorCode.NOT_FOUND,
            recoverable=False,
            suggested_fix="先寄信再呼叫此 helper，或用 link_to_email 手動傳 thread_id",
        )
    # 從 short_result 抓 thread_id（gmail tool 通常會在 result 含 thread_id）
    short = candidate.get("short_result", "") or ""
    thread_match = re.search(r"thread[_\s]*id[:\s=]+(\S+)", short, re.IGNORECASE)
    if not thread_match:
        # 退而求其次 — 抓 18+ char 16進位（gmail thread id 長這樣）
        thread_match = re.search(r"\b([0-9a-f]{16,})\b", short, re.IGNORECASE)
    if not thread_match:
        return ToolResult.failure(
            f"近期寄信紀錄沒抓到 thread_id（tool={candidate.get('tool')}, "
            f"id={candidate.get('id', '')[:30]}）",
            error_code=ErrorCode.NOT_FOUND,
            recoverable=False,
            suggested_fix="用 link_to_email(task_id, thread_id) 手動連結",
        )
    thread_id = thread_match.group(1)
    # 連結
    return link_to_email(task_id, thread_id, kind="thread")


def notify_task_for_thread(thread_id: str, channel: str = "telegram") -> int:
    """🟢 給 daemon 用：收到 email reply 時，若 thread_id 關聯到 open task，
    自動 push 一則 hint 訊息提醒大王。

    Args:
        thread_id: gmail thread id
        channel: 'telegram'（預設）/ 其他先保留

    Returns:
        push 出去的 hint 數（0 = thread_id 沒關聯到任何 open task）

    使用情境：daemon_email_ingest / mailcheck 收到新信時呼叫，自動把
    「這封信屬於 task X」推給大王。把「task → email」反查做成主動通知。
    """
    if not thread_id:
        return 0
    data = _load_tasks()
    matches = [t for t in data["tasks"]
               if t.get("status") in _OPEN_STATUSES
               and (thread_id in (t.get("linked_email_threads") or [])
                    or thread_id in (t.get("linked_email_messages") or []))]
    if not matches:
        return 0
    pushed = 0
    for t in matches:
        try:
            from agent_core.telegram import telegram_push
            title = _redact_text(t.get("title", ""))[:60]
            owner = t.get("owner_requested_by", "")
            msg = (
                f"📧 收到關聯 task 的回信：\n"
                f"  task: {title}\n"
                + (f"  由 {owner} 交代\n" if owner else "")
                + f"  task_id: {t['id']}\n"
                + f"  thread: {thread_id[:24]}\n"
                f"  💡 task_detail('{t['id']}') 看完整 context"
            )
            telegram_push(msg)
            pushed += 1
        except Exception:
            pass
    return pushed


# ────────────────────────────────────────────────────────────────────
# Dashboard helper
# ────────────────────────────────────────────────────────────────────
def task_summary() -> dict:
    """給 dashboard 的精簡摘要。"""
    now_iso = _now_iso()
    today = datetime.now().strftime("%Y-%m-%d")
    data = _load_tasks()
    counts: dict[str, int] = {}
    overdue = 0
    due_today = 0
    has_reminder = 0
    for t in data["tasks"]:
        st = t.get("status", "?")
        counts[st] = counts.get(st, 0) + 1
        if st in _OPEN_STATUSES:
            d = t.get("deadline") or ""
            # date-only 截止日要過 _deadline_cmp 補到 T23:59:59，否則當天
            # 00:00 起就被算逾期、due_today 反而搶不到它。
            if d and _deadline_cmp(d) < now_iso:
                overdue += 1
            elif d.startswith(today):
                due_today += 1
            if t.get("next_reminder_at"):
                has_reminder += 1
    return {
        "total": len(data["tasks"]),
        "by_status": counts,
        "overdue": overdue,
        "due_today": due_today,
        "with_reminder": has_reminder,
    }
