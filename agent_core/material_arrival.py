"""採購「料到貨進度」LOT 視角追蹤 — 從內部郵件 lake 採購信抽料批(LOT)的到貨狀態。

跟 `check_material_readiness`（factory_bom：型體 → BOM 供應商 → 採購信）**互補、不重疊**：
那支是**型體視角**（某雙鞋的料各供應商備到哪），這支是**料批(LOT)視角**（LOT 217 的布
到了沒／幾號到／數量多少），並多一個 `check_material_readiness` 沒有的能力——**主動掃描**
未到貨、久未更新的料批（盤點看板，不必先知道型體）。

資料源＝內部 lake 採購 dept 信（每封帶 LLM 摘要）。布料以公尺(M)計、供應商是布廠
（三井／Vetex／Huafon／NASTROTEX…）。狀態用 主旨＋摘要 啟發式判（**無新 LLM 呼叫**、
純函式可測）；數量另掃信件內文（raw_body_preview）**LOT 字樣附近視窗**——一行
thread 摘要會把數字洗掉（2026-07-27 UserA 案：LOT 217-2026 實際總量 62,600m 只在
7/23 內文，摘要層抓到的是 200M F.O.C），視窗化則多 LOT 信不互染（60200M 貼著
LOT 204 寫就不會算到 LOT 217 頭上）。事件日期用 thread 最後往來（last_message_date）
而非首信日。確切到貨數量／單據仍以 Drive 採購夾或 ERP 為準（信件只反映往來文字）。
"""
import re

import pandas as pd

# 共用 email_timeline 的 mtime+TTL parquet 快取（免每次呼叫整檔重讀 2 萬列）。
from agent_core.email_timeline import _load_df as _load_lake_df
from agent_core.internal_emails_store import INTERNAL_PARQUET
from agent_core.prompt_injection import sanitize_for_llm

# 內部 lake 採購 dept（dept_rules 命名，與外部 lake 的 email_classify 不同名）。
_PROC_DEPT = "採購"

# LOT 料批號：'LOT 217-2026' / 'Lot218-2025' / 'LOT217' → 正規化成 'LOT <num>[-<year>]'。
# \b 防「Pilot 300」這種字尾恰為 lot 的字被誤抓成 LOT 300。
_LOT_RE = re.compile(r"\bLOT\s*#?\s*(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?", re.IGNORECASE)
# 布料數量：'6500M' / '26,000 M' / '91碼' / '120 公斤'。取最大值當該批代表量（信中值、非權威）。
_QTY_RE = re.compile(r"(\d[\d,]{0,7})\s*(M|公尺|米|碼|yds?|公斤|kgs?)\b", re.IGNORECASE)
_UNIT_NORM = {"公尺": "M", "米": "M", "YD": "碼", "公斤": "KG", "KG": "KG"}

# 狀態啟發式：依「目前到哪」優先序判（到貨 > 缺料 > 延誤 > 出貨 > 付款 > 下單 > 進行中）。
# 缺料/延誤是「問題狀態」，排在出貨/下單前以免被表面進度蓋過。
_STATUS_RULES = [
    ("已到貨", re.compile(r"到[廠港]|到達|已到|入庫|抵達|抵港|received|arrived", re.I)),
    # ↑「已到貨」另有 ETA 語境排除：見 _classify_status ——「預計 7/10 抵港」是在途不是已到。
    ("缺料/短少", re.compile(r"短少|短碼|短裝|缺料|缺貨|不足|少了|shortage|\bshort\b", re.I)),
    # 暫扣/暫緩＝出貨被扣住（短裝爭議等），歸「延誤」問題狀態。
    ("延誤", re.compile(r"延誤|延遲|遲交|延後|推遲|暫扣|暫緩|擱置|delay|postpone|on\s*hold|\bhold\b", re.I)),
    ("已出貨", re.compile(r"出貨|已寄|已發|已出|shipped|dispatch|ex-?factory", re.I)),
    # 付款/對帳/運費/發票 — 財務往來（採購信大宗），非材料物流；排在「已下單」前，因
    # 「…訂單…付款」這類財務信會誤觸 訂單 關鍵字。帶 到貨/出貨 物流訊號者已先被上面接走。
    ("付款/對帳", re.compile(r"付款|請款|貨款|匯款|對帳|運費|發票|信用狀|\bL/?C\b|\bT/?T\b|預付", re.I)),
    ("已下單", re.compile(r"PO確認|下單|採購單|訂單|new\s*order|new\s*po|po\s*#|po\s*no", re.I)),
]
# 「在途未到貨」＝有材料物流訊號、尚未確認到貨；盤點看板只掃這些（排除已到貨/付款/進行中
# 等，避免拿財務信當「料沒到」誤報）。
_INTRANSIT = frozenset({"已下單", "已出貨", "延誤", "缺料/短少"})
# 問題旗標（LOT 任一封提到就標，提醒人留意）。
_FLAG_SHORT = re.compile(r"短少|短碼|短裝|缺料|缺貨|不足|shortage|\bshort\b", re.I)
_FLAG_DELAY = re.compile(r"延誤|延遲|遲交|延後|推遲|暫扣|暫緩|擱置|delay|postpone|on\s*hold|\bhold\b", re.I)

_STATUS_ICON = {"已到貨": "✅", "已出貨": "🚚", "已下單": "📝", "延誤": "⏰",
                "缺料/短少": "⚠️", "付款/對帳": "💰", "進行中": "·"}


def _norm_lot(m) -> str:
    a, b = m.group(1), m.group(2)
    if not b:
        return f"LOT {a}"
    if len(b) == 2:  # 兩位年補四位：'217-25' → '217-2025'（否則同批被拆成兩個 LOT）
        b = f"20{b}"
    return f"LOT {a}-{b}"


def _extract_lots(text: str) -> list[str]:
    """抽文中所有 LOT 號（正規化、去重、保序）。一封多 LOT → 各記一筆。"""
    out, seen = [], set()
    for m in _LOT_RE.finditer(text or ""):
        lot = _norm_lot(m)
        if lot not in seen:
            seen.add(lot)
            out.append(lot)
    return out


def _extract_qty_m(text: str):
    """抽最大一筆數量(連單位)，回 (value:int, unit:str) 或 None。布料多以 M 計。"""
    best = None
    for m in _QTY_RE.finditer(text or ""):
        try:
            v = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = m.group(2).upper().rstrip("S")
        unit = _UNIT_NORM.get(unit, unit)
        if best is None or v > best[0]:
            best = (v, unit)
    return best


# LOT 附近視窗：往前 30 / 往後 80 字，且**不跨句讀**（。；！？換行、欄位分隔｜）。
# 抓「LOT 217-2026 -- Total: 62600m = …」這種貼著 LOT 寫的總量；一封多 LOT 的信
# （LOT 204 的 60200M＋LOT 217 同信）或同段落後面不相干的數字（預購 25000M），
# 視窗/句讀外就不歸這個 LOT，避免互染。逗號不當邊界——「LOT 204 ,收料單: X , 60200M」
# 是常見寫法。
_QTY_WIN_BEFORE = 30
_QTY_WIN_AFTER = 80
_QTY_WIN_BOUNDARY = re.compile(r"[。；;！!？?\n｜]")


def _extract_qty_windowed(text: str, lot: str):
    """只在 lot 字樣附近視窗（同句讀段內）抽最大數量；lot 為 _norm_lot 正規化後的號。"""
    best = None
    for m in _LOT_RE.finditer(text or ""):
        if _norm_lot(m) != lot:
            continue
        left = text[max(0, m.start() - _QTY_WIN_BEFORE):m.start()]
        cut = None
        for b in _QTY_WIN_BOUNDARY.finditer(left):
            cut = b.end()
        if cut is not None:
            left = left[cut:]
        right = text[m.end():m.end() + _QTY_WIN_AFTER]
        b = _QTY_WIN_BOUNDARY.search(right)
        if b:
            right = right[:b.start()]
        q = _extract_qty_m(left + text[m.start():m.end()] + right)
        if q and (best is None or q[0] > best[0]):
            best = q
    return best


# ETA 語境：到貨字眼前面若是「預計/預定/預估/ETA/expected/will arrive」等預告用語，
# 表示還沒到（在途），不能算已到貨。往前看 30 字內、且中間不跨句讀。
_ETA_PREFIX_RE = re.compile(
    r"(?:預計|預定|預估|預期|预计|预定|预估|ETA|E\.T\.A\.?|expected|will\s+arrive|due\s+to\s+arrive)"
    r"[^，,。.;；!？?\n]{0,20}$",
    re.IGNORECASE)


def _is_eta_context(text: str, start: int) -> bool:
    """text[start:] 的到貨字眼是否處於 ETA 預告語境（前綴含 預計/ETA…）。"""
    return bool(_ETA_PREFIX_RE.search(text[max(0, start - 30):start]))


def _classify_status(text: str) -> str:
    t = text or ""
    for label, rx in _STATUS_RULES:
        if label == "已到貨":
            # 「預計 7/10 抵港」是 ETA 預告不是已到貨 —— 只有非 ETA 語境的
            # 到貨字眼才算已到；全是 ETA 語境就落到後面的 已出貨/在途 規則。
            if any(not _is_eta_context(t, m.start()) for m in rx.finditer(t)):
                return label
            continue
        if rx.search(t):
            return label
    return "進行中"


def _epoch(ts) -> int:
    """Timestamp → 排序用 ns（NaT 視為 0，避免 .value 例外）。"""
    try:
        return ts.value if pd.notna(ts) else 0
    except Exception:  # noqa: BLE001
        return 0


def _proc_rows(df):
    """篩採購 dept 的列（primary_dept==採購 或 all_depts 含採購）。"""
    n = len(df)
    pdept = df["primary_dept"].fillna("") if "primary_dept" in df.columns else pd.Series([""] * n, index=df.index)
    alld = df["all_depts"].fillna("").astype(str) if "all_depts" in df.columns else pd.Series([""] * n, index=df.index)
    return df[(pdept == _PROC_DEPT) | alld.str.contains(_PROC_DEPT)]


_EVENT_COLS = ["date", "lot", "status", "qty", "unit", "subject", "summary", "flag_short", "flag_delay"]


def _events_from_df(df, query="", days=0):
    """採購信 DataFrame → 逐(LOT×信)事件 DataFrame（純函式、可測、免讀檔）。

    每列＝一個 LOT 在一封信裡的一次狀態事件。一封多 LOT 信展開成多列。query 對
    主旨＋摘要＋品牌 過濾；days>0 只看近 N 天。**沒有 LOT 號的採購信不進來**（這支是
    LOT 視角；要全部往來用 read_dept_email_timeline）。
    """
    sm = _proc_rows(df).copy()
    if sm.empty:
        return pd.DataFrame(columns=_EVENT_COLS)
    subj = sm["subject"].fillna("")
    summ = sm["summary"].fillna("") if "summary" in sm.columns else pd.Series([""] * len(sm), index=sm.index)
    brands = sm["brands"].fillna("").astype(str) if "brands" in sm.columns else pd.Series([""] * len(sm), index=sm.index)
    body = sm["raw_body_preview"].fillna("").astype(str) if "raw_body_preview" in sm.columns else pd.Series([""] * len(sm), index=sm.index)
    q = (query or "").strip()
    if q:
        hay = subj + " " + summ + " " + brands
        keep = hay.str.contains(re.escape(q), case=False, regex=True)
        sm, subj, summ, body = sm[keep], subj[keep], summ[keep], body[keep]
    # 事件日期＝thread 最後往來（last_message_date）優先——thread 首信日會把
    # 「6/4 開頭、7/23 還在談」的批次看成一個多月沒動靜。
    dates = pd.to_datetime(sm["date"], errors="coerce") if "date" in sm.columns else pd.Series(pd.NaT, index=sm.index)
    if "last_message_date" in sm.columns:
        lmd = pd.to_datetime(sm["last_message_date"], errors="coerce")
        dates = lmd.fillna(dates)
    if days and int(days) > 0:
        keep = dates >= pd.Timestamp.now() - pd.Timedelta(days=int(days))
        sm, subj, summ, body, dates = sm[keep], subj[keep], summ[keep], body[keep], dates[keep]
    rows = []
    for idx in sm.index:
        s, m = str(subj[idx]), str(summ[idx])
        text = s + " ｜ " + m
        lots = _extract_lots(text)
        if not lots:
            continue
        # 數量：主旨＋摘要＋內文的 LOT 附近視窗（多 LOT 不互染）；單 LOT 信另退回
        # 主旨＋摘要全域最大值（數字不一定貼著 LOT 寫），取兩者較大。
        full = text + ((" ｜ " + str(body[idx])) if str(body[idx]) else "")
        status = _classify_status(text)
        short, delay = bool(_FLAG_SHORT.search(text)), bool(_FLAG_DELAY.search(text))
        for lot in lots:
            qty = _extract_qty_windowed(full, lot)
            if len(lots) == 1:
                wide = _extract_qty_m(text)
                if wide and (qty is None or wide[0] > qty[0]):
                    qty = wide
            rows.append({
                "date": dates[idx], "lot": lot, "status": status,
                "qty": qty[0] if qty else None, "unit": qty[1] if qty else "",
                "subject": s[:60], "summary": m[:140],
                "flag_short": short, "flag_delay": delay,
            })
    return pd.DataFrame(rows, columns=_EVENT_COLS)


def _canonicalize_lots(events):
    """同號的「無年份 LOT」併入**唯一**的有年份 LOT（'LOT 217' → 'LOT 217-2026'）。

    同一批料有的信寫全號有的只寫短號，不併會拆成兩個 LOT 各自算狀態。
    同號存在多個年份時**不合併**（無從判斷短號指哪一年，寧可分開列）。
    """
    if events is None or len(events) == 0:
        return events
    lots = set(events["lot"])
    years_by_num: dict = {}
    for lot in lots:
        m = re.fullmatch(r"LOT (\d+)-(\d+)", lot)
        if m:
            years_by_num.setdefault(m.group(1), set()).add(lot)
    mapping = {}
    for lot in lots:
        m = re.fullmatch(r"LOT (\d+)", lot)
        if m:
            variants = years_by_num.get(m.group(1)) or set()
            if len(variants) == 1:
                mapping[lot] = next(iter(variants))
    if not mapping:
        return events
    events = events.copy()
    events["lot"] = events["lot"].map(lambda x: mapping.get(x, x))
    return events


def _aggregate_by_lot(events):
    """逐事件 → 每個 LOT 一筆現況（最新狀態＋代表數量＋問題旗標＋信數）。

    現況狀態＝最新一封信的狀態；數量取所有信中最大一筆；缺料/延誤旗標跨全部信取 OR。
    聚合前先把無年份短號併入唯一的有年份 LOT（_canonicalize_lots）。
    排序：未到貨優先、再依最新日期新→舊。
    """
    if events is None or len(events) == 0:
        return []
    events = _canonicalize_lots(events)
    out = []
    for lot, g in events.groupby("lot"):
        g = g.sort_values("date")
        last = g.iloc[-1]
        qtys = [int(q) for q in g["qty"].tolist() if pd.notna(q)]
        qmax = max(qtys) if qtys else None
        unit = ""
        if qmax is not None:
            uu = g[g["qty"] == qmax]["unit"].tolist()
            unit = uu[0] if uu else ""
        out.append({
            "lot": lot, "status": str(last["status"]),
            "last_date": last["date"], "first_date": g.iloc[0]["date"],
            "last_summary": str(last["summary"]), "last_subject": str(last["subject"]),
            "qty": qmax, "unit": unit, "n": int(len(g)),
            "flag_short": bool(g["flag_short"].any()),
            "flag_delay": bool(g["flag_delay"].any()),
        })
    out.sort(key=lambda a: (a["status"] == "已到貨", -_epoch(a["last_date"])))
    return out


def _open_unarrived(lots, now, stale_days=21, limit=30):
    """聚合後清單 → 在途未到貨者，算停滯天數、最久未更新排前、取前 limit（純函式、注入 now）。"""
    open_lots = []
    for a in lots:
        if a["status"] not in _INTRANSIT:
            continue
        b = dict(a)
        b["stale"] = (now - a["last_date"]).days if pd.notna(a["last_date"]) else 999
        open_lots.append(b)
    open_lots.sort(key=lambda a: -a["stale"])
    return open_lots[:max(1, int(limit))]


def _fmt_qty(a) -> str:
    return f"，{a['qty']:,}{a['unit']}" if a.get("qty") else ""


def _fmt_flags(a) -> str:
    return "".join(f" 🔸{t}" for t, on in [("缺料", a.get("flag_short")), ("延誤", a.get("flag_delay"))] if on)


def _fmt_summary_line(a) -> str:
    """摘要縮排行。last_summary 來自信件主旨/LLM 摘要（untrusted）— 給 LLM 前必過 sanitize_for_llm。"""
    return f"\n     ↳ {sanitize_for_llm(a['last_summary'])}" if a.get("last_summary") else ""


def _fmt_lot_line(a) -> str:
    icon = _STATUS_ICON.get(a["status"], "•")
    d = str(a["last_date"])[:10] if pd.notna(a["last_date"]) else "?"
    head = f"{icon} {a['lot']}  [{a['status']}{_fmt_qty(a)}]  {d}（{a['n']} 封）{_fmt_flags(a)}"
    return head + _fmt_summary_line(a)


def query_material_arrival(query: str = "", days: int = 0, limit: int = 20) -> str:
    """查料批(LOT)的到貨進度 ——「LOT 217 的布到了沒／幾號到／數量多少」的首選工具。

    從採購信（內部 lake）抽每個 LOT 料批的到貨狀態，聚合成現況：最新狀態（已下單→已
    出貨→已到貨，含延誤／缺料旗標）＋信中數量(M)＋往來信數。**分流**：要「某型體的料各
    供應商備到哪」用 `check_material_readiness`（型體視角）；要採購信原始往來時間軸用
    `read_dept_email_timeline`；要倉庫即時庫存用 `read_warehouse_stock`。免 +確認。

    Args:
        query: LOT 號／客戶／材料／供應商關鍵字（如 'LOT 217'、'217-2026'、'Decathlon'、
               'NASTROTEX'）；留空＝最近有動態的料批。
        days:  只看近 N 天的信；0＝不限（預設）。
        limit: 最多回幾個 LOT（預設 20）。

    Returns:
        逐 LOT 的到貨現況（狀態＋數量＋最新摘要＋問題旗標），未到貨優先、再新→舊。
    """
    try:
        df = _load_lake_df()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀內部郵件 lake 失敗（{INTERNAL_PARQUET}）：{type(exc).__name__}: {exc}"
    lots = _aggregate_by_lot(_events_from_df(df, query=query, days=days))
    if not lots:
        return (f"查無含 LOT 號的採購信（query={(query or '').strip()!r}）。"
                "可放寬 query、或改用 read_dept_email_timeline('採購', query=…) 查無 LOT 的往來。")
    lots = lots[:max(1, int(limit))]
    q = (query or "").strip()
    head = f"📦 料到貨進度（採購信 LOT 視角，{'查:' + q if q else '最近動態'}，{len(lots)} 個 LOT，未到優先）"
    body = "\n".join(_fmt_lot_line(a) for a in lots)
    return f"{head}\n{body}\n（狀態源自採購信文字；確切到貨數量／單據以 Drive 採購夾或 ERP 為準）"


def material_arrival_overview(days: int = 120, stale_days: int = 21, limit: int = 30) -> str:
    """盤點「在途未到貨」的料批 —— 主動掃描哪些 LOT 的料還在路上、久未更新該追。

    給「最近哪些料還沒到／要催」的看板用：抽近 days 天採購信所有 LOT，只列**有材料物流
    訊號、未確認到貨**者（已下單／已出貨／延誤／缺料），最久沒更新排前面、超過 stale_days
    天標 🔴。**刻意排除純財務信**（付款／對帳／運費）與只能標「進行中」的模糊信，避免拿
    付款往來誤報成「料沒到」。不靠精確 ETA（信件 ETA 多為自然語言），以「久未回報」當可追
    訊號。免 +確認。

    Args:
        days:       盤點近 N 天的採購信（預設 120）。
        stale_days: 未到貨且最新信超過幾天 → 標「久未更新」🔴（預設 21）。
        limit:      最多列幾個 LOT（預設 30）。

    Returns:
        在途未到貨 LOT 清單（狀態＋最新摘要＋停滯天數），久未更新者標 🔴、其餘 🟡。
    """
    try:
        df = _load_lake_df()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀內部郵件 lake 失敗（{INTERNAL_PARQUET}）：{type(exc).__name__}: {exc}"
    lots = _aggregate_by_lot(_events_from_df(df, days=days))
    open_lots = _open_unarrived(lots, pd.Timestamp.now(), stale_days=stale_days, limit=limit)
    if not open_lots:
        return f"近 {days} 天採購信沒有「在途未到貨」的料批（已下單／出貨而未確認到貨者）。"
    n_stale = sum(1 for a in open_lots if a["stale"] >= stale_days)
    head = (f"🚨 在途未到貨料批盤點（近 {days} 天採購信，{len(open_lots)} 個在途未確認到貨，"
            f"其中 {n_stale} 個 ≥{stale_days} 天沒更新）")
    lines = []
    for a in open_lots:
        mark = "🔴" if a["stale"] >= stale_days else "🟡"
        d = str(a["last_date"])[:10] if pd.notna(a["last_date"]) else "?"
        line = f"{mark} {a['lot']}  [{a['status']}{_fmt_qty(a)}]  {a['stale']} 天沒更新（最新 {d}）{_fmt_flags(a)}"
        lines.append(line + _fmt_summary_line(a))
    tail = "（久未更新＝可能已到未回報或卡關，建議向採購／供應商確認）"
    return f"{head}\n" + "\n".join(lines) + f"\n{tail}"
