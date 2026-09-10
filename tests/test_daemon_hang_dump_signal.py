"""SIGUSR1 → all-thread Python traceback wiring (daemon_helpers).

`kill -USR1 <pid>` must dump every thread's Python stack to stderr (→ launchd
log) WITHOUT killing the daemon, so the next rag_sync wedge can be pinned to the
exact Python frame — `sample` only gives C frames, and SIGUSR1's default action
is terminate. run_with_deadline (which every single-shot cron funnels through)
installs it.
"""
from __future__ import annotations

import io
import os
import signal
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import daemon_helpers


class InstallHangDumpSignalTests(unittest.TestCase):
    def setUp(self):
        self._orig_flag = daemon_helpers._hang_dump_installed
        daemon_helpers._hang_dump_installed = False
        self.addCleanup(setattr, daemon_helpers, "_hang_dump_installed", self._orig_flag)

    def test_registers_sigusr1_all_threads(self):
        with mock.patch.object(daemon_helpers, "faulthandler") as fh:
            daemon_helpers.install_hang_dump_signal()
        fh.register.assert_called_once_with(
            signal.SIGUSR1, all_threads=True, chain=False
        )
        self.assertTrue(daemon_helpers._hang_dump_installed)

    def test_idempotent_across_calls(self):
        with mock.patch.object(daemon_helpers, "faulthandler") as fh:
            daemon_helpers.install_hang_dump_signal()
            daemon_helpers.install_hang_dump_signal()
            daemon_helpers.install_hang_dump_signal()
        fh.register.assert_called_once()

    def test_graceful_when_register_raises(self):
        """A stderr with no real fileno (test harness) or an unsupported
        platform must not break the run — the failure is swallowed + logged."""
        with mock.patch.object(daemon_helpers, "faulthandler") as fh:
            fh.register.side_effect = ValueError("sys.stderr is invalid")
            buf = io.StringIO()
            with redirect_stdout(buf):
                daemon_helpers.install_hang_dump_signal()  # must not raise
        self.assertIn("SIGUSR1", buf.getvalue())
        # Attempted once, so a broken stderr doesn't spam every run_with_deadline.
        self.assertTrue(daemon_helpers._hang_dump_installed)


class RunWithDeadlineInstallsSignalTests(unittest.TestCase):
    def setUp(self):
        self._orig_flag = daemon_helpers._hang_dump_installed
        self.addCleanup(setattr, daemon_helpers, "_hang_dump_installed", self._orig_flag)

    def test_run_with_deadline_installs_and_returns(self):
        with mock.patch.object(daemon_helpers, "install_hang_dump_signal") as inst:
            result = daemon_helpers.run_with_deadline(lambda: 7, 5, label="unit")
        self.assertEqual(result, 7)
        inst.assert_called_once()


if __name__ == "__main__":
    unittest.main()
