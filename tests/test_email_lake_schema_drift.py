"""Tests for _lake_append schema-drift defence.

The classify_email schema evolves over time: new columns get added
(e.g. routing tier, attachment count) while older parquets on disk
carry the previous shape. Naive ``pd.concat([old, new])`` works in
pandas 2.x but is fragile when one side has columns the other doesn't:

  - All-NA columns in the empty side get dtype inferred as ``object``,
    which then sticks to the parquet on next write — downstream
    queries that filter by typed columns silently miss those rows.
  - Column order can flip between runs depending on dict ordering,
    breaking parquet schema stability for external consumers.

The fix being pinned here:
  1. Compute a stable column_order as the ordered union of both sides
  2. Drop entirely-empty columns BEFORE concat so dtype inference
     uses real data
  3. Reindex back to the union after concat so every column survives
     even if it was empty on both sides

Rescued from an orphan stash (\"WIP - email_lake + ponder edits, not
mine\") — the ponder portion of that stash already shipped in PR #31;
this is the email_lake half.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


try:
    import numpy as np
    import pandas as pd
    _PANDAS_AVAILABLE = True
except ImportError:  # pragma: no cover - test env without pandas
    _PANDAS_AVAILABLE = False


@unittest.skipUnless(_PANDAS_AVAILABLE, "pandas not installed")
class LakeAppendSchemaDriftTests(unittest.TestCase):
    """Drive _lake_append through a mocked parquet I/O so we can pin the
    column-ordering and dtype invariants without touching real disk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="email_lake_test_")
        self.parquet_path = os.path.join(self.tmpdir, "emails_master.parquet")
        # Patch the module-level paths so the function under test writes
        # into our tmpdir and reads from it.
        from agent_core import email_lake
        self._orig_dir = email_lake._EMAIL_LAKE_DIR
        self._orig_parquet = email_lake._EMAIL_LAKE_PARQUET
        email_lake._EMAIL_LAKE_DIR = self.tmpdir
        email_lake._EMAIL_LAKE_PARQUET = self.parquet_path

    def tearDown(self):
        from agent_core import email_lake
        email_lake._EMAIL_LAKE_DIR = self._orig_dir
        email_lake._EMAIL_LAKE_PARQUET = self._orig_parquet
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_old_parquet(self, df):
        df.to_parquet(self.parquet_path, index=False)

    def _read_back(self):
        return pd.read_parquet(self.parquet_path)

    # ── column preservation ─────────────────────────────────────────

    def test_columns_unique_to_old_df_survive_append(self):
        """An old parquet has 'legacy_field'; new rows from updated
        classify_email don't. Naive concat would emit a FutureWarning;
        the fix preserves the column with NA for new rows."""
        from agent_core.email_lake import _lake_append
        self._seed_old_parquet(pd.DataFrame([
            {"message_id": "m1", "subject": "old1", "legacy_field": "x"},
        ]))
        _lake_append([{"message_id": "m2", "subject": "new1"}])
        result = self._read_back()
        self.assertIn("legacy_field", result.columns)
        # Old row preserves its legacy_field, new row gets NA.
        m1_row = result[result["message_id"] == "m1"].iloc[0]
        m2_row = result[result["message_id"] == "m2"].iloc[0]
        self.assertEqual(m1_row["legacy_field"], "x")
        self.assertTrue(pd.isna(m2_row["legacy_field"]))

    def test_columns_unique_to_new_df_survive_append(self):
        """Reverse direction: classify_email added a new column the old
        parquet doesn't have."""
        from agent_core.email_lake import _lake_append
        self._seed_old_parquet(pd.DataFrame([
            {"message_id": "m1", "subject": "old1"},
        ]))
        _lake_append([
            {"message_id": "m2", "subject": "new1", "tier": "high"},
        ])
        result = self._read_back()
        self.assertIn("tier", result.columns)

    # ── ordering stability ──────────────────────────────────────────

    def test_column_order_is_old_then_new(self):
        """Column order must be deterministic so the parquet schema
        doesn't shuffle between runs."""
        from agent_core.email_lake import _lake_append
        self._seed_old_parquet(pd.DataFrame([
            {"message_id": "m1", "a": 1, "b": 2},
        ]))
        _lake_append([{"message_id": "m2", "c": 3, "d": 4}])
        cols = list(self._read_back().columns)
        # message_id, a, b come first (from old); c, d appended (from new).
        self.assertEqual(cols.index("a") < cols.index("c"), True)
        self.assertEqual(cols.index("b") < cols.index("d"), True)

    # ── dedupe correctness preserved ────────────────────────────────

    def test_dedupe_by_message_id_keeps_last(self):
        """The schema-drift fix must not regress the existing
        last-write-wins dedupe behaviour."""
        from agent_core.email_lake import _lake_append
        self._seed_old_parquet(pd.DataFrame([
            {"message_id": "m1", "subject": "v1"},
        ]))
        _lake_append([{"message_id": "m1", "subject": "v2"}])
        result = self._read_back()
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["subject"], "v2")

    # ── empty-input edge case ───────────────────────────────────────

    def test_empty_rows_returns_zero_without_writing(self):
        from agent_core.email_lake import _lake_append
        self.assertFalse(os.path.exists(self.parquet_path))
        result = _lake_append([])
        self.assertEqual(result, 0)
        self.assertFalse(os.path.exists(self.parquet_path))

    def test_first_append_no_existing_parquet(self):
        """Cold-start: no parquet yet. The else-branch (no schema drift
        path) must still work."""
        from agent_core.email_lake import _lake_append
        n = _lake_append([{"message_id": "m1", "subject": "first"}])
        self.assertEqual(n, 1)
        result = self._read_back()
        self.assertEqual(list(result["message_id"]), ["m1"])

    # ── all-NaN column behaviour ────────────────────────────────────

    def test_all_na_column_in_old_df_does_not_corrupt_typed_new_data(self):
        """The exact failure mode the fix targets: an old parquet has a
        column that's all-NaN (from past schema drift). Naive concat
        would let the empty side's object dtype propagate to the merged
        column. After the fix, the new row's typed value should win."""
        from agent_core.email_lake import _lake_append
        self._seed_old_parquet(pd.DataFrame([
            {"message_id": "m1", "subject": "old", "score": pd.NA},
        ]))
        _lake_append([{"message_id": "m2", "subject": "new", "score": 0.95}])
        result = self._read_back()
        m2_row = result[result["message_id"] == "m2"].iloc[0]
        self.assertAlmostEqual(float(m2_row["score"]), 0.95)

    def test_int_values_in_new_batch_upcast_to_old_float_dtype(self):
        """Codex finding on PR #32 round 2: when old has typed all-NA
        column (e.g. amount float64) and new has typed int values for
        the same column, the merged dtype must remain compatible with
        old's parquet schema. pd.concat handles this correctly via
        NaN-padding-induced upcast — int64 + NaN → float64. Verify the
        invariant explicitly so a future refactor that re-introduces a
        dropna pre-pass would fail loudly."""
        from agent_core.email_lake import _lake_append
        old_df = pd.DataFrame({
            "message_id": pd.Series(["m1"], dtype="object"),
            "amount": pd.Series([np.nan], dtype="float64"),
        })
        self._seed_old_parquet(old_df)
        # New batch types 'amount' as int64.
        _lake_append([
            {"message_id": "m2", "amount": 100},
            {"message_id": "m3", "amount": 200},
        ])
        result = self._read_back()
        # dtype must accommodate both NaN (from old) and int values
        # (from new) — float64 is the natural lowest common type.
        self.assertEqual(str(result["amount"].dtype), "float64")
        # Values preserved (just upcast).
        m2 = result[result["message_id"] == "m2"].iloc[0]
        m3 = result[result["message_id"] == "m3"].iloc[0]
        self.assertEqual(float(m2["amount"]), 100.0)
        self.assertEqual(float(m3["amount"]), 200.0)

    def test_typed_all_na_legacy_column_keeps_dtype_when_new_batch_omits_it(self):
        """Codex finding on PR #32: if an old column is typed (e.g.
        float64) but currently all-NaN AND the new batch omits the
        column, dropna+pd.NA-rebuild would flip its dtype to object.
        The dtype-capture path must restore the original typing."""
        from agent_core.email_lake import _lake_append
        old_df = pd.DataFrame({
            "message_id": pd.Series(["m1"], dtype="object"),
            # np.nan (not pd.NA) is the only thing that fits a float64
            # series — pd.NA → TypeError. The point is the column is
            # typed float64 with no real values, simulating the
            # 'legacy column never populated yet' production state.
            "amount": pd.Series([np.nan], dtype="float64"),
        })
        self._seed_old_parquet(old_df)
        # New batch deliberately omits the 'amount' column.
        _lake_append([{"message_id": "m2", "subject": "new"}])
        result = self._read_back()
        # Column survived: ✓ (old fix already covered this)
        self.assertIn("amount", result.columns)
        # The actual regression: dtype should still be float64, NOT object.
        self.assertEqual(
            str(result["amount"].dtype),
            "float64",
            f"Expected amount dtype float64, got {result['amount'].dtype}. "
            "Schema-drift fix would silently flip dtypes when an all-NA "
            "legacy column is omitted from the new batch.",
        )

    def test_append_emits_no_concat_future_warning(self):
        """Review finding #15: dropping all-NA columns before concat must keep
        pandas from emitting the all-NA-dtype FutureWarning. Pinning this means
        a future pandas that flips the behaviour can't silently corrupt typed
        columns — the warning being gone proves we no longer rely on it."""
        import warnings

        from agent_core.email_lake import _lake_append
        self._seed_old_parquet(pd.DataFrame({
            "message_id": pd.Series(["m1"], dtype="object"),
            "amount": pd.Series([np.nan], dtype="float64"),
        }))
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            # Must not raise the concatenation FutureWarning.
            _lake_append([{"message_id": "m2", "subject": "new"}])
        result = self._read_back()
        self.assertEqual(str(result["amount"].dtype), "float64")


if __name__ == "__main__":
    unittest.main()
