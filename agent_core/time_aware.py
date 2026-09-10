"""Time-aware filter（R10）— 偵測 query 裡的時間詞，自動過濾 date 範圍。

Why:
  「這個月的 Blaklader 進度」不應該跟「2020 年的 Blaklader 進度」
  混在一起。但向量空間裡時間相似度幾乎無關，純向量 recall 會把
  舊信件排上來。需要**硬過濾** date range。

How:
  - 規則式偵測中英時間詞：今天/今日/本週/最近一週/上週/本月/上月/
    今年/去年/上季/去年同期、this week, last month, YTD ...
  - 回傳 (start_date, end_date) or None
  - recall_reranked 拿到 date range 後 pass 給 chroma query 的 where + BM25 filter
"""
import re
from datetime import date, datetime, timedelta
from typing import Optional


# (start_offset_days, end_offset_days) — 相對今天
_RELATIVE_TIME_RULES = [
    # 最常見、最具體
    (r"今天|今日|today",                          (0, 0)),
    (r"昨天|昨日|yesterday",                       (-1, -1)),
    (r"前天|day before yesterday",                (-2, -2)),
    (r"本週|這週|this week",                       (-7, 0)),
    (r"上週|上星期|last week",                     (-14, -7)),
    (r"最近一週|過去一週|past week",                 (-7, 0)),
    (r"最近兩週|過去兩週|past two weeks",             (-14, 0)),
    (r"本月|這個月|這一個月|this month",               ("this_month", 0)),
    (r"上個月|上月|last month",                    ("last_month", "last_month_end")),
    (r"最近一個月|過去一個月|past month|past 30 days", (-30, 0)),
    (r"最近三個月|過去三個月|本季|這季|past 3 months",    (-90, 0)),
    (r"上季|last quarter|past quarter",           (-180, -90)),
    (r"最近半年|過去半年|past 6 months|半年內",         (-180, 0)),
    (r"今年|本年|this year",                       ("ytd", 0)),
    (r"去年|last year",                           ("last_year", "last_year_end")),
    (r"前年|year before last",                    ("two_years_ago", "two_years_ago_end")),
    (r"最近一年|過去一年|past year|past 12 months",     (-365, 0)),
    (r"最近兩年|過去兩年|past two years",               (-730, 0)),
    (r"最近三年|過去三年|past three years",             (-1095, 0)),
    (r"最近五年|過去五年|past five years",              (-1825, 0)),
]


def detect_time_filter(query: str) -> Optional[tuple[str, str]]:
    """偵測 query 裡的時間詞，回傳 (start_date, end_date) YYYY-MM-DD 格式。
    偵測不到就回 None。

    Args:
        query: 使用者 query。
    Returns:
        ("2026-04-01", "2026-04-24") or None
    """
    if not query:
        return None
    q = query.lower()
    today = date.today()

    # 先檢查：query 裡有沒有明顯不是時間詞的產業 token（PO/LOT/料號/訂單號）。
    # 避免「FU CHUN JA260226-06」的「260226-06」被誤讀為年月。
    # 這類 query 幾乎不會同時夾帶時間意圖。
    non_time_signals = re.search(
        r"\b(PO|LOT|JF0P|JFH|JOM|FC\d|JA\d|L\d{4})\b|PO[#\s-]|訂單|料號|品號",
        query,
        re.IGNORECASE,
    )
    # 如果 query 含 PO / 料號訊號，**先跑 relative time 但不跑純數字 regex**
    for pattern, offsets in _RELATIVE_TIME_RULES:
        if re.search(pattern, q, re.IGNORECASE):
            return _resolve_offsets(offsets, today)

    # 純數字格式（YYYY 年 MM 月 / YYYY-MM / YYYY 年）都要驗年份在合理範圍（1990-2099）
    # 且 query 不能含 PO / 料號訊號（那些會有類似格式但不是日期）
    if non_time_signals:
        return None

    def _year_ok(y: int) -> bool:
        return 1990 <= y <= 2099

    # YYYY 年 MM 月 形式
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", query)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if _year_ok(y) and 1 <= mo <= 12:
            start = date(y, mo, 1)
            next_mo = date(y, mo + 1, 1) if mo < 12 else date(y + 1, 1, 1)
            end = next_mo - timedelta(days=1)
            return (start.isoformat(), end.isoformat())

    # YYYY-MM 純格式（要求前後是 word boundary，且不是 hyphen/dash 前後）
    # 例如「2025-03 的採購」OK、「JA260226-06」的「260226-06」不 OK（因為前面黏著更多數字）
    m = re.search(r"(?<!\d)(\d{4})[-/](\d{1,2})(?!\d)", query)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if _year_ok(y) and 1 <= mo <= 12:
            start = date(y, mo, 1)
            next_mo = date(y, mo + 1, 1) if mo < 12 else date(y + 1, 1, 1)
            end = next_mo - timedelta(days=1)
            return (start.isoformat(), end.isoformat())

    # YYYY 年
    m = re.search(r"(\d{4})\s*年(?!\s*\d)", query)
    if m:
        y = int(m.group(1))
        if _year_ok(y):
            return (f"{y}-01-01", f"{y}-12-31")

    return None


def _month_last_day(y: int, m: int) -> date:
    """Last calendar day of month (y, m)."""
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return nxt - timedelta(days=1)


def _resolve_offsets(offsets, today: date) -> tuple[str, str]:
    """把 rule 裡的 offset 解析成實際 (start, end) 日期字串。

    整數 = 相對今天的天數；字串 = 具名邊界。月份用「真正的日曆月」
    邊界（本月 1 號 / 上月最後一天），不是滾動 30 天，否則「上個月」
    會橫跨兩個月、漏掉大半個月的資料。
    """
    start_off, end_off = offsets

    # Previous calendar month (handles the January → prev-year-December wrap).
    if today.month == 1:
        prev_y, prev_m = today.year - 1, 12
    else:
        prev_y, prev_m = today.year, today.month - 1

    start_specials = {
        "ytd": date(today.year, 1, 1),
        "last_year": date(today.year - 1, 1, 1),
        "two_years_ago": date(today.year - 2, 1, 1),
        "this_month": date(today.year, today.month, 1),
        "last_month": date(prev_y, prev_m, 1),
    }
    end_specials = {
        "last_year_end": date(today.year - 1, 12, 31),
        "two_years_ago_end": date(today.year - 2, 12, 31),
        "this_month_end": _month_last_day(today.year, today.month),
        "last_month_end": _month_last_day(prev_y, prev_m),
    }

    start = start_specials[start_off] if isinstance(start_off, str) else today + timedelta(days=int(start_off))
    end = end_specials[end_off] if isinstance(end_off, str) else today + timedelta(days=int(end_off))
    return (start.isoformat(), end.isoformat())


def preview_time_detection(query: str) -> str:
    """給大王查 time detection 結果（debug 用）。"""
    r = detect_time_filter(query)
    if r is None:
        return f"🕐 '{query}' 沒偵測到時間詞；無 date filter"
    start, end = r
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return (
        f"🕐 '{query}'\n"
        f"  → 日期範圍: {start} ~ {end} ({days} 天)"
    )
