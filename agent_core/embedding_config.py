"""Single source of truth for the embedding backend, output dimension, and the
Chroma collection name that goes with them. Shared by `ingest/vector_store` and
`memory` so the two embedding functions can never drift on backend / dimension /
normalization / collection suffix (they have drifted before — see the "對齊
ingest/vector_store._GeminiEF" note in memory.py).

## Backend（RED_EMBED_BACKEND）

- **"gemini"（預設）** — gemini-embedding-001 走 API。維度由 RED_EMBED_DIM 控制
  （見下節），行為 byte-for-byte 不變。
- **"bge"** — 本機 bge-m3 走共用 embedding HTTP server（`agent_core/embed_server.py`
  daemon、`RED_EMBED_HTTP_URL`）。固定 1024 維、server 端已 L2-normalize，
  collections 掛 `_bge1024` 後綴（`drive_docs_bge1024`、…）。
  依據 2026-08-01 影子評測（project memory local-embedding-eval）：品質可用、
  M5 全量 6.25M chunks ≈ 52h、經常性 embedding API 成本歸零。

兩個 backend 的索引實體隔離（`_768` vs `_bge1024`），cutover = 翻
RED_EMBED_BACKEND + restart，gemini collections 原地保留 → 分鐘級回退，
與 3072→768 遷移同一套劇本。

## Gemini 維度（RED_EMBED_DIM，僅 backend=gemini 時有意義）

- **3072 (default)** — the full Matryoshka vector. The model returns it already
  L2-normalized. We pass NO `output_dimensionality` and do NOT touch the vector,
  so this path is byte-for-byte the legacy behavior. Collections keep their bare
  logical names (`drive_docs`, `gmail_threads`, `xiaohong_memory`, …).
- **768 (Matryoshka truncation)** — embed with `output_dimensionality=768`.
  Native 768 is exactly the truncated 3072 direction but returned un-normalized
  (|v| ≈ 0.58)，所以這裡 L2-normalize（verified 2026-06-20, see project memory
  chroma_dim_migration_768）。768 vectors live in suffixed collections
  (`drive_docs_768`, …)。

注意：`embed_config_extra()` / `maybe_normalize()` 是 gemini 專屬數學，只看
RED_EMBED_DIM、對 backend flag 免疫 — bge 路徑不呼叫它們，混用也不會把
gemini 向量規則弄壞。
"""
from __future__ import annotations

import math
import os

from agent_core.env_utils import env_int

# The full, native output dim of gemini-embedding-001. At this dim the model
# returns a pre-normalized vector and we add zero overhead — the legacy path.
FULL_EMBED_DIM = 768 * 4  # 3072
# Matryoshka dims we support truncating to. Keep this conservative: each value
# needs its own _<dim> collections built + verified before it's a real option.
_VALID_EMBED_DIMS = (768, FULL_EMBED_DIM)

# 本機 bge-m3 backend：維度與後綴是常數（模型固有 1024 維，無 Matryoshka 需求）。
BGE_EMBED_DIM = 1024
BGE_COLLECTION_SUFFIX = "bge1024"
BGE_MODEL_NAME = "bge-m3"

_VALID_BACKENDS = ("gemini", "bge")


def embed_backend() -> str:
    """Resolve RED_EMBED_BACKEND at call time (cheap: os.environ). Read live
    rather than captured at import so tests can flip it with mock.patch.dict and
    both embedding functions observe the same value without a module reload.
    Fails loud on a misconfigured value — a typo'd backend must crash, never
    embed into the wrong space silently."""
    backend = os.environ.get("RED_EMBED_BACKEND", "gemini").strip() or "gemini"
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"RED_EMBED_BACKEND={backend!r} 不支援；必須是 {_VALID_BACKENDS} 之一"
        )
    return backend


def _gemini_dim() -> int:
    """gemini 空間的維度（只看 RED_EMBED_DIM）。gemini 專屬數學一律走這個，
    不走 embed_dim() — 後者在 backend=bge 時回 1024，會把 gemini 的
    normalize/extra-kwargs 判斷弄錯。"""
    dim = env_int("RED_EMBED_DIM", FULL_EMBED_DIM, min_value=1)
    if dim not in _VALID_EMBED_DIMS:
        raise ValueError(
            f"RED_EMBED_DIM={dim} 不支援；必須是 {_VALID_EMBED_DIMS} 之一"
        )
    return dim


def embed_dim() -> int:
    """當前 active embedding 空間的維度：backend=bge 固定 1024，
    backend=gemini 依 RED_EMBED_DIM（768 或 3072）。"""
    if embed_backend() == "bge":
        return BGE_EMBED_DIM
    return _gemini_dim()


def embed_config_extra() -> dict:
    """Extra kwargs to merge into gemini `EmbedContentConfig`. Empty at the full
    dim (legacy: no output_dimensionality passed), else `{output_dimensionality}`.
    gemini 專屬 — 只看 RED_EMBED_DIM，backend flag 不影響。"""
    dim = _gemini_dim()
    return {} if dim == FULL_EMBED_DIM else {"output_dimensionality": dim}


def maybe_normalize(vec: list[float]) -> list[float]:
    """L2-normalize a truncated gemini vector; identity at the full dim.

    gemini 專屬 — 只看 RED_EMBED_DIM，backend flag 不影響（bge server 端
    已 normalize，不經過這裡）。At FULL_EMBED_DIM the model already returns a
    normalized vector and we return it untouched (same object) so the 3072 path
    is unchanged. Below it, the native truncated output is un-normalized, so we
    normalize. A zero vector is returned as-is (no div-by-zero); upstream
    already rejects None/missing embeds so a true zero vector does not occur in
    practice."""
    if _gemini_dim() == FULL_EMBED_DIM:
        return vec
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _physical_name(logical: str, backend: str, gemini_dim: int) -> str:
    """某 (backend, gemini_dim) 組合下 logical collection 的實體名。"""
    if backend == "bge":
        return f"{logical}_{BGE_COLLECTION_SUFFIX}"
    return logical if gemini_dim == FULL_EMBED_DIM else f"{logical}_{gemini_dim}"


def physical_collection_name(logical: str) -> str:
    """Map a logical collection name to the physical Chroma collection for the
    active backend+dim: bare name at gemini full dim, `<logical>_<dim>` when
    truncated, `<logical>_bge1024` on the bge backend. The suffix keeps every
    embedding space physically isolated so a cutover never mutates other spaces'
    data and can be rolled back by flipping the env back."""
    return _physical_name(logical, embed_backend(), _gemini_dim())


def sibling_collection_names(logical: str) -> list[str]:
    """`logical` 在「非當前組態」的其他支援空間下的實體 collection 名。

    vector_store 的裸名誤連警告用：手動/唯讀 session 忘帶 RED_EMBED_DIM /
    RED_EMBED_BACKEND 時會 get_or_create 錯的實體名，而 production 資料全在
    兄弟 collection 裡——查誰都 absent 卻不報錯。開名前對照這份清單就能發現
    兄弟還活著。涵蓋跨 backend 的組合（bare / _768 / _bge1024）。"""
    current = physical_collection_name(logical)
    variants = [
        _physical_name(logical, "gemini", d) for d in _VALID_EMBED_DIMS
    ] + [_physical_name(logical, "bge", FULL_EMBED_DIM)]
    return [v for v in variants if v != current]
