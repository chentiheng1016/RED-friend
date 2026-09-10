"""Tests for RPA-oriented helper modules.

These cover the building blocks behind data-entry automation:
- accessibility tree matching
- keyboard input safety rails
- workflow retry / recovery behavior
"""
import os
import sys
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import accessibility
from agent_core import input_devices
from agent_core import workflow


class AccessibilityHelperTests(unittest.TestCase):
    def test_find_matching_walks_tree_and_collects_multiple_hits(self):
        tree = {
            "role": "AXWindow",
            "title": "Main",
            "children": [
                {"role": "AXButton", "title": "儲存"},
                {
                    "role": "AXGroup",
                    "title": "Nested",
                    "children": [
                        {"role": "AXButton", "title": "儲存並關閉"},
                        {"role": "AXTextField", "title": "客戶名稱"},
                    ],
                },
            ],
        }

        def fake_get_attr(node, attr):
            mapping = {
                accessibility._ROLE_ATTR: "role",
                accessibility._TITLE_ATTR: "title",
                accessibility._CHILDREN_ATTR: "children",
            }
            return node.get(mapping[attr])

        with mock.patch.object(accessibility, "_get_attr", side_effect=fake_get_attr):
            matches = accessibility._find_matching(
                tree,
                lambda node: "儲存" in str(accessibility._get_attr(node, accessibility._TITLE_ATTR) or ""),
                max_depth=5,
            )

        titles = [node["title"] for node in matches]
        self.assertEqual(titles, ["儲存", "儲存並關閉"])


class InputDeviceSafetyTests(unittest.TestCase):
    def test_press_keys_blocks_dangerous_shortcut(self):
        with mock.patch.object(input_devices, "pyautogui", object()):
            out = input_devices.press_keys("cmd+shift+delete")
        self.assertIn("已攔截", out)

    def test_type_text_force_keystroke_rejects_non_ascii(self):
        with mock.patch.object(input_devices, "pyautogui", object()):
            out = input_devices.type_text("中文ABC", force_keystroke=True)
        self.assertIn("只支援純 ASCII", out)

    def test_type_text_paste_mode_uses_clipboard_and_hotkey(self):
        fake_gui = types.SimpleNamespace(hotkey=mock.Mock())
        fake_pyperclip = types.SimpleNamespace(
            paste=mock.Mock(return_value="old"),
            copy=mock.Mock(),
        )

        with mock.patch.object(input_devices, "pyautogui", fake_gui), \
             mock.patch.dict(sys.modules, {"pyperclip": fake_pyperclip}), \
             mock.patch.object(input_devices.time, "sleep"):
            out = input_devices.type_text("hello")

        self.assertIn("剪貼簿貼上", out)
        fake_gui.hotkey.assert_called_once()
        self.assertGreaterEqual(fake_pyperclip.copy.call_count, 1)


class WorkflowStepTests(unittest.TestCase):
    def test_step_retries_and_eventually_succeeds(self):
        calls = []

        def flaky():
            calls.append("x")
            if len(calls) < 3:
                raise RuntimeError("boom")
            return "ok"

        with mock.patch.object(workflow.time, "sleep") as sleep_mock:
            out = workflow.step("flaky", flaky, retry=3, backoff="linear")

        self.assertEqual(out, "ok")
        self.assertEqual(len(calls), 3)
        sleep_mock.assert_any_call(1)
        sleep_mock.assert_any_call(2)

    def test_step_uses_on_fail_recovery(self):
        def always_fail():
            raise ValueError("bad")

        def recover(exc, record):
            return f"recovered:{type(exc).__name__}:{record['attempts']}"

        out = workflow.step("recoverable", always_fail, retry=2, on_fail=recover)
        self.assertEqual(out, "recovered:ValueError:2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
