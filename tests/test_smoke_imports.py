"""Baseline smoke tests — run before refactoring agent.py.

Purpose: establish a pre-refactor invariant that `import agent` works and the
core public surface (tools_list, _BUILTIN_TOOLS, agent_persona) has the
expected shape. If any of these fail after a refactor step, the step is
incomplete or broken.

Run: python -m unittest tests.test_smoke_imports
"""
import importlib
import os
import sys
import unittest


# Suppress startup prints from agent.py during import
os.environ.setdefault("AGENT_DAEMON_MODE", "1")

# Ensure repo root is on sys.path when run via `python -m unittest`
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class AgentImportSmoke(unittest.TestCase):
    """Invariants for `agent` module shape. Adjust thresholds only after
    intentional changes to the tool catalog or persona."""

    @classmethod
    def setUpClass(cls):
        # Clear any prior stubbed `agent` (e.g. from test_agent_daemon_smoke)
        sys.modules.pop("agent", None)
        sys.modules.pop("agent_daemon", None)
        cls.agent = importlib.import_module("agent")

    def test_agent_imports_cleanly(self):
        self.assertTrue(hasattr(self.agent, "__file__"))

    def test_builtin_tools_stable(self):
        tools = getattr(self.agent, "_BUILTIN_TOOLS", None)
        self.assertIsInstance(tools, list)
        # Current baseline: ~80 tools. Alarm if it drops sharply.
        self.assertGreaterEqual(len(tools), 60, "_BUILTIN_TOOLS shrank unexpectedly")
        # All entries must be callable (function references, not names)
        for fn in tools:
            self.assertTrue(callable(fn), f"non-callable entry in _BUILTIN_TOOLS: {fn!r}")

    def test_tools_list_is_builtin_plus_skills(self):
        tools_list = getattr(self.agent, "tools_list", None)
        builtins = getattr(self.agent, "_BUILTIN_TOOLS", [])
        self.assertIsInstance(tools_list, list)
        self.assertGreaterEqual(len(tools_list), len(builtins))

    def test_agent_persona_substantive(self):
        persona = getattr(self.agent, "agent_persona", "")
        self.assertIsInstance(persona, str)
        self.assertGreater(len(persona), 1000, "agent_persona is suspiciously short")
        # Must reference the assistant name — guards against a blank f-string
        self.assertIn("小紅", persona)

    def test_core_public_symbols_present(self):
        # Symbols that agent_daemon.py and other callers depend on. If any of
        # these disappears during refactor, downstream imports break.
        required = [
            "MEMORY_FILE",
            "GEMINI_MODEL",
            "get_service",
            "send_gmail",
            "summarize_inbox",
            "search_gmail",
            "_gemini_generate",
            "_classify_email_raw",
            "_load_daemon_tasks",
            "_save_daemon_tasks",
            "tools_list",
            "agent_persona",
            "recall",
            "telegram_push",
            "check_sample_deadlines",
            "health_check",
        ]
        missing = [name for name in required if not hasattr(self.agent, name)]
        self.assertEqual(missing, [], f"missing public symbols: {missing}")


class AgentDaemonImportSmoke(unittest.TestCase):
    """agent_daemon must be importable alongside the real agent module."""

    def test_daemon_imports_with_real_agent(self):
        # Real agent must already be loaded by the previous class; ensure daemon
        # can bind against it without patching.
        if "agent" not in sys.modules:
            sys.modules.pop("agent_daemon", None)
            importlib.import_module("agent")
        sys.modules.pop("agent_daemon", None)
        daemon = importlib.import_module("agent_daemon")
        self.assertTrue(hasattr(daemon, "__file__"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
