#!/usr/bin/env python3
"""把「美金對台幣匯率」的兩個排程寫進 daemon_tasks.json（每天 09:00 / 14:00）。

冪等：已存在同名任務就更新設定（保留 last_run_at / run_count / dedup_hashes 這些
跑出來的狀態），不存在就新增。可以重跑。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 跟著執行的那份
checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔、live daemon 看不到。

    cd ~/RED && .venv/bin/python scripts/register_fx_rate_tasks.py
    cd ~/RED && .venv/bin/python scripts/register_fx_rate_tasks.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_fx_rate_tasks.py --remove

這兩個任務走 dispatcher 的 deterministic_tool 路徑：直接呼叫 usd_twd_rate_brief()
把回傳原樣推出去，完全不經 Gemini（數字不會被轉述錯，也不燒 quota）。
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

# 收匯率的四個部門：紅（大王/UserS）、黃（採購）、紫（會計）、橘（業務）。
_COLORS = ["red", "yellow", "purple", "orange"]

# 視窗寬度 = interval → 一個視窗內只會觸發一次（dispatcher 每 5 分鐘掃一次，
# 所以實際送出時間是 09:00–09:05 之間）。與 ashley_daily_brief_am 同型。
_TASKS = [
    {
        "name": "usd_twd_rate_am",
        "start_hour": 9,
        "end_hour": 10,
        "desc": "每天早上 09:00",
    },
    {
        "name": "usd_twd_rate_pm",
        "start_hour": 14,
        "end_hour": 15,
        "desc": "每天下午 14:00",
    },
]

_SHARED = {
    "deterministic_tool": "usd_twd_rate_brief",
    "interval_minutes": 60,
    "enabled": True,
    "notify_channel": "telegram",
    "notify_agent_colors": _COLORS,
    "notify_plain": True,
    "notify_emails": [],
}


def _task_payload(spec: dict) -> dict:
    return {
        "name": spec["name"],
        "prompt": (
            f"{spec['desc']}把美金對台幣即時匯率推給 {'/'.join(_COLORS)} 四色。"
            "本任務走 deterministic_tool（直接呼叫 usd_twd_rate_brief() 原樣推播、"
            "不經 LLM），這段 prompt 只是給人看的說明，不會送進任何模型。"
        ),
        "start_hour": spec["start_hour"],
        "end_hour": spec["end_hour"],
        **_SHARED,
    }


# 跑出來的狀態，更新設定時要保留（不然會重跑/重寄）。
_RUNTIME_FIELDS = ("last_run_at", "run_count", "dedup_hashes", "last_error", "created_at")


def seed(dry_run: bool = False) -> list[str]:
    actions: list[str] = []

    def mutate(data: dict) -> None:
        tasks = data.setdefault("tasks", [])
        by_name = {t.get("name"): t for t in tasks}
        for spec in _TASKS:
            payload = _task_payload(spec)
            existing = by_name.get(spec["name"])
            if existing is None:
                payload.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
                payload.setdefault("last_run_at", None)
                payload.setdefault("run_count", 0)
                payload.setdefault("dedup_hashes", [])
                payload.setdefault("last_error", None)
                tasks.append(payload)
                actions.append(f"➕ 新增 {spec['name']}（{spec['desc']}）")
            else:
                keep = {k: existing[k] for k in _RUNTIME_FIELDS if k in existing}
                existing.clear()
                existing.update(payload)
                existing.update(keep)
                actions.append(f"♻️ 更新 {spec['name']}（保留跑過 {keep.get('run_count', 0)} 次的紀錄）")

    if dry_run:
        mutate(_load_daemon_tasks())
        return actions
    update_daemon_tasks(mutate)
    return actions


def remove(dry_run: bool = False) -> list[str]:
    names = {spec["name"] for spec in _TASKS}
    actions: list[str] = []

    def mutate(data: dict) -> None:
        before = data.get("tasks") or []
        data["tasks"] = [t for t in before if t.get("name") not in names]
        for t in before:
            if t.get("name") in names:
                actions.append(f"🗑️ 刪除 {t.get('name')}")

    if dry_run:
        mutate(_load_daemon_tasks())
        return actions
    update_daemon_tasks(mutate)
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只印出會做什麼，不寫檔")
    parser.add_argument("--remove", action="store_true", help="移除這兩個排程")
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
