"""越南倉庫每日簡報（UserY / warehouse@）—— ERP 側確定性報表。

起因：2026-08-04 倉庫主管井戶良枝(UserY)開出「每天要看什麼」的清單（大貨採購 4 項
＋倉庫 9 項）。本模組只做**ERP 鏡像查得到權威數字**的那幾項，全部確定性 SQL、
零 Gemini —— 數字不經 LLM 就沒有幻覺空間（同 erp_kitting / erp_warehouse 防線）。

對應 UserY 清單：
  倉庫②進貨通知單（驗貨/人員&倉庫空間安排）→ arrival_plan：生效採購單的未到料，
      逾期段（要催）與將到段（備驗貨/空間）分開，帶供應商 + 付款/送貨/採購方式 +
      該料現有庫存與倉別（驗收後入哪倉、還有沒有位置的依據）。
  大貨①訂單材料資料（廠商 / 廠商的條件）→ 同上的 付款/送貨/採購方式 欄。ERP 只存
      這三項條件，**lead time 與 MOQ 全廠沒建**（PO_SUMM_M.LEAD_TIME 0 列、
      PO_ITEM.MAX_MOQ_YS 1,462 列全空），所以本工具不報這兩欄——沒有就是沒有，
      不拿別的欄硬湊。
  大貨③/倉庫⑤庫存數（採購數/發料）→ stock_watch：進貨預告涵蓋料號的現有庫存，
      以及零庫存但本週有需求的料。
  大貨④/倉庫⑦生產製程（誰先領料）→ 只能給「已放行工單×料」的粗略順序，見
      erp_kitting.kitting_alert；ERP 製程路由表（RD_ROUT_M / RD_PROD_ROUT）與排程
      模組（WK_SCHE_*）**都是 0 列**，領料單 SF_DRAW_* 又不在每日熱表（凍在初鏡
      2026-07-07），所以本模組不產「領料部門/加工順序」——那要先讓 ERP 有資料。
  倉庫④分段資料 → RD_STYLE_SEGMENT 僅 263 列且不在熱表，同樣不報。
  倉庫①材料樣品(驗貨)、⑥材料保管方式 → 不在 ERP，走 Drive/SOP 知識庫
      （search_operation_sops），不在本模組。
  倉庫⑧郵件摘要、⑨待辦追蹤 → agent_core/warehouse_mail.py。

⚠️ 替代料：ERP 的 BOM 替代料表 RD_BOM_ITEMSUB **0 列**（全廠沒在用這個功能），
   所以「含 有沒有替代料」一律回「ERP 未建替代料」而不是猜——改色/替代料實務走
   郵件與樣品室確認，那條線在 warehouse_mail 的待辦追蹤。
"""
import datetime
import os
import threading
import unicodedata

from agent_core.prompt_injection import sanitize_untrusted_text

_QUERY_DEADLINE_S = 60.0
_MAX_CELL = 34
_MAX_ROWS = 40

# SY00.CD_CODE 碼類（v_code_dictionary 的底表）——採購單頭上的三項廠商條件。
_RULE_PAY = "2103"    # 付款方式：T/T、T/T30天、預付100%…
_RULE_SEND = "2105"   # 送貨方式：海運/陸運/快遞/廠商自送
_RULE_PRWAY = "1303"  # 採購方式：台灣/越南/韓國/義大利採購


def _db_path():
    from agent_core.logging_and_paths import DATA_DIR
    return os.path.join(DATA_DIR, "erp_mirror", "erp_full.duckdb")


def _db_ready():
    return os.path.exists(_db_path())


def _connect_ro():
    import duckdb
    return duckdb.connect(_db_path(), read_only=True,
                          config={"enable_external_access": "false"})


def _run_ro(sql):
    """單查詢帶 deadline（同 erp_warehouse：跑飛的查詢會被 interrupt）。"""
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


# ---------- 表格輸出（CJK 對齊，Telegram/信件等寬皆可讀） ----------
def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _wpad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _cell(v) -> str:
    """單格：淨化 ERP 自由文字（品名/供應商是自由輸入欄，防 prompt injection）+ 截斷。"""
    if v is None:
        return ""
    s = sanitize_untrusted_text(v).strip() if isinstance(v, str) else str(v)
    s = s.replace("\n", " ").replace("\t", " ")
    return s[: _MAX_CELL - 1] + "…" if len(s) > _MAX_CELL else s


def _table(cols, rows) -> str:
    if not rows:
        return "（無）"
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


def _n(v) -> str:
    """數量顯示：整數不拖 .0，小數留兩位（採購單位常是 Y/M/PR，會有小數）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if abs(f - round(f)) < 1e-9 else f"{f:.2f}"


def _today() -> datetime.date:
    return datetime.date.today()


# ---------- 進貨預告 ----------
def _arrival_sql(lo: str, hi: str) -> str:
    """未到齊的生效採購單明細 × 廠商條件 × 該料現有庫存。

    只取 明細狀態=生效 且 單頭狀態=生效（0=取消/1=新建/99=結案 都不是「在等貨」）。
    已收數量 NULL 要 COALESCE 成 0：未結案 PO 的 RCPT_QTY 大量是 NULL，不補的話
    「未到 = 訂購 − 已收」整列變 NULL、料就靜默從預告裡消失（同 erp_kitting 的坑）。
    庫存用 SUM 跨倉合計，倉別另存 string_agg 給倉庫看該料現在放哪。
    """
    return f"""
WITH po AS (
  SELECT p."計劃到貨日"[1:10]                       AS eta,
         p."採購單號"                                AS po_no,
         p."供應商簡稱"                              AS vend,
         p."料號"                                    AS item,
         p."品名描述"                                AS item_name,
         p."採購單位"                                AS unit,
         p."訂購數量"                                AS ord_qty,
         COALESCE(p."已收數量", 0)                   AS rcpt_qty,
         pay.NAME_T                                  AS pay_term,
         snd.NAME_T                                  AS send_way,
         prw.NAME_T                                  AS pr_way
  FROM v_purchase_orders p
  JOIN SC00__PO_ORDER_M m
    ON m.ORG_ID = p."組織" AND m.ORDER_NO = p."採購單號"
  LEFT JOIN SY00__CD_CODE pay
    ON pay.RULE_NO = '{_RULE_PAY}'   AND pay.CODE_NO = m.PAY_NO    AND pay.ORG_ID = m.ORG_ID
  LEFT JOIN SY00__CD_CODE snd
    ON snd.RULE_NO = '{_RULE_SEND}'  AND snd.CODE_NO = m.SEND_TYPE AND snd.ORG_ID = m.ORG_ID
  LEFT JOIN SY00__CD_CODE prw
    ON prw.RULE_NO = '{_RULE_PRWAY}' AND prw.CODE_NO = m.PR_WAY    AND prw.ORG_ID = m.ORG_ID
  WHERE p."明細狀態" = '生效' AND p."單頭狀態" = '生效'
    AND p."計劃到貨日" IS NOT NULL
    AND p."計劃到貨日"[1:10] BETWEEN '{lo}' AND '{hi}'
), pend AS (
  SELECT eta, po_no, vend, item, item_name, unit, pay_term, send_way, pr_way,
         SUM(ord_qty - rcpt_qty) AS due_qty
  FROM po
  GROUP BY ALL
  HAVING SUM(ord_qty - rcpt_qty) > 0.0001
), stk AS (
  SELECT "料號" AS item,
         SUM("結存數量")                              AS on_hand,
         string_agg(DISTINCT "倉庫名稱", '/')          AS whs
  FROM v_stock
  GROUP BY 1
)
SELECT d.eta, d.po_no, d.vend, d.item, d.due_qty, d.unit,
       COALESCE(s.on_hand, 0) AS on_hand, s.whs,
       d.pay_term, d.send_way, d.pr_way, d.item_name
FROM pend d
LEFT JOIN stk s ON s.item = d.item
ORDER BY d.eta, d.po_no, d.item
"""


def warehouse_arrival_plan(days_ahead: int = 14, days_overdue: int = 30) -> str:
    """進貨預告：生效採購單裡「還沒到齊」的料，逾期段與將到段分開列。免 +確認。

    給倉庫排驗貨人力與倉儲空間用（UserY 清單 倉庫②「進貨通知單」）。每列帶
    供應商、未到數量與單位、該料現有庫存與存放倉別，以及採購單上的廠商條件
    （付款方式 / 送貨方式 / 採購方式）。

    ⚠️ 資料界線（沒有就說沒有，不硬湊）：
      - ERP 沒有 lead time 與 MOQ 欄位資料（全廠未建），本表不報這兩項。
      - 「計劃到貨日」是採購單上的 PLAN_DATE，不是船期 ETA；船期在採購/業務的
        shipping advice 郵件裡（走 warehouse_mail_digest 那條線）。
      - 已收數量以 ERP 收料單為準；倉庫實地點收未 KEY 進 ERP 的不會反映。

    Args:
        days_ahead: 往後看幾天內計劃到貨（預設 14）。
        days_overdue: 往回看幾天內的逾期未到（預設 30；更早視為呆單不洗版）。
    Returns:
        兩段表格；完全沒有未到料時回「(無新發現)」。
    """
    if not _db_ready():
        return "❌ ERP 本地鏡像倉不存在（要先跑 scripts/erp_mirror.py）。"
    today = _today()
    lo = (today - datetime.timedelta(days=max(0, int(days_overdue)))).isoformat()
    hi = (today + datetime.timedelta(days=max(0, int(days_ahead)))).isoformat()
    today_s = today.isoformat()
    try:
        _, rows = _run_ro(_arrival_sql(lo, hi))
    except Exception as e:  # noqa: BLE001
        return f"❌ 進貨預告查詢失敗：{type(e).__name__}: {str(e)[:200]}"
    if not rows:
        return "(無新發現)"

    late, soon = [], []
    for eta, po_no, vend, item, due, unit, on_hand, whs, pay, send, prw, name in rows:
        line = [eta, po_no, vend, item, _n(due), unit,
                _n(on_hand), whs or "無庫存", pay or "-", send or "-", prw or "-"]
        (late if str(eta) < today_s else soon).append((line, name))

    cols = ["計劃到貨", "採購單號", "供應商", "料號", "未到", "單位",
            "現有庫存", "倉別", "付款", "送貨", "採購方式"]
    out = [
        f"📥 進貨預告（計劃到貨日 {lo}〜{hi}，生效採購單）："
        f"逾期未到 {len(late)} 項、近期將到 {len(soon)} 項。"
    ]
    if late:
        out.append(f"\n【已逾計劃到貨日 {len(late)} 項 —— 要催貨/確認船期】")
        out.append(_table(cols, [r for r, _ in late]))
    if soon:
        out.append(f"\n【近期將到 {len(soon)} 項 —— 排驗貨人力與倉儲空間】")
        out.append(_table(cols, [r for r, _ in soon]))
    out.append(
        "\n註：ERP 未建 lead time / MOQ / 替代料，這三項本表不列；"
        "「計劃到貨日」是採購單上的日期、非船期 ETA。"
    )
    return "\n".join(out)


# ---------- 庫存注意 ----------
def warehouse_stock_watch(top: int = 15) -> str:
    """庫存注意：本週有進貨或有生效採購在途的料號裡，目前零庫存 / 低庫存的。免 +確認。

    用途是發料前的預警（UserY 清單 大貨③/倉庫⑤「庫存數」）：料號正在採購 = 有人
    要用，而現在庫存是 0 或很低 → 一到貨就要優先驗收發料。

    ⚠️ ERP 沒有安全庫存/再訂購點欄位，所以「低」的定義只能相對於在途量
    （現有庫存 < 在途未到量的 20%），不是廠內正式的安全庫存標準。

    Args:
        top: 最多列幾項（預設 15）。
    Returns:
        清單表格；沒有符合的回「(無新發現)」。
    """
    if not _db_ready():
        return "❌ ERP 本地鏡像倉不存在。"
    top = max(1, min(int(top), 50))
    sql = """
WITH pend AS (
  SELECT p."料號" AS item, p."品名描述" AS item_name, p."採購單位" AS unit,
         SUM(p."訂購數量" - COALESCE(p."已收數量", 0)) AS due_qty
  FROM v_purchase_orders p
  WHERE p."明細狀態" = '生效' AND p."單頭狀態" = '生效'
  GROUP BY ALL
  HAVING SUM(p."訂購數量" - COALESCE(p."已收數量", 0)) > 0.0001
), stk AS (
  SELECT "料號" AS item, SUM("結存數量") AS on_hand,
         string_agg(DISTINCT "倉庫名稱", '/') AS whs
  FROM v_stock GROUP BY 1
)
SELECT d.item, d.item_name, COALESCE(s.on_hand, 0) AS on_hand,
       s.whs, d.due_qty, d.unit
FROM pend d LEFT JOIN stk s ON s.item = d.item
WHERE COALESCE(s.on_hand, 0) < d.due_qty * 0.2
ORDER BY COALESCE(s.on_hand, 0), d.due_qty DESC
"""
    try:
        _, rows = _run_ro(sql)
    except Exception as e:  # noqa: BLE001
        return f"❌ 庫存注意查詢失敗：{type(e).__name__}: {str(e)[:200]}"
    if not rows:
        return "(無新發現)"
    body = [[item, name, _n(oh), whs or "無庫存", _n(due), unit]
            for item, name, oh, whs, due, unit in rows[:top]]
    head = (f"📦 庫存注意（有生效採購在途、但現有庫存不到在途量 20%）："
            f"共 {len(rows)} 項，列前 {len(body)} 項。")
    return head + "\n" + _table(
        ["料號", "品名", "現有庫存", "倉別", "在途未到", "單位"], body
    ) + "\n註：ERP 無安全庫存/再訂購點欄位，「低」是相對在途量的相對值、非廠內標準。"


# ---------- 組合簡報 ----------
def warehouse_daily_brief(days_ahead: int = 14, days_overdue: int = 30) -> str:
    """越南倉庫每日 ERP 簡報：進貨預告 ＋ 齊套/缺料 ＋ 庫存注意，一次組好。免 +確認。

    給 warehouse-mgr@ / warehouse@ 的每日通知用（排程 warehouse_brief_0900）。三段都是
    確定性 SQL 產出、零 Gemini；任何一段查詢失敗只讓該段標錯誤，不讓整份報告消失。

    Args:
        days_ahead: 進貨預告往後看幾天（預設 14）。
        days_overdue: 進貨預告往回看幾天的逾期（預設 30）。
    Returns:
        組合好的報告文字；三段都沒東西時回「(無新發現)」。
    """
    sections = []

    arrivals = warehouse_arrival_plan(days_ahead=days_ahead, days_overdue=days_overdue)
    if arrivals.strip() and "(無新發現)" not in arrivals:
        sections.append(arrivals)

    try:
        from skills.erp_kitting import kitting_alert
        kit = kitting_alert()
    except Exception as e:  # noqa: BLE001
        kit = f"❌ 齊套預警載入失敗：{type(e).__name__}: {str(e)[:120]}"
    if kit.strip() and "(無新發現)" not in kit:
        sections.append("🧩 齊套/缺料（快上線的訂單哪張料沒齊）\n" + kit)

    stock = warehouse_stock_watch()
    if stock.strip() and "(無新發現)" not in stock:
        sections.append(stock)

    if not sections:
        return "(無新發現)"
    return "\n\n".join(sections)


for _fn in (warehouse_arrival_plan, warehouse_stock_watch, warehouse_daily_brief):
    _fn.background_safe = True  # 純讀 DuckDB 鏡像，背景排程可用。

SKILL_TOOLS = [warehouse_arrival_plan, warehouse_stock_watch, warehouse_daily_brief]
