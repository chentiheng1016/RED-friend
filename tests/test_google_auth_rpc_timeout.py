"""Tests for the shared Google RPC wall-clock guard (google_auth).

_GOOGLE_API_TIMEOUT_S bounds each httplib2 recv() but NOT the whole request: a
dribbling response stalls a single .execute() for many minutes at 0% CPU
(rag_sync 2026-07-06 wedged ~32min on a Google SSL header read). run_rpc_with_
timeout bounds the whole call and — critically — clears the service caches so the
abandoned worker thread's wedged connection is never reused.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import google_auth


class RunRpcWithTimeoutTests(unittest.TestCase):
    def test_fast_call_returns_value(self):
        sentinel = {"threads": []}
        result = google_auth.run_rpc_with_timeout(30, "gmail.list", lambda: sentinel)
        self.assertIs(result, sentinel)

    def test_zero_timeout_runs_inline(self):
        """0 disables the guard — fn runs on the caller thread, no daemon."""
        ran_on = []

        def fn():
            ran_on.append(threading.current_thread())
            return 42

        result = google_auth.run_rpc_with_timeout(0, "x", fn)
        self.assertEqual(result, 42)
        self.assertEqual(ran_on, [threading.current_thread()])

    def test_inner_exception_propagates(self):
        def boom():
            raise ValueError("boom-xyz")

        with self.assertRaises(ValueError) as cm:
            google_auth.run_rpc_with_timeout(30, "x", boom)
        self.assertIn("boom-xyz", str(cm.exception))

    def test_hang_raises_and_clears_caches(self):
        """The core guarantee: a wedged RPC aborts (instead of hanging the sync
        forever) AND drops both service caches so the half-open connection the
        leaked worker still holds is never handed back out."""
        # Seed both caches with sentinels; the timeout must wipe them.
        google_auth._service_cache["gmail_v1"] = object()
        with google_auth._sa_cache_lock:
            google_auth._sa_service_cache[("chat:x", "chat", "v1")] = object()
        self.addCleanup(google_auth._service_cache.clear)
        self.addCleanup(google_auth._sa_service_cache.clear)

        never_returns = threading.Event()
        self.addCleanup(never_returns.set)  # release the leaked daemon thread

        def hang():
            never_returns.wait(timeout=10)  # blocks; safety net
            return "late"

        with self.assertRaises(google_auth.RpcWallClockTimeout):
            google_auth.run_rpc_with_timeout(0.1, "chat.list", hang)

        self.assertEqual(google_auth._service_cache, {})
        self.assertEqual(google_auth._sa_service_cache, {})

    def test_timeout_is_ordinary_exception(self):
        """Sync loops defer on `except Exception`; the timeout must be an
        ordinary Exception (via TimeoutError), not BaseException, or a stalled
        RPC would escape the per-item handler and crash the whole run."""
        self.assertTrue(issubclass(google_auth.RpcWallClockTimeout, Exception))
        self.assertTrue(issubclass(google_auth.RpcWallClockTimeout, TimeoutError))


class ClearServiceCachesTests(unittest.TestCase):
    def test_clears_both_caches(self):
        google_auth._service_cache["drive_v3"] = object()
        with google_auth._sa_cache_lock:
            google_auth._sa_service_cache[("k", "gmail", "v1")] = object()
        self.addCleanup(google_auth._service_cache.clear)
        self.addCleanup(google_auth._sa_service_cache.clear)

        google_auth.clear_service_caches()

        self.assertEqual(google_auth._service_cache, {})
        self.assertEqual(google_auth._sa_service_cache, {})


if __name__ == "__main__":
    unittest.main()
