"""量化決策計算 skill — 經濟 / 財務 / 統計的「驗算過」計算器。

對象：鞋廠老闆（大王）的真實生意決策。小紅在回答定價、成本、損益兩平、
彈性、投資評估、敘述統計、簡單迴歸這類問題時，**不要心算硬湊**，
直接呼叫這裡的函式拿「算過、可驗證」的數字，再用白話解讀商業意義。

設計：
  - 純函式、無副作用、不打網路、不打 Gemini → 全部標 background_safe，背景排程也能用。
  - 參數一律用「逗號/空白分隔的字串」收多筆數字（對齊本 repo 既有工具慣例，
    避免 Gemini function-calling 對 list 型別 annotation 的派發雷）。
  - 輸入不合理時回「友善的繁中錯誤字串」而不是 raise，方便小紅直接轉述給大王。
  - 回傳字串保留英文術語（Contribution Margin / NPV / R²）並附中文解釋。

這些只是「算」。要不要這樣決策、假設是否成立、相關是否等於因果 —
由小紅依 persona 的【量化分析素養】守則判斷與提醒。
"""
import math
import statistics


# ────────────────────────────────────────────────────────────────────
# 共用：把「1, 2 3\n4」這種字串解析成 float list
# ────────────────────────────────────────────────────────────────────
def _parse_numbers(raw):
    """把逗號 / 空白 / 換行分隔的字串解析成 float list。失敗時 raise ValueError。"""
    if raw is None:
        raise ValueError("沒有提供數字")
    text = str(raw).replace(",", " ").replace("\n", " ").replace("\t", " ")
    parts = [p for p in text.split(" ") if p.strip()]
    if not parts:
        raise ValueError("沒有解析到任何數字")
    out = []
    for p in parts:
        try:
            out.append(float(p))
        except ValueError:
            raise ValueError(f"「{p}」不是有效數字")
    return out


def _fmt(x, nd=4):
    """數字格式化：整數就不帶小數，否則保留 nd 位且去掉尾端 0。"""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return str(x)
    r = round(float(x), nd)
    if r == int(r):
        return f"{int(r):,}"
    return f"{r:,.{nd}f}".rstrip("0").rstrip(".")


# ────────────────────────────────────────────────────────────────────
# 1. 損益兩平 / 貢獻邊際
# ────────────────────────────────────────────────────────────────────
def break_even_analysis(fixed_cost: float, price_per_unit: float,
                        variable_cost_per_unit: float) -> str:
    """算損益兩平點（Break-even）與貢獻邊際（Contribution Margin）。

    用在大王問「要賣幾雙才回本」、「這價格撐不撐得住固定成本」。

    Args:
        fixed_cost: 固定成本（總額，例如模具+管銷）。
        price_per_unit: 單位售價。
        variable_cost_per_unit: 單位變動成本（料+工+包裝等隨產量變動的）。
    Returns:
        貢獻邊際、CM 比率、損益兩平數量（含無條件進位）、損益兩平營收。
    """
    cm = price_per_unit - variable_cost_per_unit
    if price_per_unit <= 0:
        return "❌ 售價必須 > 0。"
    if cm <= 0:
        return (f"❌ 貢獻邊際 = 售價 {_fmt(price_per_unit)} − 單位變動成本 "
                f"{_fmt(variable_cost_per_unit)} = {_fmt(cm)} ≤ 0；"
                "每多賣一單位反而虧更多，再多銷量也無法損益兩平。"
                "要嘛提價、要嘛降單位變動成本。")
    be_qty_exact = fixed_cost / cm
    be_qty = math.ceil(be_qty_exact)
    cm_ratio = cm / price_per_unit
    be_revenue = be_qty * price_per_unit
    return (
        "📊 損益兩平分析（Break-even）\n"
        f"  貢獻邊際 Contribution Margin = 售價 − 單位變動成本 = "
        f"{_fmt(price_per_unit)} − {_fmt(variable_cost_per_unit)} = {_fmt(cm)}/單位\n"
        f"  CM 比率 = {_fmt(cm_ratio * 100, 2)}%（每 1 元營收有這麼多拿去攤固定成本+利潤）\n"
        f"  損益兩平數量 = 固定成本 ÷ 貢獻邊際 = {_fmt(fixed_cost)} ÷ {_fmt(cm)} "
        f"= {_fmt(be_qty_exact, 2)} → 需 {be_qty:,} 單位（無條件進位）\n"
        f"  損益兩平營收 ≈ {_fmt(be_revenue)}\n"
        "  解讀：賣超過這個量才開始賺；低於就虧。產量假設不變動成本結構才成立。"
    )


def profit_at_quantity(quantity: float, fixed_cost: float,
                       price_per_unit: float,
                       variable_cost_per_unit: float) -> str:
    """給定銷量，算營收、總成本、利潤、利潤率。

    Args:
        quantity: 銷售/生產數量。
        fixed_cost: 固定成本總額。
        price_per_unit: 單位售價。
        variable_cost_per_unit: 單位變動成本。
    Returns:
        Revenue / Total Cost / Profit / Profit Margin 一覽。
    """
    if quantity < 0:
        return "❌ 數量不能為負。"
    revenue = price_per_unit * quantity
    variable_total = variable_cost_per_unit * quantity
    total_cost = fixed_cost + variable_total
    profit = revenue - total_cost
    margin = (profit / revenue * 100) if revenue else float("nan")
    margin_str = f"{_fmt(margin, 2)}%" if revenue else "N/A（營收為 0）"
    return (
        f"📊 在數量 {_fmt(quantity)} 下：\n"
        f"  營收 Revenue = 價×量 = {_fmt(price_per_unit)} × {_fmt(quantity)} = {_fmt(revenue)}\n"
        f"  總成本 Total Cost = 固定 {_fmt(fixed_cost)} + 變動 {_fmt(variable_cost_per_unit)}×{_fmt(quantity)} "
        f"= {_fmt(total_cost)}\n"
        f"  利潤 Profit = 營收 − 總成本 = {_fmt(profit)}\n"
        f"  利潤率 Profit Margin = {margin_str}\n"
        f"  {'✅ 賺錢' if profit > 0 else ('⚖️ 剛好打平' if profit == 0 else '🔴 虧損')}"
    )


# ────────────────────────────────────────────────────────────────────
# 2. 需求價格彈性（中點法）
# ────────────────────────────────────────────────────────────────────
def price_elasticity(price_old: float, qty_old: float,
                     price_new: float, qty_new: float) -> str:
    """用中點法（arc / midpoint）算需求的價格彈性，並判斷對營收的影響。

    用在大王問「降價能不能讓總營收變多」、「我的客人對價格敏不敏感」。

    Args:
        price_old: 變動前價格。
        qty_old: 變動前銷量。
        price_new: 變動後價格。
        qty_new: 變動後銷量。
    Returns:
        彈性係數 Ed、彈性分類（彈性/缺乏彈性/單位彈性）、對總營收的方向。
    """
    if min(price_old, price_new, qty_old, qty_new) < 0:
        return "❌ 價格與數量不能為負。"
    dq = qty_new - qty_old
    dp = price_new - price_old
    q_avg = (qty_new + qty_old) / 2
    p_avg = (price_new + price_old) / 2
    if q_avg == 0 or p_avg == 0:
        return "❌ 平均價格或平均數量為 0，無法計算彈性。"
    if dp == 0:
        return "❌ 價格沒有變動（Δ價格 = 0），無法計算價格彈性。"
    pct_q = dq / q_avg
    pct_p = dp / p_avg
    ed = pct_q / pct_p
    abs_ed = abs(ed)
    if abs_ed > 1:
        kind = "有彈性 Elastic（|Ed|>1，量對價敏感）"
        rev_hint = "降價會讓總營收上升、漲價會讓總營收下降"
    elif abs_ed < 1:
        kind = "缺乏彈性 Inelastic（|Ed|<1，量對價不敏感）"
        rev_hint = "漲價會讓總營收上升、降價會讓總營收下降"
    else:
        kind = "單位彈性 Unit elastic（|Ed|=1）"
        rev_hint = "價格小幅變動下總營收大致不變"
    return (
        "📊 需求價格彈性（中點法 arc elasticity）\n"
        f"  %Δ量 = {_fmt(pct_q * 100, 2)}%，%Δ價 = {_fmt(pct_p * 100, 2)}%\n"
        f"  Ed = %Δ量 ÷ %Δ價 = {_fmt(ed, 3)}（|Ed| = {_fmt(abs_ed, 3)}）\n"
        f"  分類：{kind}\n"
        f"  對總營收：{rev_hint}\n"
        "  ⚠️ 這是兩點間的弧彈性，假設其他條件不變（競品、所得、季節）。"
        "對利潤的結論還要看邊際成本與貢獻邊際，不只看營收。"
    )


# ────────────────────────────────────────────────────────────────────
# 3. NPV / 回收期（投資評估）
# ────────────────────────────────────────────────────────────────────
def npv_analysis(annual_rate_percent: float, cashflows: str) -> str:
    """算淨現值 NPV 與簡單回收期（Payback）。

    用在大王問「買這台機器/開這條線划不划算」。

    Args:
        annual_rate_percent: 折現率（年化，%，例如 8 代表 8%）。
        cashflows: 逗號分隔的現金流，**第 0 個是期初（通常是負的投入）**，
                   之後每期一個。例如 "-100000, 30000, 40000, 50000, 60000"。
    Returns:
        各期折現值、NPV、是否值得（NPV>0）、簡單回收期。
    """
    try:
        flows = _parse_numbers(cashflows)
    except ValueError as e:
        return f"❌ cashflows 解析失敗：{e}（範例：-100000, 30000, 40000, 50000）"
    if len(flows) < 2:
        return "❌ 至少要 2 期（期初投入 + 至少 1 期回收）。"
    r = annual_rate_percent / 100.0
    if r <= -1:
        return "❌ 折現率不合理（≤ −100%）。"
    npv = 0.0
    lines = []
    cumulative = 0.0
    payback_period = None
    for t, cf in enumerate(flows):
        pv = cf / ((1 + r) ** t)
        npv += pv
        cumulative += cf
        if payback_period is None and t > 0 and cumulative >= 0:
            payback_period = t
        lines.append(f"    t={t}: 現金流 {_fmt(cf)} → 折現值 {_fmt(pv, 2)}")
    verdict = ("✅ NPV > 0：在此折現率下值得投資" if npv > 0
               else ("⚖️ NPV = 0：剛好打平折現率門檻" if npv == 0
                     else "🔴 NPV < 0：在此折現率下不值得"))
    payback_str = (f"{payback_period} 期（簡單回收，未折現）" if payback_period
                   else "在提供的期數內未回本")
    return (
        f"📊 投資評估（折現率 {_fmt(annual_rate_percent, 2)}%）\n"
        + "\n".join(lines) + "\n"
        f"  NPV = Σ 折現值 = {_fmt(npv, 2)}\n"
        f"  {verdict}\n"
        f"  簡單回收期 Payback ≈ {payback_str}\n"
        "  ⚠️ NPV 對折現率敏感；折現率是假設，建議用幾個情境（保守/基準/樂觀）各算一次。"
    )


# ────────────────────────────────────────────────────────────────────
# 4. 年複合成長率 CAGR
# ────────────────────────────────────────────────────────────────────
def cagr(begin_value: float, end_value: float, periods: float) -> str:
    """算年複合成長率（CAGR, Compound Annual Growth Rate）。

    用在大王問「這幾年營收平均一年成長幾 %」。

    Args:
        begin_value: 期初值（> 0）。
        end_value: 期末值（> 0）。
        periods: 期數（年數，> 0）。
    Returns:
        CAGR 百分比與一句解讀。
    """
    if begin_value <= 0 or end_value <= 0:
        return "❌ 期初值與期末值都必須 > 0 才能算 CAGR。"
    if periods <= 0:
        return "❌ 期數必須 > 0。"
    growth = (end_value / begin_value) ** (1.0 / periods) - 1.0
    total = end_value / begin_value - 1.0
    return (
        "📊 年複合成長率 CAGR\n"
        f"  公式：(期末 ÷ 期初)^(1/期數) − 1 = ({_fmt(end_value)} ÷ {_fmt(begin_value)})"
        f"^(1/{_fmt(periods)}) − 1\n"
        f"  CAGR = {_fmt(growth * 100, 2)}%/期（總成長 {_fmt(total * 100, 2)}%）\n"
        "  解讀：這是「平滑後」的固定成長率，會掩蓋中間的波動與單一爆衝年份。"
    )


# ────────────────────────────────────────────────────────────────────
# 5. 敘述統計
# ────────────────────────────────────────────────────────────────────
def descriptive_stats(numbers: str) -> str:
    """算一組數字的敘述統計（Descriptive statistics）。

    用在大王丟一串數字（日產量、不良數、報價…）問「平均多少、分散程度如何」。

    Args:
        numbers: 逗號/空白分隔的數字，例如 "120, 135, 128, 142, 119"。
    Returns:
        n、平均、中位數、樣本標準差、最小/最大/全距、四分位數、IQR、變異係數。
    """
    try:
        data = _parse_numbers(numbers)
    except ValueError as e:
        return f"❌ 數字解析失敗：{e}"
    n = len(data)
    mean = statistics.mean(data)
    median = statistics.median(data)
    sd = statistics.stdev(data) if n >= 2 else float("nan")
    lo, hi = min(data), max(data)
    cv = (sd / mean * 100) if (n >= 2 and mean != 0) else float("nan")
    # 四分位數（exclusive；n<2 時 statistics.quantiles 會 raise）
    if n >= 4:
        q1, _, q3 = statistics.quantiles(data, n=4, method="exclusive")
        iqr_line = (f"  Q1 / Q3 = {_fmt(q1, 2)} / {_fmt(q3, 2)}，"
                    f"IQR = {_fmt(q3 - q1, 2)}\n")
    else:
        iqr_line = "  （資料點 < 4，略過四分位數）\n"
    sd_str = _fmt(sd, 4) if n >= 2 else "N/A（需 ≥ 2 點）"
    cv_str = f"{_fmt(cv, 2)}%" if (n >= 2 and mean != 0) else "N/A"
    return (
        f"📊 敘述統計（n = {n}）\n"
        f"  平均 Mean = {_fmt(mean, 4)}，中位數 Median = {_fmt(median, 4)}\n"
        f"  樣本標準差 Sample SD = {sd_str}\n"
        f"  最小 / 最大 = {_fmt(lo)} / {_fmt(hi)}，全距 Range = {_fmt(hi - lo)}\n"
        + iqr_line +
        f"  變異係數 CV = {cv_str}（越大代表相對離散越高）\n"
        "  ⚠️ 這是「描述」這組資料本身；要推論到母體需另談抽樣與信賴區間。"
    )


# ────────────────────────────────────────────────────────────────────
# 6. 簡單線性迴歸（OLS）
# ────────────────────────────────────────────────────────────────────
def simple_linear_regression(x_values: str, y_values: str) -> str:
    """對兩組等長數字做簡單線性迴歸（OLS）：y = b0 + b1·x。

    用在大王問「廣告花費 vs 銷量有沒有關係」、「溫度 vs 退貨率」這類兩變數關係。

    Args:
        x_values: 自變數 x，逗號/空白分隔。
        y_values: 應變數 y，逗號/空白分隔，長度需與 x 相同。
    Returns:
        斜率 b1、截距 b0、R²、Pearson r、樣本數，加白話解讀與因果警告。
    """
    try:
        xs = _parse_numbers(x_values)
        ys = _parse_numbers(y_values)
    except ValueError as e:
        return f"❌ 數字解析失敗：{e}"
    if len(xs) != len(ys):
        return f"❌ x（{len(xs)} 筆）與 y（{len(ys)} 筆）長度必須相同。"
    n = len(xs)
    if n < 3:
        return "❌ 至少要 3 組 (x, y) 點，迴歸才有意義。"
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return "❌ 所有 x 都相同（無變異），無法配適斜率。"
    b1 = sxy / sxx
    b0 = my - b1 * mx
    if syy == 0:
        r = float("nan")
        r2 = float("nan")
        fit_note = "（y 沒有變異，R²/r 無意義）"
    else:
        r = sxy / math.sqrt(sxx * syy)
        r2 = r ** 2
        fit_note = ""
    sign = "正" if b1 > 0 else ("負" if b1 < 0 else "無")
    return (
        f"📊 簡單線性迴歸 OLS（n = {n}）\n"
        f"  模型：y = b0 + b1·x\n"
        f"  斜率 b1 = {_fmt(b1, 4)}（x 每增加 1，y 平均{sign}向變動 {_fmt(b1, 4)}）\n"
        f"  截距 b0 = {_fmt(b0, 4)}\n"
        f"  Pearson r = {_fmt(r, 4)}，R² = {_fmt(r2, 4)} {fit_note}\n"
        f"  解讀：R² 代表 x 能解釋 y 變異的比例（{_fmt((r2 if r2 == r2 else 0) * 100, 1)}%）。\n"
        "  🛑 相關 ≠ 因果：這只是配適出的關聯，不代表 x 造成 y。\n"
        "  ⚠️ 還要看殘差、離群值、樣本量、是否遺漏重要變數，再下結論。"
    )


# 全部都是純計算、無副作用 → 背景排程任務也能安全呼叫
for _f in (break_even_analysis, profit_at_quantity, price_elasticity,
           npv_analysis, cagr, descriptive_stats, simple_linear_regression):
    _f.background_safe = True

SKILL_TOOLS = [
    break_even_analysis,
    profit_at_quantity,
    price_elasticity,
    npv_analysis,
    cagr,
    descriptive_stats,
    simple_linear_regression,
]
