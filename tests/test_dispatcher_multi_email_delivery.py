"""排程任務的「多收件人各自自寄」投遞管道（notify_emails）。

跟既有的 notify_channel=="telegram" 分流（test_dispatcher_telegram_delivery.py）
平行的第三條路：task 帶 notify_emails 清單時，每個地址各自收到「自己寄給自己」
的一封信（send_gmail_as 用網域委派冒充該地址寄信），不是大王代寄的群發信、也
不 fallback 到 telegram/owner-email。daily_production_8am / daily_production_alert
改造後就是靠這條管道回報給 4 位收件人。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class NotifyEmailsDeliveryTests(unittest.TestCase):
    def setUp(self):
        from agent_core import daemon_dispatcher
        self.dd = daemon_dispatcher

    def test_each_recipient_gets_a_self_addressed_send(self):
        notify = mock.Mock()
        tpa = mock.Mock()
        sga = mock.Mock(return_value="✅ 已寄出")
        task = {
            "name": "daily_production_8am",
            "notify_emails": ["owner@company.example", "gm@company.example"],
        }

        self.dd.notify_dispatcher_result(
            task, "前一天成型 1480 雙",
            notify=notify, telegram_push_agent=tpa, send_gmail_as=sga,
        )

        self.assertEqual(sga.call_count, 2)
        calls = {c.args[0]: c.args for c in sga.call_args_list}
        # 每通呼叫的 actor_email 跟 to 都是同一個地址（自己寄給自己）。
        self.assertEqual(calls["owner@company.example"][:2],
                          ("owner@company.example", "owner@company.example"))
        self.assertEqual(calls["gm@company.example"][:2],
                          ("gm@company.example", "gm@company.example"))
        self.assertIn("前一天成型 1480 雙", calls["owner@company.example"][3])
        # notify_emails 優先於 telegram/owner-email，兩者都不該被觸發。
        tpa.assert_not_called()
        notify.assert_not_called()

    def test_one_recipient_failure_does_not_block_others(self):
        notify = mock.Mock()
        sga = mock.Mock(side_effect=[RuntimeError("scope not authorized"), "✅ 已寄出"])
        task = {
            "name": "x",
            "notify_emails": ["owner@company.example", "gm@company.example"],
        }

        # 不該拋例外冒出去打斷 dispatcher 迴圈。
        self.dd.notify_dispatcher_result(
            task, "報表內容", notify=notify, send_gmail_as=sga
        )

        self.assertEqual(sga.call_count, 2)
        notify.assert_not_called()

    def test_notify_emails_and_telegram_both_fire_when_both_configured(self):
        """兩個管道並存（2026-08-12 起）。

        原本 notify_emails 一設就 return、Telegram 整條被吃掉。貨款到帳通知要的
        正是「同一份內容同時進紫色 bot 與台越會計的信箱」，兩者缺一都不算送到，
        所以改成並行。舊任務不受影響——沒有任何既有任務同時設了兩個非空值。
        """
        notify = mock.Mock()
        tpa = mock.Mock(return_value="✅ 已推送")
        sga = mock.Mock(return_value="已寄出給 twaccounting@company.example")
        task = {
            "name": "payment_notice_watch",
            "notify_channel": "telegram",
            "notify_agent_colors": ["purple"],
            "notify_emails": ["twaccounting@company.example", "accounting-vn@company.example"],
        }

        self.dd.notify_dispatcher_result(
            task, "💵 客戶貨款通知 1 則",
            notify=notify, telegram_push_agent=tpa, send_gmail_as=sga,
        )

        self.assertEqual(sga.call_count, 2)
        tpa.assert_called_once()
        self.assertEqual(tpa.call_args.args[0], "purple")
        # 兩條都成功 → 不必再往大王信箱補一封。
        notify.assert_not_called()

    def test_telegram_failure_does_not_double_notify_when_email_delivered(self):
        # Telegram 掛掉但 email 已經送到收件人手上：內容沒遺失，再寄一封給大王
        # 只是重複，也違反 notify_emails「不往 owner 信箱倒別人的通知」的取捨。
        notify = mock.Mock()
        tpa = mock.Mock(return_value="部分失敗：0/1 段成功")
        sga = mock.Mock(return_value="已寄出給 twaccounting@company.example")
        task = {
            "name": "payment_notice_watch",
            "notify_channel": "telegram",
            "notify_agent_colors": ["purple"],
            "notify_emails": ["twaccounting@company.example"],
        }

        self.dd.notify_dispatcher_result(
            task, "y", notify=notify, telegram_push_agent=tpa, send_gmail_as=sga
        )

        notify.assert_not_called()

    def test_owner_fallback_when_both_channels_fail(self):
        # 兩條都沒送出去 → 報表真的會消失，這時才吵大王。
        notify = mock.Mock()
        tpa = mock.Mock(return_value="部分失敗：0/1 段成功")
        sga = mock.Mock(return_value="❌ send_gmail_as：service account 金鑰未設定")
        task = {
            "name": "payment_notice_watch",
            "notify_channel": "telegram",
            "notify_agent_colors": ["purple"],
            "notify_emails": ["twaccounting@company.example"],
        }

        self.dd.notify_dispatcher_result(
            task, "y", notify=notify, telegram_push_agent=tpa, send_gmail_as=sga
        )

        notify.assert_called_once()

    def test_email_only_task_still_never_touches_telegram(self):
        # 既有的十來個 notify_emails 任務（採購簡報/倉庫簡報…）行為不變。
        notify = mock.Mock()
        tpa = mock.Mock(return_value="✅ 已推送")
        sga = mock.Mock(return_value="已寄出給 twpurchase2@company.example")
        task = {"name": "ashley_daily_brief_am",
                "notify_emails": ["twpurchase2@company.example"]}

        self.dd.notify_dispatcher_result(
            task, "y", notify=notify, telegram_push_agent=tpa, send_gmail_as=sga
        )

        sga.assert_called_once()
        tpa.assert_not_called()
        notify.assert_not_called()

    def test_empty_notify_emails_falls_through_to_existing_behaviour(self):
        notify = mock.Mock()
        sga = mock.Mock()
        task = {"name": "x", "notify_emails": []}

        self.dd.notify_dispatcher_result(
            task, "y", notify=notify, send_gmail_as=sga
        )

        sga.assert_not_called()
        notify.assert_called_once()

    def test_notify_emails_without_send_gmail_as_falls_through(self):
        # 防呆：notify_emails 設了但沒注入 send_gmail_as（None）→ 走既有 email 行為。
        notify = mock.Mock()
        task = {"name": "x", "notify_emails": ["owner@company.example"]}

        self.dd.notify_dispatcher_result(task, "y", notify=notify, send_gmail_as=None)

        notify.assert_called_once()

    def test_email_delivery_requests_markdown_html_rendering(self):
        # Gemini 產的結果常帶 markdown 表格（生產日報）→ 兩條 email 路都要開
        # markdown_html，否則 Mail 客戶端把 `| :---: |` 原樣露出、欄位沒對齊。
        sga = mock.Mock(return_value="✅ 已寄出")
        task = {"name": "daily_production_8am",
                "notify_emails": ["owner@company.example"]}
        self.dd.notify_dispatcher_result(
            task, "| a |\n| --- |\n| 1 |", notify=mock.Mock(), send_gmail_as=sga
        )
        self.assertIs(sga.call_args.kwargs.get("markdown_html"), True)

        notify = mock.Mock()
        self.dd.notify_dispatcher_result(
            {"name": "x"}, "| a |\n| --- |\n| 1 |", notify=notify
        )
        self.assertIs(notify.call_args.kwargs.get("markdown_html"), True)

    def test_subject_includes_task_name(self):
        sga = mock.Mock(return_value="✅ 已寄出")
        task = {"name": "email_pending_tracker", "notify_emails": ["owner@company.example"]}

        self.dd.notify_dispatcher_result(
            task, "清單內容", notify=mock.Mock(), send_gmail_as=sga
        )

        subject = sga.call_args.args[2]
        self.assertIn("email_pending_tracker", subject)


class EmailSubjectTests(unittest.TestCase):
    """task 可自訂中文主旨（收件人是同事時，英文任務名說不出這是什麼信）。"""

    def setUp(self):
        from agent_core import daemon_dispatcher
        self.dd = daemon_dispatcher

    def test_default_is_unchanged_task_name(self):
        self.assertEqual(self.dd.dispatcher_email_subject({"name": "foo"}), "【foo】")
        self.assertEqual(self.dd.dispatcher_email_subject({"name": "foo",
                                                           "email_subject": "  "}),
                         "【foo】")
        self.assertEqual(self.dd.dispatcher_email_subject({}), "【?】")

    def test_custom_subject_with_date_placeholder(self):
        import datetime as _dt
        got = self.dd.dispatcher_email_subject(
            {"name": "x", "email_subject": "生產管理每日簡報 {date}（早）"},
            now=_dt.datetime(2026, 8, 6, 10, 3),
        )
        self.assertEqual(got, "生產管理每日簡報 08-06（早）")

    def test_bad_placeholder_falls_back_to_raw(self):
        """主旨格式化失敗不該讓整封報表寄不出去。"""
        got = self.dd.dispatcher_email_subject({"name": "x", "email_subject": "壞的 {nope}"})
        self.assertEqual(got, "壞的 {nope}")

    def test_used_by_notify_path(self):
        sga = mock.Mock(return_value="✅ 已寄出")
        self.dd.notify_dispatcher_result(
            {"name": "production_capacity_brief_am",
             "email_subject": "生產管理每日簡報（早）",
             "notify_emails": ["production-mgr@company.example"]},
            "報表", notify=mock.Mock(), send_gmail_as=sga,
        )
        self.assertEqual(sga.call_args.args[2], "生產管理每日簡報（早）")


if __name__ == "__main__":
    unittest.main()
