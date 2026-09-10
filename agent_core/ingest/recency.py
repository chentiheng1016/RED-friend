"""RAG 命中結果的「語意相似度 × 時間新鮮度」重排（leaf helper）。

純語意向量檢索只看 cosine 距離、完全不管文件新舊（見 drive_search /
chat_search 的 query()）。被問到「最新 / 最近 / 目前」的問題時，語意最像的
舊檔會壓過剛更新的新檔，甚至讓新檔擠不進 top-k。這個 helper 把較大的候選池用

    blended = (1 - w) · sim + w · freshness

重排後取前 k；freshness 隨文件年齡指數衰減（age == 半衰期時 == 0.5）。

不碰 ChromaDB 索引、不需重建、不改預設行為——呼叫端 opt-in（prefer_recent）。
時間欄位是 ingest 時存的 ISO 字串：Drive 用 `modified_time`、Chat 用
`last_message_time`，皆取前 10 碼 YYYY-MM-DD 解析。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

from agent_core.env_utils import env_float, env_int

# 預設半衰期 180 天：工廠營運資料（報價、料況、排程）通常半年內最有參考價值。
_DEFAULT_HALF_LIFE = 180.0
# 時間權重 0.35：以語意為主、新鮮度為輔，避免把不相關的新檔排到相關舊檔前面。
_DEFAULT_WEIGHT = 0.35
# 候選池上限 20：VectorStore.query() 本就把 n_results clamp 在 20，再大也拿不到。
_DEFAULT_POOL = 20

_LN2 = math.log(2)


def _parse_date(raw: Any) -> date | None:
    s = str(raw or "").strip()[:10]
    if len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _today() -> date:
    return datetime.now(timezone.utc).date()


def pool_size(n: int) -> int:
    """opt-in recency 重排時要撈的候選池大小（≥ 回傳數、≤ query 的 20 上限）。"""
    pool = env_int("RAG_RECENCY_POOL", _DEFAULT_POOL, min_value=1, max_value=20)
    return max(int(n), pool)


def freshness_score(
    meta: dict[str, Any] | None,
    time_field: str,
    half_life_days: float,
    *,
    today: date | None = None,
) -> float:
    """文件新鮮度 ∈ [0, 1]。無日期 / 解析失敗 → 0（視為最舊，prefer_recent 下會被壓低）。"""
    d = _parse_date((meta or {}).get(time_field))
    if d is None:
        return 0.0
    if half_life_days <= 0:
        return 1.0
    age = ((today or _today()) - d).days
    if age <= 0:
        return 1.0  # 今天或（時鐘偏移造成的）未來日期一律當最新
    return math.exp(-_LN2 * age / half_life_days)


def similarity(hit: dict[str, Any]) -> float:
    """把 ChromaDB 的 cosine distance 翻成相似度 ∈ [0, 1]（壞值當最遠）。"""
    try:
        dist = float(hit.get("distance"))
    except (TypeError, ValueError):
        dist = 1.0
    return max(0.0, 1.0 - dist)


def rerank_by_recency(
    hits: list[dict[str, Any]],
    time_field: str,
    k: int,
    *,
    half_life_days: float | None = None,
    recency_weight: float | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """用 blended = (1-w)·sim + w·freshness 重排候選池，回傳前 k 筆。

    每筆命中加一個 `_recency` 欄位（blended/sim/freshness）供輸出層標注，
    原 hit dict 不就地修改（淺拷貝）。
    """
    if not hits:
        return []
    hl = (
        float(half_life_days)
        if half_life_days is not None
        else env_float("RAG_RECENCY_HALF_LIFE_DAYS", _DEFAULT_HALF_LIFE, min_value=1.0)
    )
    w = (
        float(recency_weight)
        if recency_weight is not None
        else env_float("RAG_RECENCY_WEIGHT", _DEFAULT_WEIGHT, min_value=0.0, max_value=1.0)
    )
    ref = today or _today()
    scored: list[tuple[float, dict[str, Any]]] = []
    for h in hits:
        sim = similarity(h)
        fresh = freshness_score(h.get("metadata") or {}, time_field, hl, today=ref)
        blended = (1.0 - w) * sim + w * fresh
        h2 = dict(h)
        h2["_recency"] = {"blended": blended, "sim": sim, "freshness": fresh}
        scored.append((blended, h2))
    # 穩定排序：blended 相同則保留原向量距離順序（Python sort 穩定）。
    scored.sort(key=lambda t: t[0], reverse=True)
    return [h for _, h in scored[: max(1, int(k))]]
