"""Spec-sheet tools: parse / list / compare.

Extracted from agent_core/vision.py. Takes a
customer spec sheet (PDF / Word / image), extracts structured fields
with Gemini, stores versioned JSON under specs/<customer>__<model>/,
and can diff two versions to surface "what actually changed + how risky".

Each product's versions live at specs/<safe_customer>__<safe_model>/v<YYYYMMDD_HHMMSS>.json
so compare_specs can walk the directory chronologically.
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
    _get_gemini_client,
    _gemini_generate,
    _prepare_image_for_gemini,
    _wait_for_file_ready,
    upload_file,
)
from agent_core.logging_and_paths import logger, _SCRIPT_DIR

_SPECS_DIR = os.path.join(_SCRIPT_DIR, "specs")


def _specs_key(customer: str, product_model: str) -> str:
    c = _sanitize_filename(customer, max_len=40, strip_whitespace=True)
    p = _sanitize_filename(product_model, max_len=40, strip_whitespace=True)
    return f"{c}__{p}"


def _specs_dir_for(customer: str, product_model: str) -> str:
    d = os.path.join(_SPECS_DIR, _specs_key(customer, product_model))
    os.makedirs(d, exist_ok=True)
    return d


def _parse_spec_with_gemini(file_path: str, file_mime: str) -> dict:
    """用 Gemini Vision 讀 PDF/圖片/Word 抽取結構化規格。回傳 dict 或 {error}。"""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except Exception as e:
        return {"error": f"讀檔失敗：{e}"}

    size_mb = len(data) / (1024 * 1024)
    use_upload = size_mb > 15

    prompt = (
        "你是鞋類材料工程師。請從這份客戶規格書中**完整抽取**所有技術規格欄位，"
        "**嚴格回傳 JSON**（無 markdown、無說明）：\n"
        "{\n"
        '  "customer": "客戶名（若文件中有）",\n'
        '  "product_model": "料號/鞋型代號",\n'
        '  "document_date": "文件日期 YYYY-MM-DD 或空",\n'
        '  "document_version": "版本號或空",\n'
        '  "specs": {\n'
        '    "upper": {"material": "...", "color": "...", "thickness_mm": 1.2, ...},\n'
        '    "outsole": {"material": "...", "hardness_shore_A": 65, ...},\n'
        '    "midsole": {...},\n'
        '    "lining": {...},\n'
        '    "insole": {...},\n'
        '    "laces": {...},\n'
        '    "eyelets": {...},\n'
        '    "sizing": {"range": "US 6-12", "grading": "..."},\n'
        '    "packaging": {...},\n'
        '    "labeling": {...},\n'
        '    "performance_requirements": {...},\n'
        '    "quality_standards": ["ISO 20344", ...]\n'
        '  },\n'
        '  "tolerances": {"color_delta_E": "<=3.0", "hardness": "±3", ...},\n'
        '  "critical_notes": ["有色差爭議的歷史...", "這個料號要送 SGS..."],\n'
        '  "extra": {}\n'
        "}\n\n"
        "規則：\n"
        "1. 沒寫的欄位用空字串或 null，不要亂編\n"
        "2. 數值一律抽成 number（不要加單位），單位記在 key 名稱（thickness_mm、hardness_shore_A）\n"
        "3. 文件可能是中文、英文或中英混合，統一抽 key 用英文（材料值保留原文）\n"
        "4. 能抽多少抽多少，越細越好\n"
    )
    uploaded = None
    try:
        if use_upload:
            uploaded = upload_file(_get_gemini_client(), file_path, mime_type=file_mime)
            uploaded = _wait_for_file_ready(uploaded)
            resp = _gemini_generate(model=GEMINI_MODEL, contents=[uploaded, prompt])
        else:
            part_data, part_mime = data, file_mime
            if (file_mime or "").startswith("image/"):
                # Gemini 拒收 tiff/bmp 等 → 先轉 JPEG（PDF/Word 不動）。
                part_data, part_mime = _prepare_image_for_gemini(data, file_mime)
            part = _get_genai_types().Part.from_bytes(data=part_data, mime_type=part_mime)
            resp = _gemini_generate(model=GEMINI_MODEL, contents=[part, prompt])
        text = ((resp.text if resp else None) or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"error": f"Gemini 回非 JSON：{text[:300]}"}
        parsed = json.loads(m.group(0))
        if not isinstance(parsed, dict):
            return {"error": f"Gemini 回的不是 dict（是 {type(parsed).__name__}）"}
        return parsed
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        if uploaded is not None:
            try:
                _get_gemini_client().files.delete(name=uploaded.name)
            except Exception as _e:
                logger.debug("清 Gemini upload 失敗（%s），48h 會自動過期", _e)


def parse_spec_sheet(file_path: str, customer: str = "", product_model: str = "",
                     save: bool = True):
    """解析一份規格書（PDF / 圖片 / Word），抽結構化欄位並存檔。
    - file_path：規格書路徑（支援 .pdf / .docx / .jpg / .png）
    - customer / product_model：可選，若空會從文件內容推斷
    - save：True 則存到 specs/<customer>__<model>/v<YYYYMMDD_HHMMSS>.json

    回傳：抽出的結構化規格 summary
    """
    src = _clean_path(file_path)
    if not os.path.exists(src):
        return f"錯誤：找不到 {src}"

    mime, _ = mimetypes.guess_type(src)
    if not mime:
        ext = os.path.splitext(src)[1].lower()
        mime_map = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
            ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp",
        }
        mime = mime_map.get(ext, "application/octet-stream")

    print(f"📄 解析規格書：{src}（MIME={mime}）...")
    spec = _parse_spec_with_gemini(src, mime)
    if "error" in spec:
        return f"❌ 解析失敗：{spec['error']}"

    c = customer.strip() or spec.get("customer", "") or "unknown"
    p = product_model.strip() or spec.get("product_model", "") or "unknown"
    spec["customer"] = c
    spec["product_model"] = p
    spec["parsed_at"] = datetime.now().isoformat(timespec="seconds")
    spec["source_file"] = src

    saved_path = ""
    if save:
        try:
            d = _specs_dir_for(c, p)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_path = os.path.join(d, f"v{ts}.json")
            with open(saved_path, "w", encoding="utf-8") as f:
                json.dump(spec, f, ensure_ascii=False, indent=2)
        except Exception as e:
            saved_path = f"(存檔失敗：{e})"

    specs_obj = spec.get("specs", {}) or {}
    lines = [
        f"📄 規格書已解析：{c} / {p}",
        f"   文件日期：{spec.get('document_date', '?')}   版本：{spec.get('document_version', '?')}",
        f"   抽出 {len(specs_obj)} 大類：{', '.join(specs_obj.keys())}",
    ]
    crit = spec.get("critical_notes", [])
    if crit:
        lines.append(f"   ⚠️ 關鍵備註 {len(crit)}：")
        for n in crit[:3]:
            lines.append(f"     - {n}")
    if saved_path:
        lines.append(f"   📂 已存：{saved_path}")
    return "\n".join(lines)


def list_specs(customer: str = "", product_model: str = ""):
    """列出已解析的規格書版本。兩個參數空 → 全部；有值則過濾。"""
    if not os.path.isdir(_SPECS_DIR):
        return "尚未解析過任何規格書。用 parse_spec_sheet(檔案) 開始。"
    lines = []
    for d in sorted(os.listdir(_SPECS_DIR)):
        dp = os.path.join(_SPECS_DIR, d)
        if not os.path.isdir(dp):
            continue
        if "__" not in d:
            continue
        c_part, p_part = d.split("__", 1)
        if customer.strip() and customer.strip().lower() not in c_part.lower():
            continue
        if product_model.strip() and product_model.strip().lower() not in p_part.lower():
            continue
        versions = sorted([f for f in os.listdir(dp) if f.startswith("v") and f.endswith(".json")])
        if not versions:
            continue
        lines.append(f"📁 {c_part} / {p_part}  （{len(versions)} 版）")
        for v in versions[-3:]:
            lines.append(f"   • {v}")
    if not lines:
        return f"找不到符合 customer={customer!r} product_model={product_model!r} 的規格。"
    return "\n".join(lines)


def _load_spec_version(customer: str, product_model: str, version: str = "latest") -> dict:
    """回傳指定版本的 spec dict，失敗回 {}。"""
    d = _specs_dir_for(customer, product_model)
    if not os.path.isdir(d):
        return {}
    versions = sorted([f for f in os.listdir(d) if f.startswith("v") and f.endswith(".json")])
    if not versions:
        return {}
    if version == "latest":
        fname = versions[-1]
    elif version == "previous":
        if len(versions) < 2:
            return {}
        fname = versions[-2]
    else:
        key = version if version.startswith("v") else "v" + version
        fname = key + ".json" if not key.endswith(".json") else key
        if fname not in versions:
            return {}
    try:
        with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def compare_specs(customer: str, product_model: str,
                  new_file_path: str = "", new_version_spec: dict = None,
                  old_version: str = "previous"):
    """比對新舊規格，用 Gemini 判斷「真正的變更 + 風險」。
    - customer / product_model：指定要比對的產品
    - new_file_path：若有新檔案路徑，會先 parse 存下新版再跟舊版比
    - new_version_spec：直接傳 parsed dict（內部用）
    - old_version：'previous' 倒數第 2 版 | 'latest' 最新 | 或指定 'v20260419_120000'

    回傳：變更清單 + 每項風險等級（致命/嚴重/普通/微小）
    """
    if not customer.strip() or not product_model.strip():
        return "錯誤：customer 和 product_model 都要填"

    if new_version_spec:
        spec_new = new_version_spec
    elif new_file_path:
        parse_msg = parse_spec_sheet(new_file_path, customer, product_model, save=True)
        if "❌" in parse_msg:
            return parse_msg
        spec_new = _load_spec_version(customer, product_model, "latest")
    else:
        spec_new = _load_spec_version(customer, product_model, "latest")

    spec_old = _load_spec_version(customer, product_model, old_version)
    if not spec_new:
        return f"找不到新版 spec for {customer}/{product_model}"
    if not spec_old:
        return f"找不到可比對的舊版（old_version={old_version}）— 可能是第一次解析這個 model"

    prompt = (
        "以下是同一個鞋類產品的兩個版本規格書（JSON）。請**專業工程師角度**比對，找出所有實質變更：\n"
        "**嚴格回傳 JSON**（無 markdown）：\n"
        "{\n"
        '  "total_changes": 整數,\n'
        '  "changes": [\n'
        '    {"path": "specs.outsole.hardness_shore_A",\n'
        '     "old_value": 65,\n'
        '     "new_value": 70,\n'
        '     "change_type": "modified|added|removed",\n'
        '     "risk": "致命|嚴重|普通|微小",\n'
        '     "impact": "硬度增加可能需要換模，影響模具成本 NT$15-30 萬",\n'
        '     "action_needed": "跟客戶確認是否願意承擔模具費"}\n'
        '  ],\n'
        '  "summary": "一段 100 字內的變更總結",\n'
        '  "requires_requote": true/false,\n'
        '  "requires_new_sample": true/false\n'
        "}\n\n"
        "風險評級：\n"
        "- 致命：需換模、換材料源、認證要重做\n"
        "- 嚴重：影響成本 >5%、需大幅調整製程、交期可能延長\n"
        "- 普通：可吸收成本、小調整製程、不影響交期\n"
        "- 微小：純文書（客戶地址改了之類）\n\n"
        f"舊版（{old_version}）：\n```json\n{json.dumps(spec_old, ensure_ascii=False)[:4000]}\n```\n\n"
        f"新版（latest）：\n```json\n{json.dumps(spec_new, ensure_ascii=False)[:4000]}\n```\n"
    )
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[prompt])
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return f"Gemini 回非 JSON：{text[:300]}"
        diff = json.loads(m.group(0))
    except Exception as e:
        return f"比對失敗：{e}"

    changes = diff.get("changes", [])
    lines = [
        f"📊 {customer} / {product_model} 規格變更分析",
        f"   舊版：{old_version}　新版：{spec_new.get('parsed_at', '?')[:16]}",
        f"   變更總數：{diff.get('total_changes', len(changes))}",
        f"   需重新報價：{'✅' if diff.get('requires_requote') else '❌'}　需重打樣：{'✅' if diff.get('requires_new_sample') else '❌'}",
    ]
    if diff.get("summary"):
        lines.append(f"\n📝 {diff['summary']}")

    if changes:
        risk_order = {"致命": 0, "嚴重": 1, "普通": 2, "微小": 3}
        changes_sorted = sorted(changes, key=lambda c: risk_order.get(c.get("risk", "微小"), 4))
        lines.append("\n🔍 變更明細：")
        for c in changes_sorted:
            risk = c.get("risk", "?")
            emoji = {"致命": "💀", "嚴重": "🔴", "普通": "🟡", "微小": "⚪"}.get(risk, "❓")
            lines.append(f"  {emoji} [{risk}] {c.get('path', '?')}")
            lines.append(f"       {c.get('old_value', '?')} → {c.get('new_value', '?')}")
            if c.get("impact"):
                lines.append(f"       影響：{c['impact']}")
            if c.get("action_needed"):
                lines.append(f"       行動：{c['action_needed']}")
    else:
        lines.append("\n✅ 無實質變更")

    return "\n".join(lines)
