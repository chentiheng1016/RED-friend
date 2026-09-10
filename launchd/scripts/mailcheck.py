#!/usr/bin/env python3
"""Standalone new-mail classifier — thin wrapper for agent_core.daemon_mailcheck."""
import sys
import traceback
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
from agent_core.daemon_mailcheck import task_mailcheck
from agent_core.email_classify import _classify_email_raw
from agent_core.env_utils import env_int
from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
from agent_core.gmail import _extract_body
from agent_core.google_auth import get_service


def main():
    rotate_log("mailcheck")
    # 整輪 wall-clock 看門狗（見 email_ingest 2026-06-15 卡死事故）：單發 cron 打
    # Gmail + Gemini，per-request timeout 可能被半關閉 TCP 漏接 → 卡死堵住下一輪。
    # 預設 1200s，RED_MAILCHECK_DEADLINE_S 可調。
    run_with_deadline(
        lambda: task_mailcheck(
            get_service=get_service,
            classify_email_raw=_classify_email_raw,
            extract_body=_extract_body,
            gemini_generate=_gemini_generate,
            gemini_model=GEMINI_MODEL,
            notify=notify,
            load_state=load_state,
            update_state=update_state,
        ),
        env_int("RED_MAILCHECK_DEADLINE_S", 1200, min_value=60, max_value=7200),
        label="mailcheck",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[mailcheck] 主程序失敗：{e}")
        traceback.print_exc()
        # exit 0 會讓「exit≠0 → daemon_fail 告警」失明：OAuth 壞掉每小時炸一次
        # 也看不到。同族 morning/ponder/briefing 沒有這層外包（例外自然 exit≠0）。
        sys.exit(1)
