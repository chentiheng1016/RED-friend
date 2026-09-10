from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException


def _make_request(session: dict) -> SimpleNamespace:
    return SimpleNamespace(session=session, headers={})


class CsrfHelperTests(unittest.TestCase):
    def test_get_csrf_token_creates_and_persists(self):
        from agent_core.web_server import app as app_module

        session: dict = {}
        request = _make_request(session)

        token = app_module._get_csrf_token(request)

        self.assertTrue(token)
        self.assertEqual(session[app_module._CSRF_SESSION_KEY], token)
        # Second call must return the same token, not rotate it.
        self.assertEqual(app_module._get_csrf_token(request), token)

    def test_require_csrf_rejects_empty_session_token(self):
        from agent_core.web_server import app as app_module

        request = _make_request({})

        with self.assertRaises(HTTPException) as ctx:
            app_module._require_csrf(request, "anything")

        self.assertEqual(ctx.exception.status_code, 403)

    def test_require_csrf_rejects_mismatched_token(self):
        from agent_core.web_server import app as app_module

        request = _make_request({app_module._CSRF_SESSION_KEY: "expected"})

        with self.assertRaises(HTTPException) as ctx:
            app_module._require_csrf(request, "wrong")

        self.assertEqual(ctx.exception.status_code, 403)

    def test_require_csrf_accepts_matching_token(self):
        from agent_core.web_server import app as app_module

        token = "csrf-value"
        request = _make_request({app_module._CSRF_SESSION_KEY: token})

        # No exception means accepted.
        app_module._require_csrf(request, token)


class CsrfEndpointRejectionTests(unittest.TestCase):
    """Hit the live routes through TestClient to make sure the CSRF gate is wired."""

    def _client(self):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module

        # Pin a fixed session key so cookies are signable and middleware initializes.
        self._patches = [
            mock.patch.object(app_module, "get_secret_key", return_value="x" * 32),
            mock.patch.object(
                app_module,
                "_get_session_user",
                return_value={
                    "email": "boss@example.com",
                    "name": "Boss",
                    "color": "red",
                    "is_boss": "True",
                },
            ),
            # _require_login 會對 registry 重驗；回一個 color=red 員工讓 boss 身分維持。
            mock.patch.object(
                app_module,
                "get_employee",
                return_value={"email": "boss@example.com", "name": "Boss", "color": "red"},
            ),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop_patches)
        return TestClient(app_module.app)

    def _stop_patches(self):
        for patch in self._patches:
            patch.stop()

    def test_admin_employees_post_rejects_without_csrf_token(self):
        client = self._client()

        response = client.post(
            "/admin/employees",
            data={"email": "alice@example.com", "name": "Alice", "color": "green"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

    def test_admin_employees_post_rejects_wrong_csrf_token(self):
        client = self._client()

        response = client.post(
            "/admin/employees",
            data={
                "email": "alice@example.com",
                "name": "Alice",
                "color": "green",
                "csrf_token": "definitely-not-the-right-value",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

    def test_api_query_rejects_without_csrf_header(self):
        client = self._client()

        response = client.post(
            "/api/dept/orange/query",
            json={"intent": "query.customer_360", "payload": {}},
        )

        self.assertEqual(response.status_code, 403)

    def test_api_command_rejects_without_csrf_header(self):
        client = self._client()

        response = client.post(
            "/api/dept/orange/command",
            json={"intent": "command.something", "payload": {}},
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
