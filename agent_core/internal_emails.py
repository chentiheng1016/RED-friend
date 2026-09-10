"""Internal / inter-department email ingestion (RAG for company knowledge base)."""

import os

from collections import defaultdict

from agent_core.dept_rules import (
    DEPT_RULES, BRAND_KEYWORDS,
    classify_dept, classify_direction, detect_brands, should_exclude,
)
from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
from agent_core.google_auth import get_service
from agent_core import internal_emails_extract as _extract
from agent_core import internal_emails_orchestrator as _orchestrator
from agent_core import internal_emails_preview as _preview
from agent_core.logging_and_paths import logger
from agent_core import internal_emails_store as _store

_INTERNAL_PARQUET = _store.INTERNAL_PARQUET
_INTERNAL_PROCESSED = _store.INTERNAL_PROCESSED
_INTERNAL_FAILED = _store.INTERNAL_FAILED


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload", {}).get("headers") or []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def preview_internal_ingest(days_back: int = 365, max_threads: int = 5000) -> str:
    return _preview.preview_internal_ingest(
        days_back=days_back,
        max_threads=max_threads,
        get_service=get_service,
        header_fn=_header,
        classify_dept=classify_dept,
        classify_direction=classify_direction,
        detect_brands=detect_brands,
        should_exclude=should_exclude,
        dept_rules=DEPT_RULES,
        brand_keywords=BRAND_KEYWORDS,
        logger=logger,
    )


# ============================================================================
# Milestone 2: Real ingestion (Gemini extraction + parquet + ChromaDB)
# ============================================================================

def _ensure_dir():
    _store.ensure_dir()


def _load_id_set(path: str) -> set:
    return _store.load_id_set(path)


def _save_id_set(path: str, s: set):
    _store.save_id_set(path, s)


def _decode_body(part: dict) -> str:
    return _extract.decode_body(part)


def _extract_message_date(msg: dict) -> str:
    return _extract.extract_message_date(msg)


def _thread_to_context(thread: dict, max_chars: int = 10000) -> dict:
    return _extract.thread_to_context(
        thread,
        header_fn=_header,
        max_chars=max_chars,
    )

def _is_prohibited_block(resp) -> bool:
    return _extract.is_prohibited_block(resp)


def _gemini_extract(ctx: dict, model: str = _extract.INGEST_MODEL, timeout_retry: int = 10) -> dict:
    return _extract.gemini_extract(
        ctx,
        gemini_generate=_gemini_generate,
        logger=logger,
        model=model,
        timeout_retry=timeout_retry,
    )


def _row_from_thread(thread: dict, extracted: dict):
    return _extract.row_from_thread(
        thread,
        extracted,
        header_fn=_header,
        should_exclude=should_exclude,
        classify_dept=classify_dept,
        classify_direction=classify_direction,
        detect_brands=detect_brands,
    )


def _upsert_to_chroma(rows: list):
    _store.upsert_to_chroma(rows)


def _append_parquet(rows: list):
    _store.append_parquet(rows)


def _fetch_thread_ids(svc, days_back: int, max_threads: int) -> list:
    return _orchestrator.fetch_thread_ids(svc, days_back, max_threads)


def _fetch_thread_latest_ts(svc, tid: str) -> tuple:
    """Gmail thread 目前的 (最新訊息 internalDate epoch ms, 訊息數)。

    threads.get(format='minimal') 一發很便宜 —— 給 orchestrator 的重抽偵測用：
    已 processed 的 thread 又來新信時，比對 parquet 快照決定要不要重抽。
    """
    thread = svc.users().threads().get(userId="me", id=tid, format="minimal").execute()
    msgs = thread.get("messages") or []
    latest_ms = max((int(m.get("internalDate", 0) or 0) for m in msgs), default=0)
    return latest_ms, len(msgs)


def _load_thread_freshness() -> dict:
    """一次載入 parquet 的 {thread_id: (last_message_date, message_count)} 對照。

    給重抽偵測用（免逐 thread 掃 parquet）。last_message_date 是
    extract_message_date 的 '%Y-%m-%d'（本地時間、日粒度）。讀不到就回 {}
    （重抽偵測本輪跳過，不影響正常 ingest）。
    """
    if not os.path.exists(_INTERNAL_PARQUET):
        return {}
    try:
        import pandas as pd

        df = pd.read_parquet(
            _INTERNAL_PARQUET,
            columns=["thread_id", "last_message_date", "message_count"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("讀 parquet freshness 失敗（%s）— 本輪跳過重抽偵測", exc)
        return {}
    out: dict = {}
    for tid, lmd, cnt in zip(df["thread_id"], df["last_message_date"], df["message_count"]):
        if tid:
            out[str(tid)] = (str(lmd or ""), cnt)
    return out


def _process_thread_id(svc, tid: str) -> tuple:
    return _extract.process_thread_id(
        tid,
        get_service=get_service,
        header_fn=_header,
        should_exclude=should_exclude,
        gemini_generate=_gemini_generate,
        classify_dept=classify_dept,
        classify_direction=classify_direction,
        detect_brands=detect_brands,
        logger=logger,
    )


def ingest_internal_emails(
    days_back: int = 3650,
    max_threads: int = 100000,
    parallel_workers: int = 2,  # LibreSSL/Python 3.9 環境保守一點，降低原生 HTTPS 崩潰風險
    flush_every: int = 20,
    retry_failed: bool = True,
) -> str:
    return _orchestrator.ingest_internal_emails(
        days_back=days_back,
        max_threads=max_threads,
        parallel_workers=parallel_workers,
        flush_every=flush_every,
        retry_failed=retry_failed,
        ensure_dir=_ensure_dir,
        load_id_set=_load_id_set,
        save_id_set=_save_id_set,
        processed_path=_INTERNAL_PROCESSED,
        failed_path=_INTERNAL_FAILED,
        parquet_path=_INTERNAL_PARQUET,
        get_service=get_service,
        fetch_thread_ids_fn=_fetch_thread_ids,
        process_thread_id_fn=_process_thread_id,
        append_parquet_fn=_append_parquet,
        logger=logger,
        fetch_thread_latest_ts_fn=_fetch_thread_latest_ts,
        load_thread_freshness_fn=_load_thread_freshness,
    )


def sample_internal_ingest(sample_size: int = 10, days_back: int = 60) -> str:
    import random

    return _orchestrator.sample_internal_ingest(
        sample_size=sample_size,
        days_back=days_back,
        get_service=get_service,
        fetch_thread_ids_fn=_fetch_thread_ids,
        process_thread_id_fn=_process_thread_id,
        shuffle_fn=random.shuffle,
    )


def ingest_status() -> str:
    return _store.ingest_status()


# ============================================================================
# Milestone 3: separate vectorization pass (parquet → ChromaDB)
# 為什麼拆開：_upsert_to_chroma 在 ingest flush 時會觸發 native-code
# SIGABRT 把整個 process 殺掉。parquet 先寫、向量化之後另跑一支 — 壞了可
# 安全重試，壞了也不會重做整夜的 Gemini 抽取。
# ============================================================================

def vectorize_internal_lake(batch_size: int = 50, skip_existing: bool = True) -> str:
    return _orchestrator.vectorize_internal_lake(
        batch_size=batch_size,
        skip_existing=skip_existing,
        parquet_path=_INTERNAL_PARQUET,
        upsert_to_chroma_fn=_upsert_to_chroma,
        logger=logger,
    )


def ingest_daily_delta(days_back: int = 3) -> str:
    return _orchestrator.ingest_daily_delta(
        days_back=days_back,
        ingest_internal_emails_fn=ingest_internal_emails,
        vectorize_internal_lake_fn=vectorize_internal_lake,
    )
