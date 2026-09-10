"""通用內部郵件 lake 部門時間軸查詢 — 把 2.1萬 thread 的任一部門做成可查的狀態時間軸。

樣品室那支（read_sample_status）證明可行；這支泛化給其他「信多、卻無結構化查詢工具」
的部門（倉庫/會計/採購/船務/老闆/生產管理）。讀內部 lake、依 primary_dept（或 all_depts
含該部門）過濾、回新→舊摘要時間軸。數量/單據明細仍以對應 Drive 夾或業務確認信為準。
"""
import re

import pandas as pd

# 共用 email_timeline 的 mtime+TTL parquet 快取（免每次呼叫整檔重讀 2 萬列）。
from agent_core.email_timeline import _load_df as _load_lake_df
from agent_core.internal_emails_store import INTERNAL_PARQUET
from agent_core.prompt_injection import sanitize_for_llm

# 內部 lake 的有效部門（dept_rules 分類法；與外部 lake 的 email_classify 命名不同）。
INTERNAL_DEPTS = ["老闆", "業務", "樣品室", "生產管理", "採購", "船務", "會計", "倉庫"]


def _dept_timeline_from_df(df, dept, query="", days=0, limit=20):
    """純函式：依 primary_dept==dept（或 all_depts 含 dept）過濾 + query + days + 新→舊排序。"""
    n = len(df)
    pdept = df["primary_dept"].fillna("") if "primary_dept" in df.columns else pd.Series([""] * n, index=df.index)
    alld = df["all_depts"].fillna("").astype(str) if "all_depts" in df.columns else pd.Series([""] * n, index=df.index)
    sm = df[(pdept == dept) | alld.str.contains(re.escape(dept))].copy()
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


def _format_dept_timeline(sm, dept, query=""):
    q = (query or "").strip()
    lines = [f"🧵 {dept}（內部郵件 lake，{'查:' + q if q else '最近全部'}，{len(sm)} 筆，新→舊）"]
    for _, r in sm.iterrows():
        d = str(r["_d"])[:10] if pd.notna(r.get("_d")) else "?"
        # subject/summary 來自寄件人主旨與 LLM 摘要（untrusted）— 給 LLM 前必過 sanitize_for_llm。
        lines.append(f"  [{d}] {sanitize_for_llm(str(r.get('subject', ''))[:52])}")
        summ = str(r.get("summary", "") or "").strip()
        if summ and summ.lower() != "nan":
            lines.append(f"       ↳ {sanitize_for_llm(summ[:110])}")
    lines.append("（狀態源自信件摘要；數量/單據明細請查對應 Drive 夾或業務確認信）")
    return "\n".join(lines)


def read_dept_email_timeline(dept: str, query: str = "", days: int = 0, limit: int = 20) -> str:
    """查某部門的郵件狀態時間軸 —— 倉庫/會計/採購/船務/生產管理/老闆 等內部部門。

    把內部郵件 lake（2016–2026、2.1萬 thread）某部門的信，按時間＋摘要整理成可查的狀態
    時間軸。用於「倉庫最近進出庫/庫存」「會計付款/發票/匯款狀態」「採購到料/欠料」「船務
    出貨櫃況」等 —— 這些部門信很多卻沒有結構化工具。**分流**：樣品進度用 read_sample_status；
    客戶訂單量用 read_customer_order_pos；量產進度用 read_production_progress_sheet。免 +確認。

    Args:
        dept:  部門名，須為：老闆/業務/樣品室/生產管理/採購/船務/會計/倉庫 之一。
        query: 客戶/單號/關鍵字過濾（如 '庫存'、'付款'、'DECA'）；留空=該部門最近全部。
        days:  只看近 N 天；0=不限（預設）。
        limit: 最多回幾筆 thread（預設 20）。

    Returns:
        依日期新→舊的部門狀態時間軸（日期＋主旨＋摘要）。
    """
    d = (dept or "").strip()
    if d not in INTERNAL_DEPTS:
        return (f"dept 須為其中之一：{'/'.join(INTERNAL_DEPTS)}（樣品進度用 read_sample_status、"
                f"客戶訂單用 read_customer_order_pos）。收到：{d!r}")
    try:
        df = _load_lake_df()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀內部郵件 lake 失敗（{INTERNAL_PARQUET}）：{type(exc).__name__}: {exc}"
    sm = _dept_timeline_from_df(df, d, query=query, days=days, limit=limit)
    if sm.empty:
        return f"查無 {d} 相關信件（query={(query or '').strip()!r}）。可放寬 query。"
    return _format_dept_timeline(sm, d, query=query)
