from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_WORK_MODE_BACKEND": "postgres",
}


class OperationalWorkModeTests(unittest.TestCase):
    def setUp(self):
        from agent_core import mode_manager

        mode_manager._PG_WORK_MODE_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_work_mode_switch(self):
        from agent_core import operational_work_mode as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_get_current_mode_reads_postgres_backend(self):
        from agent_core import mode_manager

        row = {
            "mode": "security",
            "set_at": "2026-06-24T12:00:00",
            "expires_at": "",
            "set_by": "test",
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_work_mode.load_mode_state",
                    return_value=row,
                ) as load_state:
            self.assertEqual(mode_manager.get_current_mode(), "security")

        load_state.assert_called_once()

    def test_set_work_mode_writes_postgres_backend(self):
        from agent_core import mode_manager

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_state():
            data = {"mode": "normal", "set_at": "", "expires_at": "", "set_by": ""}
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_work_mode.locked_mode_state",
                    side_effect=fake_locked_state,
                ) as locked_state, \
                mock.patch(
                    "agent_core.operational_work_mode.append_history",
                ) as append_history:
            out = mode_manager.set_work_mode(
                "security",
                duration_minutes=30,
                set_by="operator",
            )

        self.assertTrue(out.ok, out)
        self.assertEqual(captured["mode"], "security")
        self.assertEqual(captured["set_by"], "operator")
        self.assertTrue(captured["expires_at"])
        locked_state.assert_called_once()
        append_history.assert_called_once()
        history = append_history.call_args.args[0]
        self.assertEqual(history["from_mode"], "normal")
        self.assertEqual(history["to_mode"], "security")
        self.assertEqual(history["duration_minutes"], 30)

    def test_expired_mode_reverts_postgres_backend(self):
        from agent_core import mode_manager

        expired = {
            "mode": "meeting",
            "set_at": "2020-01-01T00:00:00",
            "expires_at": "2020-01-01T00:01:00",
            "set_by": "test",
        }
        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_state():
            data = dict(expired)
            yield data
            captured.update(data)

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_work_mode.load_mode_state",
                    return_value=expired,
                ), \
                mock.patch(
                    "agent_core.operational_work_mode.locked_mode_state",
                    side_effect=fake_locked_state,
                ), \
                mock.patch(
                    "agent_core.operational_work_mode.append_history",
                ) as append_history:
            self.assertEqual(mode_manager.get_current_mode(), "normal")

        self.assertEqual(captured["mode"], "normal")
        self.assertEqual(captured["set_by"], "auto_expired")
        append_history.assert_called_once()
        self.assertEqual(append_history.call_args.args[0]["reason"], "auto_expired")

    def test_mode_history_reads_postgres_backend(self):
        from agent_core import mode_manager

        rows = [
            {
                "at": "2026-06-24T12:00:00",
                "from_mode": "normal",
                "to_mode": "dev",
                "set_by": "operator",
                "duration_minutes": 15,
            },
        ]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_work_mode.load_history",
                    return_value=rows,
                ) as load_history:
            out = mode_manager.mode_history(hours=6, limit=5)

        self.assertIn("mode 切換歷史", out)
        self.assertIn("normal", out)
        self.assertIn("dev", out)
        self.assertIn("operator", out)
        load_history.assert_called_once_with(hours=6, limit=50000)


if __name__ == "__main__":
    unittest.main()
