"""一次性清理：同 Shared Drive 內 content_hash 相同的複本 → 留正本、餘者刪 chunk＋記 marker。

配套 RAG_DRIVE_DEDUP_SCOPE=drive（vector_store 去重閘 scope 放寬，PR 同批）。正本選擇
與閘一致：同 (drive_id, content_hash) 群中 doc_id 最小的 sync_complete 檔——marker 的
自癒檢查（find_duplicate_doc_id）會解出同一個正本，marker 站得住、夜跑不會回填複本。

順序刻意「先刪 chunk、後記 marker」：中途斷掉＝chunks 已刪但無 marker → 下輪夜跑
重抽該檔 → 去重閘（drive scope）自己判定重複、補回 marker——自癒、不留殭屍。

用法（在 repo 根目錄）：
  .venv/bin/python scripts/purge_drive_samehash_dups.py            # dry-run 唯讀統計
  .venv/bin/python scripts/purge_drive_samehash_dups.py --apply    # 實刪

--apply 防呆：①RAG_DRIVE_DEDUP_SCOPE 必須=drive（否則 marker 會被舊 scope 自癒清掉、
夜跑回填）；②偵測到 rag_sync.py 在跑即中止（skip-state 檔不可並寫）；③正本與複本的
owner_color 不同即跳過該群（ACL 保險絲，現況 drive 級規則下不應發生）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections import defaultdict
from typing import Any


def _rag_sync_running() -> bool:
    out = subprocess.run(
        ["pgrep", "-f", "rag_sync.py"], capture_output=True, text=True
    ).stdout.strip()
    return bool(out)


def _collect_docs(col) -> tuple[dict[str, dict[str, Any]], int]:
    """分頁掃全 collection metadata → 每 doc 的彙總（chunk ids/數量/雜湊/scope/完整性）。"""
    docs: dict[str, dict[str, Any]] = {}
    off = 0
    while True:
        got = col.get(limit=2000, offset=off, include=["metadatas"])
        ids = got["ids"]
        if not ids:
            break
        for cid, md in zip(ids, got["metadatas"]):
            did = str(md.get("doc_id") or "")
            if not did:
                continue
            rec = docs.setdefault(did, {
                "chunk_ids": [], "drive_id": "", "content_hash": "",
                "title": "", "modified_time": "", "folder_id": "",
                "mime_type": "", "owner_color": "", "complete": True,
            })
            rec["chunk_ids"].append(cid)
            for k in ("drive_id", "content_hash", "title", "modified_time",
                      "folder_id", "mime_type", "owner_color"):
                if not rec[k] and md.get(k):
                    rec[k] = str(md[k])
            sc = md.get("sync_complete", True)
            if sc in (False, 0, "0", "false", "False"):
                rec["complete"] = False
        off += len(ids)
        if off % 200_000 < 2000:
            print(f"[scan] {off} chunks…", flush=True)
    return docs, off


def main() -> None:
    apply_mode = "--apply" in sys.argv

    from agent_core.ingest import drive_sync
    from agent_core.ingest.vector_store import get_store

    if apply_mode:
        scope = os.environ.get("RAG_DRIVE_DEDUP_SCOPE", "folder").strip().lower()
        assert scope == "drive", (
            "--apply 需要 RAG_DRIVE_DEDUP_SCOPE=drive，否則 marker 會被舊 scope "
            "自癒清掉、夜跑把複本抽回來")
        assert not _rag_sync_running(), "rag_sync.py 正在跑——skip-state 不可並寫，中止"

    store = get_store("drive_docs")
    col = store._open_collection()
    docs, total_chunks = _collect_docs(col)
    print(f"[scan] docs={len(docs)} chunks={total_chunks}", flush=True)

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for did, rec in docs.items():
        if rec["drive_id"] and rec["content_hash"]:
            groups[(rec["drive_id"], rec["content_hash"])].append(did)

    plans = []           # (canonical, [dup_doc, ...])
    skipped_acl = 0
    for (dr, h), members in groups.items():
        if len(members) < 2:
            continue
        complete = sorted(d for d in members if docs[d]["complete"])
        if not complete:
            continue
        canonical = complete[0]
        dups = []
        for d in sorted(members):
            if d == canonical:
                continue
            if docs[d]["owner_color"] != docs[canonical]["owner_color"]:
                skipped_acl += 1
                continue
            dups.append(d)
        if dups:
            plans.append((canonical, dups))

    dup_docs = sum(len(v) for _, v in plans)
    dup_chunks = sum(len(docs[d]["chunk_ids"]) for _, v in plans for d in v)
    print(f"\n[plan] 同drive同hash 群 {len(plans)}、可合併 {dup_docs} 檔 / "
          f"{dup_chunks} chunks（佔索引 {dup_chunks / max(1, total_chunks):.1%}）"
          f"｜ACL 保險絲跳過 {skipped_acl} 檔", flush=True)
    by_size = sorted(plans, key=lambda p: len(p[1]), reverse=True)
    for canonical, dups in by_size[:8]:
        print(f"  x{len(dups) + 1}: {docs[canonical]['title']!r}", flush=True)

    if not apply_mode:
        print("\n[dry-run] 未動任何資料。實刪加 --apply。", flush=True)
        return

    print("\n[apply] 開始刪 chunk＋記 marker…", flush=True)
    deleted_docs = deleted_chunks = marker_fail = 0
    batch: list[str] = []
    pending_markers: list[tuple[str, dict[str, Any]]] = []

    def _flush_batch():
        nonlocal deleted_chunks, marker_fail
        if not batch:
            return
        col.delete(ids=list(batch))
        deleted_chunks += len(batch)
        batch.clear()
        for fid, rec in pending_markers:
            try:
                drive_sync._record_skip_marker(
                    fid,
                    reason="duplicate_content",
                    modified_time=rec["modified_time"],
                    folder_id=rec["folder_id"],
                    drive_id=rec["drive_id"],
                    title=rec["title"],
                    extra={
                        "canonical_doc_id": rec["canonical"],
                        "content_hash": rec["content_hash"],
                        "mime_type": rec["mime_type"],
                    },
                )
            except Exception as exc:  # noqa: BLE001 — 單檔 marker 失敗→夜跑自癒補
                marker_fail += 1
                print(f"[apply] marker 失敗 {fid}: {exc}", flush=True)
        pending_markers.clear()

    for canonical, dups in plans:
        for d in dups:
            rec = dict(docs[d])
            rec["canonical"] = canonical
            batch.extend(rec["chunk_ids"])
            pending_markers.append((d, rec))
            deleted_docs += 1
            if len(batch) >= 2000:
                _flush_batch()
        if deleted_docs % 2000 < len(dups):
            print(f"[apply] {deleted_docs}/{dup_docs} docs…", flush=True)
    _flush_batch()
    print(f"\n[apply] 完成：刪 {deleted_docs} 檔 / {deleted_chunks} chunks、"
          f"marker 失敗 {marker_fail}（失敗者夜跑會經去重閘自癒）", flush=True)
    print(f"[apply] 索引 {total_chunks} → {total_chunks - deleted_chunks}", flush=True)


if __name__ == "__main__":
    main()
