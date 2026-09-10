"""tg_auth 確認消費原子化（健檢 LOW：check→revoke 非原子）。

原本 wrap_sensitive_tool 的 token 路徑：check_confirmed()（讀）→ 執行 →
finally revoke_after_use()（寫）之間有窗口 —— 軟超時轉背景的舊推理線程與
新對話輪可在窗口內共用同一枚 one-shot 確認（一枚確認放行兩個動作）。

修法：consume_confirmation() 在 _state_lock 內 check+pop 一次完成；
wrap_sensitive_tool 的 token/token+warn 路徑改在執行前原子消費。消費語意
與舊「finally 必 revoke」等價：工具執行失敗（含 raise）不退還確認。

隔離：tg_auth in-memory state 每測自清（unittest 下 conftest autouse
fixture 不生效，見 CLAUDE.md）；tool_budgets 全 mock 不寫 var/；run_history
導 tmpdir —— send_gmail / run_shell 是 CONFIRM／DANGEROUS tier，
wrap_sensitive_tool 會自動掛 @audited，不隔離就會把 fixture 假 run 寫進
live var/runs/index.jsonl（見 tests/run_history_isolation.py）。
"""
import threading
import time
import unittest
from unittest import mock

from tests.run_history_isolation import RunHistoryIsolationMixin


class _ConsumeBase(RunHistoryIsolationMixin, unittest.TestCase):
    def setUp(self):
        from agent_core import tg_auth
        self.tg_auth = tg_auth
        self._run_history_iso_setup()
        with tg_auth._state_lock:
            tg_auth._confirm_state.clear()
            tg_auth._dangerous_confirm_state.clear()
            tg_auth._confirm_history.clear()
            tg_auth._lockout_until.clear()

    def tearDown(self):
        self._run_history_iso_teardown()

    def _budget_mocks(self):
        return (
            mock.patch("agent_core.tool_budgets.check_budget",
                       return_value=(True, "")),
            mock.patch("agent_core.tool_budgets.record_use"),
        )


class ConsumeConfirmationTests(_ConsumeBase):
    def test_consume_pops_state_second_call_fails(self):
        cid = "510001"
        self.tg_auth.mark_confirmed(cid)
        ok1, elapsed1 = self.tg_auth.consume_confirmation(cid)
        self.assertTrue(ok1)
        self.assertGreaterEqual(elapsed1, 0.0)
        ok2, elapsed2 = self.tg_auth.consume_confirmation(cid)
        self.assertFalse(ok2)
        self.assertEqual(elapsed2, -1.0)
        # check_confirmed 也看不到（已消費）
        self.assertFalse(self.tg_auth.check_confirmed(cid)[0])

    def test_consume_also_revokes_dangerous_confirm(self):
        # one-shot 語意對齊 revoke_after_use：一併撤 DANGEROUS 二次確認
        cid = "510002"
        self.tg_auth.mark_confirmed(cid)
        self.tg_auth.mark_dangerous_confirmed(cid)
        self.assertTrue(self.tg_auth.consume_confirmation(cid)[0])
        self.assertFalse(self.tg_auth.check_dangerous_confirmed(cid)[0])

    def test_group_composite_scope_same_path(self):
        # 群組 scope（"<chat_id>:<from_id>"）與私聊 scope 走同一路
        for cid in ("-100123:456", "510003"):
            self.tg_auth.mark_confirmed(cid)
            self.assertTrue(self.tg_auth.consume_confirmation(cid)[0],
                            f"scope {cid} 第一次 consume 應成功")
            self.assertFalse(self.tg_auth.consume_confirmation(cid)[0],
                             f"scope {cid} 第二次 consume 應失敗")

    def test_invalid_scope_rejected(self):
        self.assertEqual(self.tg_auth.consume_confirmation("../etc"),
                         (False, -1.0))
        self.assertEqual(self.tg_auth.consume_confirmation(""), (False, -1.0))

    def test_expired_confirmation_not_consumable(self):
        cid = "510004"
        self.tg_auth.mark_confirmed(cid)
        with self.tg_auth._state_lock:
            self.tg_auth._confirm_state[cid] = time.time() - 500  # > 90s 窗
        ok, elapsed = self.tg_auth.consume_confirmation(cid)
        self.assertFalse(ok)
        self.assertGreater(elapsed, self.tg_auth._CONFIRM_WINDOW_SEC)

    def test_concurrent_consume_only_one_winner(self):
        """兩個（以上）線程同時 consume 同一 scope，只有一個成功。"""
        cid = "510005"
        self.tg_auth.mark_confirmed(cid)
        n = 8
        barrier = threading.Barrier(n)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            ok, _ = self.tg_auth.consume_confirmation(cid)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 1,
                         f"應恰好一個線程搶到確認，實得 {sum(results)}/{n}")


class WrapSensitiveToolAtomicConsumeTests(_ConsumeBase):
    def test_one_confirmation_admits_single_execution_under_race(self):
        """模擬軟超時背景線程 race：舊線程還在執行工具時，新一輪呼叫
        不能共用同一枚確認（修前 revoke 在 finally，工具執行中確認仍活著）。"""
        cid = "520001"
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def fake_send_gmail(to, subject, body):
            calls.append(to)
            entered.set()
            release.wait(timeout=10)
            return "sent"
        fake_send_gmail.__name__ = "send_gmail"  # CONFIRM tier

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_send_gmail, get_chat_id=lambda: cid)
        self.tg_auth.mark_confirmed(cid)

        check_p, record_p = self._budget_mocks()
        with check_p, record_p:
            first_result: list = []
            t = threading.Thread(
                target=lambda: first_result.append(wrapped("a@b.c", "s", "b")))
            t.start()
            self.assertTrue(entered.wait(timeout=10), "第一輪應已進入工具執行")
            # 第一輪還在跑（確認已於執行前被原子消費）——第二輪必須被擋
            second = wrapped("x@y.z", "s2", "b2")
            release.set()
            t.join(timeout=10)

        self.assertEqual(calls, ["a@b.c"], "同一枚確認只能放行一個動作")
        self.assertIn("sent", str(first_result[0]))
        self.assertFalse(getattr(second, "ok", True))
        from agent_core.tool_result import ErrorCode
        self.assertEqual(second.error_code, ErrorCode.NEEDS_CONFIRMATION)

    def test_confirmation_not_refunded_when_tool_raises(self):
        # 舊語意（finally 必 revoke = 失敗不退還）要保持
        cid = "520002"

        def fake_send_gmail(to, subject, body):
            raise RuntimeError("boom")
        fake_send_gmail.__name__ = "send_gmail"

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_send_gmail, get_chat_id=lambda: cid)
        self.tg_auth.mark_confirmed(cid)
        check_p, record_p = self._budget_mocks()
        with check_p, record_p:
            with self.assertRaises(RuntimeError):
                wrapped("a@b.c", "s", "b")
        self.assertFalse(self.tg_auth.check_confirmed(cid)[0],
                         "工具 raise 也不退還確認（與舊 finally revoke 等價）")

    def test_second_sequential_call_needs_new_confirmation(self):
        # one-shot 基本語意不變：跑完一次就要重新確認
        cid = "520003"

        def fake_send_gmail(to, subject, body):
            return "sent"
        fake_send_gmail.__name__ = "send_gmail"

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_send_gmail, get_chat_id=lambda: cid)
        self.tg_auth.mark_confirmed(cid)
        check_p, record_p = self._budget_mocks()
        with check_p, record_p:
            first = wrapped("a@b.c", "s", "b")
            second = wrapped("a@b.c", "s", "b")
        self.assertIn("sent", str(first))
        self.assertFalse(getattr(second, "ok", True))

    def test_dangerous_gate_block_does_not_consume(self):
        # +確認 有、+雙確認 沒有 → 擋下但不消費（大王補 +雙確認 後不用重來）
        cid = "520004"

        def fake_run_shell(cmd):
            return "ran"
        fake_run_shell.__name__ = "run_shell"  # DANGEROUS tier

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_run_shell, get_chat_id=lambda: cid)
        self.tg_auth.mark_confirmed(cid)
        out = wrapped("ls")
        self.assertFalse(getattr(out, "ok", True))
        self.assertTrue(self.tg_auth.check_confirmed(cid)[0],
                        "二次確認未過的失敗路徑不該消耗 +確認")

    def test_budget_block_does_not_consume(self):
        # 既有語意回歸：budget 滿被擋不消耗確認
        cid = "520005"

        def fake_send_gmail(to, subject, body):
            return "sent"
        fake_send_gmail.__name__ = "send_gmail"

        wrapped = self.tg_auth.wrap_sensitive_tool(
            fake_send_gmail, get_chat_id=lambda: cid)
        self.tg_auth.mark_confirmed(cid)
        with mock.patch("agent_core.tool_budgets.check_budget",
                        return_value=(False, "額度用完")), \
                mock.patch("agent_core.tool_budgets.record_use"):
            out = wrapped("a@b.c", "s", "b")
        self.assertFalse(getattr(out, "ok", True))
        self.assertTrue(self.tg_auth.check_confirmed(cid)[0],
                        "budget 擋下不該消耗 +確認")


if __name__ == "__main__":
    unittest.main()
