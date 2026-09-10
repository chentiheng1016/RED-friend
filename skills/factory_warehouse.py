"""工廠數據倉 text-to-SQL 工具 — Phase 1。

小紅用自然語言精確查生產數據（加總／未完／趨勢／跨客戶比較）：Gemini 生 DuckDB SELECT
→ 靜態驗證 → 唯讀連線（外部檔案存取關閉）+ deadline 看門狗執行 → 文字。比 RAG 撈片段可靠。
倉由 agent_core.factory_warehouse 建（每日重建）。設計見 docs/factory_warehouse_design.md。

防線（defense-in-depth，參考 skills/excel_ops.py excel_query）：
  1. 只允許 SELECT/WITH、單語句
  2. deny 寫入／檔案存取／擴充關鍵字（read_csv/COPY/ATTACH/INSTALL…）
  3. read_only 連線 —— 引擎層擋寫，驗證被繞過也寫不進去
  4. enable_external_access=false —— 擋掉 DuckDB 讀本機檔案（防 SELECT … FROM '/etc/passwd' 外洩）
  5. deadline 中斷跑飛的查詢
"""
import os
import re
import threading

# 寫入 / 檔案存取 / 擴充 / 多語句控制 —— 一律 deny（唯讀查詢用不到）。
_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|MERGE|GRANT|"
    r"ATTACH|DETACH|COPY|PRAGMA|INSTALL|LOAD|EXPORT|IMPORT|CALL|SET|RESET|"
    r"read_csv|read_parquet|read_json|read_text|read_blob|read_ndjson|"
    r"glob|sniff_csv|parquet_scan|csv_scan)\b",
    re.IGNORECASE,
)
_QUERY_DEADLINE_S = 20.0
_MAX_ROWS = 50


def _db_path():
    from agent_core.factory_warehouse import warehouse_db_path
    return warehouse_db_path()


def _ensure_db():
    """倉不存在就建一次（首查自舉）。回 (ok, msg)。"""
    if os.path.exists(_db_path()):
        return True, ""
    from agent_core.factory_warehouse import build_factory_warehouse
    res = build_factory_warehouse()
    if res.get("error"):
        return False, res["error"]
    return os.path.exists(_db_path()), ""


def _connect_ro():
    import duckdb
    return duckdb.connect(_db_path(), read_only=True,
                          config={"enable_external_access": "false"})


def _schema_text(con):
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
    ).fetchall()
    by_table = {}
    for t, c, dt in rows:
        by_table.setdefault(t, []).append(f"{c} {dt}")
    return "\n".join(f"{t}({', '.join(cols)})" for t, cols in by_table.items())


def _validate_select(sql):
    """只放行單一 SELECT/WITH；否則 raise ValueError。"""
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
    """唯讀執行 + deadline 看門狗。回 (columns, rows)。"""
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


def _format(cols, rows):
    if not rows:
        return "（查無資料）"
    out = [" | ".join(cols)]
    for r in rows[:_MAX_ROWS]:
        out.append(" | ".join("" if v is None else str(v) for v in r))
    if len(rows) > _MAX_ROWS:
        out.append(f"…（共 {len(rows)} 列，只顯示前 {_MAX_ROWS}）")
    return "\n".join(out)


def query_factory_warehouse(question: str) -> str:
    """用自然語言查工廠數據倉（生產數量／累計／未完／趨勢／跨客戶比較）。

    問「DECA 累計完工多少」「這月哪天產能最高」「誰欠最多沒出貨」這類**要精確數字／加總**
    的問題走這個 —— Gemini 生 SQL 直查結構化倉，比 read_drive_file/RAG 撈片段可靠。免 +確認。
    劃界：生管日報（欠數/希望/實際出貨日）、郵件實體、LOT 單據/付款對帳的權威源在這；
    「訂單明細/BOM 用料/採購 MRP/庫存/工單」請改用 query_erp_warehouse（ERP 本地鏡像）。

    Args:
        question: 自然語言問題（中英文皆可）。
    Returns:
        答案（附實際執行的 SQL，可複核）。
    """
    ok, msg = _ensure_db()
    if not ok:
        return f"❌ 數據倉不可用：{msg or '建倉失敗'}"

    from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
    con = _connect_ro()
    try:
        schema = _schema_text(con)
    finally:
        con.close()

    prompt = (
        "你是 DuckDB SQL 專家。下面是工廠數據倉 schema。重點：\n"
        "- 日期欄是文字 ISO 字串如 '2026-06-25'，空字串代表無；actual_ship 有值代表已出貨；\n"
        "- molding 此廠走 PU 灌注多為 0；fact_order_state.status ∈ shipped/completed/open；\n"
        "- fact_email_thread=內部郵件，customers/suppliers/po_numbers/products 是 ' | ' 串文字（用 LIKE 查）；\n"
        "- 要把某客戶的『生產進度＋郵件』對起來：dim_customer.customer_id = fact_order_state.customer_raw，"
        "郵件端用 bridge_email_customer(thread_id, customer_id)；\n"
        "- bridge_email_po 是郵件裡的 PO 號，通常**不等於**生產 work_order（指令），別硬 join；\n"
        "- fact_shipment_doc=出貨/付款單據登記（從 Drive 檔名解析）：doc_type∈invoice/shipping/courier/"
        "remittance/receipt/coo/bill_of_lading/insurance/other、lot_number 像 '309-2025'；想盤「LOT X 有哪些單據」"
        "「哪些 LOT 缺匯款單」用它 GROUP BY lot_number。**amount/currency 只是檔名提示、非權威**。\n"
        "- fact_payment=單據抽的金額/日期/對方/發票號（file_id 接 fact_shipment_doc）；"
        "問「LOT X 付了多少」「本月匯款總額」用它。currency 已正規化成 ISO 碼（USD/EUR/CNY/TWD）；"
        "**算總額/分組請用 counterparty_norm**（對方正規化鍵，已併大小寫/標點變體）、顯示用 counterparty；"
        "amount_source∈content（模型抽）/filename（檔名回填）/空（無金額）。涵蓋率隨每日 backfill 漸增、可能還不全。\n"
        "- dim_order_xref=以**客戶**串 identifier（id_type='work_order'|'po'、identifier、customer_id、confidence）；"
        "要「某客戶所有指令＋PO」WHERE customer_id=…。⚠️LOT↔PO↔指令 row-level 連不起來（資料不支援），只能客戶級。\n\n"
        f"{schema}\n\n"
        "只輸出**一句 DuckDB SELECT**（可用 WITH）回答問題。不要解釋、不要 markdown、"
        "不要分號結尾、不要寫入或讀外部檔。\n問題：" + (question or "")
    )
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[prompt],
                                caller="factory_warehouse.query")
        sql = (resp.text or "").strip()
        m = re.search(r"```(?:sql)?\s*\n?(.+?)```", sql, re.DOTALL)
        if m:
            sql = m.group(1).strip()
    except Exception as e:  # noqa: BLE001
        return f"❌ 生 SQL 失敗：{e}"

    try:
        safe = _validate_select(sql)
    except ValueError as e:
        return f"❌ 生成的 SQL 被拒（{e}）：\n{sql[:300]}"
    try:
        cols, rows = _run_ro(safe)
    except Exception as e:  # noqa: BLE001
        return f"❌ 查詢失敗：{type(e).__name__}: {e}\n\nSQL：{safe[:300]}"

    return f"問：{question}\nSQL：{safe}\n\n{_format(cols, rows)}"


def run_warehouse_sql(sql: str) -> str:
    """直接對工廠數據倉跑唯讀 SQL（給要精準控制查詢時用，例如大王自己寫 SQL）。

    倉內表：fact_production_daily（每日×各客戶×指令 各站日產量、累計、欠數、希望/實際出貨日）、
    fact_order_state（每張指令當前完工率/未完/狀態 shipped|completed|open）。日期欄為文字。
    只接受單一 SELECT/WITH；寫入、多語句、檔案存取一律拒絕。免 +確認。

    Args:
        sql: 單一 DuckDB SELECT/WITH 語句。
    Returns:
        查詢結果表（文字，前 50 列）。
    """
    ok, msg = _ensure_db()
    if not ok:
        return f"❌ 數據倉不可用：{msg or '建倉失敗'}"
    try:
        safe = _validate_select(sql)
    except ValueError as e:
        return f"❌ 查詢被拒：{e}"
    try:
        cols, rows = _run_ro(safe)
    except Exception as e:  # noqa: BLE001
        return f"❌ 查詢失敗：{type(e).__name__}: {e}\n\nSQL：{safe[:300]}"
    return _format(cols, rows)


def backfill_factory_payments(max_docs: int = 25) -> str:
    """抽出貨/付款單據(PDF)的**真實**金額/日期/對方/發票號進 fact_payment（增量、預算上限）。

    fact_shipment_doc 只盤點單據；這個讀單據內容抽真實金額。每次抽 max_docs 份未抽過的，
    結果持久化、下次續抽（每日 cron 也會自動補一小批）。抽完重建倉才反映。免確認。

    Args:
        max_docs: 這次最多抽幾份（預設 25，上限 200）。
    Returns:
        摘要（抽了幾份、還剩幾份、花費 USD）。
    """
    from agent_core.factory_warehouse_extract import extract_payments_batch
    n = max(1, min(int(max_docs or 25), 200))
    res = extract_payments_batch(max_docs=n)
    if res.get("error"):
        return f"❌ {res['error']}"
    return (f"✅ 抽取 {res['extracted']} 份單據金額（還剩 {res['candidates_remaining']} 份待抽，"
            f"花費 ${res['spent_usd']}）。重建倉後 fact_payment 反映。")


def query_accounts_payable(supplier: str = "", currency: str = "", top: int = 15) -> str:
    """查應付帳款：我們還欠哪些供應商、多少、帳齡多久（付款請示單↔匯款 金額錨定配對推估）。

    用「付款請示單(應付/欠) ↔ 匯款(已付)」按 幣別+精確金額 配對（繞過供應商中英名不一致），
    配不到匯款的應付款＝未付 open AP，依供應商分組 + 帳齡分桶（0-30/31-60/61-90/90+ 天）。
    問「我們欠 X 多少」「哪些供應商逾期最久」「未付應付款」用這個。免 +確認。

    ⚠️ 是「從零散單據推估」非 ERP 總帳：只涵蓋已 ingest+抽到金額的單據；一張匯款沖多張
    應付款（合併付款）會讓那些誤判未付。

    Args:
        supplier: 只看某供應商（名稱含此字串；留空=全部）。
        currency: 只看某幣別（USD/EUR/CNY/TWD；留空=全部）。
        top: 顯示未付最多的前幾家（預設 15）。
    Returns:
        應付帳款帳齡摘要（文字）。
    """
    ok, msg = _ensure_db()
    if not ok:
        return f"❌ 數據倉不可用：{msg or '建倉失敗'}"
    from collections import defaultdict
    from datetime import datetime

    from agent_core.factory_ap_recon import reconcile_payments
    con = _connect_ro()
    try:
        rows = con.execute(
            "SELECT doc_type, amount, currency, counterparty, counterparty_norm, doc_date "
            "FROM fact_payment WHERE doc_type IN ('payable','remittance')").fetchall()
    except Exception as e:  # noqa: BLE001
        return f"❌ 讀 fact_payment 失敗：{type(e).__name__}: {e}"
    finally:
        con.close()
    cols = ["doc_type", "amount", "currency", "counterparty", "counterparty_norm", "doc_date"]
    data = [dict(zip(cols, r)) for r in rows]
    if not any(d["doc_type"] == "payable" for d in data):
        return ("（尚無 payable 應付款單據可對帳 — 付款請示單要先經 backfill_factory_payments 抽取入倉。）")

    res = reconcile_payments(data, as_of_date=datetime.now().strftime("%Y-%m-%d"))
    sups = res["suppliers"]
    if supplier:
        sups = [s for s in sups if supplier.lower() in (s["counterparty"] or "").lower()]
    if currency:
        sups = [s for s in sups if s["currency"] == currency.upper()]
    open_sups = [s for s in sups if s["open_balance"] > 0.5]

    cur_open = defaultdict(float)
    for s in open_sups:
        cur_open[s["currency"]] += s["open_balance"]

    exact = res["n_payables_matched_exact"]
    agg = res.get("n_payables_matched_aggregated", 0)
    fee_adj = res["n_payables_matched_paid"] - exact - agg
    out = [f"📊 應付帳款對帳（as_of {res['as_of']}）",
           f"付款請示單 {res['n_payables']} 張、已配到匯款(已付) {res['n_payables_matched_paid']} 張"
           f"（{exact} 精確 + {fee_adj} 扣匯費 + {agg} 合併付款）；"
           f"匯款 {res['n_payments']} 張（{res['n_payments_unmatched']} 張未配到應付款）"]
    if cur_open:
        out.append("未付合計：" + "、".join(
            f"{c} {v:,.0f}" for c, v in sorted(cur_open.items(), key=lambda x: -x[1])))
    n = min(int(top or 15), len(open_sups))
    out.append(f"\n未付最多 TOP{n}（供應商｜幣｜未付｜最舊未付｜0-30/31-60/61-90/90+）：")
    for s in open_sups[:n]:
        a = s["aging"]
        out.append(f"  {s['counterparty'][:22]:<24} {s['currency']:<4} {s['open_balance']:>11,.0f}"
                   f"｜{s['oldest_open'] or '-':<10}｜"
                   f"{a['0-30']:,.0f}/{a['31-60']:,.0f}/{a['61-90']:,.0f}/{a['90+']:,.0f}")
    out.append("\n⚠️ 從付款請示單↔匯款金額配對推估、非 ERP 總帳；合併付款/未 ingest 單據會有誤差。")
    return "\n".join(out)


def rebuild_factory_warehouse() -> str:
    """重建工廠數據倉（抓 Drive 最新生產日報重新落庫）。日報更新後想立即反映時用。免 +確認。"""
    from agent_core.factory_warehouse import build_factory_warehouse
    res = build_factory_warehouse()
    if res.get("error"):
        return f"❌ 建倉失敗：{res['error']}"
    msg = (f"✅ 數據倉已重建：{res.get('daily_rows', 0)} 日產量 / {res.get('orders', 0)} 指令 / "
           f"{res.get('email_threads', 0)} 郵件 / {res.get('shipment_docs', 0)} 單據 / "
           f"{res.get('payment_rows', 0)} 付款")
    if res.get("report_modified"):
        msg += f"（來源修改日 {res['report_modified']}）"
    if res.get("warnings"):
        msg += "\n⚠️ 解析提醒：" + "；".join(res["warnings"])
    return msg


SKILL_TOOLS = [query_factory_warehouse, run_warehouse_sql, query_accounts_payable,
               rebuild_factory_warehouse, backfill_factory_payments]
