"""福群/集團降成本 —— 財務長 Phase 3：材料漲價偵測 / 買貴清單 / 費用異常。

資料源與口徑（2026-09-07 實資料驗證）：

- **材料價**：SC00.PO_ORDER_M/D（每日熱表、鮮到當天）。價格比較一律鎖同
  (料號, 供應商, 單位) —— PR_UNIT 不同＝計價基準不同，混比會出現 -100% 的
  假降價（實測 STC90200：147 萬/批 vs 62.9/單位）。基準用近 12 月**中位數**
  不是單一前次價；比率 >10 倍或 <0.1 倍的另列「疑似單位/登打異常」，不進
  漲跌榜 —— 那是資料問題不是價格問題。
- **幣別**：PO 表沒有幣別欄，從 AP_STDPRICE_ITEM 的 CURR_NO 反查同
  (料號, 供應商)；查無標 '?'。金額影響換算只做 VND/USD（GL_EXCHANGE 只有
  USD 匯率），其他幣別照原幣列、不硬換 —— 同 Phase 2 的誠實規則。
- **標準價**：GL00.AP_STDPRICE_ITEM（料號×供應商×幣別，取 AP_DATE 最新一筆）。
  「買貴」＝PO 價 > 標準價 5%+；輸出帶標準價日期 —— 標準價可能過時，這是
  「該去談」的線索清單，不是指控。實測近 3 月 733 筆有標準價的明細 217 筆
  買貴 >5%（最大宗 +36.3%）。
- **費用異常**：沿用 finance_statements 的關帳期/群組口徑（排結轉、排作廢），
  本期 vs 歷史關帳月中位數，門檻（倍數＋絕對額）寫死在輸出裡，是算術不是預測。

零幻覺原則：全部確定性 SQL；排序用金額影響（Δ價×量）不是百分比 —— 3 塊錢
的料漲 50% 不值得大王看。財務全貌屬敏感，不進員工白名單（守門測試釘死）。
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

from agent_core.finance_statements import (
    _active_book, _clean, _esq, _fmt_m, _fmt_period, _group_names, _group_nets,
    _guard, _run, _usd_rate, closed_periods,
)

_STATE_BASENAME = "finance_cost_push.json"

# 極端價格比率 → 疑似單位/登打異常，不當成真漲跌。
_RATIO_ABSURD_HI = 10.0
_RATIO_ABSURD_LO = 0.1
# 金額影響門檻（VND）：低於這個的漲價不值得進榜。
_IMPACT_FLOOR_VND = 5_000_000
# 買貴判準：PO 價超過標準價的比例。
_OVER_STD_PCT = 0.05
# 費用異常門檻：本期 > 歷史中位數 × 倍數，且差額 > 絕對額（VND）。
_ANOMALY_RATIO = 1.3
_ANOMALY_FLOOR_VND = 50_000_000


def _item_currency_map() -> dict[tuple[str, str], str]:
    """(料號, 供應商) → 幣別，取標準價表 AP_DATE 最新一筆。"""
    rows = _run(
        "SELECT ITEM_NO, VEND_NO, MAX_BY(CURR_NO, AP_DATE) "
        "FROM GL00__AP_STDPRICE_ITEM "
        "WHERE COALESCE(CURR_NO, '') <> '' GROUP BY 1, 2", [])
    return {(_clean(i), _clean(v)): _clean(c) for i, v, c in rows}


def _vendor_names() -> dict[str, str]:
    rows = _run(
        "SELECT VEND_NO, COALESCE(NULLIF(SHORTNM_T, ''), NULLIF(SHORTNM_E, ''), "
        "NULLIF(FULLNM_T, ''), VEND_NO) FROM SC00__PO_VENDER_M", [])
    return {_clean(v): _clean(n) for v, n in rows}


def _to_vnd(currency: str, amount: float, usd_rate: float | None) -> float | None:
    if currency == "VND":
        return amount
    if currency == "USD" and usd_rate:
        return amount * usd_rate
    return None


def _fmt_price(p: float) -> str:
    return f"{p:,.4g}" if p < 100 else f"{p:,.0f}"


# ────────────────────────────────────────────────────────────────────
# 材料漲價偵測
# ────────────────────────────────────────────────────────────────────

def _price_moves(window_days: int, baseline_days: int) -> list[dict]:
    """近 window 天每個 (料號,供應商,單位) 的最新價 vs 前 baseline 天中位數。

    回 dict：item/vend/unit/last_price/last_date/base_median/base_n/qty
    （qty=窗內總量，金額影響用）。窗內才有首購（基準期無資料）的不回 —— 沒有
    基準就沒有「漲」可言。
    """
    today = date.today()
    win_lo = (today - timedelta(days=window_days)).isoformat()
    base_lo = (today - timedelta(days=baseline_days)).isoformat()
    rows = _run(
        "WITH po AS ("
        "  SELECT d.ITEM_NO item, m.VEND_NO vend, COALESCE(d.PR_UNIT, '?') unit, "
        "         SUBSTR(CAST(m.ORD_DATE AS VARCHAR), 1, 10) dt, "
        "         TRY_CAST(d.PRICE AS DOUBLE) price, "
        "         COALESCE(TRY_CAST(d.ORD_QTY AS DOUBLE), 0) qty "
        "  FROM SC00__PO_ORDER_D d "
        "  JOIN SC00__PO_ORDER_M m ON m.ORDER_NO = d.ORDER_NO "
        "  WHERE TRY_CAST(d.PRICE AS DOUBLE) > 0 "
        "    AND SUBSTR(CAST(m.ORD_DATE AS VARCHAR), 1, 10) >= ?), "
        "recent AS ("
        "  SELECT item, vend, unit, MAX_BY(price, dt) last_price, MAX(dt) last_date, "
        "         SUM(qty) qty "
        "  FROM po WHERE dt >= ? GROUP BY 1, 2, 3), "
        "base AS ("
        "  SELECT item, vend, unit, MEDIAN(price) base_median, COUNT(*) base_n "
        "  FROM po WHERE dt < ? GROUP BY 1, 2, 3) "
        "SELECT r.item, r.vend, r.unit, r.last_price, r.last_date, r.qty, "
        "       b.base_median, b.base_n "
        "FROM recent r JOIN base b "
        "  ON b.item = r.item AND b.vend = r.vend AND b.unit = r.unit",
        [base_lo, win_lo, win_lo])
    return [
        {"item": _clean(i), "vend": _clean(v), "unit": _clean(u),
         "last_price": float(lp), "last_date": _clean(ld), "qty": float(q or 0),
         "base_median": float(bm), "base_n": int(bn)}
        for i, v, u, lp, ld, q, bm, bn in rows if bm and lp
    ]


def material_price_watch(months: int = 3, top: int = 10) -> str:
    guard = _guard()
    if guard:
        return guard
    months = max(1, min(int(months or 3), 12))
    top = max(3, min(int(top or 10), 30))
    try:
        book, _, currency = _active_book()
        rate = _usd_rate(book, "999912")
    except Exception:  # noqa: BLE001 —— GL 讀不到只影響換匯，價格面照走
        rate = None

    moves = _price_moves(window_days=months * 30, baseline_days=365)
    curmap = _item_currency_map()
    vnames = _vendor_names()

    ups: list[tuple[float, str]] = []       # (impact_vnd, line)
    downs: list[tuple[float, str]] = []
    weird: list[str] = []
    other_cur: list[str] = []
    for mv in moves:
        ratio = mv["last_price"] / mv["base_median"]
        cur = curmap.get((mv["item"], mv["vend"]), "?")
        vend = vnames.get(mv["vend"], mv["vend"])
        desc = (f"{mv['item']}（{vend}，{cur}）"
                f"{_fmt_price(mv['base_median'])}→{_fmt_price(mv['last_price'])}"
                f"/{mv['unit']}（{ratio * 100 - 100:+.1f}%，基準={mv['base_n']} 筆中位數，"
                f"最近 {mv['last_date']}）")
        if ratio > _RATIO_ABSURD_HI or ratio < _RATIO_ABSURD_LO:
            weird.append("- " + desc)
            continue
        if abs(ratio - 1) < 0.03:
            continue
        if mv["base_n"] < 2:
            continue  # 基準只有 1 筆＝拿單一舊價當中位數，太弱不進榜
        delta_amt = (mv["last_price"] - mv["base_median"]) * mv["qty"]
        vnd = _to_vnd(cur, delta_amt, rate)
        if vnd is None:
            if abs(ratio - 1) >= 0.10:
                other_cur.append(f"- {desc}，窗內量 {mv['qty']:,.0f}")
            continue
        if abs(vnd) < _IMPACT_FLOOR_VND:
            continue
        line = f"- {desc}，窗內量 {mv['qty']:,.0f} ≈ 影響 {_fmt_m(vnd)} 百萬 VND"
        (ups if vnd > 0 else downs).append((abs(vnd), line))

    lines = [f"📈 材料價格觀察（近 {months} 個月 vs 前 12 個月中位數，"
             f"同料同供應商同單位才比）"]
    ups.sort(reverse=True)
    downs.sort(reverse=True)
    if ups:
        lines.append(f"🔺 漲價（按金額影響排序，門檻 {_IMPACT_FLOOR_VND / 1e6:.0f}M VND）：")
        lines.extend(line for _, line in ups[:top])
    else:
        lines.append("🔺 漲價：無達門檻項目 ✅")
    if downs:
        lines.append("🔻 降價（省到的）：")
        lines.extend(line for _, line in downs[:max(3, top // 2)])
    if other_cur:
        lines.append("💱 其他幣別（無匯率不換算，變動 ≥10% 才列）：")
        lines.extend(other_cur[:top])
    if weird:
        lines.append("⚠️ 疑似單位/登打異常（價差 >10 倍，請人工確認，不當漲跌計）：")
        lines.extend(weird[:5])
    if rate:
        lines.append(f"💵 換算匯率 {rate:,.0f} {currency}/USD（僅 VND/USD 參與影響排序）。")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 買貴清單（PO 價 vs 標準價）
# ────────────────────────────────────────────────────────────────────

def overpriced_purchases(months: int = 3, top: int = 10) -> str:
    guard = _guard()
    if guard:
        return guard
    months = max(1, min(int(months or 3), 12))
    top = max(3, min(int(top or 10), 30))
    try:
        book, _, currency = _active_book()
        rate = _usd_rate(book, "999912")
    except Exception:  # noqa: BLE001
        rate = None
    lo = (date.today() - timedelta(days=months * 30)).isoformat()

    rows = _run(
        "WITH std AS ("
        "  SELECT ITEM_NO, VEND_NO, "
        "         MAX_BY(TRY_CAST(PRICE AS DOUBLE), AP_DATE) std_price, "
        "         MAX_BY(CURR_NO, AP_DATE) curr, "
        "         SUBSTR(CAST(MAX(AP_DATE) AS VARCHAR), 1, 10) std_date "
        "  FROM GL00__AP_STDPRICE_ITEM "
        "  WHERE TRY_CAST(PRICE AS DOUBLE) > 0 GROUP BY 1, 2) "
        "SELECT d.ITEM_NO, m.VEND_NO, s.curr, s.std_price, s.std_date, "
        "       MAX_BY(TRY_CAST(d.PRICE AS DOUBLE), m.ORD_DATE) po_price, "
        "       SUM(COALESCE(TRY_CAST(d.ORD_QTY AS DOUBLE), 0)) qty, "
        "       SUM((TRY_CAST(d.PRICE AS DOUBLE) - s.std_price) "
        "           * COALESCE(TRY_CAST(d.ORD_QTY AS DOUBLE), 0)) overpay, "
        "       COUNT(*) n "
        "FROM SC00__PO_ORDER_D d "
        "JOIN SC00__PO_ORDER_M m ON m.ORDER_NO = d.ORDER_NO "
        "JOIN std s ON s.ITEM_NO = d.ITEM_NO AND s.VEND_NO = m.VEND_NO "
        "WHERE SUBSTR(CAST(m.ORD_DATE AS VARCHAR), 1, 10) >= ? "
        "  AND TRY_CAST(d.PRICE AS DOUBLE) > s.std_price * ? "
        "GROUP BY 1, 2, 3, 4, 5 ORDER BY 8 DESC", [lo, 1 + _OVER_STD_PCT])
    vnames = _vendor_names()

    ranked: list[tuple[float, str]] = []
    other_cur: list[str] = []
    for item, vend, curr, std_p, std_d, po_p, qty, overpay, n in rows:
        curr = _clean(curr) or "?"
        vend_name = vnames.get(_clean(vend), _clean(vend))
        pct = (float(po_p) - float(std_p)) / float(std_p) * 100
        desc = (f"{_clean(item)}（{vend_name}）標準 {_fmt_price(float(std_p))} "
                f"[{_clean(std_d)}] → 實買 {_fmt_price(float(po_p))} {curr}"
                f"（{pct:+.1f}%，{int(n)} 筆、量 {float(qty):,.0f}）")
        vnd = _to_vnd(curr, float(overpay or 0), rate)
        if vnd is None:
            other_cur.append(f"- {desc}，多付 {float(overpay or 0):,.0f} {curr}")
        else:
            ranked.append((vnd, f"- {desc}，多付 ≈ {_fmt_m(vnd)} 百萬 VND"))

    lines = [f"💸 買貴清單（近 {months} 個月，PO 價 > 標準價 "
             f"{_OVER_STD_PCT * 100:.0f}%+，標準價取最新一筆、[日期] 供判斷是否過時）"]
    if not ranked and not other_cur:
        lines.append("✅ 沒有超過標準價 5% 的採購。")
    ranked.sort(reverse=True)
    total_vnd = sum(v for v, _ in ranked)
    if ranked:
        lines.extend(line for _, line in ranked[:top])
        lines.append(f"Σ 以上（VND/USD 換算）合計多付 ≈ {_fmt_m(total_vnd)} 百萬 VND"
                     f"（{len(ranked)} 個料號）。")
    if other_cur:
        lines.append("💱 其他幣別（不換算）：")
        lines.extend(other_cur[:top])
    lines.append("📌 標準價可能過時 —— 這是「該去跟供應商談」的線索清單，不是結論。")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 費用異常偵測（關帳月 vs 歷史）
# ────────────────────────────────────────────────────────────────────

def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def expense_anomaly(period: str = "") -> str:
    guard = _guard()
    if guard:
        return guard
    try:
        book, book_name, currency = _active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    closed = closed_periods(book)
    if len(closed) < 4:
        return "⚠️ 關帳月不足 4 個月，歷史基準不夠，先用 expense_breakdown 看結構。"
    p = "".join(ch for ch in str(period or "") if ch.isdigit()) or closed[-1]
    if p not in closed:
        return f"⚠️ {p} 不是已關帳月（可用：{', '.join(closed[-6:])}）。"
    history = [x for x in closed if x < p][-12:]
    if len(history) < 3:
        return f"⚠️ {p} 之前的關帳月不足 3 個，基準不夠。"

    cur_g = {g: -v for g, v in _group_nets(book, p, ("5", "6")).items()}
    hist_g: dict[str, list[float]] = {}
    for hp in history:
        for g, v in _group_nets(book, hp, ("5", "6")).items():
            hist_g.setdefault(g, []).append(-v)

    flags: list[tuple[float, str]] = []
    news: list[str] = []
    names = _group_names(sorted(set(cur_g) | set(hist_g)))
    for g, v in cur_g.items():
        hist = hist_g.get(g)
        if not hist:
            if v > _ANOMALY_FLOOR_VND:
                news.append(f"- {g} {names.get(g, g)}：本月新出現 {_fmt_m(v)}")
            continue
        med = _median(hist)
        if med <= 0:
            continue
        if v > med * _ANOMALY_RATIO and v - med > _ANOMALY_FLOOR_VND:
            flags.append((v - med, f"- {g} {names.get(g, g)}：{_fmt_m(v)}，"
                          f"歷史中位數 {_fmt_m(med)}（{(v / med - 1) * 100:+.0f}%、"
                          f"高出 {_fmt_m(v - med)}；近 {len(hist)} 個關帳月）"))
    gone = [g for g, hist in hist_g.items()
            if g not in cur_g and _median(hist) > _ANOMALY_FLOOR_VND]

    lines = [f"🔎 {book_name} {_fmt_period(p)} 費用/成本異常（vs 前 {len(history)} 個"
             f"關帳月中位數；門檻＝超過 {_ANOMALY_RATIO:.1f} 倍且差額 "
             f"> {_ANOMALY_FLOOR_VND / 1e6:.0f}M；單位百萬 {currency}）"]
    flags.sort(reverse=True)
    if flags:
        lines.extend(line for _, line in flags)
    else:
        lines.append("✅ 沒有超過門檻的異常科目。")
    if news:
        lines.append("🆕 本月新出現的科目群組：")
        lines.extend(news)
    if gone:
        lines.append("💤 歷史常態有、本月歸零：" + "、".join(
            f"{g} {names.get(g, g)}" for g in sorted(gone)))
    lines.append("📌 異常＝算術偏離，不是結論 —— 大額請用 expense_breakdown 下鑽再判斷。")
    lines.append(_esq()._stale_hint().strip())
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 每月自動推播（新關帳月 → 費用異常 + 買貴摘要）
# ────────────────────────────────────────────────────────────────────

def _push_state_path() -> str:
    from agent_core.logging_and_paths import STATE_DIR
    return os.path.join(STATE_DIR, _STATE_BASENAME)


def cost_review_autopush() -> str:
    """每日排程檢查：出現「新關帳月」才回成本月報，否則回 (無新發現)。

    infra 壞掉用 raise（dispatcher 記 last_error，不天天推警告）—— 同
    income_statement_autopush 的慣例。
    """
    guard = _guard()
    if guard:
        raise RuntimeError(guard)
    book, _, _ = _active_book()
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
        last = ""
    if latest <= last:
        return "(無新發現)"

    body = expense_anomaly(latest) + "\n\n" + overpriced_purchases(months=1, top=6)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_pushed_period": latest}, f, ensure_ascii=False)
    os.replace(tmp, path)
    return f"🧾 {_fmt_period(latest)} 關帳，降成本月報：\n\n{body}"
