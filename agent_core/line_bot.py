"""LINE Messaging API entrypoint for employee-facing RED access.

Telegram remains the owner/admin channel. LINE is intentionally narrower:
it verifies LINE signatures, maps the LINE userId to the employee registry,
and exposes a small deterministic command surface before broader agent
workflows are enabled for staff.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import requests

from agent_core.secret_provider import get_secret
from agent_core.web_server.employee_registry import get_employee_by_line_user_id

_LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_LINE_TEXT_LIMIT = 5000
_LINE_QUICK_REPLY_ACTIONS = (
    ("狀態", "狀態"),
    ("部門", "部門"),
    ("幫助", "幫助"),
)


@dataclass
class LineWebhookError(Exception):
    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message


def get_line_channel_secret() -> str:
    return get_secret(
        "line-channel-secret",
        env_names=("LINE_CHANNEL_SECRET", "RED_LINE_CHANNEL_SECRET"),
        keyring_name="line-channel-secret",
    ).value


def get_line_channel_access_token() -> str:
    return get_secret(
        "line-channel-access-token",
        env_names=("LINE_CHANNEL_ACCESS_TOKEN", "RED_LINE_CHANNEL_ACCESS_TOKEN"),
        keyring_name="line-channel-access-token",
    ).value


def verify_line_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    if not body or not signature or not channel_secret:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def _line_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _quick_reply() -> dict[str, list[dict[str, Any]]]:
    return {
        "items": [
            {
                "type": "action",
                "action": {"type": "message", "label": label, "text": text},
            }
            for label, text in _LINE_QUICK_REPLY_ACTIONS
        ]
    }


def _text_message(text: str, *, quick_reply: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {"type": "text", "text": (text or "")[:_LINE_TEXT_LIMIT] or "（空回覆）"}
    if quick_reply:
        message["quickReply"] = _quick_reply()
    return message


def line_reply(
    reply_token: str,
    text: str,
    *,
    access_token: str = "",
    quick_reply: bool = True,
    requests_module=requests,
) -> tuple[bool, str]:
    token = access_token or get_line_channel_access_token()
    if not token:
        return False, "LINE channel access token is missing"
    if not reply_token:
        return False, "LINE reply token is missing"
    try:
        response = requests_module.post(
            _LINE_REPLY_URL,
            headers=_line_headers(token),
            json={"replyToken": reply_token, "messages": [_text_message(text, quick_reply=quick_reply)]},
            timeout=10,
        )
    except Exception as exc:
        return False, f"LINE reply failed: {type(exc).__name__}: {exc}"
    if 200 <= getattr(response, "status_code", 0) < 300:
        return True, ""
    return False, f"LINE reply HTTP {response.status_code}: {getattr(response, 'text', '')[:200]}"


def line_push(
    line_user_id: str,
    text: str,
    *,
    access_token: str = "",
    quick_reply: bool = True,
    requests_module=requests,
) -> tuple[bool, str]:
    """Push a LINE message to one registered employee userId."""
    token = access_token or get_line_channel_access_token()
    if not token:
        return False, "LINE channel access token is missing"
    if not line_user_id:
        return False, "LINE userId is missing"
    try:
        response = requests_module.post(
            _LINE_PUSH_URL,
            headers=_line_headers(token),
            json={"to": line_user_id, "messages": [_text_message(text, quick_reply=quick_reply)]},
            timeout=10,
        )
    except Exception as exc:
        return False, f"LINE push failed: {type(exc).__name__}: {exc}"
    if 200 <= getattr(response, "status_code", 0) < 300:
        return True, ""
    return False, f"LINE push HTTP {response.status_code}: {getattr(response, 'text', '')[:200]}"


def _dept_label(color: str) -> str:
    labels = {
        "red": "總經理辦公室",
        "orange": "業務",
        "yellow": "採購",
        "green": "樣品開發",
        "blue": "船務",
        "indigo": "倉庫",
        "purple": "會計",
        "gray": "生產管理",
        "black": "出納",
        "white": "法務",
    }
    return labels.get(color, color or "未設定")


def _unregistered_reply(line_user_id: str) -> str:
    visible_id = line_user_id or "（LINE 沒提供 userId；請用一對一聊天室加入小紅）"
    return (
        "這個 LINE 帳號尚未登記到小紅員工名單。\n\n"
        "請把下面這串 LINE userId 傳給管理員，管理員在小紅後台員工管理頁填入後，"
        "你就可以用 LINE 找小紅：\n"
        f"{visible_id}"
    )


def _event_line_user_id(event: dict[str, Any]) -> str:
    source = event.get("source") or {}
    return str(source.get("userId") or "")


def build_line_employee_reply(text: str, line_user_id: str) -> str:
    employee = get_employee_by_line_user_id(line_user_id)
    if employee is None:
        return _unregistered_reply(line_user_id)

    # Owner session 主控台：登記這條 LINE 對話 + 套用暫停 gate。LINE 是員工
    # 窄通道、無 owner，所以 paused 一律擋，回主控台的暫停通知。LINE 入口本身
    # 無 LLM 對話上下文，reset 沒有東西可清，收到旗標僅消費掉（免得主控台一直
    # 顯示「待重置」）。registry 出錯不影響正常回覆（fail-open）。
    if line_user_id:
        try:
            from agent_core import session_registry
            _sid = f"line:{line_user_id}"
            _sess = session_registry.touch_session(_sid, channel="line", actor={
                "name": employee.get("name") or "",
                "email": employee.get("email") or "",
                "color": employee.get("color") or "",
                "is_owner": "false",
            })
            if _sess.get("reset_pending"):
                session_registry.consume_reset(_sid)
            if _sess.get("status") == "paused":
                return session_registry.paused_notice(_sess.get("paused_reason") or "")
        except Exception:
            pass

    raw = (text or "").strip()
    normalized = raw.lower()
    name = employee.get("name") or employee.get("email") or "同事"
    color = employee.get("color", "")
    dept = _dept_label(color)

    if normalized in {"/start", "start", "/help", "help", "幫助", "說明", "小紅"}:
        from agent_core.dept_nlp_query import capability_hint, nlp_query_enabled

        nl_line = ""
        if nlp_query_enabled():
            nl_line = "\n也可以直接用中文問問題（唯讀查詢）。" + capability_hint(color)
        return (
            f"{name}，我是小紅。\n\n"
            "LINE 入口開放：\n"
            "1. 狀態：確認小紅服務在線\n"
            "2. 部門：查看你的部門與權限\n"
            "3. 幫助：顯示這份選單\n"
            + nl_line
            + "\n\n傳檔與管理動作仍保留在 Telegram/網站後台。"
        )
    if normalized in {"/status", "status", "狀態", "健康", "在線"}:
        return f"小紅在線。你目前登記為：{name} / {dept}（{color}）。"
    if normalized in {"/dept", "dept", "部門", "我的部門", "權限"}:
        return (
            f"員工：{name}\n"
            f"Email：{employee.get('email', '未設定')}\n"
            f"部門：{dept}（{color}）\n"
            "LINE 員工入口目前是低風險模式，不會直接執行寫入、寄信或刪除類動作。"
        )

    # 非罐頭指令的自由文字 → 唯讀自然語言查詢引擎（query.* + ACL RAG）。
    # LINE reply token 約 1 分鐘內有效；NL pipeline（2 次 flash + 唯讀查詢）
    # 一般 <30s，webhook 端已 offload threadpool，不會卡 event loop。
    from agent_core.dept_nlp_query import answer_dept_question

    try:
        return answer_dept_question(
            color,
            raw,
            actor_name=name,
            channel="line",
        )
    except Exception:
        # 引擎內部已把錯誤收斂成訊息；這裡是最後保險，不讓例外炸 webhook。
        return (
            f"{name}，查詢暫時失敗，請稍後再試。\n"
            "也可以傳「幫助」看目前可用功能。"
        )


def handle_line_webhook(
    body: bytes,
    signature: str,
    *,
    channel_secret: str = "",
    access_token: str = "",
    requests_module=requests,
) -> dict[str, Any]:
    secret = channel_secret or get_line_channel_secret()
    if not secret:
        raise LineWebhookError("LINE channel secret is not configured", status_code=503)
    if not verify_line_signature(body, signature, secret):
        raise LineWebhookError("Invalid LINE signature", status_code=401)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise LineWebhookError(f"Invalid LINE webhook JSON: {exc}", status_code=400) from exc

    events = payload.get("events") or []
    replies = 0
    errors: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "follow":
            reply_text = _unregistered_reply(_event_line_user_id(event))
        elif event_type == "message":
            message = event.get("message") or {}
            if message.get("type") != "text":
                reply_text = "目前 LINE 入口先支援文字訊息；檔案與圖片請先走 Telegram 或網站後台。"
            else:
                reply_text = build_line_employee_reply(
                    str(message.get("text") or ""),
                    _event_line_user_id(event),
                )
        else:
            continue
        ok, err = line_reply(
            str(event.get("replyToken") or ""),
            reply_text,
            access_token=access_token,
            requests_module=requests_module,
        )
        if ok:
            replies += 1
        else:
            errors.append(err)

    return {"ok": not errors, "events": len(events), "replies": replies, "reply_errors": errors}
