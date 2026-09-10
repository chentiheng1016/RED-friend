#!/usr/bin/env python3
"""註冊生產管理部每日簡報排程 —— 生管主管經理（gray）早 09:00 / 午 15:00 各一封。

2026-08-05 需求（生管主管 production-mgr@company.example，越南廠經理／生產管理部主管）：
  1. 迪卡儂剩下的材料，每個形體還能做多少雙（判斷能不能接新單）
  2. 針車 / 射出 / 包裝 每天的產能，用折線圖看趨勢

兩段都走 `production_capacity_brief()` 一顆確定性工具：dispatcher 的
``deterministic_tool`` 路徑直接把工具回傳當信件內容，**完全不經 Gemini** —— 數字
沒有被轉述錯的空間，也不燒 quota（同 usd_twd_rate_am/pm）。折線圖以
``[[MAIL_FILE:]]`` 標記帶出，由 dispatcher 轉成附件寄出。

daemon_tasks.json 是 runtime 狀態（.gitignore 內），任務定義本身進不了版控，所以
這支腳本把定義放進版控、並以冪等方式寫入：同名任務存在就更新定義欄位（保留
last_run_at / run_count / dedup_hashes），不存在才新建。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 跟著執行的那份
checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔、live daemon 看不到。

    cd ~/RED && .venv/bin/python scripts/register_production_brief_tasks.py
    cd ~/RED && .venv/bin/python scripts/register_production_brief_tasks.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_production_brief_tasks.py --disable

排程：dispatcher 每 5 分鐘掃一次，任務靠 start_hour/end_hour + interval 決定何時
觸發。早上開 10-11 點視窗、下午開 15-16 點，interval 60 分 → 一個視窗內只會跑一次
（實際送出時間落在 10:00–10:05 / 15:00–15:05）。一個任務只能有一個視窗，所以早/晚
拆成兩支而不是一支。

寄送走 notify_emails：生管主管會收到「自己寄給自己」的一封信（網域委派冒充該地址寄信）。
production-mgr@company.example 的 gmail.send 委派已實測可用（2026-08-05）。
"""
from __future__ import annotations

import argparse
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

CHENFU = "production-mgr@company.example"      # 越南廠經理 / 生產管理部主管（gray）
_TOOL = "production_capacity_brief"

_SHARED = {
    "deterministic_tool": _TOOL,
    "interval_minutes": 60,
    "enabled": True,
    "notify_emails": [CHENFU],
    # notify_plain 對 notify_emails 這條路沒作用（信件本來就不包維運外殼），
    # 留著是為了萬一之後改推 Telegram 時語意一致。
    "notify_plain": True,
}

# 早上那封原本排 09:00，2026-08-06 往後移一小時：生管主管 08:00 已經收
# daily_production_8am（昨日產量）與 daily_production_alert（落後預警）兩封，09:00
# 再來一封等於一小時內三封。改 10:00 讓他先看完昨天的實績，再看「還能接多少單」這
# 個規劃視角。**不合併**進 daily_production_8am：那支是 LLM 產文、且同時寄給
# owner/gm/twsales，把確定性數字塞進去等於重新引入轉述風險又打擾另外三個人。
_TASKS = [
    {"name": "production_capacity_brief_am", "start_hour": 10, "end_hour": 11,
     "desc": "每天早上 10:00", "subject": "生產管理每日簡報 {date}（早）"},
    {"name": "production_capacity_brief_pm", "start_hour": 15, "end_hour": 16,
     "desc": "每天下午 15:00", "subject": "生產管理每日簡報 {date}（下午）"},
]


def _payload(spec: dict) -> dict:
    return {
        "name": spec["name"],
        "prompt": (
            f"{spec['desc']}寄生產管理每日簡報給生管主管經理（{CHENFU}）："
            "迪卡儂各形體剩餘材料可做雙數 + 針車/射出/包裝 每日產能折線圖。"
            f"本任務走 deterministic_tool（直接呼叫 {_TOOL}() 把回傳原樣寄出、"
            "不經 LLM），這段 prompt 只是給人看的說明，不會送進任何模型。"
        ),
        "start_hour": spec["start_hour"],
        "end_hour": spec["end_hour"],
        # 主旨給人看（收件人是同事，不是維運面）；{date} 由 dispatcher 在寄出時填。
        "email_subject": spec["subject"],
        **_SHARED,
    }


def build_tasks() -> list[dict]:
    return [_payload(spec) for spec in _TASKS]


# 只覆蓋「定義」欄位；last_run_at / run_count / dedup_hashes / last_error 是跑出來
# 的狀態，重跑這支腳本不該把它們洗掉（洗掉 dedup 會讓同一份報表再寄一次）。
_DEFINITION_KEYS = ("prompt", "deterministic_tool", "interval_minutes",
                    "start_hour", "end_hour", "enabled", "notify_emails",
                    "notify_plain", "email_subject")


def register(dry_run: bool = False, disable: bool = False) -> int:
    wanted = build_tasks()
    if disable:
        for task in wanted:
            task["enabled"] = False
    changes: list[str] = []

    def mutate(data: dict) -> None:
        tasks = data.setdefault("tasks", [])
        by_name = {t.get("name"): t for t in tasks if isinstance(t, dict)}
        for spec in wanted:
            existing = by_name.get(spec["name"])
            if existing is None:
                tasks.append(dict(spec))
                changes.append(f"+ 新增 {spec['name']}")
                continue
            diffs = [key for key in _DEFINITION_KEYS if existing.get(key) != spec[key]]
            if not diffs:
                changes.append(f"= {spec['name']}（無變更）")
                continue
            for key in _DEFINITION_KEYS:
                existing[key] = spec[key]
            changes.append(f"~ 更新 {spec['name']}：{', '.join(diffs)}")

    if dry_run:
        from agent_core.scheduler import _load_daemon_tasks
        mutate(_load_daemon_tasks())
    else:
        from agent_core.scheduler import update_daemon_tasks
        update_daemon_tasks(mutate)

    print("dry-run（未寫入）" if dry_run else "已寫入 daemon_tasks.json")
    for line in changes:
        print("  " + line)
    print("\n下一步：dispatcher 每 5 分鐘自動掃描，不必重啟 daemon。")
    print("要立刻試跑一支：跟小紅說「立刻執行排程 production_capacity_brief_am」。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只印出會做的變更，不寫檔")
    parser.add_argument("--disable", action="store_true",
                        help="註冊/更新但一律設 enabled=False（先掛著不跑）")
    args = parser.parse_args()
    return register(dry_run=args.dry_run, disable=args.disable)


if __name__ == "__main__":
    raise SystemExit(main())
