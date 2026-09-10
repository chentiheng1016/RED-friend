"""Boolean-metadata ACL post-filter in VectorStore.query.

Chroma indexes int/float/string metadata values but NOT bool_value, so a
per-colour ACL filter ({"access_orange": {"$eq": True}}) full-scans the shared
embedding_metadata table for tens of seconds (measured 54s on an 8k collection,
2026-07-24). VectorStore.query sidesteps that: it over-fetches a candidate pool
with only the cheap (indexed) scope terms pushed down to Chroma, then evaluates
the boolean predicate in Python. These tests pin the where-splitting, the
in-Python predicate, and the query() fetch/post-filter behaviour so the fast
path can't silently regress into re-sending the bool filter to the server.
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.ingest import vector_store as vs  # noqa: E402


class WhereTermIsBoolTests(unittest.TestCase):
    def test_bare_bool_is_bool(self):
        self.assertTrue(vs._where_term_is_bool(True))
        self.assertTrue(vs._where_term_is_bool(False))

    def test_eq_bool_is_bool(self):
        self.assertTrue(vs._where_term_is_bool({"$eq": True}))
        self.assertTrue(vs._where_term_is_bool({"$ne": False}))

    def test_in_list_of_bool_is_bool(self):
        self.assertTrue(vs._where_term_is_bool({"$in": [True]}))

    def test_string_and_int_terms_are_not_bool(self):
        self.assertFalse(vs._where_term_is_bool("D1"))
        self.assertFalse(vs._where_term_is_bool({"$eq": "D1"}))
        self.assertFalse(vs._where_term_is_bool({"$eq": 3}))
        # A bool stored in Python is an int subclass; make sure a numeric $eq
        # 1 is NOT treated as boolean (that must push down to the int index).
        self.assertFalse(vs._where_term_is_bool({"$eq": 1}))


class SplitWherePushdownTests(unittest.TestCase):
    def test_none_where(self):
        self.assertEqual(vs._split_where_pushdown(None), (None, False))

    def test_pure_acl_drops_to_none_with_bool_flag(self):
        pd, has_bool = vs._split_where_pushdown({"access_orange": {"$eq": True}})
        self.assertIsNone(pd)
        self.assertTrue(has_bool)

    def test_scope_only_pushes_down_unchanged(self):
        w = {"drive_id": {"$eq": "D1"}}
        pd, has_bool = vs._split_where_pushdown(w)
        self.assertEqual(pd, w)
        self.assertFalse(has_bool)

    def test_combined_and_keeps_scope_drops_bool(self):
        w = {"$and": [{"drive_id": {"$eq": "D1"}}, {"access_orange": {"$eq": True}}]}
        pd, has_bool = vs._split_where_pushdown(w)
        self.assertEqual(pd, {"drive_id": {"$eq": "D1"}})
        self.assertTrue(has_bool)

    def test_nested_and_scope_survives(self):
        w = {"$and": [
            {"$and": [{"drive_id": {"$eq": "D1"}}, {"folder_id": {"$eq": "F1"}}]},
            {"access_green": {"$eq": True}},
        ]}
        pd, has_bool = vs._split_where_pushdown(w)
        self.assertEqual(
            pd, {"$and": [{"drive_id": {"$eq": "D1"}}, {"folder_id": {"$eq": "F1"}}]}
        )
        self.assertTrue(has_bool)

    def test_or_with_bool_branch_is_dropped_wholesale(self):
        # Removing one OR branch would make the pushdown a SUBSET and could hide
        # authorised rows, so an $or containing any bool term must push nothing.
        w = {"$or": [{"drive_id": {"$eq": "D1"}}, {"access_orange": {"$eq": True}}]}
        pd, has_bool = vs._split_where_pushdown(w)
        self.assertIsNone(pd)
        self.assertTrue(has_bool)


class EvalWhereTests(unittest.TestCase):
    def test_eq_bool_present_and_absent(self):
        w = {"access_orange": {"$eq": True}}
        self.assertTrue(vs._eval_where(w, {"access_orange": True}))
        self.assertFalse(vs._eval_where(w, {"access_orange": False}))
        self.assertFalse(vs._eval_where(w, {}))  # absent field never matches $eq

    def test_int_one_matches_bool_true(self):
        # Python's 1 == True closes the storage-type gap that made a server-side
        # {"$eq": 1} miss bool-stored rows.
        self.assertTrue(vs._eval_where({"access_orange": {"$eq": True}}, {"access_orange": 1}))

    def test_and_of_scope_and_acl(self):
        w = {"$and": [{"drive_id": {"$eq": "D1"}}, {"access_orange": {"$eq": True}}]}
        self.assertTrue(vs._eval_where(w, {"drive_id": "D1", "access_orange": True}))
        self.assertFalse(vs._eval_where(w, {"drive_id": "D2", "access_orange": True}))
        self.assertFalse(vs._eval_where(w, {"drive_id": "D1", "access_orange": False}))

    def test_ne_and_in(self):
        self.assertTrue(vs._eval_where({"owner_color": {"$ne": "red"}}, {"owner_color": "orange"}))
        self.assertFalse(vs._eval_where({"owner_color": {"$ne": "red"}}, {"owner_color": "red"}))
        self.assertTrue(vs._eval_where({"owner_color": {"$in": ["orange", "green"]}}, {"owner_color": "green"}))
        self.assertFalse(vs._eval_where({"owner_color": {"$in": ["orange"]}}, {"owner_color": "green"}))

    def test_numeric_comparisons(self):
        self.assertTrue(vs._eval_where({"chunk_index": {"$gte": 2}}, {"chunk_index": 3}))
        self.assertFalse(vs._eval_where({"chunk_index": {"$gt": 3}}, {"chunk_index": 3}))
        # A bool must not satisfy a numeric comparison.
        self.assertFalse(vs._eval_where({"x": {"$gt": 0}}, {"x": True}))


class _FakeCol:
    """Records the kwargs of the last query() and returns canned rows."""

    def __init__(self, rows):
        # rows: list of (doc, metadata, distance), assumed distance-sorted.
        self._rows = rows
        self.last_kwargs = None

    def query(self, **kwargs):
        self.last_kwargs = kwargs
        n = kwargs["n_results"]
        rows = self._rows[:n]
        return {
            "documents": [[r[0] for r in rows]],
            "metadatas": [[r[1] for r in rows]],
            "distances": [[r[2] for r in rows]],
        }


class QueryPostFilterTests(unittest.TestCase):
    def _store_with(self, rows):
        store = vs.VectorStore("drive_docs")
        fake = _FakeCol(rows)
        store._with_collection = lambda op: op(fake)  # bypass the real server
        return store, fake

    def test_non_bool_where_unchanged_fast_path(self):
        rows = [("d", {"drive_id": "D1"}, 0.1)]
        store, fake = self._store_with(rows)
        store.query("hello", n_results=5, where={"drive_id": {"$eq": "D1"}})
        self.assertEqual(fake.last_kwargs["n_results"], 5)
        self.assertEqual(fake.last_kwargs["where"], {"drive_id": {"$eq": "D1"}})

    def test_no_where_passes_nothing(self):
        rows = [("d", {"drive_id": "D1"}, 0.1)]
        store, fake = self._store_with(rows)
        store.query("hello", n_results=5, where=None)
        self.assertEqual(fake.last_kwargs["n_results"], 5)
        self.assertNotIn("where", fake.last_kwargs)

    def test_pure_acl_overfetches_and_drops_where(self):
        rows = [("d%d" % i, {"access_orange": i % 2 == 0}, 0.01 * i) for i in range(200)]
        store, fake = self._store_with(rows)
        hits = store.query("hello", n_results=5, where={"access_orange": {"$eq": True}})
        expected_pool = min(vs._ACL_POSTFILTER_MAX_POOL, max(5, 5 * vs._ACL_POSTFILTER_OVERFETCH))
        self.assertEqual(fake.last_kwargs["n_results"], expected_pool)
        self.assertNotIn("where", fake.last_kwargs)  # pushdown is None
        self.assertEqual(len(hits), 5)
        self.assertTrue(all(h["metadata"]["access_orange"] is True for h in hits))

    def test_combined_pushes_scope_and_postfilters_acl(self):
        rows = [
            ("a", {"drive_id": "D1", "access_orange": True}, 0.1),
            ("b", {"drive_id": "D1", "access_orange": False}, 0.2),
            ("c", {"drive_id": "D1", "access_orange": True}, 0.3),
        ]
        store, fake = self._store_with(rows)
        where = {"$and": [{"drive_id": {"$eq": "D1"}}, {"access_orange": {"$eq": True}}]}
        hits = store.query("hello", n_results=5, where=where)
        # scope pushed down, bool removed
        self.assertEqual(fake.last_kwargs["where"], {"drive_id": {"$eq": "D1"}})
        self.assertGreater(fake.last_kwargs["n_results"], 5)  # over-fetched
        self.assertEqual([h["text"] for h in hits], ["a", "c"])

    def test_under_return_is_fail_safe(self):
        # Only one authorised row in the whole pool → return just it, never an
        # unauthorised chunk to pad up to n.
        rows = [("a", {"access_orange": False}, 0.1),
                ("b", {"access_orange": True}, 0.2),
                ("c", {"access_orange": False}, 0.3)]
        store, _ = self._store_with(rows)
        hits = store.query("hello", n_results=5, where={"access_orange": {"$eq": True}})
        self.assertEqual([h["text"] for h in hits], ["b"])

    def test_respects_requested_n_after_filter(self):
        rows = [("d%d" % i, {"access_orange": True}, 0.01 * i) for i in range(200)]
        store, _ = self._store_with(rows)
        hits = store.query("hello", n_results=3, where={"access_orange": {"$eq": True}})
        self.assertEqual(len(hits), 3)


if __name__ == "__main__":
    unittest.main()
