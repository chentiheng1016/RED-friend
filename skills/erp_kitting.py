"""齊套/缺料上線預警 —— 掃「快要上線的訂單哪張料沒齊、缺哪些、補貨何時到」。

資料 = 飛越 ERP 本地鏡像（DuckDB，每日 02:00 熱表刷新，見 agent_core/erp_mirror）。
全部確定性 SQL + Python 彙算，不經 Gemini；唯讀連線、SAFE tier 免確認。

設計依 2026-07-24 八路探勘實測校正（每條都有數字支撐，改動前先想清楚）：
  - 「上線日」ERP 裡不存在（預定開工日全空、排程模組 0 列）——用 交期−63 天（實測
    交期−首次入站 p75）畫寬預警窗；工單+預建派工當「已放行」優先訊號。
  - 「點收」在完工單 ground truth 上零鑑別力（31% 料項 發料>0 點收=0 = 漏登），
    已到訊號 = max(點收, 發料, PO已收×配額份額)，料類可信度另以完工單發料達成率
    動態校準（≥0.70 才可信；不可信料類無訊號時標「無法判定」而非缺料）。
  - 在途不可拿 PO 明細整行量（一行平均拆給 10.66 張訂單，直加膨脹 15 倍）——一律
    經 SC00__PO_MRP_PO 配額按比例分攤、cap 至配額。
  - v_mrp 全面禁用（生效版 80.6% 需求掛已銷貨/取消死單、計劃需求日全過期）。
  - 庫存共用料 78% 跨單共用且 ERP 無保留概念——FCFS-by-交期 跨單模擬扣減，
    否則單張看夠、全體看不夠（實測 42/94 短缺料號如此）。
  - 單位陷阱：31.4% 料號 v_stock 單位≠採購單位——不一致者庫存標「單位待換算」
    不入數字；需求 vs 點收/發料/訂購 同表同單位可直接比。
  - 可用庫存＝**0 批**（2026-07-29 採購實務校正）：M01 批要倉庫實地確認調撥到
    0 批才可用、預購批(J0C…)綁單不可挪用——FCFS 池只吃 0 批，M01/預購另掛
    「待確認」資訊欄；足跡判定仍看全批（存在性訊號）。
  - 量欄 NULL 一律 COALESCE 成 0（未結案 PO 已收 60.9% NULL，不補=在途靜默消失）；
    視圖量欄已是 DOUBLE 直接用，生表才 TRY_CAST(AS DOUBLE)。
  - 委外加工單 SC00__PO_PROC_D.STATUS 開放值域實測 = {'0','7'}（'99'=結案）。
  - 客供料 PR_TYPE='13' 或 IS_PR='N' → 免採購，不報缺料。
  - Khai Hoan 3 結構性無 BOM（39 張單 0 張有）→ 白名單排除防常駐假警報。
  - 微量列（縫線 0.02–0.9 單位換算殘渣）會霸榜 → 需求 < min_need 直接略過；
    最終缺口 < min_need 亦視為齊（損耗率小數尾差）。
  - 「無追蹤資料」分層（探勘⑤的 8%）：全系統零足跡（庫存表無行、本單無 MRP 配額、
    零到料）＝帳外管理料（實測 55KG 桶裝化工料如此），標資訊層不掛紅——上線首日
    62 張全紅的另一主因。足跡判定看全倉（含廢料倉），扣抵才用排除後的可用庫存。
"""
import datetime
import os
import threading
import unicodedata

from agent_core.prompt_injection import sanitize_untrusted_text

# ---------- 探勘實測校正的常數 ----------
_DEFAULT_WINDOW_D = 63    # 預警窗 = 交期 − p75(交期−首次入站)；lead time p50=33
_TRUST_FILL = 0.70        # 料類可信門檻：完工單(2025+)發料達成率 ≥ 0.70
_MIN_NEED = 1.0           # 微量列門檻（低於此需求量的料項不列入判定）
_NO_BOM_CUSTOMERS = ("Khai Hoan 3",)   # 結構性無 BOM 客戶（探勘②：39 張單 0 張有）
_EXCLUDE_WAREHOUSES = ("SW",)          # 廢料倉不算可用庫存
_QUERY_DEADLINE_S = 60.0
_MAX_CELL = 40


# ---------- DB（同 skills/erp_warehouse.py 防線：唯讀 + 禁外部檔 + deadline） ----------
def _db_path():
    from agent_core.logging_and_paths import DATA_DIR
    return os.path.join(DATA_DIR, "erp_mirror", "erp_full.duckdb")


def _db_ready():
    return os.path.exists(_db_path())


def _connect_ro():
    import duckdb
    return duckdb.connect(_db_path(), read_only=True,
                          config={"enable_external_access": "false"})


def _q(con, sql):
    """單查詢帶 deadline（跑飛的查詢會被 interrupt）。"""
    timer = threading.Timer(_QUERY_DEADLINE_S, con.interrupt)
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()


def _today() -> datetime.date:
    return datetime.date.today()


# ---------- 快照載入 ----------
def _load_snapshot(today: datetime.date):
    """一次連線把齊套計算要的資料全撈回 Python（都是小結果集）。

    today 下推進 SQL 做在途 ETA 分桶（future/overdue）——行級聚合要在 SQL 端先按
    (單號, 料號) 收斂，否則同 PO 多項次會把 MRP 配額乘上行數（重複計數）。
    """
    today_s = today.isoformat()
    con = _connect_ro()
    try:
        snap = {}
        # 生效訂單 + 已上線/工單/派工放行訊號
        snap["orders"] = _q(con, """
            WITH started AS (
              SELECT DISTINCT 訂單號 FROM v_production WHERE 異動類型='入站'
            ), wo AS (
              SELECT DISTINCT 訂單號 FROM v_work_orders
            ), disp AS (
              SELECT DISTINCT w.訂單號
              FROM v_work_orders w JOIN v_dispatch d ON d.工單號 = w.工單號
            )
            SELECT o.單號, MIN(o.客戶), MIN(o.客戶PO), MIN(o.鞋款),
                   SUM(COALESCE(o.數量, 0)),
                   COALESCE(MIN(NULLIF(substr(o.交期, 1, 10), '')), '') AS 交期日,
                   bool_or(s.訂單號 IS NOT NULL) AS 已上線,
                   bool_or(w.訂單號 IS NOT NULL) AS 有工單,
                   bool_or(p.訂單號 IS NOT NULL) AS 有派工
            FROM v_orders o
            LEFT JOIN started s ON s.訂單號 = o.單號
            LEFT JOIN wo w ON w.訂單號 = o.單號
            LEFT JOIN disp p ON p.訂單號 = o.單號
            WHERE o.狀態 = '生效'
            GROUP BY o.單號   -- v_orders 是 (單號,訂單序) 級：多 SEQ 同時生效要聚合
        """)
        # 生效單逐料需求/進度（視圖量欄已是 DOUBLE；NULL 一律補 0）
        snap["materials"] = _q(con, """
            SELECT m.單號, m.料號, COALESCE(m.材料類型, ''), COALESCE(m.品名描述, ''),
                   COALESCE(m.需求, 0), COALESCE(m.訂購, 0),
                   COALESCE(m.點收, 0), COALESCE(m.發料, 0)
            FROM v_order_materials m
            WHERE m.單號 IN (SELECT 單號 FROM v_orders WHERE 狀態 = '生效')
              AND COALESCE(m.需求, 0) > 0
        """)
        # 在途/已收：MRP 配額 × (採購單 ∪ 委外加工單)。行級先在 SQL 按 (單號, 料號)
        # 聚合成 future/overdue 兩桶（同 PO 多項次不聚合會把配額乘上行數 = 重複計數）；
        # alloc 分母必須是「全訂單」的配額總和（只算生效單會把分攤比例灌高）。
        snap["transit_lines"] = _q(con, f"""
            WITH alloc AS (
              SELECT ORDER_NO, ITEM_NO,
                     SUM(COALESCE(TRY_CAST(ORDER_QTY AS DOUBLE), 0)) AS tot
              FROM SC00__PO_MRP_PO GROUP BY 1, 2
            ), mrp AS (
              SELECT SE_ID, ITEM_NO, ORDER_NO,
                     SUM(COALESCE(TRY_CAST(ORDER_QTY AS DOUBLE), 0)) AS q
              FROM SC00__PO_MRP_PO
              WHERE SE_ID IN (SELECT 單號 FROM v_orders WHERE 狀態 = '生效')
              GROUP BY 1, 2, 3
            ), lines AS (
              SELECT 採購單號 AS ORDER_NO, 料號 AS ITEM_NO,
                     GREATEST(COALESCE(訂購數量, 0) - COALESCE(已收數量, 0), 0) AS open_qty,
                     COALESCE(已收數量, 0) AS rcpt_qty,
                     NULLIF(substr(計劃到貨日, 1, 10), '') AS eta,
                     CASE WHEN 明細狀態 IN ('生效', '新建') THEN 1 ELSE 0 END AS is_open
              FROM v_purchase_orders
              UNION ALL
              SELECT PROC_NO, ITEM_NO,
                     GREATEST(COALESCE(TRY_CAST(PROC_QTY AS DOUBLE), 0)
                              - COALESCE(TRY_CAST(RCPT_QTY AS DOUBLE), 0), 0),
                     COALESCE(TRY_CAST(RCPT_QTY AS DOUBLE), 0),
                     NULLIF(substr(PLAN_DATE, 1, 10), ''),
                     CASE WHEN STATUS IN ('0', '7') THEN 1 ELSE 0 END
              FROM SC00__PO_PROC_D
            ), lines_agg AS (
              SELECT ORDER_NO, ITEM_NO,
                     SUM(CASE WHEN is_open = 1 AND eta IS NOT NULL AND eta >= '{today_s}'
                              THEN open_qty ELSE 0 END) AS open_future,
                     SUM(CASE WHEN is_open = 1 AND (eta IS NULL OR eta < '{today_s}')
                              THEN open_qty ELSE 0 END) AS open_overdue,
                     SUM(rcpt_qty) AS rcpt_qty,
                     MIN(CASE WHEN is_open = 1 AND open_qty > 0 AND eta >= '{today_s}'
                              THEN eta END) AS eta_min
              FROM lines GROUP BY 1, 2
            )
            SELECT m.SE_ID, m.ITEM_NO, m.q, a.tot,
                   COALESCE(l.open_future, 0), COALESCE(l.open_overdue, 0),
                   COALESCE(l.rcpt_qty, 0), l.eta_min,
                   (l.ORDER_NO IS NULL) AS orphan
            FROM mrp m
            JOIN alloc a ON a.ORDER_NO = m.ORDER_NO AND a.ITEM_NO = m.ITEM_NO
            LEFT JOIN lines_agg l
              ON l.ORDER_NO = m.ORDER_NO AND l.ITEM_NO = m.ITEM_NO
            -- LEFT JOIN：MRP 有配額但單據不在鏡像（如委外單同步落後）時不可
            -- 靜默丟列——孤兒配額歸「在途但單據不可考」催辦類（Python 端處理）
        """)
        # 可用庫存（排除廢料倉）＝**0 批**（2026-07-29 UserA 案：M01 批要倉庫
        # 實地確認調撥到 0 批、預購批 J0C… 綁單——全批加總會把不可挪用的量
        # 當可補）。v_stock_lot 缺失（舊鏡像）退回全批口徑。
        wh = ", ".join(f"'{w}'" for w in _EXCLUDE_WAREHOUSES)
        try:
            snap["stock"] = _q(con, f"""
                SELECT 料號, SUM(COALESCE(結存數量, 0))
                FROM v_stock_lot
                WHERE COALESCE(倉庫代碼, '') NOT IN ({wh})
                  AND TRIM(COALESCE(批號, '')) IN ('0', '')
                GROUP BY 1
            """)
            snap["stock_pending"] = _q(con, f"""
                SELECT 料號, SUM(COALESCE(結存數量, 0))
                FROM v_stock_lot
                WHERE COALESCE(倉庫代碼, '') NOT IN ({wh})
                  AND TRIM(COALESCE(批號, '')) NOT IN ('0', '')
                GROUP BY 1
            """)
        except Exception:  # noqa: BLE001 - 批次是口徑修正，舊鏡像退回全批
            snap["stock"] = _q(con, f"""
                SELECT 料號, SUM(COALESCE(結存數量, 0))
                FROM v_stock
                WHERE COALESCE(倉庫代碼, '') NOT IN ({wh})
                GROUP BY 1
            """)
            snap["stock_pending"] = []
        # 料號足跡集合（含排除倉）：判「無追蹤資料」要看全倉，扣抵才用上面的過濾版
        snap["stock_items"] = _q(con, "SELECT DISTINCT 料號 FROM v_stock")
        # 單位不一致料號（庫存不可直接扣抵，標「單位待換算」）
        snap["unit_mismatch"] = _q(con, """
            SELECT DISTINCT s.料號
            FROM (SELECT DISTINCT 料號, COALESCE(單位, '') AS u FROM v_stock) s
            JOIN (SELECT DISTINCT 料號, COALESCE(採購單位, '') AS u
                  FROM v_purchase_orders) p
              ON p.料號 = s.料號 AND p.u <> s.u
        """)
        # 客供料（免採購，不報缺料）
        snap["pr_items"] = _q(con, """
            SELECT DISTINCT SE_ID, ITEM_NO FROM SC00__SE_ITEMSCHE_M
            WHERE PR_TYPE = '13' OR IS_PR = 'N'
        """)
        # 料類可信度動態校準：2025+ 完工/出货/销货 訂單的發料達成率
        snap["cat_fill"] = _q(con, """
            WITH done AS (
              SELECT DISTINCT 單號 FROM v_orders
              WHERE 狀態 IN ('完工', '出货', '销货') AND substr(單據日, 1, 4) >= '2025'
            )
            SELECT COALESCE(m.材料類型, ''),
                   SUM(LEAST(COALESCE(m.發料, 0), m.需求)) / NULLIF(SUM(m.需求), 0)
            FROM v_order_materials m JOIN done d ON d.單號 = m.單號
            WHERE COALESCE(m.需求, 0) > 0
            GROUP BY 1
        """)
        return snap
    finally:
        con.close()


# ---------- 齊套引擎（純 Python，可測） ----------
def _counts_as_demand(it, trusted) -> bool:
    """這列的缺口算不算「會真的吃掉庫存的需求」。

    _analyze 的 FCFS 扣抵與 material_capacity_by_style 的「已被在手單佔用」共用同
    一道門，兩邊各寫一次遲早會漂移（同一批料，齊套說有、產能說沒有）。跳過的四類：
    客供（不用我們的料）／無追蹤（帳外管理料，數字不可比）／單位待換算（庫存與需求
    不同單位，相減無意義）／不可信料類且零到料訊號（分不出沒到還是沒登）。
    """
    if it["gap_after_transit"] <= 0:
        return False
    if it["客供"] or it["無追蹤"] or it["單位待換算"]:
        return False
    if it["料類"] not in trusted and it["已到"] <= 1e-6:
        return False
    return True


def _analyze(snap, today: datetime.date, *, min_need: float = _MIN_NEED):
    """回傳 {"orders": {單號: 分析結果}, "conflicts": [...], "excluded": [...],
    "zombies": [...], "trusted_cats": set}。

    分析結果每張單含 items（逐料判定）與 buckets（各判定計數）。判定值：
      齊 / 在途 / 庫存可補 / 在途逾期 / 缺料無來源 / 無法判定 / 客供 / 單位待換算
    """
    today_s = today.isoformat()
    trusted = {cat for cat, fill in snap["cat_fill"]
               if fill is not None and fill >= _TRUST_FILL}

    # 在途/已收 per (單號, 料號)：配額比例分攤；rcpt+open 合計 cap 至配額
    #（分開 cap 會讓同條 PO 的「已到＋在途」重複計到 2 倍配額）
    transit = {}
    for se_id, item, quota, tot, open_future, open_overdue, rcpt_qty, eta_min, orphan in \
            snap["transit_lines"]:
        t = transit.setdefault((se_id, item),
                               {"open_future": 0.0, "open_overdue": 0.0,
                                "rcpt": 0.0, "eta_min": None})
        if orphan:
            # MRP 有配額但採購/委外單不在鏡像（同步落後）：在途但單據不可考 → 催辦類
            t["open_overdue"] += quota
            continue
        frac = min(quota / tot, 1.0) if tot > 0 else 0.0
        rshare = min(rcpt_qty * frac, quota)
        cap_open = max(quota - rshare, 0.0)
        fut, ovd = open_future * frac, open_overdue * frac
        if fut + ovd > cap_open:
            scale = cap_open / (fut + ovd) if fut + ovd > 0 else 0.0
            fut, ovd = fut * scale, ovd * scale
        t["rcpt"] += rshare
        t["open_future"] += fut
        t["open_overdue"] += ovd   # ETA 已過或空白：在途但不可信會到 → 催辦類
        if fut > 0 and eta_min and (t["eta_min"] is None or eta_min < t["eta_min"]):
            t["eta_min"] = eta_min

    stock = {item: qty for item, qty in snap["stock"]}
    pending = {item: qty for item, qty in snap.get("stock_pending", [])}
    known_items = {r[0] for r in snap.get("stock_items", [])} | set(stock)
    unit_bad = {r[0] for r in snap["unit_mismatch"]}
    pr_items = {(r[0], r[1]) for r in snap["pr_items"]}

    orders, zombies, excluded = {}, [], []
    for (se_id, cust, cust_po, style, qty, due, started, has_wo, has_disp) in snap["orders"]:
        rec = {"單號": se_id, "客戶": cust or "", "客戶PO": cust_po or "",
               "鞋款": style or "", "數量": qty, "交期": due,
               "已上線": bool(started), "已放行": bool(has_wo and has_disp),
               "items": [], "buckets": {}}
        if (cust or "") in _NO_BOM_CUSTOMERS:
            excluded.append(rec)
        elif due and due < today_s:
            zombies.append(rec)
        orders[se_id] = rec

    zombie_ids = {r["單號"] for r in zombies}
    excluded_ids = {r["單號"] for r in excluded}

    # 逐料第一輪：算 arrived / gap / 在途覆蓋（庫存留給 FCFS 第二輪）
    mat_rows = []   # (單號, item dict) 保序
    for se_id, item, cat, name, need, ordered, checked, issued in snap["materials"]:
        if se_id not in orders or need < min_need:
            continue
        t = transit.get((se_id, item),
                        {"open_future": 0.0, "open_overdue": 0.0, "rcpt": 0.0, "eta_min": None})
        arrived = min(need, max(checked, issued, t["rcpt"]))
        gap0 = need - arrived
        cover_transit = min(gap0, t["open_future"]) if gap0 > 0 else 0.0
        it = {"料號": item, "料類": cat, "品名": name, "需求": need,
              "已到": arrived, "在途": t["open_future"], "在途逾期": t["open_overdue"],
              "ETA": t["eta_min"], "訂購": ordered,
              "gap_after_transit": max(gap0 - cover_transit, 0.0),
              "庫存可補": 0.0, "缺": 0.0, "判定": None,
              "待確認": pending.get(item, 0.0),   # M01/預購批（不可挪用，僅資訊）
              "零批": stock.get(item, 0.0),       # 可用0批毛量（採購面判定參考）
              "客供": (se_id, item) in pr_items,
              "單位待換算": item in unit_bad,
              # 全系統零足跡（庫存表無行、本單無 MRP 配額、零到料）＝帳外管理料
              # （實測桶裝化工料如此），標「無追蹤資料」而非缺料——探勘⑤的 8%。
              "無追蹤": (item not in known_items and (se_id, item) not in transit
                        and arrived <= 1e-6)}
        mat_rows.append((se_id, it))
        orders[se_id]["items"].append(it)

    # FCFS-by-交期 跨單庫存模擬扣減：pool = 非殭屍、非排除客戶的生效單
    # （殭屍單需求佔 35%，不分層會把未來衝突分母灌爆——它們另列催辦，不搶庫存）。
    # 衝突分子與庫存扣抵走同一道 skip 門：客供／無追蹤／單位待換算（數字跨表不可比）
    # ／將判「無法判定」（不可信料類零訊號）都不入 demand、不搶庫存——
    # 否則衝突表會混入不可比或幻影缺口（審查實測 36% 衝突列如此）。
    stock_left = dict(stock)
    demand_by_item = {}
    impact_by_item = {}
    pool = sorted((r for r in mat_rows
                   if r[0] not in zombie_ids and r[0] not in excluded_ids),
                  key=lambda r: (orders[r[0]]["交期"] or "9999-99-99", r[0]))
    for se_id, it in pool:
        g = it["gap_after_transit"]
        if not _counts_as_demand(it, trusted):
            continue
        demand_by_item[it["料號"]] = demand_by_item.get(it["料號"], 0.0) + g
        impact_by_item[it["料號"]] = impact_by_item.get(it["料號"], 0) + 1
        if it["料號"] not in stock_left:
            continue
        alloc = min(g, max(stock_left[it["料號"]], 0.0))
        if alloc > 0:
            stock_left[it["料號"]] -= alloc
            it["庫存可補"] = alloc

    # 共用料衝突：pool 內總需求（未到未在途、可比單位）> 可用庫存 的料號
    conflicts = []
    for item, dem in demand_by_item.items():
        avail = stock.get(item, 0.0)
        if dem > avail + 1e-6:
            conflicts.append({"料號": item, "總缺口": dem, "庫存": avail,
                              "影響單數": impact_by_item.get(item, 0)})
    conflicts.sort(key=lambda c: c["總缺口"] - c["庫存"], reverse=True)

    # 逐料最終判定
    for se_id, it in mat_rows:
        short = it["gap_after_transit"] - it["庫存可補"]
        it["缺"] = max(short, 0.0)
        if it["客供"]:
            it["判定"] = "客供"
        elif it["需求"] - it["已到"] <= 1e-6:
            it["判定"] = "齊"
        elif short <= 1e-6:
            it["判定"] = "在途" if it["gap_after_transit"] < it["需求"] - it["已到"] else "庫存可補"
            if it["庫存可補"] > 0 and it["在途"] > 0:
                it["判定"] = "在途+庫存"
        elif short < _MIN_NEED:
            # 尾差殘渣（損耗率小數），不當缺料。門檻固定用常數：min_need 參數只
            # 濾「需求微量列」，不可兼任尾差容忍（調大會把真缺口靜默標齊）。
            it["判定"] = "齊"
            it["缺"] = 0.0
        elif it["在途逾期"] > 1e-6:
            it["判定"] = "在途逾期"
        elif it["料類"] not in trusted and it["已到"] <= 1e-6:
            # 不可信料類 + 零到料訊號：分不出「沒到」還是「沒登」——誠實標無法判定。
            # 不看「訂購」：化工料常有 MRP 訂購紀錄但收貨/點收從不維護，
            # 看訂購會把它們誤判成缺料。
            it["判定"] = "無法判定"
        elif it["無追蹤"]:
            it["判定"] = "無追蹤資料"
        elif it["單位待換算"] and it["料號"] in stock and stock[it["料號"]] > 0:
            # 庫存有貨但單位 ≠ 採購單位：數字不可驗（資訊層，非行動項）
            it["判定"] = "單位待換算"
        elif ((se_id in zombie_ids or se_id in excluded_ids)
                and stock.get(it["料號"], 0.0) >= it["gap_after_transit"] - 1e-6):
            # 殭屍/排除單不入 FCFS（不搶生效單庫存），庫存可補恆 0——但庫存
            # 毛量其實蓋得住時標「缺料無來源」是假紅（審查實測 833/981 料項如此）。
            # 非佔用式比對：只示意有貨、不承諾保留。
            it["判定"] = "庫存或可補"
        else:
            it["判定"] = "缺料無來源"

    for rec in orders.values():
        b = {}
        for it in rec["items"]:
            b[it["判定"]] = b.get(it["判定"], 0) + 1
        rec["buckets"] = b

    return {"orders": orders, "conflicts": conflicts, "excluded": excluded,
            "zombies": zombies, "trusted_cats": trusted}


# ---------- 形體剩餘產能（「這些料還能做幾雙」——接新單用） ----------
#
# 2026-08-05 生管主管（生產管理部主管）需求：「看迪卡儂剩下的材料、每個形體還能做多少
# 雙，有助於接新單」。與齊套預警是**反向**的問題：齊套問「這張單的料齊不齊」，這裡
# 問「扣掉在手單之後，剩的料還能開幾雙新的」。共用同一份快照與同一道需求門
# （_counts_as_demand），兩份報表的數字才不會互相矛盾。
#
# 口徑（探勘實測，2026-08-05）：
#   - 可用池 = 0 批 + M01 批（排除廢料倉、每列負值歸零）。**不能只吃 0 批**：實測
#     迪卡儂主要料號的 0 批幾乎全為 0、量都壓在 M01（織標 0批 4,908 vs M01 75,852、
#     熱熔膠 0批 0 vs M01 16,334），只算 0 批會讓每個形體都回「0 雙」= 報表無用。
#     M01 要倉庫實地確認調撥才能領，所以標註在報表上、不是靜默併入。
#     預購批 J0C… 綁單不可挪用，一律不計（且實測淨額為負，本就是配帳殘影）。
#   - 每雙用量 = 近 N 個月同形體訂單的 Σ需求 / Σ雙數（只算「有出現這支料」的單）。
#     ERP 沒有形體級 BOM 表可查，訂單用料（v_order_materials.需求）是唯一權威來源。
#   - 只採「覆蓋率 ≥ _CAP_MIN_COV」的料（該形體近期單有一半以上都用到）：另外那些
#     只出現在 1、2 張單的多是尺寸/配色變體，拿它的每雙用量套到新單會高估需求。
#   - 單位待換算／庫存表查無 的料一律不列入判定（數字不可比），但要報數量——
#     報表只蓋得住可判定的那部分，講清楚比給一個看似精確的數字重要。
_CAP_MIN_COV = 0.5          # 形體用料覆蓋率門檻
_CAP_BOM_MONTHS = 12        # 每雙用量取樣窗（月）
_CAP_MIN_PER = 1e-9         # 每雙用量下限（避免除以 0）
_CAP_RATE_DAYS = 28         # 「還夠做幾週」的產出速度取樣窗（天）
_BRIEF_CHART_DAYS = 21      # 每日簡報的產能折線圖天數（跨月往前補滿）


def _load_capacity_extras(customer: str, since: str):
    """可用庫存池（0批+M01）與該客戶近期形體用料 —— 一次連線撈完。

    回 {"pool": [(料號, 量)], "bom": [(鞋款, 料號, 品名, 材料類型, Σ需求, Σ雙數,
    出現單數, 該形體總單數)]}。
    """
    con = _connect_ro()
    try:
        wh = ", ".join(f"'{w}'" for w in _EXCLUDE_WAREHOUSES)
        try:
            pool = _q(con, f"""
                SELECT 料號, SUM(GREATEST(COALESCE(結存數量, 0), 0))
                FROM v_stock_lot
                WHERE COALESCE(倉庫代碼, '') NOT IN ({wh})
                  AND (TRIM(COALESCE(批號, '')) IN ('0', '') OR 批號 LIKE 'M%')
                GROUP BY 1
            """)
        except Exception:  # noqa: BLE001 - 舊鏡像沒有 v_stock_lot 就退回全批
            pool = _q(con, f"""
                SELECT 料號, SUM(GREATEST(COALESCE(結存數量, 0), 0))
                FROM v_stock
                WHERE COALESCE(倉庫代碼, '') NOT IN ({wh})
                GROUP BY 1
            """)
        # 取樣窗內同形體的訂單用料。取消單不算（沒生產過、用量不具代表性）；
        # 已完工/銷貨的單要算 —— 那才是這個形體真正做出來的用量。
        bom = con.execute("""
            WITH win AS (
              SELECT 單號, 鞋款, 數量 FROM v_orders
              WHERE 客戶 = ? AND 狀態 <> '取消' AND COALESCE(數量, 0) > 0
                AND COALESCE(鞋款, '') <> '' AND substr(單據日, 1, 10) >= ?
            )
            SELECT w.鞋款, m.料號, ANY_VALUE(m.品名描述), ANY_VALUE(m.材料類型),
                   SUM(m.需求), SUM(w.數量), COUNT(*),
                   (SELECT COUNT(*) FROM win w2 WHERE w2.鞋款 = w.鞋款)
            FROM win w JOIN v_order_materials m ON m.單號 = w.單號
            WHERE COALESCE(m.需求, 0) > 0
            GROUP BY w.鞋款, m.料號
        """, [customer, since]).fetchall()
        # 見底料的採購面：有沒有開單、跟誰買、欠多少、ETA。生管主管 2026-08-06 要求
        # ——「見底」只講「沒料」等於把人推去問採購，補這段才是可以直接行動的資訊。
        try:
            po = _q(con, """
                SELECT 料號, COALESCE(供應商簡稱, ''),
                       SUM(CASE WHEN 明細狀態 IN ('生效', '新建')
                                THEN GREATEST(COALESCE(訂購數量, 0)
                                              - COALESCE(已收數量, 0), 0)
                                ELSE 0 END),
                       MIN(CASE WHEN 明細狀態 IN ('生效', '新建')
                                 AND COALESCE(訂購數量, 0) > COALESCE(已收數量, 0)
                                THEN NULLIF(substr(計劃到貨日, 1, 10), '') END),
                       MAX(NULLIF(substr(下單日期, 1, 10), ''))
                FROM v_purchase_orders
                GROUP BY 1, 2
            """)
        except Exception:  # noqa: BLE001 - 舊鏡像少欄位：報表照出，只是沒有採購欄
            po = []
        return {"pool": pool, "bom": bom, "po": po}
    finally:
        con.close()


def _purchase_status(po_rows):
    """v_purchase_orders 彙總列 → {料號: {"已開單": bool, "供應商": str, "在途": float,
    "ETA": str, "最後下單": str}}。

    有未收完的生效/新建單就算「已開單」（供應商取在途量最大的那家）；否則回最後一次
    下單的供應商與日期 —— 「上次跟誰買、多久以前」才是採購接手時要的第一句話。
    """
    by_item: dict = {}
    for item, supplier, open_qty, eta, last_po in po_rows:
        rec = by_item.setdefault(item, {"已開單": False, "供應商": "", "在途": 0.0,
                                        "ETA": "", "最後下單": "", "_best_open": 0.0})
        open_qty = float(open_qty or 0.0)
        if open_qty > 0:
            rec["已開單"] = True
            rec["在途"] += open_qty
            if open_qty > rec["_best_open"]:
                rec["_best_open"] = open_qty
                rec["供應商"] = supplier or rec["供應商"]
            if eta and (not rec["ETA"] or eta < rec["ETA"]):
                rec["ETA"] = eta
        if last_po and last_po > rec["最後下單"]:
            rec["最後下單"] = last_po
            if not rec["已開單"]:
                rec["供應商"] = supplier or rec["供應商"]
    for rec in by_item.values():
        rec.pop("_best_open", None)
    return by_item


def _committed_by_item(res) -> dict:
    """在手生效單還會吃掉的料 → {料號: 量}。

    含逾期（殭屍）單：它們在 ERP 裡仍是生效單，料還壓在上面。齊套表把它們排除在
    FCFS 之外是為了不讓未來衝突的分母被灌爆；但問「還能接多少新單」時把它們當成
    不存在，就是把別人的料拿去承諾新單。排除的只有結構性無 BOM 的客戶。
    """
    trusted = res["trusted_cats"]
    excluded_ids = {r["單號"] for r in res["excluded"]}
    out: dict = {}
    for se_id, rec in res["orders"].items():
        if se_id in excluded_ids:
            continue
        for it in rec["items"]:
            if not _counts_as_demand(it, trusted):
                continue
            out[it["料號"]] = out.get(it["料號"], 0.0) + it["gap_after_transit"]
    return out


def _capacity_by_style(snap, res, extras, *, min_cov: float = _CAP_MIN_COV,
                       weekly_rates: dict | None = None):
    """各形體「剩餘材料還能做幾雙」→ [dict]，依淨可做雙數升冪（最緊的在前）。

    每個形體回：可判定料的 淨/毛 可做雙數、瓶頸料排序、見底料清單（含採購狀態）、
    以及沒能判定的料數（單位待換算／庫存表查無／覆蓋率不足）—— 覆蓋說明是報表的
    一部分，不是註腳。weekly_rates 給定時（{鞋款: 近期週產雙數}）另算「還夠做幾週」。
    """
    pool = {item: qty for item, qty in extras["pool"]}
    committed = _committed_by_item(res)
    po_status = _purchase_status(extras.get("po") or [])
    weekly_rates = weekly_rates or {}
    unit_bad = {r[0] for r in snap["unit_mismatch"]}
    known = {r[0] for r in snap.get("stock_items", [])} | set(pool)

    styles: dict = {}
    for style, item, name, cat, need, pairs, n_ord, tot_ord in extras["bom"]:
        rec = styles.setdefault(str(style), {"鞋款": str(style), "取樣單數": int(tot_ord or 0),
                                             "items": []})
        per = (need or 0.0) / pairs if pairs else 0.0
        rec["items"].append({
            "料號": item, "品名": name or "", "料類": cat or "",
            "每雙": per, "覆蓋": (n_ord / tot_ord) if tot_ord else 0.0,
        })

    out = []
    for style, rec in styles.items():
        core = [it for it in rec["items"]
                if it["覆蓋"] >= min_cov and it["每雙"] > _CAP_MIN_PER]
        judged, skip_unit, skip_unknown = [], 0, 0
        for it in core:
            if it["料號"] in unit_bad:
                skip_unit += 1
                continue
            if it["料號"] not in known:
                skip_unknown += 1
                continue
            gross = max(pool.get(it["料號"], 0.0), 0.0)
            used = min(committed.get(it["料號"], 0.0), gross)
            net = max(gross - used, 0.0)
            judged.append({**it, "庫存": gross, "在手單佔用": used, "可用": net,
                           "淨可做": net / it["每雙"], "毛可做": gross / it["每雙"],
                           "採購": po_status.get(it["料號"])})
        judged.sort(key=lambda x: x["淨可做"])
        empty = [x for x in judged if x["淨可做"] < 1]
        have = [x for x in judged if x["淨可做"] >= 1]
        net_pairs = judged[0]["淨可做"] if judged else 0.0
        weekly = weekly_rates.get(style, 0.0)
        out.append({
            "鞋款": style,
            "週產": weekly,
            # 「還夠做幾週」＝淨可做 ÷ 近期實際週產。近期沒生產（週產 0）就是 None，
            # 顯示「無法換算」——除以一個猜的速度只會給出看似精確的假答案。
            "可做週數": (net_pairs / weekly) if weekly > 0 else None,
            "取樣單數": rec["取樣單數"],
            "料項": len(rec["items"]),
            "核心料": len(core),
            "可判定": len(judged),
            "單位待換算": skip_unit,
            "庫存表查無": skip_unknown,
            "低覆蓋": len(rec["items"]) - len(core),
            "淨可做": net_pairs,
            "毛可做": min((x["毛可做"] for x in judged), default=0.0),
            "見底": empty,
            "瓶頸": have[:5],
            "全部": judged,
        })
    out.sort(key=lambda r: (r["淨可做"], r["鞋款"]))
    return out


def _order_light(rec) -> str:
    """單張訂單燈號：🔴 有缺料無來源 > 🟠 在途逾期 > 🟡 資訊不足佔比>30% > ✅。

    單位待換算歸資訊層（庫存有貨、只是數字不可驗）——當 🟠 會讓 31% 料號單位不一致
    的現實把每張單都染橘（警報疲勞）。"""
    b = rec["buckets"]
    n = max(len(rec["items"]), 1)
    if b.get("缺料無來源"):
        return "🔴"
    if b.get("在途逾期"):
        return "🟠"
    if (b.get("無法判定", 0) + b.get("單位待換算", 0)
            + b.get("無追蹤資料", 0)) / n > 0.30:
        return "🟡"
    return "✅"


# ---------- 輸出（Telegram code block；CJK 顯示寬度對齊） ----------
def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _wpad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _cell(v) -> str:
    if v is None:
        return ""
    s = sanitize_untrusted_text(v).strip() if isinstance(v, str) else str(v)
    s = s.replace("\n", " ").replace("\t", " ")
    return s[: _MAX_CELL - 1] + "…" if len(s) > _MAX_CELL else s


def _table(cols, rows) -> str:
    body = [[_cell(v) for v in r] for r in rows]
    widths = [_disp_width(str(c)) for c in cols]
    for r in body:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], _disp_width(c))

    def fmt(r):
        return "  ".join(_wpad(str(c), widths[i]) if i < len(widths) else str(c)
                         for i, c in enumerate(r))

    return "```\n" + "\n".join([fmt(cols)] + [fmt(r) for r in body]) + "\n```"


def _n(v: float) -> str:
    return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.1f}"


def _shortage_rows(rec, top: int):
    """一張單的缺料明細列（缺最大排前）。單位待換算不進表（庫存有貨，數字會誤導）。"""
    bad = [it for it in rec["items"] if it["判定"] in ("缺料無來源", "在途逾期")]
    bad.sort(key=lambda x: x["缺"], reverse=True)
    rows = []
    for it in bad[:top]:
        note = {"缺料無來源": "🔴無來源", "在途逾期": "🟠在途逾期"}[it["判定"]]
        rows.append([it["料號"], it["品名"], _n(it["需求"]), _n(it["已到"]),
                     _n(it["在途"] + it["在途逾期"]), it["ETA"] or "-", _n(it["缺"]), note])
    return rows


_JUDGE_ICON = {"缺料無來源": "🔴缺料無來源", "在途逾期": "🟠在途逾期",
               "單位待換算": "🟡單位待換算", "無法判定": "❓無法判定",
               "無追蹤資料": "❓無追蹤資料", "客供": "客供"}


def _render_order_item_focus(rec, terms: set, item_label: str, top: int) -> str:
    """單張訂單×指定料的聚焦視圖：命中料**不分桶全列**（齊/可補/待換算也給判定）。

    起因：2026-07-29 UserA SF24 案——問「JFC26490/26491 SF24 短少數量」，該料
    落在缺料表 top 之外/非缺料桶就看不到，freeform 拿舊單採購已到貨腦補成
    「短少 0」。聚焦視圖讓「這張單×這個料」的 需求/已到/在途/缺/判定 一眼可對。
    """
    head = (f"{_order_light(rec)} {_cell(rec['單號'])}  {_cell(rec['客戶'])}  "
            f"{_cell(rec['鞋款'])}  {_n(rec['數量'])}雙  交期 {_cell(rec['交期']) or '?'}")
    matched = [it for it in rec["items"]
               if any(t in it["料號"].lower() or t in (it["品名"] or "").lower()
                      for t in terms)]
    if not matched:
        return (head + f"\n   （此單無符合「{_cell(item_label)}」的料項——"
                "BOM 未用此料、或需求低於門檻/客供）")
    matched.sort(key=lambda x: x["缺"], reverse=True)
    shown = matched[:max(1, top)]
    rows = [[it["料號"], it["品名"], _n(it["需求"]), _n(it["訂購"]), _n(it["已到"]),
             _n(it["在途"] + it["在途逾期"]), it["ETA"] or "-", _n(it["缺"]),
             _n(it.get("待確認", 0.0)),
             _JUDGE_ICON.get(it["判定"], "✅" + it["判定"])]
            for it in shown]
    lines = [head, _table(
        ["料號", "品名", "需求", "訂購", "已到", "在途", "ETA", "缺", "待確認批", "判定"],
        rows)]
    # 採購面判死（2026-07-29 UserA 案二）：缺料且 訂購＜需求 ＝ 本單根本沒開(足)
    # ERP 採購單——不明講，freeform 會拿「舊單 PO 全數已收」腦補成「採購面無短少」
    #（採購配額綁單，舊單到貨≠本單有採購來源）。需求/訂購同表同單位恆可比，所以
    # 「單位待換算」也要判（實案 SF24 就是 stock 'M' vs 採購 '59/M2' 落這桶）；
    # 差別在：可比者待確認批蓋得住缺口就不催採購（等調撥即可、交給 🟡 池判定），
    # 單位不可比者無法自動核銷、明講要人工核對。
    for it in shown:
        if it["訂購"] + 1e-6 >= it["需求"] or it["客供"]:
            continue
        if (it["判定"] == "缺料無來源"
                and it["缺"] > it.get("待確認", 0.0) + 1e-6):
            lines.append(f"   ⛔ 採購面：{_cell(it['料號'])} 本單 ERP 訂購 "
                         f"{_n(it['訂購'])} ＜ 需求 {_n(it['需求'])}——未開(足) ERP "
                         "採購單，採購面即判缺料（ERP 外手工下單 ERP 看不到，請補登）。")
        elif it["判定"] == "單位待換算":
            lines.append(f"   ⛔ 採購面：{_cell(it['料號'])} 本單 ERP 訂購 "
                         f"{_n(it['訂購'])} ＜ 需求 {_n(it['需求'])}（同表同單位可直接比）"
                         "——未開(足) ERP 採購單，採購面即判缺料；廠內結存單位與採購"
                         "單位不一致，可否調撥抵扣需倉庫人工核對。")
    if any(r[8] != "0" for r in rows):
        lines.append("   ℹ️ 待確認批＝M01/預購批（要倉庫實地確認調撥到 0 批才可用，"
                     "不計入可補/缺口；同料號各單顯示的是**同一池**，跨單不可加總）。")
    return "\n".join(lines)


def _render_order(rec, top: int) -> str:
    if not rec["items"] and rec["客戶"] in _NO_BOM_CUSTOMERS:
        return (f"❓ {_cell(rec['單號'])}  {_cell(rec['客戶'])}  交期 "
                f"{_cell(rec['交期']) or '?'} —— 結構性無 BOM 客戶，"
                "ERP 無備料資料可判齊套。")
    b = rec["buckets"]
    lines = [f"{_order_light(rec)} {_cell(rec['單號'])}  {_cell(rec['客戶'])}  "
             f"{_cell(rec['鞋款'])}  {_n(rec['數量'])}雙  交期 {_cell(rec['交期']) or '?'}"
             f"{'  [已上線]' if rec['已上線'] else ''}"
             f"{'  [已放行]' if rec['已放行'] and not rec['已上線'] else ''}"]
    order = ["齊", "在途", "庫存可補", "在途+庫存", "庫存或可補", "在途逾期",
             "單位待換算", "缺料無來源", "無法判定", "無追蹤資料", "客供"]
    parts = [f"{k}{b[k]}" for k in order if b.get(k)]
    lines.append("   料項判定：" + (" / ".join(parts) if parts else "（無 ≥門檻 的料項）"))
    rows = _shortage_rows(rec, top)
    if rows:
        lines.append(_table(["料號", "品名", "需求", "已到", "在途", "ETA", "缺", "狀態"], rows))
    info = []
    if b.get("無法判定"):
        info.append(f"{b['無法判定']} 項不可信料類零訊號無法判定")
    if b.get("單位待換算"):
        info.append(f"{b['單位待換算']} 項庫存有貨但單位待換算")
    if b.get("無追蹤資料"):
        info.append(f"{b['無追蹤資料']} 項全系統零足跡（帳外管理料）")
    if info:
        lines.append("   ❓ " + "；".join(info) + "——需人工或倉庫資料確認。")
    return "\n".join(lines)


def _pairs(v: float) -> str:
    """可做雙數一律**無條件捨去**成整數：0.9 雙做不出來，四捨五入會多承諾。"""
    return f"{int(max(v, 0.0)):,}"


def _mat_label(it) -> str:
    """料的顯示名：品名描述通常已經以料類開頭（『熱熔膠 熱熔膠 981…』），不再重複前綴。"""
    name = _cell(it["品名"]) or it["料號"]
    cat = str(it["料類"] or "")
    return name if (not cat or name.startswith(cat)) else f"{cat} {name}"


def _weeks(v) -> str:
    """可做週數：None＝近期沒生產、無從換算（顯示「—」，不要用猜的速度硬換）。"""
    if v is None:
        return "—"
    if v <= 0:
        return "0 週"
    if v < 0.1:
        return "不足 0.1 週"
    return f"{v:.1f} 週"


def _po_line(po) -> str:
    """見底料的採購狀態一句話 —— 直接看得出「該不該去催採購」。"""
    if not po:
        return "ERP 查無這支料的採購紀錄（可能是客供或帳外料）"
    supplier = _cell(po.get("供應商") or "") or "供應商未載"
    if po.get("已開單"):
        eta = po.get("ETA") or "無計劃到貨日"
        return f"✅ 已開單 {supplier}｜未收 {_n(po.get('在途', 0.0))}｜ETA {eta}"
    last = po.get("最後下單") or "?"
    return f"⚠️ 目前沒有未結採購單（最近一次 {last} 向 {supplier} 買）"


def _render_capacity(rows, customer: str, mirror_ts: str, *, top_style: int = 8,
                     detail: int = 4) -> str:
    """形體剩餘產能報表：總表 + 每個形體的瓶頸/見底/涵蓋說明。"""
    if not rows:
        return (f"查無 {customer} 近 {_CAP_BOM_MONTHS} 個月的訂單用料，算不出形體產能"
                "（客戶代號要跟 ERP 一致，迪卡儂是 'DECA.'）。")
    head = [f"🧮 {customer} 各形體「剩下的材料還能做幾雙」"
            f"（近 {_CAP_BOM_MONTHS} 個月訂單用料推每雙用量）"]
    if mirror_ts:
        head.append(f"資料：ERP 鏡像 {mirror_ts}")
    table_rows = []
    for r in rows[:top_style]:
        neck = r["見底"][0] if r["見底"] else (r["瓶頸"][0] if r["瓶頸"] else None)
        table_rows.append([
            r["鞋款"], _pairs(r["淨可做"]), _weeks(r["可做週數"]), _pairs(r["毛可做"]),
            _mat_label(neck) if neck else "（無可判定料）",
        ])
    head.append(_table(["形體", "淨可做(雙)", "約可再做", "毛可做(雙)", "卡在哪支料"],
                       table_rows))

    for r in rows[:top_style]:
        pace = (f"｜近期週產 {_pairs(r['週產'])} 雙 → 約可再做 {_weeks(r['可做週數'])}"
                if r["週產"] else "｜近期沒生產，無法換算週數")
        lines = [f"▸ {r['鞋款']}　淨可做 {_pairs(r['淨可做'])} 雙｜毛可做 "
                 f"{_pairs(r['毛可做'])} 雙（取樣 {r['取樣單數']} 張單）{pace}"]
        if r["見底"]:
            lines.append(f"   🔴 已見底 {len(r['見底'])} 支（不補料就開不了新單）：")
            for x in r["見底"][:detail]:
                lines.append(f"      {x['料號']} {_mat_label(x)}｜每雙 {x['每雙']:.4f}"
                             f"｜庫存 {_n(x['庫存'])}、在手單已佔 {_n(x['在手單佔用'])}")
                lines.append(f"         採購：{_po_line(x.get('採購'))}")
            if len(r["見底"]) > detail:
                lines.append(f"      …另 {len(r['見底']) - detail} 支")
        if r["瓶頸"]:
            lines.append("   ⛳ 有料的料裡最緊的：" + "、".join(
                f"{x['料類'] or x['料號']} {_pairs(x['淨可做'])}雙"
                for x in r["瓶頸"][:detail]))
        lines.append(f"   ℹ️ 判定涵蓋 {r['可判定']}/{r['核心料']} 支核心料"
                     f"（單位待換算 {r['單位待換算']}、庫存表查無 {r['庫存表查無']}；"
                     f"另 {r['低覆蓋']} 支覆蓋率不足未列）")
        head.append("\n".join(lines))

    head.append(
        f"「約可再做」＝淨可做 ÷ 近 {_CAP_RATE_DAYS} 天該形體的實際週產（生管日報包裝欄）；"
        "近期沒生產的形體顯示「—」，不用猜的速度硬換算。\n"
        "口徑：可用庫存＝0 批＋M01 批（排除廢料倉）；⚠️ M01 批要倉庫實地確認調撥才能領，"
        "預購批(J0C…)綁單不計。「淨可做」已扣掉在手生效單（含逾期單）還沒到的料需求，"
        "「毛可做」沒扣、只看庫存本身。每雙用量取自同形體近期訂單的實際需求量。\n"
        "⚠️ 各形體是「各自」拿整池料算的，共用料會被重複計算 —— 兩個形體的雙數不能相加。"
        "單位待換算/庫存表查無的料沒有列入判定，實際上線前仍要看該形體的齊套表。")
    return "\n\n".join(head)


# ---------- 工具 ----------
def _weekly_rates(styles) -> dict:
    """各形體近 _CAP_RATE_DAYS 天的實際週產（來源＝生管日報包裝欄，在 Drive）。

    Drive 讀不到就回空 dict —— 週數欄顯示「—」，但材料數字照出。ERP 那邊的
    v_production 不能用：包裝站實測只有 105 雙（全廠沒在維護），拿它換算會離譜。
    """
    try:
        from agent_core.factory_production_report import (
            style_output_rate, weekly_rates_for_styles,
        )
        return weekly_rates_for_styles(style_output_rate(days=_CAP_RATE_DAYS), styles)
    except Exception:  # noqa: BLE001
        return {}


def material_capacity_by_style(customer: str = "DECA.", top_style: int = 8) -> str:
    """查某客戶「剩下的材料，每個形體還能再做幾雙／還夠做幾週」——接新單前先看這個。

    問「迪卡儂的料還能做幾雙」「這個形體還能不能再接單」「剩的材料夠開幾雙」時用這個。
    給的是 淨可做（扣掉在手生效單未到料需求，接新單看這個）與 毛可做（不扣，看庫存
    本身），換算成「照近期速度還能再做幾週」，並點出卡在哪支料、見底那幾支的採購狀態
    （跟誰買、開單沒、ETA）。**兩個形體的雙數不可相加**（共用料會重複計）。
    確定性 SQL + Python 彙算，不經 LLM；免 +確認。

    Args:
        customer: ERP 客戶代號（迪卡儂＝"DECA."，預設值就是它）。
        top_style: 最多列幾個形體（依最緊的排前，預設 8）。
    Returns:
        形體總表 + 每個形體的瓶頸料/見底料(含採購狀態)/判定涵蓋說明。
    """
    if not _db_ready():
        return "⚠️ ERP 鏡像還沒建立（var/data/erp_mirror/erp_full.duckdb 不存在），無法計算。"
    cust = str(customer or "DECA.").strip() or "DECA."
    today = _today()
    since = (today - datetime.timedelta(days=30 * _CAP_BOM_MONTHS)).isoformat()
    try:
        snap = _load_snapshot(today)
        res = _analyze(snap, today)
        extras = _load_capacity_extras(cust, since)
    except Exception as exc:  # noqa: BLE001 - 鏡像刷新中/欄位變動都不該炸掉排程
        return f"⚠️ 讀 ERP 鏡像失敗：{type(exc).__name__}: {exc}"
    styles = {str(r[0]) for r in extras["bom"]}
    rows = _capacity_by_style(snap, res, extras, weekly_rates=_weekly_rates(styles))
    try:
        mirror_ts = datetime.datetime.fromtimestamp(
            os.path.getmtime(_db_path())).strftime("%Y-%m-%d %H:%M")
    except OSError:
        mirror_ts = ""
    return _render_capacity(rows, cust, mirror_ts, top_style=max(1, int(top_style or 8)))


def kitting_check(query: str = "", top: int = 8, item: str = "") -> str:
    """查訂單「齊套/缺料」狀態：逐料看 需求/已到/在途(ETA)/庫存可補/缺口，含共用料衝突。

    問「JFC26490 料齊了嗎」「這單缺什麼料」「這單能不能上線」，以及**「某訂單×某料
    短少多少」（如 JFC26490 庫存編號 SF24 短少數量）**時用這個——訂單×材料的
    需求/已到/在途/短少是本工具的主場，**別**拿 query_erp_bom 用量×雙數自算、或用
    query_erp_purchase_orders 的舊單到貨腦補（採購配額是綁單的，舊單到貨≠新單有料）。
    聚焦視圖帶「訂購」欄與「⛔ 採購面」判定行：訂購＜需求＝本單未開(足) ERP 採購單、
    採購面即缺料；**待確認批（M01/預購）≠可發料**，調撥夠不夠看「採購面判定」行，
    回覆照著念、不要自行推論「調撥後即可上線」。只分析 狀態=生效 的訂單
    （完工/出貨單沒有齊套問題）。資料=ERP 鏡像（T-1，每日 02:00 刷新）；
    SAFE tier 免確認。

    Args:
        query: 訂單單號（如 JFC26490）或客戶關鍵字（如 DECA）；必填。
        top:   每張單最多列幾筆缺料明細（預設 8）。
        item:  選填，聚焦單一材料：料號片段/品名/庫存編號舊短碼（如 SF24）——
               命中料不分桶全列（齊/可補/待換算也給判定），舊短碼自動對應料號。

    Returns:
        markdown —— 每張命中訂單的燈號/料項判定彙總/缺料明細表（或指定料聚焦表）。
    """
    if not _db_ready():
        return "❌ ERP 本地鏡像倉不存在。"
    q = (query or "").strip()
    top = int(top)
    if not q:
        return "請給訂單單號（如 JFC26490）或客戶關鍵字（如 DECA）。"
    try:
        today = _today()
        snap = _load_snapshot(today)
        res = _analyze(snap, today)
    except Exception as e:  # noqa: BLE001
        return f"❌ 齊套查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    ql = q.lower()
    hits = [r for r in res["orders"].values()
            if ql in r["單號"].lower() or ql in (r["客戶"] or "").lower()
            or ql in (r["客戶PO"] or "").lower()]
    if not hits:
        note = ""
        if any(ql in c.lower() for c in _NO_BOM_CUSTOMERS):
            note = f"（{_NO_BOM_CUSTOMERS[0]} 為結構性無 BOM 客戶，ERP 無備料資料可判）"
        return f"查無符合「{q}」的生效訂單{note}。完工/出貨單不做齊套分析。"
    hits.sort(key=lambda r: (r["交期"] or "9999-99-99", r["單號"]))

    item_kw = (item or "").strip().lstrip("#")
    if item_kw:
        # 聚焦單一材料：舊短碼（SF24）先過 _item_alias 對應成料號（精確比對、
        # 查無對照就用原字面搜料號/品名——同 #307 三查詢面慣例）。
        try:
            from agent_core.erp_stock_query import _alias_expand
            alias = _alias_expand(item_kw)
        except Exception:  # noqa: BLE001 - alias 是加值路徑，失敗不擋主查詢
            alias = []
        terms = {item_kw.lower()} | {i.lower() for i, _a in alias}
        blocks = [_render_order_item_focus(r, terms, item_kw, top)
                  for r in hits[:6]]
        if alias:
            items = "、".join(_cell(i) for i, _a in alias)
            blocks.insert(0, f"ℹ️ 「{_cell(item_kw)}」是庫存編號（ERP 舊短碼），"
                             f"對應料號：{items}")
        # 採購面總判定（2026-07-29 UserA 案二）：待確認批（M01/預購）是全料號
        # 共用一池，逐單各列 26.4 會被誤讀成「各單都有 26.4 可調撥」——合計缺口
        # 91.9 vs 池 26.4 時，freeform 曾腦補「倉庫調撥後即可發料上線」。這裡把
        # 「調撥夠不夠、不夠差多少要採購」算死，回覆照念。單位待換算料一樣判
        # （需求/訂購/缺同表同單位可比），只是庫存側數字掛「如同單位」保留。
        pooled = {}
        for r in hits[:6]:
            for it in r["items"]:
                if it["判定"] not in ("缺料無來源", "單位待換算") or it["缺"] <= 1e-6:
                    continue
                if not any(t in it["料號"].lower() or t in (it["品名"] or "").lower()
                           for t in terms):
                    continue
                p = pooled.setdefault(it["料號"], {"缺": 0.0, "單數": 0,
                                                   "需求": 0.0, "訂購": 0.0,
                                                   "池": it.get("待確認", 0.0),
                                                   "零批": it.get("零批", 0.0),
                                                   "unit_bad": False})
                p["缺"] += it["缺"]
                p["需求"] += it["需求"]
                p["訂購"] += it["訂購"]
                p["單數"] += 1
                p["unit_bad"] = p["unit_bad"] or it["單位待換算"]
        for code, p in sorted(pooled.items()):
            if p["unit_bad"]:
                # 單位不可比：FCFS 沒扣 0 批，庫存側=0批+待確認 全掛參考；
                # 減法給「如同單位」條件句，不做無 hedge 的跨單位結論。
                ref = p["零批"] + p["池"]
                gap = p["缺"] - ref
                src = ("ERP 未開(足) 採購單，採購面判缺料"
                       if p["訂購"] + 1e-6 < p["需求"]
                       else "ERP 已開採購但無到貨/在途訊號（請對採購單催辦）")
                if gap > 1e-6:
                    blocks.append(
                        f"⛔ 採購面判定 {_cell(code)}：命中 {p['單數']} 張單合計缺 "
                        f"{_n(p['缺'])}（需求−已到−在途，同表同單位）；{src}。"
                        f"廠內結存單位與採購單位不一致僅供參考"
                        f"（0批 {_n(p['零批'])}＋待確認批 {_n(p['池'])}＝{_n(ref)}，"
                        f"如同單位**全數調撥仍缺 {_n(gap)}**）——缺口需開 ERP 採購單"
                        "補量；已在 ERP 外手工下單者請補登 ERP，否則 ERP 持續判缺。")
                else:
                    blocks.append(
                        f"🟡 {_cell(code)}：命中單合計缺 {_n(p['缺'])}；廠內結存"
                        f"（0批 {_n(p['零批'])}＋待確認批 {_n(p['池'])}）帳面蓋得住，"
                        "但單位與採購單位不一致、無法自動核銷——請倉庫實地核對並"
                        "調撥，實測不足再開 ERP 採購。")
                continue
            gap = p["缺"] - p["池"]
            if gap > 1e-6 and p["池"] > 1e-6:
                blocks.append(
                    f"⛔ 採購面判定 {_cell(code)}：命中 {p['單數']} 張單合計缺 "
                    f"{_n(p['缺'])}；待確認批（M01/預購）全料號僅此一池 {_n(p['池'])}"
                    f"——**即使全數調撥仍缺 {_n(gap)}，需開 ERP 採購單補量**；"
                    "已在 ERP 外手工下單者請補登 ERP，否則 ERP 持續判缺。")
            elif gap > 1e-6:
                blocks.append(
                    f"⛔ 採購面判定 {_cell(code)}：命中 {p['單數']} 張單合計缺 "
                    f"{_n(p['缺'])}，廠內無待確認批可調撥——**需開 ERP 採購單補量**；"
                    "已在 ERP 外手工下單者請補登 ERP，否則 ERP 持續判缺。")
            elif p["缺"] > 1e-6:
                blocks.append(
                    f"🟡 {_cell(code)}：命中單合計缺 {_n(p['缺'])}，待確認批 "
                    f"{_n(p['池'])} 調撥到 0 批後可補——仍需倉庫實地確認調撥，"
                    "**非**已可發料。")
        if len(hits) > 6:
            blocks.append(f"…共 {len(hits)} 張命中，只顯示交期最近 6 張"
                          "（可用單號縮小範圍）。")
        focus_items = {it["料號"] for r in hits[:6] for it in r["items"]
                       if any(t in it["料號"].lower()
                              or t in (it["品名"] or "").lower() for t in terms)}
        conf = [c for c in res["conflicts"] if c["料號"] in focus_items]
        if conf:
            rows = [[c["料號"], _n(c["總缺口"]), _n(c["庫存"]),
                     _n(c["總缺口"] - c["庫存"]), c["影響單數"]] for c in conf[:5]]
            blocks.append("⚠️ 此料共用衝突（生效單池未到未在途總需求 > 庫存，"
                          "先到先贏）：\n"
                          + _table(["料號", "總需求", "庫存", "淨缺", "影響單數"], rows))
        return "\n\n".join(blocks)

    blocks = [_render_order(r, top) for r in hits[:6]]
    if len(hits) > 6:
        blocks.append(f"…共 {len(hits)} 張命中，只顯示交期最近 6 張（可用單號縮小範圍）。")
    conf = [c for c in res["conflicts"]
            if any(it["料號"] == c["料號"] for r in hits for it in r["items"])]
    if conf:
        rows = [[c["料號"], _n(c["總缺口"]), _n(c["庫存"]),
                 _n(c["總缺口"] - c["庫存"]), c["影響單數"]] for c in conf[:5]]
        blocks.append("⚠️ 相關共用料衝突（生效單池未到未在途總需求 > 庫存，"
                      "先到先贏；殭屍/不可比單位不入池）：\n"
                      + _table(["料號", "總需求", "庫存", "淨缺", "影響單數"], rows))
    return "\n\n".join(blocks)


def kitting_alert(days_ahead: int = _DEFAULT_WINDOW_D, min_need: float = _MIN_NEED,
                  top: int = 12) -> str:
    """齊套/缺料上線預警（確定性、免 Gemini）：掃「交期在 days_ahead 天內、還沒上線」
    的生效訂單，找出料沒齊的——缺料無來源(最急)/在途但ETA逾期(催辦)/共用料衝突，
    另附「交期已過仍未上線」催排清單。每日早報 daemon 用；互動問答也可直接叫。免 +確認。

    上線日 ERP 裡不存在（排程模組未啟用），用交期畫寬窗：實測 交期−首次入站 p50=33 天、
    p75=63 天，預設 63 天窗=多數單在備料期就被掃到。

    Args:
        days_ahead: 交期在幾天內的未上線單納入掃描（預設 63）。
        min_need:   料項需求量門檻，低於此不判定（預設 1.0，濾微量縫線殘渣）。
        top:        缺料訂單最多列幾張（預設 12）。

    Returns:
        風險報告；全綠時回「(無新發現)」（daemon 據此不推播）。
    """
    if not _db_ready():
        return "❌ ERP 本地鏡像倉不存在。"
    top = int(top)
    min_need = float(min_need)
    try:
        today = _today()
        snap = _load_snapshot(today)
        res = _analyze(snap, today, min_need=min_need)
    except Exception as e:  # noqa: BLE001
        return f"❌ 齊套預警查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    today_s = today.isoformat()
    hi = (today + datetime.timedelta(days=int(days_ahead))).isoformat()
    urgent_s = (today + datetime.timedelta(days=14)).isoformat()
    excluded_ids = {r["單號"] for r in res["excluded"]}

    scope = [r for r in res["orders"].values()
             if not r["已上線"] and r["單號"] not in excluded_ids
             and r["交期"] and today_s <= r["交期"] <= hi]
    scope.sort(key=lambda r: (r["交期"], r["單號"]))
    lights = {r["單號"]: _order_light(r) for r in scope}
    red = [r for r in scope if lights[r["單號"]] == "🔴"]
    orange = [r for r in scope if lights[r["單號"]] == "🟠"]
    yellow = [r for r in scope if lights[r["單號"]] == "🟡"]
    # 🟡（大半料項無法判定）平時只計數；交期逼近仍資料不足=該有人去現場看
    yellow_urgent = [r for r in yellow if r["交期"] <= urgent_s]
    overdue_unstarted = sorted((r for r in res["zombies"] if not r["已上線"]),
                               key=lambda r: r["交期"])

    if not red and not orange and not overdue_unstarted and not yellow_urgent:
        return "(無新發現)"

    # 卡單料視圖：同一個缺料常擋住一整批單，聚合比逐單重複列有用
    blockers = {}
    for r in red:
        for it in r["items"]:
            if it["判定"] != "缺料無來源":
                continue
            blk = blockers.setdefault(it["料號"], {"品名": it["品名"], "總缺": 0.0,
                                                   "單數": 0, "最近交期": r["交期"]})
            blk["總缺"] += it["缺"]
            blk["單數"] += 1
            blk["最近交期"] = min(blk["最近交期"], r["交期"])
    ranked_blockers = sorted(blockers.items(),
                             key=lambda kv: (-kv[1]["單數"], -kv[1]["總缺"]))
    conf = [c for c in res["conflicts"]
            if any(it["料號"] == c["料號"] for r in scope for it in r["items"])]

    def _compose(n_red: int, n_conf: int) -> str:
        out = [f"🧩 齊套/缺料上線預警（生效未上線、交期 {today_s}〜{hi}：{len(scope)} 張 —— "
               f"🔴{len(red)}／🟠{len(orange)}／🟡{len(yellow)} 資料不足／"
               f"✅{len(scope) - len(red) - len(orange) - len(yellow)}）"]
        if ranked_blockers:
            rows = [[k, b["品名"], _n(b["總缺"]), b["單數"], b["最近交期"]]
                    for k, b in ranked_blockers[:10]]
            out.append("\n⛔ 卡單料（無來源缺料 × 影響單數，補這些解最多單；"
                       "缺口為窗內紅單口徑，採購總量看下方衝突表）：\n"
                       + _table(["料號", "品名", "總缺口", "卡單數", "最近交期"], rows))
        if red:
            out.append(f"\n🔴 缺料且無補貨來源：{len(red)} 張（列交期最近 {n_red} 張，"
                       "其餘用 kitting_check 逐張看）")
            for r in red[:n_red]:
                out.append(_render_order(r, top=5))
        if orange:
            out.append(f"\n🟠 在途 ETA 逾期需催辦：{len(orange)} 張")
            rows = []
            for r in orange[:top]:
                worst = max((it for it in r["items"] if it["判定"] == "在途逾期"),
                            key=lambda x: x["缺"], default=None)
                rows.append([r["單號"], r["客戶"], r["交期"],
                             (worst["品名"] or worst["料號"]) if worst else "-"])
            out.append(_table(["單號", "客戶", "交期", "最大逾期在途料"], rows))
        if yellow_urgent:
            out.append(f"\n🟡 交期 14 天內仍大半無資料可判：{len(yellow_urgent)} 張"
                       "（建議現場確認備料）")
            rows = [[r["單號"], r["客戶"], r["交期"],
                     (r["buckets"].get("無法判定", 0)
                      + r["buckets"].get("單位待換算", 0)
                      + r["buckets"].get("無追蹤資料", 0))]
                    for r in yellow_urgent[:top]]
            out.append(_table(["單號", "客戶", "交期", "不可驗項數"], rows))
        if conf:
            rows = [[c["料號"], _n(c["總缺口"]), _n(c["庫存"]),
                     _n(c["總缺口"] - c["庫存"]), c["影響單數"]] for c in conf[:n_conf]]
            out.append("\n⚠️ 共用料衝突（生效單池未到未在途總需求 > 庫存，先到先贏；"
                       "殭屍/不可比單位不入池）：\n"
                       + _table(["料號", "總需求", "庫存", "淨缺", "影響單數"], rows))
        if overdue_unstarted:
            out.append(f"\n📋 交期已過仍未上線（催排＋查齊套）：{len(overdue_unstarted)} 張"
                       "（燈號為非佔用式庫存比對，未保留）")
            rows = [[r["單號"], r["客戶"], r["交期"], _n(r["數量"]), _order_light(r)]
                    for r in overdue_unstarted[:top]]
            out.append(_table(["單號", "客戶", "交期(已過)", "雙數", "齊套"], rows))
            if len(overdue_unstarted) > top:
                out.append(f"…另 {len(overdue_unstarted) - top} 張略。")
        if res["excluded"]:
            out.append(f"\nℹ️ {_NO_BOM_CUSTOMERS[0]} {len(res['excluded'])} 張生效單無 BOM"
                       "（結構性），不在判定範圍。")
        out.append("\n（已到=max(點收,發料,PO已收份額)；不可信料類零訊號標「無法判定」"
                   "不算缺料。資料為 T-1 鏡像。）")
        return "\n".join(out)

    # Telegram 單則 4096 上限：超預算就縮紅單明細/衝突列數（實測滿載會貼線掉版）
    for n_red, n_conf in ((5, 8), (3, 6), (2, 4), (1, 3)):
        text = _compose(n_red, n_conf)
        if len(text) <= 3500:
            return text
    return text


def production_capacity_brief() -> str:
    """生產管理部每日簡報：迪卡儂各形體剩餘材料可做雙數 ＋ 針車/射出/包裝 每日產能折線圖。

    生管主管經理（生產管理部主管）每天 09:00 / 15:00 的固定報表，走 dispatcher 的
    ``deterministic_tool``：整封信就是這顆工具的回傳、**完全不經 Gemini**，數字沒有
    被轉述錯的空間。折線圖以 ``[[MAIL_FILE:]]`` 標記帶出，由 dispatcher 轉成附件。
    免 +確認。

    Returns:
        兩段報表（材料可做雙數 / 三站每日產能）＋ 折線圖附件標記。
    """
    now = datetime.datetime.now()
    parts = [f"【生產管理每日簡報】{now.strftime('%Y-%m-%d (%a) %H:%M')}"]
    try:
        parts.append(material_capacity_by_style("DECA."))
    except Exception as exc:  # noqa: BLE001 - 一段掛掉不該讓整封信發不出去
        parts.append(f"⚠️ 材料可做雙數這段失敗：{type(exc).__name__}: {exc}")
    try:
        # 生管日報在 Drive（不在 ERP 鏡像），所以這段是 agent_core 的工具；
        # 兩段合成一顆確定性工具，才能走 deterministic_tool 不經 LLM。
        from agent_core.factory_production_report import station_capacity_report
        parts.append(station_capacity_report(days=_BRIEF_CHART_DAYS))
    except Exception as exc:  # noqa: BLE001
        parts.append(f"⚠️ 每日產能折線圖這段失敗：{type(exc).__name__}: {exc}")
    return "\n\n".join(parts)


SKILL_TOOLS = [kitting_check, kitting_alert, material_capacity_by_style,
               production_capacity_brief]

# 純讀 ERP 鏡像、無副作用 → 允許背景 dispatcher 用。
# kitting_alert_daily 排程任務的第 1 步就是呼叫它；沒這行它進不了背景工具集，
# 任務只會照 prompt 的退路回「(無新發現)」，天天靜默空轉。
kitting_alert.background_safe = True
material_capacity_by_style.background_safe = True
# deterministic_tool 也是從 safe_tools() 解析工具名的（見 run_deterministic_task），
# 沒這行排程會直接 raise「不在背景唯讀工具集裡」。
production_capacity_brief.background_safe = True
