#!/usr/bin/env python3
"""把 erp_delivery_risk_daily 改成走 deterministic_tool —— 整封信＝工具輸出，不經 Gemini。

為什麼要改（2026-08-06 大王反映）：交期風險表原本走 dispatcher 的 LLM 路徑
（工具輸出 → Gemini 轉述 → 寄信）。工具本身已經自給自足（已包裝/未完逐單照生管
日報念、來源逐列標明），再讓模型轉述一次只有壞處：
  1. 數字有被改寫的空間（〈員工零幻覺〉在固定數字報表上最好的做法就是別讓 LLM 碰）
  2. 模型會自己補「要精準看某單進度可再問我」這種邀請 —— 收信的人因此要再問一次，
     而該問的資訊其實已經在表上了
  3. 每天白燒一次 Gemini quota

同 usd_twd_rate_am/pm 與 production_capacity_brief_am/pm 的做法（dispatcher 的
``run_deterministic_task``：直接呼叫工具、把回傳原樣當任務結果）。

⚠️ 這支任務是既有的、不是這支腳本建的，所以**只覆蓋 deterministic_tool 與 prompt
說明**：start_hour / end_hour / interval_minutes / notify_* 一律沿用線上設定，不動
大王排好的送信時間（要改請用下面的 --start-hour/--end-hour/--interval）。任務不存在
時才用預設值（每天 08:00 那個視窗）新建。

可逆：轉換時把原本的 LLM prompt 原封不動存進 ``prompt_before_deterministic``，
``--revert`` 會把它放回 prompt 並拿掉 deterministic_tool（回到 Gemini 路徑）。

冪等：重跑只會更新定義欄位，last_run_at / run_count / dedup_hashes / last_error
這些跑出來的狀態一律保留（洗掉 dedup 會讓同一份報表再寄一次）。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 跟著執行的那份
checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔、live daemon 看不到。

    cd ~/RED && .venv/bin/python scripts/register_erp_delivery_risk_task.py
    cd ~/RED && .venv/bin/python scripts/register_erp_delivery_risk_task.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_erp_delivery_risk_task.py --revert
    cd ~/RED && .venv/bin/python scripts/register_erp_delivery_risk_task.py \
        --start-hour 8 --end-hour 9 --interval 60      # 順手把送信視窗釘死

改完不必重啟 daemon：dispatcher 是 StartInterval 300 的短命 process，每 5 分鐘
重開一次、每次重新讀 daemon_tasks.json 也重新 import 程式碼。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core.scheduler import (  # noqa: E402
    DAEMON_TASKS_FILE,
    _load_daemon_tasks,
    update_daemon_tasks,
)

_TASK = "erp_delivery_risk_daily"
_TOOL = "erp_delivery_risk_alert"
_STASH_KEY = "prompt_before_deterministic"

# 這段 prompt 不會送進任何模型（run_one_dispatcher_task 一開頭就轉去
# run_deterministic_task），純粹是給翻 daemon_tasks.json 的人看的說明。
_PROMPT = (
    f"每天寄 ERP 交期風險表給大王。本任務走 deterministic_tool（直接呼叫 {_TOOL}() "
    "把回傳原樣寄出、不經 LLM），這段 prompt 只是給人看的說明，不會送進任何模型。"
    "表上「已包裝/未完」逐單照生管日報念、來源逐列標明；生管日報讀不到才退回 ERP "
    "包裝流水粗估（該列標 ≈）。沒有風險單時工具回「(無新發現)」，dispatcher 會安靜。"
)

# 任務不存在才用的預設視窗：視窗寬度 = interval → 一個視窗內只觸發一次
# （dispatcher 每 5 分鐘掃一次，實際送出落在 08:00–08:05）。同 usd_twd_rate_am。
_CREATE_DEFAULTS = {
    "interval_minutes": 60,
    "start_hour": 8,
    "end_hour": 9,
    "enabled": True,
    "notify_emails": [],      # 空清單 = 走預設的 owner email 通知
}


def _check_tool_reachable() -> str:
    """deterministic_tool 必須進得了 safe_tools —— run_deterministic_task 從那裡解析。

    回空字串＝沒問題；回字串＝錯誤訊息。import 不起來（缺依賴/在沒裝好的機器上跑）
    只回警告字串讓呼叫端印出來，不擋寫入。
    """
    try:
        from agent_core.daemon_dispatcher import safe_tools
        from agent_core.tool_registry import tools_list
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 無法驗證工具可達性（{type(exc).__name__}: {exc}）—— 跳過檢查。"
    names = {getattr(t, "__name__", "") for t in safe_tools(tools_list)}
    if _TOOL not in names:
        return (f"❌ {_TOOL} 不在背景唯讀工具集裡，設成 deterministic_tool 會每輪失敗。"
                f"\n   修法：skills/erp_warehouse.py 尾巴要有 `{_TOOL}.background_safe = True`。")
    return ""


def _schedule_line(task: dict) -> str:
    return (f"視窗 {int(task.get('start_hour', 0)):02d}:00-{int(task.get('end_hour', 24)):02d}:00"
            f"／間隔 {task.get('interval_minutes')} 分"
            f"／enabled={task.get('enabled', True)}"
            f"／notify_emails={task.get('notify_emails') or '（預設 owner email）'}")


def _apply_schedule_overrides(task: dict, args: argparse.Namespace) -> list[str]:
    """--start-hour/--end-hour/--interval 有給才動，回變更說明。"""
    changed = []
    for key, val in (("start_hour", args.start_hour), ("end_hour", args.end_hour),
                     ("interval_minutes", args.interval)):
        if val is not None and task.get(key) != val:
            changed.append(f"{key}: {task.get(key)} → {val}")
            task[key] = val
    return changed


def _to_deterministic(task: dict, args: argparse.Namespace) -> list[str]:
    changes = []
    if task.get("deterministic_tool") != _TOOL:
        changes.append(f"deterministic_tool: {task.get('deterministic_tool')!r} → {_TOOL!r}")
        task["deterministic_tool"] = _TOOL
    old = str(task.get("prompt") or "")
    # 只在第一次轉換時收原 prompt；重跑不可以把說明文字覆蓋進 stash（那就回不去了）。
    if _STASH_KEY not in task and old and old != _PROMPT:
        task[_STASH_KEY] = old
        changes.append(f"原 prompt 已備份進 {_STASH_KEY}（{len(old)} 字）")
    if old != _PROMPT:
        task["prompt"] = _PROMPT
        changes.append("prompt → 改成人看的說明（不進模型）")
    changes += _apply_schedule_overrides(task, args)
    return changes


def _revert(task: dict) -> list[str]:
    changes = []
    if task.pop("deterministic_tool", None):
        changes.append("拿掉 deterministic_tool（改回 Gemini 路徑）")
    stashed = task.pop(_STASH_KEY, "")
    if stashed:
        task["prompt"] = stashed
        changes.append(f"prompt ← 還原自 {_STASH_KEY}（{len(stashed)} 字）")
    elif task.get("prompt") == _PROMPT:
        changes.append("⚠️ 沒有備份的原 prompt，prompt 仍是說明文字 —— 改回 LLM 路徑前要自己補")
    return changes


def register(*, dry_run: bool = False, revert: bool = False,
             args: argparse.Namespace) -> int:
    warn = "" if revert else _check_tool_reachable()
    if warn.startswith("❌"):
        print(warn)
        return 1
    if warn:
        print(warn)

    report: list[str] = []

    def mutate(data: dict) -> None:
        tasks = data.setdefault("tasks", [])
        task = next((t for t in tasks
                     if isinstance(t, dict) and t.get("name") == _TASK), None)
        if task is None:
            if revert:
                report.append(f"找不到任務「{_TASK}」，沒東西可還原。")
                return
            task = {"name": _TASK, "prompt": _PROMPT, "deterministic_tool": _TOOL,
                    **_CREATE_DEFAULTS}
            _apply_schedule_overrides(task, args)
            tasks.append(task)
            report.append(f"+ 新增 {_TASK}（線上原本沒有這支任務）")
            report.append(f"  排程：{_schedule_line(task)}")
            return
        report.append(f"排程（沿用線上設定）：{_schedule_line(task)}")
        changes = _revert(task) if revert else _to_deterministic(task, args)
        if not changes:
            report.append(f"= {_TASK}（無變更）")
            return
        report.append(f"~ 更新 {_TASK}：")
        # extend 不是 += —— 閉包裡對 report 做增量賦值會把它變成 mutate 的區域變數。
        report.extend(f"    {c}" for c in changes)

    if dry_run:
        mutate(_load_daemon_tasks())
    else:
        update_daemon_tasks(mutate)

    print(f"檔案：{DAEMON_TASKS_FILE}")
    print("dry-run（未寫入）" if dry_run else "已寫入 daemon_tasks.json")
    for line in report:
        print("  " + line)
    print("\n下一步：dispatcher 每 5 分鐘自動掃描，不必重啟 daemon。")
    print(f"要立刻試跑：跟小紅說「立刻執行排程 {_TASK}」。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只印出會做的變更，不寫檔")
    parser.add_argument("--revert", action="store_true",
                        help="改回 Gemini 路徑（還原備份的原 prompt、拿掉 deterministic_tool）")
    parser.add_argument("--start-hour", type=int, default=None, help="選填，改送信視窗起點")
    parser.add_argument("--end-hour", type=int, default=None, help="選填，改送信視窗終點")
    parser.add_argument("--interval", type=int, default=None, help="選填，改 interval_minutes")
    args = parser.parse_args()
    if args.start_hour is not None and args.end_hour is not None \
            and args.end_hour <= args.start_hour:
        parser.error("--end-hour 必須大於 --start-hour")
    return register(dry_run=args.dry_run, revert=args.revert, args=args)


if __name__ == "__main__":
    raise SystemExit(main())
