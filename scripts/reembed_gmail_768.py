#!/usr/bin/env python3
"""記憶體安全的 gmail_threads → 768 重 embed（Phase 2 收尾 gmail 缺口）。

為什麼不能像其他 collection 那樣截斷現有向量：
  gmail 的 3072 HNSW 段 48GB > 32GB RAM，chroma server 一載入該段就 OOM 被 SIGKILL
  （連 get(limit=2) 都會載段）；且 gmail 向量已從 embeddings_queue 修剪、只存在 HNSW
  binary（hnswlib 載入也要 48GB RAM）。所以唯一記憶體安全的完成路 = 從『文字』重 embed。

做法：
  1. 直讀 chroma.sqlite3 的 METADATA 段（純 sqlite EAV，永不碰 48GB HNSW binary）
     → (embedding_id, document, metadata-dict)，型別照原 column 還原（bool/int/float/str）
  2. Gemini 重算 768（RED_EMBED_DIM=768 → output_dimensionality=768 + L2 normalize，
     與查詢同空間；native-768 == truncate(3072) 正規化後 cos=1.0）
  3. upsert 到 gmail_threads_768（gemini EF、顯式 768 向量）；server 只寫小的 768 段、不 OOM

可續跑：跳過 dst 已有的 id。--fresh 先 drop 重建（首跑用，清掉 /tmp 舊腳本留下的 default-EF
半成品）。多 worker 並行 embed（Gemini API 是瓶頸），upsert 由 server 串行化。
"""
from __future__ import annotations

import argparse
import os
import queue
import sqlite3
import sys
import threading
import time

# RED_EMBED_DIM 必須在 import embedding_config / vector_store 前設好
os.environ.setdefault("RED_EMBED_DIM", "768")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import numpy as np  # noqa: E402

from agent_core.chroma_backend import build_chroma_client  # noqa: E402
from agent_core.embedding_config import physical_collection_name  # noqa: E402
from agent_core.ingest import vector_store as vs  # noqa: E402

# 預設讀 live DB，但 chroma sqlite 是 DELETE journal mode（非 WAL）→ server 寫入時會 lock
# 住所有 reader。實戰會卡死。所以實跑要 REEMBED_SRC_DB 指到 COW clone（cp -c 瞬間、零競爭）。
DB = os.environ.get("REEMBED_SRC_DB") or os.path.join(_REPO, "var/data/chroma_db/chroma.sqlite3")
SRC_COLL = "gmail_threads"
EMBED_BATCH = int(os.environ.get("REEMBED_BATCH", "96"))
WORKERS = int(os.environ.get("REEMBED_WORKERS", "4"))
LOG_EVERY = int(os.environ.get("REEMBED_LOG_EVERY", "20000"))
ERR_LOG = os.path.join(_REPO, "var/logs/reembed_gmail_768.errors")


def _metadata_segment_id(con: sqlite3.Connection, coll_name: str) -> str:
    cid = con.execute(
        "SELECT id FROM collections WHERE name=?", (coll_name,)
    ).fetchone()[0]
    return con.execute(
        "SELECT id FROM segments WHERE collection=? AND scope='METADATA'", (cid,)
    ).fetchone()[0]


def _iter_rows(con: sqlite3.Connection, seg: str):
    """Stream (embedding_id, metadata-dict) from sqlite, grouped by embeddings.id.

    一個 embedding 的多個 metadata key 在結果中連續 → group。document 也在 metadata 裡
    （key='chroma:document'），由呼叫端 pop 出來。型別照非 NULL 的 column 還原。

    **不加 ORDER BY e.id**：plan 是 nested-loop（SEARCH e by segment_id index = outer，
    SEARCH em by id index = inner），每個 e 的所有 em 列天然 contiguous emit，grouping 靠
    連續即可。加 ORDER BY 會 USE TEMP B-TREE 排 62M 列、首列要等數十分鐘（實戰卡 30min）。
    """
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true",
                    help="先 drop 重建 dst（首跑用，清掉舊 default-EF 半成品）")
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 筆（smoke 測）")
    args = ap.parse_args()

    dst_name = os.environ.get("REEMBED_DST") or physical_collection_name(SRC_COLL)
    assert dst_name.startswith("gmail_threads_768"), dst_name
    client = build_chroma_client("")

    if args.fresh:
        try:
            print(f"[fresh] 刪除 {dst_name}（1.38M、chroma delete 同步但很慢、可能 ~8min，"
                  f"勿中斷 client 否則 server 端 rollback 留殘骸）…", flush=True)
            client.delete_collection(dst_name)
        except Exception as e:  # noqa: BLE001
            print(f"[fresh] delete raised（可能本來就沒有）：{e}", flush=True)
        # 確認真的消失：殘骸（default-EF）會讓下面 get_or_create(gemini EF) 撞衝突 → 必須先確認。
        for _ in range(150):  # up to ~25min
            try:
                if dst_name not in [c.name for c in client.list_collections()]:
                    print(f"[fresh] 確認 {dst_name} 已刪除", flush=True)
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(10)
        else:
            raise RuntimeError(f"{dst_name} 25min 內刪不掉，放棄（人工查 chroma server）")
    dst = client.get_or_create_collection(
        name=dst_name, embedding_function=vs._ef, metadata={"hnsw:space": "cosine"}
    )

    done: set[str] = set()
    if not args.fresh:
        try:
            done = set(dst.get(include=[])["ids"])
        except Exception as e:  # noqa: BLE001
            print(f"[resume] 讀 dst 既有 id 失敗（當空集處理）：{e}", flush=True)
    print(f"[init] dst={dst_name} 已有 {len(done)} 筆（會跳過）；"
          f"workers={WORKERS} batch={EMBED_BATCH}", flush=True)

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    seg = _metadata_segment_id(con, SRC_COLL)
    total = con.execute(
        "SELECT count(*) FROM embeddings WHERE segment_id=?", (seg,)
    ).fetchone()[0]
    print(f"[init] src {SRC_COLL} METADATA 段 {total} 筆", flush=True)

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
                # 網路 blip（DNS/socket）會讓整批失敗；退避重試 5 次（~10/20/30/40s）讓短暫
                # 斷線騎過去、不要把整條 stream 燒成 err，最後才認賠寫 ERR_LOG。持久 wedge
                # （client 連線池壞掉、網路回來也不復原）仍靠 watchdog 偵測 + 重啟新 process。
                for _attempt in range(5):
                    try:
                        embs = vs._gemini_embed(docs, "RETRIEVAL_DOCUMENT")
                        dst.upsert(
                            ids=ids,
                            embeddings=np.asarray(embs, dtype="float32"),
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
                            with open(ERR_LOG, "a", encoding="utf-8") as fh:
                                fh.write("\t".join(ids) + f"\t{type(e).__name__}:{e}\n")
                        except Exception:  # noqa: BLE001
                            pass
                        print(f"[worker] batch err ({len(ids)}) after retries："
                              f"{type(e).__name__}: {e}", flush=True)
            finally:
                q.task_done()

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for w in workers:
        w.start()

    ids_b: list[str] = []
    docs_b: list[str] = []
    metas_b: list[dict] = []
    seen = 0
    skipped = 0

    def log_progress() -> None:
        with lock:
            emb = stats["embed"]
            err = stats["err"]
        rate = emb / max(1e-6, time.time() - t0)
        remaining = max(0, total - len(done) - emb)
        eta_h = remaining / max(1e-6, rate) / 3600
        print(f"[{time.strftime('%H:%M:%S')}] embedded={emb} err={err} "
              f"skip={skipped} rate={rate:.0f}/s ETA~{eta_h:.1f}h", flush=True)

    for eid, meta in _iter_rows(con, seg):
        if args.limit and seen >= args.limit:
            break
        seen += 1
        doc = meta.pop("chroma:document", None)
        if eid in done or not doc:
            skipped += 1
            continue
        ids_b.append(eid)
        docs_b.append(doc)
        metas_b.append(meta)
        if len(ids_b) >= EMBED_BATCH:
            q.put((ids_b, docs_b, metas_b))
            ids_b, docs_b, metas_b = [], [], []
            if (stats["embed"] + skipped) and seen % LOG_EVERY < EMBED_BATCH:
                log_progress()
    if ids_b:
        q.put((ids_b, docs_b, metas_b))

    q.join()
    for _ in workers:
        q.put(None)
    log_progress()
    final = dst.count()
    print(f"✅ 完成：embedded={stats['embed']} err={stats['err']} skip={skipped} "
          f"dst.count={final} / src={total}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
