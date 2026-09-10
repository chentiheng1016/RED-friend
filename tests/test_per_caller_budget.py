"""Tests for the P4 per-caller tool-budget extension.

The existing tool_budgets module enforced only a *global* per-tool quota.
The P4 change adds an opt-in per-caller cap that prevents a single caller
(e.g. one Telegram chat_id) from eating the entire global quota for a tool.

These tests rely on the autouse `_isolate_tool_budgets` fixture in
conftest.py — it redirects `tool_budgets._BUDGET_DIR` to a tmp dir so
counters don't leak into the real var/data/.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core import tool_budgets as tb  # noqa: E402


class _IsolatedToolBudgetTestCase(unittest.TestCase):
    """Keep unittest runs from reading/writing real var/data/tool_budgets."""

    def setUp(self):
        super().setUp()
        self._budget_tmp = tempfile.mkdtemp(prefix="red_budget_test_")
        self._orig_budget_dir = tb._BUDGET_DIR
        tb._BUDGET_DIR = self._budget_tmp

    def tearDown(self):
        tb._BUDGET_DIR = self._orig_budget_dir
        shutil.rmtree(self._budget_tmp, ignore_errors=True)
        super().tearDown()


class TestBackwardsCompat(_IsolatedToolBudgetTestCase):
    """Without caller_id, behavior must be identical to pre-P4."""

    def test_check_budget_no_caller_id_unchanged(self):
        os.environ["RED_BUDGET_TEST_TOOL_X_DAILY"] = "3"
        try:
            ok, _ = tb.check_budget("test_tool_x")
            self.assertTrue(ok)
            for _ in range(3):
                tb.record_use("test_tool_x")
            ok, msg = tb.check_budget("test_tool_x")
            self.assertFalse(ok, "global daily cap should still trigger")
            self.assertIn("3", msg)
        finally:
            os.environ.pop("RED_BUDGET_TEST_TOOL_X_DAILY", None)

    def test_record_use_without_caller_does_not_track_per_caller(self):
        os.environ["RED_BUDGET_TEST_TOOL_Y_DAILY"] = "5"
        try:
            tb.record_use("test_tool_y")
            tb.record_use("test_tool_y")
            state = tb._load_today()
            rec = state.get("test_tool_y", {})
            self.assertEqual(rec.get("daily"), 2)
            # No by_caller dict was created.
            self.assertNotIn("by_caller", rec)
        finally:
            os.environ.pop("RED_BUDGET_TEST_TOOL_Y_DAILY", None)


class TestPerCallerCap(_IsolatedToolBudgetTestCase):
    """When per_caller_daily/_hourly is set, caller A exhausting their quota
    must NOT block caller B."""

    def setUp(self):
        super().setUp()
        # Enable per-caller cap of 2/day for the test tool, with a generous
        # 100/day global cap so global never trips first.
        os.environ["RED_BUDGET_TEST_PC_TOOL_DAILY"] = "100"
        os.environ["RED_BUDGET_TEST_PC_TOOL_PER_CALLER_DAILY"] = "2"

    def tearDown(self):
        os.environ.pop("RED_BUDGET_TEST_PC_TOOL_DAILY", None)
        os.environ.pop("RED_BUDGET_TEST_PC_TOOL_PER_CALLER_DAILY", None)
        os.environ.pop("RED_BUDGET_TEST_PC_TOOL_PER_CALLER_HOURLY", None)
        super().tearDown()

    def test_caller_a_exhaustion_does_not_block_caller_b(self):
        # A uses up its 2/day cap.
        for _ in range(2):
            ok, _ = tb.check_budget("test_pc_tool", caller_id="alice")
            self.assertTrue(ok)
            tb.record_use("test_pc_tool", caller_id="alice")
        # Third call by A is rejected per-caller.
        ok, msg = tb.check_budget("test_pc_tool", caller_id="alice")
        self.assertFalse(ok)
        self.assertIn("per-caller", msg)
        self.assertIn("alice", msg)
        # B should still get a fresh quota — A's exhaustion is irrelevant.
        ok, _ = tb.check_budget("test_pc_tool", caller_id="bob")
        self.assertTrue(ok)
        tb.record_use("test_pc_tool", caller_id="bob")
        ok, _ = tb.check_budget("test_pc_tool", caller_id="bob")
        self.assertTrue(ok, "bob should not be blocked by alice's exhaustion")

    def test_global_cap_takes_precedence_over_per_caller_cap(self):
        # Lower global to 3 so global hits first regardless of caller.
        os.environ["RED_BUDGET_TEST_PC_TOOL_DAILY"] = "3"
        # Drive 3 different callers each using 1.
        tb.record_use("test_pc_tool", caller_id="x")
        tb.record_use("test_pc_tool", caller_id="y")
        tb.record_use("test_pc_tool", caller_id="z")
        # Even a fresh caller (never seen before) is now blocked by global.
        ok, msg = tb.check_budget("test_pc_tool", caller_id="newcomer")
        self.assertFalse(ok)
        # Reason should reference global, not per-caller (global is checked first).
        self.assertIn("今日已執行", msg)
        self.assertNotIn("per-caller", msg)

    def test_per_caller_hourly_cap(self):
        os.environ["RED_BUDGET_TEST_PC_TOOL_PER_CALLER_HOURLY"] = "1"
        tb.record_use("test_pc_tool", caller_id="alice")
        ok, msg = tb.check_budget("test_pc_tool", caller_id="alice")
        self.assertFalse(ok)
        self.assertIn("per-caller 本小時", msg)

    def test_per_caller_cap_only_active_when_set(self):
        """A tool without per_caller_daily/_hourly must not enforce
        a per-caller limit even if caller_id is provided."""
        # No env override → no per-caller cap on this tool name.
        os.environ.pop("RED_BUDGET_TEST_PC_TOOL_PER_CALLER_DAILY", None)
        # Spam 50 times from the same caller — should never block.
        for i in range(50):
            ok, _ = tb.check_budget("test_pc_tool", caller_id="alice")
            self.assertTrue(ok, f"call {i+1} should be allowed (no per-caller cap)")
            tb.record_use("test_pc_tool", caller_id="alice")

    def test_collision_resistant_isolation_in_practice(self):
        """Codex P2 — concrete behavioral regression for the team/a vs team_a
        case. Drive caller 'team/a' to its per-caller cap and verify that
        caller 'team_a' (different raw input, would collide under the
        old normalizer) still has a full fresh quota.
        """
        # cap of 2 for per-caller; raise global to 100 so it never trips.
        os.environ["RED_BUDGET_TEST_PC_TOOL_DAILY"] = "100"
        os.environ["RED_BUDGET_TEST_PC_TOOL_PER_CALLER_DAILY"] = "2"
        # Exhaust team/a
        for _ in range(2):
            ok, _ = tb.check_budget("test_pc_tool", caller_id="team/a")
            self.assertTrue(ok)
            tb.record_use("test_pc_tool", caller_id="team/a")
        ok, msg = tb.check_budget("test_pc_tool", caller_id="team/a")
        self.assertFalse(ok, "team/a should be at per-caller cap")
        # team_a (totally different caller) must still have full quota.
        for i in range(2):
            ok, _ = tb.check_budget("test_pc_tool", caller_id="team_a")
            self.assertTrue(ok,
                f"team_a call {i+1} must NOT be blocked by team/a's cap "
                f"(collision regression)")
            tb.record_use("test_pc_tool", caller_id="team_a")


class TestCallerIdNormalization(_IsolatedToolBudgetTestCase):
    def test_empty_caller_id_means_no_tracking(self):
        os.environ["RED_BUDGET_NRM_TOOL_DAILY"] = "10"
        try:
            tb.record_use("nrm_tool", caller_id="")
            tb.record_use("nrm_tool", caller_id="   ")  # whitespace = empty
            state = tb._load_today()
            self.assertNotIn("by_caller", state.get("nrm_tool", {}),
                             "empty caller_id must not create by_caller key")
        finally:
            os.environ.pop("RED_BUDGET_NRM_TOOL_DAILY", None)

    def test_unsafe_chars_replaced_in_prefix(self):
        # Slashes / control chars / backslashes / quotes must be neutralized
        # in the human-readable prefix (the part before '#').
        normalized = tb._normalize_caller_id("a/b\\c\x00d'e\"f")
        prefix, _, suffix = normalized.partition("#")
        self.assertNotIn("/", prefix)
        self.assertNotIn("\\", prefix)
        self.assertNotIn("\x00", prefix)
        self.assertNotIn("'", prefix)
        self.assertNotIn('"', prefix)
        # Hash suffix exists and is 10 hex chars.
        self.assertEqual(len(suffix), 10)
        self.assertTrue(all(c in "0123456789abcdef" for c in suffix))

    def test_long_caller_id_capped(self):
        very_long = "x" * 500
        out = tb._normalize_caller_id(very_long)
        # 64-char prefix cap + "#" + 10-char hash = 75
        self.assertLessEqual(len(out), 80)
        self.assertIn("#", out)

    def test_collision_safe_when_sanitization_collapses_chars(self):
        """Codex P2 regression: 'team/a' and 'team_a' must NOT bucket the
        same. Before the hash-suffix fix, both collapsed to 'team_a' →
        caller A could drain caller B's quota."""
        a = tb._normalize_caller_id("team/a")
        b = tb._normalize_caller_id("team_a")
        # Human-readable prefixes happen to look the same — that's fine.
        self.assertEqual(a.split("#")[0], b.split("#")[0])
        # But the full normalized keys MUST differ — that's what makes the
        # per-caller buckets distinct.
        self.assertNotEqual(a, b,
                            "different inputs must produce different bucket keys")

    def test_collision_safe_when_long_inputs_share_prefix(self):
        """Even when both inputs share the first 80 chars (truncation case),
        the hash suffix derived from the raw input keeps them distinct."""
        a = tb._normalize_caller_id("x" * 100 + "AAA")
        b = tb._normalize_caller_id("x" * 100 + "BBB")
        self.assertNotEqual(a, b)

    def test_normalize_is_deterministic(self):
        """Same input → same key, every time. Otherwise per-caller counters
        would lose continuity across calls."""
        for caller in ["alice", "telegram:9990000001", "agent:gray",
                       "daemon:dispatcher"]:
            self.assertEqual(
                tb._normalize_caller_id(caller),
                tb._normalize_caller_id(caller),
            )


class TestRecordUseTracksPerCaller(_IsolatedToolBudgetTestCase):
    def test_record_use_updates_both_global_and_per_caller(self):
        os.environ["RED_BUDGET_TR_TOOL_DAILY"] = "20"
        try:
            tb.record_use("tr_tool", caller_id="alice")
            tb.record_use("tr_tool", caller_id="alice")
            tb.record_use("tr_tool", caller_id="bob")
            state = tb._load_today()
            rec = state["tr_tool"]
            # Global = 3
            self.assertEqual(rec["daily"], 3)
            # Per-caller breakdown — use the same normalizer the impl uses,
            # so the test stays robust if we change the key shape later.
            by_caller = rec.get("by_caller", {})
            alice_key = tb._normalize_caller_id("alice")
            bob_key = tb._normalize_caller_id("bob")
            self.assertEqual(by_caller[alice_key]["daily"], 2)
            self.assertEqual(by_caller[bob_key]["daily"], 1)
        finally:
            os.environ.pop("RED_BUDGET_TR_TOOL_DAILY", None)


class TestStatusReport(_IsolatedToolBudgetTestCase):
    def test_status_for_single_tool_shows_per_caller_when_present(self):
        os.environ["RED_BUDGET_ST_TOOL_DAILY"] = "10"
        try:
            tb.record_use("st_tool", caller_id="alice")
            tb.record_use("st_tool", caller_id="alice")
            tb.record_use("st_tool", caller_id="bob")
            out = tb.tool_budget_status("st_tool")
            self.assertIn("alice", out)
            self.assertIn("bob", out)
            self.assertIn("各 caller 用量", out)
        finally:
            os.environ.pop("RED_BUDGET_ST_TOOL_DAILY", None)


if __name__ == "__main__":
    unittest.main()
