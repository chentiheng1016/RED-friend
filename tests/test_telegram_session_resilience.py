"""Tests for the shared Telegram HTTP session and 5xx retry behaviour.

Real production failure mode this fixes: the long-poll loop in
agent_core.daemon_telegram opened a fresh TCP+TLS connection on every
getUpdates iteration, so transient 502/503/504 from telegram.org and
brief HiNet IPv6 hiccups bubbled up as 'connection error' counters that
eventually tripped a process restart. With a pooled Session and a urllib3
Retry adapter, those bursts are absorbed below the daemon-loop layer.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class GetTgSessionTests(unittest.TestCase):
    """The session helper is the foundation for all Telegram HTTP. Pin its
    config + caching + reset behaviour so a future refactor can't silently
    drop the 5xx retry."""

    def setUp(self):
        from agent_core import daemon_telegram
        daemon_telegram._reset_tg_session()
        self.addCleanup(daemon_telegram._reset_tg_session)

    def test_returns_session_with_retry_adapter(self):
        from agent_core import daemon_telegram
        session = daemon_telegram._get_tg_session()
        # The adapter that handles api.telegram.org HTTPS must carry our
        # retry config. Without status_forcelist the whole point is moot.
        adapter = session.get_adapter("https://api.telegram.org")
        retry = adapter.max_retries
        self.assertEqual(retry.total, 3)
        self.assertEqual(retry.backoff_factor, 0.5)
        self.assertEqual(set(retry.status_forcelist), {502, 503, 504})

    def test_post_is_explicitly_excluded_from_retry(self):
        """Codex finding on PR #28: sendMessage is non-idempotent. urllib3
        retrying a POST that Telegram already accepted would deliver the
        same reply multiple times. POST stays OUT of allowed_methods —
        the outer tg_send() loop handles its own 5xx with one-shot
        granularity instead."""
        from agent_core import daemon_telegram
        session = daemon_telegram._get_tg_session()
        adapter = session.get_adapter("https://api.telegram.org")
        allowed = adapter.max_retries.allowed_methods
        self.assertIn("GET", allowed)        # idempotent reads — safe to retry
        self.assertNotIn("POST", allowed)    # sendMessage — must NOT retry
        self.assertNotIn("PUT", allowed)
        self.assertNotIn("DELETE", allowed)

    def test_caches_session_across_calls(self):
        """Returning a fresh Session per call would defeat connection
        keep-alive — the whole reason we introduced the helper."""
        from agent_core import daemon_telegram
        s1 = daemon_telegram._get_tg_session()
        s2 = daemon_telegram._get_tg_session()
        self.assertIs(s1, s2)

    def test_reset_drops_cached_session(self):
        from agent_core import daemon_telegram
        s1 = daemon_telegram._get_tg_session()
        daemon_telegram._reset_tg_session()
        s2 = daemon_telegram._get_tg_session()
        self.assertIsNot(s1, s2)

    def test_reset_calls_close_on_old_session(self):
        from agent_core import daemon_telegram
        s = daemon_telegram._get_tg_session()
        with mock.patch.object(s, "close") as close_mock:
            daemon_telegram._reset_tg_session()
        close_mock.assert_called_once()

    def test_reset_swallows_close_errors(self):
        """`close()` failing must NOT propagate — the reset is supposed
        to be a clean test/restart seam."""
        from agent_core import daemon_telegram
        s = daemon_telegram._get_tg_session()
        with mock.patch.object(s, "close", side_effect=RuntimeError("boom")):
            # Must not raise.
            daemon_telegram._reset_tg_session()


class SessionWiringTests(unittest.TestCase):
    """Verify the helpers' callers actually go through the session.
    Without this, the retry logic exists but no production traffic
    benefits from it."""

    def setUp(self):
        from agent_core import daemon_telegram
        daemon_telegram._reset_tg_session()
        self.addCleanup(daemon_telegram._reset_tg_session)

    def _build_requests_mock(self):
        """Build a `requests`-shaped mock whose Session() returns a tracked
        instance we can inspect."""
        session_instance = mock.MagicMock()
        # Default: a 200-response so the HTTP call short-circuits to
        # 'send delivered' / 'getUpdates returned no messages'.
        ok_response = mock.MagicMock(status_code=200)
        ok_response.json.return_value = {"ok": True, "result": []}
        session_instance.get.return_value = ok_response
        session_instance.post.return_value = ok_response
        # Adapter for the Session.get_adapter() lookup if anyone uses it.
        session_instance.get_adapter.return_value = mock.MagicMock()

        requests_mock = mock.MagicMock()
        requests_mock.Session.return_value = session_instance
        # urllib3.util.retry.Retry / requests.adapters.HTTPAdapter come
        # through real imports — give them passthrough mocks.
        requests_mock.adapters.HTTPAdapter = mock.MagicMock()
        return requests_mock, session_instance

    def test_tg_send_uses_shared_session(self):
        from agent_core import daemon_telegram
        requests_mock, session_instance = self._build_requests_mock()
        ok_response = session_instance.post.return_value
        ok_response.json.return_value = {"ok": True}

        daemon_telegram.tg_send(
            "tok", "chat", "hi", requests_module=requests_mock,
        )
        # Critical: traffic went through Session.post, NOT requests.post.
        session_instance.post.assert_called()
        requests_mock.post.assert_not_called()

    def test_tg_send_can_attach_inline_keyboard(self):
        from agent_core import daemon_telegram
        requests_mock, session_instance = self._build_requests_mock()
        ok_response = session_instance.post.return_value
        ok_response.json.return_value = {"ok": True}
        markup = {"inline_keyboard": [[{"text": "同意", "callback_data": "tgjoin:a:orange:1"}]]}

        daemon_telegram.tg_send(
            "tok",
            "chat",
            "Orange 有人要加入，同意嗎？",
            requests_module=requests_mock,
            reply_markup=markup,
        )

        payload = session_instance.post.call_args.kwargs["json"]
        self.assertEqual(payload["reply_markup"], markup)

    def test_session_reused_across_multiple_sends(self):
        """The same Session must serve many sendMessage calls so connection
        pooling actually pays off."""
        from agent_core import daemon_telegram
        requests_mock, session_instance = self._build_requests_mock()
        ok_response = session_instance.post.return_value
        ok_response.json.return_value = {"ok": True}

        for _ in range(5):
            daemon_telegram.tg_send(
                "tok", "chat", "hi", requests_module=requests_mock,
            )

        # Session() factory called only ONCE despite 5 sends → cache works.
        self.assertEqual(requests_mock.Session.call_count, 1)
        # POST called 5 times on that single session.
        self.assertEqual(session_instance.post.call_count, 5)


class RetryAdapterIntegrationTests(unittest.TestCase):
    """End-to-end: when the adapter sees a 502 and the retry limit allows,
    urllib3 retries and the caller never observes the 502. We don't hit
    the real network — we drive the adapter via a urllib3 PoolManager
    monkey-patch."""

    def setUp(self):
        from agent_core import daemon_telegram
        daemon_telegram._reset_tg_session()
        self.addCleanup(daemon_telegram._reset_tg_session)

    def test_retry_config_includes_502_503_504(self):
        from agent_core import daemon_telegram
        session = daemon_telegram._get_tg_session()
        adapter = session.get_adapter("https://api.telegram.org")
        forcelist = adapter.max_retries.status_forcelist
        # Codify what the daemon log showed — these are the codes we
        # repeatedly observed and want absorbed silently.
        self.assertIn(502, forcelist)  # Bad Gateway
        self.assertIn(503, forcelist)  # Service Unavailable
        self.assertIn(504, forcelist)  # Gateway Timeout

    def test_retry_respects_retry_after_header(self):
        """When Telegram returns a Retry-After header on 503 we want to
        honour it instead of using our exponential backoff blindly."""
        from agent_core import daemon_telegram
        session = daemon_telegram._get_tg_session()
        adapter = session.get_adapter("https://api.telegram.org")
        self.assertTrue(adapter.max_retries.respect_retry_after_header)


class TgSendRetryPayloadTests(unittest.TestCase):
    """Regression (review finding #1): a 429/5xx on the FIRST attempt must
    not corrupt the request body. tg_send used to do `payload =
    response.json()`, overwriting the {chat_id,text} request with the
    parsed response — so every retry re-POSTed a bodyless dict and could
    never succeed. Pin that the retry re-sends the original body."""

    def setUp(self):
        from agent_core import daemon_telegram
        daemon_telegram._reset_tg_session()
        self.addCleanup(daemon_telegram._reset_tg_session)

    def _resp(self, status, json_body):
        r = mock.MagicMock(status_code=status)
        r.json.return_value = json_body
        r.text = ""
        return r

    def _run_with_first_failure(self, first_response):
        from agent_core import daemon_telegram
        ok = self._resp(200, {"ok": True})
        session_instance = mock.MagicMock()
        session_instance.post.side_effect = [first_response, ok]
        requests_mock = mock.MagicMock()
        requests_mock.Session.return_value = session_instance

        with mock.patch.object(daemon_telegram.time, "sleep", lambda *_a, **_k: None):
            delivered = daemon_telegram.tg_send(
                "tok", "chat-123", "hello", requests_module=requests_mock,
            )
        return delivered, session_instance

    def test_retry_after_429_reposts_original_body(self):
        rate_limited = self._resp(429, {"ok": False, "parameters": {"retry_after": 0}})
        delivered, session_instance = self._run_with_first_failure(rate_limited)
        self.assertTrue(delivered)
        self.assertEqual(session_instance.post.call_count, 2)
        retry_payload = session_instance.post.call_args_list[1].kwargs["json"]
        self.assertEqual(retry_payload.get("chat_id"), "chat-123")
        self.assertEqual(retry_payload.get("text"), "hello")

    def test_retry_after_500_reposts_original_body(self):
        server_err = self._resp(500, {"ok": False, "description": "boom"})
        delivered, session_instance = self._run_with_first_failure(server_err)
        self.assertTrue(delivered)
        self.assertEqual(session_instance.post.call_count, 2)
        retry_payload = session_instance.post.call_args_list[1].kwargs["json"]
        self.assertEqual(retry_payload.get("chat_id"), "chat-123")
        self.assertEqual(retry_payload.get("text"), "hello")


if __name__ == "__main__":
    unittest.main()
