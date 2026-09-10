from __future__ import annotations

import os
import unittest
from unittest import mock


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.fetchone_rows = [(1,), (1,)]
        self.fetchall_rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.fetchone_rows.pop(0) if self.fetchone_rows else None

    def fetchall(self):
        return self.fetchall_rows


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class OperationalDBTests(unittest.TestCase):
    def setUp(self):
        from agent_core import operational_db

        operational_db._SCHEMA_READY = False
        operational_db._FAILURE_UNTIL = 0.0

    def test_database_url_precedence(self):
        from agent_core import operational_db

        with mock.patch.dict(
            os.environ,
            {
                "RED_OPERATIONAL_DB_URL": "postgresql://red-primary",
                "RED_DATABASE_URL": "postgresql://red-secondary",
            },
            clear=True,
        ):
            self.assertEqual(
                operational_db.database_url(),
                "postgresql://red-primary",
            )

    def test_write_audit_event_skips_when_unconfigured(self):
        from agent_core import operational_db

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(operational_db.write_audit_event({"event": "x"}))

    def test_ensure_schema_is_idempotent_ddl(self):
        from agent_core import operational_db

        conn = FakeConnection()
        operational_db.ensure_schema(conn=conn)

        sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_schema_migrations", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_audit_events", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_task_queue", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_task_dead_letters", sql)
        self.assertIn("red_task_queue_ready_idx", sql)
        self.assertIn("red_task_dead_letters_moved_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_telegram_private_approvals", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_telegram_join_requests", sql)
        self.assertIn("red_telegram_private_approvals_status_idx", sql)
        self.assertIn("red_telegram_join_requests_pending_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_tg_auth_state", sql)
        self.assertIn("red_tg_auth_state_updated_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_tool_budget_usage", sql)
        self.assertIn("red_tool_budget_usage_updated_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_cost_events", sql)
        self.assertIn("red_cost_events_ts_idx", sql)
        self.assertIn("red_cost_events_caller_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_api_error_events", sql)
        self.assertIn("red_api_error_events_ts_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_run_history", sql)
        self.assertIn("red_run_history_started_idx", sql)
        self.assertIn("red_run_history_tool_status_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_task_memory", sql)
        self.assertIn("red_task_memory_status_idx", sql)
        self.assertIn("red_task_memory_reminder_idx", sql)
        self.assertIn("red_task_memory_customer_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_edge_devices", sql)
        self.assertIn("red_edge_devices_department_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_edge_tasks", sql)
        self.assertIn("red_edge_tasks_claim_idx", sql)
        self.assertIn("red_edge_tasks_device_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_policy_decisions", sql)
        self.assertIn("red_policy_decisions_ts_idx", sql)
        self.assertIn("red_policy_decisions_decision_idx", sql)
        self.assertIn("red_policy_decisions_tool_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_work_modes", sql)
        self.assertIn("red_work_modes_updated_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_work_mode_history", sql)
        self.assertIn("red_work_mode_history_at_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_dry_run_state", sql)
        self.assertIn("red_dry_run_state_updated_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_web_domain_policies", sql)
        self.assertIn("red_web_domain_policies_policy_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_intent_classifications", sql)
        self.assertIn("red_intent_classifications_ts_idx", sql)
        self.assertIn("red_intent_classifications_intent_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_alert_push_state", sql)
        self.assertIn("red_alert_push_state_level_idx", sql)
        self.assertIn("red_alert_push_state_pushed_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_gemini_circuit_state", sql)
        self.assertIn("red_gemini_circuit_open_idx", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS red_backfill_events", sql)
        self.assertIn("red_backfill_events_seen_idx", sql)
        self.assertIn("ON CONFLICT (version) DO NOTHING", sql)

    def test_write_audit_event_inserts_payload(self):
        from agent_core import operational_db

        conn = FakeConnection()
        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}), \
                mock.patch.object(operational_db, "connect", return_value=conn), \
                mock.patch.object(operational_db, "_failure_cooldown", return_value=0):
            ok = operational_db.write_audit_event({
                "logged_at": "2026-06-24T12:00:00+00:00",
                "event": "telegram_update",
                "status": "ok",
                "chat_id": "123",
                "actor_is_owner": "true",
            })

        self.assertTrue(ok)
        insert_calls = [
            call for call in conn.cursor_obj.calls
            if "INSERT INTO red_audit_events" in call[0]
        ]
        self.assertEqual(len(insert_calls), 1)
        params = insert_calls[0][1]
        self.assertEqual(params[1], "telegram_update")
        self.assertEqual(params[3], "123")
        self.assertIs(params[9], True)
        payload = getattr(params[-1], "obj", params[-1])
        self.assertEqual(payload["chat_id"], "123")

    def test_health_status_reports_configured_failure(self):
        from agent_core import operational_db

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}), \
                mock.patch.object(operational_db, "connect", side_effect=RuntimeError("boom")):
            status = operational_db.health_status()

        self.assertTrue(status["enabled"])
        self.assertFalse(status["ok"])
        self.assertIn("boom", status["error"])


if __name__ == "__main__":
    unittest.main()
