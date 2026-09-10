"""多格式文件匯出 — 把小紅整理好的資料/報告輸出成 Excel / Word / PDF 並回傳給大王。

動機：大王常說「這份資料整理成 excel 給我」「給我 word / pdf」。小紅以前只會回
文字（telegram screenshot 那次就是），檔案沒真的產出、也沒送到 Telegram。這個模組
補上整段：

  1. 一個彈性的「文件規格」(document spec)：標題 + 有序區塊（heading / paragraph
     / table / list / keyvalue）。同一份邏輯文件能一次渲染成三種格式。
  2. 三個 renderer：openpyxl(xlsx) / python-docx(docx) / reportlab(pdf)，全部
     支援繁體中文 —— PDF 走 reportlab 內建 Adobe CID 字型 MSung-Light，免外掛字型。
  3. export_report() tool：產檔 → 存 var/data/exports/ → 主動用 telegram_send_file
     送到大王 Telegram（best-effort）→ 回 ToolResult.artifacts。

輸入刻意做得寬鬆（LLM 友善），content_json 可以是：
  - 純資料表（list of dict）：'[{"品名":"A","數量":10}, ...]'
  - 完整報告（dict）：'{"title":..., "subtitle":..., "blocks":[...]}'

formats 可以是 "excel" / "word" / "pdf" / "all"，或逗號/空白分隔如 "excel,pdf"。

重的第三方庫（openpyxl / docx / reportlab）一律在 renderer 內 lazy import —— 匯入
本模組很便宜，缺某個庫只會在實際產該格式時報清楚的錯，不影響其他格式。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Callable

from agent_core.logging_and_paths import EXPORTS_DIR, logger
from agent_core.tool_result import ErrorCode, ToolResult

# 房子風格（沿用 quote_gen.py 的配色，讓三種格式視覺一致）
_DARK = "2C3E50"      # 標題 / 表頭底色
_LIGHT = "ECF0F1"     # key 欄 / section 底色
_MUTED = "7F8C8D"     # 副標題灰
_ZEBRA = "F7F9FA"     # 表格斑馬列
_GRID = "CCCCCC"      # 格線

_EXT = {"excel": ".xlsx", "word": ".docx", "pdf": ".pdf"}
_LABEL = {"excel": "Excel", "word": "Word", "pdf": "PDF"}

# ── 寬鬆別名（LLM 可能用各種說法）─────────────────────────────────────
_FORMAT_ALIASES = {
    "excel": "excel", "xlsx": "excel", "xls": "excel", "spreadsheet": "excel",
    "試算表": "excel", "表格檔": "excel",
    "word": "word", "docx": "word", "doc": "word", "文件檔": "word",
    "pdf": "pdf",
}
_ALL_TOKENS = {"all", "全部", "三種", "都要", "everything"}

_HEADING_TYPES = {"heading", "title", "header", "h1", "h2", "h3", "section",
                  "標題", "段落標題"}
_PARAGRAPH_TYPES = {"paragraph", "text", "p", "body", "para", "段落", "內文", "文字"}
_TABLE_TYPES = {"table", "tbl", "grid", "表格", "資料表", "明細"}
_LIST_TYPES = {"list", "bullets", "bullet", "items", "ul", "清單", "項目", "條列"}
_KEYVALUE_TYPES = {"keyvalue", "key_value", "kv", "pairs", "fields", "meta",
                   "屬性", "鍵值"}


# ────────────────────────────────────────────────────────────────────
# 小工具
# ────────────────────────────────────────────────────────────────────
def _stringify(v: Any) -> str:
    """把任意值轉成乾淨的字串（Word/PDF 儲存格、敘事文字都用這個）。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:g}"
    if isinstance(v, _ImageRef):
        return v.text()
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# ────────────────────────────────────────────────────────────────────
# 圖片儲存格（2026-08-12 UserAng 案：規格單 PDF → 有圖的 tracking log）
# ────────────────────────────────────────────────────────────────────
# 客人的開發追蹤表「remarks / Bemerkungen」欄擺的是產品圖本體。以前 export_report
# 只吃字串，小紅只能填「見 2001.pdf 第 1 頁」的文字對照，還在 Telegram 上回
# 「無法將 PDF 內部的繪圖與照片直接嵌入 Excel 儲存格」。現在表格儲存格可以是
# {"image": "<圖檔路徑>"}（或 "[[IMG:<路徑>]]"），三種格式都會把圖嵌進去。
# 圖從哪來：doc_images.extract_uploaded_pdf_images（PDF 挖圖）、fetch_shoe_photos、
# 員工自己上傳的照片。
_IMAGE_KEYS = ("image", "img", "photo", "picture", "image_path", "圖", "圖片", "照片")
_IMAGE_MARKER_RE = re.compile(r"^\[\[(?:IMG|IMAGE|圖|圖片)\s*:\s*(.+?)\]\]$", re.IGNORECASE)
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})
_IMAGE_MAX_BYTES = 12 * 1024 * 1024
_IMAGE_MAX_PER_DOC = 200          # 防一次塞幾千張把檔案撐爆 / 開不起來
_IMAGE_HEIGHT_PX = 90             # 預設列高（客人原表的縮圖大小差不多這樣）
_IMAGE_MAX_WIDTH_PX = 300         # 超寬的圖改用寬度為準，免撐爆欄寬


class _ImageRef:
    """一格圖片。``path`` 空 = 解析失敗，renderer 改印 ``note`` 文字。

    尺寸在解析時就用 Pillow 讀好（順便驗這檔真的是圖），三種 renderer 共用。
    """
    __slots__ = ("path", "nat_w", "nat_h", "note")

    def __init__(self, path: str = "", nat_w: int = 0, nat_h: int = 0, note: str = ""):
        self.path, self.nat_w, self.nat_h, self.note = path, nat_w, nat_h, note

    @property
    def ok(self) -> bool:
        return bool(self.path)

    def text(self) -> str:
        """給不嵌圖的地方（欄寬估算、失敗 fallback）的字串。"""
        return self.note or (os.path.basename(self.path) if self.path else "")

    def display_px(self, height_px: int = _IMAGE_HEIGHT_PX) -> tuple[int, int]:
        """依目標列高等比縮放；過寬的改用 _IMAGE_MAX_WIDTH_PX 為準。"""
        if not self.nat_w or not self.nat_h:
            return height_px, height_px
        w = max(1, round(height_px * self.nat_w / self.nat_h))
        if w > _IMAGE_MAX_WIDTH_PX:
            scale = _IMAGE_MAX_WIDTH_PX / w
            return _IMAGE_MAX_WIDTH_PX, max(1, round(height_px * scale))
        return w, height_px

    def __repr__(self) -> str:  # pragma: no cover - 只給 debug
        return f"_ImageRef({self.path or self.note!r})"


def _image_allowed_roots() -> tuple[str, ...] | None:
    """嵌圖來源白名單。None = 不設白名單（大王路徑，改用敏感路徑黑名單）。

    部門員工（Telegram 自由對話）收窄成 per-color 分艙目錄 ∪ 上傳目錄，理由同
    daemon_telegram._reply_photo_allowed_roots：產出根目錄頂層混著大王與別色的
    圖，只認整個目錄的話，一次 prompt injection 就能讓員工的 LLM 把別人的圖
    嵌進 Excel 帶走。上傳目錄維持共用 —— analyze_uploaded_image 早就是這個
    邊界（讀得到就描述得出來），嵌圖不比它更寬。
    """
    color = _dept_scope_color()
    if not color:
        return None
    from agent_core.logging_and_paths import DATA_DIR, GENERATED_IMAGES_DIR
    roots = [
        os.path.join(DATA_DIR, "doc_images", "dept", color),
        os.path.join(GENERATED_IMAGES_DIR, "dept", color),
        os.path.join(DATA_DIR, "product_photos", "fetched"),
    ]
    try:
        from agent_core.exchange_policy import telegram_upload_root
        roots.append(telegram_upload_root())
    except Exception:
        pass
    return tuple(os.path.realpath(r) for r in roots)


def _resolve_image(raw: Any) -> _ImageRef:
    """圖檔路徑 → _ImageRef（fail-closed：任何一關不過就回帶 note 的失敗 ref）。

    ⚠️ note 只放檔名不放完整路徑 —— 產出的檔案會傳給員工/客戶，本機絕對路徑
    不該印在儲存格裡。
    """
    p = str(raw or "").strip().strip("'\"")
    if not p:
        return _ImageRef(note="（圖片路徑空白）")
    name = os.path.basename(p)[:60]
    try:
        real = os.path.realpath(os.path.expanduser(p))
    except Exception:
        return _ImageRef(note=f"（圖片路徑無效：{name}）")
    if os.path.splitext(real)[1].lower() not in _IMAGE_EXTS:
        return _ImageRef(note=f"（不支援的圖片格式：{name}）")

    roots = _image_allowed_roots()
    if roots is None:
        try:
            from agent_core.telegram import _validate_send_path
            ok, _ = _validate_send_path(real)
        except Exception:
            ok = False
        if not ok:
            return _ImageRef(note=f"（圖片不在允許的目錄：{name}）")
    elif not any(real == r or real.startswith(r + os.sep) for r in roots):
        return _ImageRef(note=f"（圖片不在允許的目錄：{name}）")

    if not os.path.isfile(real):
        return _ImageRef(note=f"（找不到圖片：{name}）")
    try:
        if os.path.getsize(real) > _IMAGE_MAX_BYTES:
            return _ImageRef(note=f"（圖片太大：{name}）")
        from PIL import Image as PILImage
        with PILImage.open(real) as im:
            w, h = im.size
    except Exception:
        return _ImageRef(note=f"（圖片讀不出來：{name}）")
    if not w or not h:
        return _ImageRef(note=f"（圖片尺寸異常：{name}）")
    return _ImageRef(path=real, nat_w=int(w), nat_h=int(h))


def _as_image_ref(v: Any) -> _ImageRef | None:
    """把儲存格值認成圖片參照；不是圖片就回 None（照舊當文字處理）。

    兩種寫法：{"image": "<路徑>"}（各種 key 別名）與 "[[IMG:<路徑>]]" 標記。
    刻意不把「看起來像圖檔路徑的裸字串」當圖 —— 客人表格本來就常有檔名欄位。
    """
    if isinstance(v, _ImageRef):
        return v
    if isinstance(v, dict):
        for k in v.keys():
            if str(k).strip().lower() in _IMAGE_KEYS:
                return _resolve_image(v[k])
        return None
    if isinstance(v, str):
        m = _IMAGE_MARKER_RE.match(v.strip())
        if m:
            return _resolve_image(m.group(1))
    return None


def _image_height(src: Any, fallback: int = 0) -> int:
    """讀 image_height（圖片列高，px）；沒給/不合理回 fallback。"""
    if not isinstance(src, dict):
        return fallback
    for k in ("image_height", "圖片高度", "image_size"):
        if src.get(k) in (None, ""):
            continue
        try:
            return max(24, min(int(float(src[k])), 600))
        except (TypeError, ValueError):
            return fallback
    return fallback


def _count_images(spec: dict, limit: int = 0) -> tuple[int, list[str], int]:
    """統計/封頂圖片儲存格：回 (成功張數, 失敗說明, 超額被降成文字的張數)。

    超過 limit 的就地降級成檔名文字 —— 不是靜默丟掉（儲存格會空掉、對不上列），
    也不是硬塞（幾千張圖的 xlsx 開不起來）。limit=0 → 用 _IMAGE_MAX_PER_DOC
    （刻意不寫成預設引數：預設引數在 def 當下就綁死，測試 patch 常數會失效）。
    """
    limit = limit or _IMAGE_MAX_PER_DOC
    ok = 0
    failed: list[str] = []
    dropped = 0
    for b in spec.get("blocks") or []:
        if b.get("type") != "table":
            continue
        for row in b.get("rows") or []:
            for i, cell in enumerate(row):
                if not isinstance(cell, _ImageRef):
                    continue
                if not cell.ok:
                    failed.append(cell.note)
                elif ok >= limit:
                    row[i] = cell.text()
                    dropped += 1
                else:
                    ok += 1
    return ok, failed, dropped


def _cellify(v: Any) -> Any:
    """表格儲存格用：保留原生數字（int/float），讓 Excel 能排序/加總；其餘轉字串。

    刻意不把字串型數字（"120"）轉回數字 —— 尊重 LLM 給的型別：給 120 就是數字，
    給 "120" 就維持文字（避免把 ID / 編號 / 日期 誤判成數字）。

    圖片參照（{"image": "<路徑>"} / "[[IMG:<路徑>]]"）會在這裡解析成 _ImageRef，
    三種 renderer 各自把圖嵌進儲存格。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return v
    ref = _as_image_ref(v)
    if ref is not None:
        # 解析失敗的也照樣回 _ImageRef（renderer 印 note 文字）——就地轉成字串的話
        # export_report 統計不到，回覆就會宣稱「圖都放好了」，實際有幾格是空的。
        return ref
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _disp_width(s: Any) -> int:
    """顯示寬度（CJK 全形字算 2）。圖片格用縮圖寬度換算成字元數。"""
    if isinstance(s, _ImageRef):
        return (s.display_px()[0] // 7) if s.ok else _disp_width(s.text())
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", str(name or "")).strip().strip(".")
    name = re.sub(r"\s+", "_", name)
    return name[:60]


def _sanitize_sheet_name(name: str) -> str:
    # Excel sheet 名禁用字元 + 上限 31
    name = re.sub(r"[\\/?*\[\]:]", " ", str(name or "")).strip()
    return (name or "Sheet")[:31]


def _parse_formats(formats: str) -> list[str]:
    if not formats:
        return ["excel"]
    out: list[str] = []
    for tok in re.split(r"[,\s、，/|]+", str(formats).strip().lower()):
        if not tok:
            continue
        if tok in _ALL_TOKENS:
            return ["excel", "word", "pdf"]
        fmt = _FORMAT_ALIASES.get(tok)
        if fmt and fmt not in out:
            out.append(fmt)
    return out


# ────────────────────────────────────────────────────────────────────
# 輸入正規化：寬鬆 JSON → {title, subtitle, blocks:[...]}
# ────────────────────────────────────────────────────────────────────
def _coerce_table_data(data: list) -> tuple[list[str], list[list]]:
    """list → (columns, rows)。list of dict 取 key 聯集；list of list 直接排。"""
    if not data:
        return [], []
    if all(isinstance(r, dict) for r in data):
        cols: list[str] = []
        for r in data:
            for k in r.keys():
                if str(k) not in cols:
                    cols.append(str(k))
        rows = [[_cellify(r.get(c, "")) for c in cols] for r in data]
        return cols, rows
    if all(isinstance(r, (list, tuple)) for r in data):
        width = max((len(r) for r in data), default=0)
        rows = [[_cellify(x) for x in r] + [""] * (width - len(r)) for r in data]
        return [f"欄{i + 1}" for i in range(width)], rows
    # 純量列表 → 單欄
    return ["項目"], [[_cellify(r)] for r in data]


def _coerce_table_block(b: dict) -> tuple[list[str], list[list]]:
    cols = b.get("columns") or b.get("headers") or b.get("欄位")
    rows = b.get("rows")
    data = b.get("data")
    if cols and rows is not None:
        cols = [str(c) for c in cols]
        norm: list[list] = []
        for r in (rows or []):
            if isinstance(r, dict):
                norm.append([_cellify(r.get(c, "")) for c in cols])
            elif isinstance(r, (list, tuple)):
                vals = [_cellify(x) for x in r][:len(cols)]
                norm.append(vals + [""] * (len(cols) - len(vals)))
            else:
                norm.append([_cellify(r)] + [""] * (len(cols) - 1))
        return cols, norm
    if isinstance(data, list):
        return _coerce_table_data(data)
    if isinstance(rows, list):
        return _coerce_table_data(rows)
    return [], []


def _coerce_pairs(p: Any) -> list[list[str]]:
    out: list[list[str]] = []
    if isinstance(p, dict):
        for k, v in p.items():
            out.append([str(k), _stringify(v)])
    elif isinstance(p, list):
        for item in p:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append([_stringify(item[0]), _stringify(item[1])])
            elif isinstance(item, dict):
                if "key" in item and "value" in item:
                    out.append([_stringify(item["key"]), _stringify(item["value"])])
                else:
                    for k, v in item.items():
                        out.append([str(k), _stringify(v)])
    return out


def _normalize_block(b: Any) -> dict | None:
    if isinstance(b, str):
        return {"type": "paragraph", "text": b.strip()} if b.strip() else None
    if not isinstance(b, dict):
        return None
    btype = str(b.get("type") or b.get("kind") or "").strip().lower()
    title = str(b.get("title") or b.get("heading") or b.get("名稱") or "").strip()

    is_table = (btype in _TABLE_TYPES or b.get("columns") or b.get("rows")
                or (isinstance(b.get("data"), list)))
    if is_table:
        cols, rows = _coerce_table_block(b)
        if cols or rows:
            out = {"type": "table", "title": title, "columns": cols, "rows": rows}
            height = _image_height(b)
            if height:
                out["image_height"] = height
            return out

    if btype in _LIST_TYPES or isinstance(b.get("items"), list):
        items = [_stringify(x) for x in (b.get("items") or b.get("bullets") or [])]
        items = [x for x in items if x.strip()]
        if items:
            return {"type": "list", "title": title, "items": items}

    if btype in _KEYVALUE_TYPES or b.get("pairs"):
        pairs = _coerce_pairs(b.get("pairs") or b.get("fields"))
        if pairs:
            return {"type": "keyvalue", "title": title, "pairs": pairs}

    if btype in _HEADING_TYPES:
        text = str(b.get("text") or b.get("title") or title).strip()
        try:
            level = int(b.get("level"))
        except (TypeError, ValueError):
            level = 1
        return {"type": "heading", "text": text, "level": max(1, min(level, 4))} if text else None

    text = str(b.get("text") or b.get("content") or "").strip()
    if btype in _PARAGRAPH_TYPES or text:
        return {"type": "paragraph", "text": text} if text else None

    if title:  # 只有 title 的 dict → 當小標
        return {"type": "heading", "text": title, "level": 2}
    return None


def _normalize_spec(content: Any) -> dict:
    """把寬鬆輸入轉成 {title, subtitle, blocks:[...]}。"""
    if isinstance(content, list):
        cols, rows = _coerce_table_data(content)
        blocks = [{"type": "table", "title": "", "columns": cols, "rows": rows}] if cols else []
        return {"title": "", "subtitle": "", "blocks": blocks}
    if not isinstance(content, dict):
        text = _stringify(content).strip()
        return {"title": "", "subtitle": "",
                "blocks": [{"type": "paragraph", "text": text}] if text else []}

    title = str(content.get("title") or content.get("標題") or "").strip()
    subtitle = str(content.get("subtitle") or content.get("副標題")
                   or content.get("as_of") or content.get("資料截止") or "").strip()
    raw = content.get("blocks") or content.get("sections") or content.get("區塊")
    blocks: list[dict] = []
    if isinstance(raw, list):
        for b in raw:
            nb = _normalize_block(b)
            if nb:
                blocks.append(nb)
    elif content.get("columns") or content.get("rows") or isinstance(content.get("data"), list):
        nb = _normalize_block({"type": "table", **content})
        if nb:
            blocks.append(nb)
    else:
        pairs = [[str(k), _stringify(v)] for k, v in content.items()
                 if k not in ("title", "subtitle", "標題", "副標題", "as_of", "資料截止")]
        if pairs:
            blocks.append({"type": "keyvalue", "title": "", "pairs": pairs})
    spec = {"title": title, "subtitle": subtitle, "blocks": blocks}
    height = _image_height(content)
    if height:
        spec["image_height"] = height
    return spec


# ────────────────────────────────────────────────────────────────────
# Renderer: Excel (openpyxl)
# ────────────────────────────────────────────────────────────────────
def _row_height(text: str, width_chars: int) -> float:
    disp = _disp_width(text)
    lines = max(1, -(-disp // max(1, width_chars)))
    lines = max(lines, str(text).count("\n") + 1)
    return min(15 * lines + 4, 409)


def _render_excel(spec: dict, path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    f_title = Font(name="Arial", size=16, bold=True, color="FFFFFF")
    f_sub = Font(name="Arial", size=10, color="FFFFFF")
    f_sec = Font(name="Arial", size=12, bold=True, color=_DARK)
    f_head = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    f_bold = Font(name="Arial", size=11, bold=True)
    f_norm = Font(name="Arial", size=11)
    fill_dark = PatternFill(start_color=_DARK, end_color=_DARK, fill_type="solid")
    fill_light = PatternFill(start_color=_LIGHT, end_color=_LIGHT, fill_type="solid")
    fill_zebra = PatternFill(start_color=_ZEBRA, end_color=_ZEBRA, fill_type="solid")
    side = Side(border_style="thin", color="BBBBBB")
    box = Border(left=side, right=side, top=side, bottom=side)
    wrap = Alignment(wrap_text=True, vertical="top")
    right = Alignment(horizontal="right", vertical="top", wrap_text=True)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    title = spec.get("title") or "報告"
    subtitle = spec.get("subtitle") or ""
    blocks = spec.get("blocks") or []
    height_px = _image_height(spec, _IMAGE_HEIGHT_PX)
    tables = [b for b in blocks if b["type"] == "table"]
    narrative = [b for b in blocks if b["type"] != "table"]

    wb = Workbook()
    used_names: set[str] = set()

    def sheet_name(base: str) -> str:
        name = _sanitize_sheet_name(base)
        cand, i = name, 2
        while cand.lower() in used_names:
            cand = f"{name[:28]}-{i}"
            i += 1
        used_names.add(cand.lower())
        return cand

    default_used = False

    # 報告分頁（敘事內容）—— 沒任何 table 時也走這裡
    if narrative or not tables:
        ws = wb.active
        ws.title = sheet_name(title)
        default_used = True
        ws.sheet_view.showGridLines = False
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 82
        r = 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        c = ws.cell(r, 1, title)
        c.font, c.fill = f_title, fill_dark
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[r].height = 30
        r += 1
        if subtitle:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
            c = ws.cell(r, 1, subtitle)
            c.font, c.fill = f_sub, fill_dark
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            r += 1
        r += 1

        for b in narrative:
            if b["type"] == "heading":
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
                ws.cell(r, 1, b["text"]).font = f_sec
                r += 1
            elif b["type"] == "paragraph":
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
                c = ws.cell(r, 1, b["text"])
                c.font, c.alignment = f_norm, wrap
                ws.row_dimensions[r].height = _row_height(b["text"], 104)
                r += 1
            elif b["type"] == "list":
                if b.get("title"):
                    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
                    ws.cell(r, 1, b["title"]).font = f_sec
                    r += 1
                for it in b["items"]:
                    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
                    c = ws.cell(r, 1, f"•  {it}")
                    c.font, c.alignment = f_norm, wrap
                    ws.row_dimensions[r].height = _row_height(it, 100)
                    r += 1
            elif b["type"] == "keyvalue":
                if b.get("title"):
                    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
                    ws.cell(r, 1, b["title"]).font = f_sec
                    r += 1
                for k, v in b["pairs"]:
                    kc = ws.cell(r, 1, k)
                    kc.font, kc.fill, kc.alignment, kc.border = f_bold, fill_light, wrap, box
                    vc = ws.cell(r, 2, v)
                    vc.font, vc.alignment, vc.border = f_norm, wrap, box
                    ws.row_dimensions[r].height = _row_height(v, 80)
                    r += 1
            r += 1  # 區塊間空一列

    # 每張 table 一個分頁
    for idx, b in enumerate(tables):
        base = b.get("title") or (title if len(tables) == 1 and not narrative else f"資料表{idx + 1}")
        if default_used:
            ws = wb.create_sheet(sheet_name(base))
        else:
            ws = wb.active
            ws.title = sheet_name(base)
            default_used = True

        cols, rows = b["columns"], b["rows"]
        ncol = max(1, len(cols))
        hrow = 1
        for ci, col in enumerate(cols, start=1):
            cell = ws.cell(hrow, ci, col)
            cell.font, cell.fill, cell.alignment, cell.border = f_head, fill_dark, center, box
        ws.row_dimensions[hrow].height = 22
        img_cells: list[tuple[int, int, _ImageRef]] = []
        for ri, row in enumerate(rows, start=hrow + 1):
            for ci in range(ncol):
                val = row[ci] if ci < len(row) else ""
                if isinstance(val, _ImageRef):
                    if val.ok:
                        img_cells.append((ri, ci + 1, val))
                        val = ""      # 圖用 anchor 貼上去，格子本身留空
                    else:
                        val = val.text()   # 解析失敗 → 那格印「（找不到圖片：…）」
                cell = ws.cell(ri, ci + 1, val)
                is_num = isinstance(val, (int, float)) and not isinstance(val, bool)
                cell.font = f_norm
                cell.alignment = right if is_num else wrap
                cell.border = box
                if (ri - hrow) % 2 == 0:
                    cell.fill = fill_zebra
        # 欄寬：依內容（含表頭），CJK 算 2，夾在 8~52
        for ci in range(ncol):
            vals = [cols[ci]] if ci < len(cols) else []
            vals += [row[ci] for row in rows if ci < len(row)]
            width = max((_disp_width(v) for v in vals), default=8) + 3
            ws.column_dimensions[get_column_letter(ci + 1)].width = max(8, min(width, 52))
        _embed_excel_images(ws, img_cells, _image_height(b, height_px))
        ws.freeze_panes = ws.cell(row=hrow + 1, column=1)
        if rows:
            ws.auto_filter.ref = f"A{hrow}:{get_column_letter(ncol)}{hrow + len(rows)}"

    wb.save(path)


_IMAGE_INSET_PX = 3   # 圖離格線的內縮，免得整張貼在框線上


def _cell_anchor(col: int, row: int, w: int, h: int):
    """OneCellAnchor（帶內縮偏移）；openpyxl API 有變就回 None 讓 caller 用字串錨點。"""
    try:
        from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
        from openpyxl.drawing.xdr import XDRPositiveSize2D
        from openpyxl.utils.units import pixels_to_EMU
        off = pixels_to_EMU(_IMAGE_INSET_PX)
        marker = AnchorMarker(col=col - 1, colOff=off, row=row - 1, rowOff=off)
        return OneCellAnchor(_from=marker,
                             ext=XDRPositiveSize2D(pixels_to_EMU(w), pixels_to_EMU(h)))
    except Exception:
        return None


def _embed_excel_images(ws, img_cells: list[tuple[int, int, "_ImageRef"]],
                        height_px: int) -> None:
    """把圖片貼進儲存格，並把該列列高 / 該欄欄寬撐到裝得下。

    openpyxl 的圖是「錨在某格左上角」的浮動物件，不會自己撐格子 —— 不調列高
    欄寬的話圖會壓在下面幾列上，客人拿到的表就是糊的。
    Excel 幾何換算：列高 1pt = 1/0.75 px；欄寬 1 字元 ≈ 7px（+5px padding）。
    """
    if not img_cells:
        return
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    pad = 6
    for ri, ci, ref in img_cells:
        w, h = ref.display_px(height_px)
        try:
            img = XLImage(ref.path)
        except Exception as e:      # 圖在解析後被刪 / 格式怪 —— 退成檔名文字
            logger.warning("doc_export 嵌圖失敗 %s：%s", os.path.basename(ref.path), e)
            ws.cell(ri, ci, ref.text())
            continue
        img.width, img.height = w, h
        img.anchor = _cell_anchor(ci, ri, w, h) or f"{get_column_letter(ci)}{ri}"
        ws.add_image(img)
        ws.cell(ri, ci).alignment = Alignment(horizontal="center", vertical="center")
        need_pt = h * 0.75 + pad
        current = ws.row_dimensions[ri].height or 0
        ws.row_dimensions[ri].height = min(409, max(current, need_pt))
        need_chars = (w + pad) / 7 + 1
        col = get_column_letter(ci)
        ws.column_dimensions[col].width = max(ws.column_dimensions[col].width or 8,
                                              need_chars)


# ────────────────────────────────────────────────────────────────────
# Renderer: Word (python-docx)
# ────────────────────────────────────────────────────────────────────
def _set_style_eastasia(style, font_name: str) -> None:
    from docx.oxml.ns import qn
    try:
        rpr = style.element.get_or_add_rPr()
        rpr.get_or_add_rFonts().set(qn("w:eastAsia"), font_name)
    except Exception:
        pass


def _shade_word_cell(cell, fill_hex: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    cell._tc.get_or_add_tcPr().append(shd)


def _render_word(spec: dict, path: str) -> None:
    from docx import Document
    from docx.shared import Pt, RGBColor

    cjk = "PingFang TC"  # 大王在 macOS 上看；其他平台 Word 會自動 fallback CJK
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    _set_style_eastasia(normal, cjk)
    for sname in ("Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4"):
        try:
            _set_style_eastasia(doc.styles[sname], cjk)
        except KeyError:
            pass

    if spec.get("title"):
        doc.add_heading(spec["title"], level=0)
    if spec.get("subtitle"):
        p = doc.add_paragraph()
        run = p.add_run(spec["subtitle"])
        run.italic = True
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x7F, 0x8C, 0x8D)

    for b in spec["blocks"]:
        if b["type"] == "heading":
            doc.add_heading(b["text"], level=min(b["level"], 4))
        elif b["type"] == "paragraph":
            doc.add_paragraph(b["text"])
        elif b["type"] == "list":
            if b.get("title"):
                doc.add_heading(b["title"], level=2)
            for it in b["items"]:
                doc.add_paragraph(it, style="List Bullet")
        elif b["type"] == "keyvalue":
            if b.get("title"):
                doc.add_heading(b["title"], level=2)
            t = doc.add_table(rows=0, cols=2)
            t.style = "Table Grid"
            for k, v in b["pairs"]:
                cells = t.add_row().cells
                cells[0].text = k
                cells[1].text = v
                _shade_word_cell(cells[0], _LIGHT)
                for run in cells[0].paragraphs[0].runs:
                    run.font.bold = True
        elif b["type"] == "table":
            if b.get("title"):
                doc.add_heading(b["title"], level=2)
            cols = b["columns"] or ["欄1"]
            t = doc.add_table(rows=1, cols=len(cols))
            t.style = "Table Grid"
            for i, col in enumerate(cols):
                cell = t.rows[0].cells[i]
                cell.text = str(col)
                _shade_word_cell(cell, _DARK)
                for run in cell.paragraphs[0].runs:
                    run.font.bold = True
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            height_px = _image_height(b, _image_height(spec, _IMAGE_HEIGHT_PX))
            for row in b["rows"]:
                cells = t.add_row().cells
                for i in range(len(cols)):
                    val = row[i] if i < len(row) else ""
                    if isinstance(val, _ImageRef) and val.ok and \
                            _add_word_picture(cells[i], val, height_px):
                        continue
                    cells[i].text = _stringify(val)

    doc.save(path)


def _add_word_picture(cell, ref: "_ImageRef", height_px: int) -> bool:
    """Word 表格格子裡塞圖；失敗回 False 讓 caller 退回文字。

    EMU 是 python-docx 的長度單位：1px（96dpi）= 9525 EMU。
    """
    from docx.shared import Emu
    w, h = ref.display_px(height_px)
    try:
        cell.text = ""
        cell.paragraphs[0].add_run().add_picture(ref.path, width=Emu(w * 9525),
                                                 height=Emu(h * 9525))
        return True
    except Exception as e:
        logger.warning("doc_export Word 嵌圖失敗 %s：%s", os.path.basename(ref.path), e)
        return False


# ────────────────────────────────────────────────────────────────────
# Renderer: PDF (reportlab)
# ────────────────────────────────────────────────────────────────────
_CJK_FONT: str | None = None

# 能被 reportlab 嵌入（subset 進 PDF）的繁中字型候選，依序試。
# 鐵則：一定要「嵌入」TrueType 字型，PDF 才能在任何檢視器/手機 Telegram 顯示。
# reportlab 的內建 CID 字型（MSung-Light 等）不嵌入字型檔，沒裝 Adobe 亞洲字型包
# 的輕量檢視器會整片看不到字 —— 只當最後退路。
# PingFang / Hiragino 是 PostScript outline 的 .ttc，reportlab 無法 subset，故不列。
# (regular_path, regular_subfont_idx, bold_path, bold_subfont_idx)
_CJK_FONT_CANDIDATES = [
    ("/System/Library/Fonts/STHeiti Light.ttc", 1,
     "/System/Library/Fonts/STHeiti Medium.ttc", 1),       # 華文黑體（繁中，sans）
    ("/System/Library/Fonts/STHeiti Light.ttc", 0,
     "/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 1, None, None),  # 宋體（serif）
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None, None, None),
    ("/Library/Fonts/Arial Unicode.ttf", None, None, None),
]


def _ensure_cjk_font() -> str:
    """註冊並回傳一個能印繁體中文的字型名。

    優先嵌入系統 TrueType CJK 字型（STHeiti 黑體 → Songti → Arial Unicode），同時
    註冊粗體變體讓 <b> 有效；全都不行才退到 reportlab 內建 CID 字型。
    """
    global _CJK_FONT
    if _CJK_FONT:
        return _CJK_FONT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont

    for reg_path, reg_idx, bold_path, bold_idx in _CJK_FONT_CANDIDATES:
        if not os.path.exists(reg_path):
            continue
        try:
            reg = "RED-CJK"
            pdfmetrics.registerFont(
                TTFont(reg, reg_path, **({} if reg_idx is None else {"subfontIndex": reg_idx})))
            bold = reg
            if bold_path and os.path.exists(bold_path):
                try:
                    bold = "RED-CJK-Bold"
                    pdfmetrics.registerFont(
                        TTFont(bold, bold_path,
                               **({} if bold_idx is None else {"subfontIndex": bold_idx})))
                except Exception:
                    bold = reg
            registerFontFamily(reg, normal=reg, bold=bold, italic=reg, boldItalic=bold)
            _CJK_FONT = reg
            logger.info("doc_export PDF 用嵌入字型：%s (idx=%s)", reg_path, reg_idx)
            return reg
        except Exception:
            continue

    # 退路：內建 CID 字型（macOS Preview / Acrobat 能顯示，但不嵌入）
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        for name in ("MSung-Light", "STSong-Light"):
            try:
                pdfmetrics.registerFont(UnicodeCIDFont(name))
                _CJK_FONT = name
                logger.warning("doc_export PDF 找不到可嵌入的 CJK 字型，退用 CID 字型 %s（部分檢視器可能看不到字）", name)
                return name
            except Exception:
                continue
    except Exception:
        pass
    _CJK_FONT = "Helvetica"
    return _CJK_FONT


def _esc(s: Any) -> str:
    return _stringify(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pdf_cell(val: Any, style, height_px: int):
    """PDF 表格格子：圖片回 Image flowable，其餘回 Paragraph。

    reportlab 的長度單位是 point：1px（96dpi）= 0.75pt。
    """
    if isinstance(val, _ImageRef) and val.ok:
        try:
            from reportlab.platypus import Image as RLImage
            w, h = val.display_px(height_px)
            return RLImage(val.path, width=w * 0.75, height=h * 0.75)
        except Exception as e:
            logger.warning("doc_export PDF 嵌圖失敗 %s：%s",
                           os.path.basename(val.path), e)
    from reportlab.platypus import Paragraph
    return Paragraph(_esc(val), style)


def _render_pdf(spec: dict, path: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (ListFlowable, ListItem, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    font = _ensure_cjk_font()
    ss = getSampleStyleSheet()
    st_title = ParagraphStyle("RT", parent=ss["Title"], fontName=font, fontSize=18,
                              leading=24, textColor=colors.HexColor(f"#{_DARK}"),
                              alignment=TA_LEFT, spaceAfter=2)
    st_sub = ParagraphStyle("RS", parent=ss["Normal"], fontName=font, fontSize=9,
                            textColor=colors.HexColor(f"#{_MUTED}"), spaceAfter=12)
    st_h = {lvl: ParagraphStyle(f"RH{lvl}", parent=ss["Heading1"], fontName=font,
                                fontSize=15 - lvl, leading=21 - lvl,
                                textColor=colors.HexColor(f"#{_DARK}"),
                                spaceBefore=10, spaceAfter=4) for lvl in (1, 2, 3, 4)}
    st_body = ParagraphStyle("RB", parent=ss["Normal"], fontName=font, fontSize=10.5,
                             leading=16, spaceAfter=6)
    st_cell = ParagraphStyle("RC", parent=st_body, fontSize=9.5, leading=13, spaceAfter=0)
    st_cell_h = ParagraphStyle("RCH", parent=st_cell, textColor=colors.white)

    avail = A4[0] - 32 * mm

    def make_table(cols: list, rows: list, height_px: int = _IMAGE_HEIGHT_PX) -> Table:
        ncol = max(1, len(cols))
        weights = []
        for i in range(ncol):
            w = _disp_width(cols[i]) if i < len(cols) else 4
            for r in rows:
                if i < len(r):
                    w = max(w, min(_disp_width(r[i]), 40))
            weights.append(max(w, 4))
        total = sum(weights) or 1
        colw = [avail * wt / total for wt in weights]
        header = [Paragraph(f"<b>{_esc(c)}</b>", st_cell_h) for c in cols]
        body = [[_pdf_cell(r[i] if i < len(r) else "", st_cell, height_px)
                 for i in range(ncol)] for r in rows]
        data = [header] + body
        t = Table(data, colWidths=colw, repeatRows=1)
        style = [
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(f"#{_DARK}")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(f"#{_GRID}")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for ri in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor(f"#{_ZEBRA}")))
        t.setStyle(TableStyle(style))
        return t

    story: list = []
    if spec.get("title"):
        story.append(Paragraph(_esc(spec["title"]), st_title))
    if spec.get("subtitle"):
        story.append(Paragraph(_esc(spec["subtitle"]), st_sub))

    for b in spec["blocks"]:
        if b["type"] == "heading":
            story.append(Paragraph(_esc(b["text"]), st_h[b["level"]]))
        elif b["type"] == "paragraph":
            story.append(Paragraph(_esc(b["text"]).replace("\n", "<br/>"), st_body))
        elif b["type"] == "list":
            if b.get("title"):
                story.append(Paragraph(_esc(b["title"]), st_h[2]))
            items = [ListItem(Paragraph(_esc(it), st_body), leftIndent=10) for it in b["items"]]
            story.append(ListFlowable(items, bulletType="bullet", start="•",
                                      leftIndent=12, bulletFontName=font))
            story.append(Spacer(1, 4))
        elif b["type"] == "keyvalue":
            if b.get("title"):
                story.append(Paragraph(_esc(b["title"]), st_h[2]))
            data = [[Paragraph(f"<b>{_esc(k)}</b>", st_cell), Paragraph(_esc(v), st_cell)]
                    for k, v in b["pairs"]]
            t = Table(data, colWidths=[avail * 0.3, avail * 0.7])
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(f"#{_GRID}")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor(f"#{_LIGHT}")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(t)
            story.append(Spacer(1, 6))
        elif b["type"] == "table":
            if b.get("title"):
                story.append(Paragraph(_esc(b["title"]), st_h[2]))
            story.append(make_table(b["columns"] or ["欄1"], b["rows"],
                                    _image_height(b, _image_height(spec, _IMAGE_HEIGHT_PX))))
            story.append(Spacer(1, 8))

    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            title=spec.get("title") or "報告")
    doc.build(story)


_RENDERERS: dict[str, Callable[[dict, str], None]] = {
    "excel": _render_excel,
    "word": _render_word,
    "pdf": _render_pdf,
}


# ────────────────────────────────────────────────────────────────────
# 部門員工（Telegram 自由對話）產檔範圍
# ────────────────────────────────────────────────────────────────────
def _dept_scope_color() -> str:
    """目前這次呼叫是否在部門色 AgentRequest context 底下？回部門色或 ""。

    員工自由對話的工具都被 dept_tool_scope.wrap_tools_with_agent_caller 包進
    caller=<色> 的 AgentRequest；大王 / REPL / daemon 路徑沒有 context（或
    caller=red）→ 回 ""，行為完全不變。

    這顆色決定三件事（2026-08-03 UserC 案 + 2026-08-12 UserAng 案）：
      1. 產檔目錄改成 EXPORTS_DIR/dept/<色> —— 員工的檔跟大王的匯出分艙。
      2. 交付改走 [[TG_FILE:]] 回覆附檔，不走 telegram_send_file —— 後者的
         chat_id 閘（_resolve_chat_id）只認大王 keyring chat，員工要的檔會
         傳到大王手機，員工自己收不到。
      3. 儲存格嵌圖的來源白名單（_image_allowed_roots）收窄成該色自己的目錄。

    判讀邏輯的本體在 dept_tool_scope（那裡是 AgentRequest context 的唯一產生
    者）；這個薄包裝保留給既有 caller 與測試。
    """
    try:
        from agent_core.dept_tool_scope import dept_scope_color
    except Exception:
        return ""
    return dept_scope_color()


def _prune_dept_exports(out_dir: str, keep_days: int | None = None) -> None:
    """清掉部門匯出目錄裡過期的舊檔（best-effort，失敗絕不擋產檔）。

    比照 chart_export._prune_old_exports 的 RED_EXPORTS_KEEP_DAYS 慣例（預設
    14 天、<=0 停用）。那顆是非遞迴的、只掃 EXPORTS_DIR 頂層，掃不到這裡新開
    的 dept/<色> 子目錄 —— 不自己清就會無限累積。
    """
    import time
    if keep_days is None:
        try:
            keep_days = int(os.environ.get("RED_EXPORTS_KEEP_DAYS", "14"))
        except (TypeError, ValueError):
            keep_days = 14
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    try:
        entries = os.listdir(out_dir)
    except OSError:
        return
    for fn in entries:
        if not fn.lower().endswith((".xlsx", ".docx", ".pdf")):
            continue
        p = os.path.join(out_dir, fn)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


# ────────────────────────────────────────────────────────────────────
# Telegram 交付（best-effort）
# ────────────────────────────────────────────────────────────────────
def _deliver_as_reply_attachment(paths: list[str]) -> list[str]:
    """員工交付：回 [[TG_FILE:...]] 標記，讓檔案跟著**回覆**傳回當前對話。

    daemon_telegram 的回覆送出點抽標記、用「當前 bot token + 當前 chat_id」
    sendDocument —— chat_id 不經 LLM 之手，出站授權閘（RED_AUTHORIZED_CHAT_IDS）
    不必動。同 [[TG_PHOTO:]] 的機制（PR #305）。
    """
    lines = ["📎 檔案會跟著這則回覆一起傳給你："]
    for p in paths:
        lines.append(f"[[TG_FILE:{p}]]")
    lines.append("⚠️ 回覆時請把上面 [[TG_FILE:...]] 標記行原樣保留在回覆最後，"
                 "系統會自動轉成檔案傳送（標記本身不會顯示）。")
    return lines


def _deliver_to_telegram(paths: list[str], spec: dict, chat_id: str) -> list[str]:
    title = spec.get("title") or "資料"
    try:
        from agent_core.telegram import telegram_send_file
    except Exception as e:  # pragma: no cover - 載入失敗極少見
        return [f"⚠️ 無法載入 Telegram 傳檔工具：{e}（檔案已存到 {EXPORTS_DIR}）"]
    lines = ["📤 已傳到 Telegram："]
    for i, p in enumerate(paths, start=1):
        ext = os.path.splitext(p)[1].lstrip(".").upper()
        caption = f"小紅整理的{title}（{ext}）"[:900]
        try:
            res = telegram_send_file(p, caption=caption, chat_id=chat_id)
            ok = getattr(res, "ok", True)
            lines.append(f"  {i}. {'✅' if ok else '❌'} {os.path.basename(p)}"
                         + ("" if ok else f"：{res}"))
        except Exception as e:
            lines.append(f"  {i}. ❌ {os.path.basename(p)} 傳送失敗：{type(e).__name__}: {e}")
    return lines


# ────────────────────────────────────────────────────────────────────
# Tool entry point
# ────────────────────────────────────────────────────────────────────
def export_report(content_json: str, formats: str = "excel",
                  filename: str = "", deliver: bool = True,
                  chat_id: str = "") -> ToolResult:
    """把整理好的資料/報告輸出成 Excel / Word / PDF，並（在 Telegram 上）直接傳給大王。

    大王說「這份資料整理成 excel / word / pdf 給我」時就用這個工具 —— 它會真的產出
    檔案並送到 Telegram，而不是只回文字。同一份內容可一次輸出多種格式（excel/word/pdf）。

    Args:
        content_json: 文件內容（JSON 字串），兩種寫法都接受：
          1) 純資料表（list of dict）：
             '[{"品名":"Jalas buffer","數量":120,"盤點日":"2026-05-18"}, ...]'
          2) 完整報告（dict，含標題與有序區塊）：
             '{"title":"化學防具 & 緩衝物料 庫存盤點",
               "subtitle":"資料截止：2026-06-02",
               "blocks":[
                 {"type":"heading","text":"摘要"},
                 {"type":"paragraph","text":"……"},
                 {"type":"table","title":"庫存明細",
                  "columns":["品名","數量","盤點日","來源檔案"],
                  "rows":[["Jalas buffer",120,"2026-05-18","5-15Jalas..."]]},
                 {"type":"list","title":"建議","items":["請倉庫主管提供6月月結","……"]},
                 {"type":"keyvalue","title":"依據資料","pairs":[["檔案","5-15Jalas..."]]}
               ]}'
          區塊型別：heading（小標）/ paragraph（內文）/ table（表格）/
          list（條列）/ keyvalue（鍵值對）。Excel 會把每張 table 放一個分頁、
          敘事內容放「報告」分頁；Word/PDF 是完整排版的報告。

          🖼️ **表格儲存格可以放圖片本體**（產品照 / 規格單挖出來的圖）：該格
          寫 {"image": "<圖檔絕對路徑>"}，圖就會**嵌進儲存格**（不是文字對照、
          不是超連結），Excel/Word/PDF 三種格式都會嵌。例：
             {"type":"table",
              "columns":["款號","顏色","remarks / 圖"],
              "rows":[["2001A","Magic forest viola",
                       {"image":"/…/2001_p01_01.png"}]]}
          圖的來源：extract_uploaded_pdf_images（把客人規格單 PDF 裡的產品圖挖
          出來）、fetch_shoe_photos、員工上傳的照片。可加 "image_height"（px，
          預設 90）調整縮圖大小，放在該 table 區塊或最外層都行。
          🛑 路徑一定要用工具實際回傳的路徑，不可自己編 —— 編出來的路徑會被
          擋掉、那格只會剩「（找不到圖片：…）」。
        formats: 要產出的格式，可多選。"excel" / "word" / "pdf" / "all"，
                 或逗號分隔如 "excel,pdf"。預設 "excel"。
        filename: 檔名（不含副檔名）；留空自動用標題 + 時間戳。
        deliver: 是否自動傳到 Telegram（預設 True）。在 Telegram 對話裡叫小紅做就
                 保持 True，對方會直接在手機收到檔案；只想存檔不傳設 False。
        chat_id: 指定 Telegram chat；留空用大王預設。（部門員工對話中此參數無效
                 ——檔案一律跟著回覆傳回當前對話。）

    部門員工（Telegram 自由對話）呼叫時：檔案產到 exports/dept/<部門色>/，回傳
    文字會帶 [[TG_FILE:...]] 標記 —— **回覆時必須把標記行原樣保留**，系統才會把
    檔案傳給該員工（標記本身不會顯示給使用者）。

    Returns:
        ToolResult.success — summary 列出產出與傳送結果；artifacts 是檔案絕對路徑清單。
    """
    try:
        content = json.loads(content_json) if isinstance(content_json, str) else content_json
    except Exception as e:
        return ToolResult.failure(
            f"content_json 不是合法 JSON：{e}\n   💡 給 list of dict 或含 blocks 的 dict。",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False)

    spec = _normalize_spec(content)
    if not spec["blocks"]:
        return ToolResult.failure(
            "content_json 解析後沒有可輸出的內容。請給資料表（list of dict）或含 blocks 的報告。",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False)

    n_img, img_failed, n_dropped = _count_images(spec)

    fmts = _parse_formats(formats)
    if not fmts:
        return ToolResult.failure(
            f"不認得的格式：{formats!r}（支援 excel / word / pdf / all，可逗號分隔）。",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False)

    base = _sanitize_filename(filename) or _sanitize_filename(spec.get("title") or "報告")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 部門員工（Telegram 自由對話）→ 產到 dept/<色> 分艙目錄；大王路徑不變。
    dept_color = _dept_scope_color()
    out_dir = (os.path.join(EXPORTS_DIR, "dept", dept_color)
               if dept_color else EXPORTS_DIR)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        return ToolResult.failure(f"建立輸出目錄失敗：{e}", error_code=ErrorCode.INTERNAL)
    if dept_color:
        _prune_dept_exports(out_dir)

    produced: list[str] = []
    errors: list[str] = []
    for fmt in fmts:
        out = os.path.join(out_dir, f"{base}_{stamp}{_EXT[fmt]}")
        try:
            _RENDERERS[fmt](spec, out)
            produced.append(out)
        except ImportError as e:
            errors.append(f"{_LABEL[fmt]}: 缺套件（{e}）")
        except Exception as e:
            logger.exception("export_report 產 %s 失敗", fmt)
            errors.append(f"{_LABEL[fmt]}: {type(e).__name__}: {e}")

    if not produced:
        return ToolResult.failure("產檔全部失敗：\n  " + "\n  ".join(errors),
                                  error_code=ErrorCode.INTERNAL)

    lines = [f"✅ 已產出 {len(produced)} 個檔案："]
    for p in produced:
        lines.append(f"  • {os.path.basename(p)}（{os.path.getsize(p):,} bytes）")
    if errors:
        lines.append("⚠️ 部分格式失敗：" + "；".join(errors))
    if n_img:
        lines.append(f"🖼️ 已嵌入 {n_img} 張圖片到儲存格。")
    if n_dropped:
        lines.append(f"⚠️ 超過 {_IMAGE_MAX_PER_DOC} 張上限的 {n_dropped} 格改填檔名文字。")
    if img_failed:
        # 逐格失敗要照實回報：那幾格會空著，LLM 不能對使用者宣稱「圖都放好了」。
        shown = "；".join(dict.fromkeys(img_failed))[:400]
        lines.append(f"⚠️ {len(img_failed)} 張圖沒放進去：{shown}"
                     "（回覆時要照實說哪幾張沒圖，不要宣稱都嵌好了）")

    if deliver and dept_color:
        # 員工路徑：chat_id 參數刻意忽略 —— 目的地由 daemon 用當前對話決定，
        # 不讓 LLM 指定收件人。
        lines.append("")
        lines.extend(_deliver_as_reply_attachment(produced))
    elif deliver:
        lines.append("")
        lines.extend(_deliver_to_telegram(produced, spec, chat_id))
    else:
        lines.append(f"（未自動傳送；檔案在 {out_dir}）")

    return ToolResult.success("\n".join(lines),
                              data={"paths": produced, "formats": fmts,
                                    "images_embedded": n_img},
                              artifacts=produced, warnings=errors + img_failed)
