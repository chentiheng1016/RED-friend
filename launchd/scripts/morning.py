#!/usr/bin/env python3
"""Standalone morning briefing — thin wrapper for agent_core.daemon_morning."""
import sys
from pathlib import Path

# repo root = 向上 3 層（scripts/ → launchd/ → repo）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import notify, rotate_log, run_with_deadline
from agent_core.daemon_morning import task_morning
from agent_core.env_utils import env_int
from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
from agent_core.gmail import summarize_inbox
from agent_core.google_auth import get_service
from agent_core.memory import recall


def main():
    rotate_log("morning")
    # 整輪 wall-clock 看門狗（見 email_ingest 2026-06-15 卡死事故）：單發 cron 打
    # Gmail + Gemini，卡死會堵住下一輪。預設 1200s，RED_MORNING_DEADLINE_S 可調。
    run_with_deadline(
        lambda: task_morning(
            get_service=get_service,
            summarize_inbox=summarize_inbox,
            recall=recall,
            gemini_generate=_gemini_generate,
            gemini_model=GEMINI_MODEL,
            notify=notify,
        ),
        env_int("RED_MORNING_DEADLINE_S", 1200, min_value=60, max_value=7200),
        label="morning",
    )


if __name__ == "__main__":
    main()
