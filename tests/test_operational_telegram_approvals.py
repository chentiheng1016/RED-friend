from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_TELEGRAM_APPROVALS_BACKEND": "postgres",
    "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "green",
    "RED_TELEGRAM_STATE_SUFFIX": "green",
}


class OperationalTelegramApprovalsTests(unittest.TestCase):
    def test_backend_requires_explicit_telegram_switch(self):
        from agent_core import operational_telegram_approvals as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_private_approval_actors_read_postgres_backend(self):
        from agent_core import daemon_telegram

        actors = {
            "12345": {
                "chat_id": "12345",
                "telegram_user_id": "12345",
                "color": "green",
                "email": "telegram-12345@example.com",
                "name": "Alice",
                "source": "telegram_private_approval:green",
                "is_owner": "false",
            }
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_telegram_approvals.list_approved_actors",
                    return_value=actors,
                ) as list_actors:
            result = daemon_telegram._telegram_private_approval_actors()

        self.assertEqual(result, actors)
        list_actors.assert_called_once_with(namespace="green", default_color="green")

    def test_join_request_write_uses_postgres_backend(self):
        from agent_core import daemon_telegram

        message = {
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345, "username": "alice", "first_name": "Alice"},
        }
        db_record = {"chat_id": "12345", "name": "Alice", "should_notify": True}
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_telegram_approvals.record_join_request",
                    return_value=db_record,
                ) as record_join:
            result = daemon_telegram._telegram_record_join_request(
                message,
                text="我要加入\n第二行",
                update_id=7,
                now=1000.0,
            )

        self.assertEqual(result, db_record)
        record_join.assert_called_once()
        kwargs = record_join.call_args.kwargs
        self.assertEqual(kwargs["namespace"], "green")
        self.assertEqual(kwargs["chat_id"], "12345")
        self.assertEqual(kwargs["telegram_user_id"], "12345")
        self.assertEqual(kwargs["username"], "alice")
        self.assertEqual(kwargs["name"], "Alice")
        self.assertEqual(kwargs["text"], "我要加入 第二行")
        self.assertEqual(kwargs["update_id"], 7)

    def test_approve_join_request_writes_postgres_backend(self):
        from agent_core import daemon_telegram

        pending = [{
            "chat_id": "12345",
            "telegram_user_id": "12345",
            "username": "alice",
            "name": "Alice",
            "last_seen_ts": 1000.0,
            "status": "pending",
        }]
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_telegram_approvals.pending_join_requests",
                    return_value=pending,
                ) as pending_join, \
                mock.patch(
                    "agent_core.operational_telegram_approvals.save_private_approval",
                    return_value="telegram-12345@example.com",
                ) as save_approval, \
                mock.patch(
                    "agent_core.operational_telegram_approvals.update_join_status",
                    return_value={"chat_id": "12345", "status": "approved"},
                ) as update_status:
            result = daemon_telegram._telegram_approve_join_request(
                chat_id="12345",
                color="green",
                approved_by="999",
            )

        self.assertIn("已核准", result)
        pending_join.assert_called_once()
        save_approval.assert_called_once()
        update_status.assert_called_once()
        self.assertEqual(save_approval.call_args.kwargs["namespace"], "green")
        self.assertEqual(save_approval.call_args.kwargs["chat_id"], "12345")
        self.assertEqual(save_approval.call_args.kwargs["color"], "green")
        self.assertEqual(save_approval.call_args.kwargs["name"], "Alice")
        self.assertEqual(update_status.call_args.kwargs["status"], "approved")


if __name__ == "__main__":
    unittest.main()
