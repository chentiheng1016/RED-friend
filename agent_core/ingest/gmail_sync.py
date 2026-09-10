"""Gmail threads → ChromaDB ingest pipeline.

Each Gmail thread is stored as one or more chunks in the `gmail_threads`
collection. The text indexed is:  Subject + Snippet + body (first message).

Metadata schema:
  thread_id   str   Gmail thread ID
  subject     str   thread subject
  sender      str   From header of first message
  date        str   Date header of first message
  chunk_index int   0-based chunk position
  synced_at   str   ISO-8601 UTC
  sync_complete bool  False while a thread upsert is still in progress; flipped
                    True only after upsert + stale-tail cleanup both succeed
                    (missing field = legacy data = treated as complete)
  generated_by_red bool  True when EVERY message in the thread carries the
                    X-RED-Generated header — i.e. the thread is RED's own
                    generated output (a scheduled report it emailed out),
                    not primary company data. Indexed anyway so "what did we
                    send last week" stays answerable; rag_gateway filters it
                    out of semantic search by default. Missing field = legacy
                    data = treated as NOT generated.

Entry points:
  sync_query(gmail_query, max_threads)  — sync threads matching a Gmail search
  sync_thread(thread_id)               — sync / re-sync one thread
  sync_status()                        — count in collection
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any

from agent_core.env_utils import env_bool, env_int
from agent_core.google_auth import get_service, run_rpc_with_timeout
from agent_core.provenance import thread_is_red_generated
from agent_core.ingest.vector_store import get_store
from agent_core.ingest import contextualize
from agent_core.rag_gateway import metadata_access_fields, metadata_access_matches

CHUNK_SIZE = 600
CHUNK_OVERLAP = 80
_COLLECTION = "gmail_threads"

# Wall-clock ceiling per Gmail RPC. httplib2's socket timeout bounds each recv,
# but a dribbling response can stall one .execute() for many minutes at 0% CPU
# (rag_sync 2026-07-06 wedge). Mirrors drive_sync's RAG_DRIVE_RPC_TIMEOUT_S;
# gmail had no such backstop. 0 disables.
_GMAIL_RPC_TIMEOUT_S = env_int("RAG_GMAIL_RPC_TIMEOUT_S", 150, min_value=0)

# ── 回信引用鏈剝除（quote chain 治本）──────────────────────────────
# thread 文本 = 全部訊息串接，而每封回信又自帶前文引用 → thread 文本呈二次方
# 膨脹（實測有 16K chunks 的巨獸 thread），同 thread 近重複 chunk 團塊也是
# HNSW 召回缺陷的病灶。開了這個旗標，thread 的第 2 封起剝除「> 引用行」與
# 回覆歸屬標記（On…wrote:/寄件者:表頭/在…寫道:/Original Message）之後的整段。
# 首封永不剝；轉寄標記刻意不當剝除點（轉寄內容可能是本 thread 唯一正本）。
# 只影響之後有新活動的 thread（重抽時自然變瘦），歷史索引不動、零額外 embed 費。
_STRIP_QUOTES = env_bool("RAG_GMAIL_STRIP_QUOTES", False)

# 回覆歸屬標記：整行命中即從該行剝到訊息尾。涵蓋 Gmail/Outlook 的中英越常見樣式。
_QUOTE_ATTRIBUTION_RES = (
    re.compile(r"^On .{4,80} wrote:\s*$"),                      # Gmail EN
    re.compile(r"^[在於于] ?.{4,60}[寫写]道[：:]\s*$"),           # Gmail zh
    re.compile(r"^Vào .{4,80} đã viết:\s*$"),                    # Gmail vi
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.I),  # Outlook EN
    re.compile(r"^-{2,}\s*原始[郵邮]件\s*-{2,}\s*$"),             # Outlook zh
    re.compile(r"^_{10,}\s*$"),                                  # Outlook 分隔線
    re.compile(r"^(From|寄件者|寄件人|發件人|发件人|Từ)\s*[:：].{0,120}$"),  # 頂部引用表頭塊首行
)
# 轉寄標記：命中就「整封保留、停止剝除」——轉寄正文可能只存在這一封。
_FORWARD_MARKER_RE = re.compile(
    r"-{2,}\s*(Forwarded message|轉寄郵件|转发邮件)\s*-{2,}", re.I)


def _strip_reply_quotes(body: str) -> str:
    """剝除回信的引用鏈：「>」引用行＋歸屬標記後整段。轉寄命中即原文保留。"""
    if _FORWARD_MARKER_RE.search(body):
        return body
    kept: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if any(rx.match(s) for rx in _QUOTE_ATTRIBUTION_RES):
            break  # 歸屬標記之後全是前文引用
        if s.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _execute(request, label: str):
    """Execute a Gmail API request under a wall-clock ceiling (see
    RAG_GMAIL_RPC_TIMEOUT_S) so a slow/dribbling response that httplib2's
    per-recv socket timeout can't catch can't wedge the whole sync."""
    return run_rpc_with_timeout(_GMAIL_RPC_TIMEOUT_S, label, request.execute)


# ── text helpers ─────────────────────────────────────────────────────

def _decode_part(part: dict, service=None, message_id: str = "") -> str:
    body = part.get("body", {})
    data = body.get("data", "")
    if not data:
        attachment_id = body.get("attachmentId", "")
        if attachment_id and service and message_id:
            try:
                att = _execute(
                    service.users().messages().attachments().get(
                        userId="me", messageId=message_id, id=attachment_id,
                    ),
                    "gmail.attachments.get",
                )
                data = att.get("data", "")
            except Exception:
                return ""
        if not data:
            return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_text(payload: dict, service=None, message_id: str = "") -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload, service, message_id)
    if mime == "text/html":
        raw = _decode_part(payload, service, message_id)
        return re.sub(r"<[^>]+>", " ", raw)
    if mime.startswith("multipart/"):
        parts = payload.get("parts", [])
        # prefer text/plain over text/html
        for part in parts:
            if part.get("mimeType") == "text/plain":
                return _decode_part(part, service, message_id)
        for part in parts:
            if part.get("mimeType", "").startswith("multipart/"):
                return _extract_text(part, service, message_id)
        for part in parts:
            if part.get("mimeType") == "text/html":
                raw = _decode_part(part, service, message_id)
                return re.sub(r"<[^>]+>", " ", raw)
    return ""


def _chunk_text(text: str, subject: str = "", context: str = "") -> list[str]:
    """Split text into ~CHUNK_SIZE windows, each prefixed with thread-level
    context so a chunk reading "請寄樣品到 Saigon" keeps its connection to
    "Subject: ABC PO 樣品確認" at the embedding level (mirrors drive_sync).

    `context` given (Contextual Retrieval, flag on) → prefix is an LLM-written
    summary + blank line, payload stays CHUNK_SIZE (bolted on, not charged).
    else (flag off, today's behaviour verbatim) → prefix is `[subject] `, and
    the window shrinks by len(prefix) to stay under the embedding budget.
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []
    if context:
        prefix = f"{context}\n\n"
        payload_size = CHUNK_SIZE
    else:
        safe_subject = (subject or "")[:80]
        prefix = f"[{safe_subject}] " if safe_subject else ""
        payload_size = max(1, CHUNK_SIZE - len(prefix))

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + payload_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(prefix + chunk)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


# ── Gmail helpers ────────────────────────────────────────────────────

def _get_headers(message: dict) -> dict[str, str]:
    return {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }


def _thread_is_red_generated(messages: list[dict]) -> bool:
    """整串每封都帶 X-RED-Generated 才算純小紅產出（判定規則見 provenance）。"""
    return thread_is_red_generated(messages)


def _thread_text(service, thread_id: str) -> tuple[str, dict[str, str]]:
    """Return (full_text, first_msg_meta) for a thread."""
    thread = _execute(
        service.users().threads().get(
            userId="me",
            id=thread_id,
            format="full",
        ),
        "gmail.threads.get",
    )
    history_id = str(thread.get("historyId", ""))
    messages = thread.get("messages", [])
    if not messages:
        return "", {}

    first = messages[0]
    hdrs = _get_headers(first)
    subject = hdrs.get("Subject", "（無主旨）")
    sender = hdrs.get("From", "")
    date = hdrs.get("Date", "")
    snippet = first.get("snippet", "")

    # collect body text from all messages in thread
    bodies: list[str] = []
    for idx, msg in enumerate(messages):
        msg_id = msg.get("id", "")
        body = _extract_text(msg.get("payload", {}), service, msg_id).strip()
        # 第 2 封起剝引用鏈（首封含轉寄正文，永不剝）——見 _STRIP_QUOTES 註解。
        if body and _STRIP_QUOTES and idx > 0:
            body = _strip_reply_quotes(body)
        if body:
            bodies.append(body)

    full_text = f"主旨：{subject}\n寄件者：{sender}\n日期：{date}\n{snippet}\n\n" + "\n\n---\n\n".join(bodies)
    return full_text, {
        "subject": subject,
        "sender": sender,
        "date": date,
        "history_id": history_id,
        "generated_by_red": _thread_is_red_generated(messages),
    }


def _prefetch_doc_metadata(store, thread_ids: list[str]) -> tuple[dict[str, dict[str, str]], bool]:
    fn = getattr(store, "bulk_get_doc_metadata", None)
    if not callable(fn):
        return {}, False
    try:
        result = fn(thread_ids)
    except Exception as exc:
        print(f"[rag_sync] Gmail metadata prefetch 失敗，改走逐封同步: {exc}", flush=True)
        return {}, False
    if not isinstance(result, dict):
        return {}, False
    return result, True


def _sync_complete_allows_skip(existing: dict[str, Any]) -> bool:
    """殘缺 thread 不准 fast-skip（照抄 drive_sync 同名 helper 語意）。

    upsert_batch 內部 32 筆一子批，第 k 子批失敗時前 k-1 批已落地——跨越
    失敗邊界的 thread 前段 chunk 已帶新 history_id、後段沒寫入。skip 閘只看
    metas[0]（通常正是已寫入新 history_id 的 chunk 0）會誤判 unchanged，殘缺
    thread 直到該串收到新信才自癒。sync_complete=False 標記「寫到一半」，
    強制下一輪重抽。

    ⚠️ 欄位缺省必須視為 complete=True：舊資料沒有這個欄位，預設 False 會讓
    升級後第一輪把整個信箱（15 萬+ threads）全量重抓重 embed。只有新碼寫入
    的 False 才算殘缺。
    """
    value = existing.get("sync_complete", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no"}
    return bool(value)


def _mailbox_email(service) -> str:
    try:
        profile = _execute(
            service.users().getProfile(userId="me"), "gmail.getProfile"
        ) or {}
        return str(profile.get("emailAddress") or "").strip().lower()
    except Exception:
        return ""


# ── public API ───────────────────────────────────────────────────────

# Buffer this many chunks across threads before flushing one embed batch.
# Each ChromaDB upsert embeds up to _CHROMA_OP_BATCH(32) texts in ONE Gemini
# request, so syncing threads one-by-one (1-3 chunks each) spends ~1 request
# per 2 chunks — saturating the embedding RPM cap while TPM sits ~70% idle.
# Buffering whole threads to a multiple of 32 keeps each embed request full →
# ~16x fewer requests for the same tokens (RPM-bound → TPM-bound). Whole
# threads never split across a flush, so per-thread delete_stale stays correct.
_EMBED_FLUSH_CHUNKS = 96


def _prepare_thread(
    thread_id: str,
    history_id: str = "",
    prefetched_metadata: dict[str, str] | None = None,
    mailbox_email: str = "",
    service=None,
) -> dict[str, Any]:
    """Resolve one thread to a skip verdict or the rows to upsert — WITHOUT
    touching the store, so sync_query can batch the embed across many threads.

    Returns one of:
      {"thread_id", "skipped": True, "reason": ...}                  no write
      {"thread_id", "skipped": True, "reason": "empty_text", "delete": True}
      {"thread_id", "ids", "docs", "metas", "n_chunks", "subject"}   rows to upsert
    """
    if not mailbox_email:
        if service is None:
            service = get_service("gmail", "v1")
        mailbox_email = _mailbox_email(service)
    access_fields = metadata_access_fields("gmail", mailbox_email=mailbox_email)
    if history_id:
        existing = prefetched_metadata
        if existing is None:
            # Prefetch unavailable / failed → per-thread metadata lookup, so
            # one failed bulk call can't cascade into re-fetching + re-embedding
            # the ENTIRE mailbox this run (mirrors drive_sync._prepare_file's
            # per-file get_doc_metadata fallback). Store errors fall through to
            # a normal re-sync of just this thread.
            try:
                existing = get_store(_COLLECTION).get_doc_metadata(thread_id)
            except Exception as exc:
                print(
                    f"[rag_sync] Gmail get_doc_metadata({thread_id}) 失敗，"
                    f"照舊重同步該 thread: {exc}",
                    flush=True,
                )
                existing = None
        if (
            existing
            and existing.get("history_id") == history_id
            and metadata_access_matches(existing, access_fields)
            and _sync_complete_allows_skip(existing)
            and contextualize.ctx_ver_satisfied(existing)
        ):
            return {"thread_id": thread_id, "skipped": True, "reason": "unchanged"}

    if service is None:
        service = get_service("gmail", "v1")
    text, meta = _thread_text(service, thread_id)
    if not text.strip():
        return {"thread_id": thread_id, "skipped": True, "reason": "empty_text", "delete": True}

    ctx = contextualize.gen_doc_context(
        text, meta.get("subject", ""), "gmail_thread", "gmail"
    )
    ctx_ver = contextualize.CONTEXTUAL_VER if ctx else 0
    chunks = _chunk_text(text, subject=meta.get("subject", ""), context=ctx)
    if not chunks:
        return {"thread_id": thread_id, "skipped": True, "reason": "empty_chunks"}

    now = datetime.now(timezone.utc).isoformat()
    ids, docs, metas = [], [], []
    for i, chunk in enumerate(chunks):
        ids.append(f"{thread_id}__c{i}")
        docs.append(chunk)
        metas.append({
            "doc_id": thread_id,
            "subject": meta.get("subject", ""),
            "sender": meta.get("sender", ""),
            "date": meta.get("date", ""),
            "history_id": meta.get("history_id", "") or history_id,
            "chunk_index": i,
            "synced_at": now,
            # 先標 False；上層在「該 thread 所有 chunk 確定寫入 + stale 清完」
            # 後才 mark_doc_sync_complete 翻 True（drive_sync 同款兩段式）。
            "sync_complete": False,
            "ctx_ver": ctx_ver,
            # 小紅自產的報表：照常索引（還要能查「上週寄了什麼」），但檢索端
            # 預設濾掉，避免衍生品跟原始資料同級。舊資料沒有這個欄位 →
            # rag_gateway 的 $ne 過濾對「欄位不存在」是放行的，不會誤殺存量。
            "generated_by_red": bool(meta.get("generated_by_red")),
            **access_fields,
        })
    return {
        "thread_id": thread_id,
        "ids": ids, "docs": docs, "metas": metas,
        "n_chunks": len(chunks), "subject": meta.get("subject", ""),
    }


def _mark_thread_complete(store, thread_id: str) -> None:
    """Flip sync_complete=True on every chunk of a thread. Only call after
    upsert_batch AND delete_stale_chunks both succeeded for that thread —
    an unmarked thread is deliberately re-synced next run (self-heal)."""
    mark_complete = getattr(store, "mark_doc_sync_complete", None)
    if callable(mark_complete):
        mark_complete(thread_id)


def sync_thread(
    thread_id: str,
    history_id: str = "",
    prefetched_metadata: dict[str, str] | None = None,
    mailbox_email: str = "",
    service=None,
) -> dict[str, Any]:
    """Ingest or re-sync a single Gmail thread.

    ``service`` may be an explicit Gmail client (e.g. a delegated
    service-account client for a secondary mailbox); when omitted it falls
    back to the primary OAuth account.
    """
    prep = _prepare_thread(
        thread_id,
        history_id=history_id,
        prefetched_metadata=prefetched_metadata,
        mailbox_email=mailbox_email,
        service=service,
    )
    store = get_store(_COLLECTION)
    if prep.get("delete"):
        store.delete_by_doc_id(thread_id)
    if prep.get("skipped"):
        return {"thread_id": thread_id, "skipped": True, "reason": prep["reason"]}
    store.upsert_batch(prep["ids"], prep["docs"], prep["metas"])
    store.delete_stale_chunks(thread_id, prep["n_chunks"])
    _mark_thread_complete(store, thread_id)
    return {"thread_id": thread_id, "subject": prep["subject"], "chunks": prep["n_chunks"]}


def sync_query(
    gmail_query: str = "newer_than:180d",
    max_threads: int = 200,
    *,
    service=None,
    mailbox_email: str = "",
    service_builder=None,
    fetch_workers: int = 1,
) -> dict[str, Any]:
    """Sync threads matching a Gmail search query.

    Pass ``service`` (and optionally ``mailbox_email``) to sync a secondary
    mailbox via a delegated service-account client; both default to the
    primary OAuth account.

    Fetching each thread's body is a sequential network round-trip and is the
    dominant cost once embeds are batched. When ``service_builder`` is given and
    ``fetch_workers`` > 1, threads are fetched concurrently — each worker holds
    its OWN service from ``service_builder()`` (googleapiclient transport isn't
    thread-safe). Embedding stays serial+batched on the caller thread, so there
    are never concurrent ChromaDB writers. Default (no builder) = sequential.
    """
    if service is None:
        service = get_service("gmail", "v1")
    if not mailbox_email:
        mailbox_email = _mailbox_email(service)
    thread_refs: list[dict[str, str]] = []
    page_token = None
    while len(thread_refs) < max_threads:
        kwargs: dict[str, Any] = dict(
            userId="me",
            q=gmail_query,
            # 500 = threads.list 的 API 上限；100 一頁要多打 5 倍 list RPC。
            maxResults=min(500, max_threads - len(thread_refs)),
        )
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _execute(
            service.users().threads().list(**kwargs), "gmail.threads.list"
        )
        for t in resp.get("threads", []):
            tid = t.get("id")
            if tid:
                thread_refs.append({
                    "id": tid,
                    "history_id": str(t.get("historyId", "")),
                })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    store = get_store(_COLLECTION)
    thread_ids = [t["id"] for t in thread_refs]
    prefetched, used_prefetch = _prefetch_doc_metadata(store, thread_ids)

    results: list[dict[str, Any]] = []
    buf_ids: list[str] = []
    buf_docs: list[str] = []
    buf_metas: list[dict[str, Any]] = []
    pending: list[tuple[str, int, str]] = []  # (thread_id, n_chunks, subject)

    def _flush() -> None:
        # Embed + write everything buffered as one batch, then finalize each
        # thread individually (delete_stale is per-doc). A failed flush marks
        # exactly the buffered threads as errored — the rest are unaffected.
        if not buf_ids:
            return
        # Snapshot then clear up front: upsert_batch keeps no reference to the
        # lists after it returns, and clearing before the work means an
        # exception still leaves the buffer empty for the next round.
        ids_b, docs_b, metas_b = list(buf_ids), list(buf_docs), list(buf_metas)
        pend = list(pending)
        buf_ids.clear()
        buf_docs.clear()
        buf_metas.clear()
        pending.clear()
        try:
            store.upsert_batch(ids_b, docs_b, metas_b)
        except Exception as exc:
            # Embed/write failed mid-batch. upsert_batch writes 32-row
            # sub-batches, so earlier sub-batches may already have landed —
            # a thread straddling the failure boundary is half-written with
            # the NEW history_id on its leading chunks. Every chunk written
            # this round carries sync_complete=False, so next run's skip gate
            # (_sync_complete_allows_skip) refuses to fast-skip these threads
            # and they self-heal by re-syncing. None get marked complete here.
            for tid, _n, _subj in pend:
                results.append({"thread_id": tid, "skipped": True, "reason": f"error: {exc}"})
            return
        # Upsert landed → finalize each thread: clean the stale tail from a
        # previous (longer) version, THEN flip sync_complete=True. A thread is
        # marked complete only after upsert + delete_stale both succeeded for
        # it (drive_sync semantics); on failure it stays sync_complete=False
        # and is re-synced next run instead of fast-skipping a partial doc.
        # Per-thread try/except so one bad thread can't abort the rest.
        for tid, n, subj in pend:
            try:
                store.delete_stale_chunks(tid, n)
                _mark_thread_complete(store, tid)
            except Exception as exc:
                print(
                    f"[rag_sync] finalize({tid}) 失敗(已 upsert、未 mark complete,"
                    f"下一輪重同步): {exc}",
                    flush=True,
                )
            results.append({"thread_id": tid, "subject": subj, "chunks": n})

    def _safe_prepare(t: dict[str, str], svc) -> dict[str, Any]:
        tid = t["id"]
        try:
            meta = prefetched.get(tid, {}) if used_prefetch else None
            return _prepare_thread(
                tid,
                history_id=t.get("history_id", ""),
                prefetched_metadata=meta,
                mailbox_email=mailbox_email,
                service=svc,
            )
        except Exception as exc:
            return {"thread_id": tid, "skipped": True, "reason": f"error: {exc}"}

    def _process(prep: dict[str, Any]) -> None:
        # Caller-thread only — owns every store write (delete + embed flush) so
        # ChromaDB never sees concurrent writers even when fetch is parallel.
        if prep.get("delete"):
            store.delete_by_doc_id(prep["thread_id"])
        if prep.get("skipped"):
            results.append({
                "thread_id": prep["thread_id"],
                "skipped": True,
                "reason": prep.get("reason", ""),
            })
            return
        # Accumulate whole threads; flush once the buffer fills a few full
        # 32-row embed batches. A thread is never split across flushes.
        buf_ids.extend(prep["ids"])
        buf_docs.extend(prep["docs"])
        buf_metas.extend(prep["metas"])
        pending.append((prep["thread_id"], prep["n_chunks"], prep["subject"]))
        if len(buf_ids) >= _EMBED_FLUSH_CHUNKS:
            _flush()

    if service_builder is not None and fetch_workers > 1:
        import concurrent.futures
        import threading as _threading

        _tls = _threading.local()

        def _worker_prepare(t: dict[str, str]) -> dict[str, Any]:
            try:
                svc = getattr(_tls, "svc", None)
                if svc is None:
                    svc = service_builder()  # one fresh service per worker thread
                    _tls.svc = svc
            except Exception as exc:
                return {"thread_id": t["id"], "skipped": True,
                        "reason": f"error: service_builder {exc}"}
            return _safe_prepare(t, svc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=fetch_workers) as ex:
            # map preserves order and yields as this thread consumes; workers
            # fetch ahead while we embed → fetch/embed overlap.
            for prep in ex.map(_worker_prepare, thread_refs):
                _process(prep)
    else:
        for t in thread_refs:
            _process(_safe_prepare(t, service))
    _flush()

    synced = [r for r in results if not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    return {
        "query": gmail_query,
        "total": len(thread_ids),
        "synced": len(synced),
        "skipped": len(skipped),
    }


def sync_status() -> dict[str, Any]:
    store = get_store(_COLLECTION)
    return {"collection": _COLLECTION, "total_chunks": store.count()}
