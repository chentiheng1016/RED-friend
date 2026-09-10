"""引用回饋（Phase 3）— 被答案引用過的文件，未來檢索微幅加權。

隱性回饋訊號：小紅回答時引用了某份文件（回覆裡帶 `id=<doc_id>` /
`space=<spaces/...>` 出處，這是 drive_search / chat_search / citation_guard
的既有格式），代表那份文件「真的被用上了」。把引用次數記進 ledger，
之後檢索時對常被引用的文件做**微幅** boost——純啟發式加權，無模型訓練；
權重刻意小（預設 0.15），語意相似度永遠是主信號。

跟 recency.py 同款 blended 框架：
    blended = (1 - w) · prior + w · usage
prior = 既有排序分數（有 _recency 就用它的 blended，否則純相似度）；
usage = log 壓縮的引用次數 × last_cited 新鮮度衰減（半衰期 60 天——
半年前常被引用的舊文件不該永遠壓著新文件）。

kill switch：RED_CITATION_FEEDBACK=0 整個停用（記錄與加權都停）。
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from agent_core.env_utils import env_bool, env_float
from agent_core.logging_and_paths import STATE_DIR, logger
from agent_core.state_io import locked_json

LEDGER_FILE = os.path.join(STATE_DIR, "citation_feedback.json")
# 「近期被檢索到的 key」白名單——檢索工具回傳命中時順手登記（可能在
# tool-RPC worker 程序），record 端（daemon 程序）只採計白名單內的引用。
RECENT_KEYS_FILE = os.path.join(STATE_DIR, "citation_recent_keys.json")

_MAX_ENTRIES = 2000     # ledger 上限，超過丟最久沒被引用的
_MAX_PER_REPLY = 20     # 單則回覆最多記幾個引用（防灌水）
_MAX_KEY_CHARS = 64     # key 長度上限（Drive id ~44、space name ~40、hex 16）
_USAGE_SATURATION = 15  # 引用次數的 log 飽和點（15 次 ≈ 滿分）
_HALF_LIFE_DAYS = 60.0  # last_cited 衰減半衰期
_RECENT_KEYS_TTL_S = 1800   # 白名單 30 分鐘窗（涵蓋一輪對話 + 遲到回覆）
_RECENT_KEYS_MAX = 500

# 出處 token（對齊既有輸出格式，別自創）：
#   drive_search: id=<Drive file id>（drive_search._format_hits）
#   gmail:        id=<16 hex thread_id>（citation_guard 同款 pattern）
#   chat_search:  space=<spaces/xxx>（chat chunk 的 doc_id 就是 space_name）
# 長度封頂 {10,64}：無上限的話，被注入的超長 token 會讓 ledger 條目任意肥大。
_CITATION_RE = re.compile(
    r"\b(?:id=([A-Za-z0-9_-]{10,64})|space=(spaces/[A-Za-z0-9_-]{1,56}))"
)

_LN2 = math.log(2)


def citation_feedback_enabled() -> bool:
    return env_bool("RED_CITATION_FEEDBACK", True)


def _citation_weight() -> float:
    return env_float("RAG_CITATION_WEIGHT", 0.15, min_value=0.0, max_value=1.0)


def extract_citation_keys(text: str) -> list[str]:
    """從一則回覆抽出被引用的文件 key（去重、保序、封頂）。"""
    if not text:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for m in _CITATION_RE.finditer(text):
        key = m.group(1) or m.group(2) or ""
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
            if len(keys) >= _MAX_PER_REPLY:
                break
    return keys


def _heal_non_dict_file(path: str) -> None:
    """檔案是「合法 JSON 但不是 dict」（手改壞/半寫）時直接刪掉自癒——
    locked_json(default={}) 對壞 JSON 會回 default，但對合法非 dict 會
    原樣 yield，後續 dict 操作丟 TypeError 被外層吞掉後**永遠**跳過、
    檔案永不修復（gemini review 抓到的缺口）。刪檔讓下次寫入重建。"""
    import json
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                os.remove(path)
                logger.warning("citation feedback 檔案形狀不對已重建：%s", path)
    except Exception:
        pass  # 壞 JSON：locked_json 的 default 機制自己會處理


def _read_json_nolock(path: str) -> dict[str, Any]:
    """唯讀快照：不持鎖、不寫回、任何失敗回 {}。查詢/採計路徑專用——
    locked_json 是 read-modify-write context manager（退出時整檔重寫 +
    排他鎖），純讀走它會讓每次 RAG 查詢都拿鎖重寫 ledger、壞檔還會被
    靜默清空（審查抓到的三重問題）。撕裂讀的機率極低、代價只是這次
    不加權，可接受。"""
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def note_retrieved_keys(keys: list[str]) -> None:
    """檢索工具（drive_search / chat_search）回傳命中後呼叫：登記「這些
    key 最近真的被檢索到」。record 端只採計白名單內的引用——這是 ledger
    污染的主防線：prompt-injection 讓 LLM 在回覆灌假 id（或灌真 id 但
    本輪根本沒檢索到它）都不會入帳。絕不 raise。"""
    try:
        if not citation_feedback_enabled() or not keys:
            return
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat(timespec="seconds")
        cutoff = now.timestamp() - _RECENT_KEYS_TTL_S
        _heal_non_dict_file(RECENT_KEYS_FILE)
        with locked_json(RECENT_KEYS_FILE, default={}) as recent:
            for key in list(keys)[:50]:
                k = str(key)[:_MAX_KEY_CHARS]
                if k:
                    recent[k] = now_iso
            # 過期與超量剪裁（保最新）
            def _ts(v: Any) -> float:
                try:
                    return datetime.fromisoformat(str(v)).timestamp()
                except (ValueError, TypeError):
                    return 0.0
            expired = [k for k, v in recent.items() if _ts(v) < cutoff]
            for k in expired:
                recent.pop(k, None)
            if len(recent) > _RECENT_KEYS_MAX:
                ordered = sorted(recent.items(), key=lambda kv: _ts(kv[1]))
                for k, _v in ordered[: len(recent) - _RECENT_KEYS_MAX]:
                    recent.pop(k, None)
    except Exception as exc:
        logger.debug("citation feedback 白名單登記失敗（略過）：%s", exc)


def _recently_retrieved(keys: list[str]) -> set[str]:
    """keys 之中「最近 30 分鐘內真的被檢索到」的子集（唯讀、無鎖）。"""
    recent = _read_json_nolock(RECENT_KEYS_FILE)
    if not recent:
        return set()
    cutoff = datetime.now(timezone.utc).timestamp() - _RECENT_KEYS_TTL_S
    out: set[str] = set()
    for key in keys:
        try:
            if datetime.fromisoformat(str(recent.get(key) or "")).timestamp() >= cutoff:
                out.add(key)
        except (ValueError, TypeError):
            continue
    return out


def record_citations_from_reply(text: str) -> int:
    """bot 最終回覆送出前呼叫。絕不 raise（回覆送出比記帳重要）。回傳記了幾個。

    只採計「回覆有引用 **且** 本輪（30 分鐘窗）檢索工具真的回傳過」的 key
    ——turn-scope 交集。沒被檢索過的 key（假 id、injection 灌的、憑記憶寫的）
    一律丟棄，順便封死「灌新 key 逐出 ledger 既有條目」的清洗路徑。"""
    try:
        if not citation_feedback_enabled():
            return 0
        keys = extract_citation_keys(text)
        if not keys:
            return 0
        allowed = _recently_retrieved(keys)
        keys = [k for k in keys if k in allowed]
        if not keys:
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _heal_non_dict_file(LEDGER_FILE)
        with locked_json(LEDGER_FILE, default={}) as ledger:
            for key in keys:
                entry = ledger.get(key) or {}
                entry["n"] = int(entry.get("n") or 0) + 1
                entry["last"] = now
                ledger[key] = entry
            if len(ledger) > _MAX_ENTRIES:
                # 丟最久沒被引用的（last 最舊）
                ordered = sorted(ledger.items(), key=lambda kv: str(kv[1].get("last") or ""))
                for stale_key, _v in ordered[: len(ledger) - _MAX_ENTRIES]:
                    ledger.pop(stale_key, None)
        return len(keys)
    except Exception as exc:
        logger.debug("citation feedback 記錄失敗（略過）：%s", exc)
        return 0


def _usage_score(entry: dict[str, Any], now: datetime) -> float:
    """引用強度 ∈ [0,1]：log 壓縮次數 × last_cited 指數衰減。"""
    try:
        n = int(entry.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    count_score = min(1.0, math.log1p(n) / math.log1p(_USAGE_SATURATION))
    try:
        last = datetime.fromisoformat(str(entry.get("last") or ""))
        age_days = max(0.0, (now - last).total_seconds() / 86400.0)
        decay = math.exp(-_LN2 * age_days / _HALF_LIFE_DAYS)
    except (ValueError, TypeError):
        decay = 0.5  # 壞時間戳：給半分而非滿分/零分
    return count_score * decay


def _load_boosts(keys: list[str]) -> dict[str, float]:
    """一次讀 ledger、算好每個 key 的 usage score。唯讀無鎖快照
    （_read_json_nolock）——查詢熱路徑絕不拿排他鎖、絕不重寫檔。"""
    if not keys:
        return {}
    ledger = _read_json_nolock(LEDGER_FILE)
    if not ledger:
        return {}
    now = datetime.now(timezone.utc)
    out: dict[str, float] = {}
    for k in keys:
        v = ledger.get(k)
        if isinstance(v, dict):
            out[k] = _usage_score(v, now)
    return out


def rerank_with_citations(
    hits: list[dict[str, Any]],
    k: int,
    *,
    weight: float | None = None,
) -> list[dict[str, Any]]:
    """用 blended = (1-w)·prior + w·usage 重排候選池，回傳前 k 筆。

    prior：有 `_recency` 註記（prefer_recent 已跑過）就用它的 blended，
    否則用純相似度——兩個信號疊加而非互斥。每筆命中加 `_citation` 註記
    （boost/blended）供輸出層顯示，原 hit 淺拷貝不就地改。
    """
    if not hits:
        return []
    w = _citation_weight() if weight is None else float(weight)
    if w <= 0.0:
        return hits[: max(1, int(k))]
    from agent_core.ingest.recency import similarity

    keys = [str((h.get("metadata") or {}).get("doc_id") or "") for h in hits]
    boosts = _load_boosts([key for key in keys if key])
    scored: list[tuple[float, dict[str, Any]]] = []
    for h, key in zip(hits, keys):
        rec = h.get("_recency") or {}
        prior = float(rec.get("blended")) if rec.get("blended") is not None else similarity(h)
        boost = boosts.get(key, 0.0)
        blended = (1.0 - w) * prior + w * boost
        h2 = dict(h)
        h2["_citation"] = {"boost": boost, "blended": blended}
        scored.append((blended, h2))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [h for _score, h in scored[: max(1, int(k))]]
