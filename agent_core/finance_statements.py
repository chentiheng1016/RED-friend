"""福群（越南廠）月損益 —— 財務長面的確定性報表（Phase 1）。

資料源：ERP 本地鏡像（var/data/erp_mirror/erp_full.duckdb）的 GL00 總帳。
帳套：GL_BOOK 兩套 —— CJ 佳桀（NTD，只設到 2024 年底、零傳票）、FC 福群2025
（VND，2025-04 起）。活帳套用「傳票最多」判定，不寫死 FC：哪天台灣帳套啟用，
工具自動看得到。

口徑（2026-09-07 實資料驗證過）：

- 所有損益數字走 GL_VOUCH_M/D 傳票直加總，**排除 VCH_TYPE='99'** 的月結轉
  「Income summary」傳票 —— 結轉會把 4~9 類科目歸零，不排除損益永遠是 0。
- **也要排除 STATUS='0' 的作廢傳票**（2026-09-07 實測：5 張作廢單其中兩張碰
  5 類成本科目，不排除 202505 成本多算 94.4M）。STATUS 語意：0=作廢、1=未過
  帳（開放月草稿）、7=已過帳。草稿**不**排除 —— 開放月本來就掛「未定稿」警語。
  排除作廢後傳票累加 == ERP 自己的 GL_BALANCES_M 期末（1101/1102/1103 三組
  逐一驗證過）。
- 借方金額在 D_MONEY、貸方在 C_MONEY，**另一側是 NULL**（不是 0），加總一定要
  COALESCE；金額是 VARCHAR，用 TRY_CAST 到 DECIMAL(20,2)（VND 十億級，float
  會掉精度）。
- 科目大類靠 GL_ACCT_M.ACCT_TYPE：4 收入 / 5 成本 / 6 費用 / 7 營業外收入 /
  8 營業外費用 / 9 所得稅。科目名 NAME_T 是繁中（NAME_S 是越文）。
- 「關帳」判準＝該期存在 VCH_TYPE='99' 結轉傳票。未關帳月數字未定稿，預設不報。
- 已對過 ERP 自己的 GL_BALANCES_M（202604 收入兩邊同為 10,961M VND）。

零幻覺原則：全部確定性 SQL，LLM 只拿到算好的文字照唸。財務全貌屬敏感資料，
這批工具**不進** dept_tool_scope._HOME_TOOLS（員工面白名單），守門測試釘死。
"""
from __future__ import annotations

import json
import os
import unicodedata

_STATE_BASENAME = "finance_pnl_push.json"

# 損益科目大類：貸方為正（收入 +、成本費用自然為負）。
_PNL_TYPES = ("4", "5", "6", "7", "8", "9")
_TYPE_LABELS = {
    "4": "營業收入", "5": "營業成本", "6": "營業費用",
    "7": "營業外收入", "8": "營業外費用", "9": "所得稅",
}


# ────────────────────────────────────────────────────────────────────
# 鏡像存取（複用 erp_stock_query 的連線/逾時/淨化慣例）
# ────────────────────────────────────────────────────────────────────

def _esq():
    from agent_core import erp_stock_query as esq
    return esq


def _guard() -> str:
    esq = _esq()
    if not esq._db_ready():
        return "⚠️ ERP 本地鏡像尚未建立（var/data/erp_mirror/erp_full.duckdb 不存在）。"
    return ""


def _run(sql: str, params: list) -> list:
    return _esq()._run(sql, params)


def _clean(v) -> str:
    return _esq()._clean(v)


# ────────────────────────────────────────────────────────────────────
# 基礎查詢
# ────────────────────────────────────────────────────────────────────

def _active_book() -> tuple[str, str, str]:
    """回 (帳套代碼, 帳套名, 幣別)。取傳票最多的帳套；查無傳票時 raise。"""
    rows = _run(
        "SELECT b.BOOKS_NO, COALESCE(NULLIF(b.DESC_T, ''), b.BOOKS_NO), "
        "       COALESCE(b.MONEY_UNIT, '?') "
        "FROM GL00__GL_BOOK b "
        "JOIN (SELECT BOOKS_NO, COUNT(*) c FROM GL00__GL_VOUCH_M GROUP BY 1) v "
        "  ON v.BOOKS_NO = b.BOOKS_NO "
        "ORDER BY v.c DESC LIMIT 1", [])
    if not rows:
        raise RuntimeError("GL 總帳沒有任何傳票（GL_VOUCH_M 空）——無法算損益。")
    no, name, cur = rows[0]
    return _clean(no), _clean(name), _clean(cur)


def closed_periods(book: str) -> list[str]:
    """已關帳期間（存在月結轉傳票的 PERIOD_ID，升冪）。"""
    rows = _run(
        "SELECT DISTINCT PERIOD_ID FROM GL00__GL_VOUCH_M "
        "WHERE BOOKS_NO = ? AND VCH_TYPE = '99' ORDER BY 1", [book])
    return [_clean(r[0]) for r in rows if _clean(r[0])]


_AMT = ("COALESCE(TRY_CAST(d.C_MONEY AS DECIMAL(20,2)), 0) "
        "- COALESCE(TRY_CAST(d.D_MONEY AS DECIMAL(20,2)), 0)")


def _type_nets(book: str, period: str) -> dict[str, float]:
    """該期各損益大類的淨額（貸方為正），單位＝帳套原幣。"""
    rows = _run(
        f"SELECT a.ACCT_TYPE, SUM({_AMT}) "
        "FROM GL00__GL_VOUCH_D d "
        "JOIN GL00__GL_VOUCH_M m ON m.BOOKS_NO = d.BOOKS_NO AND m.VOUCH_ID = d.VOUCH_ID "
        "JOIN GL00__GL_ACCT_M a ON a.ACCT_ID = d.ACCT_ID "
        "WHERE m.BOOKS_NO = ? AND m.PERIOD_ID = ? AND m.VCH_TYPE <> '99' "
        "  AND m.STATUS <> '0' "
        "  AND a.ACCT_TYPE IN ('4','5','6','7','8','9') "
        "GROUP BY 1", [book, period])
    return {str(t): float(v or 0) for t, v in rows}


def _group_nets(book: str, period: str, types: tuple[str, ...]) -> dict[str, float]:
    """該期損益科目按 4 碼群組（level-2，如 6201 薪資）的淨額（貸方為正）。"""
    marks = ",".join("?" for _ in types)
    rows = _run(
        f"SELECT SUBSTR(d.ACCT_ID, 1, 4), SUM({_AMT}) "
        "FROM GL00__GL_VOUCH_D d "
        "JOIN GL00__GL_VOUCH_M m ON m.BOOKS_NO = d.BOOKS_NO AND m.VOUCH_ID = d.VOUCH_ID "
        "JOIN GL00__GL_ACCT_M a ON a.ACCT_ID = d.ACCT_ID "
        f"WHERE m.BOOKS_NO = ? AND m.PERIOD_ID = ? AND m.VCH_TYPE <> '99' "
        f"  AND m.STATUS <> '0' AND a.ACCT_TYPE IN ({marks}) "
        "GROUP BY 1", [book, period, *types])
    return {_clean(g): float(v or 0) for g, v in rows}


def _group_names(groups: list[str]) -> dict[str, str]:
    if not groups:
        return {}
    marks = ",".join("?" for _ in groups)
    rows = _run(
        f"SELECT ACCT_ID, COALESCE(NULLIF(NAME_T, ''), NULLIF(NAME_S, ''), ACCT_ID) "
        f"FROM GL00__GL_ACCT_M WHERE ACCT_ID IN ({marks})", list(groups))
    return {_clean(a): _clean(n) for a, n in rows}


def _usd_rate(book: str, period: str) -> float | None:
    """該期（或之前最近一期）的 USD 匯率（原幣/USD）；查無回 None。"""
    rows = _run(
        "SELECT TRY_CAST(LAST_RATE AS DECIMAL(18,4)) FROM GL00__GL_EXCHANGE "
        "WHERE BOOKS_NO = ? AND FORN_CURR = 'USD' AND PERIOD_ID <= ? "
        "ORDER BY PERIOD_ID DESC LIMIT 1", [book, period])
    if rows and rows[0][0]:
        rate = float(rows[0][0])
        return rate if rate > 0 else None
    return None


def _open_periods_note(book: str, after: str) -> str:
    rows = _run(
        "SELECT PERIOD_ID, COUNT(*) FROM GL00__GL_VOUCH_M "
        "WHERE BOOKS_NO = ? AND VCH_TYPE <> '99' AND STATUS <> '0' "
        "AND PERIOD_ID > ? GROUP BY 1 ORDER BY 1", [book, after])
    if not rows:
        return ""
    parts = [f"{_fmt_period(_clean(p))}（已入 {int(c)} 張傳票）" for p, c in rows]
    return "📌 尚未關帳：" + "、".join(parts) + " —— 數字未定稿，關帳後才列入報表。"


# ────────────────────────────────────────────────────────────────────
# 期間/格式 helpers
# ────────────────────────────────────────────────────────────────────

def _norm_period(period: str) -> str:
    """'2026-04' / '202604' / '2026/4' → '202604'；解不出回空字串。"""
    digits = "".join(ch for ch in str(period or "") if ch.isdigit())
    if len(digits) == 6:
        return digits
    if len(digits) == 5:  # 2026/4 → 20264
        return digits[:4] + "0" + digits[4]
    return ""


def _fmt_period(p: str) -> str:
    return f"{p[:4]}-{p[4:]}" if len(p) == 6 else p


def _prev_period(p: str) -> str:
    y, m = int(p[:4]), int(p[4:])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}{m:02d}"


def _yoy_period(p: str) -> str:
    return f"{int(p[:4]) - 1:04d}{p[4:]}"


def _fmt_m(v: float) -> str:
    """原幣 → 百萬、千分位、一位小數。"""
    return f"{v / 1e6:,.1f}"


def _pct(part: float, whole: float) -> str:
    if not whole:
        return "—"
    return f"{part / whole * 100:.1f}%"


def _delta_pct(cur: float, prev: float) -> str:
    if not prev:
        return "—"
    d = (cur - prev) / abs(prev) * 100
    return f"{d:+.1f}%"


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def _pad(s: str, width: int, right: bool = False) -> str:
    fill = " " * max(0, width - _w(s))
    return fill + s if right else s + fill


# ────────────────────────────────────────────────────────────────────
# 損益計算（結構化，可測）
# ────────────────────────────────────────────────────────────────────

def compute_pnl(book: str, period: str) -> dict[str, float]:
    """單期損益：全部欄位取正向讀數（成本/費用/稅為正的支出額）。"""
    nets = _type_nets(book, period)
    revenue = nets.get("4", 0.0)
    cogs = -nets.get("5", 0.0)
    opex = -nets.get("6", 0.0)
    nonop_in = nets.get("7", 0.0)
    nonop_out = -nets.get("8", 0.0)
    tax = -nets.get("9", 0.0)
    gross = revenue - cogs
    operating = gross - opex
    pretax = operating + nonop_in - nonop_out
    return {
        "revenue": revenue, "cogs": cogs, "gross": gross, "opex": opex,
        "operating": operating, "nonop_in": nonop_in, "nonop_out": nonop_out,
        "pretax": pretax, "tax": tax, "net": pretax - tax,
    }


def _resolve_period(book: str, period: str) -> tuple[str, str, str]:
    """回 (period, warn, err)。空 period → 最新關帳期；未關帳期照算但掛警語。"""
    closed = closed_periods(book)
    if not period:
        if not closed:
            return "", "", "⚠️ GL 總帳還沒有任何關帳期（無月結轉傳票），無法出正式月損益。"
        return closed[-1], "", ""
    p = _norm_period(period)
    if not p:
        return "", "", f"⚠️ 期間格式看不懂：{period!r}（要 YYYYMM 或 YYYY-MM，如 2026-04）。"
    if p in closed:
        return p, "", ""
    has = _run(
        "SELECT COUNT(*) FROM GL00__GL_VOUCH_M "
        "WHERE BOOKS_NO = ? AND PERIOD_ID = ? AND VCH_TYPE <> '99' "
        "AND STATUS <> '0'", [book, p])
    if not has or not int(has[0][0]):
        return "", "", f"⚠️ {_fmt_period(p)} 查無任何傳票 —— 該期不存在或會計尚未入帳。"
    return p, f"⚠️ {_fmt_period(p)} **尚未關帳**，以下數字未定稿、會計補入傳票後會變。", ""


# ────────────────────────────────────────────────────────────────────
# 對外工具面（回文字，LLM 照唸）
# ────────────────────────────────────────────────────────────────────

_LINE_LABELS = [
    ("revenue", "營業收入"), ("cogs", "營業成本"), ("gross", "毛利"),
    ("opex", "營業費用"), ("operating", "營業利益"), ("nonop_in", "營業外收入"),
    ("nonop_out", "營業外費用"), ("pretax", "稅前淨利"), ("tax", "所得稅"),
    ("net", "稅後淨利"),
]
_RATIO_OF = {"gross": "毛利率", "operating": "營益率", "net": "淨利率"}


def income_statement(period: str = "") -> str:
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001 —— 鏡像缺 GL 表等一律講清楚
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    p, warn, err = _resolve_period(book, period)
    if err:
        return err
    closed = closed_periods(book)

    cur = compute_pnl(book, p)
    prev_p = _prev_period(p)
    prev = compute_pnl(book, prev_p)
    has_prev = any(prev.values())
    yoy_p = _yoy_period(p)
    yoy = compute_pnl(book, yoy_p)
    has_yoy = any(yoy.values())

    lines = [f"📊 {book_name}（帳套 {book}）{_fmt_period(p)} 月損益"
             f"　單位：百萬 {currency}"]
    if warn:
        lines.append(warn)
    lines.append("```")
    for key, label in _LINE_LABELS:
        row = _pad(label, 12) + _pad(_fmt_m(cur[key]), 12, right=True)
        extras = []
        if key in _RATIO_OF and cur["revenue"]:
            extras.append(f"{_RATIO_OF[key]} {_pct(cur[key], cur['revenue'])}")
        if has_prev:
            extras.append(f"上月{_delta_pct(cur[key], prev[key])}")
        if extras:
            row += "  " + "、".join(extras)
        lines.append(row)
    lines.append("```")

    if has_yoy:
        lines.append(
            f"📅 去年同月（{_fmt_period(yoy_p)}）：營收 {_fmt_m(yoy['revenue'])} → "
            f"{_delta_pct(cur['revenue'], yoy['revenue'])}、"
            f"稅後淨利 {_fmt_m(yoy['net'])} → {_delta_pct(cur['net'], yoy['net'])}")

    if has_prev:
        moved = _top_movers(book, p, prev_p, top=5)
        if moved:
            lines.append(f"🔎 成本/費用與上月（{_fmt_period(prev_p)}）變化最大：")
            lines.extend(moved)

    rate = _usd_rate(book, p)
    if rate:
        lines.append(
            f"💵 美金參考（匯率 {rate:,.0f} {currency}/USD）：營收 ≈ "
            f"{cur['revenue'] / rate:,.0f}、稅後淨利 ≈ {cur['net'] / rate:,.0f} USD")

    note = _open_periods_note(book, closed[-1] if closed else p)
    if note:
        lines.append(note)
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(ln for ln in lines if ln)


def _top_movers(book: str, p: str, prev_p: str, top: int = 5) -> list[str]:
    """成本+費用（5/6 類）4 碼群組的 MoM 絕對變化 Top N（支出為正向讀數）。"""
    cur_g = {g: -v for g, v in _group_nets(book, p, ("5", "6")).items()}
    prev_g = {g: -v for g, v in _group_nets(book, prev_p, ("5", "6")).items()}
    all_g = set(cur_g) | set(prev_g)
    ranked = sorted(all_g, key=lambda g: abs(cur_g.get(g, 0) - prev_g.get(g, 0)),
                    reverse=True)[:max(1, top)]
    names = _group_names(ranked)
    out = []
    for g in ranked:
        c, pv = cur_g.get(g, 0.0), prev_g.get(g, 0.0)
        delta = c - pv
        if abs(delta) < 1:  # 原幣 < 1 元的浮動不值得列
            continue
        sign = "+" if delta >= 0 else ""
        out.append(f"- {g} {names.get(g, g)}：{sign}{_fmt_m(delta)}"
                   f"（{_fmt_m(pv)} → {_fmt_m(c)}）")
    return out


def profit_trend(months: int = 6) -> str:
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    closed = closed_periods(book)
    if not closed:
        return "⚠️ GL 總帳還沒有任何關帳期，無趨勢可看。"
    months = max(1, min(int(months or 6), 24))
    picked = closed[-months:]

    header = ["期間", "營收", "毛利", "毛利率", "營業利益", "稅後淨利"]
    widths = [8, 10, 10, 7, 10, 10]
    lines = [f"📈 {book_name} 近 {len(picked)} 個關帳月　單位：百萬 {currency}", "```",
             "".join(_pad(h, w + 2) for h, w in zip(header, widths)).rstrip()]
    for p in picked:
        r = compute_pnl(book, p)
        cells = [_fmt_period(p), _fmt_m(r["revenue"]), _fmt_m(r["gross"]),
                 _pct(r["gross"], r["revenue"]), _fmt_m(r["operating"]),
                 _fmt_m(r["net"])]
        lines.append("".join(_pad(c, w + 2, right=(i > 0)) for i, (c, w)
                             in enumerate(zip(cells, widths))).rstrip())
    lines.append("```")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(ln for ln in lines if ln)


def expense_breakdown(period: str = "", top: int = 12) -> str:
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    p, warn, err = _resolve_period(book, period)
    if err:
        return err
    top = max(3, min(int(top or 12), 40))
    prev_p = _prev_period(p)

    lines = [f"🧾 {book_name} {_fmt_period(p)} 成本/費用結構　單位：百萬 {currency}"]
    if warn:
        lines.append(warn)
    for t, title in (("5", "營業成本"), ("6", "營業費用")):
        cur_g = {g: -v for g, v in _group_nets(book, p, (t,)).items()}
        prev_g = {g: -v for g, v in _group_nets(book, prev_p, (t,)).items()}
        total = sum(cur_g.values())
        lines.append(f"◾ {title} 合計 {_fmt_m(total)}")
        if not cur_g:
            continue
        names = _group_names(sorted(cur_g))
        ranked = sorted(cur_g, key=lambda g: cur_g[g], reverse=True)[:top]
        lines.append("```")
        for g in ranked:
            c = cur_g[g]
            row = (_pad(f"{g} {names.get(g, g)}", 26) + _pad(_fmt_m(c), 11, right=True)
                   + _pad(_pct(c, total), 8, right=True))
            pv = prev_g.get(g)
            if pv is not None:
                row += _pad(f"上月{_delta_pct(c, pv)}", 12, right=True)
            lines.append(row.rstrip())
        lines.append("```")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(ln for ln in lines if ln)


# ────────────────────────────────────────────────────────────────────
# 每月自動推播（dispatcher 的 deterministic_tool，零 LLM）
# ────────────────────────────────────────────────────────────────────

def _push_state_path() -> str:
    from agent_core.logging_and_paths import STATE_DIR
    return os.path.join(STATE_DIR, _STATE_BASENAME)


def income_statement_autopush() -> str:
    """每日排程檢查：出現「新關帳月」才回完整月損益，否則回 (無新發現)。

    基礎設施壞掉（鏡像不存在 / GL 表讀不到）用 raise —— dispatcher 會記
    last_error 進 red-status，而不是天天往 Telegram 推同一則警告。
    """
    guard = _guard()
    if guard:
        raise RuntimeError(guard)
    book, _, _ = _active_book()  # 讀不到 GL 表就讓它 raise
    closed = closed_periods(book)
    if not closed:
        return "(無新發現)"
    latest = closed[-1]

    path = _push_state_path()
    last = ""
    try:
        with open(path, encoding="utf-8") as f:
            last = str(json.load(f).get("last_pushed_period") or "")
    except (OSError, ValueError, AttributeError):
        last = ""  # 沒推過或 state 壞檔 —— 視同沒推過，重推一次無害
    if latest <= last:
        return "(無新發現)"

    report = income_statement(latest)
    # 先記 state 再回：dispatcher 送 TG 失敗頂多漏推一次（大王隨時可手動查），
    # 反過來「送成功但沒記到」會每天重推同一個月。
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_pushed_period": latest}, f, ensure_ascii=False)
    os.replace(tmp, path)
    return f"🧾 {_fmt_period(latest)} 已關帳，月損益出爐：\n\n{report}"
