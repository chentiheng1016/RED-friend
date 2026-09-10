"""Lightweight audit log for Telegram entrypoints.

This writes local JSONL plus an optional Postgres operational DB copy. Telegram
handling should never depend on database/network availability just to record
that a command happened.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

from agent_core.logging_and_paths import DATA_DIR

_AUDIT_LOCK = threading.Lock()
_DEFAULT_PREVIEW_LIMIT = 500


def audit_file_path() -> str:
    return os.environ.get("RED_TELEGRAM_AUDIT_FILE") or os.path.join(DATA_DIR, "telegram_audit.jsonl")


def _redact_preview(value: Any, *, limit: int = _DEFAULT_PREVIEW_LIMIT) -> str:
    text = str(value or "")
    try:
        from agent_core.log_redact import redact_log_line
        text = redact_log_line(text)
    except Exception:
        pass
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text[:limit]


def _actor_fields(actor: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(actor, Mapping):
        return {
            "actor_color": "",
            "actor_source": "",
            "actor_label": "",
            "actor_is_owner": "false",
        }
    label = str(actor.get("name") or actor.get("email") or "").strip()
    return {
        "actor_color": str(actor.get("color") or ""),
        "actor_source": str(actor.get("source") or ""),
        "actor_label": label,
        "actor_is_owner": "true" if str(actor.get("is_owner") or "").lower() == "true" else "false",
    }


def _message_fields(message: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(message, Mapping):
        return {
            "chat_type": "",
            "from_id": "",
            "from_username": "",
            "message_id": "",
        }
    chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
    sender = message.get("from") if isinstance(message.get("from"), Mapping) else {}
    return {
        "chat_type": str(chat.get("type") or ""),
        "from_id": str(sender.get("id") or ""),
        "from_username": str(sender.get("username") or ""),
        "message_id": str(message.get("message_id") or ""),
    }


def classify_command(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    head = s.split(maxsplit=1)[0]
    if head.startswith("/"):
        return head.lower()
    return "freeform"


def log_telegram_event(
    *,
    event: str,
    status: str,
    chat_id: str = "",
    text: str = "",
    reply: str = "",
    actor: Mapping[str, Any] | None = None,
    message: Mapping[str, Any] | None = None,
    command: str = "",
    reason: str = "",
    update_id: str | int = "",
) -> None:
    """Append one JSON line. All failures are swallowed by design."""
    try:
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "event": str(event or ""),
            "status": str(status or ""),
            "chat_id": str(chat_id or ""),
            "update_id": str(update_id or ""),
            "command": command or classify_command(text),
            "text_preview": _redact_preview(text),
            "reply_preview": _redact_preview(reply),
            "reason": _redact_preview(reason, limit=200),
            **_actor_fields(actor),
            **_message_fields(message),
        }
        try:
            from agent_core.operational_db import write_audit_event

            write_audit_event(record)
        except Exception:
            pass
        path = audit_file_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        return


def _tail_lines(path: str, limit: int, *, block_size: int = 65536) -> list[str]:
    """從檔尾往回讀 block 取最後 ``limit`` 行 — 不整檔載入。

    audit JSONL 只追加、會長到很大；之前 readlines() 整檔進記憶體只為了取尾
    50 行，admin 頁每次點開都是一次全檔 I/O。這裡從 EOF 反向讀塊，湊滿
    limit+1 個換行（多讀一個確保第一行完整）或碰到檔頭就停。
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        data = b""
        # limit+1 個換行 ⇒ 至少 limit 行完整（最舊那行可能只剩尾巴，取尾時會被擠掉）
        while pos > 0 and data.count(b"\n") <= limit:
            step = min(block_size, pos)
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
    lines = data.splitlines()
    if pos > 0 and len(lines) > limit:
        # 沒讀到檔頭 → 最前面那條可能是被 block 邊界切半的殘行，丟掉
        lines = lines[1:]
    return [line.decode("utf-8", errors="replace") for line in lines[-limit:]]


def read_recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """Small diagnostic helper for tests and future admin surfaces."""
    try:
        from agent_core.operational_db import read_recent_audit_events

        db_events = read_recent_audit_events(limit)
        if db_events:
            return db_events
    except Exception:
        pass
    path = audit_file_path()
    try:
        lines = _tail_lines(path, max(1, int(limit)))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                out.append(item)
        except (ValueError, TypeError):
            continue
    return out
