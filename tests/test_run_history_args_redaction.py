"""Regression (review finding #20): run_history stored positional `args` via
_safe_stringify with NO redaction, while `result` went through log_redact and
kwargs got key-based redaction. A secret passed positionally — run_shell("…
Bearer …"), set_secret("name", "AIza…") — landed verbatim in runs/*.json /
index.jsonl, which are LLM-readable via show_run/find_past_actions. Pin that
args are value-scrubbed too.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class RunHistoryArgsRedactionTests(unittest.TestCase):
    def _capture_record(self, call):
        from agent_core import run_history

        captured = {}
        with mock.patch.object(run_history, "_write_record", side_effect=lambda rec: captured.update(rec)):
            call(run_history)
        return captured

    def test_bearer_token_positional_arg_is_redacted(self):
        planted = "ya29.abcDEF1234567890token"  # pragma: allowlist secret

        def call(run_history):
            @run_history.audited()
            def fake_run_shell(cmd):
                return "ok"

            fake_run_shell(f"curl -H 'Authorization: Bearer {planted}' https://x")

        rec = self._capture_record(call)
        self.assertNotIn(planted, rec["args"])
        self.assertIn("REDACTED", rec["args"])

    def test_google_api_key_positional_arg_is_redacted(self):
        planted = "AIzaSyA1234567890abcdefghijklmnopqrstuvw"  # pragma: allowlist secret

        def call(run_history):
            @run_history.audited()
            def store_secret(name, value):
                return "stored"

            store_secret("gemini-api-key", planted)

        rec = self._capture_record(call)
        self.assertNotIn(planted, rec["args"])

    def test_benign_args_pass_through(self):
        def call(run_history):
            @run_history.audited()
            def add_task(title):
                return "ok"

            add_task("買牛奶")

        rec = self._capture_record(call)
        self.assertIn("買牛奶", rec["args"])

    def test_redact_secrets_helper(self):
        from agent_core import run_history

        masked = run_history._redact_secrets("key=AIzaSyA1234567890abcdefghijklmnopqrstuvw")  # pragma: allowlist secret
        self.assertIn("REDACTED", masked)
        # benign text is untouched
        self.assertEqual(run_history._redact_secrets("hello world"), "hello world")


if __name__ == "__main__":
    unittest.main()
