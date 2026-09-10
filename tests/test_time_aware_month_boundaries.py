"""Regression (review finding #4): 本月/上個月 used rolling 30-day offsets
((-30,0) / (-60,-30)), so on 2026-06-09 "上個月" resolved to 2026-04-10 ~
2026-05-10 — straddling two months and dropping ~20 days of the real May.
Pin that month terms now resolve to true calendar-month boundaries, and that
the existing year/relative tokens are unchanged.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ResolveOffsetsMonthTests(unittest.TestCase):
    def setUp(self):
        from agent_core import time_aware

        self.ta = time_aware

    def test_last_month_is_full_calendar_month(self):
        today = date(2026, 6, 9)
        self.assertEqual(
            self.ta._resolve_offsets(("last_month", "last_month_end"), today),
            ("2026-05-01", "2026-05-31"),
        )

    def test_this_month_runs_from_first_to_today(self):
        today = date(2026, 6, 9)
        self.assertEqual(
            self.ta._resolve_offsets(("this_month", 0), today),
            ("2026-06-01", "2026-06-09"),
        )

    def test_last_month_january_wraps_to_previous_december(self):
        today = date(2026, 1, 15)
        self.assertEqual(
            self.ta._resolve_offsets(("last_month", "last_month_end"), today),
            ("2025-12-01", "2025-12-31"),
        )

    def test_last_month_handles_february_length(self):
        # March → February; 2024 is a leap year (29 days).
        today = date(2024, 3, 10)
        self.assertEqual(
            self.ta._resolve_offsets(("last_month", "last_month_end"), today),
            ("2024-02-01", "2024-02-29"),
        )

    def test_existing_year_tokens_unchanged(self):
        today = date(2026, 6, 9)
        self.assertEqual(self.ta._resolve_offsets(("ytd", 0), today), ("2026-01-01", "2026-06-09"))
        self.assertEqual(
            self.ta._resolve_offsets(("last_year", "last_year_end"), today),
            ("2025-01-01", "2025-12-31"),
        )

    def test_integer_offsets_still_relative_to_today(self):
        today = date(2026, 6, 9)
        self.assertEqual(self.ta._resolve_offsets((-7, 0), today), ("2026-06-02", "2026-06-09"))


class DetectTimeFilterMonthTests(unittest.TestCase):
    """End-to-end: the detector now yields calendar-month ranges (not rolling
    30-day) for 本月/上個月 against the real `today`."""

    def setUp(self):
        from agent_core import time_aware

        self.ta = time_aware

    def test_last_month_query_starts_on_the_first(self):
        r = self.ta.detect_time_filter("上個月對帳")
        self.assertIsNotNone(r)
        start, end = r
        self.assertTrue(start.endswith("-01"), f"start should be a month-first: {start}")
        # End must be the last day of that same month (28–31), proving it's a
        # calendar month rather than a rolling 30-day window ending mid-month.
        self.assertEqual(start[:7], end[:7])
        self.assertIn(int(end[8:10]), (28, 29, 30, 31))


if __name__ == "__main__":
    unittest.main()
