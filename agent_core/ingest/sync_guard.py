"""rag_sync 夜跑的「部署誤觸發」守門 + last-run 狀態檔。

問題：com.xiaohong.rag_sync_daily 的 plist 用 KeepAlive={SuccessfulExit=>false}
讓失敗的同步自動重試，但 launchd 語義上 SuccessfulExit 條件「隱含
RunAtLoad=true」（launchd 得先跑一次才有 exit code 可判斷），plist 裡寫的
RunAtLoad=false 會被無視 — 於是每次 redeploy bootstrap 都立刻觸發一輪
7~15 小時的全量同步，而不只有 03:00 的 StartCalendarInterval（2026-06-10
22:30 實測）。

守門規則（只裝在 launchd 入口 launchd/scripts/rag_sync.py；agent_daemon
--task rag_sync 與 telegram 觸發不經過這裡）：
- RAG_SYNC_FORCE=1 → 一律放行（手動逃生口；補跑輸出請重導向
  var/logs/rag_sync_manual*.log — stall 看門狗靠這個命名慣例把手動 run 納入
  「log 有沒有停滯」判定，見 daemon_watchdog._freshest_rag_log）
- 排程窗口內（RAG_SYNC_SCHEDULE_HOUR 前 10 分鐘 ~ 後 45 分鐘）→ 放行
  ⚠️窗口時刻必須由 plist 注入、跟 StartCalendarInterval 一致（程式預設 3）。
  2026-07-04 事故：plist 排程改 01:00 但沒注入這個 env → 每天 01:00 的正常
  排程被當成 deploy 誤觸發跳過，夜跑退化成靠 ≥16h 後門每 ~36h 才跑一輪。
- 上次不是 success（failed / running=中途被殺 / 無記錄）→ 放行
  （KeepAlive 的失敗重試語意靠這條保住）
- 其餘：上次成功距今 < RAG_SYNC_FRESH_HOURS（預設 16h）→ 跳過並 exit 0
  （成功退出不會觸發 SuccessfulExit=false 的重啟）

FRESH_HOURS=16 的取捨：同步常在 10:00~13:00 結束 —
- 當天稍晚 redeploy（距上次成功 0.5~11h）→ 跳過 ✓
- 睡過 01:00、清晨醒來補打的 calendar fire（距上次成功 18h+）→ 放行 ✓
- 01:00 正常排程（距上次成功 ~12–15h，會 <16h）→ 靠排程窗口放行，不靠新鮮度
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from agent_core.env_utils import env_float, env_int

LAST_RUN_BASENAME = "rag_sync_last_run.json"

# 排程窗口邊界（分鐘）：calendar fire 是準點，前 10 後 45 涵蓋慢開機/throttle
_WINDOW_BEFORE_MIN = 10
_WINDOW_AFTER_MIN = 45


def _last_run_path(state_dir: str | None = None) -> str:
    if state_dir is None:
        from agent_core.logging_and_paths import STATE_DIR
        state_dir = STATE_DIR
    return os.path.join(state_dir, LAST_RUN_BASENAME)


def read_last_run(state_dir: str | None = None) -> dict[str, Any]:
    """讀 last-run 狀態；缺檔/壞檔回 {}（視同無記錄 → 守門會放行）。"""
    try:
        with open(_last_run_path(state_dir), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_last_run(status: str, state_dir: str | None = None, **extra: Any) -> None:
    """原子寫 last-run 狀態。失敗 silent — 狀態檔寫不了不該擋同步主流程。

    status="running" 記 started_at，其餘（success/failed）記 finished_at；
    先讀舊檔再 update，所以 success 會保留同一輪 running 寫下的 started_at。
    """
    try:
        path = _last_run_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload: dict[str, Any] = read_last_run(state_dir)
        payload.update({"status": status, "pid": os.getpid(), **extra})
        if status == "running":
            payload["started_at"] = time.time()
        else:
            payload["finished_at"] = time.time()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def should_skip_start(
    now: float | None = None,
    state: dict[str, Any] | None = None,
    state_dir: str | None = None,
) -> tuple[bool, str]:
    """判斷這次 launchd 啟動是否該直接成功退出。回 (skip, 原因)。"""
    if env_int("RAG_SYNC_FORCE", 0):
        return False, "RAG_SYNC_FORCE=1，強制放行"
    if now is None:
        now = time.time()
    if state is None:
        state = read_last_run(state_dir)

    schedule_hour = env_int("RAG_SYNC_SCHEDULE_HOUR", 3, min_value=0, max_value=23)
    fresh_hours = env_float("RAG_SYNC_FRESH_HOURS", 16.0, min_value=0.0)

    lt = time.localtime(now)
    minutes = lt.tm_hour * 60 + lt.tm_min
    win_start = schedule_hour * 60 - _WINDOW_BEFORE_MIN
    span = _WINDOW_BEFORE_MIN + _WINDOW_AFTER_MIN
    # 模 1440 處理跨午夜窗口（schedule_hour=0 時 win_start 為負）
    if (minutes - win_start) % (24 * 60) <= span:
        return False, "排程窗口內，照常執行"

    status = state.get("status")
    if status != "success":
        return False, f"上次狀態={status or '無記錄'}，照常執行（失敗重試/首跑）"

    finished_at = state.get("finished_at")
    if not isinstance(finished_at, (int, float)):
        return False, "上次成功時間缺失，照常執行"
    age_h = (now - float(finished_at)) / 3600.0
    if age_h < 0 or age_h >= fresh_hours:
        return False, f"上次成功已 {age_h:.1f}h 前（≥{fresh_hours:g}h），照常執行"

    return True, (
        f"非排程窗口且上次成功僅 {age_h:.1f}h 前（<{fresh_hours:g}h）"
        "— 應為 deploy/load 觸發的啟動，跳過本輪"
    )
