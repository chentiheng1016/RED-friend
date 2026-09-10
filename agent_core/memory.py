"""Long-term memory: ChromaDB vector RAG + BM25 hybrid + behavior policies.

ChromaDB singleton + Gemini embeddings. BM25 secondary index for exact
matches (part numbers, PO IDs). Behavior policies are a special
`source=behavior_policy` document type; _compile_behavior_policies()
pulls them all to inject into persona at chat-build time.

Cross-module deps handled with lazy imports inside function bodies:
  - _build_chat / _chat_state (agent.py): learn_behavior / forget_behavior
    rebuild chat so new/removed rules take effect immediately.
"""
from __future__ import annotations

import os
import json
import threading
import time
import hashlib
from datetime import datetime

import numpy as np

from agent_core import memory_ops
from agent_core.chroma_backend import build_chroma_client

from agent_core.gemini_client import (
    _get_gemini_client,
    _get_genai_types,
)
from agent_core.logging_and_paths import (
    logger,
    MEMORY_FILE,
    CHROMA_DB_DIR,
)
from agent_core.embedding_config import (
    BGE_MODEL_NAME,
    embed_backend,
    embed_config_extra,
    maybe_normalize,
    physical_collection_name,
)
from agent_core.state_io import locked_json

# ChromaDB: 預設落在 var/data/chroma_db；若舊環境仍使用 repo 根的 chroma_db/ 會自動沿用。
_VECTOR_DB_DIR = CHROMA_DB_DIR
_VECTOR_COLLECTION = "xiaohong_memory"
_EMBED_MODEL = "gemini-embedding-001"
_chroma_client = None
_chroma_collection = None
_vector_ready = None  # None=未試、True=OK、False=壞（冷卻到期會放行重試）
_vector_last_error = None  # 最後一次 init 失敗原因（成功時清空；health check 引用）
# 失敗後的重試冷卻：長壽 daemon（telegram bot 艦隊）若在 chroma server
# 重啟/網路波動的窗口撞到 init 失敗，不該到 redeploy 前都永久失去向量
# 記憶——冷卻到期就放行重試。用 monotonic（牆鐘跳動不影響間隔）。
_VECTOR_RETRY_COOLDOWN_S = 60.0
_vector_last_failure_ts = 0.0  # time.monotonic() 時間基準


def _gemini_embed(texts, task_type: str = "RETRIEVAL_DOCUMENT"):
    if isinstance(texts, str):
        texts = [texts]
    try:
        t0 = time.time()
        result = _get_gemini_client().models.embed_content(
            model=_EMBED_MODEL,
            contents=list(texts),
            config=_get_genai_types().EmbedContentConfig(
                task_type=task_type, **embed_config_extra()
            )
        )
        # 記帳緊貼成功回應（token 本地估算；2026-08 稽核前 embed 完全沒進帳）。
        try:
            from agent_core.cost_tracker import record_embed_call
            kind = "query" if task_type == "RETRIEVAL_QUERY" else "document"
            record_embed_call(
                model=_EMBED_MODEL, texts=texts,
                duration_ms=(time.time() - t0) * 1000.0,
                caller=f"memory.embed_{kind}",
            )
        except Exception:
            pass
        # Identity at the full dim; L2-normalizes the un-normalized native-768
        # output so memory's vectors share the space the backfill builds.
        return [maybe_normalize(list(e.values)) for e in result.embeddings]
    except Exception as e:
        logger.warning("Gemini embed 失敗（%s）", e)
        return [None] * len(texts)


class _GeminiEmbeddingFunction:
    """讓 chromadb 走我們自己的 embedding（依 RED_EMBED_BACKEND 分派）。
    gemini：文件走 RETRIEVAL_DOCUMENT；查詢走 RETRIEVAL_QUERY，召回率最佳。
    bge：documents/queries 都走共用 embed server（embed_http_client），
    與 ingest/vector_store._BgeHttpEF 同一條路、同一空間。
    ⚠️ 任何項目 embed 失敗一律 raise，不用零向量代替（零向量會污染整個向量空間）。"""
    def __call__(self, input):
        if embed_backend() == "bge":
            from agent_core.embed_http_client import embed_texts
            return embed_texts(list(input), kind="document")
        vecs = _gemini_embed(input, task_type="RETRIEVAL_DOCUMENT")
        missing = sum(1 for v in vecs if v is None)
        if missing:
            raise RuntimeError(f"Gemini embed 失敗 {missing}/{len(vecs)} 項，放棄寫入（避免零向量污染）")
        # np.ndarray rows：chromadb HttpClient 查詢路徑會對每個 embedding 呼叫 .tolist()，
        # plain list 沒此方法（切共用 chroma server 後 query 全炸）。對齊 ingest/vector_store._GeminiEF。
        return [np.asarray(v, dtype=np.float32) for v in vecs]
    def embed_query(self, input):
        if embed_backend() == "bge":
            from agent_core.embed_http_client import embed_texts
            return embed_texts(list(input), kind="query")
        vecs = _gemini_embed(input, task_type="RETRIEVAL_QUERY")
        missing = sum(1 for v in vecs if v is None)
        if missing:
            raise RuntimeError(f"Gemini embed 查詢失敗 {missing}/{len(vecs)} 項")
        return [np.asarray(v, dtype=np.float32) for v in vecs]
    def name(self):
        return BGE_MODEL_NAME if embed_backend() == "bge" else "gemini-embedding-001"


def _build_chroma_client():
    """Build a Chroma client (HttpClient when RED_CHROMA_HTTP_URL is set, else
    a local PersistentClient).

    Delegates to the shared agent_core.chroma_backend factory so memory and the
    RAG vector_store can never drift onto different backends — mixing an embedded
    PersistentClient with the chroma server on the same path corrupts the index
    and SIGSEGVs chromadb_rust_bindings (the rag_sync_daily crashes, 2026-06).
    """
    return build_chroma_client(_VECTOR_DB_DIR)


def _get_memory_collection():
    global _chroma_client, _chroma_collection, _vector_ready, \
        _vector_last_error, _vector_last_failure_ts
    if _vector_ready is False:
        if time.monotonic() - _vector_last_failure_ts < _VECTOR_RETRY_COOLDOWN_S:
            return None
        _vector_ready = None  # 冷卻到期，允許重新嘗試 init
    if _chroma_collection is not None:
        return _chroma_collection
    try:
        _chroma_client = _build_chroma_client()
        _chroma_collection = _chroma_client.get_or_create_collection(
            name=physical_collection_name(_VECTOR_COLLECTION),
            embedding_function=_GeminiEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )
        _vector_ready = True
        _vector_last_error = None
        return _chroma_collection
    except Exception as e:
        logger.warning("向量資料庫載入失敗（%s）", e)
        _vector_ready = False
        _vector_last_error = f"{type(e).__name__}: {e}"
        _vector_last_failure_ts = time.monotonic()
        return None


def vector_store_last_error():
    """最後一次向量庫 init 失敗的原因字串（成功或未試過則 None）。

    _get_memory_collection() 失敗時只 logger.warning 後吞掉例外回 None，
    呼叫端（health check）拿不到原因、只能瞎猜文案。把例外留在模組級，
    讓 health 能區分「chroma_backend 防護拒開（RED_CHROMA_HTTP_URL 沒帶）」
    「server 連不上」「其他」。_vector_ready=False 冷卻短路期間，原因
    維持最近一次失敗時的值；冷卻到期重試後刷新（成功則清空）。
    """
    return _vector_last_error


def _index_memory(text: str, source: str, metadata: dict = None, doc_id: str = None) -> str:
    if not text or not text.strip():
        return ""
    col = _get_memory_collection()
    if col is None:
        return ""
    meta = {k: str(v) for k, v in (metadata or {}).items() if v is not None}
    meta.setdefault("source", source)
    meta.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    if doc_id is None:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        doc_id = f"{source}-{digest}-{int(time.time())}"
    try:
        col.upsert(documents=[text[:8000]], metadatas=[meta], ids=[doc_id])
        return doc_id
    except Exception as e:
        logger.warning("向量索引失敗（%s）", e)
        return ""


def _migrate_memory_json_to_vector():
    """把舊的 memory.json 一次性匯入向量庫（只跑一次）。"""
    if not os.path.exists(MEMORY_FILE):
        return
    marker = os.path.join(_VECTOR_DB_DIR, ".migrated_v1")
    if os.path.exists(marker):
        return
    col = _get_memory_collection()
    if col is None:
        return
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            mem = json.load(f)
    except Exception:
        return
    count = 0
    for k, v in (mem or {}).items():
        text = f"{k}: {v}"
        if _index_memory(text, source="note", metadata={"legacy_key": k}, doc_id=f"legacy-{k}"):
            count += 1
    try:
        with open(marker, "w") as f:
            f.write(f"migrated {count} entries at {datetime.now().isoformat(timespec='seconds')}")
    except Exception as _e:
        logger.debug("migration marker 寫入失敗: %s", _e)
    if count:
        print(f"[向量記憶] ✅ 已遷移 {count} 筆 memory.json 記憶到向量庫")


def remember(text: str, tags: str = ""):
    """把一段資訊存進長期向量記憶（之後可用 recall 語意搜尋）。
    tags 用逗號分隔，如 '客戶:XX, 主題:報價'（客戶名填大王實際講的，不要腦補）。"""
    return memory_ops.remember(text, tags=tags, index_memory_fn=_index_memory)


def sync_memory_seed():
    """把 committed memory_seed.json 同步進執行期記憶 + 向量庫（帶 index fn）。

    在 agent._main() 啟動時呼叫。KV 合併由 chat_session.load_startup_memory 已
    先做過（cheap）；這裡多帶 _index_memory 進去把 seed 也索引進向量庫供 recall。
    """
    from agent_core import memory_seed
    return memory_seed.sync_memory_seed(index_memory_fn=_index_memory)


def _simple_tokenize(text: str) -> list:
    """混合語言 tokenize：英文字/數字/料號保持整體；中文逐字切。
    對料號（AF-1 Pro）、訂單號（PO-2026-04-18）精確匹配特別有效。

    R8 升級：對 Latin 字元先做 NFKD normalization 去重音，
    讓 "Blåkläder" → "blaklader" 能跟 parquet 裡的拉丁拼法對上。
    不影響 CJK（中文字不受影響）。
    """
    if not text:
        return []
    import unicodedata
    # NFKD 拆開重音字（"å" = "a" + combining diacritic），再移掉重音 combining marks
    # 只對非 CJK 字元做；CJK 字元保持原樣
    normalized = []
    for c in text:
        if "\u4e00" <= c <= "\u9fff":
            normalized.append(c)
        else:
            # NFKD 分解該字元，移掉 combining marks (category "Mn")
            decomposed = unicodedata.normalize("NFKD", c)
            normalized.append("".join(ch for ch in decomposed
                                       if unicodedata.category(ch) != "Mn"))
    text = "".join(normalized)

    tokens = []
    buf = []
    for c in text.lower():
        if "\u4e00" <= c <= "\u9fff":
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(c)
        elif c.isalnum() or c in "-_":
            buf.append(c)
        else:
            if buf:
                tokens.append("".join(buf))
                buf = []
    if buf:
        tokens.append("".join(buf))
    return [t for t in tokens if t]


# BM25 索引 cache + 並發鎖（V10 安全性修補）
# 為什麼要鎖：
#   - daemon_email_ingest 在背景增量加 doc → col.count() 漲 → invalidate cache
#   - 同時 telegram / REPL thread 在跑 recall_reranked → 讀 _bm25_cache
#   - 沒鎖時兩邊可能撞到「重建到一半」的 cache（bm25 物件已換但 ids 還是舊的）
#   - 後果：rerank 出來的結果跟 thread_id 對不上、引文錯
# 鎖的範圍：build / read / update 都包，build 期間其他 thread 等到結果出來再讀
#
# 記憶體紀律（2026-06-11 telegram daemon RSS 棘輪事故）：cache 只准放
# 緊湊結構 — CompactBM25（numpy postings）+ ids + interned sources/dates。
# **絕不**把全量 docs/metas 留在這裡：22K 筆語料時那是 ~250MB 常駐，而且
# email ingest 每小時 +1 doc 就觸發整座重建（新舊雙份並存），長壽命 daemon
# 的 RSS 一天內 246→696MB。命中文件的全文/metadata 由消費端（memory_ops /
# rerank）對 top-N 用 col.get(ids=...) 即時補抓。
_bm25_lock = threading.Lock()
_bm25_cache = {"count": -1, "bm25": None, "ids": None, "sources": None, "dates": None}


def _iter_collection_pages(col, *, include: list[str], where: dict | None = None, batch_size: int = 1000):
    """逐頁 yield Chroma collection 內容；呼叫端用完一頁即可丟，不累積。"""
    include = list(include or [])
    batch_size = max(1, int(batch_size or 1000))
    offset = 0
    while True:
        kwargs = {
            "include": include,
            "limit": batch_size,
            "offset": offset,
        }
        if where is not None:
            kwargs["where"] = where
        page = col.get(**kwargs)
        page_ids = page.get("ids") or []
        if not page_ids:
            break
        yield page
        got = len(page_ids)
        offset += got
        if got < batch_size:
            break


def _collection_get_all(col, *, include: list[str], where: dict | None = None, batch_size: int = 1000) -> dict:
    """Page through a Chroma collection until all requested fields are loaded.

    ⚠️ 全量累積在 RAM — 大 collection 請優先考慮 _iter_collection_pages
    串流（BM25 重建已改用；這裡留給 metadatas-only 等小負載）。
    """
    include = list(include or [])
    result = {"ids": []}
    for field in include:
        result[field] = []
    for page in _iter_collection_pages(col, include=include, where=where, batch_size=batch_size):
        result["ids"].extend(page.get("ids") or [])
        for field in include:
            result[field].extend(page.get(field) or [])
    return result


def _build_bm25_index():
    """逐頁從 chromadb 串流重建緊湊 BM25 索引；資料量沒變就回 cache。

    Thread-safe（V10）：用 _bm25_lock 守住 read-modify-write，避免兩個
    thread 同時撞到 partially-built cache（bm25 物件已換但 ids 還沒）。

    記憶體紀律：每頁 tokenize 完就丟棄原文 — 全量 docs/metas 從頭到尾
    不會同時在 RAM；cache 也只留 CompactBM25 + ids + interned sources/dates
    （見 _bm25_cache 註解）。回傳 dict 鍵：count / bm25 / ids / sources / dates。
    """
    import sys as _sys

    col = _get_memory_collection()
    if col is None:
        return None
    try:
        total = col.count()
    except Exception:
        return None

    # Fast path：cache 命中（讀也要鎖，免得 update 中途被讀到）
    # 回傳 snapshot 而非 live dict — 避免呼叫端持有 reference 後另一 thread
    # update 把 ids/bm25 換掉，造成 caller iteration 中途 desync。
    with _bm25_lock:
        if _bm25_cache["count"] == total and _bm25_cache["bm25"] is not None:
            return dict(_bm25_cache)

    # Slow path：重建。昂貴的 col.get + tokenize + 建索引在鎖外做，
    # 鎖只包最後 update — 不然單次 build（大語料數秒）會擋光所有 reader。
    from agent_core.bm25_compact import CompactBM25

    ids: list = []
    sources: list = []
    dates: list = []

    def _tokenized_pages():
        """逐頁拉 documents+metadatas → yield token list；頁面用完即丟。

        ids/sources/dates 順便在這裡累積，跟 yield 出去的文件順序嚴格對齊
        （CompactBM25 的 doc index = 累積順序）。
        """
        for page in _iter_collection_pages(col, include=["documents", "metadatas"]):
            page_ids = page.get("ids") or []
            page_docs = page.get("documents") or []
            page_metas = page.get("metadatas") or []
            for i, doc_id in enumerate(page_ids):
                meta = (page_metas[i] if i < len(page_metas) else None) or {}
                ids.append(doc_id)
                # intern：source/date 是低基數字串（note/email/... 與 ISO 日期），
                # 22K+ 列表只付指標成本
                sources.append(_sys.intern(str(meta.get("source") or "")))
                dates.append(_sys.intern(str(meta.get("date") or "")))
                yield _simple_tokenize(page_docs[i] if i < len(page_docs) else "")

    try:
        bm25 = CompactBM25.from_corpus_iter(_tokenized_pages())
    except ValueError:
        logger.warning("BM25: col.get 回空；BM25 將停用")
        return None
    except Exception as e:
        logger.warning("BM25 建索引失敗：%s", e)
        return None
    if len(ids) < total:
        logger.warning("BM25 只取到 %d/%d docs（索引不完整）", len(ids), total)

    with _bm25_lock:
        # double-check：等鎖期間別 thread 已經建好了就用它的（避免重複工）
        if _bm25_cache["count"] == total and _bm25_cache["bm25"] is not None:
            return dict(_bm25_cache)
        _bm25_cache.update(count=total, bm25=bm25, ids=ids, sources=sources, dates=dates)
        logger.info(
            "BM25 索引已建：%d docs（postings %.1fMB）", len(ids), bm25.nbytes() / 1024 / 1024
        )
        return dict(_bm25_cache)


def _fetch_docs_by_ids(col, doc_ids: list) -> dict:
    """補抓指定 ids 的全文+metadata，回 {id: (doc, meta)}。

    BM25 cache 不再保存全量 docs/metas（記憶體紀律，見 _bm25_cache 註解），
    命中後由這裡對 top-N 即時抓。ids 可能在 build 與 fetch 之間被刪 —
    缺的就不在回傳 dict 裡，呼叫端自行跳過。
    """
    if not doc_ids:
        return {}
    try:
        data = col.get(ids=list(doc_ids), include=["documents", "metadatas"])
    except Exception as e:
        logger.warning("BM25 命中文件補抓失敗：%s", e)
        return {}
    out = {}
    got_ids = data.get("ids") or []
    got_docs = data.get("documents") or []
    got_metas = data.get("metadatas") or []
    for i, doc_id in enumerate(got_ids):
        doc = got_docs[i] if i < len(got_docs) else ""
        meta = got_metas[i] if i < len(got_metas) else None
        out[doc_id] = (doc or "", meta or {})
    return out


def _fetch_all_memory_metadatas() -> list[dict]:
    col = _get_memory_collection()
    if col is None:
        return []
    try:
        data = _collection_get_all(col, include=["metadatas"])
        return data.get("metadatas") or []
    except Exception as exc:
        logger.warning("memory_stats 拉 metadatas 失敗：%s", exc)
        return []


def _recall_default_min_score() -> float:
    """Daemon-level default for `recall`'s min_score, env-configurable.

    Set RED_RECALL_DEFAULT_MIN_SCORE=0.5 in production to drop 🔴 hits
    (cosine < 0.5) from every recall call by default — the LLM no longer
    even sees weak co-occurrence snippets it might cite as evidence.
    Callers can still override per call by passing an explicit min_score.
    """
    import os
    try:
        return max(0.0, min(1.0, float(os.environ.get("RED_RECALL_DEFAULT_MIN_SCORE", "0"))))
    except (TypeError, ValueError):
        return 0.0


def recall(query: str, k: int = 5, source: str = "", mode: str = "hybrid",
           min_score: float | None = None):
    """搜尋長期記憶。
    - mode='hybrid'（預設）：**向量語意 + BM25 關鍵字** 用 RRF 融合，料號/訂單號/客戶名抓得更準
    - mode='vector'：純語意搜尋（舊行為，適合模糊查詢）
    - mode='bm25'：純關鍵字搜尋（完全依字面匹配）
    - min_score: 過濾掉相似度低於此值的命中（vector cosine 0-1；hybrid 看
      vector 子分數；bm25 看 normalized 分數）。傳 None（預設）改用環境變數
      RED_RECALL_DEFAULT_MIN_SCORE（沒設則 0.0 不過濾）。要嚴格只看高信心
      命中可顯式傳 0.5 或 0.75。
    source 可填 meeting / note / email 過濾；k 回傳筆數（1-20）。"""
    effective_min = _recall_default_min_score() if min_score is None else float(min_score)
    return memory_ops.recall(
        query,
        k=k,
        source=source,
        mode=mode,
        min_score=effective_min,
        get_memory_collection_fn=_get_memory_collection,
        build_bm25_index_fn=_build_bm25_index,
        simple_tokenize_fn=_simple_tokenize,
        fetch_docs_by_ids_fn=_fetch_docs_by_ids,
        logger_obj=logger,
    )


def forget_memory(memory_id: str):
    """刪除指定 id 的長期記憶（id 從 recall 結果取得）。"""
    return memory_ops.forget_memory(memory_id, get_memory_collection_fn=_get_memory_collection)


# ----------------------------------------------------------------------------
# 🧠 行為準則（高階反思學習）
# ----------------------------------------------------------------------------
_BEHAVIOR_DEFAULT_CONFIDENCE = "1.0"


def _normalize_scenario(scenario: str) -> str:
    """情境字串正規化比對鍵（去頭尾空白 + casefold + 內部空白收斂）。
    用來判斷「這條新規則是不是在講同一個情境」，決定要不要取代舊規則。
    只做精確比對（不做語意相似），語意層級的衝突偵測留給
    memory_governance_report 的人工複核，不在這裡自動判定。"""
    return " ".join((scenario or "").strip().casefold().split())


def _validate_visibility_scope(raw: str) -> tuple[str, str]:
    """驗證 visibility_scope，無效值一律退回最保守的 owner_only。
    回傳 (scope, warning)；warning 非空代表輸入被改寫，要回報給呼叫者。"""
    from agent_core.agents.permission_matrix import Agent
    s = (raw or "").strip().lower()
    if not s or s == "owner_only":
        return "owner_only", ""
    if s == "all":
        return "all", ""
    if s.startswith("department:"):
        color = s.split(":", 1)[1].strip()
        try:
            Agent(color)
            return f"department:{color}", ""
        except ValueError:
            return "owner_only", f"⚠️ 未知部門顏色「{color}」，已退回 owner_only。"
    return "owner_only", (
        f"⚠️ 未知 visibility_scope「{raw}」，已退回 owner_only"
        "（可用：owner_only / all / department:<color>）。"
    )


def _set_archived(meta: dict, reason: str, *, superseded_by: str = "") -> dict:
    """回傳一份標記 archived 的 metadata 拷貝（不動原 dict）。統一經這裡蓋
    archived/archived_reason/archived_at 三欄，讓 memory_governance_report
    看得到「什麼時候、為什麼」被停用。"""
    new_meta = dict(meta or {})
    new_meta["archived"] = "1"
    new_meta["archived_reason"] = reason
    new_meta["archived_at"] = datetime.now().isoformat(timespec="seconds")
    if superseded_by:
        new_meta["superseded_by"] = superseded_by
    return new_meta


def _find_active_same_scenario(col, scenario_norm: str) -> list[tuple[str, dict]]:
    """回傳同情境、未停用的 (id, metadata) 清單（supersede 的候選）。"""
    try:
        data = _collection_get_all(col, include=["metadatas"], where={"source": "behavior_policy"})
    except Exception as exc:
        logger.warning("supersede 查詢舊規則失敗（略過）：%s", exc)
        return []
    out: list[tuple[str, dict]] = []
    for _id, m in zip(data.get("ids") or [], data.get("metadatas") or []):
        m = m or {}
        if str(m.get("archived") or "0") == "1":
            continue
        if _normalize_scenario(m.get("scenario") or "") != scenario_norm:
            continue
        out.append((_id, m))
    return out


def _rebuild_repl_chat(context: str) -> bool:
    """寫入/停用規則後重建 REPL chat，讓改動立即生效。回傳有沒有真的重建。

    走 tool_registry.set_build_chat_fn 註冊的 hook（agent.py 啟動時註冊，
    與 reload_skills 同模式）：REPL 程序恆有、daemon / tool-RPC worker 恆為
    None 自然跳過。不能用 `from agent import ...` 冷 import —— agent.py 以
    `__main__` 執行，冷 import 會重跑整份 top-level（重模組圖拉進長壽命
    daemon，2026-06-11 RSS 教訓）；也不能用 `"agent" in sys.modules` 判斷
    （REPL 的模組名是 __main__，永遠測不到）。"""
    try:
        from agent_core import tool_registry
        build_fn = getattr(tool_registry, "_build_chat_fn", None)
        if build_fn is None:
            return False
        from agent_core.chat_session import chat_state
        chat_state["chat"] = build_fn()
        chat_state["turns"] = 0
        return True
    except (Exception, SystemExit) as e:
        # SystemExit 也要接：build_chat → _get_gemini_api_key() 在金鑰缺失
        # 時 sys.exit(1)（BaseException，穿透 except Exception）。best-effort
        # 重建絕不能把呼叫者整個程序帶下去——規則已寫入，重建失敗只是
        # 「這輪還沒生效」。
        logger.warning("%s 後 chat 重建失敗：%s", context, e)
        return False


def _write_behavior_rule(
    scenario: str,
    rule: str,
    visibility_scope: str,
    *,
    extra_metadata: dict | None = None,
    restrict_supersede_to: frozenset | None = None,
) -> dict:
    """behavior_policy 的共用寫入核心（learn_behavior / remember_correction_rule
    兩個入口共用）：sanitize → scope 驗證 → 同情境 supersede → 寫入 → REPL chat
    重建。回傳結構化結果，訊息文案由各入口自己組。

    restrict_supersede_to：非 None 時，同情境既有規則的 visibility_scope 只要
    有任何一條不在此集合內 → 整筆拒絕（不寫入、不封存）。這是 CONFIRM 級窄化
    通道（remember_correction_rule）的邊界：Telegram 一個 +確認 絕不能停用
    all / department 級的跨部門規則——那是 LOCKED（REPL）才能做的變更。

    ⚠️ Round 7 C7-1：rule 直接灌入 persona = self-replicating jailbreak 攻擊面。
    若 LLM 受 prompt-injection 操控、過 +確認 後呼叫寫入惡意 rule，每次 chat
    都會看到該 rule（直到 大王 手動 forget_behavior）。所以：
      1. scenario / rule 進來前先過 sanitize_untrusted_text，把 injection
         字樣換成 [REDACTED-INJECTION-ATTEMPT]（持久化下也是 redacted 版本）
      2. 若 rule 完全是 injection（只剩 redacted token），直接拒絕入庫
    """
    scenario = (scenario or "").strip()
    rule = (rule or "").strip()
    if not scenario or not rule:
        return {"ok": False, "error": "錯誤：scenario 和 rule 都不能空。"}
    # C7-1: sanitize before persistence
    try:
        from agent_core.prompt_injection import sanitize_untrusted_text
        sanitized_scenario = sanitize_untrusted_text(scenario)
        sanitized_rule = sanitize_untrusted_text(rule)
        # 若 sanitize 後 rule 主要只剩 redacted token → 拒絕
        token_marker = "[REDACTED-INJECTION-ATTEMPT]"
        if (sanitized_rule.count(token_marker) >= 1
                and len(sanitized_rule.replace(token_marker, "").strip()) < 4):
            return {"ok": False, "error": (
                "❌ 拒絕學習此 rule — 內容主要是 prompt-injection 嫌疑。\n"
                "   若這是合理的中文業務規則被誤判，請改成更具體的描述（例如\n"
                "   不要用「忽略前述指令」這類字眼）後再試。")}
        scenario = sanitized_scenario
        rule = sanitized_rule
    except Exception as exc:
        logger.warning("behavior rule sanitize 失敗（仍寫入但無 redact）：%s", exc)

    scope, scope_warning = _validate_visibility_scope(visibility_scope)
    content = f"【行為準則】處理「{scenario}」相關任務時必須：{rule}"
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    doc_id = f"behavior_policy-{digest}-{int(time.time())}"

    col = _get_memory_collection()
    matches = (
        _find_active_same_scenario(col, _normalize_scenario(scenario))
        if col is not None else []
    )
    if restrict_supersede_to is not None:
        blocked_scopes = sorted({
            str((m or {}).get("visibility_scope") or "owner_only")
            for _id, m in matches
            if str((m or {}).get("visibility_scope") or "owner_only")
            not in restrict_supersede_to
        })
        if blocked_scopes:
            return {"ok": False, "error": (
                f"❌ 「{scenario}」已有可見範圍為 {'、'.join(blocked_scopes)} 的既有規則，"
                f"這條通道不能取代/停用跨部門規則。\n"
                f"   請大王在 Mac REPL 用 learn_behavior 修改（LOCKED 級變更）。")}
    prior_count = 0
    for _id, m in matches:
        try:
            prior_count = max(prior_count, int(m.get("correction_count") or 0))
        except (TypeError, ValueError):
            pass

    # 先寫新規則、成功後才封存舊規則（Codex P1, PR #202）：反過來做的話，
    # archive 成功但 _index_memory 瞬時失敗（embed / upsert 掛掉）會讓這個
    # 情境「舊規則被停用、新規則不存在」——規則憑空消失。順序對調後最壞
    # 情況是新舊短暫並存（archive 失敗有 log，且同情境下次再寫會重試封存）。
    metadata = {
        "scenario": scenario,
        "rule": rule,
        "confidence": _BEHAVIOR_DEFAULT_CONFIDENCE,
        "visibility_scope": scope,
        "correction_count": str(prior_count + 1),
        "archived": "0",
    }
    for k, v in (extra_metadata or {}).items():
        metadata.setdefault(k, v)
    written_id = _index_memory(
        content, source="behavior_policy", metadata=metadata, doc_id=doc_id,
    )
    if not written_id:
        return {"ok": False, "error": "學習失敗：向量資料庫不可用。"}

    superseded = 0
    if matches:
        stale_ids = [i for i, _m in matches]
        stale_metas = [_set_archived(m, "superseded", superseded_by=doc_id) for _i, m in matches]
        try:
            col.update(ids=stale_ids, metadatas=stale_metas)
            superseded = len(stale_ids)
        except Exception as exc:
            logger.warning("標記舊規則 archived 失敗（新舊規則暫時並存）：%s", exc)
    # 立刻重建 REPL chat，讓新規則這一輪就生效（不用等下次 compact）。
    # daemon / tool-RPC worker 裡 hook 為 None 自然跳過——Telegram 端的生效
    # 時機本來就是 chat session rebuild（閒置/輪數上限/重啟）。
    rebuilt = _rebuild_repl_chat("behavior rule 寫入")
    return {
        "ok": True, "error": "", "doc_id": written_id, "superseded": superseded,
        "scope": scope, "scope_warning": scope_warning,
        "scenario": scenario, "rule": rule, "rebuilt": rebuilt,
    }


def _scope_note(scope: str) -> str:
    return {
        "owner_only": "只有大王看得到",
        "all": "全公司/所有部門 bot 都看得到",
    }.get(scope, f"「{scope.split(':', 1)[-1]}」部門 + 大王看得到")


def learn_behavior(scenario: str, rule: str, visibility_scope: str = "owner_only"):
    """【高階反思學習】把大王剛剛糾正你的「行為規則」內化下來，以後遇到相同情境自動遵守。
    scenario: 情境描述，例如「寫信給日本客戶」、「整理報價單」、「回覆客戶詢價」。
    rule:     必須遵守的規則，例如「一律用敬語」、「報價要附加運費估算」、「金額要複誦確認」。
    visibility_scope: 這條規則誰看得到——'owner_only'(預設，只有大王)/
        'all'(全部門 bot 都看得到)/'department:<color>'(該部門 + 大王看得到，
        color 是 red/orange/yellow/green/blue/indigo/purple/gray/black/white 之一)。
        新規則預設只有大王看得到，要跨部門生效要顯式指定。
    這條規則會被注入 persona，之後每次對話 Gemini 必看到；比放在 remember 更強制。
    同一個 scenario 再教一次會取代舊規則（不會累積出互相矛盾的重複規則）。
    （安全設計見 _write_behavior_rule docstring — C7-1。）
    """
    res = _write_behavior_rule(scenario, rule, visibility_scope)
    if not res["ok"]:
        return res["error"]
    supersede_note = (
        f"；取代了 {res['superseded']} 條同情境舊規則" if res["superseded"] else ""
    )
    effect = "已注入 persona，立即生效" if res.get("rebuilt") else "已寫入，下次 chat session 重建時生效"
    msg = (f"✅ 學到了！往後遇到「{res['scenario']}」，我會遵守：「{res['rule']}」。\n"
           f"   （{effect}；可見範圍：{_scope_note(res['scope'])}"
           f"{supersede_note}；id={res['doc_id']}）")
    if res["scope_warning"]:
        msg += f"\n   {res['scope_warning']}"
    return msg


_CORRECTION_RULE_MAX_SCENARIO = 80
_CORRECTION_RULE_MAX_RULE = 300


def remember_correction_rule(scenario: str, rule: str):
    """【糾正固化】大王糾正你之後，經他口頭同意，把「以後同類情境都適用」的
    行為規則永久記下來（之後每次對話都會自動遵守）。
    scenario: 情境（80 字內），例如「回覆 DECA 交期詢問」。
    rule:     規則（300 字內），例如「一律先查生管日報再報交期，不要用記憶猜」。

    什麼時候用：大王糾正你、且糾正內容是可重複套用的行為規則時，先用一句話
    向大王確認要記的內容，他同意後才呼叫。單次的事實修正（某封信、某個數字
    記錯）**不要**記成規則——那用 recent_factual_corrections 的歷史就夠了。
    只處理大王本人的糾正；員工的糾正不要固化成規則。

    跟 learn_behavior 的差異（這是刻意窄化的 Telegram 版）：
      - 規則永遠只有大王本人可見（owner_only），不能指定跨部門 scope
      - 只能取代同為 owner_only 的舊規則；情境撞到 all / department 級規則
        會整筆拒絕（跨部門規則的變更是 LOCKED，只准 REPL learn_behavior）
      - 有長度上限——規則要短到大王聽你轉述一次就能判斷要不要 +確認
      - 要跨部門共用的規則，請大王在 Mac REPL 用 learn_behavior 指定 scope
    """
    scenario = (scenario or "").strip()
    rule = (rule or "").strip()
    if len(scenario) > _CORRECTION_RULE_MAX_SCENARIO:
        return (f"❌ scenario 超過 {_CORRECTION_RULE_MAX_SCENARIO} 字上限"
                f"（目前 {len(scenario)} 字）。請濃縮情境描述後再試——"
                f"大王要能一眼看完你要記什麼。")
    if len(rule) > _CORRECTION_RULE_MAX_RULE:
        return (f"❌ rule 超過 {_CORRECTION_RULE_MAX_RULE} 字上限"
                f"（目前 {len(rule)} 字）。請把規則拆小或濃縮後再試——"
                f"大王要能一眼看完你要記什麼。")
    res = _write_behavior_rule(
        scenario, rule, "owner_only",
        extra_metadata={"learned_via": "correction_capture"},
        restrict_supersede_to=frozenset({"owner_only"}),
    )
    if not res["ok"]:
        return res["error"]
    supersede_note = (
        f"；取代了 {res['superseded']} 條同情境舊規則" if res["superseded"] else ""
    )
    return (f"✅ 已把這次糾正固化成行為準則：遇到「{res['scenario']}」→"
            f"「{res['rule']}」（只有大王看得到{supersede_note}；id={res['doc_id']}）。\n"
            f"   生效時機：本輪對話我會直接遵守；Telegram 的 persona 注入要等"
            f"下次 chat session 重建（閒置或輪數滿）才會帶上。\n"
            f"   查看/撤銷：memory_governance_report 隨時可看；要撤銷請大王在"
            f" Mac REPL 跑 resolve_conflict(id, 'archive') 或 forget_behavior(id)。")


_CONFIRMED_FACT_MAX_FACT = 300
_CONFIRMED_FACT_MAX_EVIDENCE = 200


def confirm_inferred_fact(fact: str, evidence: str = ""):
    """【主動確認式學習】你從文件/郵件/對話**推論**出一個不確定但重要的事實時，
    先向大王提出（例如「我從這封信推測 DECA 的付款條件改成 net 60 了，對嗎？」），
    他確認後呼叫這個工具把它固化成已確認事實——之後 recall 查得到、且標記為
    大王親自確認過（信任度高於一般檢索結果）。
    fact:     被確認的事實（300 字內），例如「DECA 付款條件自 2026-07 起改為 net 60」。
    evidence: 推論依據摘要（200 字內、選填），例如「2026-07-01 Owner 給 DECA 的
              報價回信」——之後質疑這條事實時能回頭查原文。

    什麼時候用：只在（1）你是**推論**而非文件明文直述、（2）這個事實會影響
    未來多次決策、（3）大王已明確口頭確認，三者都成立時。文件明寫的事實不用
    確認（直接引用原文）；一次性的小事實不值得固化。**大王否認或猶豫就不要呼叫。**

    跟 remember（LOCKED、只准 REPL）的差異：這是刻意窄化的 Telegram 版——
    有長度上限、內容過 sanitize、audit + budget 限流。可見性的實際防線：
    非 owner 的 Telegram session 整顆拿不到 recall / load_memory 等記憶讀取
    工具（telegram_actor_scope.OWNER_PRIVATE_READ_TOOLS，fail-closed）——
    metadata 的 visibility_scope 目前只是標記，recall 本身不做 scope 過濾。
    """
    fact = (fact or "").strip()
    evidence = (evidence or "").strip()
    if not fact:
        return "錯誤：fact 不能為空。"
    if len(fact) > _CONFIRMED_FACT_MAX_FACT:
        return (f"❌ fact 超過 {_CONFIRMED_FACT_MAX_FACT} 字上限（目前 {len(fact)} 字）。"
                f"請濃縮成單一事實——複合事實拆成多次確認。")
    if len(evidence) > _CONFIRMED_FACT_MAX_EVIDENCE:
        return (f"❌ evidence 超過 {_CONFIRMED_FACT_MAX_EVIDENCE} 字上限"
                f"（目前 {len(evidence)} 字）。摘要就好，原文用 doc_id/thread_id 指涉。")
    # C7-1 同款：寫進長期記憶的內容都要先淨化、injection 拒絕入庫
    try:
        from agent_core.prompt_injection import sanitize_untrusted_text
        sanitized_fact = sanitize_untrusted_text(fact)
        sanitized_evidence = sanitize_untrusted_text(evidence)
        token_marker = "[REDACTED-INJECTION-ATTEMPT]"
        if (sanitized_fact.count(token_marker) >= 1
                and len(sanitized_fact.replace(token_marker, "").strip()) < 4):
            return ("❌ 拒絕固化此事實 — 內容主要是 prompt-injection 嫌疑。"
                    "若是合理的業務事實被誤判，請改寫後再試。")
        fact = sanitized_fact
        evidence = sanitized_evidence
    except Exception as exc:
        logger.warning("confirm_inferred_fact sanitize 失敗（仍寫入但無 redact）：%s", exc)

    content = f"【已確認事實】{fact}"
    if evidence:
        content += f"（依據：{evidence}）"
    doc_id = _index_memory(
        content,
        source="confirmed_fact",
        metadata={
            "fact": fact,
            "evidence": evidence,
            "visibility_scope": "owner_only",
            "learned_via": "proactive_confirmation",
            "confidence": "1.0",
        },
    )
    if not doc_id:
        return "固化失敗：向量資料庫不可用。"
    return (f"✅ 已固化為已確認事實：「{fact}」（大王 {datetime.now().date().isoformat()} 確認；"
            f"id={doc_id}）。之後 recall 查得到；同主題若之後又變（例如付款條件再改），"
            f"新確認會以較新時間戳並存——引用時看日期取最新。撤銷用 forget_memory(id)。")


def list_behaviors():
    """列出所有已學習、仍生效（未停用）的行為準則。"""
    col = _get_memory_collection()
    if col is None:
        return "向量資料庫不可用。"
    try:
        data = _collection_get_all(col, include=["documents", "metadatas"],
                                    where={"source": "behavior_policy"})
    except Exception as e:
        return f"查詢失敗：{e}"
    docs = data.get("documents") or []
    metas = data.get("metadatas") or []
    ids = data.get("ids") or []
    active = [
        (i, d, m) for i, d, m in zip(ids, docs, metas)
        if str((m or {}).get("archived") or "0") != "1"
    ]
    archived_count = len(ids) - len(active)
    if not active:
        tail = f"（另有 {archived_count} 條已停用）" if archived_count else ""
        return f"目前還沒有已學習的行為準則{tail}。大王可以說「以後 XX 情境要 XX」來教我。"
    header = f"目前已學習 {len(active)} 條生效中的行為準則"
    if archived_count:
        header += f"（另有 {archived_count} 條已停用，用 memory_governance_report 查看）"
    lines = [header + "："]
    for i, (_id, d, m) in enumerate(active, 1):
        m = m or {}
        scenario = m.get("scenario", "?")
        rule = m.get("rule", "?")
        ts = (m.get("ts") or "")[:16]
        scope = m.get("visibility_scope", "owner_only")
        lines.append(f"{i}. [{ts}] 情境：{scenario}（範圍：{scope}）")
        lines.append(f"     → {rule}")
        lines.append(f"     id={_id}")
    return "\n".join(lines)


def forget_behavior(memory_id: str):
    """取消一條行為準則（id 從 list_behaviors 取得）。取消後下次 chat 重建就不會再注入。"""
    col = _get_memory_collection()
    if col is None:
        return "向量資料庫不可用。"
    try:
        col.delete(ids=[memory_id])
    except Exception as e:
        return f"刪除失敗：{e}"
    _rebuild_repl_chat("forget_behavior")
    return f"已刪除行為準則 {memory_id}，下輪對話起不再生效。"


_DEFAULT_BEHAVIOR_HALF_LIFE_DAYS = 90.0
_DEFAULT_BEHAVIOR_MAX_INJECTED = 40


def _behavior_visible_to(scope: str, caller_scope: str) -> bool:
    """caller_scope 看不看得到這條規則。'red'(大王/REPL) 一律看得到全部；
    其餘 caller_scope（10 色部門之一）只看得到 all 範圍、或明確標給該部門的
    department:<color> 範圍——owner_only 對非 red caller 預設不可見（新增可
    見度一律保守、白名單制，見 CLAUDE.md 跨部門權限鐵則）。"""
    scope = (scope or "owner_only").strip().lower()
    caller_scope = (caller_scope or "red").strip().lower()
    if scope == "all":
        return True
    if caller_scope == "red":
        return True
    if scope.startswith("department:"):
        return scope.split(":", 1)[1].strip() == caller_scope
    return False  # owner_only 或未知 scope 值，對非 red caller 一律不可見


def _compile_behavior_policies(caller_scope: str = "red") -> str:
    """拉取 caller_scope 看得到、未停用的 behavior_policy 條目，依「信心 ×
    新鮮度」排序取前 N 條，編成一段文字附加到 persona。每次 chat 重建都會呼叫
    （REPL 每次 _build_chat；Telegram 艦隊在 chat session rebuild 時——閒置
    逾時 / 輪數上限 / daemon 重啟——見 daemon_telegram.tg_build_chat）。

    caller_scope 預設 'red'：REPL 既有呼叫者（agent.py._build_chat）不用改
    呼叫方式、行為維持「看得到全部規則」，只是現在會依信心排序 + 截斷筆數上限
    （避免規則量無上限灌爆 system prompt token 預算）。"""
    col = _get_memory_collection()
    if col is None:
        return ""
    try:
        data = _collection_get_all(col, include=["documents", "metadatas"],
                                    where={"source": "behavior_policy"})
    except Exception as _e:
        logger.debug("讀取行為準則失敗：%s", _e)
        return ""
    docs = data.get("documents") or []
    metas = data.get("metadatas") or []
    if not docs:
        return ""

    from agent_core.env_utils import env_float, env_int
    from agent_core.ingest.recency import freshness_score

    half_life = env_float(
        "RED_BEHAVIOR_POLICY_HALF_LIFE_DAYS", _DEFAULT_BEHAVIOR_HALF_LIFE_DAYS, min_value=1.0
    )
    max_injected = env_int(
        "RED_BEHAVIOR_POLICY_MAX_INJECTED", _DEFAULT_BEHAVIOR_MAX_INJECTED,
        min_value=1, max_value=500,
    )

    scored: list[tuple[float, str]] = []
    for d, m in zip(docs, metas):
        m = m or {}
        if str(m.get("archived") or "0") == "1":
            continue
        if not _behavior_visible_to(m.get("visibility_scope") or "owner_only", caller_scope):
            continue
        try:
            confidence = float(m.get("confidence") or 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        fresh = freshness_score(m, "ts", half_life)
        scored.append((confidence * fresh, d))
    if not scored:
        return ""
    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[:max_injected]
    dropped = len(scored) - len(top)

    lines = ["\n\n【🧠 大王已教過你的行為準則（必須絕對遵守，違反會被大王糾正）】"]
    for i, (_score, d) in enumerate(top, 1):
        lines.append(f"  {i}. {d}")
    if dropped > 0:
        lines.append(f"  （還有 {dropped} 條較舊/較低信心的規則未列出，見 memory_governance_report）")
    return "\n".join(lines)


_BEHAVIOR_DECAY_STATE_KEY = "behavior_policy_decay_last_run_date"
_DEFAULT_BEHAVIOR_DECAY_FLOOR = 0.1


def _effective_behavior_confidence(meta: dict, half_life_days: float) -> float:
    from agent_core.ingest.recency import freshness_score
    try:
        confidence = float((meta or {}).get("confidence") or 1.0)
    except (TypeError, ValueError):
        confidence = 1.0
    return confidence * freshness_score(meta, "ts", half_life_days)


def run_behavior_policy_decay(load_state, update_state) -> str:
    """每天最多跑一次：effective_confidence(confidence × ts 新鮮度) 低於門檻
    的 behavior_policy 規則標記 archived（軟停用，非刪除；resolve_conflict
    可救回）。不開新 launchd plist —— 掛在既有每 30 分鐘跑一次的
    task_health_check 裡，靠 load_state/update_state 記日期自己節流成每日一次。

    Args:
        load_state / update_state: agent_core.daemon_helpers（跟其他 daemon
            task 共用同一份 state.json，用不同 key 互不干擾）。
    Returns:
        人類可讀的一行結果（今天已跑過或無事可做則回空字串）；給呼叫端
        print 到 daemon log，不強制通知大王。
    """
    today = datetime.now().date().isoformat()
    state = load_state()
    if state.get(_BEHAVIOR_DECAY_STATE_KEY) == today:
        return ""
    col = _get_memory_collection()
    if col is None:
        return ""
    try:
        data = _collection_get_all(col, include=["metadatas"], where={"source": "behavior_policy"})
    except Exception as exc:
        logger.warning("behavior_policy 衰減檢查失敗：%s", exc)
        return ""
    ids = data.get("ids") or []
    metas = data.get("metadatas") or []

    from agent_core.env_utils import env_float
    half_life = env_float(
        "RED_BEHAVIOR_POLICY_HALF_LIFE_DAYS", _DEFAULT_BEHAVIOR_HALF_LIFE_DAYS, min_value=1.0
    )
    floor = env_float(
        "RED_BEHAVIOR_POLICY_DECAY_FLOOR", _DEFAULT_BEHAVIOR_DECAY_FLOOR,
        min_value=0.0, max_value=1.0,
    )

    stale_ids: list = []
    stale_metas: list = []
    for _id, m in zip(ids, metas):
        m = m or {}
        if str(m.get("archived") or "0") == "1":
            continue
        if _effective_behavior_confidence(m, half_life) < floor:
            stale_ids.append(_id)
            stale_metas.append(_set_archived(m, "decay"))
    if stale_ids:
        try:
            col.update(ids=stale_ids, metadatas=stale_metas)
        except Exception as exc:
            logger.warning("behavior_policy 衰減標記失敗：%s", exc)
            stale_ids = []

    def _mark_run(s):
        s[_BEHAVIOR_DECAY_STATE_KEY] = today
    update_state(_mark_run)

    if stale_ids:
        return f"[behavior_policy 衰減] 今日停用 {len(stale_ids)} 條過期/低信心規則。"
    return ""


_DEFAULT_BEHAVIOR_REVIEW_FLOOR = 0.3


def memory_governance_report() -> str:
    """【記憶治理】列出目前 behavior_policy 規則的信心/年齡狀態，給大王複核：
      - 即將衰減的（effective confidence 低但還沒被 run_behavior_policy_decay
        跨過門檻）——與其被動等衰減，先看到就能手動 resolve_conflict 處理。
      - 最近被停用的（含原因：superseded/decay/manual）。
    不做語意層級的衝突偵測（例如「跟客戶A用中文」vs「跟客戶A用英文」這種矛盾）
    ——那需要 LLM 判斷或人工看，這裡只負責把資料攤開，判斷留給大王。同 scenario
    的精確重複已由 learn_behavior 的取代邏輯處理，不會出現在這份報告。"""
    col = _get_memory_collection()
    if col is None:
        return "向量資料庫不可用。"
    try:
        data = _collection_get_all(col, include=["metadatas"], where={"source": "behavior_policy"})
    except Exception as e:
        return f"查詢失敗：{e}"
    ids = data.get("ids") or []
    metas = data.get("metadatas") or []

    from agent_core.env_utils import env_float
    half_life = env_float(
        "RED_BEHAVIOR_POLICY_HALF_LIFE_DAYS", _DEFAULT_BEHAVIOR_HALF_LIFE_DAYS, min_value=1.0
    )
    decay_floor = env_float(
        "RED_BEHAVIOR_POLICY_DECAY_FLOOR", _DEFAULT_BEHAVIOR_DECAY_FLOOR,
        min_value=0.0, max_value=1.0,
    )
    review_floor = env_float(
        "RED_BEHAVIOR_POLICY_REVIEW_FLOOR",
        min(1.0, max(decay_floor * 3, _DEFAULT_BEHAVIOR_REVIEW_FLOOR)),
        min_value=0.0, max_value=1.0,
    )

    active_rows = []
    archived_rows = []
    for _id, m in zip(ids, metas):
        m = m or {}
        if str(m.get("archived") or "0") == "1":
            archived_rows.append((_id, m))
        else:
            active_rows.append((_id, m, _effective_behavior_confidence(m, half_life)))

    # 零規則不提前 return —— 「還沒學任何規則、卻反覆被糾正」正是下面
    # 糾正訊號段最該被看到的情境。
    lines = [f"🧭 行為準則治理報告（共 {len(active_rows)} 條生效中、{len(archived_rows)} 條已停用）"]

    review_soon = sorted((r for r in active_rows if r[2] < review_floor), key=lambda r: r[2])
    if review_soon:
        lines.append(f"\n⚠️ 即將衰減（effective confidence < {review_floor:.2f}，建議複核）：")
        for _id, m, eff in review_soon:
            lines.append(f"  - [{eff:.2f}] {m.get('scenario', '?')} → {m.get('rule', '?')}（id={_id}）")
    else:
        lines.append("\n✅ 沒有即將衰減的規則。")

    if archived_rows:
        archived_sorted = sorted(
            archived_rows, key=lambda r: r[1].get("archived_at") or "", reverse=True
        )[:20]
        lines.append(f"\n🗄️ 最近停用的規則（最多列 20 條，共 {len(archived_rows)} 條）：")
        for _id, m in archived_sorted:
            reason = m.get("archived_reason", "?")
            at = (m.get("archived_at") or "")[:16]
            lines.append(f"  - [{at}] {m.get('scenario', '?')}（原因：{reason}，id={_id}）")

    # 已確認事實（Phase 4）：confirm_inferred_fact 累計量——治理報告一站
    # 看得到所有學習類寫入。
    try:
        fact_data = _collection_get_all(col, include=["metadatas"],
                                        where={"source": "confirmed_fact"})
        fact_count = len(fact_data.get("ids") or [])
        if fact_count:
            lines.append(f"\n📌 已確認事實（大王親自確認的推論）：{fact_count} 條"
                         f"——recall(source='confirmed_fact') 可查，撤銷用 forget_memory(id)。")
    except Exception as exc:
        logger.debug("治理報告 confirmed_fact 計數略過：%s", exc)

    # 糾正訊號：把 mistake ledger 的 factual_correction 觀測量跟「已固化成
    # 規則」的量並排——反覆被糾正卻沒固化的主題，就是該用
    # remember_correction_rule 收斂的候選。
    try:
        from agent_core.mistake_ledger import recent_factual_correction_entries
        observed = recent_factual_correction_entries(days=14)
        promoted_total = sum(
            1 for _id, m, *_rest in active_rows
            if (m or {}).get("learned_via") == "correction_capture"
        ) + sum(
            1 for _id, m in archived_rows
            if (m or {}).get("learned_via") == "correction_capture"
        )
        lines.append(
            f"\n🔁 糾正訊號：近 14 天被大王糾正 {len(observed)} 次；"
            f"糾正固化成規則累計 {promoted_total} 條。"
        )
        if observed:
            for e in observed[:5]:
                lines.append(
                    f"  - [{(e.get('time') or '')[:16]}] "
                    f"{(e.get('user_said') or '')[:60]}"
                )
            lines.append(
                "  💡 若同類糾正反覆出現，考慮用 remember_correction_rule 固化成規則。"
            )
    except Exception as exc:
        logger.debug("治理報告糾正訊號段落略過：%s", exc)

    lines.append(
        "\n💡 想手動處理：resolve_conflict(id, 'archive'/'revive')；"
        "想徹底刪除用 forget_behavior(id)（不可逆）。"
    )
    return "\n".join(lines)


def resolve_conflict(memory_id: str, action: str = "archive"):
    """【記憶治理】手動處理一條 behavior_policy 規則——從 memory_governance_report
    的清單挑一條 id，決定要提前退休(archive)或救回(revive)。
    action: 'archive'（軟停用，之後可 revive 救回）或 'revive'（復活一條已停用的規則）。
    跟 forget_behavior 的差異：這裡是可逆的軟性停用/復活，forget_behavior 是
    直接從向量庫刪除（不可逆）。"""
    action = (action or "").strip().lower()
    if action not in ("archive", "revive"):
        return "錯誤：action 必須是 'archive' 或 'revive'。"
    col = _get_memory_collection()
    if col is None:
        return "向量資料庫不可用。"
    try:
        data = col.get(ids=[memory_id], include=["metadatas"])
    except Exception as e:
        return f"查詢失敗：{e}"
    ids = data.get("ids") or []
    metas = data.get("metadatas") or []
    if not ids:
        return f"找不到 id={memory_id} 的規則（或已被 forget_behavior 刪除）。"
    meta = metas[0] or {}
    if meta.get("source") != "behavior_policy":
        return f"id={memory_id} 不是 behavior_policy 規則，resolve_conflict 只處理行為準則。"

    if action == "archive":
        if str(meta.get("archived") or "0") == "1":
            return f"規則 id={memory_id} 本來就已停用，不用重複處理。"
        new_meta = _set_archived(meta, "manual")
    else:
        if str(meta.get("archived") or "0") != "1":
            return f"規則 id={memory_id} 本來就生效中，不用 revive。"
        new_meta = dict(meta)
        new_meta["archived"] = "0"
        new_meta["revived_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        col.update(ids=[memory_id], metadatas=[new_meta])
    except Exception as e:
        return f"更新失敗：{e}"

    _rebuild_repl_chat("resolve_conflict")

    scenario = meta.get("scenario", "?")
    verb = "停用" if action == "archive" else "恢復"
    return f"✅ 已{verb}規則（情境：{scenario}，id={memory_id}）。"


def memory_stats():
    """回報向量記憶庫的總筆數與各 source 分佈。"""
    return memory_ops.memory_stats(
        get_memory_collection_fn=_get_memory_collection,
        fetch_all_metadatas_fn=_fetch_all_memory_metadatas,
    )


# ---------- Key-value memory (legacy; save_memory also indexes vector) ----------

def save_memory(key: str, value: str):
    """存一條 key-value 事實（例如大王生日）。會同步寫向量庫讓 recall 也能語意搜尋到。

    用 locked_json 包住 read-modify-write，避免兩個 daemon 同時 save 把彼此
    的 update 蓋掉（lost update）— 以前 _atomic_write_text 只防得了寫到一半
    被讀，防不了「兩個 writer 各讀 v1、各寫 v2」這種競爭。
    """
    try:
        with locked_json(MEMORY_FILE, default={}) as mem:
            mem[key] = value
    except Exception as e:
        return f"記憶儲存失敗：{e}"
    _index_memory(f"{key}: {value}", source="note",
                  metadata={"legacy_key": key}, doc_id=f"legacy-{key}")
    return f"已記住 {key}"


def load_memory():
    """傾印整個 memory.json 的 key-value 事實（不走語意搜尋，要語意用 recall）。"""
    if not os.path.exists(MEMORY_FILE):
        return "目前沒有記憶。"
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "記憶檔案讀取異常。"
