"""Telegram inbound routing config for department agents.

This module is deliberately config-only: it maps Telegram chat IDs to RED
department colors without granting tool access by itself. The daemon still
decides what an actor is allowed to do.
"""
from __future__ import annotations

import os
import re
from typing import Any

from agent_core.agents.permission_matrix import Agent

_TELEGRAM_CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")


def _clean_chat_id(value: Any) -> str:
    return str(value or "").strip()


def is_valid_chat_id(value: Any) -> bool:
    raw = _clean_chat_id(value)
    if not raw:
        return False
    if not _TELEGRAM_CHAT_ID_RE.fullmatch(raw):
        return False
    try:
        return int(raw) != 0
    except ValueError:
        return False


def _parse_chat_id_list(raw: str) -> set[str]:
    ids: set[str] = set()
    for piece in re.split(r"[\s|]+", raw or ""):
        chat_id = _clean_chat_id(piece)
        if chat_id:
            ids.add(chat_id)
    return ids


def env_agent_chat_bindings(raw: str | None = None) -> dict[Agent, set[str]]:
    """Parse RED_TELEGRAM_AGENT_CHATS into color -> chat IDs.

    Format:
      RED_TELEGRAM_AGENT_CHATS="green:123|456,orange:789,white=-100111"

    Commas, semicolons, and newlines separate color bindings. Within one
    binding, use "|" or whitespace for multiple chat IDs.
    """
    value = os.environ.get("RED_TELEGRAM_AGENT_CHATS", "") if raw is None else raw
    bindings: dict[Agent, set[str]] = {}
    for entry in re.split(r"[,;\n]+", value or ""):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            color_text, ids_text = entry.split(":", 1)
        elif "=" in entry:
            color_text, ids_text = entry.split("=", 1)
        else:
            continue
        try:
            color = Agent(color_text.strip().lower())
        except ValueError:
            continue
        chat_ids = _parse_chat_id_list(ids_text)
        if chat_ids:
            bindings.setdefault(color, set()).update(chat_ids)
    return bindings


def env_agent_chat_warnings(raw: str | None = None) -> list[str]:
    """Return human-readable warnings for RED_TELEGRAM_AGENT_CHATS."""
    value = os.environ.get("RED_TELEGRAM_AGENT_CHATS", "") if raw is None else raw
    warnings: list[str] = []
    for entry in re.split(r"[,;\n]+", value or ""):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            color_text, ids_text = entry.split(":", 1)
        elif "=" in entry:
            color_text, ids_text = entry.split("=", 1)
        else:
            warnings.append(f"RED_TELEGRAM_AGENT_CHATS entry ignored (missing ':' or '='): {entry}")
            continue
        try:
            color = Agent(color_text.strip().lower())
        except ValueError:
            warnings.append(f"RED_TELEGRAM_AGENT_CHATS unknown color ignored: {color_text.strip()}")
            continue
        chat_ids = _parse_chat_id_list(ids_text)
        if not chat_ids:
            warnings.append(f"RED_TELEGRAM_AGENT_CHATS {color.value} has no chat IDs")
        for chat_id in sorted(chat_ids):
            if not is_valid_chat_id(chat_id):
                warnings.append(
                    f"RED_TELEGRAM_AGENT_CHATS {color.value} chat_id has invalid format: {chat_id}"
                )
    return warnings


def employee_telegram_actors() -> dict[str, dict[str, str]]:
    """Return chat_id -> actor records from the employee registry."""
    try:
        from agent_core.web_server.employee_registry import list_employees
    except Exception:
        return {}

    actors: dict[str, dict[str, str]] = {}
    try:
        employees = list_employees()
    except Exception:
        return {}
    for employee in employees:
        chat_id = _clean_chat_id(employee.get("telegram_user_id"))
        if not chat_id:
            continue
        color_text = str(employee.get("color") or "").strip().lower()
        try:
            color = Agent(color_text)
        except ValueError:
            continue
        actors[chat_id] = {
            "chat_id": chat_id,
            "color": color.value,
            "email": str(employee.get("email") or ""),
            "name": str(employee.get("name") or ""),
            "source": "employee_registry",
        }
    return actors


def env_telegram_actors(raw: str | None = None) -> dict[str, dict[str, str]]:
    """Return chat_id -> actor records from RED_TELEGRAM_AGENT_CHATS."""
    actors: dict[str, dict[str, str]] = {}
    for color, chat_ids in env_agent_chat_bindings(raw).items():
        for chat_id in chat_ids:
            actors[chat_id] = {
                "chat_id": chat_id,
                "color": color.value,
                "email": "",
                "name": "",
                "source": "RED_TELEGRAM_AGENT_CHATS",
            }
    return actors


def owner_actor(owner_chat_id: str) -> dict[str, str]:
    chat_id = _clean_chat_id(owner_chat_id)
    if not chat_id:
        return {}
    return {
        "chat_id": chat_id,
        "color": Agent.RED.value,
        "email": "",
        "name": "Red owner",
        "source": "telegram-chat-id",
        "is_owner": "true",
    }


def telegram_actors(owner_chat_id: str = "") -> dict[str, dict[str, str]]:
    """Return all configured inbound Telegram actors keyed by chat_id."""
    actors: dict[str, dict[str, str]] = {}
    owner = owner_actor(owner_chat_id)
    if owner:
        actors[owner["chat_id"]] = owner
    actors.update(env_telegram_actors())
    actors.update(employee_telegram_actors())
    # Owner always wins even if an employee record accidentally reuses the ID.
    if owner:
        actors[owner["chat_id"]] = owner
    return actors


def authorized_inbound_chat_ids(owner_chat_id: str = "") -> set[str]:
    return set(telegram_actors(owner_chat_id).keys())


def actor_for_chat_id(chat_id: str, owner_chat_id: str = "") -> dict[str, str]:
    return telegram_actors(owner_chat_id).get(_clean_chat_id(chat_id), {})


def chat_ids_for_agent(
    color: Agent | str,
    owner_chat_id: str = "",
    *,
    include_owner_for_red: bool = True,
) -> set[str]:
    """Return configured Telegram chat IDs for one department color."""
    try:
        agent = color if isinstance(color, Agent) else Agent(str(color).strip().lower())
    except ValueError:
        return set()
    chat_ids: set[str] = set()
    for chat_id, actor in telegram_actors(owner_chat_id).items():
        if str(actor.get("color") or "").strip().lower() == agent.value:
            if agent is Agent.RED and not include_owner_for_red and actor.get("is_owner"):
                continue
            chat_ids.add(chat_id)
    return chat_ids


def _employee_binding_candidates() -> list[dict[str, str]]:
    try:
        from agent_core.web_server.employee_registry import list_employees
        employees = list_employees()
    except Exception:
        return []

    rows: list[dict[str, str]] = []
    for employee in employees:
        chat_id = _clean_chat_id(employee.get("telegram_user_id"))
        if not chat_id:
            continue
        color_text = str(employee.get("color") or "").strip().lower()
        try:
            color = Agent(color_text)
        except ValueError:
            color = None
        rows.append({
            "chat_id": chat_id,
            "color": color.value if color else color_text,
            "email": str(employee.get("email") or ""),
            "name": str(employee.get("name") or ""),
            "source": "employee_registry",
            "label": str(employee.get("name") or employee.get("email") or ""),
        })
    return rows


def _env_binding_candidates(raw: str | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for color, chat_ids in env_agent_chat_bindings(raw).items():
        for chat_id in sorted(chat_ids):
            rows.append({
                "chat_id": chat_id,
                "color": color.value,
                "email": "",
                "name": "",
                "source": "RED_TELEGRAM_AGENT_CHATS",
                "label": "",
            })
    return rows


def telegram_binding_diagnostics(owner_chat_id: str = "") -> dict[str, Any]:
    """Return effective Telegram bindings plus warnings for admin surfaces."""
    final_actors = telegram_actors(owner_chat_id)
    rows: list[dict[str, str]] = []
    owner = owner_actor(owner_chat_id)
    if owner:
        rows.append({
            **owner,
            "email": "",
            "label": owner.get("name", "Red owner"),
        })
    rows.extend(_env_binding_candidates())
    rows.extend(_employee_binding_candidates())

    warnings = set(env_agent_chat_warnings())
    by_chat: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        chat_id = row.get("chat_id", "")
        if not is_valid_chat_id(chat_id):
            warnings.add(
                f"Telegram chat_id has invalid format: {chat_id} "
                f"({row.get('source', '?')} / {row.get('color', '?')})"
            )
            row["status"] = "invalid"
            continue
        by_chat.setdefault(chat_id, []).append(row)

    for chat_id, matches in by_chat.items():
        if len(matches) > 1:
            parts = [
                f"{row.get('source')}:{row.get('color')}"
                + (f":{row.get('email')}" if row.get("email") else "")
                for row in matches
            ]
            final = final_actors.get(chat_id, {})
            warnings.add(
                f"Telegram chat_id {chat_id} is configured multiple times "
                f"({', '.join(parts)}); effective actor is "
                f"{final.get('source', '?')}:{final.get('color', '?')}."
            )

    for row in rows:
        if row.get("status") == "invalid":
            continue
        final = final_actors.get(row.get("chat_id", ""))
        if not final:
            row["status"] = "inactive"
            continue
        final_source = str(final.get("source") or "")
        final_color = str(final.get("color") or "")
        final_email = str(final.get("email") or "")
        same_email = not row.get("email") or row.get("email") == final_email
        if row.get("source") == final_source and row.get("color") == final_color and same_email:
            row["status"] = "active"
        else:
            row["status"] = "shadowed"

    rows.sort(key=lambda item: (
        item.get("status") != "active",
        item.get("color", ""),
        item.get("chat_id", ""),
        item.get("source", ""),
    ))
    actors = sorted(final_actors.values(), key=lambda item: (
        item.get("color", ""),
        item.get("chat_id", ""),
    ))
    return {
        "summary": actor_log_summary(owner_chat_id),
        "bindings": rows,
        "actors": actors,
        "warnings": sorted(warnings),
    }


def actor_log_summary(owner_chat_id: str = "") -> str:
    actors = telegram_actors(owner_chat_id)
    by_color: dict[str, int] = {}
    for actor in actors.values():
        color = actor.get("color") or "unknown"
        by_color[color] = by_color.get(color, 0) + 1
    parts = [f"{color}:{count}" for color, count in sorted(by_color.items())]
    return f"{len(actors)} chat(s)" + (f" ({', '.join(parts)})" if parts else "")
