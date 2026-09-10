from __future__ import annotations

import asyncio
import unittest
from unittest import mock


class WebSessionMiddlewareTests(unittest.TestCase):
    def test_health_routes_bypass_session_secret(self):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module

        with mock.patch.object(
            app_module,
            "get_secret_key",
            side_effect=RuntimeError("keychain down"),
        ):
            client = TestClient(app_module.app)
            for path in ("/health", "/healthz"):
                with self.subTest(path=path):
                    response = client.get(path)

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"ok": True})

    def test_line_webhook_bypasses_session_secret(self):
        from fastapi.testclient import TestClient
        from agent_core import line_bot
        from agent_core.web_server import app as app_module

        with mock.patch.object(
            app_module,
            "get_secret_key",
            side_effect=RuntimeError("keychain down"),
        ), mock.patch.object(
            line_bot,
            "handle_line_webhook",
            return_value={"ok": True, "events": 0, "replies": 0, "reply_errors": []},
        ):
            client = TestClient(app_module.app)
            response = client.post("/line/webhook", content=b'{"events":[]}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)

    def test_secret_resolution_failure_returns_clear_500(self):
        from agent_core.web_server import app as app_module

        async def inner_app(_scope, _receive, _send):
            raise AssertionError("inner app should not run without session middleware")

        middleware = app_module._LazySessionMiddleware(
            inner_app,
            max_age=60,
            https_only=False,
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        with mock.patch.object(
            app_module,
            "get_secret_key",
            side_effect=RuntimeError("keychain down"),
        ), self.assertLogs("agent_core.web_server.app", level="ERROR") as logs:
            asyncio.run(middleware(scope, receive, send))

        status = next(m for m in messages if m["type"] == "http.response.start")["status"]
        body = b"".join(
            m.get("body", b"")
            for m in messages
            if m["type"] == "http.response.body"
        ).decode("utf-8")

        self.assertEqual(status, 500)
        self.assertIn("WEB_SECRET_KEY", body)
        self.assertIn("Failed to initialize session middleware", "\n".join(logs.output))


class WebLoginRedirectTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module

        patch = mock.patch.object(app_module, "get_secret_key", return_value="x" * 32)
        patch.start()
        self.addCleanup(patch.stop)
        return TestClient(app_module.app)

    def test_admin_page_redirects_to_login_page_when_logged_out(self):
        client = self._client()

        response = client.get("/admin/employees", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/?next=/admin/employees")

    def test_login_page_preserves_next_target(self):
        client = self._client()

        response = client.get("/?next=/admin/employees")

        self.assertEqual(response.status_code, 200)
        self.assertIn('/auth/login?next=%2Fadmin%2Femployees', response.text)

    def test_external_next_url_is_ignored(self):
        client = self._client()

        response = client.get("/?next=https://example.com/admin")

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/auth/login"', response.text)
        self.assertNotIn("example.com", response.text)


if __name__ == "__main__":
    unittest.main()
