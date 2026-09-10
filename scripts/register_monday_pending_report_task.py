#!/usr/bin/env python3
"""把「週一會議未完成事項總報告」的排程寫進 daemon_tasks.json。

需求（2026-08-17，大王）：每週一 10:00 是全公司例行會議，週一早上 06:00 要先
寄一份「未完成事項總報告」給大王（owner@）與 UserS（gm@），整理範圍是
開會前三週（21 天）。

冪等：已存在同名任務就更新設定（保留 last_run_at / run_count / dedup_hashes 這些
跑出來的狀態），不存在就新增。可以重跑。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 跟著執行的那份
checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔、live daemon 看不到。
⚠️ 依賴 dispatcher 的 `weekdays` 排程欄位（同一個 PR 加的）：先 redeploy
dispatcher 再跑這支，否則舊碼看不懂 weekdays、任務會變成**每天** 06:00 寄。

    cd ~/RED && .venv/bin/python scripts/register_monday_pending_report_task.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_monday_pending_report_task.py
    cd ~/RED && .venv/bin/python scripts/register_monday_pending_report_task.py --remove

走 dispatcher 的 LLM 路徑（不是 deterministic_tool）：總報告要跨六個資料面彙整
排序，本來就是綜合整理、不是單一工具的原樣輸出。prompt 點名的工具全部都要進得了
safe_tools，否則靜默少一段（守門：tests/test_dispatcher_task_tool_reachability.py
的 SHIPPED_TASK_TOOL_REFS 有這支任務的快照）。
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

TASK_NAME = "monday_meeting_pending_report"

_PROMPT = (
    "(訊息開頭的「現在時間」是你判斷今天日期的依據。)\n"
    "背景：每週一 10:00 是全公司例行會議，這份報告是大王（Owner）與 UserS 開會前的"
    "準備資料。\n"
    "目標：彙整**開會前三週（最近 21 天）**全公司「還沒完成／還沒回覆／還在卡關」的"
    "事項，整理成一份總報告。\n"
    "這是每週一固定報表、一律完整輸出 —— 就算某一段查無資料，也要明確寫"
    "「本段無未完成事項」，不要回「(無新發現)」。\n"
    "步驟（單一工具回錯誤就在該段註明查詢失敗，繼續做其他段）：\n"
    "1) 呼叫 list_unanswered_company_threads(days=21) —— 全公司信箱裡對方寄來、"
    "我方尚未回覆的事項。\n"
    "2) 呼叫 list_unanswered_sent_threads(days_overdue=3, lookback_days=21) —— "
    "大王本人寄出、對方尚未回覆的事項。\n"
    "3) 呼叫 production_alert() 與 production_overdue_bom() —— 生產面落後與逾期"
    "未完的單。\n"
    "4) 呼叫 kitting_alert() —— 近期開工可能缺料的預警。\n"
    "5) 呼叫 warehouse_todo_board(days=21, persist=False) —— 倉庫待辦裡仍未處理"
    "的單（一定要帶 persist=False，這份報告只讀、不動倉庫任務自己的待辦狀態）。\n"
    "6) 對 1) 2) 裡看起來跟客戶/供應商訂單相關的，可用 query_erp_order 或 "
    "cross_reference_order 核對 ERP 現況；ERP 顯示已結案的標註「(ERP 顯示已結案)」"
    "但仍保留列出，最終判斷交給與會的人。\n"
    "輸出格式：\n"
    "- 第一行『📋 週一會議未完成事項總報告』，第二行寫報告期間（起訖日期＝今天往回 "
    "21 天，一定要寫實際日期）。\n"
    "- 之後分段：①全公司未回信件（按信箱分組：對方、主旨、已等待天數）②大王寄出"
    "未獲回覆 ③生產落後/逾期 ④缺料預警 ⑤倉庫待辦 ⑥其他觀察。\n"
    "- 條列精簡、重要與拖最久的排前面；只整理工具回傳的事實，不要編造數字。"
)

_TASK = {
    "name": TASK_NAME,
    "prompt": _PROMPT,
    # 只在週一（ISO 1）觸發；06–07 視窗寬度 = interval → 一個視窗只跑一次，
    # dispatcher 每 5 分鐘掃描，實際寄出落在 06:00–06:05，離 10:00 會議夠早。
    "weekdays": [1],
    "start_hour": 6,
    "end_hour": 7,
    "interval_minutes": 60,
    "enabled": True,
    # 只走 email（大王 2026-08-17 指定）：兩個信箱各自「自己寄給自己」一封
    # （網域委派，同 email_pending_tracker 的收件人，委派已開通）。
    "notify_emails": ["owner@company.example", "gm@company.example"],
    "email_subject": "週一會議未完成事項總報告 {date}",
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
                f"➕ 新增 {TASK_NAME}（每週一 06:00 未完成事項總報告 → "
                "owner@／gm@ 各一封）")
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
        print("完成。下週一 06:00–06:05 會寄出第一份（dispatcher 每 5 分鐘掃一次）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
