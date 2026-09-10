"""Indigo agent — 倉庫 / Warehouse.

倉庫查詢：
  - query.profile
  - query.list_inventory
  - query.inventory_item
  - query.stock_availability
  - query.inventory_alerts
  - query.warehouse_records
  - query.erp_stock
  - query.erp_po
  - query.erp_alloc
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents._payload import pint, pstr
from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.indigo_warehouse import warehouse as _wh
from agent_core.agents.permission_matrix import Agent


class IndigoWarehouseAgent(BaseAgent):
    identity = Agent.INDIGO

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        if intent == "query.profile":
            return {"profile": _wh.get_profile()}

        if intent == "query.list_inventory":
            return _wh.list_inventory(
                category=pstr(payload, "category"),
                keyword=pstr(payload, "keyword", "query"),
                low_stock_only=bool(payload.get("low_stock_only", False)),
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.inventory_item":
            return _wh.get_inventory_item(
                pstr(
                    payload,
                    "identifier",
                    "item_id",
                    "sku",
                    "material",
                    "product",
                )
            )

        if intent == "query.stock_availability":
            return _wh.stock_availability(
                product=pstr(payload, "product"),
                material=pstr(payload, "material"),
                sku=pstr(payload, "sku"),
                identifier=pstr(payload, "identifier", "item_id"),
                alternatives=bool(payload.get("alternatives", False)),
            )

        if intent == "query.inventory_alerts":
            return _wh.inventory_alerts(
                limit=pint(payload, "limit", default=20),
            )

        if intent == "query.warehouse_records":
            return _wh.warehouse_records(
                po_number=pstr(payload, "po_number", "po_id"),
                customer=pstr(payload, "customer"),
                product=pstr(payload, "product", "material"),
                keyword=pstr(payload, "keyword", "query"),
                movement=pstr(payload, "movement", "movement_type"),
                days_back=pint(payload, "days_back", "days", default=365),
                max_events=pint(payload, "max_events", "limit", default=20),
            )

        if intent == "query.erp_stock":
            # 確定性 ERP 庫存查詢（零 LLM）：同 yellow 的 query.erp_stock，
            # 數字直讀 ERP 鏡像（skills 的 read_warehouse_stock 管耗材 Excel，
            # 面料/主料的權威源在這）。
            from agent_core.erp_stock_query import erp_stock_lookup
            return {"text": erp_stock_lookup(
                pstr(payload, "keyword", "料號", "item", "q"),
                show_lots=bool(payload.get("lots") or payload.get("show_lots")),
            )}

        if intent == "query.erp_po":
            # 確定性採購單查詢（零 LLM）：收貨對單看訂購/已收數量。
            from agent_core.erp_stock_query import erp_po_lookup
            year = pstr(payload, "year", "年")
            return {"text": erp_po_lookup(
                pstr(payload, "keyword", "料號", "item", "q", "po", "單號"),
                date_from=pstr(payload, "date_from", "from") or year,
                date_to=pstr(payload, "date_to", "to") or year,
            )}

        if intent == "query.erp_alloc":
            # 確定性庫存分配紀錄查詢（零 LLM）：同 yellow 的 query.erp_alloc
            # ——倉庫調撥 M01→0 批前看「這批分給了哪張指令訂單、何時分配」。
            from agent_core.erp_stock_query import erp_allocation_lookup
            day = pstr(payload, "date", "日期")
            return {"text": erp_allocation_lookup(
                pstr(payload, "keyword", "料號", "item", "q", "單號", "批號"),
                date_from=pstr(payload, "date_from", "from") or day,
                date_to=pstr(payload, "date_to", "to") or day,
            )}

        raise ValueError(f"IndigoWarehouseAgent: unknown intent {intent!r}")
