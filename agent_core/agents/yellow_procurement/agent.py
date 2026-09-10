"""Yellow agent — 採購 / Procurement.

採購部門查詢與受控寫入：
  - query.profile
  - query.list_purchase_orders
  - query.purchase_order
  - query.procurement_eta
  - query.suppliers
  - query.supplier_performance
  - query.inventory_alerts
  - query.risks
  - query.report
  - query.procurement_records
  - query.erp_stock
  - query.erp_po
  - query.erp_bom
  - query.erp_demand
  - query.erp_ap
  - query.erp_alloc
  - command.create_purchase_order
  - command.update_order_status
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents._payload import pint, pstr
from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core.agents.yellow_procurement import procurement as _proc


class YellowProcurementAgent(BaseAgent):
    identity = Agent.YELLOW

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        if intent == "query.profile":
            return {"profile": _proc.get_profile()}

        if intent == "query.list_purchase_orders":
            return _proc.list_purchase_orders(
                status=pstr(payload, "status"),
                supplier=pstr(payload, "supplier"),
                days=int(payload.get("days", 180)),
                limit=int(payload.get("limit", 20)),
                delayed_only=bool(payload.get("delayed_only", False)),
            )

        if intent == "query.purchase_order":
            order_id = pstr(payload, "order_id", "po_id", "po_number")
            return _proc.get_purchase_order(order_id)

        if intent == "query.procurement_eta":
            return _proc.procurement_eta(
                order_id=pstr(payload, "order_id", "po_id"),
                product=pstr(payload, "product"),
                material=pstr(payload, "material"),
                supplier=pstr(payload, "supplier"),
            )

        if intent == "query.suppliers":
            return _proc.list_suppliers(
                category=pstr(payload, "category"),
                min_score=float(payload.get("min_score", 0) or 0),
                limit=int(payload.get("limit", 20)),
            )

        if intent == "query.supplier_performance":
            return _proc.supplier_performance(
                pstr(payload, "supplier_id", "id"),
            )

        if intent == "query.inventory_alerts":
            return _proc.inventory_alerts()

        if intent == "query.risks":
            return _proc.supply_chain_risks()

        if intent == "query.report":
            return _proc.supply_chain_report()

        if intent == "query.procurement_records":
            return _proc.procurement_records(
                po_number=pstr(payload, "po_number", "po_id"),
                customer=pstr(payload, "customer"),
                product=pstr(payload, "product"),
                days_back=pint(payload, "days_back", default=365),
                max_events=pint(payload, "max_events", default=20),
            )

        if intent == "query.erp_stock":
            # 確定性庫存查詢（零 LLM）：/dept 通道走這裡，數字直讀 ERP 鏡像。
            from agent_core.erp_stock_query import erp_stock_lookup
            return {"text": erp_stock_lookup(
                pstr(payload, "keyword", "料號", "item", "q"),
                show_lots=bool(payload.get("lots") or payload.get("show_lots")),
            )}

        if intent == "query.erp_po":
            # 確定性採購單查詢（零 LLM）：訂購/已收數量與金額分欄照表念。
            from agent_core.erp_stock_query import erp_po_lookup
            year = pstr(payload, "year", "年")
            return {"text": erp_po_lookup(
                pstr(payload, "keyword", "料號", "item", "q", "po", "單號"),
                date_from=pstr(payload, "date_from", "from") or year,
                date_to=pstr(payload, "date_to", "to") or year,
            )}

        if intent == "query.erp_bom":
            # 確定性 BOM 查詢（零 LLM）：料號→使用款反查／型體·款號→用料，
            # 顏色級照表念（防「同型體別的顏色」外推）。
            from agent_core.erp_stock_query import erp_bom_lookup
            return {"text": erp_bom_lookup(
                pstr(payload, "keyword", "料號", "item", "q", "款號", "style",
                     "model", "型體"),
            )}

        if intent == "query.erp_demand":
            # 確定性訂單實際需求查詢（零 LLM）：訂單材料追蹤照表念——逐生效
            # 訂單 需求/領料/每雙攤提（G407 案：BOM 標準用量≠訂單實際攤提）。
            from agent_core.erp_stock_query import erp_demand_lookup
            return {"text": erp_demand_lookup(
                pstr(payload, "keyword", "料號", "item", "q", "庫存編號"),
            )}

        if intent == "query.erp_ap":
            # 確定性應付請款查詢（零 LLM）：帳單總額含運費等非採購單費用，
            # 採購單金額加總看不到（三寶漏運輸費案）。
            from agent_core.erp_stock_query import erp_ap_lookup
            year = pstr(payload, "year", "年")
            return {"text": erp_ap_lookup(
                pstr(payload, "keyword", "供應商", "vendor", "q", "單號"),
                date_from=pstr(payload, "date_from", "from") or year,
                date_to=pstr(payload, "date_to", "to") or year,
            )}

        if intent == "query.erp_alloc":
            # 確定性庫存分配紀錄查詢（零 LLM）：庫存可用量分配照表念——
            # 哪天分配給哪張指令訂單（G407 案五：批號/需求數字對得上
            # ≠那張單、≠那一天）。date/日期 = 查單日。
            from agent_core.erp_stock_query import erp_allocation_lookup
            day = pstr(payload, "date", "日期")
            return {"text": erp_allocation_lookup(
                pstr(payload, "keyword", "料號", "item", "q", "單號", "批號"),
                date_from=pstr(payload, "date_from", "from") or day,
                date_to=pstr(payload, "date_to", "to") or day,
            )}

        if intent == "command.create_purchase_order":
            return _proc.create_purchase_order(
                supplier_id=pstr(payload, "supplier_id"),
                items=list(payload.get("items") or []),
                expected_delivery_days=pint(payload, "expected_delivery_days", default=7),
            )

        if intent == "command.update_order_status":
            return _proc.update_order_status(
                order_id=pstr(payload, "order_id", "po_id"),
                status=pstr(payload, "status"),
                actual_delivery=pstr(payload, "actual_delivery"),
            )

        raise ValueError(f"YellowProcurementAgent: unknown intent {intent!r}")
