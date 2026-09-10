"""IDP — Intelligent Document Processing.

大王需求（Telegram 直接證據）：
  「給 AI 一個 Word（格式不固定）→ AI 理解內容與欄位 → 抓公司資料 → 自動填入」

對應業界術語：
  Document Understanding（文件理解 — 標題 / 表格 / 欄位意義）
  Intelligent Document Processing / IDP（最常用名稱）
  Document Automation / Form Filling（自動填表）

Pipeline：
  Word/PDF/Excel
      ↓ read_document()              讀結構（paragraphs / tables）
      ↓ extract_document_fields()    LLM 找出「待填欄位」+ 標籤
      ↓ fill_document()              塞值寫新檔
      ↓ auto_fill_document()         orchestrator 自動 map 公司資料 → 欄位

支援格式（v1）：
  ✅ .docx — python-docx 已裝
  ✅ .pdf  — pdfplumber 已裝（read only，pdf 填寫太複雜留 v2）
  ✅ .xlsx — openpyxl 已裝
  ❌ .doc 舊版 — Word 97-2003 binary 沒辦法直接讀；
                  請大王另存為 .docx（或安裝 LibreOffice 自動轉）

跟既有 tool 的關係：
  pdf_extract_text / pdf_extract_tables  既有 — 純文字抽取
  excel_read / excel_query              既有 — Excel 查詢
  本 module：                           整合 + 加 LLM 欄位識別 + 自動填

不在這個 module（避免 scope creep）：
  - PDF form filling（要 pikepdf，太複雜）
  - 從 0 生成新文件（document generation）
  - 跨頁複雜 layout 推理（用 vision 而非 docx tree）
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any


# ────────────────────────────────────────────────────────────────────
# 限制
# ────────────────────────────────────────────────────────────────────
_MAX_DOC_SIZE = 20 * 1024 * 1024     # 20MB（防讀超大檔卡住）
_MAX_FIELDS_PER_DOC = 100            # LLM 一次最多識別 100 個 field
_MAX_OUTPUT_PATH_LEN = 300


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ────────────────────────────────────────────────────────────────────
# Path safety — 寫檔限縮到安全區
# ────────────────────────────────────────────────────────────────────
def _validate_read_path(path: str) -> tuple[bool, str]:
    """讀檔安全：複用 telegram._validate_send_path 的 read 規則。

    跟 path_safety.safe_path 不同：safe_path 是寫入保護太嚴。
    這裡讀入文件，安全規則同 telegram_send_file（不能讀 .ssh / vault 等）。
    """
    try:
        from agent_core.telegram import _validate_send_path
    except Exception:
        return False, "_validate_send_path 模組讀不到"
    return _validate_send_path(path)


def _validate_write_path(path: str) -> tuple[bool, str]:
    """寫檔安全：必須在 var/data/idp_outputs/ 或 ~/Downloads。

    嚴格限制 — 不允許寫到 var/state/ / agent_core/ / skills/ 等系統區。
    """
    if not path or not isinstance(path, str):
        return False, "output_path 必填"
    if len(path) > _MAX_OUTPUT_PATH_LEN:
        return False, "output_path 太長"
    abs_path = os.path.realpath(os.path.expanduser(path))
    try:
        from agent_core.logging_and_paths import DATA_DIR
        idp_dir = os.path.realpath(os.path.join(DATA_DIR, "idp_outputs"))
    except Exception:
        idp_dir = ""
    downloads = os.path.realpath(os.path.expanduser("~/Downloads"))
    allowed = [d for d in (idp_dir, downloads) if d]
    for root in allowed:
        if abs_path.startswith(root + os.sep) or abs_path == root:
            return True, abs_path
    return False, (f"output_path 必須在 var/data/idp_outputs/ 或 ~/Downloads 之下\n"
                   f"   收到：{abs_path}")


def _default_output_path(input_path: str) -> str:
    """產生預設輸出路徑：var/data/idp_outputs/{stem}_filled_{ts}.docx"""
    try:
        from agent_core.logging_and_paths import DATA_DIR
        out_dir = os.path.join(DATA_DIR, "idp_outputs")
    except Exception:
        out_dir = "/tmp/idp_outputs"
    os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(input_path))
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    return os.path.join(out_dir, f"{stem}_filled_{ts}{ext}")


# ────────────────────────────────────────────────────────────────────
# 1. read_document — 讀結構
# ────────────────────────────────────────────────────────────────────
def _read_docx(abs_path: str) -> dict:
    """讀 .docx 結構：paragraphs + tables（每格內容）。"""
    try:
        from docx import Document
    except ImportError:
        return {"error": "python-docx 沒裝（pip install python-docx）"}
    try:
        doc = Document(abs_path)
    except Exception as e:
        return {"error": f"docx 讀取失敗：{type(e).__name__}: {e}"}
    paragraphs = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            paragraphs.append(t)
    tables = []
    for t_idx, table in enumerate(doc.tables):
        rows = []
        for r_idx, row in enumerate(table.rows):
            cells = [cell.text.strip() for cell in row.cells]
            rows.append(cells)
        tables.append({"index": t_idx, "rows": rows,
                        "row_count": len(rows),
                        "col_count": len(rows[0]) if rows else 0})
    return {
        "kind": "docx",
        "path": abs_path,
        "paragraphs": paragraphs,
        "tables": tables,
        "table_count": len(tables),
        "paragraph_count": len(paragraphs),
    }


def _read_pdf(abs_path: str) -> dict:
    """讀 .pdf 結構：每頁 text + tables（複用既有 pdf_extract_text 邏輯）。"""
    try:
        import pdfplumber
    except ImportError:
        return {"error": "pdfplumber 沒裝"}
    try:
        with pdfplumber.open(abs_path) as pdf:
            pages = []
            tables_all = []
            for i, page in enumerate(pdf.pages):
                txt = page.extract_text() or ""
                pages.append({"index": i, "text": txt[:3000]})
                for t in (page.extract_tables() or []):
                    if t and any(cell for row in t for cell in row):
                        tables_all.append({"page": i, "rows": t})
            return {
                "kind": "pdf",
                "path": abs_path,
                "page_count": len(pages),
                "pages": pages,
                "tables": tables_all,
                "table_count": len(tables_all),
            }
    except Exception as e:
        return {"error": f"PDF 讀取失敗：{type(e).__name__}: {e}"}


def _read_xlsx(abs_path: str) -> dict:
    """讀 .xlsx 結構：每 sheet 的前 N 列 + headers。"""
    try:
        import openpyxl
    except ImportError:
        return {"error": "openpyxl 沒裝"}
    try:
        wb = openpyxl.load_workbook(abs_path, data_only=True, read_only=True)
        sheets = []
        for sn in wb.sheetnames:
            ws = wb[sn]
            rows = []
            for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                if r_idx >= 50:  # 上限 50 列避免巨檔
                    break
                rows.append(["" if c is None else str(c) for c in row])
            sheets.append({"name": sn,
                           "rows": rows,
                           "row_count": len(rows),
                           "col_count": len(rows[0]) if rows else 0})
        return {
            "kind": "xlsx",
            "path": abs_path,
            "sheets": sheets,
            "sheet_count": len(sheets),
        }
    except Exception as e:
        return {"error": f"xlsx 讀取失敗：{type(e).__name__}: {e}"}


def read_document(file_path: str) -> str:
    """🟢 讀 Word/PDF/Excel 文件結構（標題 / 段落 / 表格）。

    回 formatted text 給 LLM 看。要拿原始 dict 用 _read_docx 等內部函式。

    Args:
        file_path: .docx / .pdf / .xlsx 檔路徑（須在 read 安全區）

    Returns:
        formatted summary（含段落 / 表格行數 / 前幾項預覽）
    """
    ok, p = _validate_read_path(file_path)
    if not ok:
        return f"❌ {p}"
    abs_path = p
    size = os.path.getsize(abs_path)
    if size > _MAX_DOC_SIZE:
        return f"❌ 檔案 {size / 1024 / 1024:.1f}MB 超過 {_MAX_DOC_SIZE // 1024 // 1024}MB 上限"

    ext = os.path.splitext(abs_path)[1].lower()
    if ext == ".docx":
        d = _read_docx(abs_path)
    elif ext == ".doc":
        return ("❌ 舊版 .doc 格式（Word 97-2003）目前不支援。\n"
                "   請大王在 Word 開啟後「另存新檔」→ 選 `.docx`")
    elif ext == ".pdf":
        d = _read_pdf(abs_path)
    elif ext in (".xlsx", ".xls"):
        d = _read_xlsx(abs_path)
    else:
        return f"❌ 不支援的副檔名 {ext}（支援 .docx / .pdf / .xlsx）"

    if "error" in d:
        return f"❌ {d['error']}"

    # 格式化給 LLM / 大王看
    out = [f"📄 文件結構：{os.path.basename(abs_path)}",
           "─" * 60, f"  類型：{d['kind']}"]
    if d["kind"] == "docx":
        out.append(f"  段落: {d['paragraph_count']} 行")
        out.append(f"  表格: {d['table_count']} 個")
        if d["paragraphs"]:
            out.append("")
            out.append("  📝 段落預覽（前 10 行）：")
            for p in d["paragraphs"][:10]:
                out.append(f"    {p[:80]}")
        for tbl in d["tables"][:3]:
            out.append("")
            out.append(f"  📊 表格 #{tbl['index']}：{tbl['row_count']} 行 × {tbl['col_count']} 欄")
            for r in tbl["rows"][:5]:
                out.append(f"    {' | '.join(c[:25] for c in r)}")
    elif d["kind"] == "pdf":
        out.append(f"  頁數: {d['page_count']}")
        out.append(f"  表格: {d['table_count']} 個")
        if d["pages"]:
            out.append("")
            out.append("  📝 第 1 頁預覽：")
            out.append(f"    {d['pages'][0]['text'][:500]}")
    elif d["kind"] == "xlsx":
        out.append(f"  Sheets: {d['sheet_count']}")
        for s in d["sheets"][:3]:
            out.append("")
            out.append(f"  📊 sheet '{s['name']}': {s['row_count']} 列 × {s['col_count']} 欄")
            for r in s["rows"][:5]:
                out.append(f"    {' | '.join(c[:25] for c in r)}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 2. extract_document_fields — LLM 識別待填欄位
# ────────────────────────────────────────────────────────────────────
def extract_document_fields(file_path: str) -> str:
    """🟢 用 LLM 分析文件，列出所有「待填欄位」（標籤、預期類型、位置）。

    Returns formatted JSON list of fields:
        [{label, current_value, location, expected_type, hint}, ...]

    這個 tool 不修改檔案，只是讀取＋呼叫 Gemini。
    """
    ok, p = _validate_read_path(file_path)
    if not ok:
        return f"❌ {p}"
    abs_path = p

    # 讀文件結構（用內部 dict 版本）
    ext = os.path.splitext(abs_path)[1].lower()
    if ext == ".docx":
        d = _read_docx(abs_path)
    elif ext == ".pdf":
        d = _read_pdf(abs_path)
    elif ext in (".xlsx", ".xls"):
        d = _read_xlsx(abs_path)
    elif ext == ".doc":
        return "❌ 舊版 .doc 不支援；請另存為 .docx"
    else:
        return f"❌ 不支援的副檔名 {ext}"
    if "error" in d:
        return f"❌ {d['error']}"

    # 組 LLM prompt
    if d["kind"] == "docx":
        content_summary = json.dumps({
            "paragraphs": d["paragraphs"][:50],  # 上限 50 paragraph 防 prompt 爆
            "tables": [
                {"index": t["index"], "rows": t["rows"][:20]}
                for t in d["tables"][:5]
            ],
        }, ensure_ascii=False)
    elif d["kind"] == "pdf":
        content_summary = json.dumps({
            "pages": [{"index": p["index"],
                       "text": p["text"][:1500]}
                      for p in d["pages"][:5]],
            "tables": [{"page": t["page"], "rows": t["rows"][:10]}
                       for t in d["tables"][:5]],
        }, ensure_ascii=False)
    else:
        content_summary = json.dumps({
            "sheets": [{"name": s["name"], "rows": s["rows"][:20]}
                       for s in d["sheets"][:3]]
        }, ensure_ascii=False)
    content_summary = content_summary[:8000]  # 硬上限

    prompt = f"""分析這份企業文件，列出所有**需要填寫的欄位**（empty cell / 待填空格 / 待簽核項目）。

文件內容：
{content_summary}

請以**嚴格 JSON list** 格式回應（不要 markdown 包裝）：
[
  {{
    "label": "欄位標籤（中文，e.g. '統一編號'）",
    "current_value": "目前內容（空字串 = 待填）",
    "location": "在文件哪裡（e.g. 'table 0 row 1 col 1' / 'paragraph 5')",
    "expected_type": "text|number|date|email|phone|address|signature",
    "hint": "怎麼填的提示（e.g. '8 位數字' / 'YYYY-MM-DD'）"
  }}
]

規則：
- 已經有值的欄位也列出（current_value 填現有值）
- 純說明文字 / 標題 不算欄位
- 重複的欄位（多個簽核處）保留多筆
- 上限 {_MAX_FIELDS_PER_DOC} 個欄位
"""

    try:
        from agent_core.gemini_client import _gemini_generate
        resp = _gemini_generate(model="gemini-2.5-flash-lite",
                                 contents=[prompt])
        text = resp.text if hasattr(resp, "text") else str(resp)
    except Exception as e:
        return f"❌ LLM 分析失敗：{type(e).__name__}: {e}"

    # 解析 JSON
    fields = _parse_fields_json(text)
    if fields is None:
        return f"❌ LLM 回應不是合法 JSON：{text[:200]}"
    if not fields:
        return "  （沒識別到任何待填欄位）"

    # 截斷
    fields = fields[:_MAX_FIELDS_PER_DOC]

    out = [f"📋 待填欄位 — {os.path.basename(abs_path)}",
           "─" * 60, f"  共 {len(fields)} 個欄位"]
    for i, f in enumerate(fields[:30]):
        label = f.get("label", "?")
        loc = f.get("location", "")
        cur = f.get("current_value", "")
        typ = f.get("expected_type", "")
        hint = f.get("hint", "")
        cur_str = "（空）" if not cur else f"目前: {cur[:30]}"
        out.append(f"  {i+1:2d}. {label:20s} [{typ}] @ {loc[:30]}")
        out.append(f"       {cur_str}" + (f" / 提示: {hint[:30]}" if hint else ""))
    if len(fields) > 30:
        out.append(f"  ...（還有 {len(fields)-30} 個）")
    out.append("")
    out.append("💡 用 fill_document() 填值，或 auto_fill_document() 自動抓公司資料填")
    return "\n".join(out)


def _parse_fields_json(text: str) -> list | None:
    """從 LLM 回應抽 JSON list（容忍 markdown wrap / 前後贅字）。"""
    t = text.strip()
    # strip markdown fence
    if t.startswith("```"):
        t = re.sub(r"^```(json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    # 找 [...] 區塊
    m = re.search(r"\[[\s\S]*\]", t)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    # 驗每筆是 dict
    cleaned = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # 強制必要欄位
        if not item.get("label"):
            continue
        cleaned.append({
            "label": str(item.get("label", ""))[:100],
            "current_value": str(item.get("current_value", ""))[:200],
            "location": str(item.get("location", ""))[:80],
            "expected_type": str(item.get("expected_type", "text"))[:30],
            "hint": str(item.get("hint", ""))[:100],
        })
    return cleaned


# ────────────────────────────────────────────────────────────────────
# 3. fill_document — 把值塞進 docx 對應欄位
# ────────────────────────────────────────────────────────────────────
def fill_document(file_path: str, field_values_json: str,
                   output_path: str = "") -> str:
    """🟡 把 dict {label: value} 填進 .docx 的對應欄位，存成新檔。

    填法：在 table 找到 label 那格，下一格（同 row, col+1）若空就填 value。
    若沒有「下一格空白」（單欄表格 / 段落），會嘗試換行附加。

    Args:
        file_path: 來源 .docx
        field_values_json: JSON 字串 dict {"label": "value", ...}
        output_path: 輸出路徑（空 = 自動產生 var/data/idp_outputs/{stem}_filled_TS.docx）

    Returns:
        ToolResult-like message（用文字以保持 SAFE 工具相容）
        建議大王透過 telegram_send_file 取回填好的檔
    """
    ok, p = _validate_read_path(file_path)
    if not ok:
        return f"❌ {p}"
    abs_path = p
    if os.path.splitext(abs_path)[1].lower() != ".docx":
        return "❌ fill_document 目前只支援 .docx（PDF 填寫太複雜，留 v2）"

    # 解析 field_values
    try:
        values = json.loads(field_values_json) if field_values_json else {}
    except json.JSONDecodeError as e:
        return f"❌ field_values_json 不是合法 JSON：{e}"
    if not isinstance(values, dict):
        return "❌ field_values_json 必須是 dict {label: value}"
    if not values:
        return "❌ 沒有要填的值"

    # 輸出路徑
    if output_path:
        ok, out_or_err = _validate_write_path(output_path)
        if not ok:
            return f"❌ {out_or_err}"
        final_out = out_or_err
    else:
        final_out = _default_output_path(abs_path)

    try:
        os.makedirs(os.path.dirname(final_out), exist_ok=True)
    except Exception as e:
        return f"❌ 建輸出目錄失敗：{e}"

    try:
        from docx import Document
    except ImportError:
        return "❌ python-docx 沒裝"

    try:
        doc = Document(abs_path)
    except Exception as e:
        return f"❌ 開檔失敗：{e}"

    filled_count = 0
    not_found = []
    for label, value in values.items():
        if not isinstance(label, str) or not isinstance(value, (str, int, float)):
            continue
        value_str = str(value)
        # 1. 在 tables 找 label，下一格填值
        hit = False
        for table in doc.tables:
            for row in table.rows:
                cells = list(row.cells)
                for i, cell in enumerate(cells):
                    if label in cell.text and i + 1 < len(cells):
                        next_cell = cells[i + 1]
                        # 只填空格（不覆蓋既有內容）
                        if not next_cell.text.strip():
                            next_cell.text = value_str
                            filled_count += 1
                            hit = True
                            break
                if hit:
                    break
            if hit:
                break
        if not hit:
            not_found.append(label)

    try:
        doc.save(final_out)
    except Exception as e:
        return f"❌ 寫檔失敗：{e}"

    out = [f"✅ 已填 {filled_count} / {len(values)} 個欄位"]
    out.append(f"   輸出：{final_out}")
    if not_found:
        out.append("   ⚠️ 沒找到位置（可能是段落內 / 名稱不準）：")
        for nf in not_found[:10]:
            out.append(f"     - {nf}")
    out.append("")
    out.append(f"💡 用 telegram_send_file('{final_out}') 取回填好的檔")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 4. auto_fill_document — orchestrator
# ────────────────────────────────────────────────────────────────────
def auto_fill_document(file_path: str, company_context: str = "self",
                        output_path: str = "") -> str:
    """🟡 全自動：讀文件 → 識別欄位 → 抓公司資料 → 填值 → 存新檔。

    Pipeline:
      1. extract_document_fields(file_path) — 識別待填欄位
      2. 依 company_context 抓資料：
         - 'self'         → recall("公司基本資料 統一編號 代表人 地址")
         - 'customer:XXX' → customer_360(name)
         - 'supplier:XXX' → 同上
      3. LLM mapping 欄位 → 值
      4. fill_document() 寫出新 .docx

    Args:
        file_path: .docx 檔
        company_context:
          'self' = 大王公司本身（從 memory recall）
          'customer:<name>' = 該客戶資料（customer_360）
          'supplier:<name>' = 該供應商資料
        output_path: 輸出路徑（空 = 自動）
    """
    ok, p = _validate_read_path(file_path)
    if not ok:
        return f"❌ {p}"
    abs_path = p

    # Step 1: 識別欄位（內部呼叫，拿原 list）
    ext = os.path.splitext(abs_path)[1].lower()
    if ext != ".docx":
        return "❌ auto_fill 目前只支援 .docx"
    company_context = (company_context or "self").strip()
    valid_context = (
        company_context == "self"
        or company_context.startswith("customer:")
        or company_context.startswith("supplier:")
    )
    if not valid_context:
        return f"❌ company_context 格式錯：'{company_context}'。可選 self / customer:NAME / supplier:NAME"
    fields_raw = _extract_fields_internal(abs_path)
    if isinstance(fields_raw, str):  # error string
        return fields_raw
    if not fields_raw:
        return "  （沒識別到待填欄位 — 文件可能已填完）"

    # Step 2: 抓資料
    # 'self' 模式：優先 load_memory() 直接 key-value 查找（精準），再補
    # recall() 的語意 search 結果（fuzzy 但補漏）。理由：recall 會 hybrid
    # search 混到既有客戶/同事記憶（e.g. 把客戶名混成大王公司名），key-value
    # 直接查最準。
    info = ""
    try:
        if company_context == "self":
            from agent_core.memory import recall
            from agent_core.logging_and_paths import MEMORY_FILE
            # (1) 直接讀 memory.json key-value 查找 — 精準
            # 注意：load_memory() 回 JSON 字串而非 dict（API 設計給 LLM 看），
            # 這裡要直接讀檔拿原 dict
            all_mem: dict = {}
            try:
                if os.path.isfile(MEMORY_FILE):
                    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                        all_mem = json.load(f) or {}
                if not isinstance(all_mem, dict):
                    all_mem = {}
            except Exception:
                all_mem = {}
            # 過濾公司相關 keys（含中英關鍵字）
            company_kw = ("公司", "統編", "統一編號", "代表", "owner",
                          "地址", "addr", "電話", "phone", "fax", "傳真",
                          "email", "產業", "industry", "成立", "founded",
                          "資本", "員工", "company", "unified")
            structured = {
                k: v for k, v in all_mem.items()
                if isinstance(k, str) and isinstance(v, (str, int, float))
                   and any(kw.lower() in k.lower() for kw in company_kw)
            }
            # Critical：結構化 key 太少 → LLM 一定 hallucinate placeholder
            # （實測 6 次跑出 12345678 / owner.chen@example.com 之類假料）
            # 寧可拒絕 auto_fill，也不要塞假資料進政府/客戶表單
            _MIN_STRUCTURED_KEYS = 3
            if len(structured) < _MIN_STRUCTURED_KEYS:
                return (
                    f"❌ memory 中公司資料不足（找到 {len(structured)} 個 key，"
                    f"至少要 {_MIN_STRUCTURED_KEYS} 個才避免 LLM hallucinate）。\n"
                    f"\n"
                    f"請先 save_memory 存基本資料，建議 keys：\n"
                    f"  save_memory('公司名稱', '...')\n"
                    f"  save_memory('統一編號', '...')\n"
                    f"  save_memory('代表人', '...')\n"
                    f"  save_memory('公司地址', '...')\n"
                    f"  save_memory('公司電話', '...')\n"
                    f"  save_memory('公司Email', '...')\n"
                    f"\n"
                    f"或直接用 fill_document(path, values_json) 手動指定值。\n"
                    f"\n"
                    f"目前找到的 keys：{list(structured.keys()) or '（無）'}"
                )
            structured_text = "【結構化公司資料（精準 key-value）】\n" + json.dumps(
                structured, ensure_ascii=False, indent=2
            )
            # (2) 語意 search 補漏 — 但只在結構化夠多時才加（recall 噪音多）
            # recall 會帶回 email 摘要 / metadata，LLM 容易誤把摘要當值
            # 因此這裡 default 不加 recall 結果
            info = structured_text
        elif company_context.startswith("customer:"):
            name = company_context[9:].strip()
            try:
                from agent_core.customer_intel import customer_360
                info = customer_360(name) or ""
            except Exception as e:
                info = f"customer_360 失敗：{e}"
        elif company_context.startswith("supplier:"):
            name = company_context[9:].strip()
            from agent_core.memory import recall
            info = recall(f"供應商 {name} 資料", k=5) or ""
        else:
            return f"❌ company_context 格式錯：'{company_context}'。可選 self / customer:NAME / supplier:NAME"
    except Exception as e:
        return f"❌ 抓資料失敗：{type(e).__name__}: {e}"
    if not info or len(info.strip()) < 10:
        return (f"❌ 沒抓到 {company_context} 的資料；先用 save_memory 存進記憶，"
                f"或改用 fill_document 手動指定 values")

    # Step 3: LLM mapping
    mapping_prompt = f"""你要把「文件待填欄位」對應到「公司資料」。

【待填欄位】
{json.dumps(fields_raw, ensure_ascii=False)[:4000]}

【可用資料】
{info[:4000]}

請回 JSON dict {{"欄位 label": "對應值"}}（嚴格格式，不要 markdown wrap）：

🚫 **絕對不可以**：
1. 把指示文字當作值。**禁止**任何包含「請填寫」「請輸入」「請於」
   「貴公司」開頭的字串作為 value — 這些是文件**說明**不是資料
2. 用「找不到」「無資料」「N/A」「(空)」作為 value — 直接 skip 該欄位
3. 從 hint / location / current_value 欄位抄字 — 那些是 metadata 不是答案
4. 自己編造（hallucinate）— 沒明確資料就 skip，寧可少填不要錯

✅ 規則：
- 只列你**從【可用資料】明確找得到對應**的欄位
- 找不到對應 → 完全 skip 該欄位（別把 label 也寫進去 dict）
- 簽名 / 簽核欄位 → skip
- 日期格式統一：YYYY-MM-DD
- 統一編號：純 8 位數字
- 結構化 key-value 區的資料**優先**於語意搜尋結果

範例（如何處理找不到的欄位）：
  ❌ {{"成立日期": "請填寫成立日期"}}     ← 不可
  ❌ {{"成立日期": "找不到"}}              ← 不可
  ❌ {{"成立日期": "N/A"}}                 ← 不可
  ✅ （直接不放這個 key 進 dict）          ← 對
"""
    try:
        from agent_core.gemini_client import _gemini_generate
        resp = _gemini_generate(model="gemini-2.5-flash-lite",
                                 contents=[mapping_prompt])
        text = resp.text if hasattr(resp, "text") else str(resp)
    except Exception as e:
        return f"❌ LLM mapping 失敗：{type(e).__name__}: {e}"

    mapping = _parse_mapping_json(text)
    if mapping is None:
        return f"❌ LLM mapping 回應不是合法 JSON：{text[:200]}"
    if not mapping:
        return "  （LLM 沒對到任何欄位）"

    # Step 4: fill
    return fill_document(abs_path, json.dumps(mapping, ensure_ascii=False),
                          output_path)


def _extract_fields_internal(abs_path: str):
    """同 extract_document_fields 但回 list（給 orchestrator 用）。"""
    ext = os.path.splitext(abs_path)[1].lower()
    if ext == ".docx":
        d = _read_docx(abs_path)
    elif ext == ".pdf":
        d = _read_pdf(abs_path)
    else:
        return f"❌ 不支援的副檔名 {ext}"
    if "error" in d:
        return f"❌ {d['error']}"

    if d["kind"] == "docx":
        content_summary = json.dumps({
            "paragraphs": d["paragraphs"][:50],
            "tables": [{"index": t["index"], "rows": t["rows"][:20]}
                       for t in d["tables"][:5]],
        }, ensure_ascii=False)[:8000]
    else:
        content_summary = json.dumps({
            "pages": [{"index": p["index"], "text": p["text"][:1500]}
                      for p in d["pages"][:5]],
        }, ensure_ascii=False)[:8000]

    prompt = (
        "分析這份企業文件，列出所有需要填寫的欄位。"
        "以 JSON list 回應，每筆含 label / current_value / location / "
        "expected_type / hint。\n\n" + content_summary
    )
    try:
        from agent_core.gemini_client import _gemini_generate
        resp = _gemini_generate(model="gemini-2.5-flash-lite",
                                 contents=[prompt])
        text = resp.text if hasattr(resp, "text") else str(resp)
    except Exception as e:
        return f"❌ LLM 失敗：{e}"
    fields = _parse_fields_json(text)
    if fields is None:
        return "❌ LLM 回應非 JSON"
    return fields[:_MAX_FIELDS_PER_DOC]


def _parse_mapping_json(text: str) -> dict | None:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    # Defense-in-depth：濾掉 LLM 仍把 prompt-text 當 value 寫進來的情況
    # （prompt 已說別這樣，但 LLM 還是有時會犯，這層 hard guard）
    return {
        str(k)[:100]: str(v)[:200]
        for k, v in d.items()
        if v and _is_real_value(str(v))
    }


def _is_real_value(value: str) -> bool:
    """濾掉 prompt-style 的假值。"""
    v = (value or "").strip()
    if not v:
        return False
    # 開頭是「請」+ 動詞 = prompt 文字，不是真值
    if re.match(r"^請(填寫|輸入|提供|於|附|簽|寫|選)", v):
        return False
    # 「找不到」「無資料」「N/A」/「(空)」/ 「未提供」 等推託詞
    if re.match(r"^(找不到|無資料|無|N\/A|n\/a|空|\(空\)|未提供|未填|未知|--+|-)$", v):
        return False
    if "請參考" in v or "請見" in v:
        return False
    return True
