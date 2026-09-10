"""Shared RAG sync logic — Drive + Gmail → ChromaDB.

Single source of truth consumed by both entry points:
  launchd/scripts/rag_sync.py     (scheduled daemon)
  agent_daemon.task_rag_sync()    (--task rag_sync CLI / programmatic)

Both paths load the same rag_sync_targets.json config and execute the same
all_drives / folder / gmail branches, so adding a new sync mode here
automatically applies to every caller.
"""
from __future__ import annotations

from collections import Counter
import fcntl
import logging
import os
import subprocess
from typing import Any

_log = logging.getLogger(__name__)


# 帶動態尾巴（檔案大小/例外訊息/檔名）的 skip reason 前綴 — 歸併成一鍵，
# 否則每檔一鍵，summary 的 skip_reasons 會爆成幾百行。
# 順序無所謂：startswith(prefix + ":") 互不重疊。
_SKIP_REASON_PREFIXES = (
    "error",
    "too_large",
    "extract_too_large",
    "extract_error",
    "timeout",
    "embedding_unavailable",
    "image_ocr_unavailable",
    "media_transcription_unavailable",
)


def _skip_reason_bucket(reason: Any) -> str:
    text = str(reason or "unknown")
    for prefix in _SKIP_REASON_PREFIXES:
        if text.startswith(prefix + ":"):
            return prefix
    return text


def _compact_sync_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a log-friendly sync summary without per-file detail spam."""
    keys = ("drive_id", "total", "synced", "skipped", "purged", "listing_complete")
    compact = {key: result[key] for key in keys if key in result}

    details = result.get("details")
    if isinstance(details, list):
        reasons: Counter[str] = Counter()
        files_with_chunks = 0
        error_samples: list[dict[str, Any]] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            if item.get("skipped"):
                bucket = _skip_reason_bucket(item.get("reason"))
                reasons[bucket] += 1
                if bucket == "error" and len(error_samples) < 3:
                    sample = {
                        key: item[key]
                        for key in ("file_id", "title", "reason")
                        if key in item
                    }
                    error_samples.append(sample)
            elif "chunks" in item:
                files_with_chunks += 1

        if reasons:
            compact["skip_reasons"] = dict(sorted(reasons.items()))
        if files_with_chunks:
            compact["files_with_chunks"] = files_with_chunks
        if error_samples:
            compact["error_samples"] = error_samples

    return compact


def _run_sync_locked() -> dict[str, Any]:
    """Execute the full RAG sync pass based on rag_sync_targets.json.

    Returns a summary dict:
      all_drives   bool
      drive        list of per-source result dicts
      gmail        result dict | None
      errors       list of error strings (non-fatal: sync continues)
    """
    from agent_core.ingest.sync_config import load_targets
    from agent_core.ingest import drive_sync, gmail_sync

    targets = load_targets()

    # Strict boolean — "false" / 1 / 0 in a hand-edited JSON file must not
    # accidentally trigger a full-drive ingest with its cross-collection purge.
    all_drives  = targets.get("all_drives") is True
    folder_ids  = [f for f in targets.get("drive_folder_ids", []) if f]
    recursive_folder_ids = {
        f for f in targets.get("recursive_folder_ids", []) if f
    }
    gmail_query = targets.get("gmail_query", "").strip()
    max_threads = int(targets.get("gmail_max_threads", 200))

    if not all_drives and not folder_ids and not gmail_query:
        _log.info("[rag_sync] 尚未設定任何 target，略過。")
        print(
            "[rag_sync] rag_sync_targets.json 尚未設定任何 target，略過。\n"
            "  要同步整個 Drive：/ingest white command.sync_drive {\"all_drives\":true} +確認\n"
            "  要同步特定資料夾：/ingest white command.sync_drive {\"folder_id\":\"...\"} +確認"
        )
        return {"all_drives": False, "drive": [], "gmail": None, "errors": []}

    errors: list[str] = []
    drive_results: list[dict[str, Any]] = []

    # ── Google Chat backup (domain-wide) — FIRST phase, same lock ────────
    # Chat is quick (minutes) vs the multi-hour Drive sweep, so run it FIRST.
    # As the old LAST phase it starved: when a nightly ran out of wall-clock
    # before reaching it, Chat froze (~2 weeks stale at 6/15). Running first
    # guarantees freshness regardless of how long Drive/Gmail take. Still under
    # the same rag_sync.lock (single-writer ChromaDB — no concurrent writer)
    # and fully non-fatal, so a slow/failing whole-domain pass can't break the
    # Drive/Gmail phases that follow.
    chat_result = _sync_chat_backup(targets.get("chat_backup"), errors)

    # ── Drive sync ──────────────────────────────────────────────────────
    if all_drives:
        print("[rag_sync] all_drives=True → 掃描全 Drive…")
        try:
            result = drive_sync.sync_all_drives()
            print(f"[rag_sync] Drive (all): {_compact_sync_result(result)}")
            drive_results.append({"source": "all_drives", **result})
        except Exception as exc:
            msg = f"Drive (all): {exc}"
            errors.append(msg)
            print(f"[rag_sync] Drive (all) 失敗: {exc}")
    else:
        for folder_id in folder_ids:
            try:
                if drive_sync.is_shared_drive_id(folder_id):
                    result = drive_sync.sync_shared_drive(folder_id)
                    print(f"[rag_sync] SharedDrive {folder_id}: {_compact_sync_result(result)}")
                else:
                    recursive = folder_id in recursive_folder_ids
                    result = drive_sync.sync_folder(folder_id, recursive=recursive)
                    print(f"[rag_sync] Drive {folder_id}: {_compact_sync_result(result)}")
                drive_results.append({"source": folder_id, **result})
            except Exception as exc:
                msg = f"Drive {folder_id}: {exc}"
                errors.append(msg)
                print(f"[rag_sync] Drive {folder_id} 失敗: {exc}")

    # ── Gmail sync (primary OAuth account) ──────────────────────────────
    gmail_result: dict[str, Any] | None = None
    if gmail_query:
        try:
            gmail_result = gmail_sync.sync_query(gmail_query, max_threads)
            print(f"[rag_sync] Gmail {gmail_query!r} max={max_threads}: {gmail_result}")
        except Exception as exc:
            msg = f"Gmail: {exc}"
            errors.append(msg)
            print(f"[rag_sync] Gmail 失敗: {exc}")

    # ── Gmail sync (secondary mailboxes via service-account delegation) ──
    gmail_account_results = _sync_gmail_accounts(
        targets.get("gmail_accounts"), gmail_sync, errors
    )

    # ── ACL reconcile — LAST phase, same rag_sync.lock（單寫者 ChromaDB）──
    # rag_access.json 規則變動只影響「之後 ingest 的新 chunk」；存量 chunk 的
    # access_<color> 旗標要靠這個 pass 重標。放最後、同一把鎖（無並發寫者）、
    # 帶時間預算（避免撞夜跑 wall-clock 看門狗）、非致命。
    acl_result = _reconcile_acl(targets.get("acl_reconcile"), errors)

    return {
        "all_drives":     all_drives,
        "drive":          drive_results,
        "gmail":          gmail_result,
        "gmail_accounts": gmail_account_results,
        "chat":           chat_result,
        "acl_reconcile":  acl_result,
        "errors":         errors,
    }


def _reconcile_acl(
    acl_cfg: Any,
    errors: list[str],
) -> dict[str, Any] | None:
    """按 rag_access.json 現值重標存量 chunk 的 ACL 旗標（config-gated、非致命）。

    config（rag_sync_targets.json 的 "acl_reconcile"）：
      {"enabled": true, "time_budget_s": 900}
    只更新旗標與規則不符的 chunk（冪等）：首夜補完全量後，之後每晚只補當日
    規則變動的 delta，成本趨近於零。time_budget_s 到期在批次邊界乾淨停手、
    剩餘明晚續補。預設關（enabled 非 True 直接跳過）。
    """
    if not isinstance(acl_cfg, dict) or acl_cfg.get("enabled") is not True:
        return None
    try:
        from agent_core.ingest.acl_reconcile import reconcile_acl

        budget = acl_cfg.get("time_budget_s")
        summary = reconcile_acl(
            time_budget_s=float(budget) if budget else None,
            log=lambda m: print(m, flush=True),
        )
        print(f"[rag_sync] ACL reconcile: {summary}", flush=True)
        return summary
    except Exception as exc:
        errors.append(f"ACL reconcile: {exc}")
        print(f"[rag_sync] ACL reconcile 失敗: {exc}", flush=True)
        return None


def _sync_chat_backup(
    chat_cfg: Any,
    errors: list[str],
) -> dict[str, Any] | None:
    """Run the domain-wide Chat backup if enabled in config (non-fatal).

    Returns the sync summary, or None when Chat backup is disabled/unconfigured.
    """
    if not isinstance(chat_cfg, dict) or chat_cfg.get("enabled") is not True:
        return None

    try:
        from agent_core.ingest import chat_sync

        result = chat_sync.sync_domain_chat(
            admin_subject=str(chat_cfg.get("admin_subject") or "").strip(),
            service_account_file=str(chat_cfg.get("service_account_file") or "").strip(),
            drive_folder_id=str(chat_cfg.get("drive_folder_id") or "").strip(),
            space_filter=str(chat_cfg.get("space_filter") or ""),
            per_space_cap=int(chat_cfg.get("per_space_cap", 2000)),
            max_users=int(chat_cfg.get("max_users", 0)),
            errors=errors,
        )
        summary = {k: v for k, v in result.items() if k != "errors"}
        print(f"[rag_sync] Chat backup: {summary}")
        return result
    except Exception as exc:
        errors.append(f"Chat: {exc}")
        print(f"[rag_sync] Chat backup 失敗: {exc}")
        return None


def _sync_gmail_accounts(
    accounts: Any,
    gmail_sync,
    errors: list[str],
) -> list[dict[str, Any]]:
    """Sync each configured secondary mailbox via domain-wide delegation.

    Per-account failures are non-fatal: they append to ``errors`` and the loop
    continues so one bad mailbox can't block the rest of the daily pass.
    """
    results: list[dict[str, Any]] = []
    if not isinstance(accounts, list):
        return results

    from agent_core.google_auth import get_service_for_account, build_account_service

    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        mailbox = str(acct.get("mailbox") or "").strip()
        sa_file = str(acct.get("service_account_file") or "").strip()
        query = str(acct.get("gmail_query") or "").strip()
        key = str(acct.get("account_key") or mailbox or "").strip()
        max_t = int(acct.get("max_threads", 200))
        scopes = acct.get("scopes") or None
        # Parallel thread-fetch workers (fetch is the bottleneck once embeds are
        # batched). Each worker needs its own service — transport isn't
        # thread-safe — so we hand sync_query a per-worker builder. 1 = sequential.
        fetch_workers = int(acct.get("fetch_workers", 6))
        if not (mailbox and sa_file and query):
            errors.append(f"Gmail[{key or '?'}]: 設定不完整 (需 mailbox/service_account_file/gmail_query)")
            continue
        try:
            service = get_service_for_account(
                key, "gmail", "v1",
                service_account_file=sa_file,
                subject=mailbox,
                scopes=scopes,
            )

            def _builder(sa=sa_file, mb=mailbox, sc=scopes):
                return build_account_service(
                    "gmail", "v1", service_account_file=sa, subject=mb, scopes=sc,
                )

            result = gmail_sync.sync_query(
                query, max_t, service=service, mailbox_email=mailbox,
                service_builder=_builder, fetch_workers=fetch_workers,
            )
            print(f"[rag_sync] Gmail[{key}] {mailbox} {query!r} max={max_t}: {result}")
            results.append({"account": key, "mailbox": mailbox, **result})
        except Exception as exc:
            errors.append(f"Gmail[{key}]: {exc}")
            print(f"[rag_sync] Gmail[{key}] {mailbox} 失敗: {exc}")
    return results


def sync_lock_is_held() -> bool:
    """探測 rag_sync 鎖是否被別的 sync 程序持有（非阻塞、立即釋放）。

    給 launchd 薄殼在寫 last_run="running" 前用：鎖被持有時這次啟動幾乎必然
    走「已在執行中，略過本輪」，先蓋 running/started_at 只會覆寫持鎖那輪的
    真實起跑時間（2026-07-04 診斷曾被這種假 started_at 誤導）。探測→實際
    取鎖之間有極小 TOCTOU 窗口，但兩個方向的後果都只是回到舊行為（多蓋一次
    章 / started_at 略舊），無正確性影響。
    """
    from agent_core.logging_and_paths import STATE_DIR

    lock_path = os.path.join(STATE_DIR, "rag_sync.lock")
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def run_sync() -> dict[str, Any]:
    """Execute one RAG sync pass, guarded by a process-wide lock.

    launchd can fire while a manual rebuild or a long previous pass is still
    writing ChromaDB. A non-blocking lock makes that failure explicit instead
    of letting two writers corrupt/replace collection state underneath each
    other.
    """
    from agent_core.logging_and_paths import STATE_DIR

    os.makedirs(STATE_DIR, exist_ok=True)
    lock_path = os.path.join(STATE_DIR, "rag_sync.lock")
    lock_fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            msg = "rag_sync 已在執行中，略過本輪（避免同時寫 ChromaDB）"
            print(f"[rag_sync] {msg}", flush=True)
            return {
                "all_drives": False,
                "drive": [],
                "gmail": None,
                "errors": [msg],
                "locked": True,
            }
        finally:
            lock_fh.close()

    try:
        lock_fh.seek(0)
        lock_fh.truncate()
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()
        return _run_sync_locked()
    finally:
        try:
            lock_fh.seek(0)
            lock_fh.truncate()
            lock_fh.flush()
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fh.close()


def is_sync_process_active(state_dir: str | None = None) -> bool:
    """Return True when the rag_sync lock points at a live rag_sync process.

    launchd can retain a historical non-zero exit code even while a manual or
    restarted sync pass is actively writing. This helper lets health checks
    distinguish "down after failure" from "previously terminated, but a
    replacement sync is currently making progress".
    """
    if state_dir is None:
        from agent_core.logging_and_paths import STATE_DIR
        state_dir = STATE_DIR

    lock_path = os.path.join(state_dir, "rag_sync.lock")
    try:
        with open(lock_path, encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return False
    if not raw.isdigit():
        return False
    pid = int(raw)
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return False
    cmd = (proc.stdout or "").strip()
    if proc.returncode != 0 or not cmd:
        return False
    return "rag_sync.py" in cmd or "--task rag_sync" in cmd
