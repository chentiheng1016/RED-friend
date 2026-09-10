"""Regression for the SQL fast-path segment resolution after the 3072→768 cutover.

`_open_collection` writes to the *physical* collection (drive_docs_768 when
RED_EMBED_DIM=768), but `_load_metadata_segment_id` used to look up the *bare*
logical name ("drive_docs"). After the fleet-wide cutover that made every SQL
fast-path read (get_doc_metadata / list_doc_ids / count / find_duplicate) hit
the stale pre-migration 3072 collection — so fast-skip skipped files absent from
the live _768 index and re-embedded _768-only files on every nightly. This test
pins the fast-path to the same physical collection the writer uses.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_core.ingest import vector_store


class MetadataSegmentPhysicalNameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="chroma_seg_test_")
        conn = sqlite3.connect(os.path.join(self.dir, "chroma.sqlite3"))
        conn.executescript(
            """
            create table collections (id text, name text);
            create table segments (id text, collection text, scope text);
            insert into collections values ('c_old', 'drive_docs');
            insert into collections values ('c_768', 'drive_docs_768');
            insert into segments values ('seg_old', 'c_old', 'METADATA');
            insert into segments values ('seg_768', 'c_768', 'METADATA');
            insert into segments values ('seg_old_vec', 'c_old', 'VECTOR');
            """
        )
        conn.commit()
        conn.close()
        self._patch = mock.patch.object(vector_store, "_CHROMA_PATH", self.dir)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _segment(self, dim: str) -> str:
        # Fresh store each call so the per-instance TTL cache never masks the lookup.
        with mock.patch.dict(os.environ, {"RED_EMBED_DIM": dim}):
            return vector_store.VectorStore("drive_docs")._metadata_segment()

    def test_resolves_physical_768_after_cutover(self) -> None:
        self.assertEqual(self._segment("768"), "seg_768")

    def test_resolves_bare_collection_at_full_dim(self) -> None:
        self.assertEqual(self._segment("3072"), "seg_old")


if __name__ == "__main__":
    unittest.main()
