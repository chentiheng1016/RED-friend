#!/usr/bin/env python3
"""One-time cleanup: delete exact-duplicate drive_docs documents.

Drive copies the same file into many folders / Shared Drives, each with its
own file_id (== doc_id). Before the content_hash dedup gate landed in
drive_sync (see agent_core/ingest/drive_sync.py sync_file +
VectorStore.find_duplicate_doc_id), every copy was embedded independently.

The gate prevents NEW duplicates, but cannot reach the ones already indexed:
the daily sync fast-skips them on modifiedTime and never reaches the gate.
This script removes the existing backlog. For each content_hash shared by more
than one fully-synced doc_id it keeps a single canonical copy (the smallest
doc_id -- a stable total order so re-runs converge) and deletes the rest via
the collection.delete() API, preserving HNSW index consistency.

After deletion each removed file still exists in Drive, so the next daily sync
re-downloads it once, the dedup gate finds the surviving canonical copy, and
records a duplicate_content skip_marker -- so it is NOT re-embedded, and later
runs fast-skip it before any download. That one-time re-download backlog drains
incrementally under the sync's runtime cap.

Safety:
  * dry-run by default -- prints the plan, writes nothing.
  * Never deletes a group lacking a fully-synced canonical to keep.
  * Never deletes the canonical itself.
  * Access-control safe: duplicate groups were verified to share identical
    per-department access fingerprints (0 groups with conflicting access).

Usage:
    AGENT_DAEMON_MODE=1 RED_CHROMA_HTTP_URL=http://127.0.0.1:8000 \
        .venv/bin/python scripts/dedup_drive_docs.py            # dry-run
    AGENT_DAEMON_MODE=1 RED_CHROMA_HTTP_URL=http://127.0.0.1:8000 \
        .venv/bin/python scripts/dedup_drive_docs.py --apply    # delete

Safe to re-run: once a group is collapsed to one doc_id it is no longer a
duplicate group, so a second pass is a no-op.
"""
from __future__ import annotations

import argparse
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.ingest.vector_store import get_store  # noqa: E402


def _metadata_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no"}
    return bool(value)


def _resolve_collection(store):
    # VectorStore hides the chromadb collection behind _with_collection; reuse
    # it so this script honours the same HTTP/persistent backend selection.
    return store._with_collection(lambda col: col)


def _iter_all_chunks(col, page=5000):
    offset = 0
    while True:
        res = col.get(include=["metadatas"], limit=page, offset=offset)
        metas = res.get("metadatas") or []
        ids = res.get("ids") or []
        if not ids:
            return
        yield ids, metas
        if len(ids) < page:
            return
        offset += page


class _DocStat:
    __slots__ = ("content_hash", "title", "chunks", "all_complete",
                 "drive_id", "folder_id")

    def __init__(self, content_hash: str, title: str) -> None:
        self.content_hash = content_hash
        self.title = title
        self.chunks = 0
        self.all_complete = True
        self.drive_id = ""
        self.folder_id = ""


def _scan(col, page: int) -> dict[str, _DocStat]:
    """Collapse the chunk stream into one _DocStat per doc_id."""
    docs: dict[str, _DocStat] = {}
    scanned = 0
    for ids, metas in _iter_all_chunks(col, page=page):
        for meta in metas:
            scanned += 1
            doc_id = meta.get("doc_id")
            if not doc_id:
                continue
            stat = docs.get(doc_id)
            if stat is None:
                stat = _DocStat(
                    content_hash=str(meta.get("content_hash") or ""),
                    title=str(meta.get("title") or ""),
                )
                docs[doc_id] = stat
            stat.chunks += 1
            if not _metadata_bool(meta.get("sync_complete", True)):
                stat.all_complete = False
            if not stat.content_hash and meta.get("content_hash"):
                stat.content_hash = str(meta["content_hash"])
            if not stat.drive_id and meta.get("drive_id"):
                stat.drive_id = str(meta["drive_id"])
            if not stat.folder_id and meta.get("folder_id"):
                stat.folder_id = str(meta["folder_id"])
        print(f"[dedup] scanned {scanned} chunks, {len(docs)} docs...", flush=True)
    print(f"[dedup] scan complete: {scanned} chunks across {len(docs)} docs", flush=True)
    return docs


def _plan(docs: dict[str, _DocStat]):
    """Return (doc_ids_to_delete, decisions, groups_skipped).

    decisions rows: (content_hash, canonical_doc_id, victim_doc_id, victim_chunks)
    Groups with no fully-synced canonical are skipped.
    """
    # Group by (content_hash, drive_id, folder_id): only byte-identical copies
    # in the SAME scope are true duplicates. Cross-folder / cross-drive copies
    # are legitimately separate entries because search_drive_docs filters on
    # drive_id/folder_id (matches the same-scope rule in
    # VectorStore.find_duplicate_doc_id).
    by_key: dict[tuple[str, str, str], list[str]] = {}
    for doc_id, stat in docs.items():
        if not stat.content_hash:
            continue  # legacy chunk without a hash -- cannot dedup safely
        key = (stat.content_hash, stat.drive_id, stat.folder_id)
        by_key.setdefault(key, []).append(doc_id)

    to_delete: list[str] = []
    decisions: list[tuple[str, str, str, int]] = []
    groups_skipped = 0
    for (content_hash, _drive_id, _folder_id), doc_ids in by_key.items():
        if len(doc_ids) < 2:
            continue
        complete = sorted(d for d in doc_ids if docs[d].all_complete)
        if not complete:
            groups_skipped += 1
            continue
        canonical = complete[0]
        for victim in sorted(doc_ids):
            if victim == canonical:
                continue
            to_delete.append(victim)
            decisions.append((content_hash, canonical, victim, docs[victim].chunks))
    return to_delete, decisions, groups_skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete duplicates (default: dry-run)")
    parser.add_argument("--page", type=int, default=5000)
    parser.add_argument("--show", type=int, default=25, help="example groups to print")
    args = parser.parse_args()

    store = get_store("drive_docs")
    col = _resolve_collection(store)

    backend = "HTTP" if os.environ.get("RED_CHROMA_HTTP_URL") else "PersistentClient"
    print(f"[dedup] backend={backend} collection=drive_docs apply={args.apply}", flush=True)

    docs = _scan(col, page=args.page)
    to_delete, decisions, groups_skipped = _plan(docs)

    dup_chunks = sum(d[3] for d in decisions)
    total_chunks = sum(s.chunks for s in docs.values())
    groups = len({d[0] for d in decisions})

    print("\n=== dedup plan ===", flush=True)
    print(f"  duplicate groups (>=2 copies)      : {groups}", flush=True)
    print(f"  documents to delete                : {len(to_delete)}", flush=True)
    print(f"  chunks to delete                   : {dup_chunks}", flush=True)
    print(f"  total chunks now                   : {total_chunks}", flush=True)
    if total_chunks:
        print(f"  reclaim                            : {100*dup_chunks/total_chunks:.1f}% of chunks", flush=True)
    print(f"  groups skipped (no complete copy)  : {groups_skipped}\n", flush=True)

    if decisions and args.show:
        print(f"=== sample (up to {args.show} groups) ===", flush=True)
        seen: set[str] = set()
        shown = 0
        for content_hash, canonical, victim, vchunks in decisions:
            if content_hash in seen:
                continue
            seen.add(content_hash)
            print(
                f"  hash={content_hash} keep={canonical} "
                f"({docs[canonical].title[:42]!r}) drop={victim} "
                f"({docs[victim].title[:42]!r}, {vchunks} chunks)",
                flush=True,
            )
            shown += 1
            if shown >= args.show:
                break
        print("", flush=True)

    if not args.apply:
        print("[dedup] dry-run only -- nothing written. Re-run with --apply to delete.", flush=True)
        return 0

    if not to_delete:
        print("[dedup] nothing to delete.", flush=True)
        return 0

    print(f"[dedup] deleting {len(to_delete)} duplicate documents...", flush=True)
    BATCH = 200
    deleted = 0
    for i in range(0, len(to_delete), BATCH):
        batch = to_delete[i:i + BATCH]
        store.bulk_delete_by_doc_ids(batch)
        deleted += len(batch)
        print(f"[dedup]   deleted {deleted}/{len(to_delete)} docs", flush=True)
    print(f"[dedup] done. Deleted {deleted} documents ({dup_chunks} chunks).", flush=True)
    print(
        "[dedup] NOTE: next daily sync re-downloads these files once, hits the "
        "dedup gate, records duplicate_content markers (no re-embed).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
