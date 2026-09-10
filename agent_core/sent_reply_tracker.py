"""追「大王本人寄出去、對方到現在還沒回」的 thread。

跟 :mod:`agent_core.email_pending_tracker` 剛好相反的方向：

  * email_pending_tracker：**別人寄來、我方沒回** → 催我方回信。
  * 這支（sent_reply_tracker）：**我本人寄出、對方沒回** → 催對方回信。

⚠️ 這支模組最難的不是「有沒有人回」，是**「這封信到底是不是本人寄的」**。
owner@company.example 同時是小紅自己的發信身分 —— ``gmail_ops.send_email`` 走
``service.users().messages().send(userId="me")``，而 ``get_service`` 認證的帳號
就是 owner@company.example。所以這個信箱的「寄件備份」裡混了大量小紅自己發的排程
報表與提醒信（【排程: kitting_alert_daily】、【小紅新信】、【小紅 Ponder】…），
數量比本人手寫的信還多。大王要的是「**我個人**寄出的郵件」，機器發的一律不算。

判別方式（2026-08 對 owner@ 近 90 天 482 封寄件備份實測歸納，見 _is_machine_sent）：

  1. **MIME boundary 形狀**——小紅走 Gmail API 寄的信由 Python ``email.mime``
     組裝，boundary 一律是 ``===============<數字>==`` 這種形狀；本人用 Apple
     Mail（iPad / iPhone / Mac）寄的沒有任何一封長這樣。這是最硬的訊號。
  2. **只寄給自己**——小紅的提醒信收件人只有信箱主人本人（【小紅新信】那類是
     單段 MIME、boundary 測不到，得靠這條擋）。人不會寄信催自己回信。
  3. **X-Mailer 存在**——Apple Mail / iPhone Mail / iPad Mail 會留這個標頭，
     小紅不會。這是「是本人」的正向確認，用來在前兩條都沒判定時放行。

三條規則的實測結果：90 天內寄給第三人的 256 封信，253 封有 X-Mailer（本人）、
3 封單段純文字（也是本人手寫的短回覆）、**0 封是小紅的 Python MIME**。

另外會濾掉「本人自己就是句點」的 thread —— 最後一封是本人回的「收到」「謝謝」
這種確認語，對方本來就不必再回，列出來只是噪音（見 _is_ack_only）。
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from agent_core.env_utils import env_bool, env_int
from agent_core.prompt_injection import sanitize_for_llm

# 一輪的軟性時間預算。Gmail 的 threads().get 一顆約 0.3–0.5s，回溯 14 天大約
# 30–40 顆；抓 180s 讓慢的一天也跑得完，又不會拖垮 dispatcher 的 5 分鐘節奏。
_DEADLINE_S = 180.0

# Python email.mime 產生的 multipart boundary 前綴（= 小紅經 Gmail API 寄出）。
_PY_MIME_BOUNDARY = "==============="

_ADDR_RE = re.compile(r"[\w\.\-\+]+@[\w\.\-]+")

# 簽名檔起點——snippet 會把簽名一起帶進來，判斷「這封是不是只有一句確認」前要先切掉。
_SIGNATURE_MARKERS = (
    "Best regards", "Best Regards", "BEST REGARDS", "Best rgds", "B.R.", "BR,",
    "Regards", "Sincerely", "Thanks & Regards", "敬祝", "順頌", "此致",
    "Owner Name", "JAIFUNG", "Jaifung", "Sent from my", "從我的",
)

# 「本人已經把話講完」的確認語 —— 對方不需要再回，不該被列成待回覆。
# 長的排前面：alternation 是「先匹配到的贏」，"thanks" 排在 "thank you" 前面會
# 先吃掉 "thank"、剩下的 "you" 對不上任何 token，整串就判不出是確認語。
_ACK_TOKEN = (
    r"thank you|thanks|thank|noted|received|got it|copy that|okay|ok|thx|"
    r"同意辦理|已收到|收到|知道了|沒問題|了解|瞭解|謝謝|感謝|感恩|辛苦了|辛苦|"
    r"好的|好|是的|是|對|同意|可以|准"
)
# 整串只由確認語 + 標點組成才算 —— 「收到，但數量不對…」後面還有實質內容，
# 對不上 $ 就不會被判成確認語（測試釘住）。
_ACK_RE = re.compile(
    rf"^(?:(?:{_ACK_TOKEN})[\s\.。!！,，~、\-]*)+$", re.IGNORECASE
)

# 轉寄郵件的分隔標記（Apple Mail 中英、Gmail、Outlook）。標記之前的那段就是
# 本人自己打的字 —— 那段是空的就代表「純轉手歸檔」。
_FORWARD_MARKERS = (
    "開始轉寄郵件", "開始轉寄的郵件", "轉寄的郵件",
    "Begin forwarded message", "Forwarded message",
    "原始郵件", "-----Original Message-----",
)

# 本人在轉寄時自己打的字要超過這個長度，才算「他有話要說、在等對方回」。
# 實測 owner@ 轉給 twaccounting@ 的銀行/電信/發票通知都是零字純轉手（14 封裡
# 佔 9 封），那種 FYI 歸檔本來就不需要回覆，列出來只會讓提醒失去意義。
_RELAY_COMMENT_MIN_CHARS = 8


def _addrs(value: str) -> list[str]:
    return _ADDR_RE.findall((value or "").lower())


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in (message.get("payload") or {}).get("headers") or []
    }


def _is_machine_sent(headers: dict[str, str], me: str) -> bool:
    """這封寄件備份是不是小紅（而非本人）發出去的。

    見 module docstring 的三條規則。判不出來時回 False（= 當成本人寄的），
    寧可多提醒一封、也不要把大王真正在等回覆的信吃掉。
    """
    content_type = headers.get("content-type", "")
    match = re.search(r'boundary="?([^";]+)', content_type)
    if match and match.group(1).startswith(_PY_MIME_BOUNDARY):
        return True  # 規則 1：Python email.mime 組的 → 小紅經 Gmail API 寄的。

    if headers.get("x-mailer"):
        return False  # 規則 3：Apple Mail 系列留的標頭，小紅不會有 → 本人。

    recipients = set(_addrs(headers.get("to", "")) + _addrs(headers.get("cc", "")))
    if recipients and recipients <= {me.lower()}:
        return True  # 規則 2：只寄給自己 → 小紅的自寄提醒（【小紅新信】那類）。

    return False


def _strip_signature(text: str) -> str:
    """砍掉簽名檔。

    ⚠️ 用 ``idx >= 0``（而非 > 0）—— Apple Mail 轉寄時會把簽名檔放在**最前面**、
    轉寄內容接在後面，所以簽名出現在 index 0 是常態，不是例外。
    """
    for marker in _SIGNATURE_MARKERS:
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx]
    return text


def _body_gist(snippet: str) -> str:
    """把 snippet 砍到「本人自己打的那段字」——切掉簽名檔與引文/轉寄內容。"""
    text = _strip_signature((snippet or "").strip())
    # Apple Mail 的引文開頭（「於 2026年8月6日 ... 寫道：」/「On ... wrote:」）。
    text = re.split(r"(?:^|\s)(?:On\s.+?wrote:|於\s.+?寫道)", text)[0]
    for marker in _FORWARD_MARKERS:
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx]
    return text.strip(" \t\r\n>-—–&gt;")


def _is_ack_only(snippet: str) -> bool:
    """最後一封是本人回的確認語（收到/謝謝/OK…）→ 對方不必再回，不算待回覆。"""
    gist = _body_gist(snippet)
    if not gist or len(gist) > 30:
        return False
    return bool(_ACK_RE.match(gist))


def _is_pure_relay(snippet: str) -> bool:
    """純轉手歸檔：本人一個字都沒寫，只把別人的信轉出去 → 不是在等回覆。

    判準是「轉寄標記之前，本人自己打的字有多少」，不看主旨 —— 實測有主旨為空
    但寫了實質問題的轉寄（「Dear user-c 你是不是用公司 email 去註冊 FB…」），
    只認 ``Fwd:`` 前綴會把那種真的在等回覆的信一起吃掉。
    """
    text = (snippet or "").strip()
    if not any(marker in text for marker in _FORWARD_MARKERS):
        return False
    return len(_body_gist(text)) < _RELAY_COMMENT_MIN_CHARS


def _preview(text: str, width: int = 42) -> str:
    """把內文開頭壓成一行標題用的預覽。"""
    flat = re.sub(r"\s+", " ", (text or "")).strip()
    return flat[:width] + "…" if len(flat) > width else flat


def _display_recipients(headers: dict[str, str], me: str, limit: int = 3) -> str:
    """收件人的精簡顯示：同網域只留 local part，外部留完整位址。"""
    seen: list[str] = []
    for addr in _addrs(headers.get("to", "")) + _addrs(headers.get("cc", "")):
        if addr == me.lower() or addr in seen:
            continue
        seen.append(addr)
    if not seen:
        return "（無收件人）"
    own_domain = me.rsplit("@", 1)[-1].lower()
    shown = [
        a.split("@", 1)[0] if a.endswith("@" + own_domain) else a
        for a in seen[:limit]
    ]
    extra = len(seen) - len(shown)
    return "、".join(shown) + (f" 等 {len(seen)} 人" if extra > 0 else "")


def _service(mailbox: str):
    from agent_core.google_auth import get_service_for_account

    sa_file = os.environ.get("RED_SENT_REPLY_SA_FILE", "").strip()
    if not sa_file:
        from agent_core.ingest.sync_config import load_targets

        for acct in load_targets().get("gmail_accounts") or []:
            if isinstance(acct, dict) and acct.get("service_account_file"):
                sa_file = str(acct["service_account_file"])
                break
    if not sa_file:
        raise RuntimeError(
            "找不到 service account 金鑰（rag_sync_targets.json 的 gmail_accounts "
            "是空的，也沒設 RED_SENT_REPLY_SA_FILE）。"
        )
    return get_service_for_account(
        f"sent_reply:{mailbox}", "gmail", "v1",
        service_account_file=sa_file, subject=mailbox, scopes=None,
    )


def _collect_thread_ids(users, lookback_days: int) -> list[str]:
    """近 N 天寄件備份涉及的 thread id（去重、保序）。

    messages().list 直接回 threadId，所以這一步不用逐封 get —— 判「機器/本人」
    留到 thread 層一次做完，省掉一半 API 呼叫。
    """
    ordered: dict[str, None] = {}
    token = None
    while True:
        resp = users.messages().list(
            userId="me", q=f"in:sent newer_than:{lookback_days}d",
            maxResults=500, pageToken=token,
        ).execute()
        for msg in resp.get("messages") or []:
            ordered.setdefault(msg.get("threadId") or msg["id"], None)
        token = resp.get("nextPageToken")
        if not token:
            return list(ordered)


def _evaluate_thread(users, thread_id: str, me: str, now: datetime) -> dict[str, Any] | None:
    """回這個 thread 的「未回覆」判定，或 None（= 不必提醒）。"""
    thread = users.threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Subject", "X-Mailer", "Content-Type"],
    ).execute()
    messages = thread.get("messages") or []
    if not messages:
        return None

    last = messages[-1]
    headers = _headers(last)
    senders = _addrs(headers.get("from", ""))
    if not senders or senders[0] != me.lower():
        return None  # 最後一封不是我寄的 → 已經有人回了。
    if _is_machine_sent(headers, me):
        return None  # 小紅自己發的報表/提醒，不是「我個人寄出的郵件」。
    snippet = last.get("snippet", "")
    if _is_ack_only(snippet):
        return None  # 我自己回的「收到/謝謝」，對方本來就不必再回。
    if not env_bool("RED_SENT_REPLY_INCLUDE_RELAYS", False) and _is_pure_relay(snippet):
        return None  # 零字純轉手（銀行通知丟給會計那種），不是在等回覆。

    sent_at = datetime.fromtimestamp(
        int(last.get("internalDate") or 0) / 1000, timezone.utc
    )
    return {
        "thread_id": thread_id,
        "days": (now - sent_at).days,
        "sent_at": sent_at,
        # 主旨空白的信（大王在 iPad 上直接轉寄、沒打主旨）光印「（無主旨）」等於
        # 沒講 —— 退回用本人自己打的那段字當標題，才看得出是哪件事。
        "subject": (
            headers.get("subject", "").strip()
            or _preview(_body_gist(snippet))
            or "（無主旨）"
        ),
        "recipients": _display_recipients(headers, me),
        "external": any(
            not a.endswith("@" + me.rsplit("@", 1)[-1].lower())
            for a in _addrs(headers.get("to", "")) + _addrs(headers.get("cc", ""))
        ),
        "rounds": len(messages),
    }


def list_unanswered_sent_threads(
    days_overdue: int = 3,
    lookback_days: int = 14,
    mailbox: str = "",
) -> str:
    """查「**我本人**寄出去、對方到現在還沒回覆」的信，回一份可直接發出去的提醒。

    只算本人手寫的信 —— 小紅自己用同一個信箱發出去的排程報表、提醒信會被濾掉
    （判別規則見模組 docstring）；本人回的「收到/謝謝」這類確認語也不算（對方
    不需要再回）。純讀 Gmail，不改任何信件狀態。免 +確認。

    Args:
        days_overdue: 寄出後幾天沒回就算逾期（預設 3 天）。
        lookback_days: 只回溯最近幾天寄出的信（預設 14 天，避免翻出早就沒有
            跟催意義的舊信）。
        mailbox: 要查的信箱，預設 RED_SENT_REPLY_MAILBOX 或 owner@company.example。

    Returns:
        排版好的純文字提醒（Telegram / email 都可直接送）。沒有逾期的就回一句
        ✅ 開頭的「全部都有回」。
    """
    me = (
        mailbox.strip()
        or os.environ.get("RED_SENT_REPLY_MAILBOX", "").strip()
        or "owner@company.example"
    )
    days_overdue = max(1, min(int(days_overdue), 90))
    lookback_days = max(days_overdue, min(int(lookback_days), 180))
    max_items = env_int("RED_SENT_REPLY_MAX_ITEMS", 25, min_value=1, max_value=100)

    try:
        users = _service(me).users()
        thread_ids = _collect_thread_ids(users, lookback_days)
    except Exception as exc:
        return f"❌ 查不到 {me} 的寄件備份：{exc}"

    now = datetime.now(timezone.utc)
    findings: list[dict[str, Any]] = []
    truncated = False
    deadline = time.monotonic() + _DEADLINE_S
    errors = 0
    examined = 0
    for thread_id in thread_ids:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            hit = _evaluate_thread(users, thread_id, me, now)
        except Exception:
            errors += 1
            continue
        examined += 1
        if hit and hit["days"] >= days_overdue:
            findings.append(hit)

    notes = []
    if truncated:
        notes.append(f"⚠️ 本輪掃到時間上限，只檢查了部分 thread（共 {len(thread_ids)} 個）。")
    if errors:
        notes.append(f"⚠️ {errors} 個 thread 讀取失敗，已略過。")

    if not findings:
        if notes:
            # 🚨 綠勾勾是「我查過了，確實沒有」的意思，不能拿來蓋「根本沒查完」。
            # 舊版把 truncated/errors 只掛在「有發現」那條路上，於是整批 thread
            # 都讀失敗時反而回一個乾淨的 ✅ —— 一次網路故障就變成一句安心保證
            # （同 #448 shipping「掃不到 ≠ 逾期」、email_pending_tracker 同款）。
            return "\n".join([
                f"🔍 過去 {lookback_days} 天你本人寄出的信，本輪**沒有查完**"
                f"（{len(thread_ids)} 個 thread 裡實際檢查了 {examined} 個）；"
                f"已檢查的部分沒有超過 {days_overdue} 天未回覆的。",
                *notes,
            ])
        return (
            f"✅ 過去 {lookback_days} 天你本人寄出的信，沒有超過 {days_overdue} "
            "天還沒收到回覆的。"
        )

    findings.sort(key=lambda f: (-f["days"], f["subject"]))
    shown = findings[:max_items]

    lines = [
        f"📮 你寄出後還沒收到回覆（逾期 ≥{days_overdue} 天，回溯 {lookback_days} 天）",
        f"共 {len(findings)} 封" + (f"，以下列出最久的 {len(shown)} 封" if len(shown) < len(findings) else ""),
        "",
    ]
    for item in shown:
        tag = "🌐" if item["external"] else "🏠"
        # 主旨/收件人是信件內容（可能被外部寄件人影響）— 進 LLM 或推播前一律先淨化。
        lines.append(
            f"⏳ {item['days']} 天　{tag} {sanitize_for_llm(item['recipients'])}"
        )
        lines.append(f"　　{sanitize_for_llm(item['subject'])}")
        lines.append(f"　　https://mail.google.com/mail/u/0/#all/{item['thread_id']}")

    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


def sent_reply_reminder() -> str:
    """每日提醒用的無參數版本 —— 讀環境變數決定門檻，其餘同
    :func:`list_unanswered_sent_threads`。

    給排程走 ``deterministic_tool`` 用（完全不經 LLM，主旨/天數不會被轉述錯）。

    Returns:
        排版好的純文字提醒。
    """
    return list_unanswered_sent_threads(
        days_overdue=env_int("RED_SENT_REPLY_OVERDUE_DAYS", 3, min_value=1, max_value=90),
        lookback_days=env_int("RED_SENT_REPLY_LOOKBACK_DAYS", 14, min_value=1, max_value=180),
    )


# 純讀 → 背景 dispatcher 的排程任務可用（safe_tools 的第二條路徑）。
list_unanswered_sent_threads.background_safe = True
sent_reply_reminder.background_safe = True
