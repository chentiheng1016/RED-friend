"""`/dept` Telegram 指令 — 把訊息直接路由到部門 agent.

格式：
    /dept                              → 顯示用法 + 已註冊部門
    /dept <color>                      → 顯示用法（提示要加 intent）
    /dept <color> query.<name>         → 用空 payload 呼叫
    /dept <color> query.<name> <json>  → 用 JSON payload 呼叫

呼叫者一律以 Agent.RED（大王本人 = SUPER_ADMIN）身份打。Red 不在 registry
內，但矩陣允許 Red 查任何人，所以這個指令可以打到所有已註冊部門。

設計目標：
  - **只放行 query.* intent** — command.* 會 mutate state（產報價、寄信、
    寫 specs/）且 /dept 短路繞過 tg_auth 的 +確認 sensitive-tool gate，
    所以僅允許唯讀查詢，避免 SUPER_ADMIN 寫操作無確認後門。
  - 純 read-only 接點 — daemon_telegram.py 既有流程不動，僅多一個 prefix
    短路。Phase 4 升級才考慮把整段對話走部門 agent。
  - registry 採 lazy + 模組級快取，第一次 /dept 才建（避免 daemon 啟動失敗）。
  - 任何錯誤都回 Telegram-friendly 的字串，不 raise（避免 daemon 崩潰）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Tuple

from agent_core.agents.permission_matrix import Agent


_REGISTRY = None     # type: Optional[Any]
_MIDDLEWARE = None   # type: Optional[Any]
_CONFIRM_SUFFIX = "+確認"


def _coerce_caller(caller: Agent | str | None) -> Agent:
    if isinstance(caller, Agent):
        return caller
    try:
        return Agent(str(caller or Agent.RED.value).strip().lower())
    except ValueError:
        return Agent.RED


def _get_dept() -> Tuple[Any, Any]:
    """Lazy build registry + middleware（首次 /dept 才建）。"""
    global _REGISTRY, _MIDDLEWARE
    if _REGISTRY is None:
        from agent_core.agents.wire import build_default_registry
        _REGISTRY, _MIDDLEWARE = build_default_registry()
    return _REGISTRY, _MIDDLEWARE


def _reset_for_test() -> None:
    """測試用：清掉 module-level cache，強制下一次 _get_dept 重建。"""
    global _REGISTRY, _MIDDLEWARE
    _REGISTRY = None
    _MIDDLEWARE = None


def is_dept_command(user_text: str) -> bool:
    """檢查訊息是否為 /dept 指令（含尾隨參數）。"""
    s = (user_text or "").strip()
    return s == "/dept" or s.startswith("/dept ")


_GREEN_AGENT_COMMANDS = frozenset({
    "/dev", "/green", "/sample", "/sampledev", "/sampleroom", "/樣品室",
})

_GREEN_AGENT_HELP = (
    "🟢 樣品室 Agent（green / 樣品室）\n\n"
    "用法：\n"
    "  /dev profile\n"
    "  /dev recipes\n"
    "  /dev recipe <recipe_id>\n"
    "  /dev samples [open|delayed|closed|all]\n"
    "  /dev sample <sample_id>\n"
    "  /dev erpsample <料號|庫存編號|SR樣品單號|鞋款>\n"
    "  /dev validate <json_payload>\n"
    "  /dev draft <json_payload>\n\n"
    "  /dev enqueue <json_payload> +確認\n"
    "  /dev queue [queued|running|waiting_confirm|done|failed]\n"
    "  /dev task <task_id>\n\n"
    "範例：\n"
    "  /dev profile\n"
    "  /dev recipes\n"
    "  /dev sample S-2026-01\n"
    "  /dev erpsample PU468\n"
    "  /dev validate {\"recipe_id\":\"green_create_sample_record\",\"data\":{\"sample_id\":\"S-DEV-1\",\"customer\":\"Jaifung\",\"description\":\"New outsole\",\"sent_date\":\"2026-05-20\",\"expected_feedback_date\":\"2026-05-27\"}}\n"
    "  /dev draft {\"recipe_id\":\"green_update_sample_status\",\"data\":{\"sample_id\":\"S-DEV-1\",\"status\":\"closed\"},\"employee_email\":\"dev@company.example\",\"device_id\":\"mac-dev-01\"}\n\n"
    "這個入口可排樣品室 Edge 任務，但真正 ERP 寫入仍要等電腦端 Edge Agent + 人工確認。"
)

_BLUE_AGENT_COMMANDS = frozenset({"/shipping", "/ship", "/blue", "/logistics", "/船務"})
_ORANGE_AGENT_COMMANDS = frozenset({"/sales", "/orange", "/biz", "/business", "/業務"})
_YELLOW_AGENT_COMMANDS = frozenset({"/purchase", "/procurement", "/yellow", "/po", "/採購"})
_INDIGO_AGENT_COMMANDS = frozenset({"/warehouse", "/stock", "/inventory", "/indigo", "/倉庫"})
_PURPLE_AGENT_COMMANDS = frozenset({
    "/accounting", "/acct", "/purple", "/invoice", "/payment", "/會計",
})
_GRAY_AGENT_COMMANDS = frozenset({
    "/production", "/prod", "/factory", "/gray", "/anomaly", "/生產",
})
_BLACK_AGENT_COMMANDS = frozenset({
    "/cashier", "/cash", "/black", "/treasury", "/expense", "/出納", "/收支",
})
_WHITE_AGENT_COMMANDS = frozenset({
    "/legal", "/white", "/sot", "/spec", "/specs", "/contract", "/docs",
    "/法務", "/規格", "/合約",
})

_BLUE_AGENT_HELP = (
    "🔵 船務部門 Agent（blue / 船務）\n\n"
    "用法：\n"
    "  /shipping profile\n"
    "  /shipping shipments [keyword]\n"
    "  /shipping shipment <thread_id|PO|AWB|櫃號>\n"
    "  /shipping eta <PO|AWB|客戶|關鍵字>\n"
    "  /shipping records po <po_number>\n"
    "  /shipping records customer <customer> [days]\n"
    "  /shipping alerts\n\n"
    "進階：\n"
    "  /dept blue query.list_shipments {\"customer\":\"LURCHI\",\"days_back\":90}\n"
    "  /dept blue query.shipping_eta {\"po_number\":\"LJF26040067\"}\n"
    "  /dept blue query.shipping_records {\"customer\":\"JALAS\",\"days_back\":180}\n\n"
    "資料來源目前是內部 email lake 的船務信件，不會寫入 ERP。"
)

_ORANGE_AGENT_HELP = (
    "🟠 業務部門 Agent（orange / 業務）\n\n"
    "用法：\n"
    "  /sales customer <customer> [days]\n"
    "  /sales active [days] [min_emails]\n"
    "  /sales alerts [days]\n"
    "  /sales quote <customer>\n"
    "  /sales quote <json_payload>\n"
    "  /sales search <query>\n\n"
    "進階：\n"
    "  /sales query.customer_360 {\"customer\":\"PAX\",\"days\":60}\n"
    "  /sales query.quote_history {\"customer\":\"PAX\",\"sku\":\"ABC\"}\n"
    "  /sales query.search_emails {\"query\":\"PAX 報價\",\"n_results\":5}\n\n"
    "範例：\n"
    "  /sales customer Decathlon\n"
    "  /sales customer PAX 60\n"
    "  /sales active 90 2\n"
    "  /sales alerts\n"
    "  /sales quote {\"customer\":\"PAX\",\"recent_months\":12}\n"
    "  /sales search PAX outsole quote\n\n"
    "這個入口目前只做業務查詢；Gmail 同步等寫入操作請走 /ingest orange ... +確認。"
)

_YELLOW_AGENT_HELP = (
    "🟡 採購部門 Agent（yellow / 採購）\n\n"
    "用法：\n"
    "  /purchase profile\n"
    "  /purchase pos [status]\n"
    "  /purchase po <po_id>\n"
    "  /purchase eta <po_id 或材料關鍵字>\n"
    "  /purchase suppliers [category]\n"
    "  /purchase supplier <supplier_id>\n"
    "  /purchase alerts\n"
    "  /purchase risks\n"
    "  /purchase stock <料號或品名>   ← ERP 各倉結存（零 AI、照表念；加 lots 看批號）\n"
    "  /purchase erppo <料號|供應商|單號> [年份]  ← ERP 採購單訂購/已收數量（零 AI、照表念）\n"
    "  /purchase bom <料號|型體|客戶款號>  ← ERP 生效版 BOM 用料/反查（零 AI、顏色級照表念）\n"
    "  /purchase demand <料號|庫存編號>  ← ERP 訂單實際需求/每雙攤提（零 AI、訂單材料追蹤照表念）\n"
    "  /purchase ap <供應商|請示單號> [年份]  ← ERP 應付請款帳單（零 AI；含運費等非採購單費用）\n"
    "  /purchase alloc <料號|訂單號|批號> [日期]  ← ERP 庫存分配紀錄（零 AI；哪天分給哪張指令訂單）\n"
    "  /purchase records po <po_number>\n"
    "  /purchase records customer <customer> [days]\n\n"
    "進階：\n"
    "  /dept yellow query.erp_stock {\"keyword\":\"G40\"}\n"
    "  /dept yellow query.erp_po {\"keyword\":\"G40\",\"year\":\"2026\"}\n"
    "  /dept yellow query.erp_bom {\"keyword\":\"DFDT7100145000-E040\"}\n"
    "  /dept yellow query.erp_demand {\"keyword\":\"G407\"}\n"
    "  /dept yellow query.erp_ap {\"keyword\":\"三寶\",\"year\":\"2026\"}\n"
    "  /dept yellow query.erp_alloc {\"keyword\":\"G407\",\"date\":\"2026-07-30\"}\n"
    "  /dept yellow query.list_purchase_orders {\"status\":\"delayed\"}\n"
    "  /dept yellow query.procurement_eta {\"order_id\":\"PO001\"}\n"
    "  /dept yellow query.procurement_records {\"po_number\":\"JF0P26040054\"}\n\n"
    "寫入類（建立 PO、更新狀態）需走 web command 確認流程。"
)

_INDIGO_AGENT_HELP = (
    "🟪 倉庫部門 Agent（indigo / 倉庫）\n\n"
    "用法：\n"
    "  /warehouse profile\n"
    "  /warehouse inventory [keyword]\n"
    "  /warehouse item <item_id|品名|材料>\n"
    "  /warehouse stock <item_id|品名|材料>\n"
    "  /warehouse erp <料號或品名>   ← 飛越 ERP 各倉結存（零 AI、照表念；加 lots 看批號）\n"
    "  /warehouse erppo <料號|供應商|單號> [年份]  ← ERP 採購單訂購/已收數量（零 AI、照表念）\n"
    "  /warehouse alloc <料號|訂單號|批號> [日期]  ← ERP 庫存分配紀錄（零 AI；批分給哪張單/何時）\n"
    "  /warehouse alerts\n"
    "  /warehouse records po <po_number>\n"
    "  /warehouse records customer <customer> [days]\n"
    "  /warehouse records product <product> [days]\n"
    "  /warehouse records movement <inbound|outbound|adjustment>\n\n"
    "進階：\n"
    "  /dept indigo query.erp_stock {\"keyword\":\"G40\"}\n"
    "  /dept indigo query.list_inventory {\"low_stock_only\":true}\n"
    "  /dept indigo query.stock_availability {\"product\":\"NY276\",\"alternatives\":true}\n"
    "  /dept indigo query.warehouse_records {\"customer\":\"Jalas\",\"days_back\":180}\n\n"
    "資料來源是結構化供應鏈庫存 + 內部 email lake 倉庫信件；erp 指令直讀飛越 ERP"
    "本地鏡像（唯讀），一律不會寫入 ERP。"
)

_PURPLE_AGENT_HELP = (
    "🟣 會計部門 Agent（purple / 會計）\n\n"
    "用法：\n"
    "  /accounting profile\n"
    "  /accounting summary [days]\n"
    "  /accounting records [keyword]\n"
    "  /accounting record <thread_id|PO|金額|關鍵字>\n"
    "  /accounting invoices [counterparty|keyword]\n"
    "  /accounting payments [counterparty|keyword]\n"
    "  /accounting remittance [counterparty|keyword]\n"
    "  /accounting alerts [days]\n\n"
    "進階：\n"
    "  /dept purple query.list_accounting_records {\"counterparty\":\"第一銀行\",\"days_back\":90}\n"
    "  /dept purple query.invoice_records {\"keyword\":\"中華電信\"}\n"
    "  /dept purple query.payment_records {\"po_number\":\"JFPP2603009\"}\n\n"
    "資料來源目前是內部 email lake 的會計信件，不會付款、不會寄信、不會寫入 ERP。"
)

_GRAY_AGENT_HELP = (
    "⚙️ 生產管理 Agent（gray / 生產）\n\n"
    "用法：\n"
    "  /production profile\n"
    "  /production status [筆數]\n"
    "  /production history [筆數]\n"
    "  /production report <json_payload> +確認\n"
    "  /anomaly <json_payload> +確認\n\n"
    "回報異常 payload：\n"
    "  {\"order_id\":\"P-001\",\"product\":\"鞋底 A\",\"customer\":\"PAX\","
    "\"original_ecd\":\"2026-05-30\",\"reason\":\"機台故障\",\"severity\":\"medium\"}\n\n"
    "進階：\n"
    "  /dept gray query.production_status {\"recent_n\":10}\n"
    "  /dept gray query.anomaly_history {\"recent_n\":20}\n\n"
    "回報異常會觸發 Gray Trigger：查 Yellow 採購 ETA、Indigo 庫存、White 規格，"
    "再通知 Orange 業務與 Red。未加 +確認 時只會預覽，不會寫入或通知。"
)

_BLACK_AGENT_HELP = (
    "⚫ 出納部門 Agent（black / 出納）\n\n"
    "用法：\n"
    "  /cashier profile\n"
    "  /cashier summary [days]\n"
    "  /cashier records [keyword]\n"
    "  /cashier payments [counterparty|PO|keyword]\n"
    "  /cashier receipts [counterparty|keyword]\n"
    "  /cashier alerts [days]\n\n"
    "進階：\n"
    "  /dept black query.cash_summary {\"days_back\":30}\n"
    "  /dept black query.cash_records {\"direction\":\"outbound\",\"keyword\":\"Fulltide\"}\n"
    "  /dept black query.cash_receipts {\"counterparty\":\"JAI JYE\"}\n\n"
    "資料來源目前沿用內部 email lake 的會計/金流信件，只查詢，不付款、不寄信、不寫入 ERP。"
)

_WHITE_AGENT_HELP = (
    "⚪ 法務 SoT Agent（white / 法務）\n\n"
    "用法：\n"
    "  /legal profile\n"
    "  /legal specs [customer] [product_model]\n"
    "  /legal spec <customer> <product_model>\n"
    "  /legal version <customer> <product_model> [latest|previous|v...]\n"
    "  /legal compare <customer> <product_model> [old_version]\n"
    "  /legal search <query>\n\n"
    "也可以用 JSON 避免客戶名或料號有空白：\n"
    "  /legal spec {\"customer\":\"Richter\",\"product_model\":\"5001-4292\"}\n"
    "  /legal search 合約 PFAS 保固\n\n"
    "這個入口只開放 query.*；解析規格書或同步 Drive 請走 /ingest white ... +確認。"
)


def is_green_agent_command(user_text: str) -> bool:
    """檢查訊息是否為樣品室 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _GREEN_AGENT_COMMANDS


def is_blue_shipping_command(user_text: str) -> bool:
    """檢查訊息是否為船務部門 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _BLUE_AGENT_COMMANDS


def is_orange_sales_command(user_text: str) -> bool:
    """檢查訊息是否為業務部門 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _ORANGE_AGENT_COMMANDS


def is_yellow_procurement_command(user_text: str) -> bool:
    """檢查訊息是否為採購部門 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _YELLOW_AGENT_COMMANDS


def is_indigo_warehouse_command(user_text: str) -> bool:
    """檢查訊息是否為倉庫部門 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _INDIGO_AGENT_COMMANDS


def is_purple_accounting_command(user_text: str) -> bool:
    """檢查訊息是否為會計部門 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _PURPLE_AGENT_COMMANDS


def is_gray_production_command(user_text: str) -> bool:
    """檢查訊息是否為生產管理部門 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _GRAY_AGENT_COMMANDS


def is_black_cashier_command(user_text: str) -> bool:
    """檢查訊息是否為出納部門 Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _BLACK_AGENT_COMMANDS


def is_white_legal_command(user_text: str) -> bool:
    """檢查訊息是否為法務 SoT Agent 的 Telegram 捷徑。"""
    s = (user_text or "").strip()
    if not s:
        return False
    head = s.split(maxsplit=1)[0].lower()
    return head in _WHITE_AGENT_COMMANDS


def _dept_green(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept green {intent}{payload_text}", caller=caller)


def _dept_blue(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept blue {intent}{payload_text}", caller=caller)


def _dept_orange(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept orange {intent}{payload_text}", caller=caller)


def _dept_yellow(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept yellow {intent}{payload_text}", caller=caller)


def _dept_indigo(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept indigo {intent}{payload_text}", caller=caller)


def _erp_po_shortcut(
    rest: str,
    dept: str,
    caller_agent: Agent | str | None,
) -> str:
    """`/purchase erppo` 與 `/warehouse erppo` 共用：<關鍵字> [4 位年份] → query.erp_po。"""
    cmd = "purchase" if dept == "yellow" else "warehouse"
    if not rest:
        return (f"用法：/{cmd} erppo <料號|品名|供應商|採購單號> [年份]"
                f"（例：/{cmd} erppo G40 2026）")
    if rest.startswith("{"):
        return handle_dept_command(
            f"/dept {dept} query.erp_po {rest}",
            caller=caller_agent,
        )
    payload = {"keyword": rest}
    pieces = rest.rsplit(maxsplit=1)
    if len(pieces) == 2 and len(pieces[1]) == 4 and pieces[1].isdigit():
        payload = {"keyword": pieces[0], "year": pieces[1]}
    dispatch = _dept_yellow if dept == "yellow" else _dept_indigo
    return dispatch("query.erp_po", payload, caller=caller_agent)


_ALLOC_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}){0,2}$")


def _erp_alloc_shortcut(
    rest: str,
    dept: str,
    caller_agent: Agent | str | None,
) -> str:
    """`/purchase alloc` 與 `/warehouse alloc` 共用：<關鍵字> [日期] → query.erp_alloc。

    日期收 YYYY / YYYY-MM / YYYY-MM-DD（查該期間的分配紀錄；單日=起訖同值）。
    """
    cmd = "purchase" if dept == "yellow" else "warehouse"
    if not rest:
        return (f"用法：/{cmd} alloc <料號|庫存編號|指令訂單號|批號> [分配日期]"
                f"（例：/{cmd} alloc G407 2026-07-30、/{cmd} alloc JFC26574）")
    if rest.startswith("{"):
        return handle_dept_command(
            f"/dept {dept} query.erp_alloc {rest}",
            caller=caller_agent,
        )
    payload = {"keyword": rest}
    pieces = rest.rsplit(maxsplit=1)
    if len(pieces) == 2 and _ALLOC_DATE_RE.fullmatch(pieces[1]):
        payload = {"keyword": pieces[0], "date": pieces[1]}
    dispatch = _dept_yellow if dept == "yellow" else _dept_indigo
    return dispatch("query.erp_alloc", payload, caller=caller_agent)


def _dept_purple(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept purple {intent}{payload_text}", caller=caller)


def _dept_gray(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept gray {intent}{payload_text}", caller=caller)


def _dept_black(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept black {intent}{payload_text}", caller=caller)


def _dept_white(
    intent: str,
    payload: dict[str, Any] | None = None,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    payload_text = ""
    if payload is not None:
        payload_text = " " + json.dumps(payload, ensure_ascii=False)
    return handle_dept_command(f"/dept white {intent}{payload_text}", caller=caller)


def _dispatch_green(
    intent: str,
    payload: dict[str, Any],
    *,
    caller: Agent | str | None = Agent.RED,
) -> Any:
    from agent_core.agents import AgentRequest

    _registry, middleware = _get_dept()
    return middleware.dispatch(AgentRequest(
        caller=_coerce_caller(caller),
        target=Agent.GREEN,
        intent=intent,
        payload=payload,
    ))


def _dispatch_gray(
    intent: str,
    payload: dict[str, Any],
    *,
    caller: Agent | str | None = Agent.RED,
) -> Any:
    from agent_core.agents import AgentRequest

    _registry, middleware = _get_dept()
    return middleware.dispatch(AgentRequest(
        caller=_coerce_caller(caller),
        target=Agent.GRAY,
        intent=intent,
        payload=payload,
    ))


def _parse_json_payload(payload_text: str) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return None, f"JSON payload 解析失敗：{exc}"
    if not isinstance(payload, dict):
        return None, f"payload 必須是 JSON object，收到 {type(payload).__name__}"
    return payload, ""


def _parse_customer_days(rest: str, *, default_days: int = 90) -> tuple[str, int]:
    pieces = (rest or "").strip().split()
    if not pieces:
        return "", default_days
    days = default_days
    if len(pieces) >= 2 and pieces[-1].isdigit():
        days = int(pieces[-1])
        pieces = pieces[:-1]
    return " ".join(pieces).strip(), days


def _parse_ints(rest: str, defaults: tuple[int, ...]) -> tuple[int, ...]:
    values = list(defaults)
    for idx, piece in enumerate((rest or "").strip().split()):
        if idx >= len(values):
            break
        if piece.isdigit():
            values[idx] = int(piece)
    return tuple(values)


def _confirmed_for_green_write(
    *,
    chat_id: str,
    confirmed_suffix: bool,
    caller: Agent | str | None = Agent.RED,
) -> tuple[bool, str]:
    """chat_id 這裡是 tg_auth 的 confirm scope 鍵（綁定群為 "<chat>:<from>"、
    私訊==chat_id）—— 必須跟 mark 端（daemon_telegram._confirm_scope_for）同一把，
    check 與 revoke（_revoke_green_confirm）也要用同一把。"""
    caller_agent = _coerce_caller(caller)
    if caller_agent not in {Agent.RED, Agent.GREEN}:
        return False, "只有樣品室 green 部門或 Red 管理員可以排樣品室 Edge 任務。"
    if not confirmed_suffix:
        return False, "缺少 +確認"
    if not chat_id:
        if caller_agent is Agent.RED:
            return True, ""
        return False, "缺少 Telegram chat_id，無法確認員工身份。"
    try:
        from agent_core.tg_auth import check_confirmed, is_locked_out
        ok, _elapsed = check_confirmed(chat_id)
        if ok:
            return True, ""
        locked, remain = is_locked_out(chat_id)
        if locked:
            return False, f"此 chat 已被 rate-limit 鎖定，還有 {int(remain)} 秒解鎖。"
        return False, "找不到有效的 tg_auth 確認 token；請重新送出 +確認。"
    except Exception:
        if caller_agent is Agent.RED:
            return True, ""
        return False, "tg_auth 確認狀態無法讀取，員工 Telegram 寫入已保守擋下。"


def _revoke_green_confirm(chat_id: str) -> None:
    if not chat_id:
        return
    try:
        from agent_core.tg_auth import revoke_after_use
        revoke_after_use(chat_id)
    except Exception:
        pass


def _confirmed_for_gray_write(
    *,
    chat_id: str,
    confirmed_suffix: bool,
    caller: Agent | str | None = Agent.RED,
) -> tuple[bool, str]:
    """chat_id 同 _confirmed_for_green_write：傳入的是 confirm scope 鍵，
    check 與 revoke（_revoke_gray_confirm）必須用同一把。"""
    caller_agent = _coerce_caller(caller)
    if caller_agent not in {Agent.RED, Agent.GRAY}:
        return False, "只有生產管理 gray 部門或 Red 管理員可以回報生產異常。"
    if not confirmed_suffix:
        return False, "缺少 +確認"
    if not chat_id:
        if caller_agent is Agent.RED:
            return True, ""
        return False, "缺少 Telegram chat_id，無法確認員工身份。"
    try:
        from agent_core.tg_auth import check_confirmed, is_locked_out
        ok, _elapsed = check_confirmed(chat_id)
        if ok:
            return True, ""
        locked, remain = is_locked_out(chat_id)
        if locked:
            return False, f"此 chat 已被 rate-limit 鎖定，還有 {int(remain)} 秒解鎖。"
        return False, "找不到有效的 tg_auth 確認 token；請重新送出 +確認。"
    except Exception:
        if caller_agent is Agent.RED:
            return True, ""
        return False, "tg_auth 確認狀態無法讀取，員工 Telegram 寫入已保守擋下。"


def _revoke_gray_confirm(chat_id: str) -> None:
    if not chat_id:
        return
    try:
        from agent_core.tg_auth import revoke_after_use
        revoke_after_use(chat_id)
    except Exception:
        pass


def _validate_gray_anomaly_payload(payload: dict[str, Any]) -> str:
    for field in ("order_id", "product", "customer", "original_ecd", "reason"):
        if payload.get(field) is None:
            return f"{field} 不能為 null"
        if not str(payload.get(field, "")).strip():
            return f"{field} 不能為空"
    severity = str(payload.get("severity", "medium")).strip().lower()
    if severity not in {"low", "medium", "high"}:
        return "severity 必須是 low / medium / high"
    return ""


def _format_gray_anomaly_preview(payload: dict[str, Any]) -> str:
    severity = str(payload.get("severity", "medium")).strip().lower()
    return (
        "⚠️ 即將回報生產異常並觸發 Gray Trigger。\n"
        "這會查 Yellow/Indigo/White，並通知 Orange 與 Red。\n"
        "確認請在指令末尾加 +確認 重新送出。\n\n"
        f"訂單：{str(payload.get('order_id', '')).strip()}\n"
        f"產品：{str(payload.get('product', '')).strip()}\n"
        f"客戶：{str(payload.get('customer', '')).strip()}\n"
        f"原定交期：{str(payload.get('original_ecd', '')).strip()}\n"
        f"嚴重度：{severity}\n"
        f"原因：{str(payload.get('reason', '')).strip()}"
    )


def handle_green_agent_command(
    user_text: str,
    chat_id: str = "",
    *,
    caller: Agent | str | None = Agent.RED,
    confirm_scope: str = "",
) -> str:
    """處理 `/dev` / `/green` 樣品室捷徑。

    This is intentionally a thin alias over `/dept green query.*`, so Telegram
    gets a friendlier surface without creating a second security path.

    confirm_scope：+確認 的 scope 鍵（daemon_telegram._confirm_scope_for 的結果；
    綁定群為 "<chat_id>:<from_id>"）。mark 端用它記 token，這裡 check/revoke 必須
    同一把 key，否則群組內 +確認 死鎖。留空退回 chat_id（私訊零行為改變）。
    """
    parts = (user_text or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in _GREEN_AGENT_COMMANDS:
        return _GREEN_AGENT_HELP
    if len(parts) == 1:
        return _GREEN_AGENT_HELP

    action = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _GREEN_AGENT_HELP

    if action == "profile":
        return _dept_green("query.profile", caller=caller_agent)

    if action in {"recipes", "recipe-list"}:
        return _dept_green("query.rpa_recipes", caller=caller_agent)

    if action == "recipe":
        if not rest:
            return "用法：/dev recipe <recipe_id>"
        return _dept_green("query.rpa_recipe", {"recipe_id": rest}, caller=caller_agent)

    if action in {"samples", "list"}:
        status = rest or "open"
        return _dept_green("query.list_samples", {"status": status}, caller=caller_agent)

    if action == "sample":
        if not rest:
            return "用法：/dev sample <sample_id>"
        return _dept_green("query.sample_status", {"sample_id": rest}, caller=caller_agent)

    if action in {"erpsample", "sr", "樣品單"}:
        # 確定性 ERP 樣品單查詢（零 LLM；2026-07-29 UserC PU468 案）。
        if not rest:
            return ("用法：/dev erpsample <料號|庫存編號|SR樣品單號|鞋款>"
                    "（例：/dev erpsample PU468、/dev erpsample SR2511001）")
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept green query.erp_sample {rest}",
                caller=caller_agent,
            )
        return _dept_green(
            "query.erp_sample",
            {"keyword": rest},
            caller=caller_agent,
        )

    if action == "validate":
        if not rest:
            return "用法：/dev validate <json_payload>"
        return handle_dept_command(
            f"/dept green query.validate_rpa_payload {rest}",
            caller=caller_agent,
        )

    if action in {"draft", "task-draft"}:
        if not rest:
            return "用法：/dev draft <json_payload>"
        return handle_dept_command(
            f"/dept green query.edge_task_draft {rest}",
            caller=caller_agent,
        )

    if action == "enqueue":
        if not rest:
            return "用法：/dev enqueue <json_payload> +確認"
        confirmed_suffix = rest.endswith(_CONFIRM_SUFFIX)
        payload_text = rest[: -len(_CONFIRM_SUFFIX)].rstrip() if confirmed_suffix else rest
        payload, error = _parse_json_payload(payload_text)
        if error:
            return f"❌ {error}"
        if not confirmed_suffix:
            preview = handle_dept_command(
                f"/dept green query.edge_task_draft {payload_text}",
                caller=caller_agent,
            )
            return (
                "⚠️ 即將把這筆樣品室 Edge 任務排進佇列。\n"
                "這不會直接操作 ERP，但會讓電腦端 Edge Agent 看到任務。\n"
                "確認請在指令末尾加 +確認 重新送出。\n\n"
                f"{preview}"
            )
        ok, reason = _confirmed_for_green_write(
            chat_id=confirm_scope or chat_id,
            confirmed_suffix=confirmed_suffix,
            caller=caller_agent,
        )
        if not ok:
            return f"🔒 {reason}"
        try:
            result = _dispatch_green(
                "command.enqueue_edge_task",
                payload or {},
                caller=caller_agent,
            )
            _revoke_green_confirm(confirm_scope or chat_id)
            return _format_result(Agent.GREEN, "command.enqueue_edge_task", result)
        except Exception as exc:
            return f"❌ enqueue 失敗（{type(exc).__name__}）：{exc}"

    if action == "queue":
        payload: dict[str, Any] = {"limit": 10}
        if rest:
            payload["status"] = rest
        return _dept_green("query.edge_tasks", payload, caller=caller_agent)

    if action == "task":
        if not rest:
            return "用法：/dev task <task_id>"
        return _dept_green("query.edge_task", {"task_id": rest}, caller=caller_agent)

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept green {action}{suffix}", caller=caller_agent)

    return f"❌ 未知樣品室指令：{action!r}\n\n{_GREEN_AGENT_HELP}"


def handle_blue_shipping_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 `/shipping` / `/blue` 船務部門捷徑。"""
    parts = (user_text or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in _BLUE_AGENT_COMMANDS:
        return _BLUE_AGENT_HELP
    if len(parts) == 1:
        return _BLUE_AGENT_HELP

    action = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _BLUE_AGENT_HELP

    if action == "profile":
        return _dept_blue("query.profile", caller=caller_agent)

    if action in {"shipments", "shipment-list", "list", "出貨", "船務"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept blue query.list_shipments {rest}",
                caller=caller_agent,
            )
        payload: dict[str, Any] = {"limit": 20}
        if rest:
            payload["keyword"] = rest
        return _dept_blue("query.list_shipments", payload, caller=caller_agent)

    if action in {"shipment", "record", "detail"}:
        if not rest:
            return "用法：/shipping shipment <thread_id|PO|AWB|櫃號>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept blue query.shipment {rest}",
                caller=caller_agent,
            )
        return _dept_blue("query.shipment", {"identifier": rest}, caller=caller_agent)

    if action in {"eta", "etd", "交期", "船期"}:
        if not rest:
            return "用法：/shipping eta <PO|AWB|客戶|關鍵字>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept blue query.shipping_eta {rest}",
                caller=caller_agent,
            )
        key = "po_number" if any(ch.isdigit() for ch in rest) and len(rest) >= 5 else "keyword"
        return _dept_blue("query.shipping_eta", {key: rest}, caller=caller_agent)

    if action in {"records", "timeline", "emails", "紀錄"}:
        if not rest:
            return "用法：/shipping records po <po_number> 或 /shipping records customer <customer> [days]"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept blue query.shipping_records {rest}",
                caller=caller_agent,
            )
        pieces = rest.split(maxsplit=2)
        mode = pieces[0].lower()
        if mode in {"po", "order"} and len(pieces) >= 2:
            return _dept_blue(
                "query.shipping_records",
                {"po_number": pieces[1]},
                caller=caller_agent,
            )
        if mode in {"customer", "cust", "客戶"} and len(pieces) >= 2:
            payload = {"customer": pieces[1]}
            if len(pieces) >= 3 and pieces[2].isdigit():
                payload["days_back"] = int(pieces[2])
            return _dept_blue("query.shipping_records", payload, caller=caller_agent)
        return _dept_blue("query.shipping_records", {"keyword": rest}, caller=caller_agent)

    if action in {"alerts", "alert", "警示", "注意"}:
        days = _parse_ints(rest, (60,))[0]
        return _dept_blue("query.shipping_alerts", {"days_back": days}, caller=caller_agent)

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept blue {action}{suffix}", caller=caller_agent)

    return f"❌ 未知船務部門指令：{action!r}\n\n{_BLUE_AGENT_HELP}"


def handle_orange_sales_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 `/sales` / `/orange` 業務部門捷徑。"""
    parts = (user_text or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in _ORANGE_AGENT_COMMANDS:
        return _ORANGE_AGENT_HELP
    if len(parts) == 1:
        return _ORANGE_AGENT_HELP

    action = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _ORANGE_AGENT_HELP

    if action in {"customer", "customer360", "360", "客戶"}:
        if not rest:
            return "用法：/sales customer <customer> [days]"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept orange query.customer_360 {rest}",
                caller=caller_agent,
            )
        customer, days = _parse_customer_days(rest, default_days=90)
        if not customer:
            return "用法：/sales customer <customer> [days]"
        return _dept_orange(
            "query.customer_360",
            {"customer": customer, "days": days},
            caller=caller_agent,
        )

    if action in {"active", "customers", "活躍"}:
        days, min_emails = _parse_ints(rest, (90, 2))
        return _dept_orange(
            "query.active_customers",
            {"days": days, "min_emails": min_emails},
            caller=caller_agent,
        )

    if action in {"alerts", "alert", "警示"}:
        days = _parse_ints(rest, (90,))[0]
        return _dept_orange("query.customer_alerts", {"days": days}, caller=caller_agent)

    if action in {"quote", "quotes", "報價"}:
        if not rest:
            return "用法：/sales quote <customer> 或 /sales quote <json_payload>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept orange query.quote_history {rest}",
                caller=caller_agent,
            )
        return _dept_orange("query.quote_history", {"customer": rest}, caller=caller_agent)

    if action in {"search", "email", "emails", "搜尋"}:
        if not rest:
            return "用法：/sales search <query>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept orange query.search_emails {rest}",
                caller=caller_agent,
            )
        return _dept_orange(
            "query.search_emails",
            {"query": rest, "n_results": 5},
            caller=caller_agent,
        )

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept orange {action}{suffix}", caller=caller_agent)

    return f"❌ 未知業務部門指令：{action!r}\n\n{_ORANGE_AGENT_HELP}"


def handle_yellow_procurement_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 `/purchase` / `/yellow` 採購部門捷徑。"""
    parts = (user_text or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in _YELLOW_AGENT_COMMANDS:
        return _YELLOW_AGENT_HELP
    if len(parts) == 1:
        return _YELLOW_AGENT_HELP

    action = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _YELLOW_AGENT_HELP

    if action == "profile":
        return _dept_yellow("query.profile", caller=caller_agent)

    if action in {"pos", "orders", "order-list", "採購單"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.list_purchase_orders {rest}",
                caller=caller_agent,
            )
        payload = {"limit": 20}
        if rest:
            payload["status"] = rest
        return _dept_yellow("query.list_purchase_orders", payload, caller=caller_agent)

    if action in {"po", "order"}:
        if not rest:
            return "用法：/purchase po <po_id>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.purchase_order {rest}",
                caller=caller_agent,
            )
        return _dept_yellow("query.purchase_order", {"order_id": rest}, caller=caller_agent)

    if action in {"eta", "交期", "到料"}:
        if not rest:
            return "用法：/purchase eta <po_id 或材料關鍵字>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.procurement_eta {rest}",
                caller=caller_agent,
            )
        key = "order_id" if rest.upper().startswith("PO") else "material"
        return _dept_yellow("query.procurement_eta", {key: rest}, caller=caller_agent)

    if action in {"suppliers", "supplier-list", "vendors", "供應商"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.suppliers {rest}",
                caller=caller_agent,
            )
        payload = {"limit": 20}
        if rest:
            payload["category"] = rest
        return _dept_yellow("query.suppliers", payload, caller=caller_agent)

    if action in {"supplier", "vendor"}:
        if not rest:
            return "用法：/purchase supplier <supplier_id>"
        return _dept_yellow(
            "query.supplier_performance",
            {"supplier_id": rest},
            caller=caller_agent,
        )

    if action in {"alerts", "inventory", "庫存", "補貨"}:
        return _dept_yellow("query.inventory_alerts", caller=caller_agent)

    if action in {"risks", "risk", "風險"}:
        return _dept_yellow("query.risks", caller=caller_agent)

    if action in {"stock", "料況", "結存"}:
        if not rest:
            return ("用法：/purchase stock <料號或品名關鍵字>"
                    "（加批號明細：/purchase stock G40 lots）")
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.erp_stock {rest}",
                caller=caller_agent,
            )
        show_lots = False
        if rest.lower().endswith(" lots"):
            rest, show_lots = rest[: -len(" lots")].strip(), True
        return _dept_yellow(
            "query.erp_stock",
            {"keyword": rest, "lots": show_lots},
            caller=caller_agent,
        )

    if action in {"erppo", "poqty", "採購量", "訂購"}:
        return _erp_po_shortcut(rest, "yellow", caller_agent)

    if action in {"bom", "用料", "反查"}:
        if not rest:
            return ("用法：/purchase bom <料號|型體|客戶款號>"
                    "（例：/purchase bom DFDT7100145000-E040、/purchase bom 8916446）")
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.erp_bom {rest}",
                caller=caller_agent,
            )
        return _dept_yellow(
            "query.erp_bom",
            {"keyword": rest},
            caller=caller_agent,
        )

    if action in {"alloc", "allocation", "分配", "庫存分配"}:
        return _erp_alloc_shortcut(rest, "yellow", caller_agent)

    if action in {"demand", "需求", "訂單需求", "攤提"}:
        if not rest:
            return ("用法：/purchase demand <料號|庫存編號>"
                    "（例：/purchase demand G407、"
                    "/purchase demand DFD00400D05700-G050）")
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.erp_demand {rest}",
                caller=caller_agent,
            )
        return _dept_yellow(
            "query.erp_demand",
            {"keyword": rest},
            caller=caller_agent,
        )

    if action in {"ap", "請款", "帳單", "應付"}:
        if not rest:
            return ("用法：/purchase ap <供應商|請示單號> [年份]"
                    "（例：/purchase ap 三寶 2026、/purchase ap JFPA2605007）")
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.erp_ap {rest}",
                caller=caller_agent,
            )
        payload = {"keyword": rest}
        pieces = rest.rsplit(maxsplit=1)
        if len(pieces) == 2 and re.fullmatch(r"\d{4}", pieces[1]):
            payload = {"keyword": pieces[0], "year": pieces[1]}
        return _dept_yellow("query.erp_ap", payload, caller=caller_agent)

    if action in {"report", "summary", "報告"}:
        return _dept_yellow("query.report", caller=caller_agent)

    if action in {"records", "timeline", "emails", "紀錄"}:
        if not rest:
            return "用法：/purchase records po <po_number> 或 /purchase records customer <customer> [days]"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept yellow query.procurement_records {rest}",
                caller=caller_agent,
            )
        pieces = rest.split(maxsplit=2)
        mode = pieces[0].lower()
        if mode in {"po", "order"} and len(pieces) >= 2:
            return _dept_yellow(
                "query.procurement_records",
                {"po_number": pieces[1]},
                caller=caller_agent,
            )
        if mode in {"customer", "cust", "客戶"} and len(pieces) >= 2:
            customer = pieces[1]
            days = 365
            if len(pieces) >= 3 and pieces[2].isdigit():
                days = int(pieces[2])
            return _dept_yellow(
                "query.procurement_records",
                {"customer": customer, "days_back": days},
                caller=caller_agent,
            )
        return _dept_yellow(
            "query.procurement_records",
            {"po_number": rest},
            caller=caller_agent,
        )

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept yellow {action}{suffix}", caller=caller_agent)

    return f"❌ 未知採購部門指令：{action!r}\n\n{_YELLOW_AGENT_HELP}"


def handle_indigo_warehouse_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 `/warehouse` / `/indigo` 倉庫部門捷徑。"""
    parts = (user_text or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in _INDIGO_AGENT_COMMANDS:
        return _INDIGO_AGENT_HELP
    if len(parts) == 1:
        return _INDIGO_AGENT_HELP

    action = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _INDIGO_AGENT_HELP

    if action == "profile":
        return _dept_indigo("query.profile", caller=caller_agent)

    if action in {"inventory", "items", "list", "庫存"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept indigo query.list_inventory {rest}",
                caller=caller_agent,
            )
        payload: dict[str, Any] = {"limit": 20}
        if rest:
            if rest in {"low", "reorder", "缺料", "補貨"}:
                payload["low_stock_only"] = True
            else:
                payload["keyword"] = rest
        return _dept_indigo("query.list_inventory", payload, caller=caller_agent)

    if action in {"item", "detail", "material", "材料"}:
        if not rest:
            return "用法：/warehouse item <item_id|品名|材料>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept indigo query.inventory_item {rest}",
                caller=caller_agent,
            )
        return _dept_indigo("query.inventory_item", {"identifier": rest}, caller=caller_agent)

    if action in {"stock", "availability", "available", "可用", "庫存量"}:
        if not rest:
            return "用法：/warehouse stock <item_id|品名|材料>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept indigo query.stock_availability {rest}",
                caller=caller_agent,
            )
        return _dept_indigo(
            "query.stock_availability",
            {"product": rest, "alternatives": True},
            caller=caller_agent,
        )

    if action in {"erp", "erpstock", "結存"}:
        if not rest:
            return ("用法：/warehouse erp <料號或品名關鍵字>"
                    "（加批號明細：/warehouse erp G40 lots）")
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept indigo query.erp_stock {rest}",
                caller=caller_agent,
            )
        show_lots = False
        if rest.lower().endswith(" lots"):
            rest, show_lots = rest[: -len(" lots")].strip(), True
        return _dept_indigo(
            "query.erp_stock",
            {"keyword": rest, "lots": show_lots},
            caller=caller_agent,
        )

    if action in {"erppo", "poqty", "採購量", "訂購"}:
        return _erp_po_shortcut(rest, "indigo", caller_agent)

    if action in {"alloc", "allocation", "分配", "庫存分配"}:
        return _erp_alloc_shortcut(rest, "indigo", caller_agent)

    if action in {"alerts", "alert", "low", "reorder", "警示", "補貨"}:
        return _dept_indigo("query.inventory_alerts", caller=caller_agent)

    if action in {"records", "timeline", "emails", "movements", "紀錄", "出入庫"}:
        if not rest:
            return (
                "用法：/warehouse records po <po_number>、"
                "/warehouse records customer <customer> [days]、"
                "/warehouse records product <product> [days]"
            )
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept indigo query.warehouse_records {rest}",
                caller=caller_agent,
            )
        pieces = rest.split(maxsplit=2)
        mode = pieces[0].lower()
        if mode in {"po", "order"} and len(pieces) >= 2:
            return _dept_indigo(
                "query.warehouse_records",
                {"po_number": pieces[1]},
                caller=caller_agent,
            )
        if mode in {"customer", "cust", "客戶"} and len(pieces) >= 2:
            customer = pieces[1]
            days = 365
            if len(pieces) >= 3 and pieces[2].isdigit():
                days = int(pieces[2])
            return _dept_indigo(
                "query.warehouse_records",
                {"customer": customer, "days_back": days},
                caller=caller_agent,
            )
        if mode in {"product", "material", "sku", "品名", "材料"} and len(pieces) >= 2:
            product = pieces[1]
            days = 365
            if len(pieces) >= 3 and pieces[2].isdigit():
                days = int(pieces[2])
            return _dept_indigo(
                "query.warehouse_records",
                {"product": product, "days_back": days},
                caller=caller_agent,
            )
        if mode in {"movement", "type"} and len(pieces) >= 2:
            return _dept_indigo(
                "query.warehouse_records",
                {"movement": pieces[1]},
                caller=caller_agent,
            )
        return _dept_indigo("query.warehouse_records", {"keyword": rest}, caller=caller_agent)

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept indigo {action}{suffix}", caller=caller_agent)

    return f"❌ 未知倉庫部門指令：{action!r}\n\n{_INDIGO_AGENT_HELP}"


def handle_purple_accounting_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 `/accounting` / `/purple` 會計部門捷徑。"""
    parts = (user_text or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in _PURPLE_AGENT_COMMANDS:
        return _PURPLE_AGENT_HELP
    if len(parts) == 1:
        return _PURPLE_AGENT_HELP

    head = parts[0].lower()
    action = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    direct_term = " ".join(piece for piece in (parts[1], rest) if piece).strip()
    if head == "/invoice":
        payload = {"keyword": direct_term, "limit": 20} if direct_term else {"limit": 20}
        return _dept_purple("query.invoice_records", payload, caller=caller_agent)
    if head == "/payment":
        payload = {"keyword": direct_term, "limit": 20} if direct_term else {"limit": 20}
        return _dept_purple("query.payment_records", payload, caller=caller_agent)

    if action in {"help", "?", "用法"}:
        return _PURPLE_AGENT_HELP

    if action == "profile":
        return _dept_purple("query.profile", caller=caller_agent)

    if action in {"summary", "report", "摘要"}:
        days = _parse_ints(rest, (30,))[0]
        return _dept_purple("query.accounting_summary", {"days_back": days}, caller=caller_agent)

    if action in {"records", "accounts", "list", "emails", "紀錄", "帳目"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept purple query.list_accounting_records {rest}",
                caller=caller_agent,
            )
        payload: dict[str, Any] = {"limit": 20}
        if rest:
            pieces = rest.split(maxsplit=2)
            mode = pieces[0].lower()
            if mode in {"po", "order"} and len(pieces) >= 2:
                payload["po_number"] = pieces[1]
            elif mode in {"counterparty", "customer", "supplier", "bank", "對象"} and len(pieces) >= 2:
                payload["counterparty"] = pieces[1]
            elif mode in {"kind", "type", "類型"} and len(pieces) >= 2:
                payload["kind"] = pieces[1]
            elif mode in {"status", "狀態"} and len(pieces) >= 2:
                payload["status"] = pieces[1]
            else:
                payload["keyword"] = rest
        return _dept_purple("query.list_accounting_records", payload, caller=caller_agent)

    if action in {"record", "detail"}:
        if not rest:
            return "用法：/accounting record <thread_id|PO|金額|關鍵字>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept purple query.accounting_record {rest}",
                caller=caller_agent,
            )
        return _dept_purple("query.accounting_record", {"identifier": rest}, caller=caller_agent)

    if action in {"invoices", "invoice", "bills", "bill", "發票", "帳單"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept purple query.invoice_records {rest}",
                caller=caller_agent,
            )
        payload = {"limit": 20}
        if rest:
            payload["keyword"] = rest
        return _dept_purple("query.invoice_records", payload, caller=caller_agent)

    if action in {"payments", "payment", "paid", "付款", "繳費"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept purple query.payment_records {rest}",
                caller=caller_agent,
            )
        payload = {"limit": 20}
        if rest:
            payload["keyword"] = rest
        return _dept_purple("query.payment_records", payload, caller=caller_agent)

    if action in {"remittance", "remit", "forex", "匯款", "入帳", "水單"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept purple query.remittance_records {rest}",
                caller=caller_agent,
            )
        payload = {"limit": 20}
        if rest:
            payload["keyword"] = rest
        return _dept_purple("query.remittance_records", payload, caller=caller_agent)

    if action in {"alerts", "alert", "警示", "注意"}:
        days = _parse_ints(rest, (90,))[0]
        return _dept_purple("query.accounting_alerts", {"days_back": days}, caller=caller_agent)

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept purple {action}{suffix}", caller=caller_agent)

    return f"❌ 未知會計部門指令：{action!r}\n\n{_PURPLE_AGENT_HELP}"


def handle_gray_production_command(
    user_text: str,
    chat_id: str = "",
    *,
    caller: Agent | str | None = Agent.RED,
    confirm_scope: str = "",
) -> str:
    """處理 `/production` / `/gray` 生產管理部門捷徑。

    confirm_scope 語意同 handle_green_agent_command：+確認 的 scope 鍵，
    留空退回 chat_id（私訊零行為改變）。"""
    raw_parts = (user_text or "").strip().split(maxsplit=2)
    if not raw_parts or raw_parts[0].lower() not in _GRAY_AGENT_COMMANDS:
        return _GRAY_AGENT_HELP
    if len(raw_parts) == 1:
        return _GRAY_AGENT_HELP

    head = raw_parts[0].lower()
    if head == "/anomaly":
        action = "report"
        rest = " ".join(raw_parts[1:]).strip()
    else:
        action = raw_parts[1].strip().lower()
        rest = raw_parts[2].strip() if len(raw_parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _GRAY_AGENT_HELP

    if action == "profile":
        return _dept_gray("query.profile", caller=caller_agent)

    if action in {"status", "orders", "order-list", "list", "生產", "進度"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept gray query.production_status {rest}",
                caller=caller_agent,
            )
        recent_n = _parse_ints(rest, (10,))[0]
        return _dept_gray(
            "query.production_status",
            {"recent_n": recent_n},
            caller=caller_agent,
        )

    if action in {"history", "anomalies", "anomaly", "異常", "紀錄"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept gray query.anomaly_history {rest}",
                caller=caller_agent,
            )
        recent_n = _parse_ints(rest, (20,))[0]
        return _dept_gray(
            "query.anomaly_history",
            {"recent_n": recent_n},
            caller=caller_agent,
        )

    if action in {"report", "trigger", "回報"}:
        if not rest:
            return "用法：/production report <json_payload> +確認"
        confirmed_suffix = rest.endswith(_CONFIRM_SUFFIX)
        payload_text = rest[: -len(_CONFIRM_SUFFIX)].rstrip() if confirmed_suffix else rest
        payload, error = _parse_json_payload(payload_text)
        if error:
            return f"❌ {error}"
        validation_error = _validate_gray_anomaly_payload(payload or {})
        if validation_error:
            return f"❌ {validation_error}"
        if not confirmed_suffix:
            return _format_gray_anomaly_preview(payload or {})
        ok, reason = _confirmed_for_gray_write(
            chat_id=confirm_scope or chat_id,
            confirmed_suffix=confirmed_suffix,
            caller=caller_agent,
        )
        if not ok:
            return f"🔒 {reason}"
        try:
            result = _dispatch_gray(
                "command.report_anomaly",
                payload or {},
                caller=caller_agent,
            )
            _revoke_gray_confirm(confirm_scope or chat_id)
            return _format_result(Agent.GRAY, "command.report_anomaly", result)
        except Exception as exc:
            return f"❌ report anomaly 失敗（{type(exc).__name__}）：{exc}"

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept gray {action}{suffix}", caller=caller_agent)

    return f"❌ 未知生產管理指令：{action!r}\n\n{_GRAY_AGENT_HELP}"


def handle_black_cashier_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 `/cashier` / `/black` 出納部門捷徑。"""
    parts = (user_text or "").strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in _BLACK_AGENT_COMMANDS:
        return _BLACK_AGENT_HELP
    if len(parts) == 1:
        return _BLACK_AGENT_HELP

    action = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _BLACK_AGENT_HELP

    if action == "profile":
        return _dept_black("query.profile", caller=caller_agent)

    if action in {"summary", "report", "摘要"}:
        days = _parse_ints(rest, (30,))[0]
        return _dept_black("query.cash_summary", {"days_back": days}, caller=caller_agent)

    if action in {"records", "transactions", "list", "收支", "紀錄"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept black query.cash_records {rest}",
                caller=caller_agent,
            )
        payload: dict[str, Any] = {"limit": 20}
        if rest:
            pieces = rest.split(maxsplit=2)
            mode = pieces[0].lower()
            if mode in {"in", "inbound", "receipt", "receipts", "入帳", "收入"}:
                payload["direction"] = "inbound"
                if len(pieces) >= 2:
                    payload["keyword"] = " ".join(pieces[1:])
            elif mode in {"out", "outbound", "payment", "payments", "付款", "支出"}:
                payload["direction"] = "outbound"
                if len(pieces) >= 2:
                    payload["keyword"] = " ".join(pieces[1:])
            else:
                payload["keyword"] = rest
        return _dept_black("query.cash_records", payload, caller=caller_agent)

    if action in {"payments", "payment", "pay", "out", "outbound", "付款", "支出"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept black query.cash_payments {rest}",
                caller=caller_agent,
            )
        payload = {"limit": 20}
        if rest:
            payload["keyword"] = rest
        return _dept_black("query.cash_payments", payload, caller=caller_agent)

    if action in {"receipts", "receipt", "in", "inbound", "remittance", "入帳", "收入", "匯入"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept black query.cash_receipts {rest}",
                caller=caller_agent,
            )
        payload = {"limit": 20}
        if rest:
            payload["keyword"] = rest
        return _dept_black("query.cash_receipts", payload, caller=caller_agent)

    if action in {"alerts", "alert", "警示", "注意"}:
        days = _parse_ints(rest, (90,))[0]
        return _dept_black("query.cash_alerts", {"days_back": days}, caller=caller_agent)

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept black {action}{suffix}", caller=caller_agent)

    return f"❌ 未知出納部門指令：{action!r}\n\n{_BLACK_AGENT_HELP}"


def _white_customer_model_from_rest(rest: str) -> tuple[dict[str, Any] | None, str]:
    pieces = (rest or "").strip().split(maxsplit=2)
    if len(pieces) < 2:
        return None, "需要 customer 和 product_model；若有空白請改用 JSON payload。"
    payload: dict[str, Any] = {
        "customer": pieces[0],
        "product_model": pieces[1],
    }
    if len(pieces) >= 3 and pieces[2].strip():
        payload["version"] = pieces[2].strip()
        payload["old_version"] = pieces[2].strip()
    return payload, ""


def handle_white_legal_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 `/legal` / `/white` 法務 SoT 捷徑。"""
    raw_parts = (user_text or "").strip().split(maxsplit=2)
    if not raw_parts or raw_parts[0].lower() not in _WHITE_AGENT_COMMANDS:
        return _WHITE_AGENT_HELP
    head = raw_parts[0].lower()
    direct_heads = {"/specs", "/規格", "/spec", "/contract", "/docs", "/合約"}
    if len(raw_parts) == 1 and head not in direct_heads:
        return _WHITE_AGENT_HELP

    if head in {"/specs", "/規格"}:
        action = "specs"
        rest = " ".join(raw_parts[1:]).strip()
    elif head == "/spec":
        action = "spec"
        rest = " ".join(raw_parts[1:]).strip()
    elif head in {"/contract", "/docs", "/合約"}:
        action = "search"
        rest = " ".join(raw_parts[1:]).strip()
    else:
        action = raw_parts[1].strip().lower()
        rest = raw_parts[2].strip() if len(raw_parts) > 2 else ""
    caller_agent = _coerce_caller(caller)

    if action in {"help", "?", "用法"}:
        return _WHITE_AGENT_HELP

    if action == "profile":
        return _dept_white("query.profile", caller=caller_agent)

    if action in {"specs", "list", "規格列表"}:
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept white query.list_specs {rest}",
                caller=caller_agent,
            )
        payload: dict[str, Any] = {}
        if rest:
            pieces = rest.split(maxsplit=1)
            payload["customer"] = pieces[0]
            if len(pieces) >= 2:
                payload["product_model"] = pieces[1]
        return _dept_white("query.list_specs", payload, caller=caller_agent)

    if action in {"spec", "latest", "get", "規格"}:
        if not rest:
            return "用法：/legal spec <customer> <product_model> 或 /legal spec <json_payload>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept white query.get_latest_spec {rest}",
                caller=caller_agent,
            )
        payload, error = _white_customer_model_from_rest(rest)
        if error:
            return f"❌ {error}"
        return _dept_white("query.get_latest_spec", payload, caller=caller_agent)

    if action in {"version", "版本"}:
        if not rest:
            return "用法：/legal version <customer> <product_model> [latest|previous|v...]"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept white query.get_spec_version {rest}",
                caller=caller_agent,
            )
        payload, error = _white_customer_model_from_rest(rest)
        if error:
            return f"❌ {error}"
        payload.setdefault("version", "latest")
        payload.pop("old_version", None)
        return _dept_white("query.get_spec_version", payload, caller=caller_agent)

    if action in {"compare", "diff", "比對"}:
        if not rest:
            return "用法：/legal compare <customer> <product_model> [old_version]"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept white query.compare_specs {rest}",
                caller=caller_agent,
            )
        payload, error = _white_customer_model_from_rest(rest)
        if error:
            return f"❌ {error}"
        payload.setdefault("old_version", "previous")
        payload.pop("version", None)
        return _dept_white("query.compare_specs", payload, caller=caller_agent)

    if action in {"search", "docs", "contract", "contracts", "文件", "合約", "搜尋"}:
        if not rest:
            return "用法：/legal search <query>"
        if rest.startswith("{"):
            return handle_dept_command(
                f"/dept white query.search_docs {rest}",
                caller=caller_agent,
            )
        return _dept_white(
            "query.search_docs",
            {"query": rest, "n_results": 5},
            caller=caller_agent,
        )

    if action.startswith("query."):
        suffix = f" {rest}" if rest else ""
        return handle_dept_command(f"/dept white {action}{suffix}", caller=caller_agent)

    return f"❌ 未知法務 SoT 指令：{action!r}\n\n{_WHITE_AGENT_HELP}"


def _format_help() -> str:
    # registry 初始化失敗時，help 仍要可呈現（不 raise）—
    # 列表退化成「無法取得，請看後續錯誤」。
    try:
        registered = ", ".join(sorted(c.value for c in _get_dept()[0]))
    except Exception as e:
        registered = f"(無法取得：{type(e).__name__}: {e})"
    return (
        "📋 `/dept` — 直接呼叫部門 agent（read-only，只放行 query.*）\n\n"
        "用法：\n"
        "  /dept <color> query.<name> [json_payload]\n\n"
        f"已註冊：{registered}\n"
        "範例：\n"
        "  /dept green query.list_samples\n"
        "  /dept green query.sample_status {\"sample_id\": \"S-2026-01\"}\n"
        "  /dept white query.list_specs\n"
        "  /dept orange query.customer_360 {\"customer\": \"Decathlon\"}\n\n"
        "  /dept gray query.production_status {\"recent_n\": 10}\n\n"
        "command.* 不在 /dept 範圍內 — 那些會 mutate state，請走一般對話。\n"
    )


def _format_result(color: Agent, intent: str, result: Any) -> str:
    # 多數 agent 回 {"text": "..."} 或 {"result": ...} 或 stub {"status": ...}
    if isinstance(result, dict):
        if "text" in result and isinstance(result["text"], str):
            return f"[{color.value} / {intent}]\n{result['text']}"
        if result.get("status") == "stub":
            return (
                f"[{color.value} / {intent}] 🚧 stub agent\n"
                f"echo: {json.dumps(result.get('echo', {}), ensure_ascii=False)}"
            )
    # fallback — 用 JSON 格式化
    try:
        return f"[{color.value} / {intent}]\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```"
    except Exception:
        return f"[{color.value} / {intent}] {result!r}"


def handle_dept_command(
    user_text: str,
    *,
    caller: Agent | str | None = Agent.RED,
) -> str:
    """處理 /dept 指令並回 Telegram-friendly 字串。

    呼叫端應先用 is_dept_command() 判斷；若直接呼叫但不是 /dept 指令也會
    回 help 訊息（不 raise），用法錯誤 / JSON 壞 / 矩陣禁區都回字串。
    """
    caller_agent = _coerce_caller(caller)
    parts = (user_text or "").strip().split(maxsplit=3)

    if len(parts) <= 1:
        return _format_help()

    color_str = parts[1].lower()
    try:
        color = Agent(color_str)
    except ValueError:
        valid = ", ".join(c.value for c in Agent)
        return f"❌ 未知 color: {color_str!r}\n合法 color：{valid}"

    if len(parts) == 2:
        return f"用法：/dept {color.value} <intent> [json_payload]"

    intent = parts[2]

    # /dept 是 read-only 接點 — 只放行 query.*。
    # command.* 包括 sync_drive / sync_gmail 都會 mutate state（刪 vector、
    # 大量外部 API call），且這條路徑繞過了 tg_auth 的 +確認 sensitive-tool gate。
    # command.* 仍可由正常 Gemini 流程觸發（那條走 sensitive_tool 包裝 + 確認窗）。
    if not intent.startswith("query."):
        return (
            f"🔒 `/dept` 為 read-only 接點，只放行 `query.*` intent。\n"
            f"  收到：{intent!r}\n"
            f"  command.* 會 mutate state 且需要 `+確認` 機制保護；請改用一般"
            f"對話讓 Gemini 走 sensitive_tool wrapper 流程。"
        )

    payload_text = parts[3] if len(parts) > 3 else "{}"

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as e:
        return (
            f"❌ JSON payload 解析失敗：{e}\n"
            f"  收到：{payload_text!r}\n"
            f"  範例：{{\"sample_id\": \"S-2026-01\"}}"
        )

    if not isinstance(payload, dict):
        return f"❌ payload 必須是 JSON object，收到 {type(payload).__name__}"

    # registry 第一次 build 可能失敗（lazy import 鏈壞掉之類）。
    # 不該 raise — handle_dept_command 契約是永遠回字串。
    try:
        registry, middleware = _get_dept()
    except Exception as e:
        return (
            f"❌ 部門 registry 初始化失敗（{type(e).__name__}）：{e}\n"
            f"  daemon 仍正常運作，但 /dept 暫無法使用。請檢查 wire.py / "
            f"build_default_registry 的 import 鏈。"
        )

    if color not in registry:
        if color is Agent.RED:
            return (
                "ℹ️ Red 是 daemon 本身（不在 registry 內）。/dept 是給 Red 用來查"
                "其他部門的指令；查 Red 自己沒意義。"
            )
        return f"❌ {color.value} agent 尚未註冊到 registry"

    from agent_core.agents import AgentRequest, PermissionDenied

    try:
        result = middleware.dispatch(AgentRequest(
            caller=caller_agent,
            target=color,
            intent=intent,
            payload=payload,
        ))
    except PermissionDenied as e:
        return f"🔒 {e}"
    except ValueError as e:
        # 部門 agent 通常用 ValueError 拒未知 intent
        return f"❌ {e}"
    except Exception as e:
        return f"❌ 執行失敗（{type(e).__name__}）：{e}"

    return _format_result(color, intent, result)


# ── /ingest — RAG index management (write-allowed, explicit allowlist) ──────

_INGEST_ALLOWLIST = frozenset({
    "command.sync_drive",
    "command.sync_gmail",
})

_INGEST_HELP = (
    "📥 `/ingest` — 手動觸發 RAG index 同步（需加 +確認）\n\n"
    "用法：\n"
    "  /ingest white command.sync_drive {\"all_drives\":true} +確認  ← 全 Drive 同步\n"
    "  /ingest <color> command.sync_drive {\"folder_id\": \"<id>\"} +確認\n"
    "  /ingest <color> command.sync_drive {\"file_id\": \"<id>\"} +確認\n"
    "  /ingest <color> command.sync_drive          ← 查 collection 狀態（不需確認）\n"
    "  /ingest <color> command.sync_gmail {\"gmail_query\": \"newer_than:90d\"} +確認\n\n"
    "允許 intent：command.sync_drive, command.sync_gmail\n"
    "無 payload 或 payload={} 的 sync_drive 視為 sync_status，不需確認。\n"
    "其他寫操作請走一般對話（有 +確認 保護）。\n"
)

def is_ingest_command(user_text: str) -> bool:
    s = (user_text or "").strip()
    return s == "/ingest" or s.startswith("/ingest ")


def handle_ingest_command(
    user_text: str,
    chat_id: str = "",
    *,
    caller: Agent | str | None = Agent.RED,
    confirm_scope: str = "",
) -> str:
    """處理 /ingest 指令（RAG sync）— 永遠回字串，不 raise。

    confirm_scope 語意同 handle_green_agent_command：+確認 的 scope 鍵，
    check 與 revoke 用同一把；留空退回 chat_id（私訊零行為改變）。"""
    caller_agent = _coerce_caller(caller)
    confirm_scope = confirm_scope or chat_id
    if caller_agent is not Agent.RED:
        return "🔒 /ingest 目前只允許 Red 管理員從 Telegram 觸發。"

    raw = (user_text or "").strip()

    # Strip +確認 suffix before parsing (preserves split logic)
    confirmed = raw.endswith(_CONFIRM_SUFFIX)
    if confirmed:
        raw = raw[: -len(_CONFIRM_SUFFIX)].rstrip()

    parts = raw.split(maxsplit=3)

    if len(parts) <= 1:
        return _INGEST_HELP

    color_str = parts[1].lower()
    try:
        color = Agent(color_str)
    except ValueError:
        valid = ", ".join(c.value for c in Agent)
        return f"❌ 未知 color: {color_str!r}\n合法 color：{valid}"

    if len(parts) == 2:
        return f"用法：/ingest {color.value} <intent> [json_payload] +確認"

    intent = parts[2]

    if intent not in _INGEST_ALLOWLIST:
        allowed = ", ".join(sorted(_INGEST_ALLOWLIST))
        return (
            f"🔒 `/ingest` 只放行：{allowed}\n"
            f"  收到：{intent!r}\n"
            f"  其他 command.* 請走一般對話（有 +確認 保護）。"
        )

    payload_text = parts[3] if len(parts) > 3 else "{}"
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as e:
        return f"❌ JSON payload 解析失敗：{e}\n  收到：{payload_text!r}"

    if not isinstance(payload, dict):
        return f"❌ payload 必須是 JSON object，收到 {type(payload).__name__}"

    # Only sync_drive with empty payload maps to sync_status() — a pure read.
    # sync_gmail with empty payload still runs a full 200-thread ingest, so it
    # always requires +確認 regardless of payload size.
    is_status_only = (intent == "command.sync_drive" and not payload)
    if not confirmed and not is_status_only:
        return (
            f"⚠️ 即將執行 `{intent}` on {color.value}\n"
            f"  payload：{json.dumps(payload, ensure_ascii=False)}\n"
            f"  這會觸發外部 API 呼叫並修改 vector index。\n"
            f"  確認請在指令末尾加 +確認 重新送出。"
        )

    # When a real chat_id is present, verify the tg_auth token was actually registered
    # (rate-limit / lockout protection — mirrors wrap_sensitive_tool's check_confirmed gate).
    # Without this, appending +確認 to any message bypasses lockout state entirely.
    if not is_status_only and chat_id:
        try:
            from agent_core.tg_auth import check_confirmed, is_locked_out
            ok, _ = check_confirmed(confirm_scope)
            if not ok:
                locked, remain = is_locked_out(confirm_scope)
                if locked:
                    return (
                        f"🔒 此 chat 已被 rate-limit 鎖定（+確認 次數過多）。\n"
                        f"   還有 {int(remain)} 秒解鎖。"
                    )
                return (
                    "🔒 找不到有效的 tg_auth 確認 token。\n"
                    "  Token 可能已過期或未登記，請重新送出訊息。"
                )
        except (ValueError, TypeError):
            pass  # tg_auth unavailable — fallback to literal +確認 check

    try:
        registry, middleware = _get_dept()
    except Exception as e:
        return f"❌ 部門 registry 初始化失敗（{type(e).__name__}）：{e}"

    if color not in registry:
        return f"❌ {color.value} agent 尚未註冊到 registry"

    from agent_core.agents import AgentRequest, PermissionDenied

    response: str
    try:
        result = middleware.dispatch(AgentRequest(
            caller=caller_agent,
            target=color,
            intent=intent,
            payload=payload,
        ))
        response = _format_result(color, intent, result)
    except PermissionDenied as e:
        response = f"🔒 {e}"
    except ValueError as e:
        response = f"❌ {e}"
    except Exception as e:
        response = f"❌ 執行失敗（{type(e).__name__}）：{e}"
    finally:
        # Consume the tg_auth confirmation token whether dispatch succeeded or failed
        # (one-shot guarantee — mirrors tg_auth.wrap_sensitive_tool finally block).
        if not is_status_only and chat_id:
            try:
                from agent_core.tg_auth import revoke_after_use
                revoke_after_use(confirm_scope)
            except Exception:
                pass

    return response
