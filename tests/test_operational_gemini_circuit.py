from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_GEMINI_CIRCUIT_BACKEND": "postgres",
}


class FakeCursor:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = list(rows or [])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    def __init__(self, rows=None):
        self.cursor_obj = FakeCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj


class OperationalGeminiCircuitTests(unittest.TestCase):
    def test_backend_requires_explicit_circuit_switch(self):
        from agent_core import operational_gemini_circuit as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_open_state_returns_active_open_state(self):
        from agent_core import operational_gemini_circuit as store

        conn = FakeConnection(rows=[(220.0, "503")])
        with mock.patch.object(store, "ensure_schema"), \
                mock.patch.object(store, "connect", return_value=conn):
            state = store.open_state("gemini-flash-latest", now=100.0)

        self.assertEqual(state["model"], "gemini-flash-latest")
        self.assertEqual(state["opened_until"], 220.0)
        self.assertEqual(state["reason"], "503")
        sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
        self.assertNotIn("DELETE FROM red_gemini_circuit_state", sql)

    def test_open_state_deletes_expired_state(self):
        from agent_core import operational_gemini_circuit as store

        conn = FakeConnection(rows=[(90.0, "503")])
        with mock.patch.object(store, "ensure_schema"), \
                mock.patch.object(store, "connect", return_value=conn):
            state = store.open_state("gemini-flash-latest", now=100.0)

        self.assertIsNone(state)
        sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
        self.assertIn("DELETE FROM red_gemini_circuit_state", sql)

    def test_record_failure_uses_atomic_upsert(self):
        from agent_core import operational_gemini_circuit as store

        conn = FakeConnection(rows=[(2, 100.0, 100.0, 220.0, "503")])
        with mock.patch.object(store, "ensure_schema"), \
                mock.patch.object(store, "connect", return_value=conn):
            state = store.record_failure(
                "gemini-flash-latest",
                reason="503",
                now=100.0,
                threshold=2,
                window_s=60,
                open_s=120,
            )

        self.assertEqual(state["failures"], 2)
        self.assertEqual(state["opened_until"], 220.0)
        sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
        self.assertIn("ON CONFLICT (model) DO UPDATE", sql)

    def test_clear_state_deletes_model(self):
        from agent_core import operational_gemini_circuit as store

        conn = FakeConnection()
        with mock.patch.object(store, "ensure_schema"), \
                mock.patch.object(store, "connect", return_value=conn):
            store.clear_state("gemini-flash-latest")

        call = conn.cursor_obj.calls[0]
        self.assertIn("DELETE FROM red_gemini_circuit_state", call[0])
        self.assertEqual(call[1], ("gemini-flash-latest",))


if __name__ == "__main__":
    unittest.main()
