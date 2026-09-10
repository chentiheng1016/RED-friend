"""Supremo（Lurchi）出貨文件要求的追蹤／提醒／查核／結案管線。

需求（2026-08-20，大王）：Supremo 的 ContactW Cheung 對每一櫃出貨都有「正本文件
情況」的固定要求（實測通知信 2026-08-20「RE: LURCHI-CONT7 // 2454 PRS //
ETD 07 AUG 2026」與同日 CONT8 那封，內容逐年相同）：

  * 文件須要**電郵給兩位 WENDY**（shipping-contact@customer-a.example 與
    docs-contact@customer-a.example）——寄一位不算數。
  * 工廠必須提供**電子版裝箱單（EXCEL）**；船務文件含 INVOICE / PACKING LIST /
    CO / CO-FTA / BL / EBL / FCR / SWB…，及報關單／預錄單＋通知書。
  * 船開 **5 天內**電郵副本文件（掃瞄），**10 天內**正本寄達 Supremo。
  * 延誤出運／延誤文件／文件出錯都會產生罰款與額外操作費，由工廠自行負責。

流程（大王指定）：時間到 → Telegram 問業務（橙色，UserAng）是否已按要求提供
文件 → UserAng 在 Telegram 回覆 → 查核 email 是否真的辦理 → 辦理好才結案。

## 新櫃自動建案（不必有人記得加）

每天那輪會先掃 twsales@ 裡 ContactW 近 30 天（``RED_SHIPDOC_NOTICE_DAYS``）的
「正本文件情況」通知信，解析出櫃號／ETD／雙數自動加進追蹤——她每 1–2 週發一
櫃（近 60 天 CONT3/4/5/7/8/10 六封），靠人記得加一定會漏，而漏掉的那櫃是完全
靜默的（追蹤器裡沒有＝不問、不查、不催）。⚠️ 解析不出櫃號或 ETD 就**照實回報
請人補**，絕不猜：主旨的「21.08.2026」點分日期是**發文日不是 ETD**（實測
CONT10 那封真 ETD 10 AUG 2026 在內文，拿發文日當 ETD 會讓期限晚 11 天）。

## 查核的口徑（員工零幻覺：照信件事實講，不替人下判斷）

「有辦理」= twsales@（UserAng）或 shipping@（船務 Ms. Hao）的**寄件備份**裡，
查得到「帶附件、寄給兩位 ContactW（To＋Cc 合併算）、主旨含該櫃號」的信。兩位
ContactW 可以由多封信分別涵蓋（實務上 To contact-w.cheung、Cc contact-w.law 一封搞定，
但補寄也算）。結案分兩條路：

  A. **主旨含櫃號**的寄件把兩位 ContactW 都涵蓋 → 自動結案（郵件已查核）。
  B. 業務在 Telegram 回覆「已處理」（confirm_shipping_docs 記錄）＋查得到
     帶附件寄給兩位 ContactW 的寄件（主旨沒帶櫃號也算）→ 結案（業務確認＋查有
     寄件）。只有業務口頭確認、信箱查無 → **不結案**，照實回報請她確認。

## 正本文件（罰款那條）

正本走實體快遞，但兩個訊號 email 裡都有：我方寫「快遞單號 SF…／AWB CX…」、
客人回「今天簽收文件 24 AUG 2026」。狀態 pending → sent → acked，過了 ETD+10
未簽收轉 overdue。**獨立於副本是否結案**——副本電郵結案 ≠ 正本到了。
⚠️ 三個讀錯就會謊報「正本已到」的陷阱（都有回歸測試）：客人也會寫「未收到
文件／請馬上補回」那是**催件**；回信會整串引用舊信（先切引言再判讀）；併櫃信
裡「SF…A for CONT8,9,10」「SF…B for Lurchi CONT-7」兩組並存，不解析對應就會
把別櫃的單號掛到這一櫃。我方自述「已於8/21簽收」列為 ``acked_by_us``、訊息
明講來源是我方而非客人，不冒充客人確認。

## 升級線

橙色每天問業務；逾期超過 ``RED_SHIPDOC_ESCALATE_D``（預設 3）天仍未結，由
另一支排程 ``shipping_doc_escalation``（每天 10:00、推紅色）通知大王。它**只讀
本模組寫好的狀態、不掃信箱**。做成獨立 task 是因為 dispatcher 的
``notify_agent_colors`` 是靜態設定，主任務加 red 會變成大王天天收例行提醒。

## 只問一次（每天）

每櫃每天最多推一則訊息（asked_dates 記日期）；沒有到期的櫃、或全部結案時回
「(無新發現)」，dispatcher 整輪安靜。櫃號逐年重複使用（2022–2026 年年都有
LURCHI-CONT7），查核視窗鎖在 ETD 前 21 天起，舊年份的同名信不會被撿進來。

⚠️ 本模組**唯讀信箱、只寫自己的狀態檔**：不寄信、不改標籤。推播是排程 task
的 ``notify_channel=telegram`` + ``notify_agent_colors=["orange"]``
（``scripts/register_shipping_doc_task.py``），與這裡無關。

⚠️ confirm_shipping_docs / shipping_doc_status 會進**橙色員工 freeform 白名單**
（dept_tool_scope）：兩顆都只回「這幾櫃的文件寄了沒」這種窄事實（固定收件人、
固定查詢），不開放任意讀信箱——那條線（read_gmail 之類）仍然不進員工 session。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta

from agent_core.env_utils import env_int
from agent_core.logging_and_paths import STATE_DIR, logger
from agent_core.prompt_injection import sanitize_for_llm
from agent_core.state_io import locked_json

# ── 客戶要求（來源：ContactW Cheung 2026-08-20「正本文件情況」通知信）──
# 文件必須電郵給的兩位收件人。缺一位 = 沒按要求辦理。
DOC_RECIPIENTS = ("shipping-contact@customer-a.example", "docs-contact@customer-a.example")
# 會寄出貨文件的我方信箱（UserAng 台北業務、越南船務 Ms. Hao）。
DOC_SENDER_MAILBOXES = ("twsales@company.example", "shipping@company.example")
_SCAN_DEADLINE_D = 5      # 船開後幾天內須電郵副本文件（掃瞄）
_ORIGINALS_DEADLINE_D = 10  # 船開後幾天內正本須寄達 Supremo

# 給提醒訊息用的要求摘要（照通知信原文濃縮，不加詮釋）。
_REQUIREMENT_SUMMARY = (
    "Supremo 文件要求：①文件電郵給兩位 ContactW（contact-w.cheung@ 與 contact-w.law@"
    "customer-a.example，缺一不可）②電子版裝箱單 EXCEL ③船務文件（INVOICE / PACKING "
    "LIST / CO / CO-FTA / BL / EBL / FCR / SWB…）及報關單/預錄單＋通知書 "
    "④船開 5 天內電郵副本（掃瞄）、10 天內正本寄達 ⑤延誤或出錯的罰款由工廠負責"
)

_STATE_PATH = os.path.join(STATE_DIR, "shipping_doc_tracker.json")

# 查核視窗：ETD 前幾天起算（文件常在船開前先寄草稿確認）。同名櫃號逐年重複
# （2022–2026 每年都有 LURCHI-CONT7），視窗同時是「別把舊年份撿進來」的閘。
_WINDOW_BEFORE_ETD_D = 21
# 主旨沒帶櫃號的「候選寄件」要在 ETD 前 7 天之後才算——太早的多半是上一櫃的。
_CANDIDATE_WINDOW_BEFORE_ETD_D = 7
# ⚠️ 候選也要有**上界**：正本期限是 ETD+10，之後還在飛的信多半在講下一櫃。
# 沒有上界時實測 CONT5（ETD 07-23）把 08-24 那封講 CONT9+10 的信也算成自己的
# 候選——配上「業務確認＋候選」那條結案路徑，等於舊櫃可能被一個月後的無關信件
# 結掉。30 天給補寄留足餘裕，又擋掉隔了一整櫃的信。
_CANDIDATE_WINDOW_AFTER_ETD_D = 30
# 單輪單信箱最多逐封讀幾封（硬上限）。⚠️ 這是**上限不是頁數**：Gmail 的 list
# 一頁最多 100 筆，所以要自己翻頁翻到底，翻不完要出聲。
# 舊值 40 沒翻頁、也沒警告 —— 實測 2026-08-26 twsales@ 該視窗有 47 封符合，
# 等於每輪靜默丟掉 7 封；而 Gmail 是**新到舊**排序，被丟掉的正是最舊的那幾封，
# 也就是舊櫃最需要的證據。追的櫃愈多、視窗往前愈寬，就愈容易誤判成「沒寄」。
_MAX_PER_MAILBOX = env_int("RED_SHIPDOC_MAX_PER_MAILBOX", 200,
                           min_value=20, max_value=1000)
_LIST_PAGE_SIZE = 100      # Gmail messages.list 單頁上限
# 每信箱軟時間預算。逐封 metadata get 約 0.3–0.4s，讀滿 _MAX_PER_MAILBOX(200)
# 要 ~70s，所以不能沿用 payment_notice 的 45s ——那會讓「翻頁翻到底」白做，
# 每輪改成卡在逾時。dispatcher 給單一任務 900s（send timeout 600+300），
# 兩個信箱各 90s ＋ 通知信掃描，離上限還很遠。
_MAILBOX_DEADLINE_S = float(env_int("RED_SHIPDOC_MAILBOX_DEADLINE_S", 90,
                                    min_value=15, max_value=600))
_EVIDENCE_CAP = 10          # 狀態檔每櫃最多留幾筆查核證據

_ADDR_RE = re.compile(r"[\w\.\-\+]+@[\w\.\-]+")


# ────────────────────────────────────────────────────────────────────
# 櫃號比對
# ────────────────────────────────────────────────────────────────────
def _norm(text: str) -> str:
    """大寫、只留英數——主旨與櫃號兩邊都先過這個再比對。"""
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


# 主旨裡的櫃號。吃得下 CONT7 / CONT 10 / CNT-8 三種寫法，以及**多櫃併成一封**
# 的列舉：「CONT9+ 10」「CONT8,9,10」「CONT 9 and 10」——UserAng 常把幾櫃的文件
# 併在同一封寄（實測 2026-08-21「LURCHI ( KIENAST)-CONT9+ 10 and Lurchi CNT-8
# shipping documents」一封涵蓋三櫃）。逐櫃比對全名字串會把這種信整封漏掉，
# 於是「文件其實寄了」卻天天催業務。數字限 1–2 位，別把年份吃進來。
_CONT_RUN = re.compile(
    r"C(?:O)?NT\s*[-–—]?\s*(\d{1,2}(?:\s*(?:[,+&/]|＋|and)\s*\d{1,2})*)",
    re.I,
)
# 抓品牌 token 時要跳過的主旨前綴字。
_SUBJ_STOPWORDS = frozenset({"RE", "FW", "FWD", "DRAFT", "DOCS", "SHIPPING",
                             "DOCUMENTS", "CONT", "CNT"})


def _container_numbers(subject: str) -> set[int]:
    """主旨列到的所有櫃次。「CONT9+ 10 and Lurchi CNT-8」→ {8, 9, 10}。"""
    out: set[int] = set()
    for m in _CONT_RUN.finditer(str(subject or "")):
        for n in re.findall(r"\d{1,2}", m.group(1)):
            out.add(int(n))
    return out


def _ref_parts(ref: str) -> tuple[str, int | None]:
    """櫃號 → (品牌 token, 櫃次)。

    「LURCHI-CONT7」→ ("LURCHI", 7)；「LURCHI ( KIENAST)-CONT 10」→
    ("LURCHI", 10)（品牌取第一個非前綴字的英文 token，KIENAST 是副標不是品牌）。
    """
    text = str(ref or "").upper()
    nums = _container_numbers(text)
    num = min(nums) if nums else None
    brand = ""
    for tok in re.findall(r"[A-Z]{3,}", text):
        if tok not in _SUBJ_STOPWORDS:
            brand = tok
            break
    return brand, num


def _subject_matches(ref: str, subject: str) -> bool:
    """這封信的主旨是不是在講這一櫃：品牌 token 命中**且**櫃次在主旨的櫃號集合裡。

    比「整串櫃號當子字串找」硬得多也鬆得剛好：擋掉 CONT7 撞 CONT8（櫃次不同），
    也接得住 CONT9+10 這種併寄（櫃次在集合裡）。ref 沒有櫃次時（自訂代號）退回
    整串比對。
    """
    brand, num = _ref_parts(ref)
    if num is None:
        needle = _norm(ref)
        return bool(needle) and needle in _norm(subject)
    if brand and brand not in _norm(subject):
        return False
    return num in _container_numbers(str(subject or ""))


# ────────────────────────────────────────────────────────────────────
# ContactW 的「正本文件情況」通知信 → 自動建案
# ────────────────────────────────────────────────────────────────────
# 沒有這段，每個新櫃都要有人記得手動 track——實測 ContactW 每 1–2 週發一櫃
# （近 60 天 CONT3/4/5/7/8/10 六封），靠人記得一定會漏，而漏掉的那櫃是
# 完全靜默的（追蹤器裡沒有＝不會問、不會查、不會催）。
_NOTICE_SENDER = "shipping-contact@customer-a.example"
# 內文要出現這兩個字樣之一才算通知信（她也會寄別的信）。
_NOTICE_MARKERS = ("正本文件情況", "文件須要電郵給兩位")

# 第二個來源：越南船務 Ms. Hao 把該櫃文件寄給 UserAng 確認（主旨如
# 「LURCHI-CONT7」「DRAFT DOCS LURCHI-CONT 3」，內文只有 "Pls check & CFM"）。
# 這比 ContactW 的通知**早好幾天**出現，是「這一櫃開始跑文件了」的最早訊號。
# ⚠️ 她的信裡**沒有 ETD**（實測），所以只能建立「有這個櫃、請補 ETD」的待辦，
# 不能自己生期限。已經被 ContactW 通知建好案的櫃不會重複報（第二趟會過濾）。
_DRAFT_SENDER = "shipping@company.example"
_DRAFT_SUBJECT_RE = re.compile(r"DRAFT\s+DOCS|CONT", re.I)
# 🚨 Ms. Hao 也負責別的客戶（實測 JALAS-CONT4/CONT5）。這整套規則是 **Supremo**
# 的文件要求（兩位 ContactW、ETD+5/+10、罰款條款），套到別的客戶頭上就是拿錯規則
# 去催人。草稿信只認這些品牌；要納入新品牌先確認對方的要求是否相同。
_DRAFT_BRANDS = frozenset(
    b.strip().upper()
    for b in os.environ.get("RED_SHIPDOC_DRAFT_BRANDS", "LURCHI").split(",")
    if b.strip())

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# ⚠️ **只認「日 月名 年」這種帶英文月名的寫法**（07 AUG 2026 / 23 JULY 2026）。
# 主旨裡另一種「21.08.2026」點分格式是**發文日不是 ETD**——實測 CONT10 那封
# 主旨寫「// 21.08.2026」，真正的 ETD 10 AUG 2026 在內文。拿發文日當 ETD 會
# 讓期限整整晚 11 天，催信全部失準。帶月名的格式天生排除點分日期。
_DATE_SPACED = r"(\d{1,2})\s+([A-Z]{3,9})\.?\s+(\d{4})"
_ETD_LABELLED = re.compile(r"ETD\s*[:：]?\s*" + _DATE_SPACED, re.I)
_ETD_BARE = re.compile(_DATE_SPACED, re.I)
_PAIRS_RE = re.compile(r"(\d[\d,]{2,})\s*PRS", re.I)

_NOTICE_TTL_D = 180        # 處理過的通知信記多久（狀態檔不無限長大）
_MAX_NOTICES = 30          # 單輪最多讀幾封候選通知信


def _spaced_date(match: "re.Match | None") -> date | None:
    if match is None:
        return None
    day, mon, year = match.group(1), match.group(2), match.group(3)
    month = _MONTHS.get(mon[:3].upper())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _first_valid_date(pattern: "re.Pattern", text: str) -> date | None:
    """掃過所有命中，回第一個真的能組成日期的（月名打錯的那筆跳過不算）。"""
    for m in pattern.finditer(str(text or "")):
        parsed = _spaced_date(m)
        if parsed is not None:
            return parsed
    return None


def _all_dates(text: str) -> set[date]:
    """文字裡所有「日 月名 年」格式的日期（點分格式天生不會命中，見上方註解）。"""
    out: set[date] = set()
    for m in _ETD_BARE.finditer(str(text or "")):
        parsed = _spaced_date(m)
        if parsed is not None:
            out.add(parsed)
    return out


def _sole_etd(subject: str, body: str) -> date | None:
    """整封信是否只指向**一個** ETD？是就回它，否則回 None（不猜）。

    先看有沒有明寫 ETD 的；有多個不同的明寫值就算歧義。都沒明寫時退回主旨裡
    的裸日期（同 parse_doc_notice 的優先序）。
    """
    labelled: set[date] = set()
    for text in (subject, body):
        for m in _ETD_LABELLED.finditer(str(text or "")):
            parsed = _spaced_date(m)
            if parsed is not None:
                labelled.add(parsed)
    if labelled:
        return next(iter(labelled)) if len(labelled) == 1 else None
    bare = _all_dates(subject)
    return next(iter(bare)) if len(bare) == 1 else None


def parse_doc_notice(subject: str, body: str) -> dict:
    """ContactW 通知信 (主旨, 內文) → {ref, etd, pairs, problem}。純函式、無 IO。

    ``problem`` 非空表示**不要自動建案**（抓不到櫃號／ETD、或一封涵蓋多櫃），
    呼叫端要把它照實報出來讓人補，不可自己猜——猜錯 ETD 等於整條期限失準。
    """
    subject = str(subject or "")
    body = str(body or "")
    nums = _container_numbers(subject)
    brand, _ = _ref_parts(subject)
    if not nums:
        # ⚠️ 這**不是**錯誤：ContactW 把「正本文件情況／注意事項」那段當罐頭簽名附在
        # 很多信上（付款清單、驗貨、款號詢問…實測近 60 天 13 封裡有 3 封是這種）。
        # 主旨沒有櫃號＝這封不是在講某一櫃，安靜跳過，不要報成「待人工處理」。
        return {"ref": "", "etd": None, "pairs": "",
                "problem": "主旨看不出櫃號", "problem_kind": "no_ref"}
    if len(nums) > 1:
        # 一封涵蓋多櫃：信裡**只有一個 ETD** 時就一次建齊（那個 ETD 是整封的
        # 共同前提，不是猜的）；有多個或沒有 ETD 才丟人工——各櫃 ETD 未必相同，
        # 分配錯會讓期限整批失準。
        etd_one = _sole_etd(subject, body)
        listed = "、".join(f"CONT{n}" for n in sorted(nums))
        if etd_one is None:
            return {"ref": "", "etd": None, "pairs": "", "numbers": sorted(nums),
                    "problem": f"一封通知涵蓋多櫃（{listed}），且信中沒有唯一的 ETD，"
                               "請人工確認各櫃 ETD",
                    "problem_kind": "multi"}
        pairs_m = _PAIRS_RE.search(subject) or _PAIRS_RE.search(body)
        return {"ref": "", "etd": etd_one.isoformat(),
                # 雙數是整封的總數、不屬於任何單櫃，多櫃時刻意不填。
                "pairs": "", "numbers": sorted(nums), "brand": brand,
                "problem": "", "problem_kind": "multi_same_etd",
                "total_pairs": f"{pairs_m.group(1)} PRS" if pairs_m else ""}
    num = next(iter(nums))
    ref = f"{brand}-CONT{num}" if brand else f"CONT{num}"
    # ETD 取得優先序：主旨明寫 ETD → 內文明寫 ETD → 主旨的裸日期。
    etd = (_first_valid_date(_ETD_LABELLED, subject)
           or _first_valid_date(_ETD_LABELLED, body)
           or _first_valid_date(_ETD_BARE, subject))
    pairs_m = _PAIRS_RE.search(subject) or _PAIRS_RE.search(body)
    pairs = f"{pairs_m.group(1)} PRS" if pairs_m else ""
    if etd is None:
        return {"ref": ref, "etd": None, "pairs": pairs,
                "problem": "信中找不到 ETD（只有點分日期＝發文日，不能當 ETD）",
                "problem_kind": "no_etd"}
    return {"ref": ref, "etd": etd.isoformat(), "pairs": pairs,
            "problem": "", "problem_kind": ""}


def _fetch_notices(days: int, seen_ids: set[str]) -> tuple[list[dict], list[str]]:
    """讀 twsales@ 裡 ContactW 近 N 天的通知信，回 (已解析的 notice, 錯誤)。

    只讀 ``_NOTICE_SENDER`` 寄來、內文含 ``_NOTICE_MARKERS`` 的信；已處理過的
    message id 直接跳過（省 API 也避免重複報同一封）。
    """
    from agent_core.gmail_ops import extract_body

    mailbox = DOC_SENDER_MAILBOXES[0]  # twsales@ —— 兩個來源都寄給 UserAng
    try:
        users = _gmail_users(mailbox)
    except Exception as exc:  # noqa: BLE001
        return [], [f"通知信掃描（{mailbox}）：{exc}"]
    try:
        listed = users.messages().list(
            userId="me", maxResults=_MAX_NOTICES,
            q=f"(from:{_NOTICE_SENDER} OR from:{_DRAFT_SENDER}) newer_than:{days}d",
        ).execute()
    except Exception as exc:  # noqa: BLE001
        return [], [f"通知信查詢失敗（{type(exc).__name__}: {exc}）"]

    out: list[dict] = []
    deadline = time.monotonic() + _MAILBOX_DEADLINE_S
    for meta in listed.get("messages") or []:
        mid = meta.get("id")
        if not mid or mid in seen_ids:
            continue
        if time.monotonic() > deadline:
            return out, ["通知信掃描逾時，本輪只讀完部分（下輪補上）"]
        try:
            msg = users.messages().get(userId="me", id=mid, format="full").execute()
        except Exception as exc:  # noqa: BLE001
            logger.debug("shipping_doc 通知信 %s 讀取失敗：%s", mid, exc)
            continue
        head = _headers_of(msg)
        subject = head.get("subject", "")
        body = extract_body(msg.get("payload") or {})
        sender = head.get("from", "").lower()
        if _DRAFT_SENDER in sender:
            # 船務的草稿文件信：只當「這個櫃開始跑文件了」的早期訊號。
            brand, _ = _ref_parts(subject)
            if (not _DRAFT_SUBJECT_RE.search(subject)
                    or not _container_numbers(subject)
                    or brand.upper() not in _DRAFT_BRANDS):
                out.append({"id": mid, "skip": True})
                continue
            parsed = parse_doc_notice(subject, body)
            if parsed.get("problem_kind") in ("", "multi_same_etd"):
                # 罕見：草稿信裡竟然有 ETD → 照常建案。
                out.append({"id": mid, "skip": False, "subject": subject, **parsed})
            else:
                parsed["problem"] = ("船務已在跑這一櫃的文件，但信中沒有 ETD"
                                     "——請補 ETD 才能算期限")
                parsed["problem_kind"] = "no_etd"
                out.append({"id": mid, "skip": False, "subject": subject, **parsed})
            continue
        if not any(mark in body for mark in _NOTICE_MARKERS):
            # 不是通知信（她也寄別的），記下來免得每輪重讀。
            out.append({"id": mid, "skip": True})
            continue
        parsed = parse_doc_notice(subject, body)
        out.append({"id": mid, "skip": False, "subject": subject, **parsed})
    return out, []


def _discover_new_shipments(days: int) -> tuple[list[str], list[str], list[str]]:
    """掃通知信→自動建案。回 (新建案的說明, 需人工補的說明, 錯誤)。

    已在追蹤的櫃不會被動到（track_shipment 本身冪等，只更新 etd/雙數）。
    """
    state = _read_state()
    seen: dict = state.get("notices_seen") or {}
    notices, errors = _fetch_notices(days, set(seen))
    created: list[str] = []
    manual: list[str] = []
    if not notices:
        return created, manual, errors

    now = datetime.now()
    stamp = now.isoformat(timespec="seconds")
    tracked = set(state.get("shipments") or {})
    reported: set[str] = set()   # 同一件事一輪只講一次（她常一天發好幾封同櫃）

    def _already_tracked(ref: str) -> bool:
        return _find_ref({k: {} for k in tracked}, ref) is not None

    # ── 第一趟：先把解析乾淨的通知全部建案 ──
    # 兩趟是必要的：同一櫃她常發不只一封（一封帶 ETD、一封只有發文日），而
    # Gmail 回來的順序是時間序不是「好的排前面」。一趟做完會因為順序不同而
    # 忽報忽不報「請補 ETD」——那種只在某些日子出現的噪音最難查。
    # ⚠️ 單櫃通知**先跑**：那是該櫃自己的權威 ETD。多櫃信只有一個共同 ETD，
    # 實測「CONT 9+ 10 // ETD 10 AUG 2026」那封也提到 CNT-8，但 CONT8 自己的
    # 通知寫的是 15 AUG —— 讓多櫃信後跑、且只補「還沒有的櫃」，才不會把精確的
    # ETD 蓋成粗略的。
    ordered = ([n for n in notices if (n.get("problem_kind") or "") == ""]
               + [n for n in notices
                  if (n.get("problem_kind") or "") == "multi_same_etd"])
    for notice in ordered:
        kind = notice.get("problem_kind") or ""
        if notice.get("skip"):
            continue
        subject = sanitize_for_llm(str(notice.get("subject", "")))[:80]
        if kind == "multi_same_etd":
            # 一封涵蓋多櫃、但整封只有一個 ETD → 逐櫃建齊。
            brand = notice.get("brand") or ""
            made: list[str] = []
            for n in notice.get("numbers") or []:
                ref = f"{brand}-CONT{n}" if brand else f"CONT{n}"
                if _find_ref({k: {} for k in tracked}, ref) is not None:
                    continue      # 已有（多半來自它自己的通知）→ 不覆蓋 ETD
                try:
                    key, is_new = track_shipment(ref, notice["etd"])
                except ValueError as exc:  # noqa: BLE001
                    manual.append(f"　- 「{subject}」：ETD 解析結果無效（{exc}）")
                    break
                tracked.add(key)
                if is_new:
                    made.append(key)
            if made:
                total = (f"（同信共 {notice['total_pairs']}，"
                         "雙數為整封總數、未逐櫃拆分）"
                         if notice.get("total_pairs") else "")
                created.append(f"　- {'、'.join(made)}　ETD {notice['etd']}{total}")
            continue
        try:
            key, is_new = track_shipment(
                notice["ref"], notice["etd"], pairs=notice.get("pairs", ""))
        except ValueError as exc:  # noqa: BLE001 —— 壞日期只報不炸
            manual.append(f"　- 「{subject}」：ETD 解析結果無效（{exc}）")
            continue
        tracked.add(key)
        if is_new:
            pairs = f"　{notice['pairs']}" if notice.get("pairs") else ""
            created.append(f"　- {key}{pairs}　ETD {notice['etd']}")

    # ── 第二趟：剩下的問題信，對照「已追蹤」判斷還需不需要人工處理 ──
    for notice in notices:
        if notice.get("skip"):
            continue
        kind = notice.get("problem_kind") or ""
        subject = sanitize_for_llm(str(notice.get("subject", "")))[:80]
        # 罐頭簽名附在別的主題上 —— 安靜跳過（見 parse_doc_notice）。
        if not kind or kind in ("no_ref", "multi_same_etd"):
            continue
        if kind == "multi":
            nums = notice.get("numbers") or []
            # 這幾櫃**全都**已在追蹤時不必吵：多半是業務把幾櫃文件併成一封寄，
            # 那封信本身就是查核證據，不是「有新櫃要建」。
            if nums and all(_already_tracked(f"CONT{n}") for n in nums):
                continue
            dedupe = "multi:" + ",".join(str(n) for n in nums)
            if dedupe in reported:
                continue
            reported.add(dedupe)
            manual.append(f"　- 「{subject}」：{notice['problem']}")
            continue
        if kind == "no_etd":
            # 已經在追蹤的櫃不必再問 ETD（另一封通知已經給過正確的 ETD）。
            if _already_tracked(notice["ref"]) or notice["ref"] in reported:
                continue
            reported.add(notice["ref"])
            manual.append(f"　- 「{subject}」：{notice['problem']}")
            continue

    with locked_json(_STATE_PATH, default={"version": 1, "shipments": {}}) as st:
        live = st.setdefault("notices_seen", {})
        for notice in notices:
            live[notice["id"]] = {"at": stamp, "ref": notice.get("ref", "")}
        # 老紀錄修剪，狀態檔不無限長大。
        for mid, rec in list(live.items()):
            try:
                age = (now - datetime.fromisoformat(str(rec.get("at") or ""))).days
            except Exception:  # noqa: BLE001 —— 壞時間戳當剛寫入
                age = 0
            if age > _NOTICE_TTL_D:
                del live[mid]
    return created, manual, errors


# ────────────────────────────────────────────────────────────────────
# 正本文件（實體快遞）追蹤
# ────────────────────────────────────────────────────────────────────
# 客人的罰款條款主要就是這條：**船開 10 天內正本寄達**。副本電郵查得到、正本
# 走順豐查不到——但信裡其實兩個訊號都有：我方會寫「快遞單號 SF…」，客人會回
# 「今天簽收文件 24 AUG 2026」。兩邊都抓，才知道正本到底到了沒。
#
# 順豐/AWB 單號：實測「SF0214996834262」「SF0213866905821」與順豐 AWB
# 「CX3483757676544042」。長度取 10–22 位，別把發票號、金額吃進來。
_TRACKING_RE = re.compile(r"\b((?:SF|CX)\d{10,22})\b", re.I)

# 「正本已寄出」的說法（我方寄件裡出現才算）。
_DISPATCH_RE = re.compile(
    r"快遞單號|運單號|貨運單號|已(?:經)?(?:安排)?快遞|安排快遞寄件|寄出|dispatched|"
    r"courier|tracking\s*no", re.I)

# 「客人已簽收正本」的說法。
_ACK_RE = re.compile(
    r"簽收|已收件|收妥|收到(?:整份)?(?:出貨)?文件|received\s+the\s+document", re.I)

# 🚨 反向語意的保險絲：ContactW 也會寫「L060726-3**未收到文件**」「未有收到…請馬上
# 補回」——那是**催件**，跟簽收完全相反。逐行判斷（不是整封），命中反向就不採信
# 該行；把催件讀成簽收會讓系統以為正本到了、罰款默默發生。
_ACK_NEGATIVE = re.compile(
    r"未(?:有)?收到|沒(?:有)?收到|尚未(?:收到|簽收)|未收件|未簽收|"
    r"請(?:馬上)?補[回寄]|補回|not\s+received|have\s+not\s+received", re.I)

# 正本追多久：期限（ETD+10）之後再看這麼多天，之後就不再吵（案子早就該人工處理了）。
_ORIGINALS_WATCH_AFTER_D = env_int("RED_SHIPDOC_ORIGINALS_WATCH_D", 30,
                                   min_value=7, max_value=180)


# 引言/轉寄分隔線 —— 之後的內容是**上一封信**，不是這封的人講的話。
# 🚨 實測 ContactW 的回信整串引用了 UserAng 舊信裡的「(前收到文件) 即安排付款」，
# 不切掉就會把一年前別人講的話讀成「客人剛簽收」。
_QUOTE_MARKERS = (
    "寄件者:", "寄件者：", "From:", "-----Original Message-----", "原始郵件",
    "開始轉寄郵件", "Begin forwarded message", "寫道：", "wrote:", "đã viết",
)


def _own_words(text: str) -> str:
    """只留這封信自己寫的那段（切掉引言與逐行 ``>`` 引用）。"""
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if any(stripped.startswith(m) or m in stripped[:40]
               for m in _QUOTE_MARKERS):
            break
        if stripped.startswith(">"):
            continue
        lines.append(raw)
    return "\n".join(lines)


def _ack_lines(text: str) -> list[str]:
    """回內文裡「真的在講已簽收」的行（先切引言，再逐行套反向保險絲）。"""
    out: list[str] = []
    for raw in _own_words(text).splitlines():
        line = raw.strip()
        if not line or len(line) > 300:
            continue
        if _ACK_RE.search(line) and not _ACK_NEGATIVE.search(line):
            out.append(line)
    return out


# 「單號 → 哪幾櫃」的對應。實測 UserAng 會在同一封信裡寫兩組：
#   「快遞單號 SF0213866905821 for CONT8,9,10」
#   「快遞單號 SF0214996834262 for Lurchi CONT-7」
# 不解析這層對應，併櫃信裡每個櫃都會吸走所有單號 —— 對 CONT8 報出 CONT7 的
# 單號，人拿去順豐查就查到別櫃的貨。對不上就**不掛單號**，不猜。
_TRACKING_FOR = re.compile(
    r"((?:SF|CX)\d{10,22})[^\n]{0,40}?\bfor\b([^\n]{0,60})", re.I)


def _tracking_by_container(text: str) -> dict[str, set[int]]:
    """回 {單號: {櫃次…}}；只收「單號 … for … CONTx」這種明寫對應的。"""
    out: dict[str, set[int]] = {}
    for m in _TRACKING_FOR.finditer(str(text or "")):
        nums = _container_numbers(m.group(2))
        if nums:
            out.setdefault(m.group(1).upper(), set()).update(nums)
    return out


def _originals_state(rec: dict) -> dict:
    """取（必要時建立）這一櫃的正本追蹤子狀態。"""
    st = rec.get("originals")
    if not isinstance(st, dict):
        st = {"status": "pending", "tracking": [], "sent_at": None,
              "acked_at": None, "notes": []}
        rec["originals"] = st
    st.setdefault("status", "pending")
    st.setdefault("tracking", [])
    st.setdefault("notes", [])
    return st


# ────────────────────────────────────────────────────────────────────
# 期限計算
# ────────────────────────────────────────────────────────────────────
def _parse_etd(rec: dict) -> date | None:
    try:
        return date.fromisoformat(str(rec.get("etd") or ""))
    except ValueError:
        return None


def _scan_deadline(etd: date) -> date:
    return etd + timedelta(days=_SCAN_DEADLINE_D)


def _originals_deadline(etd: date) -> date:
    return etd + timedelta(days=_ORIGINALS_DEADLINE_D)


def _ask_start(etd: date) -> date:
    """哪天開始問業務。預設副本期限前 2 天（RED_SHIPDOC_ASK_LEAD_D 調），
    給業務留補救時間；期限已過的櫃（例如剛開始追的舊櫃）第一輪就會問。"""
    lead = env_int("RED_SHIPDOC_ASK_LEAD_D", 2, min_value=0, max_value=_SCAN_DEADLINE_D)
    return _scan_deadline(etd) - timedelta(days=lead)


def _fmt_due(deadline: date, today: date) -> str:
    diff = (deadline - today).days
    day = deadline.strftime("%m/%d")
    if diff > 0:
        return f"{day}（還剩 {diff} 天）"
    if diff == 0:
        return f"{day}（就是今天）"
    return f"{day}（已逾期 {-diff} 天）"


# ────────────────────────────────────────────────────────────────────
# 狀態檔
# ────────────────────────────────────────────────────────────────────
def _read_state() -> dict:
    """唯讀路徑用（status 查詢）。寫入一律走 locked_json，不用這支。"""
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("shipments", {})
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 —— 壞檔要看得見，但查詢別炸
        logger.warning("shipping_doc_tracker 狀態檔讀取失敗（%s）", exc)
    return {"version": 1, "shipments": {}}


def _find_ref(shipments: dict, ref: str) -> str | None:
    """使用者輸入的櫃號 → 狀態檔裡的正式 key。

    容忍大小寫/連字號/短形（「cont8」→「LURCHI-CONT8」）。⚠️ 只給櫃次、而剛好
    有兩個品牌都在追同一櫃次時回 None（讓呼叫端請對方指定）——結錯櫃比多問一句
    糟得多。
    """
    want = _norm(ref)
    if not want:
        return None
    for key in shipments:
        if _norm(key) == want:
            return key
    want_brand, want_num = _ref_parts(ref)
    if want_num is None:
        return None
    hits = []
    for key in sorted(shipments):
        brand, num = _ref_parts(key)
        if num != want_num:
            continue
        if want_brand and brand and want_brand != brand:
            continue
        hits.append(key)
    return hits[0] if len(hits) == 1 else None


def track_shipment(ref: str, etd: str, pairs: str = "",
                   customer: str = "Supremo (Lurchi)") -> tuple[str, bool]:
    """把一櫃加進追蹤（register 腳本與 track_shipping_docs 工具共用）。

    Returns:
        (正式櫃號 key, 是否新增)。已存在同櫃號時只補上缺的欄位（etd/pairs 以
        新值為準），**不**動 status / asked_dates 這些跑出來的狀態。
    """
    ref = str(ref or "").strip().upper()
    if not ref:
        raise ValueError("櫃號不可為空")
    date.fromisoformat(etd)  # 格式錯直接 raise，別把壞日期寫進狀態檔
    created = False
    with locked_json(_STATE_PATH, default={"version": 1, "shipments": {}}) as state:
        shipments = state.setdefault("shipments", {})
        key = _find_ref(shipments, ref) or ref
        rec = shipments.get(key)
        if rec is None:
            created = True
            rec = shipments[key] = {
                "ref": key,
                "customer": customer,
                "pairs": pairs,
                "etd": etd,
                "status": "open",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "asked_dates": [],
                "sales_confirmed_at": None,
                "sales_confirmed_note": "",
                "closed_at": None,
                "close_reason": "",
                "evidence": [],
            }
        else:
            rec["etd"] = etd
            if pairs:
                rec["pairs"] = pairs
            if customer:
                rec["customer"] = customer
    return key, created


# ────────────────────────────────────────────────────────────────────
# Gmail 查核（網域委派讀 twsales@ / shipping@ 的寄件備份）
# ────────────────────────────────────────────────────────────────────
def _sa_file(mailbox: str) -> str:
    """網域委派金鑰路徑。做法同 payment_notice：全公司同一把 SA，金鑰檔取
    rag_sync_targets.json 清單裡任一份；要冒充的信箱由本模組常數
    ``DOC_SENDER_MAILBOXES`` 指定——那是本地程式碼、不是參數，LLM 指不到
    別人的信箱。"""
    override = os.environ.get("RED_SHIPDOC_SA_FILE", "").strip()
    if override:
        return override
    from agent_core.ingest.sync_config import load_targets
    accounts = load_targets().get("gmail_accounts") or []
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
            "是空的，也沒設 RED_SHIPDOC_SA_FILE）")
    return fallback


def _gmail_users(mailbox: str):
    from agent_core.google_auth import get_service_for_account

    key = (mailbox or "").strip().lower()
    service = get_service_for_account(
        f"shipping_doc:{key}", "gmail", "v1",
        service_account_file=_sa_file(key), subject=key, scopes=None,
    )
    return service.users()


# 客人明文要求「工廠必須提供**電子版的裝箱單 (EXCEL)**」。實測附件長這樣：
#   PKL LURCHI 2454PRS-CONT7.XLS   （PKL = packing list）
#   LURCHI-EUR1-2454prs- CONT7.xls （產證，也是 xls 但不是裝箱單）
# 只查 has:attachment 的話，附一份 PDF 也算過。這裡多跑一條窄查詢標記哪些信
# 真的帶 Excel；**只警示不擋結案** —— 裝箱單也可能夾在另一封信裡，把它變成
# 結案硬條件會製造假的「沒寄」。
_EXCEL_QUERY = "(filename:xls OR filename:xlsx)"


def _excel_message_ids(users, window_start: date) -> set[str]:
    """該視窗內「寄給 ContactW 且帶 Excel 附件」的 message id 集合。"""
    recips = " OR ".join(f"to:{a} OR cc:{a}" for a in DOC_RECIPIENTS)
    query = (f"in:sent has:attachment {_EXCEL_QUERY} "
             f"after:{window_start.strftime('%Y/%m/%d')} ({recips})")
    try:
        ids, _ = _list_sent_ids(users, query)
    except Exception as exc:  # noqa: BLE001 —— 這條只是加註，掛了不該擋主流程
        logger.debug("shipping_doc Excel 查詢失敗：%s", exc)
        return set()
    return set(ids)


def _gmail_query(window_start: date) -> str:
    """寄件備份裡「帶附件、寄給任一位 ContactW」的粗篩。真判準在 _collect_evidence
    逐封看 To/Cc/Subject。"""
    recips = " OR ".join(
        f"to:{a} OR cc:{a}" for a in DOC_RECIPIENTS)
    return (f"in:sent has:attachment after:{window_start.strftime('%Y/%m/%d')} "
            f"({recips})")


def _headers_of(msg: dict) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in (msg.get("payload", {}) or {}).get("headers", [])}


def _list_sent_ids(users, query: str) -> tuple[list[str], bool]:
    """把符合 query 的寄件 id **翻頁翻到底**，回 (ids, 是否還有沒列到的)。

    Gmail 的 ``messages().list`` 一頁最多 100 筆且**不會**自己續頁：只傳
    maxResults 就是靜默截斷，而且它的排序是新到舊 —— 截掉的是最舊的，正好是
    舊櫃的證據。第二個回傳值就是給呼叫端出聲用的，不要吞掉。
    """
    ids: list[str] = []
    token: str | None = None
    while len(ids) < _MAX_PER_MAILBOX:
        resp = users.messages().list(
            userId="me", q=query, pageToken=token,
            maxResults=min(_LIST_PAGE_SIZE, _MAX_PER_MAILBOX - len(ids)),
        ).execute()
        ids.extend(m["id"] for m in (resp.get("messages") or []) if m.get("id"))
        token = resp.get("nextPageToken")
        if not token:
            return ids, False
    return ids, True


def _collect_evidence(window_start: date) -> tuple[list[dict], list[str]]:
    """掃兩個寄件信箱，回 (證據信清單, 錯誤清單)。

    每筆證據：{mailbox, subject, date(date), when(str), covered(set[str])}。
    covered = To＋Cc 裡命中的 DOC_RECIPIENTS。這裡**不**綁櫃號——同一輪的
    多個櫃共用一次掃描，櫃號比對在 _verify_shipment。
    """
    evidence: list[dict] = []
    errors: list[str] = []
    for mailbox in DOC_SENDER_MAILBOXES:
        try:
            users = _gmail_users(mailbox)
        except Exception as exc:  # noqa: BLE001 —— 設定/授權問題要看得見
            errors.append(f"{mailbox}：{exc}")
            continue
        try:
            ids, capped = _list_sent_ids(users, _gmail_query(window_start))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{mailbox}：Gmail 查詢失敗（{type(exc).__name__}: {exc}）")
            continue
        if capped:
            # 硬上限到頂＝這輪的證據**不完整**，可能把已寄的櫃判成沒寄。
            # 絕不靜默：講清楚讀了幾封、還有沒讀完的，人才知道該不該信這輪。
            errors.append(
                f"{mailbox}：符合的寄件超過單輪上限（已讀 {len(ids)} 封，"
                f"仍有未讀完的）——本輪查核可能不完整，"
                f"必要時調高 RED_SHIPDOC_MAX_PER_MAILBOX")
        excel_ids = _excel_message_ids(users, window_start)
        deadline = time.monotonic() + _MAILBOX_DEADLINE_S
        for idx, mid in enumerate(ids):
            if time.monotonic() > deadline:
                errors.append(
                    f"{mailbox}：逾時，本輪只讀完 {idx}/{len(ids)} 封寄件"
                    "（下輪補上）——本輪查核可能不完整")
                break
            try:
                msg = users.messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["To", "Cc", "Subject", "Date"],
                ).execute()
            except Exception as exc:  # noqa: BLE001 —— 單封讀不到下輪還在
                logger.debug("shipping_doc 讀取 %s 失敗，本輪跳過：%s", mid, exc)
                continue
            head = _headers_of(msg)
            addrs = {a.lower() for a in _ADDR_RE.findall(
                f"{head.get('to', '')} {head.get('cc', '')}")}
            covered = {r for r in DOC_RECIPIENTS if r in addrs}
            if not covered:
                continue
            try:
                sent_on = datetime.fromtimestamp(
                    int(msg.get("internalDate")) / 1000).date()
            except (TypeError, ValueError, OSError):
                sent_on = window_start
            evidence.append({
                "mailbox": mailbox,
                "subject": head.get("subject", ""),
                "date": sent_on,
                "when": sent_on.strftime("%m/%d"),
                "covered": covered,
                "has_excel": mid in excel_ids,
            })
    return evidence, errors


def _collect_originals(window_start: date) -> tuple[list[dict], list[str]]:
    """掃「正本已寄出」與「客人已簽收」兩種訊號，回 (signals, errors)。

    每筆：{kind: "sent"|"ack", subject, date, tracking:set[str], quote:str}。
    ``kind`` 判斷靠**寄件人方向**不是措辭：我方寄件裡的單號＝已寄出；客人來信
    裡的簽收語＝已收到。措辭在兩邊都會出現（我方也會說「已於8/21簽收」轉述），
    方向才是硬的。
    """
    from agent_core.gmail_ops import extract_body

    since = window_start.strftime("%Y/%m/%d")
    recips = " OR ".join(f"to:{a} OR cc:{a}" for a in DOC_RECIPIENTS)
    senders = " OR ".join(f"from:{a}" for a in DOC_RECIPIENTS)
    queries = [
        # 我方寄給 ContactW、帶快遞字樣的信（正本寄出通知；多半沒有附件，
        # 所以**不能**沿用證據掃描那條 has:attachment 的查詢）。
        ("sent", f"in:sent after:{since} ({recips}) "
                 "(運單號 OR 快遞單號 OR 貨運單號 OR 快遞 OR dispatched OR tracking)"),
        # 客人回覆「已簽收／已收件」。
        ("ack", f"after:{since} ({senders}) (簽收 OR 收件 OR 收到)"),
    ]

    mailbox = DOC_SENDER_MAILBOXES[0]   # twsales@ —— UserAng 在這兩條線上都在
    signals: list[dict] = []
    errors: list[str] = []
    try:
        users = _gmail_users(mailbox)
    except Exception as exc:  # noqa: BLE001
        return [], [f"正本追蹤（{mailbox}）：{exc}"]

    deadline = time.monotonic() + _MAILBOX_DEADLINE_S
    for kind, query in queries:
        try:
            ids, capped = _list_sent_ids(users, query)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"正本追蹤查詢失敗（{kind}：{type(exc).__name__}: {exc}）")
            continue
        if capped:
            errors.append(f"正本追蹤（{kind}）：超過單輪上限（已讀 {len(ids)} 封，"
                          "仍有未讀完的）——本輪正本狀態可能不完整")
        for idx, mid in enumerate(ids):
            if time.monotonic() > deadline:
                errors.append(f"正本追蹤（{kind}）：逾時，只讀完 {idx}/{len(ids)} 封")
                break
            try:
                msg = users.messages().get(
                    userId="me", id=mid, format="full").execute()
            except Exception as exc:  # noqa: BLE001
                logger.debug("shipping_doc 正本訊號 %s 讀取失敗：%s", mid, exc)
                continue
            head = _headers_of(msg)
            subject = head.get("subject", "")
            body = extract_body(msg.get("payload") or {})
            try:
                when = datetime.fromtimestamp(
                    int(msg.get("internalDate")) / 1000).date()
            except (TypeError, ValueError, OSError):
                when = window_start
            if kind == "sent":
                own = _own_words(f"{subject}\n{body}")
                tracking = {m.group(1).upper() for m in _TRACKING_RE.finditer(own)}
                if not tracking and not _DISPATCH_RE.search(own[:2000]):
                    continue
                signals.append({
                    "kind": "sent", "subject": subject, "date": when,
                    "tracking": tracking,
                    # 單號→櫃次的明寫對應（有寫才收；沒寫就別替它分配）。
                    "tracking_for": _tracking_by_container(own),
                    "quote": "",
                })
                # 我方自己回報「已於8/21簽收」也是有價值的事實 —— 但那是**我方
                # 說的**，不是客人確認的，分成另一種訊號、訊息裡也照實標明來源。
                mine = _ack_lines(f"{subject}\n{body}")
                if mine:
                    signals.append({"kind": "ack_self", "subject": subject,
                                    "date": when, "tracking": set(),
                                    "quote": mine[0][:160]})
            else:
                lines = _ack_lines(f"{subject}\n{body}")
                if not lines:
                    continue
                signals.append({"kind": "ack", "subject": subject, "date": when,
                                "tracking": set(), "quote": lines[0][:160]})
    return signals, errors


def _apply_originals(rec: dict, signals: list[dict], today: date,
                     *, scan_ok: bool = True) -> dict:
    """把正本訊號套進一櫃的子狀態，回該子狀態（純函式＋就地更新，測試直接打）。

    只認**這一櫃視窗內、主旨命中櫃號**的訊號 —— 正本這條沒有「候選」那種寬鬆
    路徑：把別櫃的簽收算成自己的，等於謊報正本已到，而那正是罰款的來源。

    🚨 ``scan_ok=False``（信箱根本沒掃成功）時**絕不**把狀態降級成 overdue：
    「掃過但沒有簽收」與「沒掃到」是兩件事，混在一起就會把一次網路故障報成
    「正本逾期」。實測 2026-08-27 09:00 機器 DNS 掛掉（oauth2.googleapis.com
    解析失敗）→ 正本掃描零訊號 → 四櫃全被標成 overdue → 10:02 的升級線對大王
    發出四櫃假警報，而那四櫃的正本其實早就簽收了。
    正向訊號照收（有拿到就是證據）；只有「判定逾期」這一步需要掃描成功。
    """
    st = _originals_state(rec)
    etd = _parse_etd(rec)
    ref = str(rec.get("ref") or "")
    for sig in signals:
        if etd is not None and not (
                etd - timedelta(days=_WINDOW_BEFORE_ETD_D)
                <= sig["date"]
                <= etd + timedelta(days=_ORIGINALS_DEADLINE_D
                                   + _ORIGINALS_WATCH_AFTER_D)):
            continue
        _, num = _ref_parts(ref)
        mapping = sig.get("tracking_for") or {}
        # 這筆訊號算不算這一櫃的？三種都算（**唯一**的相關性判斷，別再往下另設閘門
        # ——之前把「同一行點名」寫在下面的分支，結果被這裡先擋掉、整條規則失效）：
        #   ① 主旨點名這一櫃
        #   ② 內文明寫「單號 … for CONT7」的對應（實測 CONT7 的單號就藏在
        #      主旨只有 CONT 9+10/CNT-8 的那封信內文裡）
        #   ③ 簽收語**自己那一行**點名（實測「快遞單號 SF… for Lurchi CONT-7
        #      (US$44,150.20) 已於8/21簽收」）
        # 刻意**不**收「內文任一處提到櫃號」：併櫃信的一句「簽收」會灑給所有
        # 被提到的櫃，那是謊報正本已到，而正本正是罰款那條。
        named_in_body = any(num in nums for nums in mapping.values())
        named_in_quote = num in _container_numbers(str(sig.get("quote") or ""))
        if not (_subject_matches(ref, sig["subject"]) or named_in_body
                or named_in_quote):
            continue
        if sig["kind"] == "sent":
            if mapping:
                # 信裡明寫了「單號 for 哪幾櫃」→ 只收屬於這一櫃的。
                picked = {tk for tk, nums in mapping.items() if num in nums}
            elif len(_container_numbers(sig["subject"])) <= 1:
                # 整封只講一櫃 → 單號歸它。
                picked = set(sig["tracking"])
            else:
                # 併櫃信又沒寫對應 → 不掛單號（掛錯會讓人去順豐查到別櫃的貨）。
                picked = set()
            for tk in sorted(picked):
                if tk not in st["tracking"]:
                    st["tracking"].append(tk)
            iso = sig["date"].isoformat()
            if not st.get("sent_at") or iso < str(st["sent_at"]):
                st["sent_at"] = iso
        elif sig["kind"] == "ack_self":
            iso = sig["date"].isoformat()
            if not st.get("self_acked_at") or iso < str(st["self_acked_at"]):
                st["self_acked_at"] = iso
                st["self_notes"] = [sanitize_for_llm(sig["quote"])]
        else:
            iso = sig["date"].isoformat()
            if not st.get("acked_at") or iso < str(st["acked_at"]):
                st["acked_at"] = iso
                st["notes"] = [sanitize_for_llm(sig["quote"])]
    if scan_ok:
        st["last_ok_at"] = today.isoformat()
    else:
        st["last_error_at"] = today.isoformat()
    if st.get("acked_at"):
        st["status"] = "acked"
    elif st.get("self_acked_at"):
        # 我方自述已簽收：不是客人確認，但足以擋掉「🔴逾期未見簽收」的誤報
        # （實測 CONT7 就是這型：UserAng 寫「已於8/21簽收」，客人沒再回一封）。
        st["status"] = "acked_by_us"
    elif etd is not None and today > _originals_deadline(etd):
        # 只有真的掃成功、確認信箱裡沒有簽收，才敢說逾期。
        st["status"] = "overdue" if scan_ok else "unverified"
    elif st.get("sent_at"):
        st["status"] = "sent"
    else:
        st["status"] = "pending"
    return st


def _originals_ask_start(etd: date) -> date:
    """哪天開始講正本。正本期限（ETD+10）前 lead 天，沿用副本那組提前量。

    ⚠️ 不可以從建案當天就開始講：通知信常在 ETD 前三週就到，那時正本本來就
    還沒寄，天天報「尚未見寄出紀錄」是純噪音，收件人會學會忽略整條通知。
    """
    lead = env_int("RED_SHIPDOC_ASK_LEAD_D", 2, min_value=0, max_value=_SCAN_DEADLINE_D)
    return _originals_deadline(etd) - timedelta(days=lead)


def _originals_watch_over(rec: dict, today: date) -> bool:
    """這一櫃的正本還要不要繼續看？（還沒到開口時機、已簽收、或過期太久都收手）"""
    st = _originals_state(rec)
    if st.get("status") in ("acked", "acked_by_us"):
        return True
    etd = _parse_etd(rec)
    if etd is None:
        return False
    if today < _originals_ask_start(etd):
        return True          # 時候未到，本輪連掃都不用掃
    return today > _originals_deadline(etd) + timedelta(
        days=_ORIGINALS_WATCH_AFTER_D)


def _render_originals(rec: dict, today: date) -> str:
    """正本狀態的一段文字（只在還需要盯的時候才會被叫）。"""
    st = _originals_state(rec)
    etd = _parse_etd(rec)
    due = _fmt_due(_originals_deadline(etd), today) if etd else "（無 ETD）"
    head = f"📮 正本文件：{_shipment_head(rec)}"
    tracking = ("　快遞單號：" + "、".join(st["tracking"])) if st["tracking"] else ""
    if st["status"] == "overdue":
        lines = [f"{head}　🔴 已過期限未見簽收", f"　正本期限：{due}"]
        if tracking:
            lines.append(tracking)
        if st.get("sent_at"):
            lines.append(f"　我方寄出：{st['sent_at']}（客人尚未回覆簽收）")
        else:
            lines.append("　查不到我方寄出正本的紀錄（信箱裡沒有快遞單號）")
        lines.append("　⚠️ 延誤正本文件會產生罰款與額外操作費，由工廠負責——"
                     "請確認實際狀況並回覆。")
        return "\n".join(lines)
    if st["status"] == "unverified":
        return "\n".join([
            f"{head}　⚠️ 本輪信箱沒掃成功，正本狀態**未經查核**",
            f"　正本期限：{due}",
            f"　最後一次成功查核：{st.get('last_ok_at') or '（從未成功）'}",
            "　（不代表逾期，也不代表已簽收——下輪掃得動就會更新。）",
        ])
    if st["status"] == "acked_by_us":
        lines = [f"{head}　🟡 我方回報已簽收（**客人尚未回信確認**）",
                 f"　正本期限：{due}"]
        if tracking:
            lines.append(tracking)
        if st.get("self_notes"):
            lines.append(f"　我方說法：{st['self_notes'][0]}")
        lines.append("　（來源是我方自己的信、不是客人的確認信 —— 需要客人書面"
                     "確認才算數的話請自行跟催。）")
        return "\n".join(lines)
    if st["status"] == "sent":
        lines = [f"{head}　🟡 已寄出、等客人簽收", f"　正本期限：{due}"]
        if tracking:
            lines.append(tracking)
        lines.append(f"　我方寄出：{st.get('sent_at')}")
        return "\n".join(lines)
    return "\n".join([f"{head}　⚪ 尚未見寄出紀錄", f"　正本期限：{due}",
                       "　（查核只看 email：信裡出現快遞單號才算寄出，"
                       "客人回覆簽收才算到達）"])


def _verify_shipment(rec: dict, evidence: list[dict]) -> dict:
    """一櫃的查核結果（純函式，測試直接打這支）。

    Returns:
        {"strict": set, "any": set, "hits": [證據信]}——strict = 主旨含櫃號的
        寄件涵蓋到的收件人；any = 加計視窗內主旨沒帶櫃號的候選寄件。
    """
    ref = str(rec.get("ref") or "")
    etd = _parse_etd(rec)
    strict: set[str] = set()
    anyc: set[str] = set()
    hits: list[dict] = []
    for ev in evidence:
        if etd is not None and ev["date"] < etd - timedelta(days=_WINDOW_BEFORE_ETD_D):
            continue
        if _subject_matches(ref, ev["subject"]):
            strict |= ev["covered"]
            anyc |= ev["covered"]
            hits.append({**ev, "ref_match": True})
        elif etd is None or (
                etd - timedelta(days=_CANDIDATE_WINDOW_BEFORE_ETD_D)
                <= ev["date"]
                <= etd + timedelta(days=_CANDIDATE_WINDOW_AFTER_ETD_D)):
            anyc |= ev["covered"]
            hits.append({**ev, "ref_match": False})
    excel = any(h.get("has_excel") for h in hits if h.get("ref_match"))
    return {"strict": strict, "any": anyc, "hits": hits, "excel": excel}


def _evidence_lines(hits: list[dict]) -> list[str]:
    lines = []
    for ev in hits[:_EVIDENCE_CAP]:
        who = "、".join(sorted(a.split("@")[0] for a in ev["covered"]))
        note = "" if ev.get("ref_match") else "（主旨未含櫃號）"
        lines.append(
            f"　- {ev['when']} {ev['mailbox'].split('@')[0]}@ 寄「"
            f"{sanitize_for_llm(ev['subject'])[:70]}」→ {who}{note}")
    return lines


def _store_evidence(rec: dict, hits: list[dict]) -> None:
    """存查核證據。⚠️ **主旨命中櫃號的排前面**——這是結案的真正依據。

    照掃描順序硬切前 N 筆會被同期別櫃的信塞滿（實測 CONT5 結案後存下來的 10 筆
    全是 CONT7/8/9/10 的信），事後 shipping_doc_status 查起來像是拿別櫃的信結案，
    稽核軌跡自打嘴巴。
    """
    hits = sorted(hits, key=lambda h: not h.get("ref_match"))
    rec["evidence"] = [{
        "mailbox": ev["mailbox"], "subject": str(ev["subject"])[:120],
        "date": ev["date"].isoformat(), "ref_match": bool(ev.get("ref_match")),
        "covered": sorted(ev["covered"]),
    } for ev in hits[:_EVIDENCE_CAP]]


def _close(rec: dict, reason: str) -> None:
    rec["status"] = "closed"
    rec["closed_at"] = datetime.now().isoformat(timespec="seconds")
    rec["close_reason"] = reason


# ────────────────────────────────────────────────────────────────────
# 訊息排版
# ────────────────────────────────────────────────────────────────────
def _shipment_head(rec: dict) -> str:
    pairs = f"　{rec['pairs']}" if rec.get("pairs") else ""
    return f"{rec.get('ref', '?')}{pairs}　ETD {rec.get('etd', '?')}"


def _render_closed(rec: dict, hits: list[dict]) -> str:
    lines = [f"✅ 出貨文件結案：{_shipment_head(rec)}",
             f"　{rec.get('close_reason', '')}"]
    lines.extend(_evidence_lines([h for h in hits if h.get("ref_match")] or hits))
    if not any(h.get("has_excel") for h in hits if h.get("ref_match")):
        # 客人明文要求電子版裝箱單 (EXCEL)。查不到不代表沒寄（可能夾在別封信），
        # 所以只提醒、不擋結案 —— 但要講出來，不然這條要求等於沒人在看。
        lines.append("　⚠️ 這幾封信裡沒看到 Excel 附件；客人要求「工廠必須提供"
                     "電子版的裝箱單 (EXCEL)」，請確認是否已另行提供。")
    etd = _parse_etd(rec)
    if etd is not None:
        lines.append(
            f"　（提醒：正本文件須於 {_originals_deadline(etd).strftime('%m/%d')} 前"
            "寄達 Supremo——實體快遞這段 email 查核不到，請自行留意。）")
    return "\n".join(lines)


def _render_ask(rec: dict, verdict: dict, today: date) -> str:
    etd = _parse_etd(rec)
    missing = [r for r in DOC_RECIPIENTS if r not in verdict["strict"]]
    lines = [f"📄 出貨文件確認：{_shipment_head(rec)}（{rec.get('customer', '')}）"]
    if etd is not None:
        lines.append(f"　副本文件（掃瞄）電郵期限：{_fmt_due(_scan_deadline(etd), today)}")
        lines.append(f"　正本文件寄達期限：{_fmt_due(_originals_deadline(etd), today)}")
    if verdict["hits"]:
        lines.append("　目前查到的寄件：")
        lines.extend(_evidence_lines(verdict["hits"]))
    if rec.get("sales_confirmed_at"):
        # 業務說處理好了但信箱對不上——照實講差在哪，不結案也不指控。
        lines.append(
            f"　⚠️ 業務已回覆處理（{str(rec['sales_confirmed_at'])[:10]}），但寄件"
            f"備份查核未過：{'、'.join(missing)} 還查不到含此櫃號的文件信，"
            "請確認是否漏寄或由其他信箱寄出。")
    else:
        lines.append(
            f"　查核狀態：{'、'.join(missing)} 尚未查到含此櫃號＋附件的文件信。")
        lines.append(
            "　@UserAng 請確認是否已依客人要求提供文件；處理好後直接在此回覆"
            f"（例：「{rec.get('ref', '')} 文件已寄出」），小紅查核 email 無誤後結案。")
    if verdict["hits"] and not verdict.get("excel"):
        lines.append("　⚠️ 目前查到的信裡沒看到 Excel 附件（客人要求電子版裝箱單"
                     " EXCEL）——若已另行提供請忽略此行。")
    lines.append(f"　{_REQUIREMENT_SUMMARY}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 對外工具
# ────────────────────────────────────────────────────────────────────
def track_shipping_docs(ref: str, etd: str, pairs: str = "",
                        customer: str = "Supremo (Lurchi)") -> str:
    """把一櫃出貨加進「Supremo 出貨文件」追蹤，到期會在 Telegram 問業務並查核結案。

    Args:
        ref: 櫃號／出貨代號，例如 "LURCHI-CONT9"（比對容忍大小寫與連字號）。
        etd: 船開日，ISO 格式 "YYYY-MM-DD"。副本文件期限＝ETD+5 天、正本＝ETD+10 天。
        pairs: 雙數描述，例如 "2454 PRS"（只進提醒文案，可留空）。
        customer: 客戶名（預設 Supremo (Lurchi)）。
    Returns:
        新增或更新結果的一行說明。
    """
    try:
        key, created = track_shipment(ref, etd, pairs=pairs, customer=customer)
    except ValueError as exc:
        return f"❌ 加入追蹤失敗：{exc}（etd 要像 2026-08-15 這種 ISO 日期）"
    verb = "已加入追蹤" if created else "已更新（保留原查核狀態）"
    d = date.fromisoformat(etd)
    return (f"📦 {key} {verb}：ETD {etd}，副本文件期限 "
            f"{_scan_deadline(d).isoformat()}、正本期限 {_originals_deadline(d).isoformat()}。"
            "到期未結案會在 09:00 推播橙色（業務）確認。")


def shipping_doc_status(ref: str = "") -> str:
    """看「Supremo 出貨文件」追蹤現況：每櫃的期限、業務回覆與 email 查核證據。唯讀、不打 Gmail。

    Args:
        ref: 指定櫃號看單櫃明細（例 "LURCHI-CONT7"）；留空列全部。
    Returns:
        排版好的純文字狀態；沒有追蹤中的櫃回「（目前沒有追蹤中的出貨文件案件）」。
    """
    shipments = _read_state().get("shipments", {})
    if not shipments:
        return "（目前沒有追蹤中的出貨文件案件）"
    keys = [k for k in shipments]
    if ref:
        found = _find_ref(shipments, ref)
        if not found:
            return (f"查無櫃號「{sanitize_for_llm(str(ref))[:40]}」。追蹤中："
                    + "、".join(sorted(keys)))
        keys = [found]
    today = date.today()
    parts: list[str] = []
    for key in sorted(keys):
        rec = shipments[key]
        etd = _parse_etd(rec)
        if rec.get("status") == "closed":
            parts.append(f"✅ {_shipment_head(rec)}｜已結案"
                         f"（{str(rec.get('closed_at') or '')[:10]}）："
                         f"{rec.get('close_reason', '')}")
        else:
            lines = [f"⏳ {_shipment_head(rec)}｜追蹤中"]
            if etd is not None:
                lines.append(f"　副本期限 {_fmt_due(_scan_deadline(etd), today)}／"
                             f"正本期限 {_fmt_due(_originals_deadline(etd), today)}")
            if rec.get("sales_confirmed_at"):
                lines.append(f"　業務已回覆處理（{str(rec['sales_confirmed_at'])[:10]}）"
                             "，等 email 查核通過後結案")
            parts.append("\n".join(lines))
        for ev in (rec.get("evidence") or [])[:3]:
            who = "、".join(a.split("@")[0] for a in ev.get("covered", []))
            note = "" if ev.get("ref_match") else "（主旨未含櫃號）"
            parts.append(f"　- {ev.get('date', '')} {str(ev.get('mailbox', '')).split('@')[0]}@"
                         f" 寄「{sanitize_for_llm(str(ev.get('subject', '')))[:60]}」→ {who}{note}")
    parts.append(f"（查核口徑：{_REQUIREMENT_SUMMARY}）")
    return "\n".join(parts)


def confirm_shipping_docs(ref: str = "", note: str = "") -> str:
    """業務回覆「出貨文件已處理」時呼叫：記錄回覆並立刻查核 email，查核通過才結案。

    給橙色（業務）freeform 用：UserAng 在 Telegram 回「文件已寄出／處理好了」
    就帶櫃號呼叫這顆。查核 = twsales@/shipping@ 寄件備份裡有沒有「帶附件、寄給
    兩位 ContactW」的文件信（見模組 docstring 的結案口徑）。**查核結果照表念**：
    通過就結案、查無就照實說查無，不可自行宣布結案。

    Args:
        ref: 櫃號，例 "LURCHI-CONT8"。留空且只有一櫃追蹤中時自動指那一櫃。
        note: 業務回覆的原話重點（例 "已寄出，運單號 SF0214996834262"），存檔備查。
    Returns:
        記錄＋查核結果（結案／查無寄件請確認／櫃號不明請指定）。
    """
    state = _read_state()
    shipments = state.get("shipments", {})
    open_keys = [k for k, r in shipments.items() if r.get("status") != "closed"]
    key = _find_ref(shipments, ref) if ref else None
    if key is None:
        if ref:
            return (f"查無櫃號「{sanitize_for_llm(str(ref))[:40]}」。追蹤中："
                    + ("、".join(sorted(shipments)) or "（無）"))
        if len(open_keys) == 1:
            key = open_keys[0]
        else:
            return ("請指定櫃號（追蹤中："
                    + ("、".join(sorted(open_keys)) or "（無）") + "）")
    if shipments[key].get("status") == "closed":
        return f"{key} 已經結案：{shipments[key].get('close_reason', '')}"

    rec = dict(shipments[key])
    etd = _parse_etd(rec)
    window_start = (etd or date.today()) - timedelta(days=_WINDOW_BEFORE_ETD_D)
    evidence, errors = _collect_evidence(window_start)
    verdict = _verify_shipment(rec, evidence)

    now_iso = datetime.now().isoformat(timespec="seconds")
    closed_reason = ""
    if set(DOC_RECIPIENTS) <= verdict["strict"]:
        closed_reason = "email 查核通過：兩位 ContactW 都已收到含此櫃號＋附件的文件信"
    elif set(DOC_RECIPIENTS) <= verdict["any"]:
        closed_reason = ("業務確認＋email 查有寄件（兩位 ContactW 都有收到帶附件的"
                         "寄件，惟主旨未含櫃號）")

    with locked_json(_STATE_PATH, default={"version": 1, "shipments": {}}) as st:
        live = st.setdefault("shipments", {}).setdefault(key, rec)
        live["sales_confirmed_at"] = now_iso
        if note:
            live["sales_confirmed_note"] = sanitize_for_llm(str(note))[:300]
        _store_evidence(live, verdict["hits"])
        if closed_reason and live.get("status") != "closed":
            _close(live, closed_reason)
        rec = dict(live)

    if closed_reason:
        return _render_closed(rec, verdict["hits"])
    missing = [r for r in DOC_RECIPIENTS if r not in verdict["any"]]
    lines = [f"已記錄你的回覆（{key}）。不過 email 查核還沒過：",
             f"　{'、'.join(missing)} 查不到帶附件的文件信"
             f"（查核範圍：twsales@ 與 shipping@ 的寄件備份）。"]
    if verdict["hits"]:
        lines.append("　目前查到的寄件：")
        lines.extend(_evidence_lines(verdict["hits"]))
    if errors:
        lines.append("　⚠️ 查核時信箱有問題：" + "；".join(errors))
    lines.append("　請確認是否漏寄、收件人少了一位、或由其他信箱寄出；"
                 "明早排程會自動再查一次，查核通過就結案。")
    return "\n".join(lines)


def shipping_doc_escalation() -> str:
    """排程用（deterministic_tool）：只在**逾期夠久**時出聲，推給大王（紅色）。

    橙色那條每天問 UserAng；但她沒回、或正本一直沒簽收時，沒有人會知道 ——
    而延誤的罰款是我方負擔。這支就是那條升級線：門檻天數由
    ``RED_SHIPDOC_ESCALATE_D``（預設 3）控制，沒有任何一櫃越線就回「(無新發現)」
    整天安靜。

    ⚠️ 唯讀：不寄信、不改狀態、不推播（推播是排程 task 的事）。也**不掃信箱**
    ——只讀 shipping_doc_check 每天已經寫好的狀態，所以不會多花 Gmail 額度、
    也不會跟主任務互相打架。
    Returns:
        逾期清單；沒有越線的回「(無新發現)」。
    """
    return _run_escalation(date.today())


def _run_escalation(today: date) -> str:
    grace = env_int("RED_SHIPDOC_ESCALATE_D", 3, min_value=0, max_value=60)
    shipments = _read_state().get("shipments", {})
    copies_late: list[str] = []
    originals_late: list[str] = []
    unverified: list[str] = []
    for key in sorted(shipments):
        rec = shipments[key]
        etd = _parse_etd(rec)
        if etd is None:
            continue
        # ① 副本電郵：期限過了 grace 天還沒結案。
        if rec.get("status") != "closed":
            overdue_d = (today - _scan_deadline(etd)).days
            if overdue_d > grace:
                confirmed = ("；業務已回覆處理但信箱查核未過"
                             if rec.get("sales_confirmed_at") else
                             "；業務尚未在 Telegram 回覆")
                copies_late.append(
                    f"　- {_shipment_head(rec)}：副本文件電郵逾期 {overdue_d} 天"
                    f"（期限 {_scan_deadline(etd).isoformat()}）{confirmed}")
        # ② 正本：期限過了 grace 天仍未見簽收（我方自述也算數，但要標明）。
        st = _originals_state(rec)
        if st.get("status") not in ("acked", "acked_by_us"):
            overdue_d = (today - _originals_deadline(etd)).days
            if st.get("status") == "unverified" or not st.get("last_ok_at"):
                # 🚨 從未成功查核過就不能說「逾期」——那是拿一次網路故障當事實
                # 去吵大王（2026-08-27 09:00 DNS 掛掉就這樣誤報過四櫃）。
                if overdue_d > grace:
                    unverified.append(
                        f"　- {_shipment_head(rec)}：正本狀態查核不到"
                        f"（期限 {_originals_deadline(etd).isoformat()} 已過 "
                        f"{overdue_d} 天，但信箱掃描未成功，無法判定）")
                continue
            if overdue_d > grace:
                sent = (f"；我方 {st['sent_at']} 寄出"
                        + ("（單號 " + "、".join(st["tracking"]) + "）"
                           if st.get("tracking") else "")
                        if st.get("sent_at") else "；查不到我方寄出紀錄")
                originals_late.append(
                    f"　- {_shipment_head(rec)}：正本逾期 {overdue_d} 天"
                    f"（期限 {_originals_deadline(etd).isoformat()}）{sent}")
    if not copies_late and not originals_late and not unverified:
        return "(無新發現)"
    parts = [f"🚨 Supremo 出貨文件逾期升級（{today.strftime('%m/%d')}）"
             f"—— 橙色已連續提醒超過 {grace} 天仍未結"]
    if copies_late:
        parts.append("──── 副本文件電郵逾期 ────")
        parts.extend(copies_late)
    if originals_late:
        parts.append("──── 正本文件未見簽收 ────")
        parts.extend(originals_late)
    if unverified:
        parts.append("──── 查核不到（不等於逾期） ────")
        parts.extend(unverified)
    parts.append("（客人條款：延誤出運／延誤文件／文件出錯的罰款與額外操作費"
                 "由工廠負責。狀態取自每日 09:00 那輪的查核結果。）")
    return "\n".join(parts)


def shipping_doc_check() -> str:
    """排程用（deterministic_tool，不經 LLM）：掃通知信建新櫃、到期的問業務、查核過的結案。

    每天 09:00 由 dispatcher 呼叫、結果推橙色（業務）bot。三段依序：①掃 ContactW
    的「正本文件情況」通知信自動建案（解析不出 ETD 的照實回報請人補）②到期未
    結案的櫃問 UserAng ③查核通過的櫃結案。三段都沒事回「(無新發現)」整輪安靜。
    Returns:
        排版好的提醒／結案訊息；無事回「(無新發現)」。
    """
    return _run_check(date.today())


def _run_check(today: date) -> str:
    # ① 先掃 ContactW 的通知信自動建案——新櫃要先進得了追蹤器，下面的到期檢查才
    #    看得到它。掃描失敗不擋既有案件的檢查（錯誤附在最後報出來）。
    created, manual, notice_errors = _discover_new_shipments(
        env_int("RED_SHIPDOC_NOTICE_DAYS", 30, min_value=1, max_value=180))

    state = _read_state()
    shipments = state.get("shipments", {})
    due: list[str] = []
    # 正本（實體快遞）要盯到「客人簽收」為止 —— 副本電郵結案 ≠ 正本到了，而
    # 罰款條款主要就在正本這條。所以這份名單**包含已結案的櫃**，只在簽收或
    # 過期太久之後才收手。
    originals_watch: list[str] = []
    for key, rec in shipments.items():
        etd = _parse_etd(rec)
        if not _originals_watch_over(rec, today):
            originals_watch.append(key)
        if rec.get("status") == "closed":
            continue
        if etd is None:
            due.append(key)  # ETD 壞掉的紀錄要浮出來讓人修，不能靜默跳過
            continue
        if today >= _ask_start(etd) or rec.get("sales_confirmed_at"):
            due.append(key)
    head_segments: list[str] = []
    if created:
        head_segments.append("📦 偵測到新櫃、已自動加入追蹤：\n" + "\n".join(created))
    if manual:
        head_segments.append(
            "⚠️ 這幾封通知信自動建案失敗，請人工用 track_shipping_docs 補：\n"
            + "\n".join(manual))

    if not due and not originals_watch:
        if head_segments:
            return "\n\n".join(head_segments)
        if notice_errors:
            return ("⚠️ 出貨文件追蹤：本輪沒有到期案件，但通知信掃描有問題：\n"
                    + "\n".join(f"  - {e}" for e in notice_errors))
        return "(無新發現)"

    window_start = min(
        (d - timedelta(days=_WINDOW_BEFORE_ETD_D)
         for d in (_parse_etd(shipments[k])
                   for k in set(due) | set(originals_watch)) if d is not None),
        default=today - timedelta(days=_WINDOW_BEFORE_ETD_D))
    evidence, errors = _collect_evidence(window_start)
    originals_signals: list[dict] = []
    originals_ok = True
    if originals_watch:
        originals_signals, originals_errors = _collect_originals(window_start)
        # 有任何錯誤就當「這輪沒掃乾淨」：正向訊號照用，但不許判逾期。
        originals_ok = not originals_errors
        errors = list(errors) + list(originals_errors)

    segments: list[str] = []
    with locked_json(_STATE_PATH, default={"version": 1, "shipments": {}}) as st:
        live_shipments = st.setdefault("shipments", {})
        for key in due:
            rec = live_shipments.get(key)
            if rec is None or rec.get("status") == "closed":
                continue
            if _parse_etd(rec) is None:
                segments.append(f"⚠️ {key} 的 ETD 設定不是有效日期"
                                f"（{rec.get('etd')!r}），請用 track_shipping_docs 修正。")
                continue
            verdict = _verify_shipment(rec, evidence)
            _store_evidence(rec, verdict["hits"])
            if set(DOC_RECIPIENTS) <= verdict["strict"]:
                _close(rec, "email 查核通過：兩位 ContactW 都已收到含此櫃號＋附件的文件信")
                segments.append(_render_closed(rec, verdict["hits"]))
            elif rec.get("sales_confirmed_at") and set(DOC_RECIPIENTS) <= verdict["any"]:
                _close(rec, "業務確認＋email 查有寄件（兩位 ContactW 都有收到帶附件的"
                            "寄件，惟主旨未含櫃號）")
                segments.append(_render_closed(rec, verdict["hits"]))
            else:
                # 每櫃每天最多問一次——今天問過就安靜，明天再追。
                asked = rec.setdefault("asked_dates", [])
                if today.isoformat() in asked:
                    continue
                asked.append(today.isoformat())
                del asked[:-30]  # 狀態檔不無限長大
                segments.append(_render_ask(rec, verdict, today))

        # ── 正本文件（獨立於副本結案，盯到客人簽收為止）──
        for key in originals_watch:
            rec = live_shipments.get(key)
            if rec is None:
                continue
            before = _originals_state(rec).get("status")
            st_orig = _apply_originals(rec, originals_signals, today,
                                       scan_ok=originals_ok)
            if st_orig["status"] in ("acked", "acked_by_us"):
                # 剛從別的狀態變成已簽收 → 講一次就好，之後安靜。
                if before != st_orig["status"]:
                    if st_orig["status"] == "acked_by_us":
                        segments.append(_render_originals(rec, today))
                        continue
                    segments.append(
                        f"📮 正本文件已簽收：{_shipment_head(rec)}\n"
                        f"　客人回覆日期：{st_orig['acked_at']}\n"
                        + (f"　依據：{st_orig['notes'][0]}\n" if st_orig.get("notes") else "")
                        + ("　快遞單號：" + "、".join(st_orig["tracking"])
                           if st_orig["tracking"] else "").rstrip())
                continue
            # 未簽收：每天最多講一次（逾期的每天都會再講，因為每天是新的一筆）。
            # ⚠️ 不可為了「逾期更急」就在同一天重複推 —— dispatcher 重試同一輪
            # 時會變成連環轟炸，而急迫性本來就靠訊息裡的🔴與逾期天數表達。
            asked = rec.setdefault("originals_asked_dates", [])
            if today.isoformat() in asked:
                continue
            asked.append(today.isoformat())
            del asked[:-30]
            segments.append(_render_originals(rec, today))

    errors = list(errors) + list(notice_errors)
    segments = head_segments + segments
    if not segments:
        if errors:
            return ("⚠️ 出貨文件追蹤：本輪沒有新訊息，但信箱查核有問題：\n"
                    + "\n".join(f"  - {e}" for e in errors))
        return "(無新發現)"
    if errors:
        segments.append("⚠️ 另有信箱查核問題：" + "；".join(errors))
    return "\n\n".join(segments)


# 純讀信箱＋寫自己的狀態檔 → 背景 dispatcher 排程可用。
shipping_doc_check.background_safe = True
shipping_doc_escalation.background_safe = True
