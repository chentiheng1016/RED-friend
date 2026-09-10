"""Tests for the `c` / `cc` short-code aliases for confirmation tokens.

Goal: let 大王 type just `c` (instead of `+確認`) and `cc` (instead of
`+雙確認`). Word-boundary anchoring is critical — neither alias may
fire on common English words like "click", "create", "cancel".
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core.tg_auth import (  # noqa: E402
    message_grants_confirmation,
    message_grants_dangerous_confirmation,
)


class TestCAliasGrantsConfirmation(unittest.TestCase):
    """Single `c` (case-insensitive, with word boundaries) acts as `+確認`."""

    def test_bare_c(self):
        for text in ["c", "C"]:
            self.assertTrue(message_grants_confirmation(text),
                            f"{text!r} must grant confirmation")

    def test_c_with_trailing_punctuation(self):
        """Whole-message form `c` plus optional trailing punctuation grants."""
        for text in ["c.", "c!", "c?", "c。", "c！", "c ", " c", "\nc\n"]:
            self.assertTrue(message_grants_confirmation(text),
                            f"{text!r} must grant confirmation")

    def test_c_inside_sentence_does_NOT_grant(self):
        """Codex P1 regression: stricter than word-boundary — `c` MUST
        be the entire message. Common natural-language inputs that
        contain a standalone `c` must NOT grant confirmation."""
        false_positives = [
            # Codex's stated examples
            "write a C++ script and run it",
            "use column C",
            "option C",
            # Other natural sentences carrying a bare 'c' token
            "ok c",
            "yes c go",
            "好 c 動手",
            "give me option C please",
            "the C major scale",
            "see plan C",
            "section c is missing",
        ]
        for text in false_positives:
            self.assertFalse(
                message_grants_confirmation(text),
                f"{text!r} wrongly granted confirmation — c alias must "
                f"only fire when it IS the message, not contained in one",
            )

    def test_c_inside_words_does_NOT_grant(self):
        """Original word-boundary cases — these must remain safe under
        the stricter whole-message rule too."""
        false_positives = [
            "click here",
            "create event",
            "cancel that",
            "checkout",
            "calendar",
            "code review",
            "csv file",
            "cc list",
        ]
        for text in false_positives:
            self.assertFalse(
                message_grants_confirmation(text),
                f"{text!r} wrongly granted confirmation",
            )

    def test_legacy_plus_confirm_still_works(self):
        """The original `+確認` and friends must keep working."""
        for text in ["+確認", "/confirm", "確認執行", "go ahead"]:
            self.assertTrue(message_grants_confirmation(text),
                            f"{text!r} must keep granting confirmation")


class TestCcAliasGrantsDangerousConfirmation(unittest.TestCase):
    """Double `cc` acts as `+雙確認` for DANGEROUS-tier ops.

    Codex P2: bare `cc` requires a regular confirmation already active
    in the chat's 90s window; tests here pre-mark via mark_confirmed.
    """

    def setUp(self):
        from agent_core.tg_auth import mark_confirmed
        self.cid = "9" + str(id(self) % 1_000_000_000)
        mark_confirmed(self.cid)

    def test_bare_cc_with_prior_c(self):
        for text in ["cc", "Cc", "cC", "CC"]:
            self.assertTrue(
                message_grants_dangerous_confirmation(text, chat_id=self.cid),
                f"{text!r} must grant DANGEROUS confirmation when regular "
                f"+確認 is already in window",
            )

    def test_cc_with_trailing_punctuation_and_prior_c(self):
        for text in ["cc.", "cc!", "cc?", "cc。", " cc ", "\ncc\n"]:
            self.assertTrue(
                message_grants_dangerous_confirmation(text, chat_id=self.cid),
                f"{text!r} must grant DANGEROUS confirmation",
            )

    def test_cc_inside_sentence_does_NOT_grant(self):
        """Whole-message anchoring (Codex P1) — sentence containing 'cc'
        token must NOT grant DANGEROUS, even when chat has prior +確認."""
        for text in ["ok cc", "go cc 執行", "ready cc now",
                     "cc the boss please", "send cc to legal"]:
            self.assertFalse(
                message_grants_dangerous_confirmation(text, chat_id=self.cid),
                f"{text!r} wrongly granted DANGEROUS confirmation",
            )

    def test_cc_inside_words_does_NOT_grant(self):
        """Whole-message rule subsumes word-boundary protection too."""
        false_positives = [
            "occurs again",   # contains "cc" inside a word
            "soccer match",
            "according to",
            "successful",
        ]
        for text in false_positives:
            self.assertFalse(
                message_grants_dangerous_confirmation(text, chat_id=self.cid),
                f"{text!r} wrongly granted DANGEROUS confirmation",
            )

    def test_legacy_double_confirm_still_works(self):
        """Long-form tokens (+雙確認 / EXEC / etc.) keep working WITHOUT
        requiring a prior +確認 — they're noisy enough that typing them
        is itself deliberate. Only the short `cc` alias has the new
        prior-confirm requirement."""
        for text in ["+雙確認", "++確認", "/exec", "EXEC"]:
            self.assertTrue(
                message_grants_dangerous_confirmation(text),
                f"{text!r} must keep granting DANGEROUS confirmation "
                f"(no chat_id needed for long-form tokens)",
            )


class TestCodexP2BareCcRequiresPriorC(unittest.TestCase):
    """Codex P2 regression: bare `cc` must NOT pre-arm DANGEROUS half
    when there is no regular `+確認` already in window. Otherwise:
    user types cc for some benign reason → DANGEROUS armed → user
    later types c for an ordinary CONFIRM action → both timestamps
    valid → ANY DANGEROUS tool runs in turn 2 without the user ever
    seeing the DANGEROUS prompt."""

    def setUp(self):
        # Fresh chat_id every test, NO pre-marked confirmation.
        self.cid = "8" + str(id(self) % 1_000_000_000)
        # Sanity: no prior c is set
        from agent_core.tg_auth import check_confirmed
        confirmed, _ = check_confirmed(self.cid)
        self.assertFalse(confirmed, "test fixture broken — pre-existing confirm state")

    def test_bare_cc_without_prior_c_rejected(self):
        """The headline regression."""
        for text in ["cc", "Cc", "CC", "cc.", " cc ", "cc!"]:
            self.assertFalse(
                message_grants_dangerous_confirmation(text, chat_id=self.cid),
                f"bare {text!r} pre-armed DANGEROUS without prior +確認",
            )

    def test_bare_cc_without_chat_id_rejected(self):
        """Defense in depth: when caller doesn't pass chat_id at all,
        we cannot verify regular state — must conservatively reject."""
        for text in ["cc", "CC", "cc."]:
            self.assertFalse(
                message_grants_dangerous_confirmation(text),
                f"bare {text!r} granted with no chat_id (cannot verify state)",
            )

    def test_combined_form_still_works_without_prior_c(self):
        """The combined `c cc` / `cc c` form works because the message
        itself contains the regular-confirm token alongside cc — user is
        explicitly arming both at once."""
        for text in ["c cc", "cc c"]:
            self.assertTrue(
                message_grants_dangerous_confirmation(text),
                f"combined form {text!r} must work without external chat state",
            )

    def test_long_form_double_confirm_still_works_without_prior_c(self):
        """+雙確認 / EXEC / etc. keep their original behavior — no chat_id
        requirement, no prior +確認 requirement. They're noisy enough that
        typing them is itself deliberate."""
        for text in ["+雙確認", "EXEC", "/exec", "++確認"]:
            self.assertTrue(
                message_grants_dangerous_confirmation(text),
                f"long-form {text!r} should keep working standalone",
            )


class TestAliasInteraction(unittest.TestCase):
    """`c` and `cc` must remain distinct — typing only `cc` must NOT
    grant the regular confirmation, and vice versa.
    Background: DANGEROUS gate requires BOTH +確認 AND +雙確認
    within their respective windows. If `cc` accidentally also matched
    the regular pattern, the user could clear the DANGEROUS gate with
    a single `cc` message — defeating the intentional two-token
    separation.
    """

    def test_cc_alone_does_not_grant_regular_confirmation(self):
        self.assertFalse(
            message_grants_confirmation("cc"),
            "bare `cc` MUST NOT grant +確認 (regular tier)",
        )

    def test_c_alone_does_not_grant_dangerous_confirmation(self):
        self.assertFalse(
            message_grants_dangerous_confirmation("c"),
            "bare `c` MUST NOT grant +雙確認 (DANGEROUS tier)",
        )

    def test_c_and_cc_together_in_one_message_grants_both(self):
        """Convenience: typing `c cc` (or `cc c`) in one go satisfies both
        gates. Whole-message rule still applies — no surrounding text."""
        for text in ["c cc", "cc c", "c cc.", " c cc "]:
            self.assertTrue(message_grants_confirmation(text),
                            f"{text!r} must grant regular confirmation")
            self.assertTrue(message_grants_dangerous_confirmation(text),
                            f"{text!r} must grant DANGEROUS confirmation")

    def test_c_and_cc_combined_with_extra_text_does_NOT_grant(self):
        """Even the combined form must be the WHOLE message; mixing it
        with a sentence loses the alias status."""
        for text in ["please c cc me", "do c cc the boss", "c cc and copy 王"]:
            self.assertFalse(
                message_grants_confirmation(text),
                f"{text!r} wrongly granted regular confirmation",
            )
            self.assertFalse(
                message_grants_dangerous_confirmation(text),
                f"{text!r} wrongly granted DANGEROUS confirmation",
            )


class TestCodexP1RegressionExamples(unittest.TestCase):
    """Direct regression tests for the exact phrases Codex named in P1
    re-review. If any of these granted confirmation, a sensitive tool in
    the same turn could run without the user's intent — the very leak
    this fix exists to prevent."""

    def test_cpp_script(self):
        """`write a C++ script and run it` must NOT grant. The `c` here
        is part of `C++`, not a standalone confirmation."""
        self.assertFalse(message_grants_confirmation(
            "write a C++ script and run it"))

    def test_use_column_C(self):
        self.assertFalse(message_grants_confirmation("use column C"))

    def test_option_C(self):
        self.assertFalse(message_grants_confirmation("option C"))
        self.assertFalse(message_grants_confirmation("give me option C please"))

    def test_C_major(self):
        self.assertFalse(message_grants_confirmation("play in C major"))
        self.assertFalse(message_grants_confirmation("the C major scale"))

    def test_save_as_C(self):
        self.assertFalse(message_grants_confirmation("save this as C"))

    def test_windows_path(self):
        """Windows path style — common when user shares paths."""
        self.assertFalse(message_grants_confirmation(r"C:\Users\foo"))


if __name__ == "__main__":
    unittest.main()
