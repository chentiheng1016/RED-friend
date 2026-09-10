"""scripts/migrate_embed_dim_768.py reconcile logic — on a throwaway
PersistentClient so it runs anywhere (no live chroma server, no network, no
Gemini). Locks: truncate→768, L2-normalize, preserve documents+metadata, and
prune ids no longer in source. The live end-to-end (operation_sops on the shared
server) and the going-forward EF wiring are covered elsewhere; this pins the
data transform.
"""
import importlib.util
import os
import shutil
import tempfile
import unittest

import numpy as np

from agent_core.path_safety import _REPO_ROOT


def _load_script():
    path = os.path.join(_REPO_ROOT, "scripts", "migrate_embed_dim_768.py")
    spec = importlib.util.spec_from_file_location("migrate_embed_dim_768", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubEF:
    """Stand-in for the gemini EF: never called (we always upsert explicit
    embeddings), same name() so chroma sees no embedding-function conflict, but
    no serialization/network coupling in the test."""

    def __call__(self, input):  # noqa: A002 — chroma's parameter name
        return [[0.0] * 768 for _ in input]

    def name(self):
        return "gemini-embedding-001"


class ReconcileLogicTests(unittest.TestCase):
    def setUp(self):
        import chromadb

        self.mig = _load_script()
        self.mig._ef = _StubEF()  # decouple from the real gemini EF
        self.tmp = tempfile.mkdtemp(prefix="dim768test_")
        self.client = chromadb.PersistentClient(path=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reconcile_truncates_normalizes_preserves_and_prunes(self):
        ids = ["a", "b", "c"]
        # distinct, non-zero 1000-dim source vectors (>768 so truncation is real)
        src_vecs = [[(i + 1) * 1.0 + j * 0.01 for j in range(1000)] for i in range(3)]
        docs = [f"doc {x}" for x in ids]
        metas = [{"k": x, "drive_id": "D1"} for x in ids]

        src = self.client.get_or_create_collection(
            "ztest", metadata={"hnsw:space": "cosine"}
        )
        src.upsert(ids=ids, embeddings=src_vecs, documents=docs, metadatas=metas)

        # pre-existing _768 with a stale orphan (created with the same-named EF so
        # reconcile's get_or_create reopens it without an EF conflict)
        dst = self.client.get_or_create_collection(
            "ztest_768", embedding_function=self.mig._ef, metadata={"hnsw:space": "cosine"}
        )
        dst.upsert(ids=["orphan"], embeddings=[[0.1] * 768], documents=["x"], metadatas=[{"k": "o"}])

        self.mig.reconcile_collection(self.client, "ztest", apply=True)

        dst = self.client.get_collection("ztest_768")  # reconcile dropped + recreated it
        out = dst.get(include=["embeddings", "documents", "metadatas"])
        self.assertEqual(set(out["ids"]), set(ids), "orphan gone (drop+rebuild), all source present")

        emb = {i: np.asarray(e, dtype=np.float64) for i, e in zip(out["ids"], out["embeddings"])}
        doc = dict(zip(out["ids"], out["documents"]))
        meta = dict(zip(out["ids"], out["metadatas"]))
        for i, _id in enumerate(ids):
            self.assertEqual(len(emb[_id]), 768, "truncated to 768")
            self.assertAlmostEqual(float(np.linalg.norm(emb[_id])), 1.0, places=5, msg="L2-normalized")
            want = np.asarray(src_vecs[i][:768])
            want = want / np.linalg.norm(want)
            self.assertAlmostEqual(float(np.dot(emb[_id], want)), 1.0, places=5, msg="== normalize(src[:768])")
            self.assertEqual(doc[_id], f"doc {_id}", "document preserved")
            self.assertEqual(meta[_id]["drive_id"], "D1", "metadata preserved")

    def test_dry_run_writes_nothing(self):
        src = self.client.get_or_create_collection("ztest", metadata={"hnsw:space": "cosine"})
        src.upsert(ids=["a"], embeddings=[[1.0] * 1000], documents=["d"], metadatas=[{"k": "a"}])

        self.mig.reconcile_collection(self.client, "ztest", apply=False)  # dry-run

        dst = self.client.get_or_create_collection(
            "ztest_768", embedding_function=self.mig._ef, metadata={"hnsw:space": "cosine"}
        )
        self.assertEqual(dst.count(), 0, "dry-run must not write")


if __name__ == "__main__":
    unittest.main()
