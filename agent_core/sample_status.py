"""查樣品室進度 — 從內部郵件 lake 抽樣品相關 thread 的『摘要時間軸』。

樣品室沒有像生管日報那種單一現行結構化進度表（找到的『樣品進度表』多 stale 到
2005–2020）；樣品狀態散在 3,200+ 封樣品室信裡，但每封內部 lake thread 已帶 LLM 摘要。
這支把符合客戶/樣品單號的 thread 按時間＋摘要整理出來，讓「信多但查不動」的樣品進度
可結構化查詢。樣品單**數量/材料明細**仍以 Drive『FY26/FY25 樣品需求』夾為準。
"""
import re

import pandas as pd

# 共用 email_timeline 的 mtime+TTL parquet 快取（免每次呼叫整檔重讀 2 萬列）。
from agent_core.email_timeline import _load_df as _load_lake_df
from agent_core.internal_emails_store import INTERNAL_PARQUET
from agent_core.prompt_injection import sanitize_for_llm

# 樣品相關主旨關鍵字（補 primary_dept 沒標到樣品室、但其實是樣品的 thread）。
_SAMPLE_KW = re.compile(r"樣品|sample|打樣|寄樣|SP-|sample order", re.IGNORECASE)


def _sample_timeline_from_df(df, query="", days=0, limit=20):
    """純函式：internal-lake DataFrame → 過濾+排序好的樣品 thread（可測，免讀檔）。

    樣品 thread = primary_dept 為「樣品室」或主旨含樣品關鍵字；再以 query（客戶/單號）
    對 主旨＋摘要＋品牌 過濾；days>0 只看近 N 天；依日期新→舊取前 limit 筆。
    """
    n = len(df)
    subj = df["subject"].fillna("") if "subject" in df.columns else pd.Series([""] * n, index=df.index)
    dept = df["primary_dept"].fillna("") if "primary_dept" in df.columns else pd.Series([""] * n, index=df.index)
    sm = df[(dept == "樣品室") | subj.str.contains(_SAMPLE_KW)].copy()
    if sm.empty:
        return sm
    q = (query or "").strip()
    if q:
        summ = sm["summary"].fillna("") if "summary" in sm.columns else ""
        brands = sm["brands"].fillna("").astype(str) if "brands" in sm.columns else ""
        hay = sm["subject"].fillna("") + " " + summ + " " + brands
        sm = sm[hay.str.contains(re.escape(q), case=False, regex=True)]
    sm["_d"] = pd.to_datetime(sm["date"], errors="coerce") if "date" in sm.columns else pd.NaT
    if days and int(days) > 0:
        sm = sm[sm["_d"] >= pd.Timestamp.now() - pd.Timedelta(days=int(days))]
    return sm.sort_values("_d", ascending=False).head(max(1, int(limit)))


def _format_timeline(sm, query=""):
    q = (query or "").strip()
    lines = [f"🧵 樣品室進度（內部郵件 lake，{'查:' + q if q else '最近全部'}，{len(sm)} 筆，新→舊）"]
    for _, r in sm.iterrows():
        d = str(r["_d"])[:10] if pd.notna(r.get("_d")) else "?"
        # subject/summary 來自寄件人主旨與 LLM 摘要（untrusted）— 給 LLM 前必過 sanitize_for_llm。
        lines.append(f"  [{d}] {sanitize_for_llm(str(r.get('subject', ''))[:52])}")
        summ = str(r.get("summary", "") or "").strip()
        if summ and summ.lower() != "nan":
            lines.append(f"       ↳ {sanitize_for_llm(summ[:110])}")
    lines.append("（狀態源自信件摘要；樣品單數量/材料明細見 Drive『FY26/FY25 樣品需求』夾）")
    return "\n".join(lines)


def read_sample_status(query: str = "", days: int = 0, limit: int = 20) -> str:
    """查樣品室進度/狀態 —— 樣品打樣、客戶確認、寄樣的**結構化時間軸**（從樣品室信抽）。

    查「JALAS/LURCHI 某樣品單進度」「最近打樣/寄樣到哪了」的**首選**工具：生管日報
    （read_production_progress_sheet）只有**量產**進度、沒有樣品；樣品狀態散在 3,200+ 封
    樣品室信，這支把符合條件者按時間＋摘要整理出來。樣品**數量/材料明細**以 Drive
    『FY26/FY25 樣品需求』夾為準。免 +確認。

    Args:
        query: 客戶名或樣品單號（如 'JALAS'、'LURCHI'、'5618'、'1155'）；留空=最近全部樣品動態。
        days:  只看近 N 天；0=不限（預設）。
        limit: 最多回幾筆 thread（預設 20）。

    Returns:
        依日期新→舊的樣品狀態時間軸（日期＋主旨＋摘要）。
    """
    try:
        df = _load_lake_df()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀內部郵件 lake 失敗（{INTERNAL_PARQUET}）：{type(exc).__name__}: {exc}"
    sm = _sample_timeline_from_df(df, query=query, days=days, limit=limit)
    if sm.empty:
        return (f"查無樣品室相關信件（query={(query or '').strip()!r}）。"
                "可放寬 query 或改查 Drive『FY26/FY25 樣品需求』夾。")
    return _format_timeline(sm, query=query)


# ────────────────────────────────────────────────────────────────────
# read_sample_bom — 樣品單號 → 信件進度 ＋ Drive 料表（逐料）
# ────────────────────────────────────────────────────────────────────
# 樣品室實際調料表（用量明細 / Price BOM / 匯總 / material list）；跟業務部門的型體
# 報價 BOM（RFQ/CBD，走 factory_bom.get_model_bom）是不同實體檔、不同數字（報價估 vs
# 實際做樣定案）。
_BOM_FILE_HINT = re.compile(r"bom|用量|明細|匯總|material\s*list|price|pricing", re.IGNORECASE)
_SHEET_EXTS = (".xlsx", ".xlsm", ".xls")


def _bom_join_key(sample_no: str) -> str:
    """樣品單號 → 最可靠的 join 鍵：抽最長的 ≥3 位數字串（'JALAS 5618'→'5618'）。

    信件主旨與 Drive 檔名都以數字單號為核心（'JALAS -5618'、'匯總 JA#5618'），用純
    數字搜比帶客戶名的全字串穩（後者易因連字號/空白/全形差異 miss）。無數字才退原字串。
    """
    m = re.search(r"\d{3,}", sample_no or "")
    return m.group(0) if m else (sample_no or "").strip()


def _pick_bom_files(files, max_files=2):
    """從 Drive 搜尋結果挑「料表類」試算表：BOM 關鍵字優先，各按修改日期新→舊。

    純函式（可測，免打 Drive）。files: list[{id,name,mimeType,modifiedTime}]
    （樣品單號搜出來的雜檔，含 .msg 信件原檔 / .pdf 發票 / 資料夾，要濾掉）。
    """
    sheets = [f for f in files
              if str(f.get("name", "")).lower().endswith(_SHEET_EXTS)]
    hinted = [f for f in sheets if _BOM_FILE_HINT.search(str(f.get("name", "")))]
    rest = [f for f in sheets if not _BOM_FILE_HINT.search(str(f.get("name", "")))]
    hinted.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    rest.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    return (hinted + rest)[:max(1, int(max_files))]


def _search_drive_by_name(keyword):
    """用檔名搜 Drive，回結構化 list[{id,name,mimeType,modifiedTime}]（所有 Shared Drive）。

    drive_ops.search_drive_files 只回格式化字串、不利程式挑檔，故這裡薄封裝同一個
    `name contains` 查詢拿結構化結果。
    """
    from agent_core.google_auth import get_service
    svc = get_service("drive", "v3")
    safe = keyword.replace("\\", "\\\\").replace("'", "\\'")
    return svc.files().list(
        q=f"name contains '{safe}' and trashed = false",
        pageSize=50,
        fields="files(id, name, mimeType, modifiedTime)",
        includeItemsFromAllDrives=True, supportsAllDrives=True, corpora="allDrives",
    ).execute().get("files", [])


def _is_header_row(m):
    """factory_bom._parse_jalas_bom 只認首個表頭；檔中段第二區塊（如 CONSUMABLES）的表頭
    會被當成一筆料。用欄位字面值認出這種表頭噪音行並濾掉。"""
    code = str(m.get("code", "")).strip().lower()
    part = str(m.get("part", "")).strip().lower()
    return code in ("material code", "code", "料號") or part in ("material description", "material desc")


def _format_bom_materials(mats):
    """factory_bom parser 的料表 → 乾淨逐料 ＋ 供應商彙總（仿 get_model_bom、避開原檔髒格）。"""
    from collections import Counter
    mats = [m for m in mats if not _is_header_row(m)]  # 濾掉第二區塊表頭被誤當料的噪音行
    sup_cnt = Counter(m["supplier"] for m in mats if m.get("supplier"))
    lines = [f"  📦 {len(mats)} 項料"]
    for m in mats[:40]:
        up = m.get("unit_price")
        lines.append(
            f"    {str(m.get('part', ''))[:24]:<24} 料號 {str(m.get('code', ''))[:14]:<14} "
            f"供應商 {str(m.get('supplier', ''))[:16]:<16} 用量 {m.get('usage')}"
            f"{('/' + m['unit']) if m.get('unit') else ''}"
            + (f" 單價 {up}" if up is not None else ""))
    if len(mats) > 40:
        lines.append(f"    …（還有 {len(mats) - 40} 項）")
    if sup_cnt:
        lines.append("    供應商：" + "、".join(f"{s}({n})" for s, n in sup_cnt.most_common(8)))
    return "\n".join(lines)


def read_sample_bom(sample_no: str, days: int = 0, max_files: int = 2, max_chars: int = 4000) -> str:
    """查一張開發樣／樣品單的「料表(BOM)＋信件進度」——樣品室視角一鍵彙整。

    把樣品單號（如 '5618'、'JALAS 5618'）一步串起來：① 樣品室信件狀態時間軸
    （同 read_sample_status）② Drive 上該單號的料表檔（用量明細 / Price BOM / 匯總），
    自動抽成逐料（料號·供應商·用量·單價）。**分流**：要型體的標準報價 BOM 用
    get_model_bom（型體 DJS…/JA…）；只要信件進度用 read_sample_status；要採購備料進度
    用 check_material_readiness。免 +確認。

    Args:
        sample_no: 樣品單號／型體號（如 '5618'、'JALAS 5618'、'1155'）。
        days:      信件時間軸只看近 N 天；0=不限（預設）。
        max_files: 最多抽幾個料表檔（預設 2）。
        max_chars: 無法結構化解析時，原文 fallback 的字數上限（預設 4000）。

    Returns:
        markdown — 信件時間軸 ＋ 命中料表檔 ＋ 逐料明細（或原文 fallback）。
    """
    sn = (sample_no or "").strip()
    if not sn:
        return "錯誤：sample_no 不能為空（給樣品單號，如 '5618'、'JALAS 5618'）。"
    key = _bom_join_key(sn)

    out = [f"🧾 樣品 BOM ＋ 進度彙整：{sn}", "", "【信件進度】",
           read_sample_status(query=key, days=days, limit=10), "", "【Drive 料表】"]

    try:
        files = _search_drive_by_name(key)
    except Exception as exc:  # noqa: BLE001
        out.append(f"⚠️ Drive 搜尋失敗：{type(exc).__name__}: {exc}")
        return "\n".join(out)
    picks = _pick_bom_files(files, max_files=max_files)
    if not picks:
        out.append(f"Drive 上以 '{key}' 搜不到料表 xlsx（可能料表在『FY26/FY25 樣品需求』"
                   "夾的子夾、檔名不含單號；改用 search_drive_files 找）。")
        return "\n".join(out)

    # 複用 factory_bom 的 JALAS BOM 解析器（樣品料表與 JALAS RFQ 同表頭：
    # Material Code / Vendor Name / Consumption）；抓不到料才退原文。
    from agent_core import factory_bom as _fb
    from agent_core.ingest.drive_search import read_drive_file
    for f in picks:
        fid, fname = f["id"], str(f.get("name", ""))
        out.append(f"— {fname}")
        try:
            mats = _fb._parse_jalas_bom(_fb._grab(fid))
        except Exception:  # noqa: BLE001
            mats = []
        out.append(_format_bom_materials(mats) if mats
                   else read_drive_file(fid, max_chars=max_chars))
        out.append("")
    return "\n".join(out).rstrip()
