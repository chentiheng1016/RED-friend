#!/usr/bin/env python3
"""Standalone health check — thin wrapper for agent_core.daemon_health_check.

歷史備註：這檔以前 copy 了 task_health_check 邏輯（其實還比 agent_daemon
版本更新 — 多了 dedup 機制）。現在統一搬到 agent_core/daemon_health_check.py
做 single source of truth。

跑這檔：launchd com.xiaohong.health_check 排程觸發
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_health_check import task_health_check
from agent_core.daemon_helpers import (
    load_state,
    notify,
    rotate_log,
    run_with_deadline,
    update_state,
)
from agent_core.env_utils import env_int
from agent_core.health import health_check


def main():
    rotate_log("health_check")
    # 整輪 wall-clock 看門狗（比照 mailcheck/ponder 等兄弟 cron script；健檢
    # Medium：這是唯一沒包 run_with_deadline 的 tick daemon，而它是斷線 bot 的
    # 自動救援者 — chroma 半死時 _check_data_stores 的 col.count() 可能卡死，
    # StartInterval 不會在前一輪還在跑時起新輪 → 自動救援永久停擺）。
    # 預設 1200s，RED_HEALTH_CHECK_DEADLINE_S 可調。
    run_with_deadline(
        lambda: task_health_check(
            health_check_fn=health_check,
            load_state=load_state,
            update_state=update_state,
            notify=notify,
        ),
        env_int("RED_HEALTH_CHECK_DEADLINE_S", 1200, min_value=60, max_value=7200),
        label="health_check",
    )


if __name__ == "__main__":
    main()
