from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class TestSecurityWorkMode(unittest.TestCase):
    def setUp(self):
        from agent_core import mode_manager

        self.mode_manager = mode_manager
        self._tmpdir = tempfile.mkdtemp(prefix="red_security_mode_")
        self._orig_mode_file = mode_manager._MODE_FILE
        self._orig_history_file = mode_manager._HISTORY_FILE
        mode_manager._MODE_FILE = os.path.join(self._tmpdir, "work_mode.json")
        mode_manager._HISTORY_FILE = os.path.join(
            self._tmpdir, "work_mode_history.jsonl"
        )

    def tearDown(self):
        self.mode_manager._MODE_FILE = self._orig_mode_file
        self.mode_manager._HISTORY_FILE = self._orig_history_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_security_mode_can_be_selected_and_listed(self):
        from agent_core.mode_manager import (
            get_current_mode,
            list_work_modes,
            set_work_mode,
        )
        from agent_core.mode_policy import known_modes
        from agent_core.persona_profiles import list_known_modes

        result = set_work_mode("security")

        self.assertTrue(result.ok)
        self.assertEqual(get_current_mode(), "security")
        self.assertIn("security", known_modes())
        self.assertIn("security", list_known_modes())
        self.assertIn("security", list_work_modes())

    def test_security_mode_persona_keeps_authorized_research_boundary(self):
        from agent_core.persona_profiles import persona_for

        text = persona_for("security")

        self.assertIn("安全研究", text)
        self.assertIn("授權", text)
        self.assertIn("不得提供", text)
        self.assertIn("risk_guard", text)
        self.assertIn("policy_engine", text)

    def test_security_mode_narrows_to_security_research_tools(self):
        from agent_core.mode_policy import filter_tools_by_mode

        def risk_assessment():
            pass

        def run_shell():
            pass

        def media_security_blueprint():
            pass

        def send_gmail():
            pass

        def remember():
            pass

        def fill_form():
            pass

        tools = [
            risk_assessment,
            run_shell,
            media_security_blueprint,
            send_gmail,
            remember,
            fill_form,
        ]
        out = filter_tools_by_mode(tools, "security")
        names = [getattr(fn, "__name__", "") for fn in out]

        self.assertIn("risk_assessment", names)
        self.assertIn("run_shell", names)
        self.assertIn("media_security_blueprint", names)
        self.assertNotIn("send_gmail", names)
        self.assertNotIn("remember", names)
        self.assertNotIn("fill_form", names)


if __name__ == "__main__":
    unittest.main()
