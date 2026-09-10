"""One-off: backfill rag_gateway access metadata onto legacy drive_docs chunks.

Why: ~1.1M drive_docs chunks were indexed before the access-control feature, so
they carry no owner_color / access_* fields. sync_file's fast-skip requires
metadata_access_matches(), which fails for every such chunk → the daily sync
re-downloads + re-embeds the ENTIRE Drive corpus (23h+, Gemini-throttled),
starving the Gmail phase that runs after it.

These fields are pure metadata (computed, content-independent), so we patch them
in place via chromadb's metadata-only update() — no download, no Gemini, no
re-embed. After this, fast-skip passes and daily syncs are cheap again.

Idempotent: chunks that already have owner_color are left untouched, so it is
safe to re-run / resume.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("RED_CHROMA_HTTP_URL", "http://127.0.0.1:8000")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_core.ingest.vector_store import get_store
from agent_core.rag_gateway import metadata_access_fields

COLLECTION = "drive_docs"
PAGE = 2000

store = get_store(COLLECTION)


def col_get(**kw):
    return store._with_collection(lambda c: c.get(**kw))


def col_update(**kw):
    return store._with_collection(lambda c: c.update(**kw))


total = store.count()
print(f"[backfill] {COLLECTION} 總 chunk: {total}", flush=True)

# Cache access_fields per (file_id, folder_id, drive_id) so we don't reload
# rag_access.json 1.1M times — chunks of one doc share the tuple.
_cache: dict[tuple, dict] = {}
def access_for(meta: dict) -> dict:
    key = (meta.get("doc_id", ""), meta.get("folder_id", ""), meta.get("drive_id", ""))
    fields = _cache.get(key)
    if fields is None:
        fields = metadata_access_fields(
            "drive",
            file_id=key[0], folder_id=key[1], drive_id=key[2],
        )
        _cache[key] = fields
    return fields

offset = 0
seen = 0
patched = 0
already = 0
errors = 0
t0 = time.time()

while True:
    try:
        res = col_get(include=["metadatas"], limit=PAGE, offset=offset)
    except Exception as exc:
        print(f"[backfill] get offset={offset} 失敗: {exc}", flush=True)
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
        if meta.get("owner_color"):          # already has access fields
            already += 1
            continue
        merged = {**meta, **access_for(meta)}
        up_ids.append(cid)
        up_metas.append(merged)

    if up_ids:
        try:
            col_update(ids=up_ids, metadatas=up_metas)
            patched += len(up_ids)
        except Exception as exc:
            print(f"[backfill] update offset={offset} ({len(up_ids)} 筆) 失敗: {exc}", flush=True)
            errors += 1

    offset += len(ids)
    if offset % (PAGE * 10) == 0 or len(ids) < PAGE:
        rate = seen / max(1e-6, time.time() - t0)
        print(f"[backfill] 進度 {seen}/{total}  patched={patched} already={already} "
              f"errors={errors}  {rate:.0f} chunk/s", flush=True)
    if len(ids) < PAGE:
        break

dt = time.time() - t0
print(f"[backfill] ✅ 完成: seen={seen} patched={patched} already={already} "
      f"errors={errors}  耗時={dt:.0f}s  unique_docs={len(_cache)}", flush=True)
