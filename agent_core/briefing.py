"""Meeting-briefing tools.

meeting_briefing integrates calendar, gmail, quote history and RAG
memory — all now clean module-level imports.
"""
from datetime import datetime, timezone

from agent_core.google_auth import get_service
from agent_core.google_suite import _find_next_meeting
from agent_core.logging_and_paths import logger
from agent_core.memory import recall
from agent_core.prompt_injection import sanitize_for_llm
from agent_core.quote import query_quote_history
from agent_core.telegram import telegram_push


def meeting_briefing(event_id: str = "", lookback_days: int = 30,
                     push_telegram: bool = False):
    """會議前自動 briefing：整合 calendar + Gmail + 報價歷史 + RAG 會議紀錄。
    - event_id：指定活動 ID，空則自動找下一場
    - lookback_days：Gmail 往回查幾天（預設 30）
    - push_telegram：True 則把 briefing 推到 Telegram（適合 daemon 用）
    回傳 markdown briefing。"""
    try:
        service = get_service('calendar', 'v3')
    except Exception as e:
        return f"行事曆連線失敗：{e}"

    event = None
    minutes_until = None
    if event_id.strip():
        try:
            event = service.events().get(calendarId='primary', eventId=event_id.strip()).execute()
            start_raw = event['start'].get('dateTime') or event['start'].get('date')
            if start_raw and 'T' in start_raw:
                start_dt = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
                minutes_until = int((start_dt - datetime.now(timezone.utc)).total_seconds() / 60)
        except Exception as e:
            return f"找不到會議 {event_id}：{e}"
    else:
        event, minutes_until = _find_next_meeting()
        if not event:
            return "接下來 72 小時內沒有會議。"

    # Event summary/description are attacker-controllable on an external invite,
    # and this brief is both an LLM tool result AND pushed unattended to
    # Telegram — sanitize injection markers before either sink. (Title also
    # feeds the RAG recall query below; sanitizing at the source covers both.)
    # `or` (not .get default): Calendar can return summary/description present
    # but null. .get(key, default) keeps the None → title would be None and
    # later recall(query=title) / query_quote_history would choke. `or` falls
    # back to the string default for both missing-key and explicit-null.
    title = sanitize_for_llm(event.get('summary') or '(無標題)')
    start = event['start'].get('dateTime', event['start'].get('date', '?'))
    # location 與 title/description 一樣是外部邀請可控欄位（untrusted）— 同樣先淨化。
    location = sanitize_for_llm(event.get('location') or '')
    attendees = [a.get('email', '') for a in event.get('attendees', [])
                 if a.get('email') and not a.get('self', False)]
    description = sanitize_for_llm(event.get('description') or '')

    until_tag = ""
    if minutes_until is not None:
        if minutes_until < 0:
            until_tag = f"（已開始 {-minutes_until} 分鐘前）"
        elif minutes_until < 60:
            until_tag = f"（{minutes_until} 分鐘後開始）"
        else:
            until_tag = f"（{minutes_until//60} 小時 {minutes_until%60} 分後開始）"

    lines = [
        f"# 📋 會議 Briefing：{title}",
        f"- 🕐 時間：{start}  {until_tag}",
        f"- 📍 地點：{location or '未指定'}",
        f"- 👥 出席者：{', '.join(attendees) if attendees else '（僅自己）'}",
    ]
    if description:
        lines.append(f"- 📝 事件說明：{description[:300]}")
    lines.append("")

    # === 1. Gmail 往來 ===
    if attendees:
        lines.append(f"## 📧 近 {lookback_days} 天 Email 往來")
        try:
            gmail_svc = get_service('gmail', 'v1')
            for addr in attendees[:5]:
                try:
                    q = f"(from:{addr} OR to:{addr}) newer_than:{lookback_days}d"
                    r = gmail_svc.users().messages().list(userId='me', q=q, maxResults=10).execute()
                    msgs = r.get('messages', [])
                    if not msgs:
                        lines.append(f"- **{addr}**：近 {lookback_days} 天無往來")
                        continue
                    subs = []
                    unread_count = 0
                    my_last = None
                    their_last = None
                    for m in msgs[:10]:
                        msg = gmail_svc.users().messages().get(
                            userId='me', id=m['id'],
                            format='metadata',
                            metadataHeaders=['Subject', 'From', 'Date']
                        ).execute()
                        headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                        labels = msg.get('labelIds', [])
                        is_unread = 'UNREAD' in labels
                        if is_unread:
                            unread_count += 1
                        from_hdr = headers.get('From', '')
                        date_hdr = headers.get('Date', '')[:25]
                        subj = sanitize_for_llm(headers.get('Subject', '(無主旨)')[:60])
                        is_mine = 'SENT' in labels or '@company.example' in from_hdr.lower()
                        if is_mine and not my_last:
                            my_last = date_hdr
                        if not is_mine and not their_last:
                            their_last = date_hdr
                        subs.append(f"    - [{'未讀' if is_unread else '已讀'}] {subj}")
                    status_parts = [f"{len(msgs)} 封"]
                    if unread_count:
                        status_parts.append(f"🔴 {unread_count} 封未讀")
                    if their_last:
                        status_parts.append(f"對方最後 {their_last}")
                    if my_last:
                        status_parts.append(f"我最後 {my_last}")
                    lines.append(f"- **{addr}**：{' / '.join(status_parts)}")
                    lines.extend(subs[:3])
                except Exception as _e:
                    lines.append(f"- **{addr}**：查詢失敗 ({_e})")
        except Exception as e:
            lines.append(f"- ⚠️ Gmail 查詢全部失敗：{e}")
        lines.append("")

    # === 2. 報價歷史 ===
    quote_keywords = []
    if attendees:
        for a in attendees[:3]:
            if '@' in a:
                domain = a.split('@')[1].split('.')[0]
                if domain and domain not in ('gmail', 'yahoo', 'outlook', 'hotmail', 'icloud'):
                    quote_keywords.append(domain)
    quote_keywords.append(title)

    lines.append("## 💰 相關報價歷史")
    found_quote = False
    for kw in quote_keywords[:3]:
        try:
            q_res = query_quote_history(customer=kw, recent_months=24)
            if q_res and "0 筆" not in q_res and "尚未建立" not in q_res:
                lines.append(f"### 關鍵字：{kw}")
                lines.append(q_res[:600] + ("…(截斷)" if len(q_res) > 600 else ""))
                found_quote = True
                break
        except Exception:
            logger.debug("silent ignore in broad except")
    if not found_quote:
        lines.append("- 無相關歷史報價紀錄")
    lines.append("")

    # === 3. RAG 語意搜尋 ===
    lines.append("## 🧠 相關記憶（RAG 語意搜尋）")
    try:
        rag = recall(query=title, k=5, mode="hybrid")
        lines.append(str(rag)[:1000])
    except Exception as e:
        lines.append(f"RAG 查詢失敗：{e}")

    result = "\n".join(lines)

    if push_telegram:
        try:
            telegram_push(result[:3900])
        except Exception as _e:
            logger.warning("briefing telegram push 失敗：%s", _e)

    return result


def briefing_next_meeting(lookback_days: int = 30):
    """快捷：幫我 briefing 下一場會議（不推 Telegram，僅回傳）"""
    return meeting_briefing(event_id="", lookback_days=lookback_days, push_telegram=False)
