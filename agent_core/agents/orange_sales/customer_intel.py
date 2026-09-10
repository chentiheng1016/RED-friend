"""Customer-360 knowledge graph tools.

Integrates Gmail + quote
history + sample tracker + RAG memory + spec sheets into per-customer
reports, plus two discovery tools (list_active_customers, customer_alerts).
"""
import os
from datetime import datetime, timedelta

from agent_core.google_auth import get_service
from agent_core.logging_and_paths import logger
from agent_core.memory import recall
from agent_core.prompt_injection import sanitize_for_llm
# Phase 3c：orange_sales 子套件內部跨檔 import，走真實路徑而不繞 shim，
# 避免「shim 載入中 → import 真實 → 真實 import shim」的循環依賴。
from agent_core.agents.orange_sales.quote import query_quote_history, _QUOTE_CSV
from agent_core.sample_tracker import _load_sample_tracker
from agent_core.telegram import telegram_push_agent
from agent_core.specs import _SPECS_DIR


def _customer_360_gmail_summary(customer: str, days: int = 90, max_threads: int = 15) -> str:
    """拉出指定客戶近 N 天的 email 摘要（向 domain 和 customer name 雙向搜）。"""
    try:
        svc = get_service("gmail", "v1")
    except Exception as e:
        return f"（Gmail 連線失敗：{e}）"

    # `customer` 是呼叫端可控文字；未跳脫的雙引號會跳出引號 token、注入 Gmail 運算子
    # （from:/newer_than: 等）枚舉名單外信件 metadata。去掉 " 讓它留在 phrase 內當字面
    # （健檢 Low）。
    safe_customer = (customer or "").replace('"', " ").strip()
    q_parts = [f'"{safe_customer}"']
    query = " OR ".join(q_parts) + f" newer_than:{days}d"

    try:
        r = svc.users().messages().list(userId="me", q=query, maxResults=max_threads).execute()
        msgs = r.get("messages", [])
    except Exception as e:
        return f"（Gmail 查詢失敗：{e}）"

    if not msgs:
        return "（近期無 email 往來）"

    lines = [f"📧 近 {days} 天相關 email {len(msgs)} 封："]
    unread = 0
    for m in msgs[:10]:
        try:
            msg = svc.users().messages().get(
                userId="me", id=m["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            if "UNREAD" in msg.get("labelIds", []):
                unread += 1
            # Subject 為外部寄件人可控（untrusted）— 給 LLM 前必過 sanitize_for_llm。
            subj = sanitize_for_llm(hdrs.get("Subject", "(無主旨)")[:80])
            date = hdrs.get("Date", "")[:20]
            lines.append(f"  - [{date}] {subj}")
        except Exception:
            continue
    if unread:
        lines.insert(1, f"  🔴 未讀 {unread} 封")
    return "\n".join(lines)


def _customer_360_quotes(customer: str, months: int = 24) -> str:
    """拉 quote_history。"""
    try:
        return query_quote_history(customer=customer, recent_months=months)
    except Exception as e:
        return f"（查報價歷史失敗：{e}）"


def _customer_360_samples(customer: str) -> str:
    """拉 sample tracker 裡跟這客戶有關的樣品。"""
    try:
        d = _load_sample_tracker()
        items = [s for s in d.get("samples", {}).values()
                 if customer.lower() in s.get("customer", "").lower()]
        if not items:
            return "（無追蹤中樣品）"
        items.sort(key=lambda x: x.get("expected_feedback_date", "9999"))
        lines = [f"📦 樣品追蹤 {len(items)} 筆："]
        today = datetime.now().strftime("%Y-%m-%d")
        for s in items[:10]:
            due = s.get("expected_feedback_date", "?")
            status = s.get("status", "?")
            overdue = due < today and status in ("open", "delayed")
            mark = "🔴" if overdue else ("🟢" if status == "open" else "⚪")
            lines.append(f"  {mark} {s['sample_id']} | {status} | 預期 {due} | {s.get('description', '')[:40]}")
        return "\n".join(lines)
    except Exception as e:
        return f"（查樣品失敗：{e}）"


def _customer_360_rag(customer: str, k: int = 5) -> str:
    """從向量記憶庫找跟這客戶相關的會議紀錄/備忘錄。"""
    try:
        return recall(query=customer, k=k, mode="hybrid")
    except Exception as e:
        return f"（RAG 查詢失敗：{e}）"


def _customer_360_specs(customer: str) -> str:
    """看這客戶有沒有解析過的規格書。"""
    if not os.path.isdir(_SPECS_DIR):
        return "（尚未解析過規格書）"
    lines = []
    for d in sorted(os.listdir(_SPECS_DIR)):
        if "__" not in d:
            continue
        c_part, p_part = d.split("__", 1)
        if customer.lower() not in c_part.lower():
            continue
        versions = sorted([f for f in os.listdir(os.path.join(_SPECS_DIR, d))
                           if f.startswith("v") and f.endswith(".json")])
        if versions:
            lines.append(f"  📄 {p_part}  （{len(versions)} 版，最新 {versions[-1]}）")
    return "\n".join(lines) if lines else "（此客戶尚無規格書）"


_OWN_COMPANY_KEYWORDS = {"jaifung", "jai fung", "jai jye", "jaijye", "自己", "自家", "我們"}


def customer_360(customer: str, days: int = 90, push_telegram: bool = False):
    """客戶全景報告：整合 Gmail / 報價 / 樣品 / RAG / 規格書 → 一頁 markdown。
    - customer：客戶名或 domain 關鍵字（支援模糊匹配）
    - days：Gmail 往回查幾天（預設 90）
    - push_telegram：True 把報告推 Telegram

    範例：customer_360("ACME") → 近 90 天 email / 報價歷史 / 樣品追蹤 / 相關記憶 / 規格書 → 整合一頁
    """
    cust_stripped = customer.strip()
    if not cust_stripped:
        return "錯誤：customer 不能空"
    if cust_stripped.lower() in _OWN_COMPANY_KEYWORDS or any(
        kw in cust_stripped.lower() for kw in _OWN_COMPANY_KEYWORDS
    ):
        return (f"⚠️ '{cust_stripped}' 看起來是大王自家公司或內部。customer_360 只設計給**客戶**用。\n"
                "若要看內部工作狀態，請用：\n"
                "  - list_active_customers() 看近期活躍客戶\n"
                "  - customer_alerts() 看警示信號\n"
                "  - summarize_inbox() 看信箱摘要")
    customer = cust_stripped

    print(f"\n🧠 產生客戶 360：{customer}（近 {days} 天）...")
    sections = {
        "📧 Email 往來": _customer_360_gmail_summary(customer, days),
        "💰 報價歷史": _customer_360_quotes(customer, months=24),
        "📦 樣品追蹤": _customer_360_samples(customer),
        "📄 規格書": _customer_360_specs(customer),
        "🧠 相關記憶（會議/備忘）": _customer_360_rag(customer, k=5),
    }

    lines = [
        f"# 👥 客戶 360：{customer}",
        f"_生成於 {datetime.now().strftime('%Y-%m-%d %H:%M')}，涵蓋近 {days} 天_",
        "",
    ]
    for title, content in sections.items():
        lines.append(f"## {title}")
        lines.append(str(content)[:1500])
        lines.append("")

    result = "\n".join(lines)

    if push_telegram:
        try:
            telegram_push_agent("orange", result[:3900])
        except Exception as _e:
            logger.warning("customer_360 telegram push 失敗：%s", _e)

    return result


def list_active_customers(days: int = 90, min_emails: int = 2):
    """列出近 N 天互動次數 >= min_emails 的客戶（以 quote_history 的 customer 為主）。
    結合 email_lake 的 company 資訊也可擴充。"""
    try:
        if not os.path.exists(_QUOTE_CSV):
            return "無 quote_history，先 build_quote_history() 建庫再用此工具"
        import pandas as pd
        df = pd.read_csv(_QUOTE_CSV)
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        recent = df[df["email_date"].astype(str) >= cutoff]
        if recent.empty:
            return f"近 {days} 天無活動客戶"
        counts = recent["customer"].value_counts()
        active = counts[counts >= min_emails]
        if active.empty:
            return f"近 {days} 天無 customer 達到 {min_emails} 次互動門檻"
        lines = [f"🏢 近 {days} 天活躍客戶 {len(active)} 家（互動 >= {min_emails} 次）："]
        for c, n in active.head(20).items():
            lines.append(f"  • {c}: {n} 次")
        return "\n".join(lines)
    except Exception as e:
        return f"查詢失敗：{e}"


def customer_alerts(days: int = 90):
    """找出值得注意的客戶信號：
    - 很久沒互動（>=60 天）但過去是大客戶
    - 報價已發但客戶未回
    - 樣品追蹤逾期中

    回傳：警示列表 + 建議動作
    """
    alerts = []

    try:
        d = _load_sample_tracker()
        today = datetime.now().strftime("%Y-%m-%d")
        overdue_by_cust = {}
        for s in d.get("samples", {}).values():
            if s.get("status") not in ("open", "delayed"):
                continue
            due = s.get("expected_feedback_date", "")
            if due and due < today:
                c = s.get("customer", "?")
                overdue_by_cust.setdefault(c, []).append(s["sample_id"])
        for c, sids in overdue_by_cust.items():
            alerts.append(f"🔴 **{c}**：{len(sids)} 個樣品逾期未回饋（{', '.join(sids[:3])}{'...' if len(sids)>3 else ''}）")
    except Exception as _e:
        logger.debug("customer_alerts 樣品檢查：%s", _e)

    try:
        if os.path.exists(_QUOTE_CSV):
            import pandas as pd
            df = pd.read_csv(_QUOTE_CSV)
            cutoff_recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            cutoff_oldish = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
            recent = df[df["email_date"].astype(str) >= cutoff_recent]
            oldish = df[(df["email_date"].astype(str) >= cutoff_oldish) &
                        (df["email_date"].astype(str) < cutoff_recent)]
            recent_custs = set(recent["customer"].astype(str).tolist())
            old_counts = oldish["customer"].value_counts()
            big_old = old_counts[old_counts >= 5].index.tolist()
            silent = [c for c in big_old if c not in recent_custs]
            for c in silent[:10]:
                alerts.append(f"⚠️ **{c}**：過去 1 年有 {old_counts.get(c, 0)} 次互動，近 60 天**無新 email** → 主動跟進？")
    except Exception as _e:
        logger.debug("customer_alerts 沉默客戶：%s", _e)

    if not alerts:
        return "✅ 目前無異常信號（無逾期樣品、無沉默大客戶）"
    return "🚨 客戶警示 — " + datetime.now().strftime("%Y-%m-%d") + "\n\n" + "\n".join(alerts)
