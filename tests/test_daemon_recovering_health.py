from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class RagSyncActiveLockTests(unittest.TestCase):
    def test_lock_pid_with_rag_command_is_active(self):
        from agent_core.ingest import rag_runner

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "rag_sync.lock"), "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            with mock.patch.object(
                rag_runner.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="/usr/bin/python launchd/scripts/rag_sync.py\n",
                ),
            ):
                self.assertTrue(rag_runner.is_sync_process_active(d))

    def test_lock_pid_with_unrelated_command_is_not_active(self):
        from agent_core.ingest import rag_runner

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "rag_sync.lock"), "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            with mock.patch.object(
                rag_runner.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="python pytest\n"),
            ):
                self.assertFalse(rag_runner.is_sync_process_active(d))


class RecoveringDaemonHealthTests(unittest.TestCase):
    def test_alert_check_suppresses_rag_last_exit_when_sync_active(self):
        from agent_core import dashboard_alerts

        launchctl = "-\t-15\tcom.xiaohong.rag_sync_daily\n"
        with mock.patch.object(
            dashboard_alerts.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=launchctl),
        ), mock.patch(
            "agent_core.ingest.rag_runner.is_sync_process_active",
            return_value=True,
        ):
            self.assertEqual(dashboard_alerts._check_daemon_health(), [])

    def test_status_summary_treats_active_rag_as_running(self):
        from agent_core import status_center

        launchctl = "-\t-15\tcom.xiaohong.rag_sync_daily\n"
        with mock.patch.object(
            status_center.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=launchctl),
        ), mock.patch(
            "agent_core.ingest.rag_runner.is_sync_process_active",
            return_value=True,
        ):
            summary = status_center._daemons_summary()
        self.assertIn("rag_sync_daily", summary["running"])
        self.assertEqual(summary["last_exit_nonzero"], [])

    def test_dashboard_marks_active_rag_without_red_failure(self):
        from agent_core import dashboard

        launchctl = "-\t-15\tcom.xiaohong.rag_sync_daily\n"
        with mock.patch.object(
            dashboard.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=launchctl),
        ), mock.patch(
            "agent_core.ingest.rag_runner.is_sync_process_active",
            return_value=True,
        ):
            text = dashboard._section_daemons()
        self.assertIn("active now", text)
        self.assertIn("down+last_exit非0 0", text)
        self.assertNotIn("🔴 last exit -15", text)

    def test_dashboard_marks_external_active_rag_even_after_launchd_reload(self):
        from agent_core import dashboard

        launchctl = "-\t0\tcom.xiaohong.rag_sync_daily\n"
        with mock.patch.object(
            dashboard.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=launchctl),
        ), mock.patch(
            "agent_core.ingest.rag_runner.is_sync_process_active",
            return_value=True,
        ):
            text = dashboard._section_daemons()
        self.assertIn("active now", text)
        self.assertNotIn("idle (last exit 0)", text)

    def test_dashboard_surfaces_launchctl_nonzero_detail(self):
        from agent_core import dashboard

        with mock.patch.object(
            dashboard.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Operation not permitted",
            ),
        ):
            text = dashboard._section_daemons()

        self.assertIn("exit 1", text)
        self.assertIn("Operation not permitted", text)

    def test_status_summary_returns_error_on_launchctl_nonzero(self):
        from agent_core import status_center

        with mock.patch.object(
            status_center.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Operation not permitted",
            ),
        ):
            summary = status_center._daemons_summary()

        self.assertIn("_error", summary)
        self.assertIn("exit 1", summary["_error"])
        self.assertIn("Operation not permitted", summary["_error"])

    def test_manual_rag_controller_exit_143_is_completed_in_dashboard(self):
        from agent_core import dashboard

        launchctl = "-\t143\tcom.xiaohong.rag_sync_manual_until_0900\n"
        with mock.patch.object(
            dashboard.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=launchctl),
        ):
            text = dashboard._section_daemons()

        self.assertIn("down+last_exit非0 0", text)
        self.assertIn("completed one-shot (exit 143)", text)
        self.assertNotIn("🔴 last exit 143", text)

    def test_manual_rag_controller_exit_143_is_not_alert_or_status_failure(self):
        from agent_core import dashboard_alerts, status_center

        launchctl = "-\t143\tcom.xiaohong.rag_sync_manual_until_0900\n"
        result = SimpleNamespace(returncode=0, stdout=launchctl)
        with mock.patch.object(
            dashboard_alerts.subprocess,
            "run",
            return_value=result,
        ):
            self.assertEqual(dashboard_alerts._check_daemon_health(), [])

        with mock.patch.object(
            status_center.subprocess,
            "run",
            return_value=result,
        ):
            summary = status_center._daemons_summary()

        self.assertEqual(summary["last_exit_nonzero"], [])
        self.assertEqual(summary["idle_ok_count"], 1)


if __name__ == "__main__":
    unittest.main()
