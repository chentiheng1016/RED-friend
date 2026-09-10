"""EF backend 分派 — vector_store._BackendEF 與 memory._GeminiEmbeddingFunction。

鎖住的合約：
- 預設（env 未設）走 gemini：name() 與呼叫路徑 byte-for-byte 不變
- RED_EMBED_BACKEND=bge：兩處 EF 都改走 embed_http_client.embed_texts，
  documents 帶 kind=document、查詢帶 kind=query，name() 回 "bge-m3"
- call-time 解析：同一個 EF 實例在 env 翻轉後行為跟著變（長壽 daemon 不重載）
patching 都在主執行緒（見 mock.patch threading 鐵則）。
"""
import os
import unittest
from unittest import mock

import numpy as np

from agent_core import memory
from agent_core.ingest import vector_store as vs


def _fake_embed_texts_factory(calls):
    def fake(texts, kind="document"):
        calls.append((list(texts), kind))
        return [np.ones(4, dtype=np.float32) for _ in texts]
    return fake


class VectorStoreBackendEFTests(unittest.TestCase):
    def test_default_is_gemini_name(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_BACKEND", None)
            self.assertEqual(vs._ef.name(), vs._EMBED_MODEL)

    def test_bge_name(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_BACKEND": "bge"}):
            self.assertEqual(vs._ef.name(), "bge-m3")

    def test_bge_document_and_query_route_to_http_client(self):
        calls = []
        with mock.patch.dict(os.environ, {"RED_EMBED_BACKEND": "bge"}), \
                mock.patch("agent_core.embed_http_client.embed_texts",
                           side_effect=_fake_embed_texts_factory(calls)):
            docs = vs._ef(["甲", "乙"])
            qs = vs._ef.embed_query(["查詢"])
        self.assertEqual(calls, [(["甲", "乙"], "document"), (["查詢"], "query")])
        self.assertIsInstance(docs[0], np.ndarray)
        self.assertIsInstance(qs[0], np.ndarray)

    def test_gemini_path_untouched_by_dispatcher(self):
        """預設 backend 下 _BackendEF 精確委派到 _gemini_embed。"""
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_BACKEND", None)
            with mock.patch.object(
                vs, "_gemini_embed", return_value=[[1.0, 0.0]]
            ) as ge:
                out = vs._ef(["hello"])
                vs._ef.embed_query(["q"])
        self.assertEqual(ge.call_args_list[0].kwargs.get("task_type")
                         or ge.call_args_list[0].args[1], "RETRIEVAL_DOCUMENT")
        self.assertEqual(ge.call_args_list[1].kwargs.get("task_type")
                         or ge.call_args_list[1].args[1], "RETRIEVAL_QUERY")
        self.assertIsInstance(out[0], np.ndarray)

    def test_same_instance_follows_env_flip(self):
        calls = []
        with mock.patch.dict(os.environ, {"RED_EMBED_BACKEND": "bge"}), \
                mock.patch("agent_core.embed_http_client.embed_texts",
                           side_effect=_fake_embed_texts_factory(calls)):
            vs._ef(["x"])
        self.assertEqual(len(calls), 1)
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_BACKEND", None)
            self.assertEqual(vs._ef.name(), vs._EMBED_MODEL)  # 翻回 gemini


class MemoryBackendEFTests(unittest.TestCase):
    def test_default_gemini_name(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_BACKEND", None)
            self.assertEqual(
                memory._GeminiEmbeddingFunction().name(), "gemini-embedding-001"
            )

    def test_bge_routes_and_name(self):
        calls = []
        ef = memory._GeminiEmbeddingFunction()
        with mock.patch.dict(os.environ, {"RED_EMBED_BACKEND": "bge"}), \
                mock.patch("agent_core.embed_http_client.embed_texts",
                           side_effect=_fake_embed_texts_factory(calls)):
            ef(["文件"])
            ef.embed_query(["查詢"])
            self.assertEqual(ef.name(), "bge-m3")
        self.assertEqual(calls, [(["文件"], "document"), (["查詢"], "query")])

    def test_gemini_none_still_raises(self):
        """gemini 路徑的零向量守門不受 backend 改造影響。"""
        ef = memory._GeminiEmbeddingFunction()
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_EMBED_BACKEND", None)
            with mock.patch.object(
                memory, "_gemini_embed", return_value=[None]
            ):
                with self.assertRaises(RuntimeError):
                    ef(["x"])


if __name__ == "__main__":
    unittest.main()
