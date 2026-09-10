"""橫跨全公司信箱找「對方寄信給我方、我方尚未回覆」的 thread。

資料來源跟 RAG 夜跑的 Gmail 部分同一套（``var/data/rag_sync_targets.json`` 的
``gmail_accounts`` 清單 + ``agent_core.google_auth.get_service_for_account``
網域委派連線），不重造認證邏輯——只是這裡是即時查詢（給白天每 3 小時跑一次的
email_pending_tracker 排程任務用），RAG 那份是每天一次的批次索引，兩者用途
不同、不能互相取代（RAG 索引是前一晚的快照，判斷「現在誰還沒回」要看即時
Gmail 狀態）。
"""
from __future__ import annotations

import time
from typing import Any

from agent_core.prompt_injection import sanitize_for_llm

_DEFAULT_TIMEOUT_S = 30.0  # 每個信箱的軟性時間預算，一個卡住的信箱不拖垮全部。


def _is_external_sender(from_header: str, own_domain: str) -> bool:
    """From header 的網域是不是跟信箱自己的網域不同（= 外部寄來）。"""
    addr = (from_header or "").lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[-1].strip(">").strip()
    return domain != own_domain.lower()


def _thread_last_message_from(service, thread_id: str) -> tuple[str, str, str] | None:
    """回 (from_header, subject, last_message_internal_date_ms)；thread 沒有訊息時回 None。

    ⚠️ 讀取失敗**不吞**，往上拋給 _scan_one_mailbox 計入本輪失敗數。以前這裡把
    「API 讀不到」和「thread 是空的」都回 None，呼叫端一律 continue —— 一個真的
    未回信 thread 就這樣從清單上消失，而且沒有任何計數，看的人以為查過了。
    """
    thread = service.threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Subject"],
    ).execute()
    messages = thread.get("messages") or []
    if not messages:
        return None
    last = messages[-1]
    headers = {h["name"]: h["value"] for h in last.get("payload", {}).get("headers", [])}
    return (
        headers.get("From", ""),
        headers.get("Subject", "（無主旨）"),
        last.get("internalDate", ""),
    )


def _scan_one_mailbox(
    mailbox: str, sa_file: str, days: int, limit: int, errors: list[str],
) -> list[dict[str, Any]]:
    from agent_core.google_auth import get_service_for_account

    own_domain = mailbox.rsplit("@", 1)[-1] if "@" in mailbox else ""
    findings: list[dict[str, Any]] = []
    deadline = time.monotonic() + _DEFAULT_TIMEOUT_S
    last_thread_error = ""
    try:
        service = get_service_for_account(
            f"email_pending:{mailbox}", "gmail", "v1",
            service_account_file=sa_file, subject=mailbox, scopes=None,
        )
        result = service.users().threads().list(
            userId="me", q=f"newer_than:{days}d", maxResults=limit,
        ).execute()
        threads = result.get("threads") or []
        examined = 0
        thread_failures = 0
        for t in threads:
            if time.monotonic() > deadline:
                errors.append(
                    f"{mailbox}: 逾時，本輪只掃了部分 thread"
                    f"（{len(threads)} 個裡看了 {examined} 個）"
                )
                break
            try:
                info = _thread_last_message_from(service.users(), t["id"])
            except Exception as exc:  # noqa: BLE001 — 單 thread 讀不到不影響其他 thread
                thread_failures += 1
                last_thread_error = f"{type(exc).__name__}: {str(exc)[:80]}"
                continue
            examined += 1
            if info is None:
                continue
            from_header, subject, _ = info
            if _is_external_sender(from_header, own_domain):
                findings.append({
                    "mailbox": mailbox,
                    "from": from_header,
                    "subject": subject,
                    "thread_id": t["id"],
                })
        if thread_failures:
            errors.append(
                f"{mailbox}: {thread_failures} 個 thread 讀取失敗、未納入判斷"
                f"（{last_thread_error}）"
            )
    except Exception as exc:
        errors.append(f"{mailbox}: {exc}")
    return findings


def list_unanswered_company_threads(days: int = 7, limit_per_mailbox: int = 20) -> str:
    """查橫跨全公司信箱（業務/採購/樣品室/會計等，跟 RAG 夜跑同一份設定清單）
    裡「對方最後寄信給我方、我方尚未回覆」的 thread。

    days: 只看最近 N 天內有活動的 thread（預設 7 天）。
    limit_per_mailbox: 每個信箱最多檢查幾個 thread（預設 20，避免單次呼叫過久）。

    判準：thread 最後一封信的寄件網域跟信箱自己的網域不同（= 外部寄來），且
    之後沒有我方（同網域）回信 —— 就算未回。回傳依信箱分組的清單；查詢失敗
    的信箱記在最後的錯誤區塊、不影響其他信箱。"""
    from agent_core.ingest.sync_config import load_targets

    targets = load_targets()
    accounts = targets.get("gmail_accounts") or []
    if not isinstance(accounts, list) or not accounts:
        return "尚未設定任何公司信箱（rag_sync_targets.json 的 gmail_accounts 是空的）。"

    days = max(1, min(int(days), 90))
    limit_per_mailbox = max(1, min(int(limit_per_mailbox), 100))

    all_findings: list[dict[str, Any]] = []
    errors: list[str] = []
    considered = 0
    scanned_ok = 0
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        mailbox = str(acct.get("mailbox") or "").strip()
        sa_file = str(acct.get("service_account_file") or "").strip()
        if not (mailbox and sa_file):
            continue
        considered += 1
        before = len(errors)
        all_findings.extend(
            _scan_one_mailbox(mailbox, sa_file, days, limit_per_mailbox, errors)
        )
        if len(errors) == before:
            scanned_ok += 1

    if not all_findings:
        if errors:
            # 🚨「掃過、確實沒有」跟「根本沒掃成功」是兩件事。一次 DNS 故障就能讓
            # 每個信箱的 Gmail 呼叫全滅 → 這裡零結果 → 以前照樣回報「全公司信箱都
            # 沒有未回信」，等於拿網路故障當事實（同 #448 shipping「掃不到 ≠ 逾期」）。
            out = (
                f"⚠️ 過去 {days} 天內沒有查到「對方寄信未回」的 thread，但本輪"
                f"{considered} 個信箱只有 {scanned_ok} 個查核完整 —— "
                "**這不等於沒有未回信件**，下輪查得動就會更新。"
            )
        else:
            out = f"過去 {days} 天內，全公司信箱都沒有「對方寄信未回」的 thread。"
    else:
        by_mailbox: dict[str, list[dict[str, Any]]] = {}
        for f in all_findings:
            by_mailbox.setdefault(f["mailbox"], []).append(f)
        lines = [f"過去 {days} 天內，共 {len(all_findings)} 筆「對方寄信未回」："]
        for mailbox, items in sorted(by_mailbox.items()):
            lines.append(f"\n【{mailbox}】（{len(items)} 筆）")
            for it in items:
                # from/subject 為外部寄件人可控（untrusted）— 給 LLM 前必過 sanitize_for_llm。
                lines.append(
                    f"  - 來自 {sanitize_for_llm(it['from'])}："
                    f"{sanitize_for_llm(it['subject'])}（thread {it['thread_id']}）"
                )
        out = "\n".join(lines)

    if errors:
        # 措辭要準：列出來的每一筆都是真的，但清單**可能有漏** —— 舊版寫「不影響
        # 上面已列出的結果」，讀的人會當成「查核完整」。
        out += ("\n\n⚠️ 以下信箱查核不完整（已列出的都是真的，但可能有漏）：\n"
                + "\n".join(f"  - {e}" for e in errors))
    return out


list_unanswered_company_threads.background_safe = True  # 純讀，背景排程可用。
