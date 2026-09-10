"""Regression (review finding #6): reload_skills() used to `tools_list.extend(
new_tools)` with NO schema validation, while startup runs every tool through
_validate_tool_schemas. So a hot-reloaded skill with a Gemini-invalid schema
sailed in, reload reported success, and the NEXT user message 400'd the whole
session. Pin that reload validates and drops broken tools just like startup.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ReloadSkillsValidationTests(unittest.TestCase):
    def setUp(self):
        from agent_core import tool_registry

        self.tr = tool_registry
        # reload_skills mutates module-level tools_list + chat_state in place;
        # snapshot and restore so we don't leak into other tests.
        self._orig_tools = list(tool_registry.tools_list)
        self._orig_chat = dict(tool_registry.chat_state)

        def _restore():
            tool_registry.tools_list[:] = self._orig_tools
            tool_registry.chat_state.clear()
            tool_registry.chat_state.update(self._orig_chat)

        self.addCleanup(_restore)

    @staticmethod
    def _skill(name):
        def fn():
            return None

        fn.__name__ = name
        fn._is_skill = True
        return fn

    def test_reload_drops_broken_schema_tools(self):
        tr = self.tr
        good = self._skill("good_skill")
        bad = self._skill("bad_skill")

        with mock.patch.object(tr, "_load_skills_from_dir", return_value=([good, bad], {"n": 2})), \
             mock.patch.object(
                 tr, "_validate_tool_schemas",
                 return_value=([good], [("bad_skill", "array 參數缺 item type")]),
             ), \
             mock.patch.object(tr, "_build_chat_fn", lambda: object()):
            msg = tr.reload_skills()

        self.assertIn(good, tr.tools_list)
        # The whole point: the broken tool must NOT reach the live list.
        self.assertNotIn(bad, tr.tools_list)
        self.assertIn("bad_skill", msg)
        self.assertIn("剔除", msg)

    def test_reload_clean_when_all_valid(self):
        tr = self.tr
        s = self._skill("ok_skill")

        with mock.patch.object(tr, "_load_skills_from_dir", return_value=([s], {})), \
             mock.patch.object(tr, "_validate_tool_schemas", return_value=([s], [])) as validate, \
             mock.patch.object(tr, "_build_chat_fn", lambda: object()):
            msg = tr.reload_skills()

        # reload actually routed through validation (the old code didn't).
        validate.assert_called_once()
        self.assertIn(s, tr.tools_list)
        self.assertNotIn("⚠️", msg)


if __name__ == "__main__":
    unittest.main()
