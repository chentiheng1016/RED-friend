"""上傳文件的「完整讀取」：整張 Excel 表 / PDF 逐頁原文（員工通道）。

動機（2026-08-17 UserAng Richter 案，紀錄在 `var/logs/daemon-telegram-orange.log`）：
業務把 15 份客人規格單 PDF 與自己維護的 master 母表丟給橙色 bot 要一份開發追蹤
表。橙色能碰上傳檔的工具只有兩顆 —— `extract_uploaded_*_images`（只回報**有圖
的**儲存格 + 該列前 4 格）與 `parse_sample_order`（LLM 摘要成 3 欄、截前 9000
字、只列前 20 筆）。兩顆都不是「把這份檔讀完」，於是實際發生：

  * 母表 19 列 → 產出 18 列。掉的正是唯一「remarks 欄放文字、沒放圖」那列
    （``Davos | 7904 | Unspecified``）—— 整張表的列是跟著抽圖結果長出來的，
    沒有圖的列在 LLM 眼中不存在。
  * 母表 12 欄 → 產出 7 欄，寄出日期 / 追蹤號 / 發票號 / 目標日期靜默消失。
  * 要彙總 15 份 PDF 時一份都沒重讀，改用對話記憶裡的舊摘要；被 12,000 字元
    history 預算裁掉的那幾份（2001 / 2004 / 2004-motif / 5001）就整批漏掉，
    回覆卻寫「已將您上傳的 10 份規格單全部彙總」。

所以這個模組補的是「照著讀完、不做摘要」那一段：`read_uploaded_table` 回整張
表（每列帶**原始列號**，才對得回抽圖工具回報的 ``F11`` 這種儲存格），
`read_uploaded_pdf_text` 回逐頁原文。兩顆都不呼叫 LLM —— 摘要與判讀是下游的
事，這一層只負責「有什麼給什麼」，而且**截斷一定要講**：靜默截斷就是這個案子
的病灶本身。

安全邊界比照 `vision.analyze_uploaded_image` / `doc_images.extract_uploaded_*`：
共用 `doc_images._vet_uploaded` 那道閘（realpath 後只認 Telegram 上傳目錄底下的
檔），不新增任何可讀取的檔案範圍，差別只在同一批檔案拿回的是全文而不是圖。
"""
from __future__ import annotations

import datetime
import os

from agent_core.env_utils import env_int
from agent_core.logging_and_paths import logger

# 一次最多列幾列（超過就截斷並明講還剩幾列，不靜默吃掉）。
_MAX_ROWS_DEFAULT = 300
# 掃描上限：再大的表也不無限跑（只影響「總列數」統計，會照實標注）。
_MAX_SCAN_ROWS = 50_000
# 單格最多幾個字（備註欄動輒整段，全放會洗掉其他欄）。
_MAX_CELL_CHARS = 100
# 整份輸出上限（保護 LLM context；超過一樣要講）。
_MAX_OUTPUT_CHARS = 24_000
# PDF 全文上限。
_MAX_PDF_CHARS = 20_000
# 太大的活頁簿就不花時間解 in-cell 圖對應鏈（只影響 🖼️ 標示）。
_INCELL_SCAN_MAX_BYTES = 40 * 1024 * 1024

_TABLE_EXTS = (".xlsx", ".xlsm", ".csv")
_PDF_EXTS = (".pdf",)

# CSV 編碼嘗試順序（工廠的單子常是 Excel 另存的 cp950）。
_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp950", "big5")


def _max_rows(requested: int) -> int:
    if requested and requested > 0:
        return min(int(requested), _MAX_SCAN_ROWS)
    return env_int("RED_UPLOAD_TABLE_MAX_ROWS", _MAX_ROWS_DEFAULT,
                   min_value=1, max_value=_MAX_SCAN_ROWS)


def _fmt_cell(value) -> str:
    """儲存格值 → 顯示字串（日期去掉沒意義的 00:00:00、長字串截斷標 …）。"""
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        # Excel 的純日期欄讀出來是 00:00:00 的 datetime，留著只會洗掉版面。
        text = (value.date().isoformat()
                if (value.hour, value.minute, value.second) == (0, 0, 0)
                else value.isoformat(sep=" "))
    elif isinstance(value, datetime.date):
        text = value.isoformat()
    else:
        text = str(value)
    text = text.replace("\n", " ⏎ ").strip()
    if len(text) > _MAX_CELL_CHARS:
        return text[:_MAX_CELL_CHARS] + "…(截斷)"
    return text


def _incell_image_cells(xlsx_path: str) -> dict[str, set[tuple[int, int]]]:
    """{工作表: {(列, 欄), ...}}：哪些格子裡放的是「置於儲存格」的圖。

    這種格子用 openpyxl 讀值會拿到 ``#VALUE!``（看起來像壞掉的公式），標成
    🖼️ 才不會被下游當成資料錯誤，也才知道「這欄的圖要用
    extract_uploaded_excel_images 拿」。
    """
    try:
        if os.path.getsize(xlsx_path) > _INCELL_SCAN_MAX_BYTES:
            return {}
        from agent_core.doc_images import _incell_images
        found = _incell_images(xlsx_path)
    except Exception as e:  # noqa: BLE001 —— 標示用的加值資訊，失敗不影響讀表
        logger.debug("uploaded_docs 讀 in-cell 圖位置失敗：%s", e)
        return {}
    return {sheet: {(row, col) for row, col, _data in items}
            for sheet, items in found.items()}


def _row_cells(values, image_cells: set[tuple[int, int]], row_no: int) -> list[str]:
    cells: list[str] = []
    for idx, value in enumerate(values, 1):
        if (row_no, idx) in image_cells:
            cells.append("🖼️(圖)")
        else:
            cells.append(_fmt_cell(value))
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _read_csv_rows(path: str) -> tuple[list[list[str]], str]:
    """CSV → (每列的字串陣列, 用到的編碼)。都解不開就 raise。"""
    import csv
    last_error: Exception | None = None
    for encoding in _CSV_ENCODINGS:
        try:
            with open(path, encoding=encoding, newline="") as f:
                return [row for row in csv.reader(f)], encoding
        except UnicodeDecodeError as e:
            last_error = e
            continue
    raise ValueError(f"CSV 編碼判讀失敗（試過 {'/'.join(_CSV_ENCODINGS)}）：{last_error}")


def read_uploaded_table(file_path: str, sheet: str = "", max_rows: int = 0) -> str:
    """把「Telegram 上傳」的 Excel / CSV **整張表讀出來**（每列帶原始列號）。

    用途：客人或同事上傳一份表（master 追蹤表、樣品清單、對帳表…），要照它的
    內容做事之前，先用這顆把表讀完 —— 有幾列就是幾列、有幾欄就是幾欄。

    跟旁邊兩顆的分工（別搞混，選錯就會漏資料）：
      * 這顆 = 表格**文字內容**（全部的列與欄）。
      * ``extract_uploaded_excel_images`` = 表格裡**圖的本體**（只看得到有圖
        的儲存格）。要做「有圖的新表」是兩顆一起用：這顆給列，那顆給圖，
        用列號 / 款號對起來。
      * ``parse_sample_order`` = LLM 把樣品單摘要成款號/顏色/部位規格三欄，
        是**摘要**不是原表，不能拿來當「我讀完這張表了」。

    路徑限縮同 ``analyze_uploaded_image``：只收 Telegram 上傳目錄底下的
    .xlsx / .xlsm / .csv，請帶上傳訊息「路徑:」欄的完整路徑。

    Args:
        file_path: 上傳訊息「路徑:」給的完整檔案路徑。
        sheet: 只讀某個工作表；留空 = 全部工作表（CSV 忽略此參數）。
        max_rows: 每張工作表最多列幾列；0 = 用預設上限。

    Returns:
        工作表清單 + 每列一行（``r12: 值 | 值 | …``，列號就是 Excel 的列號）。
        被截斷或讀不到的部分會明講還剩幾列 —— 回覆使用者時要照這裡的列數對帳，
        不可以自行宣稱「已完整彙總」。
    """
    from agent_core.doc_images import _vet_uploaded
    real, err = _vet_uploaded(file_path, _TABLE_EXTS, "Excel/CSV")
    if err:
        return err

    limit = _max_rows(max_rows)
    name = os.path.basename(real)
    ext = os.path.splitext(real)[1].lower()

    try:
        if ext == ".csv":
            return _render_csv(real, name, limit)
        return _render_xlsx(real, name, sheet, limit)
    except Exception as e:  # noqa: BLE001 —— 回錯誤字串給 LLM，別讓工具整顆炸掉
        logger.exception("read_uploaded_table 失敗：%s", real)
        return f"❌ 讀表失敗：{type(e).__name__}: {str(e)[:160]}"


def _render_csv(real: str, name: str, limit: int) -> str:
    rows, encoding = _read_csv_rows(real)
    total = len(rows)
    lines = [f"📋 {name}（CSV、編碼 {encoding}、共 {total} 列）"]
    for i, row in enumerate(rows[:limit], 1):
        cells = [_fmt_cell(c) for c in row]
        while cells and cells[-1] == "":
            cells.pop()
        lines.append(f"  r{i}: " + " | ".join(cells))
    if total > limit:
        lines.append(f"  ⚠️ 還有 {total - limit} 列沒列出（max_rows 加大再讀一次；"
                     "**不要**把沒讀到的列當成不存在）。")
    else:
        lines.append(f"  （以上為全部 {total} 列，沒有省略）")
    return _clip("\n".join(lines))


def _render_xlsx(real: str, name: str, sheet: str, limit: int) -> str:
    import openpyxl

    image_cells = _incell_image_cells(real)
    wb = openpyxl.load_workbook(real, read_only=True, data_only=True)
    try:
        titles = [ws.title for ws in wb.worksheets]
        want = str(sheet or "").strip().lower()
        targets = [ws for ws in wb.worksheets
                   if not want or ws.title.strip().lower() == want]
        if want and not targets:
            return (f"❌ {name} 裡找不到工作表「{sheet}」。"
                    f"這份檔的工作表：{'、'.join(titles) or '（無）'}")

        lines = [f"📋 {name}（工作表 {len(titles)} 張：{'、'.join(titles)}）"]
        for ws in targets:
            lines.extend(_render_sheet(ws, image_cells.get(ws.title, set()), limit))
    finally:
        try:
            wb.close()
        except Exception:  # noqa: S110 —— finally 的 close 失敗無從補救
            pass

    lines.append("")
    lines.append("🛑 這是原表逐列抄錄：要據此做新表時，**來源列數必須跟你表上的"
                 "列數對得起來**，少一列就要講少了哪一列與原因。儲存格裡的圖以 "
                 "🖼️ 標示，圖的本體要用 extract_uploaded_excel_images 取"
                 "（它會回報每張圖錨在哪一列）。")
    return _clip("\n".join(lines))


def _render_sheet(ws, image_cells: set[tuple[int, int]], limit: int) -> list[str]:
    """單一工作表 → 輸出行；空白列跳過但**列號保留**（要對得回儲存格位置）。"""
    shown: list[str] = []
    data_rows = 0
    scanned = 0
    truncated = False
    max_cols = 0
    for row_no, values in enumerate(ws.iter_rows(values_only=True), 1):
        scanned += 1
        if scanned > _MAX_SCAN_ROWS:
            truncated = True
            break
        cells = _row_cells(values, image_cells, row_no)
        if not cells:
            continue                      # 全空列不佔額度，列號照原樣不重編
        data_rows += 1
        max_cols = max(max_cols, len(cells))
        if len(shown) < limit:
            shown.append(f"  r{row_no}: " + " | ".join(cells))

    out = ["", f"── 工作表「{ws.title}」：{data_rows} 列有資料、最多 {max_cols} 欄 ──"]
    out.extend(shown)
    hidden = data_rows - len(shown)
    if hidden > 0 or truncated:
        out.append(f"  ⚠️ 還有 {hidden} 列沒列出（max_rows 加大、或用 sheet 指定"
                   "單張再讀一次；**不要**把沒讀到的列當成不存在）。")
    else:
        out.append(f"  （以上為這張表全部 {data_rows} 列，沒有省略）")
    return out


def _clip(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return (text[:_MAX_OUTPUT_CHARS]
            + f"\n\n⚠️ 輸出超過 {_MAX_OUTPUT_CHARS} 字元已截斷 —— 後面的內容"
              "**還沒讀到**，請用 sheet / max_rows 分批讀完再彙總，不可以就這樣"
              "宣稱已完整。")


def read_uploaded_pdf_text(pdf_path: str, pages: str = "") -> str:
    """把「Telegram 上傳」的 PDF **逐頁原文**讀出來（不摘要、不判讀）。

    用途：客人規格單 / 報價單 / 通知書要照著抄進表格或回覆時，先用這顆拿原文。
    要彙總多份 PDF 時**每一份都要各自呼叫一次**——不可以拿對話記憶裡先前的
    摘要充數（記憶會被裁掉，被裁掉的那幾份就會整批漏掉且完全看不出來）。

    跟旁邊兩顆的分工：這顆給**文字**；``extract_uploaded_pdf_images`` 給**圖**；
    ``parse_sample_order`` 是 LLM 摘要（只抽款號/顏色/部位規格三欄），不能當
    「我讀完這份 PDF 了」。

    路徑限縮同 ``analyze_uploaded_image``：只收 Telegram 上傳目錄底下的 .pdf，
    請帶上傳訊息「路徑:」欄的完整路徑。

    Args:
        pdf_path: 上傳訊息「路徑:」給的完整 PDF 路徑。
        pages: 只讀某幾頁，如 "1" / "1-3,5"；留空 = 全部（最多 40 頁）。

    Returns:
        每頁一段原文。抽不到文字（掃描件 / 純圖 PDF）會直說 —— 這時候要照實
        回覆讀不到，**不可以憑檔名或既有印象臆測內容**。
    """
    from agent_core.doc_images import _parse_pages, _vet_uploaded
    real, err = _vet_uploaded(pdf_path, _PDF_EXTS, "PDF")
    if err:
        return err

    name = os.path.basename(real)
    try:
        from pypdf import PdfReader
        reader = PdfReader(real)
        n_pages = len(reader.pages)
        indices = _parse_pages(pages, n_pages)
        chunks: list[str] = [f"📄 {name}（共 {n_pages} 頁，以下為原文逐頁抄錄）"]
        empty: list[int] = []
        for i in indices:
            try:
                text = (reader.pages[i].extract_text() or "").strip()
            except Exception as e:  # noqa: BLE001 —— 單頁壞掉不該讓整份失敗
                logger.debug("read_uploaded_pdf_text 第 %d 頁抽取失敗：%s", i + 1, e)
                text = ""
            if not text:
                empty.append(i + 1)
                continue
            chunks.append(f"\n── 第 {i + 1} 頁 ──\n{text}")
    except Exception as e:  # noqa: BLE001
        logger.exception("read_uploaded_pdf_text 失敗：%s", real)
        return f"❌ 讀 PDF 失敗：{type(e).__name__}: {str(e)[:160]}"

    if len(chunks) == 1:
        return (f"⚠️ {name} 抽不到任何文字（多半是掃描件或純圖 PDF）。"
                "圖要用 extract_uploaded_pdf_images 取；文字內容請照實回覆"
                "「這份讀不到文字」，**不可以憑檔名或印象臆測內容**。")
    if empty:
        chunks.append(f"\n⚠️ 第 {'、'.join(str(p) for p in empty)} 頁抽不到文字"
                      "（掃描件或純圖），這幾頁的內容**還沒讀到**，不可臆測。")
    if len(indices) < n_pages:
        chunks.append(f"\n（本次只讀了 {len(indices)}/{n_pages} 頁；其餘頁面尚未讀取。）")
    return _clip_pdf("\n".join(chunks))


def _clip_pdf(text: str) -> str:
    if len(text) <= _MAX_PDF_CHARS:
        return text
    return (text[:_MAX_PDF_CHARS]
            + f"\n\n⚠️ 原文超過 {_MAX_PDF_CHARS} 字元已截斷 —— 後面的頁面"
              "**還沒讀到**，請用 pages 分批讀完，不可以就這樣宣稱已完整。")
