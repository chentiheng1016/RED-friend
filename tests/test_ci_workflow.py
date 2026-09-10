"""CI workflow regression tests."""
from __future__ import annotations

import os
import sys
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class GitHubActionsRuntimeTests(unittest.TestCase):
    def test_core_actions_use_node24_runtime_majors(self):
        with open(
            os.path.join(_REPO_ROOT, ".github", "workflows", "ci.yml"),
            encoding="utf-8",
        ) as f:
            workflow = f.read()

        self.assertIn("uses: actions/checkout@v7", workflow)
        self.assertIn("uses: actions/setup-node@v6", workflow)
        self.assertIn("uses: actions/setup-python@v6", workflow)
        self.assertNotIn("uses: actions/checkout@v4", workflow)
        self.assertNotIn("uses: actions/setup-node@v4", workflow)
        self.assertNotIn("uses: actions/setup-python@v5", workflow)


if __name__ == "__main__":
    unittest.main()
