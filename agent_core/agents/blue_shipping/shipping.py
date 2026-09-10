"""Blue shipping helpers.

Blue currently reads the internal email lake as its source of truth.  The lake
already classifies mail by department, so this module focuses on extracting
shipping-facing fields such as ETD, ETA, AWB, B/L, container numbers, PO numbers
and customer names from rows tagged as 船務.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any


_SHIPPING_DEPT = "船務"
_SHIPPING_KEYWORDS = (
    "shipping", "shipment", "ship", "etd", "eta", "awb", "b/l", "bill of lading",
    "container", "vessel", "air", "courier", "櫃號", "出貨", "出貨單", "貨運",
    "快遞", "空運", "海運", "船期", "報關",
)
_ALERT_KEYWORDS = (
    "delay", "delayed", "postpone", "postponed", "hold", "failed", "fail",
    "revised", "revise", "urgent", "asap", "延", "延期", "延誤", "異常",
    "不合格", "失敗", "扣關", "改", "重寄",
)
_SYSTEM_SUBJECT_MARKERS = ("【小紅", "[小紅", "小紅早安", "小紅 Ponder", "小紅新信")

_CONTAINER_RE = re.compile(r"\b[A-Z]{4}\s?\d{7}\b", re.IGNORECASE)
_AWB_RE = re.compile(
    r"(?:\bAWB\b|\bHAWB\b|\bMAWB\b|tracking|快遞)\s*[:：#-]?\s*([A-Z0-9][A-Z0-9 -]{5,24})",
    re.IGNORECASE,
)
_BL_RE = re.compile(
    r"(?:\bB/L\b|\bBL\b|bill of lading)\s*[:：#-]?\s*([A-Z0-9][A-Z0-9 -]{4,29})",
    re.IGNORECASE,
)
_ETD_RE = re.compile(
    r"\bETD\s*(?:\([^)]*\))?\s*[:：]?\s*"
    r"([A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:,\s*\d{2,4})?|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)",
    re.IGNORECASE,
)
_ETA_RE = re.compile(
    r"\bETA\s*(?:\([^)]*\))?\s*[:：]?\s*"
    r"([A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:,\s*\d{2,4})?|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)",
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
        # all_depts is sometimes stored as JSON-ish text and sometimes plain.
        try:
            loaded = json.loads(value)
            return _as_list(loaded)
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


def _shipping_row(row: Any) -> bool:
    subject = str(_get(row, "subject", "") or "")
    if any(marker in subject for marker in _SYSTEM_SUBJECT_MARKERS):
        return False
    primary = str(_get(row, "primary_dept", "") or "").strip()
    if primary == _SHIPPING_DEPT:
        return True
    if _SHIPPING_DEPT in _as_list(_get(row, "all_depts", [])):
        return True
    return _contains_any(_blob(row), _SHIPPING_KEYWORDS)


def _label_match(regex: re.Pattern[str], text: str) -> str:
    match = regex.search(text or "")
    if not match:
        return ""
    return match.group(1).strip(" .。;,，")


def _shipment_mode(text: str) -> str:
    low = (text or "").lower()
    if any(x in low for x in ("air", "awb", "空運")):
        return "air"
    if any(x in low for x in ("courier", "快遞", "express")):
        return "courier"
    if any(x in low for x in ("vessel", "container", "b/l", "sea", "海運", "櫃")):
        return "sea"
    return ""


def _status_for(row: dict[str, Any], text: str) -> str:
    if _contains_any(text, _ALERT_KEYWORDS):
        return "attention"
    if row.get("eta"):
        return "eta_known"
    if row.get("etd"):
        return "etd_only"
    return "shipping_record"


def _row_to_shipment(row: Any) -> dict[str, Any]:
    ents = _entities(row)
    text = _blob(row)
    po_numbers = _unique(ents.get("po_numbers") or [])
    customers = _unique(ents.get("customers") or [])
    products = _unique(ents.get("products") or [])
    dates = _unique(ents.get("dates_mentioned") or [])
    containers = _unique(m.group(0).replace(" ", "").upper() for m in _CONTAINER_RE.finditer(text))
    awbs = _unique(m.group(1).strip(" .。;,，") for m in _AWB_RE.finditer(text))
    bls = _unique(m.group(1).strip(" .。;,，") for m in _BL_RE.finditer(text))

    out = {
        "shipment_id": str(_get(row, "thread_id", "") or _get(row, "first_message_id", "") or ""),
        "thread_id": str(_get(row, "thread_id", "") or ""),
        "date": str(_get(row, "date", "") or ""),
        "last_message_date": str(_get(row, "last_message_date", "") or ""),
        "sender": str(_get(row, "sender", "") or ""),
        "subject": str(_get(row, "subject", "") or ""),
        "summary": str(_get(row, "summary", "") or ""),
        "direction": str(_get(row, "direction", "") or ""),
        "message_count": int(_get(row, "message_count", 0) or 0),
        "po_numbers": po_numbers,
        "customers": customers,
        "products": products,
        "dates_mentioned": dates,
        "container_numbers": containers,
        "awb_numbers": awbs,
        "bl_numbers": bls,
        "etd": _label_match(_ETD_RE, text),
        "eta": _label_match(_ETA_RE, text),
        "mode": _shipment_mode(text),
    }
    out["status"] = _status_for(out, text)
    return out


def _date_cutoff(days_back: int | None) -> str:
    if not days_back or int(days_back) <= 0:
        return ""
    return (datetime.now() - timedelta(days=int(days_back))).strftime("%Y-%m-%d")


def _matches(row: dict[str, Any], *, customer: str = "", po_number: str = "",
             keyword: str = "", identifier: str = "", status: str = "") -> bool:
    blob = " ".join([
        row.get("shipment_id", ""),
        row.get("sender", ""),
        row.get("subject", ""),
        row.get("summary", ""),
        " ".join(row.get("po_numbers") or []),
        " ".join(row.get("customers") or []),
        " ".join(row.get("products") or []),
        " ".join(row.get("container_numbers") or []),
        " ".join(row.get("awb_numbers") or []),
        " ".join(row.get("bl_numbers") or []),
    ]).lower()
    if customer and customer.lower() not in blob:
        return False
    if po_number and po_number.lower() not in blob:
        return False
    if keyword and keyword.lower() not in blob:
        return False
    if identifier and identifier.lower() not in blob:
        return False
    if status and str(row.get("status") or "").lower() != status.lower():
        return False
    return True


def _search_shipments(
    *,
    customer: str = "",
    po_number: str = "",
    keyword: str = "",
    identifier: str = "",
    status: str = "",
    days_back: int | None = 180,
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
        if not _shipping_row(source):
            continue
        row = _row_to_shipment(source)
        if not _matches(
            row,
            customer=customer,
            po_number=po_number,
            keyword=keyword,
            identifier=identifier,
            status=status,
        ):
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r.get("date") or "", r.get("last_message_date") or "", r.get("shipment_id") or ""), reverse=True)
    return rows[:max(1, min(int(limit or 50), 200))]


def _row_line(row: dict[str, Any]) -> str:
    po = ", ".join(row.get("po_numbers") or []) or "-"
    customer = ", ".join((row.get("customers") or [])[:2]) or "-"
    eta = row.get("eta") or "-"
    etd = row.get("etd") or "-"
    ref = (
        ", ".join(row.get("container_numbers") or [])
        or ", ".join(row.get("awb_numbers") or [])
        or ", ".join(row.get("bl_numbers") or [])
        or row.get("shipment_id", "")[:12]
        or "-"
    )
    subject = (row.get("subject") or "").replace("\n", " ")[:80]
    return (
        f"- {row.get('date') or '-'} | {row.get('status')} | ETD {etd} / ETA {eta} | "
        f"{customer} | PO {po} | {ref} | {subject}"
    )


def get_profile() -> dict[str, Any]:
    return {
        "color": "blue",
        "department": "船務 / Shipping",
        "summary": "查船務 email lake 的出貨狀態、ETD/ETA、AWB/B/L/櫃號、PO 與客戶船務紀錄。",
        "data_sources": [
            "var/data/data_lake_internal/emails.parquet",
            "agent_core.email_timeline",
        ],
        "query_intents": [
            "query.profile",
            "query.list_shipments",
            "query.shipment",
            "query.shipping_eta",
            "query.shipping_records",
            "query.shipping_alerts",
        ],
        "command_intents": [],
    }


def list_shipments(
    *,
    customer: str = "",
    po_number: str = "",
    keyword: str = "",
    status: str = "",
    days_back: int = 180,
    limit: int = 20,
) -> dict[str, Any]:
    rows = _search_shipments(
        customer=customer,
        po_number=po_number,
        keyword=keyword,
        status=status,
        days_back=days_back,
        limit=limit,
    )
    lines = [f"🔵 船務紀錄 {len(rows)} 筆"]
    filters = []
    if customer:
        filters.append(f"customer={customer}")
    if po_number:
        filters.append(f"po={po_number}")
    if keyword:
        filters.append(f"keyword={keyword}")
    if status:
        filters.append(f"status={status}")
    if filters:
        lines[0] += "（" + ", ".join(filters) + "）"
    if not rows:
        lines.append("目前沒有符合條件的船務紀錄。")
    for row in rows:
        lines.append(_row_line(row))
    return {"text": "\n".join(lines), "shipments": rows, "total": len(rows)}


def get_shipment(identifier: str, *, days_back: int = 3650) -> dict[str, Any]:
    ident = (identifier or "").strip()
    if not ident:
        return {"error": "missing identifier"}
    rows = _search_shipments(identifier=ident, days_back=days_back, limit=5)
    if not rows:
        return {"text": f"查無船務紀錄：{ident}", "found": False, "identifier": ident}
    row = rows[0]
    lines = [
        f"🔵 船務紀錄 {row.get('shipment_id') or ident}",
        f"日期: {row.get('date') or '-'}",
        f"狀態: {row.get('status') or '-'}",
        f"模式: {row.get('mode') or '-'}",
        f"ETD: {row.get('etd') or '-'}",
        f"ETA: {row.get('eta') or '-'}",
        f"客戶: {', '.join(row.get('customers') or []) or '-'}",
        f"PO: {', '.join(row.get('po_numbers') or []) or '-'}",
        f"櫃號: {', '.join(row.get('container_numbers') or []) or '-'}",
        f"AWB: {', '.join(row.get('awb_numbers') or []) or '-'}",
        f"B/L: {', '.join(row.get('bl_numbers') or []) or '-'}",
        f"主旨: {row.get('subject') or '-'}",
    ]
    if row.get("summary"):
        lines.append(f"摘要: {row['summary']}")
    return {"text": "\n".join(lines), "shipment": row, "found": True}


def shipping_eta(
    *,
    identifier: str = "",
    po_number: str = "",
    customer: str = "",
    keyword: str = "",
    days_back: int = 365,
    limit: int = 10,
) -> dict[str, Any]:
    ident = (identifier or "").strip()
    rows = _search_shipments(
        identifier=ident,
        po_number=po_number,
        customer=customer,
        keyword=keyword,
        days_back=days_back,
        limit=max(limit, 20),
    )
    rows = [row for row in rows if row.get("eta") or row.get("etd")][:max(1, int(limit or 10))]
    label = ident or po_number or customer or keyword or "(no filter)"
    if not rows:
        return {"status": "not_found", "text": f"查不到船務 ETA/ETD：{label}", "matches": []}
    first = rows[0]
    lines = [f"🔵 船務 ETA/ETD：{label}"]
    for row in rows:
        lines.append(_row_line(row))
    return {
        "status": first.get("status") or "",
        "eta": first.get("eta") or "",
        "etd": first.get("etd") or "",
        "shipment_id": first.get("shipment_id") or "",
        "matches": rows,
        "text": "\n".join(lines),
    }


def shipping_records(
    *,
    po_number: str = "",
    customer: str = "",
    keyword: str = "",
    days_back: int = 365,
    max_events: int = 20,
) -> dict[str, Any]:
    po = (po_number or "").strip()
    cust = (customer or "").strip()
    key = (keyword or "").strip()
    if not (po or cust or key):
        return {
            "error": "missing po_number, customer or keyword",
            "hint": "payload 範例：{\"po_number\":\"JF0P...\"} 或 {\"customer\":\"PAX\"}",
        }
    rows = _search_shipments(
        po_number=po,
        customer=cust,
        keyword=key,
        days_back=days_back,
        limit=max_events,
    )
    label = po or cust or key
    lines = [f"🔵 船務紀錄：{label}（{len(rows)} 筆）"]
    if not rows:
        lines.append("目前沒有符合條件的船務 email。")
    for row in rows:
        lines.append(_row_line(row))
    return {"text": "\n".join(lines), "records": rows, "total": len(rows)}


def shipping_alerts(*, days_back: int = 60, limit: int = 20) -> dict[str, Any]:
    rows = _search_shipments(days_back=days_back, limit=200)
    alerts = [
        row for row in rows
        if row.get("status") == "attention" or (row.get("etd") and not row.get("eta"))
    ][:max(1, int(limit or 20))]
    lines = [f"🔵 船務注意事項 {len(alerts)} 筆"]
    if not alerts:
        lines.append("近期沒有系統判定的船務警示。")
    for row in alerts:
        lines.append(_row_line(row))
    return {"text": "\n".join(lines), "alerts": alerts, "total": len(alerts)}
