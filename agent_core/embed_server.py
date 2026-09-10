"""共用本機 embedding HTTP server（bge-m3）— fleet 的 embedding 咽喉。

為什麼是獨立 server：TG 十色 bot、web portal、rag_sync、dispatcher 全是
獨立 process，bge-m3 fp16 佔 ~1.2GB — 每個 process 各載一份不可行。照
chroma 共用 server（com.xiaohong.chroma）同一套劇本：單一 daemon 佔住模型，
全 fleet 走 localhost HTTP（RED_EMBED_HTTP_URL，預設 127.0.0.1:8601）。

為什麼跑在獨立 venv：torch/sentence-transformers 是重依賴，鐵則不裝主
.venv（見 CLAUDE.md）。`bin/embed-server-venv` 建 var/venvs/embed，launchd
template（com.xiaohong.embed_server.plist）用該 venv 的 python 啟動本模組。

模組層只 import stdlib + numpy，所以主 venv 的測試可以 import 本模組、
注入假模型測 HTTP 協定；sentence_transformers/torch 延後到 _load_model()
才 import（只在 embed venv 裡發生）。

協定（皆 JSON、只 bind loopback）：
  GET  /healthz          → {"ok": true, "model": "bge-m3", "dim": 1024, ...}
  POST /embed            → {"texts": [...], "kind": "document"|"query"}
                         ← {"embeddings": [[...], ...], "dim": 1024, "model": ...}
向量 server 端已 L2-normalize。任何項目失敗整批 5xx — 客戶端絕不能拿
零向量墊（零向量會污染向量空間，同 memory._GeminiEmbeddingFunction 鐵則）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from agent_core.env_utils import env_int

DEFAULT_PORT = 8601
_MODEL_NAME = "bge-m3"
_HF_MODEL_ID = "BAAI/bge-m3"

# 單一模型實體與其描述；encode 上鎖（MPS 上多執行緒 encode 沒有增益，
# 序列化反而避免記憶體尖峰）。
_model = None
_model_info: dict = {}
_encode_lock = threading.Lock()


def _batch_size() -> int:
    return env_int("RED_EMBED_BATCH", 64, min_value=1)


def _max_batch() -> int:
    """單一 HTTP 請求可帶的文本數上限（保護 server 記憶體）。"""
    return env_int("RED_EMBED_MAX_BATCH", 256, min_value=1)


def _load_model():
    """載入 bge-m3（僅在 embed venv 內呼叫；lazy import 重依賴）。"""
    global _model, _model_info
    import torch  # noqa: PLC0415 — 重依賴只活在 embed venv
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    kwargs = {}
    if device == "mps":
        # fp16 on MPS: 影子評測實測 33.6 docs/s（fp32 只有 10），品質無感。
        kwargs["model_kwargs"] = {"dtype": "float16"}
    model = SentenceTransformer(_HF_MODEL_ID, device=device, **kwargs)
    model.max_seq_length = env_int("RED_EMBED_MAX_SEQ", 1024, min_value=32)
    dim = int(model.get_sentence_embedding_dimension())
    set_model_for_tests(model, dim=dim, name=_MODEL_NAME, device=device)
    return model


def set_model_for_tests(model, dim: int, name: str = _MODEL_NAME,
                        device: str = "test") -> None:
    """注入模型（測試用假模型；production 由 _load_model 呼叫）。"""
    global _model, _model_info
    _model = model
    _model_info = {"model": name, "dim": int(dim), "device": device}


def _encode(texts: list[str], kind: str) -> np.ndarray:
    """單一入口：上鎖 encode、回 (N, dim) np.ndarray。

    bge-m3 的 document/query 走同一種編碼（不吃 instruct prompt）；`kind`
    保留在協定裡是為了未來換吃 prompt 的模型時不用改客戶端。"""
    assert _model is not None
    with _encode_lock:
        out = _model.encode(
            list(texts),
            batch_size=_batch_size(),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    return np.asarray(out, dtype=np.float32)


class _Handler(BaseHTTPRequestHandler):
    # 預設每請求兩行 access log 太吵（rag_sync 背填會打數萬次）；留錯誤即可。
    def log_message(self, format, *args):  # noqa: A002 — BaseHTTPRequestHandler 簽名
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 簽名
        if self.path != "/healthz":
            self._send_json(404, {"error": f"unknown path {self.path}"})
            return
        self._send_json(200, {"ok": True, **_model_info})

    def do_POST(self):  # noqa: N802
        if self.path != "/embed":
            self._send_json(404, {"error": f"unknown path {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"bad json: {exc}"})
            return

        texts = req.get("texts")
        kind = req.get("kind", "document")
        if (not isinstance(texts, list) or not texts
                or not all(isinstance(t, str) for t in texts)):
            self._send_json(400, {"error": "texts 必須是非空 list[str]"})
            return
        if len(texts) > _max_batch():
            self._send_json(
                400,
                {"error": f"batch {len(texts)} 超過上限 {_max_batch()}"
                          "（RED_EMBED_MAX_BATCH）"},
            )
            return
        if kind not in ("document", "query"):
            self._send_json(400, {"error": f"kind={kind!r} 必須是 document|query"})
            return

        try:
            vecs = _encode(texts, kind)
        except Exception as exc:  # 失敗整批 5xx，不墊零向量
            self._send_json(500, {"error": f"encode 失敗: {exc}"})
            return
        self._send_json(200, {
            "embeddings": vecs.tolist(),
            "dim": int(vecs.shape[1]),
            "model": _model_info.get("model", _MODEL_NAME),
        })


def make_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """建 server（不啟動）。port=0 給測試拿 ephemeral port。"""
    return ThreadingHTTPServer((host, port), _Handler)


def main() -> None:
    # 沒走 rotate_log 的入口要自己掛行首時間戳（見 daemon_helpers.rotate_log docstring）
    from agent_core.daemon_helpers import install_stdout_timestamps
    install_stdout_timestamps()
    port = env_int("RED_EMBED_HTTP_PORT", DEFAULT_PORT, min_value=1)
    print(f"[embed_server] 載入 {_HF_MODEL_ID} …", flush=True)
    _load_model()
    info = dict(_model_info)
    server = make_server("127.0.0.1", port)
    print(
        f"[embed_server] ready model={info.get('model')} dim={info.get('dim')} "
        f"device={info.get('device')} listen=127.0.0.1:{port}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
