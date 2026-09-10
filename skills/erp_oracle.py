"""飛越 ERP（Oracle / JHDB）唯讀查詢工具 — 訂單主檔 + 物料到料狀況。

資料來源是飛越 ERP 正式庫兩張核心表（schema 見 docs/erp_schema_map.md）：
  - BQ_SE_ORDITEM   訂單主檔（客戶 / 品牌 / 鞋款 / 數量 / 交期 / 狀態 / 單價）
  - BQ_SE_ITEMSCHE  訂單物料明細與到料狀況（需求 / 訂購 / 點收 / 發料 / 供應商）
  兩表用 SE_ID（訂單單號）關聯。

查詢實際在 ERP 主機上用它自己的 10g sqlplus 執行（走 SSH），見 agent_core/erp_oracle_client。
慣例：全唯讀、字串參數、識別碼防注入、ERP 自由文字過 sanitize_for_llm、Telegram code-block 表、tier SAFE。
"""
import re
import unicodedata

from agent_core.erp_oracle_client import (
    ErpOracleError,
    clean_text_col as _tc,  # 自由文字欄剝 CHR(9/10/13)，見 clean_text_col docstring
    fetch_table,
    run_raw_text,
    validate_identifier,
)
# 只用「防注入」層（NFKC + zero-width strip + injection marker），不用 PII/secret 遮蔽：
# ERP 料號/品名是業務資料，redact_log_line 的 IBAN/卡號偵測會誤遮（如把料號變 [REDACTED:IBAN]）。
from agent_core.prompt_injection import sanitize_untrusted_text

_MAX_CELL = 40


def _cell(value) -> str:
    if value is None:
        return ""
    s = sanitize_untrusted_text(value).strip() if isinstance(value, str) else str(value)
    s = s.replace("\n", " ").replace("\t", " ")
    return s[: _MAX_CELL - 1] + "…" if len(s) > _MAX_CELL else s


def _format_table(result: dict, title: str) -> str:
    cols, rows = result["columns"], result["rows"]
    if not rows:
        return f"{title}：查無資料。"
    body = [[_cell(v) for v in row] for row in rows]
    # 用顯示寬度（CJK 全形算 2）算欄寬，否則 Telegram <pre> 等寬下中文欄會把整排推歪。
    widths = [_disp_width(c) for c in cols]
    for row in body:
        for i, c in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _disp_width(c))
    def fmt(r):
        return "  ".join((c if i >= len(widths) else _wpad(c, widths[i])) for i, c in enumerate(r))
    lines = [fmt(cols), "  ".join("-" * w for w in widths)] + [fmt(r) for r in body]
    note = "\n（已截斷，請縮小範圍）" if result.get("truncated") else ""
    return f"{title}（{len(rows)} 筆）\n```\n" + "\n".join(lines) + "\n```" + note


def _friendly(e: Exception) -> str:
    if isinstance(e, ErpOracleError):
        return f"⚠️ {e}"
    return f"⚠️ ERP 查詢出錯：{str(e).splitlines()[0][:200]}"


# 福群生管日報的「指令」欄帶產線/分批後綴（如 JFC26345-1-1），但飛越 ERP 的
# SE_ID 是不帶後綴的 base 號（JFC26345）。只剝『尾端 -數字-數字』兩段，才不會
# 誤砍 Pilot-336195 這種本身就帶單一 dash 的合法 SE_ID。
_LINE_SUFFIX_RE = re.compile(r"^(.+?)-\d+-\d+$")


def _base_se_id(sid: str) -> str | None:
    """回剝掉尾端產線後綴（-數字-數字）後的 base SE_ID；沒有該後綴回 None。"""
    m = _LINE_SUFFIX_RE.match(sid)
    base = m.group(1) if m else None
    return base if base and base != sid else None


def _fetch_se_id_with_fallback(build_sel, headers, sid, **kw):
    """跑 build_sel(se_id) 的查詢；若 0 筆且 sid 帶產線後綴，改用 base 號重查一次。

    build_sel 是 sid -> SELECT 字串的函式（sid 一定是已 validate_identifier 過、或其
    子字串 base，故內嵌進 SQL 安全）。回 (result, effective_sid)。
    """
    result = fetch_table(build_sel(sid), headers, **kw)
    if not result["rows"]:
        base = _base_se_id(sid)
        if base:
            alt = fetch_table(build_sel(base), headers, **kw)
            if alt["rows"]:
                return alt, base
    return result, sid


def query_erp_order(se_id: str) -> str:
    """（SSH 即時連 ERP 主機、預設關）查訂單主檔。日常查詢請改用 query_erp_warehouse。

    ⚠️ 走 SSH 連 ERP 主機且 RED_ERP_ORACLE_ENABLED 預設 0——沒開會直接回錯。問訂單/
    庫存/BOM/生產一律優先 query_erp_warehouse（本地鏡像、每日保鮮）；只有需要「此刻
    主機上最新一秒數據」才用本工具。回客戶、品牌、鞋款、配色、數量、交期、狀態。

    Args:
        se_id: 訂單單號（SE_ID），例如 "JFC26160"。
    """
    try:
        sid = validate_identifier(se_id, "訂單單號")
        def build(x):
            return (
                "SELECT se_id||CHR(9)||TO_CHAR(se_day,'YYYY-MM-DD')"
                f"||CHR(9)||{_tc('ord_cust_name')}||CHR(9)||{_tc('brand_name')}"
                f"||CHR(9)||{_tc('shoe_no')}||CHR(9)||{_tc('color_way')}||CHR(9)||se_qty"
                "||CHR(9)||TO_CHAR(cust_req_date,'YYYY-MM-DD')||CHR(9)||TO_CHAR(fac_date,'YYYY-MM-DD')"
                f"||CHR(9)||{_tc('se_status')} "
                f"FROM bq_se_orditem WHERE se_id = '{x}' ORDER BY se_seq, se_ver"
            )
        headers = ["單號", "單據日", "客戶", "品牌", "鞋款", "配色", "數量", "客戶交期", "交廠日", "狀態"]
        result, eff = _fetch_se_id_with_fallback(build, headers, sid)
    except Exception as e:
        return _friendly(e)
    return _format_table(result, f"訂單 {eff}")


def query_erp_order_materials(se_id: str) -> str:
    """（SSH 即時連主機、預設關）查某訂單物料到料。日常請改用 query_erp_warehouse。

    ⚠️ RED_ERP_ORACLE_ENABLED 預設 0——沒開會回錯；本地鏡像版免連主機且每日保鮮。

    回每個物料的需求 / 訂購 / 點收 / 發料 / 剩餘需訂量、供應商、需求日 / 計畫訂購日、狀態。
    用來看「這張訂單的料齊了沒、卡在哪一項」。

    Args:
        se_id: 訂單單號（SE_ID）。
    """
    try:
        sid = validate_identifier(se_id, "訂單單號")
        def build(x):
            return (
                f"SELECT {_tc('item_no')}||CHR(9)||{_tc('item_name')}||CHR(9)||{_tc('item_unit')}"
                "||CHR(9)||need_qty||CHR(9)||ord_qty||CHR(9)||chk_qty||CHR(9)||issue_qty"
                f"||CHR(9)||left_req_ord_qty||CHR(9)||{_tc('vend_name')}"
                "||CHR(9)||TO_CHAR(need_date,'YYYY-MM-DD')"
                f"||CHR(9)||TO_CHAR(plan_ord_date,'YYYY-MM-DD')||CHR(9)||{_tc('se_status')} "
                f"FROM bq_se_itemsche WHERE se_id = '{x}' ORDER BY item_no"
            )
        headers = ["物料", "品名", "單位", "需求", "訂購", "點收", "發料", "剩餘需訂", "供應商", "需求日", "計畫訂購", "狀態"]
        result, eff = _fetch_se_id_with_fallback(build, headers, sid)
    except Exception as e:
        return _friendly(e)
    return _format_table(result, f"訂單 {eff} 物料到料狀況")


def run_erp_readonly_sql(sql: str) -> str:
    """（SSH 即時連主機、預設關）自訂唯讀 SQL。日常請改用 run_erp_warehouse_sql（本地鏡像）。

    ⚠️ RED_ERP_ORACLE_ENABLED 預設 0——沒開會回錯。

    給進階查詢用（跨表 join、彙總）。經 SELECT-only 守門 + 回傳列數上限。

    Schema（兩表以 SE_ID join）：
      BQ_SE_ORDITEM 訂單主檔：SE_ID, ORD_CUST_NAME(客戶), BRAND_NAME(品牌),
        SHOE_NO(鞋款), CUST_PROD_NAME(客戶型號), SE_QTY(數量), CUST_REQ_DATE(客戶交期),
        FAC_DATE(交廠日), SE_STATUS(狀態，存中文如 新单/生效/完工/出货), PRICE, PO。
      BQ_SE_ITEMSCHE 物料到料：SE_ID, ITEM_NO(料號), ITEM_NAME(品名), NEED_QTY(需求),
        ORD_QTY(訂購), CHK_QTY(點收), LEFT_REQ_ORD_QTY(剩餘需訂), VEND_NAME(供應商),
        NEED_DATE(需求日), PLAN_ORD_DATE(計畫訂購日)。

    Args:
        sql: 一條 SELECT 或 WITH 查詢。
    """
    try:
        text = run_raw_text(sql)
    except Exception as e:
        return _friendly(e)
    if not text.strip():
        return "查詢結果：查無資料。"
    # 自由 SQL 會撈到客戶/供應商可自由輸入的欄（品名/供應商名…），與其他工具一致過注入淨化。
    return "查詢結果\n```\n" + sanitize_untrusted_text(text) + "\n```"


def _order_search_terms(keys: dict) -> str:
    """從訂單關鍵欄位組出搜 Drive 用的 query（取有辨識度的：單號/PO/鞋款/客戶型號/品牌）。"""
    fields = ["SE_ID", "PO", "MER_PO", "SHOE_NO", "PROD_NO", "CUST_MODEL", "BRAND"]
    seen, terms = set(), []
    for f in fields:
        v = (keys.get(f) or "").strip()
        if v and v not in seen:
            seen.add(v)
            terms.append(v)
    return " ".join(terms)


_ORDER_KEY_COLS = ["SE_ID", "PO", "MER_PO", "SHOE_NO", "PROD_NO", "CUST", "BRAND",
                   "CUST_MODEL", "QTY", "FAC_DATE", "STATUS"]


def _fetch_order_keys(sid: str) -> dict | None:
    """查訂單主檔的辨識鍵/概況；回 dict，查無回 None（會 raise ErpOracleError 於連線問題）。"""
    def build(x):
        return (
            "SELECT se_id||CHR(9)||po||CHR(9)||mer_po||CHR(9)||shoe_no||CHR(9)||prod_no"
            f"||CHR(9)||{_tc('ord_cust_name')}||CHR(9)||{_tc('brand_name')}||CHR(9)||{_tc('cust_prod_name')}"
            "||CHR(9)||se_qty||CHR(9)||TO_CHAR(fac_date,'YYYY-MM-DD')||CHR(9)||se_status "
            # 取最新版次，與 _read_order 一致；否則多版次單會拿到任意版、與 query/verify 不一致。
            f"FROM bq_se_orditem WHERE se_id = '{x}' ORDER BY se_seq DESC, se_ver DESC"
        )
    res, _eff = _fetch_se_id_with_fallback(build, _ORDER_KEY_COLS, sid, limit=1)
    return dict(zip(_ORDER_KEY_COLS, res["rows"][0])) if res["rows"] else None


def _num(s) -> float:
    try:
        return float((s or "").strip() or "0")
    except (ValueError, TypeError):
        return 0.0


def cross_reference_order(se_id: str) -> str:
    """交叉比對：給訂單單號，把 ERP 訂單資料 + Drive 上相關文件（PO/spec/樣品/生管日報）一起拉出來。

    流程：查 ERP 訂單主檔取關鍵鍵（單號/PO/鞋款/客戶）→ 用這些鍵語意搜 Drive 已索引文件 →
    兩邊綁在一起回傳。可用來「一站式看一張單」、或拿 ERP 數字跟 Drive 文件對帳抓不一致。
    Drive 搜尋會照你的權限範圍（部門 bot 只拿得到該部門可見的檔）。

    Args:
        se_id: 訂單單號（SE_ID），例如 "JFC26160"。
    """
    try:
        sid = validate_identifier(se_id, "訂單單號")
        keys = _fetch_order_keys(sid)
    except Exception as e:
        return _friendly(e)
    if not keys:
        return f"訂單 {se_id}：ERP 查無此單。"

    erp = (
        f"【ERP 訂單 {keys['SE_ID']}】\n"
        f"客戶 {_cell(keys['CUST'])}／品牌 {_cell(keys['BRAND'])}／"
        f"鞋款 {_cell(keys['SHOE_NO'])}（{_cell(keys['CUST_MODEL'])}）\n"
        f"數量 {keys['QTY']}／交廠日 {keys['FAC_DATE']}／狀態 {_cell(keys['STATUS'])}／"
        f"PO {_cell(keys['PO'])}"
    )

    terms = _order_search_terms(keys)
    try:
        from agent_core.ingest.drive_search import search_drive_docs
        drive = search_drive_docs(terms, k=8)
    except Exception as e:
        drive = f"（Drive 搜尋失敗：{str(e).splitlines()[0][:150]}）"

    return f"{erp}\n\n【Drive 相關文件】（搜尋：{terms}）\n{drive}"


def _problem_lines(items, gap_fn, n=8):
    """按缺口量降序取前 n 項，每項附缺口量。gap_fn(item)→缺口(float)，讓最急的排前面。"""
    items = sorted(items, key=gap_fn, reverse=True)
    parts = [f"{_cell(x['ITEM_NO'])}({_cell(x['ITEM_NAME'])[:8]}) 缺{gap_fn(x):g}" for x in items[:n]]
    return "、".join(parts) + (f" …另 {len(items) - n} 項" if len(items) > n else "")


def reconcile_order(se_id: str) -> str:
    """對帳：給訂單單號，精算它的料帳缺口（短訂/未到齊/還要訂），並附 Drive 文件供 ERP↔Drive 核對。

    ERP 內部數字（需求/訂購/點收/剩餘需訂）做可確定性比對抓缺口；Drive 端附相關文件，
    讓你/小紅再核對 ERP 數字跟單據是否一致。用來回答「這張單的料對得齊嗎、卡哪、跟單據對不對」。

    Args:
        se_id: 訂單單號（SE_ID）。
    """
    try:
        sid = validate_identifier(se_id, "訂單單號")
        def build(x):
            return (
                f"SELECT {_tc('item_no')}||CHR(9)||{_tc('item_name')}||CHR(9)||need_qty||CHR(9)||ord_qty"
                "||CHR(9)||chk_qty||CHR(9)||left_req_ord_qty "
                f"FROM bq_se_itemsche WHERE se_id = '{x}' ORDER BY item_no"
            )
        res, sid = _fetch_se_id_with_fallback(
            build, ["ITEM_NO", "ITEM_NAME", "NEED", "ORD", "CHK", "LEFT"], sid)
    except Exception as e:
        return _friendly(e)
    if not res["rows"]:
        return f"訂單 {se_id}：ERP 查無料況（可能無此單或無物料）。"

    rows = [dict(zip(res["columns"], r)) for r in res["rows"]]
    eps = 1e-6
    short = [d for d in rows if _num(d["ORD"]) + eps < _num(d["NEED"])]
    not_recv = [d for d in rows if _num(d["CHK"]) + eps < _num(d["ORD"])]
    need_more = [d for d in rows if _num(d["LEFT"]) > eps]

    out = [
        f"訂單 {sid} 料帳對帳（共 {len(rows)} 項料）",
        f"  🔴 短訂（訂購<需求）：{len(short)} 項",
        f"  🟡 未到齊（點收<訂購）：{len(not_recv)} 項",
        f"  🟠 還要訂（剩餘需訂>0）：{len(need_more)} 項",
    ]
    if short:
        out.append(f"  短訂：{_problem_lines(short, lambda d: _num(d['NEED']) - _num(d['ORD']))}")
    if not_recv:
        out.append(f"  未到齊：{_problem_lines(not_recv, lambda d: _num(d['ORD']) - _num(d['CHK']))}")
    if need_more:
        out.append(f"  還要訂：{_problem_lines(need_more, lambda d: _num(d['LEFT']))}")
    if not (short or not_recv or need_more):
        out.append("  ✅ 料況齊全：每項訂購≥需求、點收≥訂購、無剩餘需訂。")

    drive = ""
    try:
        keys = _fetch_order_keys(sid)
        if keys:
            from agent_core.ingest.drive_search import search_drive_docs
            drive = ("\n\n【Drive 文件供 ERP↔Drive 核對】\n"
                     + search_drive_docs(_order_search_terms(keys), k=5))
    except Exception as e:
        drive = f"\n\n（Drive 文件查詢略過：{str(e).splitlines()[0][:120]}）"

    return "\n".join(out) + drive


# ── A. 副駕駛建單：以既有訂單為範本產『建單卡』+ 事後核對 ─────────────────────
# 飛越 ERP 是 Oracle Forms applet（無 DOM、無法可靠瀏覽器自動化），故小紅不自己建單；
# 改走副駕駛：把對好 ERP 內部代碼的建單卡備齊 → 人在 ERP 畫面照填 → 小紅讀回新單核對。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_QTY_RE = re.compile(r"^\d{1,8}$")

# 建單卡沿用範本的「描述/代碼」欄（不含每單要換的數量/交期/PO）。順序大致照建單畫面。
_CARD_FIELDS = [
    ("ORD_CUST_NO", "訂貨客戶編號"), ("ORD_CUST_NAME", "訂貨客戶名"),
    ("BRAND_NO", "品牌編號"), ("BRAND_NAME", "品牌名"),
    ("SE_TYPE", "單別"), ("SE_TYPE_NAME", "單別名"),
    ("PROD_NO", "產品編號"), ("PROD_NAME", "產品名"),
    ("SHOE_NO", "鞋款編號"), ("SHOE_NAME", "鞋款名"),
    ("CUST_PROD_NAME", "客戶型號"), ("COLOR_WAY", "配色"),
    ("GENDER", "性別"), ("WIDTH_NAME", "楦寬"),
    ("MONEY_UNIT", "幣別"), ("PRICE", "單價"),
]
# 事後核對讀回的欄位（變動欄 + 該與範本一致的描述欄）。
_VERIFY_FIELDS = ["ORD_CUST_NAME", "BRAND_NAME", "SHOE_NO", "COLOR_WAY", "CUST_PROD_NAME",
                  "SE_QTY", "CUST_REQ_DATE", "FAC_DATE", "PO", "SE_STATUS"]


def _disp_width(s: str) -> int:
    """字串在等寬/CJK 終端的顯示寬度（全形/寬字算 2）。用來對齊建單卡的冒號。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _wpad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _valid_date(value: str, field: str) -> str:
    v = (value or "").strip()
    if not _DATE_RE.match(v):
        raise ErpOracleError(f"{field}格式要 YYYY-MM-DD（收到「{value}」）。")
    return v


def _valid_qty(value) -> str:
    v = str(value).strip()
    if not _QTY_RE.match(v) or int(v) <= 0:
        raise ErpOracleError(f"數量要 1–99999999 的正整數（收到「{value}」）。")
    return v


def _delimited(cols: list[str]) -> str:
    """組 CHR(9) 分隔 SELECT 片段；日期/數值欄轉 TO_CHAR 避免格式/null 吃掉 tab。"""
    exprs = []
    for c in cols:
        if c in ("CUST_REQ_DATE", "FAC_DATE", "REQ_FAC_DATE", "SE_DAY"):
            exprs.append(f"TO_CHAR({c},'YYYY-MM-DD')")
        elif c in ("PRICE", "SE_QTY"):
            exprs.append(f"TO_CHAR({c})")
        else:
            exprs.append(_tc(c))  # 自由文字欄剝 CHR(9/10/13)，免打散切列切欄
    return "||CHR(9)||".join(exprs)


def _read_order(sid: str, cols: list[str]) -> dict | None:
    """讀單一訂單（取最新版次）指定欄位；回 dict，查無回 None。"""
    sel = (f"SELECT {_delimited(cols)} FROM bq_se_orditem "
           f"WHERE se_id = '{sid}' ORDER BY se_seq DESC, se_ver DESC")
    res = fetch_table(sel, cols, limit=1)
    return dict(zip(cols, res["rows"][0])) if res["rows"] else None


def prepare_order_card(template_se_id: str, qty: int = 0, cust_req_date: str = "",
                       fac_date: str = "", po: str = "", expected_color: str = "") -> str:
    """副駕駛建單：以一張既有訂單為範本，產出『建單卡』供你照填飛越 ERP 訂單畫面。

    飛越 ERP 是 Oracle Forms 畫面、無法可靠自動化，所以小紅不直接建單，而是把要填的
    每一欄（客戶/品牌/鞋款/配色/單價等已對到 ERP 內部代碼）整理好，你照著在 ERP 輸入。
    描述欄沿用範本；數量、交期、交廠日、PO 用你提供的新值（卡上以 ★ 標）。建完用
    verify_order_entry 把新單讀回來核對。挑範本請用同客戶同款的近期訂單。

    Args:
        template_se_id: 當範本的既有訂單單號，例如 "JFC26160"。
        qty: 新訂單數量（正整數）；不填則卡上標「待填」。
        cust_req_date: 客戶要求交期 YYYY-MM-DD。
        fac_date: 交廠日 YYYY-MM-DD。
        po: 客戶 PO 單號。
        expected_color: 本單實際配色（如讀單得到的 NAVY-STEEL）；與範本配色不同會標 ⚠️
            提醒 ERP 建單要選本單配色（配色是最容易沿用範本沿用錯的一欄）。
    """
    try:
        sid = validate_identifier(template_se_id, "範本訂單單號")
        new = {}
        if qty:
            new["數量"] = _valid_qty(qty)
        if cust_req_date:
            new["客戶交期"] = _valid_date(cust_req_date, "客戶交期")
        if fac_date:
            new["交廠日"] = _valid_date(fac_date, "交廠日")
        if po:
            new["客戶 PO"] = validate_identifier(po, "客戶 PO")
        tpl = _read_order(sid, [c for c, _ in _CARD_FIELDS])
    except Exception as e:
        return _friendly(e)
    if not tpl:
        return (f"範本訂單 {template_se_id}：ERP 查無此單，沒範本可複製。\n"
                "請先用 query_erp_order 找一張同客戶同款的近期訂單當範本。")

    rows = [(label, _cell(tpl.get(col)) or "—") for col, label in _CARD_FIELDS]
    var_rows = [(f"★ {k}", new.get(k, "（待填）")) for k in ("數量", "客戶交期", "交廠日", "客戶 PO")]
    warns = []
    if expected_color:
        ec, tpl_color = _cell(expected_color), _cell(tpl.get("COLOR_WAY"))
        differ = bool(tpl_color) and ec.upper() != tpl_color.upper()
        var_rows.append(("★ 本單配色", ec + ("　⚠️ 與範本不同" if differ else "")))
        if differ:
            warns.append(f"本單配色「{ec}」≠ 範本配色「{tpl_color}」——ERP 建單請選本單配色，別沿用範本")
    width = max(_disp_width(label) for label, _ in rows + var_rows)
    body = "\n".join(f"{_wpad(label, width)} : {val}" for label, val in rows + var_rows)

    if not expected_color and not _cell(tpl.get("COLOR_WAY")):
        warns.append("範本無配色，請自行確認")
    if _num(tpl.get("PRICE")) <= 0:
        warns.append("範本單價為 0 或空，請確認報價")
    warn = ("\n⚠️ " + "；".join(warns) + "。") if warns else ""

    cust = _cell(tpl.get("ORD_CUST_NAME"))
    model = _cell(tpl.get("CUST_PROD_NAME")) or _cell(tpl.get("SHOE_NO"))
    hint = (f"\n\n建完把新單號給我核對：verify_order_entry('<新單號>', template_se_id='{sid}'"
            + (f", expected_qty={new['數量']}" if "數量" in new else "") + ")")
    return (
        f"【ERP 建單卡】範本 {sid}（{cust} / {model}）\n"
        "照下表逐欄填入飛越 ERP「訂單建立」畫面；★ 為你提供的新值，其餘沿用範本。\n"
        f"```\n{body}\n```" + warn
        + "\n\n⚠️ 小紅不會自己建單（ERP 是 Forms 畫面）；請你在 ERP 照填。"
        "若這是 ERP 還沒有的新鞋款/新客戶，要先在 ERP 建檔再下單。" + hint
    )


def verify_order_entry(new_se_id: str, template_se_id: str = "", expected_qty: int = 0,
                       expected_cust_req_date: str = "", expected_fac_date: str = "",
                       expected_po: str = "") -> str:
    """事後核對：把剛在 ERP 建好的新單讀回來，跟你打算建的值（與範本）逐欄比對抓打錯。

    用在副駕駛建單之後 — 你在 ERP 畫面建完單，把新單號給小紅，小紅讀回新單，確認
    數量/交期/PO 等於你打算填的、且客戶/品牌/鞋款/配色與範本一致。

    Args:
        new_se_id: 剛建好的新訂單單號。
        template_se_id: 建單時的範本單號（會比對客戶/品牌/鞋款/配色是否一致）。
        expected_qty: 你打算填的數量。
        expected_cust_req_date: 你打算填的客戶交期 YYYY-MM-DD。
        expected_fac_date: 你打算填的交廠日 YYYY-MM-DD。
        expected_po: 你打算填的客戶 PO。
    """
    try:
        nid = validate_identifier(new_se_id, "新訂單單號")
        exp = {}
        if expected_qty:
            exp["SE_QTY"] = _valid_qty(expected_qty)
        if expected_cust_req_date:
            exp["CUST_REQ_DATE"] = _valid_date(expected_cust_req_date, "客戶交期")
        if expected_fac_date:
            exp["FAC_DATE"] = _valid_date(expected_fac_date, "交廠日")
        if expected_po:
            exp["PO"] = validate_identifier(expected_po, "客戶 PO")
        new = _read_order(nid, _VERIFY_FIELDS)
        tpl = None
        if template_se_id:
            tpl = _read_order(validate_identifier(template_se_id, "範本訂單單號"), _VERIFY_FIELDS)
    except Exception as e:
        return _friendly(e)
    if not new:
        return f"新單 {new_se_id}：ERP 查不到，確認單號或建單是否已存檔。"

    lines = [f"核對新單 {nid}（狀態 {_cell(new.get('SE_STATUS')) or '—'}）"]
    bad = []
    for c, lab in [("SE_QTY", "數量"), ("CUST_REQ_DATE", "客戶交期"),
                   ("FAC_DATE", "交廠日"), ("PO", "客戶 PO")]:
        if c not in exp:
            continue
        got = _cell(new.get(c))
        same = (_num(got) == _num(exp[c])) if c == "SE_QTY" else (got == exp[c])
        if not same:
            bad.append(lab)
        lines.append(f"  {'✅' if same else '❌'} {lab}：ERP={got or '—'}  你要填={exp[c]}")
    if tpl:
        for c, lab in [("ORD_CUST_NAME", "客戶"), ("BRAND_NAME", "品牌"), ("SHOE_NO", "鞋款"),
                       ("COLOR_WAY", "配色"), ("CUST_PROD_NAME", "客戶型號")]:
            got, ref = _cell(new.get(c)), _cell(tpl.get(c))
            if got != ref:
                bad.append(lab)
            lines.append(f"  {'✅' if got == ref else '❌'} {lab}：新單={got or '—'}  範本={ref or '—'}")
    if len(lines) == 1:
        lines.append("  （未給 expected_* 或 template，只回讀新單，無從比對）")
        lines.append(f"  數量 {_cell(new.get('SE_QTY'))}／客戶 {_cell(new.get('ORD_CUST_NAME'))}／"
                     f"鞋款 {_cell(new.get('SHOE_NO'))}／交廠日 {_cell(new.get('FAC_DATE'))}")
    elif bad:
        lines.append(f"  ❌ 有不符：{'、'.join(bad)} — 請回 ERP 修正這 {len(bad)} 欄後再核對。")
    else:
        lines.append("  ✅ 全部相符。")
    return "\n".join(lines)


# ── 讀單引擎：Supremo「PURCHASE CONTRACT」訂單 PDF → 型體/數量/交期，接 prepare_order_card ──
# LURCHI 等客戶經 Supremo 下單，訂單是固定版式 PDF。逐行明細：
#   <型體> <描述> <配色> <尺寸> <數量> PRS <單價>/PR <金額>，其後多個續行加同型體的尺寸/數量
#   （續行常與材質欄 Upper:/Lining: 黏在同一行，故數量要在整行裡 search 不能只看行首）。
# ERP 訂單主檔 CUST_PROD_NAME 欄存著這個 Supremo 型體（如 74L130300300(NA)），故可自動對到舊單當範本。
_SUP_STYLE_RE = re.compile(
    r"^(\d{2}[A-Z]\d{5,})\s+(.+?)\s+\d{1,2}-\d{1,2}\s+([\d,]+)\s+PRS\s+([\d.]+)/PR", re.I)
_SUP_QTY_RE = re.compile(r"\d{1,2}-\d{1,2}\s+([\d,]+)\s+PRS\s+[\d.]+/PR", re.I)
_SUP_TOTAL_RE = re.compile(r"TOTAL\s*:?\s*([\d,]+)\s*PRS", re.I)
_SUP_HDR = {
    "contract_no": re.compile(r"CONTRACT NO\.?\s*:\s*(\S+)"),
    "order_date": re.compile(r"(?<!SHIP )\bDATE\s*:\s*(\d{2}/\d{2}/\d{4})"),
    "ship_date": re.compile(r"SHIP DATE\s*:\s*(\d{2}/\d{2}/\d{4})"),
    "ship_to": re.compile(r"SHIP TO\s*:\s*(.+)"),
}
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,80}$")


def _sup_int(s) -> int:
    try:
        return int(str(s).replace(",", "").strip() or "0")
    except ValueError:
        return 0


def _sup_date(dmy: str) -> str:
    """DD/MM/YYYY → YYYY-MM-DD（給 prepare_order_card 用）。"""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", dmy or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def parse_supremo_text(text: str) -> dict:
    """純文字 → Supremo 訂單結構（不碰 Drive/ERP，便於測試）。"""
    hdr = {k: (m.group(1).strip() if (m := r.search(text)) else "") for k, r in _SUP_HDR.items()}
    lines, cur = [], None
    for raw in text.splitlines():
        ln = raw.strip()
        m = _SUP_STYLE_RE.match(ln)
        if m:
            toks = m.group(2).split()
            cur = {"style": m.group(1).upper(), "colour": toks[-1] if toks else "",
                   "desc": " ".join(toks[:-1]), "qty": _sup_int(m.group(3)),
                   "rows": 1, "unit_price": m.group(4)}
            lines.append(cur)
            continue
        if cur:
            for qm in _SUP_QTY_RE.finditer(ln):  # 續行（可能黏在材質欄後）
                cur["qty"] += _sup_int(qm.group(1))
                cur["rows"] += 1
    printed = [_sup_int(x) for x in _SUP_TOTAL_RE.findall(text)]
    return {
        "contract_no": hdr["contract_no"], "order_date": _sup_date(hdr["order_date"]),
        "ship_date": _sup_date(hdr["ship_date"]), "ship_to": hdr["ship_to"],
        "lines": lines, "total_qty": sum(line["qty"] for line in lines),
        "printed_total": printed[0] if printed else None,
        "is_supremo": "PURCHASE CONTRACT" in text and "SUPREMO" in text.upper(),
    }


def _drive_pdf_bytes(file_id: str) -> bytes:
    if not _DRIVE_ID_RE.match((file_id or "").strip()):
        raise ErpOracleError("Drive 檔案 ID 格式不對（應為一串英數 _ - 字元）。")
    from agent_core.google_auth import get_service
    svc = get_service("drive", "v3")
    return svc.files().get_media(fileId=file_id.strip(), supportsAllDrives=True).execute()


def _pdf_text(data: bytes, max_pages: int = 40) -> str:
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages[:max_pages])


def _supremo_templates(styles: list[str]) -> dict:
    """批次查 ERP：每個 Supremo 型體 → 對應舊單 [(se_id, cust_prod_name), …]（最新在前）。"""
    ors = " OR ".join(f"cust_prod_name LIKE '{s}%'" for s in styles)  # style 已是 \d\d[A-Z]\d+，無自由文字
    res = fetch_table(
        f"SELECT {_tc('cust_prod_name')}||CHR(9)||se_id "
        f"FROM bq_se_orditem WHERE {ors} ORDER BY se_id DESC",
        ["CUST_PROD_NAME", "SE_ID"], limit=300)
    out: dict = {}
    for cpn, sid in res["rows"]:
        for s in styles:
            if cpn.startswith(s):
                out.setdefault(s, []).append((sid, cpn))
    return out


def parse_supremo_order(drive_file_id: str) -> str:
    """讀 Supremo「PURCHASE CONTRACT」訂單 PDF（Google Drive），抽每個型體的數量/配色/交期，
    並對應 ERP 既有訂單當建單範本 —— 副駕駛建單的「讀單」步，讓建單卡的型體/數量自動帶出。

    先用 Drive 找到那張 Supremo 合約 PDF 的 file id（檔名像 Fu Chun[26S292066].pdf），傳進來。
    小紅回每個型體的總雙數、配色、交期、單價，並（ERP 已啟用時）幫每個型體找到 ERP 對應舊單號當
    範本、附上可直接用的 prepare_order_card 呼叫。數量會跟合約列印總計交叉核對。

    Args:
        drive_file_id: Supremo 合約 PDF 在 Google Drive 的檔案 ID。
    """
    try:
        data = _drive_pdf_bytes(drive_file_id)
    except ErpOracleError as e:
        return _friendly(e)
    except Exception as e:
        return f"⚠️ 讀 Drive PDF 失敗：{str(e).splitlines()[0][:200]}"
    try:
        text = _pdf_text(data)
    except Exception as e:
        return f"⚠️ PDF 解析失敗：{str(e).splitlines()[0][:200]}"

    o = parse_supremo_text(text)
    if not o["is_supremo"] or not o["lines"]:
        return ("這份 PDF 看起來不是 Supremo PURCHASE CONTRACT 訂單（找不到合約抬頭或型體明細）。"
                "請確認檔案，或這是出貨標籤/其他格式。")

    templates, erp_note = {}, ""
    try:
        templates = _supremo_templates(sorted({line["style"] for line in o["lines"]}))
    except ErpOracleError as e:
        erp_note = f"\n（ERP 範本對應略過：{e}）"
    except Exception as e:
        erp_note = f"\n（ERP 範本對應略過：{str(e).splitlines()[0][:120]}）"

    chk = ""
    if o["printed_total"] is not None:
        chk = (f"（合約列印總計 {o['printed_total']:,}"
               + ("✓ 相符）" if o["printed_total"] == o["total_qty"] else " ⚠️ 與明細不符，請核對）"))
    head = (f"【Supremo 訂單】合約 {_cell(o['contract_no'])}　下單 {o['order_date'] or '—'}"
            f"　交期 {o['ship_date'] or '—'}　到 {_cell(o['ship_to']) or '—'}\n"
            f"共 {len(o['lines'])} 型體 / {o['total_qty']:,} 雙{chk}")

    blocks = []
    for line in o["lines"]:
        tag = ""
        if not erp_note:
            cands = templates.get(line["style"], [])
            if cands:
                sid = cands[0][0]
                variants = "、".join(sorted({c for _, c in cands})[:5])
                tag = (f"\n   範本：{sid}（ERP 既有 {variants}）"
                       f"\n   → prepare_order_card('{sid}', qty={line['qty']}"
                       + (f", fac_date='{o['ship_date']}'" if o["ship_date"] else "")
                       + (f", expected_color='{_cell(line['colour'])}'" if line.get("colour") else "")
                       + ")")
            else:
                tag = "\n   ⚠️ ERP 查無此型體舊單 → 可能是新款，需先在 ERP 建檔再下單。"
        blocks.append(
            f"• {line['style']}　{line['qty']:,} 雙　配色 {_cell(line['colour']) or '—'}"
            f"　${line['unit_price']}/雙　{_cell(line['desc'])[:24]}" + tag)

    return (head + erp_note + "\n\n" + "\n".join(blocks)
            + "\n\n副駕駛流程：挑對範本→prepare_order_card 產建單卡→你在 ERP 照填→verify_order_entry 核對。"
            "\n⚠️ 配色請自行確認（範本沿用舊單配色，未必等於本單）。")


SKILL_TOOLS = [
    query_erp_order,
    query_erp_order_materials,
    run_erp_readonly_sql,
    cross_reference_order,
    reconcile_order,
    prepare_order_card,
    verify_order_entry,
    parse_supremo_order,
]
