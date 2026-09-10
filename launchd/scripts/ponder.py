#!/usr/bin/env python3
"""Standalone background ponder — thin wrapper for agent_core.daemon_ponder.

歷史備註：以前這檔 copy 了整段 task_ponder 邏輯，跟
agent_core.daemon_ponder.task_ponder 重複。現在改 thin wrapper，避免雙份
實作 drift。

跑這檔：launchd com.xiaohong.ponder 排程觸發
"""
import sys
from datetime import datetime
from pathlib import Path

# repo root = 向上 3 層（scripts/ → launchd/ → repo）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import (
    load_state,
    notify,
    rotate_log,
    run_with_deadline,
    update_state,
)
from agent_core.daemon_ponder import (
    extract_fresh_insights, remember_ponder_insights, task_ponder,
)
from agent_core.env_utils import env_int
from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
from agent_core.gmail import search_gmail, summarize_inbox
from agent_core.google_auth import get_service

WORK_HOUR_START = 9
WORK_HOUR_END = 22


def _in_working_hours() -> bool:
    h = datetime.now().hour
    return WORK_HOUR_START <= h < WORK_HOUR_END


def _remember_ponder_insights(seen_hashes, fresh):
    remember_ponder_insights(
        seen_hashes,
        fresh,
        update_state=update_state,
    )


def main():
    rotate_log("ponder")
    # 整輪 wall-clock 看門狗（見 email_ingest 2026-06-15 卡死事故）：單發 cron 打
    # Gmail + Gemini，卡死會堵住下一輪。預設 1200s，RED_PONDER_DEADLINE_S 可調。
    # 共用實作：所有邏輯在 agent_core.daemon_ponder.task_ponder
    run_with_deadline(
        lambda: task_ponder(
            in_working_hours=_in_working_hours,
            work_hour_start=WORK_HOUR_START,
            work_hour_end=WORK_HOUR_END,
            load_state=load_state,
            summarize_inbox=summarize_inbox,
            get_service=get_service,
            search_gmail=search_gmail,
            gemini_generate=_gemini_generate,
            gemini_model=GEMINI_MODEL,
            extract_fresh_insights_fn=extract_fresh_insights,
            remember_ponder_insights_fn=_remember_ponder_insights,
            notify=notify,
        ),
        env_int("RED_PONDER_DEADLINE_S", 1200, min_value=60, max_value=7200),
        label="ponder",
    )


if __name__ == "__main__":
    main()
