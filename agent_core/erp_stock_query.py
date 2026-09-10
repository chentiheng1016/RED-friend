"""飛越 ERP 確定性查詢 —— 零 LLM 的料況/採購單面（給員工的「照表念」數字）。

跟 skills/erp_warehouse.py 的 text-to-SQL 不同：這裡的 SQL 是寫死的參數化模板，
從查詢到格式化全程不經任何 LLM——輸入關鍵字、輸出數字，保證與 ERP 鏡像
一字不差。入口共用本模組：

  - skills/erp_warehouse.query_erp_stock       freeform 工具面（owner＋員工白名單）
  - skills/erp_warehouse.query_erp_purchase_orders  freeform 採購單面（同上）
  - skills/erp_warehouse.query_erp_bom         freeform BOM 用料面（同上）
  - skills/erp_warehouse.query_erp_payables    freeform 應付請款面（同上）
  - skills/erp_warehouse.query_erp_allocation  freeform 庫存分配紀錄面（同上）
  - /dept yellow|indigo query.erp_stock        telegram_command 確定性通道（零 LLM）
  - /dept yellow|indigo query.erp_po           採購單同通道
  - /dept yellow query.erp_bom                 BOM 用料同通道
  - /dept yellow query.erp_ap                  應付請款同通道
  - /dept yellow|indigo query.erp_alloc        庫存分配紀錄同通道
  - /purchase stock、/purchase erppo、/purchase bom、/purchase ap、
    /purchase alloc、/warehouse erp、/warehouse erppo、/warehouse alloc
    捷徑（同上）

erp_po_lookup 的由來：2026-07-26 UserA 反應「AI 查採購單數量抓成總金額」——
freeform 走 RAG 讀採購單 PDF，Gemini 把金額欄（USD）當訂購數量回報（六張單
全中、還加總成 38,927.02 說是數量）。採購單數字從此走這裡照表念，數量/金額
分欄明示。

erp_bom_lookup 的由來：2026-07-27 UserA 反應「查 G40 用在哪幾款，AI 多算了
#8916447」——freeform 走 RAG 讀 Drive BOM 表（型體級證據），把同型體
DJS336195 的兩個顏色都算成用料款；實際上料號尾碼（-E040）是**顏色級**用料，
只有 -01GY.BU（#8916446）在用。BOM 用料從此走這裡照 ERP 研發 BOM（RD_BOM_*，
只取生效版）念，並主動列出「同型體但不用此料」的顏色防外推。

erp_ap_lookup 的由來：2026-07-27 三寶案——問「泉州三寶電子 2026 年帳單總金額」，
freeform 拿 query_erp_purchase_orders 的採購單金額加總回 111,502.20，漏了
不掛採購單的運輸費 5,140（請款明細表上 採購單號 0 那筆）；正確帳單總額是
應付請款單 JFPA2605007 的 116,642.20。「帳單/請款/應付」的權威源是 GL00__AP_*
應付域（NET_MONEY 含運費等非 PO 費用），採購單加總天生答不了這類問題。

防線同 skills/erp_warehouse.py：唯讀連線＋enable_external_access=false＋deadline
中斷。關鍵字走 duckdb 參數繫結（非字串拼接）且 LIKE 萬用字元逐字轉義——輸入
永遠當字面值，無注入面。品名/倉名是 ERP 自由文字欄，輸出前逐格過
sanitize_untrusted_text（防 prompt injection 迴流 freeform session）。
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime

from agent_core.prompt_injection import sanitize_untrusted_text

_QUERY_DEADLINE_S = 30.0
_MAX_ITEMS = 20            # 一次最多列幾個料號（超過=關鍵字太寬，明講截斷）
_MAX_LOT_ITEMS = 5         # show_lots 最多展開幾個料號的批號明細
_MAX_PO_LINES = 30         # 採購單明細最多列幾個項次（合計仍含全部符合列）
_EXCLUDE_WAREHOUSES = ("SW",)   # 廢料倉不算可用庫存（同 skills/erp_kitting.py）


def _db_path() -> str:
    from agent_core.logging_and_paths import DATA_DIR
    return os.path.join(DATA_DIR, "erp_mirror", "erp_full.duckdb")


def _db_ready() -> bool:
    return os.path.exists(_db_path())


def _connect_ro():
    import duckdb
    return duckdb.connect(_db_path(), read_only=True,
                          config={"enable_external_access": "false"})


def _run(sql: str, params: list) -> list:
    con = _connect_ro()
    timer = threading.Timer(_QUERY_DEADLINE_S, con.interrupt)
    timer.start()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        timer.cancel()
        con.close()


def _escape_like(keyword: str) -> str:
    """LIKE 萬用字元逐字轉義——關鍵字永遠是字面值，'%' 就是查含 '%' 的料號。"""
    return (keyword.replace("\\", "\\\\")
                   .replace("%", "\\%")
                   .replace("_", "\\_"))


def _clean(v) -> str:
    s = sanitize_untrusted_text(v).strip() if isinstance(v, str) else str(v or "")
    return s.replace("\n", " ").replace("\t", " ")


def _fmt_qty(q: float) -> str:
    if q == int(q):
        return str(int(q))
    return f"{q:.2f}".rstrip("0").rstrip(".")


def _mirror_stamp() -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(_db_path())).strftime(
            "%Y-%m-%d %H:%M")
    except OSError:
        return "未知"


def _stale_hint() -> str:
    """查無時的快照時滯說明。

    起因：2026-07-27 Hawai OZ20 案——庫存編碼當天才輸入 ERP，鏡像（每日凌晨刷新）
    自然查無，但小紅回「查不到」沒講資料是快照，讓使用者以為系統沒這筆。
    查無 ≠ ERP 沒有：今天剛輸入的資料要到明晨刷新後才查得到。
    """
    return (f"\n⏱️ 註：資料截至鏡像時間 {_mirror_stamp()}（每日凌晨刷新）——"
            "今天剛輸入 ERP 的資料，要到明晨刷新後才查得到；"
            "急件請直接開 ERP 畫面確認。")


def _alias_expand(kw: str) -> list[tuple[str, str]]:
    """庫存編號（倉庫/採購慣用舊短碼，如 SF24）→ [(料號, 庫存編號)]。

    對照表 _item_alias 由鏡像每日同步（erp_mirror.sync_item_alias；源頭是
    SP00.SP_ITEM.O_ITEMNO，該表唯讀帳號無授權、借 GG_1001 函式解碼）。表不存在
    （尚未同步）或查詢失敗回 []——alias 是加值路徑，不影響主查詢。

    比對規則（2026-07-29 UserA G407 案）：**精確**比對（去空白、不分大小寫）
    ＋ ERP 自家「+版次」變體（G407 也命中 G407+EP5）。「+」是 ERP 短碼的版次
    分隔符、同一材料的工程版；研發 BOM／庫存常只掛版次料號（-G050-005），
    只查裸碼會全漏。仍不做字串外推：SF24 不得命中 SF24.5/SF23.7，
    舊短碼一字之差就是不同材料（同 2026-07-27 G40 顏色級外推教訓）。
    """
    k = (kw or "").strip()
    if not k:
        return []
    try:
        return _run(
            'SELECT ITEM_NO, O_ITEMNO FROM "_item_alias" '
            "WHERE UPPER(TRIM(O_ITEMNO)) = UPPER(?) "
            "   OR UPPER(TRIM(O_ITEMNO)) LIKE UPPER(?) || '+%' ESCAPE '\\' "
            "ORDER BY ITEM_NO",
            [k, _escape_like(k)])
    except Exception:  # noqa: BLE001 - 表未同步/舊鏡像，一律靜默退回無 alias
        return []


def _alias_note(kw: str, alias: list[tuple[str, str]]) -> str:
    base = _clean(kw).upper()

    def _fmt(item: str, a: str) -> str:
        a = _clean(a)
        return f"{_clean(item)}({a})" if a.upper() != base else _clean(item)

    items = "、".join(_fmt(i, a) for i, a in alias)
    return (f"ℹ️ 「{_clean(kw)}」是庫存編號（ERP 舊短碼），"
            f"對應料號：{items}")


def erp_stock_lookup(keyword: str, *, show_lots: bool = False) -> str:
    """依料號片段/品名關鍵字查 v_stock 各倉結存。確定性：同輸入必同輸出。"""
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return ("用法：輸入至少 2 個字的料號片段或品名關鍵字"
                "（例：G40、佳積布、DFDT7100145000）。")
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    pattern = f"%{_escape_like(kw)}%"
    alias = _alias_expand(kw)
    where = "料號 ILIKE ? ESCAPE '\\' OR 品名描述 ILIKE ? ESCAPE '\\'"
    params: list = [pattern, pattern]
    if alias:
        ph = ", ".join("?" for _ in alias)
        where += f" OR 料號 IN ({ph})"
        params += [i for i, _a in alias]
    try:
        rows = _run(
            "SELECT 料號, COALESCE(品名描述, ''), COALESCE(材料類型, ''), "
            "       COALESCE(倉庫代碼, ''), COALESCE(倉庫名稱, ''), "
            "       COALESCE(單位, ''), SUM(COALESCE(結存數量, 0)) "
            "FROM v_stock "
            f"WHERE {where} "
            "GROUP BY 1, 2, 3, 4, 5, 6 "
            "ORDER BY 1, 4",
            params,
        )
    except Exception as e:  # noqa: BLE001 - 對話面工具，回錯誤字串不 raise
        return f"❌ 庫存查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    if not rows:
        prefix = _alias_note(kw, alias) + "\n" if alias else ""
        return (f"{prefix}查無符合「{_clean(kw)}」的料號（ERP 庫存帳無任何倉別紀錄）。\n"
                "提示：耗材（膠水/底料/工具/文具）記在倉庫 Excel，"
                "請改用 read_warehouse_stock；面料/主料才在 ERP；"
                "「庫存編號」舊短碼（如 SF24）可直接輸入、會自動對應料號。"
                + _stale_hint())

    # 按料號聚合（rows 已按 料號, 倉庫代碼 排序）
    items: dict[str, dict] = {}
    for item_no, desc, cat, wh_code, wh_name, unit, qty in rows:
        it = items.setdefault(item_no, {
            "desc": desc, "cat": cat, "unit": unit, "whs": [],
        })
        it["whs"].append((wh_code, wh_name, float(qty or 0)))

    total_items = len(items)
    truncated = total_items > _MAX_ITEMS
    if truncated:
        # 保留可用量最大的前 _MAX_ITEMS 個（確定性排序：量降冪、料號升冪）
        ranked = sorted(
            items.items(),
            key=lambda kv: (
                -sum(abs(q) for c, _n, q in kv[1]["whs"]
                     if c not in _EXCLUDE_WAREHOUSES),
                kv[0],
            ),
        )[:_MAX_ITEMS]
        items = dict(ranked)

    # 批次三分解（2026-07-29 UserA 案：可用庫存＝**0 批**；M01 要倉庫實地確認
    # 調撥到 0 批、預購批(J0C…)綁單——全批加總會高報可用）。lot 資料缺失時
    # batches 為空 → 該料退回舊「非廢料倉合計」口徑並註明。
    batches: dict[str, dict] = {}
    try:
        ph = ", ".join("?" for _ in items)
        brows = _run(
            "SELECT 料號, "
            "  CASE WHEN TRIM(COALESCE(批號,'')) IN ('0','') THEN '可用' "
            "       WHEN TRIM(COALESCE(批號,'')) = 'M01' THEN '待確認' "
            "       ELSE '預購批' END, "
            "  SUM(COALESCE(結存數量, 0)) "
            f"FROM v_stock_lot WHERE 料號 IN ({ph}) "
            f"  AND COALESCE(倉庫代碼, '') NOT IN "
            f"      ({', '.join(repr(w) for w in _EXCLUDE_WAREHOUSES)}) "
            "GROUP BY 1, 2",
            list(items.keys()),
        )
        for item_no, klass, qty in brows:
            batches.setdefault(item_no, {})[klass] = float(qty or 0)
    except Exception:  # noqa: BLE001 - 批次是口徑修正，查壞退回舊口徑不擋主結果
        batches = {}

    lots_by_item: dict[str, list] = {}
    lots_note = ""
    if show_lots:
        lot_targets = list(items.keys())[:_MAX_LOT_ITEMS]
        if len(items) > _MAX_LOT_ITEMS:
            lots_note = (f"（批號明細只展開前 {_MAX_LOT_ITEMS} 個料號，"
                         "請用更精確關鍵字）")
        try:
            ph = ", ".join("?" for _ in lot_targets)
            lot_rows = _run(
                "SELECT 料號, COALESCE(倉庫代碼, ''), COALESCE(批號, ''), "
                "       COALESCE(儲位, ''), SUM(COALESCE(結存數量, 0)) "
                f"FROM v_stock_lot WHERE 料號 IN ({ph}) "
                "GROUP BY 1, 2, 3, 4 HAVING SUM(COALESCE(結存數量, 0)) <> 0 "
                "ORDER BY 1, 2, 3",
                lot_targets,
            )
            for item_no, wh_code, lot_no, store, qty in lot_rows:
                lots_by_item.setdefault(item_no, []).append(
                    (wh_code, lot_no, store, float(qty or 0)))
        except Exception as e:  # noqa: BLE001 - 批號是加值資訊，失敗不擋主結果
            lots_note = f"（批號明細查詢失敗：{type(e).__name__}）"

    out = [f"🔎 ERP 庫存「{_clean(kw)}」：{total_items} 個料號符合"
           f"（鏡像時間 {_mirror_stamp()}）"]
    if alias:
        out.append(_alias_note(kw, alias))
    if truncated:
        out.append(f"⚠️ 只列可用量最大的前 {_MAX_ITEMS} 個——請用更精確的關鍵字。")
    out.append("")

    for item_no, it in items.items():
        unit = _clean(it["unit"])
        head = f"▪ {_clean(item_no)}"
        if it["desc"]:
            head += f"｜{_clean(it['desc'])}"
        if it["cat"]:
            head += f"｜{_clean(it['cat'])}"
        out.append(head)

        avail = 0.0
        wh_parts = []
        for wh_code, wh_name, qty in it["whs"]:
            excluded = wh_code in _EXCLUDE_WAREHOUSES
            if not excluded:
                avail += qty
            if qty == 0 and not excluded:
                continue  # 非廢料倉的 0 結存倉別不逐一列（合計已含）
            label = _clean(wh_name) or _clean(wh_code)
            if _clean(wh_code) and _clean(wh_name):
                label = f"{_clean(wh_name)}({_clean(wh_code)})"
            line = f"{label} {_fmt_qty(qty)} {unit}".rstrip()
            if excluded:
                line += "（廢料倉，不計入可用）"
            wh_parts.append(line)
        if wh_parts:
            out.append("  " + "｜".join(wh_parts))
        else:
            out.append("  各倉結存皆為 0")
        b = batches.get(item_no)
        if b:
            seg = [f"  ✅ 可用(0批) {_fmt_qty(b.get('可用', 0.0))} {unit}".rstrip()]
            if b.get("待確認"):
                seg.append(f"待倉確認(M01) {_fmt_qty(b['待確認'])} {unit}".rstrip())
            if b.get("預購批"):
                seg.append(f"預購批(綁單) {_fmt_qty(b['預購批'])} {unit}".rstrip())
            out.append("｜".join(seg))
        else:
            out.append(f"  可用合計 {_fmt_qty(avail)} {unit}（無批次資料，"
                       "全批合計口徑）".rstrip())

        for wh_code, lot_no, store, qty in lots_by_item.get(item_no, []):
            store_s = f" 儲位{_clean(store)}" if _clean(store) else ""
            out.append(f"    批 {_clean(lot_no)}｜{_clean(wh_code)}{store_s}｜"
                       f"{_fmt_qty(qty)} {unit}".rstrip())

    out.append("")
    footer = ("──\n數字直讀 ERP 鏡像（每日凌晨刷新），未經 AI 生成；"
              "**可用庫存＝0 批**（排除廢料倉SW）——M01 批要倉庫實地確認"
              "調撥到 0 批後才可用、預購批(J0C…)綁單不可挪用，"
              "回答「可用/能撥多少」只能講 0 批數字；單位=庫存單位，可能≠採購單位。"
              "預購批**綁哪張指令訂單/何時分配**這裡看不到，"
              "用 query_erp_allocation 查分配紀錄，別自行推論。")
    if lots_note:
        footer += "\n" + lots_note
    out.append(footer)
    return "\n".join(out)


_DATE_ARG_RE = re.compile(r"^\d{4}(?:-\d{2}){0,2}$")


def _normalize_date_arg(raw: str, *, is_to: bool) -> str | None:
    """把 ''/YYYY/YYYY-MM/YYYY-MM-DD 正規化成可做字串比較的日期；格式錯回 None。

    月份/整年展開成邊界日（to 端補 -12-31 / -31——字串比較用，不需是真實日期）。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if not _DATE_ARG_RE.match(s):
        return None
    if len(s) == 4:
        return f"{s}-12-31" if is_to else f"{s}-01-01"
    if len(s) == 7:
        return f"{s}-31" if is_to else f"{s}-01"
    return s


def _sum_by(pairs: list[tuple[str, float]]) -> str:
    """[(單位, 量)] → '5459 M、300 Y'（單位排序固定，確定性輸出）。"""
    agg: dict[str, float] = {}
    for unit, qty in pairs:
        agg[unit] = agg.get(unit, 0.0) + qty
    return "、".join(f"{_fmt_qty(q)} {u}".rstrip()
                     for u, q in sorted(agg.items()))


def erp_po_lookup(keyword: str, *, date_from: str = "", date_to: str = "") -> str:
    """依料號/品名/供應商/採購單號關鍵字查 v_purchase_orders 逐項次明細＋合計。

    確定性：同輸入必同輸出。「訂購數量」與「金額」分欄明示——這支工具存在的
    原因就是 LLM 讀採購單 PDF 會把金額當數量（見模組 docstring 案例）。
    """
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return ("用法：輸入至少 2 個字的料號/品名/供應商/採購單號關鍵字"
                "（例：G40、富泰、JF0P26060007）；可加 date_from/date_to"
                "（YYYY 或 YYYY-MM-DD）篩下單期間。")
    lo = _normalize_date_arg(date_from, is_to=False)
    hi = _normalize_date_arg(date_to, is_to=True)
    if lo is None or hi is None:
        return "日期格式錯誤：date_from/date_to 要是 YYYY、YYYY-MM 或 YYYY-MM-DD。"
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    pattern = f"%{_escape_like(kw)}%"
    alias = _alias_expand(kw)
    alias_sql = ""
    alias_params: list = []
    if alias:
        ph = ", ".join("?" for _ in alias)
        alias_sql = f" OR 料號 IN ({ph})"
        alias_params = [i for i, _a in alias]
    sql = (
        "SELECT 採購單號, 項次, substr(COALESCE(下單日期, ''), 1, 10), "
        "       COALESCE(供應商簡稱, 供應商全名, ''), 料號, "
        "       COALESCE(品名描述, ''), COALESCE(採購單位, ''), "
        "       COALESCE(訂購數量, 0), COALESCE(已收數量, 0), "
        "       COALESCE(單價, 0), COALESCE(幣別, ''), COALESCE(金額, 0), "
        "       substr(COALESCE(計劃到貨日, ''), 1, 10), COALESCE(明細狀態, '') "
        "FROM v_purchase_orders "
        "WHERE (採購單號 ILIKE ? ESCAPE '\\' OR 料號 ILIKE ? ESCAPE '\\' "
        "       OR 品名描述 ILIKE ? ESCAPE '\\' "
        "       OR 供應商簡稱 ILIKE ? ESCAPE '\\' "
        f"       OR 供應商全名 ILIKE ? ESCAPE '\\'{alias_sql})"
    )
    params: list = [pattern] * 5 + alias_params
    if lo:
        sql += " AND substr(COALESCE(下單日期, ''), 1, 10) >= ?"
        params.append(lo)
    if hi:
        sql += " AND substr(COALESCE(下單日期, ''), 1, 10) <= ?"
        params.append(hi)
    sql += (" ORDER BY substr(COALESCE(下單日期, ''), 1, 10) DESC, "
            "採購單號 DESC, TRY_CAST(項次 AS INT)")
    try:
        rows = _run(sql, params)
    except Exception as e:  # noqa: BLE001 - 對話面工具，回錯誤字串不 raise
        return f"❌ 採購單查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    period = ""
    if lo or hi:
        period = f"（下單期間 {lo or '…'} ～ {hi or '…'}）"
    if not rows:
        prefix = _alias_note(kw, alias) + "\n" if alias else ""
        return (f"{prefix}查無符合「{_clean(kw)}」的採購單項次{period}。\n"
                "提示：關鍵字可用料號片段、品名、供應商、採購單號或庫存編號"
                "（舊短碼如 SF24）；查庫存結存請用 query_erp_stock。"
                + _stale_hint())

    po_count = len({r[0] for r in rows})
    total_lines = len(rows)
    shown = rows[:_MAX_PO_LINES]

    out = [f"🧾 ERP 採購單「{_clean(kw)}」：{po_count} 張單 / "
           f"{total_lines} 個項次符合{period}（鏡像時間 {_mirror_stamp()}）"]
    if alias:
        out.append(_alias_note(kw, alias))
    if total_lines > _MAX_PO_LINES:
        out.append(f"⚠️ 明細只列最近 {_MAX_PO_LINES} 個項次（合計仍含全部）"
                   "——請用更精確的關鍵字或日期篩選。")
    out.append("")

    for (po_no, seq, ord_date, vendor, item_no, desc, unit, ord_qty,
         rcpt_qty, price, cur, money, plan_date, status) in shown:
        unit_s = _clean(unit)
        cur_s = _clean(cur)
        head = f"▪ {_clean(po_no)} #{_clean(seq)}"
        if ord_date:
            head += f"｜{_clean(ord_date)} 下單"
        if vendor:
            head += f"｜{_clean(vendor)}"
        status_s = _clean(status)
        if status_s:
            head += f"｜{status_s}"
            if status_s == "取消":
                head += "（不計入合計）"
        out.append(head)
        line2 = f"  {_clean(item_no)}"
        if desc:
            line2 += f"｜{_clean(desc)}"
        out.append(line2)
        line3 = (f"  訂購數量 {_fmt_qty(float(ord_qty))} {unit_s}".rstrip()
                 + f"｜已收數量 {_fmt_qty(float(rcpt_qty))} {unit_s}".rstrip()
                 + f"｜單價 {_fmt_qty(float(price))} {cur_s}".rstrip()
                 + f"｜金額 {_fmt_qty(float(money))} {cur_s}".rstrip())
        if plan_date:
            line3 += f"｜計劃到貨 {_clean(plan_date)}"
        out.append(line3)

    # 合計看全部符合列（不只列出的），排除「取消」項次。
    active = [r for r in rows if _clean(r[13]) != "取消"]
    ord_pairs = [(_clean(r[6]), float(r[7])) for r in active]
    rcpt_pairs = [(_clean(r[6]), float(r[8])) for r in active]
    money_pairs = [(_clean(r[10]), float(r[11])) for r in active]
    out.append("")
    out.append("── 合計（排除「取消」項次）──")
    out.append(f"訂購數量 {_sum_by(ord_pairs) or 0}｜已收數量 {_sum_by(rcpt_pairs) or 0}")
    out.append(f"採購金額 {_sum_by(money_pairs) or 0}（⚠️ 金額是錢、不是數量）")
    out.append("")
    out.append("──\n數字直讀 ERP 鏡像（每日凌晨刷新），未經 AI 生成；"
               "要「採購了多少」看「訂購數量」、到貨看「已收數量」，"
               "「金額」是採購金額；單位=採購單位，可能≠庫存單位。\n"
               "⚠️ 合計只含「採購單」金額，**不含**運費等不掛採購單的費用——"
               "問「帳單/請款/應付總額」要用 query_erp_payables（應付請款域）。")
    return "\n".join(out)


# ── BOM 用料查詢（研發 BOM RD_BOM_*，只取生效版）────────────────────────────
_MAX_BOM_MATERIALS = 5     # 反查方向：一次最多展開幾個料號的使用款
_MAX_BOM_MODELS = 4        # 用料方向：一次最多展開幾個型體-顏色的 BOM
_MAX_BOM_LINES = 40        # 用料方向：單一型體-顏色最多列幾項料
_MAX_SIBLINGS = 12         # 反查方向：同型體「不用此料」的顏色最多列幾個

# 型體-顏色（PROD_NO）→ 基底型體：'DJS336189-02BEIGE' / 'DJS336195 GREEN' → 'DJS336189/95'。
_BASE_MODEL_RE = re.compile(r"^[A-Za-z]+\d+")

# 生效版 CTE：每個 型體-顏色×材料類別 取 STATUS='7'(生效) 的最大版次；
# 整組都沒有生效版（新建/歷史）才退回最大版次。舊版用過、新版拿掉的料不能再算「在用」。
_EFF_VER_CTE = (
    "WITH eff AS ("
    "  SELECT PROD_NO, BOM_TYPE, "
    "         COALESCE(MAX(CASE WHEN STATUS = '7' THEN TRY_CAST(BOM_VER AS INT) END), "
    "                  MAX(TRY_CAST(BOM_VER AS INT))) AS ver "
    "  FROM SC00__RD_BOM_M GROUP BY 1, 2"
    ") "
)


def _base_model(prod_no: str) -> str:
    m = _BASE_MODEL_RE.match(prod_no or "")
    return m.group(0) if m else (prod_no or "")


def _fmt_usage(q) -> str:
    """BOM 單位用量常見 0.0449 這種 4 位小數——不能套 _fmt_qty 的 2 位截斷。"""
    try:
        v = float(q)
    except (TypeError, ValueError):
        return ""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _cust_lots_by_prod(prod_nos: list[str]) -> dict[str, str]:
    """PROD_NO → 客戶款號（SE_ORD_ITEM.CUST_LOT，去重排序頓號串接）。查不到=''。"""
    if not prod_nos:
        return {}
    ph = ", ".join("?" for _ in prod_nos)
    rows = _run(
        "SELECT DISTINCT PROD_NO, CUST_LOT FROM SC00__SE_ORD_ITEM "
        f"WHERE PROD_NO IN ({ph}) AND COALESCE(CUST_LOT, '') <> ''",
        list(prod_nos),
    )
    agg: dict[str, set] = {}
    for prod_no, lot in rows:
        agg.setdefault(prod_no, set()).add(_clean(lot))
    return {p: "、".join(sorted(s)) for p, s in agg.items()}


def _vend_label(vend_no, vend_name) -> str:
    name, no = _clean(vend_name), _clean(vend_no)
    if name and no:
        return f"{name}({no})"
    return name or no


def _bom_where_used(kw: str, pattern: str,
                    alias: list[tuple[str, str]] | None = None) -> list[str] | None:
    """反查：料號關鍵字（或庫存編號對應的料號組）→ 哪些型體-顏色在用。查無回 None。"""
    item_cond = "i.ITEM_NO ILIKE ? ESCAPE '\\'"
    params: list = [pattern]
    if alias:
        ph = ", ".join("?" for _ in alias)
        item_cond = f"({item_cond} OR i.ITEM_NO IN ({ph}))"
        params += [i for i, _a in alias]
    rows = _run(
        _EFF_VER_CTE +
        "SELECT i.ITEM_NO, i.PROD_NO, i.BOM_TYPE, COALESCE(bt.NAME_T, ''), "
        "       i.PART_NO, i.UNIT_QTY, COALESCE(i.BOM_UNIT, ''), "
        "       i.VEND_NO, COALESCE(v.SHORTNM_T, ''), e.ver "
        "FROM SC00__RD_BOM_ITEM i "
        "JOIN eff e ON e.PROD_NO = i.PROD_NO AND e.BOM_TYPE = i.BOM_TYPE "
        "          AND TRY_CAST(i.BOM_VER AS INT) = e.ver "
        "LEFT JOIN SY00__CD_CODE bt ON bt.RULE_NO = '1301' "
        "          AND bt.CODE_NO = i.BOM_TYPE AND bt.ORG_ID = i.ORG_ID "
        "LEFT JOIN SC00__PO_VENDER_M v ON v.VEND_NO = i.VEND_NO "
        "          AND v.ORG_ID = i.ORG_ID "
        f"WHERE {item_cond} "
        "ORDER BY i.ITEM_NO, i.PROD_NO, i.BOM_TYPE, i.PART_NO",
        params,
    )
    if not rows:
        return None

    # 料號 → PROD_NO → 該料在此款的 (類別, 部位, 用量, 單位, 供應商, 版次)
    by_item: dict[str, dict[str, list]] = {}
    for item_no, prod_no, _bt, bt_name, part, uqty, unit, vno, vname, ver in rows:
        by_item.setdefault(item_no, {}).setdefault(prod_no, []).append(
            (bt_name, part, uqty, unit, vno, vname, ver))

    total_items = len(by_item)
    truncated = total_items > _MAX_BOM_MATERIALS
    shown_items = sorted(by_item)[:_MAX_BOM_MATERIALS]

    all_users = sorted({p for it in shown_items for p in by_item[it]})
    lots = _cust_lots_by_prod(all_users)

    out = [f"🧵 ERP BOM 反查「{_clean(kw)}」：{total_items} 個料號在生效版 BOM 中使用"
           f"（鏡像時間 {_mirror_stamp()}）"]
    if truncated:
        out.append(f"⚠️ 只展開前 {_MAX_BOM_MATERIALS} 個料號——請用更精確的料號關鍵字。")
    alias_by_item = {i: a for i, a in (alias or [])}
    for item_no in shown_items:
        users = by_item[item_no]
        out.append("")
        code = alias_by_item.get(item_no)
        label = f"{_clean(item_no)}（庫存編號 {_clean(code)}）" if code \
            else _clean(item_no)
        out.append(f"◆ {label} — {len(users)} 個型體-顏色使用：")
        for prod_no in sorted(users):
            lot = lots.get(prod_no, "")
            head = f"▪ {_clean(prod_no)}"
            head += f"｜客戶款號 {lot}" if lot else "｜客戶款號 —"
            out.append(head)
            for bt_name, part, uqty, unit, vno, vname, ver in users[prod_no]:
                seg = [f"  {_clean(bt_name) or '材料'} 部位{_clean(part)}"]
                usage = _fmt_usage(uqty)
                if usage:
                    seg.append(f"用量 {usage} {_clean(unit)}".rstrip())
                vend = _vend_label(vno, vname)
                if vend:
                    seg.append(f"供應商 {vend}")
                seg.append(f"BOM v{_clean(ver)}")
                out.append("｜".join(seg))

    # 防外推區：列「同型體其他顏色**不用**此料」——這一段就是 2026-07-27
    # UserA 案的解（#8916447 被型體級證據外推成用料款）。取所有展開料號的
    # 使用款聯集算 siblings；基底型體太多（寬關鍵字）就不列，避免洗版。
    users = {p for it in shown_items for p in by_item[it]}
    bases = sorted({_base_model(p) for p in users if _base_model(p)})
    if 1 <= len(bases) <= 3:
        like_sql = " OR ".join("PROD_NO LIKE ? ESCAPE '\\'" for _ in bases)
        sib_rows = _run(
            f"SELECT DISTINCT PROD_NO FROM SC00__RD_BOM_M WHERE {like_sql}",
            [f"{_escape_like(b)}%" for b in bases],
        )
        siblings = sorted({r[0] for r in sib_rows} - users)
        if siblings:
            sib_lots = _cust_lots_by_prod(siblings)
            out.append("")
            out.append("同型體「不用」此料的顏色（別把款號外推過去）：")
            for prod_no in siblings[:_MAX_SIBLINGS]:
                lot = sib_lots.get(prod_no, "")
                line = f"  ✗ {_clean(prod_no)}"
                if lot:
                    line += f"｜客戶款號 {lot}"
                out.append(line)
            if len(siblings) > _MAX_SIBLINGS:
                out.append(f"  …（還有 {len(siblings) - _MAX_SIBLINGS} 個顏色）")
    return out


def _bom_materials(kw: str, pattern: str) -> list[str] | None:
    """用料：型體-顏色 / 客戶款號 關鍵字 → 生效版 BOM 逐料明細。查無回 None。"""
    style_rows = _run(
        "SELECT DISTINCT PROD_NO, CUST_LOT FROM SC00__SE_ORD_ITEM "
        "WHERE CUST_LOT ILIKE ? ESCAPE '\\' AND COALESCE(PROD_NO, '') <> ''",
        [pattern],
    )
    model_rows = _run(
        "SELECT DISTINCT PROD_NO FROM SC00__RD_BOM_M "
        "WHERE PROD_NO ILIKE ? ESCAPE '\\'",
        [pattern],
    )
    prods = sorted({r[0] for r in style_rows} | {r[0] for r in model_rows})
    if not prods:
        return None

    total_models = len(prods)
    truncated = total_models > _MAX_BOM_MODELS
    prods = prods[:_MAX_BOM_MODELS]

    ph = ", ".join("?" for _ in prods)
    rows = _run(
        _EFF_VER_CTE +
        "SELECT i.PROD_NO, i.BOM_TYPE, COALESCE(bt.NAME_T, ''), e.ver, "
        "       i.PART_NO, i.ITEM_NO, i.UNIT_QTY, COALESCE(i.BOM_UNIT, ''), "
        "       i.VEND_NO, COALESCE(v.SHORTNM_T, '') "
        "FROM SC00__RD_BOM_ITEM i "
        "JOIN eff e ON e.PROD_NO = i.PROD_NO AND e.BOM_TYPE = i.BOM_TYPE "
        "          AND TRY_CAST(i.BOM_VER AS INT) = e.ver "
        "LEFT JOIN SY00__CD_CODE bt ON bt.RULE_NO = '1301' "
        "          AND bt.CODE_NO = i.BOM_TYPE AND bt.ORG_ID = i.ORG_ID "
        "LEFT JOIN SC00__PO_VENDER_M v ON v.VEND_NO = i.VEND_NO "
        "          AND v.ORG_ID = i.ORG_ID "
        f"WHERE i.PROD_NO IN ({ph}) "
        "ORDER BY i.PROD_NO, i.BOM_TYPE, i.PART_NO, i.ITEM_NO",
        list(prods),
    )
    lots = _cust_lots_by_prod(prods)

    by_prod: dict[str, list] = {}
    for r in rows:
        by_prod.setdefault(r[0], []).append(r[1:])

    out = [f"📋 ERP BOM 用料「{_clean(kw)}」：{total_models} 個型體-顏色符合"
           f"（生效版；鏡像時間 {_mirror_stamp()}）"]
    if truncated:
        out.append(f"⚠️ 只展開前 {_MAX_BOM_MODELS} 個型體-顏色——請用更精確的型體/款號。")
    for prod_no in prods:
        out.append("")
        head = f"▪ {_clean(prod_no)}"
        lot = lots.get(prod_no, "")
        if lot:
            head += f"｜客戶款號 {lot}"
        out.append(head)
        items = by_prod.get(prod_no, [])
        if not items:
            out.append("  （此型體-顏色查無生效版 BOM 明細）")
            continue
        cur_cat = None
        n_shown = 0
        for _bt, bt_name, ver, part, item_no, uqty, unit, vno, vname in items:
            if n_shown >= _MAX_BOM_LINES:
                out.append(f"  …（還有 {len(items) - n_shown} 項，"
                           "請用料號反查或更精確關鍵字）")
                break
            cat = f"{_clean(bt_name) or _clean(_bt)} v{_clean(ver)}"
            if cat != cur_cat:
                out.append(f"  ─ {cat} ─")
                cur_cat = cat
            seg = [f"  {_clean(part)}｜{_clean(item_no)}"]
            usage = _fmt_usage(uqty)
            if usage:
                seg.append(f"{usage} {_clean(unit)}".rstrip())
            vend = _vend_label(vno, vname)
            if vend:
                seg.append(vend)
            out.append("｜".join(seg))
            n_shown += 1
    return out


def erp_bom_lookup(keyword: str) -> str:
    """依 料號 / 型體-顏色 / 客戶款號 關鍵字查生效版研發 BOM。確定性：同輸入必同輸出。

    方向自動判定（兩者都中就兩段都給）：關鍵字命中 型體-顏色(PROD_NO) 或
    客戶款號(CUST_LOT) → 列該款用料；命中 BOM 料號(ITEM_NO) → 反查哪些
    型體-顏色在用。用料是**顏色級**——同型體不同顏色用料不同，反查會附
    「同型體不用此料」清單防外推（見模組 docstring 2026-07-27 案例）。
    """
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return ("用法：輸入至少 2 個字的 料號 / 型體 / 客戶款號 關鍵字"
                "（例：DFDT7100145000-E040、DJS336195、8916446）。")
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    pattern = f"%{_escape_like(kw)}%"
    alias = _alias_expand(kw)
    try:
        sections: list[str] = []
        materials = _bom_materials(kw, pattern)
        if materials:
            sections.extend(materials)
        where_used = _bom_where_used(kw, pattern, alias)
        if where_used:
            if alias:
                where_used.insert(1, _alias_note(kw, alias))
            if sections:
                sections.append("")
            sections.extend(where_used)
    except Exception as e:  # noqa: BLE001 - 對話面工具，回錯誤字串不 raise
        return f"❌ BOM 查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    if not sections:
        prefix = _alias_note(kw, alias) + "\n" if alias else ""
        return (f"{prefix}查無符合「{_clean(kw)}」的 BOM 紀錄（生效版研發 BOM 無此 "
                "料號/型體/客戶款號）。\n提示：料號可用片段（DFDT7100145000）、"
                "型體用生管編號（DJS336195）、款號用客戶 STYLE#（8916446）、"
                "庫存編號用 ERP 舊短碼（SF24）；"
                "查庫存用 query_erp_stock、查採購單用 query_erp_purchase_orders。"
                + _stale_hint())

    sections.append("")
    sections.append("──\n數字直讀 ERP 鏡像（每日凌晨刷新）生效版研發 BOM，未經 AI 生成；"
                    "用料是「型體×顏色」級——同型體不同顏色用料不同，"
                    "回答「哪幾款用某料」以反查清單為準、別從型體外推。"
                    "此處用量是每雙**標準**用量；逐訂單實際需求量/每雙攤提"
                    "（訂單材料追蹤口徑）用 query_erp_order_demand。")
    return "\n".join(sections)


# ── 應付請款查詢（GL00__AP_* 應付域）─────────────────────────────────────────
_MAX_AP_BILLS = 20          # 一次最多列幾張請款單（合計仍含全部符合單）
_MAX_AP_DETAIL_BILLS = 6    # 最多展開幾張單的明細分解（貨款彙總＋費用逐列）
_MAX_AP_FEE_LINES = 8       # 單張單最多列幾筆非採購單費用

# AP_APPLY_M.STATUS 已確認碼值才翻譯，其餘照碼顯示（確定性、不猜語意）。
_AP_STATUS = {"1": "新建", "7": "生效", "99": "結案"}


def _fmt_money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _sum_money_by(pairs: list[tuple[str, float]]) -> str:
    agg: dict[str, float] = {}
    for cur, money in pairs:
        agg[cur] = agg.get(cur, 0.0) + money
    return "、".join(f"{_fmt_money(m)} {c}".rstrip()
                     for c, m in sorted(agg.items()))


def _ap_details(apply_ids: list[str]) -> dict[str, dict]:
    """APPLY_ID → {貨款彙總, 費用逐列}。明細是加值資訊，查壞回空 dict 不擋主結果。"""
    if not apply_ids:
        return {}
    ph = ", ".join("?" for _ in apply_ids)
    try:
        rows = _run(
            "SELECT a.APPLY_ID, COALESCE(dd.PO_ORDERNO, ''), "
            "       COALESCE(dd.ITEM_NAME, ''), "
            "       COALESCE(TRY_CAST(a.AP_MONEY AS DOUBLE), 0) "
            "FROM GL00__AP_APPLY_D a "
            "LEFT JOIN GL00__AP_DUE_D dd "
            "  ON dd.AP_ID = a.ITEM_ID AND dd.ORG_ID = a.ORG_ID "
            f"WHERE a.APPLY_ID IN ({ph})",
            list(apply_ids),
        )
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for apply_id, po_no, item_name, money in rows:
        d = out.setdefault(apply_id, {"goods_n": 0, "goods_sum": 0.0,
                                      "goods_pos": set(), "fees": []})
        po = _clean(po_no)
        if po and po != "0":
            d["goods_n"] += 1
            d["goods_sum"] += float(money or 0)
            d["goods_pos"].add(po)
        else:
            d["fees"].append((_clean(item_name) or "（未填品名）", float(money or 0)))
    return out


def erp_ap_lookup(keyword: str, *, date_from: str = "", date_to: str = "") -> str:
    """依供應商/請示單號/備註關鍵字查應付請款單（帳單）＋合計。確定性：同輸入必同輸出。

    「帳單/請款/應付 總金額」的正解——NET_MONEY（應付淨額含稅）含運費等
    **不掛採購單的費用**，採購單金額加總看不到（見模組 docstring 三寶案）。
    明細分解：貨款彙總一行＋非採購單費用逐列明示。
    """
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return ("用法：輸入至少 2 個字的 供應商/請示單號/備註 關鍵字"
                "（例：三寶、H1L10001、JFPA2605007）；可加 date_from/date_to"
                "（YYYY 或 YYYY-MM-DD）篩申請日期。")
    lo = _normalize_date_arg(date_from, is_to=False)
    hi = _normalize_date_arg(date_to, is_to=True)
    if lo is None or hi is None:
        return "日期格式錯誤：date_from/date_to 要是 YYYY、YYYY-MM 或 YYYY-MM-DD。"
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    pattern = f"%{_escape_like(kw)}%"
    sql = (
        "SELECT m.APPLY_ID, m.APPLY_NO, substr(COALESCE(m.APPLY_DATE, ''), 1, 10), "
        "       COALESCE(m.PAY_PERIOD, ''), m.VEND_NO, "
        "       COALESCE(v.SHORTNM_T, v.FULLNM_T, ''), "
        "       COALESCE(m.MONEY_UNIT, ''), "
        "       COALESCE(TRY_CAST(m.NET_MONEY AS DOUBLE), 0), "
        "       COALESCE(m.STATUS, ''), COALESCE(m.REMARK, '') "
        "FROM GL00__AP_APPLY_M m "
        "LEFT JOIN SC00__PO_VENDER_M v "
        "  ON v.VEND_NO = m.VEND_NO AND v.ORG_ID = m.ORG_ID "
        "WHERE (m.APPLY_NO ILIKE ? ESCAPE '\\' OR m.VEND_NO ILIKE ? ESCAPE '\\' "
        "       OR v.SHORTNM_T ILIKE ? ESCAPE '\\' OR v.FULLNM_T ILIKE ? ESCAPE '\\' "
        "       OR m.REMARK ILIKE ? ESCAPE '\\')"
    )
    params: list = [pattern] * 5
    if lo:
        sql += " AND substr(COALESCE(m.APPLY_DATE, ''), 1, 10) >= ?"
        params.append(lo)
    if hi:
        sql += " AND substr(COALESCE(m.APPLY_DATE, ''), 1, 10) <= ?"
        params.append(hi)
    sql += (" ORDER BY substr(COALESCE(m.APPLY_DATE, ''), 1, 10) DESC, "
            "m.APPLY_NO DESC")
    try:
        rows = _run(sql, params)
    except Exception as e:  # noqa: BLE001 - 對話面工具，回錯誤字串不 raise
        return f"❌ 應付請款查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    period = ""
    if lo or hi:
        period = f"（申請日期 {lo or '…'} ～ {hi or '…'}）"
    if not rows:
        return (f"查無符合「{_clean(kw)}」的應付請款單{period}。\n"
                "提示：關鍵字可用供應商簡稱/全名/代號、請示單號（JFPA…）或備註；"
                "查採購單數量/金額用 query_erp_purchase_orders。" + _stale_hint())

    total_bills = len(rows)
    shown = rows[:_MAX_AP_BILLS]
    details = _ap_details([r[0] for r in shown[:_MAX_AP_DETAIL_BILLS]])

    out = [f"💰 ERP 應付請款「{_clean(kw)}」：{total_bills} 張請示單符合{period}"
           f"（鏡像時間 {_mirror_stamp()}）"]
    if total_bills > _MAX_AP_BILLS:
        out.append(f"⚠️ 明細只列最近 {_MAX_AP_BILLS} 張（合計仍含全部）"
                   "——請用更精確的關鍵字或日期篩選。")
    out.append("")

    for (apply_id, apply_no, adate, pay_period, vend_no, vend_name,
         cur, net, status, remark) in shown:
        cur_s = _clean(cur)
        head = f"▪ {_clean(apply_no)}"
        if adate:
            head += f"｜{_clean(adate)} 申請"
        if pay_period:
            head += f"｜付款年月 {_clean(pay_period)}"
        vend = _vend_label(vend_no, vend_name)
        if vend:
            head += f"｜{vend}"
        status_s = _clean(status)
        head += f"｜{_AP_STATUS.get(status_s, '狀態' + status_s)}"
        out.append(head)
        out.append(f"  應付淨額(含稅) {_fmt_money(net)} {cur_s}".rstrip())
        if remark:
            out.append(f"  備註：{_clean(remark)[:80]}")
        d = details.get(apply_id)
        if d:
            if d["goods_n"]:
                pos = "、".join(sorted(d["goods_pos"])[:5])
                more = f" 等 {len(d['goods_pos'])} 張" if len(d["goods_pos"]) > 5 else ""
                out.append(f"  貨款 {d['goods_n']} 筆合計 {_fmt_money(d['goods_sum'])}"
                           f" {cur_s}（採購單 {pos}{more}）".rstrip())
            for name, money in d["fees"][:_MAX_AP_FEE_LINES]:
                out.append(f"  ＋費用（不掛採購單）：{name} {_fmt_money(money)} {cur_s}".rstrip())
            if len(d["fees"]) > _MAX_AP_FEE_LINES:
                out.append(f"  …（還有 {len(d['fees']) - _MAX_AP_FEE_LINES} 筆費用）")

    money_pairs = [(_clean(r[6]), float(r[7] or 0)) for r in rows]
    out.append("")
    out.append("── 合計（全部符合單）──")
    out.append(f"應付淨額 {_sum_money_by(money_pairs) or 0}")
    out.append("")
    out.append("──\n數字直讀 ERP 鏡像（每日凌晨刷新）應付請款單，未經 AI 生成；"
               "「帳單/請款/應付總額」以本表 NET_MONEY 為準（含運費等非採購單費用）；"
               "採購單金額加總不含這些費用，兩邊對不上是正常的。")
    return "\n".join(out)


# ── 訂單實際需求查詢（SC00__SE_ITEMSCHE_M 訂單材料追蹤）──────────────────────
#
# erp_demand_lookup 的由來：2026-07-29 G407 案——問「G407 用在哪個 STYLE#」，
# freeform 走 RAG 讀 Drive BOM 成本表回單一標準用量 0.05268 米/雙；大王要的是
# ERP 訂單材料追蹤（SE-SETF_440）的**逐訂單實際需求數量**與每雙攤提
# （需求數量÷訂單雙數，如 12.04/221≈0.06、1.14/32≈0.04，無條件進位）。
# 需求數量是 ERP 依尺寸配比＋損耗展算的，≠BOM 標準用量×雙數。
#
# 資料陷阱（實測 G407）：同一筆需求會**同時**掛在裸碼（-G050）與 +EP 版次料號
# （-G050-005）兩列、數值相同——逐單彙總要取「裸碼列 vs 版次列合計」較大者，
# 直接 SUM 會翻倍。領料出庫兩列可能不同（裸碼列才含全部出庫），GREATEST 通吃。
_MAX_DEMAND_FAMILIES = 3   # 一次最多展開幾個料號家族（寬關鍵字截斷明講）
_MAX_DEMAND_STYLES = 6     # 單一家族最多列幾個 STYLE#×型體 分組
_MAX_DEMAND_ORDERS = 8     # 單一分組最多逐列幾張訂單（合計仍含全部）

# 訂單項狀態（SE_ORD_ITEM.STATUS，碼值同 v_orders）；只列生效、其餘計數註記。
_SE_STATUS = {"1": "新單", "7": "生效", "25": "完工", "29": "出貨",
              "99": "銷貨", "0": "取消"}

# +EP 版次料號尾碼是「-三位數字」（-005＝G407+EP5）；色碼尾段（-G050）是字母
# 開頭不受影響。歸戶到裸碼家族後才彙總。
_FAMILY_SQL = "regexp_replace({col}, '-[0-9]{{3}}$', '')"


def _ceil2(v: float) -> float:
    """ERP 畫面口徑：需求÷訂單雙數 無條件進位到小數 2 位（G407 案 0.06/0.04）。"""
    import math
    return math.ceil(round(v * 100, 6)) / 100


def _demand_units_by_family(fams: list[str]) -> dict[str, str]:
    """家族 → 單位標示（IV_STOC_M.Q_UNIT）。主檔家族內 M/Y 混用時明講、不硬猜。"""
    if not fams:
        return {}
    ph = ", ".join("?" for _ in fams)
    fam_col = _FAMILY_SQL.format(col="ITEM_NO")
    rows = _run(
        f"SELECT {fam_col}, COALESCE(Q_UNIT, '') FROM SC00__IV_STOC_M "
        f"WHERE {fam_col} IN ({ph}) GROUP BY 1, 2",
        list(fams))
    by_fam: dict[str, set] = {}
    for fam, unit in rows:
        u = _clean(unit)
        if u:
            by_fam.setdefault(fam, set()).add(u)
    out = {}
    for fam, units in by_fam.items():
        if len(units) == 1:
            out[fam] = next(iter(units))
        else:
            out[fam] = "/".join(sorted(units)) + "（主檔混用，以 ERP 畫面為準）"
    return out


def _order_bom_usage_by_style(fams: list[str]) -> dict[tuple, str]:
    """(家族, 款號, 型體) → 訂單 BOM 每雙用量（取眾數）。

    來源 SE_BOM_PART（訂單 BOM）：同料多部位（A025＋A025.1）要相加；裸碼/版次
    雙記與同單重複列以 DISTINCT(單, 部位, 用量) 去重；跨訂單取最常見值當代表
    （BOM 改版時各單可能不同，眾數最能代表現行）。查不到＝{}，加值路徑不擋主查詢。
    """
    if not fams:
        return {}
    ph = ", ".join("?" for _ in fams)
    fam_col = _FAMILY_SQL.format(col="p.ITEM_NO")
    rows = _run(
        f"SELECT DISTINCT {fam_col}, COALESCE(oi.CUST_LOT, ''), oi.PROD_NO, "
        "        p.SE_ID, p.PART_NO, TRY_CAST(p.UNIT_QTY AS DOUBLE) "
        "FROM SC00__SE_BOM_PART p "
        "JOIN SC00__SE_ORD_ITEM oi "
        "  ON oi.SE_ID = p.SE_ID AND oi.SE_SEQ = p.SE_SEQ "
        f"WHERE {fam_col} IN ({ph})",
        list(fams))
    per_order: dict[tuple, float] = {}
    for fam, lot, prod, se_id, _part, uqty in rows:
        key = (fam, lot, prod, se_id)
        per_order[key] = per_order.get(key, 0.0) + float(uqty or 0)
    votes: dict[tuple, dict[float, int]] = {}
    for (fam, lot, prod, _se_id), usage in per_order.items():
        u = round(usage, 4)
        if u > 0:
            bucket = votes.setdefault((fam, lot, prod), {})
            bucket[u] = bucket.get(u, 0) + 1
    return {key: _fmt_usage(max(bucket, key=bucket.get))
            for key, bucket in votes.items()}


def erp_demand_lookup(keyword: str) -> str:
    """依 料號/庫存編號 查訂單實際材料需求（訂單材料追蹤）。確定性：同輸入必同輸出。

    逐**生效**訂單列 需求數量/領料出庫/訂單雙數/每雙攤提（＝需求÷雙數，另附
    ERP 採購習慣的無條件進位值），依 STYLE#×型體分組小計，並附訂單 BOM 每雙
    用量對照。口徑同 ERP 訂單材料追蹤畫面（SE-SETF_440，預設 7-生效）。
    """
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return ("用法：輸入至少 2 個字的 料號 或 庫存編號（ERP 舊短碼）"
                "（例：DFD00400D05700-G050、G407）。")
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    pattern = f"%{_escape_like(kw)}%"
    alias = _alias_expand(kw)
    fam_expr = _FAMILY_SQL.format(col="ITEM_NO")
    try:
        # 料號家族：關鍵字直配料號，或庫存編號對照出的料號歸戶家族。
        cond = "ITEM_NO ILIKE ? ESCAPE '\\'"
        params: list = [pattern]
        alias_fams = sorted({re.sub(r"-[0-9]{3}$", "", i) for i, _a in alias})
        if alias_fams:
            ph = ", ".join("?" for _ in alias_fams)
            cond = f"({cond} OR {fam_expr} IN ({ph}))"
            params += alias_fams
        fam_rows = _run(
            f"SELECT DISTINCT {fam_expr} FROM SC00__SE_ITEMSCHE_M WHERE {cond} "
            "ORDER BY 1", params)
        fams = [r[0] for r in fam_rows]
        total_fams = len(fams)
        fams = fams[:_MAX_DEMAND_FAMILIES]

        if not fams:
            prefix = _alias_note(kw, alias) + "\n" if alias else ""
            return (f"{prefix}查無符合「{_clean(kw)}」的訂單材料需求紀錄"
                    "（訂單材料追蹤無此料號/庫存編號）。\n提示：料號可用片段"
                    "（DFD00400D05700）、庫存編號用 ERP 舊短碼（G407）；"
                    "查 BOM 標準用量用 query_erp_bom、查庫存用 query_erp_stock。"
                    + _stale_hint())

        # 逐單需求：裸碼列 vs 版次列合計 取大者（雙記去重，見上方註解）。
        ph = ", ".join("?" for _ in fams)
        s_fam = _FAMILY_SQL.format(col="s.ITEM_NO")
        rows = _run(
            "WITH fam_rows AS ("
            f"  SELECT {s_fam} AS fam, s.SE_ID, s.SE_SEQ, "
            f"         CASE WHEN s.ITEM_NO = {s_fam} THEN 1 ELSE 0 END AS is_base, "
            "         TRY_CAST(s.NEED_QTY AS DOUBLE) AS need, "
            "         TRY_CAST(s.ISSUE_QTY AS DOUBLE) AS issue "
            "  FROM SC00__SE_ITEMSCHE_M s "
            f"  WHERE {s_fam} IN ({ph})"
            "), per_order AS ("
            "  SELECT fam, SE_ID, SE_SEQ, "
            "         GREATEST(COALESCE(MAX(CASE WHEN is_base = 1 THEN need END), 0), "
            "                  COALESCE(SUM(CASE WHEN is_base = 0 THEN need END), 0)) AS need, "
            "         GREATEST(COALESCE(MAX(CASE WHEN is_base = 1 THEN issue END), 0), "
            "                  COALESCE(SUM(CASE WHEN is_base = 0 THEN issue END), 0)) AS issue "
            "  FROM fam_rows GROUP BY 1, 2, 3"
            ") "
            "SELECT p.fam, p.SE_ID, p.need, p.issue, "
            "       TRY_CAST(oi.SE_QTY AS BIGINT), COALESCE(oi.PROD_NO, ''), "
            "       COALESCE(oi.CUST_LOT, ''), COALESCE(oi.STATUS, '') "
            "FROM per_order p "
            "JOIN SC00__SE_ORD_ITEM oi "
            "  ON oi.SE_ID = p.SE_ID AND oi.SE_SEQ = p.SE_SEQ "
            "ORDER BY p.fam, p.SE_ID",
            list(fams))

        units = _demand_units_by_family(fams)
        bom_usage = _order_bom_usage_by_style(fams)
    except Exception as e:  # noqa: BLE001 - 對話面工具，回錯誤字串不 raise
        return f"❌ 訂單需求查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    # 家族 → (款號, 型體) → 生效訂單列；非生效只計數。
    active: dict[str, dict[tuple, list]] = {}
    inactive: dict[str, dict[str, int]] = {}
    n_active = 0
    for fam, se_id, need, issue, qty, prod, lot, status in rows:
        if status == "7":
            active.setdefault(fam, {}).setdefault((lot, prod), []).append(
                (se_id, float(need or 0), float(issue or 0), qty))
            n_active += 1
        else:
            label = _SE_STATUS.get(_clean(status), "碼" + _clean(status))
            fam_counts = inactive.setdefault(fam, {})
            fam_counts[label] = fam_counts.get(label, 0) + 1

    alias_by_item = {i: a for i, a in alias}
    out = [f"📐 ERP 訂單實際需求「{_clean(kw)}」：{total_fams} 個料號家族、"
           f"生效訂單 {n_active} 張（訂單材料追蹤口徑；鏡像時間 {_mirror_stamp()}）"]
    if alias:
        out.append(_alias_note(kw, alias))
    if total_fams > _MAX_DEMAND_FAMILIES:
        out.append(f"⚠️ 只展開前 {_MAX_DEMAND_FAMILIES} 個料號家族"
                   "——請用更精確的料號/庫存編號。")

    for fam in fams:
        out.append("")
        code = alias_by_item.get(fam)
        head = f"◆ {_clean(fam)} 家族"
        head += f"（庫存編號 {_clean(code)}）" if code else ""
        unit = units.get(fam, "")
        if unit:
            head += f"｜單位 {unit}"
        out.append(head)

        groups = active.get(fam, {})
        if not groups:
            out.append("  （無生效訂單）")
        styles = sorted(groups, key=lambda k: -sum(o[1] for o in groups[k]))
        if len(styles) > _MAX_DEMAND_STYLES:
            out.append(f"  ⚠️ 只列需求前 {_MAX_DEMAND_STYLES} 個款式分組"
                       f"（共 {len(styles)} 組）。")
            styles = styles[:_MAX_DEMAND_STYLES]
        for lot, prod in styles:
            orders = groups[(lot, prod)]
            t_need = sum(o[1] for o in orders)
            t_issue = sum(o[2] for o in orders)
            t_qty = sum(o[3] or 0 for o in orders)
            head = f"▪ STYLE# {_clean(lot) or '—'}｜{_clean(prod) or '—'}"
            head += f"｜生效 {len(orders)} 單、{_fmt_qty(t_qty)} 雙"
            out.append(head)
            agg = [f"  需求合計 {_fmt_qty(t_need)}", f"領料合計 {_fmt_qty(t_issue)}"]
            if t_qty:
                agg.append(f"實攤 {_fmt_usage(t_need / t_qty)}/雙")
            std = bom_usage.get((fam, lot, prod))
            if std:
                agg.append(f"訂單BOM {std}/雙")
            out.append("｜".join(agg))
            for se_id, need, issue, qty in sorted(
                    orders, key=lambda o: -o[1])[:_MAX_DEMAND_ORDERS]:
                seg = [f"  {_clean(se_id)}｜{_fmt_qty(qty or 0)}雙",
                       f"需 {_fmt_qty(need)}", f"領 {_fmt_qty(issue)}"]
                if qty:
                    per = need / qty
                    seg.append(f"攤 {_fmt_usage(per)}"
                               f"（進位 {_fmt_qty(_ceil2(per))}）")
                out.append("｜".join(seg))
            if len(orders) > _MAX_DEMAND_ORDERS:
                out.append(f"  …（還有 {len(orders) - _MAX_DEMAND_ORDERS} 單，"
                           "已含在合計）")
        others = inactive.get(fam)
        if others:
            counts = "、".join(f"{label} {n} 單" for label, n in
                               sorted(others.items(), key=lambda kv: -kv[1]))
            out.append(f"  另有非生效訂單未列：{counts}"
                       "（同 ERP 畫面預設只看 7-生效）")

    out.append("")
    out.append("──\n數字直讀 ERP 鏡像（每日凌晨刷新）訂單材料追蹤，未經 AI 生成；"
               "需求數量是 ERP 依尺寸配比＋損耗展算的訂單實際需求，≠BOM 標準"
               "用量×雙數；「攤」＝需求÷訂單雙數（進位＝無條件進位至小數 2 位，"
               "採購習慣口徑）。缺不缺料/短少判定用 kitting_check；"
               "BOM 標準用量用 query_erp_bom。")
    return "\n".join(out)


# ── 庫存分配紀錄查詢（庫存可用量分配 PO-POTF_240；SC00__PO_ITEM_SELOT）──────
#
# erp_allocation_lookup 的由來：2026-07-30 UserA G407 案五——問「G407 2026/7/30
# 有分配數量給哪一張指令訂單」，freeform 手上只有庫存批次面（預購批 J0C26070005
# 11.19M）與訂單需求面（JFC26563 需求 11.19），Gemini 見數量對得上就自行縫合
# 「已分配給 JFC26563」，並把使用者問的日期當成分配日期回報；實際 JFC26563 是
# 2026-07-23 分配的舊紀錄，07-30 當天 11:41 新建的分配是 JFC26574（11.01Y）。
# 「哪張單、何時分配」的權威源是庫存可用量分配表，從此走這裡照表念。
#
# 表語意（2026-07-31 對 47,908 列快照實測）：一列＝一筆「庫存→指令訂單」指派；
# STATUS='1'（畫面類別 1-預購）時 STOC_NO 欄放**轉出批號**（J0C…，26,357/26,357
# 列皆然）、'3'/'9' 時放倉別（RFW/UMW/PW…，畫面類別名未在 CD_CODE 字典、照碼
# 列出）；LOT_DATE＝分配日期時刻；裸碼與 +EP 版次料號會雙記同一筆（同
# LOT_SEQ×訂單×數量×時刻，快照實測 1,198 對）——歸戶列一次、雙碼並列。
_MAX_ALLOC_FAMILIES = 3    # 一次最多展開幾個料號家族（寬關鍵字截斷明講）
_MAX_ALLOC_LINES = 20      # 單一家族最多逐列幾筆分配（其餘計數註記）

_ALLOC_STATUS = {"1": "1-預購"}   # 僅 '1' 有畫面實證名稱，其餘照碼列出


def _alloc_stamp() -> str:
    """分配表（PO_ITEM_SELOT）自己的鏡像時戳。

    2026-07-31 前該表不在每日熱表名單（凍在初鏡快照），時戳可能明顯落後
    DB mtime——照 manifest 念、不冒充整庫時間（同 _sample_bom_stamp 手法）。
    """
    import json
    try:
        path = os.path.join(os.path.dirname(_db_path()), "manifest.json")
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
        ts = (man.get("SC00.PO_ITEM_SELOT") or {}).get("ts") or ""
        if ts:
            return str(ts)[:16]
    except Exception:  # noqa: BLE001 - 時戳是加值資訊，讀不到不擋查詢
        pass
    return _mirror_stamp()


def erp_allocation_lookup(keyword: str, *, date_from: str = "",
                          date_to: str = "") -> str:
    """依 料號/庫存編號/指令訂單號/批號 查庫存分配紀錄。確定性：同輸入必同輸出。

    逐筆列 分配日期/指令訂單/本次分配數/轉出批號或倉別/類別，口徑同 ERP
    庫存可用量分配畫面（PO-POTF_240）。date_from/date_to 篩**分配日期**。
    """
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return ("用法：輸入至少 2 個字的 料號/庫存編號/指令訂單號/批號"
                "（例：G407、DFD00400D05700-G050、JFC26574、J0C26070005）；"
                "可加 date_from/date_to（YYYY、YYYY-MM 或 YYYY-MM-DD）篩分配日期。")
    lo = _normalize_date_arg(date_from, is_to=False)
    hi = _normalize_date_arg(date_to, is_to=True)
    if lo is None or hi is None:
        return "日期格式錯誤：date_from/date_to 要是 YYYY、YYYY-MM 或 YYYY-MM-DD。"
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    pattern = f"%{_escape_like(kw)}%"
    alias = _alias_expand(kw)
    fam_expr = _FAMILY_SQL.format(col="ITEM_NO")
    cond = ("(ITEM_NO ILIKE ? ESCAPE '\\' OR SE_ID ILIKE ? ESCAPE '\\' "
            "OR STOC_NO ILIKE ? ESCAPE '\\')")
    params: list = [pattern, pattern, pattern]
    alias_fams = sorted({re.sub(r"-[0-9]{3}$", "", i) for i, _a in alias})
    if alias_fams:
        ph = ", ".join("?" for _ in alias_fams)
        cond = f"({cond} OR {fam_expr} IN ({ph}))"
        params += alias_fams
    sql = (
        f"SELECT {fam_expr}, ITEM_NO, COALESCE(STOC_NO, ''), "
        "       COALESCE(SE_ID, ''), COALESCE(SE_SEQ, ''), COALESCE(LOT_SEQ, ''), "
        "       COALESCE(TRY_CAST(LOT_QTY AS DOUBLE), 0), COALESCE(LOT_DATE, ''), "
        "       COALESCE(STATUS, ''), COALESCE(MOVE_MARK, ''), "
        "       COALESCE(MOVE_NO, ''), COALESCE(CANCEL_MARK, '') "
        f"FROM SC00__PO_ITEM_SELOT WHERE {cond}"
    )
    if lo:
        sql += " AND substr(COALESCE(LOT_DATE, ''), 1, 10) >= ?"
        params.append(lo)
    if hi:
        sql += " AND substr(COALESCE(LOT_DATE, ''), 1, 10) <= ?"
        params.append(hi)
    sql += " ORDER BY COALESCE(LOT_DATE, '') DESC, SE_ID, ITEM_NO"
    try:
        raw = _run(sql, params)
        # 裸碼/+EP 版次雙記歸戶：同 家族×批序×訂單×時刻×數量 只列一次、雙碼並列。
        merged: dict[tuple, dict] = {}
        order: list[tuple] = []
        for fam, item, stoc, se_id, se_seq, lot_seq, qty, dt, st, mv, mvno, cx in raw:
            key = (fam, lot_seq, se_id, se_seq, dt, qty)
            rec = merged.get(key)
            if rec is None:
                merged[key] = {"fam": fam, "items": [item], "stoc": stoc,
                               "se_id": se_id, "se_seq": se_seq, "qty": qty,
                               "dt": dt, "st": st, "mv": mv, "mvno": mvno,
                               "cx": cx}
                order.append(key)
            else:
                if item not in rec["items"]:
                    rec["items"].append(item)
                if rec["st"] == "9" and st != "9":  # 鏡射列不搶類別碼
                    rec["st"] = st
        fams_all = sorted({merged[k]["fam"] for k in order})
        units = _demand_units_by_family(fams_all[:_MAX_ALLOC_FAMILIES])
    except Exception as e:  # noqa: BLE001 - 對話面工具，回錯誤字串不 raise
        return f"❌ 庫存分配查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    span = ""
    if lo or hi:
        span = f"｜分配日期 {lo or '…'}～{hi or '…'}" if lo != hi else f"｜分配日期 {lo}"

    if not order:
        prefix = _alias_note(kw, alias) + "\n" if alias else ""
        span_note = f"（{span[1:]}）" if span else ""
        return (f"{prefix}查無符合「{_clean(kw)}」的庫存分配紀錄{span_note}"
                f"（庫存可用量分配表無此 料號/訂單/批號；"
                f"分配表鏡像時間 {_alloc_stamp()}）。\n"
                "提示：料號可用片段、庫存編號用 ERP 舊短碼（G407）、"
                "訂單用指令單號（JFC26574）、批號用預購批（J0C26070005）；"
                "批次現況/可用量用 query_erp_stock。" + _stale_hint())

    by_fam: dict[str, list[dict]] = {}
    for key in order:
        rec = merged[key]
        by_fam.setdefault(rec["fam"], []).append(rec)
    fams = fams_all[:_MAX_ALLOC_FAMILIES]

    # 家族→庫存編號標示：多個 alias 歸同家族時（裸碼 G407＋版次 G407+EP5）
    # 優先取與關鍵字相同的那個，別讓版次碼蓋掉使用者查的短碼。
    alias_by_item: dict[str, str] = {}
    for i, a in alias:
        fam_key = re.sub(r"-[0-9]{3}$", "", i)
        if (fam_key not in alias_by_item
                or _clean(a).upper() == _clean(kw).upper()):
            alias_by_item[fam_key] = a
    out = [f"📤 ERP 庫存分配紀錄「{_clean(kw)}」：{len(order)} 筆、"
           f"{len(fams_all)} 個料號家族{span}"
           f"（庫存可用量分配口徑；分配表鏡像時間 {_alloc_stamp()}）"]
    if alias:
        out.append(_alias_note(kw, alias))
    if len(fams_all) > _MAX_ALLOC_FAMILIES:
        out.append(f"⚠️ 只展開前 {_MAX_ALLOC_FAMILIES} 個料號家族"
                   "——請用更精確的料號/庫存編號。")

    for fam in fams:
        out.append("")
        head = f"◆ {_clean(fam)} 家族"
        code = alias_by_item.get(fam)
        if code:
            head += f"（庫存編號 {_clean(code)}）"
        unit = units.get(fam, "")
        if unit:
            head += f"｜單位 {unit}"
        out.append(head)
        recs = by_fam.get(fam, [])
        for rec in recs[:_MAX_ALLOC_LINES]:
            day = _clean(rec["dt"])[:16] or "—"
            se = _clean(rec["se_id"]) or "—"
            seq = _clean(rec["se_seq"])
            se_lbl = f"訂單 {se}(序{seq})" if seq else f"訂單 {se}"
            st = _clean(rec["st"])
            if st == "1":
                src = f"轉出批號 {_clean(rec['stoc'])}"
            else:
                src = f"自倉 {_clean(rec['stoc'])}"
            cat = _ALLOC_STATUS.get(st, f"類別碼{st}" if st else "類別碼—")
            seg = [f"  {day}", se_lbl, f"分配 {_fmt_qty(rec['qty'])}", src, cat]
            if len(rec["items"]) > 1:
                seg.append("雙記歸戶 " + "/".join(_clean(i) for i in rec["items"]))
            if _clean(rec["cx"]) == "Y":
                seg.append("⚠️已取消")
            if _clean(rec["mv"]) == "Y":
                seg.append("調撥" + (f" {_clean(rec['mvno'])}"
                                     if _clean(rec["mvno"]) else ""))
            out.append("｜".join(seg))
        if len(recs) > _MAX_ALLOC_LINES:
            out.append(f"  …（還有 {len(recs) - _MAX_ALLOC_LINES} 筆，"
                       "可加 date_from/date_to 縮小範圍）")

    out.append("")
    out.append("──\n數字直讀 ERP 鏡像（每日凌晨刷新）庫存可用量分配表，未經 AI "
               "生成；「分配」＝ERP 把庫存/預購量指派給指令訂單的紀錄——**分配"
               "日期以此表為準，別拿批號/需求數字對得上就推論是哪天/哪張單**。"
               "類別 1-預購 的「轉出批號」即綁單預購批（J0C…）；類別碼 3/9 為"
               "自倉別分配（畫面名稱未在代碼字典，照碼列出）；裸碼與 +EP 版次"
               "料號雙記已歸戶。⚠️ 鏡像是快照：**今天剛在 ERP 做的分配要明晨"
               "刷新後才查得到**，問「今天」請以 ERP 畫面為準。批次現況/可用量"
               "用 query_erp_stock；訂單需求/攤提用 query_erp_order_demand；"
               "缺料判定用 kitting_check。")
    return "\n".join(out)


# ── 樣品單查詢（樣品開發 SP00__SP_PROD_SP 主檔＋SP_PROD_SPBOM 樣品 BOM）──────
#
# erp_sample_lookup 的由來：2026-07-29 UserC PU468 案——問「PU468 的使用 SR
# 樣品單號」，freeform 手上只有研發 BOM/訂單/採購面工具（SP00 樣品模組是
# 盲區），LLM 拿真實存在的 J0M 備料採購單斷言「ERP 無獨立 SR 樣品單」＝幻覺；
# 實際 SP_PROD_SPBOM 有 98 列、4 張 SR 單（SR2511001/2、SR2601001/2）在用
# 該料。樣品單用料從此走這裡照表念。
#
# 表關係（見 var/data/erp_mirror/DATA_DICTIONARY.md）：SP_PROD_SP 一列＝一個
# 樣品單號(SAMPLE_NO，SR 開頭為主)×鞋款配色(PROD_NO)×版次(VER)，(PROD_NO,VER)
# → SAMPLE_NO 唯一；SP_PROD_SPBOM 一列＝鞋款×版次×部位的一項用料。樣品是
# 迭代開發、一款動輒 20+ 版，反查以「快照最新版」為主口徑並附含此料版數。
_MAX_SAMPLE_MATERIALS = 5   # 反查：一次最多展開幾個料號
_MAX_SAMPLE_USERS = 12      # 反查：單一料號最多列幾組 樣品單×鞋款
_MAX_SAMPLE_PRODS = 4       # 樣品單/鞋款方向：一次最多展開幾組 樣品單×鞋款
_MAX_SAMPLE_BOM_LINES = 40  # 單一鞋款最新版 BOM 最多列幾項料
_MAX_SAMPLE_PARTS = 3       # 反查：單一組最多列幾個使用部位


def _sample_bom_stamp() -> str:
    """樣品 BOM（SP_PROD_SPBOM）自己的鏡像時戳。

    43.6 萬列大表曾長期不在每日熱表名單（一次性快照），時戳可能明顯落後
    DB mtime——照 manifest 念、不冒充整庫時間。讀不到退回整庫 mtime。
    """
    import json
    try:
        path = os.path.join(os.path.dirname(_db_path()), "manifest.json")
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
        ts = (man.get("SP00.SP_PROD_SPBOM") or {}).get("ts") or ""
        if ts:
            return str(ts)[:16]
    except Exception:  # noqa: BLE001 - 時戳是加值資訊，讀不到不擋查詢
        pass
    return _mirror_stamp()


def _sample_custs(sample_nos: list[str]) -> dict[str, str]:
    """SAMPLE_NO → 客戶/品牌（SP_EXCEL_M，樣品室 excel 匯出）。

    該表非全量（僅涵蓋部分樣品單），查不到＝''——SP00 主檔無客戶欄，
    這是樣品域唯一的客戶名來源，能補多少補多少。
    """
    if not sample_nos:
        return {}
    ph = ", ".join("?" for _ in sample_nos)
    try:
        rows = _run(
            "SELECT DISTINCT SAMPLE_NO, COALESCE(CUSTNM, ''), "
            "       COALESCE(BRANDNM, '') "
            f"FROM SP00__SP_EXCEL_M WHERE SAMPLE_NO IN ({ph})",
            list(sample_nos))
    except Exception:  # noqa: BLE001 - 表缺席（舊鏡像）不影響主查詢
        return {}
    agg: dict[str, set] = {}
    for sno, cust, brand in rows:
        label = "/".join(x for x in (_clean(cust), _clean(brand)) if x)
        if label:
            agg.setdefault(sno, set()).add(label)
    return {s: "、".join(sorted(v)) for s, v in agg.items()}


def _sample_sources(item_nos: list[str]) -> dict[str, list[str]] | None:
    """料號 → 料品主檔「來源樣品單號」（SP00__SP_ITEM_RDITEM.SRC_SPNO 照表念）。

    這張開發料↔量產料對照表是「來源樣品單號」欄的唯一權威源（2026-07-30
    sysdba GRANT 後入鏡像；PU468→SR2511002 已實證）。表不可用（舊鏡像／
    授權被撤→退出鏡像）回 None——呼叫端退回「開 ERP 畫面」提示；表可用但
    料號無紀錄＝該欄未記載（dict 缺 key），照實講、不拿使用清單冒充。
    """
    if not item_nos:
        return {}
    ph = ", ".join("?" for _ in item_nos)
    try:
        rows = _run(
            "SELECT DISTINCT SP_ITEMNO, RD_ITEMNO, SRC_SPNO "
            "FROM SP00__SP_ITEM_RDITEM "
            f"WHERE (SP_ITEMNO IN ({ph}) OR RD_ITEMNO IN ({ph})) "
            "AND COALESCE(SRC_SPNO, '') <> ''",
            list(item_nos) + list(item_nos))
    except Exception:  # noqa: BLE001 - 表缺席（舊鏡像/撤權），退回提示路徑
        return None
    wanted = set(item_nos)
    agg: dict[str, set] = {}
    for sp_no, rd_no, src in rows:
        for k in {sp_no, rd_no} & wanted:
            agg.setdefault(k, set()).add(_clean(src))
    return {k: sorted(v) for k, v in agg.items()}


def _sample_where_used(kw: str, pattern: str,
                       alias: list[tuple[str, str]] | None = None
                       ) -> list[str] | None:
    """反查：料號關鍵字（或庫存編號對應料號組）→ 哪些樣品單×鞋款在用。查無回 None。"""
    item_cond = "b.ITEM_NO ILIKE ? ESCAPE '\\'"
    params: list = [pattern]
    if alias:
        ph = ", ".join("?" for _ in alias)
        item_cond = f"({item_cond} OR b.ITEM_NO IN ({ph}))"
        params += [i for i, _a in alias]
    rows = _run(
        "WITH hits AS ("
        "  SELECT b.ORG_ID, b.ITEM_NO, b.PROD_NO, "
        "         TRY_CAST(b.VER AS INT) AS ver, "
        "         MAX(COALESCE(b.ITEM_NAME, '')) AS item_name "
        "  FROM SP00__SP_PROD_SPBOM b "
        f"  WHERE {item_cond} "
        "  GROUP BY 1, 2, 3, 4"
        "), latest AS ("
        "  SELECT ORG_ID, PROD_NO, MAX(TRY_CAST(VER AS INT)) AS maxver "
        "  FROM SP00__SP_PROD_SPBOM "
        "  WHERE PROD_NO IN (SELECT DISTINCT PROD_NO FROM hits) "
        "  GROUP BY 1, 2"
        ") "
        "SELECT h.ITEM_NO, ANY_VALUE(h.item_name), s.SAMPLE_NO, h.PROD_NO, "
        "       COUNT(DISTINCT h.ver) AS n_vers, MAX(h.ver) AS max_item_ver, "
        "       ANY_VALUE(l.maxver) AS maxver "
        "FROM hits h "
        "JOIN SP00__SP_PROD_SP s ON s.ORG_ID = h.ORG_ID "
        "     AND s.PROD_NO = h.PROD_NO AND TRY_CAST(s.VER AS INT) = h.ver "
        "JOIN latest l ON l.ORG_ID = h.ORG_ID AND l.PROD_NO = h.PROD_NO "
        "GROUP BY 1, 3, 4 ORDER BY 1, 3, 4",
        params,
    )
    if not rows:
        return None

    # 料號 → [(樣品單, 鞋款, 含此料版數, 最新含此料版, 快照最新版)]
    by_item: dict[str, list] = {}
    names: dict[str, str] = {}
    for item_no, item_name, sno, prod, n_vers, item_ver, maxver in rows:
        by_item.setdefault(item_no, []).append(
            (sno, prod, n_vers, item_ver, maxver))
        if item_name and item_no not in names:
            names[item_no] = item_name

    total_items = len(by_item)
    truncated = total_items > _MAX_SAMPLE_MATERIALS
    shown_items = sorted(by_item)[:_MAX_SAMPLE_MATERIALS]

    # 展開組的「最新含此料版」部位/用量/供應商明細（一發 IN 查詢）
    triples = [(it, prod, item_ver)
               for it in shown_items
               for sno, prod, _n, item_ver, _m in by_item[it][:_MAX_SAMPLE_USERS]]
    detail: dict[tuple, list] = {}
    if triples:
        cond = " OR ".join(
            "(b.ITEM_NO = ? AND b.PROD_NO = ? AND TRY_CAST(b.VER AS INT) = ?)"
            for _ in triples)
        dparams: list = [x for t in triples for x in t]
        drows = _run(
            "SELECT b.ITEM_NO, b.PROD_NO, TRY_CAST(b.VER AS INT), b.PART_NO, "
            "       b.UNIT_QTY, COALESCE(b.UNIT, ''), "
            "       b.VEND_NO, COALESCE(v.SHORTNM_T, '') "
            "FROM SP00__SP_PROD_SPBOM b "
            "LEFT JOIN SC00__PO_VENDER_M v ON v.VEND_NO = b.VEND_NO "
            "          AND v.ORG_ID = b.ORG_ID "
            f"WHERE {cond} ORDER BY b.ITEM_NO, b.PROD_NO, b.PART_NO",
            dparams,
        )
        for item_no, prod, ver, part, uqty, unit, vno, vname in drows:
            detail.setdefault((item_no, prod, ver), []).append(
                (part, uqty, unit, vno, vname))

    all_snos = sorted({sno for it in shown_items
                       for sno, *_rest in by_item[it]})
    custs = _sample_custs(all_snos)

    out = [f"🧪 ERP 樣品 BOM 反查「{_clean(kw)}」：{total_items} 個料號在樣品單"
           f"使用（樣品 BOM 鏡像 {_sample_bom_stamp()}）"]
    if truncated:
        out.append(f"⚠️ 只展開前 {_MAX_SAMPLE_MATERIALS} 個料號——請用更精確的料號關鍵字。")
    alias_by_item = {i: a for i, a in (alias or [])}
    sources = _sample_sources(shown_items)
    for item_no in shown_items:
        users = by_item[item_no]
        out.append("")
        code = alias_by_item.get(item_no)
        label = f"{_clean(item_no)}（庫存編號 {_clean(code)}）" if code \
            else _clean(item_no)
        name = names.get(item_no)
        if name:
            label += f"｜{_clean(name)}"
        out.append(f"◆ {label} — {len(users)} 組樣品單×鞋款使用：")
        if sources is not None:
            src = sources.get(item_no)
            if src:
                out.append("📌 來源樣品單號（料品主檔）：" + "、".join(src))
            else:
                out.append("📌 來源樣品單號：料品主檔未記載（此欄非必填）")
        for sno, prod, n_vers, item_ver, maxver in users[:_MAX_SAMPLE_USERS]:
            head = f"▪ {_clean(sno)}｜{_clean(prod)}"
            cust = custs.get(sno, "")
            if cust:
                head += f"｜客戶 {cust}"
            head += f"｜{n_vers} 個版次含此料"
            out.append(head)
            segs = [f"  最新含此料版 v{item_ver}"]
            for part, uqty, unit, vno, vname in \
                    detail.get((item_no, prod, item_ver), [])[:_MAX_SAMPLE_PARTS]:
                seg = f"部位{_clean(part)}"
                usage = _fmt_usage(uqty)
                if usage:
                    seg += f" 用量 {usage} {_clean(unit)}".rstrip()
                vend = _vend_label(vno, vname)
                if vend:
                    seg += f"｜{vend}"
                segs.append(seg)
            out.append("｜".join(segs))
            if item_ver < maxver:
                out.append(f"  ⚠️ 鏡像最新版 v{maxver} 已無此料（可能已替換，"
                           "請開 ERP 確認）")
        if len(users) > _MAX_SAMPLE_USERS:
            out.append(f"  …（還有 {len(users) - _MAX_SAMPLE_USERS} 組）")
    # 使用清單 ≠ 來源單（2026-07-29 PU468 實例：來源=SR2511002、字典序首張
    # =SR2511001——不可拿首張冒充）。來源欄本尊 SP_ITEM_RDITEM 2026-07-30
    # GRANT 後已入鏡像、上面逐料照表念；表不可用（舊鏡像/撤權）才退回提示。
    out.append("")
    if sources is None:
        out.append("ℹ️ 以上是樣品 BOM「使用」紀錄；此料的「來源樣品單號」"
                   "（因哪張單建立）本鏡像讀不到，要確認請開 ERP 開發料品畫面。")
    else:
        out.append("ℹ️ 「使用」清單來自樣品 BOM；「📌 來源樣品單號」照料品主檔"
                   "對照表（SP_ITEM_RDITEM）念——兩者語意不同，別混用。")
    return out


def _sample_detail(kw: str, pattern: str) -> list[str] | None:
    """樣品單號 / 鞋款(PROD_NO) 關鍵字 → 樣品單清單＋快照最新版 BOM。查無回 None。"""
    rows = _run(
        "SELECT s.SAMPLE_NO, s.PROD_NO, COUNT(*) AS n_vers, "
        "       MAX(TRY_CAST(s.VER AS INT)) AS maxver, "
        "       MAX(COALESCE(s.LAST_DATE, '')) AS last_date, "
        "       MAX(CASE WHEN s.STATUS = '7' THEN 1 ELSE 0 END) AS has_eff "
        "FROM SP00__SP_PROD_SP s "
        "WHERE s.SAMPLE_NO ILIKE ? ESCAPE '\\' "
        "   OR s.PROD_NO ILIKE ? ESCAPE '\\' "
        "GROUP BY 1, 2 ORDER BY 1, 2",
        [pattern, pattern],
    )
    if not rows:
        return None

    total = len(rows)
    truncated = total > _MAX_SAMPLE_PRODS
    rows = rows[:_MAX_SAMPLE_PRODS]
    custs = _sample_custs(sorted({r[0] for r in rows}))

    out = [f"🧪 ERP 樣品單「{_clean(kw)}」：{total} 組樣品單×鞋款符合"
           f"（主檔鏡像 {_mirror_stamp()}、樣品 BOM 鏡像 {_sample_bom_stamp()}）"]
    if truncated:
        out.append(f"⚠️ 只展開前 {_MAX_SAMPLE_PRODS} 組——請用更精確的樣品單號/鞋款。")
    for sno, prod, n_vers, maxver, last_date, has_eff in rows:
        out.append("")
        head = f"▪ {_clean(sno)}｜{_clean(prod)}｜共 {n_vers} 版（最新 v{maxver}）"
        cust = custs.get(sno, "")
        if cust:
            head += f"｜客戶 {cust}"
        if not has_eff:
            head += "｜（無生效版）"
        if last_date:
            head += f"｜最後異動 {_clean(str(last_date))[:10]}"
        out.append(head)

        # 樣品 BOM 快照裡該鞋款的最新版明細（快照可能落後主檔版次，照實標）
        bom = _run(
            "WITH mv AS (SELECT MAX(TRY_CAST(VER AS INT)) AS v "
            "            FROM SP00__SP_PROD_SPBOM WHERE PROD_NO = ?) "
            "SELECT TRY_CAST(b.VER AS INT), b.PART_NO, b.ITEM_NO, "
            "       COALESCE(b.ITEM_NAME, ''), b.UNIT_QTY, COALESCE(b.UNIT, ''), "
            "       b.VEND_NO, COALESCE(v2.SHORTNM_T, '') "
            "FROM SP00__SP_PROD_SPBOM b "
            "LEFT JOIN SC00__PO_VENDER_M v2 ON v2.VEND_NO = b.VEND_NO "
            "          AND v2.ORG_ID = b.ORG_ID "
            "WHERE b.PROD_NO = ? AND TRY_CAST(b.VER AS INT) = (SELECT v FROM mv) "
            "ORDER BY b.PART_NO, b.ITEM_NO",
            [prod, prod],
        )
        if not bom:
            out.append("  （樣品 BOM 鏡像無此鞋款明細——可能未開發 BOM，"
                       "或版次新於鏡像時間）")
            continue
        bom_ver = bom[0][0]
        note = "" if bom_ver == maxver else f"（主檔已有 v{maxver}，鏡像未含）"
        out.append(f"  ─ 樣品 BOM v{bom_ver}{note}，{len(bom)} 項 ─")
        for _v, part, item_no, item_name, uqty, unit, vno, vname in \
                bom[:_MAX_SAMPLE_BOM_LINES]:
            seg = [f"  {_clean(part)}｜{_clean(item_no)}"]
            if item_name:
                seg.append(_clean(item_name)[:40])
            usage = _fmt_usage(uqty)
            if usage:
                seg.append(f"{usage} {_clean(unit)}".rstrip())
            vend = _vend_label(vno, vname)
            if vend:
                seg.append(vend)
            out.append("｜".join(seg))
        if len(bom) > _MAX_SAMPLE_BOM_LINES:
            out.append(f"  …（還有 {len(bom) - _MAX_SAMPLE_BOM_LINES} 項）")
    return out


def erp_sample_lookup(keyword: str) -> str:
    """依 樣品單號 / 鞋款 / 料號 / 庫存編號 查樣品開發單。確定性：同輸入必同輸出。

    方向自動判定（兩者都中就兩段都給）：關鍵字命中 樣品單號(SAMPLE_NO，SR 開頭
    為主) 或 鞋款(PROD_NO) → 列樣品單＋快照最新版 BOM；命中 樣品 BOM 料號
    (ITEM_NO，含庫存編號舊短碼對應) → 反查哪些樣品單×鞋款在用。樣品是迭代
    開發（一款動輒 20+ 版），反查含「幾個版次含此料／最新版是否仍用」防過時。
    """
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return ("用法：輸入至少 2 個字的 樣品單號 / 鞋款 / 料號 / 庫存編號"
                "（例：SR2511001、JA1065、PUB014T1718D05400000、PU468）。")
    if not _db_ready():
        return ("❌ ERP 本地鏡像倉不存在（var/data/erp_mirror/erp_full.duckdb）。"
                "要先跑 scripts/erp_mirror.py 全量鏡像。")

    pattern = f"%{_escape_like(kw)}%"
    alias = _alias_expand(kw)
    try:
        sections: list[str] = []
        det = _sample_detail(kw, pattern)
        if det:
            sections.extend(det)
        used = _sample_where_used(kw, pattern, alias)
        if used:
            if alias:
                used.insert(1, _alias_note(kw, alias))
            if sections:
                sections.append("")
            sections.extend(used)
    except Exception as e:  # noqa: BLE001 - 對話面工具，回錯誤字串不 raise
        return f"❌ 樣品單查詢失敗：{type(e).__name__}: {str(e)[:200]}"

    if not sections:
        prefix = _alias_note(kw, alias) + "\n" if alias else ""
        return (f"{prefix}查無符合「{_clean(kw)}」的樣品單紀錄（SP00 樣品主檔／"
                "樣品 BOM 皆無此 樣品單號/鞋款/料號）。\n提示：樣品單號如 "
                "SR2511001、鞋款如 JA1065 BK-RED、料號可用片段、庫存編號用 ERP "
                "舊短碼（PU468）；樣品**備料採購單**（J0M…）用 "
                "query_erp_purchase_orders 查；量產 BOM 用 query_erp_bom。"
                + _stale_hint())

    sections.append("")
    sections.append("──\n數字直讀 ERP 鏡像 SP00 樣品開發域（SP_PROD_SP／樣品 "
                    "BOM），未經 AI 生成；樣品 BOM 依鏡像時間為準、新版次以 ERP "
                    "畫面為準。量產型體 BOM 用 query_erp_bom；樣品備料採購單"
                    "（J0M…）用 query_erp_purchase_orders；打樣進度/寄樣動態用 "
                    "read_sample_status。")
    return "\n".join(sections)
