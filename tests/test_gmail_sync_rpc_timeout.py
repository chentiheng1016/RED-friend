"""gmail_sync routes every Gmail RPC through a wall-clock guard.

Before this, gmail_sync called .execute() raw and relied solely on httplib2's
per-recv socket timeout — a dribbling response could wedge the nightly sync for
tens of minutes (rag_sync 2026-07-06). _execute now bounds each RPC and a
per-thread body fetch that times out is isolated by sync_query's _safe_prepare.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import google_auth
from agent_core.ingest import gmail_sync


class _Req:
    """Stands in for a googleapiclient HttpRequest — only .execute() matters."""

    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class GmailExecuteTimeoutTests(unittest.TestCase):
    def test_passes_value_through(self):
        result = gmail_sync._execute(_Req(lambda: {"ok": 1}), "gmail.test")
        self.assertEqual(result, {"ok": 1})

    def test_hang_raises_rpc_timeout(self):
        never_returns = threading.Event()
        self.addCleanup(never_returns.set)
        self.addCleanup(google_auth._service_cache.clear)

        with mock.patch.object(gmail_sync, "_GMAIL_RPC_TIMEOUT_S", 0.1):
            with self.assertRaises(google_auth.RpcWallClockTimeout):
                gmail_sync._execute(
                    _Req(lambda: never_returns.wait(timeout=10)), "gmail.hang"
                )

    def test_thread_fetch_timeout_is_isolated_per_thread(self):
        """A wall-clock timeout fetching one thread's body must skip that thread
        (recorded as an error) — not abort the whole sync_query run."""
        never_returns = threading.Event()
        self.addCleanup(never_returns.set)
        self.addCleanup(google_auth._service_cache.clear)

        service = mock.MagicMock()
        # threads().list → one page with one thread ref
        service.users().threads().list().execute.return_value = {
            "threads": [{"id": "T1", "historyId": "9"}]
        }
        # getProfile → mailbox email
        service.users().getProfile().execute.return_value = {
            "emailAddress": "boss@example.com"
        }

        store = mock.MagicMock()
        store.bulk_get_doc_metadata = None  # force per-thread path (no prefetch)

        # _thread_text calls _execute(threads().get(...)) — make that hang.
        with mock.patch.object(gmail_sync, "_GMAIL_RPC_TIMEOUT_S", 0.1), \
                mock.patch.object(gmail_sync, "get_store", return_value=store), \
                mock.patch.object(
                    gmail_sync, "_thread_text",
                    side_effect=google_auth.RpcWallClockTimeout("gmail.threads.get exceeded"),
                ):
            out = gmail_sync.sync_query(
                "in:inbox", max_threads=5, service=service,
                mailbox_email="boss@example.com",
            )

        # The run completed; the one thread was skipped as an error, not raised.
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["synced"], 0)
        self.assertEqual(out["skipped"], 1)
        store.upsert_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
