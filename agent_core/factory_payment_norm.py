"""付款單據欄位正規化 + 檔名回填（leaf util；建倉 _load_payments 與抽取共用）。

解兩個讓 fact_payment「查得到卻算不準」的問題：

  1. **沒正規化** → 'USD' / 'US$'、'JAI JYE CORPORATION' / 'Jai Jye Corporation' 並存，
     SUM/GROUP BY 會分裂、同一對方／幣別被算成好幾筆。
  2. **匯款單常是掃描檔/手機拍**，PDF 無文字層、內容抽不到金額；但員工把
     金額/幣別/對方/日期全寫在檔名（'佳桀-匯款-MASTROTTO EUR16,006.89-LOT 310-2026--20260327.pdf'）
     → 檔名是免費、可靠的回填來源（實測匯款單 ~23% 金額只在檔名）。

`enrich_payment_record(rec)` 是單一咽喉：① 正規化 currency／counterparty
② 內容欄位為空時以檔名標題回填 amount/currency/doc_date/counterparty
③ 標 `amount_source`（content=模型抽到／filename=檔名 regex 回填）。
**冪等**——每次全量重建倉都對整份 store 重跑，故能回溯修好現有已抽記錄、免重抽。

只 import 標準庫（leaf 規則，見 CLAUDE.md）。
"""
import re

# ── 幣別正規化 ─────────────────────────────────────────────────────────────
# 變體（清掉空白、轉大寫後比對）→ ISO 代碼。涵蓋實際 store 看到的寫法。
_CURRENCY_RULES = [
    ({"USD", "US$", "US", "USD$", "美金", "美元", "US＄"}, "USD"),
    ({"EUR", "EUR$", "EURO", "€", "歐元"}, "EUR"),
    # ¥(U+00A5)/￥(U+FFE5) 形式上 yen，但本廠無日圓交易、料件供應商多為中國 → 視為人民幣。
    ({"CNY", "RMB", "人民幣", "￥", "¥", "CNY￥", "人民币"}, "CNY"),
    ({"TWD", "NT$", "NTD", "NT", "台幣", "新台幣", "新臺幣", "NT＄"}, "TWD"),
    ({"JPY", "日元", "日圓", "日幣", "円"}, "JPY"),
    ({"GBP", "£", "英鎊"}, "GBP"),
    ({"HKD", "HK$", "港幣"}, "HKD"),
]
_CURRENCY_LOOKUP = {v: code for variants, code in _CURRENCY_RULES for v in variants}


def normalize_currency(s):
    """幣別字串 → ISO 代碼（USD/EUR/CNY/TWD/JPY…）。認不得就回清過的大寫原值（不臆測）。"""
    if not s:
        return ""
    t = re.sub(r"\s+", "", str(s)).upper()
    if not t:
        return ""
    if t in _CURRENCY_LOOKUP:
        return _CURRENCY_LOOKUP[t]
    stripped = t.rstrip("$.＄ 　")
    if stripped in _CURRENCY_LOOKUP:
        return _CURRENCY_LOOKUP[stripped]
    return t[:8]


# ── 對方正規化 ─────────────────────────────────────────────────────────────
# 已知同一實體的別名（左→正規顯示名）。保守只放確定的；中英對照風險高，缺證據不放。
_COUNTERPARTY_ALIASES = {
    "JAI JYE CORP": "JAI JYE CORPORATION",
    "FU CHUN CORP": "FU CHUN CORPORATION",
}


def normalize_counterparty(s):
    """對方名 → 合併大小寫/空白/標點變體的正規化鍵（給 GROUP BY/SUM 用，顯示仍用原 counterparty）。

    'Jai Jye Corporation' / 'JAI JYE CORPORATION' → 同鍵；'DAESUNG CO.,LTD.' /
    'DAESUNG CO., LTD.' → 同鍵。不做中英對照（無對照表、易誤併）。
    """
    if not s:
        return ""
    t = str(s).strip().strip("\"'，,。.、 　")
    t = re.sub(r"\s+", " ", t)            # 多空白收成一個
    t = re.sub(r"\s*,\s*", ", ", t)        # 逗號間距統一（CO.,LTD. ↔ CO., LTD.）
    key = t.upper()
    return _COUNTERPARTY_ALIASES.get(key, key)


# ── 檔名回填（零成本、零 OCR） ──────────────────────────────────────────────
# 金額：幣別符號/碼緊跟數字（容千分位逗號）。多筆時偏好 TOTAL 後者、否則取最大（總額≥分項）。
_TITLE_AMT_RE = re.compile(
    r"(US\$|USD|EUR|RMB|CNY|NT\$|NTD|TWD|JPY|HKD|GBP|€|£)\s*"
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_TITLE_TOTAL_RE = re.compile(r"(TOTAL|總額|總計|合計)", re.IGNORECASE)
# 日期：YYYYMMDD（可夾 - / .），月 01-12、日 01-31，後面不接數字（避免吃進更長數字串）。
_TITLE_DATE_RE = re.compile(
    r"(20[12]\d)[-/.]?(0[1-9]|1[0-2])[-/.]?(0[1-9]|[12]\d|3[01])(?!\d)"
)
# 對方：'…匯款-{對方}{幣別/LOT/數字}'（佳桀-匯款- 是本廠自己，對方在其後）。
_TITLE_CP_RE = re.compile(
    r"匯款[-_\s]+([A-Za-z一-鿿][\w一-鿿()（）]{0,18}?)\s*"
    r"(?=US\$|USD|EUR|RMB|CNY|NT\$|NTD|TWD|JPY|HKD|GBP|€|£|LOT|[0-9]|$)",
    re.IGNORECASE,
)
_TITLE_CP_NOISE = {"匯款", "匯款單", "通知單", "通知", "水單", "水", "單", "佳桀",
                   "REMITTANCE", "INVOICE", "RECEIPT", "PAYMENT"}


def _amount_of(match):
    try:
        return float(match.group(2).replace(",", ""))
    except (ValueError, IndexError):
        return None


def parse_amount_from_title(title):
    """從檔名抽 (amount, currency_iso)；抽不到回 (None, '')。多金額時取總額（TOTAL 後／最大）。"""
    t = str(title or "")
    matches = list(_TITLE_AMT_RE.finditer(t))
    if not matches:
        return None, ""
    chosen = None
    tot = _TITLE_TOTAL_RE.search(t)
    if tot:
        after = [m for m in matches if m.start() >= tot.end()]
        if after:
            chosen = after[0]
    if chosen is None:
        chosen = max(matches, key=lambda m: _amount_of(m) or 0.0)
    return _amount_of(chosen), normalize_currency(chosen.group(1))


def parse_date_from_title(title):
    """從檔名抽 'YYYY-MM-DD'；抽不到回 ''。"""
    m = _TITLE_DATE_RE.search(str(title or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def parse_counterparty_from_title(title):
    """從檔名的 '匯款-{對方}' 樣式保守抽對方；雜訊詞/純數字/抽不到回 ''。"""
    m = _TITLE_CP_RE.search(str(title or ""))
    if not m:
        return ""
    cp = m.group(1).strip(" -_、")
    if not cp or cp in _TITLE_CP_NOISE or cp.upper() in _TITLE_CP_NOISE or cp.isdigit():
        return ""
    return cp


def _coerce_amount(v):
    """內容抽到的 amount → float|None。容千分位逗號/空白（'16,006.89' 不再變 null）。"""
    if v in (None, ""):
        return None
    if isinstance(v, str):
        v = v.replace(",", "").replace(" ", "").strip()
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def enrich_payment_record(rec):
    """正規化 + 檔名回填 + 標 amount_source。回新 dict（不改原物件），冪等。

    amount/currency/doc_date/counterparty：先用內容抽取值，為空才以檔名回填。
    amount_source：'content'（模型抽到）/ 'filename'（檔名 regex 回填）/ ''（都沒有）。
    額外輸出 counterparty_norm（GROUP BY/SUM 用的正規化鍵）。
    """
    out = dict(rec)
    title = out.get("title") or ""

    out["currency"] = normalize_currency(out.get("currency"))

    amt = _coerce_amount(out.get("amount"))
    source = "content" if amt is not None else ""
    if amt is None:
        f_amt, f_cur = parse_amount_from_title(title)
        if f_amt is not None:
            amt = f_amt
            source = "filename"
            if not out["currency"]:
                out["currency"] = f_cur
    out["amount"] = amt
    out["amount_source"] = source

    if not str(out.get("doc_date") or "").strip():
        d = parse_date_from_title(title)
        if d:
            out["doc_date"] = d

    cp = str(out.get("counterparty") or "").strip()
    if not cp:
        cp = parse_counterparty_from_title(title)
    out["counterparty"] = cp
    out["counterparty_norm"] = normalize_counterparty(cp)
    return out
