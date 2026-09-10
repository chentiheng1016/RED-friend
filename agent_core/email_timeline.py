"""Cross-department PO / customer timeline lookup over the email data lake.

Queries `data_lake_internal/emails.parquet` and stitches a chronological
timeline of how a PO (or a customer/product combo) flowed across the 6
departments: 樣品室 → 採購 → 倉庫 → 船務 → 會計（老闆 觀察線）.

Typical usage from 小紅:
    r = timeline_by_po("JF0P26040054")
    print(format_timeline(r))

All functions read parquet lazily; no writes, no side effects.
"""
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from agent_core.logging_and_paths import INTERNAL_LAKE_DIR, logger

_INTERNAL_PARQUET = os.path.join(INTERNAL_LAKE_DIR, "emails.parquet")

# 部門排序（timeline 的 "自然流程" 順序）
_DEPT_ORDER = ["樣品室", "生產管理", "採購", "倉庫", "船務", "會計", "老闆"]
_DEPT_RANK = {d: i for i, d in enumerate(_DEPT_ORDER)}


# ────────────────────────────────────────────────────────────────────
# parquet loader (cache 5 分鐘：daily_delta 跑完才需要重新讀)
# ────────────────────────────────────────────────────────────────────
_df_cache = {"df": None, "ts": 0.0, "mtime": 0.0}
_CACHE_TTL_SEC = 300


def _load_df():
    import pandas as pd
    import time as _t

    if not os.path.exists(_INTERNAL_PARQUET):
        raise FileNotFoundError(
            f"{_INTERNAL_PARQUET} 不存在 — 請先跑 ingest_internal_emails()"
        )

    mtime = os.path.getmtime(_INTERNAL_PARQUET)
    now = _t.time()
    cached = _df_cache["df"]
    if (cached is not None
            and (now - _df_cache["ts"]) < _CACHE_TTL_SEC
            and _df_cache["mtime"] == mtime):
        return cached

    df = pd.read_parquet(_INTERNAL_PARQUET)
    # 預解析 entities_json 成一欄物件：timeline_by_po / _customer / stale_active_pos
    # / overdue_promises 原本各自對整個內部 lake（~2 萬列）逐列 json.loads，
    # 改成每次載入（5 分快取內共用）只解析一次。
    if "entities_json" in df.columns:
        df["_entities"] = df["entities_json"].map(_safe_json_loads)
    _df_cache.update(df=df, ts=now, mtime=mtime)
    return df


# ────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────
def _safe_json_loads(raw) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _entities(row) -> dict:
    # _load_df 已把 entities_json 預解析成 _entities 欄，走快取免每列重 json.loads；
    # 若 row 不是來自 _load_df（如測試直接造）則退回即時解析。
    if hasattr(row, "get"):
        cached = row.get("_entities")
        if cached is not None:
            return cached
    return _safe_json_loads(row["entities_json"]) if "entities_json" in row else {}


def _po_list(row) -> list:
    return list(_entities(row).get("po_numbers") or [])


def _norm_po(s: str) -> str:
    """大小寫一致 + 去空白。"""
    return (s or "").strip().upper()


def _po_matches(target: str, candidate: str) -> bool:
    """target 是使用者查詢的 PO；candidate 是 parquet entities 裡抓到的 PO。
    完全等值（忽略大小寫/空白）才算命中。"""
    return _norm_po(target) == _norm_po(candidate)


def _body_has_po(target: str, text: str) -> bool:
    """subject/summary/raw_body_preview 的「全文搜」fallback。
    純數字 PO 要求 word boundary，避免電話號碼誤中；
    含字母的 PO 直接 case-insensitive substring 就夠準。"""
    if not text or not target:
        return False
    t = _norm_po(target)
    if t.isdigit():
        # 純數字：要求前後非數字
        return re.search(rf"(?<!\d){re.escape(t)}(?!\d)", text) is not None
    return t in text.upper()


def _event_key(row):
    """sort key：date → last_message_date → thread_id 穩定排序。"""
    d = row.get("date") or ""
    lm = row.get("last_message_date") or d
    return (d, lm, row.get("thread_id") or "")


def _row_to_event(row) -> dict:
    ents = _entities(row)
    return {
        "thread_id": row.get("thread_id") or "",
        "date": row.get("date") or "",
        "last_message_date": row.get("last_message_date") or "",
        "dept": row.get("primary_dept") or "",
        "direction": row.get("direction") or "",
        "sender": row.get("sender") or "",
        "subject": row.get("subject") or "",
        "summary": row.get("summary") or "",
        "state": row.get("state") or "",
        "message_count": int(row.get("message_count") or 0),
        "po_numbers": list(ents.get("po_numbers") or []),
        "customers": list(ents.get("customers") or []),
        "products": list(ents.get("products") or []),
        "people": list(ents.get("people") or []),
        "amounts": list(ents.get("amounts") or []),
        # 2026-06 起抽取才有這欄；舊列回 [] — 消費端要能吃空值
        "promised_dates": list(ents.get("promised_dates") or []),
    }


def _aggregate_entities(events: list) -> dict:
    """把一串 events 的 entities 壓扁成 unique-sorted-list，順便用 Counter 排熱度。"""
    agg = defaultdict(Counter)
    for e in events:
        for k in ("po_numbers", "customers", "products", "people", "amounts",
                  "promised_dates"):
            for v in e.get(k) or []:
                v = (v or "").strip()
                if v:
                    agg[k][v] += 1
    return {k: [x for x, _ in agg[k].most_common()] for k in agg}


def _date_range(events: list) -> tuple:
    dates = [e.get("date") for e in events if e.get("date")]
    if not dates:
        return ("", "")
    return (min(dates), max(dates))


def _departments_hit(events: list) -> list:
    """回 [(dept, count)]，依 _DEPT_ORDER 排，缺的 dept 不列。"""
    c = Counter(e["dept"] for e in events if e["dept"])
    return sorted(c.items(), key=lambda x: _DEPT_RANK.get(x[0], 99))


# ────────────────────────────────────────────────────────────────────
# public API
# ────────────────────────────────────────────────────────────────────
def timeline_by_po(po_number: str, fulltext_fallback: bool = True, limit: int = 200) -> dict:
    """查 PO 在 6 部門的時間軸。

    - po_number: 使用者輸入的 PO（大小寫不敏感）
    - fulltext_fallback: True 時，entities 沒抓到的 thread 也會在 subject/
      summary/raw_body_preview 全文找 — 抓 Gemini 漏掉的案子
    - limit: 最多回幾筆 event（依日期 asc）

    回 dict 結構見 format_timeline()。
    """
    po_in = (po_number or "").strip()
    if not po_in:
        return {"po": "", "total_hits": 0, "events": [], "error": "PO 為空"}

    df = _load_df()
    hits = []
    for _, row in df.iterrows():
        matched = False
        for cand in _po_list(row):
            if _po_matches(po_in, cand):
                matched = True
                break
        if not matched and fulltext_fallback:
            # 全文 fallback：subject + summary + raw_body_preview
            blob = "\n".join([
                row.get("subject") or "",
                row.get("summary") or "",
                row.get("raw_body_preview") or "",
            ])
            if _body_has_po(po_in, blob):
                matched = True
        if matched:
            hits.append(row)

    hits.sort(key=_event_key)
    events = [_row_to_event(r) for r in hits[:limit]]
    return {
        "po": po_in.upper(),
        "total_hits": len(hits),
        "shown": len(events),
        "departments_touched": _departments_hit(events),
        "date_range": _date_range(events),
        "entities": _aggregate_entities(events),
        "events": events,
    }


def timeline_by_customer(customer: str, days_back: int = 365,
                        product: str = None, limit: int = 200) -> dict:
    """查某客戶（可選再加 product）的時間軸。

    - customer: 客戶名（case-insensitive substring match entities.customers）
    - days_back: 只看最近 N 天（None = 全部）
    - product: 可選，再過濾 entities.products substring match
    - limit: 最多 event
    """
    cust = (customer or "").strip()
    if not cust:
        return {"customer": "", "total_hits": 0, "events": [], "error": "customer 為空"}
    prod = (product or "").strip() or None

    # R3 Entity canonicalization：自動展開客戶名的所有 alias
    # 使用者問 "Blaklader" → 也去抓 "BLAKLADER" / "AB Blåkläder" / "blaklader.com" 等 variants
    try:
        from agent_core.entity_resolver import resolve_entity
        resolved = resolve_entity(cust, entity_type="customers")
        cust_aliases_upper = [a.upper() for a in resolved.get("aliases", [cust])]
        resolved_canonical = resolved.get("canonical", cust)
    except Exception:
        cust_aliases_upper = [cust.upper()]
        resolved_canonical = cust

    prod_u = prod.upper() if prod else None

    cutoff = None
    if days_back:
        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    df = _load_df()
    hits = []
    for _, row in df.iterrows():
        if cutoff and (row.get("date") or "") < cutoff:
            continue
        ents = _entities(row)
        row_customers = [(c or "").upper() for c in (ents.get("customers") or [])]
        # alias-aware match：row 的 customer 欄位有任何一個 alias 就算命中。
        # 反向包含（rc in alias）要求 rc 至少 4 字 —— 抽取出的超短客戶名（'AB'、'CO'）
        # 幾乎必然是別家名字的子字串，雙向 substring 會交叉誤中別的客戶。
        cust_ok = any(
            any(alias in rc or (len(rc) >= 4 and rc in alias) for alias in cust_aliases_upper)
            for rc in row_customers
        )
        if not cust_ok:
            # fallback: subject + summary + body，任一 alias 命中
            blob = "\n".join([
                row.get("subject") or "",
                row.get("summary") or "",
                row.get("raw_body_preview") or "",
            ]).upper()
            cust_ok = any(alias in blob for alias in cust_aliases_upper)
        if not cust_ok:
            continue
        if prod_u:
            prod_ok = any(prod_u in (p or "").upper() for p in (ents.get("products") or []))
            if not prod_ok:
                blob = (row.get("subject") or "") + (row.get("summary") or "")
                prod_ok = prod_u in blob.upper()
            if not prod_ok:
                continue
        hits.append(row)

    hits.sort(key=_event_key)
    events = [_row_to_event(r) for r in hits[:limit]]
    return {
        "customer": cust,
        "canonical": resolved_canonical,
        "aliases_matched": cust_aliases_upper if len(cust_aliases_upper) > 1 else [],
        "product": prod or "",
        "days_back": days_back,
        "total_hits": len(hits),
        "shown": len(events),
        "departments_touched": _departments_hit(events),
        "date_range": _date_range(events),
        "entities": _aggregate_entities(events),
        "events": events,
    }


# ────────────────────────────────────────────────────────────────────
# pretty printing
# ────────────────────────────────────────────────────────────────────
def format_timeline(result: dict, max_events: int = 30) -> str:
    if not result or result.get("error"):
        return f"❌ {result.get('error','查無資料') if result else '查無資料'}"

    title = result.get("po") and f"📦 PO {result['po']}" \
        or result.get("customer") and (
            f"👤 {result['customer']}"
            + (f" / {result['product']}" if result.get("product") else "")
        ) \
        or "📋 Timeline"
    lines = [title]
    lines.append("=" * 60)
    total = result.get("total_hits", 0)
    dr = result.get("date_range") or ("", "")
    if total == 0:
        lines.append("（沒有任何 email 提到這個 PO / 客戶）")
        return "\n".join(lines)
    lines.append(f"共 {total} 封 email  |  {dr[0]} ~ {dr[1]}")
    depts = result.get("departments_touched") or []
    if depts:
        trace = " → ".join(f"{d}({c})" for d, c in depts)
        lines.append(f"部門軌跡：{trace}")
    # C4 補丁：customers/products/amounts/sender/subject/summary（及承諾交期短語）
    # 都是 LLM 從 email 抽出的實體（untrusted）— sanitize 後才給 LLM。
    from agent_core.prompt_injection import sanitize_for_llm
    ents = result.get("entities") or {}
    if ents.get("customers"):
        lines.append(f"客戶：{', '.join(sanitize_for_llm(c) for c in ents['customers'][:5])}")
    if ents.get("products"):
        lines.append(f"產品：{', '.join(sanitize_for_llm(p) for p in ents['products'][:5])}")
    if ents.get("amounts"):
        lines.append(f"金額：{', '.join(sanitize_for_llm(a) for a in ents['amounts'][:5])}")
    if ents.get("promised_dates"):
        promised = ", ".join(sanitize_for_llm(p[:30]) for p in ents["promised_dates"][:5])
        lines.append(f"⏰ 承諾交期：{promised}")
    lines.append("")
    lines.append("📍 Timeline:")
    events = result.get("events") or []
    for i, e in enumerate(events[:max_events], 1):
        dept = e.get("dept") or "(未分類)"
        arrow = "→" if e.get("direction") == "outbound" else ("←" if e.get("direction") == "inbound" else "·")
        sender = sanitize_for_llm(e.get("sender", "")[:40])
        subj = sanitize_for_llm(e.get("subject", "")[:70])
        lines.append(f"  {i:2d}. {e['date']}  [{dept}] {arrow} {sender}")
        lines.append(f"      {subj}")
        if e.get("summary"):
            lines.append(f"      📝 {sanitize_for_llm(e['summary'][:120])}")
        if e.get("promised_dates"):
            pd_ = ", ".join(sanitize_for_llm(p[:30]) for p in e["promised_dates"][:3])
            lines.append(f"      ⏰ {pd_}")
    if len(events) > max_events:
        lines.append(f"  ...（還有 {len(events) - max_events} 封未顯示）")
    return "\n".join(lines)


def list_pos_for_customer(customer: str, days_back: int = 365, top_n: int = 20) -> str:
    """給個客戶，列他最常出現的 PO numbers（給人工挑 PO 查 timeline 用）。"""
    r = timeline_by_customer(customer, days_back=days_back, limit=10000)
    pos = (r.get("entities") or {}).get("po_numbers") or []
    if not pos:
        return f"🔍 {customer} 最近 {days_back} 天沒抓到任何 PO number"
    lines = [f"🔍 {customer} 最近 {days_back} 天的 PO（依出現頻率）："]
    for p in pos[:top_n]:
        lines.append(f"  - {p}")
    return "\n".join(lines)


# 內部 lake 的 thread state（LLM 抽取）— 最新一串已標這些狀態的 PO
# 視為結案，不用催。含簡體變體（抽取模型偶爾輸出簡體）。
_PO_CLOSED_STATES = frozenset({"已完成", "已完成 ", "取消", "已取消", "已拒絕", "已拒绝"})


def stale_active_pos(stale_days: int = 7, active_window_days: int = 45,
                     min_threads: int = 2, top_n: int = 20) -> dict:
    """活躍 PO 斷訊雷達 — 「最近還有往來、但已超過 stale_days 沒新信」的 PO。

    「活躍」= 最近 active_window_days 內有任何信件往來；出貨結案的 PO
    沉寂超過視窗後自然掉出清單，不需人工除名。min_threads 濾掉只被
    提過一次的雜訊 PO 號（誤抓的料號 / 引用舊單）。另外：PO **最新**
    一串信的 state 已是 已完成/取消 → 視為結案直接跳過（剛出完貨的
    單在視窗內也不誤報）。
    """
    df = _load_df()
    now = datetime.now()

    last_by_po: dict[str, datetime] = {}
    last_state_by_po: dict[str, str] = {}
    threads_by_po: Counter = Counter()
    customers_by_po: dict[str, Counter] = defaultdict(Counter)

    for _, row in df.iterrows():
        pos = _po_list(row)
        if not pos:
            continue
        raw = str(row.get("last_message_date") or row.get("date") or "")[:10]
        try:
            d = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            continue
        state = str(row.get("state") or "").strip()
        customers = [c.strip() for c in (_entities(row).get("customers") or []) if c and c.strip()]
        for po in pos:
            po = (po or "").strip()
            if not po:
                continue
            threads_by_po[po] += 1
            if po not in last_by_po or d > last_by_po[po]:
                last_by_po[po] = d
                last_state_by_po[po] = state
            for c in customers:
                customers_by_po[po][c] += 1

    active_cut = now - timedelta(days=active_window_days)
    stale_cut = now - timedelta(days=stale_days)
    stale: list[dict] = []
    active_total = 0
    closed_skipped = 0
    for po, last in last_by_po.items():
        if last < active_cut:
            continue  # 沉寂超過活躍視窗 — 視為已結案，不追
        if last_state_by_po.get(po, "") in _PO_CLOSED_STATES:
            closed_skipped += 1
            continue  # 最新一串已標結案/取消 — 不用催
        active_total += 1
        if last >= stale_cut:
            continue  # 近期有往來 — 健康
        if threads_by_po[po] < min_threads:
            continue
        top_customer = customers_by_po[po].most_common(1)
        stale.append({
            "po": po,
            "last_date": last.strftime("%Y-%m-%d"),
            "days_quiet": (now - last).days,
            "threads": threads_by_po[po],
            "customer": top_customer[0][0] if top_customer else "",
        })
    stale.sort(key=lambda x: (-x["days_quiet"], x["po"]))
    return {
        "stale": stale[:top_n],
        "stale_total": len(stale),
        "active_total": active_total,
        "closed_skipped": closed_skipped,
        "stale_days": stale_days,
        "active_window_days": active_window_days,
    }


# ────────────────────────────────────────────────────────────────────
# Agent-facing tools (registered in tool_registry.BUILTIN_TOOLS)
# 這些函式會被 Gemini function-calling 叫到；docstring 就是給 LLM 看的
# 使用說明，signature 裡的 default value 可以省略參數。
# ────────────────────────────────────────────────────────────────────
def query_po_timeline(po_number: str, max_events: int = 30) -> str:
    """查某張 PO（訂單號）在公司六部門的完整時間軸。

    會掃 data_lake_internal/emails.parquet 找所有提到這張 PO 的 email，
    依日期排序並顯示 [樣品室/生產管理/採購/倉庫/船務/會計/老闆] 的流程軌跡。
    支援各種 PO 格式（JF0P...、LJF...、PRJ...、純數字如 71608 等）。

    Args:
        po_number: PO 編號或訂單號，例如 "JF0P26040054"、"71608"、"PRJ24100005"
        max_events: 最多顯示幾筆 email（依日期 asc），預設 30，超過會顯示「還有 N 封未顯示」

    Returns:
        人類可讀的 timeline 字串，包含部門軌跡、日期範圍、客戶、產品、金額、
        逐筆 email 的 [部門] 寄件者 主旨 摘要。
    """
    r = timeline_by_po(po_number)
    return format_timeline(r, max_events=max_events)


def query_customer_timeline(customer: str, days_back: int = 365,
                           product: str = "", max_events: int = 30) -> str:
    """查某客戶（可加產品過濾）在公司六部門的往來時間軸。

    預設看最近 365 天；要看全部歷史請明確傳 days_back=0 或很大的數字。
    會從 entities.customers 及 subject/summary 雙向匹配（case-insensitive）。

    Args:
        customer: 客戶名稱，例如 "Blaklader"、"Lurchi"、"PAX"。
        days_back: 只看最近 N 天；傳 0 或負數代表全部歷史（預設 365）。
        product: 可選，再依產品名過濾（substring match），例如 "工作鞋"、"SCA326"。
        max_events: 最多顯示幾筆 email，預設 30。

    Returns:
        人類可讀的 timeline，含部門分布、PO 清單、產品、金額、逐筆 email 摘要。
    """
    # 0 或負數視為「全部歷史」
    db = None if (days_back is None or days_back <= 0) else days_back
    r = timeline_by_customer(customer, days_back=db, product=product or None)
    return format_timeline(r, max_events=max_events)


def list_customer_pos(customer: str, days_back: int = 365, top_n: int = 20) -> str:
    """列某客戶最近 N 天出現過的 PO 清單（依頻率排序）。
    當你不知道要查哪張 PO 時，先用這個找候選。

    Args:
        customer: 客戶名稱
        days_back: 看最近 N 天，預設 365；傳 0 代表全部歷史
        top_n: 顯示前 N 個 PO

    Returns:
        PO 清單字串。
    """
    db = 365000 if (days_back is None or days_back <= 0) else days_back
    return list_pos_for_customer(customer, days_back=db, top_n=top_n)


def check_stale_pos(stale_days: int = 7, active_window_days: int = 45) -> str:
    """活躍 PO 斷訊雷達：列出「最近還有往來、但已超過 N 天沒新信件」的 PO。

    營運分析守則的 stale-data 警示 — 活躍訂單的證據過期就該跟催。
    「活躍」= 最近 active_window_days（預設 45 天）內有信件往來；出貨
    結案的 PO 沉寂超過視窗自然掉出，不會誤報。每日 sample_check daemon
    也會自動跑一次，有發現才推 Telegram。

    Args:
        stale_days: 斷訊門檻天數（預設 7）
        active_window_days: 活躍視窗天數（預設 45）

    Returns:
        人類可讀清單：每張斷訊 PO 的客戶、最後往來日、斷訊天數、串數；
        全部健康時回 ✅ 開頭的一行。
    """
    from agent_core.prompt_injection import sanitize_for_llm

    try:
        r = stale_active_pos(stale_days=int(stale_days),
                             active_window_days=int(active_window_days))
    except FileNotFoundError as e:
        return f"⚠️ internal email lake 還沒建：{e}"
    if not r["stale"]:
        return (f"✅ 活躍 PO（{r['active_total']} 張）最近 {int(stale_days)} 天"
                "內都有往來，沒有斷訊。")
    lines = [
        f"📡 {r['stale_total']} 張活躍 PO 超過 {int(stale_days)} 天沒有新往來"
        f"（活躍視窗 {int(active_window_days)} 天，活躍共 {r['active_total']} 張）："
    ]
    for s in r["stale"]:
        # PO 號與客戶名抽自 email — 進 LLM 前 sanitize（C4 慣例）
        po = sanitize_for_llm(s["po"][:40])
        cust = sanitize_for_llm(s["customer"][:40])
        cust_part = f"｜{cust}" if cust else ""
        lines.append(
            f"  - {po}{cust_part}：最後往來 {s['last_date']}"
            f"（斷訊 {s['days_quiet']} 天，{s['threads']} 串）"
        )
    lines.append("建議：query_po_timeline(po) 看最後證據，必要時向對口要最新狀態。")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 承諾交期解析 + 逾期雷達
# promised_dates 是 2026-06 起 EXTRACT_PROMPT 抽的短語（「5/20 出貨」
# 「ETD 8/27」「六月底出貨」「本週五」…）— 解析成具體日期才能算逾期。
# 解析原則：保守 — 模糊粒度取「最晚」合理日（週→週日、X月底→月末），
# 寧可晚報不誤報；解析不出就放棄該短語。
# ────────────────────────────────────────────────────────────────────
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_CN_NUMS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
_CN_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_FULL_DATE_RE = re.compile(r"(20\d{2})\s*[./年\-]\s*(\d{1,2})\s*[./月\-]\s*(\d{1,2})\s*日?")
_MD_RE = re.compile(r"(?<![\d./\-])(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?![\d./\-])")
_EN_MD_RE = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*"
    # (?!\d) — 防止把年份的頭兩位吃成日（"June 2026" ≠ June 20）
    r"(\d{1,2})(?!\d)(?:st|nd|rd|th)?\s*\.?\s*,?\s*(20\d{2})?", re.IGNORECASE)
_EN_DM_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"[a-z]*\.?\s*,?\s*(20\d{2})?", re.IGNORECASE)
_CN_MONTH_PART_RE = re.compile(r"(\d{1,2}|十[一二]?|[一二三四五六七八九])\s*月\s*(底|中|初)")
_WEEK_N_RE = re.compile(r"week\s*(\d{1,2})(?:st|nd|rd|th)?", re.IGNORECASE)
_REL_WEEK_RE = re.compile(r"(本|這|这|下)\s*[週周]\s*([一二三四五六日天]|初|內|内|末)?")


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _month_end(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year, 12, 31)
    return datetime(year, month + 1, 1) - timedelta(days=1)


def _closest_year(month: int, day: int, anchor: datetime) -> datetime | None:
    """月/日沒帶年 → 在 anchor 前後一年內挑「離 anchor 最近」的那個年份。
    既能把 12 月信裡的「1/15」推成明年，也保留近期已過的交期（算逾期用）。"""
    candidates = [
        d for y in (anchor.year - 1, anchor.year, anchor.year + 1)
        if (d := _safe_date(y, month, day)) is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - anchor).days))


def parse_promised_date(phrase: str, anchor: datetime) -> datetime | None:
    """把承諾交期短語解析成日期；anchor = 該封信的日期（相對詞的基準）。

    解析不出（「盡快」「隨新訂單」）回 None。短語含多個日期時取最晚
    （區間「7/1 ~ 12/31」的期限是尾端）。
    """
    text = (phrase or "").strip()
    if not text:
        return None
    candidates: list[datetime] = []

    # 1) 完整日期（含年）— 取走後從字串移除，避免 md regex 重複比對
    def _eat_full(m: re.Match) -> str:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            candidates.append(d)
        return " "
    remaining = _FULL_DATE_RE.sub(_eat_full, text)

    # 2) 英文月名（month-day 與 day-month 兩種語序）
    for m in _EN_MD_RE.finditer(remaining):
        month = _EN_MONTHS[m.group(1).lower()[:3]]
        day = int(m.group(2))
        year = m.group(3)
        d = _safe_date(int(year), month, day) if year else _closest_year(month, day, anchor)
        if d:
            candidates.append(d)
    for m in _EN_DM_RE.finditer(remaining):
        day = int(m.group(1))
        month = _EN_MONTHS[m.group(2).lower()[:3]]
        year = m.group(3)
        d = _safe_date(int(year), month, day) if year else _closest_year(month, day, anchor)
        if d:
            candidates.append(d)

    # 3) 數字 月/日（無年）
    for m in _MD_RE.finditer(remaining):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 12 and 1 <= b <= 31:
            d = _closest_year(a, b, anchor)
            if d:
                candidates.append(d)

    # 4) 中文「X月底/中/初」（含中文數字月，例「六月底出貨」）
    for m in _CN_MONTH_PART_RE.finditer(remaining):
        raw_month = m.group(1)
        month = int(raw_month) if raw_month.isdigit() else _CN_NUMS.get(raw_month, 0)
        if not 1 <= month <= 12:
            continue
        year_probe = _closest_year(month, 15, anchor)
        if year_probe is None:
            continue
        part = m.group(2)
        if part == "底":
            candidates.append(_month_end(year_probe.year, month))
        elif part == "中":
            candidates.append(datetime(year_probe.year, month, 15))
        else:  # 初
            candidates.append(datetime(year_probe.year, month, 5))

    # 5) "end of June" 類
    m = re.search(r"end\s+of\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*",
                  remaining, re.IGNORECASE)
    if m:
        month = _EN_MONTHS[m.group(1).lower()[:3]]
        probe = _closest_year(month, 15, anchor)
        if probe:
            candidates.append(_month_end(probe.year, month))

    # 6) week N（出貨排期常用 ISO 週；取該週週日 = 最晚）
    m = _WEEK_N_RE.search(remaining)
    if m:
        week = int(m.group(1))
        if 1 <= week <= 53:
            try:
                d = datetime.fromisocalendar(anchor.year, week, 7)
                # 跨年修正：離 anchor 太遠就試相鄰年
                alts = [d] + [
                    alt for y in (anchor.year - 1, anchor.year + 1)
                    if (alt := _try_isoweek(y, week)) is not None
                ]
                candidates.append(min(alts, key=lambda x: abs((x - anchor).days)))
            except ValueError:
                pass

    # 7) 相對詞（基準 = anchor）
    low = remaining.lower()
    if re.search(r"今日|今天|today", low):
        candidates.append(anchor)
    if re.search(r"明日|明天|tomorrow", low):
        candidates.append(anchor + timedelta(days=1))
    if re.search(r"後天|后天", remaining):
        candidates.append(anchor + timedelta(days=2))
    for m in _REL_WEEK_RE.finditer(remaining):
        offset_week = 1 if m.group(1) == "下" else 0
        monday = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=offset_week)
        tail = m.group(2) or ""
        if tail in _CN_WEEKDAYS:
            candidates.append(monday + timedelta(days=_CN_WEEKDAYS[tail]))
        elif tail == "初":
            candidates.append(monday)
        else:  # 內/末/空 → 該週週日（最晚）
            candidates.append(monday + timedelta(days=6))

    if not candidates:
        return None
    return max(candidates)


def _try_isoweek(year: int, week: int) -> datetime | None:
    try:
        return datetime.fromisocalendar(year, week, 7)
    except ValueError:
        return None


def overdue_promises(grace_days: int = 1, days_back: int = 60, top_n: int = 20) -> dict:
    """交期逾期雷達 — 還在進行中、但承諾交期已過的 threads。

    資料來源：promised_dates 抽取欄位（2026-06 起的新信才有）。
    只看最近 days_back 內有往來、state=進行中 的 threads；已完成/取消/
    僅參考 不掃。逾期 = 解析出的承諾日 < 今天 - grace_days。
    """
    df = _load_df()
    now = datetime.now()
    active_cut = now - timedelta(days=days_back)
    due_cut = now - timedelta(days=grace_days)

    overdue: list[dict] = []
    scanned = 0
    seen: set[tuple] = set()
    for _, row in df.iterrows():
        state = str(row.get("state") or "").strip()
        if state not in ("進行中", "进行中"):
            continue
        ents = _entities(row)
        promises = [p for p in (ents.get("promised_dates") or []) if p and str(p).strip()]
        if not promises:
            continue
        raw = str(row.get("last_message_date") or row.get("date") or "")[:10]
        try:
            anchor = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            continue
        if anchor < active_cut:
            continue
        scanned += 1
        pos = _po_list(row)
        po = pos[0].strip() if pos else ""
        customers = [c.strip() for c in (ents.get("customers") or []) if c and c.strip()]
        for phrase in promises:
            due = parse_promised_date(str(phrase), anchor)
            if due is None or due >= due_cut:
                continue
            key = (po or row.get("thread_id"), due.date())
            if key in seen:
                continue
            seen.add(key)
            overdue.append({
                "po": po,
                "customer": customers[0] if customers else "",
                "subject": str(row.get("subject") or ""),
                "promise": str(phrase),
                "due": due.strftime("%Y-%m-%d"),
                "days_overdue": (now - due).days,
                "last_date": raw,
                "thread_id": str(row.get("thread_id") or ""),
            })
    overdue.sort(key=lambda x: -x["days_overdue"])
    return {
        "overdue": overdue[:top_n],
        "overdue_total": len(overdue),
        "scanned_threads": scanned,
        "grace_days": grace_days,
        "days_back": days_back,
    }


def check_overdue_promises(grace_days: int = 1, days_back: int = 60) -> str:
    """交期逾期雷達：列出「信件裡承諾的交期已過、案子卻還在進行中」的項目。

    營運守則的 delay radar — 承諾日來自信件抽取欄位 promised_dates
    （2026-06 起的新信才有，舊信掃不到）。「5/20 出貨」「ETD 8/27」
    「六月底」這類短語會解析成具體日期後跟今天比。每日 sample_check
    daemon 也會自動跑，有發現才推 Telegram。

    Args:
        grace_days: 寬限天數（預設 1 — 昨天到期今天才算逾期）
        days_back: 只掃最近 N 天有往來的 threads（預設 60）

    Returns:
        逾期清單（PO/客戶/承諾原文/到期日/逾期天數）；沒有逾期回 ✅ 開頭。
    """
    from agent_core.prompt_injection import sanitize_for_llm

    try:
        r = overdue_promises(grace_days=int(grace_days), days_back=int(days_back))
    except FileNotFoundError as e:
        return f"⚠️ internal email lake 還沒建：{e}"
    if not r["overdue"]:
        return (f"✅ 最近 {int(days_back)} 天進行中且有承諾交期的 "
                f"{r['scanned_threads']} 串裡，沒有已逾期的承諾。")
    lines = [
        f"⏰ {r['overdue_total']} 筆承諾交期已過、案子仍進行中"
        f"（掃描 {r['scanned_threads']} 串，寬限 {int(grace_days)} 天）："
    ]
    for o in r["overdue"]:
        po = sanitize_for_llm(o["po"][:30]) if o["po"] else ""
        cust = sanitize_for_llm(o["customer"][:30]) if o["customer"] else ""
        head = "｜".join(x for x in (po, cust) if x) or sanitize_for_llm(o["subject"][:40])
        lines.append(
            f"  - {head}：承諾「{sanitize_for_llm(o['promise'][:40])}」"
            f"→ {o['due']} 到期，已逾 {o['days_overdue']} 天"
            f"（最後往來 {o['last_date']}）"
        )
    lines.append("建議：先 query_po_timeline / fetch_email_by_thread_id 確認有沒有後續，"
                 "確認真逾期再向對口跟催。")
    return "\n".join(lines)
