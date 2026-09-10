"""Tool metrics — 從 runs/index.jsonl 算 tool-level success rate / latency / failure 分佈。

問題：
  run_history.py 紀錄每筆 call，但**沒人聚合成指標**。dashboard 各 section
  各自掃 file，沒統一 metrics API。「上週哪個 tool 失敗最多」、「哪個 tool
  變慢了」要靠肉眼撈 audit log。

設計：
  純讀層 — 從 runs/index.jsonl tail 讀後 N 筆，groupby tool 算統計。
  不寫狀態，不啟 background thread。給 dashboard / status_center / API caller
  共用。

  index.jsonl shape（已有 / 新欄位 best-effort）：
    id, tool, started_at, ended_at, status, elapsed_sec, short_result
    [ok, error_code, recoverable]  ← ToolResult 之後寫的才有

  metrics 計算（tool-level）：
    calls       總次數
    success     status='success' AND ok != False
    error       status='error' OR ok == False
    success_pct = success / calls
    avg_latency average elapsed_sec
    p95_latency 95% 分位（粗算 — 排序後取 index）
    last_error  最近一次 error 的 short_result（過 redact）
    error_codes Counter of error_code（從新欄位）

效能：
  index.jsonl 通常 < 100k 行（runs 約 1 個月後才到這量），整檔讀進記憶體
  即可。caller 給 hours 限制窗，再過濾 → 剩可能 < 1k 行。實測 < 10ms。

  若日後規模上來（>100k 行），應該分檔（按月切）或上 sqlite。先簡單做。
"""
from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import Any

from agent_core.logging_and_paths import RUNS_DIR, logger

_PG_METRICS_WARNING_UNTIL = 0.0


def _warn_pg_metrics_fallback(exc: Exception) -> None:
    global _PG_METRICS_WARNING_UNTIL
    import time

    now = time.monotonic()
    if now < _PG_METRICS_WARNING_UNTIL:
        return
    _PG_METRICS_WARNING_UNTIL = now + 30
    logger.warning("Postgres metrics source failed; falling back to run index: %s", exc)


def _pg_run_store():
    try:
        from agent_core import operational_run_history as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - metrics must stay best-effort
        _warn_pg_metrics_fallback(exc)
    return None


# ────────────────────────────────────────────────────────────────────
# 載入 raw runs（共用 helper）
# ────────────────────────────────────────────────────────────────────
def _load_runs_in_window(hours: int) -> list[dict]:
    """讀 runs/index.jsonl 過去 hours 小時的 entry，已 parsed。

    從檔尾倒讀，遇到 started_at < cutoff 即停（檔尾是最新）。
    """
    store = _pg_run_store()
    if store is not None:
        try:
            return store.load_metrics_entries(hours=hours)
        except Exception as exc:  # noqa: BLE001 - fall back to local index
            _warn_pg_metrics_fallback(exc)

    path = os.path.join(RUNS_DIR, "index.jsonl")
    if not os.path.isfile(path):
        return []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except (ValueError, TypeError):
            continue
        started = r.get("started_at") or ""
        if started and started < cutoff:
            break
        out.append(r)
    out.reverse()  # 最舊→最新
    return out


def _is_error(record: dict) -> bool:
    """區分一筆 run 是不是錯誤（兼容舊 / 新 shape）。"""
    if record.get("ok") is False:
        return True
    if record.get("status") == "error":
        return True
    return False


def _percentile(values: list[float], pct: float) -> float:
    """簡單 percentile（不依賴 numpy）。"""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(sorted_v) - 1)
    if f == c:
        return sorted_v[f]
    return sorted_v[f] + (sorted_v[c] - sorted_v[f]) * (k - f)


# ────────────────────────────────────────────────────────────────────
# Per-tool metrics
# ────────────────────────────────────────────────────────────────────
def tool_metrics(hours: int = 24, min_calls: int = 1) -> list[dict]:
    """每個 tool 的指標（過去 N 小時）。

    Returns list of dict, sorted by calls desc:
      {tool, calls, success, error, success_pct, avg_latency_sec,
       p95_latency_sec, last_error_at, last_error, error_codes}
    """
    rows = _load_runs_in_window(hours)
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        tool = r.get("tool", "?")
        if tool:
            by_tool[tool].append(r)

    out = []
    for tool, calls in by_tool.items():
        if len(calls) < min_calls:
            continue
        successes = sum(1 for r in calls if not _is_error(r))
        errors = len(calls) - successes
        latencies = []
        for r in calls:
            try:
                latencies.append(float(r.get("elapsed_sec") or 0))
            except (TypeError, ValueError):
                pass
        # 取最近一個 error
        last_err = None
        last_err_at = None
        for r in reversed(calls):
            if _is_error(r):
                last_err = (r.get("short_result") or "")[:120]
                last_err_at = r.get("started_at") or ""
                break
        # error code 分佈
        ec = Counter()
        for r in calls:
            if _is_error(r):
                code = r.get("error_code") or "(unclassified)"
                ec[code] += 1
        out.append({
            "tool": tool,
            "calls": len(calls),
            "success": successes,
            "error": errors,
            "success_pct": round(100.0 * successes / len(calls), 1) if calls else 0.0,
            "avg_latency_sec": round(statistics.mean(latencies), 3) if latencies else 0.0,
            "p95_latency_sec": round(_percentile(latencies, 95), 3),
            "last_error_at": last_err_at,
            "last_error": last_err,
            "error_codes": dict(ec),
        })
    out.sort(key=lambda d: -d["calls"])
    return out


# ────────────────────────────────────────────────────────────────────
# Top-N rankings — 失敗多 / 慢 / 用得多
# ────────────────────────────────────────────────────────────────────
def tool_top_failures(hours: int = 24, top_n: int = 5) -> list[dict]:
    """錯誤最多的 top-N tool。"""
    metrics = tool_metrics(hours=hours)
    metrics = [m for m in metrics if m["error"] > 0]
    metrics.sort(key=lambda m: (-m["error"], -m["calls"]))
    return metrics[:top_n]


def tool_top_slow(hours: int = 24, top_n: int = 5,
                  min_calls: int = 3) -> list[dict]:
    """p95 latency 最高的 top-N tool（要至少 min_calls 次才上榜，避免單筆極端值騙人）。"""
    metrics = tool_metrics(hours=hours, min_calls=min_calls)
    metrics.sort(key=lambda m: -m["p95_latency_sec"])
    return metrics[:top_n]


def tool_top_usage(hours: int = 24, top_n: int = 10) -> list[dict]:
    """call 次數最多的 top-N。"""
    return tool_metrics(hours=hours)[:top_n]


# ────────────────────────────────────────────────────────────────────
# Aggregate （給 status_center / dashboard 用）
# ────────────────────────────────────────────────────────────────────
def metrics_summary(hours: int = 24) -> dict:
    """全系統 metrics 摘要 — 一個 dict 全部給。"""
    rows = _load_runs_in_window(hours)
    if not rows:
        return {
            "window_hours": hours, "total_calls": 0,
            "total_success": 0, "total_error": 0,
            "success_pct": 100.0,
            "tools_with_calls": 0,
            "avg_latency_sec": 0.0,
            "error_codes": {},
        }
    total = len(rows)
    errors = sum(1 for r in rows if _is_error(r))
    successes = total - errors
    latencies = []
    for r in rows:
        try:
            latencies.append(float(r.get("elapsed_sec") or 0))
        except (TypeError, ValueError):
            pass
    by_tool: dict[str, int] = defaultdict(int)
    error_codes: Counter = Counter()
    for r in rows:
        by_tool[r.get("tool", "?")] += 1
        if _is_error(r):
            error_codes[r.get("error_code") or "(unclassified)"] += 1
    return {
        "window_hours": hours,
        "total_calls": total,
        "total_success": successes,
        "total_error": errors,
        "success_pct": round(100.0 * successes / total, 1),
        "tools_with_calls": len(by_tool),
        "avg_latency_sec": round(statistics.mean(latencies), 3) if latencies else 0.0,
        "p95_latency_sec": round(_percentile(latencies, 95), 3),
        "error_codes": dict(error_codes),
    }


# ────────────────────────────────────────────────────────────────────
# Public LLM-facing tools — 可讀 formatted output
# ────────────────────────────────────────────────────────────────────
def metrics_overview(hours: int = 24) -> str:
    """🟢 全系統 metrics 摘要 + top failure / slow / usage 排行榜。"""
    s = metrics_summary(hours)
    if s["total_calls"] == 0:
        return f"  （過去 {hours}h 沒有 audited tool 跑過）"
    out = [f"📊 系統 metrics — 過去 {hours}h"]
    out.append("─" * 60)
    out.append(f"  總 call 數     : {s['total_calls']}")
    out.append(f"  成功 / 失敗    : {s['total_success']} / {s['total_error']}  "
               f"(success_rate {s['success_pct']}%)")
    out.append(f"  avg latency    : {s['avg_latency_sec']:.3f}s   "
               f"p95: {s['p95_latency_sec']:.3f}s")
    out.append(f"  涉及工具數     : {s['tools_with_calls']}")

    # Error code breakdown
    if s["error_codes"]:
        out.append("")
        out.append("  失敗 by error_code：")
        for code, n in sorted(s["error_codes"].items(), key=lambda kv: -kv[1])[:6]:
            out.append(f"    {code:25s} {n}")

    # Top failures
    failures = tool_top_failures(hours=hours, top_n=5)
    if failures:
        out.append("")
        out.append("  🔴 Top 失敗 tool：")
        for m in failures:
            out.append(f"    {m['tool']:30s} {m['error']:3d} 失敗 / {m['calls']:3d} call  "
                       f"({m['success_pct']}% success)")

    # Top slow
    slow = tool_top_slow(hours=hours, top_n=5, min_calls=3)
    if slow:
        out.append("")
        out.append("  🐢 Top 慢 tool（≥3 call，按 p95）：")
        for m in slow:
            out.append(f"    {m['tool']:30s} p95={m['p95_latency_sec']:.2f}s  "
                       f"avg={m['avg_latency_sec']:.2f}s  ({m['calls']} call)")

    # Top usage
    top = tool_top_usage(hours=hours, top_n=8)
    if top:
        out.append("")
        out.append("  🔥 Top 使用 tool：")
        for m in top:
            mark = "✅" if m["success_pct"] >= 95 else ("🟡" if m["success_pct"] >= 80 else "🔴")
            out.append(f"    {mark} {m['tool']:30s} {m['calls']:4d} call  "
                       f"{m['success_pct']:5.1f}% success  "
                       f"avg {m['avg_latency_sec']:.2f}s")
    return "\n".join(out)


def tool_health(tool_name: str, hours: int = 24) -> str:
    """🟢 單一 tool 的 metrics（含 last error）。"""
    if not tool_name:
        return "❌ tool_name 必填"
    metrics = tool_metrics(hours=hours)
    m = next((mm for mm in metrics if mm["tool"] == tool_name), None)
    if m is None:
        return f"  （過去 {hours}h `{tool_name}` 沒被呼叫過）"
    out = [f"📊 tool: {m['tool']}  過去 {hours}h"]
    out.append("─" * 60)
    out.append(f"  calls          : {m['calls']}")
    out.append(f"  success / error: {m['success']} / {m['error']}  "
               f"({m['success_pct']}%)")
    out.append(f"  avg latency    : {m['avg_latency_sec']:.3f}s")
    out.append(f"  p95 latency    : {m['p95_latency_sec']:.3f}s")
    if m["error_codes"]:
        out.append("  error 分佈     :")
        for code, n in m["error_codes"].items():
            out.append(f"    {code}: {n}")
    if m["last_error"]:
        out.append(f"  last error     : [{(m['last_error_at'] or '')[:16]}]")
        out.append(f"    {m['last_error'][:120]}")
    return "\n".join(out)
