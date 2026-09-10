from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_ALERT_PUSH_BACKEND": "postgres",
}


class OperationalAlertPusherTests(unittest.TestCase):
    def setUp(self):
        from agent_core import alert_pusher

        alert_pusher._PG_ALERT_PUSH_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_alert_push_switch(self):
        from agent_core import operational_alert_pusher as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_load_and_save_state_route_to_postgres_backend(self):
        from agent_core import alert_pusher

        rows = {"alert-1": {"level": "crit", "title": "boom"}}
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_alert_pusher.load_state",
                    return_value=rows,
                ) as load_state:
            self.assertEqual(alert_pusher._load_state(), rows)
        load_state.assert_called_once()

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_alert_pusher.replace_state",
                ) as replace_state:
            alert_pusher._save_state(rows)
        replace_state.assert_called_once_with(rows)

    def test_push_pending_alerts_uses_postgres_locked_state(self):
        from agent_core import alert_pusher

        captured: dict = {}

        @contextlib.contextmanager
        def fake_locked_state():
            yield captured

        fake_alerts = [{
            "id": "alert-1",
            "level": "crit",
            "title": "service down",
            "detail": "exit 1",
        }]
        pushed: list[str] = []

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_alert_pusher.locked_state",
                    side_effect=fake_locked_state,
                ) as locked_state, \
                mock.patch(
                    "agent_core.dashboard_alerts.check_alerts",
                    return_value=fake_alerts,
                ), \
                mock.patch(
                    "agent_core.telegram.telegram_push",
                    side_effect=lambda message: (pushed.append(message), "✅")[1],
                ):
            result = alert_pusher.push_pending_alerts()

        self.assertEqual(result["pushed"], 1)
        self.assertEqual(len(pushed), 1)
        self.assertIn("alert-1", captured)
        self.assertEqual(captured["alert-1"]["title"], "service down")
        self.assertEqual(locked_state.call_count, 2)

    def test_alert_push_status_reads_postgres_backend(self):
        from agent_core import alert_pusher

        rows = {
            "alert-1": {
                "level": "warn",
                "title": "stale daemon",
                "first_seen_at": "2026-06-24T12:00:00",
                "last_pushed_at": "2026-06-24T12:05:00",
            },
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_alert_pusher.load_state",
                    return_value=rows,
                ):
            out = alert_pusher.alert_push_status()

        self.assertIn("alert-1", out)
        self.assertIn("stale daemon", out)


if __name__ == "__main__":
    unittest.main()
