"""Telegram deployment preflight checks.

The checks are deliberately read-only by default. Use check_api=True to call
Telegram getMe and verify the token against the live Bot API.
"""
from __future__ import annotations

import os
from typing import Any

import requests as _requests


def _check(status: str, name: str, detail: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "name": name,
        "detail": detail,
        "data": data or {},
    }


def _token_summary(token: str) -> str:
    if not token:
        return "not configured"
    return f"configured ({len(token)} chars)"


def _nearest_existing_parent(path: str) -> str:
    cur = os.path.abspath(os.path.expanduser(path or "."))
    while cur and not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return cur


def _audit_path_check() -> dict[str, Any]:
    from agent_core.telegram_audit import audit_file_path

    path = audit_file_path()
    parent = os.path.dirname(os.path.abspath(os.path.expanduser(path)))
    if os.path.isdir(parent):
        writable = os.access(parent, os.W_OK)
        if writable:
            status = "warn" if path.startswith("/tmp/") else "pass"
            detail = f"audit path parent is writable: {path}"
            if path.startswith("/tmp/"):
                detail += " (ephemeral /tmp; use durable storage for production)"
            return _check(status, "audit_path", detail, data={"path": path})
        return _check("fail", "audit_path", f"audit path parent is not writable: {parent}", data={"path": path})

    nearest = _nearest_existing_parent(parent)
    if os.access(nearest, os.W_OK):
        return _check(
            "warn",
            "audit_path",
            f"audit path parent does not exist yet but nearest parent is writable: {parent}",
            data={"path": path, "nearest_existing_parent": nearest},
        )
    return _check(
        "fail",
        "audit_path",
        f"audit path parent does not exist and nearest parent is not writable: {parent}",
        data={"path": path, "nearest_existing_parent": nearest},
    )


def _api_check(
    token: str,
    *,
    bot_username: str = "",
    requests_module=_requests,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    if not token:
        return _check("skip", "telegram_api", "skipped because bot token is missing")
    try:
        response = requests_module.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=timeout_s,
        )
        data = response.json()
    except Exception as exc:
        return _check("fail", "telegram_api", f"getMe failed: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description") if isinstance(data, dict) else data
        return _check("fail", "telegram_api", f"getMe returned not ok: {description}")
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    api_username = str(result.get("username") or "")
    expected = bot_username.strip().lstrip("@")
    if expected and api_username and expected.lower() != api_username.lower():
        return _check(
            "warn",
            "telegram_api",
            f"getMe username @{api_username} does not match RED_TELEGRAM_BOT_USERNAME @{expected}",
            data={"api_username": api_username, "configured_username": expected},
        )
    return _check("pass", "telegram_api", f"getMe ok: @{api_username or '?'}", data={"api_username": api_username})


def run_preflight(
    *,
    check_api: bool = False,
    requests_module=_requests,
) -> dict[str, Any]:
    from agent_core.daemon_telegram import (
        _telegram_approval_owner_chat_id,
        _telegram_default_actor_color,
        _telegram_default_private_actor_enabled,
        _telegram_default_private_requires_owner_approval,
    )
    from agent_core.telegram import _get_telegram_chat_id, _get_telegram_token
    from agent_core.telegram_agent_config import is_valid_chat_id, telegram_binding_diagnostics

    checks: list[dict[str, Any]] = []
    default_private_enabled = _telegram_default_private_actor_enabled()
    default_private_requires_approval = _telegram_default_private_requires_owner_approval()
    default_actor_color = _telegram_default_actor_color()

    token = _get_telegram_token()
    if token:
        checks.append(_check("pass", "bot_token", _token_summary(token)))
    else:
        checks.append(_check("fail", "bot_token", "Telegram bot token is missing"))

    owner_chat_id = _get_telegram_chat_id()
    approval_owner_chat_id = _telegram_approval_owner_chat_id(owner_chat_id)
    if not owner_chat_id:
        if default_private_enabled and default_actor_color and not default_private_requires_approval:
            checks.append(_check(
                "pass",
                "owner_chat",
                "owner chat_id is optional because default private actor is enabled",
            ))
        elif default_private_enabled and default_actor_color and approval_owner_chat_id:
            checks.append(_check(
                "pass",
                "owner_chat",
                "owner chat_id is optional because approval owner chat_id is configured",
            ))
        else:
            checks.append(_check("fail", "owner_chat", "Telegram owner chat_id is missing"))
    elif not is_valid_chat_id(owner_chat_id):
        checks.append(_check("fail", "owner_chat", f"owner chat_id has invalid format: {owner_chat_id}"))
    else:
        checks.append(_check("pass", "owner_chat", f"owner chat_id configured: {owner_chat_id}"))

    if default_private_enabled:
        if default_actor_color:
            if default_private_requires_approval:
                detail = f"unknown private chats to {default_actor_color} require Red owner approval"
            else:
                detail = f"unknown private chats will route as {default_actor_color}"
            checks.append(_check(
                "pass",
                "default_private_actor",
                detail,
                data={
                    "color": default_actor_color,
                    "owner_approval_required": default_private_requires_approval,
                },
            ))
        else:
            checks.append(_check(
                "fail",
                "default_private_actor",
                "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS is enabled but RED_TELEGRAM_DEFAULT_ACTOR_COLOR is missing or invalid",
            ))

    bot_username = (
        os.environ.get("RED_TELEGRAM_BOT_USERNAME")
        or os.environ.get("TELEGRAM_BOT_USERNAME")
        or ""
    ).strip().lstrip("@")
    if bot_username:
        checks.append(_check("pass", "bot_username", f"bot username configured: @{bot_username}"))
    else:
        checks.append(_check(
            "warn",
            "bot_username",
            "RED_TELEGRAM_BOT_USERNAME is not set; group /command@BotName routing cannot verify bot mentions",
        ))

    diagnostics = telegram_binding_diagnostics(owner_chat_id)
    actors = diagnostics.get("actors", [])
    if actors:
        checks.append(_check("pass", "inbound_actors", diagnostics.get("summary", ""), data={"count": len(actors)}))
    elif default_private_enabled and default_actor_color:
        detail = (
            f"default private actor requires owner approval for {default_actor_color}"
            if default_private_requires_approval
            else f"default private actor is active for {default_actor_color}"
        )
        checks.append(_check(
            "pass",
            "inbound_actors",
            detail,
            data={
                "default_private_actor": default_actor_color,
                "owner_approval_required": default_private_requires_approval,
            },
        ))
    else:
        checks.append(_check("fail", "inbound_actors", "no authorized Telegram actors are configured"))

    dept_actors = [
        actor for actor in actors
        if str(actor.get("color") or "") != "red"
    ]
    if dept_actors:
        colors = sorted({str(actor.get("color") or "") for actor in dept_actors})
        checks.append(_check(
            "pass",
            "department_chats",
            f"department Telegram chats configured for: {', '.join(colors)}",
            data={"colors": colors, "count": len(dept_actors)},
        ))
    elif default_private_enabled and default_actor_color and default_actor_color != "red":
        detail = (
            f"private chats to this bot can request approval for {default_actor_color}"
            if default_private_requires_approval
            else f"private chats to this bot will enter {default_actor_color}"
        )
        checks.append(_check(
            "pass",
            "department_chats",
            detail,
            data={
                "colors": [default_actor_color],
                "count": 0,
                "default_private_actor": True,
                "owner_approval_required": default_private_requires_approval,
            },
        ))
    else:
        checks.append(_check(
            "warn",
            "department_chats",
            "only Red owner is configured; no department Telegram chat is active yet",
        ))

    for warning in diagnostics.get("warnings", []):
        checks.append(_check("warn", "binding_warning", str(warning)))

    checks.append(_audit_path_check())
    if check_api:
        checks.append(_api_check(
            token,
            bot_username=bot_username,
            requests_module=requests_module,
        ))
    else:
        checks.append(_check("skip", "telegram_api", "live getMe check skipped; pass --check-api to enable"))

    failed = sum(1 for item in checks if item["status"] == "fail")
    warnings = sum(1 for item in checks if item["status"] == "warn")
    skipped = sum(1 for item in checks if item["status"] == "skip")
    return {
        "ok": failed == 0,
        "failed": failed,
        "warnings": warnings,
        "skipped": skipped,
        "checks": checks,
        "binding_diagnostics": diagnostics,
    }


def format_preflight_report(result: dict[str, Any]) -> str:
    icon = {
        "pass": "OK",
        "warn": "WARN",
        "fail": "FAIL",
        "skip": "SKIP",
    }
    lines = ["Telegram preflight"]
    for check in result.get("checks", []):
        status = str(check.get("status") or "")
        name = str(check.get("name") or "")
        detail = str(check.get("detail") or "")
        lines.append(f"[{icon.get(status, status.upper())}] {name}: {detail}")
    lines.append(
        "summary: "
        f"{result.get('failed', 0)} failed, "
        f"{result.get('warnings', 0)} warning(s), "
        f"{result.get('skipped', 0)} skipped"
    )
    return "\n".join(lines)
