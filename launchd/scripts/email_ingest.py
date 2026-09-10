#!/usr/bin/env python3
"""Standalone 15-min email data-lake ingest.

歷史備註：以前這檔 copy 了 agent_daemon.task_email_ingest 的整段邏輯，造成
雙份實作 — 修 bug 漏改一邊（e.g. 2026-04-27 加 heartbeat 在 agent_core 版本
但這份沒同步，導致 alert 仍誤觸 stale）。

現在改成 thin wrapper：直接呼叫 agent_core.daemon_email_ingest.task_email_ingest
共用實作。修補只要改一個地方，行為保證一致。

跑這檔：launchd com.xiaohong.email_ingest 每 15 分鐘觸發
（plist ProgramArguments → 這檔）
"""
import sys
from pathlib import Path

# repo root = 向上 3 層（scripts/ → launchd/ → repo）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_email_ingest import task_email_ingest
from agent_core.daemon_helpers import rotate_log, run_with_deadline
from agent_core.email_classify import _classify_email_for_lake
from agent_core.email_lake import _EMAIL_LAKE_DIR, _lake_append, _lake_load_df
from agent_core.env_utils import env_int
from agent_core.google_auth import get_service


def main():
    rotate_log("email_ingest")
    # 整輪 wall-clock 硬上限。plist StartInterval=900s 的單發 cron：即使 Gmail /
    # Gemini 呼叫各有 per-request timeout，半關閉 TCP 仍可能讓整輪卡死數小時、堵住
    # launchd 下一輪（2026-06-15 卡 poll() 5.4h、郵件停止進 lake 事故）。看門狗到期
    # 強制退出，launchd 下個 interval 重跑。預設 1200s（> 正常一輪含 503 退避，
    # << 數小時）；RED_EMAIL_INGEST_DEADLINE_S 可調。
    deadline_s = env_int("RED_EMAIL_INGEST_DEADLINE_S", 1200, min_value=60, max_value=7200)
    # 共用實作：含 heartbeat write、phase1/2、stats — 不再 copy 邏輯到這
    run_with_deadline(
        lambda: task_email_ingest(
            email_lake_dir=_EMAIL_LAKE_DIR,
            lake_load_df=_lake_load_df,
            lake_append=_lake_append,
            classify_email_for_lake=_classify_email_for_lake,
            get_service=get_service,
        ),
        deadline_s,
        label="email_ingest",
    )


if __name__ == "__main__":
    main()
