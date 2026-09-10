from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from unittest import mock


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class _FakeResponse:
    status_code = 200
    text = "{}"


class _FakeRequests:
    def __init__(self):
        self.posts = []

    def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse()


class LineBotTests(unittest.TestCase):
    def test_verify_line_signature(self):
        from agent_core import line_bot

        body = b'{"events":[]}'
        sig = _signature(body, "secret")

        self.assertTrue(line_bot.verify_line_signature(body, sig, "secret"))
        self.assertFalse(line_bot.verify_line_signature(body, sig, "wrong"))

    def test_unregistered_user_gets_registration_reply(self):
        from agent_core import line_bot

        with mock.patch.object(line_bot, "get_employee_by_line_user_id", return_value=None):
            reply = line_bot.build_line_employee_reply("狀態", "U123")

        self.assertIn("尚未登記", reply)
        self.assertIn("U123", reply)

    def test_registered_user_can_check_status(self):
        from agent_core import line_bot

        employee = {
            "email": "alice@example.com",
            "name": "Alice",
            "color": "green",
            "line_user_id": "U123",
        }
        with mock.patch.object(line_bot, "get_employee_by_line_user_id", return_value=employee):
            reply = line_bot.build_line_employee_reply("狀態", "U123")

        self.assertIn("小紅在線", reply)
        self.assertIn("Alice", reply)
        self.assertIn("樣品開發", reply)

    def test_webhook_verifies_signature_and_replies(self):
        from agent_core import line_bot

        secret = "line-secret"
        body = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "replyToken": "reply-token",
                        "source": {"type": "user", "userId": "U123"},
                        "message": {"type": "text", "text": "幫助"},
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        fake_requests = _FakeRequests()
        employee = {
            "email": "alice@example.com",
            "name": "Alice",
            "color": "green",
            "line_user_id": "U123",
        }

        with mock.patch.object(line_bot, "get_employee_by_line_user_id", return_value=employee):
            result = line_bot.handle_line_webhook(
                body,
                _signature(body, secret),
                channel_secret=secret,
                access_token="line-token",
                requests_module=fake_requests,
            )

        self.assertEqual(result["events"], 1)
        self.assertEqual(result["replies"], 1)
        self.assertFalse(result["reply_errors"])
        self.assertEqual(fake_requests.posts[0]["json"]["replyToken"], "reply-token")
        message = fake_requests.posts[0]["json"]["messages"][0]
        self.assertIn("Alice", message["text"])
        self.assertIn("quickReply", message)

    def test_follow_event_replies_with_registration_user_id(self):
        from agent_core import line_bot

        secret = "line-secret"
        body = json.dumps(
            {
                "events": [
                    {
                        "type": "follow",
                        "replyToken": "reply-token",
                        "source": {"type": "user", "userId": "U999"},
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        fake_requests = _FakeRequests()

        result = line_bot.handle_line_webhook(
            body,
            _signature(body, secret),
            channel_secret=secret,
            access_token="line-token",
            requests_module=fake_requests,
        )

        self.assertEqual(result["replies"], 1)
        text = fake_requests.posts[0]["json"]["messages"][0]["text"]
        self.assertIn("尚未登記", text)
        self.assertIn("U999", text)

    def test_invalid_signature_raises_401(self):
        from agent_core import line_bot

        with self.assertRaises(line_bot.LineWebhookError) as ctx:
            line_bot.handle_line_webhook(
                b'{"events":[]}',
                "bad",
                channel_secret="secret",
                access_token="token",
                requests_module=_FakeRequests(),
            )

        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()


class LineEmployeeNlpTests(unittest.TestCase):
    """自由文字 → dept_nlp_query 唯讀 NL 查詢的路由測試。"""

    _EMPLOYEE = {
        "email": "bob@example.com",
        "name": "Bob",
        "color": "blue",
        "line_user_id": "U555",
    }

    def test_freeform_routes_to_nlp_engine(self):
        from agent_core import line_bot

        with mock.patch.object(
            line_bot, "get_employee_by_line_user_id", return_value=self._EMPLOYEE,
        ), mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            return_value="LURCHI 出貨 ETA 08-15。",
        ) as nlp:
            reply = line_bot.build_line_employee_reply("LURCHI 的貨何時到", "U555")

        self.assertEqual(reply, "LURCHI 出貨 ETA 08-15。")
        args, kwargs = nlp.call_args
        self.assertEqual(args[0], "blue")   # caller = 員工自己的色
        self.assertEqual(args[1], "LURCHI 的貨何時到")
        self.assertEqual(kwargs.get("channel"), "line")
        self.assertEqual(kwargs.get("actor_name"), "Bob")

    def test_canned_commands_take_precedence(self):
        from agent_core import line_bot

        with mock.patch.object(
            line_bot, "get_employee_by_line_user_id", return_value=self._EMPLOYEE,
        ), mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
        ) as nlp:
            reply = line_bot.build_line_employee_reply("狀態", "U555")

        self.assertIn("小紅在線", reply)
        nlp.assert_not_called()

    def test_engine_crash_falls_back_gracefully(self):
        from agent_core import line_bot

        with mock.patch.object(
            line_bot, "get_employee_by_line_user_id", return_value=self._EMPLOYEE,
        ), mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            side_effect=RuntimeError("boom"),
        ):
            reply = line_bot.build_line_employee_reply("任何問題", "U555")

        self.assertIn("查詢暫時失敗", reply)

    def test_help_mentions_nlp(self):
        from agent_core import line_bot

        with mock.patch.object(
            line_bot, "get_employee_by_line_user_id", return_value=self._EMPLOYEE,
        ):
            reply = line_bot.build_line_employee_reply("幫助", "U555")

        self.assertIn("直接用中文問問題", reply)
