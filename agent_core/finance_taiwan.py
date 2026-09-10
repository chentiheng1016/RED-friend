"""台灣端金流台帳（email 證據）＋ 台越合併損益視圖 —— 財務長合併損益第一步。

背景（2026-09-07，大王）：台灣佳桀/佳紘的帳不在 ERP（CJ 帳套 2024 底就停、
零傳票），但金流證據散在 email：第一銀行匯入匯款（客戶貨款）、媒體劃撥代繳
（勞保/勞退/健保）、採購付款水單、中華電信發票、富邦轉帳通知（薪資鏈）。
這支把它們解析成本地台帳，湊出台灣端「現金收付制」的月收支，再跟福群 ERP
關帳損益並排成合併視圖。

## 口徑與誠實邊界（輸出必帶）

- **這是現金收付制的 email 證據彙總，不是帳冊**：只有寄進 email 的金流才看
  得到。實測缺口（lake 普查 2026-09-07）：薪資本體（富邦通知最新只到
  2025-06）、房租、水電、營業稅/營所稅、快遞行請款 —— 這些要嘛設定銀行/廠商
  改寄通知到 owner@，要嘛人工補數。
- **lake 的 body 只留 500 字**（媒體劃撥的批次表會被截斷）→ 一律走 Gmail API
  讀全文（複用 payment_notice 的 service/HTML 轉文字/欄位解析）。
- **匯入款 ≠ 全是營收**（payment_notice 實測）：混著關係企業（FU CHUN）內部
  調撥、信保退費、退稅。台帳逐筆標 interco / 非營業，營收合計不吃它們。
- **不硬換匯**：TWD/USD 分列；美金參考換算用 fx_rates 本地歷史（匯率推播
  每天存兩次），沒有歷史檔就不換。合併視圖同理：福群 VND 用 GL_EXCHANGE、
  台灣 TWD 用 fx 歷史，都標明匯率與日期。

## 台帳存哪

``var/data/taiwan_cash_ledger.json``：``{"entries": {state_key: entry}}``，
state_key = RFC Message-ID（跨信箱去重）或 ``mailbox:gmail_id``。sync 是
增量的：已見過的 key 直接跳過，Gmail 只掃 ``newer_than:Nd``。

零幻覺：全部確定性 parser；解析不出金額的信照樣入帳（amount=None，只計
筆數）——寧可標「有 N 筆解不出」也不猜數字。財務全貌敏感，不進員工白名單。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Callable

from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text, logger
from agent_core.prompt_injection import sanitize_for_llm

_LEDGER_PATH = os.path.join(DATA_DIR, "taiwan_cash_ledger.json")

# 收入類 category（合計時走營收面；interco/非營業另列）
_INCOME_CATS = ("客戶匯入款", "出口託收", "其他匯入款")
# 餘額快照/參考件類：不是現金流，月表用 💼 顯示、不進收支語意。
# 電費憑證＝台電 e-Bill 收據 —— 錢是走一銀媒體劃撥扣的（批次列才是現金事實，
# 實測兩邊金額逐筆相同），憑證留參考（電號/期別）避免雙算。
_SNAPSHOT_CATS = ("國泰活存餘額", "電費憑證(參考)")

# 關係企業匯款人 → interco（不算營收也不算費用，合併時要對消）
_INTERCO_RE = re.compile(r"FU\s*CHUN|FUCHUN|JAI\s*FUNG|JAI\s*JYE|佳桀|佳紘|福群|富群", re.I)
# 非營業匯入（信保退費/退稅這類）
_NONOP_RE = re.compile(r"信保|信用保證|退稅|國稅局|財政部")

_AMOUNT_RE = re.compile(
    r"(?P<cur>US\$|USD|NT\$|NTD|TWD|EUR|CNY|RMB|VND|€)\s*(?P<amt>[\d,]+(?:\.\d+)?)",
    re.I)
_CUR_NORM = {"US$": "USD", "NT$": "TWD", "NTD": "TWD", "RMB": "CNY", "€": "EUR"}

_COMPANY_MAP = (("JAI JYE", "佳桀"), ("佳桀", "佳桀"),
                ("JAI FUNG", "佳紘"), ("佳紘", "佳紘"))


def _pn():
    from agent_core import payment_notice
    return payment_notice


# ────────────────────────────────────────────────────────────────────
# 各串流的 parser（純函式，可單測）：msg dict → list[entry]
# msg = {"key","date","subject","sender","body"}；entry 見 module docstring
# ────────────────────────────────────────────────────────────────────

def _norm_amount(raw: str) -> float | None:
    try:
        v = float(str(raw).replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _company_of(text: str) -> str:
    for pat, name in _COMPANY_MAP:
        if pat in (text or "").upper() or pat in (text or ""):
            return name
    return "台灣"


def _base(msg: dict, **kw) -> dict:
    entry = {
        "date": str(msg.get("date") or "")[:10],
        "company": "台灣", "category": "", "currency": "", "amount": None,
        "counterparty": "", "ref": "", "interco": False,
        "subject": sanitize_for_llm(str(msg.get("subject") or ""))[:120],
    }
    entry.update(kw)
    return entry


def parse_fcb_foreign(msg: dict) -> list[dict]:
    f = _pn()._parse_fcb_foreign(msg["body"])
    if not f.get("amount"):
        return [_base(msg, category="客戶匯入款")]
    remitter = f.get("remitter", "")
    interco = bool(_INTERCO_RE.search(remitter))
    cat = ("其他匯入款" if _NONOP_RE.search(remitter or "") else "客戶匯入款")
    return [_base(
        msg, category=cat, currency=(f.get("currency") or "").upper() or "USD",
        amount=_norm_amount(f.get("amount")),
        counterparty=sanitize_for_llm(remitter)[:60],
        ref=sanitize_for_llm(f.get("remark") or f.get("ref") or "")[:80],
        company=_company_of(f.get("payee", "")),
        date=(f.get("value_date") or "").replace("/", "-")[:10] or _base(msg)["date"],
        interco=interco,
    )]


def parse_fcb_domestic(msg: dict) -> list[dict]:
    f = _pn()._parse_fcb_domestic(msg["body"])
    if not f.get("amount"):
        return [_base(msg, category="其他匯入款")]
    remitter = f.get("remitter", "")
    return [_base(
        msg, category=("其他匯入款" if _NONOP_RE.search(remitter + f.get("remark", ""))
                       else "客戶匯入款"),
        currency="TWD", amount=_norm_amount(f.get("amount")),
        counterparty=sanitize_for_llm(remitter)[:60],
        ref=sanitize_for_llm(f.get("remark") or "")[:80],
        date=(f.get("value_date") or "").replace("/", "-")[:10] or _base(msg)["date"],
        interco=bool(_INTERCO_RE.search(remitter)),
    )]


# 媒體劃撥批次列：`帳號 說明 交易日期 金額 …`（HTML 表格轉文字後）
_MEDIA_ROW = re.compile(
    r"(?P<acct>\d[\d*]{5,})\s+(?P<desc>\S{2,12}?)\s+"
    r"(?P<date>\d{4}/\d{1,2}/\d{1,2})\s+(?P<amt>[\d,]+(?:\.\d+)?)")
# 交易說明 → category（2026-09-08 大王指路水費後盤點整份批次的實際項目）。
# 「退」字頭（退營所稅 27,011 實測）是退稅入帳不是支出 —— 歸其他匯入款，
# 計成費用會把成本灌水。
_MEDIA_CAT = (("勞保", "勞健保費"), ("健保", "勞健保費"), ("勞退", "勞退提繳"),
              ("水費", "水費"), ("瓦斯", "瓦斯費"), ("電費", "電費"))


def parse_media_batch(msg: dict) -> list[dict]:
    out = []
    for m in _MEDIA_ROW.finditer(msg["body"]):
        desc = m.group("desc")
        if desc.startswith("退"):
            cat = "其他匯入款"
        else:
            cat = next((c for k, c in _MEDIA_CAT if k in desc), "代繳-其他")
        d = m.group("date").split("/")
        out.append(_base(
            msg, category=cat, currency="TWD", amount=_norm_amount(m.group("amt")),
            counterparty=sanitize_for_llm(desc)[:30],
            date=f"{int(d[0]):04d}-{int(d[1]):02d}-{int(d[2]):02d}",
        ))
    return out or [_base(msg, category="代繳-其他")]


def parse_purchase_remit(msg: dict) -> list[dict]:
    """採購付款水單：金額在主旨最常見（LOT 228-2025 – US$ 438.61, 已付款…）。

    主旨標「福群付款」的是越南端付的（UserA 只是轉知）→ interco，不算台灣
    費用（2026-09-08 實測 twaccounting@ 的水單有這種）。
    """
    subject = str(msg.get("subject") or "")
    text = f"{subject} {msg.get('body', '')[:800]}"
    m = _AMOUNT_RE.search(text)
    vn_paid = "福群付款" in subject or "福群支付" in subject
    kw = {
        "category": "採購付款(福群支付)" if vn_paid else "採購付款",
        "counterparty": "", "interco": vn_paid,
    }
    if m:
        cur = _CUR_NORM.get(m.group("cur").upper(), m.group("cur").upper())
        kw.update(currency=cur, amount=_norm_amount(m.group("amt")))
    return [_base(msg, **kw)]


def parse_cht_invoice(msg: dict) -> list[dict]:
    """中華電信電子發票：金額在 .htm 附件（Big5 編碼），「總計：5072」。

    2026-09-08 實測：body 只有公版說明，附件才有發票明細；附件是 text/html
    但 charset=cp950，用 utf-8 硬解會全變亂碼。
    """
    text = msg.get("body", "")
    for fn, blob in (msg.get("attachments") or []):
        try:
            text += "\n" + _pn()._html_to_text(blob.decode("cp950", errors="replace"))
        except Exception as exc:  # noqa: BLE001 —— 附件壞掉退回 body-only
            logger.warning("cht 附件 %s 解碼失敗: %s", fn, exc)
    m = re.search(r"(?:總計|金額|應繳金額|帳單金額)[：:\s]*(?:NT\$|新臺幣)?\s*([\d,]+)",
                  text)
    return [_base(msg, category="電信費", currency="TWD",
                  amount=_norm_amount(m.group(1)) if m else None,
                  counterparty="中華電信")]


# 發票號取自附件檔名（HNHOC2608073電子發票.pdf）或引文前正文。
_FWD_INV_NO = re.compile(r"([A-Z]{3,8}\d{6,12})")
# 引文起點：之後的金額是舊信引文，抽了會把同一張帳單抽 N 次。
_QUOTE_SPLIT = re.compile(r"寄件者:|From:")
# 電子發票 PDF：發票號碼（DE07829689）；千分位金額（費用明細 323 這種無
# 千分位小額不會誤中）。
_EINV_NO = re.compile(r"([A-Z]{2}\d{8})")
_EINV_AMTS = re.compile(r"\d{1,3}(?:,\d{3})+")


def _parse_forwarder_invoice_text(text: str) -> dict | None:
    """電子發票 PDF 文字 → {amount, ref}。

    pypdf 抽出的版面是欄位打散的（「11,554 / 銷售額合計 11,004 / 總計 / 550」
    順序亂跳），錨「總計」旁邊的數字不可靠。用發票的不變量：**總計＝頁面最大
    金額**（明細行金額恆 ≤ 合計、稅額 < 合計）。實測 HNHOC2608073：max=11,554
    ＝大寫「壹萬壹仟伍佰伍拾肆元整」✓。沒有「總計」字樣＝不是發票頁 → None。
    """
    if "總" not in text or "計" not in text:
        return None
    amts = [_norm_amount(a) for a in _EINV_AMTS.findall(text)]
    amts = [a for a in amts if a]
    if not amts:
        return None
    m = _EINV_NO.search(text)
    return {"amount": max(amts), "ref": m.group(1) if m else ""}


def parse_forwarder(msg: dict) -> list[dict]:
    """貨代/快遞（沛華等）：**只認「電子發票」的金額**，其餘一律計筆數。

    2026-09-09 全量掃過 owner@ 68 封＋UserA 37 封＋會計 22 封的教訓：帶
    NTD 金額的多半是**促銷報價**（「6月限時大特價 海運費USD 75/150」）——
    無差別抽金額會把報價當費用入帳。寧可漏、不可錯（payment_notice 同款
    教條）：主旨含「電子發票」或帶發票 PDF 的才是請款事實。金額只從引文前
    的正文抽（RE: 串會把同一張帳單引 N 次）。歷史帳單不在 email 在 Drive
    （freight_bills_from_drive）。
    """
    subject = str(msg.get("subject") or "")
    atts = msg.get("attachments") or []
    inv_att = next(((fn, blob) for fn, blob in atts
                    if "發票" in str(fn) and str(fn).lower().endswith(".pdf")), None)
    is_bill = "電子發票" in subject or inv_att is not None
    kw: dict[str, Any] = {"category": "貨代/快遞費", "counterparty": ""}
    if is_bill and inv_att is not None:
        # PDF 發票＝權威源（實測：信裡引文是 11,675 的更新帳單版、最終發票
        # 11,554 —— 抽引文會抽到舊金額）。
        try:
            import io

            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(inv_att[1]))
            if reader.is_encrypted:
                reader.decrypt(_tw_tax_id())
            parsed = _parse_forwarder_invoice_text(reader.pages[0].extract_text())
            if parsed:
                kw.update(currency="TWD", amount=parsed["amount"])
                if parsed["ref"]:
                    kw["ref"] = parsed["ref"]
        except Exception as exc:  # noqa: BLE001 —— PDF 壞掉退回 body/檔名
            logger.warning("沛華發票 PDF 解析失敗 %s: %s", inv_att[0], exc)
    if is_bill and not kw.get("amount"):
        head = _QUOTE_SPLIT.split(msg.get("body", ""))[0]
        m = _AMOUNT_RE.search(head)
        if m:
            cur = _CUR_NORM.get(m.group("cur").upper(), m.group("cur").upper())
            kw.update(currency=cur, amount=_norm_amount(m.group("amt")))
    if is_bill and not kw.get("ref"):
        rm = _FWD_INV_NO.search((inv_att[0] if inv_att else "")
                                or _QUOTE_SPLIT.split(msg.get("body", ""))[0])
        if rm:
            kw["ref"] = rm.group(1)
    return [_base(msg, **kw)]


# ── 台電 e-Bill 電費繳費憑證（加密 PDF，密碼＝統編）──────────────────

_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")
# 憑證頂部的入帳戳記「＊＊＊＊２９０３ 元」（全形、前綴遮罩）＝繳費總金額。
_TP_MASKED = re.compile(r"＊+\s*(\d[\d,]*)\s*元")
# 備援：「繳費總金額」後值區的千分位金額（實測 2,903 元 —— 明細值無千分位）。
_TP_TOTAL = re.compile(r"繳費總金額[\s\S]{0,200}?(\d{1,3}(?:,\d{3})+)\s*元")
_TP_PERIOD = re.compile(r"(\d{3})年(\d{2})月")
_TP_REF = re.compile(r"單據號碼[：:\s]*([A-Z0-9-]+)")
_TP_METER = re.compile(r"(\d{2}-\d{2}-\d{4}-\d{2}-\d)")


def _parse_taipower_text(text: str) -> dict | None:
    """繳費憑證第一頁文字 → {amount, date, ref, meter}。全形數字先正規化。"""
    text = text.translate(_FULLWIDTH)
    m = _TP_MASKED.search(text) or _TP_TOTAL.search(text)
    if not m:
        return None
    out = {"amount": _norm_amount(m.group(1)), "date": "", "ref": "", "meter": ""}
    pm = _TP_PERIOD.search(text)
    if pm:  # 民國年 → 西元；電費是月帳單，日期記月底前
        out["date"] = f"{int(pm.group(1)) + 1911:04d}-{pm.group(2)}-28"
    rm = _TP_REF.search(text)
    if rm:
        out["ref"] = rm.group(1)
    mm = _TP_METER.search(text)
    if mm:
        out["meter"] = mm.group(1)
    return out


def parse_taipower_bill(msg: dict) -> list[dict]:
    """台電 e-Bill（2026-09-08 大王指路）：PDF 密碼＝統編，同國泰對帳單。

    ⚠️ 同一份憑證常被轉寄多次（owner@ 與 twaccounting@ 各一份）——靠
    ``ref``（單據號碼）在彙總層去重（_all_rows），這裡照解。沒有 PDF 附件的
    （轉寄摘要/空信）回空清單：訊息記為已看過、不入帳。
    """
    tax_id = _tw_tax_id()
    for fn, blob in (msg.get("attachments") or []):
        if not str(fn).lower().endswith(".pdf"):
            continue
        try:
            import io

            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(blob))
            if reader.is_encrypted:
                if not tax_id or not reader.decrypt(tax_id):
                    return [_base(msg, category="電費憑證(參考)",
                                  counterparty="需統編解密（tw_tax_id 未設）")]
            parsed = _parse_taipower_text(reader.pages[0].extract_text())
            if parsed and parsed["amount"]:
                return [_base(
                    msg, category="電費憑證(參考)", currency="TWD",
                    amount=parsed["amount"],
                    counterparty=f"台電（電號 {parsed['meter']}）" if parsed["meter"]
                                 else "台電",
                    ref=parsed["ref"],
                    date=parsed["date"] or _base(msg)["date"])]
            return [_base(msg, category="電費憑證(參考)", counterparty="台電")]
        except Exception as exc:  # noqa: BLE001 —— 單封解不開計筆數
            logger.warning("taipower e-bill 解析失敗: %s", exc)
            return [_base(msg, category="電費憑證(參考)", counterparty="台電")]
    return []


# ── 國泰世華簡易對帳單（加密 PDF，密碼＝統編）────────────────────────

def _tw_tax_id() -> str:
    """公司統編（國泰對帳單 PDF 的密碼）。env 優先，其次 var/state 設定檔。

    不寫死在 repo（會上 GitHub）；部署時把統編放進
    ``var/state/finance_taiwan_config.json`` 的 ``tw_tax_id``。
    """
    env = os.environ.get("RED_TW_TAX_ID", "").strip()
    if env:
        return env
    try:
        from agent_core.logging_and_paths import STATE_DIR
        with open(os.path.join(STATE_DIR, "finance_taiwan_config.json"),
                  encoding="utf-8") as f:
            return str(json.load(f).get("tw_tax_id") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


_CUB_BAL = re.compile(r"合計\s*([\d,]+(?:\.\d+)?)")
_CUB_PERIOD = re.compile(r"對帳單期間[：:]\s*(\d{4})/(\d{2})/\d{2}")


def _parse_cub_statement_text(text: str) -> dict | None:
    """對帳單第一頁文字 → {date, amount}。實測只有餘額快照、無交易明細頁。"""
    m = _CUB_BAL.search(text)
    if not m:
        return None
    pm = _CUB_PERIOD.search(text)
    date = f"{pm.group(1)}-{pm.group(2)}-28" if pm else ""
    return {"date": date, "amount": _norm_amount(m.group(1))}


def parse_cub_statement(msg: dict) -> list[dict]:
    tax_id = _tw_tax_id()
    for fn, blob in (msg.get("attachments") or []):
        if not str(fn).lower().endswith(".pdf"):
            continue
        try:
            import io

            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(blob))
            if reader.is_encrypted:
                if not tax_id or not reader.decrypt(tax_id):
                    return [_base(msg, category="國泰活存餘額",
                                  counterparty="需統編解密（tw_tax_id 未設）")]
            parsed = _parse_cub_statement_text(reader.pages[0].extract_text())
            if parsed:
                return [_base(msg, category="國泰活存餘額", currency="TWD",
                              amount=parsed["amount"], counterparty="國泰世華",
                              date=parsed["date"] or _base(msg)["date"])]
        except Exception as exc:  # noqa: BLE001 —— 單封解不開計筆數
            logger.warning("cub statement 解析失敗: %s", exc)
    return [_base(msg, category="國泰活存餘額", counterparty="國泰世華")]


def parse_export_collection(msg: dict) -> list[dict]:
    """出口託收款項收妥 —— 版面未定版，抓得到幣別金額就記、抓不到記筆數。"""
    m = _AMOUNT_RE.search(msg.get("body", ""))
    kw = {"category": "出口託收", "counterparty": ""}
    if m:
        cur = _CUR_NORM.get(m.group("cur").upper(), m.group("cur").upper())
        kw.update(currency=cur, amount=_norm_amount(m.group("amt")))
    return [_base(msg, **kw)]


# 富邦網銀通知（實測 2026-09-07 body）：「轉帳金額 臺幣 349,655 元」＋
# 「暱稱/轉入戶名 Owner 陳Ｏ亨」。轉入戶名＝大王自己 → 一銀→富邦的內部調撥
# （interco，不是費用）；其他戶名 → 富邦→員工/廠商的實際轉出（薪資鏈末端）。
_FUBON_AMT = re.compile(r"轉帳金額\s*臺幣\s*([\d,]+)\s*元")
_FUBON_PAYEE = re.compile(r"暱稱/轉入戶名\s*([^\n]{1,30})")
_OWN_NAME_RE = re.compile(r"Owner|陳Ｏ亨|陳一男", re.I)


def parse_fubon_transfer(msg: dict) -> list[dict]:
    body = msg.get("body", "")
    m = _FUBON_AMT.search(body)
    payee = ""
    pm = _FUBON_PAYEE.search(body)
    if pm:
        payee = pm.group(1).strip()
    own = bool(_OWN_NAME_RE.search(payee)) if payee else True  # 沒戶名保守當調撥
    kw = {
        "category": "富邦內部調撥" if own else "富邦轉出(薪資/付款)",
        "interco": own,
        "counterparty": sanitize_for_llm(payee or "台北富邦")[:40],
    }
    if m:
        kw.update(currency="TWD", amount=_norm_amount(m.group(1)))
    else:
        alt = _AMOUNT_RE.search(f"{msg.get('subject', '')} {body[:800]}")
        if alt:
            cur = _CUR_NORM.get(alt.group("cur").upper(), alt.group("cur").upper())
            kw.update(currency=cur, amount=_norm_amount(alt.group("amt")))
    return [_base(msg, **kw)]


# ────────────────────────────────────────────────────────────────────
# 串流表：mailbox × Gmail query × parser
# ────────────────────────────────────────────────────────────────────

_STREAMS: tuple[dict[str, Any], ...] = (
    {"key": "fcb_foreign", "mailbox": "owner@company.example",
     "query": "from:fx-desk@bank.example 國外匯入匯款",
     "parser": parse_fcb_foreign, "label": "一銀國外匯入款"},
    {"key": "fcb_domestic", "mailbox": "owner@company.example",
     "query": "from:bank.example 國內匯入匯款",
     "parser": parse_fcb_domestic, "label": "一銀國內匯入款"},
    {"key": "fcb_media", "mailbox": "owner@company.example",
     "query": "from:sms-adm@mail.bank.example 媒體劃撥",
     "parser": parse_media_batch, "label": "一銀代繳批次(勞健保/勞退)"},
    {"key": "fcb_export_col", "mailbox": "owner@company.example",
     "query": "from:bank.example 出口託收款項收妥",
     "parser": parse_export_collection, "label": "一銀出口託收"},
    # 2026-09-08 大王指路：UserA 水單都寄 twaccounting@ —— in:sent 只搜到
    # 1 封、這裡 37 封（20 封含金額），收件端才是全集。
    {"key": "purchase_remit", "mailbox": "twaccounting@company.example",
     "query": "from:twpurchase2@company.example (水單 OR 已付款)",
     "parser": parse_purchase_remit, "label": "採購付款水單"},
    {"key": "lan_remit", "mailbox": "owner@company.example",
     "query": "from:accounting-vn@company.example 水單",
     "parser": parse_purchase_remit, "label": "會計匯款水單"},
    {"key": "cht", "mailbox": "owner@company.example",
     "query": "from:invoice@cht.com.tw",
     "parser": parse_cht_invoice, "label": "中華電信發票",
     "attachments": True},
    {"key": "fubon", "mailbox": "owner@company.example",
     "query": "from:taipeifubon.com.tw",
     "parser": parse_fubon_transfer, "label": "富邦轉帳(薪資鏈)"},
    # 2026-09-08 大王指路：國泰世華也是合作銀行。簡易對帳單=加密 PDF（密碼
    # =統編），實測只有餘額快照、無交易明細 —— 有實際進出要另設交易通知。
    {"key": "cub_statement", "mailbox": "owner@company.example",
     "query": "from:bank2.example 簡易對帳單",
     "parser": parse_cub_statement, "label": "國泰世華對帳單(餘額快照)",
     "attachments": True},
    {"key": "pacificstar", "mailbox": "twaccounting@company.example",
     "query": "from:pacificstargroup.com",
     "parser": parse_forwarder, "label": "貨代費(沛華)"},
    # 2026-09-09 實測：沛華「*電子發票*」正本落在 owner@（To: 業務、CC 大王），
    # 會計/UserA 只有討論串。同信多信箱靠 Message-ID 去重、同發票多信靠 ref。
    {"key": "pacificstar_dylan", "mailbox": "owner@company.example",
     "query": "from:pacificstargroup.com (電子發票 OR 帳單)",
     "parser": parse_forwarder, "label": "貨代費(沛華/owner)",
     "attachments": True},
    # 2026-09-08 大王指路：台電 e-Bill 電費憑證 PDF，密碼同統編。原始來源是
    # ebill@ebppsmtp.taipower.com.tw → twaccounting@（實測隔月一張）；只收
    # 「繳費憑證」（已繳事實），「電費通知」是待繳額不入現金台帳。owner@ 偶有
    # 轉寄複本 —— ref（單據號碼）去重扛住。
    {"key": "taipower", "mailbox": "twaccounting@company.example",
     "query": "from:taipower.com.tw 繳費憑證",
     "parser": parse_taipower_bill, "label": "台電電費憑證",
     "attachments": True},
)


def _fetch_stream(mailbox: str, query: str, days_back: int,
                  include_attachments: bool = False) -> list[dict]:
    """Gmail 抓一個串流的全文。回 [{key,date,subject,sender,body[,attachments]}]。

    include_attachments=True 時多帶 ``attachments``：[(檔名, bytes), …]（最多
    2 個、單檔 ≤ 3MB）—— 中華電信發票/國泰對帳單的金額都在附件裡。
    """
    pn = _pn()
    users = pn._gmail_users(mailbox)
    q = f"{query} newer_than:{max(1, int(days_back))}d"
    resp = users.messages().list(userId="me", q=q, maxResults=500).execute()
    out = []
    for stub in resp.get("messages", []) or []:
        msg = users.messages().get(userId="me", id=stub["id"],
                                   format="full").execute()
        head = pn._headers_of(msg)
        msg_id = (head.get("Message-ID") or head.get("Message-Id") or "").strip()
        key = f"msgid:{msg_id}" if msg_id else f"{mailbox}:{stub['id']}"
        # ⚠️ 不用 payment_notice._msg_when —— 它回「MM/DD HH:MM」沒有年份，
        # 進台帳月彙總會全進亂桶（實測富邦 259 封全掛）。這裡要完整 ISO 日期。
        try:
            ts = int(msg.get("internalDate") or 0) / 1000
            when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
        except (TypeError, ValueError, OSError):
            when = ""
        row = {
            "key": key,
            "date": when,
            "subject": head.get("Subject", ""),
            "sender": head.get("From", ""),
            "body": pn._body_text(msg.get("payload", {}) or {}),
        }
        if include_attachments:
            row["attachments"] = _fetch_attachments(users, msg, stub["id"])
        out.append(row)
    return out


def _fetch_attachments(users, msg: dict, mid: str,
                       limit: int = 2, max_bytes: int = 3_000_000) -> list:
    """抓該封信的附件 bytes（最多 limit 個、單檔 ≤ max_bytes）。"""
    import base64
    found: list[tuple[str, bytes]] = []

    def walk(part: dict) -> None:
        if len(found) >= limit:
            return
        fn = part.get("filename")
        body = part.get("body") or {}
        att_id = body.get("attachmentId")
        if fn and att_id and int(body.get("size") or 0) <= max_bytes:
            try:
                data = users.messages().attachments().get(
                    userId="me", messageId=mid, id=att_id).execute()
                found.append((fn, base64.urlsafe_b64decode(data["data"])))
            except Exception as exc:  # noqa: BLE001 —— 單附件失敗不擋整封
                logger.warning("附件下載失敗 %s: %s", fn, exc)
        for sub in (part.get("parts") or []):
            walk(sub)

    walk(msg.get("payload", {}) or {})
    return found


# ────────────────────────────────────────────────────────────────────
# 台帳存取 + 同步
# ────────────────────────────────────────────────────────────────────

def _load_ledger() -> dict:
    try:
        with open(_LEDGER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 —— 壞檔重建，台帳可全量重掃回來
        logger.warning("taiwan_cash_ledger 讀取失敗（%s），視為空", exc)
    return {"entries": {}}


def _save_ledger(data: dict) -> None:
    os.makedirs(os.path.dirname(_LEDGER_PATH), exist_ok=True)
    _atomic_write_text(_LEDGER_PATH, json.dumps(data, ensure_ascii=False))


def sync_taiwan_ledger(days_back: int = 120) -> str:
    days_back = max(1, min(int(days_back or 120), 730))
    data = _load_ledger()
    entries: dict[str, Any] = data["entries"]
    lines = [f"🔄 台灣金流台帳同步（近 {days_back} 天）"]
    total_new = 0
    for stream in _STREAMS:
        try:
            msgs = _fetch_stream(stream["mailbox"], stream["query"], days_back,
                                 include_attachments=bool(stream.get("attachments")))
        except Exception as exc:  # noqa: BLE001 —— 單一串流掛掉不拖垮其他
            lines.append(f"  ⚠️ {stream['label']}：抓取失敗 {type(exc).__name__}: "
                         f"{str(exc)[:60]}")
            continue
        new_n, parsed_amt = 0, 0
        parser: Callable[[dict], list[dict]] = stream["parser"]
        for msg in msgs:
            if msg["key"] in entries:
                continue
            try:
                rows = parser(msg)
            except Exception as exc:  # noqa: BLE001 —— 單封解析失敗記筆數
                logger.warning("finance_taiwan parser %s 失敗: %s",
                               stream["key"], exc)
                rows = [_base(msg, category="解析失敗")]
            for row in rows:
                row["stream"] = stream["key"]
            entries[msg["key"]] = rows
            new_n += 1
            parsed_amt += sum(1 for r in rows if r.get("amount"))
        total_new += new_n
        lines.append(f"  {stream['label']}：{len(msgs)} 封掃到、新增 {new_n} 封"
                     f"（{parsed_amt} 筆有金額）")
    data["synced_at"] = datetime.now().isoformat(timespec="seconds")
    _save_ledger(data)
    lines.append(f"Σ 新增 {total_new} 封；台帳共 {len(entries)} 封。"
                 "接著用 taiwan_cash_summary 看月收支。")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 稅（Drive 會計資料夾，RAG 已索引 + OCR）
# ────────────────────────────────────────────────────────────────────

# OCR 金額常被斷開（「24, 051」）：允許數字群中夾逗號/空白，取應納稅額附近的。
_TAX_AMT = re.compile(
    r"應納稅額(?:合計)?[^0-9]{0,30}((?:\d{1,3}[,\s]\s?)+\d{3}|\d{3,9})")
# 捕完整檔名（可含空格，如「應付款申請-沛華海運 HK x VN (NTD11,554).pdf」）：
# 錨定在 sim=/新鮮度= 分數欄之後，\S+? 會斷在第一個空白只剩尾巴。
_TAX_FILE = re.compile(
    r"【\d+】\s+sim=[\d.]+\s+(?:新鮮度=[\d.]+\s+)?(.+?\.pdf)（(\d{4}-\d{2}-\d{2})）")


def _parse_tax_hits(rag_text: str) -> list[dict]:
    """search_drive_docs 的輸出 → [{file, date, amount}]。amount 可能 None。"""
    out = []
    blocks = re.split(r"(?=【\d+】)", rag_text)
    for block in blocks:
        fm = _TAX_FILE.search(block)
        if not fm:
            continue
        am = _TAX_AMT.search(block)
        amount = None
        if am:
            amount = _norm_amount(re.sub(r"[,\s]", "", am.group(1)))
        out.append({"file": sanitize_for_llm(fm.group(1))[:80],
                    "date": fm.group(2), "amount": amount})
    return out


def taiwan_tax_from_drive(k: int = 10) -> str:
    """從 Drive 會計資料夾撈營業稅(401)/營所稅繳款書，抽「應納稅額」。

    來源＝RAG 索引的 OCR 文字（2026-09-08 大王指路：國稅局/扣稅通知都在
    Drive 會計資料夾；實測 401 繳款書齊到 115-06、含營所稅結算）。⚠️ 金額是
    OCR 抽取，以繳款書原件為準 —— 輸出帶檔名讓會計可回查。
    """
    k = max(3, min(int(k or 10), 20))
    try:
        from agent_core.ingest.drive_search import search_drive_docs
        raw = search_drive_docs("營業稅繳款書 401 營所稅 結算 繳款書 應納稅額",
                                k=k, prefer_recent=True)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Drive 稅單查詢失敗：{type(exc).__name__}: {str(exc)[:80]}"
    hits = _parse_tax_hits(str(raw))
    if not hits:
        return ("⚠️ Drive 索引裡查不到繳款書 —— 確認會計資料夾有上傳、"
                "且夜跑 RAG 已同步。")
    # 只有「繳款書」是繳費事實；「申報書」是申報表（版面不同、金額欄多），
    # 混在一起會誤導 —— 分列。
    slips = [h for h in hits if "繳款書" in h["file"]]
    decls = [h for h in hits if "申報書" in h["file"]]
    lines = ["🧾 台灣稅單（Drive 會計資料夾，OCR 抽取、以原件為準）"]
    for h in slips:
        amt = (f"應納稅額 {h['amount']:,.0f} TWD" if h["amount"]
               else "金額抽不出（OCR 版面亂，開原件看）")
        lines.append(f"- {h['file']}（{h['date']} 上傳）：{amt}")
    if not slips:
        lines.append("（這輪語意檢索只撈到申報書 —— 需要繳款書明細就把 k 調大再查）")
    if decls:
        lines.append(f"📄 另有 401 申報書 {len(decls)} 份（申報表非繳費單，不列金額）："
                     + "、".join(h["file"] for h in decls[:6]))
    lines.append("📌 401=每兩月營業稅；營所稅=年度結算。檔名含公司別（佳桀/佳紘）。")
    return "\n".join(lines)


# ── 貨代/海運費歷史單（Drive 會計資料夾）─────────────────────────────
# 2026-09-09 實測：email 裡沛華正式請款只有一張電子發票（2026-08），歷史帳單
# 是掃描檔進 Drive —— 「應付款申請-沛華海運 HK x VN (NTD11,554).pdf」這種
# **金額就在檔名**（最可靠），2020-2021 的「沛華海運費帳單」要靠 OCR。

_FREIGHT_FILE_AMT = re.compile(r"(?:NTD?|NT\$|TWD)\s*([\d,]+(?:\.\d+)?)")
_FREIGHT_OCR_AMT = re.compile(
    r"(?:合計|總計|應收|應付)(?:金額)?[^0-9]{0,30}((?:\d{1,3}[,\s]\s?)+\d{3}|\d{4,9})")


def _parse_freight_hits(rag_text: str) -> list[dict]:
    """search_drive_docs 輸出 → [{file, date, amount, src}]。檔名金額優先。"""
    out = []
    for block in re.split(r"(?=【\d+】)", rag_text):
        fm = _TAX_FILE.search(block)
        if not fm:
            continue
        fname = sanitize_for_llm(fm.group(1))[:90]
        amount, src = None, ""
        m = _FREIGHT_FILE_AMT.search(fname)
        if m:
            amount, src = _norm_amount(m.group(1)), "檔名"
        else:
            m = _FREIGHT_OCR_AMT.search(block)
            if m:
                amount, src = _norm_amount(re.sub(r"[,\s]", "", m.group(1))), "OCR"
        out.append({"file": fname, "date": fm.group(2),
                    "amount": amount, "src": src})
    return out


def freight_bills_from_drive(k: int = 10) -> str:
    """從 Drive 會計資料夾撈貨代/海運費歷史單（沛華等），抽金額。

    email 只有 2026-08 起的電子發票；更早的請款單是掃描檔在 Drive。金額來源
    分兩級：檔名（應付款申請-…(NTD11,554).pdf，確定性）與 OCR（以原件為準）。
    """
    k = max(3, min(int(k or 10), 20))
    try:
        from agent_core.ingest.drive_search import search_drive_docs
        raw = search_drive_docs("沛華 海運費 帳單 應付款申請 貨代 請款",
                                k=k, prefer_recent=True)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Drive 查詢失敗：{type(exc).__name__}: {str(exc)[:80]}"
    hits = _parse_freight_hits(str(raw))
    if not hits:
        return "⚠️ Drive 索引裡查不到貨代/海運費單據。"
    lines = ["🚢 貨代/海運費歷史單（Drive 會計資料夾）"]
    for h in hits:
        if h["amount"]:
            amt = f"{h['amount']:,.0f} TWD（{h['src']}" + \
                  ("，以原件為準）" if h["src"] == "OCR" else "）")
        else:
            amt = "金額抽不出（開原件看）"
        lines.append(f"- {h['file']}（{h['date']} 上傳）：{amt}")
    lines.append("📌 email 端只有 2026-08 起的沛華電子發票（台帳 貨代/快遞費），"
                 "更早的費用以這裡的掃描檔為準。")
    return "\n".join(lines)


def taiwan_ledger_autosync() -> str:
    """（排程專用）每日安靜同步台帳：成功回 (無新發現) 不推播、串流失敗 raise。

    dispatcher 的 deterministic_tool 用 —— 同步是水電工程不是報告，天天推
    「新增 N 封」只是噪音；抓取失敗才需要浮上來（raise → last_error →
    red-status），同 income_statement_autopush 的 infra 慣例。
    """
    out = sync_taiwan_ledger(days_back=14)
    if "抓取失敗" in out:
        raise RuntimeError(out)
    return "(無新發現)"


# ────────────────────────────────────────────────────────────────────
# 彙總 + 合併視圖
# ────────────────────────────────────────────────────────────────────

# 同一張憑證常被轉寄多份（實測台電 e-Bill：owner@ 與 twaccounting@ 各轉一次，
# Message-ID 不同、去重擋不住）→ 這些串流改用文件自己的唯一號（ref）在彙總層
# 去重；同文件跨串流（沛華發票同時在會計與 owner 串流）用「家族鍵」對消。
# 只列「文件號確定唯一」的串流 —— fcb 的 ref 是匯款附言，兩筆分批付款可能寫
# 同一張發票號，硬去重會吃掉真金流。
_REF_DEDUP_FAMILY = {"taipower": "taipower",
                     "pacificstar": "pacificstar",
                     "pacificstar_dylan": "pacificstar"}


def _all_rows() -> list[dict]:
    rows: list[dict] = []
    seen_refs: set[tuple[str, str]] = set()
    for entry_rows in _load_ledger()["entries"].values():
        for r in (entry_rows if isinstance(entry_rows, list) else []):
            family = _REF_DEDUP_FAMILY.get(r.get("stream") or "")
            ref = r.get("ref") or ""
            if family and ref:
                key = (family, ref)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
            rows.append(r)
    return rows


def _usd_twd() -> float | None:
    """USD/TWD 參考匯率 —— fx_rates 本地歷史（匯率推播每日存），免打網路。"""
    try:
        from agent_core import fx_rates
        hist = fx_rates._load_history()
        rate = float(hist[-1].get("rate", 0)) if hist else 0.0
        return rate if 20 <= rate <= 45 else None
    except Exception:  # noqa: BLE001
        return None


_COVERAGE_NOTE = (
    "📌 現金收付制、只計 email 有通知的金流。「富邦轉出」含薪資與其他付款，"
    "通知不分對象性質，無法逐筆拆薪資/廠商。營業稅/營所稅在 Drive 會計資料夾"
    "（用 taiwan_tax_from_drive 查）。水電瓦斯＝一銀代繳批次自動入帳；"
    "房產自有無房租（2026-09-08 大王確認）。已知缺口（要人工補或改寄通知）："
    "貨代/快遞金額（在附件，沛華月結單進來後可接）、"
    "台灣供應商未走水單的付款、國泰帳戶逐筆交易（對帳單只有餘額快照）。")


def taiwan_cash_summary(months: int = 6) -> str:
    rows = _all_rows()
    if not rows:
        return ("⚠️ 台帳是空的 —— 先跑 sync_taiwan_ledger(days_back=365) "
                "從 Gmail 建台帳。")
    months = max(1, min(int(months or 6), 24))
    by_month: dict[str, dict[str, dict[str, float]]] = {}
    no_amount: dict[str, int] = {}
    for r in rows:
        ym = str(r.get("date") or "")[:7]
        if len(ym) != 7:
            continue
        cat = r.get("category") or "?"
        if r.get("interco"):
            cat = f"{cat}·關係企業"
        if not r.get("amount"):
            no_amount[cat] = no_amount.get(cat, 0) + 1
            continue
        cur = r.get("currency") or "?"
        by_month.setdefault(ym, {}).setdefault(cat, {})
        by_month[ym][cat][cur] = by_month[ym][cat].get(cur, 0.0) + float(r["amount"])
    picked = sorted(by_month)[-months:]
    rate = _usd_twd()

    lines = ["🇹🇼 台灣金流台帳（email 現金收付彙總）"]
    for ym in picked:
        cats = by_month[ym]
        inc_twd = sum(v for cat, curmap in cats.items()
                      for cur, v in curmap.items()
                      if cat in _INCOME_CATS and cur == "TWD")
        inc_usd = sum(v for cat, curmap in cats.items()
                      for cur, v in curmap.items()
                      if cat in _INCOME_CATS and cur == "USD")
        lines.append(f"◾ {ym}")
        for cat in sorted(cats, key=lambda c: (c not in _INCOME_CATS, c)):
            part = "、".join(f"{v:,.0f} {cur}" for cur, v in
                             sorted(cats[cat].items(), key=lambda x: -x[1]))
            root = cat.split("·")[0]
            arrow = ("💼" if root in _SNAPSHOT_CATS
                     else "📥" if root in _INCOME_CATS else "📤")
            lines.append(f"  {arrow} {cat}：{part}")
        if rate and (inc_twd or inc_usd):
            lines.append(f"  ＝營收面合計 ≈ USD {inc_usd + inc_twd / rate:,.0f}"
                         f"（TWD 以 {rate:.2f} 換算）")
    silent = {c: n for c, n in no_amount.items() if n}
    if silent:
        lines.append("⚠️ 有筆數但解析不出金額（照筆數列、不猜數字）："
                     + "、".join(f"{c} {n} 筆" for c, n in sorted(silent.items())))
    lines.append(_COVERAGE_NOTE)
    return "\n".join(lines)


def consolidated_pnl_view(period: str = "") -> str:
    """台越合併視圖：福群 ERP 關帳損益 ＋ 台灣 email 台帳同月收支，並排＋美金參考。

    不是真合併報表 —— 台灣端是現金收付估算、關係企業內部交易只標示未對消，
    輸出會講清楚。
    """
    from agent_core import finance_statements as fin
    guard = fin._guard()
    if guard:
        return guard
    try:
        book, book_name, currency = fin._active_book()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀 GL 總帳失敗：{exc}"
    p, warn, err = fin._resolve_period(book, period)
    if err:
        return err
    vn = fin.compute_pnl(book, p)
    vn_rate = fin._usd_rate(book, p)

    ym = f"{p[:4]}-{p[4:]}"
    tw_rows = [r for r in _all_rows() if str(r.get("date") or "")[:7] == ym]
    tw_rate = _usd_twd()

    lines = [f"🌏 台越合併視圖 {ym}（並排，非正式合併報表）"]
    if warn:
        lines.append(warn)
    vn_usd = f"（≈ USD {vn['net'] / vn_rate:,.0f}）" if vn_rate else ""
    lines.append(
        f"🇻🇳 {book_name}（ERP 關帳、權責發生制、百萬 {currency}）：營收 "
        f"{fin._fmt_m(vn['revenue'])}、毛利 {fin._fmt_m(vn['gross'])}、"
        f"稅後淨利 {fin._fmt_m(vn['net'])}{vn_usd}")

    if not tw_rows:
        lines.append(f"🇹🇼 台灣：{ym} 台帳無資料 —— 先跑 sync_taiwan_ledger。")
    else:
        inc: dict[str, float] = {}
        exp: dict[str, float] = {}
        interco_n = 0
        for r in tw_rows:
            if r.get("interco"):
                interco_n += 1
                continue
            if not r.get("amount"):
                continue
            cur = r.get("currency") or "?"
            bucket = inc if (r.get("category") in _INCOME_CATS) else exp
            bucket[cur] = bucket.get(cur, 0.0) + float(r["amount"])
        fmt = lambda d: "、".join(f"{v:,.0f} {c}" for c, v in  # noqa: E731
                                  sorted(d.items(), key=lambda x: -x[1])) or "無資料"
        lines.append(f"🇹🇼 台灣（email 現金收付估算）：收 {fmt(inc)}｜支 {fmt(exp)}")
        if tw_rate and ("TWD" in inc or "USD" in inc):
            usd_in = inc.get("USD", 0) + inc.get("TWD", 0) / tw_rate
            lines.append(f"　台灣收入 ≈ USD {usd_in:,.0f}"
                         f"（TWD 以 {tw_rate:.2f} 換算）")
        if interco_n:
            lines.append(f"🔁 關係企業內部金流 {interco_n} 筆已排除在上列數字外"
                         "（合併時應對消：台灣付福群的貨款＝福群的代工營收）。")
    lines.append(
        "📌 兩邊口徑不同（福群=關帳權責制／台灣=email 現金制），不能直接相加成"
        "「集團淨利」；台灣正式損益要等會計師報表或帳冊資料源。")
    lines.append(_COVERAGE_NOTE)
    lines.append(fin._esq()._stale_hint().strip())
    return "\n".join(lines)
