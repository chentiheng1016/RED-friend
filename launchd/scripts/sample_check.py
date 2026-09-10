#!/usr/bin/env python3
"""Standalone sample deadline check — thin wrapper for agent_core.daemon_sample_check.

歷史：以前 copy 了邏輯，現在改 thin wrapper 共用 agent_core 實作。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import notify, rotate_log, run_with_deadline, ts
from agent_core.daemon_sample_check import task_sample_check
from agent_core.env_utils import env_int
from agent_core.sample_tracker import check_sample_deadlines
from agent_core.telegram import telegram_push


def main():
    rotate_log("sample_check")
    # 打 Drive/Telegram 的單發 cron：整輪 wall-clock 看門狗（健檢 Low：對齊覆蓋）。
    deadline_s = env_int("RED_SAMPLE_CHECK_DEADLINE_S", 1200, min_value=60, max_value=7200)
    run_with_deadline(
        lambda: task_sample_check(
            check_sample_deadlines_fn=check_sample_deadlines,
            notify=notify,
            telegram_push_fn=telegram_push,
            ts_fn=ts,
        ),
        deadline_s,
        label="sample_check",
    )


if __name__ == "__main__":
    main()
