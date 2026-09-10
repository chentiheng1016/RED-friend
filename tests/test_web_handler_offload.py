"""健檢 Medium regression: api_query / api_command 的 middleware.dispatch（阻塞的
Gmail/Drive/Gemini I/O，可達數分鐘）改用 run_in_threadpool offload，否則一個慢請求卡死
單一 web event loop、凍住所有並發請求。驗證 success-path 仍正確回傳 dispatch 結果，且兩個
handler 確實走 threadpool。
"""
import inspect
import unittest
from unittest import mock


class ApiDispatchOffloadTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module
        self._patches = [
            mock.patch.object(app_module, "get_secret_key", return_value="x" * 32),
            mock.patch.object(app_module, "_get_session_user", return_value={
                "email": "boss@example.com", "name": "Boss", "color": "red", "is_boss": "True"}),
            # _require_login 對 registry 重驗；回 color=red 員工維持 boss 身分。
            mock.patch.object(app_module, "get_employee", return_value={
                "email": "boss@example.com", "name": "Boss", "color": "red"}),
            mock.patch.object(app_module, "_require_csrf", return_value=None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        return TestClient(app_module.app)

    def test_api_query_returns_dispatch_result_through_threadpool(self):
        fake_mw = mock.MagicMock()
        fake_mw.dispatch.return_value = {"answer": 42}
        with mock.patch("agent_core.agents.wire.get_default_registry", return_value=(None, fake_mw)):
            client = self._client()
            resp = client.post(
                "/api/dept/orange/query",
                json={"intent": "query.customer_360", "payload": {}},
                headers={"x-csrf-token": "ok"},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"ok": True, "result": {"answer": 42}})
        self.assertTrue(fake_mw.dispatch.called)

    def test_both_dept_handlers_offload_to_threadpool(self):
        from agent_core.web_server import app as app_module
        src = inspect.getsource(app_module)
        self.assertIn("run_in_threadpool(_dispatch)", src)
        # exactly the two dept handlers (query + command) offload their dispatch
        self.assertEqual(src.count("await run_in_threadpool(_dispatch)"), 2)


class LineWebhookOffloadTests(unittest.TestCase):
    """健檢 Low：LINE webhook 的 handle_line_webhook 內部是同步 requests.post
    （LINE reply API）— 直接在 async handler 跑會卡死單一 event loop。改
    run_in_threadpool 後：成功結果照樣回、LineWebhookError 照樣轉 4xx JSON。"""

    def _client(self):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module
        app_module._line_webhook_limiter = None  # type: ignore[attr-defined]
        p = mock.patch.object(app_module, "get_secret_key", return_value="x" * 32)
        p.start()
        self.addCleanup(p.stop)
        return TestClient(app_module.app)

    def test_webhook_source_offloads_handler(self):
        import inspect as _inspect
        from agent_core.web_server import app as app_module
        src = _inspect.getsource(app_module.line_webhook)
        self.assertIn("run_in_threadpool(handle_line_webhook", src)

    def test_webhook_success_result_through_threadpool(self):
        with mock.patch(
            "agent_core.line_bot.handle_line_webhook",
            return_value={"ok": True, "events": 1, "replies": 1, "reply_errors": []},
        ) as handler:
            client = self._client()
            r = client.post("/line/webhook", content=b'{"events":[]}',
                            headers={"x-line-signature": "sig"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        handler.assert_called_once()
        args = handler.call_args.args
        self.assertEqual(args[0], b'{"events":[]}')
        self.assertEqual(args[1], "sig")

    def test_webhook_error_still_maps_to_status_code(self):
        from agent_core.line_bot import LineWebhookError
        with mock.patch(
            "agent_core.line_bot.handle_line_webhook",
            side_effect=LineWebhookError("bad signature", status_code=403),
        ):
            client = self._client()
            r = client.post("/line/webhook", content=b"{}",
                            headers={"x-line-signature": "bad"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("bad signature", r.text)


class AdminTelegramOffloadTests(unittest.TestCase):
    """健檢 Low：/admin/telegram 的重活（audit tail 讀 / policy rows / binding
    診斷的 registry+keyring I/O）全部丟 threadpool，不佔住 event loop。"""

    def test_admin_page_source_offloads_heavy_calls(self):
        import inspect as _inspect
        from agent_core.web_server import app as app_module
        src = _inspect.getsource(app_module.admin_telegram_page)
        self.assertIn("run_in_threadpool(read_recent_events", src)
        self.assertIn("run_in_threadpool(telegram_command_policy_rows)", src)
        self.assertIn("run_in_threadpool(_build_binding_diagnostics)", src)


if __name__ == "__main__":
    unittest.main()
