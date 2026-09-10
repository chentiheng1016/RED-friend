"""Multi-account Gmail sync: secondary mailboxes via service-account delegation.

Covers the two seams added for syncing a second company's Workspace mailbox:
  * gmail_sync.sync_query accepts an explicit (delegated) service + mailbox_email
    and never falls back to the primary OAuth account.
  * rag_runner._sync_gmail_accounts builds a delegated service per configured
    account, tolerates incomplete/erroring entries, and tags each result.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


_SECONDARY_MAILBOX = "gm@company.example"


def _gmail_service_for_list(threads):
    list_req = mock.MagicMock()
    list_req.execute.return_value = {"threads": threads}
    threads_obj = mock.MagicMock()
    threads_obj.list.return_value = list_req
    profile_req = mock.MagicMock()
    profile_req.execute.return_value = {"emailAddress": _SECONDARY_MAILBOX}
    users_obj = mock.MagicMock()
    users_obj.threads.return_value = threads_obj
    users_obj.getProfile.return_value = profile_req
    service = mock.MagicMock()
    service.users.return_value = users_obj
    return service


class SyncQueryServicePassthroughTests(unittest.TestCase):
    def test_sync_query_uses_passed_service_not_primary_oauth(self):
        from agent_core.ingest import gmail_sync

        service = _gmail_service_for_list([])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {}

        # If sync_query reached for the primary OAuth account this would blow up.
        def _boom(*a, **k):
            raise AssertionError("sync_query must not call get_service when a "
                                 "service is supplied")

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service", side_effect=_boom):
            result = gmail_sync.sync_query(
                "newer_than:1d",
                max_threads=5,
                service=service,
                mailbox_email=_SECONDARY_MAILBOX,
            )

        self.assertEqual(result["total"], 0)
        # The delegated service was the one queried.
        service.users.return_value.threads.return_value.list.assert_called()

    def test_sync_query_stamps_secondary_mailbox_on_chunks(self):
        from agent_core.ingest import gmail_sync

        service = _gmail_service_for_list([{"id": "t1", "historyId": "h1"}])
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {}

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "get_service",
                               side_effect=AssertionError("must not be called")), \
             mock.patch.object(
                 gmail_sync, "_thread_text",
                 return_value=("body", {"subject": "S", "sender": "x@company.example",
                                        "date": "today", "history_id": "h1"}),
             ):
            gmail_sync.sync_query(
                "newer_than:1d", max_threads=1,
                service=service, mailbox_email=_SECONDARY_MAILBOX,
            )

        metas = store.upsert_batch.call_args.args[2]
        self.assertEqual(metas[0]["mailbox_email"], _SECONDARY_MAILBOX)


class SyncGmailAccountsTests(unittest.TestCase):
    def _run(self, accounts):
        from agent_core.ingest import rag_runner

        sentinel_service = object()
        gmail_sync = mock.MagicMock()
        gmail_sync.sync_query.return_value = {"total": 3, "synced": 3, "skipped": 0}
        errors: list[str] = []

        with mock.patch(
            "agent_core.google_auth.get_service_for_account",
            return_value=sentinel_service,
        ) as get_svc:
            results = rag_runner._sync_gmail_accounts(accounts, gmail_sync, errors)
        return results, errors, gmail_sync, get_svc, sentinel_service

    def test_valid_account_builds_delegated_service_and_syncs(self):
        accounts = [{
            "account_key": "jaifung",  # pragma: allowlist secret
            "mailbox": _SECONDARY_MAILBOX,
            "service_account_file": "var/state/google/service_account.json",
            "gmail_query": "newer_than:730d",
            "max_threads": 100,
        }]
        results, errors, gmail_sync, get_svc, svc = self._run(accounts)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["mailbox"], _SECONDARY_MAILBOX)
        self.assertEqual(results[0]["synced"], 3)
        get_svc.assert_called_once_with(
            "jaifung", "gmail", "v1",
            service_account_file="var/state/google/service_account.json",
            subject=_SECONDARY_MAILBOX,
            scopes=None,
        )
        gmail_sync.sync_query.assert_called_once_with(
            "newer_than:730d", 100, service=svc, mailbox_email=_SECONDARY_MAILBOX,
            service_builder=mock.ANY, fetch_workers=6,
        )

    def test_incomplete_entry_is_skipped_with_error_not_crash(self):
        accounts = [{"account_key": "broken", "mailbox": _SECONDARY_MAILBOX}]  # pragma: allowlist secret
        results, errors, gmail_sync, get_svc, _ = self._run(accounts)

        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0])
        get_svc.assert_not_called()
        gmail_sync.sync_query.assert_not_called()

    def test_one_account_failure_is_non_fatal(self):
        from agent_core.ingest import rag_runner

        accounts = [
            {"account_key": "bad", "mailbox": "a@company.example",  # pragma: allowlist secret
             "service_account_file": "k.json", "gmail_query": "newer_than:1d"},
            {"account_key": "good", "mailbox": "b@company.example",  # pragma: allowlist secret
             "service_account_file": "k.json", "gmail_query": "newer_than:1d"},
        ]
        gmail_sync = mock.MagicMock()
        gmail_sync.sync_query.return_value = {"total": 1, "synced": 1, "skipped": 0}
        errors: list[str] = []

        def _svc(key, *a, **k):
            if key == "bad":
                raise RuntimeError("delegation not authorised")
            return object()

        with mock.patch("agent_core.google_auth.get_service_for_account",
                        side_effect=_svc):
            results = rag_runner._sync_gmail_accounts(accounts, gmail_sync, errors)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["account"], "good")
        self.assertEqual(len(errors), 1)
        self.assertIn("bad", errors[0])

    def test_non_list_accounts_returns_empty(self):
        from agent_core.ingest import rag_runner
        self.assertEqual(rag_runner._sync_gmail_accounts(None, mock.MagicMock(), []), [])


if __name__ == "__main__":
    unittest.main()
