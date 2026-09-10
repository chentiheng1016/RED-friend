"""排程任務的 Telegram 投遞管道。

排程任務以前一律走 Gmail 寄給大王一人（daemon_helpers.notify）。新增 per-task
``notify_channel=="telegram"`` 後，dispatcher 會把結果用 ``telegram_push_agent``
扇出到該色的 chat（大王 + 同色員工，收件人來自可信本地設定、非 LLM 輸出，故無
prompt-injection 風險）；只有 Telegram 沒「完全成功」時才 fallback 回 email，
確保排程報告不會默默遺失。每日生產回報（daily_production_8am）就是靠這條管道
把前一天的生產數推給大王與 UserS。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class NotifyDispatcherResultChannelTests(unittest.TestCase):
    def setUp(self):
        from agent_core import daemon_dispatcher
        self.dd = daemon_dispatcher

    def test_telegram_channel_routes_to_telegram_not_email(self):
        notify = mock.Mock()
        tpa = mock.Mock(return_value="✅ 已推送到 red Telegram（2 chat，共 1 段）")
        task = {
            "name": "daily_production_8am",
            "notify_channel": "telegram",
            "notify_agent_color": "red",
        }

        self.dd.notify_dispatcher_result(
            task, "前一天成型 1480 雙", notify=notify, telegram_push_agent=tpa
        )

        tpa.assert_called_once()
        color, body = tpa.call_args.args
        self.assertEqual(color, "red")
        self.assertIn("前一天成型 1480 雙", body)
        self.assertIn("daily_production_8am", body)
        # 全成功（回傳含 ✅）→ 絕不再多寄一封 email。
        notify.assert_not_called()

    def test_email_default_when_no_channel(self):
        notify = mock.Mock()
        tpa = mock.Mock(return_value="✅ 不該被呼叫")
        task = {"name": "ebay_watch"}  # 沒有 notify_channel → 維持原本 email 行為

        self.dd.notify_dispatcher_result(
            task, "找到 3 筆新上架", notify=notify, telegram_push_agent=tpa
        )

        tpa.assert_not_called()
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["subject"], "【排程: ebay_watch】")
        self.assertIn("找到 3 筆新上架", kwargs["body"])
        self.assertEqual(kwargs["task_name"], "dispatcher:ebay_watch")

    def test_telegram_partial_failure_falls_back_to_email(self):
        notify = mock.Mock()
        # telegram_push_agent 部分失敗時回字串（非例外），不含 ✅。
        tpa = mock.Mock(return_value="部分失敗：1/2 段成功；1 chat 失敗。")
        task = {"name": "daily_production_8am", "notify_channel": "telegram"}

        self.dd.notify_dispatcher_result(
            task, "報表內容", notify=notify, telegram_push_agent=tpa
        )

        tpa.assert_called_once()
        # 沒 ✅ → email fallback 補送，報告不遺失。
        notify.assert_called_once()
        self.assertIn("報表內容", notify.call_args.kwargs["body"])

    def test_telegram_exception_falls_back_to_email(self):
        notify = mock.Mock()
        tpa = mock.Mock(side_effect=RuntimeError("boom"))
        task = {"name": "daily_production_8am", "notify_channel": "telegram"}

        # 例外絕不能冒出去打斷 dispatcher 迴圈（會凍住所有排程任務）。
        self.dd.notify_dispatcher_result(
            task, "報表內容", notify=notify, telegram_push_agent=tpa
        )

        notify.assert_called_once()
        self.assertIn("報表內容", notify.call_args.kwargs["body"])

    def test_telegram_channel_defaults_to_red_color(self):
        notify = mock.Mock()
        tpa = mock.Mock(return_value="✅ done")
        task = {"name": "x", "notify_channel": "telegram"}  # 未指定 color

        self.dd.notify_dispatcher_result(
            task, "y", notify=notify, telegram_push_agent=tpa
        )

        self.assertEqual(tpa.call_args.args[0], "red")

    def test_telegram_channel_without_pusher_falls_back_to_email(self):
        # 防呆：notify_channel=telegram 但沒注入 telegram_push_agent（None）→ email。
        notify = mock.Mock()
        task = {"name": "x", "notify_channel": "telegram"}

        self.dd.notify_dispatcher_result(
            task, "y", notify=notify, telegram_push_agent=None
        )

        notify.assert_called_once()


class MultiColorTelegramFanOutTests(unittest.TestCase):
    """一份結果發給多色（匯率推播同時給紅/黃/紫/橘）。

    ``notify_agent_colors``（清單）是新的欄位，單色的 ``notify_agent_color``
    必須原封不動繼續有效 —— 現役排程（daily_production_8am / daily_warehouse_report）
    全都還在用它。
    """

    def setUp(self):
        from agent_core import daemon_dispatcher
        self.dd = daemon_dispatcher

    def test_colors_resolution(self):
        f = self.dd.dispatcher_agent_colors
        self.assertEqual(f({"notify_agent_colors": ["red", "yellow"]}), ["red", "yellow"])
        self.assertEqual(f({"notify_agent_color": "indigo"}), ["indigo"])
        self.assertEqual(f({}), ["red"])
        # 大小寫/空白正規化 + 去重（重複色會讓同一個 chat 收到兩封）
        self.assertEqual(f({"notify_agent_colors": [" Red ", "RED", "purple"]}),
                         ["red", "purple"])
        # 誤寫成字串而非清單時當單色處理，不要逐字元拆成 ['r','e','d']
        self.assertEqual(f({"notify_agent_colors": "orange"}), ["orange"])

    def test_pushes_once_per_color(self):
        notify = mock.Mock()
        tpa = mock.Mock(return_value="✅ 已推送")
        task = {
            "name": "usd_twd_rate_am",
            "notify_channel": "telegram",
            "notify_agent_colors": ["red", "yellow", "purple", "orange"],
        }

        self.dd.notify_dispatcher_result(
            task, "1 USD = 32.44 TWD", notify=notify, telegram_push_agent=tpa
        )

        self.assertEqual([c.args[0] for c in tpa.call_args_list],
                         ["red", "yellow", "purple", "orange"])
        for call in tpa.call_args_list:
            self.assertIn("32.44", call.args[1])
        notify.assert_not_called()

    def test_one_failing_color_still_falls_back_to_email(self):
        # 四色少一色收到 = 那個部門今天沒收到，跟完全沒送一樣要看得見。
        notify = mock.Mock()
        tpa = mock.Mock(side_effect=["✅ ok", "✅ ok", "部分失敗：0/1 段成功", "✅ ok"])
        task = {
            "name": "usd_twd_rate_am",
            "notify_channel": "telegram",
            "notify_agent_colors": ["red", "yellow", "purple", "orange"],
        }

        self.dd.notify_dispatcher_result(
            task, "報價內容", notify=notify, telegram_push_agent=tpa
        )

        # 失敗的那色不該中斷後面的色。
        self.assertEqual(tpa.call_count, 4)
        notify.assert_called_once()
        self.assertIn("報價內容", notify.call_args.kwargs["body"])

    def test_exception_in_one_color_does_not_stop_the_rest(self):
        notify = mock.Mock()
        tpa = mock.Mock(side_effect=[RuntimeError("boom"), "✅ ok"])
        task = {"name": "t", "notify_channel": "telegram",
                "notify_agent_colors": ["red", "yellow"]}

        self.dd.notify_dispatcher_result(
            task, "x", notify=notify, telegram_push_agent=tpa
        )

        self.assertEqual(tpa.call_count, 2)
        notify.assert_called_once()


class NotifyPlainBodyTests(unittest.TestCase):
    """員工面推播不要包大王的維運外殼。"""

    def setUp(self):
        from agent_core import daemon_dispatcher
        self.dd = daemon_dispatcher

    def test_plain_body_is_the_result_verbatim(self):
        tpa = mock.Mock(return_value="✅ ok")
        task = {"name": "usd_twd_rate_am", "notify_channel": "telegram",
                "notify_agent_colors": ["yellow"], "notify_plain": True}

        self.dd.notify_dispatcher_result(
            task, "💵 美金 → 台幣\n1 USD = 32.44 TWD",
            notify=mock.Mock(), telegram_push_agent=tpa,
        )

        body = tpa.call_args.args[1]
        self.assertEqual(body, "💵 美金 → 台幣\n1 USD = 32.44 TWD")
        self.assertNotIn("取消排程", body)
        self.assertNotIn("usd_twd_rate_am", body)

    def test_default_keeps_the_wrapper(self):
        tpa = mock.Mock(return_value="✅ ok")
        task = {"name": "daily_production_8am", "notify_channel": "telegram",
                "notify_agent_color": "red"}

        self.dd.notify_dispatcher_result(
            task, "成型 1480 雙", notify=mock.Mock(), telegram_push_agent=tpa
        )

        body = tpa.call_args.args[1]
        self.assertIn("取消排程", body)
        self.assertIn("daily_production_8am", body)


class TaskDispatcherPassesTaskTests(unittest.TestCase):
    """迴圈把 task dict（非舊的 name 字串）交給 notify fn —— call-site 改動的關鍵。"""

    def test_loop_passes_task_dict_to_notify_fn(self):
        from agent_core import daemon_dispatcher as dd

        task = {
            "name": "t1",
            "prompt": "p",
            "notify_channel": "telegram",
            "notify_agent_color": "red",
            "dedup_hashes": [],
        }
        recorded = []

        dd.task_dispatcher(
            load_daemon_tasks=lambda: {"tasks": [task]},
            save_daemon_tasks=lambda data: True,
            should_run_task_fn=lambda t, now: True,
            run_one_dispatcher_task_fn=lambda t: "全新報表內容",
            mark_dispatcher_task_failed_fn=lambda t, e, now: None,
            mark_dispatcher_task_succeeded_fn=lambda t, now: None,
            dispatcher_result_is_empty_fn=dd.dispatcher_result_is_empty,
            remember_dispatcher_result_fn=dd.remember_dispatcher_result,
            notify_dispatcher_result_fn=lambda t, result: recorded.append((t, result)),
            network_is_up_fn=lambda: True,
        )

        self.assertEqual(len(recorded), 1)
        passed_task, passed_result = recorded[0]
        self.assertIsInstance(passed_task, dict)
        self.assertEqual(passed_task["name"], "t1")
        self.assertEqual(passed_task["notify_channel"], "telegram")
        self.assertEqual(passed_result, "全新報表內容")


class DispatcherHeaderDatetimeTests(unittest.TestCase):
    """run_one_dispatcher_task 把現在時間注入 header，讓日期敏感的背景任務
    （如每日生產回報判斷『昨天的日報是否遲到』）能可靠推算今天/昨天/星期幾。"""

    def test_header_includes_current_datetime(self):
        import datetime as _dt
        import types
        from agent_core import daemon_dispatcher, daemon_telegram

        captured = {}

        def fake_helper(chat_obj, wrapped, *, timeout_s=999, caller=""):
            captured["wrapped"] = wrapped
            return types.SimpleNamespace(text="ok")

        client = types.SimpleNamespace(
            chats=types.SimpleNamespace(create=lambda **kw: object())
        )
        gtypes = types.SimpleNamespace(
            GenerateContentConfig=lambda **kw: kw,
            AutomaticFunctionCallingConfig=lambda **kw: kw,
        )

        with mock.patch.object(
            daemon_telegram, "_send_message_with_timeout", side_effect=fake_helper
        ):
            daemon_dispatcher.run_one_dispatcher_task(
                {"name": "t", "prompt": "p", "interval_minutes": 30,
                 "start_hour": 8, "end_hour": 12},
                tools_list=[],
                gemini_model="gemini-flash-latest",
                agent_client_factory=lambda: client,
                agent_types_factory=lambda: gtypes,
            )

        wrapped = captured["wrapped"]
        self.assertIn("現在時間", wrapped)
        self.assertIn(_dt.date.today().strftime("%Y-%m-%d"), wrapped)


if __name__ == "__main__":
    unittest.main()
