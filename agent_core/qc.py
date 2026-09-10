"""QC photo-inspection tools.

Extracted from agent_core/vision.py. Manages
`qc_masters/` directory of reference photos and compares submitted
factory samples against them with Gemini Vision, producing a JSON QC
report (grade A/B/C/FAIL, defect list, recommendations).

Reports are saved under `qc_reports/` as markdown with the raw JSON.
qc_inspect can optionally email the report back to the factory QC lead
via agent_core.gmail.send_gmail.
"""
import os
import re
import json
import mimetypes
from datetime import datetime

from agent_core.file_ops import _clean_path, _sanitize_filename
from agent_core.gemini_client import (
    GEMINI_MODEL,
    _get_genai_types,
    _gemini_generate,
    _prepare_image_for_gemini,
)
from agent_core.logging_and_paths import logger, _SCRIPT_DIR

_MAX_IMAGE_SIZE_MB = 20
_QC_MASTERS_DIR = os.path.join(_SCRIPT_DIR, "qc_masters")
_QC_REPORTS_DIR = os.path.join(_SCRIPT_DIR, "qc_reports")

_GRADE_EMOJI = {"A": "🟢", "B": "🟡", "C": "🟠", "FAIL": "🔴"}
_SEVERITY_EMOJI = {"致命": "💀", "嚴重": "🔴", "中等": "🟠", "輕微": "🟡"}


def _format_qc_summary(report: dict, product_model: str,
                       has_master: bool, master_path: str) -> str:
    """Turn Gemini's QC JSON into the human-readable summary block."""
    grade = report.get("grade", "?")
    score = report.get("score", "?")
    pass_ok = report.get("pass_for_shipment", False)
    comment = report.get("overall_comment", "")
    grade_emoji = _GRADE_EMOJI.get(grade, "❓")

    lines = [
        f"{grade_emoji} **QC 報告** | 鞋型：{product_model or '未指定'} | 等級：{grade} ({score}/100)",
        f"出貨建議：{'✅ 可出貨' if pass_ok else '❌ 不可出貨'}",
        f"總評：{comment}",
    ]
    if has_master:
        lines.append(f"🔍 已比對 master：{os.path.basename(master_path)}")

    defects = report.get("defects", [])
    if defects:
        lines.append(f"\n⚠️ 缺陷 {len(defects)} 項：")
        for d in defects:
            sev_mark = _SEVERITY_EMOJI.get(d.get("severity", ""), "⚪")
            lines.append(f"  {sev_mark} [{d.get('category', '?')}] {d.get('location', '?')}：{d.get('description', '?')}")

    for head, key, bullet in (("\n✨ 優點：", "strengths", "  • "),
                              ("\n📋 改善建議：", "recommendations", "  • ")):
        items = report.get(key, [])
        if items:
            lines.append(head)
            for s in items:
                lines.append(f"{bullet}{s}")

    return "\n".join(lines)


def _save_qc_report(summary: str, report: dict, src: str,
                    product_model: str, master_path: str) -> str:
    """Save markdown QC report to qc_reports/; return path or ""."""
    try:
        os.makedirs(_QC_REPORTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        safe_m = _sanitize_filename(product_model or "unknown", max_len=40)
        report_path = os.path.join(_QC_REPORTS_DIR, f"QC_{safe_m}_{ts}.md")
        md = [
            f"# QC 報告 {ts}",
            f"- 鞋型：{product_model or '未指定'}",
            f"- 檔案：{src}",
            f"- master：{master_path or '（無）'}",
            "",
            summary,
            "",
            "## Raw JSON",
            "```json",
            json.dumps(report, ensure_ascii=False, indent=2),
            "```",
        ]
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        return report_path
    except Exception as e:
        logger.warning("QC 報告存檔失敗：%s", e)
        return ""


def _notify_qc_to_factory(notify_factory_email: str, product_model: str,
                          summary: str, src: str, grade: str, score) -> str:
    """Email the QC report to factory contacts; return '\n  ✉️ ...' lines."""
    addrs = [a.strip() for a in re.split(r"[,\s;，、]+", notify_factory_email) if a.strip()]
    if not addrs:
        return ""
    try:
        from agent_core.gmail import send_gmail_internal
        subj = f"[QC] {product_model or '樣品'} - {grade} ({score}/100)"
        body = summary + f"\n\n原始照片：{os.path.basename(src)}\n（JAIFUNG AI QC Assistant）"
        out = ""
        for addr in addrs:
            # QC 報告是小紅產的判讀結果，寄給廠內地址會被 RAG 吃回去。
            r_send = send_gmail_internal(
                addr, subj, body, attachments=src,
                generated_by=f"qc:{product_model or 'sample'}",
            )
            out += f"\n  ✉️ {addr}: {r_send[:60]}"
        return out
    except Exception as e:
        return f"\n  ⚠️ 寄工廠失敗：{e}"


def _qc_master_meta_path(product_model: str) -> str:
    safe = _sanitize_filename(product_model, max_len=80)
    return os.path.join(_QC_MASTERS_DIR, f"{safe}.meta.json")


def _qc_master_photo_path(product_model: str, ext: str = "jpg") -> str:
    safe = _sanitize_filename(product_model, max_len=80)
    return os.path.join(_QC_MASTERS_DIR, f"{safe}.{ext}")


def set_qc_master(product_model: str, photo_path: str, notes: str = ""):
    """設定某鞋型的 QC master sample 照片（日後 qc_inspect 會跟這張比對）。
    - product_model：鞋型代號（例 AF-1 Pro v2）
    - photo_path：master 照片路徑
    - notes：可選的規格備註（材質/顏色/特殊要求）

    Round 8 M8-3：notes 會被 list_qc_masters 印出 — 同 C7 class 永久 prompt-
    injection。寫入前 sanitize_untrusted_text。
    """
    pm = product_model.strip()
    if not pm:
        return "錯誤：product_model 不能空"
    # M8-3 sanitize notes
    try:
        from agent_core.prompt_injection import sanitize_untrusted_text
        notes = sanitize_untrusted_text(notes or "")
    except Exception:
        pass
    src = _clean_path(photo_path)
    if not os.path.exists(src):
        return f"錯誤：找不到 {src}"

    os.makedirs(_QC_MASTERS_DIR, exist_ok=True)
    ext = os.path.splitext(src)[1].lstrip(".").lower() or "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp", "heic"):
        return f"錯誤：僅支援 jpg/png/webp/heic（收到 {ext}）"
    dst = _qc_master_photo_path(pm, ext)

    try:
        import shutil as _sh
        _sh.copy2(src, dst)
        meta = {
            "product_model": pm,
            "master_photo": dst,
            "notes": notes.strip(),
            "set_at": datetime.now().isoformat(timespec="seconds"),
            "source_path": src,
        }
        with open(_qc_master_meta_path(pm), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return f"✅ 已設定 QC master：{pm}\n   檔案：{dst}\n   備註：{notes or '（無）'}"
    except Exception as e:
        return f"設定失敗：{e}"


def list_qc_masters():
    """列出所有已設定的 QC master sample。"""
    if not os.path.isdir(_QC_MASTERS_DIR):
        return "還沒設定任何 master。用 set_qc_master('鞋型', '照片路徑') 設第一個。"
    metas = [f for f in os.listdir(_QC_MASTERS_DIR) if f.endswith(".meta.json")]
    if not metas:
        return "還沒設定任何 master。"
    lines = [f"📷 QC master 共 {len(metas)} 個："]
    for m in sorted(metas):
        try:
            with open(os.path.join(_QC_MASTERS_DIR, m), "r", encoding="utf-8") as _f:
                d = json.load(_f)
            pm = d.get("product_model", "?")
            when = d.get("set_at", "?")[:16]
            notes = d.get("notes", "")
            lines.append(f"  • {pm}  設於 {when}  {('│ ' + notes[:40]) if notes else ''}")
        except Exception:
            lines.append(f"  ⚠️ {m} 讀取失敗")
    return "\n".join(lines)


def _qc_inspect_prompt(product_model: str, master_notes: str, has_master: bool) -> str:
    base = (
        "你是資深鞋類品質檢驗員（30 年經驗）。客戶把代工鞋樣拍照寄來，請以**專業 QC 角度**分析。\n"
        f"鞋型：{product_model or '(未指定)'}\n"
        + (f"master 規格備註：{master_notes}\n" if master_notes else "")
        + (
            "\n你會同時看到【master sample】和【被檢樣品】兩張圖：\n"
            "請逐項比對差異。\n"
            if has_master else
            "\n只有被檢樣品一張圖，請以專業標準單獨評分。\n"
        )
        + "\n**嚴格回傳 JSON**（無 markdown 圍欄、無說明）：\n"
        "{\n"
        '  "grade": "A/B/C/FAIL",\n'
        '  "score": 85,     // 0-100 整數\n'
        '  "defects": [\n'
        '    {"category": "色差|車縫|Logo|鞋面皺褶|對稱性|針數|大底|配料|污漬|其他",\n'
        '     "severity": "致命|嚴重|中等|輕微",\n'
        '     "location": "鞋頭外側/後跟/鞋舌/鞋底/...",\n'
        '     "description": "具體狀況一句話"}\n'
        '  ],\n'
        '  "strengths": ["做得好的地方 (1-3 個)"],\n'
        '  "recommendations": [\n'
        '    "給工廠的具體改善指示 (1-3 條)"\n'
        '  ],\n'
        '  "pass_for_shipment": true/false,\n'
        '  "overall_comment": "一段 50 字內的總評"\n'
        "}\n\n"
        "評分原則：\n"
        "  A (90-100)：可直接出貨，無須返工\n"
        "  B (75-89)：小瑕疵但可出貨，記錄下次改善\n"
        "  C (60-74)：需返工修正再出貨\n"
        "  FAIL (<60)：無法出貨，重做\n\n"
        "**致命** 缺陷一率 FAIL（無論其他多好）；如：Logo 歪斜明顯、大底脫膠、色差 ΔE>3\n"
    )
    return base


def qc_inspect(photo_path: str, product_model: str = "",
               save_report: bool = True, use_master: bool = True,
               notify_factory_email: str = ""):
    """用 Gemini Vision 檢驗一張鞋子照片，產專業 QC 報告。
    - photo_path：待檢照片
    - product_model：鞋型代號；若有 master 照片會自動比對
    - use_master：True 則載入對應 master 做雙圖比對；False 單圖評分
    - save_report：存 markdown 報告到 qc_reports/
    - notify_factory_email：如填，把報告內文寄給工廠 QC 主管（附原照片）

    回傳：JSON 報告 + 人類可讀摘要
    """
    src = _clean_path(photo_path)
    if not os.path.exists(src):
        return f"錯誤：找不到 {src}"
    img_size_mb = os.path.getsize(src) / (1024 * 1024)
    if img_size_mb > _MAX_IMAGE_SIZE_MB:
        return f"錯誤：圖片太大（{img_size_mb:.1f}MB），超過 {_MAX_IMAGE_SIZE_MB}MB 上限"

    master_path = ""
    master_notes = ""
    has_master = False
    if product_model.strip() and use_master:
        meta_p = _qc_master_meta_path(product_model.strip())
        if os.path.exists(meta_p):
            try:
                with open(meta_p, "r", encoding="utf-8") as _f:
                    meta = json.load(_f)
                master_path = meta.get("master_photo", "")
                master_notes = meta.get("notes", "")
                if master_path and os.path.exists(master_path):
                    has_master = True
            except Exception as _e:
                logger.debug("讀 master meta 失敗：%s", _e)

    parts = []
    try:
        if has_master:
            with open(master_path, "rb") as f:
                mdata = f.read()
            m_mime, _ = mimetypes.guess_type(master_path)
            # Gemini 拒收 tiff/bmp 等 → 先轉 JPEG。
            mdata, m_mime = _prepare_image_for_gemini(mdata, m_mime or "image/jpeg")
            parts.append(_get_genai_types().Part.from_bytes(data=mdata, mime_type=m_mime))
            parts.append("↑ 這是 master sample")
        with open(src, "rb") as f:
            idata = f.read()
        mime, _ = mimetypes.guess_type(src)
        idata, mime = _prepare_image_for_gemini(idata, mime or "image/jpeg")
        parts.append(_get_genai_types().Part.from_bytes(data=idata, mime_type=mime))
        parts.append("↑ 這是被檢樣品")
        parts.append(_qc_inspect_prompt(product_model, master_notes, has_master))
    except Exception as e:
        return f"讀圖失敗：{e}"

    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=parts)
        text = ((resp.text if resp else None) or "").strip()
    except Exception as e:
        return f"Gemini 分析失敗：{e}"

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return f"Gemini 回傳非 JSON：\n{text[:500]}"
    try:
        report = json.loads(m.group(0))
    except Exception as e:
        return f"JSON 解析失敗：{e}\n原文：{text[:500]}"

    if not isinstance(report, dict):
        return f"Gemini 回的 JSON 不是物件（是 {type(report).__name__}）：\n{text[:500]}"

    summary = _format_qc_summary(report, product_model, has_master, master_path)

    report_path = _save_qc_report(summary, report, src, product_model, master_path) if save_report else ""
    email_line = _notify_qc_to_factory(
        notify_factory_email, product_model, summary, src,
        report.get("grade", "?"), report.get("score", "?"),
    )

    tail = ""
    if report_path:
        tail += f"\n\n📂 已存檔：{report_path}"
    if email_line:
        tail += f"\n📧 已通知工廠：{email_line}"
    return summary + tail


def qc_batch_inspect(folder_path: str, product_model: str = ""):
    """批次檢查一整個資料夾裡的所有圖片。
    - folder_path：資料夾
    - product_model：統一鞋型（若照片都是同一型）；留空則對每張單獨評

    回傳：總覽（各檔等級 + 缺陷數）
    """
    folder = _clean_path(folder_path)
    if not os.path.isdir(folder):
        return f"錯誤：{folder} 不是資料夾"

    exts = (".jpg", ".jpeg", ".png", ".webp")
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(exts))
    if not files:
        return f"{folder} 中沒有圖片檔（支援 {exts}）"

    lines = [f"📷 批次 QC 檢查：{folder}（{len(files)} 張）"]
    stats = {"A": 0, "B": 0, "C": 0, "FAIL": 0, "?": 0}
    for f in files:
        fp = os.path.join(folder, f)
        print(f"[QC] 檢查 {f}...")
        r = qc_inspect(fp, product_model=product_model, save_report=True)
        first_line = r.split("\n")[0] if isinstance(r, str) else "?"
        grade = "?"
        for g in ("FAIL", "A", "B", "C"):
            if f"等級：{g}" in first_line:
                grade = g
                break
        stats[grade] = stats.get(grade, 0) + 1
        lines.append(f"  {first_line[:120]}")

    lines.append(f"\n📊 統計：🟢 A={stats['A']} | 🟡 B={stats['B']} | 🟠 C={stats['C']} | 🔴 FAIL={stats['FAIL']} | ❓ ?={stats['?']}")
    return "\n".join(lines)
