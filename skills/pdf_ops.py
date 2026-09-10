"""PDF skill：抽文字、合併、拆分、搜尋、轉圖。

讓小紅能處理日常 PDF — 報價、發票、合約、DHL 提單。

依賴：pypdf, pdfplumber（表格抽得準）。OCR 交給 vision.analyze_image
（現有的 Gemini 視覺工具）處理，這裡不重造輪子。
"""
import io
import os
import re
from typing import Any


def _resolve_path(path: str) -> str:
    """展開 ~ 跟相對路徑 + V7 path safety guard（擋 ~/.ssh / *.pem 等）。
    被擋會 raise ValueError。"""
    from agent_core.path_safety import safe_path
    return safe_path(path)


def _safe_or_err(path: str):
    """Wrapper：成功回 (resolved, None)；被擋回 (None, error_msg)。"""
    try:
        return _resolve_path(path), None
    except ValueError as e:
        return None, str(e)


def _parse_page_range(spec: str, n_pages: int) -> list[int]:
    """把 '1-3,5,7-9' 解析成 [0,1,2,4,6,7,8]（0-indexed）。
    空字串代表全部。超出頁數的會被截掉。"""
    if not spec or not spec.strip():
        return list(range(n_pages))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                a_i = max(1, int(a))
                b_i = min(n_pages, int(b))
                for i in range(a_i, b_i + 1):
                    out.add(i - 1)
            except ValueError:
                continue
        else:
            try:
                i = int(part)
                if 1 <= i <= n_pages:
                    out.add(i - 1)
            except ValueError:
                continue
    return sorted(out)


def pdf_info(path: str) -> str:
    """看 PDF 檔的基本資訊：頁數、檔案大小、meta、首頁前 500 字預覽。

    Args:
        path: PDF 檔案路徑。
    """
    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到檔案：{p}"

    from pypdf import PdfReader
    try:
        reader = PdfReader(p)
    except Exception as e:
        return f"❌ 讀 PDF 失敗: {type(e).__name__}: {e}"

    size = os.path.getsize(p)
    meta = reader.metadata or {}
    lines = [
        f"📄 {os.path.basename(p)}",
        f"檔案大小：{size:,} bytes",
        f"頁數：{len(reader.pages)}",
    ]
    for k in ("/Title", "/Author", "/Subject", "/Creator", "/Producer"):
        v = meta.get(k)
        if v:
            lines.append(f"{k.strip('/')}: {v}")

    # 首頁預覽
    try:
        first = reader.pages[0].extract_text() or ""
        first = first.strip()[:500]
        if first:
            lines += ["", "--- 首頁前 500 字 ---", first]
    except Exception:
        pass

    return "\n".join(lines)


def pdf_extract_text(path: str, pages: str = "") -> str:
    """抽 PDF 的純文字內容。

    Args:
        path: PDF 路徑。
        pages: 頁範圍，例如 "1-3,5"；空字串代表全部。
    Returns:
        文字內容（如果全空可能是 scanned PDF，建議用 analyze_image OCR）。
    """
    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到檔案：{p}"

    from pypdf import PdfReader
    try:
        reader = PdfReader(p)
    except Exception as e:
        return f"❌ 讀 PDF 失敗: {type(e).__name__}: {e}"

    idxs = _parse_page_range(pages, len(reader.pages))
    if not idxs:
        return f"❌ 頁範圍 '{pages}' 無效（這 PDF 有 {len(reader.pages)} 頁）"

    chunks = []
    for i in idxs:
        try:
            t = reader.pages[i].extract_text() or ""
        except Exception as e:
            t = f"[頁 {i+1} 抽取失敗: {e}]"
        chunks.append(f"--- 頁 {i+1} ---\n{t.strip()}")

    text = "\n\n".join(chunks)
    if not text.strip() or len(text.strip()) < 20:
        text += "\n\n⚠️ 抽出的文字很少，可能是掃描版 PDF。試 analyze_image 工具做 OCR。"
    # Round 7：PDF 內容是攻擊者可控（任何寄件人可附 PDF 含 prompt-injection
    # 或 PII）。流入 LLM context 前過 sanitize_for_llm — 同 C4 recall 路徑。
    try:
        from agent_core.prompt_injection import sanitize_for_llm
        text = sanitize_for_llm(text)
    except Exception:
        pass
    return text


def pdf_extract_tables(path: str, pages: str = "") -> str:
    """抽 PDF 裡的表格（用 pdfplumber）— 合約/發票常用。

    Args:
        path: PDF 路徑。
        pages: 頁範圍，空字串=全部。
    Returns:
        每個表格用 Markdown 格式呈現。
    """
    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到檔案：{p}"

    import pdfplumber
    try:
        with pdfplumber.open(p) as pdf:
            idxs = _parse_page_range(pages, len(pdf.pages))
            if not idxs:
                return f"❌ 頁範圍 '{pages}' 無效"

            all_tables = []
            for i in idxs:
                tables = pdf.pages[i].extract_tables() or []
                for j, tbl in enumerate(tables):
                    if not tbl or len(tbl) < 2:
                        continue
                    header = tbl[0]
                    rows = tbl[1:]
                    md_lines = [f"### 頁 {i+1} 表 {j+1}",
                                "| " + " | ".join(str(c or "") for c in header) + " |",
                                "|" + "|".join("---" for _ in header) + "|"]
                    for r in rows[:20]:
                        md_lines.append("| " + " | ".join(str(c or "") for c in r) + " |")
                    if len(rows) > 20:
                        md_lines.append(f"...（共 {len(rows)} 列，只顯示前 20）")
                    all_tables.append("\n".join(md_lines))
    except Exception as e:
        return f"❌ pdfplumber 開檔失敗: {type(e).__name__}: {e}"

    if not all_tables:
        return f"📄 {os.path.basename(p)}：沒抓到任何表格（可能是 layout-based PDF）"
    out = f"📄 {os.path.basename(p)}\n\n" + "\n\n".join(all_tables)
    # Round 7: 表格 cell 內容也是 attacker-controlled
    try:
        from agent_core.prompt_injection import sanitize_for_llm
        out = sanitize_for_llm(out)
    except Exception:
        pass
    return out


def pdf_search(path: str, keyword: str, context_chars: int = 100) -> str:
    """在 PDF 全文搜尋關鍵字，回命中的頁碼與 context。

    Args:
        path: PDF 路徑。
        keyword: 要找的字（case-insensitive）。
        context_chars: 每次命中前後多少字 context（預設 100）。
    """
    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到檔案：{p}"
    if not keyword.strip():
        return "❌ keyword 不能空"

    from pypdf import PdfReader
    try:
        reader = PdfReader(p)
    except Exception as e:
        return f"❌ 讀 PDF 失敗: {e}"

    pat = re.compile(re.escape(keyword), re.IGNORECASE)
    hits = []
    for i, pg in enumerate(reader.pages):
        try:
            text = pg.extract_text() or ""
        except Exception:
            continue
        for m in pat.finditer(text):
            start = max(0, m.start() - context_chars)
            end = min(len(text), m.end() + context_chars)
            snippet = text[start:end].replace("\n", " ")
            hits.append(f"  頁 {i+1}: ...{snippet}...")
            if len(hits) >= 20:
                break
        if len(hits) >= 20:
            break

    if not hits:
        return f"🔍 '{keyword}' 在 {os.path.basename(p)} 找不到"
    out = (f"🔍 '{keyword}' 在 {os.path.basename(p)} 找到 {len(hits)} 處"
           + (" (只顯示前 20)" if len(hits) == 20 else "")
           + ":\n" + "\n".join(hits))
    # Round 8 C8-2：snippet 來自 attacker-supplied PDF — sanitize before LLM context
    try:
        from agent_core.prompt_injection import sanitize_for_llm
        out = sanitize_for_llm(out)
    except Exception:
        pass
    return out


def pdf_merge(paths: list[str], output_path: str) -> str:
    """合併多個 PDF 成一個檔案。

    Args:
        paths: PDF 檔案路徑的 list，按順序合併。
        output_path: 輸出檔名。
    Returns:
        成功 → 總頁數；失敗 → 錯誤訊息。
    """
    if not paths or len(paths) < 2:
        return "❌ 至少要 2 個 PDF 才需要合併"
    out, err = _safe_or_err(output_path)
    if err:
        return err
    if not out.lower().endswith(".pdf"):
        out += ".pdf"

    from pypdf import PdfWriter, PdfReader
    writer = PdfWriter()
    total = 0
    for raw in paths:
        p, err = _safe_or_err(raw)
        if err:
            return err
        if not os.path.isfile(p):
            return f"❌ 找不到：{p}"
        try:
            reader = PdfReader(p)
            for pg in reader.pages:
                writer.add_page(pg)
            total += len(reader.pages)
        except Exception as e:
            return f"❌ 讀 {p} 失敗: {e}"

    try:
        with open(out, "wb") as f:
            writer.write(f)
    except Exception as e:
        return f"❌ 寫 {out} 失敗: {e}"

    size = os.path.getsize(out)
    return f"✅ 合併完成：{out}（{len(paths)} 檔 → {total} 頁，{size:,} bytes）"


def pdf_split(path: str, pages: str, output_path: str) -> str:
    """把指定頁範圍抽出成新 PDF。

    Args:
        path: 來源 PDF。
        pages: 頁範圍，例如 "1-3,5"。
        output_path: 輸出路徑。
    """
    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到：{p}"
    out, err = _safe_or_err(output_path)
    if err:
        return err
    if not out.lower().endswith(".pdf"):
        out += ".pdf"

    from pypdf import PdfWriter, PdfReader
    try:
        reader = PdfReader(p)
    except Exception as e:
        return f"❌ 讀 {p} 失敗: {e}"
    idxs = _parse_page_range(pages, len(reader.pages))
    if not idxs:
        return f"❌ 頁範圍 '{pages}' 無效（PDF 有 {len(reader.pages)} 頁）"

    writer = PdfWriter()
    for i in idxs:
        writer.add_page(reader.pages[i])
    try:
        with open(out, "wb") as f:
            writer.write(f)
    except Exception as e:
        return f"❌ 寫檔失敗: {e}"
    size = os.path.getsize(out)
    return f"✅ 抽出 {len(idxs)} 頁 → {out}（{size:,} bytes）"


def pdf_to_images(path: str, output_dir: str = "", dpi: int = 150) -> str:
    """把 PDF 每一頁轉成 PNG（用 pypdfium2，不用裝 poppler）。
    適合要 OCR 掃描版 PDF 時，轉完丟給 analyze_image。

    Args:
        path: PDF 路徑。
        output_dir: 輸出資料夾，空字串=跟 PDF 同目錄下的 <name>_pages/。
        dpi: 解析度（預設 150；OCR 建議 200 以上，較清晰但慢）。
    """
    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到：{p}"

    if not output_dir:
        base = os.path.splitext(os.path.basename(p))[0]
        output_dir = os.path.join(os.path.dirname(p), f"{base}_pages")
    output_dir, err = _safe_or_err(output_dir)
    if err:
        return err
    os.makedirs(output_dir, exist_ok=True)

    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(p)
    except Exception as e:
        return f"❌ 開 PDF 失敗: {type(e).__name__}: {e}"

    zoom = dpi / 72
    saved = []
    try:
        for i, page in enumerate(pdf):
            pil = page.render(scale=zoom).to_pil()
            out = os.path.join(output_dir, f"page_{i+1:03d}.png")
            pil.save(out)
            saved.append(out)
    except Exception as e:
        return f"❌ 渲染失敗: {e}"

    return (f"✅ 已輸出 {len(saved)} 頁 → {output_dir}\n"
            f"   第一張：{saved[0] if saved else '無'}\n"
            f"   第一張可丟給 analyze_image 做 OCR / 結構化抽取")


def pdf_extract_images(path: str, output_dir: str = "", pages: str = "",
                       max_images: int = 0) -> str:
    """把 PDF 裡的**產品圖／繪圖**挖成獨立圖檔（不是整頁截圖）。

    跟 pdf_to_images 的差別：那顆是每頁一張整頁截圖（給 OCR 用）；這顆抽的是頁面
    裡的內嵌照片本體（logo/icon 會被濾掉、跨頁重複的頁首頁尾圖也會濾掉），純向量
    繪圖的頁才退回整頁繪製＋裁白邊。用途：客人規格單 / 型錄 → 產品圖 → 用
    export_report 的圖片儲存格（{"image": "<路徑>"}）嵌進 Excel 追蹤表。

    Args:
        path: PDF 路徑。
        output_dir: 輸出資料夾；留空 = 跟 PDF 同目錄下的 <name>_images/。
        pages: 只抽某幾頁，如 "1" / "1-3,5"；留空 = 全部（最多掃 40 頁）。
        max_images: 這份 PDF 最多抽幾張；0 = 預設上限。
    """
    from agent_core.doc_images import extract_pdf_images

    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到：{p}"
    if os.path.splitext(p)[1].lower() != ".pdf":
        return "❌ 這顆工具只吃 PDF。"

    if not output_dir:
        base = os.path.splitext(os.path.basename(p))[0]
        output_dir = os.path.join(os.path.dirname(p), f"{base}_images")
    out_dir, err = _safe_or_err(output_dir)
    if err:
        return err
    try:
        items = extract_pdf_images(
            p, out_dir, pages=pages,
            max_images=(max_images if max_images and max_images > 0 else None))
    except Exception as e:
        return f"❌ 抽圖失敗: {type(e).__name__}: {e}"
    if not items:
        return f"⚠️ {os.path.basename(p)} 抽不到可用的圖（整份可能都是文字，或圖太小被當 logo 濾掉）。"
    lines = [f"✅ 抽出 {len(items)} 張圖 → {out_dir}"]
    for it in items:
        lines.append(f"  • {it['path']}（第 {it['page']} 頁、{it['width']}×{it['height']}px、"
                     f"{'內嵌原圖' if it['source'] == 'embedded' else '整頁繪製裁白邊'}）")
    lines.append('要嵌進 Excel：export_report 的該格填 {"image": "<路徑>"}。')
    return "\n".join(lines)


SKILL_TOOLS = [pdf_info, pdf_extract_text, pdf_extract_tables,
               pdf_search, pdf_merge, pdf_split, pdf_to_images,
               pdf_extract_images]
