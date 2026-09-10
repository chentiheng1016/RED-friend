"""健檢 Low batch 2（security/bounds）：
- tg_auth check_message_rate_limit 的 GC 用呼叫計數器（非 id(cid)&0xF，macOS 恆 0）
- tg_auth RED_BLOCK_TOOL 降級 fallback 用 glob（對齊 policy_engine + 文件 delete_*）
- orange customer_360 Gmail query 去掉雙引號（堵運算子注入）
- web 確認 token store 加硬上限（非只逐出過期）
"""
import inspect
import unittest
from unittest import mock


class TgAuthHardeningTests(unittest.TestCase):
    def test_rate_limit_gc_uses_counter_not_id(self):
        import agent_core.tg_auth as tg
        src = inspect.getsource(tg.check_message_rate_limit)
        self.assertIn("if _gc_call_counter == 0:", src)  # GC gated on the amortised counter
        self.assertNotIn("(id(cid) & 0xF)", src)  # the buggy always-0-on-macOS gate is gone

    def test_red_block_tool_fallback_uses_glob(self):
        import agent_core.tg_auth as tg
        src = inspect.getsource(tg)
        self.assertIn("fnmatch.fnmatch(fn.__name__", src)


class CustomerQueryInjectionTests(unittest.TestCase):
    def test_quote_injection_stays_inside_phrase(self):
        from agent_core.agents.orange_sales import customer_intel as ci
        captured = {}

        def fake_list(userId, q, maxResults):  # noqa: N803 — mirrors gmail API
            captured["q"] = q
            r = mock.MagicMock()
            r.execute.return_value = {"messages": []}
            return r

        fake = mock.MagicMock()
        fake.users.return_value.messages.return_value.list.side_effect = fake_list
        with mock.patch.object(ci, "get_service", return_value=fake):
            ci._customer_360_gmail_summary('" from:ceo@x.com newer_than:1d "', days=7, max_threads=5)

        q = captured["q"]
        # exactly the wrapping pair of quotes → caller injected none → no operator breakout
        self.assertEqual(q.count('"'), 2)
        self.assertTrue(q.startswith('"'))
        self.assertIn('" newer_than:7d', q)


class ConfirmTokenStoreCapTests(unittest.TestCase):
    def test_store_hard_capped(self):
        from agent_core.web_server import app as app_module
        app_module._PENDING_COMMANDS.clear()
        try:
            for i in range(app_module._MAX_PENDING_COMMANDS + 50):
                app_module._issue_confirm_token(f"command.x{i}", f"fp{i}")
            self.assertLessEqual(len(app_module._PENDING_COMMANDS), app_module._MAX_PENDING_COMMANDS)
        finally:
            app_module._PENDING_COMMANDS.clear()


if __name__ == "__main__":
    unittest.main()
