#!/usr/bin/env python3
"""Daily reflection — 把近 48h 新進 RAG 的文件歸納成跨文件洞察。

Delegates to agent_core.reflection.run_daily_reflection()；intake 由
agent_core/ingest/reflection_intake.py 在夜跑寫入時累積。

Triggered by: launchd com.xiaohong.reflection（每日 21:30——夜跑 03:00 起
7-15h 最晚 ~18:00 收尾，21:30 錯開；就算撞到，run_daily_reflection 會探測
rag_sync.lock 自行跳過）。
Also callable via: python agent_daemon.py --task reflection（手動觸發，
不受下面「今天跑過就跳過」守門限制）。

注意：plist 的 KeepAlive={SuccessfulExit=>false} 隱含 RunAtLoad，每次
redeploy bootstrap 都會拉起本腳本——用「今天已成功跑過就跳過」的 state
守門擋掉（同 rag_sync 的 sync_guard 精神，反思冪等成本低所以守門更簡單）。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import load_state, rotate_log, update_state

_STATE_KEY = "reflection_last_success_date"


def main():
    rotate_log("reflection")
    today = datetime.now().date().isoformat()
    if load_state().get(_STATE_KEY) == today:
        print(f"[reflection] ⏭️ 今天（{today}）已成功跑過——redeploy 誤觸發，跳過", flush=True)
        return
    from agent_core.reflection import run_daily_reflection
    result = run_daily_reflection()
    print(f"[reflection] 結果：{result}", flush=True)
    if result.get("skipped"):
        # rag_sync 還在跑：不記 success（KeepAlive 不重生因為 exit 0；
        # 明天 21:30 排程照常，今天這輪讓掉——watermark 沒推進，文件不會漏）。
        return
    errors = result.get("errors") or []
    if errors:
        # launchd 排程路徑沒有 agent_daemon._task_wrapper 的連續失敗通知——
        # 這裡自己補，否則反思壞掉是靜默的（dashboard 沒接反思監控）。
        try:
            from agent_core.daemon_helpers import notify
            notify(
                subject=f"【小紅每日反思】{len(errors)} 個分組失敗"
                        + ("（全軍覆沒）" if not result.get("groups_reflected") else ""),
                body="\n".join(str(e) for e in errors),
                task_name="reflection",
            )
        except Exception as exc:
            print(f"[reflection] ⚠️ 失敗通知寄送失敗：{exc}", flush=True)
    if errors and not result.get("groups_reflected"):
        # 全軍覆沒：不記今天成功——今晚 redeploy 或手動 kickstart 可重試
        # （watermark 也沒推進，重試會處理同一窗）。
        return
    update_state(lambda s: s.__setitem__(_STATE_KEY, today))


if __name__ == "__main__":
    main()
