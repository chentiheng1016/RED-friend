from __future__ import annotations

import contextlib
import os
import unittest
from collections import deque
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_DRY_RUN_BACKEND": "postgres",
}


class OperationalDryRunTests(unittest.TestCase):
    def setUp(self):
        from agent_core import dry_run

        self._dry_run = dry_run
        self._old_state = {
            "enabled": dry_run._STATE["enabled"],
            "enabled_at": dry_run._STATE["enabled_at"],
            "simulated_calls": dry_run._STATE["simulated_calls"],
        }
        self._old_refreshed_at = dry_run._STATE_REFRESHED_AT
        self._old_file_mtime = dry_run._STATE_FILE_MTIME
        dry_run._PG_DRY_RUN_WARNING_UNTIL = 0.0
        dry_run._STATE["enabled"] = False
        dry_run._STATE["enabled_at"] = None
        dry_run._STATE["simulated_calls"] = deque(maxlen=200)
        dry_run._STATE_REFRESHED_AT = 0.0
        dry_run._STATE_FILE_MTIME = None

    def tearDown(self):
        dry_run = self._dry_run
        dry_run._STATE["enabled"] = self._old_state["enabled"]
        dry_run._STATE["enabled_at"] = self._old_state["enabled_at"]
        dry_run._STATE["simulated_calls"] = self._old_state["simulated_calls"]
        dry_run._STATE_REFRESHED_AT = self._old_refreshed_at
        dry_run._STATE_FILE_MTIME = self._old_file_mtime

    def test_backend_requires_explicit_dry_run_switch(self):
        from agent_core import operational_dry_run as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_is_dry_run_reads_postgres_backend(self):
        from agent_core import dry_run

        state = {
            "enabled": True,
            "enabled_at": "2026-06-24T12:00:00",
            "simulated_calls": [],
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_dry_run.load_state",
                    return_value=state,
                ) as load_state:
            self.assertTrue(dry_run.is_dry_run())

        load_state.assert_called_once()

    def test_enable_dry_run_writes_locked_postgres_state(self):
        from agent_core import dry_run

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_state():
            data = {"enabled": False, "enabled_at": None, "simulated_calls": [{"tool": "old"}]}
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_dry_run.locked_state",
                    side_effect=fake_locked_state,
                ) as locked_state:
            out = dry_run.enable_dry_run_mode()

        self.assertIn("Dry-run mode 已啟動", out)
        self.assertTrue(captured["enabled"])
        self.assertTrue(captured["enabled_at"])
        self.assertEqual(captured["simulated_calls"], [])
        locked_state.assert_called_once()

    def test_record_simulated_appends_under_postgres_lock(self):
        from agent_core import dry_run

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_state():
            data = {
                "enabled": True,
                "enabled_at": "2026-06-24T12:00:00",
                "simulated_calls": [{"tool": "existing"}],
            }
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_dry_run.locked_state",
                    side_effect=fake_locked_state,
                ):
            dry_run._record_simulated("send_gmail", "would send", {"to": "x@y.com"})

        self.assertEqual(len(captured["simulated_calls"]), 2)
        self.assertEqual(captured["simulated_calls"][-1]["tool"], "send_gmail")
        self.assertIn("to=", captured["simulated_calls"][-1]["args_preview"])

    def test_disable_dry_run_reads_and_writes_postgres_state(self):
        from agent_core import dry_run

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_state():
            data = {
                "enabled": True,
                "enabled_at": "2026-06-24T12:00:00",
                "simulated_calls": [{"tool": "send_gmail"}, {"tool": "create_calendar"}],
            }
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_dry_run.locked_state",
                    side_effect=fake_locked_state,
                ):
            out = dry_run.disable_dry_run_mode()

        self.assertIn("共演練了 2 次", out)
        self.assertFalse(captured["enabled"])


if __name__ == "__main__":
    unittest.main()
