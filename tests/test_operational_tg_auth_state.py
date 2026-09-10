from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_TELEGRAM_AUTH_BACKEND": "postgres",
    "RED_TELEGRAM_STATE_SUFFIX": "green",
}


class OperationalTelegramAuthStateTests(unittest.TestCase):
    def test_backend_requires_explicit_auth_switch(self):
        from agent_core import operational_tg_auth_state as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_namespace_uses_telegram_state_suffix(self):
        from agent_core import operational_tg_auth_state as store

        with mock.patch.dict(os.environ, {"RED_TELEGRAM_STATE_SUFFIX": "Green Bot!"}, clear=True):
            self.assertEqual(store.namespace(), "green-bot")

    def test_mark_confirmed_algorithm_triggers_lockout(self):
        from agent_core import operational_tg_auth_state as store

        state = {}

        def fake_with_state(_chat_id, callback):
            return callback(state)

        with mock.patch.object(store, "_with_state", side_effect=fake_with_state):
            self.assertTrue(store.mark_confirmed(
                "12345",
                now=1000.0,
                rate_window_sec=300,
                rate_max_confirms=2,
                rate_lockout_sec=600,
            ))
            self.assertEqual(state["confirm_ts"], 1000.0)
            self.assertTrue(store.mark_confirmed(
                "12345",
                now=1001.0,
                rate_window_sec=300,
                rate_max_confirms=2,
                rate_lockout_sec=600,
            ))
            self.assertFalse(store.mark_confirmed(
                "12345",
                now=1002.0,
                rate_window_sec=300,
                rate_max_confirms=2,
                rate_lockout_sec=600,
            ))

        self.assertEqual(state["confirm_history"], [1000.0, 1001.0])
        self.assertEqual(state["lockout_until"], 1602.0)

    def test_message_rate_limit_algorithm_rejects_without_extending_window(self):
        from agent_core import operational_tg_auth_state as store

        state = {}

        def fake_with_state(_chat_id, callback):
            return callback(state)

        with mock.patch.object(store, "_with_state", side_effect=fake_with_state):
            self.assertEqual(
                store.check_message_rate_limit(
                    "12345",
                    now=1000.0,
                    window_sec=60,
                    max_per_window=2,
                ),
                (True, 0.0, 1),
            )
            self.assertEqual(
                store.check_message_rate_limit(
                    "12345",
                    now=1001.0,
                    window_sec=60,
                    max_per_window=2,
                ),
                (True, 0.0, 2),
            )
            allowed, retry, count = store.check_message_rate_limit(
                "12345",
                now=1002.0,
                window_sec=60,
                max_per_window=2,
            )

        self.assertFalse(allowed)
        self.assertEqual(count, 2)
        self.assertGreater(retry, 0)
        self.assertEqual(state["message_history"], [1000.0, 1001.0])

    def test_tg_auth_routes_confirm_state_to_postgres_backend(self):
        from agent_core import tg_auth

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_tg_auth_state.mark_confirmed",
                    return_value=True,
                ) as mark_confirmed, \
                mock.patch(
                    "agent_core.operational_tg_auth_state.check_confirmed",
                    return_value=(True, 0.5),
                ) as check_confirmed, \
                mock.patch(
                    "agent_core.operational_tg_auth_state.revoke_after_use",
                ) as revoke:
            self.assertTrue(tg_auth.mark_confirmed("12345"))
            self.assertEqual(tg_auth.check_confirmed("12345"), (True, 0.5))
            tg_auth.revoke_after_use("12345")

        mark_confirmed.assert_called_once()
        self.assertEqual(mark_confirmed.call_args.args[0], "12345")
        self.assertEqual(mark_confirmed.call_args.kwargs["rate_max_confirms"], 5)
        check_confirmed.assert_called_once()
        revoke.assert_called_once_with("12345")

    def test_tg_auth_routes_message_rate_limit_to_postgres_backend(self):
        from agent_core import tg_auth

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_tg_auth_state.check_message_rate_limit",
                    return_value=(False, 12.0, 20),
                ) as check_rate:
            self.assertEqual(
                tg_auth.check_message_rate_limit("12345"),
                (False, 12.0, 20),
            )

        check_rate.assert_called_once()
        self.assertEqual(check_rate.call_args.args[0], "12345")
        self.assertEqual(check_rate.call_args.kwargs["max_per_window"], 20)


if __name__ == "__main__":
    unittest.main()
