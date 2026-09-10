"""wrap_sensitive_tool 的結果處理（健檢 Low ×2）。

1. budget：`record_use` 之前無條件執行 — 工具回 ToolResult.failure（沒 raise）
   也扣每日 budget。修法：`getattr(result, "ok", True)` 為 True 才記
   （plain string 結果視為成功，行為不變）。
2. token+warn（DANGEROUS 警示前綴）：`warn_prefix + str(result)` 會把 ToolResult
   壓平成普通 str — `.ok/.error_code/.data/.artifacts` 全丟，下游 daemon retry
   判斷 / dashboard 分類就瞎了。修法：用同一組 metadata 重建 ToolResult，
   只換文字表面。

隔離：tool_budgets 的 record_use / check_budget 全 mock（不寫 var/）；
tg_auth in-memory confirm state 每測自清；run_history 導 tmpdir —— 本檔的
工具都是 CONFIRM／DANGEROUS tier，會被 wrap_sensitive_tool 自動掛
@audited，不隔離就會把 fixture 假 run 寫進 live var/runs/index.jsonl，
metrics_overview() 的失敗率跟著虛高（見 tests/run_history_isolation.py）。
"""
import unittest
from unittest import mock

from agent_core.tool_result import ErrorCode, ToolResult
from tests.run_history_isolation import RunHistoryIsolationMixin


class _WrapBase(RunHistoryIsolationMixin, unittest.TestCase):
    def setUp(self):
        from agent_core import tg_auth
        self.tg_auth = tg_auth
        self._run_history_iso_setup()
        with tg_auth._state_lock:
            tg_auth._confirm_state.clear()
            tg_auth._dangerous_confirm_state.clear()

    def tearDown(self):
        self._run_history_iso_teardown()

    def _budget_mocks(self):
        return (
            mock.patch("agent_core.tool_budgets.check_budget",
                       return_value=(True, "")),
            mock.patch("agent_core.tool_budgets.record_use"),
        )


class BudgetOnlyOnSuccessTests(_WrapBase):
    def test_toolresult_failure_does_not_consume_budget(self):
        def fake_send_gmail(to, subject, body):
            return ToolResult.failure("Gmail API 掛了",
                                      error_code=ErrorCode.NETWORK)
        fake_send_gmail.__name__ = "send_gmail"  # CONFIRM tier

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_send_gmail, get_chat_id=lambda: "310001")
        self.tg_auth.mark_confirmed("310001")
        check_p, record_p = self._budget_mocks()
        with check_p, record_p as record_use:
            out = wrapped("a@b.c", "s", "b")
        self.assertFalse(getattr(out, "ok", True))
        record_use.assert_not_called()  # 失敗不扣 budget

    def test_plain_string_result_still_consumes_budget(self):
        def fake_send_gmail(to, subject, body):
            return "sent"
        fake_send_gmail.__name__ = "send_gmail"

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_send_gmail, get_chat_id=lambda: "310002")
        self.tg_auth.mark_confirmed("310002")
        check_p, record_p = self._budget_mocks()
        with check_p, record_p as record_use:
            out = wrapped("a@b.c", "s", "b")
        self.assertIn("sent", str(out))
        record_use.assert_called_once_with("send_gmail")

    def test_toolresult_success_consumes_budget(self):
        def fake_send_gmail(to, subject, body):
            return ToolResult.success("sent ok")
        fake_send_gmail.__name__ = "send_gmail"

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_send_gmail, get_chat_id=lambda: "310003")
        self.tg_auth.mark_confirmed("310003")
        check_p, record_p = self._budget_mocks()
        with check_p, record_p as record_use:
            wrapped("a@b.c", "s", "b")
        record_use.assert_called_once_with("send_gmail")


class TokenWarnPreservesToolResultTests(_WrapBase):
    def _wrap_dangerous(self, fn, cid):
        fn.__name__ = "run_shell"  # tier=DANGEROUS → telegram=token+warn
        wrapped = self.tg_auth.wrap_sensitive_tool(fn, get_chat_id=lambda: cid)
        self.tg_auth.mark_confirmed(cid)
        self.tg_auth.mark_dangerous_confirmed(cid)
        return wrapped

    def test_success_metadata_survives_warn_prefix(self):
        def fake(cmd):
            return ToolResult.success(
                "done", data={"rows": 3}, artifacts=["/tmp/out.xlsx"],
                warnings=["w1"], cost={"usd": 0.1},
            )
        wrapped = self._wrap_dangerous(fake, "310010")
        check_p, record_p = self._budget_mocks()
        with check_p, record_p:
            out = wrapped("ls")
        # 文字表面：警示前綴 + 原文
        self.assertIn("DANGEROUS", str(out))
        self.assertIn("done", str(out))
        # 結構化 metadata 不能被壓平掉
        self.assertIsInstance(out, ToolResult)
        self.assertTrue(out.ok)
        self.assertEqual(out.data, {"rows": 3})
        self.assertEqual(out.artifacts, ["/tmp/out.xlsx"])
        self.assertEqual(out.warnings, ["w1"])
        self.assertEqual(out.cost, {"usd": 0.1})

    def test_failure_metadata_survives_warn_prefix(self):
        def fake(cmd):
            return ToolResult.failure(
                "找不到檔案", error_code=ErrorCode.NOT_FOUND,
                suggested_fix="檢查路徑",
            )
        wrapped = self._wrap_dangerous(fake, "310011")
        check_p, record_p = self._budget_mocks()
        with check_p, record_p as record_use:
            out = wrapped("ls")
        self.assertIsInstance(out, ToolResult)
        self.assertFalse(out.ok)
        self.assertEqual(out.error_code, ErrorCode.NOT_FOUND)
        self.assertFalse(out.recoverable)
        self.assertIn("找不到檔案", str(out))
        self.assertIn("DANGEROUS", str(out))
        record_use.assert_not_called()  # 失敗也不扣 budget（同上）

    def test_plain_string_dangerous_result_unchanged(self):
        def fake(cmd):
            return "executed"
        wrapped = self._wrap_dangerous(fake, "310012")
        check_p, record_p = self._budget_mocks()
        with check_p, record_p:
            out = wrapped("ls")
        self.assertIn("DANGEROUS", out)
        self.assertIn("executed", out)


if __name__ == "__main__":
    unittest.main()
