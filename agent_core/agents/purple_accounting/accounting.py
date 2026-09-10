"""Purple accounting helpers.

Purple v1 is read-only.  It uses the internal email lake as the accounting
event source, extracting invoice, payment, remittance, statement and budget
records from rows classified as 會計 or containing accounting keywords.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any


_ACCOUNTING_DEPT = "會計"
_ACCOUNTING_KEYWORDS = (
    "accounting", "invoice", "payment", "paid", "payable", "receivable",
    "remittance", "forex", "statement", "bill", "billing", "receipt",
    "loan", "budget", "prepayment", "deduct", "bank", "帳", "會計",
    "發票", "付款", "匯款", "入帳", "扣帳", "扣繳", "繳費", "帳單",
    "水單", "對帳", "貸款", "預算", "預付款", "應付", "應收",
)
_SYSTEM_SUBJECT_MARKERS = ("【小紅", "[小紅", "小紅早安", "小紅 Ponder", "小紅新信")

_REMITTANCE_KEYWORDS = (
    "remittance", "inward remittance", "forex inward", "外匯匯入",
    "國外匯入", "匯入", "入帳", "水單",
)
_PAYMENT_KEYWORDS = (
    "payment", "paid", "prepayment", "deduct", "付款", "已付款",
    "預付款", "扣帳", "扣繳", "繳費", "代繳",
)
_INVOICE_KEYWORDS = (
    "invoice", "e-invoice", "einvoice", "bill", "billing", "receipt",
    "發票", "帳單", "費用通知", "通知單",
)
_STATEMENT_KEYWORDS = ("statement", "對帳單", "簡易對帳", "綜合對帳")
_BUDGET_KEYWORDS = ("budget", "loan", "預算", "貸款", "信保", "撥款")
_MEETING_KEYWORDS = ("meeting", "會議", "已接受")
_ATTENTION_KEYWORDS = (
    "fail", "failed", "reject", "rejected", "overdue", "urgent", "催",
    "失敗", "退件", "拒絕", "逾期", "未付款", "未繳", "異常", "急",
    "帳戶名稱有誤", "扣帳失敗", "繳費失敗",
)
_SUCCESS_KEYWORDS = ("success", "successful", "已繳", "已付款", "成功", "完成", "扣帳結果")
_AMOUNT_RE = re.compile(
    r"\b(?:USD|EUR|RMB|NTD|TWD)\s*[\d,]+(?:\.\d+)?\b|"
    r"\b[\d,]+(?:\.\d+)?\s*(?:USD|EUR|RMB|NTD|TWD|元)\b|"
    r"(?:NT\$|US\$|EUR)\s*[\d,]+(?:\.\d+)?",
    re.IGNORECASE,
)


def _load_df():
    from agent_core import email_timeline

    return email_timeline._load_df()


def _get(row: Any, name: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(name, default)
    return getattr(row, name, default)


def _entities(row: Any) -> dict[str, Any]:
    # email_timeline._load_df() 已把 entities_json 預解析成 _entities 欄（dict），
    # 走快取免對整個 ~2 萬列 lake 每列重 json.loads；row 不帶該欄（如測試直造）
    # 才退回即時解析。copy 一份避免呼叫端 mutate 污染共享 df。
    cached = _get(row, "_entities", None)
    if isinstance(cached, Mapping):
        return dict(cached)
    raw = _get(row, "entities_json", "{}")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            return _as_list(json.loads(value))
        except Exception:
            return [part.strip() for part in re.split(r"[,|/]", value) if part.strip()]
    if isinstance(value, Iterable):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return [str(value).strip()]


def _unique(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _blob(row: Any) -> str:
    return "\n".join(
        str(_get(row, name, "") or "")
        for name in ("subject", "summary", "raw_body_preview")
    )


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    hay = (text or "").lower()
    return any(str(needle or "").lower() in hay for needle in needles if needle)


def _accounting_row(row: Any) -> bool:
    subject = str(_get(row, "subject", "") or "")
    if any(marker in subject for marker in _SYSTEM_SUBJECT_MARKERS):
        return False
    primary = str(_get(row, "primary_dept", "") or "").strip()
    if primary == _ACCOUNTING_DEPT:
        return True
    if _ACCOUNTING_DEPT in _as_list(_get(row, "all_depts", [])):
        return True
    return _contains_any(_blob(row), _ACCOUNTING_KEYWORDS)


def _kind_for(text: str) -> str:
    # payment 先於 remittance 是刻意的：一筆「付款」常含 "remittance form"（匯款單）
    # 字樣（例：工廠付供應商的預付款），若先判 remittance 會把它誤歸 inbound 收款
    # （見 test_payment_records_by_po）。kind 的歧義無法靠對調順序解，別輕易對調。
    if _contains_any(text, _PAYMENT_KEYWORDS):
        return "payment"
    if _contains_any(text, _REMITTANCE_KEYWORDS):
        return "remittance"
    if _contains_any(text, _INVOICE_KEYWORDS):
        return "invoice"
    if _contains_any(text, _STATEMENT_KEYWORDS):
        return "statement"
    if _contains_any(text, _BUDGET_KEYWORDS):
        return "budget"
    if _contains_any(text, _MEETING_KEYWORDS):
        return "meeting"
    return "accounting_record"


def _status_for(text: str, kind: str) -> str:
    if _contains_any(text, _ATTENTION_KEYWORDS):
        return "attention"
    if kind == "remittance":
        return "received"
    if _contains_any(text, _SUCCESS_KEYWORDS):
        return "completed"
    if kind in {"payment", "invoice"}:
        return "pending_review"
    if kind == "statement":
        return "statement"
    return "record"


def _cash_direction(text: str, kind: str) -> str:
    low = (text or "").lower()
    if kind == "remittance" or any(x in low for x in ("inward", "匯入", "入帳")):
        return "inbound"
    if kind == "payment" or any(x in low for x in ("prepayment", "付款", "扣帳", "扣繳", "繳費")):
        return "outbound"
    return ""


def _amounts(row: Any, text: str) -> list[str]:
    ents = _entities(row)
    return _unique([*(ents.get("amounts") or []), *[m.group(0) for m in _AMOUNT_RE.finditer(text)]])


def _row_to_record(row: Any) -> dict[str, Any]:
    ents = _entities(row)
    text = _blob(row)
    kind = _kind_for(text)
    customers = _unique(ents.get("customers") or [])
    suppliers = _unique(ents.get("suppliers") or [])
    return {
        "record_id": str(_get(row, "thread_id", "") or _get(row, "first_message_id", "") or ""),
        "thread_id": str(_get(row, "thread_id", "") or ""),
        "date": str(_get(row, "date", "") or ""),
        "last_message_date": str(_get(row, "last_message_date", "") or ""),
        "sender": str(_get(row, "sender", "") or ""),
        "subject": str(_get(row, "subject", "") or ""),
        "summary": str(_get(row, "summary", "") or ""),
        "direction": str(_get(row, "direction", "") or ""),
        "message_count": int(_get(row, "message_count", 0) or 0),
        "kind": kind,
        "status": _status_for(text, kind),
        "cash_direction": _cash_direction(text, kind),
        "po_numbers": _unique(ents.get("po_numbers") or []),
        "customers": customers,
        "suppliers": suppliers,
        "counterparties": _unique([*customers, *suppliers]),
        "amounts": _amounts(row, text),
        "dates_mentioned": _unique(ents.get("dates_mentioned") or []),
        "actions": _unique(ents.get("actions") or []),
    }


def _date_cutoff(days_back: int | None) -> str:
    if not days_back or int(days_back) <= 0:
        return ""
    return (datetime.now() - timedelta(days=int(days_back))).strftime("%Y-%m-%d")


def _record_matches(
    row: dict[str, Any],
    *,
    kind: str = "",
    status: str = "",
    counterparty: str = "",
    po_number: str = "",
    keyword: str = "",
    cash_direction: str = "",
) -> bool:
    blob = " ".join([
        row.get("record_id", ""),
        row.get("sender", ""),
        row.get("subject", ""),
        row.get("summary", ""),
        row.get("kind", ""),
        row.get("status", ""),
        row.get("cash_direction", ""),
        " ".join(row.get("po_numbers") or []),
        " ".join(row.get("counterparties") or []),
        " ".join(row.get("amounts") or []),
        " ".join(row.get("actions") or []),
    ]).lower()
    if kind and str(row.get("kind") or "").lower() != kind.lower():
        return False
    if status and str(row.get("status") or "").lower() != status.lower():
        return False
    if cash_direction and str(row.get("cash_direction") or "").lower() != cash_direction.lower():
        return False
    if counterparty and counterparty.lower() not in blob:
        return False
    if po_number and po_number.lower() not in blob:
        return False
    if keyword and keyword.lower() not in blob:
        return False
    return True


def _search_records(
    *,
    kind: str = "",
    status: str = "",
    counterparty: str = "",
    po_number: str = "",
    keyword: str = "",
    cash_direction: str = "",
    days_back: int | None = 365,
    limit: int = 50,
) -> list[dict[str, Any]]:
    df = _load_df()
    cutoff = _date_cutoff(days_back)
    if cutoff and "date" in df.columns:
        # 日期窗口先用 vectorized mask 篩掉，免對窗口外（通常佔多數）的列逐列建
        # Series + 跑 row 判斷 + regex 抽取。map(str(v or "")) 等價原本逐列判斷的
        # 語義（NaN→"nan" 保留、None/空→"" 篩掉）。
        df = df[df["date"].map(lambda v: str(v or "")) >= cutoff]
    rows: list[dict[str, Any]] = []
    for _, source in df.iterrows():
        if not _accounting_row(source):
            continue
        row = _row_to_record(source)
        if not _record_matches(
            row,
            kind=kind,
            status=status,
            counterparty=counterparty,
            po_number=po_number,
            keyword=keyword,
            cash_direction=cash_direction,
        ):
            continue
        rows.append(row)
    rows.sort(
        key=lambda r: (
            r.get("date") or "",
            r.get("last_message_date") or "",
            r.get("record_id") or "",
        ),
        reverse=True,
    )
    return rows[:max(1, min(int(limit or 50), 200))]


def _record_line(row: dict[str, Any]) -> str:
    parties = ", ".join((row.get("counterparties") or [])[:2]) or "-"
    amounts = ", ".join((row.get("amounts") or [])[:3]) or "-"
    po = ", ".join((row.get("po_numbers") or [])[:3]) or "-"
    subject = (row.get("subject") or "").replace("\n", " ")[:88]
    cash = f"/{row.get('cash_direction')}" if row.get("cash_direction") else ""
    return (
        f"- {row.get('date') or '-'} | {row.get('kind')}{cash} | "
        f"{row.get('status')} | {parties} | {amounts} | PO {po} | {subject}"
    )


def get_profile() -> dict[str, Any]:
    return {
        "color": "purple",
        "department": "會計 / Accounting",
        "summary": "查會計 email lake 的 invoice/帳單、付款/匯款、對帳單、金額與警示紀錄。",
        "data_sources": [
            "var/data/data_lake_internal/emails.parquet",
            "agent_core.email_timeline",
        ],
        "query_intents": [
            "query.profile",
            "query.list_accounts",
            "query.list_accounting_records",
            "query.accounting_record",
            "query.invoice_records",
            "query.payment_records",
            "query.remittance_records",
            "query.accounting_alerts",
            "query.accounting_summary",
        ],
        "command_intents": [],
    }


def list_accounting_records(
    *,
    kind: str = "",
    status: str = "",
    counterparty: str = "",
    po_number: str = "",
    keyword: str = "",
    days_back: int = 365,
    limit: int = 20,
) -> dict[str, Any]:
    rows = _search_records(
        kind=kind,
        status=status,
        counterparty=counterparty,
        po_number=po_number,
        keyword=keyword,
        days_back=days_back,
        limit=limit,
    )
    lines = [f"🟣 會計紀錄 {len(rows)} 筆"]
    filters = []
    for label, value in (
        ("kind", kind),
        ("status", status),
        ("counterparty", counterparty),
        ("po", po_number),
        ("keyword", keyword),
    ):
        if value:
            filters.append(f"{label}={value}")
    if filters:
        lines[0] += "（" + ", ".join(filters) + "）"
    if not rows:
        lines.append("目前沒有符合條件的會計紀錄。")
    for row in rows:
        lines.append(_record_line(row))
    return {"text": "\n".join(lines), "records": rows, "total": len(rows)}


def get_accounting_record(identifier: str, *, days_back: int = 3650) -> dict[str, Any]:
    ident = (identifier or "").strip()
    if not ident:
        return {"error": "missing identifier"}
    rows = _search_records(keyword=ident, days_back=days_back, limit=5)
    if not rows:
        return {"text": f"查無會計紀錄：{ident}", "found": False, "identifier": ident}
    row = rows[0]
    lines = [
        f"🟣 會計紀錄 {row.get('record_id') or ident}",
        f"日期: {row.get('date') or '-'}",
        f"類型: {row.get('kind') or '-'}",
        f"狀態: {row.get('status') or '-'}",
        f"現金方向: {row.get('cash_direction') or '-'}",
        f"對象: {', '.join(row.get('counterparties') or []) or '-'}",
        f"金額: {', '.join(row.get('amounts') or []) or '-'}",
        f"PO/單號: {', '.join(row.get('po_numbers') or []) or '-'}",
        f"主旨: {row.get('subject') or '-'}",
    ]
    if row.get("summary"):
        lines.append(f"摘要: {row['summary']}")
    return {"text": "\n".join(lines), "record": row, "found": True}


def invoice_records(
    *,
    counterparty: str = "",
    po_number: str = "",
    keyword: str = "",
    days_back: int = 365,
    max_events: int = 20,
) -> dict[str, Any]:
    return list_accounting_records(
        kind="invoice",
        counterparty=counterparty,
        po_number=po_number,
        keyword=keyword,
        days_back=days_back,
        limit=max_events,
    )


def payment_records(
    *,
    counterparty: str = "",
    po_number: str = "",
    keyword: str = "",
    cash_direction: str = "",
    days_back: int = 365,
    max_events: int = 20,
) -> dict[str, Any]:
    rows = _search_records(
        counterparty=counterparty,
        po_number=po_number,
        keyword=keyword,
        cash_direction=cash_direction,
        days_back=days_back,
        limit=max(max_events, 50),
    )
    rows = [
        row for row in rows
        if row.get("kind") in {"payment", "remittance"}
    ][:max(1, int(max_events or 20))]
    label = counterparty or po_number or keyword or cash_direction or "(all)"
    lines = [f"🟣 付款/匯款紀錄：{label}（{len(rows)} 筆）"]
    if not rows:
        lines.append("目前沒有符合條件的付款或匯款紀錄。")
    for row in rows:
        lines.append(_record_line(row))
    return {"text": "\n".join(lines), "records": rows, "total": len(rows)}


def remittance_records(
    *,
    counterparty: str = "",
    keyword: str = "",
    days_back: int = 365,
    max_events: int = 20,
) -> dict[str, Any]:
    return list_accounting_records(
        kind="remittance",
        counterparty=counterparty,
        keyword=keyword,
        days_back=days_back,
        limit=max_events,
    )


def accounting_alerts(*, days_back: int = 90, limit: int = 20) -> dict[str, Any]:
    rows = _search_records(days_back=days_back, limit=200)
    alerts = [
        row for row in rows
        if row.get("status") == "attention"
        or (row.get("kind") in {"invoice", "payment"} and row.get("status") == "pending_review")
    ][:max(1, int(limit or 20))]
    lines = [f"🟣 會計注意事項 {len(alerts)} 筆"]
    if not alerts:
        lines.append("近期沒有系統判定的會計警示。")
    for row in alerts:
        lines.append(_record_line(row))
    return {"text": "\n".join(lines), "alerts": alerts, "total": len(alerts)}


def accounting_summary(*, days_back: int = 30, limit: int = 5) -> dict[str, Any]:
    rows = _search_records(days_back=days_back, limit=200)
    by_kind = Counter(str(row.get("kind") or "") for row in rows)
    by_status = Counter(str(row.get("status") or "") for row in rows)
    lines = [f"🟣 會計摘要：近 {days_back} 天 {len(rows)} 筆"]
    if rows:
        lines.append("類型: " + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()))
        lines.append("狀態: " + ", ".join(f"{k}={v}" for k, v in by_status.most_common()))
        lines.append("最近紀錄:")
        for row in rows[:max(1, int(limit or 5))]:
            lines.append(_record_line(row))
    else:
        lines.append("近期沒有會計紀錄。")
    return {
        "text": "\n".join(lines),
        "total": len(rows),
        "by_kind": dict(by_kind),
        "by_status": dict(by_status),
        "recent": rows[:max(1, int(limit or 5))],
    }
