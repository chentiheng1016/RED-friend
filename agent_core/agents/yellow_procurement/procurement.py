"""Yellow procurement helpers.

This module keeps Yellow's department logic read-friendly and side-effect free
for query.* intents. command.* wrappers intentionally go through the existing
factory_supply_chain manager so web confirmations can protect writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping


_STATUS_ORDER = {
    "delayed": 0,
    "pending": 1,
    "confirmed": 2,
    "shipped": 3,
    "delivered": 4,
}


def _manager(manager: Any | None = None) -> Any:
    if manager is not None:
        return manager
    from agent_core import factory_supply_chain

    return factory_supply_chain.supply_chain_manager


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if "T" in text:
        return text.split("T", 1)[0]
    if " " in text:
        return text.split(" ", 1)[0]
    return text


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text + "T00:00:00")
        except ValueError:
            return None


def _supplier_map(manager: Any) -> dict[str, Any]:
    return {
        str(_get(supplier, "id", "")).strip(): supplier
        for supplier in getattr(manager, "suppliers", []) or []
        if str(_get(supplier, "id", "")).strip()
    }


def _supplier_name(manager: Any, supplier_id: str) -> str:
    supplier = _supplier_map(manager).get(supplier_id)
    return str(_get(supplier, "name", "")) if supplier else ""


def _order_items_text(items: list[dict[str, Any]], *, max_items: int = 3) -> str:
    chunks = []
    for item in items[:max_items]:
        name = str(item.get("name") or item.get("sku") or item.get("material") or "").strip()
        qty = item.get("quantity", "")
        if name and qty != "":
            chunks.append(f"{name} x{qty}")
        elif name:
            chunks.append(name)
    if len(items) > max_items:
        chunks.append(f"+{len(items) - max_items}")
    return ", ".join(chunks)


def _order_to_dict(order: Any, manager: Any) -> dict[str, Any]:
    supplier_id = str(_get(order, "supplier_id", "")).strip()
    items = list(_get(order, "items", []) or [])
    expected = _as_datetime(_get(order, "expected_delivery"))
    actual = _as_datetime(_get(order, "actual_delivery"))
    delay_days = 0
    if expected and actual and actual > expected:
        delay_days = (actual.date() - expected.date()).days
    elif expected and str(_get(order, "status", "")).lower() == "delayed":
        delay_days = max(1, (datetime.now().date() - expected.date()).days)
    return {
        "id": str(_get(order, "id", "")).strip(),
        "supplier_id": supplier_id,
        "supplier_name": _supplier_name(manager, supplier_id),
        "items": items,
        "items_text": _order_items_text(items),
        "order_date": _as_date(_get(order, "order_date")),
        "expected_delivery": _as_date(_get(order, "expected_delivery")),
        "actual_delivery": _as_date(_get(order, "actual_delivery")),
        "status": str(_get(order, "status", "") or "").strip(),
        "total_value": float(_get(order, "total_value", 0) or 0),
        "delay_days": delay_days,
    }


def _supplier_to_dict(supplier: Any, manager: Any) -> dict[str, Any]:
    supplier_id = str(_get(supplier, "id", "")).strip()
    orders = [
        order for order in getattr(manager, "purchase_orders", []) or []
        if str(_get(order, "supplier_id", "")).strip() == supplier_id
    ]
    delayed = [o for o in orders if str(_get(o, "status", "")).lower() == "delayed"]
    return {
        "id": supplier_id,
        "name": str(_get(supplier, "name", "") or ""),
        "category": str(_get(supplier, "category", "") or ""),
        "contact_info": dict(_get(supplier, "contact_info", {}) or {}),
        "performance_score": float(_get(supplier, "performance_score", 0) or 0),
        "total_orders": len(orders),
        "delayed_orders": len(delayed),
        "last_evaluation": _as_date(_get(supplier, "last_evaluation")),
    }


def _format_currency(value: float) -> str:
    return f"${value:,.0f}" if abs(value) >= 1 else f"${value:.2f}"


def get_profile() -> dict[str, Any]:
    return {
        "color": "yellow",
        "department": "採購 / Procurement",
        "summary": "供應商、採購單、到料 ETA、庫存補貨警示與 PO/email 採購紀錄查詢。",
        "query_intents": [
            "query.profile",
            "query.list_purchase_orders",
            "query.purchase_order",
            "query.procurement_eta",
            "query.suppliers",
            "query.supplier_performance",
            "query.inventory_alerts",
            "query.risks",
            "query.report",
            "query.procurement_records",
        ],
        "command_intents": [
            "command.create_purchase_order",
            "command.update_order_status",
        ],
    }


def list_purchase_orders(
    *,
    status: str = "",
    supplier: str = "",
    days: int = 180,
    limit: int = 20,
    delayed_only: bool = False,
    manager: Any | None = None,
) -> dict[str, Any]:
    mgr = _manager(manager)
    status_l = status.strip().lower()
    supplier_l = supplier.strip().lower()
    cutoff = datetime.now() - timedelta(days=max(1, int(days or 180)))
    rows = []
    for order in getattr(mgr, "purchase_orders", []) or []:
        od = _as_datetime(_get(order, "order_date")) or datetime.min
        if days and od < cutoff:
            continue
        order_status = str(_get(order, "status", "") or "").lower()
        if status_l and order_status != status_l:
            continue
        if delayed_only and order_status != "delayed":
            continue
        supplier_id = str(_get(order, "supplier_id", "") or "")
        supplier_name = _supplier_name(mgr, supplier_id)
        if supplier_l and supplier_l not in supplier_id.lower() and supplier_l not in supplier_name.lower():
            continue
        rows.append(_order_to_dict(order, mgr))

    rows.sort(key=lambda row: (
        _STATUS_ORDER.get(str(row.get("status") or "").lower(), 99),
        row.get("expected_delivery") or "9999-12-31",
        row.get("id") or "",
    ))
    rows = rows[:max(1, min(int(limit or 20), 100))]

    lines = [f"🟡 採購單 {len(rows)} 筆"]
    if status_l:
        lines[0] += f"（status={status_l}）"
    if supplier:
        lines[0] += f"（supplier={supplier}）"
    if not rows:
        lines.append("目前沒有符合條件的採購單。")
    for row in rows:
        eta = row.get("expected_delivery") or "-"
        supplier_label = row.get("supplier_name") or row.get("supplier_id") or "-"
        delay = f", delay {row['delay_days']}d" if row.get("delay_days") else ""
        lines.append(
            f"- {row['id']} | {row['status']} | ETA {eta}{delay} | "
            f"{supplier_label} | {row.get('items_text') or '-'} | "
            f"{_format_currency(float(row.get('total_value') or 0))}"
        )
    return {"text": "\n".join(lines), "orders": rows, "total": len(rows)}


def get_purchase_order(
    order_id: str,
    *,
    manager: Any | None = None,
) -> dict[str, Any]:
    po = (order_id or "").strip().upper()
    if not po:
        return {"error": "missing order_id"}
    mgr = _manager(manager)
    for order in getattr(mgr, "purchase_orders", []) or []:
        if str(_get(order, "id", "")).strip().upper() == po:
            row = _order_to_dict(order, mgr)
            lines = [
                f"🟡 採購單 {row['id']}",
                f"供應商: {row.get('supplier_name') or row.get('supplier_id') or '-'}",
                f"狀態: {row.get('status') or '-'}",
                f"下單: {row.get('order_date') or '-'}",
                f"預計到料: {row.get('expected_delivery') or '-'}",
                f"實際到料: {row.get('actual_delivery') or '-'}",
                f"金額: {_format_currency(float(row.get('total_value') or 0))}",
                "項目:",
            ]
            for item in row.get("items") or []:
                lines.append(
                    f"  - {item.get('name') or item.get('sku') or '-'} "
                    f"x{item.get('quantity', '-')} @ {item.get('unit_price', '-')}"
                )
            return {"text": "\n".join(lines), "order": row, "found": True}
    return {"text": f"查無採購單：{po}", "order_id": po, "found": False}


def procurement_eta(
    *,
    order_id: str = "",
    product: str = "",
    material: str = "",
    supplier: str = "",
    manager: Any | None = None,
) -> dict[str, Any]:
    mgr = _manager(manager)
    order_id_u = (order_id or "").strip().upper()
    needle = (material or product or "").strip().lower()
    supplier_l = (supplier or "").strip().lower()
    candidates = []
    for order in getattr(mgr, "purchase_orders", []) or []:
        row = _order_to_dict(order, mgr)
        if order_id_u and row["id"].upper() != order_id_u:
            continue
        if supplier_l and supplier_l not in row["supplier_id"].lower() and supplier_l not in row["supplier_name"].lower():
            continue
        if needle:
            blob = " ".join(
                str(item.get("name") or item.get("sku") or item.get("material") or "")
                for item in row.get("items") or []
            ).lower()
            if needle not in blob:
                continue
        candidates.append(row)

    candidates.sort(key=lambda row: (
        row.get("expected_delivery") or "9999-12-31",
        row.get("id") or "",
    ))
    if not candidates:
        label = order_id or material or product or supplier or "(no filter)"
        return {
            "status": "not_found",
            "eta": "",
            "text": f"查不到採購 ETA：{label}",
            "matches": [],
        }
    row = candidates[0]
    eta = row.get("expected_delivery") or ""
    return {
        "status": row.get("status") or "",
        "eta": eta,
        "order_id": row.get("id") or "",
        "supplier_id": row.get("supplier_id") or "",
        "supplier_name": row.get("supplier_name") or "",
        "delay_days": row.get("delay_days") or 0,
        "matches": candidates[:5],
        "text": (
            f"🟡 ETA {eta or '-'} | {row.get('id')} | {row.get('status')} | "
            f"{row.get('supplier_name') or row.get('supplier_id') or '-'} | "
            f"{row.get('items_text') or '-'}"
        ),
    }


def list_suppliers(
    *,
    category: str = "",
    min_score: float = 0.0,
    limit: int = 20,
    manager: Any | None = None,
) -> dict[str, Any]:
    mgr = _manager(manager)
    category_l = category.strip().lower()
    rows = []
    for supplier in getattr(mgr, "suppliers", []) or []:
        row = _supplier_to_dict(supplier, mgr)
        if category_l and category_l not in row["category"].lower():
            continue
        if float(row.get("performance_score") or 0) < float(min_score or 0):
            continue
        rows.append(row)
    rows.sort(key=lambda row: (-float(row.get("performance_score") or 0), row.get("id") or ""))
    rows = rows[:max(1, min(int(limit or 20), 100))]

    lines = [f"🟡 供應商 {len(rows)} 筆"]
    if not rows:
        lines.append("目前沒有符合條件的供應商。")
    for row in rows:
        lines.append(
            f"- {row['id']} | {row['name']} | {row['category']} | "
            f"score {row['performance_score']:.2f} | "
            f"orders {row['total_orders']} / delayed {row['delayed_orders']}"
        )
    return {"text": "\n".join(lines), "suppliers": rows, "total": len(rows)}


def supplier_performance(
    supplier_id: str,
    *,
    manager: Any | None = None,
) -> dict[str, Any]:
    sid = (supplier_id or "").strip()
    if not sid:
        return {"error": "missing supplier_id"}
    mgr = _manager(manager)
    if hasattr(mgr, "evaluate_supplier_performance"):
        result = mgr.evaluate_supplier_performance(sid)
    else:
        result = {"error": "manager does not support evaluate_supplier_performance"}
    if isinstance(result, Mapping) and result.get("error"):
        return dict(result)
    text = (
        f"🟡 {result.get('supplier_name') or sid} 績效\n"
        f"score: {result.get('performance_score')}\n"
        f"orders: {result.get('total_orders')} | delivered: {result.get('delivered_orders')}\n"
        f"on-time: {result.get('on_time_delivery_rate')} | avg delay: {result.get('average_delay_days')}d"
    )
    out = dict(result)
    out["text"] = text
    return out


def inventory_alerts(*, manager: Any | None = None) -> dict[str, Any]:
    mgr = _manager(manager)
    alerts = mgr.check_inventory_levels() if hasattr(mgr, "check_inventory_levels") else []
    lines = [f"🟡 庫存補貨 / 積壓警示 {len(alerts)} 項"]
    if not alerts:
        lines.append("目前沒有庫存警示。")
    for alert in alerts[:20]:
        lines.append(f"- {alert.get('item_name') or alert.get('item_id')}: {alert.get('message')}")
    return {"text": "\n".join(lines), "alerts": alerts, "total": len(alerts)}


def supply_chain_risks(*, manager: Any | None = None) -> dict[str, Any]:
    mgr = _manager(manager)
    risks = mgr.get_supply_chain_risks() if hasattr(mgr, "get_supply_chain_risks") else []
    lines = [f"🟡 供應鏈風險 {len(risks)} 項"]
    if not risks:
        lines.append("目前沒有系統判定的供應鏈風險。")
    for risk in risks[:20]:
        lines.append(
            f"- {risk.get('severity', '?')} | {risk.get('risk_type', '?')}: "
            f"{risk.get('recommendation', '')}"
        )
    return {"text": "\n".join(lines), "risks": risks, "total": len(risks)}


def supply_chain_report(*, manager: Any | None = None) -> dict[str, Any]:
    mgr = _manager(manager)
    if hasattr(mgr, "generate_supply_chain_report"):
        return {"text": mgr.generate_supply_chain_report()}
    return {"error": "manager does not support generate_supply_chain_report"}


def procurement_records(
    *,
    po_number: str = "",
    customer: str = "",
    product: str = "",
    days_back: int = 365,
    max_events: int = 20,
) -> dict[str, Any]:
    po = (po_number or "").strip()
    cust = (customer or "").strip()
    prod = (product or "").strip()
    try:
        from agent_core import email_timeline

        if po:
            text = email_timeline.query_po_timeline(po, max_events=max_events)
            return {"text": text, "source": "email_timeline", "po_number": po}
        if cust:
            text = email_timeline.query_customer_timeline(
                cust,
                days_back=int(days_back or 365),
                product=prod,
                max_events=max_events,
            )
            return {"text": text, "source": "email_timeline", "customer": cust}
    except Exception as exc:
        return {"error": f"email_timeline unavailable: {type(exc).__name__}: {exc}"}
    return {
        "error": "missing po_number or customer",
        "hint": "payload 範例：{\"po_number\":\"JF0P...\"} 或 {\"customer\":\"PAX\",\"days_back\":180}",
    }


def create_purchase_order(
    *,
    supplier_id: str,
    items: list[dict[str, Any]],
    expected_delivery_days: int = 7,
    manager: Any | None = None,
) -> dict[str, Any]:
    sid = (supplier_id or "").strip()
    if not sid:
        return {"ok": False, "error": "missing supplier_id"}
    if (
        not isinstance(items, list)
        or not items
        or not all(isinstance(item, Mapping) for item in items)
    ):
        return {"ok": False, "error": "items must be a non-empty list of JSON objects"}
    clean_items = [dict(item) for item in items]
    mgr = _manager(manager)
    order = mgr.create_purchase_order(
        sid,
        clean_items,
        expected_delivery_days=int(expected_delivery_days or 7),
    )
    if order is None:
        return {"ok": False, "error": f"supplier not found: {sid}"}
    row = _order_to_dict(order, mgr)
    return {
        "ok": True,
        "order": row,
        "text": f"✅ 已建立採購單 {row['id']}，ETA {row.get('expected_delivery') or '-'}",
    }


def update_order_status(
    *,
    order_id: str,
    status: str,
    actual_delivery: str = "",
    manager: Any | None = None,
) -> dict[str, Any]:
    po = (order_id or "").strip()
    st = (status or "").strip().lower()
    if not po:
        return {"ok": False, "error": "missing order_id"}
    allowed = {"pending", "confirmed", "shipped", "delivered", "delayed"}
    if st not in allowed:
        return {"ok": False, "error": f"status must be one of: {', '.join(sorted(allowed))}"}
    actual_dt = _as_datetime(actual_delivery) if actual_delivery else None
    mgr = _manager(manager)
    ok = mgr.update_order_status(po, st, actual_delivery=actual_dt)
    return {
        "ok": bool(ok),
        "order_id": po,
        "status": st,
        "text": f"✅ 已更新 {po} → {st}" if ok else f"❌ 查無採購單：{po}",
    }
