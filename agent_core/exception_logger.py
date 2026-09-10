"""BigQuery exception-event logger.

Logs "Exception Events" to BigQuery for weekly review by Red (GM).

Design principles:
  • Fire-and-forget via daemon thread — never blocks the main request path.
  • Graceful no-op when BIGQUERY_PROJECT_ID is not set; no import errors.
  • Auto-creates the dataset + table on first write (idempotent).
  • Uses Google Application Default Credentials (ADC) or the path in
    GOOGLE_APPLICATION_CREDENTIALS — separate from the user-OAuth flow used
    by Gmail/Drive/Calendar so no scope changes are required.

Environment variables:
  BIGQUERY_PROJECT_ID   GCP project ID (required to enable logging)
  BIGQUERY_DATASET_ID   dataset name (default: red_agent_logs)
  BIGQUERY_TABLE_ID     table name   (default: exception_events)

Table schema (auto-created):
  logged_at     TIMESTAMP   when the event was recorded (UTC)
  event_type    STRING      e.g. "production_anomaly", "agent_error", "cross_query_failure"
  source_agent  STRING      agent color / module name
  severity      STRING      "high" | "medium" | "low" | "error" | "info"
  trace_id      STRING      optional correlation ID
  detail        STRING      human-readable summary
  extra         STRING      JSON-encoded dict of additional structured fields
  environment   STRING      "production" | "development"

Usage:
  from agent_core.exception_logger import log_event

  log_event(
      event_type="production_anomaly",
      source_agent="gray",
      severity="high",
      detail=f"訂單 {order_id} 延誤 {delay_days} 天",
      trace_id=trace_id,
      extra={"order_id": order_id, "product": product, "delay_days": delay_days},
  )
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)

_PROJECT_ID  = os.environ.get("BIGQUERY_PROJECT_ID", "").strip()
_DATASET_ID  = os.environ.get("BIGQUERY_DATASET_ID", "red_agent_logs").strip()
_TABLE_ID    = os.environ.get("BIGQUERY_TABLE_ID",   "exception_events").strip()
_ENV         = "production" if os.environ.get("AGENT_DAEMON_MODE") == "1" else "development"

# BigQuery table schema — kept in sync with the docstring above.
_SCHEMA = [
    {"name": "logged_at",    "type": "TIMESTAMP", "mode": "REQUIRED"},
    {"name": "event_type",   "type": "STRING",    "mode": "REQUIRED"},
    {"name": "source_agent", "type": "STRING",    "mode": "NULLABLE"},
    {"name": "severity",     "type": "STRING",    "mode": "NULLABLE"},
    {"name": "trace_id",     "type": "STRING",    "mode": "NULLABLE"},
    {"name": "detail",       "type": "STRING",    "mode": "NULLABLE"},
    {"name": "extra",        "type": "STRING",    "mode": "NULLABLE"},
    {"name": "environment",  "type": "STRING",    "mode": "NULLABLE"},
]

_client_lock   = threading.Lock()
_client        = None   # google.cloud.bigquery.Client, lazily initialised
_table_ensured = False  # flag: dataset+table creation attempted once per process

# Codex P2: per-call write outcomes so flush_pending() can report whether the
# inserts ACTUALLY landed in BigQuery, not just whether the daemon thread
# returned. Each entry is (succeeded: bool, detail: str) appended once per
# log_event() invocation that reached the insert path.
_results_lock = threading.Lock()
_send_results: list[tuple[bool, str]] = []


# ── internal helpers ─────────────────────────────────────────────────────────

def _get_client():
    """Return a lazily-created BigQuery Client, or None if unavailable."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        if not _PROJECT_ID:
            return None
        try:
            from google.cloud import bigquery  # type: ignore[import-untyped]
            _client = bigquery.Client(project=_PROJECT_ID)
        except Exception as exc:
            _log.warning("BigQuery client init failed: %s", exc)
            _client = None
    return _client


def _ensure_table(client) -> str:
    """Create dataset + table if they don't exist. Returns full table ref."""
    global _table_ensured
    table_ref = f"{_PROJECT_ID}.{_DATASET_ID}.{_TABLE_ID}"
    if _table_ensured:
        return table_ref
    try:
        from google.cloud import bigquery
        from google.api_core.exceptions import NotFound, Conflict

        # Dataset
        dataset_ref = client.dataset(_DATASET_ID)
        try:
            client.get_dataset(dataset_ref)
        except NotFound:
            dataset = bigquery.Dataset(f"{_PROJECT_ID}.{_DATASET_ID}")
            dataset.location = "US"
            try:
                client.create_dataset(dataset)
                _log.info("BigQuery dataset created: %s", _DATASET_ID)
            except Conflict:
                pass  # race condition — another process created it

        # Table
        table_full = f"{_PROJECT_ID}.{_DATASET_ID}.{_TABLE_ID}"
        try:
            client.get_table(table_full)
        except NotFound:
            schema = [bigquery.SchemaField(**f) for f in _SCHEMA]
            table = bigquery.Table(table_full, schema=schema)
            try:
                client.create_table(table)
                _log.info("BigQuery table created: %s", table_full)
            except Conflict:
                pass

        _table_ensured = True
    except Exception as exc:
        _log.warning("BigQuery ensure_table failed: %s", exc)
    return table_ref


def _safe_extra_json(extra: dict[str, Any] | None, limit: int) -> str | None:
    """Serialize *extra* to JSON, guaranteeing the result is always valid.

    If the full serialization fits within *limit* chars, return it as-is.
    If it exceeds the limit, return a compact fallback object that preserves
    the top-level keys and marks the entry as truncated — never a mid-token
    slice that would produce invalid JSON.
    """
    if extra is None:
        return None
    try:
        full = json.dumps(extra, ensure_ascii=False)
        if len(full) <= limit:
            return full
        # Over the limit: store a safe fallback with just the key names so
        # downstream JSON parsers always receive a valid object.
        fallback = json.dumps(
            {"_truncated": True, "keys": list(extra.keys())},
            ensure_ascii=False,
        )
        return fallback
    except Exception:
        return json.dumps({"_serialization_error": True}, ensure_ascii=False)


def _record_result(succeeded: bool, detail: str) -> None:
    """Append per-write outcome for flush_pending() to read back."""
    with _results_lock:
        _send_results.append((succeeded, detail[:200]))


def _do_log(
    event_type: str,
    source_agent: str,
    severity: str,
    detail: str,
    trace_id: str,
    extra: dict[str, Any] | None,
) -> None:
    """Blocking insert — called from a daemon thread."""
    client = _get_client()
    if client is None:
        _record_result(False, "client_init_failed")
        return
    try:
        table_ref = _ensure_table(client)
        row = {
            "logged_at":    datetime.now(timezone.utc).isoformat(),
            "event_type":   event_type,
            "source_agent": source_agent,
            "severity":     severity,
            "trace_id":     trace_id,
            "detail":       detail[:2000] if detail else "",
            "extra":        _safe_extra_json(extra, limit=4000),
            "environment":  _ENV,
        }
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            _log.warning("BigQuery insert errors: %s", errors)
            _record_result(False, f"insert_errors: {str(errors[:1])[:120]}")
        else:
            _record_result(True, table_ref)
    except Exception as exc:
        _log.warning("BigQuery log_event failed: %s", exc)
        _record_result(False, f"{type(exc).__name__}: {str(exc)[:120]}")


# ── public API ───────────────────────────────────────────────────────────────

def log_event(
    event_type: str,
    source_agent: str = "",
    severity: str = "info",
    detail: str = "",
    trace_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Asynchronously log an exception event to BigQuery.

    No-op if BIGQUERY_PROJECT_ID is not configured.
    Never raises — failures are logged at WARNING level only.
    """
    if not _PROJECT_ID:
        return
    t = threading.Thread(
        target=_do_log,
        args=(event_type, source_agent, severity, detail, trace_id, extra),
        daemon=True,
        name="bq_log",
    )
    try:
        t.start()
    except Exception as exc:
        _log.warning("BigQuery log thread could not start: %s", exc)


def flush_pending(timeout: float = 5.0) -> dict:
    """Wait for in-flight `bq_log` daemon threads + report ACTUAL outcomes.

    Codex P2 (gray_mvp.py): in long-running daemons (telegram bot,
    dispatcher) the fire-and-forget pattern is fine. But short-lived CLIs
    like `scripts/gray_mvp.py` need to wait — and need to KNOW whether
    the rows actually landed. Earlier version of this helper just
    returned the joined count; callers that printed "✅ 已寫入" did so
    purely on join completion, even when:
      • Thread.join() returned because of TIMEOUT (work still incomplete)
      • insert_rows_json() returned errors that _do_log only warning-logged
      • client init failed (_do_log silently no-op'd)

    Now returns a dict with separate counters so callers can render an
    accurate status:

      {
        "joined":      int,  # threads waited on
        "still_alive": int,  # of those, how many timed out (work unfinished)
        "succeeded":   int,  # rows that actually landed in BigQuery
        "failed":      int,  # rows that failed (client init / insert errors)
        "last_error":  str,  # most recent failure detail (or "")
      }

    Backwards-compat: callers using `int(flush_pending(...))` previously
    saw thread count. Now returns dict — callers must update.

    Counter scope is **cumulative for this process**. Short-lived CLIs
    (the only callers that actually need this helper) call it exactly
    once before exit, so cumulative == "this run". Long-running daemons
    don't normally call flush_pending — they just let threads finish.
    A long-running daemon that DID call this multiple times would see
    monotonically growing counts; that's fine because the contract is
    "report total writes attempted by this process".
    """
    joined = 0
    still_alive = 0
    for t in list(threading.enumerate()):
        if t.name == "bq_log" and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                still_alive += 1
            joined += 1

    with _results_lock:
        snapshot = list(_send_results)
    succeeded = sum(1 for ok, _ in snapshot if ok)
    failed = sum(1 for ok, _ in snapshot if not ok)
    last_error = next((d for ok, d in reversed(snapshot) if not ok), "")

    return {
        "joined": joined,
        "still_alive": still_alive,
        "succeeded": succeeded,
        "failed": failed,
        "last_error": last_error,
    }
