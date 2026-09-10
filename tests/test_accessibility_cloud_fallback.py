from __future__ import annotations

import builtins
import importlib
import sys
import unittest
from unittest import mock


class AccessibilityCloudFallbackTests(unittest.TestCase):
    def test_import_without_macos_applicationservices(self):
        original_import = builtins.__import__
        sys.modules.pop("agent_core.accessibility", None)

        def guarded_import(name, *args, **kwargs):
            if name == "ApplicationServices":
                raise ModuleNotFoundError("No module named 'ApplicationServices'")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            accessibility = importlib.import_module("agent_core.accessibility")

        self.assertIn("ApplicationServices", accessibility.ax_check_permission())
        self.assertIn("ApplicationServices", accessibility.ax_list_running_apps())

        sys.modules.pop("agent_core.accessibility", None)
        importlib.import_module("agent_core.accessibility")


if __name__ == "__main__":
    unittest.main()
