#!/usr/bin/env python3
"""把「客戶貨款到帳通知 → 台越會計」的排程寫進 daemon_tasks.json。

需求（2026-08-12，大王）：UserAng（twsales@）與大王本人（owner@）會收到客人
付貨款的通知，要直接推到**紫色 agent**（台灣會計 UserJ＋越南會計 UserL 都綁紫色，
一次推播兩人都收得到），同時也**寄 email** 給兩人的信箱。

冪等：已存在同名任務就更新設定（保留 last_run_at / run_count / dedup_hashes 這些
跑出來的狀態），不存在就新增。可以重跑。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 跟著執行的那份
checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔、live daemon 看不到。

    cd ~/RED && .venv/bin/python scripts/register_payment_notice_task.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_payment_notice_task.py
    cd ~/RED && .venv/bin/python scripts/register_payment_notice_task.py --remove

走 dispatcher 的 deterministic_tool 路徑：直接呼叫 payment_notice_alert() 把回傳
原樣送出，完全不經 Gemini —— 金額／匯款人／發票號讓 LLM 轉述一次就有講錯的風險。

兩個投遞管道**並行**（2026-08-12 起 dispatcher 支援）：notify_channel=telegram
推紫色 bot、notify_emails 各自自寄一封。以前 notify_emails 一設就會把 Telegram
整條吃掉，所以那個並行是這個需求的前提。

門檻用環境變數調（改完要 redeploy dispatcher 才吃得到）：
    RED_PAYMENT_NOTICE_DAYS      每輪往回看幾天的信（預設 3）
    RED_PAYMENT_NOTICE_SA_FILE   網域委派金鑰路徑（預設取 rag_sync_targets.json）
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

TASK_NAME = "payment_notice_watch"

_TASK = {
    "name": TASK_NAME,
    "prompt": (
        "每天 09:00 把 UserAng（twsales@）與大王（owner@）信箱收到的『客戶貨款到帳／"
        "付款通知』彙整一次，轉知台灣會計（UserJ）與越南會計（UserL）。本任務走 "
        "deterministic_tool（直接呼叫 payment_notice_alert() 原樣送出、不經 LLM），"
        "這段 prompt 只是給人看的說明，不會送進任何模型。每封通知只送一次"
        "（工具自己記狀態），沒有新的就回「(無新發現)」、整天安靜。"
    ),
    "deterministic_tool": "payment_notice_alert",
    # 每天固定彙整一次（大王 2026-08-12 選的節奏，不要準即時逐筆推）。
    # 09:00 那個窗口＝把昨天到今早的到帳一次講完，跟採購/倉庫/匯率那幾支同節奏。
    # 視窗寬度 = interval → 一個視窗內只觸發一次（dispatcher 每 5 分鐘掃一次，
    # 所以實際送出時間落在 09:00–09:05）。同 usd_twd_rate_am / sent_reply_reminder_am。
    # 沒有新到帳的那天工具回「(無新發現)」，dispatcher 直接跳過 —— 會計不會每天
    # 收到一封「今天沒有」。
    "start_hour": 9,
    "end_hour": 10,
    "interval_minutes": 60,
    "enabled": True,
    # 兩條管道並行：紫色 bot（台/越兩位會計，收件人由員工 registry 解析、
    # 不吃 LLM 輸出；chat_id↔身分對照不進 repo）＋ 兩個信箱各自「自己寄給自己」一封。
    "notify_channel": "telegram",
    "notify_agent_colors": ["purple"],
    "notify_plain": True,
    "notify_emails": ["twaccounting@company.example", "accounting-vn@company.example"],
    "email_subject": "客戶貨款到帳通知 {date} {time}",
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
            actions.append(
                f"➕ 新增 {TASK_NAME}（每天 09:00 彙整一次 → 紫色 bot ＋ "
                "twaccounting@／lan@ 各一封）")
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
