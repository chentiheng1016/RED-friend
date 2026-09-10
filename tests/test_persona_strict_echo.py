"""Lock the strict-echo anti-hallucination rule into persona text.

Background: 2026-05-06 incident — small red verbally claimed
"stop_hotword_daemon 已徹底關閉" (success) when the actual tool result
was "⚠️ 需要 +確認" (blocked). The daemon kept running for ~60s while
小紅 reported success. This test guards against a future persona edit
silently removing the explicit "do NOT fabricate success" rule that
mitigates that class of LLM hallucination.

It only checks the system-prompt string content; behavior testing of
the LLM itself is out of scope (and impossible without hitting Gemini).
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core.daemon_telegram import _TG_BOT_SYSTEM_INSTRUCTION_APPEND  # noqa: E402


class TestPersonaContainsStrictEchoRule(unittest.TestCase):
    """Required substrings in the persona — at least one of each set must
    appear so the rule is unambiguous to the LLM regardless of prompt
    formatting tweaks."""

    def test_mentions_prefix_anchored_emoji_markers(self):
        """The persona must list the prefix-anchored failure emoji."""
        text = _TG_BOT_SYSTEM_INSTRUCTION_APPEND
        for marker in ["⚠️", "❌", "🔒"]:
            self.assertIn(
                marker, text,
                f"persona missing failure-prefix emoji {marker!r}",
            )

    def test_mentions_specific_failure_phrases(self):
        """Persona must enumerate the specific phrases (not generic
        keywords like '失敗' or 'Token' that show up in healthy summaries)."""
        text = _TG_BOT_SYSTEM_INSTRUCTION_APPEND
        for phrase in ["需要確認", "permission_denied", "rate-limit", "lockout"]:
            self.assertIn(
                phrase, text,
                f"persona missing specific failure phrase {phrase!r}",
            )

    def test_explicitly_excludes_count_summary_false_positives(self):
        """Codex P2 regression: `system_status` / `workflow_stats` legitimately
        print '成功 N / 失敗 M' counts. The rule must explicitly say those
        are NOT tool failures — otherwise small red would echo healthy
        status reports as if the tool didn't run."""
        text = _TG_BOT_SYSTEM_INSTRUCTION_APPEND
        # The persona should call out the false-positive pattern by name.
        self.assertIn(
            "不算失敗", text,
            "persona must explicitly carve out the 'not a failure' case "
            "for count summaries (Codex P2 regression)",
        )
        # And reference the affected tools so the LLM can pattern-match.
        self.assertTrue(
            any(name in text for name in
                ["system_status", "workflow_stats", "metrics_overview"]),
            "persona should name the observation tools whose 失敗 counts "
            "are NOT actual failures",
        )

    def test_mentions_no_fabrication_directive(self):
        """The directive must explicitly disallow rewriting failure
        messages into success language."""
        text = _TG_BOT_SYSTEM_INSTRUCTION_APPEND
        forbidden_phrases = ["不准美化", "不要改寫", "逐字 echo"]
        self.assertTrue(
            any(p in text for p in forbidden_phrases),
            f"persona must explicitly forbid fabricating success when a "
            f"tool returned a failure message; expected at least one of "
            f"{forbidden_phrases}",
        )

    def test_mentions_historical_incident(self):
        """A concrete past-incident reference makes the rule sticky for
        the LLM — abstract rules get ignored under pressure but a named
        bug ('stop_hotword_daemon 1 分鐘後才從 launchctl 發現…') stays
        salient."""
        text = _TG_BOT_SYSTEM_INSTRUCTION_APPEND
        self.assertIn(
            "stop_hotword_daemon", text,
            "persona should reference the real incident this rule "
            "exists to prevent — abstract directives get tuned out",
        )

    def test_does_not_list_overly_broad_keywords_as_triggers(self):
        """Codex P2: the rule MUST NOT instruct the LLM to fail-classify
        on bare '失敗' or bare 'Token'. The first appears in healthy
        count summaries; the second appears in cost reports."""
        text = _TG_BOT_SYSTEM_INSTRUCTION_APPEND
        # The trigger list must NOT contain bare backtick-quoted 失敗 / Token.
        self.assertNotIn("`失敗`", text,
            "bare `失敗` must not be a strict-echo trigger — too broad")
        self.assertNotIn("`Token`", text,
            "bare `Token` must not be a strict-echo trigger — too broad")


if __name__ == "__main__":
    unittest.main()
