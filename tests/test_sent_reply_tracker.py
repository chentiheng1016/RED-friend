"""sent_reply_tracker —— 「我本人寄出、對方沒回」的追蹤器。

跟 test_email_pending_tracker 是相反方向：那支測「別人寄來我方沒回」，這支測
「我寄出去對方沒回」。

這支模組最容易錯的地方不是「有沒有人回」，是**「這封信到底是不是本人寄的」**：
owner@company.example 同時是小紅自己的發信身分（gmail_ops 走 userId="me"），寄件
備份裡混了大量小紅發的排程報表。判錯 → 大王每天被自己的機器人洗版。所以
_is_machine_sent 的三條規則各自都有測試釘住。

不碰真 Gmail：_service 整個 mock 掉（那層是純 API I/O）。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

ME = "owner@company.example"

# 實測到的兩種 boundary：小紅走 Gmail API（Python email.mime）vs Gmail 網頁版。
PY_MIME_CT = 'multipart/mixed; boundary="===============7315952348069428218=="'
GMAIL_CT = 'multipart/alternative; boundary="000000000000f1a2b305fd0c1e2a"'


def _hdrs(**kw) -> dict[str, str]:
    return {k.replace("_", "-").lower(): v for k, v in kw.items()}


def _msg(from_addr=ME, to=None, subject="測試主旨", days_ago=5,
         content_type="text/plain; charset=UTF-8", x_mailer=None, snippet=""):
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "To", "value": to if to is not None else "sampledev@company.example"},
        {"name": "Subject", "value": subject},
        {"name": "Content-Type", "value": content_type},
    ]
    if x_mailer:
        headers.append({"name": "X-Mailer", "value": x_mailer})
    sent = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)
    return {
        "payload": {"headers": headers},
        "internalDate": str(int(sent.timestamp() * 1000)),
        "snippet": snippet,
    }


class IsMachineSentTests(unittest.TestCase):
    """三條判別規則 —— 錯一條大王就會被小紅自己的報表洗版。"""

    def setUp(self):
        from agent_core import sent_reply_tracker
        self.srt = sent_reply_tracker

    def test_python_mime_boundary_is_machine(self):
        """規則 1：小紅走 Gmail API 寄的，boundary 是 Python email.mime 的形狀。"""
        self.assertTrue(self.srt._is_machine_sent(
            _hdrs(Content_Type=PY_MIME_CT, To="owner@company.example, production-mgr@company.example"),
            ME,
        ))

    def test_self_addressed_only_is_machine(self):
        """規則 2：只寄給自己 = 小紅的自寄提醒（【小紅新信】那類是單段 MIME）。"""
        self.assertTrue(self.srt._is_machine_sent(
            _hdrs(Content_Type="text/plain", To="owner@company.example"), ME,
        ))

    def test_x_mailer_means_human_even_when_self_addressed(self):
        """規則 3 優先於規則 2：本人用 iPhone 寄給自己備忘，仍算本人寄的。"""
        self.assertFalse(self.srt._is_machine_sent(
            _hdrs(Content_Type="text/plain", To="owner@company.example",
                  X_Mailer="iPhone Mail (23G71)"),
            ME,
        ))

    def test_gmail_web_boundary_is_human(self):
        """網頁版 Gmail 的 boundary 不是 Python 那個形狀 → 本人。"""
        self.assertFalse(self.srt._is_machine_sent(
            _hdrs(Content_Type=GMAIL_CT, To="sampledev@company.example"), ME,
        ))

    def test_plain_single_part_to_others_is_human(self):
        """實測有 3 封本人手寫的單段純文字短回覆，不能被當成機器。"""
        self.assertFalse(self.srt._is_machine_sent(
            _hdrs(Content_Type="text/plain; charset=UTF-8", To="someone@freemail.example"), ME,
        ))

    def test_unknown_shape_defaults_to_human(self):
        """判不出來時寧可多提醒一封，也不要吃掉大王真正在等的信。"""
        self.assertFalse(self.srt._is_machine_sent(_hdrs(), ME))


class AckOnlyTests(unittest.TestCase):
    """本人回的「收到/謝謝」= 話已講完，對方不必再回。"""

    def setUp(self):
        from agent_core import sent_reply_tracker
        self.srt = sent_reply_tracker

    def test_short_chinese_ack(self):
        self.assertTrue(self.srt._is_ack_only("收到，謝謝"))

    def test_ack_with_signature_still_detected(self):
        """snippet 會把簽名檔一起帶進來 —— 切掉簽名後才判長度。"""
        self.assertTrue(self.srt._is_ack_only(
            "收到。 Best regards, Owner Name (Owner) General Manager JAIFUNG"
        ))

    def test_english_ack(self):
        self.assertTrue(self.srt._is_ack_only("Noted, thanks"))

    def test_real_question_is_not_ack(self):
        self.assertFalse(self.srt._is_ack_only(
            "請問這批 604A 的鞋面什麼時候可以進來？我要回客戶交期"
        ))

    def test_long_message_starting_with_ack_is_not_ack(self):
        """「收到」開頭但後面還有實質內容/要求 → 仍在等回覆。"""
        self.assertFalse(self.srt._is_ack_only(
            "收到，但是這個數量跟我上週給的不一樣，請重新確認後回覆我正確的數字"
        ))

    def test_empty_snippet_is_not_ack(self):
        self.assertFalse(self.srt._is_ack_only(""))


class PureRelayTests(unittest.TestCase):
    """純轉手歸檔 vs 有評論的轉寄 —— snippet 取自 owner@ 2026-08 的真實信件。

    實測 14 封「未回覆」裡有 9 封是零字轉寄給 twaccounting@ 的銀行/電信/發票
    通知，那是歸檔不是等回覆；但同樣是轉寄、他寫了字的那幾封是真的在等人動作。
    """

    SIG = ("Owner Name (Owner) General Manager JAIFUNG CORPORATION "
           "NO.1 EXAMPLE RD.,TAIPEI,TAIWAN P : +886-2-00000000")

    def setUp(self):
        from agent_core import sent_reply_tracker
        self.srt = sent_reply_tracker

    def test_signature_then_forward_marker_is_pure_relay(self):
        """Apple Mail 轉寄會把簽名放最前面，之後直接接轉寄內容 = 一個字都沒寫。"""
        self.assertTrue(self.srt._is_pure_relay(
            f"{self.SIG} 開始轉寄郵件： 寄件人: sms-adm@mail.bank.example"
        ))

    def test_quoted_forward_marker_only_is_pure_relay(self):
        self.assertTrue(self.srt._is_pure_relay(
            "&gt; 開始轉寄郵件： &gt; &gt; 寄件人: Adobe &gt; 標題: 與 Adobe 的免稅交易"
        ))

    def test_forward_with_real_comment_is_not_relay(self):
        """「這個注意一下 有更改帳號的行為 需要電話確認」—— 他在交辦，要留著。"""
        self.assertFalse(self.srt._is_pure_relay(
            "客戶迪卡儂（Anushri）來信請求協助批准錯誤銀行帳號的 GEX，"
            "這個注意一下 有更改帳號的行為 需要電話確認 " + self.SIG
        ))

    def test_forward_with_question_and_empty_subject_is_not_relay(self):
        """真實案例：主旨空白但問了 UserC 實質問題，只認 Fwd: 前綴會漏掉。"""
        self.assertFalse(self.srt._is_pure_relay(
            "Dear user-c 你是不是用公司email去註冊FB 麻煩把公司無關的email刪掉 "
            "因為ai會去讀 開始轉寄郵件： 寄件人: owner chen"
        ))

    def test_non_forward_is_never_relay(self):
        self.assertFalse(self.srt._is_pure_relay("請問這批什麼時候可以進來？"))

    def test_english_forward_marker(self):
        self.assertTrue(self.srt._is_pure_relay(
            "---------- Forwarded message --------- From: bank@example.com"
        ))


class EvaluateThreadTests(unittest.TestCase):
    def setUp(self):
        from agent_core import sent_reply_tracker
        self.srt = sent_reply_tracker
        self.now = datetime.now(timezone.utc)

    def _users(self, messages):
        users = mock.MagicMock()
        users.threads.return_value.get.return_value.execute.return_value = {
            "messages": messages
        }
        return users

    def test_last_message_from_me_is_unanswered(self):
        hit = self.srt._evaluate_thread(
            self._users([_msg(days_ago=7, x_mailer="iPad Mail (23F84)")]),
            "t1", ME, self.now,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["days"], 7)
        self.assertEqual(hit["thread_id"], "t1")

    def test_reply_from_other_side_clears_it(self):
        messages = [
            _msg(days_ago=9, x_mailer="iPad Mail (23F84)"),
            _msg(from_addr="UserC <sampledev@company.example>", days_ago=8),
        ]
        self.assertIsNone(
            self.srt._evaluate_thread(self._users(messages), "t1", ME, self.now)
        )

    def test_machine_sent_thread_is_skipped(self):
        """小紅自己的排程報表 —— 大王要的是「我個人寄出的郵件」。"""
        messages = [_msg(days_ago=6, to="owner@company.example",
                         subject="【排程: kitting_alert_daily】")]
        self.assertIsNone(
            self.srt._evaluate_thread(self._users(messages), "t1", ME, self.now)
        )

    def test_my_own_ack_is_skipped(self):
        messages = [
            _msg(from_addr="UserC <sampledev@company.example>", days_ago=9),
            _msg(days_ago=8, x_mailer="iPad Mail (23F84)", snippet="收到，謝謝"),
        ]
        self.assertIsNone(
            self.srt._evaluate_thread(self._users(messages), "t1", ME, self.now)
        )

    def test_pure_relay_is_skipped(self):
        messages = [_msg(days_ago=6, to="twaccounting@company.example",
                         subject="Fwd: 第一銀行 國內匯入匯款通知",
                         x_mailer="iPad Mail (23F84)",
                         snippet="Owner Name (Owner) General Manager JAIFUNG "
                                 "開始轉寄郵件： 寄件人: sms-adm@mail.bank.example")]
        self.assertIsNone(
            self.srt._evaluate_thread(self._users(messages), "t1", ME, self.now)
        )

    def test_pure_relay_kept_when_env_opt_in(self):
        messages = [_msg(days_ago=6, to="twaccounting@company.example",
                         subject="Fwd: 第一銀行 國內匯入匯款通知",
                         x_mailer="iPad Mail (23F84)",
                         snippet="Owner Name 開始轉寄郵件： 寄件人: bank")]
        with mock.patch.dict(os.environ, {"RED_SENT_REPLY_INCLUDE_RELAYS": "1"}):
            hit = self.srt._evaluate_thread(self._users(messages), "t1", ME, self.now)
        self.assertIsNotNone(hit)

    def test_forward_with_comment_is_kept(self):
        messages = [_msg(days_ago=6, to="gm@company.example",
                         subject="Fwd: 【小紅 Ponder】發現幾件事",
                         x_mailer="iPad Mail (23F84)",
                         snippet="這個注意一下 有更改帳號的行為 需要電話確認 "
                                 "Owner Name 開始轉寄郵件：")]
        self.assertIsNotNone(
            self.srt._evaluate_thread(self._users(messages), "t1", ME, self.now)
        )

    def test_external_recipient_flagged(self):
        hit = self.srt._evaluate_thread(
            self._users([_msg(days_ago=4, to="buyer@customer-d.example",
                              x_mailer="Apple Mail (2.3864.600.51.1.1)")]),
            "t1", ME, self.now,
        )
        self.assertTrue(hit["external"])

    def test_internal_recipient_not_flagged_external(self):
        hit = self.srt._evaluate_thread(
            self._users([_msg(days_ago=4, to="sampledev@company.example",
                              x_mailer="Apple Mail (2.3864.600.51.1.1)")]),
            "t1", ME, self.now,
        )
        self.assertFalse(hit["external"])

    def test_blank_subject_falls_back_to_body_preview(self):
        """主旨空白時印「（無主旨）」等於沒講 —— 改用本人打的那段字當標題。"""
        hit = self.srt._evaluate_thread(
            self._users([_msg(days_ago=13, subject="", x_mailer="iPad Mail (23F84)",
                              snippet="Dear user-c 你是不是用公司email去註冊FB "
                                      "麻煩把公司無關的email刪掉 開始轉寄郵件：")]),
            "t1", ME, self.now,
        )
        self.assertIn("你是不是用公司email去註冊FB", hit["subject"])
        self.assertNotIn("開始轉寄郵件", hit["subject"])

    def test_blank_subject_and_blank_body_says_no_subject(self):
        hit = self.srt._evaluate_thread(
            self._users([_msg(days_ago=5, subject="", x_mailer="iPad Mail (23F84)",
                              snippet="")]),
            "t1", ME, self.now,
        )
        self.assertEqual(hit["subject"], "（無主旨）")

    def test_empty_thread_returns_none(self):
        self.assertIsNone(
            self.srt._evaluate_thread(self._users([]), "t1", ME, self.now)
        )


class ListUnansweredSentThreadsTests(unittest.TestCase):
    """端到端（mock 掉 Gmail）——輸出是要直接推給大王的，格式也要釘住。"""

    def setUp(self):
        from agent_core import sent_reply_tracker
        self.srt = sent_reply_tracker

    def _run(self, threads: dict[str, list], **kwargs) -> str:
        """threads: {thread_id: [messages]}。"""
        users = mock.MagicMock()
        users.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": f"m{i}", "threadId": t}
                         for i, t in enumerate(threads)],
        }

        def fake_get(userId, id, format, metadataHeaders):
            return mock.MagicMock(execute=lambda: {"messages": threads[id]})

        users.threads.return_value.get.side_effect = fake_get
        service = mock.MagicMock()
        service.users.return_value = users
        with mock.patch.object(self.srt, "_service", return_value=service):
            return self.srt.list_unanswered_sent_threads(**kwargs)

    def test_all_answered_returns_ok_line(self):
        out = self._run({"t1": [
            _msg(days_ago=6, x_mailer="iPad Mail (23F84)"),
            _msg(from_addr="sampledev@company.example", days_ago=5),
        ]})
        self.assertTrue(out.startswith("✅"), out)

    def test_lists_overdue_thread_with_link(self):
        out = self._run(
            {"t1": [_msg(days_ago=6, subject="604A 楦頭確認",
                         x_mailer="iPad Mail (23F84)")]},
            days_overdue=3, lookback_days=14,
        )
        self.assertIn("604A 楦頭確認", out)
        self.assertIn("6 天", out)
        self.assertIn("https://mail.google.com/mail/u/0/#all/t1", out)
        self.assertIn("sampledev", out)

    def test_not_yet_overdue_is_excluded(self):
        out = self._run(
            {"t1": [_msg(days_ago=1, x_mailer="iPad Mail (23F84)")]},
            days_overdue=3,
        )
        self.assertTrue(out.startswith("✅"), out)

    def test_sorted_oldest_first(self):
        out = self._run({
            "t1": [_msg(days_ago=4, subject="較新的", x_mailer="iPad Mail (23F84)")],
            "t2": [_msg(days_ago=20, subject="最久的", x_mailer="iPad Mail (23F84)")],
        }, days_overdue=3, lookback_days=30)
        self.assertLess(out.index("最久的"), out.index("較新的"), out)

    def test_machine_reports_never_appear(self):
        """小紅自己每天寄給大王的排程報表，一封都不該出現。"""
        out = self._run({
            "t1": [_msg(days_ago=5, to="owner@company.example",
                        subject="【小紅新信】🔴1 急件")],
            "t2": [_msg(days_ago=5, to="owner@company.example, production-mgr@company.example",
                        subject="【daily_production_8am】", content_type=PY_MIME_CT)],
        }, days_overdue=3)
        self.assertTrue(out.startswith("✅"), out)
        self.assertNotIn("小紅新信", out)
        self.assertNotIn("daily_production_8am", out)

    def test_max_items_caps_the_list(self):
        threads = {
            f"t{i}": [_msg(days_ago=5 + i, subject=f"主旨{i}",
                           x_mailer="iPad Mail (23F84)")]
            for i in range(8)
        }
        with mock.patch.dict(os.environ, {"RED_SENT_REPLY_MAX_ITEMS": "3"}):
            out = self._run(threads, days_overdue=3, lookback_days=60)
        self.assertIn("共 8 封", out)
        self.assertEqual(out.count("⏳"), 3, out)

    def test_gmail_failure_returns_error_line_not_exception(self):
        with mock.patch.object(self.srt, "_service", side_effect=RuntimeError("boom")):
            out = self.srt.list_unanswered_sent_threads()
        self.assertTrue(out.startswith("❌"), out)
        self.assertIn("boom", out)

    def test_lookback_never_shorter_than_overdue(self):
        """回溯窗比逾期門檻短的話，永遠不可能有結果 —— 夾住。"""
        out = self._run(
            {"t1": [_msg(days_ago=10, subject="夾住", x_mailer="iPad Mail (23F84)")]},
            days_overdue=10, lookback_days=3,
        )
        self.assertIn("夾住", out)


class DeterministicEntryTests(unittest.TestCase):
    def setUp(self):
        from agent_core import sent_reply_tracker
        self.srt = sent_reply_tracker

    def test_reminder_reads_env_thresholds(self):
        with mock.patch.object(self.srt, "list_unanswered_sent_threads") as fake:
            fake.return_value = "ok"
            with mock.patch.dict(os.environ, {
                "RED_SENT_REPLY_OVERDUE_DAYS": "5",
                "RED_SENT_REPLY_LOOKBACK_DAYS": "21",
            }):
                self.srt.sent_reply_reminder()
        fake.assert_called_once_with(days_overdue=5, lookback_days=21)

    def test_both_tools_are_background_safe(self):
        """排程走 deterministic_tool，工具要進得了 safe_tools。"""
        self.assertTrue(self.srt.sent_reply_reminder.background_safe)
        self.assertTrue(self.srt.list_unanswered_sent_threads.background_safe)


class IncompleteScanTests(unittest.TestCase):
    """「查不完」不可以長成「查過了、沒事」。

    2026-08-27 的 bug family（同 #448 shipping「掃不到 ≠ 逾期」）：truncated /
    errors 原本只掛在「有發現」那條路上，整批 thread 讀失敗時反而回一個乾淨的
    ✅ ——一次網路故障就被包裝成一句安心保證推給大王。
    """

    def setUp(self):
        from agent_core import sent_reply_tracker
        self.srt = sent_reply_tracker

    def _run_with_thread_errors(self, n_threads: int, *, fail_all: bool):
        users = mock.MagicMock()
        users.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": f"m{i}", "threadId": f"t{i}"}
                         for i in range(n_threads)],
        }
        service = mock.MagicMock()
        service.users.return_value = users
        calls = {"n": 0}

        def boom(*_a, **_kw):
            calls["n"] += 1
            if fail_all or calls["n"] == 1:
                raise RuntimeError("Max retries exceeded: Failed to resolve")
            return None          # 讀成功、但這個 thread 不算未回

        with mock.patch.object(self.srt, "_service", return_value=service), \
             mock.patch.object(self.srt, "_evaluate_thread", side_effect=boom):
            return self.srt.list_unanswered_sent_threads()

    def test_all_threads_failing_does_not_claim_all_clear(self):
        out = self._run_with_thread_errors(3, fail_all=True)
        self.assertNotIn("✅", out)
        self.assertIn("沒有查完", out)
        self.assertIn("3 個 thread 讀取失敗", out)

    def test_partial_failure_reports_the_denominator(self):
        out = self._run_with_thread_errors(3, fail_all=False)
        self.assertNotIn("✅", out)
        # 3 個裡實際檢查了 2 個 —— 分母要講出來，才知道這句否定有多少底氣
        self.assertIn("實際檢查了 2 個", out)

    def test_clean_scan_still_gets_the_green_check(self):
        """沒有任何失敗時，原本的 ✅ 一字不動（別為了防假警報就永遠不敢說沒事）。"""
        users = mock.MagicMock()
        users.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "m0", "threadId": "t0"}],
        }
        service = mock.MagicMock()
        service.users.return_value = users
        with mock.patch.object(self.srt, "_service", return_value=service), \
             mock.patch.object(self.srt, "_evaluate_thread", return_value=None):
            out = self.srt.list_unanswered_sent_threads()
        self.assertIn("✅", out)
        self.assertNotIn("沒有查完", out)


if __name__ == "__main__":
    unittest.main()
