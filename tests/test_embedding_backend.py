"""RED_EMBED_BACKEND switch — gemini(API) vs bge(共用本機 embed server)。

Locks three things: (1) the default (unset) is byte-for-byte the gemini path —
dims/suffix/normalization all follow RED_EMBED_DIM exactly as before the flag
existed; (2) backend=bge pins dim=1024 and the `_bge1024` collection suffix
regardless of RED_EMBED_DIM; (3) the gemini-only math helpers
(embed_config_extra / maybe_normalize) are immune to the backend flag, so a
mixed configuration can never corrupt gemini vector rules.
"""
import math
import os
import unittest
from unittest import mock

from agent_core import embedding_config as ec


class EmbedBackendTests(unittest.TestCase):
    def test_default_backend_is_gemini(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_BACKEND", None)
            self.assertEqual(ec.embed_backend(), "gemini")

    def test_empty_env_is_gemini(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_BACKEND": ""}):
            self.assertEqual(ec.embed_backend(), "gemini")

    def test_invalid_backend_fails_loud(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_BACKEND": "openai"}):
            with self.assertRaises(ValueError):
                ec.embed_backend()

    def test_bge_pins_dim_and_suffix_regardless_of_red_embed_dim(self):
        for dim_env in ({}, {"RED_EMBED_DIM": "768"}, {"RED_EMBED_DIM": "3072"}):
            with mock.patch.dict(
                os.environ, {"RED_EMBED_BACKEND": "bge", **dim_env}
            ):
                self.assertEqual(ec.embed_dim(), 1024)
                self.assertEqual(
                    ec.physical_collection_name("drive_docs"),
                    "drive_docs_bge1024",
                )
                self.assertEqual(
                    ec.physical_collection_name("xiaohong_memory"),
                    "xiaohong_memory_bge1024",
                )

    def test_gemini_backend_unchanged_by_flag(self):
        with mock.patch.dict(
            os.environ,
            {"RED_EMBED_BACKEND": "gemini", "RED_EMBED_DIM": "768"},
        ):
            self.assertEqual(ec.embed_dim(), 768)
            self.assertEqual(
                ec.physical_collection_name("gmail_threads"), "gmail_threads_768"
            )

    def test_gemini_math_immune_to_bge_flag(self):
        """embed_config_extra / maybe_normalize 只看 RED_EMBED_DIM。"""
        with mock.patch.dict(
            os.environ,
            {"RED_EMBED_BACKEND": "bge", "RED_EMBED_DIM": "768"},
        ):
            self.assertEqual(ec.embed_config_extra(), {"output_dimensionality": 768})
            out = ec.maybe_normalize([3.0, 4.0])
            self.assertAlmostEqual(math.sqrt(sum(x * x for x in out)), 1.0, places=6)
        with mock.patch.dict(os.environ, {"RED_EMBED_BACKEND": "bge"}):
            os.environ.pop("RED_EMBED_DIM", None)
            v = [3.0, 4.0]
            self.assertIs(ec.maybe_normalize(v), v)  # gemini full-dim identity

    def test_siblings_cover_cross_backend_variants(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_DIM": "768"}):
            os.environ.pop("RED_EMBED_BACKEND", None)
            sibs = ec.sibling_collection_names("drive_docs")
            self.assertIn("drive_docs", sibs)            # gemini 3072 bare
            self.assertIn("drive_docs_bge1024", sibs)    # bge
            self.assertNotIn("drive_docs_768", sibs)     # current
        with mock.patch.dict(
            os.environ,
            {"RED_EMBED_BACKEND": "bge", "RED_EMBED_DIM": "768"},
        ):
            sibs = ec.sibling_collection_names("drive_docs")
            self.assertIn("drive_docs", sibs)
            self.assertIn("drive_docs_768", sibs)
            self.assertNotIn("drive_docs_bge1024", sibs)


if __name__ == "__main__":
    unittest.main()
