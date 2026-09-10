"""客戶貨款到帳通知 → 台灣／越南會計（紫色）的轉知管線。

需求（2026-08-12，大王）：UserAng（``twsales@company.example``）與大王本人
（``owner@company.example``）會收到「客人付貨款」的通知，要直接在紫色 agent
上通知台越會計、同時也寄 email。目前這件事是**人工轉寄**——實測 owner@ 收到
第一銀行匯入匯款通知後，再手動轉給 twaccounting@（UserJ），越南 UserL 那邊只有
UserS 轉的 Decathlon 那條線才收得到。

## 這支只認「錢真的進來了／客戶明講已付款」的信

2026-08-12 對 twsales@ 近 365 天做過一次實測：用寬鬆關鍵字（payment /
remittance / 匯款 / 貨款 / 付款 / 水單 / transfer / advice）掃外部來信，49 封
候選裡真正是「客戶付款通知」的只有 8 封，其餘 41 封是**反向**的東西——
我方在催款（outstanding payment list）、我方在申請付款（payment application）、
在談付款排程（payment schedule）、供應商要我方付錢、甚至是自動回覆
（Automatische Antwort）。所以這裡**不做關鍵字廣撒**，走兩條窄而硬的路：

  A. **銀行匯入款通知**（``_BANK_RULES``）——錢已經到／即將解付，銀行系統信、
     格式固定，逐欄 parse。這是唯一「確定有錢進來」的來源。
  B. **客戶自己寄的付款通知**（``_ADVICE_SENDER_RULES`` / ``_ADVICE_PHRASES``）
     ——寄件人+主旨形狀的白名單，加上少數「已完成付款」的斷言句型。

兩條都是 allowlist。**寧可漏、不可錯**：漏掉的那封信本來就還躺在 UserAng 或
大王的信箱裡（人工轉寄的舊流程沒有被拿掉），但把「我方催款」報成「客戶已付款」
會讓會計去對一筆不存在的帳。要擴充就把新形狀加進規則表（並補 tests）。

## 不替客戶下判斷

銀行通知裡的 ``匯款人名稱`` 原樣列出，**不**自動翻譯成「某某客戶的貨款」：
實測匯入款裡混著關係企業（FU CHUN CORPORATION）、信保手續費退費（財團法人
中小企業信用保證基金）、營所稅退稅。這些都是「匯入款」但都不是客戶貨款。工具
只照表念欄位，是不是貨款由會計看匯款人／摘要自己判斷——CLAUDE.md〈員工零幻覺〉。

## 只講一次

每封通知只推播一次：``var/state/payment_notice_seen.json`` 以 RFC ``Message-ID``
（跨信箱同一封只算一次）或 ``<mailbox>:<gmail id>`` 為鍵記已看過的信（保留
``_SEEN_TTL_D`` 天）。沒有新的就回 ``(無新發現)``，dispatcher 看到這五個字就整輪
安靜——所以排程要每天彙整一次或要跑更密都行，本模組不綁節奏；現行排程是每天
09:00 一次（``scripts/register_payment_notice_task.py``）。回溯天數（預設 3 天）
只是「漏掉的那天補得回來」的餘裕，去重靠狀態檔不是靠天數。

⚠️ 本模組**唯讀**：不寄信、不改標籤。寄信是排程 task 的 ``notify_emails``
（dispatcher 自寄），推播是 ``notify_channel=telegram`` + ``notify_agent_colors``，
都跟這裡無關。
"""
from __future__ import annotations

import base64
import html as _html
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Callable

from agent_core.env_utils import env_int
from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger
from agent_core.prompt_injection import sanitize_for_llm

# 要掃的兩個信箱（順序＝報告裡的順序）。大王排前面：銀行匯入款通知只寄到他這裡。
PAYMENT_MAILBOXES = ("owner@company.example", "twsales@company.example")

_SEEN_PATH = os.path.join(STATE_DIR, "payment_notice_seen.json")
_SEEN_TTL_D = 120           # 已看過紀錄保留天數（狀態檔不無限長大）。
_MAILBOX_DEADLINE_S = 45.0  # 每信箱軟時間預算：一個卡住不拖垮另一個。
# 單輪單信箱最多處理幾封。Gmail 端粗篩實測：owner@ 近 3 天 14 封、近 14 天 29
# 封（twsales@ 各約一半），60 是「催款 thread 爆量那幾天」也吃得下的餘裕，
# 又遠低於逐封 get 會撞到時間預算的量。
_MAX_PER_MAILBOX = 60
_REMARK_MAX = 300


# ────────────────────────────────────────────────────────────────────
# 規則表 A：銀行匯入款通知（錢進來了）
# ────────────────────────────────────────────────────────────────────
# 每條規則：
#   key          內部識別字（狀態檔／測試用）
#   label        報告裡的分類抬頭
#   sender_re    寄件人 address 必須命中（銀行的系統信寄件人固定）
#   subject_re   主旨必須命中
#   parser       body 純文字 → 欄位 dict
#
# ⚠️ subject_re 一律要求「匯入」：同一個寄件人也會寄**匯出**匯款通知
#   （2025-12-06「第一銀行 國內匯出匯款通知」= 我方付錢出去），那不是這支要抓的。
_BANK_RULES: tuple[dict[str, Any], ...] = ()  # 於下方 parser 定義後填入


def _parse_fcb_foreign(text: str) -> dict[str, str]:
    """第一銀行「國外匯入匯款通知」（fx-desk@）——欄名與值同列、以空白分隔。

    實測樣本（2026-08-11）::

        匯入款編號 S6EJ010927
        匯款生效日期 2026/08/11
        匯款幣別 USD
        匯款金額 99,559.20
        受款人名稱 JAI JYE CORPORATION
        匯款人名稱 SUPREMO-ORIENTAL CO LTD
        付款明細 SETTLED YOUR INV NO. L200726-5 ANDL130726-4
    """
    fields = _labelled_fields(text, {
        "remitter": "匯款人名稱",
        "amount": "匯款金額",
        "currency": "匯款幣別",
        "value_date": "匯款生效日期",
        "notice_date": "銀行通知日期",
        "ref": "匯入款編號",
        "payee": "受款人名稱",
    })
    if fields.get("ref"):
        fields["ref_label"] = "匯入款編號"
    # 付款明細（= 客戶在匯款附言寫的 invoice 號）會換行：實測
    # 「SETTLED YOUR INV NO. L200726-5 ANDL⏎130726-4」——只取一行會把第二張
    # 發票號切掉，而那正是會計要拿去銷帳的東西。收到「說明」那段公版文字為止。
    remark = _multiline_field(text, "付款明細", stop=("說明", "匯款行名稱"))
    if remark:
        fields["remark"] = remark
    return fields


def _parse_cub_forex(text: str) -> dict[str, str]:
    """國泰世華「外匯匯入匯款通知 / Forex inward remittance notice」。

    版面是中英雙語標籤各佔一行、值接在英文標籤後（``幣別金額\\nCurrency and
    Amount USD 40,000.00``），所以用英文標籤當錨點比中文可靠。
    """
    fields = _labelled_fields(text, {
        "amount": "Currency and Amount",
        # 撇號在原信是 HTML entity，unescape 後可能是直式 ' 或彎式 ’ —— 用萬用
        # 字元吃掉那一格，別讓一個字元差異把匯款人整欄弄丟。
        "remitter": "Remitter.s Name",
        "payee": "Payee Name",
        "value_date": "Value Date",
        "notice_date": "通知日期 Notice Date：",
        "ref": "Reference Number of Remitting Bank",
        "remark": "Remark",
    })
    # 「USD 40,000.00」是幣別＋金額黏在一起的單一欄位，拆出來對齊其他規則的欄位。
    amount = fields.get("amount", "")
    m = re.match(r"^([A-Z]{3})\s+([\d,\.]+)$", amount)
    if m:
        fields["currency"], fields["amount"] = m.group(1), m.group(2)
    if fields.get("ref"):
        fields["ref_label"] = "匯款行參考編號"
    return fields


# 第一銀行「國內匯入匯款通知」是一張表：標頭各佔一行，資料列在標頭之後、
# 以空白分隔（實測 2026-08-12）::
#
#   匯款日期 匯款序號 匯款時間 匯出銀行 匯款人戶名 匯款金額 收款銀行 收款帳號 摘要
#   2026/08/12 000034 10:46:30 臺銀營業部 財團法人中小企業信用保證基金 1,735.00 …
#
# 匯出銀行與匯款人戶名都可能含空白（「臺銀營業部」「財團法人中小企業信用保證
# 基金」），所以不能純用 split()。錨點取「金額」——表裡唯一的千分位數字格式，
# 它左邊是戶名、右邊是收款銀行/帳號/摘要。
_FCB_DOMESTIC_ROW = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2})\s+(?P<seq>\d+)\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<from_bank>\S+)\s+(?P<remitter>.+?)\s+(?P<amount>\d[\d,]*\.\d{2})\s+"
    r"(?P<to_bank>\S+)\s+(?P<to_acct>\S+)\s*(?P<remark>.*)"
)


def _parse_fcb_domestic(text: str) -> dict[str, str]:
    """第一銀行「國內匯入匯款通知」——表格式，取第一列資料。

    ⚠️ 實測這條線大多是**信保手續費退費／退稅**，不是客戶貨款。所以「摘要」欄
    一定要原樣帶出，讓會計一眼看得出來（規則不替它判斷）。
    """
    m = _FCB_DOMESTIC_ROW.search(text)
    if not m:
        return {}
    return {
        "remitter": m.group("remitter").strip(),
        "amount": m.group("amount").strip(),
        "currency": "TWD",
        "value_date": m.group("date").strip(),
        "notice_date": m.group("date").strip(),
        "ref": m.group("seq").strip(),
        "ref_label": "匯款序號",
        "remark": m.group("remark").strip(),
        "from_bank": m.group("from_bank").strip(),
    }


_BANK_RULES = (
    {
        "key": "fcb_foreign_inward",
        "label": "第一銀行 國外匯入匯款",
        "sender_re": re.compile(r"fx-desk@bank\.example", re.I),
        "subject_re": re.compile(r"國外匯入匯款"),
        "parser": _parse_fcb_foreign,
    },
    {
        "key": "fcb_domestic_inward",
        "label": "第一銀行 國內匯入匯款",
        "sender_re": re.compile(r"@(?:mail\.)?bank\.example", re.I),
        "subject_re": re.compile(r"國內匯入匯款"),
        "parser": _parse_fcb_domestic,
    },
    {
        "key": "cub_forex_inward",
        "label": "國泰世華 外匯匯入匯款",
        "sender_re": re.compile(r"@\S*bank2\.example", re.I),
        "subject_re": re.compile(r"外匯匯入匯款|inward remittance", re.I),
        "parser": _parse_cub_forex,
    },
)


# ────────────────────────────────────────────────────────────────────
# 規則表 B：客戶自己寄的付款通知
# ────────────────────────────────────────────────────────────────────
# 寄件網域＋主旨形狀的白名單。每條都是實際跑過的信（括號內是實測樣本日期），
# 加新客戶請照這個格式，並在 tests/test_payment_notice.py 補一筆真實主旨。
_ADVICE_SENDER_RULES: tuple[dict[str, Any], ...] = (
    {
        # Supremo 每次匯款前寄「Payment on <date> - Jai Jye」附匯款明細/水單。
        # （2025-08-19、2025-12-16、2026-01-13、2026-02-09、2026-07-21、
        #   2026-07-28、2026-08-11 — 一年 7 封，全是真付款通知）
        "key": "supremo_payment_on",
        "label": "Supremo 付款通知",
        "sender_re": re.compile(r"@customer-a\.example", re.I),
        "subject_re": re.compile(r"^\s*payment\s+on\s+\d", re.I),
    },
    {
        # Decathlon 的 Datalog TMS「Notice of transfer / Avis de virement」。
        # 原信寄到 UserS@，他再轉給 owner@/lan@/twaccounting@（2026-05-28、
        # 2026-07-23、2026-07-30），所以寄件人可能是 UserS 的轉寄。
        "key": "decathlon_notice_of_transfer",
        "label": "Decathlon 匯款通知",
        "sender_re": re.compile(r"@(?:finance-portal\.example|company\.example)", re.I),
        "subject_re": re.compile(r"notice of transfer|avis de virement", re.I),
    },
)

# 「對方明講已經付了」的斷言句型。只認完成式的說法——刻意**不**收
# outstanding / application / schedule / pending / reminder 那些反向語意
# （實測那些全是我方在催款或申請付款，見模組 docstring）。
_ADVICE_PHRASES = re.compile(
    r"payment\s+(?:has\s+been|was|is)\s+(?:completed|made|done|effected|released|"
    r"transferred|remitted)|"
    r"(?:we|i)\s+have\s+(?:paid|remitted|transferred|arranged\s+the\s+payment)|"
    r"remittance\s+advice|payment\s+advice|zahlungsavis|"
    r"已(?:經)?(?:匯款|付款|匯出|付清)|款項?已(?:匯出|匯入|支付|付訖)|匯款水單",
    re.I,
)

# 反向語意的保險絲：句子裡出現這些字就不採信 ``_ADVICE_PHRASES``（例如
# "outstanding payment ... payment advice" 這種混寫）。規則 A 與
# ``_ADVICE_SENDER_RULES`` 不受影響——那兩條的形狀本身已經夠硬。
_ADVICE_NEGATIVE = re.compile(
    r"outstanding|overdue|payment\s+application|payment\s+schedule|pending\s+payment|"
    r"reminder|請款|催款|申請付款|付款申請|尚未(?:付款|收到)",
    re.I,
)

_OWN_DOMAIN = "company.example"


# ────────────────────────────────────────────────────────────────────
# 分類
# ────────────────────────────────────────────────────────────────────
def classify_notice(sender: str, subject: str) -> dict[str, Any] | None:
    """(寄件人, 主旨) → 命中的規則，沒中回 None。純函式、無 IO，測試直接打這支。"""
    addr = (sender or "").strip()
    subj = (subject or "").strip()
    # 轉寄前綴（Fwd:/FW:/Re:）不影響形狀判斷，先剝掉再比主旨。
    core = re.sub(r"^(?:\s*(?:re|fw|fwd|轉寄)\s*[:：]\s*)+", "", subj, flags=re.I)
    for rule in _BANK_RULES:
        if rule["sender_re"].search(addr) and rule["subject_re"].search(core):
            return {"kind": "bank", **rule}
    for rule in _ADVICE_SENDER_RULES:
        if rule["sender_re"].search(addr) and rule["subject_re"].search(core):
            return {"kind": "advice", **rule}
    if _ADVICE_PHRASES.search(core) and not _ADVICE_NEGATIVE.search(core):
        # 泛用句型只認**外部**寄件人：自家網域寄的「已匯款」多半是我方付供應商。
        if _OWN_DOMAIN not in addr.lower():
            return {
                "kind": "advice", "key": "phrase_payment_done",
                "label": "客戶付款通知", "parser": None,
            }
    return None


# ────────────────────────────────────────────────────────────────────
# Gmail 讀取
# ────────────────────────────────────────────────────────────────────
def _sa_file(mailbox: str) -> str:
    """網域委派用的 service account 金鑰路徑。

    ⚠️ **owner@ 不在 rag_sync_targets.json 的 gmail_accounts 裡**（RAG 夜跑同步
    大王的信箱走的是最上層那條 OAuth 主帳號設定，不是委派清單），所以不能像
    purchasing_brief 那樣「不在清單就拒絕」——那會讓銀行匯入款通知這條**主要**
    來源整條讀不到，而且失敗訊息看起來還很合理。做法同 sent_reply_tracker：
    金鑰檔取清單裡任一份（全公司同一把 SA），要冒充的信箱由本模組的常數
    ``PAYMENT_MAILBOXES`` 指定——那是本地程式碼、不是參數，LLM 指不到別人的信箱。
    """
    override = os.environ.get("RED_PAYMENT_NOTICE_SA_FILE", "").strip()
    if override:
        return override
    try:
        from agent_core.ingest.sync_config import load_targets
        accounts = load_targets().get("gmail_accounts") or []
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"讀不到 gmail_accounts 設定（{exc}）") from exc
    key = (mailbox or "").strip().lower()
    fallback = ""
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        sa = str(acct.get("service_account_file") or "").strip()
        if not sa:
            continue
        if str(acct.get("mailbox") or "").strip().lower() == key:
            return sa
        fallback = fallback or sa
    if not fallback:
        raise RuntimeError(
            "找不到 service account 金鑰（rag_sync_targets.json 的 gmail_accounts "
            "是空的，也沒設 RED_PAYMENT_NOTICE_SA_FILE）")
    return fallback


def _gmail_users(mailbox: str):
    """回該信箱的 Gmail ``users()`` resource（網域委派冒充 mailbox）。"""
    from agent_core.google_auth import get_service_for_account

    key = (mailbox or "").strip().lower()
    service = get_service_for_account(
        f"payment_notice:{key}", "gmail", "v1",
        service_account_file=_sa_file(key), subject=key, scopes=None,
    )
    return service.users()


def _headers_of(msg: dict) -> dict[str, str]:
    return {h.get("name", ""): h.get("value", "")
            for h in (msg.get("payload", {}) or {}).get("headers", [])}


def _html_to_text(raw: str) -> str:
    """銀行通知只有 text/html（實測 fx-desk 那封整封就一個 text/html part）。

    表格欄位靠 ``</td>`` → 空白、``</tr>`` → 換行還原成「欄名 值」的行，
    下游的 ``_labelled_fields`` 才抓得到。
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"</(?:td|th)>", " \t", text, flags=re.I)
    text = re.sub(r"<br\s*/?>|</(?:tr|div|p|table|h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\xa0　]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _body_text(payload: dict) -> str:
    """整封信的純文字（text/plain 優先，沒有才用 text/html 轉）。"""
    plains: list[str] = []
    htmls: list[str] = []

    def walk(part: dict) -> None:
        mime = str(part.get("mimeType") or "")
        data = ((part.get("body") or {}).get("data") or "")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 —— 壞掉的 part 跳過，別讓整封讀不到
                decoded = ""
            if decoded:
                if mime == "text/plain":
                    plains.append(decoded)
                elif mime == "text/html":
                    htmls.append(decoded)
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload or {})
    if plains:
        return "\n".join(plains)
    return _html_to_text("\n".join(htmls)) if htmls else ""


def _labelled_fields(text: str, labels: dict[str, str]) -> dict[str, str]:
    """從「欄名 值」的行抽欄位。值取到行尾，抓不到的欄位就不放進 dict。

    ⚠️ label 是 **regex 片段**不是純字串（``Remitter.s Name`` 要吃掉撇號那格）。
    加新欄位時若欄名含 regex 特殊字元，自己 escape。

    銀行信末的「說明」段落也會提到欄名（「匯款生效日期：『匯款行』指定之解款
    日…」），這裡取**第一個**命中——表格排在說明前面，所以拿到的是真值。
    """
    out: dict[str, str] = {}
    for field, label in labels.items():
        m = re.search(label + r"[ \t:：]*([^\n]*)", text)
        if m:
            value = m.group(1).strip(" \t:：|")
            if value:
                out[field] = value
    return out


def _multiline_field(text: str, label: str, *, stop: tuple[str, ...],
                     max_lines: int = 4) -> str:
    """取「欄名 值」但值會**跨行續寫**的欄位，收到下一個已知欄名／段落標題為止。

    銀行把客戶的匯款附言原樣塞進表格，長一點就換行（實測 invoice 號被切成兩
    行）。只取一行＝把第二張發票號吃掉，而那是會計拿去銷帳的東西。
    """
    m = re.search(label + r"[ \t:：]*([^\n]*)((?:\n[^\n]*)*)", text)
    if not m:
        return ""
    lines = [m.group(1).strip(" \t:：|")]
    for line in (m.group(2) or "").split("\n")[1:max_lines]:
        stripped = line.strip()
        if not stripped or any(stripped.startswith(s) for s in stop):
            break
        lines.append(stripped)
    return " ".join(p for p in lines if p).strip()


# ────────────────────────────────────────────────────────────────────
# 已推播狀態
# ────────────────────────────────────────────────────────────────────
def _load_seen() -> dict:
    if not os.path.exists(_SEEN_PATH):
        return {"version": 1, "seen": {}, "updated_at": ""}
    try:
        with open(_SEEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("格式不是 dict")
        data.setdefault("seen", {})
        data.setdefault("version", 1)
        return data
    except Exception as exc:  # noqa: BLE001
        # 讀不到就當空的會**重推一輪**（吵，但不會漏）。相反的做法（當成全部
        # 都推過）會靜靜吃掉真實的到帳通知，那個代價高得多。
        logger.warning("payment_notice_seen 讀取失敗（%s），本輪視為全新", exc)
        return {"version": 1, "seen": {}, "updated_at": ""}


def _prune_seen(seen: dict[str, Any], now: datetime) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, rec in seen.items():
        try:
            age = (now - datetime.fromisoformat(str(rec.get("at") or ""))).days
        except Exception:  # noqa: BLE001 —— 壞掉的時間戳當作剛寫入，下輪再說
            age = 0
        if age <= _SEEN_TTL_D:
            kept[key] = rec
    return kept


def _save_seen(data: dict) -> bool:
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(os.path.dirname(_SEEN_PATH), exist_ok=True)
        _atomic_write_text(_SEEN_PATH, json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("payment_notice_seen 寫入失敗（%s）", exc)
        return False


# ────────────────────────────────────────────────────────────────────
# 掃描 + 排版
# ────────────────────────────────────────────────────────────────────
def _gmail_query(days: int) -> str:
    """一次撈完兩類候選：銀行匯入款通知 + 付款通知句型。

    Gmail 端先粗篩（省下逐封 get 的 RPC），真正的判準是 ``classify_notice``。
    刻意不加 ``is:unread``：大王常常已經先讀過了，未讀與否跟「會計該不該知道」
    無關。
    """
    return (
        f"newer_than:{days}d ("
        "subject:匯款 OR subject:貨款 OR subject:付款 OR subject:水單 OR "
        "subject:inward OR subject:remittance OR subject:payment OR "
        'subject:"notice of transfer" OR subject:"avis de virement"'
        ")"
    )


# 匯入款裡確定**不是**客戶貨款的那幾種——判準取自匯款人戶名／摘要的原文字樣，
# 不是「這個名字看起來不像客戶」的推測。實測 12 個月的國內匯入通知裡，7 筆有
# 5 筆是這類（信保手續費退費、營所稅退稅）。命中的只是**移到另一區**、仍然列
# 出來，不是丟掉——會計要對的是全部入帳。
_NON_CUSTOMER_INFLOW = re.compile(
    r"信保(?:手續費)?退費|信用保證基金|退稅|退還稅款|利息(?:收入)?|存款利息|手續費退")


def _is_non_customer_inflow(fields: dict[str, str]) -> bool:
    blob = f"{fields.get('remitter', '')} {fields.get('remark', '')}"
    return bool(_NON_CUSTOMER_INFLOW.search(blob))


def _fmt_money(fields: dict[str, str]) -> str:
    currency = fields.get("currency", "")
    amount = fields.get("amount", "")
    if amount and currency:
        return f"{currency} {amount}"
    return amount or currency or "（信中未列金額）"


def _render_bank(rule: dict, fields: dict[str, str], head: dict[str, str],
                 when: str) -> str:
    """銀行通知：欄位照表念。抓不到的欄位寫明「信中未列」，不留空、不猜。"""
    lines = [
        f"💰 {rule['label']}　{_fmt_money(fields)}",
        f"　匯款人：{sanitize_for_llm(fields.get('remitter', '（信中未列）'))}",
    ]
    if fields.get("payee"):
        lines.append(f"　受款人：{sanitize_for_llm(fields['payee'])}")
    if fields.get("value_date"):
        lines.append(f"　匯款生效日：{sanitize_for_llm(fields['value_date'])}")
    if fields.get("ref"):
        lines.append(
            f"　{fields.get('ref_label') or '編號'}：{sanitize_for_llm(fields['ref'])}")
    if fields.get("remark"):
        lines.append(
            f"　付款明細／摘要：{sanitize_for_llm(fields['remark'])[:_REMARK_MAX]}")
    lines.append(f"　信件：{when}　{sanitize_for_llm(head.get('Subject', ''))[:80]}")
    return "\n".join(lines)


def _render_advice(rule: dict, head: dict[str, str], snippet: str,
                   when: str) -> str:
    """客戶付款通知：不 parse 金額（各家格式不一，猜錯比不寫更糟），列原文摘要。"""
    return "\n".join([
        f"📨 {rule['label']}",
        f"　寄件人：{sanitize_for_llm(head.get('From', ''))[:80]}",
        f"　主旨：{sanitize_for_llm(head.get('Subject', ''))[:120]}",
        f"　摘要：{sanitize_for_llm(snippet)[:_REMARK_MAX]}",
        f"　信件：{when}（金額／單號請開原信或附件確認，本則未解析）",
    ])


def _scan_mailbox(mailbox: str, days: int, seen: dict[str, Any],
                  now: datetime) -> tuple[list[dict], list[str], str]:
    """掃一個信箱，回 (新命中的 notice list, 看過但沒命中的 key, 錯誤訊息或空字串)。

    第二個回傳值是為了**省 API**：粗篩每輪都會撈回同一批不相干的信（催款
    thread、付款申請…），不記下來的話每輪都要把它們全部重 get 一次。記成
    「看過了」之後，每輪只 get 真正新進來的信。

    ⚠️ 代價：規則改了之後，之前判定不命中的信不會被重新檢查。改規則要補抓歷史
    就刪掉 ``var/state/payment_notice_seen.json`` 讓它重掃一輪。
    """
    try:
        users = _gmail_users(mailbox)
    except Exception as exc:  # noqa: BLE001 —— 設定/授權問題要看得見，不吃掉
        return [], [], f"{mailbox}：{exc}"

    try:
        listed = users.messages().list(
            userId="me", q=_gmail_query(days), maxResults=_MAX_PER_MAILBOX,
        ).execute()
    except Exception as exc:  # noqa: BLE001
        return [], [], f"{mailbox}：Gmail 查詢失敗（{type(exc).__name__}: {exc}）"

    ids = [m["id"] for m in (listed.get("messages") or []) if m.get("id")]
    if not ids:
        return [], [], ""

    deadline = time.monotonic() + _MAILBOX_DEADLINE_S
    found: list[dict] = []
    examined: list[str] = []
    for mid in ids:
        if time.monotonic() > deadline:
            return found, examined, f"{mailbox}：逾時，本輪只讀完部分候選（下輪補上）"
        if f"{mailbox}:{mid}" in seen:
            continue
        try:
            msg = users.messages().get(userId="me", id=mid, format="full").execute()
        except Exception as exc:  # noqa: BLE001 —— 單封讀不到就跳過，下輪還會再遇到
            # 但要留痕：這裡靜默跳過等於**漏掉一筆客戶貨款通知**，而收件人不會
            # 知道少了什麼。訊息量有界（每輪最多掃幾十封），不會洗版。
            logger.debug("payment_notice 讀取訊息 %s 失敗，本輪跳過：%s", mid, exc)
            continue
        head = _headers_of(msg)
        rule = classify_notice(head.get("From", ""), head.get("Subject", ""))
        if not rule:
            examined.append(f"{mailbox}:{mid}")
            continue
        # 同一封信寄給 UserAng、副本給大王時，兩個信箱各有一個 Gmail id，但
        # RFC 的 Message-ID 相同 —— 用它當去重鍵，會計才不會收到兩則一樣的。
        # 沒有這個標頭（極少見）才退回 mailbox:id。
        msg_id = (head.get("Message-ID") or head.get("Message-Id") or "").strip()
        state_key = f"msgid:{msg_id}" if msg_id else f"{mailbox}:{mid}"
        if state_key in seen:
            continue
        when = _msg_when(msg.get("internalDate"))
        if rule["kind"] == "bank":
            parser: Callable[[str], dict[str, str]] | None = rule.get("parser")
            fields = parser(_body_text(msg.get("payload") or {})) if parser else {}
            body = _render_bank(rule, fields, head, when)
            kind = "other_inflow" if _is_non_customer_inflow(fields) else "bank"
        else:
            body = _render_advice(rule, head, msg.get("snippet", ""), when)
            kind = "advice"
        found.append({
            "state_key": state_key,
            # 同一輪裡另一個信箱又撞到同一封時，_scan_mailbox 之間靠這把 key
            # 在呼叫端過濾（狀態檔要跑完整輪才寫）。
            "mailbox": mailbox,
            "kind": kind,
            "rule": rule["key"],
            "subject": head.get("Subject", ""),
            "at": now.isoformat(timespec="seconds"),
            "text": body,
        })
        # 命中的信也記一份 Gmail id：下輪在 get 之前就擋掉（msgid 那把鍵要 get
        # 完才拿得到），省一次 API。
        examined.append(f"{mailbox}:{mid}")
    return found, examined, ""


def _msg_when(internal_date_ms: Any) -> str:
    try:
        return datetime.fromtimestamp(
            int(internal_date_ms) / 1000).strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def scan_payment_notices(days: int = 3, persist: bool = True) -> str:
    """掃 UserAng／大王信箱的「客戶貨款到帳／付款通知」，只回**還沒通知過**的那些。唯讀，免 +確認。

    抓兩類（都是 allowlist，寧可漏不可錯）：①銀行匯入款通知（第一銀行國外/國內
    匯入、國泰世華外匯匯入）—— 逐欄列出匯款人／金額／生效日／付款明細；②客戶自己
    寄的付款通知（Supremo「Payment on …」、Decathlon「Notice of transfer」、以及
    明講「payment has been completed／已匯款」的外部來信）。

    ⚠️ 匯款人欄位**照表念**：匯入款裡混著關係企業匯款、信保手續費退費、退稅，
    工具不替它判斷是不是客戶貨款，看匯款人與摘要自己確認。

    每封只報一次（狀態存 ``var/state/payment_notice_seen.json``）；沒有新的就回
    「(無新發現)」。

    Args:
        days: 往回看幾天的信（預設 3，上限 30）。狀態檔負責去重，天數放寬只是
            讓漏掉的那輪補得回來。
        persist: 是否把本輪推播過的信寫進狀態檔（預設 True；預覽/測試傳 False
            就不會把信「用掉」）。
    Returns:
        排版好的純文字通知；沒有新的回「(無新發現)」。
    """
    days = max(1, min(int(days or 3), 30))
    now = datetime.now()
    state = _load_seen()
    seen: dict[str, Any] = state.get("seen") or {}

    notices: list[dict] = []
    errors: list[str] = []
    examined: list[str] = []
    this_round: set[str] = set()
    for mailbox in PAYMENT_MAILBOXES:
        found, checked, err = _scan_mailbox(mailbox, days, seen, now)
        examined.extend(checked)
        for notice in found:
            # 跨信箱同一封（To UserAng / CC 大王）只留第一次看到的那則。
            if notice["state_key"] in this_round:
                continue
            this_round.add(notice["state_key"])
            notices.append(notice)
        if err:
            errors.append(err)

    if persist and examined:
        # 沒命中的信也要記（省下每輪重 get 同一批不相干的信）。就算本輪一則
        # 通知都沒有也要寫檔——那正是最常見的情況。
        stamp = now.isoformat(timespec="seconds")
        for key in examined:
            seen.setdefault(key, {"at": stamp, "matched": False})
        state["seen"] = _prune_seen(seen, now)
        _save_seen(state)

    if not notices:
        if errors:
            # 全部信箱都掛掉時要出聲——安靜的「沒有新到帳」跟「讀不到信箱」長得
            # 一樣，後者悄悄持續下去就等於這條通知線死了沒人知道。
            return "⚠️ 貨款到帳通知：本輪沒有新通知，但信箱查詢有問題：\n" + "\n".join(
                f"  - {e}" for e in errors)
        return "(無新發現)"

    banks = [n for n in notices if n["kind"] == "bank"]
    advices = [n for n in notices if n["kind"] == "advice"]
    others = [n for n in notices if n["kind"] == "other_inflow"]
    parts: list[str] = [f"💵 客戶貨款通知 {len(notices)} 則（{now.strftime('%m/%d %H:%M')}）"]
    if banks:
        parts.append("──── 銀行匯入款通知 ────")
        parts.extend(n["text"] for n in banks)
    if advices:
        parts.append("──── 客戶付款通知（來信） ────")
        parts.extend(n["text"] for n in advices)
    if others:
        parts.append("──── 其他匯入款（退費／退稅類，非客戶貨款） ────")
        parts.extend(n["text"] for n in others)
    parts.append(
        "（來源：UserAng twsales@ / 大王 owner@ 信箱，銀行欄位照原信逐欄擷取；"
        "匯入款不等於客戶貨款，請看匯款人與摘要確認。）")
    if errors:
        parts.append("⚠️ 另有信箱查詢問題：" + "；".join(errors))

    if persist:
        for n in notices:
            seen[n["state_key"]] = {"at": n["at"], "subject": n["subject"][:120],
                                    "rule": n["rule"], "matched": True}
        state["seen"] = _prune_seen(seen, now)
        _save_seen(state)

    return "\n".join(parts)


def payment_notice_alert() -> str:
    """排程用的無參數版本（``deterministic_tool``，完全不經 LLM）。

    回溯天數讀 ``RED_PAYMENT_NOTICE_DAYS``（預設 3 天）。走確定性路徑是刻意的：
    金額、匯款人、單號讓 LLM 轉述一次就有講錯的風險，這條線寧可少一層。

    Returns:
        同 :func:`scan_payment_notices`；沒有新通知時回「(無新發現)」。
    """
    return scan_payment_notices(
        days=env_int("RED_PAYMENT_NOTICE_DAYS", 3, min_value=1, max_value=30),
    )


# 純讀 → 背景 dispatcher 的排程任務可用（safe_tools 的第二條路徑）。
scan_payment_notices.background_safe = True
payment_notice_alert.background_safe = True
