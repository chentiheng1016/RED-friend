"""Task-queue bootstrap helpers — wire worker into the right daemon mode.

由 `agent_daemon.py main()` 呼叫一行 `maybe_start_queue_worker(task_name)`，
依任務型態決定怎麼跑 task_queue 的 worker：

  long-lived 任務（telegram_bot）
    → start_worker_thread()，背景 thread 整個 daemon 生命週期都在跑

  tick 任務（dispatcher / email_ingest / briefing_15min / ...）
    → tick 即將結束前 drain 幾個 queue task 再退出
       (這些 daemon 由 launchd 每 N 分鐘叫起來跑一輪就退，
        起 thread 沒意義 — thread 也會跟著 process 死)

  一次性任務（morning / mailcheck / sample_check / health_check）
    → 不接 queue，maybe_start_queue_worker 直接 no-op

設計考量：
  - 不在 module-import time 就啟動 worker — 這個 module 可能被 REPL / 測試
    間接 import 到，不該因此就跑 background thread
  - 失敗安全：worker 起不來不應該擋掉 daemon 本身
  - 重複呼叫 idempotent：start_worker_thread 內部已自帶 alive 檢查
"""
from __future__ import annotations

# 哪些 task 需要常駐 worker thread
_LONG_LIVED_TASKS = frozenset({
    "telegram_bot",
})

# 哪些 task tick 完才 drain queue（短壽命，起 thread 無意義）
_TICK_TASKS = frozenset({
    "dispatcher",
    "email_ingest",
    "briefing_15min",
    "sample_check",
})

# 每次 tick 最多 drain 幾個 queue task（防一個慢 task 拖累整輪）
_TICK_DRAIN_MAX = 20


def maybe_start_queue_worker(task_name: str) -> None:
    """daemon main() 派發 task 前呼叫；依 task 名決定要不要啟 worker。"""
    if task_name in _LONG_LIVED_TASKS:
        try:
            from agent_core.task_queue import start_worker_thread
            ok = start_worker_thread()
            if ok:
                print(f"[queue] ✅ worker thread started ({task_name} 為 long-lived daemon)")
            else:
                print(f"[queue] ⚠️ worker thread 啟動失敗（不影響 {task_name}）")
        except Exception as exc:
            print(f"[queue] ⚠️ start_worker_thread 例外（不影響 daemon）：{exc}")


def drain_after_tick(max_iterations: int = _TICK_DRAIN_MAX) -> int:
    """tick 任務在主邏輯跑完後呼叫，把 queue 中可跑的 task 一口氣 drain 掉。

    Returns:
        實際跑了幾個 task。
    """
    try:
        from agent_core.task_queue import run_worker_once
    except Exception as exc:
        print(f"[queue] ⚠️ drain 失敗（不影響 daemon）：{exc}")
        return 0
    ran = 0
    for _ in range(max_iterations):
        try:
            n = run_worker_once()
        except Exception as exc:
            print(f"[queue] ⚠️ run_worker_once 例外：{exc}")
            break
        if not n:
            break
        ran += 1
    if ran:
        print(f"[queue] ✅ tick 結束前 drain {ran} 個 task")
    return ran


def _fire_task_reminders() -> int:
    """fire 到期的 task_memory reminder（同 tick 順便做）。"""
    try:
        from agent_core.task_memory import fire_due_reminders
        n = fire_due_reminders()
        if n:
            print(f"[task_memory] 🔔 fired {n} reminder(s)")
        return n
    except Exception as exc:
        print(f"[task_memory] ⚠️ fire_due_reminders 失敗（不影響 daemon）：{exc}")
        return 0


def maybe_drain_after_tick(task_name: str) -> int:
    """tick 任務跑完呼叫；非 tick 任務 no-op。

    `agent_daemon.py main()` 在 _task_wrapper 之後呼叫一次：

        maybe_drain_after_tick(args.task)

    （long-lived 已有常駐 thread；tick 任務 drain 一輪；一次性任務 no-op）

    順便 fire 到期的 task_memory reminder — tick 已經是「定期觸發」的
    場合，最自然就插在這。reminder fire 不算 queue task drain count。
    """
    if task_name in _TICK_TASKS:
        drained = drain_after_tick()
        _fire_task_reminders()
        return drained
    return 0
