"""Tests for system_status() input-type robustness.

Real production failure mode this fixes: Gemini (or any LLM tool caller)
sometimes passes the `sections` argument as a list — e.g.
``["daemons", "cost"]`` — when it interprets the docstring's
"comma-separated section name" as semantically equivalent to a list.

The previous implementation called ``sections.split(",")`` directly, so a
list / None / int / dict argument crashed with ``AttributeError`` that
the LLM then surfaced to the user as "system_status broken". That's a UX
gap, not a real outage — the tool's purpose is to give a snapshot of
system health, and refusing to do that because the input shape was
slightly off is exactly the wrong default.
"""
from __future__ import annotations

import os
import sys
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class SystemStatusInputCoercionTests(unittest.TestCase):
    """All exercised inputs must produce a non-empty string instead of
    raising. We don't pin exact content — the dashboard's section bodies
    pull from live state files and would make these tests flaky."""

    def setUp(self):
        from agent_core.dashboard import system_status
        self.system_status = system_status
        # system_status → _ensure_chroma_endpoint 會在共用 chroma server 活著時
        # 把 RED_CHROMA_HTTP_URL / RED_EMBED_DIM 寫進 os.environ（process 級、
        # 給人工 red-status 的便利），會洩漏給同輪後續測試模組。這裡照常真跑
        # （本檔本來就吃 live state），但 env 副作用要在 tearDown 收乾淨。
        self._env_saved = {
            k: os.environ.get(k) for k in ("RED_CHROMA_HTTP_URL", "RED_EMBED_DIM")
        }

    def tearDown(self):
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _assert_returns_string(self, result, label: str):
        self.assertIsInstance(result, str, label)
        # The header alone is ~80 chars; anything <50 means we crashed
        # mid-render or returned an empty error.
        self.assertGreater(len(result), 50, f"{label}: too short, likely crashed")

    def test_default_no_arg_returns_full_dashboard(self):
        self._assert_returns_string(self.system_status(), "default")

    def test_empty_string_treated_as_default(self):
        self._assert_returns_string(self.system_status(""), "empty str")

    def test_comma_separated_string_filters_sections(self):
        result = self.system_status("daemons,cost")
        self._assert_returns_string(result, "comma str")

    def test_single_section_string(self):
        self._assert_returns_string(self.system_status("daemons"), "single str")

    def test_list_argument_does_not_crash(self):
        """The actual production bug: Gemini passed ['daemons', 'cost']
        and got AttributeError: 'list' object has no attribute 'split'."""
        result = self.system_status(["daemons", "cost"])
        self._assert_returns_string(result, "LLM list")

    def test_list_with_one_element(self):
        result = self.system_status(["daemons"])
        self._assert_returns_string(result, "list of one")

    def test_none_treated_as_default(self):
        self._assert_returns_string(self.system_status(None), "None")

    def test_integer_does_not_crash(self):
        """Defensive coercion — even nonsense input yields a graceful
        (likely empty body but valid header) response."""
        self._assert_returns_string(self.system_status(123), "integer")

    def test_dict_sections_filter_is_honored(self):
        result = self.system_status({"sections": "daemons"})
        self._assert_returns_string(result, "dict")
        self.assertIn("Daemon 健康", result)
        self.assertNotIn("Gemini 成本", result)


if __name__ == "__main__":
    unittest.main()
