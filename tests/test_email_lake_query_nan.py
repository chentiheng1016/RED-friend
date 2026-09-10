"""Regression (review finding #5): query_email_lake rendered row fields with
bare bracket access (`r['subject'][:60]`, `r['amount']`). A NaN subject —
exactly what a schema-drift reindex pads missing cells with — is a float, so
slicing it raised and aborted the whole query; a NaN amount is truthy, so it
printed the literal "nan nan". Pin that NaN cells render cleanly.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class QueryEmailLakeNaNTests(unittest.TestCase):
    def _df(self):
        import numpy as np
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "dept": "業務", "doc_type": "quote_out", "entity": "ACME",
                    "date": "2026-06-01", "amount": 1234.5, "currency": "USD",
                    "summary": "報價單", "sender": "alice@acme.com",
                    "subject": "Quote for ACME", "message_id": "m1",
                },
                {
                    # The drifted row: amount/currency/subject all missing → NaN.
                    "dept": "業務", "doc_type": "invoice", "entity": "BETA",
                    "date": "2026-06-02", "amount": np.nan, "currency": np.nan,
                    "summary": "發票", "sender": "bob@beta.com",
                    "subject": np.nan, "message_id": "m2",
                },
            ]
        )

    def _query(self, **kwargs):
        from agent_core import email_lake

        df = self._df()
        with mock.patch.object(email_lake, "_lake_load_df", return_value=df):
            return email_lake.query_email_lake(days=0, limit=20, **kwargs)

    def test_nan_row_does_not_crash_and_renders_both_rows(self):
        out = self._query()
        self.assertIn("ACME", out)
        self.assertIn("BETA", out)  # the NaN row survives instead of crashing

    def test_nan_cells_never_render_literal_nan(self):
        out = self._query()
        self.assertNotIn("nan", out.lower())

    def test_good_amount_still_shown(self):
        out = self._query()
        self.assertIn("1234.5 USD", out)

    def test_nan_amount_row_has_no_money_line(self):
        out = self._query()
        # The BETA row has no amount → no 💰 line attached to it. Only the
        # ACME row should carry a money line.
        self.assertEqual(out.count("💰"), 1)

    def _query_raw(self, **kwargs):
        from agent_core import email_lake

        df = self._df()
        with mock.patch.object(email_lake, "_lake_load_df", return_value=df):
            return email_lake.query_email_lake(**kwargs)

    def test_non_int_days_and_limit_do_not_crash(self):
        # LLM-callable tool: a non-int days/limit must coerce to the documented
        # defaults (30/20) instead of ValueError-ing the whole query. Assert it
        # runs the query path to completion (clock-independent — fixed test
        # dates may fall outside a 30-day window as wall-clock advances).
        out = self._query_raw(days="三十天", limit="一些")
        self.assertIsInstance(out, str)
        self.assertIn("筆", out)  # "找到 N 筆" or "0 筆符合" — not an exception

    def test_none_days_and_limit_do_not_crash(self):
        out = self._query_raw(days=None, limit=None)
        self.assertIsInstance(out, str)
        self.assertIn("筆", out)

    def test_zero_days_shows_rows_regardless_of_clock(self):
        # days=0 disables the date filter, so the fixed-date rows always render
        # — proves the coercion didn't break the normal no-time-filter path.
        out = self._query_raw(days=0, limit=20)
        self.assertIn("ACME", out)
        self.assertIn("BETA", out)


if __name__ == "__main__":
    unittest.main()
