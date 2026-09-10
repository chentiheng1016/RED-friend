"""Persistent configuration for scheduled RAG auto-sync targets.

File: var/data/rag_sync_targets.json
Schema:
  all_drives        bool       If true, sync ALL accessible Drive files daily
                               (overrides drive_folder_ids — no folder needed)
  drive_folder_ids  list[str]  Drive folder IDs to sync daily (used when
                               all_drives is false)
  recursive_folder_ids list[str] Plain Drive folder IDs that should include
                               supported files in all child folders
  gmail_query       str        Gmail search query (default: newer_than:180d)
  gmail_max_threads int        Max threads per sync run (default: 200)
  gmail_accounts    list[dict] Secondary mailboxes synced via service-account
                               domain-wide delegation. Each entry:
                                 account_key          str  cache/log label
                                 mailbox              str  address to impersonate
                                 service_account_file str  JSON key path
                                                            (abs, or repo-relative)
                                 gmail_query          str  search query
                                 max_threads          int  cap per run
                                 scopes               list optional, defaults to
                                                            gmail.readonly
  chat_backup       dict       Google Chat domain-wide backup (empty/absent =
                               disabled). Synced as the last phase of run_sync,
                               under the same rag_sync.lock. Keys:
                                 enabled              bool whether to run
                                 admin_subject        str  Workspace admin to
                                                            impersonate for the
                                                            Admin SDK user list
                                 service_account_file str  JSON key path
                                                            (abs, or repo-relative)
                                 drive_folder_id      str  Red-owned Drive folder
                                                            for JSON backups
                                 space_filter         str  Chat spaces.list filter
                                                            ("" = all space types,
                                                            incl. DMs/group chats)
                                 per_space_cap        int  max messages per space
                                                            per run (default 2000)
                                 max_users            int  0 = all users; small
                                                            value for staged rollout

Usage:
  from agent_core.ingest.sync_config import load_targets, save_targets, enable_all_drives
  enable_all_drives()          # activate the global Drive sync mode

Concurrency:
  All mutators (add_drive_folder, enable_all_drives, etc.) go through
  `_modify_targets`, which holds a cross-process fcntl lock for the
  whole read → mutate → write window. Without that, two concurrent
  callers (REPL + daemon, or two daemons) could both add different
  folder_ids and silently lose one — last write wins.
"""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any, Iterator

from agent_core.logging_and_paths import DATA_DIR
from agent_core.state_io import locked_json

_CONFIG_FILE = os.path.join(DATA_DIR, "rag_sync_targets.json")

_DEFAULTS: dict[str, Any] = {
    "all_drives": False,
    "drive_folder_ids": [],
    "recursive_folder_ids": [],
    "gmail_query": "newer_than:180d",
    "gmail_max_threads": 200,
    "gmail_accounts": [],
    "chat_backup": {},
}

_CHAT_BACKUP_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "admin_subject": "",
    "service_account_file": "var/state/google/service_account.json",
    "drive_folder_id": "",
    "space_filter": "",
    "per_space_cap": 2000,
    "max_users": 0,
}


def load_targets() -> dict[str, Any]:
    """Load sync targets; returns defaults if file absent or corrupt.

    Lock-free read — safe because writes are atomic via locked_json. The
    returned dict is a fresh copy; callers may mutate it without affecting
    the file, but to persist changes use a mutator helper, not save_targets,
    so concurrent writers don't clobber each other.
    """
    if not os.path.exists(_CONFIG_FILE):
        return dict(_DEFAULTS)
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**_DEFAULTS, **data}
    except Exception:
        return dict(_DEFAULTS)


def save_targets(data: dict[str, Any]) -> None:
    """Persist sync targets atomically.

    ⚠️ Lock-unaware — this is a blind overwrite. Prefer the typed mutator
    helpers (`add_drive_folder`, `enable_all_drives`, etc.) which run
    inside `_modify_targets` and hold the cross-process lock. Use this
    only for tests or admin tools that genuinely want to replace the whole
    config.
    """
    with locked_json(_CONFIG_FILE, default=dict(_DEFAULTS)) as current:
        current.clear()
        current.update(data)


@contextlib.contextmanager
def _modify_targets() -> Iterator[dict[str, Any]]:
    """Yield the targets dict under a cross-process lock for safe RMW.

    Merges defaults on entry so callers always see the full schema, and
    persists the mutated dict on normal exit (errors don't persist).
    """
    with locked_json(_CONFIG_FILE, default=dict(_DEFAULTS)) as current:
        # Merge defaults in place so callers can rely on every key existing
        # without rebinding (which wouldn't persist — see state_io.locked_json).
        for key, value in _DEFAULTS.items():
            current.setdefault(key, value if not isinstance(value, list) else list(value))
        yield current


def add_drive_folder(folder_id: str) -> dict[str, Any]:
    """Add a Drive folder_id to the sync list (idempotent). Returns updated targets."""
    fid = folder_id.strip()
    if not fid:
        raise ValueError("folder_id 不能為空")
    with _modify_targets() as targets:
        if fid not in targets["drive_folder_ids"]:
            targets["drive_folder_ids"].append(fid)
        return dict(targets)


def remove_drive_folder(folder_id: str) -> dict[str, Any]:
    """Remove a Drive folder_id from the sync list. Returns updated targets."""
    fid = folder_id.strip()
    with _modify_targets() as targets:
        targets["drive_folder_ids"] = [x for x in targets["drive_folder_ids"] if x != fid]
        targets["recursive_folder_ids"] = [
            x for x in targets.get("recursive_folder_ids", []) if x != fid
        ]
        return dict(targets)


def enable_recursive_folder(folder_id: str) -> dict[str, Any]:
    """Mark a plain Drive folder target as recursive. Returns updated targets."""
    fid = folder_id.strip()
    if not fid:
        raise ValueError("folder_id 不能為空")
    with _modify_targets() as targets:
        recursive = targets.setdefault("recursive_folder_ids", [])
        if fid not in targets["drive_folder_ids"]:
            targets["drive_folder_ids"].append(fid)
        if fid not in recursive:
            recursive.append(fid)
        return dict(targets)


def disable_recursive_folder(folder_id: str) -> dict[str, Any]:
    """Stop recursively scanning a plain Drive folder target."""
    fid = folder_id.strip()
    with _modify_targets() as targets:
        targets["recursive_folder_ids"] = [
            x for x in targets.get("recursive_folder_ids", []) if x != fid
        ]
        return dict(targets)


def enable_all_drives() -> dict[str, Any]:
    """Enable global Drive sync mode (all_drives=True). Returns updated targets."""
    with _modify_targets() as targets:
        targets["all_drives"] = True
        return dict(targets)


def disable_all_drives() -> dict[str, Any]:
    """Disable global Drive sync mode (fall back to drive_folder_ids). Returns updated targets."""
    with _modify_targets() as targets:
        targets["all_drives"] = False
        return dict(targets)


def enable_chat_backup(
    admin_subject: str,
    drive_folder_id: str,
    *,
    service_account_file: str = "",
    space_filter: str | None = None,
    per_space_cap: int | None = None,
    max_users: int | None = None,
) -> dict[str, Any]:
    """Enable domain-wide Google Chat backup. Returns updated targets.

    Merges onto _CHAT_BACKUP_DEFAULTS so a hand-edited partial config keeps the
    rest of the schema. admin_subject + drive_folder_id are required; the others
    only override when explicitly supplied.
    """
    admin = str(admin_subject or "").strip()
    folder = str(drive_folder_id or "").strip()
    if not admin:
        raise ValueError("admin_subject 不能為空（需一位 Workspace 管理員）")
    if not folder:
        raise ValueError("drive_folder_id 不能為空（備份檔要存進哪個 Drive 資料夾）")
    with _modify_targets() as targets:
        cfg = {**_CHAT_BACKUP_DEFAULTS, **(targets.get("chat_backup") or {})}
        cfg["enabled"] = True
        cfg["admin_subject"] = admin
        cfg["drive_folder_id"] = folder
        if service_account_file:
            cfg["service_account_file"] = str(service_account_file).strip()
        if space_filter is not None:
            cfg["space_filter"] = str(space_filter)
        if per_space_cap is not None:
            cfg["per_space_cap"] = int(per_space_cap)
        if max_users is not None:
            cfg["max_users"] = int(max_users)
        targets["chat_backup"] = cfg
        return dict(targets)


def disable_chat_backup() -> dict[str, Any]:
    """Disable Chat backup, preserving the rest of its config. Returns updated targets."""
    with _modify_targets() as targets:
        cfg = {**_CHAT_BACKUP_DEFAULTS, **(targets.get("chat_backup") or {})}
        cfg["enabled"] = False
        targets["chat_backup"] = cfg
        return dict(targets)
