"""Shared launchd status classification helpers."""
from __future__ import annotations


_BENIGN_TRANSIENT_EXIT_CODES = {0, 1, 15, 143, -15}


def short_launchd_label(label: str) -> str:
    """Return a RED-short launchd label without the com.xiaohong prefix."""
    return str(label or "").removeprefix("com.xiaohong.")


def is_known_daemon_recovering(label: str) -> bool:
    """True when launchd has stale failure state but live work is active now."""
    if short_launchd_label(label) != "rag_sync_daily":
        return False
    try:
        from agent_core.ingest.rag_runner import is_sync_process_active

        return is_sync_process_active()
    except Exception:
        return False


def is_benign_stopped_daemon(label: str, exit_code: int) -> bool:
    """Suppress completed one-shot jobs that launchd keeps in last-exit output.

    Manual RAG controllers intentionally stop the child process at the planned
    deadline. macOS reports that planned TERM as 143, and an already-expired
    controller can report 1 after reboot. Neither is an actionable daemon
    failure for smoke checks or operator alerts.
    """
    short = short_launchd_label(label)
    return (
        short.startswith("rag_sync_manual_until_")
        and exit_code in _BENIGN_TRANSIENT_EXIT_CODES
    )
