"""Regression (review finding #19): policy_engine computed forced_dry_run from
RED_FORCE_DRY_RUN_FOR but wrap_sensitive_tool only ever read policy.allow — so
an admin who set RED_FORCE_DRY_RUN_FOR=send_gmail expecting a simulation got
the tool executed for real. Pin that a forced tool is simulated, not run.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ForcedDryRunTests(unittest.TestCase):
    def _wrap(self, name, chat_id):
        from agent_core.tg_auth import wrap_sensitive_tool

        called = {"n": 0}

        def real_tool(to="", subject="", body="", **k):
            called["n"] += 1
            return f"SENT to {to}"

        real_tool.__name__ = name
        wrapped = wrap_sensitive_tool(real_tool, get_chat_id=lambda: chat_id)
        return wrapped, called

    def test_forced_dry_run_simulates_without_executing(self):
        wrapped, called = self._wrap("send_gmail", "200001")
        with mock.patch.dict(os.environ, {"RED_FORCE_DRY_RUN_FOR": "send_gmail"}):
            out = wrapped(to="a@b.com", subject="Hi", body="x")
        self.assertEqual(called["n"], 0, "forced dry-run MUST NOT execute the real tool")
        self.assertIn("DRY RUN", out)
        self.assertIn("send_gmail", out)

    def test_forced_dry_run_matches_glob(self):
        wrapped, called = self._wrap("send_gmail", "200002")
        with mock.patch.dict(os.environ, {"RED_FORCE_DRY_RUN_FOR": "send_*"}):
            out = wrapped(to="a@b.com")
        self.assertEqual(called["n"], 0)
        self.assertIn("DRY RUN", out)

    def test_without_force_still_requires_confirmation(self):
        # Control: same CONFIRM-tier tool, no force → normal +確認 gate, still
        # not executed (and NOT a dry-run message).
        wrapped, called = self._wrap("send_gmail", "200003")
        with mock.patch.dict(os.environ, {"RED_FORCE_DRY_RUN_FOR": ""}):
            out = wrapped(to="a@b.com")
        self.assertEqual(called["n"], 0)
        self.assertIn("需要大王確認", out)
        self.assertNotIn("DRY RUN", out)


if __name__ == "__main__":
    unittest.main()
