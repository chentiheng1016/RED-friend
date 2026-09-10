"""list_unanswered_company_threads —— 橫跨全公司信箱找「對方寄信未回」的 thread。

資料來源跟 RAG 夜跑 Gmail 部分同一套設定（rag_sync_targets.json 的
gmail_accounts + 網域委派連線），差別是這裡是即時查詢（給白天排程用），不
是每天一次的批次索引。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class IsExternalSenderTests(unittest.TestCase):
    def setUp(self):
        from agent_core import email_pending_tracker
        self.ept = email_pending_tracker

    def test_same_domain_is_not_external(self):
        self.assertFalse(self.ept._is_external_sender(
            "UserS <gm@company.example>", "company.example"
        ))

    def test_different_domain_is_external(self):
        self.assertTrue(self.ept._is_external_sender(
            "Customer <buyer@customer-d.example>", "company.example"
        ))

    def test_case_insensitive(self):
        self.assertFalse(self.ept._is_external_sender(
            "x@company.example", "company.example"
        ))

    def test_missing_at_sign_treated_as_not_external(self):
        self.assertFalse(self.ept._is_external_sender("garbage", "company.example"))


class ScanOneMailboxTests(unittest.TestCase):
    def setUp(self):
        from agent_core import email_pending_tracker
        self.ept = email_pending_tracker

    def _fake_service(self, threads, thread_details):
        """threads: list of {"id": ...}; thread_details: {id: messages-payload}."""
        svc = mock.MagicMock()
        svc.users.return_value.threads.return_value.list.return_value.execute.return_value = {
            "threads": threads
        }
        def fake_get(userId, id, format, metadataHeaders):
            return mock.MagicMock(execute=lambda: thread_details[id])
        svc.users.return_value.threads.return_value.get.side_effect = fake_get
        return svc

    def test_finds_thread_awaiting_reply(self):
        threads = [{"id": "t1"}]
        details = {
            "t1": {"messages": [
                {"payload": {"headers": [
                    {"name": "From", "value": "buyer@customer-d.example"},
                    {"name": "Subject", "value": "PO update"},
                ]}, "internalDate": "1000"},
            ]},
        }
        svc = self._fake_service(threads, details)
        errors = []
        with mock.patch(
            "agent_core.google_auth.get_service_for_account", return_value=svc
        ):
            findings = self.ept._scan_one_mailbox(
                "twsales@company.example", "/fake/sa.json", 7, 20, errors
            )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["mailbox"], "twsales@company.example")
        self.assertEqual(findings[0]["from"], "buyer@customer-d.example")
        self.assertEqual(findings[0]["subject"], "PO update")
        self.assertEqual(errors, [])

    def test_own_reply_is_last_message_not_flagged(self):
        threads = [{"id": "t1"}]
        details = {
            "t1": {"messages": [
                {"payload": {"headers": [
                    {"name": "From", "value": "buyer@customer-d.example"},
                    {"name": "Subject", "value": "PO update"},
                ]}, "internalDate": "1000"},
                {"payload": {"headers": [
                    {"name": "From", "value": "twsales@company.example"},
                    {"name": "Subject", "value": "Re: PO update"},
                ]}, "internalDate": "2000"},
            ]},
        }
        svc = self._fake_service(threads, details)
        errors = []
        with mock.patch(
            "agent_core.google_auth.get_service_for_account", return_value=svc
        ):
            findings = self.ept._scan_one_mailbox(
                "twsales@company.example", "/fake/sa.json", 7, 20, errors
            )
        self.assertEqual(findings, [])

    def test_connection_failure_recorded_as_error_not_raised(self):
        errors = []
        with mock.patch(
            "agent_core.google_auth.get_service_for_account",
            side_effect=RuntimeError("auth boom"),
        ):
            findings = self.ept._scan_one_mailbox(
                "twsales@company.example", "/fake/sa.json", 7, 20, errors
            )
        self.assertEqual(findings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("twsales@company.example", errors[0])

    def test_malformed_thread_skipped_not_fatal(self):
        threads = [{"id": "t1"}, {"id": "t2"}]
        # t1 的 get() 拋例外（模擬單一 thread 壞掉），t2 正常。
        details = {
            "t2": {"messages": [
                {"payload": {"headers": [
                    {"name": "From", "value": "x@external.com"},
                    {"name": "Subject", "value": "s2"},
                ]}, "internalDate": "1000"},
            ]},
        }
        svc = mock.MagicMock()
        svc.users.return_value.threads.return_value.list.return_value.execute.return_value = {
            "threads": threads
        }
        def fake_get(userId, id, format, metadataHeaders):
            if id == "t1":
                raise RuntimeError("thread gone")
            return mock.MagicMock(execute=lambda: details[id])
        svc.users.return_value.threads.return_value.get.side_effect = fake_get
        errors = []
        with mock.patch(
            "agent_core.google_auth.get_service_for_account", return_value=svc
        ):
            findings = self.ept._scan_one_mailbox(
                "twsales@company.example", "/fake/sa.json", 7, 20, errors
            )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["thread_id"], "t2")


class ListUnansweredCompanyThreadsTests(unittest.TestCase):
    def setUp(self):
        from agent_core import email_pending_tracker
        self.ept = email_pending_tracker

    def test_no_gmail_accounts_configured(self):
        with mock.patch(
            "agent_core.ingest.sync_config.load_targets", return_value={}
        ):
            out = self.ept.list_unanswered_company_threads()
        self.assertIn("尚未設定", out)

    def test_aggregates_across_mailboxes_and_reports_errors(self):
        targets = {
            "gmail_accounts": [
                {"mailbox": "twsales@company.example", "service_account_file": "sa.json"},
                {"mailbox": "broken@company.example", "service_account_file": "sa.json"},
            ]
        }
        with mock.patch(
            "agent_core.ingest.sync_config.load_targets", return_value=targets
        ), mock.patch.object(
            self.ept, "_scan_one_mailbox",
            side_effect=[
                [{"mailbox": "twsales@company.example", "from": "x@ext.com",
                  "subject": "s", "thread_id": "t1"}],
                [],
            ],
        ) as scan:
            out = self.ept.list_unanswered_company_threads(days=3, limit_per_mailbox=5)
        self.assertEqual(scan.call_count, 2)
        self.assertIn("twsales@company.example", out)
        self.assertIn("x@ext.com", out)
        self.assertIn("共 1 筆", out)

    def test_empty_result_says_nothing_found(self):
        targets = {"gmail_accounts": [
            {"mailbox": "twsales@company.example", "service_account_file": "sa.json"},
        ]}
        with mock.patch(
            "agent_core.ingest.sync_config.load_targets", return_value=targets
        ), mock.patch.object(self.ept, "_scan_one_mailbox", return_value=[]):
            out = self.ept.list_unanswered_company_threads()
        self.assertIn("沒有", out)

    def test_marked_background_safe(self):
        self.assertTrue(
            getattr(self.ept.list_unanswered_company_threads, "background_safe", False)
        )

    def test_all_mailboxes_failing_does_not_claim_all_clear(self):
        """🚨 同 #448 shipping「掃不到 ≠ 逾期」：全部信箱掛掉時零結果，
        不可以回報「全公司信箱都沒有未回信」——那是拿網路故障當事實。"""
        targets = {"gmail_accounts": [
            {"mailbox": "twsales@company.example", "service_account_file": "sa.json"},
            {"mailbox": "purchase@company.example", "service_account_file": "sa.json"},
        ]}

        def blow_up(mailbox, sa_file, days, limit, errors):
            errors.append(f"{mailbox}: TransportError: Failed to resolve")
            return []

        with mock.patch(
            "agent_core.ingest.sync_config.load_targets", return_value=targets
        ), mock.patch.object(self.ept, "_scan_one_mailbox", side_effect=blow_up):
            out = self.ept.list_unanswered_company_threads()
        self.assertNotIn("全公司信箱都沒有", out)
        self.assertIn("不等於沒有未回信件", out)
        self.assertIn("2 個信箱只有 0 個查核完整", out)

    def test_partial_failure_keeps_findings_but_flags_incompleteness(self):
        """一個信箱成功、一個失敗：列出來的照列，但不可以說「不影響結果」。"""
        targets = {"gmail_accounts": [
            {"mailbox": "ok@company.example", "service_account_file": "sa.json"},
            {"mailbox": "bad@company.example", "service_account_file": "sa.json"},
        ]}

        def scan(mailbox, sa_file, days, limit, errors):
            if mailbox.startswith("bad"):
                errors.append(f"{mailbox}: TransportError")
                return []
            return [{"mailbox": mailbox, "from": "x@ext.com",
                     "subject": "s", "thread_id": "t1"}]

        with mock.patch(
            "agent_core.ingest.sync_config.load_targets", return_value=targets
        ), mock.patch.object(self.ept, "_scan_one_mailbox", side_effect=scan):
            out = self.ept.list_unanswered_company_threads()
        self.assertIn("共 1 筆", out)
        self.assertIn("可能有漏", out)
        self.assertNotIn("不影響上面已列出的結果", out)


class ThreadReadFailureTests(unittest.TestCase):
    """單 thread 讀取失敗要計數，不可以靜默從清單消失。"""

    def setUp(self):
        from agent_core import email_pending_tracker
        self.ept = email_pending_tracker

    def _svc(self, thread_ids, failing):
        users = mock.MagicMock()
        users.threads.return_value.list.return_value.execute.return_value = {
            "threads": [{"id": t} for t in thread_ids],
        }

        def get(userId, id, format, metadataHeaders):
            if id in failing:
                raise RuntimeError("Max retries exceeded: Failed to resolve")
            return mock.MagicMock(execute=lambda: {"messages": [
                {"payload": {"headers": [
                    {"name": "From", "value": "someone@outside.com"},
                    {"name": "Subject", "value": "hi"},
                ]}, "internalDate": "1"},
            ]})

        users.threads.return_value.get.side_effect = get
        svc = mock.MagicMock()
        svc.users.return_value = users
        return svc

    def test_failed_thread_reads_are_counted_as_errors(self):
        errors = []
        with mock.patch(
            "agent_core.google_auth.get_service_for_account",
            return_value=self._svc(["t1", "t2", "t3"], failing={"t2", "t3"}),
        ):
            findings = self.ept._scan_one_mailbox(
                "twsales@company.example", "/fake/sa.json", 7, 20, errors)
        self.assertEqual(len(findings), 1)          # 只有 t1 讀成功
        self.assertEqual(len(errors), 1)
        self.assertIn("2 個 thread 讀取失敗", errors[0])

    def test_clean_scan_records_no_errors(self):
        errors = []
        with mock.patch(
            "agent_core.google_auth.get_service_for_account",
            return_value=self._svc(["t1", "t2"], failing=set()),
        ):
            findings = self.ept._scan_one_mailbox(
                "twsales@company.example", "/fake/sa.json", 7, 20, errors)
        self.assertEqual(len(findings), 2)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
