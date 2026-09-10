"""gated Vertex embedding client 測試（_get_embed_client）。

全 mock 邊界、不打網路（真 Vertex 路徑已手動驗過、不放進 CI 免費額度/憑證）。
主執行緒 patch + setUp/tearDown 重置 module 快取，符合 unittest（非 pytest）隔離。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from agent_core import gemini_client as gc


class EmbedUseVertexFlagTests(unittest.TestCase):
    def test_default_off(self):
        env = dict(os.environ)
        env.pop("RED_EMBED_USE_VERTEX", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(gc._embed_use_vertex())

    def test_on(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_USE_VERTEX": "1"}):
            self.assertTrue(gc._embed_use_vertex())


class GetEmbedClientTests(unittest.TestCase):
    def setUp(self):
        gc._vertex_embed_client = None

    def tearDown(self):
        gc._vertex_embed_client = None

    def test_flag_off_uses_public_no_vertex_build(self):
        pub = object()
        with mock.patch.object(gc, "_embed_use_vertex", return_value=False), \
             mock.patch.object(gc, "_get_gemini_client", return_value=pub), \
             mock.patch.object(gc, "_build_vertex_embed_client") as build:
            self.assertIs(gc._get_embed_client(), pub)
            build.assert_not_called()

    def test_flag_on_builds_vertex_once_and_caches(self):
        vsent = object()
        with mock.patch.object(gc, "_embed_use_vertex", return_value=True), \
             mock.patch.object(gc, "_build_vertex_embed_client", return_value=vsent) as build:
            self.assertIs(gc._get_embed_client(), vsent)
            self.assertIs(gc._get_embed_client(), vsent)  # 第二次走快取
            build.assert_called_once()

    def test_flag_on_build_failure_falls_back_to_public(self):
        pub = object()
        with mock.patch.object(gc, "_embed_use_vertex", return_value=True), \
             mock.patch.object(gc, "_build_vertex_embed_client",
                               side_effect=RuntimeError("no creds")), \
             mock.patch.object(gc, "_get_gemini_client", return_value=pub):
            self.assertIs(gc._get_embed_client(), pub)
            # fallback 不應污染快取（下次仍會再試 Vertex）
            self.assertIsNone(gc._vertex_embed_client)


class VectorStoreUsesEmbedClientTests(unittest.TestCase):
    def test_gemini_embed_raw_routes_through_embed_client(self):
        from agent_core.ingest import vector_store as vs
        fake_emb = mock.MagicMock()
        fake_emb.values = [0.1, 0.2, 0.3]
        fake_client = mock.MagicMock()
        fake_client.models.embed_content.return_value.embeddings = [fake_emb]
        # _gemini_embed_raw 內部 `from agent_core.gemini_client import _get_embed_client`
        # record_embed_call 打掉：別讓測試把估算 token 寫進主 checkout 的 live cost.jsonl
        with mock.patch.object(gc, "_get_embed_client", return_value=fake_client), \
             mock.patch("agent_core.cost_tracker.record_embed_call"):
            out = vs._gemini_embed_raw(["hi"], "RETRIEVAL_DOCUMENT")
        fake_client.models.embed_content.assert_called_once()
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), 3)


if __name__ == "__main__":
    unittest.main()
