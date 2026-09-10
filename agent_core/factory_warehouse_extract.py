"""Phase 3b：從出貨/付款單據「內容」抽結構化欄位（金額/日期/對方/發票號）。

3a 只解析檔名；3b 讀單據內容（read_drive_file → PDF 走 pypdf 文字、便宜）再用 Gemini
flash-lite 結構化。結果 append 到倉外持久化 jsonl（factory_warehouse.payments_store_path）—
倉每次全量重建會清表，抽取昂貴不能每次重做，故存倉外、重建時由 _load_payments 載回 fact_payment。

設計取捨（成本責任）：
  - **增量**：跳過已抽過的 file_id（讀 store）。
  - **預算上限**：caller 今日成本達 budget_usd 即停（沿用 drive_sync 的 cost 對帳）。
  - **時間上限**：time_budget_s wall-clock 預算、逐份檢查。單份耗時高變異（drive_sync
    belt 超時一份就吃 150s、掃描 PDF 走本地 OCR、Gemini 503 重試），沒有時間上限時
    cron 的 run_with_deadline 會整輪撞牆 exit 75（2026-07-24 12:50 事故）。
  - **先只吃 PDF**：mime 含 pdf。圖片類要 vision（貴、且撞 $1/天 image OCR 閘門），留待後續批次。
"""
import json
import os
import time
from datetime import datetime, timezone

from agent_core.factory_warehouse import payments_store_path, warehouse_db_path

_EXTRACT_CALLER = "factory_warehouse.extract_payment"

_EXTRACT_PROMPT = (
    "你是單據抽取員。下面是一份出貨/付款單據的文字（PDF/OCR/試算表）；"
    "開頭【檔名】常已寫明金額/對方/日期，掃描檔內文抽不到時請從檔名抓。嚴格輸出 JSON："
    '{"amount": number|null, "currency": string, "doc_date": "YYYY-MM-DD"|"", '
    '"counterparty": string, "invoice_no": string}\n'
    "- amount：單據主要金額（總額/匯款額），純數字不要千分位逗號，抓不到回 null\n"
    "- currency：USD/EUR/CNY/TWD…抓不到回 \"\"\n"
    "- doc_date：單據日期（出貨/匯款/開立），轉 YYYY-MM-DD；抓不到回 \"\"\n"
    "- counterparty：對方公司/客戶/供應商名；抓不到回 \"\"\n"
    "- invoice_no：發票或單據號；抓不到回 \"\"\n"
    "不要解釋、不要 markdown。文字：\n"
)


def _done_file_ids(store_path):
    done = set()
    if os.path.exists(store_path):
        with open(store_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["file_id"])
                except (ValueError, KeyError):
                    continue
    return done


def _structure_payment(text):
    """單據文字 → 結構化欄位 dict（Gemini flash-lite）。"""
    from agent_core.gemini_client import _gemini_generate
    from agent_core.internal_emails_extract import INGEST_MODEL
    resp = _gemini_generate(
        model=INGEST_MODEL,
        contents=[_EXTRACT_PROMPT + (text or "")[:6000]],
        config={"response_mime_type": "application/json"},
        caller=_EXTRACT_CALLER,
    )
    try:
        d = json.loads(resp.text or "{}")
    except (ValueError, TypeError):
        d = {}
    if not isinstance(d, dict):   # Gemini 偶爾回 JSON 陣列 [...]，當作抽不到
        d = {}
    # 容千分位逗號（'16,006.89' 不再 float() 失敗變 null）；共用建倉的 coercion。
    from agent_core.factory_payment_norm import _coerce_amount
    amt = _coerce_amount(d.get("amount"))
    return {
        "amount": amt,
        "currency": str(d.get("currency") or "").strip()[:8],
        "doc_date": str(d.get("doc_date") or "").strip()[:10],
        "counterparty": str(d.get("counterparty") or "").strip()[:120],
        "invoice_no": str(d.get("invoice_no") or "").strip()[:60],
    }


def _now_iso():
    return datetime.now(timezone.utc).isoformat()[:19]


# 掃描 PDF（無文字層）的 OCR fallback：匯款單/收據常是掃描檔，pypdf 抽不到文字 → 金額是 null。
# 用 pypdfium2（已是相依）rasterize 前幾頁 → drive_sync._extract_image_vision（macOS ocrmac
# 本地 OCR、免費、無 503）讀回文字。非 macOS / pypdfium2 缺 / 下載失敗皆 graceful 回 ''。
_OCR_PDF_MAX_PAGES = 3        # 收據/匯款單通常 1 頁，上限保護
_OCR_PDF_RENDER_SCALE = 2.0   # ≈144 dpi，夠 OCR、不過大


def _ocr_scanned_pdf(file_id, max_pages=_OCR_PDF_MAX_PAGES):
    """掃描 PDF → pypdfium2 rasterize → ocrmac 本地 OCR。回文字或 ''（全程 graceful）。

    成本：rasterize 與 ocrmac 都本地免費；只打一次 Drive get_media。
    """
    try:
        import io

        import pypdfium2 as pdfium
    except Exception:  # noqa: BLE001 — pypdfium2 缺 → 交回呼叫端用原文字
        return ""
    from agent_core.google_auth import get_service
    from agent_core.ingest.drive_sync import _execute_drive_request, _extract_image_vision
    try:
        service = get_service("drive", "v3")
        data = _execute_drive_request(
            lambda: service.files().get_media(fileId=file_id, supportsAllDrives=True),
            f"Drive get_media (payment OCR) {file_id}")
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(data, bytes) or not data:
        return ""
    parts, pdf = [], None
    try:
        pdf = pdfium.PdfDocument(data)
        for i in range(min(len(pdf), max_pages)):
            bitmap = pdf[i].render(scale=_OCR_PDF_RENDER_SCALE)
            png = io.BytesIO()
            bitmap.to_pil().save(png, format="PNG")
            text = _extract_image_vision(png.getvalue(), "image/png")
            if text:
                parts.append(text)
    except Exception:  # noqa: BLE001 — 非 PDF / 解析失敗 → 回 ''
        return ""
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(parts).strip()


def _read_doc_text(file_id, max_chars=6000):
    """付款單據文字讀取：read_drive_file 為主；內容過薄（掃描 PDF 無文字層）fallback 本地 ocrmac。

    read_drive_file 成功以 '📄' 開頭；空抽取回「…抽出來是空的」；不支援/太大回 '⚠'（不重抽，
    避免重新下載）。只有「📄 但本文過短」或「空抽取」才補 OCR。
    """
    from agent_core.ingest.drive_search import read_drive_file
    from agent_core.ingest.drive_sync import _VISION_OCR_MIN_CHARS

    txt = read_drive_file(file_id, max_chars=max_chars) or ""
    if txt.startswith("📄"):
        body = txt.split("\n", 1)[1] if "\n" in txt else ""
        need_ocr = len(body.strip()) < _VISION_OCR_MIN_CHARS
    elif "抽出來是空的" in txt:        # pypdf 抽不到文字（掃描 PDF）
        need_ocr = True
    else:                              # ⚠ 不支援/太大/讀取失敗 → 不 OCR
        need_ocr = False
    if need_ocr:
        ocr = _ocr_scanned_pdf(file_id)
        if ocr:
            return f"📄 OCR\n{ocr[:max_chars]}"
    return txt


def _append_payment_record(store, rec):
    """逐筆 append 落盤：整批結束才寫的話，看門狗砍掉的輪會把已抽（已付費）的全部丟掉、
    下輪重抽重付；逐筆寫最多丟「正在處理的那一份」，done 集合也逐份前進。"""
    d = os.path.dirname(store)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(store, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def extract_payments_batch(max_docs=25, budget_usd=0.5, db_path=None,
                           read_text=None, structure=None, time_budget_s=None):
    """從倉的 fact_shipment_doc 挑「未抽過的付款/發票單據」抽結構化欄位 → append 持久化 store。

    增量（跳過已抽）+ 預算上限（caller 今日成本達 budget_usd 即停）+ 時間上限
    （time_budget_s 秒，None=不限；逐份開工前檢查，超時提前收工、summary 帶
    time_exhausted=True）。吃 PDF/Excel/Word/圖片（掃描 PDF 走 pypdfium2+ocrmac 本地
    OCR fallback）。read_text/structure 可注入（測試用）。回 summary dict。
    """
    import duckdb

    from agent_core.ingest.drive_sync import _caller_cost_today_usd

    if read_text is None:
        read_text = lambda fid: _read_doc_text(fid, max_chars=6000)  # noqa: E731
    if structure is None:
        structure = _structure_payment

    db_path = db_path or warehouse_db_path()
    if not os.path.exists(db_path):
        return {"error": "倉不存在，先 rebuild_factory_warehouse"}
    store = payments_store_path()
    done = _done_file_ids(store)

    con = duckdb.connect(db_path, read_only=True)
    try:
        # 不再只吃 PDF：read_drive_file 同一套抽取器也讀 xlsx/docx/txt（多數發票是 Excel）
        # 與圖片（匯款單/收據常是掃描檔；圖片走本地 ocrmac OCR、文件類免費）。只排掉
        # 抽不出文字的 folder/shortcut/outlook .msg；其餘交給 read_drive_file，不支援者回 ⚠ 自會跳過。
        rows = con.execute(
            "SELECT file_id, title, lot_number, doc_type FROM fact_shipment_doc "
            "WHERE doc_type IN ('remittance','invoice','receipt','payable') "
            "AND lower(mime_type) NOT LIKE '%folder%' "
            "AND lower(mime_type) NOT LIKE '%shortcut%' "
            "AND lower(mime_type) NOT LIKE '%ms-outlook%' "
            "ORDER BY modified DESC"
        ).fetchall()
    finally:
        con.close()
    todo = [r for r in rows if r[0] not in done]

    start_cost = _caller_cost_today_usd(_EXTRACT_CALLER)
    started_ts = time.monotonic()
    time_exhausted = False
    extracted = 0
    for file_id, title, lot, doc_type in todo:
        if extracted >= max_docs:
            break
        if time_budget_s is not None and time.monotonic() - started_ts >= time_budget_s:
            time_exhausted = True
            break
        if _caller_cost_today_usd(_EXTRACT_CALLER) - start_cost >= budget_usd:
            break
        try:
            text = read_text(file_id)
        except Exception:  # noqa: BLE001
            continue
        if not text or text.lstrip()[:1] in ("⚠", "❌"):
            continue
        try:
            # 檔名常含金額/對方/日期（掃描檔內文抽不到時的救命稻草），一起餵給抽取器。
            fields = structure(f"【檔名】{title}\n{text}")
        except Exception:  # noqa: BLE001 — 單份抽取失敗只跳過，別中斷整批
            continue
        _append_payment_record(store, {
            "file_id": file_id, "lot_number": lot, "doc_type": doc_type,
            "title": title, "extracted_at": _now_iso(), **fields})
        extracted += 1

    return {"extracted": extracted, "candidates_remaining": len(todo) - extracted,
            "store": store, "time_exhausted": time_exhausted,
            "spent_usd": round(_caller_cost_today_usd(_EXTRACT_CALLER) - start_cost, 4)}
