from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class FreshDiagnosticsTests(unittest.TestCase):
    def test_env_timeout_parser_falls_back_for_bad_values(self):
        from agent_core import fresh_diagnostics

        with mock.patch.dict(os.environ, {"RED_FRESH_DIAGNOSTICS_TIMEOUT_S": "soon"}):
            self.assertEqual(
                fresh_diagnostics._env_float(
                    "RED_FRESH_DIAGNOSTICS_TIMEOUT_S",
                    25,
                    min_value=1,
                    max_value=300,
                ),
                25,
            )

    def test_fresh_call_returns_subprocess_stdout(self):
        from agent_core import fresh_diagnostics

        with mock.patch.object(
            fresh_diagnostics.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="fresh", stderr=""),
        ) as run:
            out = fresh_diagnostics._fresh_call("m", "f", {"x": 1}, lambda _arg: "fallback")

        self.assertEqual(out, "fresh")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn('"x": 1', argv[3])

    def test_fresh_call_falls_back_on_subprocess_failure(self):
        from agent_core import fresh_diagnostics

        with mock.patch.object(
            fresh_diagnostics.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=7, stdout="", stderr="boom"),
        ):
            out = fresh_diagnostics._fresh_call("m", "f", "arg", lambda arg: f"fallback {arg}")

        self.assertIn("fresh diagnostic subprocess failed", out)
        self.assertIn("fallback arg", out)

    def test_registry_uses_fresh_diagnostic_wrappers(self):
        from agent_core import fresh_diagnostics, tool_registry_catalog

        names = {getattr(tool, "__name__", ""): tool for tool in tool_registry_catalog.BASE_BUILTIN_TOOLS}
        self.assertIs(names["system_status"], fresh_diagnostics.system_status)
        self.assertIs(names["system_alerts"], fresh_diagnostics.system_alerts)


if __name__ == "__main__":
    unittest.main()
