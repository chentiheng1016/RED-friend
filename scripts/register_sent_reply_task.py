#!/usr/bin/env python3
"""把「我寄出去、對方沒回」的每日提醒排程寫進 daemon_tasks.json（每天 09:00）。

冪等：已存在同名任務就更新設定（保留 last_run_at / run_count / dedup_hashes 這些
跑出來的狀態），不存在就新增。可以重跑。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 跟著執行的那份
checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔、live daemon 看不到。

    cd ~/RED && .venv/bin/python scripts/register_sent_reply_task.py
    cd ~/RED && .venv/bin/python scripts/register_sent_reply_task.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_sent_reply_task.py --remove

這個任務走 dispatcher 的 deterministic_tool 路徑：直接呼叫 sent_reply_reminder()
把回傳原樣推出去，完全不經 Gemini（主旨/天數不會被轉述錯，也不燒 quota）。

門檻用環境變數調（改完要 redeploy dispatcher 才吃得到）：
    RED_SENT_REPLY_OVERDUE_DAYS   幾天沒回算逾期（預設 3）
    RED_SENT_REPLY_LOOKBACK_DAYS  只回溯最近幾天寄出的信（預設 14）
    RED_SENT_REPLY_MAX_ITEMS      一則訊息最多列幾封（預設 25）
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core.scheduler import (  # noqa: E402
    DAEMON_TASKS_FILE,
    _load_daemon_tasks,
    update_daemon_tasks,
)

TASK_NAME = "sent_reply_reminder_am"

# 這是大王本人的待辦，只推紅色（owner 主通道）—— 不擴散給其他部門。
_TASK = {
    "name": TASK_NAME,
    "prompt": (
        "每天早上 09:00 提醒大王：他本人寄出去、對方到現在還沒回覆的信。"
        "本任務走 deterministic_tool（直接呼叫 sent_reply_reminder() 原樣推播、"
        "不經 LLM），這段 prompt 只是給人看的說明，不會送進任何模型。"
    ),
    "deterministic_tool": "sent_reply_reminder",
    "start_hour": 9,
    "end_hour": 10,
    # 視窗寬度 = interval → 一個視窗內只會觸發一次（dispatcher 每 5 分鐘掃一次，
    # 所以實際送出時間是 09:00–09:05 之間）。與 usd_twd_rate_am 同型。
    "interval_minutes": 60,
    "enabled": True,
    "notify_channel": "telegram",
    "notify_agent_colors": ["red"],
    "notify_plain": True,
    "notify_emails": [],
}

# 跑出來的狀態，更新設定時要保留（不然會重跑/重寄）。
_RUNTIME_FIELDS = ("last_run_at", "run_count", "dedup_hashes", "last_error", "created_at")


def seed(dry_run: bool = False) -> list[str]:
    actions: list[str] = []

    def mutate(data: dict) -> None:
        tasks = data.setdefault("tasks", [])
        existing = next((t for t in tasks if t.get("name") == TASK_NAME), None)
        payload = dict(_TASK)
        if existing is None:
            payload.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
            payload.setdefault("last_run_at", None)
            payload.setdefault("run_count", 0)
            payload.setdefault("dedup_hashes", [])
            payload.setdefault("last_error", None)
            tasks.append(payload)
            actions.append(f"➕ 新增 {TASK_NAME}（每天 09:00 推紅色 bot）")
        else:
            keep = {k: existing[k] for k in _RUNTIME_FIELDS if k in existing}
            existing.clear()
            existing.update(payload)
            existing.update(keep)
            actions.append(f"♻️ 更新 {TASK_NAME}（保留跑過 {keep.get('run_count', 0)} 次的紀錄）")

    if dry_run:
        mutate(_load_daemon_tasks())
        return actions
    update_daemon_tasks(mutate)
    return actions


def remove(dry_run: bool = False) -> list[str]:
    actions: list[str] = []

    def mutate(data: dict) -> None:
        before = data.get("tasks") or []
        data["tasks"] = [t for t in before if t.get("name") != TASK_NAME]
        if len(data["tasks"]) != len(before):
            actions.append(f"🗑️ 刪除 {TASK_NAME}")

    if dry_run:
        mutate(_load_daemon_tasks())
        return actions
    update_daemon_tasks(mutate)
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只印出會做什麼，不寫檔")
    parser.add_argument("--remove", action="store_true", help="移除這個排程")
    args = parser.parse_args()

    print(f"daemon_tasks.json：{DAEMON_TASKS_FILE}")
    actions = remove(args.dry_run) if args.remove else seed(args.dry_run)
    for line in actions or ["（沒有變更）"]:
        print("  " + line)
    if args.dry_run:
        print("（--dry-run，沒有寫入）")
    else:
        print("完成。dispatcher 每 5 分鐘掃一次，下一個視窗就會生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
