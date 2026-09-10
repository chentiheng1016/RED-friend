import json
import os
import tempfile
import types
import unittest
from unittest import mock

from agent_core.web_access_guard import (
    assess_web_access,
    web_access_diagnose,
    web_access_diagnose_json,
    web_domain_policy_clear,
    web_domain_policy_list,
    web_domain_policy_set,
)


class WebAccessGuardTests(unittest.TestCase):
    def test_detects_cloudflare_challenge(self):
        html = """
        <html>
          <head><title>Just a moment...</title></head>
          <body>Checking your browser before accessing example.com</body>
        </html>
        """

        result = assess_web_access(
            url="https://example.com",
            final_url="https://example.com",
            http_status=503,
            body=html,
        )

        self.assertEqual(result.status, "blocked_by_waf")
        self.assertFalse(result.can_extract)
        self.assertIn("cloudflare_title", result.signals)
        self.assertIn("cloudflare_checking_browser", result.signals)

    def test_detects_turnstile_as_captcha_required(self):
        html = """
        <html>
          <body>
            <div class="cf-turnstile"
                 data-sitekey="redacted"></div>
            <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
          </body>
        </html>
        """

        result = assess_web_access(
            url="https://example.com",
            final_url="https://example.com",
            http_status=200,
            body=html,
        )

        self.assertEqual(result.status, "captcha_required")
        self.assertFalse(result.can_extract)
        self.assertIn("turnstile_widget", result.signals)

    def test_detects_rate_limit(self):
        result = assess_web_access(
            url="https://example.com",
            final_url="https://example.com",
            http_status=429,
            body="Too Many Requests",
            headers={"Retry-After": "120"},
        )

        self.assertEqual(result.status, "rate_limited")
        self.assertFalse(result.can_extract)
        self.assertEqual(result.retry_after, "120")
        self.assertEqual(result.retry_after_seconds, 120)

    def test_allows_normal_page(self):
        result = assess_web_access(
            url="https://example.com",
            final_url="https://example.com",
            http_status=200,
            body="<html><title>Hello</title><body>Welcome</body></html>",
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.can_extract)
        self.assertEqual(result.title, "Hello")

    def test_diagnose_rejects_non_http_urls(self):
        result = web_access_diagnose("file:///etc/passwd")

        self.assertIn("URL 只支援完整的 http/https", result)

    def test_diagnose_uses_compliant_user_agent_and_reports_block(self):
        response = types.SimpleNamespace(
            status_code=403,
            url="https://example.com/",
            headers={"Content-Type": "text/html"},
            text="<html><title>Attention Required! | Cloudflare</title></html>",
            apparent_encoding="utf-8",
            encoding="utf-8",
            is_redirect=False,
            is_permanent_redirect=False,
        )

        with mock.patch("agent_core.web_access_guard.assert_url_is_public", lambda *a, **k: None), \
             mock.patch("agent_core.web_access_guard.requests.get", return_value=response) as get:
            result = web_access_diagnose("https://example.com")

        self.assertIn("blocked_by_waf", result)
        headers = get.call_args.kwargs["headers"]
        self.assertIn("RED-Agent/1.0", headers["User-Agent"])
        self.assertNotIn("Mozilla", headers["User-Agent"])

    def test_diagnose_caps_streamed_response_body(self):
        class FakeResponse:
            status_code = 200
            url = "https://example.com/"
            headers = {"Content-Type": "text/html; charset=utf-8"}
            encoding = "utf-8"
            is_redirect = False
            is_permanent_redirect = False

            def __init__(self):
                self.closed = False

            def iter_content(self, chunk_size=65536, decode_unicode=False):
                yield b"<html><title>Hello</title><body>"
                yield b"x" * 128

            def close(self):
                self.closed = True

        response = FakeResponse()

        with mock.patch("agent_core.web_access_guard._MAX_RESPONSE_BYTES", 64), \
             mock.patch("agent_core.web_access_guard.assert_url_is_public", lambda *a, **k: None), \
             mock.patch("agent_core.web_access_guard.requests.get", return_value=response):
            result = web_access_diagnose("https://example.com")

        self.assertIn("ok", result)
        self.assertIn("內容樣本：已限制", result)
        self.assertTrue(response.closed)

    def test_diagnose_json_returns_structured_retry_after(self):
        response = types.SimpleNamespace(
            status_code=429,
            url="https://example.com/",
            headers={"Content-Type": "text/html", "Retry-After": "60"},
            text="Too Many Requests",
            encoding="utf-8",
            is_redirect=False,
            is_permanent_redirect=False,
        )

        with mock.patch("agent_core.web_access_guard.assert_url_is_public", lambda *a, **k: None), \
             mock.patch("agent_core.web_access_guard.requests.get", return_value=response):
            payload = json.loads(web_access_diagnose_json("https://example.com"))

        self.assertEqual(payload["status"], "rate_limited")
        self.assertEqual(payload["retry_after"], "60")
        self.assertEqual(payload["retry_after_seconds"], 60)
        self.assertFalse(payload["can_extract"])

    def test_domain_policy_blocks_before_network_request(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch(
                 "agent_core.web_access_guard._DOMAIN_POLICY_FILE",
                 os.path.join(tmpdir, "policy.json"),
             ):
            self.assertIn(
                "已設定",
                web_domain_policy_set(
                    "example.com",
                    "needs_api",
                    "Use the vendor API only.",
                ),
            )
            self.assertIn("example.com: needs_api", web_domain_policy_list())

            with mock.patch("agent_core.web_access_guard.requests.get") as get:
                payload = json.loads(web_access_diagnose_json("https://sub.example.com/path"))

            get.assert_not_called()
            self.assertEqual(payload["status"], "needs_api")
            self.assertEqual(payload["domain_policy"], "needs_api")
            self.assertEqual(payload["policy_domain"], "example.com")
            self.assertIn("vendor API", payload["next_action"])

            self.assertIn("已移除", web_domain_policy_clear("example.com"))
            self.assertIn("尚未設定", web_domain_policy_list())


class SSRFGuardTests(unittest.TestCase):
    """assert_url_is_public 必須擋掉內網/loopback/link-local/metadata 與非 http(s)。"""

    def test_blocks_internal_and_reserved_ips(self):
        from agent_core.web_access_guard import SSRFBlockedError, assert_url_is_public
        # 用字面 IP：getaddrinfo 對字面 IP 免真實 DNS，測試離線可跑。
        blocked = [
            "http://127.0.0.1:8000/",       # 本機共用 Chroma
            "http://169.254.169.254/latest/meta-data/",  # 雲端 metadata
            "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
            "http://[::1]/", "http://0.0.0.0/",
        ]
        for url in blocked:
            with self.assertRaises(SSRFBlockedError, msg=url):
                assert_url_is_public(url)

    def test_blocks_non_http_schemes(self):
        from agent_core.web_access_guard import SSRFBlockedError, assert_url_is_public
        for url in ("file:///etc/passwd", "ftp://host/x", "gopher://x/", "data:text/plain,x"):
            with self.assertRaises(SSRFBlockedError, msg=url):
                assert_url_is_public(url)

    def test_allows_public_literal_ip(self):
        from agent_core.web_access_guard import assert_url_is_public
        # 字面公開 IP：不需真實 DNS，也不該被擋。
        assert_url_is_public("http://8.8.8.8/")
        assert_url_is_public("https://1.1.1.1/")

    def test_ipv4_mapped_ipv6_loopback_blocked(self):
        from agent_core.web_access_guard import _ip_is_blocked
        self.assertTrue(_ip_is_blocked("::ffff:127.0.0.1"))
        self.assertTrue(_ip_is_blocked("169.254.169.254"))
        self.assertFalse(_ip_is_blocked("8.8.8.8"))

    def test_redirect_to_internal_is_blocked(self):
        # 公開 URL 302 到內網也要擋（逐跳重驗）。mock 讓第一跳放行、第二跳丟 SSRF。
        from unittest import mock
        from agent_core import web_access_guard as wag

        redirect_resp = types.SimpleNamespace(
            is_redirect=True, is_permanent_redirect=False,
            headers={"Location": "http://127.0.0.1:8000/x"},
            close=lambda: None,
        )
        with mock.patch.object(wag, "assert_url_is_public") as chk, \
             mock.patch.object(wag.requests, "get", return_value=redirect_resp) as get:
            chk.side_effect = [None, wag.SSRFBlockedError("blocked")]
            with self.assertRaises(wag.SSRFBlockedError):
                wag.guarded_requests_get("http://public.example/")
            self.assertEqual(get.call_count, 1)  # 第二跳在 fetch 前就被擋


class TelegramTokenRedactionTests(unittest.TestCase):
    def test_bot_token_in_requests_exception_is_redacted(self):
        from agent_core.log_redact import redact_log_line
        # 低熵假 token（避免 detect-secrets 誤判），仍符合 \d{8,10}:[A-Za-z0-9_-]{30,}。
        fake_auth = "A" * 36
        leak = (
            "HTTPSConnectionPool(host='api.telegram.org', port=443): "
            f"url: /bot8123456789:{fake_auth}/sendMessage"
        )
        out = redact_log_line(leak)
        self.assertNotIn(fake_auth, out)
        self.assertIn("TELEGRAM_BOT_TOKEN", out)


if __name__ == "__main__":
    unittest.main()
