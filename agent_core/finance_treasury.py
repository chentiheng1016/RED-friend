"""福群（越南廠）資金面 —— 財務長 Phase 2：現金水位 / 現金流 / 待付壓力 / 資金展望。

資料源與口徑（2026-09-07 實資料驗證）：

- **現金水位＝GL 傳票累加**（1101 現金 / 1102 銀行存款，逐 8 碼帳戶），排除
  STATUS='0' 作廢傳票後，累加值 == ERP 自己的 GL_BALANCES_M 期末（1101/1102/
  1103 三組驗證過）。GL 是本位幣 VND，所以金額一律 VND（美金另附參考換算）。
  比餘額表新鮮：開放月的未過帳草稿也算進來（水位本來就要最即時）。
- **應收**只有 GL 餘額可用（1103 三類：外銷／代工 JAI FUNG／代工 JAI JYE）：
  AR 模組（AR_SALES_M/AR_RCV_M）是佳桀 2023-24 舊資料，福群沒在用 ——
  **應收沒有逐筆到期日**，別想從 ERP 排收款時程表。
- **待付兩條線**：① 付款請示單 AP_APPLY_M（PAYMENT_ID 空＝未付，確定性判準，
  同 purchasing_brief；這裡是全部門 CFO 彙總，不只 TWP/VNP）② ERP 對帳付款
  排程 AP_DUE_D（PAY_NO 空＝未付、PL_PAY_DATE=計畫付款日 98.6% 有填）——
  ⚠️ 未付池含 2022-23 殭屍項（帳外付掉沒銷），一律加日期窗過濾。
- **月現金流**從 1101/1102 傳票借（流入）/貸（流出）直加總；同張傳票同時
  借貸現金科目的部分（帳戶互轉）先軋掉再計，不然轉帳會虛胖流量。
- 幣別：GL 本位幣 VND；請示單/付款排程是原幣 —— USD 用 GL_EXCHANGE 換算
  VND，**其他幣別（NTD/CNY/EUR）不硬換**（ERP 沒有這些匯率），照幣別分列。

零幻覺原則：全部確定性 SQL；「歷史月均」是算術不是預測，輸出會標明。
財務全貌屬敏感，不進員工白名單（守門測試釘死）。
"""
from __future__ import annotations

from datetime import date, timedelta

from agent_core.finance_statements import (
    _active_book, _clean, _esq, _fmt_m, _fmt_period, _guard, _pad, _run,
    _usd_rate,
)

_CASH_PREFIXES = ("1101", "1102")
_AR_PREFIX = "1103"
# (前綴, 標籤, 是否貸方為正)。CFO 快照要看的負債線。
_DEBT_GROUPS = (
    ("2102", "應付帳款"), ("2202", "應付薪資/租金"),
    ("2101", "短期借款"), ("2301", "長期借款"),
)


# ────────────────────────────────────────────────────────────────────
# 基礎查詢
# ────────────────────────────────────────────────────────────────────

def _acct_balances(book: str, like: str, credit_positive: bool = False) -> list[tuple[str, str, float]]:
    """某科目前綴的逐 8 碼帳戶累加餘額（排除作廢；含開放月草稿）。"""
    sign = ("COALESCE(TRY_CAST(d.C_MONEY AS DECIMAL(20,2)), 0) "
            "- COALESCE(TRY_CAST(d.D_MONEY AS DECIMAL(20,2)), 0)") if credit_positive else (
           "COALESCE(TRY_CAST(d.D_MONEY AS DECIMAL(20,2)), 0) "
           "- COALESCE(TRY_CAST(d.C_MONEY AS DECIMAL(20,2)), 0)")
    rows = _run(
        f"SELECT d.ACCT_ID, COALESCE(NULLIF(a.NAME_T, ''), d.ACCT_ID), SUM({sign}) "
        "FROM GL00__GL_VOUCH_D d "
        "JOIN GL00__GL_VOUCH_M m ON m.BOOKS_NO = d.BOOKS_NO AND m.VOUCH_ID = d.VOUCH_ID "
        "LEFT JOIN GL00__GL_ACCT_M a ON a.ACCT_ID = d.ACCT_ID "
        "WHERE m.BOOKS_NO = ? AND m.STATUS <> '0' AND d.ACCT_ID LIKE ? "
        "GROUP BY 1, 2 HAVING ABS(SUM(" + sign + ")) > 0.5 ORDER BY 3 DESC",
        [book, like + "%"])
    return [(_clean(a), _clean(n), float(v or 0)) for a, n, v in rows]


def _group_total(book: str, like: str, credit_positive: bool = False) -> float:
    return sum(v for _, _, v in _acct_balances(book, like, credit_positive))


def _monthly_cash_flows(book: str, months: int) -> list[tuple[str, float, float, float]]:
    """逐月 (期間, 流入, 流出, 淨額)。帳戶互轉在傳票層先軋掉（取借貸較小值）。"""
    rows = _run(
        "WITH per_voucher AS ("
        "  SELECT m.PERIOD_ID, d.VOUCH_ID, "
        "         SUM(COALESCE(TRY_CAST(d.D_MONEY AS DECIMAL(20,2)), 0)) AS din, "
        "         SUM(COALESCE(TRY_CAST(d.C_MONEY AS DECIMAL(20,2)), 0)) AS cout "
        "  FROM GL00__GL_VOUCH_D d "
        "  JOIN GL00__GL_VOUCH_M m ON m.BOOKS_NO = d.BOOKS_NO AND m.VOUCH_ID = d.VOUCH_ID "
        "  WHERE m.BOOKS_NO = ? AND m.STATUS <> '0' "
        "    AND (d.ACCT_ID LIKE '1101%' OR d.ACCT_ID LIKE '1102%') "
        "  GROUP BY 1, 2) "
        "SELECT PERIOD_ID, "
        "       SUM(din - LEAST(din, cout)) AS inflow, "
        "       SUM(cout - LEAST(din, cout)) AS outflow "
        "FROM per_voucher GROUP BY 1 ORDER BY 1", [book])
    out = [(_clean(p), float(i or 0), float(o or 0), float(i or 0) - float(o or 0))
           for p, i, o in rows]
    return out[-max(1, min(int(months or 6), 24)):]


# 未付請示單的「活壓力」窗：申請超過這天數還沒付的，多半是帳外已付沒銷單的
# 殭屍（實測 2024-09 的 USD 2.44M 掛兩年、STATUS 仍是待付款）——STATUS 分不出
# 死活，只能用申請日切。舊項另列一行提醒人工清帳，缺口判定不吃它。
_APPLY_FRESH_DAYS = 180


def _apply_cutoff() -> str:
    return (date.today() - timedelta(days=_APPLY_FRESH_DAYS)).isoformat()


def _unpaid_apply_by_currency(fresh: bool = True) -> list[tuple[str, int, float]]:
    """未付請示單（全部門）幣別彙總。fresh=True 只算近 _APPLY_FRESH_DAYS 天。"""
    op = ">=" if fresh else "<"
    rows = _run(
        "SELECT COALESCE(NULLIF(MONEY_UNIT, ''), '?'), COUNT(*), "
        "       SUM(COALESCE(TRY_CAST(NET_MONEY AS DECIMAL(20,2)), 0)) "
        "FROM GL00__AP_APPLY_M "
        "WHERE (PAYMENT_ID IS NULL OR PAYMENT_ID = '') "
        f"  AND SUBSTR(CAST(APPLY_DATE AS VARCHAR), 1, 10) {op} ? "
        "GROUP BY 1 ORDER BY 3 DESC", [_apply_cutoff()])
    return [(_clean(c), int(n), float(v or 0)) for c, n, v in rows]


def _unpaid_apply_top(limit: int = 8) -> list[tuple]:
    rows = _run(
        "SELECT a.APPLY_NO, SUBSTR(CAST(a.APPLY_DATE AS VARCHAR), 1, 10), a.GRT_DEPT, "
        "       COALESCE(NULLIF(v.SHORTNM_T, ''), NULLIF(v.SHORTNM_E, ''), "
        "                NULLIF(v.FULLNM_T, ''), a.VEND_NO), "
        "       a.MONEY_UNIT, TRY_CAST(a.NET_MONEY AS DECIMAL(20,2)) "
        "FROM GL00__AP_APPLY_M a "
        "LEFT JOIN SC00__PO_VENDER_M v ON v.VEND_NO = a.VEND_NO AND v.ORG_ID = a.ORG_ID "
        "WHERE (a.PAYMENT_ID IS NULL OR a.PAYMENT_ID = '') "
        "  AND SUBSTR(CAST(a.APPLY_DATE AS VARCHAR), 1, 10) >= ? "
        "ORDER BY CASE a.MONEY_UNIT WHEN 'USD' THEN 0 WHEN 'VND' THEN 1 ELSE 2 END, "
        "         TRY_CAST(a.NET_MONEY AS DECIMAL(20,2)) DESC "
        "LIMIT ?", [_apply_cutoff(), max(1, min(int(limit or 8), 30))])
    return rows


def _planned_payments(days_back: int = 45, days_ahead: int = 60) -> list[tuple]:
    """ERP 對帳付款排程：未付（PAY_NO 空）× 計畫付款日在窗內。

    窗外舊項（2022-23 殭屍：帳外付掉沒銷 PAY_NO）一律不列 —— 列了只會把
    待付合計灌水成假警報。回 (計畫付款日, 幣別, 金額) 逐日彙總。
    """
    today = date.today()
    lo = (today - timedelta(days=days_back)).isoformat()
    hi = (today + timedelta(days=days_ahead)).isoformat()
    rows = _run(
        "SELECT SUBSTR(CAST(PL_PAY_DATE AS VARCHAR), 1, 10), "
        "       COALESCE(NULLIF(MONEY_UNIT, ''), '?'), "
        "       SUM(COALESCE(TRY_CAST(AP_MONEY AS DECIMAL(20,2)), 0)) "
        "FROM GL00__AP_DUE_D "
        "WHERE (PAY_NO IS NULL OR PAY_NO = '') AND PL_PAY_DATE IS NOT NULL "
        "  AND SUBSTR(CAST(PL_PAY_DATE AS VARCHAR), 1, 10) BETWEEN ? AND ? "
        "GROUP BY 1, 2 ORDER BY 1", [lo, hi])
    return [(_clean(d), _clean(c), float(v or 0)) for d, c, v in rows]


def _to_vnd(currency: str, amount: float, usd_rate: float | None) -> float | None:
    """原幣→VND；只換 VND/USD（ERP 只有 USD 匯率），其他回 None＝別硬換。"""
    if currency == "VND":
        return amount
    if currency == "USD" and usd_rate:
        return amount * usd_rate
    return None


def _fmt_cur(currency: str, amount: float) -> str:
    return f"{amount:,.0f} {currency}"


# ────────────────────────────────────────────────────────────────────
# 對外工具面
# ────────────────────────────────────────────────────────────────────

def cash_position() -> str:
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"

    lines = [f"💰 {book_name} 資金水位快照　單位：百萬 {currency}"]
    total_cash = 0.0
    lines.append("```")
    for prefix, label in (("1101", "現金"), ("1102", "銀行存款")):
        accts = _acct_balances(book, prefix)
        sub = sum(v for _, _, v in accts)
        total_cash += sub
        lines.append(f"◾ {label} 小計 {_fmt_m(sub)}")
        for a, n, v in accts:
            lines.append(_pad(f"  {a} {n}", 40) + _pad(_fmt_m(v), 10, right=True))
    lines.append(f"◾ 可動用現金合計 {_fmt_m(total_cash)}")
    lines.append("```")

    ar = _acct_balances(book, _AR_PREFIX)
    ar_total = sum(v for _, _, v in ar)
    lines.append(f"📥 應收帳款 {_fmt_m(ar_total)}：" + "、".join(
        f"{n} {_fmt_m(v)}" for _, n, v in ar))

    debt_lines = []
    debt_total = 0.0
    for prefix, label in _DEBT_GROUPS:
        v = _group_total(book, prefix, credit_positive=True)
        if abs(v) > 0.5:
            debt_total += v
            debt_lines.append(f"{label} {_fmt_m(v)}")
    lines.append(f"📤 負債 {_fmt_m(debt_total)}：" + "、".join(debt_lines))

    net = total_cash + ar_total - debt_total
    lines.append(f"⚖️ 淨部位（現金＋應收－上列負債）：{_fmt_m(net)}")
    rate = _usd_rate(book, "999912")
    if rate:
        lines.append(f"💵 美金參考（{rate:,.0f} {currency}/USD）：現金 ≈ "
                     f"{total_cash / rate:,.0f}、應收 ≈ {ar_total / rate:,.0f} USD")
    lines.append("📌 含未過帳草稿傳票（水位取最即時）；作廢傳票已排除。")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(lines)


def cash_flow_monthly(months: int = 6) -> str:
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    flows = _monthly_cash_flows(book, months)
    if not flows:
        return "⚠️ GL 總帳查無現金科目傳票。"

    all_flows = _monthly_cash_flows(book, 240)
    running: dict[str, float] = {}
    acc = 0.0
    for p, _i, _o, n in all_flows:
        acc += n
        running[p] = acc

    header = ["期間", "流入", "流出", "淨額", "期末現金"]
    widths = [8, 11, 11, 11, 11]
    lines = [f"🌊 {book_name} 逐月現金流（銀行＋現金，帳戶互轉已軋）　單位：百萬 {currency}",
             "```",
             "".join(_pad(h, w + 2) for h, w in zip(header, widths)).rstrip()]
    for p, i, o, n in flows:
        cells = [_fmt_period(p), _fmt_m(i), _fmt_m(o), _fmt_m(n), _fmt_m(running[p])]
        lines.append("".join(_pad(c, w + 2, right=(k > 0)) for k, (c, w)
                             in enumerate(zip(cells, widths))).rstrip())
    lines.append("```")
    lines.append("📌 期末現金＝傳票累加（作廢已排除），最新月含未過帳草稿。")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(lines)


def payment_pressure(top: int = 8) -> str:
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    rate = _usd_rate(book, "999912")

    lines = [f"🧾 {book_name} 待付壓力"]
    by_cur = _unpaid_apply_by_currency(fresh=True)
    if by_cur:
        parts = [f"{c} {v:,.0f}（{n} 張）" for c, n, v in by_cur]
        lines.append(f"◾ 未付款請示單（全部門、近 {_APPLY_FRESH_DAYS} 天申請，"
                     "PAYMENT_ID 空＝ERP 尚未付款）：")
        lines.append("　" + "、".join(parts))
        rows = _unpaid_apply_top(top)
        if rows:
            lines.append("```")
            for no, dt, dept, vend, cur, money in rows:
                lines.append(
                    _pad(f"{_clean(no)} {_clean(dt)}", 25)
                    + _pad(_clean(dept), 5)
                    + _pad(_clean(vend)[:14], 16)
                    + _pad(_fmt_cur(_clean(cur), float(money or 0)), 18, right=True))
            lines.append("```")
    else:
        lines.append(f"◾ 未付款請示單（近 {_APPLY_FRESH_DAYS} 天申請）：無 ✅")
    stale = _unpaid_apply_by_currency(fresh=False)
    if stale:
        lines.append(f"🗄️ 另有 {_APPLY_FRESH_DAYS} 天前的舊未付請示 "
                     f"{sum(n for _, n, _ in stale)} 張（"
                     + "、".join(f"{c} {v:,.0f}" for c, v in
                                 ((c, v) for c, _n, v in stale))
                     + "）—— 多半已帳外處理未銷單，建議請會計清帳；缺口判定不計入。")

    planned = _planned_payments()
    today = date.today().isoformat()
    overdue: dict[str, float] = {}
    upcoming: dict[str, float] = {}
    for d, c, v in planned:
        (overdue if d < today else upcoming).setdefault(c, 0.0)
        (overdue if d < today else upcoming)[c] += v
    if overdue:
        lines.append("⚠️ ERP 付款排程已逾期未銷（近 45 天內計畫日）："
                     + "、".join(_fmt_cur(c, v) for c, v in sorted(
                         overdue.items(), key=lambda x: -x[1])))
    if upcoming:
        lines.append("◾ 未來 60 天計畫付款："
                     + "、".join(_fmt_cur(c, v) for c, v in sorted(
                         upcoming.items(), key=lambda x: -x[1])))

    committed_vnd = 0.0
    unconverted: list[str] = []
    for c, _n, v in by_cur:
        vnd = _to_vnd(c, v, rate)
        if vnd is None:
            unconverted.append(c)
        else:
            committed_vnd += vnd
    cash = _group_total(book, "1101") + _group_total(book, "1102")
    lines.append(f"⚖️ 可動用現金 {_fmt_m(cash)} 百萬 vs 未付請示（VND+USD 換算）"
                 f"{_fmt_m(committed_vnd)} 百萬"
                 + (f"（另有 {'/'.join(unconverted)} 未換算）" if unconverted else ""))
    if committed_vnd > cash:
        lines.append(f"🚨 缺口 {_fmt_m(committed_vnd - cash)} 百萬 {currency} —— "
                     "未付請示已超過現金水位，須排收款或動用額度。")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(lines)


def cash_outlook(weeks: int = 8) -> str:
    """資金展望（也是每週一晨推的內容）：水位 → 逐週計畫付款 → 缺口判定。"""
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    weeks = max(2, min(int(weeks or 8), 13))
    rate = _usd_rate(book, "999912")

    cash = _group_total(book, "1101") + _group_total(book, "1102")
    ar_total = _group_total(book, _AR_PREFIX)

    lines = [f"🔭 {book_name} 資金展望（未來 {weeks} 週）　單位：百萬 {currency}",
             f"💰 可動用現金 {_fmt_m(cash)}｜📥 應收帳款 {_fmt_m(ar_total)}"]

    today = date.today()
    planned = _planned_payments(days_back=45, days_ahead=weeks * 7)
    weekly: dict[int, float] = {}
    weekly_other: dict[int, dict[str, float]] = {}
    overdue_vnd = 0.0
    overdue_other: dict[str, float] = {}
    for d, c, v in planned:
        dd = date.fromisoformat(d)
        vnd = _to_vnd(c, v, rate)
        if dd < today:
            if vnd is None:
                overdue_other[c] = overdue_other.get(c, 0.0) + v
            else:
                overdue_vnd += vnd
            continue
        w = (dd - today).days // 7
        if vnd is None:
            weekly_other.setdefault(w, {})
            weekly_other[w][c] = weekly_other[w].get(c, 0.0) + v
        else:
            weekly[w] = weekly.get(w, 0.0) + vnd

    apply_vnd = 0.0
    apply_other: list[str] = []
    for c, _n, v in _unpaid_apply_by_currency():
        vnd = _to_vnd(c, v, rate)
        if vnd is None:
            apply_other.append(_fmt_cur(c, v))
        else:
            apply_vnd += vnd

    lines.append("```")
    lines.append(_pad("週", 12) + _pad("計畫付款", 11, right=True)
                 + _pad("累計", 11, right=True) + _pad("現金-累計", 12, right=True))
    cum = overdue_vnd
    if overdue_vnd > 0.5 or overdue_other:
        extra = ("＋" + "、".join(_fmt_cur(c, v) for c, v in overdue_other.items())
                 if overdue_other else "")
        lines.append(_pad("已逾期", 12) + _pad(_fmt_m(overdue_vnd), 11, right=True)
                     + _pad(_fmt_m(cum), 11, right=True)
                     + _pad(_fmt_m(cash - cum), 12, right=True) + extra)
    for w in range(weeks):
        amt = weekly.get(w, 0.0)
        cum += amt
        label = (f"{(today + timedelta(days=w * 7)).strftime('%m/%d')}"
                 f"-{(today + timedelta(days=w * 7 + 6)).strftime('%m/%d')}")
        extra = ("＋" + "、".join(_fmt_cur(c, v) for c, v in weekly_other[w].items())
                 if w in weekly_other else "")
        lines.append(_pad(label, 12) + _pad(_fmt_m(amt), 11, right=True)
                     + _pad(_fmt_m(cum), 11, right=True)
                     + _pad(_fmt_m(cash - cum), 12, right=True) + extra)
    lines.append("```")

    total_committed = cum + apply_vnd
    lines.append(f"🧾 另有未付請示單（近 {_APPLY_FRESH_DAYS} 天、VND+USD 換算）"
                 f"{_fmt_m(apply_vnd)}"
                 + (f"；未換算：{'、'.join(apply_other)}" if apply_other else "")
                 + " —— 與付款排程可能部分重疊（請示單付款後才銷排程）。")
    if cash - cum < 0:
        lines.append(f"🚨 資金缺口：{weeks} 週內計畫付款累計已超過現金 "
                     f"{_fmt_m(cum - cash)} 百萬 —— 要靠期間收款回補，"
                     f"應收餘額 {_fmt_m(ar_total)} 是回補來源，建議先排大客戶收款。")
    elif cash - total_committed < 0:
        lines.append(f"⚠️ 現金可覆蓋 {weeks} 週計畫付款，但加計未付請示後差 "
                     f"{_fmt_m(total_committed - cash)} 百萬，注意收款節奏。")
    else:
        lines.append(f"✅ 現金可覆蓋 {weeks} 週計畫付款＋未付請示，"
                     f"餘裕 {_fmt_m(cash - total_committed)} 百萬。")

    flows = _monthly_cash_flows(book, 3)
    if flows:
        avg_in = sum(f[1] for f in flows) / len(flows)
        avg_out = sum(f[2] for f in flows) / len(flows)
        lines.append(f"📊 近 {len(flows)} 個月實績月均：流入 {_fmt_m(avg_in)}、"
                     f"流出 {_fmt_m(avg_out)}（歷史算術，非預測）。")
    if rate:
        lines.append(f"💵 匯率參考：{rate:,.0f} {currency}/USD。")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(lines)
