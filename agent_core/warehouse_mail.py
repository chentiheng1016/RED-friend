"""越南倉庫信箱的每日郵件摘要與待辦追蹤（UserY 清單 倉庫⑧⑨）。

兩個信箱：``warehouse@company.example``（越南倉庫）與 ``warehouse-mgr@company.example``（倉庫
主管井戶良枝）。認證沿用 email_pending_tracker 同一套——``rag_sync_targets.json``
的 ``gmail_accounts`` 網域委派，不另建連線邏輯；委派若被撤掉，該信箱只記錯誤、
不影響另一個。

兩件事分開：
  ⑧ 郵件重點摘要 —— ``warehouse_mail_digest``：近 N 天有動靜的 thread，逐條給
     寄件人/主旨/摘要，並標「我方最後有沒有回」。本工具**只出素材、不寫回覆**；
     建議回覆由排程 prompt 交給 Gemini 產（草稿層級、不自動寄出）。
  ⑨ 待辦追蹤 —— ``warehouse_todo_board``：把「對方寄來、我方還沒回」的 thread
     落成持久狀態（``var/state/warehouse_todos.json``），早上開單、下午複查。
     判定「已處理完畢」的依據就是 UserY 說的「依照回覆的內容」——我方（同網域）
     在對方最後一封之後有回信 → 結案。這是可驗證的事實訊號，不讓 LLM 猜。

⚠️ 郵件是外部可控內容：所有寄件人/主旨/摘要在回傳前一律過 ``sanitize_for_llm``，
   整份摘要再包 ``wrap_as_untrusted``（CLAUDE.md 鐵則：每條讀取路徑各自套，沒有
   單一咽喉點）。

⚠️ 本模組**唯讀**：不寄信、不改標籤、不動草稿。要寄的是排程的 notify_emails 那條
   路（dispatcher 自寄），與這裡無關。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger
from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted

# 倉庫這條線的兩個信箱（順序 = 報告裡的順序）。
WAREHOUSE_MAILBOXES = ("warehouse@company.example", "warehouse-mgr@company.example")

_TODO_PATH = os.path.join(STATE_DIR, "warehouse_todos.json")
_MAILBOX_DEADLINE_S = 45.0   # 每個信箱的軟時間預算，一個卡住不拖垮另一個。
_SNIPPET_MAX = 220
_CLOSED_TTL_D = 30           # 已結案紀錄保留天數（狀態檔不無限長大）。
_MAX_LIST = 15               # 單一區塊最多列幾筆（超出只報數量，不洗版）。


# ---------- Gmail 讀取 ----------
def _sa_for(mailbox: str) -> str:
    """從 rag_sync_targets.json 取該信箱的 service account 路徑（沒設定回空字串）。"""
    try:
        from agent_core.ingest.sync_config import load_targets
        for acct in (load_targets().get("gmail_accounts") or []):
            if isinstance(acct, dict) and str(acct.get("mailbox") or "").strip() == mailbox:
                return str(acct.get("service_account_file") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("warehouse_mail：讀 gmail_accounts 失敗（%s）", exc)
    return ""


def _last_message(users, thread_id: str) -> dict[str, Any] | None:
    """thread 最後一封的 From / Subject / Date / snippet / internalDate。"""
    try:
        thread = users.threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except Exception:
        return None
    messages = thread.get("messages") or []
    if not messages:
        return None
    last = messages[-1]
    headers = {h["name"]: h["value"] for h in last.get("payload", {}).get("headers", [])}
    return {
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", "（無主旨）"),
        "date": headers.get("Date", ""),
        "snippet": last.get("snippet", ""),
        "internal_ms": last.get("internalDate", ""),
        "msg_count": len(messages),
    }


def _addr(from_header: str) -> str:
    """From header → 純 email address（小寫）。"""
    s = (from_header or "").strip().lower()
    if "<" in s and ">" in s:
        s = s[s.rfind("<") + 1: s.rfind(">")]
    return s.strip()


def _is_warehouse_side(from_header: str) -> bool:
    """最後一封是不是「倉庫這條線」自己寄的（= 我方已回話）。

    ⚠️ 判準刻意是「這兩個信箱本身」而非 email_pending_tracker 用的「同網域」。
    同網域會把 UserA(採購)、業務寄給 UserY 的內部交辦也算成「我方已回」，
    UserY 的待辦有一大半正是這種內部來信——用網域判會整批漏掉（2026-08-04
    實測：warehouse-mgr@ 25 封裡 vnpurchase2@ 那封被誤判成已回）。
    """
    return _addr(from_header) in {m.lower() for m in WAREHOUSE_MAILBOXES}


# 自動通知寄件人：資訊有用（進貨/出貨通知）但不需要回覆，不該佔待辦板。
_NOREPLY_HINTS = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
                  "mailer-daemon", "postmaster", "notification", "notifications",
                  "automated", "auto-confirm", "bounce")

# local part 看不出是機器人、但確實是自動信的寄件人（實測補進來的）。
_NOREPLY_ADDRESSES = frozenset({
    "gemini-notes@google.com",       # 2026-08-04：會議記錄自動信被開成待辦
})


def _is_noreply(from_header: str) -> bool:
    addr = _addr(from_header)
    if addr in _NOREPLY_ADDRESSES:
        return True
    local = addr.split("@", 1)[0]
    return any(h in local for h in _NOREPLY_HINTS)


def _scan_mailbox(mailbox: str, days: int, limit: int, errors: list[str],
                  *, unread_only: bool = False) -> list[dict[str, Any]]:
    """回該信箱近 days 天有動靜的 thread（已含 我方是否已回 判定）。

    unread_only=True 時只取未讀（UserY 清單 ⑧ 講的是「未讀郵件」）；待辦追蹤
    ⑨ 反而要掃全部收件匣——已讀但沒回的一樣是待辦，用未讀當判準會漏。
    """
    sa_file = _sa_for(mailbox)
    if not sa_file:
        errors.append(f"{mailbox}：rag_sync_targets.json 沒有這個信箱的委派設定")
        return []

    from agent_core.google_auth import get_service_for_account

    found: list[dict[str, Any]] = []
    deadline = time.monotonic() + _MAILBOX_DEADLINE_S
    try:
        service = get_service_for_account(
            f"warehouse_mail:{mailbox}", "gmail", "v1",
            service_account_file=sa_file, subject=mailbox, scopes=None,
        )
        users = service.users()
        query = f"in:inbox newer_than:{days}d" + (" is:unread" if unread_only else "")
        listing = users.threads().list(
            userId="me", q=query, maxResults=limit,
        ).execute()
        for t in (listing.get("threads") or []):
            if time.monotonic() > deadline:
                errors.append(f"{mailbox}：逾時，本輪只掃了部分 thread")
                break
            info = _last_message(users, t["id"])
            if info is None:
                continue
            info["mailbox"] = mailbox
            info["thread_id"] = t["id"]
            info["answered"] = _is_warehouse_side(info["from"])
            info["noreply"] = _is_noreply(info["from"])
            found.append(info)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{mailbox}：{type(exc).__name__}: {str(exc)[:160]}")
    return found


def _clean(text: str, limit: int = 0) -> str:
    """外部可控字串 → 淨化 + 壓成單行 +（選擇性）截斷。"""
    s = sanitize_for_llm(str(text or "")).replace("\n", " ").replace("\r", " ").strip()
    s = " ".join(s.split())
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


# ---------- ⑧ 郵件重點摘要 ----------
def warehouse_mail_digest(days: int = 2, limit_per_mailbox: int = 25,
                          unread_only: bool = True) -> str:
    """倉庫兩個信箱（warehouse@ / warehouse-mgr@）近 N 天的未讀郵件重點清單。唯讀，免 +確認。

    每條給：寄件人、主旨、最後一封摘要、我方是否已回。**本工具不產回覆內容**，
    建議回覆交給呼叫端（排程 prompt）根據這份素材寫草稿。

    Args:
        days: 看最近幾天有動靜的 thread（預設 2）。
        limit_per_mailbox: 每個信箱最多檢查幾個 thread（預設 25）。
        unread_only: 只看未讀（預設 True，對應「每日未讀郵件重點摘要」）。
    Returns:
        依信箱分組的清單（外部內容已 sanitize 並包 untrusted 標記）；
        兩個信箱都沒動靜時回「(無新發現)」。
    """
    days = max(1, min(int(days), 30))
    limit_per_mailbox = max(1, min(int(limit_per_mailbox), 60))

    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    for mailbox in WAREHOUSE_MAILBOXES:
        rows.extend(_scan_mailbox(mailbox, days, limit_per_mailbox, errors,
                                  unread_only=bool(unread_only)))

    if not rows:
        if errors:
            return "⚠️ 倉庫信箱查詢失敗，本輪拿不到郵件摘要：\n" + "\n".join(
                f"  - {e}" for e in errors
            )
        return "(無新發現)"

    scope = "未讀" if unread_only else "有動靜"
    lines = [f"近 {days} 天倉庫信箱共 {len(rows)} 個{scope} thread："]
    for mailbox in WAREHOUSE_MAILBOXES:
        mine = [r for r in rows if r["mailbox"] == mailbox]
        if not mine:
            continue
        # 自動通知（noreply）分開列：內容有用（到貨/出貨通知），但不需要人回覆，
        # 混在待回清單裡會把真正要處理的信淹掉。
        human = [r for r in mine if not r["noreply"]]
        autos = [r for r in mine if r["noreply"]]
        waiting = sum(1 for r in human if not r["answered"])
        lines.append(
            f"\n【{mailbox}】{len(mine)} 封（需人處理 {len(human)}、"
            f"自動通知 {len(autos)}），其中 {waiting} 封倉庫尚未回覆"
        )
        # 待回的排前面：一次撈到大量未讀時，先被截掉的應該是已回的那些。
        for r in sorted(human, key=lambda x: x["answered"])[:_MAX_LIST]:
            flag = "✅倉庫已回" if r["answered"] else "⏳待回"
            lines.append(
                f"  - {flag}｜{_clean(r['from'], 60)}｜{_clean(r['subject'], 90)}\n"
                f"      摘要：{_clean(r['snippet'], _SNIPPET_MAX)}\n"
                f"      thread={r['thread_id']}（共 {r['msg_count']} 封）"
            )
        if len(human) > _MAX_LIST:
            lines.append(f"  …另有 {len(human) - _MAX_LIST} 封未列")
        if autos:
            lines.append(f"  · 自動通知 {len(autos)} 封（免回覆，僅供掌握）：")
            for r in autos[:_MAX_LIST]:
                lines.append(
                    f"      · {_clean(r['from'], 45)}｜{_clean(r['subject'], 80)}"
                )
            if len(autos) > _MAX_LIST:
                lines.append(f"      · …另有 {len(autos) - _MAX_LIST} 封")

    out = wrap_as_untrusted("\n".join(lines), label="warehouse-mailbox")
    if errors:
        out += "\n\n⚠️ 以下信箱查詢有問題（不影響上面已列出的）：\n" + "\n".join(
            f"  - {e}" for e in errors
        )
    return out


# ---------- ⑨ 待辦追蹤（持久狀態） ----------
def _load_todos() -> dict:
    if not os.path.exists(_TODO_PATH):
        return {"version": 1, "threads": {}, "updated_at": ""}
    try:
        with open(_TODO_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("格式不是 dict")
        data.setdefault("threads", {})
        data.setdefault("version", 1)
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("warehouse_todos 讀取失敗（%s），視為空", exc)
        return {"version": 1, "threads": {}, "updated_at": ""}


def _save_todos(data: dict) -> bool:
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(os.path.dirname(_TODO_PATH), exist_ok=True)
        _atomic_write_text(_TODO_PATH, json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("warehouse_todos 寫入失敗（%s）", exc)
        return False


def _age_days(iso_ts: str) -> int:
    try:
        return max(0, (datetime.now() - datetime.fromisoformat(iso_ts)).days)
    except Exception:  # noqa: BLE001
        return 0


def warehouse_todo_board(days: int = 7, limit_per_mailbox: int = 30,
                         persist: bool = True) -> str:
    """倉庫待辦追蹤：對方寄來、我方還沒回的 thread —— 開單、追天數、自動結案。唯讀 Gmail，免 +確認。

    「已處理完畢」的判定用可驗證的事實：我方（company.example 網域）在對方最後一封
    之後有回信 → 結案。不讓 LLM 猜「看起來處理好了沒」。

    狀態存 ``var/state/warehouse_todos.json``，所以早上開的單下午複查時認得出
    是「舊的還沒處理」還是「新進來的」；已結案的 thread 之後再有新信會重新開單。

    Args:
        days: 掃最近幾天的 thread（預設 7）。
        limit_per_mailbox: 每信箱最多檢查幾個 thread（預設 30）。
        persist: 是否寫回狀態檔（預設 True；預覽/測試可傳 False）。
    Returns:
        三段報告：本輪新增待辦 / 仍未處理（含已等待天數）/ 本輪已結案；
        全部都空時回「(無新發現)」。
    """
    days = max(1, min(int(days), 60))
    limit_per_mailbox = max(1, min(int(limit_per_mailbox), 60))

    errors: list[str] = []
    scanned: list[dict[str, Any]] = []
    for mailbox in WAREHOUSE_MAILBOXES:
        scanned.extend(_scan_mailbox(mailbox, days, limit_per_mailbox, errors))

    data = _load_todos()
    threads: dict[str, Any] = data["threads"]
    now_iso = datetime.now().isoformat(timespec="seconds")

    new_items, closed_items = [], []
    seen_ids = set()

    for r in scanned:
        tid = r["thread_id"]
        if r["noreply"]:
            # 自動通知不進待辦板（Decathlon 出貨通知這類，實測一天就能灌 18 筆
            # 假待辦把真正要回的信淹掉）。它們在 warehouse_mail_digest 另列。
            continue
        seen_ids.add(tid)
        rec = threads.get(tid)
        if r["answered"]:
            # 我方已回 → 這條沒有待辦。只有「本來開著」的才算本輪結案。
            if rec and rec.get("status") == "open":
                rec["status"] = "closed"
                rec["closed_at"] = now_iso
                rec["last_seen"] = now_iso
                closed_items.append(rec)
            continue
        # 對方最後說話 = 待辦。已結案的 thread 又有新來信 → 重新開單。
        if rec is None or rec.get("status") == "closed":
            rec = {
                "mailbox": r["mailbox"],
                "subject": _clean(r["subject"], 120),
                "from": _clean(r["from"], 80),
                "first_seen": now_iso,
                "status": "open",
                "reopened": bool(rec),
            }
            threads[tid] = rec
            new_items.append((tid, rec))
        else:
            rec["subject"] = _clean(r["subject"], 120)
            rec["from"] = _clean(r["from"], 80)
        rec["last_seen"] = now_iso
        rec["snippet"] = _clean(r["snippet"], _SNIPPET_MAX)

    new_ids = {tid for tid, _ in new_items}
    still_open = [
        (tid, rec) for tid, rec in threads.items()
        if rec.get("status") == "open" and tid in seen_ids and tid not in new_ids
    ]
    # 掃描窗外（超過 days 天沒動靜）的開單不刪除也不列——資料還在，只是不洗版。
    # 已結案超過 _CLOSED_TTL_D 天的清掉，狀態檔才不會無限長大。
    for tid in [t for t, r in threads.items()
                if r.get("status") == "closed"
                and _age_days(r.get("closed_at", "")) > _CLOSED_TTL_D]:
        threads.pop(tid, None)

    if persist:
        _save_todos(data)

    if not (new_items or still_open or closed_items):
        # 全部信箱都掛掉時只回錯誤，不要回一個空的 untrusted 區塊誤導成「都處理完了」。
        if errors:
            return "⚠️ 倉庫信箱查詢失敗，本輪無法判斷待辦：\n" + "\n".join(
                f"  - {e}" for e in errors
            )
        return "(無新發現)"

    lines = []
    if new_items:
        lines.append(f"🆕 本輪新增待辦 {len(new_items)} 筆（對方寄來、我方尚未回）：")
        for tid, rec in new_items[:_MAX_LIST]:
            tag = "（舊案再啟）" if rec.get("reopened") else ""
            lines.append(
                f"  - [{rec['mailbox']}]{tag} {rec['from']}｜{rec['subject']}\n"
                f"      摘要：{rec.get('snippet', '')}\n      thread={tid}"
            )
        if len(new_items) > _MAX_LIST:
            lines.append(f"  …另有 {len(new_items) - _MAX_LIST} 筆未列（全部已開單追蹤）")
    if still_open:
        lines.append(f"\n⏳ 仍未處理 {len(still_open)} 筆（等最久的排前面）：")
        # 等最久的優先——上線首輪會一次撈出整批積壓，全列會把新進的待辦淹掉。
        ordered = sorted(still_open, key=lambda kv: kv[1].get("first_seen", ""))
        for tid, rec in ordered[:_MAX_LIST]:
            lines.append(
                f"  - [{rec['mailbox']}] 已等 {_age_days(rec.get('first_seen', ''))} 天"
                f"｜{rec['from']}｜{rec['subject']}（thread={tid}）"
            )
        if len(ordered) > _MAX_LIST:
            lines.append(f"  …另有 {len(ordered) - _MAX_LIST} 筆未列（全部仍在追蹤）")
    if closed_items:
        lines.append(f"\n✅ 本輪已結案 {len(closed_items)} 筆（我方已回覆）：")
        for rec in closed_items:
            lines.append(f"  - [{rec['mailbox']}] {rec['subject']}")

    out = wrap_as_untrusted("\n".join(lines), label="warehouse-todo")
    if errors:
        out += "\n\n⚠️ 以下信箱查詢有問題（不影響上面已列出的）：\n" + "\n".join(
            f"  - {e}" for e in errors
        )
    return out


warehouse_mail_digest.background_safe = True   # 純讀 Gmail，背景排程可用。
warehouse_todo_board.background_safe = True    # 讀 Gmail + 寫自己的狀態檔，無外部副作用。
