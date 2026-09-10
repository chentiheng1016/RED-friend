"""Workflow State Machine（T2）— 讓 skill 從單一函式升級成多步驟可 resume 流程。

真正 RPA 平台的核心能力之一。解決的問題：
  - 跑 20 步的流程，第 15 步死掉 → 前面 14 步白做
  - 某一步偶爾會 flaky → 要能 retry 不用整個重跑
  - 單步失敗 → 想走 fallback 分支，不要整條 workflow 爛掉
  - 跑到一半人介入 → 能從 checkpoint 繼續

設計：
  workflow 就是**一支普通的 Python 函式**（不改 skill 寫法的自由度），
  步驟透過 step(name, fn, *args, retry=..., on_fail=..., **kwargs) 包起來。
  失敗時 state 寫到 workflows/{run_id}/state.json，之後可以查 / 手動 resume。

使用範例：
    from agent_core.workflow import workflow, step

    @workflow(name="月度發票整理")
    def process_monthly_invoices(month: str):
        files = step("下載發票", download_from_gmail, month, retry=3)
        records = step("OCR 辨識", ocr_all, files, retry=2, backoff="exponential")
        return step("寫入 Excel", write_report, records,
                    on_fail=lambda e, ctx: notify_human(f"OCR 都失敗了：{e}"))

每次呼叫 process_monthly_invoices("2026-04") → run_history + workflows/ 都有紀錄。
失敗時 show_workflow_run(run_id) 能看到哪一步、第幾次 retry、前後值。
"""
from __future__ import annotations

import functools
import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agent_core.logging_and_paths import WORKFLOWS_DIR, logger
from agent_core.run_history import audited as _audited

# ────────────────────────────────────────────────────────────────────
# Thread-local state — step() 透過它找到當前 workflow
# ────────────────────────────────────────────────────────────────────
_tls = threading.local()


def _current_run() -> "WorkflowRun | None":
    return getattr(_tls, "run", None)


def _set_current_run(run: "WorkflowRun | None"):
    _tls.run = run


# ────────────────────────────────────────────────────────────────────
# Workflow run state — 被 @workflow 建立、被 step() 更新
# ────────────────────────────────────────────────────────────────────
class WorkflowRun:
    def __init__(self, workflow_name: str, args: tuple, kwargs: dict):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        self.run_id = f"{ts}_{workflow_name}_{suffix}"
        self.workflow_name = workflow_name
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.args_repr = _safe_repr(args)
        self.kwargs_repr = _safe_repr(kwargs)
        self.status = "running"
        self.steps: list[dict] = []
        self.ended_at: str | None = None
        self.final_result_repr: str | None = None
        self.error: str | None = None
        self.run_dir = os.path.join(WORKFLOWS_DIR, self.run_id)
        os.makedirs(self.run_dir, exist_ok=True)
        self._save()

    def _save(self):
        state = {
            "run_id": self.run_id,
            "workflow_name": self.workflow_name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "args": self.args_repr,
            "kwargs": self.kwargs_repr,
            "status": self.status,
            "final_result": self.final_result_repr,
            "error": self.error,
            "step_count": len(self.steps),
            "steps": self.steps,
        }
        path = os.path.join(self.run_dir, "state.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.warning("workflow 寫 state 失敗：%s", e)

    def record_step(self, record: dict):
        self.steps.append(record)
        self._save()

    def finish(self, status: str, result=None, error: str | None = None):
        self.status = status
        self.ended_at = datetime.now().isoformat(timespec="seconds")
        if result is not None:
            self.final_result_repr = _safe_repr(result)
        if error is not None:
            self.error = error
        self._save()


def _safe_repr(obj: Any, limit: int = 1500) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = repr(obj)
    return s[:limit] + (f"...(truncated, {len(s)} chars total)" if len(s) > limit else "")


# ────────────────────────────────────────────────────────────────────
# step() — 在 @workflow 內呼叫；外面呼叫會回傳普通執行結果（degrade gracefully）
# ────────────────────────────────────────────────────────────────────
def step(name: str, fn: Callable, *args,
         retry: int = 1, backoff: str = "none",
         on_fail: Callable | None = None,
         **kwargs) -> Any:
    """把一個 function call 包成 workflow 的一個 step。

    有效情境：被 @workflow 裝飾的函式在執行中 → 記錄到 workflow state。
    Fallback：沒有 active workflow → 直接呼叫 fn（方便本地測試）。

    Args:
        name: step 名稱（顯示、查詢、除錯用）。
        fn: 要執行的 function。
        *args, **kwargs: 傳給 fn 的參數。
        retry: 失敗時最多重試幾次（預設 1 = 不 retry）。
        backoff: 重試間隔策略：
                 - "none"（預設）：每次間隔 1s
                 - "linear"：1s, 2s, 3s, ...
                 - "exponential"：1s, 2s, 4s, 8s, ...
        on_fail: 所有 retry 都失敗後呼叫；簽名 on_fail(exception, step_context_dict) -> Any。
                 回傳值當作 step 結果；None 時 step 算 failed，workflow 會丟 exception 出去。

    Returns:
        fn 的回傳值（成功），或 on_fail 的回傳值（on_fail 處理成功）。

    Raises:
        fn 最後一次的 exception（沒 on_fail 或 on_fail 也 raise）。
    """
    run = _current_run()
    step_no = (len(run.steps) + 1) if run else 0
    record = {
        "step_no": step_no,
        "name": name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "fn": getattr(fn, "__name__", str(fn)),
        "args": _safe_repr(args),
        "kwargs": _safe_repr(kwargs),
        "attempts": 0,
        "status": "running",
    }

    last_err = None
    for attempt in range(1, retry + 1):
        record["attempts"] = attempt
        t0 = time.time()
        try:
            result = fn(*args, **kwargs)
            record["status"] = "success"
            record["result"] = _safe_repr(result)
            record["elapsed_sec"] = round(time.time() - t0, 3)
            record["ended_at"] = datetime.now().isoformat(timespec="seconds")
            if run:
                run.record_step(record)
            return result
        except Exception as e:
            last_err = e
            record["last_error"] = f"{type(e).__name__}: {e}"
            logger.info(
                "[workflow] step '%s' 嘗試 %d/%d 失敗：%s",
                name, attempt, retry, e,
            )
            if attempt < retry:
                wait = _compute_backoff(backoff, attempt)
                time.sleep(wait)
                continue

    # 全部 retry 都失敗 → on_fail 救一次；再不行就 raise
    record["elapsed_sec"] = round(time.time() - t0, 3)
    record["ended_at"] = datetime.now().isoformat(timespec="seconds")
    if on_fail:
        try:
            fallback_result = on_fail(last_err, record)
            record["status"] = "recovered_by_on_fail"
            record["result"] = _safe_repr(fallback_result)
            if run:
                run.record_step(record)
            return fallback_result
        except Exception as handler_err:
            record["on_fail_error"] = f"{type(handler_err).__name__}: {handler_err}"

    record["status"] = "failed"
    record["traceback"] = traceback.format_exc()[-1500:]
    if run:
        run.record_step(record)
    raise last_err


def _compute_backoff(strategy: str, attempt: int) -> float:
    strategy = (strategy or "none").lower()
    if strategy == "exponential":
        return min(60, 2 ** (attempt - 1))  # 1, 2, 4, 8, 16, 32, 60
    if strategy == "linear":
        return min(30, attempt)
    return 1.0  # "none" 也至少等 1s 避免打爆


# ────────────────────────────────────────────────────────────────────
# @workflow 裝飾器
# ────────────────────────────────────────────────────────────────────
_REGISTERED_WORKFLOWS: dict[str, dict] = {}


def workflow(name: str, description: str = ""):
    """把 function 標記為 workflow（會有 run_id + state persistence + run_history audit）。

    用法：
        @workflow(name="月度發票整理", description="每月對帳 → Excel")
        def process_monthly_invoices(month: str):
            files = step("下載", download, month, retry=3)
            ...

    Args:
        name: workflow 顯示名稱（中英都可，小紅會用這個叫）。
        description: 可選，給 LLM 看的說明（docstring 也會用）。
    """
    def deco(fn: Callable):
        # 記錄這個 workflow 存在（list_workflows 會顯示）
        _REGISTERED_WORKFLOWS[name] = {
            "function_name": fn.__name__,
            "module": fn.__module__,
            "description": description or (fn.__doc__ or "").strip().split("\n")[0][:200],
            "signature": str(_get_signature(fn)),
        }

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            run = WorkflowRun(workflow_name=name, args=args, kwargs=kwargs)
            _set_current_run(run)
            try:
                result = fn(*args, **kwargs)
                run.finish("success", result=result)
                return result
            except Exception as e:
                run.finish("failed", error=f"{type(e).__name__}: {e}")
                raise
            finally:
                _set_current_run(None)

        wrapper._is_workflow = True
        wrapper._workflow_name = name
        # 同時 audit：workflow 整條執行也進 run_history
        return _audited(capture_screen=False)(wrapper)

    return deco


def _get_signature(fn):
    import inspect
    try:
        return inspect.signature(fn)
    except Exception:
        return "(?)"


# ────────────────────────────────────────────────────────────────────
# 查詢 tools — 暴露給 agent
# ────────────────────────────────────────────────────────────────────
def list_workflows() -> str:
    """列出已註冊的 workflow（被 @workflow 裝飾過的）。"""
    if not _REGISTERED_WORKFLOWS:
        return ("（還沒有任何 workflow 註冊）\n"
                "寫 workflow：在 skill 檔裡 @workflow(name=\"xxx\") 裝飾函式。")
    lines = [f"📋 已註冊的 Workflows（{len(_REGISTERED_WORKFLOWS)} 個）："]
    for name, info in _REGISTERED_WORKFLOWS.items():
        lines.append(f"  • {name}  ({info['function_name']})")
        if info["description"]:
            lines.append(f"      {info['description']}")
        lines.append(f"      signature: {info['function_name']}{info['signature']}")
    return "\n".join(lines)


def list_workflow_runs(workflow_name: str = "", status: str = "",
                       since_hours: int = 168, limit: int = 20) -> str:
    """列最近的 workflow 執行記錄。

    Args:
        workflow_name: 可選，只看特定 workflow；空字串 = 全部。
        status: 可選 "success"/"failed"/"running"；空字串 = 全部。
        since_hours: 看最近幾小時內（預設 168 = 一週）。
        limit: 最多回幾筆。
    """
    if not os.path.isdir(WORKFLOWS_DIR):
        return "（還沒有任何 workflow 執行過）"

    cutoff = datetime.now() - timedelta(hours=since_hours)
    runs = []
    for d in os.listdir(WORKFLOWS_DIR):
        state_path = os.path.join(WORKFLOWS_DIR, d, "state.json")
        if not os.path.isfile(state_path):
            continue
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            continue
        if workflow_name and s.get("workflow_name") != workflow_name:
            continue
        if status and s.get("status") != status:
            continue
        try:
            t = datetime.fromisoformat(s.get("started_at", ""))
            if t < cutoff:
                continue
        except (ValueError, TypeError):
            pass
        runs.append(s)

    if not runs:
        return f"🔍 最近 {since_hours}h 沒符合條件的 workflow run"

    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    runs = runs[:limit]

    lines = [f"📋 最近 {len(runs)} 筆 workflow run（新到舊）"]
    lines.append("-" * 60)
    for r in runs:
        icon = {"success": "✅", "failed": "❌", "running": "⏳"}.get(r["status"], "?")
        lines.append(f"{icon} {r['run_id']}")
        lines.append(f"   {r['workflow_name']}  |  {r['started_at']}  |  {r['step_count']} steps")
        if r["status"] == "failed":
            lines.append(f"   ⚠️ error: {(r.get('error') or '')[:120]}")
    return "\n".join(lines)


def show_workflow_run(run_id: str) -> str:
    """顯示某個 workflow run 的每個 step 狀態與 retry 次數。

    Args:
        run_id: 由 list_workflow_runs 回的 id。
    """
    state_path = os.path.join(WORKFLOWS_DIR, run_id, "state.json")
    if not os.path.isfile(state_path):
        return f"❌ 找不到 workflow run: {run_id}"

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception as e:
        return f"❌ 讀 state 失敗: {e}"

    lines = [
        f"📋 Workflow run {s['run_id']}",
        "=" * 60,
        f"workflow:    {s['workflow_name']}",
        f"status:      {s['status']}",
        f"started:     {s['started_at']}",
        f"ended:       {s.get('ended_at', '—')}",
        f"args:        {s.get('args', '')}",
        f"kwargs:      {s.get('kwargs', '')}",
        "",
        f"--- Steps ({s['step_count']} 個) ---",
    ]
    for i, st in enumerate(s.get("steps") or [], 1):
        icon = {
            "success": "✅",
            "recovered_by_on_fail": "🩹",
            "failed": "❌",
            "running": "⏳",
        }.get(st["status"], "?")
        retry_info = f"（嘗試 {st['attempts']} 次）" if st["attempts"] > 1 else ""
        lines.append(f"{icon} [{i}] {st['name']}  {retry_info}  {st.get('elapsed_sec', '?')}s")
        lines.append(f"      fn={st['fn']}  args={st['args'][:80]}")
        if st["status"] == "success":
            lines.append(f"      → {st.get('result', '')[:120]}")
        elif st["status"] == "recovered_by_on_fail":
            lines.append(f"      ⚠️ {st.get('last_error', '')[:100]}")
            lines.append(f"      🩹 on_fail → {st.get('result', '')[:120]}")
        elif st["status"] == "failed":
            lines.append(f"      ❌ {st.get('last_error', '')[:150]}")

    if s["status"] == "success":
        lines.append("")
        lines.append(f"final_result: {(s.get('final_result') or '')[:300]}")
    elif s["status"] == "failed":
        lines.append("")
        lines.append(f"workflow error: {s.get('error', '')}")
    return "\n".join(lines)


def workflow_stats(since_hours: int = 168) -> str:
    """workflow 統計：最近 N 小時的執行次數、成功率、平均步數、常失敗 step。"""
    if not os.path.isdir(WORKFLOWS_DIR):
        return "（還沒有任何 workflow 執行過）"

    cutoff = datetime.now() - timedelta(hours=since_hours)
    from collections import Counter
    total = 0
    ok = 0
    failed = 0
    wf_counter = Counter()
    failed_steps = Counter()
    total_steps = 0

    for d in os.listdir(WORKFLOWS_DIR):
        state_path = os.path.join(WORKFLOWS_DIR, d, "state.json")
        if not os.path.isfile(state_path):
            continue
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            continue
        try:
            t = datetime.fromisoformat(s.get("started_at", ""))
            if t < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        total += 1
        wf_counter[s.get("workflow_name", "?")] += 1
        if s.get("status") == "success":
            ok += 1
        elif s.get("status") == "failed":
            failed += 1
            for st in s.get("steps") or []:
                if st.get("status") == "failed":
                    failed_steps[f"{s['workflow_name']}.{st['name']}"] += 1
        total_steps += s.get("step_count", 0)

    if total == 0:
        return f"🔍 最近 {since_hours}h 沒有 workflow 執行過"

    pct = (ok * 100 / total) if total else 0
    avg_steps = (total_steps / total) if total else 0
    lines = [
        f"📊 Workflow 統計（最近 {since_hours}h）",
        f"總執行: {total}",
        f"成功: {ok}（{pct:.1f}%）/ 失敗: {failed}",
        f"平均步數: {avg_steps:.1f}",
        "",
    ]
    if wf_counter:
        lines.append("🔝 最常跑的 workflow：")
        for name, n in wf_counter.most_common(5):
            lines.append(f"  {n:3d}x  {name}")
    if failed_steps:
        lines.append("")
        lines.append("💥 最常失敗的 step：")
        for name, n in failed_steps.most_common(5):
            lines.append(f"  {n:3d}x  {name}")
    return "\n".join(lines)
