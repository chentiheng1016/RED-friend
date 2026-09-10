"""Regression (review finding #13): the interactive chat path
(chat.send_message) bypassed _gemini_generate, so its usage was never recorded
by cost_tracker — the most expensive calls (full tool schema + attachments)
went uncounted and the monthly-cap alert could stay quiet. Pin that the chat
paths now feed cost_tracker, best-effort.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _resp_with_usage(model_version="gemini-2.5-pro"):
    um = types.SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=50,
        cached_content_token_count=0,
        thoughts_token_count=0,
        tool_use_prompt_token_count=0,
        total_token_count=150,
    )
    return types.SimpleNamespace(usage_metadata=um, model_version=model_version, text="hi")


class RecordChatResponseTests(unittest.TestCase):
    def test_records_with_model_version(self):
        from agent_core import cost_tracker

        resp = _resp_with_usage("gemini-flash-latest")
        with mock.patch.object(cost_tracker, "record_call") as rc:
            cost_tracker.record_chat_response(resp, caller="telegram_chat")
        rc.assert_called_once()
        kwargs = rc.call_args.kwargs
        self.assertEqual(kwargs["model"], "gemini-flash-latest")
        self.assertEqual(kwargs["caller"], "telegram_chat")

    def test_falls_back_to_configured_model_when_no_version(self):
        from agent_core import cost_tracker
        from agent_core.gemini_client import GEMINI_MODEL

        resp = _resp_with_usage(model_version="")
        with mock.patch.object(cost_tracker, "record_call") as rc:
            cost_tracker.record_chat_response(resp)
        self.assertEqual(rc.call_args.kwargs["model"], GEMINI_MODEL)

    def test_no_usage_metadata_is_noop(self):
        from agent_core import cost_tracker

        resp = types.SimpleNamespace(usage_metadata=None)
        with mock.patch.object(cost_tracker, "record_call") as rc:
            cost_tracker.record_chat_response(resp)
        rc.assert_not_called()

    def test_never_raises_on_garbage(self):
        from agent_core import cost_tracker

        # An object with no usage_metadata attribute at all must not blow up.
        cost_tracker.record_chat_response(object())


class TelegramChatRecordsCostTests(unittest.TestCase):
    def test_send_message_with_timeout_records_cost(self):
        from agent_core import cost_tracker, daemon_telegram

        resp = _resp_with_usage("gemini-2.5-pro")
        chat = types.SimpleNamespace(send_message=lambda _w: resp)
        with mock.patch.object(cost_tracker, "record_call") as rc:
            out = daemon_telegram._send_message_with_timeout(chat, "hi", timeout_s=5)
        self.assertIs(out, resp)
        rc.assert_called_once()
        self.assertEqual(rc.call_args.kwargs["caller"], "telegram_chat")


if __name__ == "__main__":
    unittest.main()
