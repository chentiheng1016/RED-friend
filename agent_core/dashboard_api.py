"""Dashboard API — 結構化 JSON 接口（薄層 wrapper）。

dashboard.py 是給人看的 formatted text；本檔給 caller 拿結構化資料。

用途：
  - 未來接 web UI / Grafana / Slack notifier 不用 parse text
  - 寫 daily report 拼 markdown / email
  - 外部 monitoring 抓 health check（GET /api/health）

設計：
  純讀層 — 沒新計算，全部透過 status_center / metrics / 各 module 既有 API。
  輸出 JSON-serializable dict（默認 default=str 處理 datetime 等）。

  對外 endpoint（concept，當前以 Python function 提供，未來可包 FastAPI）：
    GET /api/health         → {"score": 100, "status": "🟢 healthy"}
    GET /api/overview       → 全 system_overview dict
    GET /api/metrics?h=24   → metrics summary
    GET /api/tools          → tool_metrics list
    GET /api/tool/<name>    → 單一 tool health

  目前以 5 個 Python function 對應，其中 dashboard_json / health_json 列為
  SAFE tier 給 LLM / 大王 introspect，其他保留作為 lib 函式。
"""
from __future__ import annotations

import json
from typing import Any


def _to_jsonable(obj: Any) -> Any:
    """確保結果都 JSON-serializable（datetime / set / 等遞迴轉）。"""
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "isoformat"):  # datetime
        return obj.isoformat()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# ────────────────────────────────────────────────────────────────────
# Public API — Python interface
# ────────────────────────────────────────────────────────────────────
def get_overview() -> dict:
    """全 system_overview dict（status_center 包好的）。"""
    from agent_core.status_center import system_overview
    return _to_jsonable(system_overview())


def get_health() -> dict:
    """system_overview + health_score。輕量端點。"""
    from agent_core.status_center import health_score, system_overview
    o = system_overview()
    # 把算好的 overview 傳進 health_score，避免它內部再跑一次 system_overview
    # （= 整套 alert 電池 / launchctl / log 掃描重跑第二遍；健檢 Low）。
    h = health_score(o)
    return _to_jsonable({
        "at": o.get("at"),
        "score": h["score"],
        "status": h["status"],
        "reasons": h["reasons"],
        "alerts": o.get("alerts", {}),
        "queue_dlq": (o.get("queue") or {}).get("dlq", 0),
        "task_overdue": (o.get("task_memory") or {}).get("overdue", 0),
        "success_pct_24h": (o.get("metrics") or {}).get("success_pct", 0),
        "daemon_bad_count": len((o.get("daemons") or {}).get("last_exit_nonzero", [])),
    })


def get_metrics(hours: int = 24) -> dict:
    """metrics_summary + top failure / slow / usage。"""
    from agent_core.metrics import (
        metrics_summary, tool_top_failures, tool_top_slow, tool_top_usage,
    )
    return _to_jsonable({
        "window_hours": hours,
        "summary": metrics_summary(hours=hours),
        "top_failures": tool_top_failures(hours=hours, top_n=5),
        "top_slow": tool_top_slow(hours=hours, top_n=5),
        "top_usage": tool_top_usage(hours=hours, top_n=10),
    })


def get_tool_metrics(tool_name: str, hours: int = 24) -> dict:
    """單一 tool 的指標。404-style：tool 沒被呼叫過 returns {found: False}。"""
    from agent_core.metrics import tool_metrics
    metrics = tool_metrics(hours=hours)
    m = next((mm for mm in metrics if mm["tool"] == tool_name), None)
    if m is None:
        return _to_jsonable({"tool": tool_name, "found": False,
                             "window_hours": hours})
    return _to_jsonable({"tool": tool_name, "found": True,
                         "window_hours": hours, **m})


# ────────────────────────────────────────────────────────────────────
# LLM-facing tool — get a JSON snapshot
# ────────────────────────────────────────────────────────────────────
def dashboard_json(section: str = "overview", hours: int = 24) -> str:
    """🟢 結構化 JSON snapshot（給 web UI / 外部監控用）。

    Args:
        section: 'overview' / 'health' / 'metrics' / 'tool:<name>'
        hours: metrics 視窗（預設 24h）

    Returns:
        JSON string（pretty-printed），可直接喂 jq / curl / web UI。
    """
    if not section:
        section = "overview"
    section = section.strip().lower()
    if section == "overview":
        data = get_overview()
    elif section == "health":
        data = get_health()
    elif section == "metrics":
        data = get_metrics(hours=hours)
    elif section.startswith("tool:"):
        tool_name = section[5:].strip()
        if not tool_name:
            return "❌ tool: 後要接 tool 名（例：tool:send_gmail）"
        data = get_tool_metrics(tool_name, hours=hours)
    else:
        return (f"❌ 未知 section '{section}'。可選："
                f"overview / health / metrics / tool:<name>")
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def health_json() -> str:
    """🟢 輕量健康檢查 JSON — 適合 cron / external monitoring poll。"""
    return json.dumps(get_health(), ensure_ascii=False, indent=2, default=str)
