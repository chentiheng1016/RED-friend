"""Regression (review finding #7): the Telegram chat retry wrapper classified
transient errors by keyword ("429", "quota", ...) but never consulted
gemini_client._is_non_retryable_quota_error. So "prepayment credits depleted"
and "monthly spending cap exceeded" 429s — which a retry can never fix — were
retried 3x with backoff, wasting ~6s and logging spurious failures, exactly
the behaviour _gemini_generate was fixed to avoid. Pin fail-fast parity.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeChatRaising:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def send_message(self, wrapped):
        self.calls += 1
        raise self.exc


class SendMessageQuotaRetryTests(unittest.TestCase):
    def _send(self, exc):
        from agent_core import daemon_telegram

        chat = _FakeChatRaising(exc)
        with mock.patch.object(daemon_telegram.time, "sleep", lambda *_a, **_k: None):
            with self.assertRaises(type(exc)):
                daemon_telegram._send_message_with_timeout(chat, "hi", timeout_s=5)
        return chat

    def test_prepayment_depleted_not_retried(self):
        chat = self._send(RuntimeError("429 RESOURCE_EXHAUSTED: prepayment credits are depleted"))
        self.assertEqual(chat.calls, 1)  # failed fast, no retry

    def test_monthly_spending_cap_not_retried(self):
        chat = self._send(RuntimeError("429: monthly spending cap exceeded"))
        self.assertEqual(chat.calls, 1)

    def test_transient_503_still_retried(self):
        from agent_core import daemon_telegram

        chat = self._send(RuntimeError("503 UNAVAILABLE: model is overloaded"))
        # A genuine transient error must still exhaust the retry budget —
        # proves the fast-fail didn't break normal retry behaviour.
        self.assertEqual(chat.calls, daemon_telegram._GEMINI_SEND_MESSAGE_MAX_ATTEMPTS)

    def test_high_demand_still_retried_via_shared_classifier(self):
        # "high demand" used to live only in daemon_telegram's local keyword
        # list; it now comes from the shared gemini_client._is_transient_error
        # so the two retry loops can't drift. The duplicate constant is gone.
        from agent_core import daemon_telegram
        from agent_core.gemini_client import _is_transient_error

        chat = self._send(RuntimeError("503 model is under high demand"))
        self.assertEqual(chat.calls, daemon_telegram._GEMINI_SEND_MESSAGE_MAX_ATTEMPTS)
        self.assertTrue(_is_transient_error("high demand"))
        self.assertFalse(_is_transient_error("400 invalid argument"))
        self.assertFalse(hasattr(daemon_telegram, "_GEMINI_SEND_MESSAGE_TRANSIENT_KEYWORDS"))


class SendMessageCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        from agent_core import gemini_client

        gemini_client._reset_gemini_circuit_for_tests()

    def tearDown(self):
        from agent_core import gemini_client

        gemini_client._reset_gemini_circuit_for_tests()

    def test_open_circuit_blocks_before_chat_call(self):
        from agent_core import cost_tracker, daemon_telegram, gemini_client

        chat = _FakeChatRaising(RuntimeError("should not call"))
        with mock.patch.object(
            gemini_client,
            "_check_gemini_circuit",
            side_effect=gemini_client.GeminiCircuitOpenError("open"),
        ) as check, mock.patch.object(
            cost_tracker, "record_api_error"
        ) as record_error:
            with self.assertRaises(gemini_client.GeminiCircuitOpenError):
                daemon_telegram._send_message_with_timeout(
                    chat,
                    "hi",
                    timeout_s=5,
                    gemini_model="gemini-flash-latest",
                )

        check.assert_called_once_with("gemini-flash-latest")
        self.assertEqual(chat.calls, 0)
        record_error.assert_not_called()

    def test_final_transient_failure_records_api_error_and_circuit(self):
        from agent_core import cost_tracker, daemon_telegram, gemini_client

        exc = RuntimeError("503 UNAVAILABLE: model is overloaded")
        chat = _FakeChatRaising(exc)
        with mock.patch.object(gemini_client, "_check_gemini_circuit") as check, \
                mock.patch.object(
                    gemini_client, "_record_gemini_circuit_failure"
                ) as record_circuit, \
                mock.patch.object(cost_tracker, "record_api_error") as record_error, \
                mock.patch.object(daemon_telegram.time, "sleep", lambda *_a, **_k: None):
            with self.assertRaises(RuntimeError):
                daemon_telegram._send_message_with_timeout(
                    chat,
                    "hi",
                    timeout_s=5,
                    gemini_model="gemini-flash-latest",
                )

        check.assert_called_once_with("gemini-flash-latest")
        self.assertEqual(chat.calls, daemon_telegram._GEMINI_SEND_MESSAGE_MAX_ATTEMPTS)
        record_error.assert_called_once()
        self.assertEqual(record_error.call_args.args[:2], ("gemini", "503"))
        self.assertEqual(record_error.call_args.kwargs["model"], "gemini-flash-latest")
        record_circuit.assert_called_once_with(
            "gemini-flash-latest",
            "503",
            str(exc),
        )

    def test_non_retryable_quota_records_api_error_but_not_circuit(self):
        from agent_core import cost_tracker, daemon_telegram, gemini_client

        chat = _FakeChatRaising(
            RuntimeError("429 RESOURCE_EXHAUSTED: prepayment credits are depleted")
        )
        with mock.patch.object(gemini_client, "_check_gemini_circuit"), \
                mock.patch.object(
                    gemini_client, "_record_gemini_circuit_failure"
                ) as record_circuit, \
                mock.patch.object(cost_tracker, "record_api_error") as record_error:
            with self.assertRaises(RuntimeError):
                daemon_telegram._send_message_with_timeout(
                    chat,
                    "hi",
                    timeout_s=5,
                    gemini_model="gemini-flash-latest",
                )

        self.assertEqual(chat.calls, 1)
        record_error.assert_called_once()
        self.assertEqual(record_error.call_args.args[:2], ("gemini", "quota_depleted"))
        record_circuit.assert_not_called()


class TelegramGeminiFallbackTests(unittest.TestCase):
    def test_timeout_does_not_try_chat_fallback(self):
        from agent_core import daemon_telegram

        self.assertFalse(
            daemon_telegram._should_try_gemini_chat_fallback(
                TimeoutError("chat.send_message exceeded 180s")
            )
        )

    def test_handle_message_rebuilds_chat_with_fallback_model(self):
        from agent_core import daemon_telegram, gemini_client

        primary_chat = object()
        fallback_chat = object()
        chat_state = {
            "chat": primary_chat,
            "turns": 0,
            "last_msg_ts": daemon_telegram.time.time(),
            "gemini_model": "gemini-flash-latest",
            "actor_fp": "",
        }
        sent_models = []
        built_models = []

        def fake_send(chat_obj, _wrapped, **kwargs):
            sent_models.append(kwargs.get("gemini_model"))
            if chat_obj is primary_chat:
                raise gemini_client.GeminiCircuitOpenError("open")
            self.assertIs(chat_obj, fallback_chat)
            return SimpleNamespace(text="fallback ok")

        def fake_build_chat(**kwargs):
            built_models.append(kwargs.get("gemini_model"))
            return fallback_chat

        with mock.patch.dict(
            os.environ,
            {"RED_GEMINI_FALLBACK_MODEL": "gemini-2.5-flash-lite"},
            clear=False,
        ), \
             mock.patch.object(daemon_telegram, "_send_message_with_timeout", side_effect=fake_send), \
             mock.patch.object(daemon_telegram, "tg_build_chat", side_effect=fake_build_chat), \
             mock.patch.object(daemon_telegram, "_load_tg_chat_history_for_rebuild", return_value=[]):
            out = daemon_telegram.tg_handle_message(
                "hi",
                agent_persona="persona",
                tools_list=[],
                gemini_model="gemini-flash-latest",
                agent_client_factory=lambda: None,
                agent_types_factory=lambda: None,
                chat_state=chat_state,
            )

        self.assertEqual(out, "fallback ok")
        self.assertEqual(sent_models, ["gemini-flash-latest", "gemini-2.5-flash-lite"])
        self.assertEqual(built_models, ["gemini-2.5-flash-lite"])
        self.assertIs(chat_state["chat"], fallback_chat)
        self.assertEqual(chat_state["gemini_model"], "gemini-2.5-flash-lite")


if __name__ == "__main__":
    unittest.main()
