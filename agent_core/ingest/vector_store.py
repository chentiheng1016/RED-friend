"""ChromaDB persistent vector store — shared across all ingest sources.

Collections:
  drive_docs  — Google Drive files (Docs / Sheets / PDFs)
  gmail_threads — Gmail thread summaries

Metadata schema (drive_docs):
  doc_id      str   Drive file ID
  title       str   file name
  mime_type   str   original MIME
  chunk_index int   0-based chunk position within document
  synced_at   str   ISO-8601 UTC timestamp
  folder_id   str   immediate Drive parent folder ID (optional)
  drive_id    str   Shared Drive root ID, when synced via sync_shared_drive (optional)
  content_hash str  extracted text hash used by Drive fast-skip gates
  sync_complete bool False while a Drive file upsert is still in progress

Usage:
  from agent_core.ingest.vector_store import get_store
  store = get_store("drive_docs")
  store.upsert("file-id-chunk-0", "契約內容…", {"title": "ABC合約", ...})
  hits = store.query("保固條款", n_results=5)
"""
from __future__ import annotations

import concurrent.futures
import os
import random
import sqlite3
import threading
import time
from typing import Any

import numpy as np
import chromadb

from agent_core.chroma_backend import build_chroma_client
from agent_core.embedding_config import (
    BGE_MODEL_NAME,
    embed_backend,
    embed_config_extra,
    maybe_normalize,
    physical_collection_name,
    sibling_collection_names,
)
from agent_core.env_utils import env_float as _env_float, env_int as _env_int
from agent_core.logging_and_paths import CHROMA_DB_DIR

_CHROMA_PATH = CHROMA_DB_DIR
# operation_sops：教學影片深析出的操作 SOP（獨立 collection，不被每日 sync_all_drives
# 的 drive_docs 對帳清除——drive_sync 只 reconcile drive_docs）。見 agent_core/operation_sops.py。
# xiaohong_reflections：反思層產物（agent_core/reflection.py），同樣刻意獨立、
# 不被夜跑 reconcile/purge 清除。
# skill_cards：影片知識卡搜尋索引（agent_core/skill_cards.py；ledger 在
# var/data/skill_cards/ 是真相、這裡只是索引），同樣獨立、不被夜跑清除。
_VALID_COLLECTIONS = {
    "drive_docs", "gmail_threads", "google_chat_messages",
    "operation_sops", "xiaohong_reflections", "skill_cards",
}
_EMBED_MODEL = "gemini-embedding-001"


def _metadata_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no"}
    return bool(value)


# Bound each embed RPC so a half-closed socket can't wedge the daemon for
# hours (production sync ran 7h with 0% CPU + TCP CLOSE_WAIT before being
# killed manually). The default is intentionally modest: normal calls are
# usually a few seconds, and retrying a fresh socket is better than waiting
# silently on a dead one.
_EMBED_TIMEOUT_S = _env_int("RAG_EMBED_TIMEOUT_S", 45, min_value=0)

# ── embed 的 HTTP 逾時必須「短於」上面那條 belt，否則 belt 每次放棄都在洩漏 ──
# 2026-08-03 卡死案：genai client 建立時的 http_options.timeout 是 600s（全域值，
# generation 的長輸出需要它），而 belt 只有 45s ⇒ 任何 >45s 的 embed，belt 先放棄、
# 那條 daemon thread 卻仍抓著 httpx 連線再跑最多 600s（Python 殺不掉 thread）。實測
# 現場 53 個 CLOSE_WAIT 對 3 個 ESTABLISHED，整輪 wedge 到 6h 看門狗才解脫。
#
# 解法是把順序擺正：**SDK 自己先死、belt 只當保險**。走 per-request http_options
# （`EmbedContentConfig.http_options`）只縮 embedding，不動全域 generation 逾時。
# 預設留 5s 餘裕給 SDK 拋錯 → belt 幾乎不再需要放生 thread，洩漏轉成乾淨例外。
_EMBED_HTTP_TIMEOUT_S = _env_int(
    "RAG_EMBED_HTTP_TIMEOUT_S", max(5, _EMBED_TIMEOUT_S - 5), min_value=1
)

# belt 放生 thread 的累積次數（per-process）。修正生效時應恆為 0——非 0 就代表
# SDK 逾時沒攔住，是下次 wedge 的第一手線索。只在 timeout 例外訊息裡露出。
_EMBED_LEAKED_THREADS = 0

# Wall-clock budget for a single Chroma HTTP op (upsert/query/get/delete/...).
# ChromaDB's HttpClient has NO per-request timeout. A collection whose HNSW
# segment is too large to stay in the server's cache cache-misses on EVERY
# upsert and reloads it from a 30GB+ DB ("Cache miss for collection ..." logged
# per call); on 2026-06-15 that thrash froze the whole nightly sync for ~55min
# on a single collection_upsert, main thread parked in socket.recv() with no
# timeout to break it. This bounds each HTTP op the way _SQL_FASTPATH_TIMEOUT_S
# bounds the direct-SQLite path: on overshoot we abandon the call (raise
# ChromaOpTimeout) and the caller's per-item error handling defers that chunk to
# the next run instead of hanging the sync. The default MUST stay above the
# embed retry budget: upsert(documents=) embeds via _ef → _gemini_embed INSIDE
# the op, whose worst case (a legitimate 429 storm — fast rejects, no
# per-attempt timeouts) stays well under 300s at the default 5 attempts via
# _backoff_sleep_s's 60s-capped, jittered exponential backoff, so a shorter
# cap would falsely abort a legitimate 429 retry storm.
# 600s clears that with margin yet still trips the ~55min cache-thrash hang at
# 10min. A healthy op is milliseconds–seconds, so only a real wedge trips it.
# 0 disables.
_CHROMA_OP_TIMEOUT_S = _env_int("RAG_CHROMA_OP_TIMEOUT_S", 600, min_value=0)

# Quota-burst retry budget — Gemini's per-minute rate cap (Vertex DSQ or the
# public API's shared pool alike) is the most likely real failure once the
# index grows past ~1k files. The first attempt pays no extra cost;
# subsequent attempts back off exponentially (capped, ±50% jitter so many
# concurrent callers hitting the same rate-limit window don't retry in
# lockstep) so we don't pile against the same bucket. 5 attempts was
# empirically sufficient in production — every observed give-up was a
# transient 429, never a real error (see launchd/templates/
# com.xiaohong.rag_sync_daily.plist).
_RETRY_MAX_ATTEMPTS = _env_int("RAG_EMBED_RETRY_MAX_ATTEMPTS", 5, min_value=0)
_RETRY_BASE_SLEEP_S = _env_int("RAG_EMBED_RETRY_BASE_SLEEP_S", 10, min_value=0)
_RETRY_MAX_SLEEP_S = _env_int("RAG_EMBED_RETRY_MAX_SLEEP_S", 60, min_value=1)

# content-hash 去重的 scope："folder"（預設，同 drive 同 folder 才合併——歷史行為）
# 或 "drive"（同 drive 即合併）。2026-07-31 普查：同 drive 跨 folder 的位元組相同
# 複本（QUI TRÌNH SX.pdf ×72 之類的複本文化）灌爆索引兩成多，且形成 HNSW 近重複
# 團塊（召回缺陷溫床）。folder 級檢索（search_drive_docs folder_id=）已加零命中
# 自動放寬退路，故 drive scope 不再犧牲可發現性。ACL 規則現況全為 drive 級
# （rag_access.json），同 drive 合併不影響權限過濾。
_DEDUP_SCOPE = os.environ.get("RAG_DRIVE_DEDUP_SCOPE", "folder").strip().lower()


def _backoff_sleep_s(attempt: int) -> float:
    """Exponential backoff capped at _RETRY_MAX_SLEEP_S, ±50% jitter."""
    capped = min(_RETRY_BASE_SLEEP_S * (2 ** attempt), _RETRY_MAX_SLEEP_S)
    return capped * random.uniform(0.5, 1.5)

# Cap chunks per ChromaDB upsert/delete/update — bundled SQLite limits
# bound parameters to SQLITE_MAX_VARIABLE_NUMBER=999. upsert/update bind
# ~11 vars per row (id + doc + 8 metadata cols + slack); delete binds 1
# per id but a doc can carry thousands of chunks (real example: a 4775-
# chunk text file), which would still overflow a single delete call.
# 32 leaves headroom across all surfaces and stays well under Gemini's
# 100/request embed cap so per-call cost is unchanged.
_CHROMA_OP_BATCH = 32

# Page size for get(where=...) calls that span an entire drive/folder.
# ChromaDB's get() result hydration runs WHERE id IN (...) over the matched
# row set, so an unpaginated query against a Shared Drive with 100k+ chunks
# overflows the SQL variable cap. Empirical probe on chromadb 1.5.8: 30k
# works, 50k fails. 10k leaves comfortable margin.
_PAGE_LIMIT = 10000
_METADATA_SEGMENT_CACHE_TTL_S = _env_float("RAG_METADATA_SEGMENT_CACHE_TTL_S", 1.0, min_value=0.0)

# Wall-clock budget for the direct-SQLite fast-path. chroma.sqlite3 can grow to
# tens of GB; a fast-path query that degrades into a full table scan has NO
# statement timeout (sqlite3's connect `timeout` only bounds lock waits), so a
# single query can block the whole sync for hours — a 34GB DB hung rag_sync for
# 23h on 2026-06-13. On overshoot we abort the query via a progress handler;
# every _sql_* caller already maps a sqlite error to "fall back to the HTTP
# path", so the abort degrades gracefully instead of hanging. 0 disables.
_SQL_FASTPATH_TIMEOUT_S = _env_float("RAG_SQL_FASTPATH_TIMEOUT_S", 30.0, min_value=0.0)
# VDBE opcodes between deadline checks. Small enough to abort a scan promptly
# (the handler fires between a scan's row reads), large enough to stay cheap.
_SQL_FASTPATH_PROGRESS_OPS = _env_int("RAG_SQL_FASTPATH_PROGRESS_OPS", 50_000, min_value=1)

# rag_gateway access-control fields stamped on every chunk. The fast-skip path
# (metadata_access_matches) compares these, so per-doc metadata readers MUST
# surface them — both the SQL fast-path and the HTTP col.get reducer.
_ACCESS_META_KEYS = (
    "rag_source", "owner_color", "department", "mailbox_email",
    "access_red", "access_orange", "access_yellow", "access_green",
    "access_blue", "access_indigo", "access_purple", "access_gray",
    "access_black", "access_white",
)

# --- Boolean metadata filter post-filtering (ACL fast path) -----------------
# ChromaDB indexes int_value/float_value/string_value in embedding_metadata but
# NOT bool_value (migration 00004-metadata-indices only ships those three; the
# bool column was bolted on later in 00002 with the comment "adding a boolean
# type column ... is over kill"). So a metadata filter on a *boolean* value —
# e.g. rag_gateway.access_where() emitting {"access_orange": {"$eq": True}} —
# compiles to `... WHERE key=? AND bool_value=?`, which the SQLite planner can
# only satisfy with a full SCAN of the shared embedding_metadata table. That
# table holds one row per (chunk, metadata-key) across EVERY collection (tens of
# millions of rows on the live 56GB chroma.sqlite3), so the scan runs for tens
# of seconds regardless of how tiny the target collection is — measured 54s on
# the 8k-row google_chat_messages_768 collection, 2026-07-24.
#
# Every access_where() caller feeds the filter into a *vector* query
# (VectorStore.query → col.query), i.e. a top-K search. For that shape we don't
# need the server to pre-filter at all: an unfiltered HNSW query returns its
# top-K in ~0.12s whether K is 5 or 200 (graph traversal dominates, result count
# is free), so we over-fetch a candidate pool WITHOUT the bool predicate — only
# pushing down the cheap string/int scope terms (drive_id/folder_id, which ARE
# indexed) — then evaluate the boolean ACL predicate in Python over the pool and
# truncate to the caller's n. This sidesteps the missing index entirely and, as
# a bonus, is immune to the bool-vs-int storage mismatch that made a server-side
# {"$eq": 1} return 0 hits (Python's 1 == True closes that gap).
#
# The pool cap trades recall for latency: a colour that can see only a tiny
# fraction of a collection near the query may get fewer than n hits. That fails
# SAFE (it can never surface unauthorised chunks — the predicate is applied to
# every returned row) and only under-returns authorised results ranked past the
# pool depth, which for a colour with sparse visibility in a collection are
# unlikely to be top-K anyway.
_ACL_POSTFILTER_OVERFETCH = _env_int("RAG_ACL_POSTFILTER_OVERFETCH", 25, min_value=1)
_ACL_POSTFILTER_MAX_POOL = _env_int("RAG_ACL_POSTFILTER_MAX_POOL", 300, min_value=1)
# Comparison operators we can evaluate in Python for the post-filter predicate.
_WHERE_COMPARE_OPS = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"}


def _where_term_is_bool(cond: Any) -> bool:
    """True if a single field term filters on a boolean value.

    Matches the shapes access_where() can emit — a bare bool, or an operator
    dict whose operand is a bool ({"$eq": True}) or a list of bools
    ({"$in": [True]}). Anything else (string/int scope terms) is False so it
    still gets pushed down to Chroma's indexed fast path.
    """
    if isinstance(cond, bool):
        return True
    if isinstance(cond, dict):
        for op, operand in cond.items():
            if op in ("$eq", "$ne") and isinstance(operand, bool):
                return True
            if op in ("$in", "$nin") and isinstance(operand, list) and operand and isinstance(operand[0], bool):
                return True
    return False


def _split_where_pushdown(where: dict[str, Any] | None) -> tuple[dict[str, Any] | None, bool]:
    """Split a where tree into (pushdown, has_bool_term).

    ``pushdown`` is the subset of predicates safe to send to Chroma such that it
    matches a SUPERSET of the full filter (so post-filtering the pool stays
    correct): boolean terms are dropped from AND contexts, and any $or that
    contains a boolean term is dropped wholesale (removing an OR branch would
    make the pushdown a subset, which could hide authorised rows). ``has_bool``
    reports whether any boolean term existed at all — when False the caller
    keeps the original (unchanged) fast path.
    """
    has_bool = False

    def walk(node: Any) -> Any:
        nonlocal has_bool
        if not isinstance(node, dict):
            return node
        and_parts: list[Any] = []
        for key, val in node.items():
            if key == "$and":
                for child in val:
                    child_pd = walk(child)
                    if child_pd is not None:
                        and_parts.append(child_pd)
            elif key == "$or":
                child_pds = [walk(child) for child in val]
                # An $or is only pushable if EVERY branch survives; a dropped
                # (boolean) branch would let the pushdown exclude real matches.
                if any(pd is None for pd in child_pds):
                    has_bool_or = any(_branch_has_bool(child) for child in val)
                    if has_bool_or:
                        has_bool = True
                    # else: a branch was None for another reason — keep nothing
                    # rather than risk an unsound partial OR.
                else:
                    and_parts.append({"$or": child_pds})
            else:
                if _where_term_is_bool(val):
                    has_bool = True
                else:
                    and_parts.append({key: val})
        if not and_parts:
            return None
        if len(and_parts) == 1:
            return and_parts[0]
        return {"$and": and_parts}

    return walk(where), has_bool


def _branch_has_bool(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    for key, val in node.items():
        if key in ("$and", "$or"):
            if any(_branch_has_bool(c) for c in val):
                return True
        elif _where_term_is_bool(val):
            return True
    return False


def _eval_where(node: Any, meta: dict[str, Any]) -> bool:
    """Evaluate a Chroma where tree against one chunk's metadata in Python.

    Covers the operators access_where()/base_where produce plus the standard
    comparison set. Python's ``1 == True`` means this matches regardless of
    whether the value was stored as a bool or an int, unlike the server-side
    filter. An absent field never satisfies an equality/inclusion test.
    """
    if not isinstance(node, dict):
        return True
    for key, val in node.items():
        if key == "$and":
            if not all(_eval_where(c, meta) for c in val):
                return False
        elif key == "$or":
            if not any(_eval_where(c, meta) for c in val):
                return False
        else:
            if not _eval_term(meta, key, val):
                return False
    return True


def _eval_term(meta: dict[str, Any], field: str, cond: Any) -> bool:
    missing = object()
    mv = meta.get(field, missing)
    if not isinstance(cond, dict):
        return mv is not missing and mv == cond
    for op, operand in cond.items():
        if op not in _WHERE_COMPARE_OPS:
            # Unknown operator: don't silently mis-filter — treat as satisfied
            # so the (already fetched) row survives rather than being dropped.
            continue
        if op == "$eq":
            if mv is missing or mv != operand:
                return False
        elif op == "$ne":
            if mv is not missing and mv == operand:
                return False
        elif op == "$in":
            if mv is missing or mv not in operand:
                return False
        elif op == "$nin":
            if mv is not missing and mv in operand:
                return False
        else:  # numeric comparisons
            if mv is missing or not isinstance(mv, (int, float)) or isinstance(mv, bool):
                return False
            if op == "$gt" and not mv > operand:
                return False
            if op == "$gte" and not mv >= operand:
                return False
            if op == "$lt" and not mv < operand:
                return False
            if op == "$lte" and not mv <= operand:
                return False
    return True


_client_lock = threading.Lock()
_client: Any = None  # PersistentClient | HttpClient, picked by chroma_backend
_hard_quota_lock = threading.Lock()
_hard_quota_message: str | None = None


class GeminiHardQuotaError(RuntimeError):
    """Embedding cannot proceed until Gemini billing/quota is changed."""


class ChromaOpTimeout(TimeoutError):
    """A single Chroma HTTP op exceeded RAG_CHROMA_OP_TIMEOUT_S — most likely a
    bloated collection thrashing the server's segment cache (cache miss + full
    segment reload on every call), or a wedged socket. Raised so the caller
    defers the batch and the sync keeps moving instead of freezing."""


def _is_stale_collection_error(exc: BaseException) -> bool:
    """Chroma collection handles can go stale if a rebuild swaps the
    collection while a long-running daemon still holds the old UUID."""
    msg = str(exc)
    return "Collection [" in msg and "does not exist" in msg


def _is_quota_error(exc: BaseException) -> bool:
    """Heuristic: classify as quota burst if the SDK / transport surfaced a
    429-flavoured signal. We don't import google.api_core.exceptions because
    the Gemini SDK can wrap the error in ClientError or pass it through grpc;
    string-matching is robust across all surfaces and won't break on SDK
    upgrades."""
    cls = type(exc).__name__.lower()
    msg = str(exc).lower()
    return (
        "resourceexhausted" in cls
        or "ratelimit" in cls
        or "429" in msg
        or "quota" in msg
        or "rate limit" in msg
    )


def _is_timeout_error(exc: BaseException) -> bool:
    """傳輸層逾時？——要和 belt 逾時走同一條 backoff 重試路徑。

    ⚠️ Python 3.11 起 `concurrent.futures.TimeoutError` 與 `socket.timeout`
    **都是內建 `TimeoutError` 的別名**，本來就被 `_gemini_embed` 的第一個
    except 接住並重試；真正的漏網之魚是 **httpx 的逾時**——`httpx.TimeoutException`
    不繼承 `TimeoutError`，會掉進第二個 except 被當「非配額錯誤」直接 raise。

    2026-08-04 教訓：PR #340 把 embed 的 HTTP 逾時縮到 40s（belt-5s）讓 SDK 自己
    先死、不再洩漏 thread，但也因此讓「慢一次」從**重試 5 次通常就過**變成
    **整批直接失敗**（舊 log 那些 `attempt 1/5; sleep 6.7s` 正是這條救生索在默默
    救場）。修正洩漏的同時得把重試補回來，兩個性質要並存。

    比照 `_is_quota_error` 走字串比對而非 import 具體例外類別：genai SDK 可能把
    httpx 例外包成自己的錯誤型別，且會隨版本改變；另外查 `__cause__`/`__context__`
    鏈，包一層的逾時同樣認得出來。"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, TimeoutError):     # 內建＝concurrent.futures/socket 別名
            return True
        cls = type(cur).__name__.lower()
        msg = str(cur).lower()
        if "timeout" in cls or "timedout" in cls:
            return True
        if "timed out" in msg or "timeout" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_hard_quota_error(exc: BaseException) -> bool:
    """Quota errors caused by billing/spend caps will not clear via backoff.

    委派給 gemini_client._is_non_retryable_quota_error（單一事實來源）。
    2026-07-23 教訓：這裡曾是獨立複本、只認 "spending cap"，prepayment
    credits 燒乾的 429 被當暫時性重試 5 次/批 — 夜跑被拖過 6h wall-clock
    上限、KeepAlive 無限重跑，連鎖壓垮 chroma。"""
    from agent_core.gemini_client import _is_non_retryable_quota_error
    return _is_non_retryable_quota_error(str(exc))


def _summarize_exception(exc: BaseException, limit: int = 240) -> str:
    msg = " ".join(str(exc).split())
    if len(msg) <= limit:
        return msg
    return msg[: max(0, limit - 3)] + "..."


def _latch_hard_quota(exc: BaseException) -> str:
    global _hard_quota_message
    msg = _summarize_exception(exc)
    with _hard_quota_lock:
        if _hard_quota_message is None:
            _hard_quota_message = msg
        return _hard_quota_message


def get_embedding_hard_quota_message() -> str | None:
    with _hard_quota_lock:
        return _hard_quota_message


def raise_if_embedding_hard_quota() -> None:
    msg = get_embedding_hard_quota_message()
    if msg:
        raise GeminiHardQuotaError(f"Gemini embed hard quota exhausted: {msg}")


def _clear_embedding_hard_quota_for_tests() -> None:
    global _hard_quota_message
    with _hard_quota_lock:
        _hard_quota_message = None


def _record_embed_api_error(exc: BaseException) -> None:
    """embedding **最終失敗**記一筆到 api_errors.jsonl。best-effort，永不 raise。

    為什麼要獨立的 service tag（`gemini_embed`）而不是沿用 `gemini`：
    `api_error_stats` 的分母在 2026-08-14（假警報普查 E / #408）已經把 embedding
    的成功呼叫剔掉了 —— 因為 embedding 不經 `_gemini_generate`、不可能貢獻那條
    生成路徑的分子。若現在把 embedding 失敗混進同一個分子，就變成「分子含
    embedding、分母不含」，是同一個母體錯配的鏡像版（改成高估）。
    分開記 → 兩條路徑各自有成對的分子分母，見 cost_tracker.embed_error_stats。

    語意跟生成路徑一致：**只在重試用盡／不可重試而真的放棄時記一筆**，retry 後
    成功的暫時性 429/503 不計入（否則夜跑的 backoff 會把錯誤率洗到 100%）。
    """
    try:
        from agent_core.cost_tracker import record_api_error
        from agent_core.gemini_client import _classify_api_error
        msg = f"{type(exc).__name__}: {exc}"
        detail = msg
        try:
            from agent_core.log_redact import has_secret, redact_log_line
            detail = redact_log_line(msg) if has_secret(msg) else msg
        except Exception:
            detail = ""      # redact 壞掉寧可不留 detail，也不要漏秘密
        record_api_error("gemini_embed", _classify_api_error(msg),
                         model=_EMBED_MODEL, detail=detail)
    except Exception:
        pass


def _record_embed_cost(texts: list[str], task_type: str, duration_ms: float) -> None:
    """embedding 記帳（cost_tracker.record_embed_call；token 本地估算）。

    caller 依 task_type 拆 document/query：夜跑 ingest（RETRIEVAL_DOCUMENT）與
    互動查詢（RETRIEVAL_QUERY）是不同預算，cost_by_tool 才分得開。best-effort，
    絕不拖累 embed 主流程。"""
    try:
        from agent_core.cost_tracker import record_embed_call
        kind = "query" if task_type == "RETRIEVAL_QUERY" else "document"
        record_embed_call(
            model=_EMBED_MODEL, texts=texts, duration_ms=duration_ms,
            caller=f"vector_store.embed_{kind}",
        )
    except Exception:
        pass


def _gemini_embed_raw(texts: list[str], task_type: str) -> list[list[float]]:
    """The naked SDK call — no timeout, no retry. Wrapped by _gemini_embed.

    走 _get_embed_client()：RED_EMBED_USE_VERTEX=1 時改用 Vertex AI（專屬配額、少
    503），預設仍公開 API。只影響 embedding，generation 不變。"""
    from agent_core.gemini_client import _get_embed_client, _get_genai_types
    t0 = time.time()
    result = _get_embed_client().models.embed_content(
        model=_EMBED_MODEL,
        contents=list(texts),
        config=_get_genai_types().EmbedContentConfig(
            task_type=task_type,
            # 只縮 embedding 的 HTTP 逾時（全域 client 那條 600s 是 generation 在用）。
            # 單位是毫秒，同 gemini_client._gemini_http_timeout_ms 的慣例。
            http_options={"timeout": _EMBED_HTTP_TIMEOUT_S * 1000},
            **embed_config_extra(),
        ),
    )
    # 記帳緊貼成功回應：retry 的每次成功呼叫都各計一次（帳單也是這樣收）。
    # 2026-08 稽核前這條路徑完全沒進 cost.jsonl，embedding 月燒 $8k–13k 隱形。
    _record_embed_cost(texts, task_type, (time.time() - t0) * 1000.0)
    vecs = [
        list(e.values) if (e is not None and getattr(e, "values", None) is not None) else None
        for e in result.embeddings
    ]
    missing = sum(1 for v in vecs if v is None)
    if missing:
        raise RuntimeError(f"Gemini embed 失敗 {missing}/{len(vecs)} 項，放棄（避免零向量污染）")
    # No-op at the full dim (vectors already normalized); L2-normalizes the
    # un-normalized native-768 output so truncated embeds match the backfill.
    return [maybe_normalize(v) for v in vecs]


def _gemini_embed_with_timeout(texts: list[str], task_type: str) -> list[list[float]]:
    """Run the embed call in a daemon thread with a hard wall-clock deadline.

    Python can't kill a thread, so a hung SDK call leaks one daemon thread
    until process exit — that's the price for not blocking forever. The next
    attempt builds a fresh thread; we never wait on the wedged one.
    """
    holder: dict[str, Any] = {}
    def target() -> None:
        try:
            holder["value"] = _gemini_embed_raw(texts, task_type)
        except BaseException as exc:
            holder["exc"] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=_EMBED_TIMEOUT_S)
    if t.is_alive():
        # 放生一條抓著 httpx 連線的 thread。正常情況下 SDK 的
        # _EMBED_HTTP_TIMEOUT_S 會先拋錯 → 這裡不該常走到；真走到就代表
        # 「SDK 逾時也沒攔住」，把累積洩漏數印出來，下次 wedge 才有得對帳
        # （2026-08-03 案：現場 53 CLOSE_WAIT，但當時無從得知洩漏了幾條）。
        global _EMBED_LEAKED_THREADS
        _EMBED_LEAKED_THREADS += 1
        raise concurrent.futures.TimeoutError(
            f"Gemini embed exceeded {_EMBED_TIMEOUT_S}s "
            f"(http timeout {_EMBED_HTTP_TIMEOUT_S}s 未先攔下) — "
            f"累積洩漏 thread {_EMBED_LEAKED_THREADS}、目前存活 thread "
            f"{threading.active_count()}；possible CLOSE_WAIT socket"
        )
    if "exc" in holder:
        raise holder["exc"]
    return holder["value"]


def _gemini_embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Quota-aware + timeout-bounded embedding wrapper. Retries on 429-style
    bursts and on socket timeouts; bubbles other exceptions immediately so
    real bugs aren't hidden by the retry layer."""
    if isinstance(texts, str):
        texts = [texts]
    raise_if_embedding_hard_quota()
    last_exc: BaseException | None = None
    attempts = max(1, _RETRY_MAX_ATTEMPTS)
    for attempt in range(attempts):
        is_final_attempt = attempt + 1 >= attempts
        try:
            return _gemini_embed_with_timeout(list(texts), task_type)
        except concurrent.futures.TimeoutError as exc:
            last_exc = exc
            if is_final_attempt:
                print(
                    f"[rag_sync] Gemini embed timeout "
                    f"attempt {attempt + 1}/{attempts}; giving up: {exc}",
                    flush=True,
                )
                break
            sleep_s = _backoff_sleep_s(attempt)
            print(
                f"[rag_sync] Gemini embed timeout "
                f"attempt {attempt + 1}/{attempts}; sleep {sleep_s:.1f}s: {exc}",
                flush=True,
            )
            time.sleep(sleep_s)
        except Exception as exc:
            if not _is_quota_error(exc):
                # ⚠️ 順序要緊：配額分類**必須先判**，逾時只當 fallback。訊息同時
                # 帶 "429 rate limit" 與 "timeout" 的錯誤兩邊都會命中，若先判逾時
                # 就會繞過下面的硬配額短路 → 預付燒乾的 429 被當暫時性錯誤重試
                # 5 次/批，重演 2026-07-23 拖過 6h 上限、連鎖壓垮 chroma 的事故。
                #
                # 傳輸層逾時（httpx 那三種不繼承 TimeoutError，見 _is_timeout_error）
                # 要和 belt 逾時同等對待：backoff 重試，別讓整批 embedding 失敗。
                if _is_timeout_error(exc):
                    last_exc = exc
                    if is_final_attempt:
                        print(
                            f"[rag_sync] Gemini embed 傳輸逾時 "
                            f"attempt {attempt + 1}/{attempts}; giving up: {exc}",
                            flush=True,
                        )
                        break
                    sleep_s = _backoff_sleep_s(attempt)
                    print(
                        f"[rag_sync] Gemini embed 傳輸逾時 "
                        f"attempt {attempt + 1}/{attempts}; sleep {sleep_s:.1f}s: {exc}",
                        flush=True,
                    )
                    time.sleep(sleep_s)
                    continue
                _record_embed_api_error(exc)   # 不可重試 → 這批真的失敗了
                raise
            last_exc = exc
            if _is_hard_quota_error(exc):
                msg = _latch_hard_quota(exc)
                print(
                    f"[rag_sync] Gemini embed hard quota; aborting retries: {msg}",
                    flush=True,
                )
                _record_embed_api_error(exc)
                raise GeminiHardQuotaError(
                    f"Gemini embed hard quota exhausted: {msg}"
                ) from exc
            if is_final_attempt:
                print(
                    f"[rag_sync] Gemini embed quota/backoff "
                    f"attempt {attempt + 1}/{attempts}; giving up: {exc}",
                    flush=True,
                )
                break
            sleep_s = _backoff_sleep_s(attempt)
            print(
                f"[rag_sync] Gemini embed quota/backoff "
                f"attempt {attempt + 1}/{attempts}; sleep {sleep_s:.1f}s: {exc}",
                flush=True,
            )
            time.sleep(sleep_s)
    assert last_exc is not None
    # 重試用盡（上面三種 break 都到這）→ 這批 embedding 真的失敗了，記一筆。
    _record_embed_api_error(last_exc)
    raise last_exc


class _GeminiEF:
    """Gemini multilingual embedding function for ChromaDB.
    Documents use RETRIEVAL_DOCUMENT; queries use RETRIEVAL_QUERY.

    Returns np.ndarray rows, not plain lists: chromadb's HttpClient query path
    (fastapi `_query` → `convert_np_embeddings_to_list`) calls `.tolist()` on
    each embedding, which a plain list lacks. The embedded PersistentClient
    tolerated lists, so this only surfaced once the fleet moved to the shared
    `chroma run` server over HTTP — every semantic_search then raised
    "'list' object has no attribute 'tolist'".
    """
    def __call__(self, input: list[str]) -> list[Any]:
        return [np.asarray(e, dtype=np.float32)
                for e in _gemini_embed(input, task_type="RETRIEVAL_DOCUMENT")]

    def embed_query(self, input: list[str]) -> list[Any]:
        return [np.asarray(e, dtype=np.float32)
                for e in _gemini_embed(input, task_type="RETRIEVAL_QUERY")]

    def name(self) -> str:
        return _EMBED_MODEL


class _BgeHttpEF:
    """bge backend：documents/queries 都走共用 embed server
    （agent_core/embed_http_client → com.xiaohong.embed_server daemon）。
    embed_texts 失敗一律 raise（客戶端合約：絕不墊零向量/None），
    回傳已是 np.ndarray float32 rows — 與 _GeminiEF 同一 chromadb 合約。"""

    def __call__(self, input: list[str]) -> list[Any]:
        from agent_core.embed_http_client import embed_texts
        return embed_texts(list(input), kind="document")

    def embed_query(self, input: list[str]) -> list[Any]:
        from agent_core.embed_http_client import embed_texts
        return embed_texts(list(input), kind="query")

    def name(self) -> str:
        return BGE_MODEL_NAME


class _BackendEF:
    """RED_EMBED_BACKEND 分派器 — call-time 解析（同 embedding_config 的
    live-read 哲學：長壽 daemon 不重載模組也能被測試 env 翻轉），gemini
    路徑 byte-for-byte 走原 _GeminiEF。"""

    def __init__(self) -> None:
        self._gemini = _GeminiEF()
        self._bge = _BgeHttpEF()

    def _active(self):
        return self._bge if embed_backend() == "bge" else self._gemini

    def __call__(self, input: list[str]) -> list[Any]:
        return self._active()(input)

    def embed_query(self, input: list[str]) -> list[Any]:
        return self._active().embed_query(input)

    def name(self) -> str:
        return self._active().name()


_ef = _BackendEF()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            # Honor RED_CHROMA_HTTP_URL → talk to the shared `chroma run` server
            # instead of opening _CHROMA_PATH embedded. Opening it embedded while
            # the server owns the same dir corrupts the HNSW index and SIGSEGVs
            # chromadb_rust_bindings (the rag_sync_daily crashes, 2026-06).
            _client = build_chroma_client(_CHROMA_PATH)
    return _client


def _run_chroma_op_with_timeout(op, col, label: str):
    """Run one Chroma op in a daemon thread with a hard wall-clock deadline.

    ChromaDB's HttpClient has no per-request timeout, so a server-side stall
    (a bloated collection reloading its HNSW segment on every upsert, or a
    half-closed socket) blocks the calling thread — and therefore the whole
    sync — indefinitely. Like _gemini_embed_with_timeout, an over-deadline call
    leaks one daemon thread (Python can't kill it) but the caller raises
    ChromaOpTimeout and moves on. Leaks stay bounded because the caller defers
    the batch on timeout rather than hammering the same wedged collection.
    """
    if _CHROMA_OP_TIMEOUT_S <= 0:
        return op(col)
    holder: dict[str, Any] = {}

    def target() -> None:
        try:
            holder["value"] = op(col)
        except BaseException as exc:  # re-raised on the caller thread below
            holder["exc"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=_CHROMA_OP_TIMEOUT_S)
    if t.is_alive():
        raise ChromaOpTimeout(
            f"Chroma op on collection {label!r} exceeded {_CHROMA_OP_TIMEOUT_S}s "
            "(bloated-segment cache thrash or wedged socket); deferring batch"
        )
    if "exc" in holder:
        raise holder["exc"]
    return holder["value"]


# 裸名誤連警告的 warn-once 記錄（per-process、per-logical-name）。
_BARE_NAME_SIBLING_WARNED: set[str] = set()


def _print_bare_name_warning(logical: str, sibling: str) -> None:
    print(
        f"[vector_store] 警告：開的是裸名 collection {logical!r}"
        f"（RED_EMBED_DIM 未設 → 3072 舊索引），但同庫存在 {sibling!r}——"
        f"production 資料在後者，這裡查什麼都會 absent。"
        f"手動/唯讀查詢請帶 RED_EMBED_DIM={sibling.rsplit('_', 1)[-1]}。",
        flush=True,
    )


def _warn_if_bare_name_shadows_suffixed(client, logical: str, physical: str) -> None:
    """手動/唯讀 session 忘帶 RED_EMBED_DIM 時，這裡 get_or_create 的是裸名
    3072 collection，而 production 資料全在 _<dim> 兄弟裡——查誰都 absent、
    不報錯（2026-07-15 差點因此把 3,431 顆已入庫檔誤判成未入庫）。開裸名且
    suffixed 兄弟存在時印一次警告。production 帶 RED_EMBED_DIM 走 suffixed
    名（physical != logical），這個檢查零觸發、零成本。"""
    if physical != logical or logical in _BARE_NAME_SIBLING_WARNED:
        return
    _BARE_NAME_SIBLING_WARNED.add(logical)
    for sibling in sibling_collection_names(logical):
        try:
            client.get_collection(sibling)
        except Exception:  # noqa: BLE001 — 兄弟不存在（或暫時查不到）→ 不警告
            continue
        _print_bare_name_warning(logical, sibling)
        return


def _warn_if_bare_name_shadows_suffixed_sqlite(conn, logical: str, physical: str) -> None:
    """同一個警告的 SQL fast-path 版：get_doc_metadata/bulk_get_doc_metadata/
    list_doc_ids/count 走 _metadata_segment() 直讀 chroma.sqlite3，整條路
    不經過 _open_collection——在主 checkout（sqlite 檔存在）忘帶 RED_EMBED_DIM
    會靜默讀裸名舊 collection 的 metadata segment、上面的 HTTP 版警告永遠
    不觸發。這裡用同一條 sqlite 連線直查兄弟 collection 是否存在。
    與 HTTP 版共用 warn-once 記錄。"""
    if physical != logical or logical in _BARE_NAME_SIBLING_WARNED:
        return
    _BARE_NAME_SIBLING_WARNED.add(logical)
    siblings = sibling_collection_names(logical)
    if not siblings:
        return
    placeholders = ",".join("?" for _ in siblings)
    try:
        row = conn.execute(
            f"select name from collections where name in ({placeholders}) limit 1",
            siblings,
        ).fetchone()
    except sqlite3.Error:  # 表不存在/schema 變動 → 警告是 best-effort，不擋查詢
        return
    if row:
        _print_bare_name_warning(logical, str(row[0]))


class VectorStore:
    def __init__(self, collection_name: str) -> None:
        if collection_name not in _VALID_COLLECTIONS:
            raise ValueError(f"Unknown collection: {collection_name!r}")
        self._collection_name = collection_name
        self._col = None
        self._metadata_segment_cache_id = ""
        self._metadata_segment_cache_until = 0.0

    def _open_collection(self):
        client = _get_client()
        physical = physical_collection_name(self._collection_name)
        _warn_if_bare_name_shadows_suffixed(client, self._collection_name, physical)
        return client.get_or_create_collection(
            name=physical,
            embedding_function=_ef,
            metadata={"hnsw:space": "cosine"},
        )

    def _refresh_collection(self) -> None:
        self._col = self._open_collection()

    def _with_collection(self, op):
        if self._col is None:
            self._refresh_collection()
        label = getattr(self, "_collection_name", "")  # label only — for the timeout message
        try:
            return _run_chroma_op_with_timeout(op, self._col, label)
        except Exception as exc:
            if not _is_stale_collection_error(exc):
                raise
            self._refresh_collection()
            return _run_chroma_op_with_timeout(op, self._col, label)

    def _sqlite_connect(self):
        db_path = os.path.join(_CHROMA_PATH, "chroma.sqlite3")
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=60)
        if _SQL_FASTPATH_TIMEOUT_S > 0:
            # Bound runaway queries (e.g. a full scan of a multi-GB chroma.sqlite3)
            # so they abort instead of blocking the sync indefinitely. The handler
            # runs between VDBE opcodes — including between a scan's page reads —
            # and a non-zero return aborts the query with OperationalError, which
            # every _sql_* caller treats as "fall back to the HTTP path". One
            # deadline covers all queries on this short-lived connection.
            deadline = time.monotonic() + _SQL_FASTPATH_TIMEOUT_S
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0,
                _SQL_FASTPATH_PROGRESS_OPS,
            )
        return conn

    def _load_metadata_segment_id(self) -> str:
        db_path = os.path.join(_CHROMA_PATH, "chroma.sqlite3")
        if not os.path.exists(db_path):
            return ""
        try:
            with self._sqlite_connect() as conn:
                _warn_if_bare_name_shadows_suffixed_sqlite(
                    conn,
                    self._collection_name,
                    physical_collection_name(self._collection_name),
                )
                row = conn.execute(
                    """
                    select s.id
                    from collections c
                    join segments s on s.collection = c.id
                    where c.name = ? and s.scope = 'METADATA'
                    limit 1
                    """,
                    # Must resolve the *physical* collection (e.g. drive_docs_768
                    # when RED_EMBED_DIM=768) — the same name _open_collection
                    # writes to. Using the bare logical name made the SQL
                    # fast-path read the stale pre-migration 3072 collection, so
                    # get_doc_metadata/list_doc_ids/count silently reported the
                    # wrong segment after the 768 cutover (fast-skip then skipped
                    # files absent from the live _768 index, and re-embedded
                    # _768-only files every night).
                    (physical_collection_name(self._collection_name),),
                ).fetchone()
        except sqlite3.Error:
            return ""
        return str(row[0]) if row else ""

    def _metadata_segment(self) -> str:
        """Return the current metadata segment id with a short TTL cache.

        External Chroma rebuilds can swap the metadata segment underneath a
        long-lived VectorStore, so this must not be cached forever. A short TTL
        keeps a single sync batch from opening thousands of one-row SQLite
        connections while still refreshing quickly after rebuilds.
        """
        if not getattr(self, "_collection_name", ""):
            return ""
        now = time.monotonic()
        if now < getattr(self, "_metadata_segment_cache_until", 0.0):
            return getattr(self, "_metadata_segment_cache_id", "")
        segment_id = self._load_metadata_segment_id()
        self._metadata_segment_cache_id = segment_id
        self._metadata_segment_cache_until = now + _METADATA_SEGMENT_CACHE_TTL_S
        return segment_id

    def upsert(self, chunk_id: str, text: str, metadata: dict[str, Any]) -> None:
        self._with_collection(lambda col: col.upsert(
            ids=[chunk_id],
            documents=[text],
            metadatas=[metadata],
        ))

    def upsert_batch(
        self,
        chunk_ids: list[str],
        texts: list[str],
        metadatas: list[dict[str, Any]],
        _batch_size: int = _CHROMA_OP_BATCH,
    ) -> None:
        for i in range(0, len(chunk_ids), _batch_size):
            self._with_collection(lambda col, i=i: col.upsert(
                ids=chunk_ids[i:i + _batch_size],
                documents=texts[i:i + _batch_size],
                metadatas=metadatas[i:i + _batch_size],
            ))
        # 反思層 intake：寫入成功後順手記「哪些 doc 剛進來」。record_batch
        # 自己會濾 collection（只追 3 個來源）、自己吞例外——這裡再包一層
        # 是熱路徑鐵則的雙保險（intake 掛掉不准影響 7-15h 的夜跑）。
        try:
            from agent_core.ingest.reflection_intake import record_batch
            record_batch(self._collection_name, chunk_ids, metadatas)
        except Exception:
            pass

    def get_by_ids(self, chunk_ids: list[str]) -> dict[str, Any]:
        """撈指定 chunk ids 的原文+metadata（反思層讀取用）。缺的 id 靜默跳過。"""
        if not chunk_ids:
            return {"ids": [], "documents": [], "metadatas": []}
        return self._with_collection(lambda col: col.get(
            ids=list(chunk_ids), include=["documents", "metadatas"],
        ))

    def query(
        self,
        text: str,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not text.strip():
            return []
        want = min(max(1, n_results), 20)
        # Boolean metadata predicates (the per-colour ACL filter) have no
        # server-side index and full-scan the shared metadata table for tens of
        # seconds. Detect them and, instead of paying that, over-fetch a
        # candidate pool with only the cheap (indexed) scope terms pushed down,
        # then apply the boolean predicate in Python. See the module-level note
        # above _ACL_POSTFILTER_OVERFETCH.
        pushdown, has_bool = _split_where_pushdown(where) if where else (None, False)
        if has_bool:
            fetch_n = min(_ACL_POSTFILTER_MAX_POOL, max(want, want * _ACL_POSTFILTER_OVERFETCH))
            effective_where = pushdown
        else:
            fetch_n = want
            effective_where = where
        kwargs: dict[str, Any] = dict(
            query_texts=[text],
            n_results=fetch_n,
            include=["documents", "metadatas", "distances"],
        )
        if effective_where:
            kwargs["where"] = effective_where
        results = self._with_collection(lambda col: col.query(**kwargs))
        hits = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            if has_bool and not _eval_where(where, meta or {}):
                continue
            hits.append({"text": doc, "metadata": meta, "distance": dist})
            if has_bool and len(hits) >= want:
                break
        return hits[:want]

    def _paged_metadatas(self, where: dict[str, Any] | None = None):
        """Yield metadatas in pages of _PAGE_LIMIT.

        Bounds the per-call result set so ChromaDB's hydration plan stays
        under SQLite's variable cap. Without this, get() over a high-cardinality
        predicate (e.g. a Shared Drive with 100k+ chunks) raises 'too many
        SQL variables'.
        """
        offset = 0
        while True:
            kwargs: dict[str, Any] = {
                "include": ["metadatas"],
                "limit": _PAGE_LIMIT,
                "offset": offset,
            }
            if where is not None:
                kwargs["where"] = where
            metas = self._with_collection(lambda col, kwargs=kwargs: col.get(**kwargs))["metadatas"]
            if not metas:
                return
            for m in metas:
                yield m
            if len(metas) < _PAGE_LIMIT:
                return
            offset += _PAGE_LIMIT

    @staticmethod
    def _metadata_value(row: sqlite3.Row | tuple[Any, ...]) -> Any:
        string_value = row[3]
        if string_value is not None:
            return string_value
        int_value = row[4]
        if int_value is not None:
            return int_value
        float_value = row[5]
        if float_value is not None:
            return float_value
        return row[6]

    def _log_paged_fallback(self, reason: str, filter_key: str = "", filter_value: str = "") -> None:
        """One-line breadcrumb when the SQL fast-path bails to the HTTP paged
        scan. The slow path is silent otherwise — a nightly that suddenly takes
        hours longer gives no clue that every doc_id listing degraded to
        _paged_metadatas until someone straces it."""
        scope = f"{filter_key}={filter_value}" if filter_key else "all docs"
        name = getattr(self, "_collection_name", "?")
        print(
            f"[rag_sync] {name}: doc_id SQL fast-path unavailable ({reason}); "
            f"falling back to slow HTTP paged scan ({scope})",
            flush=True,
        )

    def _sql_doc_ids(self, filter_key: str = "", filter_value: str = "") -> set[str] | None:
        segment_id = self._metadata_segment()
        if not segment_id:
            self._log_paged_fallback("no sqlite metadata segment", filter_key, filter_value)
            return None
        try:
            with self._sqlite_connect() as conn:
                if filter_key:
                    # Drive the (key, string_value) filter lookup off the
                    # embedding_metadata_string_value index FIRST. Without the
                    # hint SQLite's planner starts from the doc_id side and
                    # range-scans every doc_id row in the whole DB per call
                    # (~88s on the live 54GB chroma.sqlite3, measured 2026-07);
                    # filter-first is an exact index probe over just this
                    # drive/folder's rows (~1.8s). Same planner bug + same fix
                    # as _sql_find_duplicate_doc_id (2026-06-27, 83799307).
                    # CROSS JOIN (SQLite's documented join-order directive —
                    # identical semantics to JOIN) pins flt as the outer loop
                    # even without fresh sqlite_stat1, so a rebuilt/unanalyzed
                    # DB can't regress to the degenerate plan; e and doc then
                    # resolve via rowid / (id, key)-PK point lookups.
                    rows = conn.execute(
                        """
                        select distinct doc.string_value
                        from embedding_metadata flt indexed by embedding_metadata_string_value
                        cross join embeddings e
                          on e.id = flt.id
                        cross join embedding_metadata doc
                          on doc.id = e.id
                         and doc.key = 'doc_id'
                         and doc.string_value is not null
                        where flt.key = ?
                          and flt.string_value = ?
                          and e.segment_id = ?
                        """,
                        (filter_key, filter_value, segment_id),
                    )
                else:
                    rows = conn.execute(
                        """
                        select distinct doc.string_value
                        from embeddings e
                        join embedding_metadata doc
                          on doc.id = e.id
                         and doc.key = 'doc_id'
                         and doc.string_value is not null
                        where e.segment_id = ?
                        """,
                        (segment_id,),
                    )
                return {str(row[0]) for row in rows if row[0]}
        except sqlite3.Error as exc:
            self._log_paged_fallback(f"sqlite error: {exc}", filter_key, filter_value)
            return None

    def _sql_metadata_by_doc_ids(self, doc_ids: list[str]) -> dict[str, dict[str, Any]] | None:
        segment_id = self._metadata_segment()
        if not segment_id:
            return None
        wanted = (
            "synced_at", "folder_id", "drive_id", "content_hash", "title",
            "sync_complete", "history_id", "subject", "date", "modified_time",
            *_ACCESS_META_KEYS,
        )
        by_doc: dict[str, list[dict[str, Any]]] = {}
        try:
            with self._sqlite_connect() as conn:
                for i in range(0, len(doc_ids), 500):
                    batch = doc_ids[i:i + 500]
                    if not batch:
                        continue
                    doc_placeholders = ",".join("?" for _ in batch)
                    key_placeholders = ",".join("?" for _ in wanted)
                    sql = f"""
                        select d.string_value as doc_id,
                               e.id as row_id,
                               m.key,
                               m.string_value,
                               m.int_value,
                               m.float_value,
                               m.bool_value
                        from embeddings e
                        join embedding_metadata d
                          on d.id = e.id
                         and d.key = 'doc_id'
                         and d.string_value in ({doc_placeholders})
                        join embedding_metadata m
                          on m.id = e.id
                         and m.key in ({key_placeholders})
                        where e.segment_id = ?
                        order by d.string_value, e.id
                    """
                    rows = conn.execute(sql, (*batch, *wanted, segment_id))
                    by_row: dict[tuple[str, int], dict[str, Any]] = {}
                    for row in rows:
                        doc_id = str(row[0])
                        row_id = int(row[1])
                        key = str(row[2])
                        by_row.setdefault((doc_id, row_id), {"doc_id": doc_id})[key] = self._metadata_value(row)
                    for (doc_id, _row_id), meta in by_row.items():
                        by_doc.setdefault(doc_id, []).append(meta)
        except sqlite3.Error:
            return None
        return {did: self._reduce_doc_metadata(metas) for did, metas in by_doc.items()}

    def _sql_embedding_metadatas_by_doc_id(self, doc_id: str) -> list[tuple[str, dict[str, Any]]] | None:
        segment_id = self._metadata_segment()
        if not segment_id:
            return None
        try:
            with self._sqlite_connect() as conn:
                rows = conn.execute(
                    """
                    select d.string_value as doc_id,
                           e.id as row_id,
                           e.embedding_id,
                           m.key,
                           m.string_value,
                           m.int_value,
                           m.float_value,
                           m.bool_value
                    from embeddings e
                    join embedding_metadata d
                      on d.id = e.id
                     and d.key = 'doc_id'
                     and d.string_value = ?
                    join embedding_metadata m on m.id = e.id
                    where e.segment_id = ?
                    order by e.id
                    """,
                    (doc_id, segment_id),
                )
                by_row: dict[int, tuple[str, dict[str, Any]]] = {}
                for row in rows:
                    row_id = int(row[1])
                    embedding_id = str(row[2])
                    key = str(row[3])
                    # ChromaDB stores the document text under embedding_metadata
                    # row with key='chroma:document', but rejects that key in
                    # update(metadatas=...). Excluding it here keeps the dict
                    # safe for callers that round-trip metas back into update()
                    # (touch_synced_at, mark_doc_sync_complete).
                    if key == "chroma:document":
                        continue
                    if row_id not in by_row:
                        by_row[row_id] = (embedding_id, {"doc_id": doc_id})
                    by_row[row_id][1][key] = self._metadata_value((row[0], row[1], row[3], row[4], row[5], row[6], row[7]))
        except sqlite3.Error:
            return None
        return list(by_row.values())

    def _sql_embedding_ids_by_doc_ids(self, doc_ids: list[str]) -> list[str] | None:
        segment_id = self._metadata_segment()
        if not segment_id:
            return None
        ids: list[str] = []
        try:
            with self._sqlite_connect() as conn:
                for i in range(0, len(doc_ids), 500):
                    batch = doc_ids[i:i + 500]
                    if not batch:
                        continue
                    placeholders = ",".join("?" for _ in batch)
                    rows = conn.execute(
                        f"""
                        select e.embedding_id
                        from embeddings e
                        join embedding_metadata d
                          on d.id = e.id
                         and d.key = 'doc_id'
                         and d.string_value in ({placeholders})
                        where e.segment_id = ?
                        """,
                        (*batch, segment_id),
                    )
                    ids.extend(str(row[0]) for row in rows)
        except sqlite3.Error:
            return None
        return ids

    def _sql_find_duplicate_doc_id(
        self, content_hash: str, exclude_doc_id: str, drive_id: str, folder_id: str
    ) -> str | None | bool:
        """SQLite fast-path for find_duplicate_doc_id.

        Returns the canonical doc_id, None when no duplicate exists, or False
        to signal "no sqlite backend — caller must use the HTTP fallback".
        (None and False are distinct: None is an authoritative "none found".)
        """
        segment_id = self._metadata_segment()
        if not segment_id:
            return False
        try:
            with self._sqlite_connect() as conn:
                # Candidates: other docs whose chunks carry the same content_hash.
                # content_hash is indexed (embedding_metadata_string_value), and a
                # hash is shared by only a handful of files, so this stays cheap —
                # BUT only if SQLite drives off that index. Without the explicit
                # `INDEXED BY`, the planner picks the doc_id table first and range-
                # scans every doc_id row (`string_value <> ?`), turning this into a
                # full scan of the multi-million-row metadata table that blows the
                # _SQL_FASTPATH_TIMEOUT and silently falls back to the (also slow)
                # HTTP path — ~30s/file on the live drive_docs_768 collection.
                # Forcing the content_hash table first via the hint makes the exact
                # (key, string_value) lookup land in O(matches), not O(rows). Verified
                # 2026-06-27: 30s -> 0.00s, identical result set. Drive h first.
                candidate_rows = conn.execute(
                    """
                    select distinct doc.string_value
                    from embedding_metadata h indexed by embedding_metadata_string_value
                    join embeddings e
                      on e.id = h.id
                    join embedding_metadata doc
                      on doc.id = e.id
                     and doc.key = 'doc_id'
                     and doc.string_value is not null
                     and doc.string_value <> ?
                    where h.key = 'content_hash'
                      and h.string_value = ?
                      and e.segment_id = ?
                    """,
                    (exclude_doc_id, content_hash, segment_id),
                ).fetchall()
                candidates = sorted({str(r[0]) for r in candidate_rows if r[0]})
                if not candidates:
                    return None
                # Pull each candidate's scope (drive_id/folder_id) + sync_complete
                # so we can keep only same-scope, fully-synced copies. Empty
                # drive_id/folder_id are never stored as metadata rows, so an
                # absent row means "" — handled by the defaultdict below.
                placeholders = ",".join("?" for _ in candidates)
                rows = conn.execute(
                    f"""
                    select doc.string_value, m.key,
                           m.string_value, m.int_value, m.bool_value
                    from embeddings e
                    join embedding_metadata doc
                      on doc.id = e.id
                     and doc.key = 'doc_id'
                     and doc.string_value in ({placeholders})
                    join embedding_metadata m
                      on m.id = e.id
                     and m.key in ('drive_id', 'folder_id', 'sync_complete')
                    where e.segment_id = ?
                    """,
                    (*candidates, segment_id),
                ).fetchall()
        except sqlite3.Error:
            return False
        info: dict[str, dict[str, Any]] = {
            c: {"drive_id": "", "folder_id": "", "complete": True}
            for c in candidates
        }
        for doc_id, key, sval, ival, bval in rows:
            rec = info.get(str(doc_id))
            if rec is None:
                continue
            if key == "drive_id" and sval:
                rec["drive_id"] = str(sval)
            elif key == "folder_id" and sval:
                rec["folder_id"] = str(sval)
            elif key == "sync_complete":
                raw = bval if bval is not None else (ival if ival is not None else sval)
                if not _metadata_bool(raw):
                    rec["complete"] = False
        return self._pick_same_scope_canonical(info, candidates, drive_id, folder_id)

    @staticmethod
    def _pick_same_scope_canonical(
        info: dict[str, dict[str, Any]],
        candidates: list[str],
        drive_id: str,
        folder_id: str,
    ) -> str | None:
        """Smallest fully-synced candidate in the same dedup scope, or None.
        A total order on doc_id guarantees a group converges on one survivor
        instead of two files deferring to each other.

        Scope by RAG_DRIVE_DEDUP_SCOPE：預設 "folder"＝同 (drive_id, folder_id)
        才算重複（歷史行為，folder 級檢索完整）；"drive"＝同 drive 即算（清複本
        文化的索引灌水）。跨 drive 永不合併——drive 是權限過濾的 scope 單位。
        drive 模式需要非空 drive_id：plain folder 目標（Meet Recordings 等）的
        drive_id 為空，彼此不同源，退回 folder 等值比對以免誤併。"""
        want_drive = drive_id or ""
        want_folder = folder_id or ""
        drive_scope = _DEDUP_SCOPE == "drive" and bool(want_drive)
        matches = sorted(
            c for c in candidates
            if info[c]["complete"]
            and info[c]["drive_id"] == want_drive
            and (drive_scope or info[c]["folder_id"] == want_folder)
        )
        return matches[0] if matches else None

    def find_duplicate_doc_id(
        self, content_hash: str, exclude_doc_id: str,
        drive_id: str = "", folder_id: str = "",
    ) -> str | None:
        """Return a fully-synced doc_id that already indexes ``content_hash``
        under a *different* doc_id **in the same dedup scope**（見
        RAG_DRIVE_DEDUP_SCOPE／_pick_same_scope_canonical），or None if there
        is no such canonical copy.

        Drive copies the same file into many folders/Shared Drives, each with
        its own file_id (== doc_id). Re-embedding a byte-identical copy is
        pure waste, so the dedup gate skips it. 跨 drive 的複本永遠不合併
        （drive 是權限過濾的 scope 單位）；folder scope 模式下跨 folder 也不
        合併（歷史行為）。

        Only ``content_hash`` is matched server-side (it is indexed and cheap);
        scope + ``sync_complete`` are evaluated in Python because a compound
        where-filter on the HTTP backend is pathologically slow and legacy docs
        predate the fields.
        """
        if not content_hash:
            return None
        sql_result = self._sql_find_duplicate_doc_id(
            content_hash, exclude_doc_id, drive_id, folder_id
        )
        if sql_result is not False:
            return sql_result  # type: ignore[return-value]
        # HTTP / no-sqlite fallback.
        try:
            res = self._with_collection(lambda col: col.get(
                where={"content_hash": {"$eq": content_hash}},
                include=["metadatas"],
            ))
        except Exception:
            return None
        info: dict[str, dict[str, Any]] = {}
        for meta in res.get("metadatas") or []:
            doc_id = meta.get("doc_id")
            if not doc_id or doc_id == exclude_doc_id:
                continue
            rec = info.setdefault(
                doc_id, {"drive_id": "", "folder_id": "", "complete": True}
            )
            if meta.get("drive_id"):
                rec["drive_id"] = str(meta["drive_id"])
            if meta.get("folder_id"):
                rec["folder_id"] = str(meta["folder_id"])
            if not _metadata_bool(meta.get("sync_complete", True)):
                rec["complete"] = False
        return self._pick_same_scope_canonical(
            info, list(info.keys()), drive_id, folder_id
        )

    def list_doc_ids(self) -> set[str]:
        """Return the set of all distinct doc_id values currently indexed."""
        ids = self._sql_doc_ids()
        if ids is not None:
            return ids
        return {m["doc_id"] for m in self._paged_metadatas() if m.get("doc_id")}

    def list_doc_ids_by_folder(self, folder_id: str) -> set[str]:
        """Return doc_ids indexed under a specific Drive folder_id."""
        ids = self._sql_doc_ids("folder_id", folder_id)
        if ids is not None:
            return ids
        return {
            m["doc_id"]
            for m in self._paged_metadatas(where={"folder_id": {"$eq": folder_id}})
            if m.get("doc_id")
        }

    def list_generated_doc_ids(self) -> set[str]:
        """Return doc_ids flagged as RED's own generated output.

        Observability for the provenance filter (rag_coverage). Deliberately
        skips the _sql_doc_ids fast path: that query matches on
        embedding_metadata.string_value, but Chroma stores booleans in
        bool_value, so it would always come back empty.

        $eq (not $ne) is correct here — we want the rows that HAVE the flag
        set, and legacy chunks without the field rightly don't match.
        """
        from agent_core.provenance import METADATA_FIELD
        return {
            m["doc_id"]
            for m in self._paged_metadatas(where={METADATA_FIELD: {"$eq": True}})
            if m.get("doc_id")
        }

    def list_doc_ids_by_drive(self, drive_id: str) -> set[str]:
        """Return doc_ids indexed under a specific Shared Drive root."""
        ids = self._sql_doc_ids("drive_id", drive_id)
        if ids is not None:
            return ids
        return {
            m["doc_id"]
            for m in self._paged_metadatas(where={"drive_id": {"$eq": drive_id}})
            if m.get("doc_id")
        }

    @staticmethod
    def _empty_doc_metadata() -> dict[str, Any]:
        return {
            "synced_at": "", "folder_id": "", "drive_id": "",
            "content_hash": "", "title": "", "history_id": "",
            "subject": "", "date": "", "modified_time": "",
            "sync_complete": True,
        }

    @staticmethod
    def _reduce_doc_metadata(metas: list[dict[str, Any]]) -> dict[str, Any]:
        latest = ""
        for m in metas:
            s = m.get("synced_at", "")
            if s and s > latest:
                latest = s
        first = metas[0]
        sync_complete = all(
            _metadata_bool(m.get("sync_complete", True))
            for m in metas
        )
        reduced = {
            "synced_at":    latest,
            "folder_id":    first.get("folder_id", ""),
            "drive_id":     first.get("drive_id", ""),
            "content_hash": first.get("content_hash", ""),
            "title":        first.get("title", ""),
            "sync_complete": sync_complete,
            "history_id":   first.get("history_id", ""),
            "subject":      first.get("subject", ""),
            "date":         first.get("date", ""),
            "modified_time": first.get("modified_time", ""),
        }
        # Surface access-control fields so sync_*'s fast-skip can run
        # metadata_access_matches() over the HTTP (col.get) path. Without this
        # the reduced dict never carries owner_color/access_*, so the access
        # check always mismatches and EVERY file is re-fetched + re-embedded —
        # the SQL fast-path already returns these (see _sql_metadata_by_doc_ids
        # `wanted`), but HttpClient deployments fall through to this reducer.
        # Copy only keys actually present so a not-yet-tagged doc still fails
        # the match and self-heals on its next embed.
        for key in _ACCESS_META_KEYS:
            if key in first:
                reduced[key] = first.get(key)
        return reduced

    def bulk_get_doc_metadata(self, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Same shape as get_doc_metadata for each id, fetched in one paginated
        sweep. sync_*'s fast-skip path otherwise issues one SQL get per file —
        on a 4760-file Shared Drive that's 4760 round-trips before the first
        embed even runs. Bulk-fetching collapses it to ~ceil(chunks/_PAGE_LIMIT)
        SQL calls regardless of file count.

        Returns a dict keyed by doc_id; doc_ids that aren't in the index are
        absent (callers should treat absence as 'not yet synced').
        """
        if not doc_ids:
            return {}
        result = self._sql_metadata_by_doc_ids(doc_ids)
        if result is not None:
            return result
        by_doc: dict[str, list[dict[str, Any]]] = {}
        BATCH = 32
        for i in range(0, len(doc_ids), BATCH):
            batch = doc_ids[i:i + BATCH]
            for m in self._paged_metadatas(where={"doc_id": {"$in": batch}}):
                did = m.get("doc_id")
                if did:
                    by_doc.setdefault(did, []).append(m)
        return {did: self._reduce_doc_metadata(metas) for did, metas in by_doc.items()}

    def get_doc_metadata(self, doc_id: str) -> dict[str, Any]:
        """Return the per-doc metadata fields drive_sync/gmail_sync fast-skip
        paths need. Returns empty strings when the doc is not yet indexed."""
        if self._metadata_segment():
            result = self._sql_metadata_by_doc_ids([doc_id])
            if result is not None:
                return result.get(doc_id, self._empty_doc_metadata())
        result = self._with_collection(lambda col: col.get(
            where={"doc_id": {"$eq": doc_id}},
            include=["metadatas"],
        ))
        metas = result["metadatas"]
        if not metas:
            return self._empty_doc_metadata()
        return self._reduce_doc_metadata(metas)

    def set_doc_metadata_fields(self, doc_id: str, fields: dict[str, Any]) -> None:
        """Set metadata fields on every chunk of a doc WITHOUT re-embedding.

        update() with metadatas only does NOT recompute embeddings — this is
        the cheap path for metadata-only repairs and backfills (synced_at
        bumps, sync_complete flips, modified_time backfill on fast-skip).
        """
        if not fields:
            return
        sql_rows = self._sql_embedding_metadatas_by_doc_id(doc_id)
        if sql_rows is not None:
            ids = [eid for eid, _meta in sql_rows]
            new_metas = [{**m, **fields} for _eid, m in sql_rows]
        else:
            existing = self._with_collection(lambda col: col.get(where={"doc_id": {"$eq": doc_id}}))
            ids = existing["ids"]
            new_metas = [{**m, **fields} for m in existing["metadatas"]]
        if not ids:
            return
        for i in range(0, len(ids), _CHROMA_OP_BATCH):
            self._with_collection(lambda col, i=i: col.update(
                ids=ids[i:i + _CHROMA_OP_BATCH],
                metadatas=new_metas[i:i + _CHROMA_OP_BATCH],
            ))

    def touch_synced_at(self, doc_id: str, synced_at: str) -> None:
        """Bump synced_at on every chunk of a doc WITHOUT re-embedding."""
        self.set_doc_metadata_fields(doc_id, {"synced_at": synced_at})

    def mark_doc_sync_complete(self, doc_id: str) -> None:
        """Mark every chunk of a doc as fully written without re-embedding.

        drive_sync writes new chunks with sync_complete=False, then calls this
        only after upsert_batch and stale-tail deletion both succeed. If an
        embed/upsert/delete fails mid-file, the next run will refuse fast-skip
        and repair the partial document.
        """
        self.set_doc_metadata_fields(doc_id, {"sync_complete": True})

    def delete_by_doc_id(self, doc_id: str) -> None:
        sql_rows = self._sql_embedding_metadatas_by_doc_id(doc_id)
        if sql_rows is not None:
            ids = [eid for eid, _meta in sql_rows]
        else:
            ids = self._with_collection(lambda col: col.get(where={"doc_id": {"$eq": doc_id}}))["ids"]
        for i in range(0, len(ids), _CHROMA_OP_BATCH):
            self._with_collection(lambda col, i=i: col.delete(ids=ids[i:i + _CHROMA_OP_BATCH]))

    def bulk_delete_by_doc_ids(self, doc_ids: list[str]) -> None:
        """Delete chunks for many doc_ids in one ChromaDB pass per small batch."""
        ids = self._sql_embedding_ids_by_doc_ids(doc_ids)
        if ids is not None:
            for i in range(0, len(ids), _CHROMA_OP_BATCH):
                self._with_collection(lambda col, i=i: col.delete(ids=ids[i:i + _CHROMA_OP_BATCH]))
            return
        BATCH = 8
        for i in range(0, len(doc_ids), BATCH):
            batch = doc_ids[i:i + BATCH]
            self._with_collection(lambda col, batch=batch: col.delete(where={"doc_id": {"$in": batch}}))

    def delete_stale_chunks(self, doc_id: str, chunk_count: int) -> None:
        """Delete chunks for doc_id whose chunk_index >= chunk_count.

        Call this AFTER upsert_batch so data is never absent: new chunks are
        written first, then any extra tail chunks from a previous (longer)
        version are cleaned up.
        """
        sql_rows = self._sql_embedding_metadatas_by_doc_id(doc_id)
        if sql_rows is not None:
            stale_ids = [
                eid for eid, emeta in sql_rows
                if int(emeta.get("chunk_index") or 0) >= chunk_count
            ]
        else:
            existing = self._with_collection(lambda col: col.get(where={"doc_id": {"$eq": doc_id}}))
            stale_ids = [
                eid for eid, emeta in zip(existing["ids"], existing["metadatas"])
                if emeta.get("chunk_index", 0) >= chunk_count
            ]
        for i in range(0, len(stale_ids), _CHROMA_OP_BATCH):
            self._with_collection(lambda col, i=i: col.delete(ids=stale_ids[i:i + _CHROMA_OP_BATCH]))

    def count(self) -> int:
        segment_id = self._metadata_segment()
        if segment_id:
            try:
                with self._sqlite_connect() as conn:
                    row = conn.execute(
                        "select count(*) from embeddings where segment_id = ?",
                        (segment_id,),
                    ).fetchone()
                return int(row[0]) if row else 0
            except sqlite3.Error:
                pass
        return self._with_collection(lambda col: col.count())

    def is_empty(self) -> bool:
        """集合是否為空 —— cold-start gate 用的便宜判斷（別用 count()==0）。

        count() 在大段（drive_docs ~430 萬列）上是 covering-index 全掃、不短路
        （實測 ~90-420ms）；這裡用 EXISTS(... LIMIT 1) 一命中就停（~20µs），只為
        回一個布林。同一份 SQL fast-path，fallback 走 col.get(limit=1)。"""
        segment_id = self._metadata_segment()
        if segment_id:
            try:
                with self._sqlite_connect() as conn:
                    row = conn.execute(
                        "select exists(select 1 from embeddings where segment_id = ? limit 1)",
                        (segment_id,),
                    ).fetchone()
                return not (row and row[0])
            except sqlite3.Error:
                pass
        return not (self._with_collection(lambda col: col.get(limit=1)).get("ids") or [])

_stores: dict[str, VectorStore] = {}
_stores_lock = threading.Lock()


def get_store(collection_name: str) -> VectorStore:
    with _stores_lock:
        if collection_name not in _stores:
            _stores[collection_name] = VectorStore(collection_name)
    return _stores[collection_name]
