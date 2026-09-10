#!/usr/bin/env python3
"""Periodic runtime housekeeping for RED.

Prunes:
  - old run-history JSON/screenshots
  - old post-deploy smoke JSON records
  - old workflow checkpoints
  - old runtime logs
  - old migration manifests
  - old vault access log entries
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.logging_and_paths import RUNS_DIR, WORKFLOWS_DIR, RUNTIME_ROOT, _LOG_DIR
from agent_core.run_history import prune_old_runs
from agent_core.vault import prune_vault_log


RUN_RETENTION_DAYS = 90
SMOKE_RETENTION_DAYS = int(os.environ.get("RED_SMOKE_RETENTION_DAYS", "60"))
WORKFLOW_RETENTION_DAYS = 45
LOG_RETENTION_DAYS = 30
MIGRATION_RETENTION_DAYS = 30


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _prune_dir_entries(dir_path: str, days: int, *, remove_tree: bool) -> tuple[int, int]:
    if not os.path.isdir(dir_path):
        return 0, 0
    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0
    kept = 0
    for name in os.listdir(dir_path):
        full = os.path.join(dir_path, name)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(full))
        except OSError:
            continue
        if mtime >= cutoff:
            kept += 1
            continue
        try:
            if os.path.isdir(full) and remove_tree:
                shutil.rmtree(full)
            elif os.path.isfile(full):
                os.remove(full)
            else:
                continue
            deleted += 1
        except OSError:
            kept += 1
    return deleted, kept


def _prune_logs(days: int = LOG_RETENTION_DAYS) -> str:
    deleted, kept = _prune_dir_entries(_LOG_DIR, days, remove_tree=False)
    return f"🧹 logs：刪除 {deleted} 個舊檔，保留 {kept} 個（{days} 天）"


def _prune_workflows(days: int = WORKFLOW_RETENTION_DAYS) -> str:
    deleted, kept = _prune_dir_entries(WORKFLOWS_DIR, days, remove_tree=True)
    return f"🧹 workflows：刪除 {deleted} 個舊 run，保留 {kept} 個（{days} 天）"


def _prune_smoke_logs(days: int = SMOKE_RETENTION_DAYS) -> str:
    smoke_dir = os.path.join(RUNS_DIR, "post_deploy_smoke")
    deleted, kept = _prune_dir_entries(smoke_dir, days, remove_tree=False)
    return f"🧹 post-deploy smoke：刪除 {deleted} 個舊紀錄，保留 {kept} 個（{days} 天）"


def _prune_migrations(days: int = MIGRATION_RETENTION_DAYS) -> str:
    migration_dir = os.path.join(RUNTIME_ROOT, "migrations")
    deleted, kept = _prune_dir_entries(migration_dir, days, remove_tree=False)
    return f"🧹 migrations：刪除 {deleted} 個舊 manifest，保留 {kept} 個（{days} 天）"


def main() -> int:
    lines = [
        f"[{_ts()}] ▶️ housekeeping 開始",
        prune_old_runs(days=RUN_RETENTION_DAYS),
        _prune_smoke_logs(days=SMOKE_RETENTION_DAYS),
        _prune_workflows(days=WORKFLOW_RETENTION_DAYS),
        _prune_logs(days=LOG_RETENTION_DAYS),
        _prune_migrations(days=MIGRATION_RETENTION_DAYS),
        prune_vault_log(days=90),
        f"[{_ts()}] ✅ housekeeping 完成",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
