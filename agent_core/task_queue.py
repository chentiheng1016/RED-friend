"""Task queue — priority + retry + timeout + cancel + mutex + DLQ.

問題：背景任務都各跑各的，沒互斥也沒重試。
  - Gmail ingest 跟 internal_email ingest 同時寫 data_lake_internal/*.parquet
    → file lock 撞到，其中一邊默默壞掉
  - email_lake_rebuild 跑時剛好 quote_batch_extract 在讀 → race
  - send_briefing_email 失敗就丟掉，沒人知道也沒 retry
  - 沒辦法「給某個慢 task 設 timeout，超過就殺掉」

設計：
  Queue：FIFO + priority（小數字 = 高優先），高優先先跑
  Retry：失敗自動重試，指數 backoff（30s → 60s → 120s）
  Timeout：每個 task 一個硬上限秒數，超過視為失敗 retry
  Cancel：pending task 可直接取消；running task 設旗標但無法強殺 thread
  Mutex：named lock — 同 group 同時只有一個 task 跑
         （e.g. ingest_internal_emails、email_lake_rebuild、quote_batch_extract
          都歸 "data_lake_writer" group，自動排隊）
  DLQ：retry 用完進 dead-letter queue，大王手動審核要不要 requeue

存儲：
  var/state/task_queue.json     — pending + running tasks（短壽）
  var/state/task_queue_dlq.json — dead letters（保 30 天）
  原子寫（_atomic_write_text）+ threading.Lock（同 process race）

安全考量（這 codebase 是 paranoid 路線）：
  - submit_task 自身列為 DANGEROUS — 因為它會在 daemon channel 跑任意 tool
    （daemon channel CONFIRM/DANGEROUS = "allow"，等於繞過 +確認）
  - inner tool 仍走 wrap_sensitive_tool(channel='daemon') → LOCKED 還是拒絕
  - allow-list（_QUEUEABLE_TOOLS）多一道：即使 LLM 騙到 +雙確認，也只能
    queue 列在白名單的 tool；不在 list 的直接拒
  - tool kwargs redact — DLQ 顯示時過 log_redact 防歷史 secret 外洩
  - 不允許 queue submit_task 自身（防遞迴 self-bomb）

未來可加（沒做，避免一次堆太多）：
  - 跨 process file lock（fcntl）— 目前 single-process daemon 內 OK
  - cron-style 重複（已有 scheduler.py 處理；queue 是 one-shot）
  - 進度回報（task 跑到一半回傳 % progress）
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from agent_core.env_utils import env_int as _env_int
from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text


# ────────────────────────────────────────────────────────────────────
# 常數
# ────────────────────────────────────────────────────────────────────
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"     # 已 retry 用完，等下一個 tick 移到 DLQ
STATE_DEAD = "dead"          # 在 DLQ 中
STATE_CANCELLED = "cancelled"

PRIORITY_HIGH = 1
PRIORITY_NORMAL = 5
PRIORITY_LOW = 9

# 預設值
_DEFAULT_TIMEOUT_SEC = 300       # 5 分鐘
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_PRIORITY = PRIORITY_NORMAL
_BACKOFF_BASE_SEC = 30           # 30, 60, 120, 240... 指數
_IDLE_POLL_SEC = 5               # worker 空閒時 poll 間隔
_DLQ_RETAIN_DAYS = 30
_QUEUE_MAX_SIZE = 500            # 防爆量


# 殭屍 RUNNING：worker 程序在 claim 後、寫回前死掉（watchdog os._exit /
# SIGTERM / 自我重啟），task 會永遠卡 RUNNING、佔住 mutex group 又算進上限。
# 超過 timeout_sec + 這個寬限就視為殭屍、reap 掉。
_STALE_RUNNING_GRACE_SEC = _env_int("RED_QUEUE_STALE_GRACE_SEC", 120, min_value=30, max_value=3600)

_QUEUE_FILE = os.path.join(STATE_DIR, "task_queue.json")
_DLQ_FILE = os.path.join(STATE_DIR, "task_queue_dlq.json")


# ────────────────────────────────────────────────────────────────────
# 允許 queue 的 tool 白名單（defense in depth）
# 原則：背景跑得慢、副作用可重試的、long-running ingest/batch
# 不允許：互動式（read_mac_clipboard 等）/ 寫 vault / persona 修改
# ────────────────────────────────────────────────────────────────────
_QUEUEABLE_TOOLS: frozenset[str] = frozenset({
    # Email ingest / lake 處理（最常打架的就是這幾個）
    "email_lake_rebuild",
    "ingest_internal_emails",
    "ingest_internal_emails_oneoff",
    "batch_extract_quotes_from_parquet",
    # Outbound batch 通訊（要重試的）
    "send_gmail",
    "reply_gmail",
    "send_briefing_email",
    "push_briefing_telegram",
    "telegram_push",
    # Quote / report 生成
    "generate_quote",
    "process_monthly_invoices",
    # AI 燒錢但需要批次跑的
    "generate_image",
    "edit_image",
    # Calendar 寫入
    "create_calendar_event",
    "update_calendar_event",
    # QC 批次
    "qc_batch_inspect",
})


# ────────────────────────────────────────────────────────────────────
# 預設 mutex group — 不用每次手動指定，自動歸類
# ────────────────────────────────────────────────────────────────────
_DEFAULT_MUTEX_GROUPS: dict[str, str] = {
    # data_lake_internal/ 寫入者全互斥
    "ingest_internal_emails": "data_lake_writer",
    "ingest_internal_emails_oneoff": "data_lake_writer",
    "email_lake_rebuild": "data_lake_writer",
    "batch_extract_quotes_from_parquet": "data_lake_writer",
    # 對外通訊有頻率敏感（避免 spam Gmail / Telegram API）
    "send_gmail": "outbound_email",
    "reply_gmail": "outbound_email",
    "send_briefing_email": "outbound_email",
    "telegram_push": "outbound_telegram",
    "push_briefing_telegram": "outbound_telegram",
    # 燒 GPU/API 配額
    "generate_image": "ai_burn",
    "edit_image": "ai_burn",
}


# ────────────────────────────────────────────────────────────────────
# 持久化 state — 雙層 lock：
#   - _state_lock (threading)：同 process 內 thread 互鎖（worker thread vs
#     主 thread vs API server thread）
#   - fcntl.flock on _QUEUE_LOCK_FILE：跨 process 互鎖（telegram daemon /
#     dispatcher daemon / queue_bootstrap drain 都會動 task_queue.json）
# 兩層都要才安全。光 threading.Lock 在多 daemon 同時跑時會 lost-update。
# ────────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_QUEUE_LOCK_FILE = _QUEUE_FILE + ".lock"
_worker_stop_event: threading.Event | None = None
_worker_thread: threading.Thread | None = None
_running_cancel_flags: dict[str, threading.Event] = {}  # task_id → cancel signal


@contextlib.contextmanager
def _queue_lock():
    """Context manager: 取得 in-process + cross-process 互斥。

    用法：
        with _queue_lock():
            data = _load_queue()
            data["tasks"].append(...)
            _save_queue(data)

    跨 process flock 失敗時 fallback to threading-only — 不擋住 daemon
    跑（畢竟 lock file 開不了通常是 fs 問題，硬擋會讓整個 queue 死）。
    """
    import fcntl
    _state_lock.acquire()
    lock_fd = None
    try:
        try:
            _ensure_dirs()
            lock_fd = open(_QUEUE_LOCK_FILE, "w")
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            # flock 失敗：log 但不 raise，繼續 in-process only
            print(f"[task_queue] cross-process lock 失敗（{e}）— fallback to threading-only")
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


def _ensure_dirs() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception:
        pass


def _db_queue_enabled() -> bool:
    try:
        from agent_core.operational_task_queue import enabled

        return enabled()
    except Exception:
        return False


def _db_queue():
    from agent_core import operational_task_queue

    return operational_task_queue


def _worker_id() -> str:
    return f"{os.uname().nodename}:{os.getpid()}:{threading.get_ident()}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_task_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(3)


def _load_queue() -> dict:
    _ensure_dirs()
    if not os.path.isfile(_QUEUE_FILE):
        return {"version": 1, "tasks": [], "mutex_holders": {}}
    try:
        with open(_QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("tasks", [])
        data.setdefault("mutex_holders", {})
        return data
    except Exception:
        return {"version": 1, "tasks": [], "mutex_holders": {}}


def _save_queue(data: dict) -> None:
    try:
        _atomic_write_text(_QUEUE_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _load_dlq() -> dict:
    _ensure_dirs()
    if not os.path.isfile(_DLQ_FILE):
        return {"version": 1, "tasks": []}
    try:
        with open(_DLQ_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("tasks", [])
        return data
    except Exception:
        return {"version": 1, "tasks": []}


def _save_dlq(data: dict) -> None:
    try:
        _atomic_write_text(_DLQ_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# Validation
# ────────────────────────────────────────────────────────────────────
_VALID_NAME_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_tool_name(name: str) -> tuple[bool, str]:
    if not isinstance(name, str) or not name:
        return False, "tool name 必須是非空字串"
    if not _VALID_NAME_RE.match(name):
        return False, f"tool name '{name}' 含非法字元（只允許 A-Z/a-z/0-9/_）"
    if name == "submit_task":
        return False, "不允許 queue submit_task 自身（防遞迴 self-bomb）"
    if name not in _QUEUEABLE_TOOLS:
        return False, (
            f"tool '{name}' 不在 _QUEUEABLE_TOOLS 白名單中。\n"
            f"   允許 queue 的 tool（{len(_QUEUEABLE_TOOLS)} 個）：\n"
            f"   {', '.join(sorted(_QUEUEABLE_TOOLS))}"
        )
    # 額外：不允許 queue LOCKED tool（即使白名單放了也擋）
    try:
        from agent_core.tool_tiers import get_tier, TIER_LOCKED
        if get_tier(name) == TIER_LOCKED:
            return False, f"tool '{name}' 是 LOCKED tier — queue 也禁止"
    except Exception:
        pass
    return True, ""


def _validate_mutex_group(group: str) -> tuple[bool, str]:
    if not group:
        return True, ""  # 空字串 = 不上鎖
    if not isinstance(group, str):
        return False, "mutex_group 必須是字串"
    if len(group) > 64:
        return False, "mutex_group 太長（>64）"
    if not _VALID_NAME_RE.match(group):
        return False, f"mutex_group '{group}' 含非法字元"
    return True, ""


# ────────────────────────────────────────────────────────────────────
# Public API — submit / cancel / status
# ────────────────────────────────────────────────────────────────────
def submit_task(tool: str, kwargs: Optional[dict] = None, *,
                priority: int = _DEFAULT_PRIORITY,
                timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
                max_retries: int = _DEFAULT_MAX_RETRIES,
                mutex_group: str = "") -> str:
    """🔴 把一個 tool 排進背景 queue，回傳 task_id。

    LLM 注意：此 tool 為 DANGEROUS — 大王要 +確認 + +雙確認 才會 enqueue。
    enqueued 之後 worker 會在 daemon channel 跑（LOCKED 仍會被拒）。

    Args:
        tool: tool 名稱（必須在 _QUEUEABLE_TOOLS 白名單中）
        kwargs: 傳給 tool 的參數 dict
        priority: 1=高 5=正常 9=低
        timeout_sec: 硬 timeout（預設 300s）
        max_retries: 失敗重試次數（預設 3，超過進 DLQ）
        mutex_group: 互斥鎖名稱（同 group 同時只一個跑）。
                     空字串 = 自動套用 _DEFAULT_MUTEX_GROUPS

    Returns:
        task_id（成功）/ 錯誤訊息（失敗）
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    ok, err = _validate_tool_name(tool)
    if not ok:
        return ToolResult.failure(err, error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False,
                                   suggested_fix="檢查 tool 名是否在 _QUEUEABLE_TOOLS 白名單")
    if not isinstance(kwargs, (dict, type(None))):
        return ToolResult.failure("kwargs 必須是 dict 或 None",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    kwargs = dict(kwargs or {})
    # mutex group 自動 fallback
    if not mutex_group:
        mutex_group = _DEFAULT_MUTEX_GROUPS.get(tool, "")
    ok, err = _validate_mutex_group(mutex_group)
    if not ok:
        return ToolResult.failure(err, error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    # 範圍檢查
    priority = max(1, min(9, int(priority)))
    timeout_sec = max(5, min(3600, int(timeout_sec)))   # 5s ~ 1h
    max_retries = max(0, min(10, int(max_retries)))

    task = {
        "id": _new_task_id(),
        "tool": tool,
        "kwargs": kwargs,
        "priority": priority,
        "state": STATE_PENDING,
        "submitted_at": _now_iso(),
        "started_at": None,
        "ended_at": None,
        "attempts": 0,
        "max_retries": max_retries,
        "timeout_sec": timeout_sec,
        "mutex_group": mutex_group,
        "next_run_at": _now_iso(),  # 立即可跑
        "last_error": None,
    }
    if _db_queue_enabled():
        res = _db_queue().enqueue_task(
            task,
            queue_max_size=_QUEUE_MAX_SIZE,
            stale_grace_sec=_STALE_RUNNING_GRACE_SEC,
            backoff_base_sec=_BACKOFF_BASE_SEC,
            dlq_retain_days=_DLQ_RETAIN_DAYS,
        )
        if not res.get("ok"):
            live = int(res.get("live") or 0)
            return ToolResult.failure(
                f"queue 已滿（{live} 個 pending/running ≥ {_QUEUE_MAX_SIZE} 上限）",
                error_code=ErrorCode.RATE_LIMITED,
                recoverable=True,
                suggested_fix="等現有 task 跑完再試，或 cancel_task 釋出空間",
            )
        return ToolResult.success(
            f"✅ enqueued: {task['id']}  tool={tool}  priority={priority}  mutex={mutex_group or '-'}",
            data={"task_id": task["id"], "tool": tool, "priority": priority,
                  "mutex_group": mutex_group, "backend": "postgres"},
            artifacts=[task["id"]],
        )
    with _queue_lock():
        data = _load_queue()
        # 先收殭屍 RUNNING（worker 死在 claim 後、寫回前留下的）再算上限。否則殭屍
        # 會一直算進 _QUEUE_MAX_SIZE 把新 submit 永久卡死 —— 而唯一清它們的
        # run_worker_once 可能正卡在長任務或根本沒在跑，遲遲輪不到。
        reaped = _reap_stale_running(data, datetime.now())
        # 限流：超過 _QUEUE_MAX_SIZE 拒絕（防 LLM 爆量 submit）
        live = [t for t in data["tasks"]
                if t["state"] in (STATE_PENDING, STATE_RUNNING)]
        if len(live) >= _QUEUE_MAX_SIZE:
            if reaped:
                _save_queue(data)  # 持久化剛剛的 reap，即使這次 submit 被拒
            return ToolResult.failure(
                f"queue 已滿（{len(live)} 個 pending/running ≥ {_QUEUE_MAX_SIZE} 上限）",
                error_code=ErrorCode.RATE_LIMITED,
                recoverable=True,
                suggested_fix="等現有 task 跑完再試，或 cancel_task 釋出空間",
            )
        data["tasks"].append(task)
        _save_queue(data)
    return ToolResult.success(
        f"✅ enqueued: {task['id']}  tool={tool}  priority={priority}  mutex={mutex_group or '-'}",
        data={"task_id": task["id"], "tool": tool, "priority": priority,
              "mutex_group": mutex_group},
        artifacts=[task["id"]],
    )


def cancel_task(task_id: str) -> str:
    """🟡 取消 pending task；running task 設 cancel flag（cooperative）。"""
    from agent_core.tool_result import ToolResult, ErrorCode
    if not isinstance(task_id, str) or not task_id:
        return ToolResult.failure("task_id 必須是非空字串",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    if _db_queue_enabled():
        res = _db_queue().cancel_task(task_id)
        if res.get("ok"):
            if res.get("previous_state") == STATE_PENDING:
                return ToolResult.success(
                    f"✅ pending task {task_id} 已取消",
                    data={"task_id": task_id, "previous_state": "pending",
                          "backend": "postgres"},
                )
            flag = _running_cancel_flags.get(task_id)
            if flag:
                flag.set()
            return ToolResult.success(
                f"⏳ running task {task_id} 已收到 cancel 信號 — "
                f"worker 會在完成當前執行後標記取消",
                data={"task_id": task_id, "previous_state": "running",
                      "backend": "postgres"},
                warnings=["cancel cooperative — 跨 worker 不保證即時中斷"],
            )
        if res.get("reason") == "not_found":
            return ToolResult.failure(
                f"找不到 task {task_id}",
                error_code=ErrorCode.NOT_FOUND,
                recoverable=False,
                suggested_fix="可能已完成移到 DLQ — 查 dead_letter_status()",
            )
        return ToolResult.failure(
            f"task {task_id} 已是 {res.get('state') or 'unknown'} 狀態，無法取消",
            error_code=ErrorCode.INVALID_INPUT,
            recoverable=False,
        )
    with _queue_lock():
        data = _load_queue()
        for t in data["tasks"]:
            if t["id"] == task_id:
                if t["state"] == STATE_PENDING:
                    t["state"] = STATE_CANCELLED
                    t["ended_at"] = _now_iso()
                    _save_queue(data)
                    return ToolResult.success(
                        f"✅ pending task {task_id} 已取消",
                        data={"task_id": task_id, "previous_state": "pending"},
                    )
                if t["state"] == STATE_RUNNING:
                    flag = _running_cancel_flags.get(task_id)
                    if flag:
                        flag.set()
                    return ToolResult.success(
                        f"⏳ running task {task_id} 已收到 cancel 信號 — "
                        f"thread 會在下個 cooperative check 退出（不保證即時）",
                        data={"task_id": task_id, "previous_state": "running"},
                        warnings=["cancel cooperative — 若 fn 不檢 flag 不會中斷"],
                    )
                return ToolResult.failure(
                    f"task {task_id} 已是 {t['state']} 狀態，無法取消",
                    error_code=ErrorCode.INVALID_INPUT,
                    recoverable=False,
                )
    return ToolResult.failure(
        f"找不到 task {task_id}",
        error_code=ErrorCode.NOT_FOUND,
        recoverable=False,
        suggested_fix="可能已完成移到 DLQ — 查 dead_letter_status()",
    )


def task_status(task_id: str = "") -> str:
    """🟢 查 task 狀態。空 task_id = 列所有 pending/running 摘要。"""
    if _db_queue_enabled():
        if task_id:
            task = _db_queue().get_task(task_id)
            if task:
                prefix = "💀 (in DLQ)\n" if task.get("state") == STATE_DEAD else ""
                return prefix + _format_task_detail(task)
            return f"❌ 找不到 task {task_id}"
        return list_queue_tasks()
    if task_id:
        with _queue_lock():
            data = _load_queue()
            for t in data["tasks"]:
                if t["id"] == task_id:
                    return _format_task_detail(t)
            # 也找 DLQ
            dlq = _load_dlq()
            for t in dlq["tasks"]:
                if t["id"] == task_id:
                    return "💀 (in DLQ)\n" + _format_task_detail(t)
        return f"❌ 找不到 task {task_id}"
    return list_queue_tasks()


def find_live_task(tool: str, mutex_group: str = "") -> dict[str, Any] | None:
    """Return an existing pending/running task for a tool, if any.

    This is intentionally read-only and returns a shallow copy so callers such
    as Telegram enqueue wrappers can de-dupe long-running jobs without mutating
    queue state or reaching into the queue file directly.
    """
    if not isinstance(tool, str) or not tool:
        return None
    if not mutex_group:
        mutex_group = _DEFAULT_MUTEX_GROUPS.get(tool, "")
    if _db_queue_enabled():
        task = _db_queue().find_live_task(tool, mutex_group)
        return dict(task) if task else None
    with _queue_lock():
        data = _load_queue()
        for task in data.get("tasks", []):
            if task.get("state") not in (STATE_PENDING, STATE_RUNNING):
                continue
            if task.get("tool") != tool:
                continue
            if mutex_group and task.get("mutex_group") != mutex_group:
                continue
            return dict(task)
    return None


def _format_queue_task_list(tasks: list[dict], state: str = "", limit: int = 30) -> str:
    if state:
        tasks = [t for t in tasks if t.get("state") == state]
    # 排序：state（running 優先）→ priority → submitted_at
    state_order = {STATE_RUNNING: 0, STATE_PENDING: 1, STATE_FAILED: 2,
                   STATE_DONE: 3, STATE_CANCELLED: 4}
    tasks.sort(key=lambda t: (state_order.get(t["state"], 9),
                               t["priority"],
                               t["submitted_at"]))
    out = [f"📋 task queue — 共 {len(tasks)} 個" + (f"（state={state}）" if state else "")]
    out.append("─" * 60)
    pending = sum(1 for t in tasks if t["state"] == STATE_PENDING)
    running = sum(1 for t in tasks if t["state"] == STATE_RUNNING)
    out.append(f"  pending={pending}  running={running}  其他={len(tasks)-pending-running}")
    out.append("")
    icons = {STATE_PENDING: "⏳", STATE_RUNNING: "🔄", STATE_DONE: "✅",
             STATE_FAILED: "❌", STATE_CANCELLED: "🚫"}
    for t in tasks[:limit]:
        ic = icons.get(t["state"], "?")
        when = (t.get("submitted_at") or "")[5:16]
        mutex = f"[{t['mutex_group']}]" if t.get("mutex_group") else ""
        out.append(f"  {ic} {t['id']}  pri={t['priority']}  "
                   f"{t['tool']:36s} {when} {mutex}")
        if t["state"] == STATE_FAILED and t.get("last_error"):
            out.append(f"      err: {t['last_error'][:80]}")
    if len(tasks) > limit:
        out.append(f"  ...（還有 {len(tasks)-limit} 個未顯示）")
    out.append("")
    out.append("💡 task_status('<id>') 看詳情；cancel_task('<id>') 取消")
    return "\n".join(out)


def list_queue_tasks(state: str = "", limit: int = 30) -> str:
    """🟢 列出 queue 中的 task（state 篩 pending/running/done/failed/cancelled）。

    （命名跟 task_memory.list_tasks 區分：queue 是「背景排程的任務」，
    task_memory 是「大王交代的承諾」；兩個是不同概念。）"""
    if _db_queue_enabled():
        tasks = _db_queue().list_tasks(state="", limit=max(limit, 200))
        return _format_queue_task_list(tasks, state=state, limit=limit)
    with _queue_lock():
        data = _load_queue()
    return _format_queue_task_list(data["tasks"], state=state, limit=limit)


def _format_task_detail(t: dict) -> str:
    out = [
        f"📋 task {t['id']}",
        f"  tool       : {t['tool']}",
        f"  state      : {t['state']}",
        f"  priority   : {t['priority']}",
        f"  attempts   : {t.get('attempts', 0)} / {t.get('max_retries', 0)} retries",
        f"  timeout    : {t.get('timeout_sec', '?')}s",
        f"  mutex      : {t.get('mutex_group') or '-'}",
        f"  submitted  : {t.get('submitted_at')}",
        f"  started    : {t.get('started_at') or '—'}",
        f"  ended      : {t.get('ended_at') or '—'}",
        f"  next_run   : {t.get('next_run_at') or '—'}",
    ]
    # kwargs 過 redact
    if t.get("kwargs"):
        try:
            from agent_core.log_redact import redact_log_line
            kwargs_str = redact_log_line(json.dumps(t["kwargs"], ensure_ascii=False))
        except Exception:
            kwargs_str = json.dumps(t["kwargs"], ensure_ascii=False)
        out.append(f"  kwargs     : {kwargs_str[:200]}")
    if t.get("last_error"):
        try:
            from agent_core.log_redact import redact_log_line
            err = redact_log_line(t["last_error"])
        except Exception:
            err = t["last_error"]
        out.append(f"  last_error : {err[:200]}")
    return "\n".join(out)


def _format_dead_letter_tasks(tasks: list[dict], limit: int = 20) -> str:
    tasks.sort(key=lambda t: t.get("ended_at") or "", reverse=True)
    if not tasks:
        return "✅ DLQ 空 — 沒有任務需要處置"
    out = [f"💀 dead-letter queue — {len(tasks)} 個任務"]
    out.append("─" * 60)
    try:
        from agent_core.log_redact import redact_log_line
    except Exception:
        redact_log_line = lambda x: x  # noqa
    for t in tasks[:limit]:
        when = (t.get("ended_at") or "")[5:16]
        out.append(f"  💀 {t['id']}  {t['tool']:36s} 失敗於 {when}")
        err = redact_log_line(t.get("last_error") or "")
        out.append(f"      err: {err[:90]}")
        out.append(f"      attempts={t.get('attempts')}/{t.get('max_retries')}")
    if len(tasks) > limit:
        out.append(f"  ...（還有 {len(tasks)-limit} 個未顯示）")
    out.append("")
    out.append("💡 大王要重跑：requeue_dead_letter('<id>')")
    return "\n".join(out)


def dead_letter_status(limit: int = 20) -> str:
    """🟢 看 DLQ — 已 retry 用完進不去 queue 的 task，等大王手動處置。"""
    if _db_queue_enabled():
        return _format_dead_letter_tasks(_db_queue().list_dead_letters(limit), limit=limit)
    with _queue_lock():
        dlq = _load_dlq()
    return _format_dead_letter_tasks(dlq["tasks"], limit=limit)


def requeue_dead_letter(task_id: str) -> str:
    """🔴 把 DLQ 中某個 task 移回 queue 重跑。

    DANGEROUS — 重跑失敗任務可能有副作用（重複寄信等），用前確認 last_error
    已修好。
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    if not isinstance(task_id, str) or not task_id:
        return ToolResult.failure("task_id 必須是非空字串",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    if _db_queue_enabled():
        res = _db_queue().requeue_dead_letter(task_id, _new_task_id())
        if not res.get("ok"):
            return ToolResult.failure(
                f"DLQ 中找不到 task {task_id}",
                error_code=ErrorCode.NOT_FOUND, recoverable=False,
            )
        task = res["task"]
        return ToolResult.success(
            f"✅ requeued — 新 task_id = {task['id']}",
            data={"new_task_id": task["id"], "tool": task["tool"],
                  "backend": "postgres"},
            artifacts=[task["id"]],
        )
    with _queue_lock():
        dlq = _load_dlq()
        target = None
        for i, t in enumerate(dlq["tasks"]):
            if t["id"] == task_id:
                target = dlq["tasks"].pop(i)
                break
        if target is None:
            return ToolResult.failure(
                f"DLQ 中找不到 task {task_id}",
                error_code=ErrorCode.NOT_FOUND, recoverable=False,
            )
        _save_dlq(dlq)
        # 重置回 pending 狀態
        target["state"] = STATE_PENDING
        target["attempts"] = 0
        target["last_error"] = None
        target["next_run_at"] = _now_iso()
        target["started_at"] = None
        target["ended_at"] = None
        target["id"] = _new_task_id()  # 換新 id 防混淆
        data = _load_queue()
        data["tasks"].append(target)
        _save_queue(data)
    return ToolResult.success(
        f"✅ requeued — 新 task_id = {target['id']}",
        data={"new_task_id": target["id"], "tool": target["tool"]},
        artifacts=[target["id"]],
    )


# ────────────────────────────────────────────────────────────────────
# Worker — 跑一個 task / loop
# ────────────────────────────────────────────────────────────────────
def _resolve_tool(tool_name: str) -> Callable | None:
    """從 tools_list 找 tool function（lazy import）。"""
    try:
        from agent_core.tool_registry import tools_list
    except Exception:
        return None
    for t in tools_list:
        if getattr(t, "__name__", "") == tool_name:
            return t
    return None


def _run_with_timeout(fn: Callable, kwargs: dict, timeout_sec: int,
                      external_cancel: threading.Event) -> tuple[bool, Any, str]:
    """跑 fn(**kwargs)，超過 timeout 視為失敗（會走 retry/DLQ）。

    external_cancel 由 cancel_task() 設定 — 用來區分「timeout 失敗」 vs
    「大王主動取消」兩種情境。timeout 不會設 external_cancel；timeout 後
    thread 變 daemon，下次 cooperative check 會看 external_cancel 自己退出。

    Returns:
        (success, result, error_msg)
    """
    result_box: list[Any] = [None]
    error_box: list[str] = [""]
    done_evt = threading.Event()

    def _runner():
        try:
            result_box[0] = fn(**kwargs)
        except Exception as e:
            error_box[0] = f"{type(e).__name__}: {e}"
        finally:
            done_evt.set()

    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    finished = done_evt.wait(timeout=timeout_sec)
    if not finished:
        # timeout — 不動 external_cancel，讓 worker 走 failure/retry path
        return False, None, f"timeout after {timeout_sec}s"
    if external_cancel.is_set():
        # 大王在這 task running 過程中按了 cancel — 視為 cancelled
        return False, None, "cancelled by user"
    if error_box[0]:
        return False, None, error_box[0]
    return True, result_box[0], ""


def _execute_task_snapshot(
    task_snapshot: dict,
    cancel_flag: threading.Event,
) -> tuple[bool, Any, str]:
    """Execute a claimed task outside any queue-state transaction."""
    fn = _resolve_tool(task_snapshot["tool"])
    fn_module = getattr(fn, "__module__", "") if fn is not None else ""
    if fn is not None and not fn_module.startswith("agent_core."):
        try:
            from agent_core.tg_auth import wrap_sensitive_tool, is_sensitive
            if is_sensitive(task_snapshot["tool"]):
                fn = wrap_sensitive_tool(fn, get_chat_id=lambda: "",
                                          channel="daemon")
        except Exception:
            pass
        return _run_with_timeout(
            fn, task_snapshot["kwargs"], task_snapshot["timeout_sec"], cancel_flag,
        )
    try:
        from agent_core.tool_runner import call_tool
        result = call_tool(
            task_snapshot["tool"],
            task_snapshot["kwargs"],
            context={
                "caller": "task_queue",
                "task_id": task_snapshot["id"],
                "worker_channel": "daemon",
            },
            timeout_sec=task_snapshot["timeout_sec"],
            prefer_rpc=False,
            cancel_event=cancel_flag,
        )
        success = bool(getattr(result, "ok", True))
        err = "" if success else str(result)
        return success, result, err
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def _backoff_seconds(attempt: int) -> int:
    """指數 backoff：30s, 60s, 120s, 240s..."""
    return _BACKOFF_BASE_SEC * (2 ** max(0, attempt - 1))


def _move_to_dlq(task: dict) -> None:
    """把 failed task 移到 DLQ。呼叫端必須持有 _state_lock。"""
    dlq = _load_dlq()
    task["state"] = STATE_DEAD
    task["moved_to_dlq_at"] = _now_iso()
    dlq["tasks"].append(task)
    # GC：只留最近 _DLQ_RETAIN_DAYS 天
    cutoff = (datetime.now() - timedelta(days=_DLQ_RETAIN_DAYS)).isoformat()
    dlq["tasks"] = [t for t in dlq["tasks"]
                    if (t.get("moved_to_dlq_at") or t.get("ended_at") or "") >= cutoff]
    _save_dlq(dlq)


def _gc_done_tasks(data: dict) -> None:
    """把 7 天前 done/cancelled task 清掉（避免 queue file 無限長）。"""
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    data["tasks"] = [t for t in data["tasks"]
                     if t["state"] in (STATE_PENDING, STATE_RUNNING, STATE_FAILED)
                     or (t.get("ended_at") or "") >= cutoff]


def _reap_stale_running(data: dict, now: datetime) -> int:
    """把卡在 RUNNING 超過 deadline 的殭屍 task 收掉。

    worker 程序在 claim（state=RUNNING、started_at 已寫）之後、寫回結果之前
    死掉（watchdog os._exit、SIGTERM、_TG_RESTART self-restart…），task 會永遠
    停在 RUNNING：busy_groups 只清「holder 不是 running」的鎖，所以這種殭屍反而
    被當成合法佔用、整個 mutex group 永遠跑不動；_gc_done_tasks 也不清 RUNNING；
    submit_task 還把它算進 _QUEUE_MAX_SIZE。

    判定：now > started_at + timeout_sec + grace（活著的 worker 會在 timeout 內
    寫回，所以超過這條線仍 RUNNING = 程序已死）。retry 沒用完→重排 PENDING+backoff、
    用完→DLQ；兩者都釋放它佔的 mutex。呼叫端須持有 _queue_lock。回傳 reap 數。
    """
    holders = data.setdefault("mutex_holders", {})
    reaped = 0
    dlq_ids: list[str] = []
    for t in data["tasks"]:
        if t.get("state") != STATE_RUNNING:
            continue
        started = t.get("started_at")
        if not started:
            continue  # 沒 started_at 無從判斷，保守不動
        try:
            started_dt = datetime.fromisoformat(started)
        except (ValueError, TypeError):
            continue
        deadline = started_dt + timedelta(
            seconds=int(t.get("timeout_sec", 60)) + _STALE_RUNNING_GRACE_SEC
        )
        if now <= deadline:
            continue  # 還在合理執行時間內
        # ── 殭屍 → reap ──
        grp = t.get("mutex_group")
        if grp and holders.get(grp) == t["id"]:
            holders.pop(grp, None)
        _running_cancel_flags.pop(t["id"], None)
        if int(t.get("attempts", 0)) >= int(t.get("max_retries", 0)) + 1:
            t["ended_at"] = now.isoformat(timespec="seconds")
            t["last_error"] = "worker died while RUNNING (reaped stale) — retries exhausted"
            _move_to_dlq(t)
            dlq_ids.append(t["id"])
        else:
            wait = _backoff_seconds(int(t.get("attempts", 0)))
            t["state"] = STATE_PENDING
            t["started_at"] = None
            t["next_run_at"] = (now + timedelta(seconds=wait)).isoformat(timespec="seconds")
            t["last_error"] = "worker died while RUNNING (reaped stale) — re-queued"
        reaped += 1
    if dlq_ids:
        dead = set(dlq_ids)
        data["tasks"] = [t for t in data["tasks"] if t["id"] not in dead]
    return reaped


def _run_worker_once_db() -> int:
    dbq = _db_queue()
    dbq.reap_stale_running(
        stale_grace_sec=_STALE_RUNNING_GRACE_SEC,
        backoff_base_sec=_BACKOFF_BASE_SEC,
        dlq_retain_days=_DLQ_RETAIN_DAYS,
    )
    task_snapshot = dbq.claim_task(_worker_id())
    if not task_snapshot:
        return 0
    cancel_flag = threading.Event()
    _running_cancel_flags[task_snapshot["id"]] = cancel_flag
    try:
        success, result, err = _execute_task_snapshot(task_snapshot, cancel_flag)
        result_preview = ""
        try:
            from agent_core.log_redact import redact_log_line

            result_preview = redact_log_line(str(result or ""))[:500]
        except Exception:
            result_preview = str(result or "")[:500]
        dbq.complete_task(
            task_snapshot["id"],
            success=success,
            error=err,
            result_preview=result_preview,
            backoff_base_sec=_BACKOFF_BASE_SEC,
            dlq_retain_days=_DLQ_RETAIN_DAYS,
        )
    finally:
        _running_cancel_flags.pop(task_snapshot["id"], None)
    return 1


def run_worker_once() -> int:
    """跑一個可跑的 task（最高優先 + mutex 沒卡 + next_run_at 已到）。

    Returns:
        1：跑了一個（不論成敗）
        0：沒可跑的（queue 空 / 全卡 mutex / 全等 backoff）
    """
    if _db_queue_enabled():
        return _run_worker_once_db()
    now_iso = _now_iso()
    with _queue_lock():
        data = _load_queue()
        # 先收殭屍 RUNNING（程序中途死掉留下的），釋放它們卡住的 mutex group，
        # 否則下面 busy_groups 會把殭屍當合法佔用、該 group 永遠跑不動。
        _reap_stale_running(data, datetime.now())
        # mutex_holders 是 group→task_id；找出哪些 group 已被佔
        busy_groups = {g for g, tid in data["mutex_holders"].items()
                       # 確認 holder 還真的是 running（防 dispatcher crash 留下殭屍鎖）
                       if any(t["id"] == tid and t["state"] == STATE_RUNNING
                              for t in data["tasks"])}
        # 沒對應 running task 的鎖直接清掉
        for g in list(data["mutex_holders"].keys()):
            if g not in busy_groups:
                data["mutex_holders"].pop(g, None)

        # 找 candidate：state=pending、next_run_at <= now、mutex_group 不忙
        candidates = [t for t in data["tasks"]
                      if t["state"] == STATE_PENDING
                      and (t.get("next_run_at") or "") <= now_iso
                      and (not t.get("mutex_group") or t["mutex_group"] not in busy_groups)]
        if not candidates:
            _save_queue(data)
            return 0
        # 優先：低 priority 數字優先 → 早 submit 先
        candidates.sort(key=lambda t: (t["priority"], t["submitted_at"]))
        task = candidates[0]
        # claim
        task["state"] = STATE_RUNNING
        task["started_at"] = now_iso
        task["attempts"] = task.get("attempts", 0) + 1
        if task.get("mutex_group"):
            data["mutex_holders"][task["mutex_group"]] = task["id"]
        # cancel flag for this run
        cancel_flag = threading.Event()
        _running_cancel_flags[task["id"]] = cancel_flag
        _save_queue(data)
        task_snapshot = dict(task)

    # 真執行（在 lock 外，不擋其他 read/write）。Real tools now use a fresh
    # subprocess worker so timeouts can kill the whole execution tree and code
    # edits are picked up per call. Unit tests often monkeypatch tools_list with
    # local fake functions; those cannot be imported by a subprocess, so keep a
    # narrow in-process compatibility path for non-agent_core callables.
    success, result, err = _execute_task_snapshot(task_snapshot, cancel_flag)

    # 寫回結果
    with _queue_lock():
        data = _load_queue()
        # 釋放 mutex
        if task_snapshot.get("mutex_group"):
            data["mutex_holders"].pop(task_snapshot["mutex_group"], None)
        # 找回 task
        task = next((t for t in data["tasks"]
                     if t["id"] == task_snapshot["id"]), None)
        if task is None:
            _save_queue(data)
            _running_cancel_flags.pop(task_snapshot["id"], None)
            return 1
        task["ended_at"] = _now_iso()
        # 區分：cancelled by user（external flag）vs failed by timeout / exception
        if cancel_flag.is_set() and "cancelled by user" in (err or ""):
            task["state"] = STATE_CANCELLED
            task["last_error"] = err
        elif success:
            task["state"] = STATE_DONE
            task["last_error"] = None
        else:
            task["last_error"] = err
            if task["attempts"] >= task["max_retries"] + 1:
                # 用完 retry → DLQ
                _move_to_dlq(task)
                # 從 active queue 移除
                data["tasks"] = [t for t in data["tasks"] if t["id"] != task["id"]]
            else:
                # 排下一次 retry
                wait = _backoff_seconds(task["attempts"])
                task["state"] = STATE_PENDING
                task["next_run_at"] = (datetime.now()
                                       + timedelta(seconds=wait)).isoformat(timespec="seconds")
        _gc_done_tasks(data)
        _save_queue(data)
        _running_cancel_flags.pop(task_snapshot["id"], None)
    return 1


def _worker_loop(stop_event: threading.Event) -> None:
    """背景 thread：不停跑 run_worker_once，空閒時 sleep。"""
    while not stop_event.is_set():
        try:
            ran = run_worker_once()
        except Exception:
            ran = 0
        if not ran:
            stop_event.wait(_IDLE_POLL_SEC)


def start_worker_thread() -> bool:
    """啟動背景 worker thread。daemon_dispatcher.py 啟動時應呼叫此。

    Returns:
        True：thread 已啟動 / 已在跑
        False：啟動失敗
    """
    global _worker_stop_event, _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return True
    _worker_stop_event = threading.Event()
    _worker_thread = threading.Thread(
        target=_worker_loop, args=(_worker_stop_event,),
        daemon=True, name="task_queue_worker",
    )
    _worker_thread.start()
    return True


def stop_worker_thread(timeout: float = 5.0) -> bool:
    """停 worker（join 至多 timeout 秒）。daemon shutdown hook 呼叫。"""
    global _worker_thread, _worker_stop_event
    if _worker_thread is None or _worker_stop_event is None:
        return True
    _worker_stop_event.set()
    _worker_thread.join(timeout=timeout)
    alive = _worker_thread.is_alive()
    if not alive:
        _worker_thread = None
        _worker_stop_event = None
    return not alive


# ────────────────────────────────────────────────────────────────────
# Dashboard helper
# ────────────────────────────────────────────────────────────────────
def queue_summary() -> dict:
    """給 dashboard 的精簡摘要。"""
    if _db_queue_enabled():
        return _db_queue().queue_summary()
    with _queue_lock():
        data = _load_queue()
        dlq = _load_dlq()
    counts: dict[str, int] = {}
    for t in data["tasks"]:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return {
        "pending": counts.get(STATE_PENDING, 0),
        "running": counts.get(STATE_RUNNING, 0),
        "done": counts.get(STATE_DONE, 0),
        "failed": counts.get(STATE_FAILED, 0),
        "cancelled": counts.get(STATE_CANCELLED, 0),
        "dlq": len(dlq["tasks"]),
        "mutex_holders": dict(data.get("mutex_holders", {})),
    }
