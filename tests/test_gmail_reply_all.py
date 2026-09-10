"""Regression (review finding #2): reply_gmail(reply_all=True) used to read
only the From and Cc headers, silently dropping anyone addressed directly in
the original `To`. These tests pin that reply-all loops in the whole thread
(To + Cc), excludes our own address and the sender, and dedupes — while a
plain reply still carries only the explicit cc.
"""
from __future__ import annotations

import os
import sys
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeMessages:
    def __init__(self, get_result):
        self._get_result = get_result
        self.sent = []

    def get(self, userId=None, id=None, format=None):
        return _Exec(self._get_result)

    def send(self, userId=None, body=None):
        self.sent.append(body)
        return _Exec({"id": "sent-1"})


class _FakeUsers:
    def __init__(self, messages, profile_email):
        self._messages = messages
        self._profile_email = profile_email
        self.profile_calls = 0

    def messages(self):
        return self._messages

    def getProfile(self, userId=None):
        self.profile_calls += 1
        return _Exec({"emailAddress": self._profile_email})


class _FakeService:
    def __init__(self, users):
        self._users = users

    def users(self):
        return self._users


class _FakeMime:
    """Minimal stand-in for the MIME message build_mime_fn returns."""

    def __init__(self):
        self.headers = {}

    def __setitem__(self, key, value):
        self.headers[key] = value

    def as_bytes(self):
        return b"raw-bytes"


def _make_service(headers, profile_email="owner@company.example", thread_id="t-1"):
    payload = {"headers": [{"name": k, "value": v} for k, v in headers.items()]}
    get_result = {"threadId": thread_id, "payload": payload}
    messages = _FakeMessages(get_result)
    users = _FakeUsers(messages, profile_email)
    return _FakeService(users), messages, users


class ReplyAllRecipientsTests(unittest.TestCase):
    def _reply(self, headers, *, reply_all=True, cc="", profile_email="owner@company.example"):
        from agent_core import gmail_ops

        service, messages, users = _make_service(headers, profile_email)
        captured = {}

        def fake_index(text, source=None, metadata=None):
            captured["text"] = text
            captured["metadata"] = metadata or {}

        result = gmail_ops.reply_gmail(
            "msg-1",
            "Body text",
            reply_all=reply_all,
            cc=cc,
            attachments=None,
            get_service=lambda *a, **k: service,
            append_signature_fn=lambda b: b,
            build_mime_fn=lambda body, attachments, html=False: _FakeMime(),
            split_paths_fn=lambda a: [],
            index_memory_fn=fake_index,
        )
        return result, captured, messages, users

    def test_reply_all_includes_original_to_recipients(self):
        headers = {
            "Subject": "Order 123",
            "From": "Alice <alice@customer.com>",
            "To": "Owner <owner@company.example>, Bob <bob@partner.com>",
            "Cc": "Carol <carol@partner.com>",
            "Message-ID": "<abc@mail>",
        }
        result, captured, messages, _ = self._reply(headers)
        cc = captured["metadata"]["cc"]
        # Bob was in the original To — the whole point of the fix.
        self.assertIn("bob@partner.com", cc)
        # Carol (original Cc) still included.
        self.assertIn("carol@partner.com", cc)
        # Never Cc ourselves.
        self.assertNotIn("owner@company.example", cc)
        # Sender goes to To, not Cc.
        self.assertNotIn("alice@customer.com", cc)
        # The reply was actually sent.
        self.assertEqual(len(messages.sent), 1)
        self.assertTrue(result.startswith("已回覆"))

    def test_reply_all_dedupes_addresses_case_insensitively(self):
        headers = {
            "Subject": "Re: Spec",
            "From": "alice@customer.com",
            "To": "bob@partner.com, BOB@partner.com",
            "Cc": "",
        }
        _result, captured, _messages, _ = self._reply(headers)
        cc = captured["metadata"]["cc"]
        self.assertEqual(cc.lower().count("bob@partner.com"), 1)

    def test_reply_all_merges_explicit_cc(self):
        headers = {
            "Subject": "Hi",
            "From": "alice@customer.com",
            "To": "bob@partner.com",
            "Cc": "",
        }
        _result, captured, _messages, _ = self._reply(headers, reply_all=True, cc="extra@x.com")
        cc = captured["metadata"]["cc"]
        self.assertIn("bob@partner.com", cc)
        self.assertIn("extra@x.com", cc)

    def test_plain_reply_keeps_only_explicit_cc(self):
        headers = {
            "Subject": "Hi",
            "From": "alice@customer.com",
            "To": "owner@company.example, bob@partner.com",
            "Cc": "carol@partner.com",
        }
        _result, captured, _messages, users = self._reply(headers, reply_all=False, cc="extra@x.com")
        cc = captured["metadata"]["cc"]
        # No thread expansion on a plain reply.
        self.assertEqual(cc, "extra@x.com")
        self.assertNotIn("bob@partner.com", cc)
        # getProfile only needed for reply-all self-exclusion.
        self.assertEqual(users.profile_calls, 0)

    def test_reply_all_survives_getprofile_failure(self):
        # If getProfile throws, we skip self-exclusion rather than break.
        from agent_core import gmail_ops

        headers = {
            "Subject": "Hi",
            "From": "alice@customer.com",
            "To": "bob@partner.com",
            "Cc": "",
        }
        service, messages, users = _make_service(headers)

        def boom(userId=None):
            raise RuntimeError("profile unavailable")

        users.getProfile = boom
        captured = {}

        result = gmail_ops.reply_gmail(
            "msg-1",
            "Body",
            reply_all=True,
            cc="",
            attachments=None,
            get_service=lambda *a, **k: service,
            append_signature_fn=lambda b: b,
            build_mime_fn=lambda body, attachments, html=False: _FakeMime(),
            split_paths_fn=lambda a: [],
            index_memory_fn=lambda *a, **k: captured.update(metadata=k.get("metadata")),
        )
        self.assertTrue(result.startswith("已回覆"))
        self.assertIn("bob@partner.com", captured["metadata"]["cc"])


if __name__ == "__main__":
    unittest.main()
