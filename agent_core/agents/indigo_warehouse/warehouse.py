"""Indigo warehouse helpers.

Indigo reads two read-only sources for v1:
  - ``factory_supply_chain.supply_chain_manager.inventory`` for structured
    stock levels.
  - The internal email lake for warehouse-facing stock, inbound/outbound,
    allocation, scrap and adjustment records.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any


_WAREHOUSE_DEPT = "倉庫"
_WAREHOUSE_KEYWORDS = (
    "warehouse", "inventory", "stock", "stock wrong", "received qty",
    "received quantity", "received", "inbound", "outbound", "allocate",
    "allocation", "scrap", "replenish", "reorder", "material", "materials",
    "出入庫", "入庫", "出庫", "庫存", "收貨", "收到", "到料", "領料",
    "分配", "報廢", "補貨", "缺料", "斷料", "裁斷", "用量", "修正",
)
_ATTENTION_KEYWORDS = (
    "wrong", "short", "shortage", "scrap", "delay", "delayed", "urgent",
    "reject", "rejected", "failed", "fail", "庫存不足", "缺料", "斷料",
    "報廢", "錯誤", "修正", "異常", "品質不佳", "拒收", "延誤", "急",
)
_INBOUND_KEYWORDS = (
    "received", "received qty", "receipt", "inbound", "replenish",
    "入庫", "收貨", "收到", "到料", "補貨",
)
_OUTBOUND_KEYWORDS = (
    "outbound", "allocate", "allocation", "issue", "issued", "consume",
    "出庫", "領料", "分配", "裁斷", "用量",
)
_ADJUSTMENT_KEYWORDS = (
    "stock wrong", "adjust", "adjustment", "scrap", "correct", "correction",
    "庫存錯", "修正", "調整", "報廢",
)
_SYSTEM_SUBJECT_MARKERS = ("【小紅", "[小紅", "小紅早安", "小紅 Ponder", "小紅新信")


def _manager(manager: Any | None = None) -> Any:
    if manager is not None:
        return manager
    from agent_core import factory_supply_chain

    return factory_supply_chain.supply_chain_manager


def _load_df():
    from agent_core import email_timeline

    return email_timeline._load_df()


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    getter = getattr(obj, "get", None)
    if callable(getter):
        return getter(name, default)
    return getattr(obj, name, default)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if "T" not in text and " " not in text:
        text += "T00:00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _as_date(value: Any) -> str:
    dt = _as_datetime(value)
    if dt:
        return dt.date().isoformat()
    text = str(value or "").strip()
    if "T" in text:
        return text.split("T", 1)[0]
    if " " in text:
        return text.split(" ", 1)[0]
    return text


def _supplier_map(manager: Any) -> dict[str, Any]:
    return {
        str(_get(supplier, "id", "")).strip(): supplier
        for supplier in getattr(manager, "suppliers", []) or []
        if str(_get(supplier, "id", "")).strip()
    }


def _supplier_name(manager: Any, supplier_id: str) -> str:
    supplier = _supplier_map(manager).get(str(supplier_id or "").strip())
    return str(_get(supplier, "name", "") or "") if supplier else ""


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


def _warehouse_row(row: Any) -> bool:
    subject = str(_get(row, "subject", "") or "")
    if any(marker in subject for marker in _SYSTEM_SUBJECT_MARKERS):
        return False
    primary = str(_get(row, "primary_dept", "") or "").strip()
    if primary == _WAREHOUSE_DEPT:
        return True
    if _WAREHOUSE_DEPT in _as_list(_get(row, "all_depts", [])):
        return True
    return _contains_any(_blob(row), _WAREHOUSE_KEYWORDS)


def _stock_status(current: int, reorder_point: int, max_stock: int) -> str:
    if current <= 0:
        return "out_of_stock"
    if reorder_point and current <= reorder_point:
        return "reorder"
    if max_stock and current / max_stock >= 0.9:
        return "overstock"
    return "available"


def _item_to_dict(item: Any, manager: Any) -> dict[str, Any]:
    current = int(float(_get(item, "current_stock", 0) or 0))
    reorder = int(float(_get(item, "reorder_point", 0) or 0))
    max_stock = int(float(_get(item, "max_stock", 0) or 0))
    supplier_id = str(_get(item, "supplier_id", "") or "").strip()
    stock_ratio = (current / max_stock) if max_stock else 0.0
    return {
        "id": str(_get(item, "id", "") or "").strip(),
        "name": str(_get(item, "name", "") or "").strip(),
        "category": str(_get(item, "category", "") or "").strip(),
        "current_stock": current,
        "reorder_point": reorder,
        "max_stock": max_stock,
        "unit_cost": float(_get(item, "unit_cost", 0) or 0),
        "supplier_id": supplier_id,
        "supplier_name": _supplier_name(manager, supplier_id),
        "last_updated": _as_date(_get(item, "last_updated")),
        "stock_ratio": round(stock_ratio, 3),
        "status": _stock_status(current, reorder, max_stock),
        "in_stock": current > 0,
    }


def _inventory_rows(manager: Any | None = None) -> list[dict[str, Any]]:
    mgr = _manager(manager)
    return [_item_to_dict(item, mgr) for item in getattr(mgr, "inventory", []) or []]


def _inventory_matches(row: dict[str, Any], keyword: str) -> bool:
    needle = (keyword or "").strip().lower()
    if not needle:
        return True
    blob = " ".join(
        str(row.get(name) or "")
        for name in ("id", "name", "category", "supplier_id", "supplier_name")
    ).lower()
    return needle in blob


def _inventory_search(keyword: str, manager: Any | None = None) -> list[dict[str, Any]]:
    needle = (keyword or "").strip().lower()
    rows = [row for row in _inventory_rows(manager) if _inventory_matches(row, needle)]

    def score(row: dict[str, Any]) -> tuple[int, str]:
        item_id = str(row.get("id") or "").lower()
        name = str(row.get("name") or "").lower()
        if needle and item_id == needle:
            rank = 0
        elif needle and name == needle:
            rank = 1
        elif needle and (item_id.startswith(needle) or name.startswith(needle)):
            rank = 2
        else:
            rank = 3
        return (rank, name or item_id)

    rows.sort(key=score)
    return rows


def _inventory_alternatives(
    category: str = "",
    *,
    exclude_id: str = "",
    limit: int = 3,
    manager: Any | None = None,
) -> list[str]:
    category_l = (category or "").strip().lower()
    exclude = (exclude_id or "").strip().lower()
    rows = []
    for row in _inventory_rows(manager):
        if exclude and str(row.get("id") or "").lower() == exclude:
            continue
        if category_l and str(row.get("category") or "").lower() != category_l:
            continue
        if int(row.get("current_stock") or 0) <= int(row.get("reorder_point") or 0):
            continue
        rows.append(row)
    rows.sort(key=lambda r: (-(int(r.get("current_stock") or 0)), str(r.get("name") or "")))
    return [str(row.get("name") or row.get("id") or "") for row in rows[:max(1, limit)]]


def _date_cutoff(days_back: int | None) -> str:
    if not days_back or int(days_back) <= 0:
        return ""
    return (datetime.now() - timedelta(days=int(days_back))).strftime("%Y-%m-%d")


def _movement_type(text: str) -> str:
    if _contains_any(text, _ADJUSTMENT_KEYWORDS):
        return "adjustment"
    if _contains_any(text, _INBOUND_KEYWORDS):
        return "inbound"
    if _contains_any(text, _OUTBOUND_KEYWORDS):
        return "outbound"
    if "forecast" in (text or "").lower() or "預測" in (text or ""):
        return "forecast"
    return "warehouse_record"


def _record_status(text: str, movement: str) -> str:
    if _contains_any(text, _ATTENTION_KEYWORDS):
        return "attention"
    return movement


def _row_to_record(row: Any) -> dict[str, Any]:
    ents = _entities(row)
    text = _blob(row)
    movement = _movement_type(text)
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
        "movement_type": movement,
        "status": _record_status(text, movement),
        "po_numbers": _unique(ents.get("po_numbers") or []),
        "customers": _unique(ents.get("customers") or []),
        "products": _unique(ents.get("products") or []),
        "suppliers": _unique(ents.get("suppliers") or []),
        "amounts": _unique(ents.get("amounts") or []),
        "dates_mentioned": _unique(ents.get("dates_mentioned") or []),
        "actions": _unique(ents.get("actions") or []),
    }


def _record_matches(
    row: dict[str, Any],
    *,
    po_number: str = "",
    customer: str = "",
    product: str = "",
    keyword: str = "",
    movement: str = "",
) -> bool:
    blob = " ".join([
        row.get("record_id", ""),
        row.get("sender", ""),
        row.get("subject", ""),
        row.get("summary", ""),
        row.get("movement_type", ""),
        " ".join(row.get("po_numbers") or []),
        " ".join(row.get("customers") or []),
        " ".join(row.get("products") or []),
        " ".join(row.get("suppliers") or []),
        " ".join(row.get("amounts") or []),
    ]).lower()
    if po_number and po_number.lower() not in blob:
        return False
    if customer and customer.lower() not in blob:
        return False
    if product and product.lower() not in blob:
        return False
    if keyword and keyword.lower() not in blob:
        return False
    if movement and str(row.get("movement_type") or "").lower() != movement.lower():
        return False
    return True


def _search_warehouse_records(
    *,
    po_number: str = "",
    customer: str = "",
    product: str = "",
    keyword: str = "",
    movement: str = "",
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
        if not _warehouse_row(source):
            continue
        row = _row_to_record(source)
        if not _record_matches(
            row,
            po_number=po_number,
            customer=customer,
            product=product,
            keyword=keyword,
            movement=movement,
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


def _inventory_line(row: dict[str, Any]) -> str:
    supplier = row.get("supplier_name") or row.get("supplier_id") or "-"
    return (
        f"- {row.get('id') or '-'} | {row.get('status')} | "
        f"{row.get('name') or '-'} | qty {row.get('current_stock', 0)} "
        f"/ reorder {row.get('reorder_point', 0)} / max {row.get('max_stock', 0)} | "
        f"{row.get('category') or '-'} | {supplier}"
    )


def _record_line(row: dict[str, Any]) -> str:
    po = ", ".join(row.get("po_numbers") or []) or "-"
    customer = ", ".join((row.get("customers") or [])[:2]) or "-"
    product = ", ".join((row.get("products") or [])[:2]) or "-"
    amount = ", ".join((row.get("amounts") or [])[:2]) or "-"
    subject = (row.get("subject") or "").replace("\n", " ")[:80]
    return (
        f"- {row.get('date') or '-'} | {row.get('status')} | "
        f"{row.get('movement_type')} | {customer} | {product} | "
        f"PO {po} | qty/ref {amount} | {subject}"
    )


def get_profile() -> dict[str, Any]:
    return {
        "color": "indigo",
        "department": "倉庫 / Warehouse",
        "summary": "查結構化庫存、庫存可用性、補貨/積壓警示，以及 email lake 的出入庫與庫存紀錄。",
        "data_sources": [
            "agent_core.factory_supply_chain.supply_chain_manager.inventory",
            "var/data/data_lake_internal/emails.parquet",
        ],
        "query_intents": [
            "query.profile",
            "query.list_inventory",
            "query.inventory_item",
            "query.stock_availability",
            "query.inventory_alerts",
            "query.warehouse_records",
        ],
        "command_intents": [],
    }


def list_inventory(
    *,
    category: str = "",
    keyword: str = "",
    low_stock_only: bool = False,
    limit: int = 20,
    manager: Any | None = None,
) -> dict[str, Any]:
    category_l = category.strip().lower()
    rows = []
    for row in _inventory_rows(manager):
        if category_l and str(row.get("category") or "").lower() != category_l:
            continue
        if keyword and not _inventory_matches(row, keyword):
            continue
        if low_stock_only and row.get("status") not in {"out_of_stock", "reorder"}:
            continue
        rows.append(row)
    severity = {"out_of_stock": 0, "reorder": 1, "overstock": 2, "available": 3}
    rows.sort(key=lambda r: (severity.get(str(r.get("status")), 9), str(r.get("name") or "")))
    rows = rows[:max(1, min(int(limit or 20), 100))]

    lines = [f"🟪 倉庫庫存 {len(rows)} 筆"]
    filters = []
    if category:
        filters.append(f"category={category}")
    if keyword:
        filters.append(f"keyword={keyword}")
    if low_stock_only:
        filters.append("low_stock_only=true")
    if filters:
        lines[0] += "（" + ", ".join(filters) + "）"
    if not rows:
        lines.append("目前沒有符合條件的庫存項目。")
    for row in rows:
        lines.append(_inventory_line(row))
    return {"text": "\n".join(lines), "items": rows, "total": len(rows)}


def get_inventory_item(identifier: str, *, manager: Any | None = None) -> dict[str, Any]:
    ident = (identifier or "").strip()
    if not ident:
        return {"error": "missing identifier"}
    rows = _inventory_search(ident, manager)
    if not rows:
        return {"text": f"查無庫存項目：{ident}", "found": False, "identifier": ident}
    row = rows[0]
    lines = [
        f"🟪 庫存項目 {row.get('id') or ident}",
        f"品名: {row.get('name') or '-'}",
        f"類別: {row.get('category') or '-'}",
        f"狀態: {row.get('status') or '-'}",
        f"現有庫存: {row.get('current_stock', 0)}",
        f"補貨點: {row.get('reorder_point', 0)}",
        f"最高庫存: {row.get('max_stock', 0)}",
        f"供應商: {row.get('supplier_name') or row.get('supplier_id') or '-'}",
        f"更新日: {row.get('last_updated') or '-'}",
    ]
    return {"text": "\n".join(lines), "item": row, "found": True}


def stock_availability(
    *,
    product: str = "",
    material: str = "",
    sku: str = "",
    identifier: str = "",
    alternatives: bool = False,
    manager: Any | None = None,
) -> dict[str, Any]:
    needle = (identifier or sku or material or product or "").strip()
    if not needle:
        return {"error": "missing product/material/sku"}
    rows = _inventory_search(needle, manager)
    if rows:
        row = rows[0]
        alts = (
            _inventory_alternatives(
                str(row.get("category") or ""),
                exclude_id=str(row.get("id") or ""),
                manager=manager,
            )
            if alternatives
            else []
        )
        text = (
            f"🟪 庫存可用性：{needle}\n"
            f"{_inventory_line(row)}"
        )
        if alts:
            text += "\n替代料: " + ", ".join(alts)
        return {
            "status": row.get("status") or "",
            "in_stock": bool(row.get("in_stock")),
            "quantity": int(row.get("current_stock") or 0),
            "item": row,
            "alternatives": alts,
            "text": text,
        }

    records = _search_warehouse_records(keyword=needle, days_back=3650, limit=5)
    alts = _inventory_alternatives(manager=manager) if alternatives else []
    lines = [f"🟪 庫存可用性：{needle}", "結構化庫存表查無此項目。"]
    if records:
        lines.append("email lake 找到可能相關的倉庫紀錄：")
        lines.extend(_record_line(row) for row in records)
    if alts:
        lines.append("可先參考高庫存項目: " + ", ".join(alts))
    return {
        "status": "not_found",
        "in_stock": False,
        "quantity": 0,
        "item": None,
        "alternatives": alts,
        "warehouse_records": records,
        "text": "\n".join(lines),
    }


def inventory_alerts(
    *,
    limit: int = 20,
    manager: Any | None = None,
) -> dict[str, Any]:
    mgr = _manager(manager)
    structured = []
    try:
        structured = list(mgr.check_inventory_levels() or [])
    except Exception:
        structured = []

    computed: list[dict[str, Any]] = []
    for row in _inventory_rows(mgr):
        if row.get("status") not in {"out_of_stock", "reorder", "overstock"}:
            continue
        computed.append({
            "item_id": row.get("id") or "",
            "item_name": row.get("name") or "",
            "alert_type": row.get("status") or "",
            "current_stock": row.get("current_stock") or 0,
            "reorder_point": row.get("reorder_point") or 0,
            "max_stock": row.get("max_stock") or 0,
            "message": (
                f"{row.get('name') or row.get('id')} 庫存狀態為 {row.get('status')} "
                f"（現有 {row.get('current_stock')}, 補貨點 {row.get('reorder_point')}）"
            ),
        })

    seen: set[tuple[str, str]] = set()
    alerts: list[dict[str, Any]] = []
    for alert in [*structured, *computed]:
        key = (str(alert.get("item_id") or ""), str(alert.get("alert_type") or alert.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        alerts.append(dict(alert))
    alerts = alerts[:max(1, min(int(limit or 20), 100))]

    lines = [f"🟪 倉庫庫存警示 {len(alerts)} 筆"]
    if not alerts:
        lines.append("目前沒有系統判定的庫存警示。")
    for alert in alerts:
        item = alert.get("item_name") or alert.get("item_id") or "-"
        msg = alert.get("message") or alert.get("alert_type") or "-"
        lines.append(f"- {item}: {msg}")
    return {"text": "\n".join(lines), "alerts": alerts, "total": len(alerts)}


def warehouse_records(
    *,
    po_number: str = "",
    customer: str = "",
    product: str = "",
    keyword: str = "",
    movement: str = "",
    days_back: int = 365,
    max_events: int = 20,
) -> dict[str, Any]:
    if not (po_number or customer or product or keyword or movement):
        return {
            "error": "missing po_number, customer, product, keyword or movement",
            "hint": "payload 範例：{\"product\":\"NY276\"} 或 {\"customer\":\"Jalas\"}",
        }
    rows = _search_warehouse_records(
        po_number=po_number,
        customer=customer,
        product=product,
        keyword=keyword,
        movement=movement,
        days_back=days_back,
        limit=max_events,
    )
    label = po_number or customer or product or keyword or movement
    lines = [f"🟪 倉庫紀錄：{label}（{len(rows)} 筆）"]
    if not rows:
        lines.append("目前沒有符合條件的倉庫 email。")
    for row in rows:
        lines.append(_record_line(row))
    return {"text": "\n".join(lines), "records": rows, "total": len(rows)}
