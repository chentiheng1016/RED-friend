"""Scheduled task management for the daemon dispatcher.

Stores task definitions
in daemon_tasks.json at the project root. agent_daemon.py reads this file
every 5 minutes and runs due tasks.

Concurrency model:
  - User-facing ops (add / remove / run_now) use update_daemon_tasks(mutate_fn),
    which holds an fcntl lock for the whole read-modify-write so two concurrent
    callers can't lost-update each other.
  - The dispatcher loads tasks once, runs them over potentially several minutes
    (Gemini calls), and at the end calls save_dispatcher_run() — which re-reads
    the current on-disk state under the same lock and merges only the runtime
    fields (last_run_at / run_count / dedup_hashes / last_error / next_force_run).
    Tasks the user added during the run are preserved; tasks the user removed
    don't get resurrected.

Mirrors the locking pattern from agent_core.daemon_helpers.update_state().
"""
import fcntl
import os
import re
import json
from datetime import datetime
from typing import Any, Callable

from agent_core.logging_and_paths import (
    _SCRIPT_DIR,
    logger,
    _atomic_write_text,
)

DAEMON_TASKS_FILE = os.path.join(_SCRIPT_DIR, "daemon_tasks.json")
DAEMON_TASKS_LOCK = DAEMON_TASKS_FILE + ".lock"

# Fields the dispatcher is allowed to overwrite during merge-save. Everything
# else (prompt, interval_minutes, enabled, etc.) is user-editable and must not
# be clobbered by a stale dispatcher view.
_DISPATCHER_RUNTIME_FIELDS = (
    "last_error",
    "last_run_at",
    "run_count",
    "dedup_hashes",
)


def _load_daemon_tasks() -> dict:
    """Unlocked snapshot read. Safe for read-only callers (dashboards, status
    pages). For read-modify-write, use update_daemon_tasks() instead."""
    if not os.path.exists(DAEMON_TASKS_FILE):
        return {"version": 1, "tasks": []}
    try:
        with open(DAEMON_TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "tasks" not in data:
            data["tasks"] = []
        return data
    except Exception as e:
        logger.warning("daemon_tasks.json 讀取失敗（%s），視為空", e)
        return {"version": 1, "tasks": []}


def _save_daemon_tasks(data: dict) -> bool:
    """⚠️ Unlocked atomic write. Used by tests and as a low-level primitive.
    Callers doing read-modify-write MUST use update_daemon_tasks() instead —
    plain save races with concurrent mutators (e.g. dispatcher + user add)."""
    try:
        _atomic_write_text(DAEMON_TASKS_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception as e:
        logger.warning("daemon_tasks.json 寫入失敗（%s）", e)
        return False


def update_daemon_tasks(mutate_fn: Callable[[dict], Any]) -> dict:
    """Read-modify-write daemon_tasks.json under fcntl lock.

    mutate_fn(data) may mutate `data` in place. Its return value is ignored;
    side-channel info (e.g. "did this add succeed?") should come back via
    closure variables (nonlocal / list/dict captures).

    Lock-fail fallback: if we can't open or acquire the lock file, return a
    plain snapshot read so caller doesn't get {} and accidentally erase state.
    Same semantics as agent_core.daemon_helpers.update_state().
    """
    lock_fd = None
    try:
        lock_fd = open(DAEMON_TASKS_LOCK, "w")
    except Exception as e:
        logger.warning("[tasks lock] 開鎖檔失敗（%s），fallback 純讀取", e)
        return _load_daemon_tasks()
    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            logger.warning("[tasks lock] 拿鎖失敗（%s），fallback 純讀取", e)
            return _load_daemon_tasks()
        try:
            data = _load_daemon_tasks()
            mutate_fn(data)
            try:
                _atomic_write_text(
                    DAEMON_TASKS_FILE,
                    json.dumps(data, ensure_ascii=False, indent=2),
                )
            except Exception as e:
                # Write failed but in-memory state is mutated. Return the
                # mutated state so caller's response is consistent — disk
                # will catch up on next successful write.
                logger.warning("[tasks lock] 寫入失敗（state 留在記憶體）：%s", e)
            return data
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        try:
            lock_fd.close()
        except Exception:
            pass


def save_dispatcher_run(dispatcher_data: dict) -> bool:
    """Merge dispatcher's mutations onto current on-disk state under lock.

    Called at end of a dispatcher cycle. dispatcher_data is what the dispatcher
    loaded at cycle start, with runtime fields mutated for tasks that ran. We
    re-read the current file under lock and apply only the runtime field deltas
    to tasks that still exist by name — so user adds during the cycle survive
    and user removes don't get resurrected.
    """
    d_by_name = {t.get("name"): t for t in (dispatcher_data.get("tasks") or [])}

    def merge(current: dict) -> None:
        for current_task in (current.get("tasks") or []):
            d_task = d_by_name.get(current_task.get("name"))
            if not d_task:
                continue
            for field in _DISPATCHER_RUNTIME_FIELDS:
                if field in d_task:
                    current_task[field] = d_task[field]
            # next_force_run is set by run_scheduled_task_now and cleared by
            # mark_dispatcher_task_succeeded (via pop). If dispatcher's view
            # doesn't have it → dispatcher cleared it → reflect on disk.
            if "next_force_run" not in d_task:
                current_task.pop("next_force_run", None)

    try:
        update_daemon_tasks(merge)
        return True
    except Exception as e:
        logger.warning("daemon_tasks.json 合併寫入失敗（%s）", e)
        return False


def add_scheduled_task(name: str, prompt: str, interval_minutes: int = 120,
                        start_hour: int = 9, end_hour: int = 22,
                        notify_emails: str = ""):
    """建立一個背景排程任務（由 daemon dispatcher 每 5 分鐘掃描、到期就跑）。
    name: 短名稱（只能英數/底線/短槓，用來辨識與刪除）。
    prompt: 要給背景小紅的完整指示，越具體越好（包含：目標、過濾條件、通知規則）。
    interval_minutes: 多久跑一次（預設 120 分 = 2 小時）。最小 15 分。
    start_hour / end_hour: 只在這個小時範圍內觸發（預設 09-22，避免半夜吵）。
    notify_emails: 選填，逗號分隔的 email 清單。設定後改走「每個地址各自收到
      自己寄給自己的一封信」（網域委派冒充該地址寄信），取代預設的單一 owner
      email 通知；清單裡每個地址都要各自開通網域委派 gmail.send 才能真的收到。
    排定後，背景小紅會用「只能讀、不能動手」的安全工具集執行，有新變化才寄 Gmail 通知。"""
    clean_name = re.sub(r"[^A-Za-z0-9_\-]", "", (name or "").strip())
    if not clean_name:
        return "錯誤：name 不能空，且僅允許英數 / 底線 / 短槓。"
    if not (prompt or "").strip():
        return "錯誤：prompt 不能空，請告訴小紅這個任務具體要做什麼。"
    # M7-3 round 7：prompt 直接灌進 cron 任務 = 一次 +確認 換永久 attacker-driven
    # pipeline（每 N 分鐘跑一次，dispatcher 結果可寄信 = exfil 通道）。把
    # injection 字樣 redact 後再存；若 prompt 主要是 injection 直接拒。
    try:
        from agent_core.prompt_injection import sanitize_untrusted_text
        sanitized_prompt = sanitize_untrusted_text(prompt.strip())
        token = "[REDACTED-INJECTION-ATTEMPT]"
        if (sanitized_prompt.count(token) >= 1 and
                len(sanitized_prompt.replace(token, "").strip()) < 10):
            return ("❌ 拒絕排程此 prompt — 內容主要是 prompt-injection 嫌疑。\n"
                    "   若是真實業務 prompt 被誤判，請改寫描述後再試。")
        prompt_for_storage = sanitized_prompt
    except Exception:
        prompt_for_storage = prompt.strip()
    interval_minutes = max(15, min(int(interval_minutes), 1440))
    start_hour = max(0, min(int(start_hour), 23))
    end_hour = max(1, min(int(end_hour), 24))
    if end_hour <= start_hour:
        return "錯誤：end_hour 必須大於 start_hour。"

    notify_emails_list = [
        tok.strip() for tok in re.split(r"[,;\s、，；]+", (notify_emails or "").strip()) if tok.strip()
    ]
    bad_emails = [e for e in notify_emails_list if "@" not in e]
    if bad_emails:
        return f"錯誤：notify_emails 裡有格式不像 email 的項目：{', '.join(bad_emails)}"

    # Smuggle "already exists" out of the locked closure via a list (Python's
    # nonlocal would also work but lists are simpler for early-return signals).
    dup_err = [None]

    def add(data: dict) -> None:
        for t in data["tasks"]:
            if t.get("name") == clean_name:
                dup_err[0] = (f"錯誤：已存在同名任務「{clean_name}」，"
                              "請先 remove_scheduled_task 或換名。")
                return
        data["tasks"].append({
            "name": clean_name,
            "prompt": prompt_for_storage,
            "interval_minutes": interval_minutes,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "enabled": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "last_run_at": None,
            "run_count": 0,
            "dedup_hashes": [],
            "last_error": None,
            "notify_emails": notify_emails_list,
        })

    update_daemon_tasks(add)
    if dup_err[0]:
        return dup_err[0]
    notify_line = (
        f"   通知：{', '.join(notify_emails_list)}（各自收到自己寄給自己的信）\n"
        if notify_emails_list else ""
    )
    return (f"✅ 已建立排程：{clean_name}\n"
            f"   間隔：每 {interval_minutes} 分鐘\n"
            f"   時段：{start_hour:02d}:00-{end_hour:02d}:00\n"
            f"{notify_line}"
            f"   （背景 dispatcher 每 5 分鐘檢查一次；到時間會自動觸發）")


def list_scheduled_tasks():
    """列出所有已建立的排程任務（不論 enabled）。"""
    data = _load_daemon_tasks()
    tasks = data.get("tasks") or []
    if not tasks:
        return "目前沒有排程任務。"
    lines = [f"共 {len(tasks)} 個排程任務："]
    for t in tasks:
        enabled = "🟢" if t.get("enabled", True) else "⭕"
        name = t.get("name", "?")
        interval = t.get("interval_minutes", "?")
        sh = t.get("start_hour", 0)
        eh = t.get("end_hour", 24)
        run_cnt = t.get("run_count", 0)
        last = t.get("last_run_at") or "（未跑過）"
        err = t.get("last_error")
        err_line = f"\n    ⚠️ 最近錯誤：{err}" if err else ""
        wd = task_weekdays(t)
        wd_str = ("週" + "".join("一二三四五六日"[d - 1] for d in wd) + " ") if wd else ""
        prompt_snip = (t.get("prompt", "")[:80] + "…") if len(t.get("prompt", "")) > 80 else t.get("prompt", "")
        lines.append(
            f"{enabled} {name}  {wd_str}每 {interval} 分 / {sh:02d}-{eh:02d}:00 / 跑過 {run_cnt} 次 / 上次 {last}\n"
            f"    prompt: {prompt_snip}{err_line}"
        )
    return "\n".join(lines)


def remove_scheduled_task(name: str):
    """刪除指定名稱的排程任務。"""
    clean = (name or "").strip()
    if not clean:
        return "錯誤：請提供任務名稱。"

    removed = [False]

    def remove(data: dict) -> None:
        before = len(data.get("tasks") or [])
        data["tasks"] = [t for t in (data.get("tasks") or []) if t.get("name") != clean]
        removed[0] = len(data["tasks"]) < before

    update_daemon_tasks(remove)
    if not removed[0]:
        return f"找不到名為「{clean}」的任務（可用 list_scheduled_tasks 查）。"
    return f"已刪除排程「{clean}」。"


def run_scheduled_task_now(name: str):
    """立刻執行一次指定排程（不等排程時間）。只會把 last_run_at 提前、不改 dedup 紀錄。
    注意：這個呼叫本身不執行 prompt 內容，而是標記下次 dispatcher 掃描時立即跑。
    dispatcher 5 分鐘內會掃到。"""
    clean = (name or "").strip()
    found = [False]

    def mark(data: dict) -> None:
        for t in data.get("tasks") or []:
            if t.get("name") == clean:
                t["next_force_run"] = True
                t["enabled"] = True
                found[0] = True
                return

    update_daemon_tasks(mark)
    if found[0]:
        return f"已標記「{clean}」下次 dispatcher 掃描（最慢 5 分鐘內）立刻觸發。"
    return f"找不到「{clean}」。"


# ────────────────────────────────────────────────────────────────────
# 排程任務健康判讀（給 dashboard / dashboard_alerts 共用）
# ────────────────────────────────────────────────────────────────────
# 為什麼需要這個（2026-08-06）：dispatcher 執行失敗時只把 last_error 寫回
# daemon_tasks.json 就結束 —— 全 repo **沒有任何東西讀它**。red-status 的排程區
# 更誤導：那個 ✅ 是 `enabled` 旗標、不是健康狀態，所以一支連續失敗一週的任務
# 顯示得跟正常的一模一樣。這批任務有一半會直接寄信給同事（採購晨報、生產回報、
# 倉庫通知），真的壞掉時第一個發現的會是收件人說「我沒收到信」。
#
# 純函式、只吃 dict + now，決定性可單元測試（tier 判讀不進這裡）。

# 「超過預期間隔幾倍算停擺」。2 倍＋緩衝：允許 dispatcher 掃描抖動與單次跳過，
# 但連兩個週期沒動就該講話。
_STALL_FACTOR = 2.0
_STALL_GRACE_MIN = 30.0


def task_weekdays(task: dict) -> tuple[int, ...]:
    """任務允許觸發的星期（ISO：1=週一 … 7=週日），排序去重後回傳。

    沒設 / 空 / 整欄不是 list / 值全是垃圾 → 回空 tuple ＝「每天都可跑」，
    跟這個欄位出現之前的行為 byte-identical。單一垃圾值只丟掉那一個，不整欄
    作廢 —— 設定檔手改打錯一個數字不該讓任務默默變成每天跑。

    消費端有兩個：dispatcher 的 should_run_task（要不要今天觸發）與下面的
    expected_gap_minutes（停擺門檻）。兩邊必須用同一份解析，否則「週一才跑」
    的任務會被按日排程的門檻天天誤報 stalled。
    """
    raw = task.get("weekdays")
    if not isinstance(raw, list):
        return ()
    days: set[int] = set()
    for v in raw:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= iv <= 7:
            days.add(iv)
    return tuple(sorted(days))


def _max_weekday_gap_days(days: tuple[int, ...]) -> int:
    """相鄰兩個排定星期之間最長隔幾天（循環一週算）。每天可跑 → 1。"""
    if not days or len(days) >= 7:
        return 1
    return max((days[(i + 1) % len(days)] - d) % 7 or 7 for i, d in enumerate(days))


def expected_gap_minutes(task: dict) -> float:
    """兩次執行之間**最長合理間隔**（分鐘）。

    任務只在 start_hour–end_hour 的時段內會被觸發，所以「每 60 分鐘一次」配上
    09–10 的視窗，實際節奏是一天一次而不是一小時一次 —— 用 interval 當停擺門檻
    會天天誤報。這裡把視窗外的等待時間算進去。

    設了 weekdays 的任務同理：「每週一 06–07」的節奏是一週一次，最壞等待要跨
    到下一個排定日，不算進去就是每週固定誤報 stalled。
    """
    try:
        interval = float(int(task.get("interval_minutes", 60) or 60))
    except (TypeError, ValueError):
        interval = 60.0
    try:
        start = int(task.get("start_hour", 0) or 0)
        end = int(task.get("end_hour", 24) or 24)
    except (TypeError, ValueError):
        start, end = 0, 24
    gap_days = _max_weekday_gap_days(task_weekdays(task))
    window_h = end - start
    if window_h <= 0 or window_h >= 24:
        # 全天可跑 → 每天跑就是 interval；週排程再加上跨到下個排定日的整天數。
        return (gap_days - 1) * 24 * 60.0 + interval
    # 只在視窗內能跑：最壞情況是剛出視窗，要等到下一個排定日視窗再開。
    return ((gap_days - 1) * 24 + (24 - window_h)) * 60.0 + interval


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def task_health(task: dict, now: datetime | None = None) -> dict:
    """單一排程任務的健康判讀。

    回 ``{"name", "state", "detail", "age_min", "threshold_min"}``；state 為：
      ``disabled``  停用中（不算問題）
      ``error``     上次執行拋例外（last_error 有值）
      ``stalled``   啟用中但超過預期間隔沒跑（dispatcher 沒觸發／視窗設錯／掃描掛了）
      ``never``     啟用中、從未跑過，且建立時間已超過預期間隔
      ``unknown``   啟用中、從未跑過但無 created_at → 無法判斷新舊，不告警
      ``ok``        正常
    """
    now = now or datetime.now()
    name = str(task.get("name") or "?")
    threshold = expected_gap_minutes(task) * _STALL_FACTOR + _STALL_GRACE_MIN

    if not task.get("enabled", True):
        return {"name": name, "state": "disabled", "detail": "已停用",
                "age_min": 0.0, "threshold_min": threshold}

    err = task.get("last_error")
    if err:
        # last_error 會在下次成功時被清掉，所以有值＝「最近一次執行是失敗的」。
        return {"name": name, "state": "error", "detail": str(err)[:300],
                "age_min": 0.0, "threshold_min": threshold}

    last = _parse_iso(task.get("last_run_at"))
    if last is None:
        created = _parse_iso(task.get("created_at"))
        if created is None:
            return {"name": name, "state": "unknown", "detail": "從未跑過（無建立時間可判斷）",
                    "age_min": 0.0, "threshold_min": threshold}
        age = (now - created).total_seconds() / 60.0
        if age > threshold:
            return {"name": name, "state": "never",
                    "detail": f"建立後 {age / 60:.1f} 小時從未執行過",
                    "age_min": age, "threshold_min": threshold}
        return {"name": name, "state": "ok", "detail": "剛建立、尚未到期",
                "age_min": age, "threshold_min": threshold}

    age = (now - last).total_seconds() / 60.0
    if age > threshold:
        return {"name": name, "state": "stalled",
                "detail": f"已 {age / 60:.1f} 小時沒執行（預期最長 {threshold / 60:.1f} 小時）",
                "age_min": age, "threshold_min": threshold}
    return {"name": name, "state": "ok", "detail": "正常",
            "age_min": age, "threshold_min": threshold}


_STATE_ICONS = {"ok": "✅", "disabled": "🚫", "error": "❌",
                "stalled": "⏰", "never": "⏳", "unknown": "❔"}


def task_health_icon(state: str) -> str:
    return _STATE_ICONS.get(state, "❔")


def all_task_health(now: datetime | None = None) -> list[dict]:
    """讀 daemon_tasks.json，回每支任務的健康判讀（讀不到就回空）。"""
    try:
        tasks = _load_daemon_tasks().get("tasks") or []
    except Exception:  # noqa: BLE001 —— 監控面不該因為讀檔失敗而炸掉呼叫端
        return []
    return [task_health(t, now) for t in tasks if isinstance(t, dict)]
