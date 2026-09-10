#!/usr/bin/env python3
"""gemini collection → `<logical>_bge1024` 背填（bge cutover 的資料面）。

從現行 gemini 空間 collection（drive_docs_768 / gmail_threads_768 /
xiaohong_memory_768）讀出文字＋metadata，經共用 embed server（bge-m3、
com.xiaohong.embed_server）重新嵌入，upsert 到 `_bge1024` collection。
production 查詢與背填走同一條 embed_http_client 路 — 空間不可能歪。

結構抄 scripts/reembed_gmail_768.py（實戰驗證的 3072→768 重嵌）：
  - 源直讀 chroma.sqlite3 METADATA 段（EAV、不加 ORDER BY、永不碰 HNSW 段）。
    ⚠️ chroma sqlite 是 DELETE journal（非 WAL），server 寫入會 lock 住 reader —
    實跑用 BGE_BACKFILL_SRC_DB 指到 COW clone（`cp -c` 瞬間完成、零競爭）。
  - 可續跑：跳過 dst 已有的 id（同 id 冪等 upsert）。
  - 批次退避重試，最後認賠寫 ERR_LOG，不燒整條 stream。

安全閘：預設 dry-run 只印計畫；要 `--execute` 才寫入。寫入前驗 embed server
活著、驗 dst 名字帶 _bge1024 後綴。gemini collections 全程唯讀。

用法（實跑範例，先 clone 再跑）：
  cp -c var/data/chroma_db/chroma.sqlite3 /tmp/chroma_src_clone.sqlite3
  BGE_BACKFILL_SRC_DB=/tmp/chroma_src_clone.sqlite3 \\
    .venv/bin/python scripts/backfill_bge_collections.py \\
    --logical drive_docs --execute
"""
from __future__ import annotations

import argparse
import os
import queue
import sqlite3
import sys
import threading
import time

# dst 命名走 backend=bge；在 import embedding_config 前設好。
os.environ.setdefault("RED_EMBED_BACKEND", "bge")
os.environ.setdefault("RED_EMBED_DIM", "768")  # 源 = 現行 768 空間
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import numpy as np  # noqa: E402

from agent_core import embedding_config as ec  # noqa: E402
from agent_core.chroma_backend import build_chroma_client  # noqa: E402
from agent_core.embed_http_client import embed_texts, server_alive  # noqa: E402

# RED_EMBED_BACKEND 一翻是全域的：所有走 vector_store._ef / memory EF 的
# logical collection 都要有 _bge1024 對應，缺一個 = 翻旗後該功能查空庫。
# （= vector_store._VALID_COLLECTIONS ∪ memory 的 xiaohong_memory）
_VALID_LOGICAL = (
    "drive_docs", "gmail_threads", "xiaohong_memory",
    "google_chat_messages", "operation_sops",
    "xiaohong_reflections", "skill_cards",
)
EMBED_BATCH = int(os.environ.get("BGE_BACKFILL_BATCH", "64"))
WORKERS = int(os.environ.get("BGE_BACKFILL_WORKERS", "2"))
LOG_EVERY = int(os.environ.get("BGE_BACKFILL_LOG_EVERY", "20000"))
# HNSW 建索引參數：召回重建 SOP 的 M32/efC400（見 project memory
# chroma-hnsw-recall-rebuild — 預設 M16/efC100 在百萬級 collection 召回劣化）。
_DST_METADATA = {"hnsw:space": "cosine", "hnsw:M": 32, "hnsw:construction_ef": 400}


def _src_db_path() -> tuple[str, bool]:
    """(sqlite 路徑, 是否 COW clone)。live 路徑能跑 smoke，實跑要 clone。"""
    override = os.environ.get("BGE_BACKFILL_SRC_DB", "").strip()
    if override:
        return override, True
    return os.path.join(_REPO, "var/data/chroma_db/chroma.sqlite3"), False


def _gemini_physical(logical: str) -> str:
    """源 collection 名（gemini 空間、依 RED_EMBED_DIM），不受本腳本設的
    RED_EMBED_BACKEND=bge 影響。"""
    return ec._physical_name(logical, "gemini", ec._gemini_dim())


def _metadata_segment_id(con: sqlite3.Connection, coll_name: str) -> str:
    row = con.execute(
        "SELECT id FROM collections WHERE name=?", (coll_name,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"源 collection {coll_name!r} 不在 sqlite 裡（路徑對嗎？）")
    return con.execute(
        "SELECT id FROM segments WHERE collection=? AND scope='METADATA'", (row[0],)
    ).fetchone()[0]


def _iter_rows(con: sqlite3.Connection, seg: str):
    """Stream (embedding_id, metadata-dict)。抄 reembed_gmail_768（實戰驗證）：
    不加 ORDER BY — nested-loop plan 天然讓同一 embedding 的 metadata 列
    contiguous，加了會 TEMP B-TREE 排幾千萬列、首列等數十分鐘。document 在
    key='chroma:document'，呼叫端 pop。"""
    sql = (
        "SELECT e.id, e.embedding_id, em.key, "
        "       em.string_value, em.int_value, em.float_value, em.bool_value "
        "FROM embeddings e JOIN embedding_metadata em ON em.id=e.id "
        "WHERE e.segment_id=?"
    )
    cur_id = None
    cur_eid = None
    meta: dict = {}
    for row_id, embedding_id, key, sv, iv, fv, bv in con.execute(sql, (seg,)):
        if row_id != cur_id:
            if cur_id is not None:
                yield cur_eid, meta
            cur_id, cur_eid, meta = row_id, embedding_id, {}
        if sv is not None:
            meta[key] = sv
        elif bv is not None:
            meta[key] = bool(bv)
        elif iv is not None:
            meta[key] = iv
        elif fv is not None:
            meta[key] = fv
    if cur_id is not None:
        yield cur_eid, meta


def iter_pending_batches(rows, done: set[str], batch_size: int):
    """rows=(eid, meta) 串流 → 過濾已完成/無文字後打包 (ids, docs, metas) 批。
    純函式（測試點）：skip 規則 = id 已在 dst 或 document 空。"""
    ids_b: list[str] = []
    docs_b: list[str] = []
    metas_b: list[dict] = []
    for eid, meta in rows:
        doc = meta.pop("chroma:document", None)
        if eid in done or not doc or not str(doc).strip():
            continue
        ids_b.append(eid)
        docs_b.append(doc)
        metas_b.append(meta)
        if len(ids_b) >= batch_size:
            yield ids_b, docs_b, metas_b
            ids_b, docs_b, metas_b = [], [], []
    if ids_b:
        yield ids_b, docs_b, metas_b


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--logical", required=True, choices=_VALID_LOGICAL)
    ap.add_argument("--execute", action="store_true",
                    help="真的寫入 dst（預設 dry-run 只印計畫）")
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 筆（smoke 測）")
    args = ap.parse_args()

    src_name = _gemini_physical(args.logical)
    dst_name = ec.physical_collection_name(args.logical)
    if not dst_name.endswith(f"_{ec.BGE_COLLECTION_SUFFIX}"):
        raise SystemExit(f"dst {dst_name!r} 沒帶 _bge1024 後綴 — env 被污染？拒跑")

    db_path, is_clone = _src_db_path()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    seg = _metadata_segment_id(con, src_name)
    total = con.execute(
        "SELECT count(*) FROM embeddings WHERE segment_id=?", (seg,)
    ).fetchone()[0]

    print(f"[plan] src={src_name}（{total} 筆、sqlite={'clone' if is_clone else 'LIVE'}）"
          f" → dst={dst_name}  batch={EMBED_BATCH} workers={WORKERS}", flush=True)
    print(f"[plan] 估時：{total} ÷ ~33 docs/s ≈ {total / 33 / 3600:.1f}h（M5 fp16 實測）",
          flush=True)
    if not is_clone:
        print("[plan] ⚠️ 讀的是 LIVE chroma.sqlite3 — server 寫入會 lock 住本腳本；"
              "實跑請 cp -c 出 COW clone 再以 BGE_BACKFILL_SRC_DB 指過去", flush=True)
    if not args.execute:
        print("[plan] dry-run 結束（要寫入加 --execute）", flush=True)
        return 0

    if not server_alive():
        raise SystemExit("embed server 沒回 healthz — 先起 com.xiaohong.embed_server"
                         "（bin/embed-server-venv → redeploy-daemons embed_server -f）")

    client = build_chroma_client("")
    dst = client.get_or_create_collection(name=dst_name, metadata=_DST_METADATA)
    done: set[str] = set()
    try:
        done = set(dst.get(include=[])["ids"])
    except Exception as e:  # noqa: BLE001
        print(f"[resume] 讀 dst 既有 id 失敗（當空集處理）：{e}", flush=True)
    print(f"[init] dst 已有 {len(done)} 筆（跳過）", flush=True)

    err_log = os.path.join(_REPO, f"var/logs/backfill_bge_{args.logical}.errors")
    q: queue.Queue = queue.Queue(maxsize=WORKERS * 3)
    stats = {"embed": 0, "err": 0}
    lock = threading.Lock()
    t0 = time.time()

    def worker() -> None:
        while True:
            item = q.get()
            try:
                if item is None:
                    return
                ids, docs, metas = item
                for _attempt in range(5):
                    try:
                        vecs = embed_texts(docs, kind="document")
                        dst.upsert(
                            ids=ids,
                            embeddings=np.asarray(vecs, dtype="float32"),
                            documents=docs,
                            metadatas=metas,
                        )
                        with lock:
                            stats["embed"] += len(ids)
                        break
                    except Exception as e:  # noqa: BLE001
                        if _attempt < 4:
                            time.sleep(min(60, 10 * (_attempt + 1)))
                            continue
                        with lock:
                            stats["err"] += len(ids)
                        try:
                            with open(err_log, "a", encoding="utf-8") as fh:
                                fh.write("\t".join(ids)
                                         + f"\t{type(e).__name__}:{e}\n")
                        except Exception:  # noqa: BLE001
                            pass
                        print(f"[worker] batch err ({len(ids)}) after retries："
                              f"{type(e).__name__}: {e}", flush=True)
            finally:
                q.task_done()

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for w in workers:
        w.start()

    seen = 0
    for batch in iter_pending_batches(_iter_rows(con, seg), done, EMBED_BATCH):
        q.put(batch)
        seen += len(batch[0])
        if args.limit and seen >= args.limit:
            break
        if seen % LOG_EVERY < EMBED_BATCH:
            with lock:
                emb, err = stats["embed"], stats["err"]
            rate = emb / max(1e-6, time.time() - t0)
            eta_h = max(0, total - len(done) - emb) / max(1e-6, rate) / 3600
            print(f"[{time.strftime('%H:%M:%S')}] embedded={emb} err={err} "
                  f"rate={rate:.0f}/s ETA~{eta_h:.1f}h", flush=True)

    q.join()
    for _ in workers:
        q.put(None)
    final = dst.count()
    print(f"✅ 完成：embedded={stats['embed']} err={stats['err']} "
          f"dst.count={final} / src={total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
