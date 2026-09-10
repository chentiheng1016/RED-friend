"""/api/dept/{color}/ask — 員工自然語言查詢端點的整合測試（TestClient）。

跟 test_web_csrf 同一套 patch 手法：釘 session key、假 session user、假
registry；NL 引擎 patch 在真實路徑 agent_core.dept_nlp_query.answer_dept_question
（app.py 是 handler 內 lazy import，patch 來源模組才會生效）。
"""
from __future__ import annotations

import unittest
from unittest import mock


def _app_module():
    from agent_core.web_server import app as app_module
    return app_module


class AskEndpointTests(unittest.TestCase):
    def _client(self, *, color="orange", is_boss=False):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module

        email = "boss@example.com" if is_boss else "emp@example.com"
        self._patches = [
            mock.patch.object(app_module, "get_secret_key", return_value="x" * 32),
            mock.patch.object(
                app_module,
                "_get_session_user",
                return_value={
                    "email": email,
                    "name": "Boss" if is_boss else "Alice",
                    "color": color,
                    "is_boss": str(is_boss),
                },
            ),
            mock.patch.object(
                app_module,
                "get_employee",
                return_value={"email": email, "name": "n", "color": color},
            ),
            # session registry gate 會寫 live var/ — 測試一律短路
            mock.patch.object(app_module, "_session_gate_or_none", return_value=None),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop_patches)
        return TestClient(app_module.app)

    def _stop_patches(self):
        for patch in self._patches:
            patch.stop()

    def _csrf(self, client):
        from agent_core.web_server import app as app_module
        # 直接對 session 塞 token 不可行（TestClient cookie 簽章）——改 patch
        # _require_csrf 短路；CSRF 拒絕行為已有專屬測試。
        patch = mock.patch.object(app_module, "_require_csrf", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        return client

    def test_rejects_without_csrf(self):
        client = self._client()
        resp = client.post("/api/dept/orange/ask", json={"question": "查 PAX"})
        self.assertEqual(resp.status_code, 403)

    def test_rejects_other_department(self):
        client = self._csrf(self._client(color="orange"))
        resp = client.post("/api/dept/purple/ask", json={"question": "查發票"})
        self.assertEqual(resp.status_code, 403)

    def test_rejects_red_page(self):
        client = self._csrf(self._client(color="red", is_boss=True))
        resp = client.post("/api/dept/red/ask", json={"question": "查全部"})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_empty_question(self):
        client = self._csrf(self._client())
        resp = client.post("/api/dept/orange/ask", json={"question": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_oversized_question(self):
        client = self._csrf(self._client())
        resp = client.post("/api/dept/orange/ask", json={"question": "問" * 2001})
        self.assertEqual(resp.status_code, 400)

    def test_happy_path_uses_page_color(self):
        client = self._csrf(self._client(color="orange"))
        with mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            return_value="PAX 最新報價 2026-07-01。",
        ) as m:
            resp = client.post("/api/dept/orange/ask", json={"question": "PAX 報價？"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["text"], "PAX 最新報價 2026-07-01。")
        args, kwargs = m.call_args
        self.assertEqual(args[0], "orange")
        self.assertEqual(args[1], "PAX 報價？")
        self.assertEqual(kwargs.get("channel"), "web")

    def test_rate_limited_returns_429(self):
        client = self._csrf(self._client(color="orange"))

        class _DenyLimiter:
            def allow(self, key):
                return False

        with mock.patch.object(
            _app_module(), "_get_ask_rate_limiter", return_value=_DenyLimiter(),
        ), mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
        ) as nlp:
            resp = client.post("/api/dept/orange/ask", json={"question": "查 PAX"})
        self.assertEqual(resp.status_code, 429)
        nlp.assert_not_called()

    def test_boss_exempt_from_rate_limit(self):
        client = self._csrf(self._client(color="red", is_boss=True))

        class _DenyLimiter:
            def allow(self, key):
                return False

        with mock.patch.object(
            _app_module(), "_get_ask_rate_limiter", return_value=_DenyLimiter(),
        ), mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            return_value="OK",
        ):
            resp = client.post("/api/dept/indigo/ask", json={"question": "庫存"})
        self.assertEqual(resp.status_code, 200)

    def test_boss_asks_as_dept_color(self):
        # boss 開 indigo 部門頁 → 以 indigo 身分查（least privilege），不是 red。
        client = self._csrf(self._client(color="red", is_boss=True))
        with mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            return_value="庫存 OK",
        ) as m:
            resp = client.post("/api/dept/indigo/ask", json={"question": "庫存狀況"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(m.call_args[0][0], "indigo")


if __name__ == "__main__":
    unittest.main()
