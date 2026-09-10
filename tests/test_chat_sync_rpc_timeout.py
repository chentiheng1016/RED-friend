"""chat_sync._execute bounds every RPC with a wall-clock guard.

chat_sync had no request-level backstop — only httplib2's per-recv socket
timeout — so a dribbling Chat/Directory/Drive response could wedge the nightly
sync (rag_sync 2026-07-06). A wall-clock timeout is NOT an HttpError, so it must
propagate (not be swallowed by the 429/5xx retry loop and re-hung).
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
from agent_core.ingest import chat_sync


class _Req:
    def __init__(self, fn):
        self._fn = fn
        self.calls = 0

    def execute(self):
        self.calls += 1
        return self._fn()


class ChatExecuteTimeoutTests(unittest.TestCase):
    def test_passes_value_through(self):
        self.assertEqual(chat_sync._execute(_Req(lambda: {"spaces": []})), {"spaces": []})

    def test_hang_raises_and_is_not_retried(self):
        """A RpcWallClockTimeout must escape _execute on the first attempt — the
        429/5xx retry loop only catches HttpError, so a wall-clock timeout is
        neither retried (which would re-hang) nor swallowed."""
        never_returns = threading.Event()
        self.addCleanup(never_returns.set)
        self.addCleanup(google_auth._service_cache.clear)

        req = _Req(lambda: never_returns.wait(timeout=10))
        with mock.patch.object(chat_sync, "_CHAT_RPC_TIMEOUT_S", 0.1):
            with self.assertRaises(google_auth.RpcWallClockTimeout):
                chat_sync._execute(req)

        self.assertEqual(req.calls, 1)  # executed once, not retried _MAX_RETRIES times


if __name__ == "__main__":
    unittest.main()
