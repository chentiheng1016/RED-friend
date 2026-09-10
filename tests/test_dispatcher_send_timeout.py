"""Regression (review finding #11): run_one_dispatcher_task called
chat.send_message directly with no timeout. The Gemini SDK can hang
indefinitely, and launchd's StartInterval won't start a new dispatcher while
the old one is alive — so one hung call silently freezes ALL scheduled
background tasks. Pin that the dispatcher routes through the bounded-timeout
helper, and that the helper actually raises on a hang.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class DispatcherSendTimeoutTests(unittest.TestCase):
    def test_dispatcher_routes_through_timeout_helper(self):
        from agent_core import daemon_dispatcher, daemon_telegram

        fake_chat = object()
        captured = {}

        def fake_helper(chat_obj, wrapped, *, timeout_s=999, caller=""):
            captured["chat"] = chat_obj
            captured["wrapped"] = wrapped
            captured["timeout_s"] = timeout_s
            captured["caller"] = caller
            return types.SimpleNamespace(text="done")

        client = types.SimpleNamespace(
            chats=types.SimpleNamespace(create=lambda **kw: fake_chat)
        )
        gtypes = types.SimpleNamespace(
            GenerateContentConfig=lambda **kw: kw,
            AutomaticFunctionCallingConfig=lambda **kw: kw,
        )

        with mock.patch.object(daemon_telegram, "_send_message_with_timeout", side_effect=fake_helper):
            out = daemon_dispatcher.run_one_dispatcher_task(
                {"name": "t", "prompt": "p", "interval_minutes": 60, "start_hour": 0, "end_hour": 24},
                tools_list=[],
                gemini_model="gemini-flash-latest",
                agent_client_factory=lambda: client,
                agent_types_factory=lambda: gtypes,
            )

        # 結果尾巴會多一段資料來源足跡（report_trail），拆掉再比正文——
        # 這個測試要驗的是「有沒有走 timeout wrapper」，不是足跡內容。
        from agent_core.report_trail import strip_source_footer
        self.assertEqual(strip_source_footer(out), "done")
        # The dispatcher's chat went through the timeout wrapper, not a raw
        # send_message — that's the protection.
        self.assertIs(captured["chat"], fake_chat)
        self.assertIn("任務內容", captured["wrapped"])
        # Background tasks get the longer dispatcher-specific timeout (not the
        # 180s interactive-chat default that would kill legit multi-tool runs)
        # and their spend is attributed to the dispatcher, not telegram_chat.
        self.assertEqual(captured["timeout_s"], daemon_dispatcher._DISPATCHER_SEND_TIMEOUT_S)
        self.assertGreaterEqual(captured["timeout_s"], 180)
        self.assertEqual(captured["caller"], "dispatcher")

    def test_timeout_helper_raises_on_hang(self):
        from agent_core import daemon_telegram

        # An event gate (never opened until cleanup) models a wedged SDK call
        # deterministically: the worker can't set `done` while gated, so
        # done.wait(timeout_s) is guaranteed to expire. A fixed time.sleep only
        # shrinks the race window — a loaded CI box can still lose it.
        never_returns = threading.Event()
        self.addCleanup(never_returns.set)

        class _HungChat:
            def send_message(self, wrapped):
                never_returns.wait(10)  # simulate an SDK hang
                return types.SimpleNamespace(text="late")

        t0 = time.time()
        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(_HungChat(), "hi", timeout_s=0.1)
        # It bailed at the soft timeout rather than waiting out the hang.
        self.assertLess(time.time() - t0, 1.5)


class DispatcherEnvIntTests(unittest.TestCase):
    def test_bad_env_value_falls_back_to_default_not_crash(self):
        # A garbage RED_DISPATCHER_SEND_TIMEOUT_S must NOT raise (it's read at
        # import; a crash would take the whole dispatcher daemon down).
        from agent_core import daemon_dispatcher as dd

        for bad in ("", "abc", "12x", None):
            env = {} if bad is None else {"RED_DISPATCHER_SEND_TIMEOUT_S": bad}
            with mock.patch.dict(os.environ, env, clear=False):
                if bad is None:
                    os.environ.pop("RED_DISPATCHER_SEND_TIMEOUT_S", None)
                self.assertEqual(
                    dd._env_int("RED_DISPATCHER_SEND_TIMEOUT_S", 600, min_value=180, max_value=3600),
                    600,
                )

    def test_env_value_is_clamped(self):
        from agent_core import daemon_dispatcher as dd

        with mock.patch.dict(os.environ, {"RED_DISPATCHER_SEND_TIMEOUT_S": "5"}):
            self.assertEqual(dd._env_int("RED_DISPATCHER_SEND_TIMEOUT_S", 600, min_value=180, max_value=3600), 180)
        with mock.patch.dict(os.environ, {"RED_DISPATCHER_SEND_TIMEOUT_S": "99999"}):
            self.assertEqual(dd._env_int("RED_DISPATCHER_SEND_TIMEOUT_S", 600, min_value=180, max_value=3600), 3600)


class DispatcherPerTaskDeadlineTests(unittest.TestCase):
    """逐任務 wall-clock 看門狗：dispatcher 一輪跑多個異質排程任務，某個卡死的
    任務只能拖垮自己（記失敗、進 DLQ），不能 hard-kill 整批。

    這跟單發 cron 用的 run_with_deadline（os._exit 整個 process）刻意不同 —— 那個
    用在這裡會把同批其他正常任務一起打斷。內層 _send_message_with_timeout 只罩住
    chat.send_message，罩不到 task 的前置設定（client build / api-key / chats.create）
    也可能被未來新路徑繞過；外層這層是 belt-and-suspenders。
    """

    def _run_two_task_cycle(self, *, run_one_fn, task_deadline_s):
        from agent_core import daemon_dispatcher as dd, daemon_helpers

        tasks = [
            {"name": "slow_task", "prompt": "p", "interval_minutes": 60, "start_hour": 0, "end_hour": 24},
            {"name": "fast_task", "prompt": "p", "interval_minutes": 60, "start_hour": 0, "end_hour": 24},
        ]
        data = {"version": 1, "tasks": tasks}
        notified = []

        with mock.patch.object(dd, "_DISPATCHER_TASK_DEADLINE_S", task_deadline_s), \
                mock.patch.object(daemon_helpers.faulthandler, "dump_traceback"), \
                mock.patch.object(daemon_helpers.sys, "stderr"):
            dd.task_dispatcher(
                load_daemon_tasks=lambda: data,
                save_daemon_tasks=lambda payload: True,
                should_run_task_fn=lambda task, now: True,
                run_one_dispatcher_task_fn=run_one_fn,
                mark_dispatcher_task_failed_fn=dd.mark_dispatcher_task_failed,
                mark_dispatcher_task_succeeded_fn=dd.mark_dispatcher_task_succeeded,
                dispatcher_result_is_empty_fn=dd.dispatcher_result_is_empty,
                remember_dispatcher_result_fn=dd.remember_dispatcher_result,
                # #140 起 dispatcher 以 task dict 呼叫 notify（main 仍傳 name 字串）；
                # 兩種都接 — 本測試聚焦「哪個任務被通知」而非通知契約細節，故對契約不敏感。
                notify_dispatcher_result_fn=lambda t, result: notified.append(
                    (t["name"] if isinstance(t, dict) else t, result)),
                network_is_up_fn=lambda: True,
            )
        return {t["name"]: t for t in tasks}, notified

    def test_wedged_task_isolated_from_rest_of_batch(self):
        # 用「真的」run_task_with_deadline + 真的卡死 fn，驗證整條路徑
        # （loop → primitive → abandon → 既有失敗處理），不是 stub 抄捷徑。
        gate = threading.Event()
        self.addCleanup(gate.set)

        def run_one(task):
            if task["name"] == "slow_task":
                gate.wait(10)  # 卡到 cleanup 才放
                return "late"
            return f"result-{task['name']}"

        by_name, notified = self._run_two_task_cycle(run_one_fn=run_one, task_deadline_s=0.1)

        # 卡死的任務：記失敗（DLQ）、不通知。
        self.assertIsNotNone(by_name["slow_task"]["last_error"])
        self.assertIn("per-task wall-clock", by_name["slow_task"]["last_error"])
        self.assertEqual(by_name["slow_task"].get("run_count", 0), 0)
        # 同一批的另一個任務照樣跑完、成功、發通知。
        self.assertEqual(by_name["fast_task"]["run_count"], 1)
        self.assertIsNone(by_name["fast_task"]["last_error"])
        self.assertEqual(notified, [("fast_task", "result-fast_task")])

    def test_inner_exception_still_flows_to_failure_path(self):
        # fn 自身（含內層 timeout）拋的例外要原樣走既有 mark_failed，不被外層
        # 看門狗誤包成 TaskDeadlineExceeded。
        def run_one(task):
            if task["name"] == "slow_task":
                raise RuntimeError("inner boom")
            return f"result-{task['name']}"

        by_name, notified = self._run_two_task_cycle(run_one_fn=run_one, task_deadline_s=30)

        self.assertEqual(by_name["slow_task"]["last_error"], "inner boom")
        self.assertEqual(by_name["fast_task"]["run_count"], 1)
        self.assertEqual(notified, [("fast_task", "result-fast_task")])

    def test_each_due_task_wrapped_in_deadline(self):
        # 防退化：每個到期任務都必須被 run_task_with_deadline 包住，且帶設定的
        # deadline + 逐任務 label。少了這層 = 卡死任務又會堵住整輪。
        from agent_core import daemon_dispatcher as dd

        calls = []

        def fake_runner(fn, deadline_s, *, label):
            calls.append((deadline_s, label))
            return fn()  # pass-through

        tasks = [
            {"name": "a", "prompt": "p", "interval_minutes": 60, "start_hour": 0, "end_hour": 24},
            {"name": "b", "prompt": "p", "interval_minutes": 60, "start_hour": 0, "end_hour": 24},
        ]
        data = {"version": 1, "tasks": tasks}

        with mock.patch.object(dd, "run_task_with_deadline", side_effect=fake_runner):
            dd.task_dispatcher(
                load_daemon_tasks=lambda: data,
                save_daemon_tasks=lambda payload: True,
                should_run_task_fn=lambda task, now: True,
                run_one_dispatcher_task_fn=lambda task: "(無新發現)",
                mark_dispatcher_task_failed_fn=dd.mark_dispatcher_task_failed,
                mark_dispatcher_task_succeeded_fn=dd.mark_dispatcher_task_succeeded,
                dispatcher_result_is_empty_fn=dd.dispatcher_result_is_empty,
                remember_dispatcher_result_fn=dd.remember_dispatcher_result,
                notify_dispatcher_result_fn=lambda task, result: None,
                network_is_up_fn=lambda: True,
            )

        self.assertEqual(
            calls,
            [
                (dd._DISPATCHER_TASK_DEADLINE_S, "dispatcher:a"),
                (dd._DISPATCHER_TASK_DEADLINE_S, "dispatcher:b"),
            ],
        )


class DispatcherTaskDeadlineEnvTests(unittest.TestCase):
    def test_default_outer_exceeds_inner_send_timeout(self):
        # 核心不變量：per-task 外層 deadline 必須 > 內層 send timeout，否則外層永遠
        # 先觸發、內層那套（成本記帳 + abandon monitor）形同虛設。
        from agent_core import daemon_dispatcher as dd

        self.assertGreater(dd._DISPATCHER_TASK_DEADLINE_S, dd._DISPATCHER_SEND_TIMEOUT_S)

    def test_floor_keeps_outer_above_inner_even_with_tiny_env(self):
        from agent_core import daemon_dispatcher as dd

        floor = dd._DISPATCHER_SEND_TIMEOUT_S + 60
        with mock.patch.dict(os.environ, {"RED_DISPATCHER_TASK_DEADLINE_S": "5"}):
            val = dd._env_int(
                "RED_DISPATCHER_TASK_DEADLINE_S",
                dd._DISPATCHER_SEND_TIMEOUT_S + 300,
                min_value=floor,
                max_value=7200,
            )
        self.assertEqual(val, floor)
        self.assertGreater(val, dd._DISPATCHER_SEND_TIMEOUT_S)

    def test_bad_env_falls_back_to_default(self):
        from agent_core import daemon_dispatcher as dd

        default = dd._DISPATCHER_SEND_TIMEOUT_S + 300
        for bad in ("", "abc", "9x"):
            with mock.patch.dict(os.environ, {"RED_DISPATCHER_TASK_DEADLINE_S": bad}):
                self.assertEqual(
                    dd._env_int(
                        "RED_DISPATCHER_TASK_DEADLINE_S",
                        default,
                        min_value=dd._DISPATCHER_SEND_TIMEOUT_S + 60,
                        max_value=7200,
                    ),
                    default,
                )

    def test_env_value_clamped_to_max(self):
        from agent_core import daemon_dispatcher as dd

        with mock.patch.dict(os.environ, {"RED_DISPATCHER_TASK_DEADLINE_S": "999999"}):
            val = dd._env_int(
                "RED_DISPATCHER_TASK_DEADLINE_S",
                dd._DISPATCHER_SEND_TIMEOUT_S + 300,
                min_value=dd._DISPATCHER_SEND_TIMEOUT_S + 60,
                max_value=7200,
            )
        self.assertEqual(val, 7200)


if __name__ == "__main__":
    unittest.main()
