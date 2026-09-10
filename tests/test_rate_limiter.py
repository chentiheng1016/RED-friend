from __future__ import annotations

import time
import unittest
from unittest import mock


class IPRateLimiterTests(unittest.TestCase):
    def test_burst_of_10_allows_first_ten_then_rejects(self):
        from agent_core.web_server.rate_limiter import IPRateLimiter

        rl = IPRateLimiter(rate=1.0, burst=10.0)
        for _ in range(10):
            self.assertTrue(rl.allow("1.2.3.4"))
        # 11th request within the same instant must be rejected.
        self.assertFalse(rl.allow("1.2.3.4"))

    def test_buckets_are_independent_per_key(self):
        from agent_core.web_server.rate_limiter import IPRateLimiter

        rl = IPRateLimiter(rate=1.0, burst=2.0)
        self.assertTrue(rl.allow("a"))
        self.assertTrue(rl.allow("a"))
        self.assertFalse(rl.allow("a"))  # a exhausted

        # b starts with its own full bucket.
        self.assertTrue(rl.allow("b"))
        self.assertTrue(rl.allow("b"))
        self.assertFalse(rl.allow("b"))

    def test_tokens_refill_over_time(self):
        from agent_core.web_server.rate_limiter import IPRateLimiter

        rl = IPRateLimiter(rate=10.0, burst=1.0)
        self.assertTrue(rl.allow("x"))
        self.assertFalse(rl.allow("x"))

        # Advance monotonic clock by 0.2s (≈ 2 tokens at rate=10).
        original_monotonic = time.monotonic
        with mock.patch.object(time, "monotonic", side_effect=lambda: original_monotonic() + 0.5):
            self.assertTrue(rl.allow("x"))

    def test_evicts_oldest_bucket_at_cap(self):
        from agent_core.web_server.rate_limiter import IPRateLimiter

        rl = IPRateLimiter(rate=1.0, burst=1.0)
        # Drop the soft cap so we don't have to generate 10k entries.
        rl._max_keys = 3

        for k in ("a", "b", "c"):
            rl.allow(k)
        self.assertEqual(len(rl._buckets), 3)

        # Adding "d" should evict "a" (oldest insertion).
        rl.allow("d")
        self.assertEqual(len(rl._buckets), 3)
        self.assertNotIn("a", rl._buckets)
        self.assertIn("d", rl._buckets)


class ClientIPTests(unittest.TestCase):
    def test_extracts_first_xff_entry(self):
        from agent_core.web_server.rate_limiter import client_ip

        ip = client_ip(
            [(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1, 10.0.0.2")],
        )
        self.assertEqual(ip, "203.0.113.5")

    def test_handles_single_xff_entry(self):
        from agent_core.web_server.rate_limiter import client_ip

        self.assertEqual(
            client_ip([(b"x-forwarded-for", b"198.51.100.7")]),
            "198.51.100.7",
        )

    def test_falls_back_when_no_xff(self):
        from agent_core.web_server.rate_limiter import client_ip

        self.assertEqual(
            client_ip([(b"content-type", b"application/json")], fallback="peer.example"),
            "peer.example",
        )

    def test_returns_unknown_when_no_headers_or_fallback(self):
        from agent_core.web_server.rate_limiter import client_ip

        self.assertEqual(client_ip(None), "unknown")
        self.assertEqual(client_ip([]), "unknown")


class LineWebhookIntegrationTests(unittest.TestCase):
    """End-to-end check that the LINE webhook actually consults the limiter
    and returns 429 once the burst is exhausted."""

    def test_429_after_burst_exhausted(self):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module

        # Fresh limiter so we don't inherit allowance from earlier tests.
        # Force the module-level cache to reset.
        app_module._line_webhook_limiter = None  # type: ignore[attr-defined]

        with mock.patch.object(
            app_module, "get_secret_key", return_value="x" * 32,
        ), mock.patch(
            "agent_core.line_bot.handle_line_webhook",
            return_value={"ok": True, "events": 0, "replies": 0, "reply_errors": []},
        ):
            client = TestClient(app_module.app)
            # The default burst is 10; the 11th request from the same IP
            # within a second must be rate-limited.
            headers = {"X-Forwarded-For": "192.0.2.99"}
            for _ in range(10):
                r = client.post("/line/webhook", content=b'{"events":[]}', headers=headers)
                self.assertEqual(r.status_code, 200)
            r = client.post("/line/webhook", content=b'{"events":[]}', headers=headers)
            self.assertEqual(r.status_code, 429)
            self.assertIn("rate limited", r.text)


if __name__ == "__main__":
    unittest.main()
