"""文件 → 圖片抽取：把規格單 / 型錄 PDF 與客人 Excel 母表裡的產品圖挖成獨立圖檔。

動機（2026-08-12 UserAng 案）：業務把 15 份客人規格單 PDF 丟給小紅，要一份
開發追蹤表（tracking log）Excel。欄位小紅產得出來，但客人原始表格的
「remarks / Bemerkungen」那欄擺的是**產品圖**——小紅只能填「見 2001.pdf 第
1 頁」這種文字對照，回覆裡還親口說「無法將 PDF 內部的繪圖與照片直接嵌入
Excel 儲存格」。

這個模組補前半段（把圖挖出來），doc_export 的圖片儲存格補後半段（把圖塞進
格子）。兩段合起來就是「規格單 / 母表 → 有圖的 tracking log」。

**PDF** 抽法兩段式：
  1. **內嵌點陣圖**（pypdf ``page.images``）—— 客人規格單多半直接貼照片，
     抽得到原圖、不失真。小圖（logo / icon / 分隔線）與跨頁重複的頁首頁尾圖
     會被濾掉，剩下的依面積排序（產品照通常是頁上最大的那張）。
  2. **整頁 render + 裁白邊**（pypdfium2 + Pillow）—— 純向量繪圖的頁抽不到
     內嵌圖（Telegram 那則回覆講的「繪圖」就是這種），退而求其次把整頁畫出來
     再裁掉四周空白，至少拿得到那張圖。

**Excel**（客人已經做好、圖貼在格子裡的 master 表）走 openpyxl 的 drawing
anchor：每張圖都帶「錨在哪一格」，所以能**連同該列的款號一起回報**
（`2001A | Magic forest viola`）。這是這條路的重點 —— 一堆沒有出處的圖檔對
不回款號，等於沒用；有了列標籤，LLM 才能把舊表的圖搬進新表的對應列。

安全邊界：``extract_uploaded_*`` 系列是員工通道的受限版本，比照
``vision.analyze_uploaded_image`` —— 只讀 Telegram 上傳目錄底下的檔，
產出寫到 ``var/data/doc_images/dept/<色>/``（per-color 分艙，理由同
daemon_telegram 的 [[TG_FILE:]] 白名單：頂層混著別人的圖）。
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from agent_core.env_utils import env_int
from agent_core.logging_and_paths import DATA_DIR, logger

# 抽出來的圖一律落這裡（doc_export 的圖片儲存格白名單也認這個根）。
DOC_IMAGES_DIR = os.path.join(DATA_DIR, "doc_images")

# 濾掉 logo / icon / 分隔線：長寬任一邊小於這個像素數就丟。
_MIN_PX_DEFAULT = 120
# 一份 PDF 最多抽幾張（防型錄類 PDF 一次吐幾百張塞爆磁碟與 LLM context）。
_MAX_IMAGES_DEFAULT = 12
# 向量頁 fallback 的 render 解析度。
_RENDER_DPI_DEFAULT = 150
# 單張圖上限（超過就不存；Excel 嵌圖也不需要這麼大）。
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
# 一份 PDF 最多翻幾頁找圖（型錄 PDF 動輒上百頁，翻完很慢）。
_MAX_PAGES_SCANNED = 40

# Excel 母表的縮圖常常本來就不大（客人自己壓過），門檻放寬到只擋 icon/分隔線。
_XLSX_MIN_PX_DEFAULT = 48
# 一份 Excel 最多抽幾張（master 追蹤表一頁幾十列很正常，比 PDF 放寬）。
_XLSX_MAX_IMAGES_DEFAULT = 60
# 列標籤取該列前幾個非空文字格（款號/顏色通常在最前面幾欄）。
_ROW_LABEL_CELLS = 4
_ROW_LABEL_MAX_LEN = 80
# 「有資料但沒圖」的列最多列幾筆（多的叫下游去讀整張表，別洗版）。
_MAX_GAP_ROWS_REPORTED = 20

_PDF_EXT = ".pdf"
_XLSX_EXTS = (".xlsx", ".xlsm")


def _min_px() -> int:
    return env_int("RED_PDF_IMAGE_MIN_PX", _MIN_PX_DEFAULT, min_value=1, max_value=5000)


def _max_images() -> int:
    return env_int("RED_PDF_IMAGE_MAX", _MAX_IMAGES_DEFAULT, min_value=1, max_value=200)


def _render_dpi() -> int:
    return env_int("RED_PDF_IMAGE_RENDER_DPI", _RENDER_DPI_DEFAULT,
                   min_value=36, max_value=600)


def _safe_stem(path: str) -> str:
    """PDF 檔名 → 安全的目錄／檔名前綴（CJK 保留，其餘非字元換底線）。"""
    from agent_core.path_safety import safe_cjk_filename
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    return safe_cjk_filename(stem, max_len=60, fallback="pdf")


def _parse_pages(spec: str, n_pages: int) -> list[int]:
    """'1-3,5' → [0,1,2,4]（0-indexed）。空字串 = 全部（上限 _MAX_PAGES_SCANNED）。"""
    if not (spec or "").strip():
        return list(range(min(n_pages, _MAX_PAGES_SCANNED)))
    out: set[int] = set()
    for part in re.split(r"[,\s、，]+", spec.strip()):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = max(1, int(a)), min(n_pages, int(b))
            except ValueError:
                continue
            out.update(range(lo - 1, hi))
        else:
            try:
                i = int(part)
            except ValueError:
                continue
            if 1 <= i <= n_pages:
                out.add(i - 1)
    return sorted(out)[:_MAX_PAGES_SCANNED]


def _pil_from_embedded(img: Any):
    """pypdf ImageFile → (PIL image, raw bytes)；解不開回 (None, None)。"""
    try:
        pil = img.image
    except Exception:  # 少見色彩空間 / 壞流 —— 跳過這張就好
        return None, None
    data = getattr(img, "data", b"") or b""
    return pil, data


def _save_image(pil, out_path: str) -> bool:
    """存成 PNG（RGBA 保留透明；CMYK/P 轉 RGB 免 PNG 存檔失敗）。"""
    try:
        if pil.mode in ("CMYK", "P", "LA", "PA"):
            pil = pil.convert("RGBA" if pil.mode in ("LA", "PA") else "RGB")
        pil.save(out_path, format="PNG")
    except Exception as e:
        logger.debug("doc_images 存檔失敗 %s：%s", out_path, e)
        return False
    if os.path.getsize(out_path) > _MAX_IMAGE_BYTES:
        try:
            os.remove(out_path)
        except OSError:
            pass
        return False
    return True


def _autocrop(pil):
    """裁掉四周同色留白（整頁 render 的圖幾乎都是一大片白底 + 中間一張圖）。"""
    try:
        from PIL import Image, ImageChops
        rgb = pil.convert("RGB")
        bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
        bbox = ImageChops.difference(rgb, bg).getbbox()
    except Exception:
        return pil
    if not bbox:
        return pil
    # 留一點邊，裁完太小（整頁幾乎全白）就維持原圖
    x0, y0, x1, y1 = bbox
    pad = 8
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(pil.size[0], x1 + pad), min(pil.size[1], y1 + pad)
    if (x1 - x0) < 32 or (y1 - y0) < 32:
        return pil
    try:
        return pil.crop((x0, y0, x1, y1))
    except Exception:
        return pil


def _render_page(pdf_path: str, page_index: int):
    """整頁 render 成 PIL image（向量繪圖頁的 fallback）；失敗回 None。"""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            return pdf[page_index].render(scale=_render_dpi() / 72).to_pil()
        finally:
            try:
                pdf.close()
            except Exception:  # noqa: S110 —— finally 裡的資源釋放，失敗無從補救
                pass           #    也無人可通知；記 log 只會在每頁失敗時洗版
    except Exception as e:
        logger.debug("doc_images render 第 %d 頁失敗：%s", page_index + 1, e)
        return None


def extract_pdf_images(
    pdf_path: str,
    out_dir: str,
    *,
    pages: str = "",
    min_px: int | None = None,
    max_images: int | None = None,
    fallback_render: bool = True,
) -> list[dict]:
    """把 PDF 裡的圖抽成 PNG 檔，回傳 [{path,page,width,height,bytes,source}]。

    source: "embedded"（內嵌點陣圖，原圖不失真）/ "rendered"（整頁畫出來裁白邊）。
    清單依「頁碼 → 面積大到小」排序 —— 同一頁的第一筆通常就是產品照。
    路徑安全由 caller 負責（本函式只讀 pdf_path、只寫 out_dir）。
    """
    from pypdf import PdfReader

    min_px = _min_px() if min_px is None else max(1, int(min_px))
    max_images = _max_images() if max_images is None else max(1, int(max_images))
    os.makedirs(out_dir, exist_ok=True)
    stem = _safe_stem(pdf_path)

    reader = PdfReader(pdf_path)
    indices = _parse_pages(pages, len(reader.pages))

    # 先蒐集候選（含 hash），全部掃完才知道哪些是跨頁重複的頁首頁尾圖。
    candidates: list[dict] = []
    for pno in indices:
        try:
            page_images = list(reader.pages[pno].images)
        except Exception as e:
            logger.debug("doc_images 讀第 %d 頁內嵌圖失敗：%s", pno + 1, e)
            page_images = []
        for img in page_images:
            pil, data = _pil_from_embedded(img)
            if pil is None:
                continue
            w, h = pil.size
            if w < min_px or h < min_px:
                continue
            digest = hashlib.sha256(data).hexdigest() if data else \
                hashlib.sha256(pil.tobytes()).hexdigest()
            candidates.append({"page": pno + 1, "pil": pil, "w": w, "h": h,
                               "digest": digest, "source": "embedded"})

    # 跨頁重複（同一張圖出現在 2 頁以上）= 頁首頁尾 logo / 浮水印 → 整組丟掉。
    seen_pages: dict[str, set[int]] = {}
    for c in candidates:
        seen_pages.setdefault(c["digest"], set()).add(c["page"])
    repeated = {d for d, pgs in seen_pages.items() if len(pgs) > 1}
    kept = [c for c in candidates if c["digest"] not in repeated]

    # 同頁內同圖去重（同一張貼兩次）
    deduped: list[dict] = []
    seen_digests: set[str] = set()
    for c in kept:
        if c["digest"] in seen_digests:
            continue
        seen_digests.add(c["digest"])
        deduped.append(c)

    # 沒有內嵌圖的頁（純向量繪圖）→ 整頁 render 裁白邊
    if fallback_render:
        covered = {c["page"] for c in deduped}
        for pno in indices:
            if (pno + 1) in covered or len(deduped) >= max_images:
                continue
            pil = _render_page(pdf_path, pno)
            if pil is None:
                continue
            pil = _autocrop(pil)
            deduped.append({"page": pno + 1, "pil": pil, "w": pil.size[0],
                            "h": pil.size[1], "digest": "", "source": "rendered"})

    deduped.sort(key=lambda c: (c["page"], -(c["w"] * c["h"])))

    out: list[dict] = []
    for c in deduped:
        if len(out) >= max_images:
            break
        idx = len(out) + 1
        path = os.path.join(out_dir, f"{stem}_p{c['page']:02d}_{idx:02d}.png")
        if not _save_image(c["pil"], path):
            continue
        out.append({"path": path, "page": c["page"], "width": c["w"],
                    "height": c["h"], "bytes": os.path.getsize(path),
                    "source": c["source"]})
    return out


# ────────────────────────────────────────────────────────────────────
# Excel：抽貼在儲存格裡的圖（含「錨在哪一列」，才對得回款號）
# ────────────────────────────────────────────────────────────────────
def _anchor_cell(img) -> tuple[int, int]:
    """圖的錨點 → (row, col)，皆 1-indexed；拿不到（AbsoluteAnchor）回 (0, 0)。"""
    anchor = getattr(img, "anchor", None)
    frm = getattr(anchor, "_from", None)
    if frm is None:
        return 0, 0
    try:
        return int(frm.row) + 1, int(frm.col) + 1
    except (TypeError, ValueError):
        return 0, 0


def _row_label(ws, row: int, skip_col: int) -> str:
    """該列的辨識字串：取前幾個非空文字格（款號 / 顏色通常在最前面幾欄）。

    圖檔本身認不出是哪一款，這個標籤就是把舊表的圖搬進新表時唯一的對照依據。
    """
    if row < 1:
        return ""
    values = [ws.cell(row, col).value
              for col in range(1, min(ws.max_column, 30) + 1)]
    return _row_label_from_values(values, skip_col)


def _row_label_from_values(values, skip_col: int) -> str:
    """同 ``_row_label``，但吃一整列的值（``iter_rows(values_only=True)`` 用）。

    ``skip_col`` 是 1-based 欄號（放圖的那欄要跳過，否則標籤會被 ``#VALUE!``
    這種圖格佔掉）。
    """
    parts: list[str] = []
    for col, val in enumerate(values, 1):
        if col == skip_col:
            continue
        if val is None or str(val).strip() == "":
            continue
        parts.append(str(val).strip())
        if len(parts) >= _ROW_LABEL_CELLS:
            break
    label = " | ".join(parts)
    return label[:_ROW_LABEL_MAX_LEN]


def rows_without_images(xlsx_path: str, items: list[dict],
                        sheet: str = "") -> list[tuple[str, int, str]]:
    """資料區間內「有資料但沒有圖」的列 → [(工作表, 列號, 該列標籤)]。

    為什麼要有這顆（2026-08-17 UserAng 案）：抽圖工具只回報**有圖的**儲存格，
    於是下游用抽圖結果當列表做新表時，母表裡「remarks 放文字不放圖」的那列
    整列消失，19 列變 18 列且完全看不出來。這裡把那幾列挑出來一起回報，讓
    LLM 至少知道「這列存在、只是沒有圖」。

    只看**第一張圖到最後一張圖之間**的列：表頭與表尾註記本來就沒有圖，全報
    出來只會洗版。這張表本來就一張圖都沒有時回空（沒有資料區間可言）。
    """
    import openpyxl

    by_sheet: dict[str, list[dict]] = {}
    for it in items:
        if it.get("row"):
            by_sheet.setdefault(it["sheet"], []).append(it)
    if not by_sheet:
        return []

    want = str(sheet or "").strip().lower()
    gaps: list[tuple[str, int, str]] = []
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            if want and ws.title.strip().lower() != want:
                continue
            sheet_items = by_sheet.get(ws.title)
            if not sheet_items:
                continue
            img_rows = {it["row"] for it in sheet_items}
            lo, hi = min(img_rows), max(img_rows)
            # 圖欄以出現最多次的欄為準（同一欄放圖是常態；有雜圖也不會誤判）。
            cols = [it["col"] for it in sheet_items if it.get("col")]
            skip_col = max(set(cols), key=cols.count) if cols else 0
            for row_no, values in enumerate(ws.iter_rows(values_only=True), 1):
                if row_no > hi:
                    break
                if row_no < lo or row_no in img_rows:
                    continue
                label = _row_label_from_values(values, skip_col)
                if label:
                    gaps.append((ws.title, row_no, label))
    except Exception as e:  # noqa: BLE001 —— 加值資訊，失敗不該讓抽圖整顆失敗
        logger.debug("doc_images 盤點無圖列失敗：%s", e)
        return []
    finally:
        try:
            wb.close()
        except Exception:  # noqa: S110 —— finally 的 close 失敗無從補救
            pass
    return gaps


# ── 「置於儲存格」的圖（Excel rich value，openpyxl 看不到）─────────────
# 2026-08-12 實測 UserAng 那份 master 追蹤表就是這型：18 張圖全在 xl/media/，
# 但 xl/drawings/ 是空的 —— 新版 Excel 的「插入圖片 → 置於儲存格」不走 drawing
# anchor，改存成 rich value，openpyxl 的 ws._images 完全看不到（回 0 張）。
# 對應鏈（每一段任一環缺就整組放棄、退回 drawing 那條）：
#   <c r="F2" vm="1">  → metadata.xml valueMetadata bk[vm-1] → rc/@v
#     → metadata.xml futureMetadata[XLRICHVALUE] bk[v] → xlrd:rvb/@i
#     → richData/rdrichvalue.xml rv[i] 第一個 <v>  → richValueRel 序號
#     → richData/richValueRel.xml rel[序號]/@r:id → _rels 對到 ../media/xxx
_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_RICH = "{http://schemas.microsoft.com/office/spreadsheetml/2017/richdata}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKGREL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _part_path(target: str, base: str) -> str:
    """OPC relationship Target → zip 內的 part 路徑。

    ⚠️ 兩種寫法都合法且實務上都會遇到：Excel 寫相對（``../media/image1.png``、
    ``worksheets/sheet1.xml``），openpyxl 寫**絕對** part 名（``/xl/worksheets/
    sheet1.xml``）。只處理相對的話，openpyxl 產出的檔會靜默抽不到圖。
    """
    t = (target or "").strip()
    if not t:
        return ""
    if t.startswith("/"):
        return t.lstrip("/")
    return os.path.normpath(os.path.join(base, t)).replace(os.sep, "/")


def _rich_media_by_index(zf) -> list[str]:
    """richValueRel 序號 → zip 內的 media 路徑（照 rel 出現順序）。"""
    from xml.etree import ElementTree as ET
    rels: dict[str, str] = {}
    for node in ET.fromstring(zf.read("xl/richData/_rels/richValueRel.xml.rels")):
        rid, target = node.get("Id"), node.get("Target") or ""
        if rid and target:
            rels[rid] = _part_path(target, "xl/richData/")
    out: list[str] = []
    for rel in ET.fromstring(zf.read("xl/richData/richValueRel.xml")):
        out.append(rels.get(rel.get(f"{_NS_REL}id") or "", ""))
    return out


def _vm_to_media(zf) -> dict[int, str]:
    """儲存格的 vm 值（1-based）→ media 路徑。任何一環對不上就回空 dict。"""
    from xml.etree import ElementTree as ET
    try:
        media = _rich_media_by_index(zf)
        if not media:
            return {}
        # rv[i] 的第一個 <v> = richValueRel 序號
        rv_to_rel: list[int] = []
        for rv in ET.fromstring(zf.read("xl/richData/rdrichvalue.xml")):
            vs = list(rv)
            rv_to_rel.append(int(vs[0].text) if vs and (vs[0].text or "").strip().isdigit() else -1)

        meta = ET.fromstring(zf.read("xl/metadata.xml"))
        # futureMetadata[XLRICHVALUE] bk[j] → rich value 索引
        fut: list[int] = []
        for fm in meta.iter(f"{_NS_MAIN}futureMetadata"):
            if fm.get("name") != "XLRICHVALUE":
                continue
            for bk in fm.findall(f"{_NS_MAIN}bk"):
                idx = -1
                for rvb in bk.iter(f"{_NS_RICH}rvb"):
                    raw = rvb.get("i")
                    idx = int(raw) if raw and raw.isdigit() else -1
                fut.append(idx)
        # valueMetadata bk[vm-1] → futureMetadata 索引
        out: dict[int, str] = {}
        vmeta = meta.find(f"{_NS_MAIN}valueMetadata")
        for vm_index, bk in enumerate(vmeta if vmeta is not None else [], start=1):
            rc = bk.find(f"{_NS_MAIN}rc")
            if rc is None:
                continue
            raw = rc.get("v")
            j = int(raw) if raw and raw.isdigit() else -1
            if not (0 <= j < len(fut)):
                continue
            rv_i = fut[j]
            if not (0 <= rv_i < len(rv_to_rel)):
                continue
            rel_i = rv_to_rel[rv_i]
            if 0 <= rel_i < len(media) and media[rel_i]:
                out[vm_index] = media[rel_i]
        return out
    except Exception as e:
        logger.debug("doc_images 讀 in-cell 圖對應表失敗：%s", e)
        return {}


def _incell_images(xlsx_path: str) -> dict[str, list[tuple[int, int, bytes]]]:
    """{工作表名: [(row, col, 圖片位元組), ...]}；沒有 in-cell 圖就回空 dict。"""
    import re
    import zipfile
    from xml.etree import ElementTree as ET

    from openpyxl.utils import column_index_from_string

    out: dict[str, list[tuple[int, int, bytes]]] = {}
    try:
        with zipfile.ZipFile(xlsx_path) as zf:
            names = set(zf.namelist())
            if "xl/metadata.xml" not in names or "xl/richData/richValueRel.xml" not in names:
                return {}
            vm_media = _vm_to_media(zf)
            if not vm_media:
                return {}
            # 工作表名 → sheetN.xml
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            wb_rels = {r.get("Id"): (r.get("Target") or "")
                       for r in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))}
            for sh in wb.iter(f"{_NS_MAIN}sheet"):
                title = sh.get("name") or ""
                part = _part_path(wb_rels.get(sh.get(f"{_NS_REL}id") or "", ""), "xl/")
                if not part or part not in names:
                    continue
                xml = zf.read(part).decode("utf-8", "replace")
                found: list[tuple[int, int, bytes]] = []
                for ref, vm in re.findall(r'<c r="([A-Z]+\d+)"[^>]*?\bvm="(\d+)"', xml):
                    path = vm_media.get(int(vm))
                    if not path or path not in names:
                        continue
                    m = re.match(r"([A-Z]+)(\d+)", ref)
                    if not m:
                        continue
                    found.append((int(m.group(2)),
                                  column_index_from_string(m.group(1)),
                                  zf.read(path)))
                if found:
                    out[title] = found
    except Exception as e:
        logger.debug("doc_images 讀 in-cell 圖失敗：%s", e)
        return {}
    return out


def extract_xlsx_images(
    xlsx_path: str,
    out_dir: str,
    *,
    sheet: str = "",
    min_px: int | None = None,
    max_images: int | None = None,
) -> list[dict]:
    """把 Excel 裡貼在儲存格上的圖抽成圖檔（兩種擺法都吃）。

    source 欄位分辨來源：``xlsx``＝浮動圖（drawing anchor）、``xlsx-incell``＝
    新版 Excel 的「置於儲存格」（rich value；openpyxl 完全看不到，走 zip 解析）。

    回傳 [{path, sheet, cell, row, col, label, width, height, bytes, source}]，
    依（工作表 → 列 → 欄）排序。``label`` 是該列前幾個非空文字格（款號/顏色），
    給下游把圖對回品項用 —— 沒有它，一堆圖檔對不回是哪一款。
    路徑安全由 caller 負責（本函式只讀 xlsx_path、只寫 out_dir）。
    """
    import openpyxl
    from openpyxl.utils import get_column_letter

    min_px = (_XLSX_MIN_PX_DEFAULT if min_px is None else max(1, int(min_px)))
    max_images = (env_int("RED_XLSX_IMAGE_MAX", _XLSX_MAX_IMAGES_DEFAULT,
                          min_value=1, max_value=500)
                  if max_images is None else max(1, int(max_images)))
    os.makedirs(out_dir, exist_ok=True)
    stem = _safe_stem(xlsx_path)

    # 圖是 drawing 物件，read_only 模式讀不到 —— 這裡不能圖快用 read_only=True。
    wb = openpyxl.load_workbook(xlsx_path)
    want = str(sheet or "").strip().lower()
    # 「置於儲存格」的圖 openpyxl 完全看不到，另外從 zip 撈（見上方對應鏈註解）
    incell = _incell_images(xlsx_path)

    out: list[dict] = []
    try:
        for ws in wb.worksheets:
            if want and ws.title.strip().lower() != want:
                continue
            # (row, col, 取位元組的 callable, 來源標籤)
            placed: list[tuple[int, int, Any, str]] = []
            for img in list(getattr(ws, "_images", []) or []):
                row, col = _anchor_cell(img)
                placed.append((row, col, lambda i=img: _image_bytes(i), "xlsx"))
            for row, col, data in incell.get(ws.title, []):
                placed.append((row, col, lambda d=data: d, "xlsx-incell"))
            placed.sort(key=lambda t: (t[0], t[1]))
            for row, col, get_bytes, source in placed:
                if len(out) >= max_images:
                    break
                data = get_bytes()
                if not data:
                    continue
                pil = _pil_from_bytes(data)
                if pil is None:            # emf/wmf 這類向量圖 PIL 讀不了 → 跳過
                    continue
                w, h = pil.size
                if w < min_px or h < min_px:
                    continue
                cell = f"{get_column_letter(col)}{row}" if row and col else ""
                idx = len(out) + 1
                path = os.path.join(out_dir, f"{stem}_{cell or 'img'}_{idx:02d}.png")
                if not _save_image(pil, path):
                    continue
                out.append({"path": path, "sheet": ws.title, "cell": cell,
                            "row": row, "col": col,
                            "label": _row_label(ws, row, col),
                            "width": w, "height": h,
                            "bytes": os.path.getsize(path), "source": source})
    finally:
        try:
            wb.close()
        except Exception:  # noqa: S110 —— 同上：finally 的 close 失敗無從補救
            pass
    return out


def _image_bytes(img) -> bytes:
    """openpyxl 的 Image → 原始位元組（載入的活頁簿是 BytesIO，新建的是路徑）。"""
    try:
        data = img._data()
    except Exception as e:
        logger.debug("doc_images 讀 xlsx 內嵌圖失敗：%s", e)
        return b""
    return data if isinstance(data, (bytes, bytearray)) else b""


def _pil_from_bytes(data: bytes):
    """位元組 → PIL image；讀不了（emf/wmf 等向量格式）回 None。"""
    try:
        import io

        from PIL import Image as PILImage
        pil = PILImage.open(io.BytesIO(data))
        pil.load()
        return pil
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────
# 員工通道工具（路徑限縮在 Telegram 上傳目錄）
# ────────────────────────────────────────────────────────────────────
def _upload_root() -> str:
    from agent_core.exchange_policy import telegram_upload_root
    return os.path.realpath(telegram_upload_root())


def output_root(color: str = "") -> str:
    """抽圖輸出根目錄：員工分艙到 dept/<色>，大王走頂層。

    doc_export 的圖片儲存格白名單認同一組路徑 —— 分艙的理由跟
    daemon_telegram._reply_file_allowed_roots 一樣：頂層混著別色抽出來的圖，
    只認整個目錄的話，一次 prompt injection 就能讓員工的 LLM 把別人的圖
    嵌進 Excel 帶走。
    """
    normalized = str(color or "").strip().lower()
    if not normalized or normalized == "red":
        return os.path.realpath(DOC_IMAGES_DIR)
    return os.path.realpath(os.path.join(DOC_IMAGES_DIR, "dept", normalized))


def _vet_uploaded(path: str, exts: tuple[str, ...],
                  kind: str) -> tuple[str, str]:
    """上傳檔路徑閘：回 (real_path, "")；不合格回 ("", 錯誤訊息)。

    比照 vision.analyze_uploaded_image —— 只認 Telegram 上傳目錄底下的檔，
    realpath 之後比對（擋 ../ 跳出上傳區）。PDF 與 Excel 兩顆工具共用同一份閘，
    免得日後只補強其中一邊。
    """
    p = (path or "").strip().strip("'\"")
    if not p:
        return "", f"錯誤：請帶上傳訊息「路徑:」欄的完整 {kind} 路徑。"
    try:
        root = _upload_root()
    except Exception:
        return "", "錯誤：無法解析 Telegram 上傳目錄。"
    real = os.path.realpath(os.path.expanduser(p))
    if not real.startswith(root + os.sep):
        return "", (f"錯誤：此工具只能讀 Telegram 上傳目錄（{root}）裡的 {kind}。"
                    "請用上傳訊息「路徑:」給的完整路徑。")
    if os.path.splitext(real)[1].lower() not in exts:
        return "", (f"錯誤：這顆工具只吃 {kind}（{' / '.join(exts)}）；"
                    "圖片請用 analyze_uploaded_image。")
    if not os.path.isfile(real):
        return "", f"錯誤：找不到檔案 {real}"
    return real, ""


def extract_uploaded_pdf_images(pdf_path: str, pages: str = "",
                                max_images: int = 0) -> str:
    """把「Telegram 上傳」的 PDF 裡的產品圖／繪圖抽成圖檔，回傳每張圖的路徑。

    用途：客人規格單 / 型錄 PDF 要整理成 Excel 追蹤表（tracking log），而表裡
    有一欄要放**產品圖本體**時，先用這顆把圖挖出來，再把回傳的路徑填進
    export_report 該欄的儲存格 —— 寫成 {"image": "<路徑>"}，圖就會嵌進 Excel
    格子裡（不是文字對照、不是超連結）。

    路徑限縮同 analyze_uploaded_image：只收 Telegram 上傳目錄底下的 .pdf，
    請帶上傳訊息「路徑:」欄的完整路徑。

    Args:
        pdf_path: 上傳訊息「路徑:」給的完整 PDF 路徑。
        pages: 只抽某幾頁，如 "1" / "1-3,5"；留空 = 全部（最多掃 40 頁）。
        max_images: 這份 PDF 最多抽幾張；0 = 用預設上限。

    Returns:
        每張圖一行：路徑、第幾頁、像素尺寸、來源（embedded=PDF 內嵌原圖、
        rendered=整頁畫出來裁白邊）。同頁多張時第一張通常是最大的那張
        （產品照）。抽不到圖會直說，不要腦補圖檔路徑。
    """
    real, err = _vet_uploaded(pdf_path, (_PDF_EXT,), "PDF")
    if err:
        return err

    from agent_core.dept_tool_scope import dept_scope_color
    color = dept_scope_color()
    out_dir = os.path.join(output_root(color), _safe_stem(real))
    try:
        items = extract_pdf_images(
            real, out_dir, pages=pages,
            max_images=(max_images if max_images and max_images > 0 else None),
        )
    except Exception as e:
        logger.exception("extract_uploaded_pdf_images 失敗：%s", real)
        return f"❌ 抽圖失敗：{type(e).__name__}: {e}"

    name = os.path.basename(real)
    if not items:
        return (f"⚠️ {name} 抽不到可用的圖（可能整份是文字，或圖都小於 "
                f"{_min_px()}px 被當成 logo 濾掉）。請照實回覆抽不到圖，"
                "不要自己編圖檔路徑。")
    lines = [f"✅ {name} 抽出 {len(items)} 張圖："]
    for it in items:
        lines.append(
            f"  • {it['path']}（第 {it['page']} 頁、{it['width']}×{it['height']}px、"
            f"{'PDF 內嵌原圖' if it['source'] == 'embedded' else '整頁繪製裁白邊'}）")
    lines.append("要把圖放進 Excel：export_report 的該格填 "
                 '{"image": "<上面的路徑>"}，圖會嵌進儲存格。')
    return "\n".join(lines)


def extract_uploaded_excel_images(xlsx_path: str, sheet: str = "",
                                  max_images: int = 0) -> str:
    """把「Telegram 上傳」的 Excel 裡貼在儲存格的圖抽成圖檔，**連同該列的款號**回報。

    用途：客人已經有一份做好的母表（master tracking log），圖就貼在表格裡；
    要接著做新版追蹤表、或把舊表的圖搬到新表時，用這顆把圖挖出來，再把回傳
    的路徑填進 export_report 對應列的儲存格 —— 寫成 {"image": "<路徑>"}。

    每張圖都會回報它原本錨在哪一格、以及**該列的前幾個欄位值**（款號 / 顏色），
    這是把圖對回品項的唯一依據：圖檔本身看不出是哪一款，要靠這個標籤配對。

    浮動圖與新版 Excel 的「置於儲存格」圖**兩種都抽得到**（客人的母表多半是
    後者）。路徑限縮同 analyze_uploaded_image：只收 Telegram 上傳目錄底下的
    .xlsx / .xlsm，請帶上傳訊息「路徑:」欄的完整路徑。（舊的 .xls 二進位格式
    存不了這種內嵌圖，請對方另存成 .xlsx。）

    Args:
        xlsx_path: 上傳訊息「路徑:」給的完整 Excel 路徑。
        sheet: 只抽某個工作表；留空 = 全部工作表。
        max_images: 最多抽幾張；0 = 用預設上限。

    Returns:
        每張圖一行：路徑、工作表!儲存格、該列標籤、像素尺寸。抽不到圖會直說
        （圖可能是浮動物件或舊格式），不要腦補圖檔路徑、也不要拿別列的圖湊數。
    """
    real, err = _vet_uploaded(xlsx_path, _XLSX_EXTS, "Excel")
    if err:
        return err

    from agent_core.dept_tool_scope import dept_scope_color
    color = dept_scope_color()
    out_dir = os.path.join(output_root(color), _safe_stem(real))
    try:
        items = extract_xlsx_images(
            real, out_dir, sheet=sheet,
            max_images=(max_images if max_images and max_images > 0 else None),
        )
    except Exception as e:
        logger.exception("extract_uploaded_excel_images 失敗：%s", real)
        return f"❌ 抽圖失敗：{type(e).__name__}: {e}"

    name = os.path.basename(real)
    if not items:
        return (f"⚠️ {name} 裡沒抽到貼在儲存格的圖"
                + (f"（工作表「{sheet}」）" if sheet else "")
                + "。可能是：圖其實在別的工作表、是舊 .xls 格式、或格子裡放的是"
                  "外部連結而非內嵌圖。請照實回覆抽不到圖，不要自己編圖檔路徑。")
    lines = [f"✅ {name} 抽出 {len(items)} 張圖（含原本在哪一列）："]
    for it in items:
        where = f"{it['sheet']}!{it['cell']}" if it["cell"] else it["sheet"]
        label = f"、該列：{it['label']}" if it["label"] else "、該列無文字可辨識"
        lines.append(f"  • {it['path']}（{where}{label}、"
                     f"{it['width']}×{it['height']}px）")
    gaps = rows_without_images(real, items, sheet=sheet)
    if gaps:
        lines.append(f"⚠️ 另外這 {len(gaps)} 列**有資料但沒有圖**——它們一樣是這張表的"
                     "品項，做新表時不可以因為沒圖就漏掉：")
        for sheet_name, row_no, label in gaps[:_MAX_GAP_ROWS_REPORTED]:
            lines.append(f"  • {sheet_name} 第 {row_no} 列：{label}（無圖）")
        if len(gaps) > _MAX_GAP_ROWS_REPORTED:
            lines.append(f"  • …還有 {len(gaps) - _MAX_GAP_ROWS_REPORTED} 列沒有圖"
                         "（用 read_uploaded_table 讀整張表拿完整清單）")
    lines.append("配對規則：用「該列」的款號/顏色對回你的表，**不要**照順序硬配；"
                 "對不上的就說對不上。要嵌進 Excel：export_report 該格填 "
                 '{"image": "<上面的路徑>"}。')
    lines.append("🛑 這顆只看得到**有圖的**儲存格，不是這張表的完整內容 —— 要照這張"
                 "表做新表，先用 read_uploaded_table 讀整張表，再用列號 / 款號把圖"
                 "對回去。")
    return "\n".join(lines)
