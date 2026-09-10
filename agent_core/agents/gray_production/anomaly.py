"""Gray Trigger — production anomaly cross-department coordination workflow.

Query strategy (two phases):

  Phase 1 (concurrent): Yellow + Indigo
    - Yellow: procurement ETA for affected materials
    - Indigo: stock availability + suggested alternative products

  Phase 2 (sequential): White — compliance check
    - If Indigo returned alternatives → query White for each alternative (cap 3)
      → only then can we claim "替代方案合規"
    - If Indigo is a stub / returned no alternatives → query White for the
      original product as a baseline reference only (NOT a compliance claim)
    This two-phase design prevents querying White with the original product_model
    and mislabelling the result as alternative-compliance confirmation.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.agents.gray_production.agent import GrayProductionAgent

from agent_core.agents.permission_matrix import Agent

_log = logging.getLogger(__name__)

_SEVERITY_DELAY: dict[str, int] = {"high": 14, "medium": 7, "low": 3}
_NOT_FOUND_PHRASES = ("查無", "找不到")


# ── helpers ─────────────────────────────────────────────────────────────────

def _safe_query(
    agent: "GrayProductionAgent",
    target: Agent,
    intent: str,
    payload: dict,
    trace_id: str,
) -> dict:
    try:
        return agent.query_peer(target, intent, payload, trace_id=trace_id)
    except Exception as exc:
        _log.warning("Gray cross-query failed target=%s intent=%s: %s",
                     target.value, intent, exc)
        try:
            from agent_core.exception_logger import log_event as _bq_log
            _bq_log(
                event_type="cross_query_failure",
                source_agent="gray",
                severity="error",
                detail=f"cross-query 失敗 → {target.value} / {intent}: {exc}",
                trace_id=trace_id,
                extra={"target": target.value, "intent": intent},
            )
        except Exception:
            pass
        return {"status": "error", "detail": str(exc)}


def _query_yellow(agent, order_id: str, product: str, trace_id: str) -> dict:
    return _safe_query(agent, Agent.YELLOW, "query.procurement_eta",
                       {"order_id": order_id, "product": product}, trace_id)


def _query_indigo(agent, product: str, trace_id: str) -> dict:
    return _safe_query(agent, Agent.INDIGO, "query.stock_availability",
                       {"product": product, "alternatives": True}, trace_id)


def _query_white_for_alternatives(
    agent,
    customer: str,
    original_product: str,
    alternatives: list,
    trace_id: str,
) -> dict:
    """Query White for Indigo-supplied alternatives, or original product as fallback.

    Returns a dict with:
      alternatives_checked : bool  — True if we queried Indigo alternatives (not the
                                     original product), which is the prerequisite for
                                     a valid "替代方案合規" claim.
      checked_models       : list  — product_model values actually queried.
      compliant_models     : list  — subset with confirmed spec coverage.
      status               : str   — "ok" | "error"
      results              : dict  — {product_model: raw_white_result}
    """
    def _has_spec(r: dict) -> bool:
        if r.get("status") in ("stub", "error"):
            return False
        text = str(r.get("text", ""))
        return bool(text) and not any(p in text for p in _NOT_FOUND_PHRASES) and len(text) > 10

    if alternatives:
        target_models = [str(a) for a in alternatives[:3]]
        raw: dict[str, dict] = {}
        for model in target_models:
            raw[model] = _safe_query(
                agent, Agent.WHITE, "query.list_specs",
                {"customer": customer, "product_model": model},
                trace_id,
            )
        compliant = [m for m, r in raw.items() if _has_spec(r)]
        all_errors = all(r.get("status") == "error" for r in raw.values())
        return {
            "alternatives_checked": True,
            "checked_models": target_models,
            "compliant_models": compliant,
            "status": "error" if all_errors else "ok",
            "results": raw,
        }

    # No alternatives from Indigo — fall back to original product as baseline
    r = _safe_query(agent, Agent.WHITE, "query.list_specs",
                    {"customer": customer, "product_model": original_product}, trace_id)
    return {
        "alternatives_checked": False,
        "checked_models": [original_product],
        "compliant_models": [],
        "status": r.get("status", "ok"),
        "results": {original_product: r},
    }


# ── ECD calculation ──────────────────────────────────────────────────────────

def _calculate_ecd(
    original_ecd: str,
    severity: str,
    yellow_result: dict,
    indigo_result: dict,
) -> tuple[str, int]:
    """Return (new_ecd_iso, delay_days).

    Raises ValueError if original_ecd is not a valid ISO date — callers should
    catch this and surface it as a validation error rather than computing an ECD
    anchored to an unrelated date.
    """
    try:
        base = date.fromisoformat(original_ecd)
    except ValueError:
        raise ValueError(
            f"original_ecd 格式不正確：{original_ecd!r}（必須是 YYYY-MM-DD）"
        )

    yellow_stub = yellow_result.get("status") in ("stub", "error")
    indigo_stub = indigo_result.get("status") in ("stub", "error")

    # Yellow contribution
    if not yellow_stub and "eta" in yellow_result:
        try:
            eta_date = date.fromisoformat(str(yellow_result["eta"]))
            yellow_delay = max(0, (eta_date - base).days)
        except ValueError:
            yellow_delay = _SEVERITY_DELAY.get(severity, 7)
    else:
        yellow_delay = _SEVERITY_DELAY.get(severity, 7)

    # Indigo contribution
    if not indigo_stub and indigo_result.get("in_stock") is False:
        indigo_extra = 3
    else:
        indigo_extra = 0

    delay_days = yellow_delay + indigo_extra
    new_ecd = (base + timedelta(days=delay_days)).isoformat()
    return new_ecd, delay_days


# ── report formatting ────────────────────────────────────────────────────────

def _format_report(
    order_id: str,
    product: str,
    customer: str,
    original_ecd: str,
    reason: str,
    severity: str,
    yellow_result: dict,
    indigo_result: dict,
    white_result: dict,
    new_ecd: str,
    delay_days: int,
) -> str:
    sev_label = {"high": "🔴 高", "medium": "🟡 中", "low": "🟢 低"}.get(severity, severity)
    yellow_stub = yellow_result.get("status") in ("stub", "error")
    indigo_stub = indigo_result.get("status") in ("stub", "error")

    # Yellow summary
    if yellow_stub:
        yellow_summary = f"⚠️ 採購系統未回應（預估延誤 {_SEVERITY_DELAY.get(severity,7)} 天）"
    elif "eta" in yellow_result:
        yellow_summary = f"預計到料：{yellow_result['eta']}"
    else:
        yellow_summary = str(yellow_result)

    # Indigo summary
    if indigo_stub:
        indigo_summary = "⚠️ 倉庫系統未回應"
    elif "in_stock" in indigo_result:
        in_stock = indigo_result.get("in_stock")
        qty = indigo_result.get("quantity", "—")
        alts = indigo_result.get("alternatives", [])
        indigo_summary = f"庫存：{'有' if in_stock else '無'}（數量：{qty}）"
        if alts:
            indigo_summary += f"\n  替代料：{', '.join(str(a) for a in alts[:3])}"
    else:
        indigo_summary = str(indigo_result)

    # White summary — labelling depends on whether we checked actual alternatives
    # or fell back to the original product.  Only alternative-specific checks
    # justify a "替代方案合規" claim.
    if white_result.get("status") == "error":
        white_summary = "⚠️ 法務系統查詢失敗"
    elif white_result.get("alternatives_checked"):
        compliant = white_result.get("compliant_models", [])
        checked   = white_result.get("checked_models", [])
        if compliant:
            white_summary = (
                f"✅ 替代方案規格確認合規：{', '.join(compliant)}"
            )
        else:
            white_summary = (
                f"⚠️ 替代方案 {', '.join(checked)} 查無規格，合規性需人工確認"
            )
    else:
        # alternatives_checked=False means White was queried on the original
        # product only; this does NOT confirm alternative compliance.
        orig_r = white_result.get("results", {}).get(product, {})
        orig_text = str(orig_r.get("text", ""))
        if orig_text and not any(p in orig_text for p in _NOT_FOUND_PHRASES) and len(orig_text) > 10:
            white_summary = "原始產品規格存在；待 Indigo 提供替代料後再確認替代方案合規性"
        else:
            white_summary = "查無原始規格，替代方案合規性需人工確認"

    delay_str = f"延誤 {delay_days} 天" if delay_days > 0 else "無延誤"

    return (
        f"🏭 生產異常決策支援報告\n"
        f"{'━'*30}\n"
        f"訂單：{order_id}\n"
        f"產品：{product}\n"
        f"客戶：{customer}\n"
        f"原定交期：{original_ecd}\n"
        f"新預計完工：{new_ecd}（{delay_str}）\n"
        f"嚴重程度：{sev_label}\n"
        f"異常原因：{reason}\n"
        f"\n{'━'*15} 跨部門查詢 {'━'*15}\n"
        f"\n📋 採購（Yellow）：\n  {yellow_summary}\n"
        f"\n📦 倉庫（Indigo）：\n  {indigo_summary}\n"
        f"\n⚖️  法務（White）：\n  {white_summary}\n"
        f"\n{'━'*30}\n"
        f"⚠️  請 GM 決策：是否批准新交期 {new_ecd}？"
    )


def _orange_notice(product: str, customer: str, original_ecd: str, new_ecd: str,
                   delay_days: int, reason: str) -> str:
    delay_str = f"延誤 {delay_days} 天" if delay_days > 0 else "時程不變"
    return (
        f"📢 業務通知（生產異常）\n"
        f"產品：{product}｜客戶：{customer}\n"
        f"原定交期 {original_ecd} → 新預估 {new_ecd}（{delay_str}）\n"
        f"原因：{reason}\n"
        f"請視需要提前與客戶溝通。"
    )


# ── main trigger ─────────────────────────────────────────────────────────────

def trigger_gray(
    agent: "GrayProductionAgent",
    order_id: str,
    product: str,
    customer: str,
    original_ecd: str,
    reason: str,
    severity: str,
    trace_id: str,
) -> dict[str, Any]:
    """Run the full Gray Trigger workflow. Returns the compiled report dict."""
    severity = severity.lower() if severity else "medium"
    if severity not in _SEVERITY_DELAY:
        severity = "medium"

    _log.info("Gray Trigger fired order=%s product=%s severity=%s trace=%s",
              order_id, product, severity, trace_id)

    # ── Phase 1: Yellow + Indigo concurrently ────────────────────────────────
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gray_p1") as pool:
        f_yellow = pool.submit(_query_yellow, agent, order_id, product, trace_id)
        f_indigo = pool.submit(_query_indigo, agent, product, trace_id)
        try:
            yellow_result = f_yellow.result()
        except Exception as exc:
            _log.warning("Gray Trigger Yellow query failed: %s", exc)
            yellow_result = {"status": "error", "detail": str(exc)}
        try:
            indigo_result = f_indigo.result()
        except Exception as exc:
            _log.warning("Gray Trigger Indigo query failed: %s", exc)
            indigo_result = {"status": "error", "detail": str(exc)}

    # ── Phase 2: White — driven by Indigo alternatives ───────────────────────
    indigo_is_stub = indigo_result.get("status") in ("stub", "error")
    alternatives   = indigo_result.get("alternatives", []) if not indigo_is_stub else []
    white_result   = _query_white_for_alternatives(
        agent, customer, product, alternatives, trace_id
    )

    # ── ECD calculation ──────────────────────────────────────────────────────
    new_ecd, delay_days = _calculate_ecd(original_ecd, severity, yellow_result, indigo_result)

    # ── Format report ────────────────────────────────────────────────────────
    report_text = _format_report(
        order_id, product, customer, original_ecd, reason, severity,
        yellow_result, indigo_result, white_result,
        new_ecd, delay_days,
    )
    orange_text = _orange_notice(product, customer, original_ecd, new_ecd, delay_days, reason)

    # ── Telegram notifications ───────────────────────────────────────────────
    notifications_sent = False
    try:
        from agent_core.telegram import telegram_push, telegram_push_agent
        r_orange = telegram_push_agent("orange", orange_text)
        if not str(r_orange).startswith("✅"):
            _log.warning("Gray Trigger Orange notification failed: %s", r_orange)
        r_red = telegram_push(report_text)
        if not str(r_red).startswith("✅"):
            _log.warning("Gray Trigger Red notification failed: %s", r_red)
        notifications_sent = (
            str(r_orange).startswith("✅") and str(r_red).startswith("✅")
        )
    except Exception as exc:
        _log.warning("Gray Trigger Telegram notification raised: %s", exc)

    # ── Persist anomaly log ──────────────────────────────────────────────────
    record = {
        "order_id": order_id,
        "product": product,
        "customer": customer,
        "original_ecd": original_ecd,
        "new_ecd": new_ecd,
        "delay_days": delay_days,
        "reason": reason,
        "severity": severity,
        "trace_id": trace_id,
        "yellow_status": yellow_result.get("status", "ok"),
        "indigo_status": indigo_result.get("status", "ok"),
        "white_alternatives_checked": white_result.get("alternatives_checked", False),
        "white_compliant_models": white_result.get("compliant_models", []),
        "notifications_sent": notifications_sent,
    }
    try:
        from agent_core.agents.gray_production import production_tracker as _pt
        _pt.append_anomaly(record)
    except Exception as exc:
        _log.warning("Gray Trigger failed to persist anomaly log: %s", exc)

    # ── BigQuery exception log ───────────────────────────────────────────────
    try:
        from agent_core.exception_logger import log_event as _bq_log
        _bq_log(
            event_type="production_anomaly",
            source_agent="gray",
            severity=severity,
            detail=f"訂單 {order_id} / 產品 {product} 延誤 {delay_days} 天（原因：{reason[:100]}）",
            trace_id=trace_id,
            extra={
                "order_id":                  order_id,
                "product":                   product,
                "original_ecd":              original_ecd,
                "new_ecd":                   new_ecd,
                "delay_days":                delay_days,
                "yellow_status":             yellow_result.get("status", "ok"),
                "indigo_status":             indigo_result.get("status", "ok"),
                "white_alternatives_checked": white_result.get("alternatives_checked", False),
                "white_compliant_models":    white_result.get("compliant_models", []),
                "notifications_sent":        notifications_sent,
            },
        )
    except Exception as exc:
        _log.warning("Gray Trigger BigQuery log failed: %s", exc)

    return {
        "order_id": order_id,
        "new_ecd": new_ecd,
        "delay_days": delay_days,
        "report": report_text,
        "orange_notice": orange_text,
        "notifications_sent": notifications_sent,
        "cross_query": {
            "yellow": yellow_result,
            "indigo": indigo_result,
            "white": white_result,
        },
    }
