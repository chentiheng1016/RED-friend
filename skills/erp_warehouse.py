"""飛越 ERP 本地鏡像 text-to-SQL 工具 —— 讓小紅用中文精確查全 ERP。

資料 = 飛越 ERP 全量鏡像進本地 DuckDB（1,538 表 / 2,198 萬列，見 agent_core/erp_mirror），
上面建了 20 個中文可讀解碼視圖（v_orders/v_bom/v_production… 見 var/data/erp_mirror/
DATA_DICTIONARY.md）。小紅用自然語言問 → Gemini 生 DuckDB SELECT（**優先用可讀視圖**）
→ 靜態驗證 → 唯讀連線（關外部檔案存取）+ deadline 看門狗執行 → 文字。

防線（同 skills/factory_warehouse.py）：
  1. 只允許 SELECT/WITH、單語句
  2. deny 寫入／檔案存取／擴充關鍵字
  3. read_only 連線（引擎層擋寫）
  4. enable_external_access=false（擋讀本機檔）
  5. deadline 中斷跑飛的查詢
tier 全 SAFE（純讀、免 +確認）。
"""
import os
import re
import threading
import unicodedata

# 只用「防注入」層（NFKC + zero-width strip + injection marker），不用 PII 遮蔽：
# ERP 品名/客戶名是業務資料，IBAN/卡號偵測會把料號誤遮（同 skills/erp_oracle.py 的取捨）。
from agent_core.prompt_injection import sanitize_untrusted_text

_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|MERGE|GRANT|"
    r"ATTACH|DETACH|COPY|PRAGMA|INSTALL|LOAD|EXPORT|IMPORT|CALL|SET|RESET|"
    r"read_csv|read_parquet|read_json|read_text|read_blob|read_ndjson|"
    r"glob|sniff_csv|parquet_scan|csv_scan)\b",
    re.IGNORECASE,
)
_QUERY_DEADLINE_S = 30.0
_MAX_ROWS = 60

# 領域指引（餵給 Gemini，濃縮自 DATA_DICTIONARY.md）——這是 text-to-SQL 準不準的關鍵。
_DOMAIN_GUIDE = """飛越 ERP 本地鏡像（DuckDB）。**優先查下面的中文可讀視圖**（已解碼客戶名/狀態/站別）；
底層 1,538 張生表名為 OWNER__TABLE（如 SC00__SE_ORD_M），只有可讀視圖不夠時才碰。
通則：**所有欄位型別都是 VARCHAR**，做加總/比大小要 TRY_CAST(欄 AS BIGINT/DOUBLE)；
日期欄是文字 'YYYY-MM-DD HH24:MI:SS'（比較用字串即可，如 >= '2026-07-01'）。

可讀視圖：
- v_orders(單號,客戶,客戶PO,單據日,鞋款,產品,數量,狀態,交期,已出貨數,...) 訂單。狀態∈新单/生效/完工/出货/销货/取消。
- v_order_materials(單號,料號,需求,訂購,點收,發料,材料類型,品名描述) 訂單物料到料。
- v_bom(訂單號,鞋款楦號,客戶簡稱,材料類別,部位碼,料號,單位用量,需求量,淨需求量,...) 訂單BOM部位級。
- **v_bom_named = v_bom + 材料類型 + 品名描述（料號已接中文品名、100% 覆蓋）→ 問 BOM 用料要看得懂的品名一律用這個**。
- v_item_names(料號base,材料類型,品名描述) 料號→中文品名主檔(~5559)。要單獨查某料號中文名用它；
  它 key 是 base 料號(去尾色碼)，join 用 regexp_replace(料號,'-[A-Z0-9]+$','')。
- v_bom_size(尺寸級BOM,98萬列) / v_rd_bom(研發標準BOM). 量大，務必帶 WHERE 訂單號/鞋款。
- v_purchase_orders(採購單:單號,供應商,料號,數量,日期,狀態,品名描述) / v_mrp(物料需求淨算:料號,需求,已訂,缺口,品名描述).
- v_stock(即時庫存:料號,倉,數量,品名描述) / v_stock_lot(含批號) / v_inventory_trans(庫存異動:料號,異動別,數量,日期,品名描述).
  **「可用庫存」＝v_stock_lot 批號='0' 的量**（M01 批=待倉庫確認調撥、J0C% 批=預購綁單，都不可挪用）——
  答「可用/能撥多少」必濾 批號='0'，別拿 v_stock 全批合計充當可用。
- v_work_orders(工單:工單號,訂單號SE_ID,鞋款,數量,狀態) / v_dispatch(派工) /
  v_production(每日生產:生產單號,生產日期,工單號,訂單號,鞋款,客戶簡稱,站別,異動類型,生產數量).
  站別∈裁斷/針車/貼合/射出/成型/包裝等；**一筆=一次站點異動**，異動類型∈入站/出站。
- v_samples(樣品) / v_sample_bom / v_sample_bom_colorway 樣品室。
- v_customers(49客戶) / v_products(613型體) / v_vendors(455供應商) / v_code_dictionary(碼類,碼,名 通用代碼).
- v_item_alias(料號,庫存編號) 料號↔庫存編號(倉庫/採購慣用舊短碼如 SF24、CL14.1)對照。
  **「庫存編號」≠料號**：使用者給 SF24 這種短碼時先 join/查 v_item_alias 換成料號再查各表，
  查無對照別憑空推論「BOM 未用此料」。

常用 join：v_production.訂單號 = v_orders.單號；v_bom.訂單號 = v_orders.單號；
v_work_orders.訂單號 = v_orders.單號。客戶用「客戶」或「客戶簡稱」欄。

⚠️ 已知資料陷阱（違反會靜默算錯，務必遵守）：
1. **按天彙總產量要用 substr(生產日期,1,10)**——生產日期粒度混雜（多數整天一筆、部分站帶
   時分秒），直接 GROUP BY 生產日期 會把同一天拆碎成多列。
1b. **加總 v_production 產量必濾 異動類型='入站'**——同批鞋「進站」「出站」各記一筆，混加
   會重複計數（完成度曾因此虛胖到 200%）。某站的日產量=當日入站該站的數量；出站是移轉/
   出貨紀錄（低頻大批量），除非明確要看移轉否則別加進產量。
2. **v_mrp 含歷史已結案列**——算「目前缺料」必濾 MRP狀態='生效'，否則把多年前的歷史缺口全算進去。
3. 財務問題答不了：GL00/EP00(總帳/應收付)幾乎全空、GL_IMP 只有 2014-19 歷史匯入——別碰；
   TIPTOP/SHIRLEY/BTW 是匯入暫存/系統 metadata，別當業務資料。
3b. **v_order_materials 同一筆需求會同時掛在裸碼與 +EP 版次料號兩列**（-G050 與
   -G050-005 各一列、數值相同）——逐單/逐料加總要先去重（同單同料家族取最大），
   直接 SUM 會翻倍。逐訂單實際需求/每雙攤提有現成工具 query_erp_order_demand。
3c. **「哪天分配給哪張指令訂單」查生表 SC00__PO_ITEM_SELOT（庫存可用量分配）**——
   SE_ID=分配訂單、LOT_QTY=本次分配數、LOT_DATE=分配日期，**別拿批號/需求數字
   對得上就推論分配關係**（G407 案）。⚠️ STOC_NO 欄雙語意：STATUS='1'(1-預購)時
   放轉出批號(J0C…)、'3'/'9' 時放倉別；裸碼/+EP 版次雙記同筆要去重。
   逐筆分配有現成工具 query_erp_allocation。
4. 「實際出貨日/成型產量/郵件/付款對帳」不在本倉——在 factory_warehouse（Drive 生管日報），
   會由另一個工具處理，回答時說明即可、別硬湊。

範例（正確寫法）：
Q: 昨天各站產量 → SELECT 站別, SUM(TRY_CAST(生產數量 AS BIGINT)) FROM v_production
   WHERE substr(生產日期,1,10)='<昨天YYYY-MM-DD>' AND 異動類型='入站' GROUP BY 站別
Q: 目前缺料最大的前10 → SELECT 料號, 品名描述, SUM(TRY_CAST(缺口數量 AS DOUBLE)) g FROM v_mrp
   WHERE MRP狀態='生效' GROUP BY 1,2 ORDER BY g DESC LIMIT 10
Q: 某客戶生效訂單 → SELECT 單號, 鞋款, 數量, 交期 FROM v_orders WHERE 客戶 LIKE '%LURCHI%' AND 狀態='生效'"""


def _db_path():
    from agent_core.logging_and_paths import DATA_DIR
    return os.path.join(DATA_DIR, "erp_mirror", "erp_full.duckdb")


def _connect_ro():
    import duckdb
    return duckdb.connect(_db_path(), read_only=True,
                          config={"enable_external_access": "false"})


def _view_schema(con):
    """列出可讀視圖(v_*)的欄位，餵給 Gemini（不列 1538 張生表，太多且欄名代碼化）。"""
    rows = con.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='main' AND table_name LIKE 'v\\_%' ESCAPE '\\' "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()
    by_view = {}
    for t, c in rows:
        by_view.setdefault(t, []).append(c)
    return "\n".join(f"{t}({', '.join(cols)})" for t, cols in by_view.items())


def _validate_select(sql):
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise ValueError("空查詢")
    if ";" in s:
        raise ValueError("不允許多語句（含分號）")
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise ValueError("只允許 SELECT / WITH 查詢")
    if _FORBIDDEN_SQL.search(s):
        raise ValueError("含禁用關鍵字（寫入／檔案存取／擴充）")
    return s


def _run_ro(sql):
    con = _connect_ro()
    timer = threading.Timer(_QUERY_DEADLINE_S, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, cur.fetchall()
    finally:
        timer.cancel()
        con.close()


_MAX_CELL = 48


def _disp_width(s: str) -> int:
    """CJK 終端顯示寬度（全形算 2）——Telegram <pre> 等寬下中文欄才對得齊。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _wpad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _cell(v) -> str:
    """單格：淨化 ERP 自由文字（品名/客戶名是自由輸入欄，防 prompt injection）+ 截斷。"""
    if v is None:
        return ""
    s = sanitize_untrusted_text(v).strip() if isinstance(v, str) else str(v)
    s = s.replace("\n", " ").replace("\t", " ")
    return s[: _MAX_CELL - 1] + "…" if len(s) > _MAX_CELL else s


def _format(cols, rows):
    """結果表 → Telegram 友善 code block（CJK 對齊；Telegram 不渲染 | 表格）。"""
    if not rows:
        return "（查無資料）"
    body = [[_cell(v) for v in r] for r in rows[:_MAX_ROWS]]
    widths = [_disp_width(str(c)) for c in cols]
    for r in body:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], _disp_width(c))

    def fmt(r):
        return "  ".join((c if i >= len(widths) else _wpad(str(c), widths[i]))
                         for i, c in enumerate(r))

    lines = [fmt(cols), "  ".join("-" * w for w in widths)] + [fmt(r) for r in body]
    tail = f"\n…（共 {len(rows)} 列，只顯示前 {_MAX_ROWS}）" if len(rows) > _MAX_ROWS else ""
    return "```\n" + "\n".join(lines) + "\n```" + tail


def _db_ready():
    return os.path.exists(_db_path())


def query_erp_warehouse(question: str) -> str:
    """用自然語言查飛越 ERP（訂單／BOM用料／採購MRP／庫存／生產工單／樣品）—— 全量鏡像本地倉。

    問「LURCHI 這個月生效訂單幾雙」「JFC26556 用哪些料」「昨天各站產量」「哪些料缺口最大」
    這類要**精確數字／加總／跨表**的問題走這個 —— Gemini 生 DuckDB SQL 直查已解碼的中文視圖，
    比翻 Drive/RAG 可靠。免 +確認。
    劃界：訂單/BOM/採購/庫存/工單的權威源在這；「實際出貨日、郵件往來、付款/應付對帳、
    生管日報的欠數」在 query_factory_warehouse（Drive 生管日報+郵件 lake），別用本工具硬湊。

    Args:
        question: 自然語言問題（中英文皆可）。
    Returns:
        答案（附實際執行的 SQL，可複核）。
    """
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
    con = _connect_ro()
    try:
        schema = _view_schema(con)
    finally:
        con.close()

    base_prompt = (
        "你是 DuckDB SQL 專家。下面是飛越 ERP 本地鏡像的領域指引與可讀視圖 schema。\n\n"
        + _DOMAIN_GUIDE + "\n\n視圖欄位：\n" + schema + "\n\n"
        "只輸出**一句 DuckDB SELECT**（可用 WITH）回答問題。不要解釋、不要 markdown、"
        "不要分號結尾、不要寫入或讀外部檔。數值欄記得 TRY_CAST。\n問題：" + (question or "")
    )

    def _gen(prompt: str) -> str:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[prompt],
                                caller="erp_warehouse.query")
        sql = (resp.text or "").strip()
        m = re.search(r"```(?:sql)?\s*\n?(.+?)```", sql, re.DOTALL)
        return m.group(1).strip() if m else sql

    # 錯誤回饋重生一次：DuckDB 的錯誤訊息品質高（BinderException 會附
    # Candidate bindings、CatalogException 會附 Did you mean），回餵幾乎必修好。
    sql, last_err = "", ""
    for attempt in (1, 2):
        prompt = base_prompt if attempt == 1 else (
            base_prompt + f"\n\n你上次生成的 SQL：\n{sql}\n執行失敗：{last_err}\n"
            "請根據錯誤訊息修正，仍只輸出一句 DuckDB SELECT。"
        )
        try:
            sql = _gen(prompt)
        except Exception as e:  # noqa: BLE001
            return f"❌ 生 SQL 失敗：{e}"
        try:
            safe = _validate_select(sql)
            cols, rows = _run_ro(safe)
            note = "（第 2 次修正成功）" if attempt == 2 else ""
            return f"問：{question}{note}\nSQL：{safe}\n\n{_format(cols, rows)}"
        except Exception as e:  # noqa: BLE001 - 驗證被拒(ValueError)與執行錯誤都回餵重生
            last_err = f"{type(e).__name__}: {str(e)[:400]}"

    return f"❌ 查詢失敗（重試 1 次仍錯）：{last_err}\n\nSQL：{sql[:300]}"


def query_erp_stock(keyword: str, show_lots: bool = False) -> str:
    """查料號/品名的 ERP 各倉庫存結存（確定性、不經 AI 生成——數字保證與 ERP 鏡像一致）。

    輸入料號片段、品名關鍵字或**庫存編號**（ERP 舊短碼，如 "SF24"——會自動對應
    料號），回每料號×倉別結存＋批次分解（**可用庫存＝0 批**；M01 批要倉庫確認
    調撥、預購批綁單，都不可直接挪用）。**問「某料庫存多少/可用多少」一律
    優先用這個**；問「採購單訂購/已收數量」用 query_erp_purchase_orders；問
    「某訂單缺不缺料/短少數量」用 kitting_check（庫存≠該單有料——共用料先到
    先贏）；要跨表加總/複雜條件才用 query_erp_warehouse。
    耗材（膠水/底料/工具/文具）在倉庫 Excel → read_warehouse_stock。免 +確認。

    Args:
        keyword: 料號片段、品名關鍵字或庫存編號舊短碼（至少 2 個字）。
        show_lots: True 時加列批號/儲位明細（最多前 5 個料號）。
    Returns:
        各倉結存清單（附鏡像時間，可複核）。
    """
    from agent_core.erp_stock_query import erp_stock_lookup
    return erp_stock_lookup(keyword, show_lots=bool(show_lots))


def query_erp_purchase_orders(keyword: str, date_from: str = "",
                              date_to: str = "") -> str:
    """查 ERP 採購單逐項次明細＋合計（確定性、不經 AI 生成——數字與 ERP 鏡像一字不差）。

    **問「某料/某供應商/某採購單 採購了多少、訂購數量、已收/進貨數量」一律用這個**，
    不要用 search_drive_docs 讀採購單 PDF——PDF 文字裡數量與金額欄混排，LLM 抓錯欄
    出過事故（把美金金額當訂購數量回報）。輸出每項次「訂購數量/已收數量/單價/金額」
    分欄明示＋合計（排除取消項次）。⚠️ 採購到貨是**綁單配額**的：判斷「某訂單
    缺不缺料/短少」要用 kitting_check，別拿別張單的到貨當這張單有料。免 +確認。

    Args:
        keyword: 料號片段/品名/供應商/採購單號/庫存編號舊短碼（至少 2 個字，
            如 "G40"、"富泰"、"JF0P26060007"、"SF24"）。
        date_from: 選填，下單日期起（YYYY、YYYY-MM 或 YYYY-MM-DD；如 "2026"）。
        date_to: 選填，下單日期迄（同上格式）。
    Returns:
        逐項次採購明細＋數量/金額分列合計（附鏡像時間，可複核）。
    """
    from agent_core.erp_stock_query import erp_po_lookup
    return erp_po_lookup(keyword, date_from=str(date_from or ""),
                         date_to=str(date_to or ""))


def query_erp_bom(keyword: str) -> str:
    """查 ERP 生效版 BOM 用料（確定性、不經 AI 生成——與 ERP 研發 BOM 一字不差）。

    **問「某材料/料號 用在哪些型體、哪幾款（客戶款號/STYLE#）」或「某型體/某款
    的 BOM 用哪些料」一律用這個**，不要用 search_drive_docs 讀 BOM 表——Drive BOM
    是型體級證據，LLM 曾把同型體「別的顏色」也算成用料款（實際用料是顏色級，
    料號尾碼即色號）。方向自動判定：給料號→反查使用款（附「同型體不用此料」
    清單防外推）；給型體/客戶款號→列生效版逐料明細；給**庫存編號**（ERP 舊短碼，
    如 "SF24"）→自動對應料號後反查。⚠️ BOM 用量是「每雙標準用量」，**別**拿它
    ×訂單雙數自算需求或短少——訂單實際需求量/每雙攤提（含尺寸配比/損耗）用
    query_erp_order_demand，缺料判定用 kitting_check。免 +確認。

    Args:
        keyword: 料號片段 / 生管型體 / 客戶款號 / 庫存編號舊短碼（至少 2 個字，
            如 "DFDT7100145000-E040"、"DJS336195"、"8916446"、"SF24"）。
    Returns:
        生效版 BOM 用料/反查清單（附鏡像時間，可複核）。
    """
    from agent_core.erp_stock_query import erp_bom_lookup
    return erp_bom_lookup(keyword)


def query_erp_order_demand(keyword: str) -> str:
    """查 ERP 訂單實際材料需求＝訂單材料追蹤照表念（確定性、不經 AI 生成）。

    **問「某料/某庫存編號 實際需求量多少、每雙實際用量/攤提用量、跟 BOM 標準
    用量差多少」一律用這個**，不要拿 BOM 單位用量×雙數自算、也不要用 Drive
    BOM 成本表的單一用量回答——ERP 需求數量是依尺寸配比＋損耗展算的訂單實際
    需求，兩者天生不同（2026-07-29 G407 案：Drive 成本表 0.05268 米/雙，ERP
    實際攤提 12.04/221≈0.06、1.14/32≈0.04）。輸出依 STYLE#×型體分組：逐生效
    訂單 需求數量/領料出庫/訂單雙數/每雙攤提（附無條件進位口徑）＋分組合計
    ＋訂單 BOM 每雙用量對照，口徑同 ERP 訂單材料追蹤畫面（SE-SETF_440）。
    劃界：問「哪幾款用某料/BOM 標準用量」用 query_erp_bom；問「某訂單缺不缺
    料/短少」用 kitting_check；查庫存用 query_erp_stock。免 +確認。

    Args:
        keyword: 料號片段或庫存編號舊短碼（至少 2 個字，如
            "DFD00400D05700-G050"、"G407"）。
    Returns:
        逐訂單需求/攤提清單（附鏡像時間，可複核）。
    """
    from agent_core.erp_stock_query import erp_demand_lookup
    return erp_demand_lookup(keyword)


def query_erp_sample_orders(keyword: str) -> str:
    """查 ERP 樣品開發單（SR 樣品單）＋樣品 BOM 用料（確定性、不經 AI 生成）。

    **問「某料/某庫存編號 用在哪些 SR 樣品單」或「某樣品單/某鞋款 的樣品 BOM
    用哪些料」一律用這個**——樣品開發域（SP00）不在研發 BOM/訂單/採購工具的
    查詢範圍，別拿 query_erp_bom 查無就推論「無樣品單」（2026-07-29 UserC
    PU468 案：freeform 憑備料採購單斷言「ERP 無獨立 SR 樣品單」，實際 4 張
    SR 單在用該料）。方向自動判定：給料號/庫存編號→反查使用的樣品單×鞋款
    （含幾個版次含此料、最新版是否仍用）；給樣品單號（SR2511001）或鞋款
    （JA1065）→列樣品單＋最新版樣品 BOM 逐料明細。
    劃界：打樣進度/寄樣動態用 read_sample_status；樣品**備料採購單**（J0M…）
    用 query_erp_purchase_orders；量產型體 BOM 用 query_erp_bom。免 +確認。
    反查同時照表念兩件事、語意不同別混用：「使用」清單＝樣品 BOM 出現過；
    「📌 來源樣品單號」＝料品主檔對照表（SP_ITEM_RDITEM，即開發料品畫面
    右下欄）記載的建立來源——問「這料源自哪張樣品單」以 📌 行為準，別拿
    使用清單第一張冒充（PU468 實例：來源=SR2511002、清單首張=SR2511001）。

    Args:
        keyword: 樣品單號 / 鞋款 / 料號片段 / 庫存編號舊短碼（至少 2 個字，
            如 "SR2511001"、"JA1065"、"PUB014T1718D05400000"、"PU468"）。
    Returns:
        樣品單反查/用料清單（附鏡像時間，可複核）。
    """
    from agent_core.erp_stock_query import erp_sample_lookup
    return erp_sample_lookup(keyword)


def query_erp_allocation(keyword: str, date_from: str = "",
                         date_to: str = "") -> str:
    """查 ERP 庫存分配紀錄＝庫存可用量分配照表念（確定性、不經 AI 生成）。

    **問「某料 哪天分配給哪張指令訂單/分配了多少」「某訂單 分到哪些料」
    「某預購批 綁哪張單/何時分配」一律用這個**，不要拿庫存批次＋訂單需求
    自行推論——批號與需求數字對得上≠就是那張單、更≠使用者問的那天分配
    （2026-07-30 UserA G407 案：freeform 把 07-23 分配給 JFC26563 的舊紀錄
    當成 07-30 的分配回報；當天實際新分配是 JFC26574）。輸出逐筆
    分配日期/指令訂單/本次分配數/轉出批號或倉別/類別，口徑同 ERP 庫存可用量
    分配畫面（PO-POTF_240）。⚠️ 鏡像是快照：**今天剛在 ERP 做的分配要明晨
    刷新後才查得到**，問「今天分配了什麼」要明講快照時點、以 ERP 畫面為準。
    劃界：批次現況/可用量用 query_erp_stock；訂單實際需求/攤提用
    query_erp_order_demand；缺料判定用 kitting_check。免 +確認。

    Args:
        keyword: 料號片段/庫存編號舊短碼/指令訂單號/預購批號（至少 2 個字，
            如 "G407"、"DFD00400D05700-G050"、"JFC26574"、"J0C26070005"）。
        date_from: 選填，分配日期起（YYYY、YYYY-MM 或 YYYY-MM-DD）。
        date_to: 選填，分配日期迄（同上格式；查單日兩者填一樣）。
    Returns:
        逐筆分配紀錄清單（附鏡像時間，可複核）。
    """
    from agent_core.erp_stock_query import erp_allocation_lookup
    return erp_allocation_lookup(keyword, date_from=str(date_from or ""),
                                 date_to=str(date_to or ""))


def query_erp_payables(keyword: str, date_from: str = "",
                       date_to: str = "") -> str:
    """查 ERP 應付請款單＝帳單（確定性、不經 AI 生成——與 ERP 應付域一字不差）。

    **問「某供應商 帳單總金額/請款/應付/付了多少」一律用這個**，不要拿
    query_erp_purchase_orders 的採購單金額加總——採購單不含運費等不掛採購單的
    費用，出過事故（三寶 2026 帳單漏運輸費 5,140 CNY，正確 116,642.20）。輸出
    每張請示單「應付淨額(含稅)」＋貨款/費用分解（非採購單費用逐列明示）＋合計。
    免 +確認。

    Args:
        keyword: 供應商簡稱/全名/代號、請示單號或備註關鍵字（至少 2 個字，如
            "三寶"、"H1L10001"、"JFPA2605007"）。
        date_from: 選填，申請日期起（YYYY、YYYY-MM 或 YYYY-MM-DD；如 "2026"）。
        date_to: 選填，申請日期迄（同上格式）。
    Returns:
        逐請示單應付明細＋幣別分列合計（附鏡像時間，可複核）。
    """
    from agent_core.erp_stock_query import erp_ap_lookup
    return erp_ap_lookup(keyword, date_from=str(date_from or ""),
                         date_to=str(date_to or ""))


def run_erp_warehouse_sql(sql: str) -> str:
    """直接對飛越 ERP 本地鏡像倉跑唯讀 SQL（要精準控制查詢、或大王自己寫 SQL 時用）。

    可查 20 個中文可讀視圖（v_orders/v_bom/v_production/v_stock/v_purchase_orders…）
    與 1,538 張底層生表（OWNER__TABLE，如 SC00__SE_ORD_M）。所有欄位為 VARCHAR、
    數值要 TRY_CAST。只接受單一 SELECT/WITH；寫入、多語句、檔案存取一律拒絕。免 +確認。

    Args:
        sql: 單一 DuckDB SELECT/WITH 語句。
    Returns:
        查詢結果表（文字，前 60 列）。
    """
    if not _db_ready():
        return "❌ ERP 本地鏡像倉不存在（要先跑 scripts/erp_mirror.py）。"
    try:
        safe = _validate_select(sql)
    except ValueError as e:
        return f"❌ 查詢被拒：{e}"
    try:
        cols, rows = _run_ro(safe)
    except Exception as e:  # noqa: BLE001
        return f"❌ 查詢失敗：{type(e).__name__}: {e}\n\nSQL：{safe[:300]}"
    return _format(cols, rows)


def list_erp_views() -> str:
    """列出飛越 ERP 本地鏡像倉可用的中文可讀視圖 + 各自欄位（給查詢前先看有哪些表）。免 +確認。"""
    if not _db_ready():
        return "❌ ERP 本地鏡像倉不存在（要先跑 scripts/erp_mirror.py）。"
    con = _connect_ro()
    try:
        schema = _view_schema(con)
        counts = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='BASE TABLE'").fetchall()
    finally:
        con.close()
    return (f"飛越 ERP 本地鏡像：{len(counts)} 張底層生表 + 下列中文可讀視圖\n\n" + schema)


# 生管日報「指令」欄帶產線/分批後綴（JFC26345-1-1），飛越 ERP 的單號(SE_ID)是不帶
# 後綴的 base 號（JFC26345）。只剝尾端「-數字-數字」兩段——與 erp_oracle._base_se_id
# 同一條規則，才不會誤砍 Pilot-336195 這種本身就帶單一 dash 的合法單號。
_LINE_SUFFIX_RE = re.compile(r"^(.+?)-\d+-\d+$")


def _pack_progress_from_schedule(schedule) -> dict[str, dict[str, int]]:
    """生管日報排程列 → {base 指令: {"cum": 已包裝累計, "rem": 未完}}。

    同一張單拆多條產線／多個型體時，日報是每 (客戶,指令,型體) 一列（每列都已是
    「最後生產日」的累計快照），對到同一個 ERP 單號要相加。
    """
    agg: dict[str, dict[str, int]] = {}
    for s in schedule:
        wo = str(s.get("work_order") or "").strip().upper()
        if not wo:
            continue
        m = _LINE_SUFFIX_RE.match(wo)
        a = agg.setdefault(m.group(1) if m else wo, {"cum": 0, "rem": 0})
        a["cum"] += int(s.get("pack_cum") or 0)
        a["rem"] += int(s.get("pack_rem") or 0)
    return agg


def _pack_progress_by_order() -> tuple[dict[str, dict[str, int]], str]:
    """讀生管日報 → (每單已包裝/未完, 來源檔名)。完成度的權威源就是這份。

    欠數欄整欄解析失敗時（版型位移，_rem_all_zero_warning 護欄）丟 ValueError 讓
    呼叫端退回 ERP 粗估：拿一份假性全 0 的未完去報「都做完了」比沒有數字更糟。
    """
    from agent_core.production_schedule import _load_schedule, _rem_all_zero_warning
    schedule, name = _load_schedule()
    warn = _rem_all_zero_warning(schedule)
    if warn:
        raise ValueError(warn)
    return _pack_progress_from_schedule(schedule), name


def erp_delivery_risk_alert(days_ahead: int = 14, days_overdue: int = 14) -> str:
    """交期風險示警（確定性、免 Gemini）：掃「仍在生效(ERP 未標完工/出貨)、但交期將到或已過」
    的訂單，逐單直接附上生管日報的已包裝／未完。每日早報 daemon 用；互動問答也可直接叫。免 +確認。

    可信訊號用 ERP 自己的狀態：狀態='生效' = ERP 認定這單「還沒做完」（完工/出货/销货 會自動
    排除）。× 交期落在 [今天-days_overdue, 今天+days_ahead]（排除長期殭屍單）。
    完成度**照生管日報念**（Drive 那份日報的欠數/包裝累計才是權威源，同 query_erp_warehouse
    的劃界）：日報「指令」帶產線後綴，剝成 base 號對 ERP 單號、多條線相加 → 看報表的人不必
    再追問一次某單做到哪。
    ⚠️ ERP 包裝流水（v_production 站別='包裝'）不能當完成度：非 1:1 對訂單、有重工/多批/尺碼
    彙總，實測完工單有顯示 3% 或 200% 的。只在生管日報讀不到時當退路，且表上標 ≈／來源「ERP粗估」。
    ⚠️ 清單不因「日報未完=0」隱藏該單（日報沒更新/沒收到也會是 0，濾掉＝漏報）——只在表頭
    標出有幾張是「工廠已做完、ERP 還沒結案」，逾期那個數字也一併註明其中幾張是待結案
    （逾期＝ERP 沒標完工，不等於真的沒做）。同理也不用完成度當門檻過濾。
    ⚠️ 缺料判斷也不做——點收/發料僅 ~50% 非零；真缺料待 v_mrp_fulfillment（串 PO_MRP_PO+收貨）。

    Args:
        days_ahead: 往後看幾天內到期（預設 14）。
        days_overdue: 往回看幾天內已逾期（預設 14；更早視為殭屍單不洗版）。
    Returns:
        風險清單（無風險回「(無新發現)」）。
    """
    if not _db_ready():
        return "❌ ERP 本地鏡像倉不存在。"
    from datetime import datetime, timedelta
    today = datetime.now()
    today_s = today.strftime("%Y-%m-%d")
    lo = (today - timedelta(days=int(days_overdue))).strftime("%Y-%m-%d")
    hi = (today + timedelta(days=int(days_ahead))).strftime("%Y-%m-%d")
    sql = f"""
WITH win AS (
  SELECT 單號, 客戶, 鞋款, TRY_CAST(數量 AS BIGINT) AS 數量, substr(交期,1,10) AS 交期日
  FROM v_orders
  WHERE 狀態='生效' AND 交期 IS NOT NULL
    AND substr(交期,1,10) BETWEEN '{lo}' AND '{hi}'
), prod AS (
  -- 異動類型='入站'：入站/出站混加會重複計數（完成度虛胖 200% 的主嫌之一）
  SELECT 訂單號 AS 單號, SUM(TRY_CAST(生產數量 AS BIGINT)) AS 包裝累計
  FROM v_production WHERE 站別='包裝' AND 異動類型='入站' GROUP BY 1
)
SELECT w.交期日,
       CASE WHEN w.交期日 < '{today_s}' THEN '逾期' ELSE '將到' END AS 風險,
       w.單號, w.客戶, w.鞋款, w.數量,
       LEAST(COALESCE(p.包裝累計,0), w.數量) AS ERP包裝流水
FROM win w LEFT JOIN prod p ON p.單號=w.單號
ORDER BY (w.交期日 < '{today_s}') DESC, w.交期日, w.單號
"""
    try:
        _cols, rows = _run_ro(sql)
    except Exception as e:  # noqa: BLE001
        return f"❌ 交期風險查詢失敗：{type(e).__name__}: {str(e)[:200]}"
    if not rows:
        return "(無新發現)"   # 早退：沒風險單就不必為了完成度去下載生管日報

    # 完成度照生管日報念；讀不到才退回 ERP 包裝流水粗估（整份報表不因此掛掉）。
    try:
        prog, sheet = _pack_progress_by_order()
        why = "" if prog else "日報裡沒有任何可用的指令列"
    except Exception as e:  # noqa: BLE001
        prog, sheet, why = {}, "", f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"

    out_cols = ["交期日", "風險", "單號", "客戶", "鞋款", "數量", "已包裝", "未完", "來源"]
    out_rows, exact, done, overdue_done = [], 0, 0, 0
    for day, risk, sid, cust, style, qty, approx in rows:
        p = prog.get(str(sid or "").strip().upper())
        if p:
            exact += 1
            if p["rem"] <= 0:
                done += 1
                overdue_done += 1 if str(day) < today_s else 0
            packed, remain, src = f"{p['cum']:,}", f"{p['rem']:,}", "生管日報"
        else:
            packed, remain, src = f"≈{int(approx or 0):,}", "—", "ERP粗估"
        out_rows.append([day, risk, sid, cust, style, qty, packed, remain, src])

    # 逾期數是這封信最上面、最嚇人的那個數字，但它只表示「ERP 沒標完工」——實測
    # 一半的逾期單其實工廠早做完、只是 ERP 沒結案（2026-08-12：2 張逾期全是這種）。
    # 有日報的精確未完就在手上，當場註明幾張是待結案，別讓人為了分辨再問一次。
    overdue = sum(1 for r in rows if str(r[0]) < today_s)
    if not overdue_done:
        od = f"{overdue} 張已逾期"
    elif overdue_done >= overdue:
        od = f"{overdue} 張已逾期（皆已做完、待 ERP 結案）"
    else:
        od = f"{overdue} 張已逾期（其中 {overdue_done} 張已做完、待 ERP 結案）"
    head = [f"⚠️ ERP 交期風險（生效訂單=ERP 未標完工、交期 {lo}〜{hi}）："
            f"共 {len(rows)} 張，其中 {od}。"]
    if why:
        head.append(f"　已包裝只有 ERP 包裝流水粗估（≈，重工/多批/尺碼彙總會失真、非完成度）"
                    f"——生管日報讀不到：{why}")
    else:
        miss = len(rows) - exact
        head.append(f"　已包裝/未完：{exact} 張照生管日報《{sheet}》逐單念（精確）"
                    + (f"，{miss} 張日報查無該指令、只有 ERP 粗估（≈）。" if miss else "。"))
        if done:
            head.append(f"　其中 {done} 張日報未完=0（工廠已做完、ERP 還沒結案），"
                        f"真正要追的是另外 {len(rows) - done} 張。")
    return "\n".join(head) + "\n" + _format(out_cols, out_rows)


SKILL_TOOLS = [query_erp_warehouse, query_erp_stock, query_erp_purchase_orders,
               query_erp_bom, query_erp_order_demand, query_erp_sample_orders,
               query_erp_payables, query_erp_allocation,
               run_erp_warehouse_sql, list_erp_views, erp_delivery_risk_alert]

# 純讀 ERP 鏡像、無副作用 → 允許背景 dispatcher 用。
# erp_delivery_risk_daily 排程任務的第 1 步就是呼叫它。只開這支：同模組的
# run_erp_warehouse_sql（任意 SQL 面）背景任務不需要，維持關閉。
erp_delivery_risk_alert.background_safe = True
