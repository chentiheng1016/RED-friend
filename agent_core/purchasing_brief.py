"""採購部門每日簡報資料層 —— UserA（台灣採購）/ UserM（越南採購）的每日信。

需求（2026-08-04，大王轉達 UserA/UserM）：兩人每天 09:00 收一份完整簡報、
15:00 再收一次「待辦有沒有新的／舊的處理完沒」複查。由 daemon_dispatcher 的排程
任務呼叫本模組的工具組裝內容，再靠 task 的 ``notify_emails``（trusted 本地設定，
不是 LLM 輸出）以「自己寄給自己」的方式送到各自信箱。

  UserA  twpurchase2@company.example：**只要兩個主題**（2026-08-06 她本人縮小範圍）
          —— ①每日未讀郵件重點摘要＋AI 簡短回覆 ②2026 出口文件整理（Excel，
          欄位排列照她給的範例表）。待辦追蹤與未完成付款自即日起不進她的信，
          下午那封「待辦複查」也一併退場。
  UserM  vnpurchase2@company.example：維持原樣 —— 未讀信摘要 / 待辦追蹤 /
          本月出口越南福群明細（Excel）/ 越南未完成付款 / TW 出貨進度 /
          VN 廠商交貨進度 / 訂單下單狀況（VN 端另有當地採購，進度歸越南採購管）

本模組刻意**全部確定性**（Gmail API / regex / SQL），LLM 只負責把結果寫成人話與
草擬回覆稿 —— 數字、日期、單號一律照表念（CLAUDE.md〈員工零幻覺〉、記憶
「員工更正→重新查核」）。所以每個函式回的是排好版的文字，不是要 LLM 再加工的
半成品。

資料源與其真實邊界（誠實前提，報表裡不可假裝有）：
  1. 未讀信 / 待辦 —— 各信箱**即時** Gmail（網域委派 SA，同 email_pending_tracker
     那套；RAG 夜跑索引是前一晚快照，判斷「現在誰還沒回」不能用）。可查的信箱
     只有 rag_sync_targets.json 的 gmail_accounts 清單（allowlist），LLM 指不到
     清單外的信箱。
  2. 出口越南福群明細 —— **UserA 自己發的出貨通知信主旨**就是權威來源，格式
     高度固定（「21 Rolls on 2 Pallets --For Jalas , LOT 302-2026 ( STOCKMAYER ),
     CFS , ETD: 4/20 , ETA (CAT LAI): 6/8 -- 預計 6/11可到福群工廠」），逐欄
     regex 解析。ERP 完全沒有這條鏈：PO_RCPT_D 的 ETD/ETA 13,868 列裡只有 2 列
     有值、LOT1/LOT2 放的是批次狀態碼（'0'/'M01'）不是 UserA 的 LOT 號。
     ⚠️ **庫存編號不在主旨裡**，它在隨信附的裝箱單 xlsx（`LOT 302- 2026
     (STOCKMAYER).xlsx`）內 —— 這裡不猜、不編，只把該 LOT 的附件檔名照列，
     要真值請開附件。信件帶「ERP receipt no : LJF…」時才另從 PO_RCPT_D→
     v_item_alias 解出真正的庫存編號（到貨後才會有收貨單，在途批次沒有）。
  3. 未完成付款 —— ERP `GL00__AP_APPLY_M`（付款請示單）。**PAYMENT_ID IS NULL
     就是未付**（實測 1,510 張：STATUS='99' 結案的 1,102 張全有 PAYMENT_ID、
     STATUS='2'/'1' 的 365 張全無），確定性、不必靠金額配對推估。台越以
     `GRT_DEPT` 分流：TWP=台灣採購、VNP=越南採購（另有 TWS/VNS 業務、TWA 會計，
     不歸這兩人）。AP_TYPE/PAY_TYPE 全 NULL 沒在用 → ERP 分不出「預付」vs
     「一般請款」，兩者都在同一張 JFPA 單上，所以報表口徑寫「未完成付款」並把
     REMARK 原樣帶出（LOT／付款日期多寫在那）。
  4. VN 廠商交貨進度 / 訂單下單狀況 —— ERP `v_purchase_orders`（這台 ERP 就是
     越南福群廠的，JF0P 開頭採購單）。

  ⚠️ ERP 面全部是**每日凌晨刷新的鏡像快照**，今天剛開的單要明晨才查得到；
     GL00 應付四表為此加進 erp_mirror.HOT_TABLES（原本只在初鏡快照裡，會凍在
     一個月前 → 未付清單看起來永遠停在上個月）。
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted

# 一個信箱的軟性時間預算（秒）。一個卡住的信箱不該拖垮整份簡報 —— 同
# email_pending_tracker 的取捨，只是這裡一次只掃一個信箱、可以放寬一點。
_MAILBOX_BUDGET_S = 60.0

_MAX_UNREAD = 40
_MAX_TODO_THREADS = 60

# 系統信不是待辦：Decathlon 訂單建立通知、自動回覆、驗證碼那類沒有人在等回覆，
# 混進「對方在等我方回覆」會把真正該回的信淹掉（實測近 7 天 7 件待處理裡 5 件是
# 這類）。只從**待辦**排除、另計一個數字，不是靜默丟掉。
_AUTOMATED_SENDER_RE = re.compile(
    r"(?:^|[<\s])(?:no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|mailer-daemon|"
    r"postmaster|notification[s]?|automated|bounce[s]?)[^@]*@", re.I)
_AUTOMATED_SUBJECT_RE = re.compile(
    r"out of office|automatic reply|auto[- ]?reply|undeliverable|自動回覆|自动回复|"
    r"退信通知", re.I)


# ────────────────────────────────────────────────────────────────────
# Gmail 面（未讀摘要 / 待辦追蹤）
# ────────────────────────────────────────────────────────────────────
def _company_mailboxes() -> dict[str, str]:
    """mailbox → service_account_file。

    來源是 RAG 夜跑同一份 ``var/data/rag_sync_targets.json``（不另建清單，避免
    兩份設定漂移）。這份 dict 同時是**唯一 allowlist**：排程任務的 LLM 只能對
    這裡有的公司信箱下手，指不到外部信箱。
    """
    from agent_core.ingest.sync_config import load_targets

    out: dict[str, str] = {}
    for acct in (load_targets().get("gmail_accounts") or []):
        if not isinstance(acct, dict):
            continue
        mailbox = str(acct.get("mailbox") or "").strip().lower()
        sa_file = str(acct.get("service_account_file") or "").strip()
        if mailbox and sa_file:
            out[mailbox] = sa_file
    return out


def _gmail_users(mailbox: str, label: str):
    """回該信箱的 Gmail ``users()`` resource；不在公司清單就 ValueError。"""
    accounts = _company_mailboxes()
    key = (mailbox or "").strip().lower()
    if key not in accounts:
        known = "、".join(sorted(accounts)) or "（設定檔沒有任何公司信箱）"
        raise ValueError(f"{mailbox} 不是已設定的公司信箱。可查的信箱：{known}")

    from agent_core.google_auth import get_service_for_account

    service = get_service_for_account(
        f"{label}:{key}", "gmail", "v1",
        service_account_file=accounts[key], subject=key, scopes=None,
    )
    return service.users()


def _headers_of(msg: dict) -> dict[str, str]:
    return {h.get("name", ""): h.get("value", "")
            for h in (msg.get("payload", {}) or {}).get("headers", [])}


def _domain_of(from_header: str) -> str:
    addr = (from_header or "").lower()
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip("> ").split()[0].strip(">").strip()


def _is_outbound(msg: dict, own_domain: str) -> bool:
    """這封是不是我方發的。

    以 Gmail 的 ``SENT`` label 為準、寄件網域只當退路：實測本人回覆的信 metadata
    的 From 可能是空的（回覆自己那條 thread 時），純比網域會把「我方已回」誤判成
    「還沒回」—— 待辦清單多出一堆假待辦，比漏掉更糟。
    """
    if "SENT" in (msg.get("labelIds") or []):
        return True
    dom = _domain_of(_headers_of(msg).get("From", ""))
    return bool(dom) and dom == (own_domain or "").lower()


def _msg_date(internal_date_ms: Any) -> date | None:
    try:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000).date()
    except (TypeError, ValueError, OSError):
        return None


def unread_digest(mailbox: str, days: int = 3, limit: int = 25) -> str:
    """列出某公司信箱**目前未讀**的信（寄件人/主旨/日期/摘要片段）。

    days 只是上界（避免翻出陳年未讀），實際判準是 Gmail 的 ``is:unread``。
    回傳內容全部包在 <untrusted-email> 裡：外部寄件人可控文字，是資料不是指令。
    """
    days = max(1, min(int(days or 3), 30))
    limit = max(1, min(int(limit or 25), _MAX_UNREAD))
    try:
        users = _gmail_users(mailbox, "purchasing_unread")
    except Exception as exc:  # noqa: BLE001 —— 設定/授權問題要看得見、不要整份簡報掛掉
        return f"⚠️ 讀不到 {mailbox} 的未讀信：{exc}"

    try:
        listed = users.messages().list(
            userId="me", q=f"is:unread newer_than:{days}d", maxResults=limit,
        ).execute()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀不到 {mailbox} 的未讀信：{exc}"

    ids = [m["id"] for m in (listed.get("messages") or []) if m.get("id")]
    if not ids:
        return f"📥 {mailbox}：近 {days} 天沒有未讀信。"

    deadline = time.monotonic() + _MAILBOX_BUDGET_S
    lines: list[str] = []
    truncated = False
    for idx, mid in enumerate(ids, start=1):
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            msg = users.messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except Exception:  # noqa: BLE001 —— 單封讀不到就跳過，不影響其他封
            continue
        head = _headers_of(msg)
        when = _msg_date(msg.get("internalDate"))
        lines.append(
            f"[{idx}] {when or '?'} ｜ {sanitize_for_llm(head.get('From', ''))}\n"
            f"    主旨：{sanitize_for_llm(head.get('Subject', '（無主旨）'))}\n"
            f"    摘要：{sanitize_for_llm(msg.get('snippet', ''))[:300]}\n"
            f"    thread：{msg.get('threadId', '')}"
        )

    if not lines:
        return f"📥 {mailbox}：抓到 {len(ids)} 封未讀，但逐封讀取都失敗（稍後重試）。"

    header = f"📥 {mailbox} 未讀信 {len(lines)} 封（近 {days} 天）"
    if truncated:
        header += f"；⚠️ 逾時只讀完 {len(lines)}/{len(ids)} 封"
    return header + "\n" + wrap_as_untrusted("\n\n".join(lines), "untrusted-email")


def todo_tracker(mailbox: str, days: int = 14, limit: int = 40) -> str:
    """待辦追蹤：某信箱近 N 天有動靜的 thread，依「最後一句是誰講的」分兩堆。

    - 🔴 待處理：最後一封是外部寄來 → 我方還沒回。附已等待天數。
    - ✅ 近期已回：最後一封是我方發的 → 已回覆。附我方回覆的摘要片段，讓上層
      **依回覆內容**判斷是不是真的結案（UserA 的原話：「依照回覆的內容判定是否
      已經處理完畢」）—— 「收到，明天給你」跟「已出貨，單號 xxx」不是同一件事，
      這個判斷交給 LLM，本函式只提供確定性的事實面。
    """
    days = max(1, min(int(days or 14), 90))
    limit = max(1, min(int(limit or 40), _MAX_TODO_THREADS))
    try:
        users = _gmail_users(mailbox, "purchasing_todo")
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀不到 {mailbox} 的待辦：{exc}"

    own_domain = mailbox.rsplit("@", 1)[-1] if "@" in mailbox else ""
    try:
        listed = users.threads().list(
            userId="me", q=f"newer_than:{days}d", maxResults=limit,
        ).execute()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀不到 {mailbox} 的待辦：{exc}"

    threads = [t for t in (listed.get("threads") or []) if t.get("id")]
    if not threads:
        return f"🗒️ {mailbox}：近 {days} 天沒有信件往來。"

    today = date.today()
    deadline = time.monotonic() + _MAILBOX_BUDGET_S
    pending: list[str] = []
    answered: list[str] = []
    scanned = 0
    automated = 0
    for th in threads:
        if time.monotonic() > deadline:
            break
        try:
            detail = users.threads().get(
                userId="me", id=th["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
        except Exception:  # noqa: BLE001
            continue
        messages = detail.get("messages") or []
        if not messages:
            continue
        scanned += 1
        last = messages[-1]
        head = _headers_of(last)
        subject = sanitize_for_llm(head.get("Subject", "（無主旨）"))
        sender = sanitize_for_llm(head.get("From", ""))
        when = _msg_date(last.get("internalDate"))
        waited = (today - when).days if when else "?"
        snippet = sanitize_for_llm(last.get("snippet", ""))[:220]

        if _is_outbound(last, own_domain):
            answered.append(
                f"  ✅ {subject}\n"
                f"     我方最後回覆：{when or '?'}（{waited} 天前）\n"
                f"     回覆內容：{snippet}"
            )
        elif (_AUTOMATED_SENDER_RE.search(head.get("From", ""))
                or _AUTOMATED_SUBJECT_RE.search(head.get("Subject", ""))):
            automated += 1
        else:
            pending.append(
                f"  🔴 {subject}\n"
                f"     來自 {sender}｜對方最後來信 {when or '?'}（已等 {waited} 天）\n"
                f"     內容：{snippet}"
            )

    out = [f"🗒️ {mailbox} 待辦追蹤（掃了近 {days} 天的 {scanned} 個 thread）",
           f"待我方處理 {len(pending)} 件、我方已回 {len(answered)} 件"
           + (f"、系統/自動通知 {automated} 件已排除" if automated else "")]
    out.append(f"\n【待處理 — 對方在等我方回覆】（{len(pending)}）")
    out.append("\n".join(pending) if pending else "  （無）")
    out.append(f"\n【我方已回 — 請依回覆內容判斷是否真的結案】（{len(answered)}）")
    out.append("\n".join(answered) if answered else "  （無）")
    return "\n".join(out[:2]) + "\n" + wrap_as_untrusted(
        "\n".join(out[2:]), "untrusted-email")


# ────────────────────────────────────────────────────────────────────
# 出口越南福群明細（UserA 出貨通知信主旨解析）
# ────────────────────────────────────────────────────────────────────
# 一則出貨通知信的主旨長這樣（實測 2026-04~08 共 60+ 封，格式高度固定）：
#   「21 Rolls on 2 Pallets --For Jalas , LOT 302-2026 ( STOCKMAYER ), CFS ,
#     ETD: 4/20 , ETA (CAT LAI): 6/8  -- 預計 6/11可到福群工廠 。」
# 每一欄各自獨立 regex：某個變體打壞了一欄，其餘欄位照樣拿得到（比一條大
# regex 整條 miss 好）。年份主旨裡沒寫 → 用信件日期當錨點推（見 _resolve_md）。
_QTY_RE = re.compile(
    r"^\s*([\d,]+\s*[A-Za-z]+(?:\s+(?:on|in)\s+[\d,]+\s*[A-Za-z]+)?)", re.I)
_LOT_RE = re.compile(r"\bLOT[\s.:#\-]*([A-Za-z0-9]+-\d{4})\b", re.I)
_ETD_RE = re.compile(r"\bETD\s*[:：]?\s*(\d{1,2}\s*/\s*\d{1,2})", re.I)
_ETA_CATLAI_RE = re.compile(
    r"\bETA\s*[(（]?\s*CAT\s*LAI\s*[)）]?\s*[:：]?\s*(\d{1,2}\s*/\s*\d{1,2})", re.I)
# 福群到廠日兩種寫法：中文「預計 6/11 可到…福群工廠」與英式「ETA (FU CHUN): 4/28」。
_ETA_FUCHUN_CN_RE = re.compile(
    r"預計\s*(\d{1,2}\s*/\s*\d{1,2})\s*(?:可)?到")
_ETA_FUCHUN_EN_RE = re.compile(
    r"\bETA\s*[(（]?\s*FU\s*CHUN\s*[)）]?\s*[:：]?\s*(\d{1,2}\s*/\s*\d{1,2})", re.I)
# 客戶：「--For Jalas ,」「-For Lurchi_ LOT」「--For Decathlon LOT 209-2026」
_CUSTOMER_RE = re.compile(
    r"[Ff]or\s+([A-Za-z][A-Za-z/&.\- ]*?)\s*(?=[,_]|\s+LOT\b|$)")
# 沒寫 For 的變體，客戶夾在供應商括號與 CFS 之間：「(三宏 ,江門利華新), LURCHI/RICHTER,  CFS」
_CUSTOMER_ALT_RE = re.compile(
    r"[)）]\s*,?\s*([A-Za-z][A-Za-z/&.\- ]*?)\s*,\s*(?:CFS|FCL|LCL)\b", re.I)
_PAREN_RE = re.compile(r"[(（]\s*([^)）]+?)\s*[)）]")
# 括號裡不是供應商的東西 —— 供應商取第一個不是這些的括號。
# 「VN Shipping advice LOT325-2026 ISCO SEA (ETD 7/13 ETA 9/5)」實測會把整串船期
# 當成供應商名寫進報表，所以船期字樣/純日期/純數字/短碼一律排除。
_NOT_SUPPLIER_RE = re.compile(
    r"CAT\s*LA|FU\s*CHUN|\bETD\b|\bETA\b|\d{1,2}\s*/\s*\d{1,2}|^\d+$|^[A-Z]{1,3}\d*$",
    re.I)
# 主旨前綴（RE:/Re:/回复:/FW:/REVISED :/RE-SEND,）—— 數量錨在字首，被前綴擋住就
# 抓不到（實測轉寄鏈上的同一封通知會少掉「9 PACKAGES」）。可重複剝多層。
_REPLY_PREFIX_RE = re.compile(
    r"^(?:\s*(?:RE|Re|FW|Fwd?|FWD|REVISED|RE-?SEND|RESENT|回复|回覆|轉寄|轉發|答复)"
    r"\s*[:：,，]\s*)+")
# 到貨後 UserA 會在信裡附 ERP 收貨單號，這是唯一能把 LOT 接回 ERP 料號的橋。
_ERP_RCPT_RE = re.compile(r"\b(L[A-Z]{2}\d{8})\b")


def _resolve_md(md: str, anchor: date) -> date | None:
    """把主旨裡的「6/8」補上年份 —— 取離信件日期最近的那一年。

    主旨從不寫年份，而 ETD→ETA 可以拖一個多月（實測 LOT 317：ETD 7/19、
    ETA 8/26），跨年信（12 月寄、1 月到）用「信件年份」直接套會差一整年。
    候選 anchor.year±1 取距離最小者；2/30 這種不存在的日期直接跳過。
    """
    try:
        month_s, day_s = md.replace(" ", "").split("/", 1)
        month, day = int(month_s), int(day_s)
    except (ValueError, AttributeError):
        return None
    best: date | None = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            cand = date(year, month, day)
        except ValueError:      # 2/30、13/1 之類
            continue
        if best is None or abs((cand - anchor).days) < abs((best - anchor).days):
            best = cand
    return best


def _first_supplier(subject: str) -> str:
    for group in _PAREN_RE.findall(subject):
        text = group.strip(" ,、")
        if text and not _NOT_SUPPLIER_RE.search(text):
            return re.sub(r"\s*,\s*", "、", text)
    return ""


def _customer(subject: str) -> str:
    m = _CUSTOMER_RE.search(subject)
    if m:
        return m.group(1).strip(" -_,")
    m = _CUSTOMER_ALT_RE.search(subject)
    return m.group(1).strip(" -_,") if m else ""


def parse_shipment_subject(subject: str, anchor: date) -> dict | None:
    """出貨通知信主旨 → 一筆出口紀錄；不是標準出貨通知就回 None。

    判準（三者都要）：LOT + ETD + 至少一個**有標名的** ETA（CAT LAI 或 福群）。
    第三項是把「運費/付款通知」擋在外面的關鍵：那類信也帶 LOT 與 ETD，但 ETD 是
    寫在付款條件的算式裡（『付款到期日: 2026/9/15 (=ETD:7/19+60天)』），欄位大半
    是空的；沒這道閘就會有一封 7/24 的運費信蓋掉 7/22 那封欄位齊全的真通知。
    """
    subject = _REPLY_PREFIX_RE.sub("", (subject or "").strip())
    if not subject:
        return None
    lot_m = _LOT_RE.search(subject)
    etd = _ETD_RE.search(subject)
    if not (lot_m and etd):
        return None
    eta_catlai = _ETA_CATLAI_RE.search(subject)
    fuchun = _ETA_FUCHUN_CN_RE.search(subject) or _ETA_FUCHUN_EN_RE.search(subject)
    if not (eta_catlai or fuchun):
        return None
    qty_m = _QTY_RE.search(subject)
    return {
        "lot": lot_m.group(1).upper(),
        "supplier": _first_supplier(subject),
        "customer": _customer(subject),
        "qty": re.sub(r"\s+", " ", qty_m.group(1)).strip() if qty_m else "",
        "etd": _resolve_md(etd.group(1), anchor),
        "eta_catlai": _resolve_md(eta_catlai.group(1), anchor) if eta_catlai else None,
        "eta_fuchun": _resolve_md(fuchun.group(1), anchor) if fuchun else None,
    }


def _completeness(row: dict) -> int:
    """一列填了幾個關鍵欄位 —— 同 LOT 取代時的優先序（比「最新」更該贏）。"""
    return sum(1 for key in ("etd", "eta_catlai", "eta_fuchun", "qty", "supplier")
               if row.get(key))


def _shipment_status(row: dict, today: date) -> str:
    """依今天 vs 三個節點推目前應該在哪一段。全是**預計**日期，故都標「預計」。"""
    etd, catlai, fuchun = row.get("etd"), row.get("eta_catlai"), row.get("eta_fuchun")
    if etd and today < etd:
        return "待出貨"
    if catlai and today < catlai:
        return "海上運送中"
    if fuchun and today < fuchun:
        return "已抵 CAT LAI（清關/內陸）"
    if fuchun and today >= fuchun:
        return "應已到福群（預計）"
    return "已出貨"


def _period_bounds(period: str) -> tuple[date, date, str]:
    """"2026" / "2026-08" / "" → (起日, 迄日「不含」, 標籤)。

    三種寫法：整年（UserA 的「2026 出口文件整理」要的是全年，不是本月）、單月、
    空＝本月。壞格式一律退回本月 —— 報表寧可講小一點，也不要把範圍猜大。
    年份限 2000–2099：四位數字什麼都收會讓 `date(year + 1, …)` 在 9999 炸掉。
    """
    today = date.today()
    text = str(period or "")
    m = re.match(r"^\s*(\d{4})\s*$", text)
    if m and 2000 <= int(m.group(1)) <= 2099:
        year = int(m.group(1))
        return date(year, 1, 1), date(year + 1, 1, 1), f"{year}"

    year, mon = today.year, today.month
    m = re.match(r"^\s*(\d{4})[-/](\d{1,2})\s*$", text)
    if m and 1 <= int(m.group(2)) <= 12:
        year, mon = int(m.group(1)), int(m.group(2))
    start = date(year, mon, 1)
    end = date(year + (mon == 12), (mon % 12) + 1, 1)
    return start, end, f"{year}-{mon:02d}"


def _erp_item_codes(receipt_nos: list[str]) -> dict[str, str]:
    """ERP 收貨單號 → 「庫存編號」字串（該收貨單上的料，去重後逗號串）。

    只有到貨後才有收貨單，在途批次查不到 —— 查不到就留空，由呼叫端寫「見附件」，
    絕不拿別的欄位頂替（UserA SF24/G407 案的教訓：查無 ≠ 可以腦補）。
    """
    if not receipt_nos:
        return {}
    from agent_core import erp_stock_query as esq

    if not esq._db_ready():
        return {}
    placeholders = ", ".join("?" for _ in receipt_nos)
    try:
        rows = esq._run(
            "SELECT d.CHK_NO, COALESCE(NULLIF(a.\"庫存編號\", ''), d.ITEM_NO) "
            "FROM SC00__PO_RCPT_D d "
            "LEFT JOIN v_item_alias a ON a.\"料號\" = d.ITEM_NO "
            f"WHERE d.CHK_NO IN ({placeholders})",
            list(receipt_nos),
        )
    except Exception:  # noqa: BLE001 —— 加值資訊，查壞不擋主表
        return {}
    out: dict[str, list[str]] = {}
    for chk_no, code in rows:
        bucket = out.setdefault(str(chk_no or "").strip(), [])
        code = str(code or "").strip()
        if code and code not in bucket:
            bucket.append(code)
    return {k: "、".join(v[:6]) for k, v in out.items() if v}


def _lake_shipment_rows(start: date, end: date) -> tuple[list[dict], str]:
    """從 email lake 撈出口通知信 → 逐 LOT 最新一筆。回 (rows, 警語)。"""
    from agent_core.email_lake import _lake_load_df

    df = _lake_load_df()
    if df is None or getattr(df, "empty", True):
        return [], "⚠️ email lake 沒有資料（var/data/data_lake/emails_master.parquet 空或讀不到）。"

    by_lot: dict[str, dict] = {}
    for _, rec in df.iterrows():
        subject = str(rec.get("subject") or "")
        try:
            anchor = datetime.strptime(str(rec.get("date"))[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        row = parse_shipment_subject(subject, anchor)
        if row is None:
            continue
        # 同一個 LOT 會被改期重寄（REVISED / RE-SEND）＋轉寄鏈上一堆 Re:。取代規則
        # 是「欄位齊的優先，一樣齊才比新」——純比新會讓轉寄鏈末端那封欄位殘缺的
        # 蓋掉原始通知（實測 LOT 317/321 就是這樣掉了數量與 ETA）。
        row["_mail_date"] = anchor
        prev = by_lot.get(row["lot"])
        if prev is not None:
            prev_key = (_completeness(prev), prev["_mail_date"])
            if prev_key >= (_completeness(row), anchor):
                continue
        row["attachments"] = str(rec.get("attachment_names") or "")
        row["receipt_no"] = ""
        blob = subject + " " + str(rec.get("body_snippet") or "")
        rcpt = _ERP_RCPT_RE.search(blob)
        if rcpt:
            row["receipt_no"] = rcpt.group(1)
        by_lot[row["lot"]] = row

    # 「本月出口」＝ 本月出的貨；但在途批次的 ETD 在上個月、這個月才到廠，UserA
    # 追的是同一批貨 → ETD/ETA(CAT LAI)/ETA(福群) 任一落在本月都收進來，狀態欄
    # 會標清楚它現在走到哪。
    picked = [
        r for r in by_lot.values()
        if any(d is not None and start <= d < end
               for d in (r.get("etd"), r.get("eta_catlai"), r.get("eta_fuchun")))
    ]
    picked.sort(key=lambda r: (r.get("etd") or date.max, r["lot"]))
    return picked, ""


def _packing_list_hint(attachments: str, lot: str) -> str:
    """該 LOT 的裝箱單附件檔名（庫存編號的真值在裡面）。"""
    for name in str(attachments or "").split("|"):
        name = name.strip()
        if not name or not name.lower().endswith((".xlsx", ".xls")):
            continue
        if name.lower().startswith("image"):
            continue
        return f"見附件 {sanitize_for_llm(name)}"
    return f"見 LOT {lot} 裝箱單"


# 欄位順序＝ UserA 2026-08-06 給的範例表：客戶／供應商／LOT／數量／ETD／
# ETA (CAT LAI)／ETA (FU CHUN)／庫存編號。**Excel 就照這 8 欄、一欄不多**
# （她的原話是「排列如附件」）。「狀態」是本模組自己推算的加值欄、不在她的表裡，
# 所以只出現在信件內文的預覽表 —— UserM 的晨報要讀它挑出「標應已到福群但倉庫
# 沒收到」的批次，拿掉會讓那一段沒東西可看。
_EXPORT_COLUMNS = ["客戶", "供應商", "LOT", "數量", "ETD", "ETA (CAT LAI)",
                   "ETA (FU CHUN)", "庫存編號"]
_STATUS_COLUMN = "狀態"

# 內文預覽表的列數上限。整年份（UserA 的 2026 表）動輒上百批，全貼進信裡會把
# 前面的未讀重點洗掉 —— 信裡留最近的幾批，完整版在 Excel 附件。單月報表遠低於
# 這個數，行為不變。
_TEXT_TABLE_MAX_ROWS = 25


def _fmt_day(value: date | None, base_year: int) -> str:
    """日期欄：同年寫 MM/DD（照 UserA 的表），跨年才補年份免得看成今年。"""
    if value is None:
        return "—"
    return value.strftime("%m/%d") if value.year == base_year \
        else value.strftime("%Y/%m/%d")


def _ascii_table(columns: list[str], rows: list[list[str]]) -> str:
    """等寬對齊的文字表（CJK 算兩格），包在 ``` 圍欄裡直接貼進信件。"""
    widths = [max([_disp_width(col)] + [_disp_width(r[i]) for r in rows if i < len(r)])
              for i, col in enumerate(columns)]
    lines = ["```",
             " | ".join(_pad(c, widths[i]) for i, c in enumerate(columns)),
             "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(_pad(c, widths[i]) for i, c in enumerate(row))
              for row in rows]
    lines.append("```")
    return "\n".join(lines)


def vn_export_shipments(period: str = "", to_excel: bool = True) -> str:
    """出口到越南福群的明細（客戶/供應商/LOT/數量/ETD/ETA(CAT LAI)/ETA(FU CHUN)/庫存編號）。

    period：空＝本月、``"2026-07"``＝單月、``"2026"``＝整年（UserA 的「2026 出口
    文件整理」）。to_excel=True 時另存一份 Excel 並回 ``[[MAIL_FILE:...]]`` 標記，
    讓排程任務把它當附件寄出；Excel 是完整的，內文表可能只列最近幾批。
    """
    start, end, label = _period_bounds(period)
    rows, warn = _lake_shipment_rows(start, end)
    if warn:
        return warn
    if not rows:
        return (f"📦 {label} 沒有查到出口越南福群的出貨通知信"
                f"（來源：出貨通知信主旨，ETD/ETA 任一落在 {label}）。")

    codes = _erp_item_codes([r["receipt_no"] for r in rows if r.get("receipt_no")])
    today = date.today()
    table: list[list[str]] = []
    statuses: list[str] = []
    for r in rows:
        code = codes.get(r.get("receipt_no", ""), "")
        table.append([
            sanitize_for_llm(r["customer"]) or "—",
            sanitize_for_llm(r["supplier"]) or "—",
            r["lot"],
            sanitize_for_llm(r["qty"]) or "—",
            _fmt_day(r["etd"], start.year),
            _fmt_day(r["eta_catlai"], start.year),
            _fmt_day(r["eta_fuchun"], start.year),
            code or _packing_list_hint(r.get("attachments", ""), r["lot"]),
        ])
        statuses.append(_shipment_status(r, today))

    # 內文預覽：8 欄 + 狀態。超過上限時留**尾端**（ETD 排序 → 最近/即將出的那批
    # 才是今天看得動的），並明講被略過幾批、完整版在附件。
    preview = [row + [status] for row, status in zip(table, statuses)]
    omitted = max(0, len(preview) - _TEXT_TABLE_MAX_ROWS)
    if omitted:
        preview = preview[-_TEXT_TABLE_MAX_ROWS:]

    out = [f"📦 {label} 出口越南福群 {len(table)} 批（來源：出貨通知信主旨；"
           f"同 LOT 改期重寄時取欄位最齊、其次最新的那封）",
           _ascii_table(_EXPORT_COLUMNS + [_STATUS_COLUMN], preview)]
    if omitted:
        out.append(f"ℹ️ 內文只列 ETD 最近的 {len(preview)} 批，較早的 {omitted} 批"
                   f"不在上表 —— 完整 {len(table)} 批在附件 Excel 裡。")
    if any(not codes.get(r.get("receipt_no", "")) for r in rows):
        out.append("ℹ️ 庫存編號欄：ERP 只有『到貨後』的收貨單才對得回料號，在途批次"
                   "查無 —— 這些列照列裝箱單附件檔名，真值請開該附件，勿以其他欄推估。")
    if to_excel:
        path = _write_export_excel(label, _EXPORT_COLUMNS, table)
        if path:
            # 側通道：LLM 沒把標記抄進回覆時，dispatcher 仍取得到這個檔
            # （見 agent_core/deliverables 的說明）。標記本身保留不動。
            from agent_core.deliverables import register as _register_deliverable
            _register_deliverable(path)
            out.append(f"[[MAIL_FILE:{path}]]")
        else:
            out.append("⚠️ Excel 產檔失敗（內容同上表，可直接看）。")
    return "\n\n".join(out)


def _disp_width(s: Any) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(s or ""))


def _pad(s: Any, width: int) -> str:
    return str(s or "") + " " * max(0, width - _disp_width(s))


def _write_export_excel(label: str, columns: list[str],
                        table: list[list[str]]) -> str:
    """把明細寫成 Excel，回絕對路徑（失敗回 ""）。

    重用 doc_export 的 renderer（欄寬/斑馬紋/繁中字型都調好了），不另造一份。
    """
    try:
        from agent_core.doc_export import EXPORTS_DIR, _normalize_spec, _render_excel
    except Exception:  # noqa: BLE001
        return ""
    out_dir = os.path.join(EXPORTS_DIR, "purchasing")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"出口越南福群明細_{label}.xlsx")
        _render_excel(_normalize_spec({
            "title": f"{label} 出口越南福群明細",
            "subtitle": f"來源：出貨通知信主旨（同 LOT 取欄位最齊、其次最新的那封）"
                        f"｜產出 {date.today()}",
            "columns": columns,
            "rows": table,
        }), path)
        return path
    except Exception:  # noqa: BLE001 —— 產檔是加值，失敗不擋文字表
        return ""


# ────────────────────────────────────────────────────────────────────
# ERP 面（未完成付款 / 交貨進度 / 下單狀況）
# ────────────────────────────────────────────────────────────────────
# GRT_DEPT 就是開單部門，台越付款各歸各的（實測未付 389 張：VNP 174、TWP 139，
# 其餘 TWS/VNS 業務、TWA 會計、S/PP 不屬這兩人）。
_DEPT_LABELS = {"TWP": "台灣採購", "VNP": "越南採購"}
# STATUS 已確認的碼值才翻譯，其餘照碼顯示（同 erp_stock_query 的確定性慣例）。
_AP_STATUS = {"1": "新建", "2": "待付款", "7": "生效", "99": "結案"}


def _erp_guard() -> str:
    from agent_core import erp_stock_query as esq
    if not esq._db_ready():
        return "⚠️ ERP 本地鏡像尚未建立（var/data/erp_mirror/erp_full.duckdb 不存在）。"
    return ""


def open_payment_requests(dept: str = "TWP", limit: int = 40) -> str:
    """某採購部門「開了付款請示單但還沒付款」的清單（含幣別小計）。

    未付判準：``PAYMENT_ID IS NULL`` —— ERP 付款後才會回填付款單 ID，這是確定性
    欄位，不用金額配對推估。dept：TWP=台灣採購、VNP=越南採購。
    """
    guard = _erp_guard()
    if guard:
        return guard
    from agent_core import erp_stock_query as esq

    dept = (dept or "TWP").strip().upper()
    if dept not in _DEPT_LABELS:
        return f"dept 只能是 {'/'.join(_DEPT_LABELS)}（收到 {dept}）。"
    limit = max(1, min(int(limit or 40), 200))
    try:
        rows = esq._run(
            "SELECT a.APPLY_NO, SUBSTR(a.APPLY_DATE, 1, 10), "
            "       COALESCE(NULLIF(v.SHORTNM_T, ''), NULLIF(v.SHORTNM_E, ''), "
            "                NULLIF(v.FULLNM_T, ''), a.VEND_NO), "
            "       a.MONEY_UNIT, TRY_CAST(a.NET_MONEY AS DOUBLE), a.STATUS, a.REMARK "
            "FROM GL00__AP_APPLY_M a "
            "LEFT JOIN SC00__PO_VENDER_M v "
            "  ON v.VEND_NO = a.VEND_NO AND v.ORG_ID = a.ORG_ID "
            "WHERE (a.PAYMENT_ID IS NULL OR a.PAYMENT_ID = '') AND a.GRT_DEPT = ? "
            "ORDER BY a.APPLY_DATE DESC",
            [dept],
        )
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 查 ERP 未完成付款失敗：{exc}"

    if not rows:
        return f"💰 {_DEPT_LABELS[dept]}：目前沒有未完成的付款請示單。" + esq._stale_hint()

    totals: dict[str, float] = {}
    for _, _, _, currency, money, _, _ in rows:
        cur = esq._clean(currency) or "?"
        totals[cur] = totals.get(cur, 0.0) + float(money or 0)

    shown = rows[:limit]
    lines = [f"💰 {_DEPT_LABELS[dept]}未完成付款 {len(rows)} 張"
             f"（未付＝ERP 尚未回填付款單號）",
             "合計：" + "、".join(f"{cur} {amount:,.2f}"
                                for cur, amount in sorted(totals.items())),
             "```"]
    for apply_no, apply_date, vendor, currency, money, status, remark in shown:
        status_txt = _AP_STATUS.get(esq._clean(status), esq._clean(status) or "?")
        note = esq._clean(remark)
        lines.append(
            f"{esq._clean(apply_no)} | {esq._clean(apply_date)} | "
            f"{esq._clean(vendor)} | {esq._clean(currency)} {float(money or 0):,.2f} | "
            f"{status_txt}" + (f" | {note}" if note else "")
        )
    lines.append("```")
    if len(rows) > len(shown):
        lines.append(f"（只列最新 {len(shown)} 張，合計含全部 {len(rows)} 張）")
    return "\n".join(lines) + esq._stale_hint()


# 逾期超過這個天數的「生效未收齊」列多半是 ERP 沒人結案的殭屍單（實測 164 筆
# 逾期裡有一票停在 2023 年、逾期 1,200 天以上）。它們不是今天要追的交期，混在
# 清單最前面只會把真正該追的 20 筆蓋掉 —— 移到一行計數、不消失也不洗版。
_STALE_OVERDUE_DAYS = 180


def supplier_delivery_progress(days_ahead: int = 21, limit: int = 40) -> str:
    """越南廠商交貨進度：ERP 生效採購單裡「還沒收齊」的明細，逾期的排前面。"""
    guard = _erp_guard()
    if guard:
        return guard
    from agent_core import erp_stock_query as esq

    days_ahead = max(1, min(int(days_ahead or 21), 180))
    limit = max(1, min(int(limit or 40), 200))
    try:
        rows = esq._run(
            'SELECT "採購單號", SUBSTR("計劃到貨日", 1, 10), "供應商簡稱", "料號", '
            '       TRY_CAST("訂購數量" AS DOUBLE), '
            '       COALESCE(TRY_CAST("已收數量" AS DOUBLE), 0), '
            '       "採購單位", "品名描述" '
            'FROM v_purchase_orders '
            'WHERE "明細狀態" = \'生效\' '
            '  AND COALESCE(TRY_CAST("已收數量" AS DOUBLE), 0) '
            '      < TRY_CAST("訂購數量" AS DOUBLE) '
            'ORDER BY "計劃到貨日"',
            [],
        )
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 查 ERP 交貨進度失敗：{exc}"
    if not rows:
        return "🚚 ERP 目前沒有『生效但未收齊』的採購明細。" + esq._stale_hint()

    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    overdue: list[tuple[date, str]] = []
    upcoming: list[str] = []
    later = 0
    stale = 0
    for po, eta, vendor, item, ordered, received, unit, name in rows:
        eta_date = _parse_iso(esq._clean(eta))
        gap = float(ordered or 0) - float(received or 0)
        line = (f"{esq._clean(po)} | {esq._clean(eta) or '無到貨日'} | "
                f"{esq._clean(vendor)} | {esq._clean(item)} | "
                f"欠 {esq._fmt_qty(gap)}{esq._clean(unit)}"
                f"（訂 {esq._fmt_qty(float(ordered or 0))}／"
                f"收 {esq._fmt_qty(float(received or 0))}）"
                + (f" | {esq._clean(name)}" if name else ""))
        if eta_date and eta_date < today:
            late_days = (today - eta_date).days
            if late_days > _STALE_OVERDUE_DAYS:
                stale += 1
                continue
            overdue.append((eta_date, f"{line} | 逾期 {late_days} 天"))
        elif eta_date is None or eta_date <= horizon:
            upcoming.append(line)
        else:
            later += 1

    # 逾期區由新到舊 —— 剛過期的才是今天追得動的，越舊越接近呆單。
    overdue.sort(key=lambda pair: pair[0], reverse=True)
    out = [f"🚚 越南廠商交貨進度：未收齊 {len(rows)} 筆"
           f"（逾期 {len(overdue)}、{days_ahead} 天內到期 {len(upcoming)}、"
           f"更晚 {later}、陳年未結 {stale}）"]
    for title, bucket in ((f"⚠️ 已逾期未到（{_STALE_OVERDUE_DAYS} 天內，新到舊）",
                           [line for _, line in overdue]),
                          (f"📅 {days_ahead} 天內應到", upcoming)):
        out.append(f"\n【{title}】（{len(bucket)}）")
        if not bucket:
            out.append("  （無）")
            continue
        out.append("```\n" + "\n".join(bucket[:limit]) + "\n```")
        if len(bucket) > limit:
            out.append(f"（只列 {limit} 筆，實際 {len(bucket)} 筆）")
    if stale:
        out.append(f"\nℹ️ 另有 {stale} 筆逾期超過 {_STALE_OVERDUE_DAYS} 天未收齊，"
                   "多為 ERP 未結案的舊單、不在每日追蹤範圍，建議另排一次清理。")
    return "\n".join(out) + esq._stale_hint()


def recent_purchase_orders(days: int = 7, limit: int = 40) -> str:
    """訂單下單狀況：近 N 天 ERP 新開的採購單（單別彙總 + 項次/數量）。"""
    guard = _erp_guard()
    if guard:
        return guard
    from agent_core import erp_stock_query as esq

    days = max(1, min(int(days or 7), 90))
    limit = max(1, min(int(limit or 40), 200))
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        rows = esq._run(
            'SELECT "採購單號", MIN(SUBSTR("下單日期", 1, 10)), '
            '       MIN("供應商簡稱"), MIN("採購類型"), COUNT(*), '
            '       SUM(TRY_CAST("訂購數量" AS DOUBLE)), '
            '       SUM(CASE WHEN "明細狀態" = \'生效\' THEN 1 ELSE 0 END), '
            '       SUM(CASE WHEN "明細狀態" = \'取消\' THEN 1 ELSE 0 END) '
            'FROM v_purchase_orders WHERE SUBSTR("下單日期", 1, 10) >= ? '
            'GROUP BY "採購單號" ORDER BY MIN("下單日期") DESC',
            [since],
        )
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 查 ERP 下單狀況失敗：{exc}"
    if not rows:
        return f"🧾 近 {days} 天 ERP 沒有新開的採購單。" + esq._stale_hint()

    lines = [f"🧾 近 {days} 天下單 {len(rows)} 張採購單", "```"]
    for po, ordered_at, vendor, po_type, items, qty, active, cancelled in rows[:limit]:
        flags = []
        if int(active or 0):
            flags.append(f"生效 {int(active)}")
        if int(cancelled or 0):
            flags.append(f"取消 {int(cancelled)}")
        lines.append(
            f"{esq._clean(po)} | {esq._clean(ordered_at)} | {esq._clean(vendor)} | "
            f"{esq._clean(po_type) or '—'} | {int(items or 0)} 項 | "
            f"共 {esq._fmt_qty(float(qty or 0))} | {'／'.join(flags) or '—'}"
        )
    lines.append("```")
    if len(rows) > limit:
        lines.append(f"（只列 {limit} 張，實際 {len(rows)} 張）")
    return "\n".join(lines) + esq._stale_hint()


def _parse_iso(value: str) -> date | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
