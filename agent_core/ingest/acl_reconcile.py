"""RAG ACL 對帳 — 讓 rag_access.json 規則變動套用到「存量」chunk。

規則檔（var/data/rag_access.json）改了只影響之後 ingest 的新 chunk；存量
chunk 的 access_<color> 旗標不會自己更新。這個模組把「按規則重標存量」抽成
單一可重用實作，供兩個入口共用：

  - `scripts/backfill_rag_access.py`：一次性手動全量 backfill（無時間預算）
  - `rag_runner`：每日夜跑最後一個 phase（帶時間預算，只補當日 delta）

對每條會授「非 red」權限的 drive / gmail 規則：
  1. 直讀 chroma.sqlite3（唯讀；filter-first planner hint，同
     vector_store._sql_doc_ids）撈出該 drive_id / mailbox_email 的 embedding id
  2. 走共用 HTTP server 分批 get(ids) → 與 rag_gateway.metadata_access_fields
     現算的期望旗標比對 → 不同才 update（只改 mismatch，冪等）

安全邊界：
  - 只透過傳入的（共用 HTTP server）client 寫 —— 絕不直開 PersistentClient
  - 期望旗標一律走 rag_gateway.metadata_access_fields 現算，與 ingest 同源，
    本模組不重複實作 ACL 語義
  - red-only 規則（與無規則的預設一致）直接跳過，不浪費 I/O
  - `time_budget_s` 到期在「批次邊界」乾淨停手（回 stopped_early=True），
    夜跑用得起、不會撞 wall-clock 看門狗
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Callable, Iterator, Mapping

_BATCH = 300
_SQL_FETCH = 5000

# (logical collection, kind, chunk metadata 的鍵, 規則段, 規則的鍵欄位)
_TARGETS: tuple[tuple[str, str, str, str, str], ...] = (
    ("drive_docs", "drive", "drive_id", "drive_sources", "drive_id"),
    ("gmail_threads", "gmail", "mailbox_email", "gmail_mailboxes", "mailbox_email"),
)


def _metadata_segment(conn: sqlite3.Connection, physical_name: str) -> str:
    row = conn.execute(
        """
        select s.id from collections c
        join segments s on s.collection = c.id
        where c.name = ? and s.scope = 'METADATA' limit 1
        """,
        (physical_name,),
    ).fetchone()
    return str(row[0]) if row else ""


def _iter_embedding_ids(
    conn: sqlite3.Connection, segment_id: str, key: str, value: str,
) -> Iterator[str]:
    """filter-first SQL fastpath（同 vector_store 的 planner hint 教訓：
    先走 embedding_metadata_string_value 索引，別讓 planner 從 doc_id 側全掃）。"""
    cur = conn.execute(
        """
        select e.embedding_id
        from embedding_metadata flt indexed by embedding_metadata_string_value
        cross join embeddings e on e.id = flt.id
        where flt.key = ? and flt.string_value = ? and e.segment_id = ?
        """,
        (key, value, segment_id),
    )
    while True:
        rows = cur.fetchmany(_SQL_FETCH)
        if not rows:
            return
        for (embedding_id,) in rows:
            yield embedding_id


def _grants_non_red(expected: Mapping[str, Any]) -> bool:
    return any(
        k.startswith("access_") and k != "access_red" and v is True
        for k, v in expected.items()
    )


def _set_request_timeout(client, seconds: float) -> None:
    """給底層 httpx session 設請求 timeout（best-effort）。

    chromadb HttpClient 預設無 per-request 上限 —— 一個 wedge 的 get/update
    會讓呼叫端永久卡在 socket recv（實測過：server 閒置卻不回，client 卡死
    整個 job）。設了 timeout 後 wedge 請求會 raise，由 caller 當「這批失敗、
    跳過、冪等下次補」處理，而不是無限期掛住。"""
    try:
        import httpx
        client._server._session.timeout = httpx.Timeout(seconds)
    except Exception:
        pass


def _apply_batch(col, ids: list[str], expected: Mapping[str, Any],
                 matches: Callable, dry_run: bool) -> int:
    got = col.get(ids=ids, include=["metadatas"])
    upd_ids: list[str] = []
    upd_mds: list[dict] = []
    for eid, md in zip(got.get("ids") or [], got.get("metadatas") or []):
        md = md or {}
        if matches(md, expected):
            continue
        upd_ids.append(eid)
        upd_mds.append({**md, **expected})
    if upd_ids and not dry_run:
        col.update(ids=upd_ids, metadatas=upd_mds)
    return len(upd_ids)


def reconcile_acl(
    *,
    client=None,
    time_budget_s: float | None = None,
    request_timeout_s: float = 60.0,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """按 rag_access.json 現值，重標存量 chunk 的 access_<color> 旗標。

    client：Chroma HttpClient（None 則走 chroma_backend.build_chroma_client
        —— 需 RED_CHROMA_HTTP_URL，絕不直開 PersistentClient）。
    time_budget_s：wall-clock 上限（秒）；到期在批次邊界停手。None = 不限。
    request_timeout_s：單一 chroma 請求上限；wedge 請求逾時 raise → 該批
        當失敗跳過（非致命），而非永久卡住整個 job。
    dry_run：只比對不寫入。

    回 {scanned, updated, failed_batches, stopped_early, sources: [...]}。
    單批 get/update 失敗（逾時／網路）不致命：記錄、跳過、冪等下次補。
    """
    from agent_core.embedding_config import physical_collection_name
    from agent_core.ingest.vector_store import _CHROMA_PATH
    from agent_core.rag_gateway import (
        _load_access_config,
        metadata_access_fields,
        metadata_access_matches,
    )

    db_path = os.path.join(_CHROMA_PATH, "chroma.sqlite3")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"找不到 {db_path}（chroma.sqlite3）")

    if client is None:
        from agent_core.chroma_backend import build_chroma_client
        client = build_chroma_client(_CHROMA_PATH)
    _set_request_timeout(client, request_timeout_s)

    config = _load_access_config()
    t0 = time.monotonic()
    deadline = (t0 + time_budget_s) if time_budget_s else None

    total_scanned = total_updated = failed_batches = 0
    sources: list[dict[str, Any]] = []
    stopped_early = False

    def _safe_apply(col, ids: list[str], expected) -> int:
        nonlocal failed_batches
        try:
            return _apply_batch(col, ids, expected, metadata_access_matches, dry_run)
        except Exception as exc:  # 逾時／網路／server wedge — 非致命
            failed_batches += 1
            log(f"[acl_reconcile] ⚠️ 批次失敗（跳過，冪等下次補）: "
                f"{type(exc).__name__}: {str(exc)[:120]}")
            return 0

    for logical, kind, chunk_key, section, rule_key in _TARGETS:
        if stopped_early:
            break
        physical = physical_collection_name(logical)
        try:
            col = client.get_collection(physical)
        except Exception as exc:
            log(f"[acl_reconcile] {physical} 取 collection 失敗，跳過: {exc}")
            continue
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
        try:
            segment_id = _metadata_segment(conn, physical)
            if not segment_id:
                log(f"[acl_reconcile] {physical} 找不到 metadata segment，跳過")
                continue
            for rule in config.get(section, []) or []:
                value = str(rule.get(rule_key) or "").strip()
                if not value:
                    continue
                if kind == "drive":
                    expected = metadata_access_fields(kind, drive_id=value)
                else:
                    expected = metadata_access_fields(kind, mailbox_email=value)
                if not _grants_non_red(expected):
                    continue  # red-only：與預設一致，無需重標

                # 先把該規則的 id 全部 materialize，讓 SELECT 走完 → 釋放
                # chroma.sqlite3 的讀鎖。若改用串流 cursor 邊 fetch 邊 update，
                # 這條唯讀 SELECT 會在 update 期間持續握著讀鎖，擋住 chroma
                # server 套用 update 的寫入 → update 逾時（實測：串流版每批
                # ReadTimeout，materialize 版正常）。Python sqlite3 的 SELECT
                # 走 autocommit，完整消耗即釋放鎖。
                rule_ids = list(
                    _iter_embedding_ids(conn, segment_id, chunk_key, value))
                scanned = updated = 0
                for start in range(0, len(rule_ids), _BATCH):
                    batch = rule_ids[start:start + _BATCH]
                    scanned += len(batch)
                    updated += _safe_apply(col, batch, expected)
                    if deadline and time.monotonic() >= deadline:
                        stopped_early = True
                        break
                total_scanned += scanned
                total_updated += updated
                if updated:
                    label = f"{rule.get('department', '')}/{value}"
                    sources.append({"source": f"{logical}:{value}",
                                    "updated": updated, "scanned": scanned})
                    log(f"[acl_reconcile] {logical} {label}: "
                        f"{updated:,}/{scanned:,} 更新"
                        f"{'（dry-run）' if dry_run else ''}")
                if stopped_early:
                    log(f"[acl_reconcile] ⏱ 時間預算用盡（{time_budget_s}s）"
                        f"，於批次邊界停手 —— 剩餘 delta 明晚續補")
                    break
        finally:
            conn.close()

    return {
        "scanned": total_scanned,
        "updated": total_updated,
        "failed_batches": failed_batches,
        "stopped_early": stopped_early,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "sources": sources,
    }
