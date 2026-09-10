#!/usr/bin/env python3
"""Standalone 15-min briefing — thin wrapper for agent_core.daemon_briefing_15min."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.briefing import meeting_briefing
from agent_core.daemon_briefing_15min import find_upcoming_meetings, task_briefing_15min
from agent_core.daemon_helpers import load_state, rotate_log, run_with_deadline, ts, update_state
from agent_core.env_utils import env_int
from agent_core.google_suite import _find_next_meeting
from agent_core.telegram import telegram_push


def main():
    rotate_log("briefing_15min")
    # 打 Calendar/Gemini/Telegram 的單發 cron：整輪 wall-clock 看門狗，避免某次呼叫漏接
    # per-request timeout 卡死整輪、堵 launchd 下一輪（健檢 Low：對齊 email_ingest 等的
    # 覆蓋；google_auth httplib2 timeout=120 雖會自解，看門狗是兜底）。
    deadline_s = env_int("RED_BRIEFING_15MIN_DEADLINE_S", 1200, min_value=60, max_value=7200)
    run_with_deadline(
        lambda: task_briefing_15min(
            find_next_meeting_fn=_find_next_meeting,
            meeting_briefing_fn=meeting_briefing,
            telegram_push_fn=telegram_push,
            load_state=load_state,
            update_state=update_state,
            ts_fn=ts,
            # 多場版本：back-to-back 會議（間隔 <10 分鐘）第二場也要被 brief——
            # 只看「下一場」的舊行為會永遠漏掉它。
            find_upcoming_meetings_fn=find_upcoming_meetings,
        ),
        deadline_s,
        label="briefing_15min",
    )


if __name__ == "__main__":
    main()
