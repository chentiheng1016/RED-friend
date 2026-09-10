"""型體 BOM（用料表）查詢 — 從業務部門的成本分析表抽料表，接生管型體。

兩個客戶的 BOM 來源（大王 2026-06-23 指認）：
- **DECATHLON**：業務部門/成本分析表(CBD)/decathlon CBD/，每型體一個 `.xlsm`，
  檔名帶 style code（如 `…336189-CBD…`），分頁 `BOM` 逐部位逐料（DSM 料號/供應商/用量）。
- **JALAS**：業務部門/報價單/jalas quotation/，每型體一個 RFQ（`RFQ_1055.xlsx`…），
  分頁 `Preliminary BOM`（料號/供應商/用量）。

**鍵＝生管型體**：`DJS336189`→DECATHLON style `336189`、`JA1055`→JALAS RFQ `1055`
（接 [[reference_production_schedule_tools]] 的生管型體）。⚠️ BOM 料號是 DSM/供應商料號，
跟倉庫『00 Stock Data』的飛越庫存編號(GL02/PS19.1)**不同套**，要對庫存還需一層料號對照；
但 BOM 帶『供應商』名，可直接接採購信查到貨進度。
"""
from __future__ import annotations

import io
import re
from typing import Any

_DECA_CBD_KW = "CBD"
_DSM_CODE_RE = re.compile(r"^\d")  # DECATHLON DSM 料號以數字開頭(98…)


def _model_to_style(model: str) -> tuple[str, str]:
    """生管型體 → (客戶, style_code)。目前支援 DECATHLON(DJS)/JALAS(JA)，其餘回 ('', '')。"""
    m = (model or "").strip().upper()
    mm = re.match(r"DJS0*(\d+)", m)
    if mm:
        return "DECATHLON", mm.group(1)
    mm = re.match(r"JA0*(\d+)", m)
    if mm:
        return "JALAS", mm.group(1)
    mm = re.match(r"((?:EG|EH|EK|FH)\d{4}V\d?)", m)
    if mm:
        return "LURCHI", mm.group(1)
    return "", ""


def _drive():
    from agent_core.google_auth import get_service
    return get_service("drive", "v3")


def _find_bom_file(customer: str, style: str) -> dict | None:
    """找該型體的 BOM 檔（DECATHLON 取最新版 CBD；JALAS 取 RFQ_<style>）。回 {id,name} 或 None。"""
    svc = _drive()

    def q(query, n=15):
        return svc.files().list(
            q=query, pageSize=n,
            fields="files(id,name,modifiedTime,mimeType)",
            includeItemsFromAllDrives=True, supportsAllDrives=True, corpora="allDrives",
        ).execute().get("files", [])

    if customer in ("DECATHLON", "LURCHI"):
        files = [f for f in q(f"name contains '{style}' and name contains '{_DECA_CBD_KW}' and trashed=false")
                 if f["name"].lower().endswith((".xlsm", ".xlsx", ".xls"))]
        files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
        return files[0] if files else None
    if customer == "JALAS":
        for query in (f"name contains 'RFQ_{style}' and trashed=false",
                      f"name contains 'RFQ-{style}' and trashed=false",
                      f"name contains 'RFQ' and name contains '{style}' and trashed=false"):
            files = [f for f in q(query) if f["name"].lower().endswith((".xlsx", ".xlsm", ".xls"))]
            if files:
                files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
                return files[0]
    return None


def _grab(file_id: str) -> bytes:
    return _drive().files().get_media(fileId=file_id, supportsAllDrives=True).execute()


def _resolve_from_schedule(model: str) -> tuple[str, str]:
    """非 DECA/JALAS/LURCHI 的型體 → (客戶, 商品號)，用生管日報的「客戶型體(cust_style)」欄。

    RICHTER 等客戶的 CBD 以商品號命名（如 5001-4691），而商品號只在生管日報；型體碼
    (FE2303V…) 不帶商品號。商品號 = cust_style 前兩段（`5001L-4691-7201` → `5001-4691`，
    去掉 L/Z 與顏色尾碼）。查無回 ('', '')。Drive/報表不可用時優雅回 ('', '')。"""
    m = (model or "").strip().upper()
    if not m:
        return "", ""
    try:
        from agent_core import production_schedule as ps
        schedule, _ = ps._load_schedule()
    except Exception:  # noqa: BLE001 — 報表/Drive 不可用 → 視為查無
        return "", ""
    core = m.split()[0]
    for s in schedule:
        smodel = str(s.get("model", "")).strip().upper()
        if smodel == m or (core and (smodel.split()[0] == core or smodel.startswith(core))):
            mm = re.match(r"(\d{4})[A-Z]?-(\d{4})", str(s.get("cust_style", "")).strip())
            if mm:
                return str(s.get("customer", "")).strip(), f"{mm.group(1)}-{mm.group(2)}"
    return "", ""


def _find_supremo_cbd_by_article(article: str) -> tuple[str, list[dict[str, Any]]]:
    """商品號 → 最新「能 parse 出料」的 Supremo CBD。回 (filename, materials)；找不到回 ('', [])。

    一個商品號可能有多個材質/色版 CBD，取最新可解者。只收 .xlsx/.xlsm（.xls 多為舊規格表
    非 BOM），且實際 parse 出料才算數——名稱相近但無 #size BOM 分頁的「規格表(Var.*)」會
    被 parse 成空而跳過，避免回傳非 BOM 檔。都解不出 → ('', [])（＝這形體沒做 CBD，正常）。"""
    svc = _drive()
    files = [f for f in svc.files().list(
        q=f"name contains '{article}' and trashed=false", pageSize=25,
        fields="files(id,name,modifiedTime)", includeItemsFromAllDrives=True,
        supportsAllDrives=True, corpora="allDrives").execute().get("files", [])
        if f["name"].lower().endswith((".xlsx", ".xlsm"))]
    files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    for f in files[:8]:
        try:
            mats = _parse_supremo_cbd(_grab(f["id"]))
        except Exception:  # noqa: BLE001 — 壞檔/非 zip(.xls 偽裝) → 跳過
            mats = []
        if mats:
            return f["name"], mats
    return "", []


def _parse_deca_bom(xlsx: bytes) -> list[dict[str, Any]]:
    """DECATHLON CBD 的 BOM 分頁：固定欄 [1]部位 [2]DSM料號 [4]供應商 [5]單位 [8]毛用量 [9]單價。
    真料列＝料號以數字開頭(98…)且有供應商（跳區段標題/加工費）。"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx), data_only=True, read_only=True)
    ws = wb["BOM"] if "BOM" in wb.sheetnames else wb.worksheets[0]
    mats: list[dict] = []
    for row in ws.iter_rows(values_only=True):
        r = list(row) + [None] * 12
        code = str(r[2] or "").strip()
        sup = str(r[4] or "").strip()
        if not _DSM_CODE_RE.match(code) or not sup:
            continue
        mats.append({"part": str(r[1] or "").strip(), "code": code, "supplier": sup,
                     "unit": str(r[5] or "").strip(), "usage": r[8], "unit_price": r[9]})
    wb.close()
    return mats


def _parse_jalas_bom(xlsx: bytes) -> list[dict[str, Any]]:
    """JALAS RFQ 的 Preliminary BOM 分頁：表頭含 'Material Code'+'Vendor'，依關鍵字定位欄。"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx), data_only=True, read_only=True)
    ws = next((wb[s] for s in wb.sheetnames if "BOM" in s.upper()), wb.worksheets[0])
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    hdr = next((i for i, r in enumerate(rows)
                if any("Material Code" in str(c) for c in r) and any("Vendor" in str(c) for c in r)), None)
    if hdr is None:
        wb.close()
        return []
    H = [str(c or "").strip() for c in rows[hdr]]

    def col(*kw):
        # 依關鍵字優先序找欄：先試第一個關鍵字，全找不到才退下一個
        # （避免 'Consumption' 比 'Total Consumption' 先命中淨用量欄）。
        for k in kw:
            for j, h in enumerate(H):
                if k in h:
                    return j
        return None

    ci = {"name": col("Material Descri"), "code": col("Material Code"),
          "sup": col("Vendor Name"), "usage": col("Total Consumpti", "Consumption"),
          "unit": col("Unit"), "price": col("Unit Price")}
    mats: list[dict] = []
    for r in rows[hdr + 1:]:
        def cell(key):
            j = ci[key]
            return r[j] if (j is not None and j < len(r)) else None
        code = str(cell("code") or "").strip()
        name = str(cell("name") or "").strip()
        if not code or not name:
            continue
        mats.append({"part": name, "code": code, "supplier": str(cell("sup") or "").strip(),
                     "unit": str(cell("unit") or "").strip(), "usage": cell("usage"),
                     "unit_price": cell("price")})
    wb.close()
    return mats


def _load_model_bom(model: str) -> tuple[str, str, str, list[dict]]:
    """型體 → (customer, style, source_filename, materials)。找不到回空 materials。
    DECA/JALAS/LURCHI 由型體碼直接定位；其餘客戶(RICHTER…)的 CBD 以商品號命名、
    型體碼不帶商品號 → 靠生管日報 cust_style 把型體換成商品號再找 CBD。"""
    customer, style = _model_to_style(model)
    if customer:
        f = _find_bom_file(customer, style)
        if not f:
            return customer, style, "", []
        xlsx = _grab(f["id"])
        if customer == "DECATHLON":
            mats = _parse_deca_bom(xlsx)
        elif customer == "LURCHI":
            mats = _parse_supremo_cbd(xlsx)
        else:
            mats = _parse_jalas_bom(xlsx)
        return customer, style, f["name"], mats
    # 其他客戶（RICHTER…）：型體碼→(客戶, 商品號)，CBD 以商品號命名、用 Supremo 模板解。
    # 不靠任何號碼推算（商品號±N 會跨到不同形體、抓錯鞋的料），只用該訂單自己的商品號。
    rc, article = _resolve_from_schedule(model)
    if not rc or not article:
        return "", "", "", []
    name, mats = _find_supremo_cbd_by_article(article)
    return rc, article, name, mats


def bom_probe(model: str) -> tuple[str, str, int]:
    """輕量探『此型體有沒有料表(CBD)』，給生產排程落後清單一鍵標示用。

    回 (status, source, n_materials)：status='yes' 有料表、'no' 沒做/找不到、
    'unsupported' 非支援客戶(BOM 在 ERP)。成本取捨：DECA/JALAS/LURCHI 只做 Drive 檔名
    搜尋（命名乾淨、不下載）；RICHTER 需下載解析確認（規格表 Var 檔與 CBD 同名、不解析
    無法分辨），故 n_materials 僅 RICHTER 命中才有值。查無/出錯一律優雅回（不丟例外）。"""
    try:
        customer, style = _model_to_style(model)
        if customer:
            f = _find_bom_file(customer, style)
            return ("yes", f["name"], 0) if f else ("no", "", 0)
        rc, article = _resolve_from_schedule(model)
        if not rc or not article:
            return ("unsupported", "", 0)
        name, mats = _find_supremo_cbd_by_article(article)
        return ("yes", name, len(mats)) if mats else ("no", "", 0)
    except Exception:  # noqa: BLE001 — 探針：任何失敗都當「測不到」，不擋落後查詢
        return ("unsupported", "", 0)


def get_model_bom(model: str) -> str:
    """查一個生管型體要用哪些原料（BOM 用料表）＋ 供應商。

    從業務部門的成本分析表抽：DECATHLON 走 CBD（型體 DJS336189…）、JALAS 走 RFQ
    報價單（型體 JA1055…）。被問「XX 型體要哪些料」「這雙鞋的 BOM」「某指令缺料要找
    誰」、或要追某個落後生產指令的料時呼叫。**支援 DECATHLON(DJS)/JALAS(JA)/LURCHI
    (EG·EH·EK·FH)/RICHTER(FE·DP)**。RICHTER 以商品號(查生管日報)對應 CBD，且**並非每個
    形體都做 CBD**，找不到屬正常（會明說）。

    model: 生管型體（如 `DJS336189-02`、`JA1055 BLACK`、`FE2303V-02`；顏色/後綴會自動去掉）

    回傳：markdown — 型體/來源檔/料數 + 逐料(部位·料號·供應商·用量·單價) + 供應商彙總。
    """
    try:
        customer, style, src, mats = _load_model_bom(model)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 BOM 失敗：{type(exc).__name__}: {exc}"
    if not customer:
        return (f"型體「{model}」目前沒有 BOM 來源。已支援 DECATHLON(型體 DJS…)、"
                "JALAS(型體 JA…)、LURCHI(型體 EG/EH/EK/FH…)、RICHTER(型體 FE/DP…，以商品號對應) "
                "的業務部門 CBD/報價單料表；其餘客戶的 BOM 仍在 ERP。")
    if not mats:
        if not src:
            return (f"型體「{model}」（{customer} 商品號 {style}）找不到對應的 CBD —— "
                    "並非每個形體都會做 CBD，這是正常情況（非錯誤）。")
        return f"找到來源檔 {src}，但抓不到料表（格式不符）。"

    from collections import Counter
    sup_cnt = Counter(m["supplier"] for m in mats if m["supplier"])
    lines = [f"📦 {customer} 型體 {style} BOM（來源：{src}）：{len(mats)} 項料"]
    for m in mats[:40]:
        up = m["unit_price"]
        lines.append(
            f"  {m['part'][:24]:<24} 料號 {m['code'][:14]:<14} 供應商 {m['supplier'][:16]:<16}"
            f" 用量 {m['usage']}{('/'+m['unit']) if m['unit'] else ''}"
            + (f" 單價 {up}" if up is not None else ""))
    if len(mats) > 40:
        lines.append(f"  …（還有 {len(mats) - 40} 項）")
    lines.append("\n供應商（要追採購/到貨時找這些）：")
    for sup, n in sup_cnt.most_common(10):
        lines.append(f"  {sup}：{n} 項料")
    return "\n".join(lines)


def _latest_procurement(supplier: str, days: int) -> str:
    """用供應商名去採購信(內部 lake 採購 dept)查最近一筆採購/到貨。
    試完整名 + 各 token（含中文）以容忍 BOM 與信件的名稱寫法差異。查到回
    '[日期] 主旨摘要'，查無回 ''。"""
    from agent_core.lake_dept_timeline import read_dept_email_timeline

    cands = [supplier] + [t for t in re.split(r"[\s,./()]+", supplier) if len(t) >= 2]
    seen: set[str] = set()
    for q in cands:
        if not q or q in seen:
            continue
        seen.add(q)
        try:
            out = read_dept_email_timeline("採購", query=q, days=days, limit=2)
        except Exception:
            continue
        if "查無" in out:
            continue
        ls = out.split("\n")
        dline = next((line.strip() for line in ls if line.strip().startswith("[")), "")
        summ = next((line.strip().lstrip("↳").strip() for line in ls if "↳" in line), "")
        if dline:
            return f"{dline[:48]} {summ[:40]}".strip()
    return ""


def check_material_readiness(model: str, days: int = 240, max_suppliers: int = 10) -> str:
    """查一個生管型體的「採購備料進度」：要哪些料、各供應商的採購/到貨到哪了。

    串起 BOM（get_model_bom 同源）＋採購信：型體 → BOM 供應商 → 去採購部信箱查每個
    供應商最近的採購單／到貨。被問「XX 型體的料備齊了沒」「某落後指令是不是卡缺料」
    「DECATHLON/JALAS 這雙鞋的材料採購進度」時呼叫。

    ⚠️ 支援 DECATHLON(DJS…)/JALAS(JA…)/LURCHI(EG·EH·EK·FH…)/RICHTER(FE·DP…) 有 BOM；
    LURCHI/RICHTER CBD 供應商欄較稀疏（多數料無供應商→只能查到有標的），RICHTER 並非每個
    形體都有 CBD。採購狀態來自採購信（非倉庫即時庫存——BOM 料號跟倉庫飛越庫存編號不同套）。
    供應商名對不上採購信的會標「人工確認」。

    model:         生管型體（DJS336189-02 / JA1055…，顏色後綴自動去）
    days:          採購信回溯天數（預設 240）
    max_suppliers: 最多查幾個供應商（依用料數排序，預設 10）

    回傳：markdown — 各供應商(用料數) + 最近一筆採購/到貨；對不上的列「人工確認」。
    """
    try:
        customer, style, src, mats = _load_model_bom(model)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 BOM 失敗：{type(exc).__name__}: {exc}"
    if not customer:
        return (f"型體「{model}」目前沒有 BOM 來源（已支援 DECATHLON DJS…、JALAS JA…、"
                "LURCHI EG/EH/EK/FH…、RICHTER FE/DP…；其餘在 ERP），無法查備料進度。")
    if not mats:
        if not src:
            return (f"型體「{model}」（{customer} 商品號 {style}）找不到對應的 CBD —— "
                    "並非每個形體都會做 CBD，無法查備料進度。")
        return f"找到來源檔 {src}，但抓不到料表，無法查備料進度。"

    from collections import Counter
    sup_cnt = Counter(m["supplier"] for m in mats if m["supplier"])
    lines = [f"📦 {customer} 型體 {style} 採購備料進度"
             f"（BOM {len(mats)} 項料、{len(sup_cnt)} 個供應商；狀態查採購信近 {days} 天）"]
    miss: list[str] = []
    for sup, n in sup_cnt.most_common(max_suppliers):
        lp = _latest_procurement(sup, days)
        if lp:
            lines.append(f"  ✅ {sup[:18]:<18}({n}料) {lp}")
        else:
            miss.append(f"{sup}({n})")
    if miss:
        lines.append(f"  ⚠️ 採購信查無、需人工確認（供應商名可能不同）：{'、'.join(miss)}")
    if len(sup_cnt) > max_suppliers:
        lines.append(f"  …（還有 {len(sup_cnt) - max_suppliers} 個供應商，未列）")
    return "\n".join(lines)


def _parse_supremo_cbd(xlsx: bytes) -> list[dict[str, Any]]:
    """Supremo 系（LURCHI/RICHTER）CBD：按鞋碼分頁、料以規格描述非料號、供應商欄稀疏；
    取首個 #size 分頁，表頭關鍵字定位欄(parts/material/supplier/pairs·sf/measurement)，
    (部位,料)去重、跳費列。LURCHI 與 RICHTER 同模板，故共用此 parser。"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx), data_only=True, read_only=True)
    sized = [n for n in wb.sheetnames if n.startswith("#")]
    ws = wb[sized[0]] if sized else wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    hdr = next((i for i, r in enumerate(rows)
                if any("parts" in str(c).lower() for c in r)
                and any("supplier" in str(c).lower() for c in r)), None)
    if hdr is None:
        wb.close()
        return []
    H = [str(c or "").strip().lower() for c in rows[hdr]]

    def col(*kw):
        return next((j for k in kw for j, h in enumerate(H) if k in h), None)
    ci = {"part": col("parts"), "mat": col("material"), "sup": col("supplier"),
          "usage": col("pairs/sf", "pairs"), "unit": col("measurement"), "price": col("unit price")}

    def cell(r, key):
        j = ci[key]
        return r[j] if (j is not None and j < len(r)) else None
    mats: list[dict] = []
    cur = ""
    seen: set = set()
    for r in rows[hdr + 1:]:
        p = str(cell(r, "part") or "").strip()
        if p:
            cur = p
        mat = str(cell(r, "mat") or "").strip()
        usage = cell(r, "usage")
        if not mat or not isinstance(usage, (int, float)) or "費" in mat or "加工" in mat:
            continue
        if (cur, mat) in seen:
            continue
        seen.add((cur, mat))
        mats.append({"part": f"{cur}: {mat}" if cur else mat, "code": "",
                     "supplier": str(cell(r, "sup") or "").strip(),
                     "unit": str(cell(r, "unit") or "").strip(), "usage": usage,
                     "unit_price": cell(r, "price")})
    wb.close()
    return mats
