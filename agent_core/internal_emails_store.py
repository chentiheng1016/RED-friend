"""Storage helpers for internal email ingestion."""

from __future__ import annotations

import json
import os
from typing import Any

from agent_core.logging_and_paths import INTERNAL_LAKE_DIR, logger

INTERNAL_LAKE_DIR_COMPAT = INTERNAL_LAKE_DIR
INTERNAL_PARQUET = os.path.join(INTERNAL_LAKE_DIR_COMPAT, "emails.parquet")
INTERNAL_PROCESSED = os.path.join(INTERNAL_LAKE_DIR_COMPAT, "processed.json")
INTERNAL_FAILED = os.path.join(INTERNAL_LAKE_DIR_COMPAT, "failed.json")
INTERNAL_PROGRESS_LOG = os.path.join(INTERNAL_LAKE_DIR_COMPAT, "progress.log")


def ensure_dir():
    os.makedirs(INTERNAL_LAKE_DIR_COMPAT, exist_ok=True)


def load_id_set(path: str) -> set:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return set(json.load(handle) or [])
    except Exception:
        return set()


def save_id_set(path: str, values: set):
    ensure_dir()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(sorted(values), handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("write %s failed: %s", path, exc)


def upsert_to_chroma(rows: list[dict[str, Any]]):
    """Write rows into ChromaDB. Parquet remains the source of truth."""
    try:
        from agent_core.memory import _get_memory_collection

        col = _get_memory_collection()
        if col is None:
            return
    except Exception as exc:
        logger.debug("chroma 連線失敗：%s（跳過向量）", exc)
        return

    docs, ids, metas = [], [], []
    seen_ids = set()
    skipped = 0
    for row in rows:
        # One row with a drifted/missing field must NOT abort the whole batch.
        # Previously bracket access (row["date"], row["sender"][:100], …) raised
        # KeyError/TypeError and killed all 50 rows' vectorization at once.
        try:
            tid = row.get("thread_id")
            if not tid or tid in seen_ids:
                continue
            text_bits = [
                f"[{row.get('primary_dept') or '未分類'} / {row.get('direction') or ''}]",
                f"主旨: {row.get('subject') or ''}",
                f"摘要: {row.get('summary') or ''}",
            ]
            try:
                ents = json.loads(row.get("entities_json") or "{}")
                if ents.get("customers"):
                    text_bits.append(f"客戶: {', '.join(ents['customers'][:5])}")
                if ents.get("products"):
                    text_bits.append(f"產品: {', '.join(ents['products'][:5])}")
                if ents.get("po_numbers"):
                    text_bits.append(f"PO: {', '.join(ents['po_numbers'][:5])}")
            except (ValueError, TypeError):
                pass
            doc = "\n".join(text_bits)

            try:
                brands_flat = ",".join(json.loads(row.get("brands") or "[]")) or ""
            except Exception:
                brands_flat = ""
            meta = {
                "source": "dept_email",
                "primary_dept": row.get("primary_dept") or "",
                "direction": row.get("direction") or "",
                "brands": brands_flat,
                "date": row.get("date") or "",
                "sender": (row.get("sender") or "")[:100],
                "msg_count": int(row.get("message_count") or 0),
            }
            docs.append(doc[:8000])
            ids.append(tid)
            metas.append(meta)
            # Mark seen ONLY after the row fully succeeds. If we add earlier and
            # the row then throws, that thread_id would poison the dedup set and
            # silently drop a later well-formed duplicate of the same thread.
            seen_ids.add(tid)
        except Exception as exc:
            skipped += 1
            tid_dbg = row.get("thread_id") if hasattr(row, "get") else "?"
            logger.warning("internal_emails 向量化跳過壞 row（thread_id=%s）：%s", tid_dbg, exc)
            continue

    if skipped:
        logger.warning("internal_emails 本批跳過 %d 筆壞 row，其餘照常 upsert", skipped)

    if not docs:
        return
    try:
        col.upsert(documents=docs, ids=ids, metadatas=metas)
    except Exception as exc:
        logger.warning("chroma upsert %d docs 失敗：%s", len(docs), exc)


def append_parquet(rows: list[dict[str, Any]]):
    """Append rows into parquet, deduplicating by thread_id."""
    if not rows:
        return
    ensure_dir()
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError("需要 pandas + pyarrow：pip install pandas pyarrow")

    df_new = pd.DataFrame(rows)
    if os.path.exists(INTERNAL_PARQUET):
        try:
            df_old = pd.read_parquet(INTERNAL_PARQUET)
            df = pd.concat([df_old, df_new], ignore_index=True)
        except Exception as exc:
            logger.warning("讀既有 parquet 失敗（%s），從頭建", exc)
            df = df_new
    else:
        df = df_new
    if "thread_id" in df.columns:
        df = df.drop_duplicates(subset=["thread_id"], keep="last").reset_index(drop=True)
    df.to_parquet(INTERNAL_PARQUET, index=False)


def ingest_status() -> str:
    """Human-readable checkpoint and parquet status."""
    processed = load_id_set(INTERNAL_PROCESSED)
    failed = load_id_set(INTERNAL_FAILED)
    parquet_size = 0
    row_count = 0
    try:
        if os.path.exists(INTERNAL_PARQUET):
            import pandas as pd

            df = pd.read_parquet(INTERNAL_PARQUET)
            row_count = len(df)
            parquet_size = os.path.getsize(INTERNAL_PARQUET) / 1024 / 1024
    except Exception:
        pass
    return (
        f"📊 Internal email ingest 狀態\n"
        f"  processed (已完成) : {len(processed)}\n"
        f"  failed    (待 retry): {len(failed)}\n"
        f"  parquet rows       : {row_count}\n"
        f"  parquet size       : {parquet_size:.1f} MB"
    )
