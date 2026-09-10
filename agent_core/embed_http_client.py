"""共用 embedding server（agent_core/embed_server.py）的客戶端。

近葉模組：stdlib + numpy + env_utils。vector_store / memory 的 EF 在
RED_EMBED_BACKEND=bge 時走這裡；背填腳本也用同一條路（production 與背填
同 code path，嵌出來的空間不可能歪）。

失敗語意：連線類錯誤（server 重啟窗口）退避重試；HTTP 4xx/5xx 或形狀
不對一律 raise — 絕不回零向量或 None 墊數（零向量會污染向量空間）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

import numpy as np

from agent_core.env_utils import env_float, env_int
from agent_core.logging_and_paths import logger


def _base_url() -> str:
    return (os.environ.get("RED_EMBED_HTTP_URL", "").strip()
            or "http://127.0.0.1:8601")


def _timeout_s() -> float:
    # 256 docs × ~33 docs/s ≈ 8s；cold cache / 搶 GPU 時再寬裕些。
    return env_float("RED_EMBED_HTTP_TIMEOUT_S", 180.0, min_value=1.0)


def _retries() -> int:
    return env_int("RED_EMBED_HTTP_RETRIES", 3, min_value=1)


class EmbedServerError(RuntimeError):
    """embed server 回了錯誤或形狀不對（不可重試層級）。"""


def server_alive(timeout_s: float = 3.0) -> bool:
    """healthz 探測 — 部署驗證 / smoke 用。"""
    try:
        with urllib.request.urlopen(
            f"{_base_url()}/healthz", timeout=timeout_s
        ) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception:
        return False


def _post_embed(texts: list[str], kind: str) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{_base_url()}/embed",
        data=json.dumps({"texts": texts, "kind": kind}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
        return json.loads(resp.read())


def embed_texts(texts: list[str], kind: str = "document") -> list[np.ndarray]:
    """texts → list of np.ndarray(float32)。EF 合約：chromadb HttpClient 的
    查詢路徑會對每個 embedding 呼叫 .tolist()，必須是 np.ndarray 不能是
    plain list（見 vector_store._GeminiEF docstring）。"""
    if isinstance(texts, str):
        texts = [texts]
    texts = list(texts)
    if not texts:
        return []

    last_exc: Exception | None = None
    for attempt in range(_retries()):
        try:
            payload = _post_embed(texts, kind)
            break
        except urllib.error.HTTPError as exc:
            # server 有回應但拒絕（4xx/5xx）：重試不會變好，直接 raise。
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = str(exc)
            raise EmbedServerError(
                f"embed server HTTP {exc.code}: {detail}"
            ) from exc
        except Exception as exc:  # URLError / timeout / conn refused
            last_exc = exc
            if attempt + 1 >= _retries():
                raise EmbedServerError(
                    f"embed server 連不上（{_base_url()}，已試 {_retries()} 次）："
                    f"{exc}。server 沒起來？見 launchd com.xiaohong.embed_server"
                ) from exc
            sleep_s = min(2.0 ** attempt, 8.0)
            logger.warning(
                "embed server 連線失敗（attempt %d/%d，%.1fs 後重試）：%s",
                attempt + 1, _retries(), sleep_s, exc,
            )
            time.sleep(sleep_s)
    else:  # pragma: no cover — break/raise 已涵蓋
        raise EmbedServerError(f"embed server 連不上: {last_exc}")

    rows = payload.get("embeddings")
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise EmbedServerError(
            f"embed server 回應形狀不對：{len(texts)} texts → "
            f"{len(rows) if isinstance(rows, list) else type(rows).__name__} rows"
        )
    out = [np.asarray(r, dtype=np.float32) for r in rows]
    dims = {v.shape[-1] for v in out}
    if len(dims) != 1:
        raise EmbedServerError(f"embed server 回應維度不一致：{sorted(dims)}")
    return out
