"""結構化讀倉庫庫存料表（Drive『00 Stock Data』料號庫存表）。

為什麼要這支工具（而不是 read_drive_file / RAG）：
  倉庫把每一類耗材各放一個 Excel（底料 PS、化學 CH/DM/GL/OL/PR、文具 BK/OT/CP…），
  每個檔的結構是「一個 `Data` 主檔分頁 ＋ 每個料號各一個進出帳分頁」。**真正在維護的
  現有庫存，是各料分頁頂端的結餘格**；`Data` 主檔的 `Stock` 欄常常是空的（實測底料 57
  個料只有 1 筆有數字）→ 盲讀主檔會把庫存全回成空白。read_drive_file 又會被幾十個分頁
  的純文字灌爆、表頭攤平糊掉，問不出「X 料還剩多少」。

這支工具直接解析、以「各料分頁結餘」為權威現有量、`Data` 主檔只補品名/單位/供應商/
架位/安全量等 metadata，輸出精簡的庫存清單。版型用「文字錨點」偵測（Stock No./Stock/
Forecast Stock），不寫死欄位位置；萬一版型位移會標 ⚠️ 提醒而非靜默算錯。

現有庫存(Stock) vs 預計庫存(Forecast Stock)：前者＝盤面現有量；後者＝含在途採購(PO)的
預計量。兩個都輸出，主打現有量。
"""
import io
import json
import re

# 找最新一個「00 Stock Data YYMMDD」根夾（月份夾會改名，故用 contains + 取最新，
# 不寫死 ID）。根夾下分類子夾：3底料/4化學/5工具/6文具/7小倉庫 等；OLD/Memo 略過。
_STOCK_ROOT_KEYWORD = "Stock Data"
_SKIP_SUBFOLDERS = {"OLD", "Memo", "memo", "old"}
_DATA_SHEET = "Data"
_SHEET_CAP = 400  # 單次最多解析幾個料分頁（避免一次掃多大檔拖太久）；超過會標明截斷。

_STOCK_FILE_RE = re.compile(r"\.(xls|xlsx|xlsm)$", re.I)
_CODE_IN_NAME_RE = re.compile(r"\(([A-Za-z]{2,3})\)")  # 檔名裡的料號碼，如 "(GL)"
_STOCK_NO_RE = re.compile(r"^\s*([A-Za-z]{2,3})\s*[\d.]")  # 料號樣式，如 GL02 / PS19.1


def _norm(s):
    """表頭/標籤比對：去空白、轉小寫、去點。"""
    return re.sub(r"\s+", "", str(s or "")).lower().replace(".", "")


def _num(v):
    """Excel 值 → float；NaN/空/取不出數字 → None。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f == f else None  # NaN guard（f==f 對 NaN 為 False）
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _clean_str(v):
    """字串欄清理：nan/none/空 → ''（避免顯示『架位 nan』）。"""
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() in ("nan", "none", "") else s


def _fmt_qty(v):
    if v is None:
        return "—"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}"


# ---- Drive 存取（自足，只靠 google_auth；不依賴 css-only 的 factory 模組）----
def _drive_service():
    from agent_core.google_auth import get_service
    return get_service("drive", "v3")


def _find_stock_root(service):
    """回 (folder_id, name) — 名稱含 'Stock Data'、修改日最新的資料夾。找不到回 (None, None)。"""
    safe = _STOCK_ROOT_KEYWORD.replace("\\", "\\\\").replace("'", "\\'")
    res = service.files().list(
        q=(f"name contains '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
           "and trashed = false"),
        pageSize=25, fields="files(id, name, modifiedTime)",
        includeItemsFromAllDrives=True, supportsAllDrives=True, corpora="allDrives",
    ).execute()
    items = res.get("files", [])
    if not items:
        return None, None
    items.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    return items[0]["id"], items[0]["name"]


def _children(service, folder_id, n=200):
    return service.files().list(
        q=f"'{folder_id}' in parents and trashed = false", pageSize=n,
        fields="files(id, name, mimeType, size)", orderBy="name",
        includeItemsFromAllDrives=True, supportsAllDrives=True, corpora="allDrives",
    ).execute().get("files", [])


def _list_stock_files(service, root_id):
    """走訪根夾各分類子夾，回每個料表檔 dict：{id,name,code,category}。
    略過 OLD/Memo 子夾、~$ 鎖檔與 <1KB 空殼。"""
    out = []
    for sub in _children(service, root_id):
        if not sub.get("mimeType", "").endswith("folder"):
            continue
        if sub.get("name", "").strip() in _SKIP_SUBFOLDERS:
            continue
        for f in _children(service, sub["id"]):
            name = f.get("name", "")
            if name.startswith("~$") or not _STOCK_FILE_RE.search(name):
                continue
            try:
                if int(f.get("size") or 0) < 1024:
                    continue
            except (TypeError, ValueError):
                pass
            m = _CODE_IN_NAME_RE.search(name)
            out.append({"id": f["id"], "name": name,
                        "code": (m.group(1).upper() if m else ""),
                        "category": sub.get("name", "")})
    return out


def _download_bytes(service, file_id):
    """下載 Drive 檔成 bytes（記憶體，不落地）。回 (bytes, meta)。"""
    from googleapiclient.http import MediaIoBaseDownload
    meta = service.files().get(
        fileId=file_id, fields="id,name,modifiedTime,mimeType,size",
        supportsAllDrives=True,
    ).execute()
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(
        buf, service.files().get_media(fileId=file_id, supportsAllDrives=True),
        chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _status, done = dl.next_chunk()
    return buf.getvalue(), meta


def _resolve_targets(service, root_id, category, material, file_id):
    """決定要讀哪些料表檔。回 (selected[list of file dict], all_files[list])。"""
    if file_id:
        return [{"id": file_id, "name": "", "code": "", "category": ""}], []
    all_files = _list_stock_files(service, root_id) if root_id else []
    cat = (category or "").strip()
    cat_is_code = bool(re.fullmatch(r"[A-Za-z]{2,3}", cat))  # 純碼(PS/GL)→精確比對，別用子字串
    code_from_mat = ""
    m = _STOCK_NO_RE.match(material or "")
    if m:
        code_from_mat = m.group(1).upper()
    sel = []
    for f in all_files:
        hay = (f["name"] + " " + f["category"]).lower()
        if code_from_mat and f["code"] == code_from_mat:
            sel.append(f)
        elif cat:
            if cat_is_code:
                if f["code"] == cat.upper():
                    sel.append(f)
            elif cat.lower() in hay:
                sel.append(f)
    # 去重（同檔可能因 code+category 雙中）
    seen, uniq = set(), []
    for f in sel:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq, all_files


# ---- 解析 ----
def _parse_data_sheet(df):
    """`Data` 主檔 → ({stock_no: meta dict}, warnings)。meta: name/unit/supplier/price/
    currency/shelf/min/master_stock。文字錨點抓表頭（含 'Stock No.'），不寫死欄位。"""
    meta = {}
    hdr_i = None
    for i in range(min(6, len(df))):
        if any(_norm(c) == "stockno" for c in df.iloc[i].tolist()):
            hdr_i = i
            break
    if hdr_i is None:
        return meta, ["Data 主檔找不到表頭（預期含 'Stock No.'）"]
    hdr = [_norm(c) for c in df.iloc[hdr_i].tolist()]

    def col(pred):
        for j, h in enumerate(hdr):
            if pred(h):
                return j
        return None

    cm = {
        "stock_no": col(lambda h: h == "stockno"),
        "name": col(lambda h: h == "productname"),
        "unit": col(lambda h: h == "unit"),
        "supplier": col(lambda h: h == "supplier"),
        "price": col(lambda h: h == "price"),
        "currency": col(lambda h: h == "currency"),
        "master_stock": col(lambda h: h == "stock"),
        # 架位＝Place(Shelf No.)；排除 Production Place(產地)——兩者都含 'place'
        "shelf": col(lambda h: "shelf" in h or ("place" in h and "production" not in h)),
        "min": col(lambda h: "minim" in h),
    }
    if cm["stock_no"] is None:
        return meta, ["Data 主檔表頭缺 'Stock No.' 欄"]

    for i in range(hdr_i + 1, len(df)):
        r = df.iloc[i].tolist()

        def cell(k, _r=r):
            j = cm.get(k)
            return _r[j] if (j is not None and j < len(_r)) else None

        sn = cell("stock_no")
        if sn is None or str(sn).strip() == "" or str(sn).strip().lower() == "nan":
            continue
        meta[str(sn).strip()] = {
            "name": _clean_str(cell("name")),
            "unit": _clean_str(cell("unit")),
            "supplier": _clean_str(cell("supplier")),
            "price": cell("price"),
            "currency": _clean_str(cell("currency")),
            "shelf": _clean_str(cell("shelf")),
            "min": _num(cell("min")),
            "master_stock": _num(cell("master_stock")),
        }
    return meta, []


def _sheet_balance(df4):
    """料分頁頂端結餘 → (current_stock, forecast_stock)。
    文字錨點：找 'Stock' / 'Forecast Stock' 標籤格，值在標籤上一列同欄。df4=該分頁前 4 列。"""
    stock = forecast = None
    nr = len(df4)
    for ri in range(1, min(4, nr)):  # 標籤多在第 2 列，值在第 1 列；故 ri 從 1 起
        row = df4.iloc[ri].tolist()
        for cj, cell in enumerate(row):
            n = _norm(cell)
            if not n:
                continue
            if "forecaststock" in n:
                if forecast is None:
                    forecast = _num(df4.iat[ri - 1, cj])
            elif n == "stock" and stock is None:
                stock = _num(df4.iat[ri - 1, cj])
    return stock, forecast


def _parse_stock_workbook(xlsx_bytes, material="", sheet_budget=_SHEET_CAP):
    """解析一個料表檔 → (rows[list of material dict], warnings, truncated[bool])。
    以各料分頁結餘為權威現有量；material 給定時只解析相符的料分頁（省時）。"""
    import pandas as pd
    warns = []
    try:
        xls = pd.ExcelFile(io.BytesIO(xlsx_bytes))
    except Exception as exc:  # noqa: BLE001
        return [], [f"解析 Excel 失敗：{type(exc).__name__}: {exc}"], False

    meta = {}
    if _DATA_SHEET in xls.sheet_names:
        try:
            meta, mw = _parse_data_sheet(xls.parse(_DATA_SHEET, header=None))
            warns += mw
        except Exception as exc:  # noqa: BLE001
            warns.append(f"Data 主檔解析失敗：{type(exc).__name__}")

    mat = (material or "").strip().lower()
    rows = []
    truncated = False
    parsed = 0
    for sn in xls.sheet_names:
        if sn == _DATA_SHEET:
            continue
        if not _STOCK_NO_RE.match(sn):  # 只認料號樣式的分頁，跳過雜項分頁
            continue
        info = meta.get(sn, {})
        name = info.get("name", "")
        if mat and mat not in sn.lower() and mat not in name.lower():
            continue
        if parsed >= sheet_budget:
            truncated = True
            break
        parsed += 1
        try:
            df4 = xls.parse(sn, header=None, nrows=4)
        except Exception:  # noqa: BLE001
            cur, fc = None, None
        else:
            cur, fc = _sheet_balance(df4)
        if cur is None:
            cur = info.get("master_stock")  # 分頁結餘缺 → 退回 Data 主檔數字
        # 略過全空殼料號（無品名/供應商、現有與預計皆 0/None、無安全量）——多為佔位分頁
        if (not name and not info.get("supplier") and not info.get("shelf")
                and not cur and not fc and info.get("min") is None):
            continue
        rows.append({
            "stock_no": sn, "name": name, "unit": info.get("unit", ""),
            "supplier": info.get("supplier", ""), "shelf": info.get("shelf", ""),
            "min": info.get("min"), "currency": info.get("currency", ""),
            "price": info.get("price"), "current": cur, "forecast": fc,
        })
    return rows, warns, truncated


def _disp_width(s):
    """顯示寬度（CJK 全形字算 2）—— 與 doc_export 同一套，供等寬欄位 padding。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def _pad(s, width, *, right=False):
    """把 s 補空白到顯示寬度 width；right=True 靠右（數字欄用）。"""
    s = str(s)
    gap = max(0, width - _disp_width(s))
    return (" " * gap + s) if right else (s + " " * gap)


def _status_label(cur, mn):
    """庫存狀態文字：現有量未知→未知；≤0→斷料；低於安全量→低於安全量；其餘→正常。

    現有量解析不到（cur is None）不能標「正常」—— 那是資料缺口不是健康訊號，
    標綠會讓人漏掉該盤點的料。
    """
    if cur is None:
        return "未知"
    if cur <= 0:
        return "斷料"
    if mn is not None and cur < mn:
        return "低於安全量"
    return "正常"


_STATUS_EMOJI = {"斷料": "🔴", "低於安全量": "🟡", "正常": "🟢", "未知": "⚪"}


def _status_emoji(cur, mn):
    """庫存燈號🔴🟡🟢 —— 與 _status_label 同源（閾值邏輯不雙寫）。"""
    return _STATUS_EMOJI[_status_label(cur, mn)]


def _incoming(r):
    """在途量＝預計(含 PO) − 現有。算不出回 None。"""
    cur, fc = r["current"], r["forecast"]
    if fc is None or cur is None:
        return None
    inc = fc - cur
    return inc if inc > 0 else 0


def _trailing(r, *, show_unit=False):
    """每列尾段自由文字：燈號 + 品名（+單位）+ 供應商 + 低量標記。

    刻意把中文品名/供應商與 emoji 放在「所有對齊欄之後」—— 中文與 emoji 在 Telegram 等寬
    字體下的字元寬度並非剛好 2 倍 ASCII，擺尾段才不會把前面的數字欄推歪。
    """
    cur, mn = r["current"], r["min"]
    seg = _status_emoji(cur, mn)
    if r.get("name"):
        seg += f" {r['name']}"
    if show_unit and r.get("unit"):
        seg += f"（{r['unit']}）"
    if r.get("supplier"):
        seg += f"／{r['supplier']}"
    if cur is not None and cur <= 0:
        seg += "　⚠️斷料"
    elif cur is not None and mn is not None and cur < mn:
        seg += "　⚠️低於安全量"
    return seg


def _render_aligned_table(rows, *, show_unit=False):
    """庫存清單 rows → 等寬對齊表（含 ``` 圍欄）。

    料號／現有／在途為純 ASCII 欄、靠 _pad 對齊（在 <pre> 裡完美對齊）；品名/供應商/燈號
    走尾段自由文字。回傳的 ``` 圍欄會被 tg_send 轉成 Telegram <pre> 等寬區塊呈現。
    """
    srt = sorted(rows, key=lambda x: str(x["stock_no"]))

    # 對齊欄一律純 ASCII（數字或 '-'）—— 不用 _fmt_qty 的全形「—」，那在等寬字體下寬度
    # 不一致會把欄位推歪。寬度與 _pad 都走 _disp_width，對 ASCII 即字元數，保證對齊。
    def cur_of(r):
        return "-" if r["current"] is None else _fmt_qty(r["current"])

    def inc_of(r):
        v = _incoming(r)
        return "-" if v is None else _fmt_qty(v)

    h_code, h_cur, h_inc = "料號", "現有", "在途"
    w_code = max([_disp_width(h_code)] + [_disp_width(r["stock_no"]) for r in srt])
    w_cur = max([_disp_width(h_cur)] + [_disp_width(cur_of(r)) for r in srt])
    w_inc = max([_disp_width(h_inc)] + [_disp_width(inc_of(r)) for r in srt])
    lines = ["```"]
    lines.append(f"{_pad(h_code, w_code)}  {_pad(h_cur, w_cur, right=True)}  "
                 f"{_pad(h_inc, w_inc, right=True)}  品名／供應商")
    for r in srt:
        lines.append(
            f"{_pad(r['stock_no'], w_code)}  {_pad(cur_of(r), w_cur, right=True)}  "
            f"{_pad(inc_of(r), w_inc, right=True)}  {_trailing(r, show_unit=show_unit)}"
        )
    lines.append("```")
    return lines


def _num_cell(v):
    """數字欄→Excel cell：整數值的 float 轉 int（免顯示 2160.0）、其餘原樣、None→''。"""
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return int(v)
    return v


def _category_listing_lines(all_files, root_name):
    """把料表檔清單整理成「可查的料類」清單行（discovery 與品名指引共用）。"""
    if not all_files:
        return [f"倉庫庫存夾『{root_name}』下找不到任何料表檔。"]
    by_cat = {}
    for f in all_files:
        by_cat.setdefault(f["category"], []).append(f"{f['code'] or '?'} {f['name']}")
    lines = [f"📦 倉庫庫存料表（{root_name}）可查的料類：", ""]
    for c in sorted(by_cat):
        lines.append(f"【{c}】")
        for it in sorted(by_cat[c]):
            lines.append(f"  ・{it}")
    return lines


# 匯出檔的完整欄位（比 Telegram 等寬表多：預計庫存／安全量／架位／單價／幣別／來源檔）
_EXPORT_COLUMNS = [
    "料號", "品名", "單位", "現有量", "在途量", "預計庫存",
    "安全量", "狀態", "供應商", "架位", "單價", "幣別", "來源檔",
]


def _build_export_spec(rows, *, title, used_files, as_of=""):
    """庫存 rows → export_report 的 content spec（含完整欄位的 table block）。

    純函式、不碰 I/O：把工具內部已算好的結構化 rows 攤成 Excel 欄位。數字欄走 _num_cell
    （保留數值好排序/加總）、狀態走 _status_label（文字，免 emoji 進儲存格）。
    """
    out_rows = []
    for r in sorted(rows, key=lambda x: str(x.get("stock_no") or "")):
        out_rows.append([
            r.get("stock_no") or "",
            r.get("name") or "",
            r.get("unit") or "",
            _num_cell(r.get("current")),
            _num_cell(_incoming(r)),
            _num_cell(r.get("forecast")),
            _num_cell(r.get("min")),
            _status_label(r.get("current"), r.get("min")),
            r.get("supplier") or "",
            r.get("shelf") or "",
            _num_cell(r.get("price")),
            r.get("currency") or "",
            r.get("_file") or "",
        ])
    subtitle_bits = []
    if as_of:
        subtitle_bits.append(f"資料截止：{as_of}")
    if used_files:
        subtitle_bits.append("來源：" + "、".join(dict.fromkeys(used_files)))
    return {
        "title": title,
        "subtitle": "；".join(subtitle_bits),
        "blocks": [
            {"type": "table", "title": "庫存明細",
             "columns": list(_EXPORT_COLUMNS), "rows": out_rows},
        ],
    }


def read_warehouse_stock(category: str = "", material: str = "",
                         file_id: str = "", export: str = "") -> str:
    """查倉庫庫存料表（Drive『00 Stock Data』料號庫存表）——某類/某料『現在還剩多少』。

    這是查「XX 料還剩多少」「化學/底料/膠水類庫存」「哪些料低於安全量」「某料在哪個架位/
    哪個供應商」的**首選**工具。直接解析倉庫各類耗材的 Excel 庫存表（每個料號一個進出帳
    分頁），以**各料分頁的結餘**為權威現有量——比 read_drive_file 可靠（那些檔幾十個分頁
    的純文字會被截斷糊掉），也比直接讀 `Data` 主檔準（主檔 Stock 欄常是空的）。免 +確認。

    料號分類碼：底料 PS/PC…、化學 CH(處理劑)/DM(溶劑)/GL(膠水)/OL(油漆)/PR(油墨)、
    文具 BK/OT/CP… 等。

    Args:
        category: 料類。可填**料號碼**（如 'GL'、'PS'）或**關鍵字**（中/英/越：'膠水'、
                  '化學'、'底料'、'Chemistry'、'文具'…）。會對應到該類的庫存檔。
        material: 只看某料。可填**料號**（如 'GL02'）或**品名關鍵字**（如 '黃膠'、'FJ-678'）。
                  填料號（如 'GL02'）時可不必填 category，會自動定位到該類檔；
                  填**品名關鍵字**時必須搭配 category（品名散在各檔內部、無法跨檔搜）。
        file_id:  指定 Drive 檔案 ID（進階用；繞過自動定位）。
        export:   要把結果**產成檔案直接傳 Telegram** 時填格式：'excel'／'word'／'pdf'／'all'
                  （可逗號分隔）。設了就**一步**產出欄位完整的檔（多含 預計庫存／安全量／架位／
                  單價／幣別／來源檔）傳給大王、回傳產檔摘要——**別再另外呼叫 export_report**。
                  留空（預設）＝回等寬對齊文字表。大王說「匯出／整理成／給我 excel」就帶 export='excel'。

    Returns:
        預設回等寬對齊的庫存表（料號／現有量／在途量 為對齊欄，尾段帶燈號🔴🟡🟢＋品名＋供應商）。
        表格本體用 ``` code fence 包住，Telegram 會渲染成等寬 <pre> 區塊讓欄位對齊 ——
        **請原樣輸出該區塊、不要改寫成 Markdown 表格**（Telegram 不渲染表格、會糊掉）。
        設了 export 則回產檔結果摘要（檔案已自動傳到 Telegram）。
        category 與 material 都留空時，列出有哪些料類可查。引用時標明「檔名」。
    """
    cat = (category or "").strip()
    mat = (material or "").strip()
    fid = (file_id or "").strip()
    try:
        service = _drive_service()
        root_id, root_name = (None, None)
        if not fid:
            root_id, root_name = _find_stock_root(service)
            if not root_id:
                return ("找不到倉庫庫存資料夾（名稱含『Stock Data』）。請確認 Drive，"
                        "或用 search_drive_files 找『00 Stock Data』後傳 file_id。")
        targets, all_files = _resolve_targets(service, root_id, cat, mat, fid)

        # material 是純品名關鍵字（非料號樣式）且沒給 category → 品名散在各料表檔
        # 內部（Data 主檔/分頁），無法跨檔定位。誠實說明需搭配 category、附料類清單，
        # 別默默回料類清單讓人誤以為「查無此料」。
        if (not targets and not fid and mat and not cat
                and not _STOCK_NO_RE.match(mat)):
            lines = [
                f"品名關鍵字（material={mat!r}）需搭配 category 參數才能定位庫存檔 ——",
                "品名只存在各料表檔內部，無法跨全部檔案搜尋。",
                f"用法：read_warehouse_stock(category='膠水', material={mat!r})。", "",
            ]
            lines += _category_listing_lines(all_files, root_name)
            return "\n".join(lines)

        # 都沒指定 → 列出可查的料類（discovery）
        if not targets and not fid:
            if not all_files:
                return f"倉庫庫存夾『{root_name}』下找不到任何料表檔。"
            lines = _category_listing_lines(all_files, root_name)
            lines.append("")
            lines.append("用法：read_warehouse_stock(category='膠水') 或 "
                         "read_warehouse_stock(material='GL02')。")
            return "\n".join(lines)

        if not targets:
            return (f"找不到對應的庫存檔（category={cat!r} material={mat!r}）。"
                    "用 read_warehouse_stock() 看有哪些料類。")

        all_rows = []
        warns = set()
        used_files = []
        budget = _SHEET_CAP
        any_trunc = False
        for t in targets:
            data, meta = _download_bytes(service, t["id"])
            fname = t["name"] or str(meta.get("name", ""))
            used_files.append(fname)
            rows, w, trunc = _parse_stock_workbook(data, material=mat, sheet_budget=budget)
            for r in rows:
                r["_file"] = fname
            all_rows += rows
            warns.update(w)
            any_trunc = any_trunc or trunc
            budget -= len(rows)
            if budget <= 0:
                any_trunc = True
                break

        if not all_rows:
            scope = f"category={cat!r} material={mat!r}"
            return (f"在 {('、'.join(used_files)) or '指定檔'} 裡查無相符料（{scope}）。"
                    "確認料號/關鍵字，或用 read_warehouse_stock() 看料類清單。")

        title = "📦 倉庫庫存"
        if mat:
            title += f"：{mat}"
        elif cat:
            title += f"：{cat}"

        # 匯出分支：把結構化 rows 直接產成 Excel/Word/PDF 並傳 Telegram（欄位完整、可排序），
        # 不回等寬文字表。大王說「匯出 excel／整理成 excel」時走這條（小紅帶 export='excel'）。
        if export.strip():
            from datetime import date
            from agent_core.doc_export import export_report
            spec = _build_export_spec(
                all_rows, title=title,
                used_files=list(dict.fromkeys(used_files)),
                as_of=date.today().isoformat())
            tag = (mat or cat or "全部").strip()
            res = export_report(
                json.dumps(spec, ensure_ascii=False),
                formats=export, filename=f"倉庫庫存_{tag}", deliver=True)
            return str(res)

        # 單位：同一類通常一致 → 放 legend；混用才逐列在尾段標。
        units = {u for r in all_rows if (u := r.get("unit"))}
        uniform_unit = next(iter(units)) if len(units) == 1 else ""
        lines = [title]
        lines.append(f"來源：{'、'.join(dict.fromkeys(used_files))}")
        legend = "現有＝盤面結餘；在途＝在途採購(PO)；🔴斷料 🟡低於安全量 🟢正常 ⚪庫存量未知"
        if uniform_unit:
            legend = f"單位 {uniform_unit}；" + legend
        lines.append(legend)
        if warns:
            lines.append("⚠️ 解析提醒：" + "；".join(sorted(warns)) + "（數字僅供參考，建議人工複核）")
        # 對齊表用 ``` 圍欄包起來 —— tg_send 會把它渲染成 Telegram <pre> 等寬區塊，
        # 欄位才會對齊（Telegram 不渲染 Markdown 表格）。請原樣輸出此區塊、勿改成表格。
        lines += _render_aligned_table(all_rows, show_unit=not uniform_unit)
        low_n = sum(1 for r in all_rows
                    if r["current"] is not None and r["min"] is not None
                    and r["current"] < r["min"])
        lines.append(f"共 {len(all_rows)} 個料號" + (f"，其中 {low_n} 個低於安全量 ⚠️" if low_n else ""))
        if any_trunc:
            lines.append(f"（已達單次解析上限 {_SHEET_CAP} 個料分頁，清單可能未完；用 category/"
                         "material 縮小範圍看完整。）")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀倉庫庫存料表失敗：{type(exc).__name__}: {exc}"
