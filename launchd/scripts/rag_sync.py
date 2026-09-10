#!/usr/bin/env python3
"""Daily RAG auto-sync — Drive folders + Gmail → ChromaDB.

Reads targets from var/data/rag_sync_targets.json.
Delegates all sync logic to agent_core.ingest.rag_runner.run_sync() so
this script and agent_daemon.task_rag_sync() always execute the same code
path (including the all_drives global-sync mode).

Triggered by: launchd com.xiaohong.rag_sync_daily (every day at 01:00)
Also callable via: python agent_daemon.py --task rag_sync

注意：plist 的 KeepAlive={SuccessfulExit=>false} 隱含 RunAtLoad，每次
redeploy bootstrap 都會拉起本腳本 — 由 sync_guard.should_skip_start()
守門（窗口外且上次成功還新鮮 → 直接成功退出），失敗重試與 01:00 排程
不受影響。詳見 agent_core/ingest/sync_guard.py。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import rotate_log, run_with_deadline
from agent_core.env_utils import env_int

# 整輪 wall-clock 硬上限（大王指示：01:00 起跑、最多跑 6 小時）。到期
# run_with_deadline 會 dump 堆疊 + os._exit(75)——不會走到下面的
# except/write_last_run("failed")，狀態留在 "running"；這跟任何其他中斷情境
# （斷電/SIGTERM）完全一致，sync_guard.should_skip_start() 本來就把非
# success 狀態當失敗重試，隔天 01:00 正常接著跑、靠既有的 skip-marker 機制
# 跨夜漸進補齊，不需要額外處理。預設 21600s=6h，RAG_SYNC_DEADLINE_S 可調。
_RAG_SYNC_DEADLINE_S = env_int(
    "RAG_SYNC_DEADLINE_S", 21600, min_value=1800, max_value=43200
)


def main():
    rotate_log("rag_sync")
    from agent_core.ingest.sync_guard import should_skip_start, write_last_run
    skip, reason = should_skip_start()
    if skip:
        print(f"[rag_sync] ⏭️ {reason}", flush=True)
        return
    print(f"[rag_sync] ▶️ {reason}", flush=True)
    try:
        from agent_core.ingest.rag_runner import run_sync, sync_lock_is_held
        # 鎖已被別的 sync 持有 → 這次幾乎必然「已在執行中，略過本輪」，
        # 不先蓋 running/started_at，免得覆寫持鎖那輪的真實起跑時間
        # （2026-07-04 診斷曾被這種假 started_at 誤導）。
        if not sync_lock_is_held():
            write_last_run("running")
        result = run_with_deadline(
            run_sync, _RAG_SYNC_DEADLINE_S, label="rag_sync_daily"
        )
    except BaseException:
        # SIGTERM/斷電不會走到這（狀態留在 running，下次啟動視同失敗重試）
        write_last_run("failed")
        raise
    if result.get("locked"):
        return  # 另一個 sync 程序持鎖在跑，最終狀態由它收尾
    write_last_run("success", errors=len(result.get("errors") or []))


if __name__ == "__main__":
    main()
