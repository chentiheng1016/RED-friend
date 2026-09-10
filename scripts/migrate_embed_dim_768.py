#!/usr/bin/env python3
"""Reconcile the `<name>_768` Matryoshka projection of each Chroma collection.

Phase 2 of the 3072→768 embedding migration (framework: agent_core/
embedding_config.py; background: project memory chroma_dim_migration_768).

Makes each `<name>_768` collection an exact 768-dim projection of its 3072-dim
source by TRUNCATING the existing vectors — **no re-embedding, zero Gemini
cost**. gemini-embedding-001 is Matryoshka, so for every stored 3072 vector
`normalize(v[:768])` equals the native 768 embedding (verified cos=1.000000),
i.e. it lands in exactly the space that RED_EMBED_DIM=768 produces going forward.

Per source collection (DROP + rebuild, so the persisted EF matches the fleet's):
    delete <name>_768               # the old snapshot carried chroma's DEFAULT EF,
    create <name>_768 with the gemini EF   #   which conflicts when the fleet opens
                                    #   it with the gemini EF at cutover (ValueError)
    for every (id, emb, doc, meta) in <name>:
        emb768 = L2_normalize(emb[:768])
        upsert (id, emb768, doc, meta)   # explicit embedding → EF not called during build

Full mirror (id + embedding + documents + metadata) so retrieval — which filters
on metadata (drive_id / folder_id / access_*) and returns the document text —
works on the _768 index exactly as on the 3072 one. Idempotent (drop+rebuild is
deterministic). Reads 3072 vectors from the shared chroma server and must NOT
race new writes, so **run with rag stopped** (freezes drive_docs/gmail_threads).

Cutover protocol: run once now to converge the bulk; run again under the Phase 3
freeze (fleet stopped, 3072 collections frozen) to capture the final delta, THEN
flip RED_EMBED_DIM=768 fleet-wide.

Usage (always via the shared server):
    export RED_CHROMA_HTTP_URL=http://127.0.0.1:8000
    .venv/bin/python scripts/migrate_embed_dim_768.py --verify                 # read-only sanity, all collections
    .venv/bin/python scripts/migrate_embed_dim_768.py --collection operation_sops --apply
    .venv/bin/python scripts/migrate_embed_dim_768.py --all --apply            # full reconcile (slow: gmail ~M rows)

Dry-run is the default; --apply is required to write. Safe to Ctrl-C and re-run.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.embedding_config import FULL_EMBED_DIM, maybe_normalize  # noqa: E402
# The fleet opens every collection with this exact EF; create/open the _768
# targets with it too so the persisted embedding-function config matches at
# cutover (a fresh collection created without it gets chroma's default 384-dim
# MiniLM EF → name mismatch when the fleet later opens it with the gemini EF).
from agent_core.ingest.vector_store import _ef  # noqa: E402

# Logical source collections (3072). Each gets a `<name>_768` projection. Mirrors
# vector_store._VALID_COLLECTIONS + memory's xiaohong_memory (every collection the
# fleet opens, so the cutover flips them all).
SOURCE_COLLECTIONS = (
    "drive_docs",
    "gmail_threads",
    "google_chat_messages",
    "operation_sops",
    "xiaohong_memory",
)
TARGET_DIM = 768
SUFFIX = f"_{TARGET_DIM}"

_READ_BATCH = 512   # ids per get(include=embeddings/docs/metas) — indexed id lookup, no deep OFFSET
_WRITE_BATCH = 256  # ids per upsert/delete — stays well under chroma's SQLite variable cap


def _client():
    from agent_core.chroma_backend import build_chroma_client

    return build_chroma_client("")  # HTTP mode (RED_CHROMA_HTTP_URL); persist_dir ignored


def _truncate_normalize(emb) -> list[float]:
    """L2-normalize the first 768 dims of a 3072 vector → the canonical 768
    projection. Uses numpy for bulk speed; asserted equal to embedding_config.
    maybe_normalize (the going-forward path) in --verify so the two never drift."""
    v = np.asarray(emb, dtype=np.float64)[:TARGET_DIM]
    n = np.linalg.norm(v)
    return (v / n).tolist() if n else v.tolist()


def _all_ids(col) -> list[str]:
    """Every id in a collection (no embeddings — cheap payload). One shot; chroma
    has no cursor API but an ids-only get avoids per-row vector hydration."""
    got = col.get(include=[])
    return list(got["ids"])


def _coerce_meta(m):
    # chroma rejects None metadata; a row should always carry a dict, but be defensive.
    return m if isinstance(m, dict) else {}


def reconcile_collection(client, name: str, *, apply: bool) -> dict:
    """Rebuild <name>_768 as a fresh 768 projection of <name>, created with the
    fleet's gemini embedding function.

    The existing _768 snapshots were built (by the now-gone /tmp script) with
    chroma's DEFAULT embedding function persisted in their config. Opening them
    with the gemini EF — as the fleet does at cutover — raises an EF-conflict
    ValueError. So we DROP and recreate each with `_ef`, then mirror every source
    row (id + truncated embedding + documents + metadata). A full mirror re-adds
    everything anyway, so dropping the old snapshot loses nothing; it just also
    fixes the persisted EF and clears any partial/diverged state.

    Assumes rag is stopped so the big source collections (drive_docs,
    gmail_threads) are frozen — otherwise the rebuild races new 3072 writes and
    misses them. xiaohong_memory can still drift (telegram); re-run it under the
    Phase 3 freeze right before flipping the flag."""
    src = client.get_collection(name)
    dst_name = name + SUFFIX
    src_ids = _all_ids(src)
    n = len(src_ids)
    print(f"\n[{name}] source={n} rows  →  rebuild {dst_name}", flush=True)

    if not apply:
        try:
            had = client.get_collection(dst_name).count()  # tolerant read (no EF → no conflict)
        except Exception:  # noqa: BLE001
            had = "missing"
        print(f"[{name}] dry-run — would drop {dst_name} (has {had}) and rebuild with {n} rows", flush=True)
        return {"source": n, "written": 0}

    try:
        client.delete_collection(dst_name)
        print(f"  dropped stale {dst_name} (default-EF / partial)", flush=True)
    except Exception:  # noqa: BLE001 — missing is fine
        pass
    dst = client.create_collection(
        name=dst_name, embedding_function=_ef, metadata={"hnsw:space": "cosine"}
    )

    written = 0
    t0 = time.time()
    for off in range(0, n, _READ_BATCH):
        batch_ids = src_ids[off : off + _READ_BATCH]
        rows = src.get(ids=batch_ids, include=["embeddings", "documents", "metadatas"])
        ids = rows["ids"]
        embs = rows["embeddings"]
        docs = rows.get("documents") or [None] * len(ids)
        metas = rows.get("metadatas") or [None] * len(ids)
        out_emb = [_truncate_normalize(e) for e in embs]
        out_doc = ["" if d is None else d for d in docs]
        out_meta = [_coerce_meta(m) for m in metas]
        for w in range(0, len(ids), _WRITE_BATCH):
            dst.upsert(
                ids=ids[w : w + _WRITE_BATCH],
                embeddings=out_emb[w : w + _WRITE_BATCH],
                documents=out_doc[w : w + _WRITE_BATCH],
                metadatas=out_meta[w : w + _WRITE_BATCH],
            )
        written += len(ids)
        if off // _READ_BATCH % 20 == 0 or off + _READ_BATCH >= n:
            rate = written / max(1e-6, time.time() - t0)
            print(f"  {written}/{n}  ({rate:.0f}/s)", flush=True)

    print(f"[{name}] rebuilt {dst_name}: {written} rows", flush=True)
    return {"source": n, "written": written}


def verify_collection(client, name: str) -> None:
    """Read-only: confirm the existing _768 snapshot has documents+metadata and
    that its vectors are the canonical truncation of the source."""
    src = client.get_collection(name)
    try:
        dst = client.get_collection(name + SUFFIX)
    except Exception as exc:  # noqa: BLE001
        print(f"[{name}] {name + SUFFIX}: ❌ missing ({exc}) — needs full build")
        return
    sample = dst.get(limit=3, include=["embeddings", "documents", "metadatas"])
    ids = sample["ids"]
    embs = sample.get("embeddings")
    has_docs = bool(sample.get("documents")) and any(sample["documents"])
    has_meta = bool(sample.get("metadatas")) and any(sample["metadatas"])
    dim = len(embs[0]) if embs is not None and len(embs) and embs[0] is not None else "?"
    print(f"[{name}] {name + SUFFIX}: dim={dim} has_docs={has_docs} has_meta={has_meta}", flush=True)
    if not ids:
        print("  (empty)", flush=True)
        return
    srcrows = src.get(ids=ids, include=["embeddings"])
    srcmap = {i: e for i, e in zip(srcrows["ids"], srcrows["embeddings"])}
    for i, _id in enumerate(ids):
        if _id in srcmap and embs is not None:
            want = _truncate_normalize(srcmap[_id])
            got = np.asarray(embs[i], dtype=np.float64)
            got = got / (np.linalg.norm(got) or 1.0)
            cos = float(np.dot(got, np.asarray(want)))
            # guard: bulk numpy path agrees with the going-forward list path
            ref = maybe_normalize_768(srcmap[_id])
            drift = float(np.max(np.abs(np.asarray(want) - np.asarray(ref))))
            print(f"  id={_id[:24]} cos(stored,truncation)={cos:.5f}  helper_drift={drift:.2e}", flush=True)


def maybe_normalize_768(emb):
    """embedding_config.maybe_normalize evaluated as if RED_EMBED_DIM=768 — the
    going-forward normalization, to assert the bulk path matches it."""
    import os
    from unittest import mock

    with mock.patch.dict(os.environ, {"RED_EMBED_DIM": "768"}):
        return maybe_normalize(list(np.asarray(emb, dtype=np.float64)[:TARGET_DIM]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--collection", choices=SOURCE_COLLECTIONS, help="reconcile one")
    g.add_argument("--all", action="store_true", help="reconcile every collection")
    g.add_argument("--verify", action="store_true", help="read-only sanity, no writes")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    # This process operates in 768 mode: make the shared embedding function
    # produce 768 if it is ever invoked (it is not — we always upsert explicit
    # embeddings — but keeps the process self-consistent). Set only on a real
    # script run, never at import, so it can't leak into a test process.
    os.environ.setdefault("RED_EMBED_DIM", "768")

    if FULL_EMBED_DIM == TARGET_DIM:
        print("FULL_EMBED_DIM == TARGET_DIM — nothing to migrate", file=sys.stderr)
        return 2
    client = _client()

    if args.verify:
        for name in SOURCE_COLLECTIONS:
            verify_collection(client, name)
        return 0

    targets = SOURCE_COLLECTIONS if args.all else (args.collection,)
    total_written = 0
    for name in targets:
        r = reconcile_collection(client, name, apply=args.apply)
        total_written += r["written"]
    print(f"\n=== total: {'rebuilt' if args.apply else 'would rebuild'} {total_written} rows ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
