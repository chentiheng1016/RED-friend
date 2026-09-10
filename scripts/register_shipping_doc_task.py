#!/usr/bin/env python3
"""把「Supremo（Lurchi）出貨文件追蹤 → 橙色業務」的排程寫進 daemon_tasks.json。

需求（2026-08-20，大王）：Supremo 的 ContactW 對每櫃出貨有固定文件要求（電郵兩位
ContactW、EXCEL 裝箱單、船開 5 天內副本／10 天內正本，逾期罰款）。時間到 →
Telegram 問業務 UserAng 是否已按要求提供文件 → 她在 Telegram 回覆 → 查核
email 真的有辦理 → 結案。判準與訊息都在 agent_core/shipping_doc_tracker.py。

新櫃**不必再手動加**：每天那輪會先掃 twsales@ 裡 ContactW 近 30 天
（``RED_SHIPDOC_NOTICE_DAYS``）的「正本文件情況」通知信自動建案。下面這兩櫃
只是把上線當下手上的案子種進去（保險，且腳本冪等）：

  * LURCHI-CONT7 // 2454 PRS // ETD 07 AUG 2026（大王轉的那封通知）
  * LURCHI-CONT8 // 6241 PRS // ETD 15 AUG 2026（同日 ContactW 發的同款通知；
    副本文件期限 08-20——正是這支上線當天）

自動建案抓不到 ETD 的（例如通知信只寫發文日）會在當天的推播裡點名，
再用 track_shipping_docs 工具補（REPL 或紅色 bot 都行）。

冪等：任務已存在就更新設定（保留 last_run_at / run_count / dedup_hashes）；
種子櫃已存在只補 etd/pairs、不動查核狀態。可以重跑。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 與狀態檔都
跟著執行的那份 checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔。

    cd ~/RED && .venv/bin/python scripts/register_shipping_doc_task.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_shipping_doc_task.py
    cd ~/RED && .venv/bin/python scripts/register_shipping_doc_task.py --remove
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
from agent_core.shipping_doc_tracker import track_shipment  # noqa: E402

TASK_NAME = "shipping_doc_watch"

_TASK = {
    "name": TASK_NAME,
    "prompt": (
        "每天 09:00 檢查 Supremo（Lurchi）出貨文件案件：先掃 ContactW 的「正本文件"
        "情況」通知信自動建立新櫃，再把到期未結案的櫃在橙色 bot "
        "問業務 UserAng 是否已按客人要求提供文件；同時查核 twsales@/shipping@ 寄件"
        "備份——「帶附件、寄給兩位 ContactW、主旨含櫃號」的文件信查核通過就自動結案。"
        "本任務走 deterministic_tool（直接呼叫 shipping_doc_check() 原樣推播、"
        "不經 LLM），這段 prompt 只是給人看的說明，不會送進任何模型。"
        "沒有到期的櫃或全部結案就回「(無新發現)」、整天安靜。"
    ),
    "deterministic_tool": "shipping_doc_check",
    # 每天上班時段固定問一次（同 payment_notice / sent_reply_reminder 的 09:00
    # 節奏；視窗寬度 = interval → 一個視窗只觸發一次，實際送出 09:00–09:05）。
    "start_hour": 9,
    "end_hour": 10,
    "interval_minutes": 60,
    "enabled": True,
    # 推橙色（業務）bot——收件人由員工 registry 解析（UserAng twsales@）。
    # UserAng 直接在同一個 bot 回覆，freeform 白名單裡的 confirm_shipping_docs
    # 會記錄她的回覆並立刻查核（dept_tool_scope 的 orange addendum 有教 LLM）。
    "notify_channel": "telegram",
    "notify_agent_colors": ["orange"],
    "notify_plain": True,
    "notify_emails": [],
}

ESCALATION_TASK_NAME = "shipping_doc_escalation"

# 升級線：橙色連催超過門檻天數還沒結 → 每天 10:00 推**紅色**（大王）。
# 為什麼要獨立一個 task 而不是在主任務裡多推一色：dispatcher 的
# notify_agent_colors 是**靜態設定**，主任務若加上 red，大王每天都會收到那份
# 例行提醒；升級的意義正是「平常安靜、出事才吵」。工具自己在沒有逾期時回
# 「(無新發現)」，dispatcher 就整輪不推。
_ESCALATION_TASK = {
    "name": ESCALATION_TASK_NAME,
    "prompt": (
        "每天 10:00 檢查 Supremo 出貨文件有沒有『橙色連催多天仍未結』的櫃："
        "副本電郵逾期、或正本過了 ETD+10 仍未見簽收，超過 RED_SHIPDOC_ESCALATE_D"
        "（預設 3）天就升級通知大王。本任務走 deterministic_tool（直接呼叫 "
        "shipping_doc_escalation() 原樣推播、不經 LLM），這段 prompt 只是給人看的"
        "說明。⚠️ 它只讀主任務寫好的狀態、不掃信箱，所以排在 09:00 那輪之後的 10:00。"
        "沒有越線的櫃就回「(無新發現)」、整天安靜。"
    ),
    "deterministic_tool": "shipping_doc_escalation",
    # 10:00 —— 刻意排在主任務（09:00–10:00 視窗、實際 09:00–09:05 送出）**之後**
    # 的下一個小時，讀到的才是今天剛更新的狀態。
    # ⚠️ 不要用「同視窗 + start_minute」來排序：scheduler 沒有 start_minute 這個
    # 欄位，寫了會被靜默忽略，兩支就變成同一個視窗搶跑（順序只看 tasks 在檔案裡
    # 的先後，很脆弱）。用不同小時是唯一硬的做法。
    "start_hour": 10,
    "end_hour": 11,
    "interval_minutes": 60,
    "enabled": True,
    "notify_channel": "telegram",
    "notify_agent_colors": ["red"],
    "notify_plain": True,
    "notify_emails": [],
}

# 現在追的兩櫃（來源：ContactW Cheung 2026-08-20 的兩封「正本文件情況」通知信主旨）。
_SEED_SHIPMENTS = (
    {"ref": "LURCHI-CONT7", "pairs": "2454 PRS", "etd": "2026-08-07"},
    {"ref": "LURCHI-CONT8", "pairs": "6241 PRS", "etd": "2026-08-15"},
)

# 跑出來的狀態，更新設定時要保留（不然會重跑/重推）。
_RUNTIME_FIELDS = ("last_run_at", "run_count", "dedup_hashes", "last_error", "created_at")


def seed(dry_run: bool = False) -> list[str]:
    actions: list[str] = []

    def _upsert(tasks: list, spec: dict, label: str) -> None:
        name = spec["name"]
        existing = next((t for t in tasks if t.get("name") == name), None)
        payload = dict(spec)
        if existing is None:
            payload.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
            payload.setdefault("last_run_at", None)
            payload.setdefault("run_count", 0)
            payload.setdefault("dedup_hashes", [])
            payload.setdefault("last_error", None)
            tasks.append(payload)
            actions.append(f"➕ 新增 {name}（{label}）")
        else:
            keep = {k: existing[k] for k in _RUNTIME_FIELDS if k in existing}
            existing.clear()
            existing.update(payload)
            existing.update(keep)
            actions.append(f"♻️ 更新 {name}（保留跑過 {keep.get('run_count', 0)} 次的紀錄）")

    def mutate(data: dict) -> None:
        tasks = data.setdefault("tasks", [])
        _upsert(tasks, _TASK, "每天 09:00 → 橙色 bot 問 UserAng／查核結案")
        _upsert(tasks, _ESCALATION_TASK, "每天 10:00 → 逾期才升級推紅色")

    if dry_run:
        mutate(_load_daemon_tasks())
    else:
        update_daemon_tasks(mutate)

    for ship in _SEED_SHIPMENTS:
        if dry_run:
            actions.append(f"（--dry-run）會把 {ship['ref']}（ETD {ship['etd']}）種進追蹤")
            continue
        key, created = track_shipment(**ship)
        verb = "已種入追蹤" if created else "已存在、只更新 ETD/雙數（查核狀態保留）"
        actions.append(f"📦 {key} {verb}")
    return actions


def remove(dry_run: bool = False) -> list[str]:
    """只移除排程任務；追蹤狀態檔留著（結案紀錄是稽核資料，不隨排程消失）。"""
    actions: list[str] = []

    def mutate(data: dict) -> None:
        before = data.get("tasks") or []
        drop = {TASK_NAME, ESCALATION_TASK_NAME}
        data["tasks"] = [t for t in before if t.get("name") not in drop]
        if len(data["tasks"]) != len(before):
            actions.append("🗑️ 刪除 " + "、".join(sorted(drop))
                           + "（var/state/shipping_doc_tracker.json 保留）")

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
        print("完成。dispatcher 每 5 分鐘掃一次，明天 09:00 的視窗生效；"
              "要立刻試跑用 run_scheduled_task_now。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
