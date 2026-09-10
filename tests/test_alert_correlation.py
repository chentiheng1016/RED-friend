"""Tests for status_center.correlate_alert (P3 — alert RCA automation).

The function takes an alert_id, tails var/logs/*.log (or a passed log_dir),
buckets error lines by normalized pattern, and pairs that with the daemon
last_exit state. These tests use synthetic log files in tmp_path so the
behavior is deterministic and doesn't depend on the live var/logs/ contents.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import date

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core import status_center  # noqa: E402


def _write_log(dir_path: str, name: str, content: str) -> None:
    with open(os.path.join(dir_path, name), "w", encoding="utf-8") as f:
        f.write(content)


class TestBucketRecentErrors(unittest.TestCase):
    """Lower-level tests on the bucketing helper that powers correlate_alert."""

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(status_center._bucket_recent_errors(log_dir=d), [])

    def test_non_log_files_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            _write_log(d, "system.txt",
                       f"{today} ERROR something terrible happened\n")
            self.assertEqual(status_center._bucket_recent_errors(log_dir=d), [])

    def test_failed_equals_summary_not_bucketed(self):
        """RCA bucketing must use the same failed(?!=) rule as the alert count."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            _write_log(
                d,
                "daemon-alert_check.log",
                f"{today} [alert_pusher] 掃 3 alerts；pushed=0 recovered=0 failed=0\n",
            )
            self.assertEqual(status_center._bucket_recent_errors(log_dir=d), [])

    def test_cjk_failed_counter_summary_not_bucketed(self):
        """email_ingest summary lines end with `失敗 0`; RCA must not rank
        them above real retry loops."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            _write_log(
                d,
                "daemon-email_ingest.log",
                f"{today} [email_ingest] 統計：新信 0 / 回填 0 / 敏感過濾 0 / 失敗 0\n",
            )
            self.assertEqual(status_center._bucket_recent_errors(log_dir=d), [])

    def test_bucketing_groups_repeated_pattern(self):
        """A retry-loop where only timestamps + numbers vary should collapse
        into a single high-count bucket — that's the whole point."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            lines = [
                f"[cloudflared] {today}T12:46:28Z ERR failed to serve tunnel "
                f"connection error=\"control stream\" connIndex=0 ip=198.41.200.{i}"
                for i in range(50)
            ]
            _write_log(d, "daemon-cloudflare.log", "\n".join(lines) + "\n")
            buckets = status_center._bucket_recent_errors(log_dir=d, top_n=5)
            self.assertEqual(len(buckets), 1, "all 50 lines should bucket as one")
            count, _, src = buckets[0]
            self.assertEqual(count, 50)
            self.assertEqual(src, "daemon-cloudflare.log")

    def test_distinct_patterns_in_separate_buckets(self):
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            lines = [
                f"{today} ERROR connect timeout error=conn-{i}" for i in range(30)
            ] + [
                f"{today} Traceback in spam handler {i}" for i in range(10)
            ]
            _write_log(d, "daemon-x.log", "\n".join(lines) + "\n")
            buckets = status_center._bucket_recent_errors(log_dir=d, top_n=5)
            self.assertEqual(len(buckets), 2)
            counts = sorted(c for c, *_ in buckets)
            self.assertEqual(counts, [10, 30])

    def test_only_today_counted(self):
        """Yesterday's lines must be excluded — alert is about today."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            yesterday_year = "2020"  # safely far in the past
            content = (
                f"{yesterday_year}-01-01 ERROR ancient failure\n" * 20
                + f"{today} ERROR fresh failure\n" * 5
            )
            _write_log(d, "daemon-y.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            self.assertEqual(len(buckets), 1)
            self.assertEqual(buckets[0][0], 5, "only today's 5 lines counted")

    def test_iso_datetime_with_T_separator_filtered_correctly(self):
        """Codex P1 regression: the date filter regex must match
        YYYY-MM-DDTHH:MM:SSZ (cloudflared-style), not just date-only.

        Bug shape: `\b(YYYY-MM-DD)\b` failed because `\b` after the day
        requires a non-word boundary, but `T` IS a word char → match
        failed → line treated as "no date in line" → trusted file mtime
        and INCLUDED yesterday's lines as today's. Buckets get polluted.
        """
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            past_iso = "2020-01-01"
            content = (
                # Yesterday's lines in cloudflared format (ISO with T sep).
                # `failed` matches _ERR_PATTERN — same as the real-world
                # `failed to serve tunnel connection` we're trying to bucket.
                f"[cf] {past_iso}T23:59:00Z ERR failed to retry\n" * 25
                # Today's matching lines — should be the only ones counted
                + f"[cf] {today}T00:00:01Z ERR failed to retry\n" * 5
            )
            _write_log(d, "daemon-cf.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            # Both kinds normalize to one bucket key (same prefix + <TS>),
            # but the today filter must drop the 25 ancient lines first.
            self.assertEqual(len(buckets), 1,
                "single normalized bucket expected — ancient ones must be filtered")
            count = buckets[0][0]
            self.assertEqual(count, 5,
                f"expected 5 today-only matches, got {count} "
                f"(ISO-T date filter regression)")

    def test_business_date_in_message_body_not_treated_as_timestamp(self):
        """Codex P2 round 3 regression: the previous `re.search` for
        YYYY-MM-DD scanned the whole line and would capture business
        dates inside the message body (e.g. `order date 2026-01-01`)
        as the line's log timestamp — wrongly dropping a today-stamped
        error during RCA. The fix anchors the date to the leading
        timestamp slot via `re.match` with a short non-digit prefix
        allowance, and falls back to the HH:MM:SS rollback classifier
        for time-only lines.
        """
        with tempfile.TemporaryDirectory() as d:
            content = (
                # Time-only log lines with a *business date* in the body.
                # The body date is from 2020 so a buggy "search anywhere"
                # filter would mis-classify these as ancient and drop them.
                "12:00:00 [ERROR] failed import for order date 2020-01-01\n" * 10
                + "12:00:01 [ERROR] retry failed for order date 2020-01-01\n" * 5
            )
            _write_log(d, "daemon-ingest.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            total = sum(b[0] for b in buckets)
            self.assertEqual(
                total, 15,
                f"expected all 15 time-only errors to count as today, "
                f"got {total} — business-date-in-body leaked into the "
                f"line-timestamp classifier",
            )

    def test_time_only_log_format_lines_counted(self):
        """Codex P1 regression: the repo's default logger uses
        `datefmt='%H:%M:%S'`, so error lines look like
        `12:34:56 [ERROR] connection refused` with NO date in the line.
        The OLD `today_str not in line` filter dropped every such line and
        correlate_alert() falsely reported "no errors" during real
        incidents. With the file-mtime + line-date semantics, lines without
        an ISO date should be trusted (file recency already gates them).
        """
        with tempfile.TemporaryDirectory() as d:
            content = (
                "12:34:56 [ERROR] connection refused\n" * 8
                + "12:34:57 [ERROR] connection refused\n" * 7
                + "Traceback (most recent call last):\n"
                + "  File \"/foo.py\", line 1, in <module>\n"
                + "RuntimeError: simulated boom\n"
            )
            _write_log(d, "agent-current.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            # 15 connection-refused lines collapse to one bucket; Traceback
            # marker line on its own is another. RuntimeError... is a third.
            total_count = sum(c for c, *_ in buckets)
            self.assertGreaterEqual(total_count, 15,
                "time-only-format ERROR lines must NOT be silently dropped")
            self.assertTrue(
                any("connection refused" in norm for _, norm, _ in buckets),
                "the connection-refused bucket must be present"
            )

    def test_old_mtime_files_skipped(self):
        """Files whose mtime is older than file_recency_hours are skipped
        wholesale — even if their last 256KB happens to mention today's
        date. This is the first-tier date scope."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            _write_log(d, "daemon-stale.log",
                       f"{today} ERROR something old but mentioning today\n" * 10)
            fp = os.path.join(d, "daemon-stale.log")
            # Push mtime to 30 hours ago (past the 26h default cutoff).
            old = time.time() - 30 * 3600
            os.utime(fp, (old, old))
            buckets = status_center._bucket_recent_errors(log_dir=d)
            self.assertEqual(buckets, [],
                "stale files past recency cutoff must be skipped entirely")

    def test_recent_mtime_with_time_only_lines_counts(self):
        """Combination case: file mtime is recent + lines are time-only.
        Both gates pass → lines counted. This is the real-world scenario
        for daemon-XXX.log with the standard logger format."""
        with tempfile.TemporaryDirectory() as d:
            content = "12:00:00 [ERROR] real error in active log\n" * 12
            _write_log(d, "daemon-active.log", content)
            # File was just written → mtime is now → recent. Default cutoff
            # is 26h so this passes.
            buckets = status_center._bucket_recent_errors(log_dir=d)
            self.assertEqual(len(buckets), 1)
            self.assertEqual(buckets[0][0], 12)

    def test_dated_filename_yesterday_skipped_even_within_mtime_window(self):
        """Codex P2 regression: the repo's standard logger writes
        `agent-YYYY-MM-DD.log` with `%H:%M:%S` line format. On May 6,
        yesterday's `agent-2026-05-05.log` (last touched 23:59:59) still
        has mtime within the 26h window AND no per-line date markers,
        so the mtime+line-date two-tier scope alone would count its
        errors as today's. The filename-date gate must override that.
        """
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            yesterday_iso = "2026-05-05"  # arbitrary past date, not today
            # 1) yesterday's daily file: time-only lines + dated filename.
            #    fresh mtime to mimic "touched at 23:59" recently.
            yest_path = os.path.join(d, f"agent-{yesterday_iso}.log")
            with open(yest_path, "w", encoding="utf-8") as f:
                f.write("23:58:01 [ERROR] yesterday error A\n" * 8)
                f.write("23:59:30 [ERROR] yesterday error B\n" * 4)
            # mtime well within the 26h window (1 minute ago).
            recent_t = time.time() - 60
            os.utime(yest_path, (recent_t, recent_t))

            # 2) today's daily file: time-only lines + dated filename.
            today_path = os.path.join(d, f"agent-{today}.log")
            with open(today_path, "w", encoding="utf-8") as f:
                f.write("00:00:01 [ERROR] today error X\n" * 3)

            buckets = status_center._bucket_recent_errors(log_dir=d)
            total_count = sum(c for c, *_ in buckets)
            # Yesterday's 12 must NOT count; today's 3 should be the only.
            self.assertEqual(
                total_count, 3,
                f"yesterday's per-day file leaked into today's buckets "
                f"(got {total_count} total; expected 3)",
            )
            self.assertTrue(
                any("today error" in norm for _, norm, _ in buckets),
                "today's bucket must be present",
            )
            self.assertFalse(
                any("yesterday error" in norm for _, norm, _ in buckets),
                "yesterday's lines must NOT appear",
            )

    def test_secrets_in_log_lines_redacted_before_bucket(self):
        """Codex P1 regression: a matched error line could carry an API
        token / Bearer / password / JWT. The bucketed normalized line is
        used directly in the human-facing RCA report (console / Telegram /
        LLM context), so secrets must be passed through log_redact BEFORE
        bucketing — otherwise they leak via the bucket key itself.
        """
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            # Embed a realistic OpenAI-style key + a Bearer token in error
            # lines. Both should be neutralized to [REDACTED:...].
            secret_key = "sk-proj-AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555FFFF6666"
            bearer = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
            content = (
                f"{today} ERROR auth failed: token={secret_key}\n" * 3
                + f"{today} ERROR upstream {bearer} got 401\n" * 2
            )
            _write_log(d, "daemon-leak.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            # Compose all bucket text — no secret substring may appear.
            joined = "\n".join(norm for _, norm, _ in buckets)
            self.assertNotIn(secret_key, joined,
                "OpenAI-style key leaked into RCA bucket text")
            self.assertNotIn(bearer.split()[1], joined,
                "JWT body leaked into RCA bucket text")
            # Sanity: the redacted form should be present.
            self.assertTrue(
                any("[REDACTED" in norm for _, norm, _ in buckets),
                "expected redaction marker in at least one bucket"
            )

    def test_dated_filename_today_passes_even_with_undated_lines(self):
        """Sanity check the other side: when the filename's date IS today,
        per-line date is irrelevant — accept all error-pattern lines."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            fp = os.path.join(d, f"agent-{today}.log")
            with open(fp, "w", encoding="utf-8") as f:
                f.write("12:00:00 [ERROR] today undated line\n" * 7)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            self.assertEqual(len(buckets), 1)
            self.assertEqual(buckets[0][0], 7)

    def test_undated_daemon_log_yesterday_lines_dropped_via_hh_rollback(self):
        """Codex P2 round 2: long-running launchd daemon writes to ONE
        persistent file with `%H:%M:%S`-only format (the Python logger
        default). If the tail spans multiple days, the previous fallback
        "trust mtime → all lines today" wrongly counted yesterday's
        errors as today's.

        Now: walk backward, when HH increases (going earlier in time)
        we crossed midnight in reverse — every line from there back is
        on a previous day. This test plants a daemon-mailcheck-style log:

          22:00 yesterday err 1
          23:00 yesterday err 2
          (midnight)
          09:00 today err A
          12:00 today err B

        Walking backward from the most-recent (12:00) line:
          12:00 → today (default starting state)
          09:00 → HH 09 < 12 → today
          23:00 → HH 23 > 09 → ROLLBACK → yesterday
          22:00 → already after rollback → yesterday

        Expected bucketed count for today: 2.
        """
        with tempfile.TemporaryDirectory() as d:
            content = (
                "22:00:01 [ERROR] yesterday err 1\n"
                "23:00:02 [ERROR] yesterday err 2\n"
                "09:00:03 [ERROR] today err A\n"
                "12:00:04 [ERROR] today err B\n"
            )
            _write_log(d, "daemon-mailcheck.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            total = sum(c for c, *_ in buckets)
            self.assertEqual(
                total, 2,
                f"expected 2 today-only matches via HH-rollback, got {total}",
            )
            joined = "\n".join(norm for _, norm, _ in buckets)
            self.assertIn("today err", joined)
            self.assertNotIn("yesterday err", joined)

    def test_undated_log_no_rollback_all_today(self):
        """Sanity: undated lines with monotonically decreasing HH (going
        backward) — no midnight crossing — all count as today."""
        with tempfile.TemporaryDirectory() as d:
            content = (
                "08:00:00 [ERROR] morning err\n"
                "10:30:00 [ERROR] mid-morning err\n"
                "12:45:00 [ERROR] noon err\n"
            )
            _write_log(d, "daemon-x.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            total = sum(c for c, *_ in buckets)
            self.assertEqual(total, 3,
                             "all 3 morning→noon lines should count as today")

    def test_undated_traceback_continuation_inherits_day(self):
        """Multi-line tracebacks have continuation lines without
        HH:MM:SS at start — they should inherit the day classification
        of the next line we DID classify."""
        with tempfile.TemporaryDirectory() as d:
            content = (
                "22:30:00 [ERROR] yesterday traceback follows\n"
                "Traceback (most recent call last):\n"
                "  File \"x.py\", line 5, in foo\n"
                "RuntimeError: yesterday boom\n"
                "09:00:00 [ERROR] today fresh\n"
                "Traceback (most recent call last):\n"
                "  File \"y.py\", line 3, in bar\n"
                "ValueError: today boom\n"
            )
            _write_log(d, "daemon-tb.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            joined = "\n".join(norm for _, norm, _ in buckets)
            # Today's content (including continuation lines) should appear
            self.assertIn("today", joined)
            # Yesterday's should NOT — continuation lines must inherit
            # the day classification of their owning HH:MM:SS line.
            self.assertNotIn("yesterday boom", joined)
            self.assertNotIn("yesterday traceback", joined)

    def test_multi_day_single_file_undated_lines_scoped_by_markers(self):
        """Codex P2 regression: long-running processes with %H:%M:%S
        format can accumulate multi-day content in one file. mtime says
        today, but the tail's beginning may be yesterday or earlier.
        Trusting mtime alone (the previous fix) included those stale
        lines in today's RCA buckets.

        New behavior: when the tail HAS at least one explicit YYYY-MM-DD
        marker, undated lines inherit the date from their nearest dated
        neighbor (forward+backward fill). So undated lines next to a
        yesterday marker get scoped as yesterday and dropped, while
        undated lines next to today's marker get kept.
        """
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            past = "2020-01-01"
            content = (
                # Yesterday (past) section — explicit marker + 5 undated
                f"=== rotated banner {past} 18:00:00 ===\n"
                + "18:30:00 [ERROR] yesterday undated 1\n"
                + "19:00:00 [ERROR] yesterday undated 2\n"
                + "19:30:00 [ERROR] yesterday undated 3\n"
                + "20:00:00 [ERROR] yesterday undated 4\n"
                + "20:30:00 [ERROR] yesterday undated 5\n"
                # Today section — explicit marker + 3 undated
                + f"=== rotated banner {today} 09:00:00 ===\n"
                + "09:30:00 [ERROR] today undated 1\n"
                + "10:00:00 [ERROR] today undated 2\n"
                + "10:30:00 [ERROR] today undated 3\n"
            )
            _write_log(d, "daemon-longrun.log", content)
            buckets = status_center._bucket_recent_errors(log_dir=d)
            # The 3 "today undated N" lines bucket as 1 entry (numbers
            # normalize). Yesterday's 5 must NOT be counted.
            total_count = sum(c for c, *_ in buckets)
            self.assertEqual(
                total_count, 3,
                f"expected 3 today-only matches; got {total_count} — "
                f"yesterday's undated lines leaked into today's buckets"
            )
            # Verify the bucket text contains the today marker, not yesterday's.
            self.assertTrue(
                any("today undated" in norm for _, norm, _ in buckets),
                "today's bucket must be present"
            )
            self.assertFalse(
                any("yesterday undated" in norm for _, norm, _ in buckets),
                "yesterday's lines must NOT appear in today's buckets"
            )

    def test_top_n_caps_results(self):
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            # 8 distinct patterns, each appearing different number of times.
            content = "\n".join(
                f"{today} ERROR pattern_{i} occurred"
                for i in range(8)
                for _ in range(i + 1)
            )
            _write_log(d, "daemon-z.log", content + "\n")
            buckets = status_center._bucket_recent_errors(log_dir=d, top_n=3)
            self.assertEqual(len(buckets), 3)


class TestCorrelateAlert(unittest.TestCase):
    """End-to-end tests of the public correlate_alert() function."""

    def test_clean_state_produces_recovery_message(self):
        with tempfile.TemporaryDirectory() as d:
            report = status_center._correlate_alert_for_test("errors_log_crit", log_dir=d)
        self.assertIn("Alert RCA — errors_log_crit", report)
        self.assertIn("今日 error-pattern", report)
        self.assertIn("(今日沒符合 error pattern 的 log 行)", report)
        # Should NOT crash on missing alert; advice section still present.
        self.assertIn("推測", report)

    def test_retry_loop_is_flagged_in_advice(self):
        """100+ lines on a single bucket → 'retry 死循環' hint."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            lines = [
                f"[cloudflared] {today}T12:00:00Z ERR failed to dial conn={i}"
                for i in range(150)
            ]
            _write_log(d, "daemon-cloudflare_tunnel.log", "\n".join(lines) + "\n")
            report = status_center._correlate_alert_for_test("errors_log_crit", log_dir=d)
        # Top bucket should appear with high count and the retry-loop advice
        self.assertIn("150×", report)
        self.assertIn("daemon-cloudflare_tunnel.log", report)
        self.assertIn("retry", report)  # advice line

    def test_bad_alert_id_does_not_crash(self):
        """An unknown alert_id is allowed (e.g. asking about a recovered
        alert) — should still produce a report shape, not raise."""
        with tempfile.TemporaryDirectory() as d:
            report = status_center._correlate_alert_for_test("nonexistent_alert_id", log_dir=d)
        self.assertIn("Alert RCA — nonexistent_alert_id", report)
        self.assertIn("狀態:", report)

    def test_unreadable_log_file_skipped_not_fatal(self):
        """A binary / unreadable file in log_dir must not break the whole
        report — just be skipped."""
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            # Real error log with content
            _write_log(d, "daemon-real.log",
                       f"{today} ERROR real failure\n" * 5)
            # Corrupted binary log alongside
            with open(os.path.join(d, "daemon-binary.log"), "wb") as f:
                f.write(b"\x00\xff\xfe\x80" * 100)  # non-utf-8 garbage
            report = status_center._correlate_alert_for_test("errors_log_crit", log_dir=d)
        # Real log's 5 errors should still bucket; report shouldn't crash
        self.assertIn("5×", report)
        self.assertIn("daemon-real.log", report)

    def test_daemon_summary_error_surfaced_not_silently_clean(self):
        """Codex P2 regression: when _daemons_summary() returns
        {"_error": "..."} (e.g. launchctl unavailable on non-macOS / CI),
        the report MUST NOT print '✅ 無' as if daemon health were fine.
        It must explicitly say the data was unavailable."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                status_center, "_daemons_summary",
                return_value={"_error": "FileNotFoundError: launchctl missing"},
            ):
                report = status_center._correlate_alert_for_test("errors_log_crit", log_dir=d)
        # The misleading success indicator must be absent.
        self.assertNotIn("✅ 無", report)
        # And the unavailability must be explicit + actionable.
        self.assertIn("daemon 狀態無法取得", report)
        self.assertIn("FileNotFoundError", report)
        # Advice section softens its empty-state language (no false "已恢復"
        # claim when we don't actually know).
        self.assertNotIn("alert 可能已恢復", report)

    def test_daemon_summary_error_with_real_log_spam(self):
        """Even when daemon data is unavailable, log-side analysis still
        runs. Verify both signals appear together correctly."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            today = date.today().isoformat()
            _write_log(d, "daemon-real.log",
                       f"{today} ERROR connection refused\n" * 25)
            with mock.patch.object(
                status_center, "_daemons_summary",
                return_value={"_error": "PermissionError: launchctl denied"},
            ):
                report = status_center._correlate_alert_for_test("errors_log_crit", log_dir=d)
        self.assertIn("25×", report)
        self.assertIn("daemon 狀態無法取得", report)
        self.assertNotIn("✅ 無", report)


class TestDaemonLabelAlias(unittest.TestCase):
    """Codex P2 regression: launchd labels and log filenames don't always
    match. e.g. `com.xiaohong.rag_sync_daily` writes to
    `daemon-rag_sync.log`. The previous heuristic
    `src.replace('daemon-', '').replace('.log', '')` produced "rag_sync"
    but bad_daemons listed "rag_sync_daily" → the "daemon X is in
    last_exit≠0 + top spam source" hint fell through, exactly when it
    would have been most useful.

    The alias map is built from launchd/templates/*.plist (Label →
    StandardOutPath basename). When that map disagrees with the strip
    heuristic, we use it; otherwise we fall back to the heuristic.
    """

    def test_label_for_log_resolves_aliased_pair(self):
        """rag_sync_daily / daemon-rag_sync.log: known alias."""
        from agent_core.status_center import _label_for_log
        result = _label_for_log("daemon-rag_sync.log",
                                bad_labels={"rag_sync_daily"})
        self.assertEqual(result, "rag_sync_daily",
            "alias map must resolve label even when filename strip differs")

    def test_label_for_log_identity_match_still_works(self):
        """Daemons whose label and log filename DO match (most of them)
        must continue to resolve via the strip-heuristic fallback."""
        from agent_core.status_center import _label_for_log
        result = _label_for_log("daemon-dispatcher.log",
                                bad_labels={"dispatcher", "other_daemon"})
        self.assertEqual(result, "dispatcher")

    def test_label_for_log_returns_none_when_no_match(self):
        from agent_core.status_center import _label_for_log
        result = _label_for_log("daemon-foo.log",
                                bad_labels={"bar", "baz"})
        self.assertIsNone(result)

    def test_alias_map_built_from_real_plist_templates(self):
        """Sanity check: at least the rag_sync_daily alias appears in the
        map built from the actual launchd/templates/ directory."""
        from agent_core.status_center import _build_label_to_logname
        m = _build_label_to_logname()
        # rag_sync_daily plist concretely has label != filename stem.
        # If this assertion ever fails, the plist may have been renamed —
        # the alias logic itself is still useful for any future divergence.
        self.assertIn("rag_sync_daily", m,
            "rag_sync_daily must appear in plist alias map")
        self.assertEqual(m.get("rag_sync_daily"), "daemon-rag_sync.log")

    def test_correlate_advice_uses_alias_for_root_cause_hint(self):
        """End-to-end: simulate rag_sync_daily failing AND its log being
        the top spam source. The advice section must surface the
        correlation pointer."""
        from agent_core.status_center import _correlate_advice
        # Mimic the real shape: bucket source = "daemon-rag_sync.log",
        # daemon listed in last_exit_nonzero with label "rag_sync_daily".
        buckets = [(150, "<TS> ERROR upstream timeout", "daemon-rag_sync.log")]
        bad_daemons = [{"label": "rag_sync_daily", "exit_code": "1"}]
        advice = _correlate_advice("errors_log_crit", buckets, bad_daemons)
        # The headline correlation line must be present.
        self.assertIn("rag_sync_daily", advice,
            "advice must name rag_sync_daily as the suspected root cause")
        self.assertIn("last_exit≠0", advice)
        self.assertIn("top-source", advice)


class TestCorrelateAlertRegistration(unittest.TestCase):
    """Codex P2 regression: correlate_alert must be reachable through the
    LLM tool path, not just direct Python imports — otherwise the
    advertised "問小紅 errors_log_crit 為什麼觸發" workflow can't dispatch
    to it. Two registration points must agree:

      1. tool_registry_catalog.BASE_BUILTIN_TOOLS  — the master callable list
      2. intent_router.INTENT_TOOL_BUCKETS         — system_maintenance allowlist
    """

    def test_correlate_alert_in_builtin_tools(self):
        # Codex P2 round 3: importing tool_registry_catalog at runtime
        # transitively imports `agent_core.accessibility`, which hard-
        # imports the macOS-only `ApplicationServices` module. That makes
        # this test fail with `ModuleNotFoundError: No module named
        # 'ApplicationServices'` on Linux CI / requirements-dev.txt-only
        # environments — even though correlate_alert itself is portable.
        # Parse the catalog source statically with `ast` instead: the
        # actual regression we guard against is "the line `correlate_alert,`
        # got deleted from BASE_BUILTIN_TOOLS", which a static read verifies
        # without triggering the macOS extras.
        import ast
        from pathlib import Path
        catalog_path = (
            Path(__file__).resolve().parent.parent
            / "agent_core" / "tool_registry_catalog.py"
        )
        tree = ast.parse(catalog_path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if not (isinstance(tgt, ast.Name) and tgt.id == "BASE_BUILTIN_TOOLS"):
                    continue
                if not isinstance(node.value, ast.List):
                    continue
                for elt in node.value.elts:
                    if isinstance(elt, ast.Name):
                        names.add(elt.id)
        self.assertIn(
            "correlate_alert", names,
            "correlate_alert missing from BASE_BUILTIN_TOOLS — "
            "the LLM cannot dispatch to it",
        )

    def test_correlate_alert_in_system_maintenance_intent(self):
        from agent_core.intent_router import (
            _TOOL_BUCKETS, INTENT_SYSTEM_MAINTENANCE,
        )
        bucket = _TOOL_BUCKETS.get(INTENT_SYSTEM_MAINTENANCE, frozenset())
        self.assertIn(
            "correlate_alert", bucket,
            "correlate_alert not in system_maintenance allowlist — "
            "the intent router will hide it from the LLM tool list",
        )

    def test_public_correlate_alert_does_not_expose_log_dir(self):
        """Codex P1 regression: the LLM-callable signature must not let
        a prompt-injected request supply log_dir = '/var/log' or any
        other readable directory, bypassing path_safety. Public
        `correlate_alert(alert_id)` accepts ONLY alert_id; tests use
        `_correlate_alert_for_test(alert_id, log_dir=...)` instead.
        """
        import inspect
        from agent_core.status_center import correlate_alert
        sig = inspect.signature(correlate_alert)
        params = list(sig.parameters)
        self.assertEqual(
            params, ["alert_id"],
            f"public correlate_alert signature leaked extra params: {params!r}; "
            f"the LLM tool dispatch path must not accept log_dir",
        )

    def test_correlate_alert_is_sensitive_tool(self):
        """Codex P2 regression: correlate_alert tails var/logs/*.log
        and returns normalized error-line snippets — even though log
        content is redacted before bucketing, prompt-injection requesting
        RCA could still exfiltrate signal that should require user
        approval. Must be in tg_auth._SENSITIVE_TOOLS so the +確認 / c
        gate is enforced through wrap_sensitive_tool when called via
        Telegram or voice."""
        from agent_core.tg_auth import _SENSITIVE_TOOLS, is_sensitive
        self.assertIn(
            "correlate_alert", _SENSITIVE_TOOLS,
            "correlate_alert is NOT in _SENSITIVE_TOOLS — Telegram-side "
            "callers can fetch log content via prompt-injection without "
            "the +確認 / c gate",
        )
        self.assertTrue(
            is_sensitive("correlate_alert"),
            "tg_auth.is_sensitive() must return True for correlate_alert",
        )


if __name__ == "__main__":
    unittest.main()
