# `agent_core/agents/` — Project Rainbow 部門化框架

權限矩陣（本檔）+ per-色子 agent（`/dept <色> query.*` 查詢通道）+ 員工
Telegram 自由對話工具白名單（`agent_core/dept_tool_scope.py`，見下節）。
QUERY_MATRIX 是唯一的跨部門授權來源：子 agent 的 query_peer 與自由對話的
工具面繼承都由它裁決。

## 10 色 → 部門對照

| Color | 部門 | dept_rules 中文 key | 角色 |
|---|---|---|---|
| RED | 老闆 / GM Office | 老闆 | SUPER_ADMIN — 可查所有 |
| ORANGE | Sales 業務 | 業務 | 客戶 360 / 活躍客戶 / 報價歷史 / Gmail 搜尋 |
| YELLOW | Procurement 採購 | 採購 | 供應商 / PO / ETA / 採購紀錄 |
| GREEN | Sample Room 樣品室 | 樣品室 | 樣品追蹤 / 樣品主檔 / 到期催信 |
| BLUE | Shipping 船務 | 船務 | 出貨狀態 / ETD / ETA / AWB / B/L / 櫃號 |
| INDIGO | Warehouse 倉庫 | 倉庫 | 庫存 / 可用量 / 補貨警示 / 出入庫紀錄 |
| PURPLE | Accounting 會計 | 會計 | invoice / 付款 / 匯款 / 對帳單 / 會計紀錄 |
| GRAY | Production 生產管理 | 生產管理 | 生產異常 / ECD / 跨部門通知 |
| BLACK | Cashier 出納 | 出納（dept_rules 目前歸於 "會計"） | 收支 / 付款 / 入帳 / 出納警示 |
| WHITE | Legal 法務 | 法務（待新增） | SoT — 合約 / 測試報告 / 規格 |

## 權限矩陣（caller → 可查 target）

```
ORANGE  → YELLOW, GREEN,  BLUE,   INDIGO, WHITE
YELLOW  → ORANGE, BLUE,   INDIGO, GRAY,   WHITE
GREEN   → ORANGE, INDIGO, WHITE
BLUE    → ORANGE, YELLOW, INDIGO, PURPLE, GRAY,  WHITE
INDIGO  → ORANGE, GREEN,  BLUE,   GRAY,   WHITE
PURPLE  → ORANGE, YELLOW, GREEN,  BLUE,   INDIGO, BLACK, WHITE
GRAY    → ORANGE, YELLOW, GREEN,  BLUE,   INDIGO, WHITE
BLACK   → ORANGE, YELLOW, GREEN,  BLUE,   INDIGO, PURPLE, WHITE
WHITE   → (SoT，不主動查)
RED     → 全部
```

## 員工 Telegram 自由對話（RED_TG_EMPLOYEE_FREEFORM）

非 red 色員工**私訊**各自部門 bot 可直接用自然語言對話（群組不開放）。
launchd 十個 telegram plist 模板現值皆為 `all`（九色全開；red 員工走 GM
全工具路徑，不在此制度內）。歷程：#263 indigo → #268 gray/blue →
#272 green → #273 orange/purple/yellow → #274 black/white（2026-07-22）。

工具面公式（`agent_core/dept_tool_scope.py`）：

```
allowed(色) = _COMMON_TOOLS ∪ _HOME_TOOLS[色] ∪ ⋃ _HOME_TOOLS[QUERY_MATRIX[色]]
實際掛載   = allowed(色) ∩ SAFE tier（CONFIRM+ 永不進員工 session）
```

| 色 | `_HOME_TOOLS` curation（皆 SAFE 唯讀） |
|---|---|
| ORANGE | `read_customer_order_pos`、`query_po_timeline`、`query_customer_timeline`、`list_customer_pos`、`check_stale_pos`、`check_overdue_promises`、`customer_360`、`list_active_customers`、`customer_alerts`、`query_quote_history` |
| YELLOW | `query_material_arrival`、`material_arrival_overview`、`check_material_readiness`、`get_model_bom`、`query_bom`、`query_quote_history`、`query_erp_stock` |
| GREEN | `read_sample_status`、`read_sample_bom`、`search_product_photos`、`list_tracked_samples`、`check_sample_deadlines` |
| INDIGO | `read_warehouse_stock`、`read_box_shipping_marks`、`query_erp_stock` |
| GRAY | `read_production_progress_sheet`、`production_alert`、`query_production_schedule`、`production_dashboard` |
| BLUE / PURPLE / BLACK | 無 home — 靠 QUERY_MATRIX 繼承（如 PURPLE 可查 orange/yellow/green/blue/indigo/black/white） |
| WHITE | 無 home、矩陣為空（SoT）→ 只有共用查詢面 |

共用查詢面 `_COMMON_TOOLS` = `search_drive_docs` + `search_operation_sops`。
資料層：每顆工具包 `AgentRequest(caller=<色>)` → rag_gateway per-color ACL。
`/dept` 子 agent 查詢通道（含黑的 `query.cash_*`、白的 specs）獨立於此白名單。

安全鐵則：

- `read_dept_email_timeline` **永不進任何色**（`dept` 是自由參數含「老闆」，
  進員工 session = 跨部門信件時間軸越權）— `tests/test_employee_freeform.py`
  有逐色守門測試釘死。
- 開新色/擴工具面：先補 `_HOME_TOOLS` curation（只放 SAFE 唯讀），
  再確認十個 plist 模板 env 一致。

## 啟動範例（Phase 2+ 才會接到 agent_daemon）

```python
from agent_core.agents import (
    Agent, AgentRegistry, PermissionMiddleware, BaseAgent,
)

registry = AgentRegistry()
middleware = PermissionMiddleware(registry)
registry.bind_middleware(middleware)

# Phase 2 會把 sample_tracker 邏輯搬進 GreenSampleDevAgent
class GreenSampleDevAgent(BaseAgent):
    identity = Agent.GREEN
    def handle_query(self, intent, payload, trace_id):
        if intent == "query.sample_status":
            return {"sp_id": payload["sp_id"], "status": "in_progress"}
        raise ValueError(f"unknown intent: {intent}")

registry.register(GreenSampleDevAgent())

# 跨部門呼叫（會被 matrix 檢查；Orange 可以查 Green）
class OrangeSalesAgent(BaseAgent):
    identity = Agent.ORANGE
    def handle_query(self, intent, payload, trace_id):
        if intent == "ask.sample_for_quote":
            sample = self.query_peer(
                target=Agent.GREEN,
                intent="query.sample_status",
                payload={"sp_id": payload["sp_id"]},
            )
            return {"sample": sample}

registry.register(OrangeSalesAgent())
```

## 為什麼 in-process？

- 小紅是單機 macOS daemon，不是 K8s 雲端服務
- 部門間呼叫保持 Python function call 的成本（µs），不引入 Redis/網路 IO
- 邏輯隔離（不能繞過 matrix）+ 介面穩定（intent + payload）兩個目標已達成
- 真要拆 process 時，PermissionMiddleware.dispatch 是唯一要改的接縫
