"""應付帳款對帳/帳齡 — 對帳引擎（payable 欠款 ↔ remittance 付款，金額錨定配對）。

為何金額錨定：同一供應商在不同單據用不同名稱（付款請示單寫中文全名「南亞塑膠工業(南通)有限公司」、
匯款水單 OCR 出「NAN YA PLASTICS」），counterparty_norm（只處理大小寫/標點）橋不了 → 名稱配不上。
但**金額是精確共同鍵**（南亞 4959.66 在兩種單據一模一樣），故用 (幣別+金額) 配對、繞過名稱問題。

模型：
  - payable    (付款請示單/應付申請) = 已核可的應付款（欠，owe-side）
  - remittance (匯款水單)            = 已匯出的款（付，paid-side）
  每張 payable 找一張同幣別同金額的 remittance → 標「已付」；配不到 → open AP（未付）。
  **未付的 payable 依其自身供應商名（全名、一致）分組 + 依日期帳齡分桶**——配對用金額、顯示用名稱。

⚠️ 誠實前提：
  - 只涵蓋已 ingest + 抽到金額的單據（涵蓋率隨 backfill 漸增）；
  - 一張匯款沖多張 payable（合併付款）會讓那些 payable 誤判 open（金額對不上單筆）；
  - invoice 型不納入（混了銷貨/關係企業，待角色分流）。

leaf：只 import 標準庫，純函式可單元測試（as_of_date 由 caller 傳，決定性）。
"""
from collections import defaultdict
from datetime import date, datetime

_OWE_TYPES = {"payable"}
_PAID_TYPES = {"remittance"}
_AGING_BUCKETS = ("0-30", "31-60", "61-90", "90+", "undated")
# 自家公司：付款請示單由佳桀/佳紘開立，抽取偶把表頭自家公司當對方 → 那筆對方抽錯，略過
# （不是真的欠自己）。福群/佳捷是關係企業、可能有真實往來，故不在此列。
_OWN_COMPANY_HINTS = ("佳桀", "佳紘")


def _parse_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _coerce_amount(v):
    if v in (None, ""):
        return None
    if isinstance(v, str):
        v = v.replace(",", "").replace(" ", "").strip()
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _bucket(aging, d, amount, as_of):
    if d is None:
        aging["undated"] += amount
        return
    age = (as_of - d).days
    if age <= 30:
        aging["0-30"] += amount
    elif age <= 60:
        aging["31-60"] += amount
    elif age <= 90:
        aging["61-90"] += amount
    else:
        aging["90+"] += amount


_AGG_MAX_SUBSET = 3      # 一張匯款最多沖幾張請示單（合併付款多為 2-3 LOT）
_AGG_POOL_CAP = 12       # 單一供應商組內參與組合的候選上限（控組合爆炸）


def _aggregate_match(owe, paid, tol):
    """合併付款配對：一張未配匯款 ↔ 同一供應商多張未配請示單之子集和（±tol）。回配對的匯款數。

    安全設計：子集**限同一 payable 供應商組**（不跨供應商湊數）、大小 2.._AGG_MAX_SUBSET、
    組內候選上限。大額匯款先配（合併款多為大額）；同組多解取「和最接近、張數最少」。
    """
    from itertools import combinations

    groups = defaultdict(list)
    for o in owe:
        if not o["paid"]:
            groups[(o["cpn"], o["currency"])].append(o)

    n = 0
    for r in sorted(paid, key=lambda x: x["amount"], reverse=True):
        if r["paid"]:
            continue
        best = None   # (sum_diff, subset_size, combo)
        for (_cpn, cur), grp in groups.items():
            if cur != r["currency"]:
                continue
            avail = [o for o in grp if not o["paid"]
                     and (r["date"] is None or o["date"] is None or o["date"] <= r["date"])]
            if len(avail) < 2:
                continue
            cap = sorted(avail, key=lambda x: (x["date"] is None, x["date"] or date.max))[:_AGG_POOL_CAP]
            for size in range(2, _AGG_MAX_SUBSET + 1):
                for combo in combinations(cap, size):
                    diff = abs(sum(o["amount"] for o in combo) - r["amount"])
                    if diff <= tol and (best is None or (diff, size) < (best[0], best[1])):
                        best = (diff, size, combo)
        if best:
            for o in best[2]:
                o["paid"] = True
                o["match"] = "aggregated"
            r["paid"] = True
            n += 1
    return n


def reconcile_payments(rows, as_of_date=None, match_tolerance=20.0):
    """rows=單據列（dict：doc_type/amount/currency/counterparty(_norm)/doc_date）。

    match_tolerance：匯款金額容差（請款額 vs 匯出額差 ~$15-20 扣匯費；0=只精確配）。
    回 dict：suppliers（未付餘額由大到小）+ 配對統計 + 略過統計。as_of_date='YYYY-MM-DD'（不給=今天）。
    """
    as_of = _parse_date(as_of_date) or date.today()
    tol = max(0.0, float(match_tolerance))

    owe, paid = [], []
    skipped_no_amount = skipped_own_company = 0
    for r in rows:
        amt = _coerce_amount(r.get("amount"))
        dt = str(r.get("doc_type") or "").strip()
        if dt not in _OWE_TYPES and dt not in _PAID_TYPES:
            continue
        if amt is None:
            skipped_no_amount += 1
            continue
        cp_text = f"{r.get('counterparty') or ''}{r.get('counterparty_norm') or ''}"
        if dt in _OWE_TYPES and any(h in cp_text for h in _OWN_COMPANY_HINTS):
            skipped_own_company += 1   # 對方抽成自家公司＝抽錯，不計入應付
            continue
        item = {
            "amount": amt,
            "currency": str(r.get("currency") or "").strip() or "?",
            "date": _parse_date(r.get("doc_date")),
            "cp": str(r.get("counterparty") or r.get("counterparty_norm") or "?").strip() or "?",
            "cpn": str(r.get("counterparty_norm") or r.get("counterparty") or "?").strip() or "?",
            "paid": False,
        }
        (owe if dt in _OWE_TYPES else paid).append(item)

    # 匯款依幣別分組（每張只能沖一次）。
    pool = defaultdict(list)
    for p in paid:
        pool[p["currency"]].append(p)

    # 第一輪：1 對 1（payable ↔ 單張匯款，幣別+金額±容差）。payable 由舊到新先配。
    for o in sorted(owe, key=lambda x: (x["date"] is None, x["date"] or date.max)):
        cands = [x for x in pool.get(o["currency"], [])
                 if not x["paid"] and abs(x["amount"] - o["amount"]) <= tol]
        if not cands:
            continue

        # 偏好：金額最接近 → 匯款日>=應付日（付款在核可後）→ 日期最近
        def _score(x, _o=o):
            amt_diff = abs(x["amount"] - _o["amount"])
            after = bool(x["date"] and _o["date"] and x["date"] >= _o["date"])
            day_gap = abs((x["date"] - _o["date"]).days) if (x["date"] and _o["date"]) else 99999
            return (round(amt_diff, 2), 0 if after else 1, day_gap)

        pick = min(cands, key=_score)
        pick["paid"] = True
        o["paid"] = True
        o["match"] = "exact" if abs(pick["amount"] - o["amount"]) < 0.02 else "fee"

    # 第二輪：合併付款（一張匯款沖**同一供應商**多張請示單）。只在 payable 自身供應商組內
    # 找子集和≈匯款額（±容差）——絕不跨供應商湊數，把偽配對壓到最低。
    _aggregate_match(owe, paid, tol)

    matched = sum(1 for o in owe if o["paid"])
    matched_exact = sum(1 for o in owe if o.get("match") == "exact")
    matched_aggregated = sum(1 for o in owe if o.get("match") == "aggregated")

    # 未付的 payable 依「自身供應商名 + 幣別」分組 + 帳齡
    groups = defaultdict(lambda: {"display": "", "n_total": 0, "n_open": 0,
                                  "payable_total": 0.0, "paid_total": 0.0,
                                  "open_total": 0.0, "oldest_open": None,
                                  "aging": dict.fromkeys(_AGING_BUCKETS, 0.0)})
    for o in owe:
        g = groups[(o["cpn"], o["currency"])]
        if not g["display"]:
            g["display"] = o["cp"]
        g["n_total"] += 1
        g["payable_total"] += o["amount"]
        if o["paid"]:
            g["paid_total"] += o["amount"]
        else:
            g["n_open"] += 1
            g["open_total"] += o["amount"]
            if o["date"] and (g["oldest_open"] is None or o["date"] < g["oldest_open"]):
                g["oldest_open"] = o["date"]
            _bucket(g["aging"], o["date"], o["amount"], as_of)

    suppliers = []
    for (cpn, cur), g in groups.items():
        suppliers.append({
            "counterparty": g["display"], "counterparty_norm": cpn, "currency": cur,
            "n_payables": g["n_total"], "payable_total": round(g["payable_total"], 2),
            "n_paid": g["n_total"] - g["n_open"], "paid_total": round(g["paid_total"], 2),
            "n_open": g["n_open"], "open_balance": round(g["open_total"], 2),
            "oldest_open": g["oldest_open"].isoformat() if g["oldest_open"] else "",
            "aging": {k: round(v, 2) for k, v in g["aging"].items()},
        })
    suppliers.sort(key=lambda s: s["open_balance"], reverse=True)

    n_payments_matched = sum(1 for p in paid if p["paid"])
    return {
        "as_of": as_of.isoformat(),
        "suppliers": suppliers,
        "n_payables": len(owe),
        "n_payables_matched_paid": matched,
        "n_payables_matched_exact": matched_exact,   # 其餘為 ±容差(扣匯費)或合併付款
        "n_payables_matched_aggregated": matched_aggregated,
        "n_payments": len(paid),
        "n_payments_unmatched": len(paid) - n_payments_matched,
        "skipped_no_amount": skipped_no_amount,
        "skipped_own_company": skipped_own_company,
    }
