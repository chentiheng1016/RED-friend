"""每日狀態 briefing — 把 `system_status()` 報告寄信 / 推 Telegram。

讓 大王 不用每天手動跑 `./bin/red-status` —— 用 launchd / scheduler
觸發 `send_briefing_email()` 或 `push_briefing_telegram()` 自動送達。

範例用法：
  - 早上 9 點寄日報：用 `add_scheduled_task` 建一個 cron，prompt 寫
    「呼叫 send_briefing_email() 把今日狀態寄給我」
  - Telegram 推送：直接叫 push_briefing_telegram() 即可
  - 自訂 section：send_briefing_email(sections="cost,errors") 只寄
    成本與錯誤兩段（避免訊息太長）

跟 `system_status()` 同樣是唯讀 — 只把 dashboard 內容包成 email/push。
但這兩個 tool 自己會發信 / 推訊息，**屬於有對外副作用** — V4 +確認
門已涵蓋（meeting_briefing / send_gmail / telegram_push 都列 sensitive）。
"""
from __future__ import annotations

import os
from datetime import datetime


def _build_subject(sections: str = "") -> str:
    """Subject 標題 — 包當日 + 簡述涵蓋哪些 section。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if sections:
        return f"[RED status] {today} — {sections}"
    return f"[RED status] {today} — daily briefing"


def _get_dashboard(sections: str = "") -> str:
    """共用 — 抓 dashboard 並回傳純文字（lazy import 避免 skill 載入時就跑）。"""
    from agent_core.dashboard import system_status
    return system_status(sections)


def send_briefing_email(to: str = "", sections: str = "") -> str:
    """把 RED 系統狀態 dashboard 寄信給 大王（或指定 email）。

    Args:
        to: 收件 email；空字串 → 用 大王 自己的（vault 'my-gmail' 或 OAuth login）。
        sections: 逗號分隔的 section name；空字串 = 全部 7 段。
                  可選：daemons, runs, gmail, rag, cost, errors, scheduled

    Returns:
        寄信結果（成功訊息或錯誤）。

    範例：
        send_briefing_email()                         # 全部寄給自己
        send_briefing_email(sections="cost,errors")   # 只寄 2 段
        send_briefing_email(to="boss@xyz.com")        # 寄給別人
    """
    # 1. 抓 dashboard 內容
    try:
        body = _get_dashboard(sections)
    except Exception as e:
        return f"❌ 抓 dashboard 失敗：{type(e).__name__}: {e}"

    # 2. 解析收件者
    if not to or not to.strip():
        try:
            from agent_core.daemon_helpers import get_my_email
            to = get_my_email()
        except Exception as e:
            return (f"❌ 沒指定 to 也抓不到 大王 email（{e}）。\n"
                    f"   試明確指定：send_briefing_email(to='你的 email')")
    if not to:
        return "❌ 沒收件者：to 是空且 vault / OAuth 也沒對應 email"

    # 3. 寄信
    subject = _build_subject(sections)
    # 用 monospace 預先包住，email viewer 才不會搞亂排版
    html_friendly_body = (
        "RED morning briefing\n"
        f"產生於 {datetime.now().isoformat(timespec='seconds')}\n\n"
        "=" * 60 + "\n"
        f"{body}\n"
        + "=" * 60 + "\n\n"
        "（這封信由 send_briefing_email 自動產生 — 不需回信）\n"
        "如要停止：在 daemon_tasks.json 移除對應 scheduled task，\n"
        "或聯絡 大王 自己。"
    )
    try:
        from agent_core.gmail import send_gmail_internal
        result = send_gmail_internal(
            to=to, subject=subject, body=html_friendly_body,
            generated_by="briefing:morning",
        )
        return f"✅ 已寄 briefing → {to}\n{result}"
    except Exception as e:
        return f"❌ 寄信失敗：{type(e).__name__}: {e}\n\n（dashboard 還是抓到了，主動退回給 caller）：\n\n{body[:1000]}"


def push_briefing_telegram(sections: str = "") -> str:
    """把 RED 系統狀態 dashboard 推到 Telegram chat（大王 那邊）。

    比 send_briefing_email 即時、不留檔；適合「現在馬上看一眼」的場景。

    Args:
        sections: 逗號分隔的 section name；空字串 = 全部 7 段。
                  Telegram 4096 字限 — 全部 7 段約 2KB 沒問題，
                  但可以指定 section 縮短訊息。

    Returns:
        推送結果。

    範例：
        push_briefing_telegram()             # 全部
        push_briefing_telegram("cost")       # 只看成本
        push_briefing_telegram("daemons,errors")
    """
    try:
        body = _get_dashboard(sections)
    except Exception as e:
        return f"❌ 抓 dashboard 失敗：{type(e).__name__}: {e}"

    # Telegram 上 monospace 用 ``` block 較好讀
    msg = f"```\n{body}\n```"
    # 如果含 ``` 那把 dashboard 弄壞了 markdown，退回純文字
    if "```" in body:
        msg = body

    try:
        from agent_core.telegram import telegram_push
        result = telegram_push(msg)
        return f"✅ 已推送 briefing 到 Telegram\n{result}"
    except Exception as e:
        return f"❌ 推送失敗：{type(e).__name__}: {e}\n\n（dashboard 還是抓到了）：\n\n{body[:1500]}"


def briefing_preview(sections: str = "") -> str:
    """純粹預覽 — 不寄信不推送，回傳 dashboard 文字。

    跟 system_status() 等價，但放在 briefing skill 裡讓 LLM 在「想寄前
    先看看」的場景容易發現。
    """
    return _get_dashboard(sections)


SKILL_TOOLS = [send_briefing_email, push_briefing_telegram, briefing_preview]
