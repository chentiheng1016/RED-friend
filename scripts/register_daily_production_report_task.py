#!/usr/bin/env python3
"""把 daily_production_8am 改成走 deterministic_tool —— 整封信＝工具輸出，不經 Gemini。

為什麼要改（2026-08-07 大王問「本來沒有 NEW WAVE、為何又突然有了」查出來的）：
這封信的表格原本是 Gemini 讀完 read_production_progress_sheet 之後自由發揮產出的，
**同一份資料每輪產出都不一樣**。2026-08-07 早上四輪實測（讀的都是同一個
08月份生產日報進度表08-06.xlsx，數字完全相同）：

    10:09  只列 DECA / LURCHI / JALAS —— 整家 RICHTER 被漏掉（還有 426 雙未完）
    10:40  列 4 家（RICHTER 回來了）
    11:16  列 5 家（NEW WAVE 也進來了）
    11:47  列 5 家

多列一家已出完貨的是雜訊，漏列一家還在產的是漏報。而且 dispatcher 的去重是拿
**整段文字的 sha1** 比對（daemon_dispatcher.remember_dispatcher_result），LLM 每輪
重寫 → 雜湊永遠對不上 → 同一份資料一個上午寄四封給四個人（7/20、8/4 也是四封）。

改走 deterministic_tool 之後：表格由 daily_production_report() 排版，客戶清單、欄位、
數字每天固定；同一份資料重跑產出同一段文字 → 去重真的生效 → 一份資料只寄一封。
「ERP 交期核對」那段也一併確定性化：抽「日報上希望出貨日最早、且仍有未完」的前 3 張
（原本是 LLM 每輪自己挑，所以每封挑到的單都不一樣）。

同 usd_twd_rate_am/pm、production_capacity_brief_am/pm、erp_delivery_risk_daily 的做法
（dispatcher 的 ``run_deterministic_task``：直接呼叫工具、把回傳原樣當任務結果）。

原 prompt 的三選一行為原封搬進工具，沒有變：
  A 進度表已涵蓋昨天 → 回報表格
  B 還沒涵蓋、但不到 11:00 或昨天是週日 → 「(無新發現)」，dispatcher 安靜
  C 過了 11:00、昨天是工作日卻仍未涵蓋 → 催收提醒

⚠️ 這支任務是既有的（2026-07-01 建、跑了 260+ 輪），所以**只覆蓋 deterministic_tool
與 prompt 說明**：start_hour / end_hour / interval_minutes / notify_emails 一律沿用線上
設定，不動大王排好的送信時間與收件人。任務不存在時才用預設值新建。

可逆：轉換時把原本的 LLM prompt 原封存進 ``prompt_before_deterministic``，
``--revert`` 會把它放回 prompt 並拿掉 deterministic_tool（回到 Gemini 路徑）。

冪等：重跑只更新定義欄位，last_run_at / run_count / dedup_hashes / last_error 一律
保留（洗掉 dedup 會讓同一份報表再寄一次）。

⚠️ 要在**部署後的主 checkout**（~/RED）跑 —— DAEMON_TASKS_FILE 跟著執行的那份
checkout 走，在 worktree 裡跑只會寫到 worktree 自己的檔、live daemon 看不到。

    cd ~/RED && .venv/bin/python scripts/register_daily_production_report_task.py --dry-run
    cd ~/RED && .venv/bin/python scripts/register_daily_production_report_task.py
    cd ~/RED && .venv/bin/python scripts/register_daily_production_report_task.py --revert

改完不必重啟 daemon：dispatcher 是 StartInterval 300 的短命 process，每 5 分鐘重開
一次、每次重新讀 daemon_tasks.json 也重新 import 程式碼。
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

_TASK = "daily_production_8am"
_TOOL = "daily_production_report"
_STASH_KEY = "prompt_before_deterministic"

# 這段 prompt 不會送進任何模型（run_one_dispatcher_task 開頭就轉去
# run_deterministic_task），純粹是給翻 daemon_tasks.json 的人看的說明。
_PROMPT = (
    f"每天早上把昨天各客戶的生產數量回報給大王與相關同仁。本任務走 deterministic_tool"
    f"（直接呼叫 {_TOOL}() 把回傳原樣寄出、不經 LLM），這段 prompt 只是給人看的說明，"
    "不會送進任何模型。工具行為：進度表已涵蓋昨天→回報表格（昨天各站雙數＋本季已包裝"
    "累計/未完＋ERP 交期核對）；還沒涵蓋但不到 11:00 或昨天是週日→回「(無新發現)」讓"
    "dispatcher 安靜；過了 11:00 而昨天是工作日卻仍未涵蓋→回催收提醒。"
    "本季已出完貨（在產 0、未完 0）的客戶不列入，只在表下報家數。"
)

# 任務不存在才用的預設值：沿用線上那支的視窗（08–12 點、每 30 分掃一次，生管什麼
# 時候更新進度表就什麼時候發第一封；內容相同的後續輪次會被 dedup 擋掉）。
_CREATE_DEFAULTS = {
    "interval_minutes": 30,
    "start_hour": 8,
    "end_hour": 12,
    "enabled": True,
    "notify_emails": ["owner@company.example", "gm@company.example",
                      "twsales@company.example", "production-mgr@company.example"],
}


def _check_tool_reachable() -> str:
    """deterministic_tool 必須進得了 safe_tools —— run_deterministic_task 從那裡解析。

    回空字串＝沒問題。import 不起來（缺依賴／機器沒裝好）只回警告、不擋寫入。
    """
    try:
        from agent_core.daemon_dispatcher import safe_tools
        from agent_core.tool_registry import tools_list
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 無法驗證工具可達性（{type(exc).__name__}: {exc}）—— 跳過檢查。"
    names = {getattr(t, "__name__", "") for t in safe_tools(tools_list)}
    if _TOOL not in names:
        return (f"❌ {_TOOL} 不在背景唯讀工具集裡，設成 deterministic_tool 會每輪失敗。"
                f"\n   修法：agent_core/daemon_dispatcher.py 的 _SAFE_TOOL_NAMES 要有"
                f" \"{_TOOL}\"（它是 builtin，拿不到 skills 那邊的 background_safe 旗標）。")
    return ""


def _schedule_line(task: dict) -> str:
    return (f"視窗 {int(task.get('start_hour', 0)):02d}:00-{int(task.get('end_hour', 24)):02d}:00"
            f"／間隔 {task.get('interval_minutes')} 分"
            f"／enabled={task.get('enabled', True)}"
            f"／notify_emails={task.get('notify_emails') or '（預設 owner email）'}")


def _to_deterministic(task: dict) -> list[str]:
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


def register(*, dry_run: bool = False, revert: bool = False) -> int:
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
            tasks.append(task)
            report.append(f"+ 新增 {_TASK}（線上原本沒有這支任務）")
            report.append(f"  排程：{_schedule_line(task)}")
            return
        report.append(f"排程（沿用線上設定）：{_schedule_line(task)}")
        changes = _revert(task) if revert else _to_deterministic(task)
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
    args = parser.parse_args()
    return register(dry_run=args.dry_run, revert=args.revert)


if __name__ == "__main__":
    raise SystemExit(main())
