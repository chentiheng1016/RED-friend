"""共用 embedding server 的 HTTP 協定 + 客戶端合約。

主 venv 沒有 torch/sentence-transformers，所以注入假模型測協定層
（embed_server 模組層只吃 stdlib+numpy 正是為了這件事）。鎖住的合約：
- /embed 回 (N, dim)、client 轉 np.ndarray float32（chromadb HttpClient
  會對每列呼叫 .tolist()，plain list 會炸 — 同 _GeminiEF 教訓）
- 空/超量/壞 JSON → 4xx；encode 例外 → 5xx；client 對 4xx/5xx 直接 raise
  不重試、絕不墊零向量
- 連線類錯誤才重試
"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import numpy as np

from agent_core import embed_http_client as client
from agent_core import embed_server


class _FakeModel:
    """encode 介面與 SentenceTransformer 對齊（kwargs 全吃）。"""

    def __init__(self, dim=8, fail=False):
        self.dim = dim
        self.fail = fail
        self.calls = []

    def encode(self, texts, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(list(texts))
        rng = np.random.default_rng(42)
        out = rng.normal(size=(len(texts), self.dim)).astype(np.float32)
        return out / np.linalg.norm(out, axis=1, keepdims=True)


class EmbedServerClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeModel(dim=8)
        # 保存/還原模組全域（別讓假模型漏進其他測試）
        self._saved = (embed_server._model, dict(embed_server._model_info))
        embed_server.set_model_for_tests(self.fake, dim=8, name="fake-bge")
        self.server = embed_server.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        # 測試環境隔離：URL 指到 ephemeral port、重試壓到 1 次加速失敗路徑
        self.env = mock.patch.dict(os.environ, {
            "RED_EMBED_HTTP_URL": f"http://127.0.0.1:{self.port}",
            "RED_EMBED_HTTP_RETRIES": "1",
            "RED_EMBED_HTTP_TIMEOUT_S": "10",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        embed_server._model, embed_server._model_info = self._saved

    def test_healthz_and_server_alive(self):
        self.assertTrue(client.server_alive())

    def test_embed_roundtrip_returns_ndarray_float32(self):
        out = client.embed_texts(["哈囉", "world"], kind="document")
        self.assertEqual(len(out), 2)
        for v in out:
            self.assertIsInstance(v, np.ndarray)  # .tolist() 合約
            self.assertEqual(v.dtype, np.float32)
            self.assertEqual(v.shape, (8,))
        self.assertEqual(self.fake.calls, [["哈囉", "world"]])

    def test_query_kind_accepted(self):
        out = client.embed_texts(["查詢句"], kind="query")
        self.assertEqual(out[0].shape, (8,))

    def test_str_input_wrapped(self):
        out = client.embed_texts("單一字串")
        self.assertEqual(len(out), 1)

    def test_empty_returns_empty_without_http(self):
        self.assertEqual(client.embed_texts([]), [])

    def test_bad_batch_rejected_4xx_no_retry(self):
        with mock.patch.dict(os.environ, {"RED_EMBED_MAX_BATCH": "2"}):
            with self.assertRaises(client.EmbedServerError) as ctx:
                client.embed_texts(["a", "b", "c"])
        self.assertIn("400", str(ctx.exception))

    def test_encode_failure_5xx_raises(self):
        self.fake.fail = True
        with self.assertRaises(client.EmbedServerError) as ctx:
            client.embed_texts(["a"])
        self.assertIn("500", str(ctx.exception))

    def test_dead_server_raises_after_retries(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(client.EmbedServerError) as ctx:
            client.embed_texts(["a"])
        self.assertIn("連不上", str(ctx.exception))

    def test_kind_validated_server_side(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/embed",
            data=json.dumps({"texts": ["a"], "kind": "weird"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
