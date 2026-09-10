"""RED_EMBED_DIM switch — the 3072→768 (Matryoshka) embedding migration.

Locks two things: (1) the default (RED_EMBED_DIM unset) is byte-for-byte the
legacy 3072 path — no output_dimensionality, no normalization, bare collection
names; (2) RED_EMBED_DIM=768 passes output_dimensionality=768, L2-normalizes the
un-normalized native truncation, and suffixes collections `_768`. Both embedding
functions (vector_store + memory) read the same helper so they can't drift.
"""
import math
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from agent_core import embedding_config as ec


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


class EmbeddingConfigHelperTests(unittest.TestCase):
    def test_default_is_legacy_full_dim(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_DIM", None)
            self.assertEqual(ec.embed_dim(), 3072)
            # legacy: no output_dimensionality kwarg, no rename, no normalize
            self.assertEqual(ec.embed_config_extra(), {})
            self.assertEqual(ec.physical_collection_name("drive_docs"), "drive_docs")
            v = [3.0, 4.0]
            self.assertIs(ec.maybe_normalize(v), v)  # identity (same object, untouched)

    def test_768_truncation_mode(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_DIM": "768"}):
            self.assertEqual(ec.embed_dim(), 768)
            self.assertEqual(ec.embed_config_extra(), {"output_dimensionality": 768})
            self.assertEqual(
                ec.physical_collection_name("gmail_threads"), "gmail_threads_768"
            )
            self.assertEqual(
                ec.physical_collection_name("xiaohong_memory"), "xiaohong_memory_768"
            )
            out = ec.maybe_normalize([3.0, 4.0])
            self.assertAlmostEqual(_norm(out), 1.0, places=6)
            self.assertAlmostEqual(out[0], 0.6, places=6)
            self.assertAlmostEqual(out[1], 0.8, places=6)

    def test_explicit_3072_equals_default(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_DIM": "3072"}):
            self.assertEqual(ec.embed_config_extra(), {})
            self.assertEqual(ec.physical_collection_name("drive_docs"), "drive_docs")

    def test_invalid_dim_fails_loud(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_DIM": "769"}):
            with self.assertRaises(ValueError):
                ec.embed_dim()


class _FakeGenaiTypes:
    """Captures the kwargs EmbedContentConfig is built with."""

    def __init__(self):
        self.last_config_kwargs = None

    def EmbedContentConfig(self, **kwargs):  # noqa: N802 — mirrors genai API
        self.last_config_kwargs = kwargs
        return SimpleNamespace(**kwargs)


def _fake_client(vec):
    def embed_content(model, contents, config):
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=list(vec)) for _ in contents]
        )

    return SimpleNamespace(models=SimpleNamespace(embed_content=embed_content))


class VectorStoreEmbedWiringTests(unittest.TestCase):
    """The real _gemini_embed_raw honors the dim switch end-to-end."""

    def _embed(self, env):
        from agent_core.ingest import vector_store as vs

        fake_types = _FakeGenaiTypes()
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_DIM", None)
            os.environ.update(env)
            with mock.patch(
                "agent_core.gemini_client._get_gemini_client",
                return_value=_fake_client([3.0, 4.0]),
            ), mock.patch(
                "agent_core.gemini_client._get_genai_types",
                return_value=fake_types,
            ), mock.patch(
                # 別讓測試把估算 token 寫進主 checkout 的 live cost.jsonl
                "agent_core.cost_tracker.record_embed_call",
            ):
                vecs = vs._gemini_embed_raw(["hello"], "RETRIEVAL_DOCUMENT")
        return fake_types.last_config_kwargs, vecs

    def test_default_passes_no_dim_and_does_not_normalize(self):
        cfg, vecs = self._embed({})
        self.assertNotIn("output_dimensionality", cfg)
        self.assertEqual(vecs, [[3.0, 4.0]])  # untouched

    def test_768_passes_dim_and_normalizes(self):
        cfg, vecs = self._embed({"RED_EMBED_DIM": "768"})
        self.assertEqual(cfg.get("output_dimensionality"), 768)
        self.assertAlmostEqual(_norm(vecs[0]), 1.0, places=6)
        self.assertAlmostEqual(vecs[0][0], 0.6, places=6)


if __name__ == "__main__":
    unittest.main()
