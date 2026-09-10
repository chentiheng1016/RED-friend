"""工廠數據倉（DuckDB）建倉層 — Phase 1。

把 factory_production_report.iter_production_rows() 的逐列產出落成結構化 DuckDB 表，
讓小紅能用 SQL 精確查（加總／join／趨勢），而不是退回 RAG 撈模糊片段。
設計見 docs/factory_warehouse_design.md。

Phase 1 範圍：生產日報 → fact_production_daily（每日明細）+ fact_order_state（每張指令當前狀態）。
（email lake / 出貨 OCR 是 Phase 2/3。）

建倉策略：每次全量重建到 <db>.tmp 再 os.replace 原子換掉 —— 查詢端（唯讀連線）永遠不會
讀到半成品倉。日期欄一律存文字（actual_ship 常是 'OK' 之類非日期標記，逾期與否交查詢時算）。
"""
import json
import os
import re

from agent_core.logging_and_paths import DATA_DIR

_DB_NAME = "factory_warehouse.duckdb"

# 內部郵件 lake：rag_sync/internal_ingest 每日寫的 parquet（含 Gemini 抽好的實體）。
_EMAIL_PARQUET_REL = ("data_lake_internal", "emails.parquet")

# 倉客戶（customer_raw）→ 郵件裡可能出現的額外別名。郵件 entities.customers 很髒
# （1877 個含物流/供應商/內部），只把對得上這 6 個真客戶的連起來。新客戶會自動進
# dim_customer（別名預設只有自己），要補別名（如 DECA.26↔Decathlon）改這裡。
_CUSTOMER_ALIAS_MAP = {
    "DECA.26": ["DECATHLON", "DECA"],
    "JALAS": ["EJENDALS"],
}

_DDL = """
CREATE TABLE fact_production_daily (
    report_file_id  TEXT,
    report_modified TEXT,
    prod_day        INTEGER,
    customer_raw    TEXT,
    work_order      TEXT,
    model           TEXT,
    ordered_pairs   INTEGER,
    stitching_day   INTEGER,
    molding_day     INTEGER,
    insock_day      INTEGER,
    injection_day   INTEGER,
    packing_day     INTEGER,
    pack_cum        INTEGER,
    pack_rem        INTEGER,
    want_ship       TEXT,
    actual_ship     TEXT
);
CREATE TABLE fact_order_state (
    work_order      TEXT,
    customer_raw    TEXT,
    model           TEXT,
    ordered_pairs   INTEGER,
    pack_cum_latest INTEGER,
    pack_rem_latest INTEGER,
    completion_pct  DOUBLE,
    want_ship       TEXT,
    actual_ship     TEXT,
    status          TEXT,
    as_of_day       INTEGER,
    report_file_id  TEXT
);
"""

# Phase 2：內部郵件 lake → 可 SQL 查的表。實體（customers/suppliers/po_numbers…）以
# ' | ' 串成文字欄（小紅 LIKE 查 + 顯示都友善）；po / customer 另炸平成 bridge 表供 join。
_EMAIL_DDL = """
CREATE TABLE fact_email_thread (
    thread_id         TEXT,
    date              TEXT,
    last_message_date TEXT,
    sender            TEXT,
    primary_dept      TEXT,
    direction         TEXT,
    state             TEXT,
    summary           TEXT,
    message_count     INTEGER,
    topic_tags        TEXT,
    customers         TEXT,
    suppliers         TEXT,
    po_numbers        TEXT,
    products          TEXT,
    promised_dates    TEXT
);
CREATE TABLE bridge_email_po (
    thread_id  TEXT,
    po_number  TEXT
);
CREATE TABLE dim_customer (
    customer_id   TEXT,   -- = 生產 fact 的 customer_raw（可直接 join）
    display_name  TEXT,
    aliases       TEXT    -- ' | ' 串，含郵件變體（DECA.26 ↔ Decathlon/DECA）
);
CREATE TABLE bridge_email_customer (
    thread_id    TEXT,
    customer_id  TEXT    -- 對得上 6 個真客戶的郵件才連（郵件客戶欄很髒，只連乾淨的）
);
"""

# Phase 3a：出貨/付款單據「登記表」—— 純解析 Drive 檔名（零 OCR）。檔名常自帶 LOT 號、
# 單據類型，有時連金額（'USD 9575--LOT 203-2025 匯款單.jpg'）/AWB 都有。內容級欄位
# （真實金額/客戶/PO）留待 3b OCR；此表先讓小紅盤點「LOT X 有哪些單據」。
_SHIPMENT_DDL = """
CREATE TABLE fact_shipment_doc (
    file_id     TEXT,
    title       TEXT,
    doc_type    TEXT,    -- invoice/shipping/courier/remittance/receipt/coo/bill_of_lading/insurance/other
    lot_number  TEXT,    -- 從檔名抽的 'NNN-YYYY'（如 '309-2025'），無則空
    awb         TEXT,    -- 快遞單號（檔名有才有）
    currency    TEXT,    -- 檔名金額提示（非權威，3b 才從內容抽）
    amount      DOUBLE,
    mime_type   TEXT,
    modified    TEXT,
    drive_id    TEXT
);
"""


def warehouse_db_path():
    """倉檔路徑（var/data/factory_warehouse.duckdb）。不保證已存在。"""
    return os.path.join(DATA_DIR, _DB_NAME)


def email_parquet_path():
    """內部郵件 lake parquet 路徑（var/data/data_lake_internal/emails.parquet）。"""
    return os.path.join(DATA_DIR, *_EMAIL_PARQUET_REL)


def payments_store_path():
    """Phase 3b：單據內容抽取結果的持久化 jsonl（倉外）。倉每次全量重建會清表，
    抽取昂貴不能每次重做，故存在這裡、重建時載回 fact_payment。由 factory_warehouse_extract 增量寫。"""
    return os.path.join(DATA_DIR, "factory_payments.jsonl")


def _date_text(v):
    """want_ship/actual_ship 原始 cell → 存庫字串：datetime 取 'YYYY-MM-DD'，其餘原樣 str。空→''。"""
    if v is None or v == "":
        return ""
    iso = getattr(v, "isoformat", None)
    if callable(iso):
        try:
            return v.isoformat()[:10]
        except Exception:  # noqa: BLE001
            pass
    return str(v).strip()


def _is_shipped(actual):
    return bool(actual) and str(actual).strip().upper() not in ("", "NONE")


def _order_status(pack_rem, actual_ship):
    """時間無關的狀態：已出貨 / 已完工待出 / 未完。逾期(overdue)交查詢時依當下日期算。"""
    if _is_shipped(actual_ship):
        return "shipped"
    if pack_rem <= 0:
        return "completed"
    return "open"


def _norm_cust(s):
    """客戶名正規化比對鍵：去空白/點/橫線/斜線、轉大寫。"""
    return re.sub(r"[\s./\\-]+", "", str(s or "")).upper()


def _dim_customer_rows(prod_customers):
    """從生產的 customer_raw 衍生 dim_customer 列 + 別名查找表。

    回 (rows, alias_lookup)：rows=[(customer_id, display, aliases_str)]；
    alias_lookup={正規化別名: customer_id}（給郵件客戶比對用）。
    """
    rows, lookup = [], {}
    for cid in sorted({c for c in prod_customers if c}):
        aliases = [cid] + _CUSTOMER_ALIAS_MAP.get(cid, [])
        rows.append((cid, cid, " | ".join(aliases)))
        for a in aliases:
            lookup[_norm_cust(a)] = cid
    return rows, lookup


def _match_customer(token, alias_lookup):
    """郵件客戶 token → 倉 customer_id（對不上回 None）。"""
    return alias_lookup.get(_norm_cust(token))


def _ingest_email_lake(con, parquet_path, alias_lookup):
    """讀內部郵件 parquet → fact_email_thread + bridge_email_po + bridge_email_customer。

    用建倉用的 con（duckdb 直讀 parquet）。實體存 ' | ' 串；po / 對得上的客戶炸平成 bridge。
    回 summary dict。
    """
    rows = con.execute(
        "SELECT thread_id, date, last_message_date, sender, primary_dept, direction, "
        "state, summary, message_count, topic_tags, entities_json "
        "FROM read_parquet(?)",
        [parquet_path],
    ).fetchall()

    def _join(xs):
        return " | ".join(x for x in xs if x)

    thread_rows, po_rows, cust_rows = [], [], []
    seen_po, seen_cust = set(), set()
    for (tid, date, lmd, sender, dept, direction, state, summary, mc, tags, ej) in rows:
        try:
            ent = json.loads(ej) if ej else {}
        except (ValueError, TypeError):
            ent = {}
        custs = [str(c).strip() for c in (ent.get("customers") or []) if str(c).strip()]
        sups = [str(s).strip() for s in (ent.get("suppliers") or []) if str(s).strip()]
        ponums = [str(p).strip() for p in (ent.get("po_numbers") or []) if str(p).strip()]
        prods = [str(p).strip() for p in (ent.get("products") or []) if str(p).strip()]
        promised = [str(p).strip() for p in (ent.get("promised_dates") or []) if str(p).strip()]
        thread_rows.append((
            tid, date, lmd, sender, dept, direction, state, summary,
            int(mc or 0), tags or "",
            _join(custs), _join(sups), _join(ponums), _join(prods), _join(promised),
        ))
        for p in ponums:
            if (tid, p) not in seen_po:
                seen_po.add((tid, p))
                po_rows.append((tid, p))
        for c in custs:
            for part in re.split(r"[/、,&]", c):  # 'LURCHI/RICHTER' 拆開分別比對
                cid = _match_customer(part, alias_lookup)
                if cid and (tid, cid) not in seen_cust:
                    seen_cust.add((tid, cid))
                    cust_rows.append((tid, cid))

    con.executemany(
        "INSERT INTO fact_email_thread VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", thread_rows)
    if po_rows:
        con.executemany("INSERT INTO bridge_email_po VALUES (?,?)", po_rows)
    if cust_rows:
        con.executemany("INSERT INTO bridge_email_customer VALUES (?,?)", cust_rows)
    return {"email_threads": len(thread_rows),
            "email_po_links": len(po_rows),
            "email_customer_links": len(cust_rows)}


# 單據類型判斷（先特定後一般）；檔名抽 LOT / AWB / 金額提示。
# remittance 先於 payable（匯款是已付、不要被「付款請示單」搶走）；payable 先於 invoice
# （「應付款 PROFORMA INVOICE」是供應商帳單＝我們的應付，歸 payable 不歸 invoice）。
_DOC_TYPE_RULES = [
    ("remittance", ("匯款", "remit", "匯出匯款")),
    ("payable", ("應付", "付款請示", "應付請示", "預付款請示", "請示單",
                 "請款帳單", "請款明細", "請款單", "payment request")),
    ("invoice", ("發票", "invoice")),
    ("statement", ("對帳單", "對帳表", "對帳函", "statement of account")),
    ("courier", ("快遞", "awb", "courier")),
    ("shipping", ("出貨", "shipping", "shipment", "packing", "delivery", "裝箱")),
    ("receipt", ("收據", "receipt")),
    ("coo", ("coo", "certificate of origin", "產地證")),
    ("bill_of_lading", ("sbl", "b/l", "bill of lading", "提單")),
    ("insurance", ("保單", "insurance")),
]
_LOT_RE = re.compile(r"LOT\s*([0-9]{1,4}-[0-9]{4})", re.IGNORECASE)
_AWB_RE = re.compile(r"\bAWB\s*[:#-]?\s*([0-9][0-9 -]{5,20})", re.IGNORECASE)
_AMT_RE = re.compile(r"\b(US\$|USD|EUR|RMB|CNY|NT\$|TWD)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
# 加 應付/請款/請示單/對帳：把真正的進貨應付單據（付款請示單、請款帳單、對帳單）也列進倉。
_SHIPMENT_NAME_KEYWORDS = ("LOT", "快遞", "AWB", "匯款", "收據", "發票", "INVOICE",
                           "出貨", "PACKING", "COO", "保單",
                           "應付", "請款", "請示單", "對帳")


def _parse_doc_title(title):
    """從 Drive 檔名抽 {doc_type, lot_number, awb, currency, amount}（純字串、零 OCR）。"""
    t = str(title or "")
    tl = t.lower()
    doc_type = "other"
    for dt, kws in _DOC_TYPE_RULES:
        if any(k in tl or k in t for k in kws):
            doc_type = dt
            break
    lot = ""
    m = _LOT_RE.search(t)
    if m:
        lot = m.group(1)
    awb = ""
    m = _AWB_RE.search(t)
    if m:
        awb = re.sub(r"[ -]", "", m.group(1))
    currency, amount = "", None
    m = _AMT_RE.search(t)
    if m:
        currency = m.group(1).upper().replace("US$", "USD").replace("NT$", "TWD").replace("RMB", "CNY")
        try:
            amount = float(m.group(2).replace(",", ""))
        except ValueError:
            amount = None
    return {"doc_type": doc_type, "lot_number": lot, "awb": awb,
            "currency": currency, "amount": amount}


def _ingest_shipment_docs(con, docs):
    """docs=[{file_id,title,mime,modified,drive_id}] → fact_shipment_doc（解析檔名）。"""
    rows = []
    for d in docs:
        p = _parse_doc_title(d.get("title", ""))
        rows.append((
            d.get("file_id", ""), d.get("title", ""), p["doc_type"], p["lot_number"],
            p["awb"], p["currency"], p["amount"], d.get("mime", ""),
            d.get("modified", ""), d.get("drive_id", ""),
        ))
    if rows:
        con.executemany(
            "INSERT INTO fact_shipment_doc VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return {"shipment_docs": len(rows)}


def _list_shipment_docs(service, max_pages_per_kw=8):
    """Drive 列出出貨/付款類檔（metadata，零下載/OCR）。失敗回 []（不擋建倉）。"""
    seen = {}
    for kw in _SHIPMENT_NAME_KEYWORDS:
        safe = kw.replace("\\", "\\\\").replace("'", "\\'")
        page, pages = None, 0
        while pages < max_pages_per_kw:
            try:
                res = service.files().list(
                    q=f"name contains '{safe}' and trashed = false",
                    pageSize=1000, pageToken=page,
                    fields="nextPageToken, files(id,name,mimeType,modifiedTime,driveId)",
                    includeItemsFromAllDrives=True, supportsAllDrives=True, corpora="allDrives",
                ).execute()
            except Exception:  # noqa: BLE001
                break
            for f in res.get("files", []):
                seen[f["id"]] = {
                    "file_id": f["id"], "title": f.get("name", ""),
                    "mime": f.get("mimeType", ""),
                    "modified": str(f.get("modifiedTime", ""))[:10],
                    "drive_id": f.get("driveId", ""),
                }
            page = res.get("nextPageToken")
            pages += 1
            if not page:
                break
    return list(seen.values())


def _load_payments(con, payments_store):
    """建 fact_payment 表，把持久化 jsonl（Phase 3b 抽取結果）正規化+回填後載回。回載入筆數。

    在這個咽喉點對整份 store 套 enrich_payment_record（每次全量重建都跑）：
      - currency 正規化成 ISO、counterparty_norm 供 SUM/GROUP BY；
      - 內容抽不到的 amount/doc_date/counterparty 以檔名標題回填、標 amount_source；
      - 同一 file_id 去重（store 是 append-only，留最後一筆＝最新抽取），免 SUM 重複計。
    故現有已抽記錄無需重抽，下次重建即享回填/正規化。
    """
    con.execute("""
        CREATE TABLE fact_payment (
            file_id TEXT, lot_number TEXT, doc_type TEXT, title TEXT,
            amount DOUBLE, currency TEXT, doc_date TEXT, counterparty TEXT,
            counterparty_norm TEXT, invoice_no TEXT, amount_source TEXT, extracted_at TEXT
        )""")
    if payments_store and os.path.exists(payments_store) and os.path.getsize(payments_store) > 0:
        from agent_core.factory_payment_norm import enrich_payment_record

        by_id, no_id = {}, []   # file_id 去重（後者覆蓋前者＝最新）；無 file_id 的全留
        with open(payments_store, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                fid = str(rec.get("file_id") or "")
                if fid:
                    by_id[fid] = rec
                else:
                    no_id.append(rec)

        rows = []
        for rec in list(by_id.values()) + no_id:
            e = enrich_payment_record(rec)
            rows.append((
                str(e.get("file_id") or ""), str(e.get("lot_number") or ""),
                str(e.get("doc_type") or ""), str(e.get("title") or ""),
                e.get("amount"), str(e.get("currency") or ""),
                str(e.get("doc_date") or ""), str(e.get("counterparty") or ""),
                str(e.get("counterparty_norm") or ""), str(e.get("invoice_no") or ""),
                str(e.get("amount_source") or ""), str(e.get("extracted_at") or ""),
            ))
        if rows:
            con.executemany(
                "INSERT INTO fact_payment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return con.execute("SELECT count(*) FROM fact_payment").fetchone()[0]


def _build_order_xref(con):
    """Phase 3c：dim_order_xref —— 以「客戶」串三套 identifier 的誠實 crosswalk。

    grounding 證明 row-level LOT↔PO↔指令不可行（生產無 LOT 欄、PO∩指令僅 25/9959），唯一
    可靠的共同維度是**客戶**。故這裡只記 (識別碼 → 客戶) 的可靠關聯，附 confidence；要「某客戶
    的所有指令/PO」就 WHERE customer_id=...。LOT→客戶 留待 fact_payment counterparty 正規化（部分）。
    """
    con.execute("""
        CREATE TABLE dim_order_xref (
            id_type     TEXT,    -- work_order | po
            identifier  TEXT,
            customer_id TEXT,    -- = 倉 customer_raw
            source      TEXT,
            confidence  DOUBLE
        )""")
    # 生產指令 → 客戶（reliable）
    con.execute(
        "INSERT INTO dim_order_xref SELECT DISTINCT 'work_order', work_order, customer_raw, "
        "'production', 1.0 FROM fact_order_state WHERE work_order <> ''")
    # 郵件 PO → 客戶（同 thread 有對上客戶才連，thread 級關聯）
    con.execute(
        "INSERT INTO dim_order_xref SELECT DISTINCT 'po', p.po_number, c.customer_id, "
        "'email_thread', 0.7 FROM bridge_email_po p "
        "JOIN bridge_email_customer c ON p.thread_id = c.thread_id WHERE p.po_number <> ''")
    return con.execute("SELECT count(*) FROM dim_order_xref").fetchone()[0]


def build_warehouse_from_bytes(xlsx_bytes, db_path=None, *, report_file_id="", report_modified="",
                               email_parquet=None, shipment_docs=None, payments_store=None):
    """從生產日報 xlsx bytes 建倉（可單元測試，不碰 Drive）。回 summary dict。"""
    import duckdb

    from agent_core.factory_production_report import iter_production_rows

    db_path = db_path or warehouse_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    tmp_path = db_path + ".tmp"
    for p in (tmp_path, tmp_path + ".wal"):  # 清掉上次中途失敗的殘留
        if os.path.exists(p):
            os.remove(p)

    warnings = set()
    daily = []
    latest = {}  # work_order -> 聚合（最新一天）
    for row in iter_production_rows(xlsx_bytes, warnings=warnings):
        daily.append((
            report_file_id, report_modified, row["prod_day"],
            row["customer"], row["work_order"], row["model"],
            int(row["pairs"]), int(row["stitching_day"]), int(row["molding_day"]),
            int(row["insock_day"]), int(row["injection_day"]), int(row["packing_day"]),
            int(row["pack_cum"]), int(row["pack_rem"]),
            _date_text(row["want_ship"]), _date_text(row["actual_ship"]),
        ))
        if row["is_latest_day"] and row["work_order"]:
            o = latest.setdefault(row["work_order"], {
                "customer": row["customer"], "model": row["model"],
                "ordered": 0, "pack_cum": 0, "pack_rem": 0,
                "want_ship": row["want_ship"], "actual_ship": row["actual_ship"],
                "as_of_day": row["prod_day"],
            })
            o["ordered"] += int(row["pairs"])
            o["pack_cum"] += int(row["pack_cum"])
            o["pack_rem"] += int(row["pack_rem"])

    orders = []
    for wo, o in latest.items():
        ordered = o["ordered"]
        pct = round(o["pack_cum"] / ordered * 100, 1) if ordered > 0 else None
        orders.append((
            wo, o["customer"], o["model"], ordered, o["pack_cum"], o["pack_rem"], pct,
            _date_text(o["want_ship"]), _date_text(o["actual_ship"]),
            _order_status(o["pack_rem"], o["actual_ship"]), o["as_of_day"], report_file_id,
        ))

    email_summary, shipment_summary, payment_rows, xref_rows = {}, {}, 0, 0
    con = duckdb.connect(tmp_path)
    try:
        con.execute(_DDL)
        con.execute(_EMAIL_DDL)
        con.execute(_SHIPMENT_DDL)
        if daily:
            con.executemany(
                "INSERT INTO fact_production_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", daily)
        if orders:
            con.executemany(
                "INSERT INTO fact_order_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", orders)
        # dim_customer 從生產客戶衍生（+ 別名）；郵件客戶 bridge 靠它回的別名查找表
        prod_customers = [r[0] for r in con.execute(
            "SELECT DISTINCT customer_raw FROM fact_order_state").fetchall()]
        dim_rows, alias_lookup = _dim_customer_rows(prod_customers)
        if dim_rows:
            con.executemany("INSERT INTO dim_customer VALUES (?,?,?)", dim_rows)
        # 內部郵件 lake（有檔才攝取；測試/離線時可不給）
        if email_parquet and os.path.exists(email_parquet):
            email_summary = _ingest_email_lake(con, email_parquet, alias_lookup)
        if shipment_docs:
            shipment_summary = _ingest_shipment_docs(con, shipment_docs)
        payment_rows = _load_payments(con, payments_store)
        xref_rows = _build_order_xref(con)
    finally:
        con.close()
    os.replace(tmp_path, db_path)
    for junk in (tmp_path + ".wal",):  # close 後若有殘留 wal（已 checkpoint）清掉
        if os.path.exists(junk):
            os.remove(junk)

    return {
        "db_path": db_path,
        "daily_rows": len(daily),
        "orders": len(orders),
        "warnings": sorted(warnings),
        "report_file_id": report_file_id,
        "report_modified": report_modified,
        "payment_rows": payment_rows,
        "xref_rows": xref_rows,
        **email_summary,
        **shipment_summary,
    }


def build_factory_warehouse(file_id="", db_path=None):
    """抓 Drive 最新（或指定 file_id）生產日報 → 建倉。回 summary dict（失敗帶 'error'）。"""
    from agent_core.factory_production_report import (
        _download_xlsx_bytes,
        _find_latest_progress_file,
    )

    fid = (file_id or "").strip()
    if not fid:
        fid, _name = _find_latest_progress_file()
        if not fid:
            return {"error": "找不到任何『生產日報進度表』檔（Drive）"}
    try:
        xlsx_bytes, meta = _download_xlsx_bytes(fid)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"下載生產日報失敗：{type(exc).__name__}: {exc}"}
    modified = str(meta.get("modifiedTime", ""))[:10]
    try:
        from agent_core.google_auth import get_service
        shipment_docs = _list_shipment_docs(get_service("drive", "v3"))
    except Exception:  # noqa: BLE001
        shipment_docs = []
    return build_warehouse_from_bytes(
        xlsx_bytes, db_path=db_path, report_file_id=fid, report_modified=modified,
        email_parquet=email_parquet_path(), shipment_docs=shipment_docs,
        payments_store=payments_store_path())
