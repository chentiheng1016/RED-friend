# 結構化數據倉 + text-to-SQL 設計（factory_warehouse）

> 狀態：設計草案。標 🚧 的區塊是「實作才能定版」的易變細節（精確 DDL、函式簽名），
> 會在對應 Phase 落地後回灌修正（doc 與 code 同一個 commit）。穩定的決策（DB 選型、
> 表清單、Rollout、風險）已定。

## 1. 動機

RAG（向量語意檢索）只擅長「在自由文字裡找語意相近片段」。工廠數據大宗是**結構化／交易型**
（每日產量、出貨、發票、付款、訂單），對這類資料 RAG 是錯的工具：chunk 切爛表格、
做不了精確查找、更做不了加總／join／算數。

實證：`agent_core/factory_production_report.py` 的開頭 docstring 已記錄 —— 那張 158 萬字的
生產日報進度表丟進 `read_drive_file` 只剩 1.9%、表頭攤平糊掉，小紅長期只能退回 email 拼湊。
最後是**另寫結構化直讀工具**才解決。倉就是把這個「點工具」升級成「可 join／可加總／可精確查」
的通用層。

### 倉裡只放真資料（重要校準）

| 來源 | 狀態 | 內容 | 形態 |
|---|---|---|---|
| 生產日報進度表 | 🟢 LIVE 每日 | 每日×各客戶×各站日產量、累計、欠數、希望/實際出貨日 | Drive xlsx（`factory_production_report.py` 已在解析） |
| 內部郵件 lake | 🟢 LIVE 每日 | Gemini 抽出的 PO號/客戶/供應商/承諾日/金額/狀態 | `var/data/data_lake/*.parquet` |
| 出貨單/快遞單/發票/匯款單 | 🟡 要 OCR | AWB、LOT、發票號、金額、出貨數 | Drive 圖片（目前只進 RAG） |

**明確排除**：`factory_supply_chain` / `factory_quality_control` / `factory_predictive_analytics`
/ `factory_brain` / `factory_data_integration` —— 這些讀的是 `var/factory_data/*.json` 的
**mock 假資料**（SUP001、假 ERP endpoint）。灌進倉等於拿假數字餵小紅，比沒有更糟。

## 2. 架構總覽

```
  生產日報 xlsx ─┐
  (Drive,每日)   │  共用既有 parser
                 ├─► iter_production_rows() ─► [ETL build] ─► factory_warehouse.db
  email parquet ─┤      (Phase 0 抽出)          每日重建        (var/data/, 唯讀查詢)
  (已是 parquet) │                              temp+rename          │
                 │                                                   ▼
  出貨/匯款 OCR ─┘ (Phase 3)              query_factory_warehouse(問題)
                                          NL→SELECT→驗證→唯讀執行→文字
                                                       │
                                                       ▼  小紅 (SAFE, 免確認)
```

## 3. DB 選型：DuckDB（建議）／ SQLite（保守備案）

- **DuckDB（建議）**：分析型聚合／join 是本命；**能直接 in-place 查現成的 email parquet**
  （`SELECT … FROM 'emails_master.parquet'`，郵件那條零 ETL）；單一 pip wheel、embedded 無 daemon、
  有 `read_only=True`。工廠查詢全是 GROUP BY（某客戶累計/這月趨勢/未出清單），DuckDB 快一個量級。
- **SQLite（備案）**：零新依賴，repo 已有現成唯讀+逾時範式可抄
  （`agent_core/ingest/vector_store.py` 的 `_sqlite_connect`：`file:…?mode=ro` + `set_progress_handler` deadline）。
  資料量才幾千列，SQLite 也夠。

兩者 schema 一致，唯讀模式都支援。**建議 DuckDB**，主因是 email parquet 直查省掉一整條 ETL。

> ✅ **已選 DuckDB 1.5.3**（Phase 1，`requirements-core.txt` 釘版）。下方 DDL 用通用型別；實際落地把日期欄改存 TEXT（見 §4 註）。

## 4. 資料表與 schema

> ✅ **Phase 1 已落地**：`fact_production_daily` + `fact_order_state`（`agent_core/factory_warehouse.py`）。
> **與下方草案的差異**：日期欄（want_ship/actual_ship/report_modified）改存 **TEXT** 不是 DATE —— actual_ship 常是 'OK' 之類非日期標記、逾期與否交查詢時算；`prod_date` 暫不存（月份無法只從分頁取得）；`fact_order_state.status` = shipped/completed/open（時間無關）。
> ✅ **Phase 2 已落地**：fact_email_thread + bridge_email_po + dim_customer + **bridge_email_customer**（21,401 封郵件 from `data_lake_internal/emails.parquet`）。**與草案差異**：實體欄（customers/suppliers/po_numbers/products/promised_dates）存 **' | ' 串文字**（小紅 LIKE 查＋顯示友善）非 JSON 陣列；`dim_customer.customer_id` = 生產 `customer_raw`（可直接 join）、aliases 含郵件變體（DECA.26↔Decathlon/DECA）；新增 **bridge_email_customer** 當可行的跨源連結（PO 級 join 實測不可行，見 §8#1）。

### LIVE 主幹（Phase 1–2）

```sql
-- 生產日報展開：grain = 一天 × 一張指令 × 客戶（直接對應 iter_production_rows() 每列）
CREATE TABLE fact_production_daily (
    report_file_id   TEXT,        -- 來源檔(可追溯)
    report_modified  DATE,        -- 該檔 modifiedTime
    prod_day         INTEGER,     -- 1..31（分頁）
    prod_date        DATE,        -- 月+日解析（月份未知則 NULL，見風險#3）
    customer_raw     TEXT,        -- 表上原樣 'DECA.26'
    customer_id      TEXT,        -- 正規化後 → dim_customer
    work_order       TEXT,        -- 指令
    model            TEXT,        -- 型體
    ordered_pairs    INTEGER,     -- 雙數
    stitching_day    INTEGER,     -- 針車 日計
    molding_day      INTEGER,     -- 成型（此廠 PU 灌注，多為 0）
    insock_day       INTEGER,     -- 中底
    injection_day    INTEGER,     -- 灌注
    packing_day      INTEGER,     -- 包裝 日計
    pack_cum         INTEGER,     -- 包裝累計
    pack_rem         INTEGER,     -- 欠數
    want_ship        DATE,        -- 希望出貨日
    actual_ship      DATE,        -- 實際出貨日
    PRIMARY KEY (report_file_id, prod_day, work_order, customer_raw)
);

-- 每張指令「當前狀態」（最新日快照，= _compute_completion/_compute_delivery_risk 持久化）
CREATE TABLE fact_order_state (
    work_order       TEXT PRIMARY KEY,
    customer_id      TEXT,
    model            TEXT,
    ordered_pairs    INTEGER,
    pack_cum_latest  INTEGER,
    pack_rem_latest  INTEGER,
    completion_pct   REAL,        -- pack_cum / ordered
    want_ship        DATE,
    actual_ship      DATE,
    status           TEXT,        -- shipped | overdue | due_soon | in_progress（衍生）
    as_of_day        INTEGER,
    report_file_id   TEXT
);

-- 郵件實體（DuckDB 直接 view 現成 parquet；SQLite 則 ETL 進來）
CREATE TABLE fact_email_thread (
    thread_id        TEXT PRIMARY KEY,
    date             DATE,
    sender           TEXT,
    primary_dept     TEXT,
    direction        TEXT,        -- inbound/outbound
    state            TEXT,        -- 進行中/已完成/暫停/取消/僅參考
    summary          TEXT,
    topic_tags       JSON,
    customers        JSON,
    suppliers        JSON,        -- 興維杰/佳桀…
    po_numbers       JSON,        -- ["65L1083024",...]
    promised_dates   JSON,
    amounts          JSON,
    message_count    INTEGER
);

-- 客戶正規化（解決 DECA / DECA.26 / Decathlon / DECATHLON 命名混亂）
CREATE TABLE dim_customer (
    customer_id      TEXT PRIMARY KEY,  -- 'DECATHLON'
    display_name     TEXT,
    aliases          JSON,              -- ['DECA','DECA.26','Decathlon']
    brands           JSON,
    contact_emails   JSON
);

-- 郵件↔指令 可 join 橋（把 po_numbers 陣列炸平，否則 SQL join 不到）
CREATE TABLE bridge_email_po (
    thread_id        TEXT,
    po_number        TEXT,
    PRIMARY KEY (thread_id, po_number)
);
```

### Phase 3a — 單據登記表（✅ 已落地，零 OCR）

> 發現：Drive 檔名自帶 LOT＋單據類型（甚至金額），例 `USD 9575--LOT 203-2025 匯款單.jpg`、
> `INVOICE -- LOT 309-2025.pdf`。純解析檔名即可盤點，**不需 OCR**。

```sql
CREATE TABLE fact_shipment_doc (    -- 從 Drive 檔名解析（_parse_doc_title）
    file_id TEXT, title TEXT,
    doc_type TEXT,    -- invoice/shipping/courier/remittance/receipt/coo/bill_of_lading/insurance/other
    lot_number TEXT,  -- 'NNN-YYYY' 如 '309-2025'
    awb TEXT, currency TEXT, amount DOUBLE,   -- amount/currency 是檔名提示、非權威
    mime_type TEXT, modified TEXT, drive_id TEXT );
```

小紅：「LOT 309-2025 有哪些單據」「哪些 LOT 缺匯款單」GROUP BY lot_number。

### Phase 3b — 單據內容抽結構化（✅ 部分落地：PDF 付款類；圖片 vision 未做）

> `factory_warehouse_extract.extract_payments_batch`：讀單據內容（read_drive_file → PDF pypdf 文字、便宜）
> → Gemini flash-lite 結構化 → **倉外持久化 jsonl**（`payments_store_path`，重建時 `_load_payments` 載回，
> 因為倉每次全量重建會清表）。**增量**（跳過已抽 file_id）+ **預算上限**（caller 今日成本達 budget_usd 即停）
> + **先只 PDF**（圖片要 vision、貴、撞 $1/天 image 閘門，留後續）。每日 cron 自動補一小批
> （`RED_WAREHOUSE_PAYMENT_BACKFILL_N` 預設 40），`backfill_factory_payments(max_docs)` 工具可手動補。

```sql
CREATE TABLE fact_payment (         -- ✅ 已落地（從單據「內容」抽，非檔名）
    file_id TEXT, lot_number TEXT, doc_type TEXT, title TEXT,
    amount DOUBLE, currency TEXT, doc_date TEXT, counterparty TEXT,
    invoice_no TEXT, extracted_at TEXT );

-- 未做：3b 圖片 vision OCR → fact_shipment / 3c crosswalk
CREATE TABLE fact_shipment (        -- 出貨單/快遞單 圖片 OCR（未做）
    shipment_id TEXT PRIMARY KEY, ship_date DATE, customer_id TEXT,
    work_order TEXT, lot_number TEXT, carrier TEXT, awb TEXT,
    qty_pairs INTEGER, doc_drive_id TEXT );

-- Phase 3c ✅（誠實版）：grounding 證實 row-level LOT↔PO↔指令不可行（生產資料無 LOT 欄、
-- PO∩指令僅 25/9959）。唯一可靠共同維度=**客戶**，故只記「識別碼→客戶」關聯（非 row-level 三方串）。
CREATE TABLE dim_order_xref (
    id_type TEXT,        -- 'work_order' | 'po'
    identifier TEXT, customer_id TEXT,
    source TEXT,         -- production(conf 1.0) / email_thread(0.7)
    confidence DOUBLE );
```

## 5. 小紅怎麼接（text-to-SQL 工具）

新 skill：`skills/factory_warehouse.py`（已落地），走 `SKILL_TOOLS` 熱載慣例，三個工具全 SAFE：
`query_factory_warehouse(question)`、`run_warehouse_sql(sql)`、`rebuild_factory_warehouse()`。

> ✅ **Phase 1 已落地**：5 層防線（SELECT/WITH only、deny 寫入/檔案/擴充關鍵字、read_only 連線、`enable_external_access=false` 擋讀本機檔、deadline 中斷）；15 測試綠。

```python
def query_factory_warehouse(question: str) -> str:
    """用自然語言查工廠數據倉（生產/出貨/訂單/郵件實體）。問數量、累計、未完、
    趨勢、跨客戶比較走這個（精確、可加總），不要用 RAG 撈片段。"""
```

四層流程（參考 `skills/excel_ops.py` `excel_query` 既有的 defense-in-depth）：
1. **schema 注入**：introspect 倉 schema + few-shot → 餵 Gemini
2. **生 SQL**：要求只出 `SELECT`/`WITH`
3. **驗證**：非 SELECT 開頭 / 含 `INSERT|UPDATE|DELETE|DROP|ATTACH|PRAGMA` / 多語句(`;`) → 退回
4. **唯讀執行 + 逾時**：DuckDB `read_only=True`（或 SQLite `?mode=ro`）—— 引擎層擋寫，驗證被繞過也寫不進去；
   裝 deadline progress-handler 砍跑飛查詢（repo 被 34GB chroma 全掃教訓過，必裝）
5. **格式化**：結果轉文字、列數封頂、**附上實際用的 SQL**（透明可複核）

外加 power-user 逃生口 `run_warehouse_sql(sql: str)`：直接寫 SQL，一樣唯讀連線。

**Tier = SAFE**：純讀無副作用，免 `+確認`（防線是唯讀連線+SELECT-only+deadline，不是 tier）。
在 `agent_core/tool_tiers.py` 不必加 override，預設就落 SAFE。

**路由**：數量/累計/趨勢/未完/跨客戶比較 → 倉；「上次跟客戶談什麼/找類似文件」自由文字語意 → RAG。
延伸現有 `learn_behavior` 觸發詞即可。

## 6. 與 factory_production_report.py 銜接

核心洞見：那支 parser 已把最難的活幹完（text-anchor 表頭偵測、站別對應、逐列抽取）。
銜接 = 把逐列抽取抽成共用 generator，文字工具與倉 loader 共用同一份解析，不重寫。

> ✅ **Phase 0 已落地**（本 commit）：簽名與 row dict 形狀已實作、測試綠（36 passed = 28 既有 + 8 新）。

```python
# factory_production_report.py 新增（Phase 0）
def iter_production_rows(xlsx_bytes, *, warnings=None):
    """每張日分頁的每一列 yield 一個正規化 dict（給數據倉 loader）：
    {prod_day, is_latest_day, customer, work_order, model, pairs,
     stitching_day, molding_day, insock_day, injection_day, packing_day,
     pack_cum, pack_rem, want_ship(raw), actual_ship(raw)}
    warnings: 選填 set，回填版型偵測警告；壞檔／非日分頁 → yield 0 列、不丟例外。"""
```

實際分層（共用核心，整支模組的版型偵測只有一份）：
- `_open_workbook(bytes)` — openpyxl read_only 讀檔
- `_normalize_row(r, colmap, day, latest)` — 單列 → 正規化 dict（無客戶名回 None）
- `_iter_rows_for_workbook(wb, day_sheets, *, warnings, day_filter)` — 對已開 workbook 逐列產出（內部核心）
- `iter_production_rows(bytes, *, warnings)` — 公開入口（開檔 → 委派核心）

- **一份解析、兩個消費者**（已驗證）：
  - `_summarize` / `read_production_progress_sheet`（文字工具）已改跑在共用核心上 —— **對外行為零變化、SAFE 不動、`daily_production_8am` 照用**；28 個既有測試續綠，另加 8 個 generator 測試。
  - 倉 loader（Phase 1）→ 消費 `iter_production_rows()` → INSERT。
- **圖表 `_compute_*` 維持單頁讀法不動**：它們只讀最新一張分頁（perf 優化），硬塞全分頁 generator 會 31× 拖慢；
  日後可選改共用 `_normalize_row()`（單頁讀法不變）。
- **單日查詢 perf 保留**：`_iter_rows_for_workbook` 帶選填 `day_filter`，`read_production_progress_sheet(day='14')` 仍只掃該分頁。
- **Loader（已落地）**：`agent_core/factory_warehouse.py` 的 `build_factory_warehouse()`／`build_warehouse_from_bytes()`，重用既有 `_download_xlsx_bytes`／`_find_latest_progress_file`，**重建到 `<db>.tmp` 再 os.replace 原子換掉**（小紅永不查到半成品倉）。
- **觸發（Phase 1 做法）**：查詢工具**首次查無倉就自建**（lazy bootstrap）+ `rebuild_factory_warehouse()` 手動刷新。**每晚自動重建的 dispatcher/rag_runner 掛載點留待後續**（Phase 1 先不碰 daemon 編排、降風險）。
- **後續可選**：`read_production_progress_sheet` 改成查倉而非每次重抓 Drive（秒回、免 Drive 來回）。

## 7. Rollout

| Phase | 內容 | 交付價值 | 前置 |
|---|---|---|---|
| **0** ✅ | 抽 `iter_production_rows()`，`_summarize` 跑在它上面 | 零行為變化、36 測試綠（地基） | — |
| **1** ✅ | 建倉 loader + `query_factory_warehouse`/`run_warehouse_sql`/`rebuild`(SAFE) | 生產數量/累計/未完 精確可查 | DuckDB ✅ |
| **2** ✅ | email lake 接入（fact_email_thread 21,401 封）+ bridge_email_po/customer + dim_customer | 郵件可 SQL 精查 + **客戶級**跨源（非 PO 級） | Phase 1 ✅ |
| **3a** ✅ | `fact_shipment_doc` 從 Drive 檔名解析（LOT/單據類型/金額提示） | 單據盤點、LOT 登記（零 OCR） | Drive 列表 |
| **3b** 🟡 | PDF 付款類抽 → `fact_payment`（真實金額/日期/對方）✅；圖片 vision OCR 未做 | 付款明細、本月匯款總額 | flash-lite 文字抽（便宜）+ 每日 cron 增量 backfill |
| **3c** 🟡 | `dim_order_xref` **客戶級** crosswalk（識別碼→客戶）✅；row-level LOT↔PO↔指令 資料不支援 | 某客戶的所有指令/PO | 客戶為唯一可靠共同鍵 |

## 8. 風險與坑

1. **三套 identifier**：日報用「指令」、郵件用 PO號（65L…）、出貨/發票用 LOT（LOT 208-2026）。
   串起來需 `dim_order_xref`，部分得半人工/啟發式 —— **最難的一塊，也是未來知識圖譜的切入點**。
   **Phase 2 實測證實**：郵件 PO（JF0P…/65L…/LJF…）∩ 生產指令（JFC…-1-1）直接重疊只有 **25 / 9959**
   → PO 級 join 不可行，已改走**客戶級**跨源（dim_customer/bridge_email_customer，6 客戶全配對、
   DECA.26 靠別名接 836 封）。LOT 全鏈仍是 Phase 3。
2. **客戶命名混亂**（DECA/DECA.26/Decathlon）→ `dim_customer` 別名表先建，否則聚合會散。
3. **月度欄位漂移**：parser 已用 text-anchor + warning 容忍，loader 要把 warning 寫進 load-log，
   壞解析要看得見，別靜默灌爛資料進倉。`prod_date` 的「年/月」若無法從檔名/分頁取得就留 NULL，只存 `prod_day`。
4. **別讓倉檔被 RAG 吃掉或備份到 Drive**（踩過明文金鑰上 Drive 的雷）—— `var/data/` 是 protected dir、
   gitignored，放這裡安全。
5. **這條功能線在部署分支 `chroma-shared-server`，不在 main**：`factory_production_report.py` 從未 PR 回 main。
   倉延續同分支；整條工廠 MES 線回 main 的路徑見 §9。

## 9. css → main 歸檔（緩做決定，2026-06-18）

Phase 0–3c 已**部署 + 運行在 `chroma-shared-server`（css，production 分支）**，功能 100% 到位。
經分析後 owner 選 **main-PR 緩做**。以下為決定理由與**續做條件**，留作冷啟動依據。

**為什麼緩做（非技術卡關）**
- 工廠線疊在 css 對 origin/main 的 80-commit 分歧之上，與別人未合併功能（export `e048aef2`、
  generate_chart 片段）經 `tool_registry_catalog.py` **文字糾纏** → 無法乾淨單獨 cherry-pick（會衝突）。
- 單獨推工廠進 main 會造成「半套 main」（工廠進、兄弟功能沒進）；下次 css→main 整併時 squash 版
  vs 原版對撞，反更難收。
- 程式碼已部署運行，main-PR 純正史歸檔、非功能 → 風險>價值，該排成一次刻意整併。

**關鍵事實（決定續做難易）**
- ✅ `origin/main` 是 css 祖先、反向落差 **0 commit**（css ⊇ main）→ **整併隨時零衝突**。
- 工廠線**功能自足**：核心倉（`factory_warehouse*.py`）不 import 任何 css-only 功能；圖表函式依賴
  `chart_export`（同屬工廠線）、**不碰 export**。
- 等待幾乎零代價：css 持續 merge origin/main 往前，css⊇main 恆成立 → 不腐爛、不變難。

**續做唯一條件 = 一個決定（不是 code 狀態）。兩條乾淨路徑：**

| 路徑 | 範圍 | 條件 | 做法 | 觸發 |
|---|---|---|---|---|
| ① 工廠線 only | 只工廠進 main | 無技術前提（功能自足） | origin/main 切枝 → 工廠線當乾淨 diff 重貼（含 chart_export barh）→ 本機在 origin/main 基底測綠 → squash PR | 喊「做工廠 PR」，~30–40 分 |
| ② 整批 css→main | 全部已部署 work | css 穩定、無人 mid-flight | 一次乾淨 merge css→main（已驗證零衝突），工廠搭順風車 | 「整理 main／對齊部署」時 |

**冷啟動接續用** — 工廠線 commit：`b774cf26`(P0) → `135ac583`(P1) → `79ea2ead`(P2) →
`09cdb19c`(3a) → `61a9dd4e`(3b) → `a7229f74`(3c) → `8db5bfc9`(3b fix)；地基（非本專案所寫）：
`d4a920c2` / `68e99de5` / `84d1b025` / `66f3d9ec`。路徑① 的 cherry-pick 順序 = 地基 4 個 + 工廠 7 個。
