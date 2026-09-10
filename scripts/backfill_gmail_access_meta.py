"""One-off: backfill rag_gateway access metadata onto legacy gmail_threads chunks.

Same root cause as the drive_docs backfill: ~785k primary-account gmail chunks
were indexed before access-control, so they lack owner_color / access_* /
mailbox_email. sync_thread's fast-skip needs metadata_access_matches(), which
fails for every legacy chunk → the gmail phase of the daily sync re-embeds the
whole primary mailbox under Gemini throttle.

Pure metadata patch, no re-embed. Legacy chunks with no mailbox_email belong to
the primary OAuth account → tagged with PRIMARY_MAILBOX. Chunks that already
carry a mailbox_email (the jaifung secondary boxes) keep theirs.

Idempotent: chunks that already have owner_color are skipped.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("RED_CHROMA_HTTP_URL", "http://127.0.0.1:8000")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_core.ingest.vector_store import get_store
from agent_core.rag_gateway import metadata_access_fields

COLLECTION = "gmail_threads"
PAGE = 2000
PRIMARY_MAILBOX = "owner@company.example"

store = get_store(COLLECTION)


def col_get(**kw):
    return store._with_collection(lambda c: c.get(**kw))


def col_update(**kw):
    return store._with_collection(lambda c: c.update(**kw))


total = store.count()
print(f"[backfill-gmail] {COLLECTION} 總 chunk: {total}  primary={PRIMARY_MAILBOX}", flush=True)

_cache: dict[str, dict] = {}
def access_for(mailbox: str) -> dict:
    fields = _cache.get(mailbox)
    if fields is None:
        fields = metadata_access_fields("gmail", mailbox_email=mailbox)
        _cache[mailbox] = fields
    return fields

offset = 0
seen = patched = already = errors = 0
t0 = time.time()

while True:
    try:
        res = col_get(include=["metadatas"], limit=PAGE, offset=offset)
    except Exception as exc:
        print(f"[backfill-gmail] get offset={offset} 失敗: {exc}", flush=True)
        errors += 1
        break
    ids = res.get("ids") or []
    metas = res.get("metadatas") or []
    if not ids:
        break

    up_ids, up_metas = [], []
    for cid, meta in zip(ids, metas):
        seen += 1
        meta = meta or {}
        if meta.get("owner_color"):
            already += 1
            continue
        mailbox = (meta.get("mailbox_email") or PRIMARY_MAILBOX)
        merged = {**meta, **access_for(mailbox)}
        up_ids.append(cid)
        up_metas.append(merged)

    if up_ids:
        try:
            col_update(ids=up_ids, metadatas=up_metas)
            patched += len(up_ids)
        except Exception as exc:
            print(f"[backfill-gmail] update offset={offset} 失敗: {exc}", flush=True)
            errors += 1

    offset += len(ids)
    if offset % (PAGE * 10) == 0 or len(ids) < PAGE:
        rate = seen / max(1e-6, time.time() - t0)
        print(f"[backfill-gmail] 進度 {seen}/{total}  patched={patched} already={already} "
              f"errors={errors}  {rate:.0f} chunk/s", flush=True)
    if len(ids) < PAGE:
        break

dt = time.time() - t0
print(f"[backfill-gmail] ✅ 完成: seen={seen} patched={patched} already={already} "
      f"errors={errors}  耗時={dt:.0f}s  mailboxes={list(_cache)}", flush=True)
