"""Black cashier helpers.

Read-only cash visibility over the existing accounting email lake. Black is
kept deliberately narrower than Purple: it surfaces payment, remittance and
cash alert records, but never executes payment actions.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from agent_core.agents.purple_accounting import accounting as _acct


def _blacken(result: dict[str, Any], *, title: str | None = None) -> dict[str, Any]:
    out = dict(result)
    text = str(out.get("text") or "")
    text = text.replace("🟣 會計", "⚫ 出納").replace("🟣 付款/匯款", "⚫ 付款/匯款")
    if title and text:
        lines = text.splitlines()
        if lines:
            lines[0] = title
            text = "\n".join(lines)
    if text:
        out["text"] = text
    return out


def _sort_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            str(r.get("date") or ""),
            str(r.get("last_message_date") or ""),
            str(r.get("record_id") or ""),
        ),
        reverse=True,
    )


def _record_line(row: dict[str, Any]) -> str:
    parties = ", ".join((row.get("counterparties") or [])[:2]) or "-"
    amounts = ", ".join((row.get("amounts") or [])[:3]) or "-"
    po = ", ".join((row.get("po_numbers") or [])[:3]) or "-"
    subject = str(row.get("subject") or "").replace("\n", " ")[:88]
    direction = row.get("cash_direction") or "-"
    return (
        f"- {row.get('date') or '-'} | {direction} | {row.get('status') or '-'} | "
        f"{parties} | {amounts} | PO {po} | {subject}"
    )


def _cash_text(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [title]
    if not rows:
        lines.append("目前沒有符合條件的出納紀錄。")
    for row in rows:
        lines.append(_record_line(row))
    return "\n".join(lines)


def get_profile() -> dict[str, Any]:
    return {
        "color": "black",
        "department": "出納 / Cashier",
        "summary": "查現金流、付款、匯入、收支警示與近期出納摘要；不做付款、不寄信、不寫 ERP。",
        "data_sources": [
            "var/data/data_lake_internal/emails.parquet",
            "agent_core.agents.purple_accounting.accounting",
        ],
        "query_intents": [
            "query.profile",
            "query.list_transactions",
            "query.cash_records",
            "query.cash_payments",
            "query.cash_receipts",
            "query.cash_alerts",
            "query.cash_summary",
        ],
        "command_intents": [],
    }


def cash_records(
    *,
    counterparty: str = "",
    po_number: str = "",
    keyword: str = "",
    direction: str = "",
    days_back: int = 365,
    limit: int = 20,
) -> dict[str, Any]:
    result = _acct.payment_records(
        counterparty=counterparty,
        po_number=po_number,
        keyword=keyword,
        cash_direction=direction,
        days_back=days_back,
        max_events=limit,
    )
    label = counterparty or po_number or keyword or direction or "(all)"
    return _blacken(result, title=f"⚫ 出納收支紀錄：{label}（{result.get('total', 0)} 筆）")


def cash_payments(
    *,
    counterparty: str = "",
    po_number: str = "",
    keyword: str = "",
    days_back: int = 365,
    limit: int = 20,
) -> dict[str, Any]:
    result = _acct.payment_records(
        counterparty=counterparty,
        po_number=po_number,
        keyword=keyword,
        cash_direction="outbound",
        days_back=days_back,
        max_events=limit,
    )
    label = counterparty or po_number or keyword or "outbound"
    return _blacken(result, title=f"⚫ 出納付款紀錄：{label}（{result.get('total', 0)} 筆）")


def cash_receipts(
    *,
    counterparty: str = "",
    keyword: str = "",
    days_back: int = 365,
    limit: int = 20,
) -> dict[str, Any]:
    result = _acct.payment_records(
        counterparty=counterparty,
        keyword=keyword,
        cash_direction="inbound",
        days_back=days_back,
        max_events=limit,
    )
    label = counterparty or keyword or "inbound"
    return _blacken(result, title=f"⚫ 出納入帳紀錄：{label}（{result.get('total', 0)} 筆）")


def cash_alerts(*, days_back: int = 90, limit: int = 20) -> dict[str, Any]:
    result = _acct.accounting_alerts(days_back=days_back, limit=limit)
    return _blacken(result, title=f"⚫ 出納注意事項 {result.get('total', 0)} 筆")


def cash_summary(*, days_back: int = 30, limit: int = 5) -> dict[str, Any]:
    result = _acct.payment_records(days_back=days_back, max_events=100)
    rows = _sort_records(list(result.get("records") or []))
    by_direction = Counter(str(row.get("cash_direction") or "unknown") for row in rows)
    by_status = Counter(str(row.get("status") or "unknown") for row in rows)
    shown = rows[:max(1, int(limit or 5))]
    lines = [f"⚫ 出納摘要：近 {days_back} 天 {len(rows)} 筆"]
    if rows:
        lines.append("方向: " + ", ".join(f"{k}={v}" for k, v in by_direction.most_common()))
        lines.append("狀態: " + ", ".join(f"{k}={v}" for k, v in by_status.most_common()))
        lines.append("最近紀錄:")
        for row in shown:
            lines.append(_record_line(row))
    else:
        lines.append("近期沒有出納付款或匯入紀錄。")
    return {
        "text": "\n".join(lines),
        "total": len(rows),
        "by_direction": dict(by_direction),
        "by_status": dict(by_status),
        "recent": shown,
    }
