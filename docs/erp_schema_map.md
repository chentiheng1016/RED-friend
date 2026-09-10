# 飛越 ERP（JHDB / Oracle 10.2.0.5）schema 對照 — 第一批

> 2026-06-28 由大王在 ERP 主機本機用 PL/SQL Developer（APP 帳號）實際查 `all_tab_columns`
> 取得。中文語意欄是**推測**（沒撈 col comments，正式用前要再對 `all_col_comments`）。
> 連線與安全前提見 `docs/erp_db_readonly_design.md` 與記憶 `project_erp_server_security_blocker`。

## 連線事實（實測）

| 項目 | 值 |
|---|---|
| Oracle 版本 | **10.2.0.5**（10g R2）→ python-oracledb **必須 thick + Instant Client 11.2** |
| 帳號 | `APP` / `APP`（飛越提供；弱密碼，正式上線前換強的唯讀帳號） |
| tnsnames 別名 | `jhdb`（PL/SQL Developer 標題顯示 APP@JHDB） |
| 別名背後真實 service | **`JHNEW`**，內部主機名 `fast`、port 1521（Net Manager 實看） |
| 公網 | <ERP_HOST>:1521 listener 活著，但 JHDB/JHNEW 從外**未註冊**（外部直連不到，要走本機/tunnel） |
| 其他別名 | `jhnew`、`testdb`、`extproc_connection_data` |

## 兩張表的關係

- **`BQ_SE_ORDITEM`** = 訂單主檔（一張訂單一筆：客戶/品牌/鞋款/數量/交期/價格）。
- **`BQ_SE_ITEMSCHE`** = 該訂單的物料明細與到料狀況（一張訂單多筆料）。
- **Join key = `SE_ID`（訂單單號）**（兩表都有；ORDITEM 另有 SE_SEQ/SE_VER 版次）。
- 串起來即「訂單 → 用料 → 需求/訂購/發料/點收/到料日」全鏈。

---

## BQ_SE_ORDITEM（訂單主檔，49 欄）

| # | 欄位 | 型別 | 空 | 推測語意 |
|---|---|---|---|---|
| 1 | GRT_ORG_ID | NUMBER(6) | N | 集團/授權組織 |
| 2 | GRT_ORG_NAME | VARCHAR2(4000) | Y | |
| 3 | ORDER_ORG | NUMBER(6) | Y | 接單組織 |
| 4 | ORDER_ORG_NAME | VARCHAR2(4000) | Y | |
| 5 | PROD_ORG | NUMBER(6) | Y | 生產組織 |
| 6 | PROD_ORG_NAME | VARCHAR2(4000) | Y | |
| 7 | GROUP_ORG | VARCHAR2(20) | Y | |
| 8 | GROUP_ORG_NAME | VARCHAR2(4000) | Y | |
| 9 | **SE_ID** | VARCHAR2(20) | N | **訂單單號（join key）** |
| 10 | SE_DAY | DATE | N | 單據日 |
| 11 | ORD_CUST_NO | VARCHAR2(4000) | Y | 訂貨客戶編號 |
| 12 | ORD_CUST_NAME | VARCHAR2(4000) | Y | 訂貨客戶名 |
| 13 | ACC_CUST_NO | VARCHAR2(4000) | Y | 帳務客戶 |
| 14 | ACC_CUST_NAME | VARCHAR2(4000) | Y | |
| 15 | PAY_CUST_NO | VARCHAR2(4000) | Y | 付款客戶 |
| 16 | PAY_CUST_NAME | VARCHAR2(4000) | Y | |
| 17 | BRAND_NO | VARCHAR2(4000) | Y | 品牌編號 |
| 18 | BRAND_NAME | VARCHAR2(4000) | Y | 品牌名 |
| 19 | MERCHANT_NO | VARCHAR2(4000) | Y | |
| 20 | MERCHANT_NAME | VARCHAR2(4000) | Y | |
| 21 | PO | VARCHAR2(20) | Y | 客戶 PO |
| 22 | MER_PO | VARCHAR2(20) | Y | |
| 23 | PRE_SEID | VARCHAR2(20) | Y | 前置單號 |
| 24 | SE_TYPE | VARCHAR2(8) | N | 單別 |
| 25 | SE_TYPE_NAME | VARCHAR2(4000) | Y | |
| 26 | SRC_SEID | VARCHAR2(20) | Y | 來源單號 |
| 27 | SE_SEQ | NUMBER(2) | N | 序 |
| 28 | SRC_SESEQ | NUMBER(2) | Y | |
| 29 | SE_VER | NUMBER(3) | N | 版次 |
| 30 | PROD_NO | VARCHAR2(20) | Y | 產品編號 |
| 31 | PROD_NAME | VARCHAR2(600) | Y | 產品名 |
| 32 | SHOE_NO | VARCHAR2(20) | Y | 鞋款編號 |
| 33 | DEV_SEASON | VARCHAR2(4000) | Y | 開發季節 |
| 34 | SHOE_NAME | VARCHAR2(4000) | Y | 鞋款名 |
| 35 | GENDER | VARCHAR2(4000) | Y | 性別 |
| 36 | CUST_PROD_NAME | VARCHAR2(60) | Y | 客戶型號 |
| 37 | WIDTH_NAME | VARCHAR2(4000) | Y | 楦寬 |
| 38 | COLOR_WAY | VARCHAR2(40) | Y | 配色 |
| 39 | PROD_TYPE_NAME | VARCHAR2(4000) | Y | 產品類別 |
| 40 | SE_QTY | NUMBER(8) | Y | **訂單數量** |
| 41 | B_QTY | NUMBER(8) | Y | |
| 42 | CUST_REQ_DATE | DATE | Y | 客戶要求交期 |
| 43 | REQ_FAC_DATE | DATE | Y | 要求交廠日 |
| 44 | FAC_DATE | DATE | Y | 交廠日 |
| 45 | CLOSE_REASON | VARCHAR2(4000) | Y | 結案原因 |
| 46 | SE_STATUS | VARCHAR2(6) | Y | 訂單狀態 |
| 47 | IS_PLAN | VARCHAR2(1) | Y | 是否計畫 |
| 48 | MONEY_UNIT | VARCHAR2(4000) | Y | 幣別 |
| 49 | PRICE | NUMBER | Y | 單價 |

## BQ_SE_ITEMSCHE（訂單物料/到料狀況，23 欄）

| # | 欄位 | 型別 | 空 | 推測語意 |
|---|---|---|---|---|
| 1 | ORG_ID | NUMBER(6) | N | 組織 |
| 2 | ORG_NAME | VARCHAR2(4000) | Y | |
| 3 | **SE_ID** | VARCHAR2(20) | N | **訂單單號（join key）** |
| 4 | SE_SEQ | NUMBER(5,1) | N | 序 |
| 5 | SE_DAY | DATE | N | 單據日 |
| 6 | PROD_NO | VARCHAR2(20) | Y | 產品編號 |
| 7 | PROD_NAME | VARCHAR2(4000) | Y | 產品名 |
| 8 | REQ_FAC_DATE | DATE | Y | 要求交廠日 |
| 9 | FAC_DATE | DATE | Y | 交廠日 |
| 10 | SE_STATUS | VARCHAR2(6) | Y | 狀態 |
| 11 | ITEM_NO | VARCHAR2(30) | N | **物料編號** |
| 12 | ITEM_NAME | VARCHAR2(4000) | Y | 物料名 |
| 13 | ITEM_UNIT | VARCHAR2(4000) | Y | 單位 |
| 14 | NEED_QTY | NUMBER(11,2) | Y | 需求量 |
| 15 | ORD_QTY | NUMBER(11,2) | Y | 訂購量 |
| 16 | LOT_QTY | NUMBER | Y | 批量 |
| 17 | LEFT_REQ_ORD_QTY | NUMBER | Y | 剩餘需訂量 |
| 18 | CHK_QTY | NUMBER(11,2) | Y | 點收量 |
| 19 | ISSUE_QTY | NUMBER(11,2) | Y | 發料量 |
| 20 | VEND_NO | VARCHAR2(8) | Y | 供應商編號 |
| 21 | VEND_NAME | VARCHAR2(4000) | Y | 供應商名 |
| 22 | NEED_DATE | DATE | Y | 需求日 |
| 23 | PLAN_ORD_DATE | DATE | Y | 計畫訂購日 |

---

## 對 RED 工具的意義（下一步）

- `BQ_SE_ORDITEM` → 可做 **客戶訂單查詢 / 交期看板**（客戶、品牌、鞋款、數量、交廠日、狀態）。
- `BQ_SE_ITEMSCHE` → 可做 **備料/到料進度**（需求 vs 訂購 vs 點收 vs 發料、供應商、計畫訂購日）——比現有 email-based `query_material_arrival` 精準得多。
- 兩表 `SE_ID` join → 「這張訂單的料齊了沒、卡在哪一項」。
- ⚠️ 正式建工具前要補：① `all_col_comments` 真語意 ② `SE_STATUS` 代碼表 ③ 抽樣對權威來源（業務訂單夾）驗數字 ④ 確認主鍵/索引（給查詢加 INDEXED BY 之類）。

---

## SE_STATUS / SE_TYPE 實值（2026-06-29 撈活庫）

⚠️ DB **沒填 col comments**（all_col_comments 空），所以欄位語意以上面推測為準。
但 SE_STATUS 是**直接存中文字串、非代碼**，實際值與生命週期：

**BQ_SE_ORDITEM.SE_STATUS**（訂單狀態，依生命週期）：
`新单 → 生效 → 完工 → 出货 → 销货`，另有 `取消`。
（樣本分布：销货 1660 / 生效 483 / 取消 157 / 完工 96 / 出货 4 / 新单 1）
- 生效 = 進行中的有效訂單；完工 = 生產完成；出货/销货 = 已出/已開銷；取消 = 作廢。

**BQ_SE_ITEMSCHE.SE_STATUS**：只見 `生效`(29792) / `完工`(5826)。

**BQ_SE_ORDITEM.SE_TYPE**：`01` = 正式訂單（目前資料只有這一型）。

---

## 全 schema 擷取（2026-07-07，唯讀掃全庫）

> 由 `scripts/erp_schema_probe.py`（走 SSH+10g sqlplus）掃 `all_tables/all_tab_columns/
> all_constraints/all_views/all_source`。完整明細在 `var/data/erp_schema_probe/`
> （每 schema 一份 `.md`+`.json`、另有 `erp-schema-OVERVIEW.md`；var/ 為 runtime、gitignored）。
> ⚠️ DB 仍**無 col comments**，欄位語意靠表名推測，正式用要抽樣對權威來源驗。

### 重大發現：資料不只在 APP（12 表），是跨 14 個業務 schema、~1,538 表

之前只摸到 `APP` schema 的 2 張訂單表；實際飛越 ERP 資料分佈在模組 schema：

| schema | 表 | 欄 | views | PL/SQL行 | trig | 推測用途（由表名推） |
|---|---|---|---|---|---|---|
| SC00 | 699 | 10735 | 128 | 13846 | 125 | **供應鏈核心**：訂單 SE_ / 採購+MRP PO_ / 庫存 IV_ / BOM(含尺寸) SE_BOM_/RD_BOM_ |
| MK00 | 221 | 3127 | 46 | 2880 | 27 | **製造現場**：工單 WK_ / 派工 DISPATCH / 領料·生產 SF_ |
| GL00 | 156 | 2754 | 7 | 2522 | 15 | **總帳**（財務） |
| GL_IMP | 156 | 2659 | 7 | 2390 | 14 | 總帳整合/匯入副本 |
| EP00 | 101 | 1379 | 11 | 625 | 1 | 財務子帳/企業入口？（待確認） |
| SP00 | 95 | 1341 | 22 | 734 | 1 | **樣品室** SP_（樣品 BOM） |
| SY00 | 74 | 785 | 162 | 1438 | 11 | 系統/設定/權限（SYPRG_LOG） |
| APP | 12 | 116 | 19 | 820 | 3 | 飛越客製訂單層 BQ_SE_*/APP_DISPATCH |
| SHIRLEY | 10 | 88 | 0 | 0 | 0 | 個人/暫存 schema |
| TIPTOP | 5 | 139 | 0 | 0 | 0 | 基底 ERP 標記（疑 TIPTOP 系） |
| PDA | 4 | 60 | 1 | 0 | 1 | 條碼/手持掃描 |
| FY00 | 3 | 121 | 2 | 35 | 0 | 條碼 FY_BARCODE_GET |
| BTW | 1 | 10 | 0 | 0 | 0 | 暫存 |
| EP_IMP | 1 | 11 | 0 | 34 | 1 | EP 匯入 |
| **合計** | **1538** | **23325** | **405** | **25324** | **199** | |

（APP 之外還可見純 Oracle 系統 schema：SYS/SYSTEM/EXFSYS/WMSYS/WEBUTIL，非 ERP 資料，未列。）

### 邏輯落點（決定「反推重寫」方案B 的天花板）

DB 端有 **~25,300 行業務 PL/SQL + 199 個 trigger + 405 views**（SC00 獨佔 13,846 行、125 trigger）。
→ 這**不是**純 Forms ERP：相當比例的寫入驗證/過帳邏輯就在 DB（尤其 SC00 的 trigger），
**可經 `all_source` 直接擷取**。方案B 的反推天花板比原假設高——原本「規則幾乎全在 Forms、
DB 反推近乎為零」只對了一半。（型別分佈：PACKAGE 389 物件/60,845 行含系統、TYPE 550、
TRIGGER 33 業務相關、PROCEDURE 14、FUNCTION 65。）

### 資料量最大的主表（供分析鎖定；完整前 50 見 OVERVIEW.md）

`SC00.SE_BOM_HIS_SIZE` 458 萬、`SP00.SP_SPITEM_PART_PCHIS` 155 萬、`SC00.RD_BOM_SIZE` 150 萬、
`SC00.PO_MRP_ORD_BAK` 111 萬、`SC00.SE_BOM_SIZE` 99 萬… → BOM（含**尺寸維度**）、MRP 採購、
庫存異動、工單派工全都在，且是尺寸級明細。**之前「撞 ERP BOM 牆」的 BOM 就是 `SC00.SE_BOM_*`
/ `RD_BOM_*`**（生管/採購工具當初做不到 BOM 展開，是因為沒接到這裡）。

### ⚠️ `BQ_SE_ORDITEM`/`BQ_SE_ITEMSCHE` 是 VIEW，不是表（2026-07-07 修正）

先前本檔說「BQ_SE_ORDITEM 是 APP schema 訂單主檔（表）」是**錯的**。實際上 `APP` schema 存的是
12 張 `APP_*`（條碼/派工/帳單）表 + 19 個 view，其中 14 個 `BQ_` view 是「訂單/物料/財務主檔」的
**呈現層**。訂單真資料在底層基表（都在鏡像範圍）：

| BQ_ view（呈現層） | 底層基表（真資料所在） |
|---|---|
| `BQ_SE_ORDITEM`（訂單主檔） | `SC00.SE_ORD_M`（表頭 ~2,406）+ `SC00.SE_ORD_ITEM`（明細 ~2,400）+ `VW_RD_PROD`（產品view） |
| `BQ_SE_ITEMSCHE`（物料到料） | `SC00.SE_ITEMSCHE_M`（~44,026）+ `SE_ORD_M` + `SE_ORD_ITEM` |
| `BQ_SE_ORDSIZE` | `SE_ORD_M`/`SE_ORD_ITEM`/`SE_ORD_SIZE`/`RD_STYLE` |
| `BQ_SE_FINISH`（完工） | `SE_ORD_M`/`SE_ORD_ITEM`/`SE_STOC_M` |
| `BQ_SE_SALES`/`BQ_SE_TRANSSIZE` | `SE_SALES*` / `SE_TRANS_M`+`SE_TRANS_SIZE` |
| `BQ_PO_ORDDELAY` | `PO_ORDER_M`/`PO_ORDER_D` |
| `BQ_GA_ORDER_RCPT` | `GA_ORDER_M`/`GA_ORDER_D`/`GA_TRANS_D` |
| `BQ_AR_SALES`/`BQ_AR_CUST_LEFTMONEY` | `AR_SALES*` / `AR_CUST_M`（應收） |
| `BQ_AP_*_LEFTMONEY` | `AP_APPLY_M`/`AP_DUE_D`（應付） |
| `BQ_SE_PLAN` | `SE_PLAN_M_N`/`SE_PLAN_SE_N`/`SE_PERIOD_N` |
| `BQ_WK_ITEMSTATE` | `SE_ORD_M`/`SE_ORD_ITEM`（工單狀態） |

**關鍵：底層基表存的是「代碼/ID」，不是友善名稱。** view 用 PL/SQL 函式（`GG_*` package，如
`GG_2101.GF_CUSTNM_J`=客戶ID→名、`GG_0002.GF_CODE_NAME`=代碼→標籤）即時算出中文欄。直接查
鏡像基表要自己解碼。已知一組：**`SE_ORD_ITEM.STATUS`：`1`=新单 `7`=生效 `25`=完工 `29`=出货
`99`=销货 `0`=取消**（BQ_SE_ORDITEM 的 DECODE 撈出）。14 個 view 全文 SQL 定義在
`var/data/erp_schema_probe/erp-bq-views.md`。

### 「庫存編號」（SF24 這類舊短碼）＝ `SP00.SP_ITEM.O_ITEMNO`（2026-07-28 SF24 案）

ERP 畫面（如 `SE-SETF_440` 訂單材料追蹤）的「庫存編號」欄不是料號、也不是倉庫代碼，
是料號主檔 `SP00.SP_ITEM` 的 **O_ITEMNO（原/舊料號）**——倉庫與台灣採購慣用的短碼
（`SF24`、`SF23.7`、`CL14.1`、`D587`）。三個重要事實：

1. **`SP00.SP_ITEM` 對 APP 唯讀帳號無 SELECT 授權**：schema probe 看不到（不在 SP00 的
   95 張表裡）、鏡像的 1,538 張表也不含它——全鏡像掃 `SF24` 掃不到任何一筆是正常的。
2. **對照走 `GG_1001.GF_ITEM_O_ITEMNO(org_id, item_no)`**（PUBLIC 執行權），以
   **完整料號（含色碼尾）**為鍵：`GF_ITEM_O_ITEMNO(1,'SFIXI0500T003600400-A010')='SF24'`，
   去掉色碼尾查不到。form 的查詢條件正是
   `GG_1001.GF_ITEM_O_ITEMNO(ORG_ID,ITEM_NO) = :庫存編碼`（從 `D:\PRG_JH\SETF_440.fmx`
   strings 挖出）；庫存編碼 LOV = `SELECT O_ITEMNO, O_ITEMNM FROM SP_ITEM`。
3. **鏡像已落地對照表**：`agent_core/erp_mirror.sync_item_alias()`（每日 02:00 隨熱表刷新）
   借上述函式把鏡像內用到的料號全集解成 `_item_alias(ITEM_NO, O_ITEMNO)` 表＋
   `v_item_alias(料號, 庫存編號)` 視圖；`query_erp_stock`/`query_erp_bom`/
   `query_erp_purchase_orders` 對關鍵字做**精確**（不分大小寫）舊碼展開——SF24 不得
   外推 SF24.5，短碼一字之差就是不同材料。手動重建：`scripts/erp_mirror.py --alias-only`。
