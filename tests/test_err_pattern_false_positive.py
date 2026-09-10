"""Regression test for the `_ERR_PATTERN` false-positive on `failed=N`.

Background: the `errors_log_*` alerts and any future log-error analytics
share a pattern that originally matched bare `failed`. Daemon summary
lines often contain `failed=N` (frequently `failed=0`) — those are
informational, not errors. Diagnosing the May 6 alert spam revealed
~1700 alert_pusher summary lines per day matching `failed`, which alone
would trip `errors_log_warn` (>=100) and `errors_log_crit` (>=500).

The pattern lives in `agent_core/dashboard_trends._ERR_PATTERN` and is
the canonical one for "is this line an error". Fix is a negative
lookahead on the `failed` alternative: `failed(?!=)`.
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core import dashboard_trends  # noqa: E402


class TestErrPatternNoFailedEqualsFalsePositive(unittest.TestCase):
    """The pattern must NOT match informational summary lines that report
    `failed=<N>` (especially `failed=0`), but MUST still match all the
    actual error shapes it caught before."""

    def test_failed_equals_zero_summary_not_matched(self):
        """The exact alert_pusher summary line — must NOT match."""
        line = "[alert_pusher] 掃 0 alerts；pushed=0 recovered=0 failed=0"
        self.assertIsNone(
            dashboard_trends._ERR_PATTERN.search(line),
            "alert_pusher summary with failed=0 wrongly counted as error",
        )

    def test_failed_equals_nonzero_summary_not_matched(self):
        """Even when failed counter is non-zero, `failed=N` is the structured
        summary form — line itself isn't a failure log entry."""
        for n in (1, 5, 42, 1000):
            line = f"[email_ingest] 統計：新信 12 / 回填 0 / 敏感過濾 0 / failed={n}"
            self.assertIsNone(
                dashboard_trends._ERR_PATTERN.search(line),
                f"failed={n} summary wrongly matched",
            )

    def test_cjk_failed_counter_summary_not_matched(self):
        """Chinese summary counters use `失敗 0`; those are status lines, not
        individual error entries."""
        for n in (0, 1, 42):
            line = f"[email_ingest] 統計：新信 12 / 回填 0 / 敏感過濾 0 / 失敗 {n}"
            self.assertIsNone(
                dashboard_trends._ERR_PATTERN.search(line),
                f"失敗 {n} summary wrongly matched",
            )

    def test_real_error_lines_still_match(self):
        """All shapes the pre-fix pattern caught must continue to match."""
        positive_cases = [
            "[ERROR] connection refused",
            "12:34:56 [error] auth denied",  # case-insensitive
            "Traceback (most recent call last):",
            "RuntimeError: Exception: something",
            "[cloudflared] failed to serve tunnel",  # `failed ` (space follows)
            "failed: timeout",                        # `failed:` colon
            "Sync 失敗，請重試",                       # CJK 失敗
            "操作失敗: timeout",                       # CJK 失敗 with text after colon
            "❌ 完成不了",                             # explicit ❌
            "Exception: connection",
            "Error: not found",
        ]
        for line in positive_cases:
            self.assertIsNotNone(
                dashboard_trends._ERR_PATTERN.search(line),
                f"real error line wrongly excluded: {line!r}",
            )

    def test_failed_at_eol_still_matches(self):
        """`failed` at end of line (no following char) is a real error
        token — must still match. The negative lookahead requires what
        comes next NOT be `=`; EOL satisfies that."""
        line = "operation failed"
        self.assertIsNotNone(
            dashboard_trends._ERR_PATTERN.search(line),
            "bare 'failed' at EOL must still count",
        )


class TestErrPatternNoJsonFailedKeyFalsePositive(unittest.TestCase):
    """2026-09-01 regression: the daemon_watchdog healthy heartbeat is a JSON
    blob containing the key `"failed": []`. `failed(?!=)` only excluded the
    `failed=` counter form, so every heartbeat matched. Invisible for months
    because heartbeat lines carried no date; once #452 prefixed every line
    with a timestamp, errors_trend() started attributing ~467 heartbeats/day
    to "today" and errors_log_crit (>=500) fired on a healthy fleet."""

    def test_watchdog_heartbeat_json_not_matched(self):
        """The exact production heartbeat line — must NOT match."""
        line = ('[2026-09-01 16:46:12] [daemon_watchdog] {"enabled": true, '
                '"findings": 0, "restarted": [], "skipped": [], "failed": [], '
                '"alerted_only": [], "stale": []}')
        self.assertIsNone(
            dashboard_trends._ERR_PATTERN.search(line),
            "watchdog heartbeat JSON wrongly counted as error",
        )

    def test_json_failed_key_variants_not_matched(self):
        """Key form in both JSON (double-quote) and Python-repr
        (single-quote) spellings, with and without a space before the
        colon."""
        for frag in ('"failed": []', '"failed":[]', "'failed': 0",
                     '"failed" : ["x"]'):
            line = f"[daemon_watchdog] {{{frag}}}"
            self.assertIsNone(
                dashboard_trends._ERR_PATTERN.search(line),
                f"JSON key form wrongly matched: {frag!r}",
            )

    def test_narrative_failed_colon_still_matches(self):
        """`failed:` with NO quote in between is a real narrative error and
        must keep matching — the lookahead only excludes the quoted key
        form."""
        for line in ("failed: timeout",
                     'upload "big.pdf" failed: quota exceeded'):
            self.assertIsNotNone(
                dashboard_trends._ERR_PATTERN.search(line),
                f"real error line wrongly excluded: {line!r}",
            )


class TestDashboardSharesCanonicalPattern(unittest.TestCase):
    """Codex P2: agent_core/dashboard.py used to have its OWN duplicate
    `_ERR_PATTERN` with the bare `failed` alternative, so the
    recent-errors panel of system_status surfaced the same `failed=N`
    daemon summaries that the trend / alert side suppresses. Result:
    alert says "no spam", but system_status shows 1700+ false-positive
    lines.

    Fix: dashboard.py now imports `_ERR_PATTERN` from dashboard_trends.
    This test locks down both halves:
      • the same compiled-regex object is in use (single source of truth)
      • dashboard's pattern correctly excludes `failed=N` end-to-end
    """

    def test_dashboard_imports_canonical_err_pattern(self):
        """dashboard._ERR_PATTERN must be the SAME object as
        dashboard_trends._ERR_PATTERN — proves there's no duplicate
        definition that could drift again."""
        from agent_core import dashboard, dashboard_trends
        self.assertIs(
            dashboard._ERR_PATTERN, dashboard_trends._ERR_PATTERN,
            "dashboard.py has its own _ERR_PATTERN copy — they will "
            "drift on the next regex tweak, just like the failed=N case",
        )

    def test_dashboard_pattern_excludes_failed_equals(self):
        """End-to-end: section_recent_errors path also drops
        the `failed=0` summary lines."""
        from agent_core.dashboard import _ERR_PATTERN
        self.assertIsNone(
            _ERR_PATTERN.search(
                "[alert_pusher] 掃 3 alerts；pushed=0 recovered=0 failed=0"
            ),
            "alert_pusher summary leaks into recent-errors panel",
        )
        self.assertIsNotNone(
            _ERR_PATTERN.search("connection failed to upstream"),
            "real failure must still match in recent-errors panel",
        )


if __name__ == "__main__":
    unittest.main()
