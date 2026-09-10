"""Coordinator flows for internal email ingestion."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable


def thread_needs_refresh(latest_ms, msg_count, last_message_date, parquet_msg_count,
                         *, tolerance_s: float = 3600.0) -> bool:
    """已 processed 的 thread 是否有新訊息、需要重抽。

    Gmail thread id 同串來新信不會變 —— 沒有這個判定，parquet 的
    state/summary/last_message_date 會永久凍結在首抽當下。

    比對素材（時區/格式對齊）：
      - latest_ms:      Gmail 端最新訊息 internalDate（epoch **毫秒**）。
      - msg_count:      Gmail 端訊息數。
      - last_message_date: parquet 記錄的最後訊息日 —— extract_message_date 的
        `%Y-%m-%d`（本地時間、**只有日粒度**）。
      - parquet_msg_count: parquet 記錄的 message_count。

    判定（任一成立就重抽）：
      a) 訊息數變了 —— 同一天內又來新信只有靠這條抓得到（日粒度看不出來）；
      b) 最新訊息時間晚於 parquet 記錄日的「當天結束 + tolerance_s 容差」——
         跨日新信；容差防時鐘偏移把同一封信誤判成新的一天。
    parquet 日期/計數解析不了 → 回 True（寧可重抽一次，也不要讓快照凍結）。
    """
    try:
        if int(msg_count) != int(parquet_msg_count):
            return True
    except (TypeError, ValueError):
        return True
    try:
        day_start = datetime.strptime(str(last_message_date)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return True
    try:
        latest_ms = int(latest_ms)
    except (TypeError, ValueError):
        return False
    if latest_ms <= 0:
        return False
    latest = datetime.fromtimestamp(latest_ms / 1000.0)
    return latest > day_start + timedelta(days=1, seconds=tolerance_s)


def fetch_thread_ids(svc, days_back: int, max_threads: int) -> list[str]:
    """列 thread IDs（不 fetch 內容）。Fast — 1-2 min for 20k。"""
    query = f"from:@company.example newer_than:{days_back}d"
    ids = []
    req = svc.users().threads().list(userId="me", q=query, maxResults=500)
    while req is not None and len(ids) < max_threads:
        resp = req.execute()
        for thread in (resp.get("threads") or []):
            ids.append(thread["id"])
        req = svc.users().threads().list_next(req, resp)
    return ids[:max_threads]


def ingest_internal_emails(
    *,
    days_back: int = 3650,
    max_threads: int = 100000,
    parallel_workers: int = 2,
    flush_every: int = 20,
    retry_failed: bool = True,
    ensure_dir: Callable[[], None],
    load_id_set: Callable[[str], set],
    save_id_set: Callable[[str, set], None],
    processed_path: str,
    failed_path: str,
    parquet_path: str,
    get_service: Callable[[str, str], Any],
    fetch_thread_ids_fn: Callable[[Any, int, int], list[str]],
    process_thread_id_fn: Callable[[Any, str], tuple],
    append_parquet_fn: Callable[[list[dict]], None],
    logger,
    # 重抽偵測（daily delta 用）：兩個都給才啟用。
    # fetch_thread_latest_ts_fn(svc, tid) → (最新訊息 internalDate epoch ms, 訊息數)
    # load_thread_freshness_fn() → {thread_id: (last_message_date, message_count)}（一次載入 parquet 對照）
    fetch_thread_latest_ts_fn: Callable[[Any, str], tuple] | None = None,
    load_thread_freshness_fn: Callable[[], dict] | None = None,
) -> str:
    ensure_dir()
    processed = load_id_set(processed_path)
    failed = load_id_set(failed_path)
    start_time = time.time()

    svc = get_service("gmail", "v1")
    print(f"[ingest] 列 thread IDs（近 {days_back} 天）...", flush=True)
    all_ids = fetch_thread_ids_fn(svc, days_back, max_threads)
    print(f"[ingest] 共 {len(all_ids)} 個 thread，已處理 {len(processed)}，上次失敗 {len(failed)}", flush=True)

    stale_fail = failed & processed
    if stale_fail:
        failed -= stale_fail
        save_id_set(failed_path, failed)
        print(f"[ingest] 清除 stale failed 記錄 {len(stale_fail)} 筆（已在 processed 裡）", flush=True)

    todo = [tid for tid in all_ids if tid not in processed]
    if retry_failed:
        extra = [tid for tid in failed if tid not in todo and tid not in processed]
        todo.extend(extra)

    # 已 processed、但 Gmail 端同串又有新信 → 重抽更新（append_parquet 以 thread_id
    # keep="last" 去重，重抽天然覆蓋舊快照）。只對「本輪 query 有回傳、且已 processed」
    # 的候選做（daily delta days_back=3 → 候選 ~數十筆/日，每筆一發 threads.get minimal）。
    refreshed = 0
    if fetch_thread_latest_ts_fn is not None and load_thread_freshness_fn is not None:
        candidates = [tid for tid in all_ids if tid in processed]
        if candidates:
            freshness = load_thread_freshness_fn()
            for tid in candidates:
                base = freshness.get(tid)
                if base is None:
                    # processed 但不在 parquet（被排除規則擋掉的 thread）→ 不必重抽
                    continue
                try:
                    latest_ms, msg_count = fetch_thread_latest_ts_fn(svc, tid)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("freshness check 失敗 %s: %s", tid, exc)
                    continue
                if thread_needs_refresh(latest_ms, msg_count, base[0], base[1]):
                    todo.append(tid)
                    refreshed += 1
            if refreshed:
                print(f"[ingest] 偵測到 {refreshed} 個已處理 thread 有新訊息 → 重抽更新", flush=True)

    if not todo:
        return f"✅ 所有 {len(all_ids)} 個 thread 都已處理過（processed={len(processed)}）"

    print(f"[ingest] 本輪要處理 {len(todo)} 個；parallel={parallel_workers}，每 {flush_every} 筆寫一次", flush=True)

    lock = threading.Lock()
    row_buf = []
    done_ids = []
    new_failed = []
    cleared_failed = []
    stats = {"ok": 0, "excluded": 0, "err": 0}

    def _flush():
        if row_buf:
            append_parquet_fn(row_buf)
            row_buf.clear()
        if done_ids:
            processed.update(done_ids)
            save_id_set(processed_path, processed)
            done_ids.clear()
        if new_failed or cleared_failed:
            failed.update(new_failed)
            for tid in cleared_failed:
                failed.discard(tid)
            save_id_set(failed_path, failed)
            new_failed.clear()
            cleared_failed.clear()

    try:
        with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
            futures = {executor.submit(process_thread_id_fn, svc, tid): tid for tid in todo}
            for index, future in enumerate(as_completed(futures), 1):
                tid, row, ok, err = future.result()
                with lock:
                    if ok:
                        done_ids.append(tid)
                        if tid in failed:
                            cleared_failed.append(tid)
                        if row:
                            row_buf.append(row)
                            stats["ok"] += 1
                        else:
                            stats["excluded"] += 1
                    else:
                        new_failed.append(tid)
                        stats["err"] += 1
                        logger.debug("ingest failed %s: %s", tid, err)
                    if index % flush_every == 0:
                        elapsed = time.time() - start_time
                        rate = index / max(1, elapsed) * 60
                        eta_sec = (len(todo) - index) / max(0.01, rate / 60)
                        print(
                            f"[ingest] {index}/{len(todo)}  "
                            f"ok={stats['ok']} excl={stats['excluded']} err={stats['err']}  "
                            f"rate={rate:.0f}/min  ETA={eta_sec/60:.0f}min",
                            flush=True,
                        )
                        _flush()
    finally:
        with lock:
            _flush()

    elapsed = time.time() - start_time
    total = stats["ok"] + stats["excluded"] + stats["err"]
    return (
        f"✅ ingest 完成：處理 {total} 個 thread\n"
        f"   成功入湖: {stats['ok']}\n"
        f"   依規則排除: {stats['excluded']}\n"
        f"   失敗（下次可 retry）: {stats['err']}\n"
        f"   總耗時: {elapsed/60:.1f} 分鐘\n"
        f"   parquet: {parquet_path}"
    )


def sample_internal_ingest(
    *,
    sample_size: int = 10,
    days_back: int = 60,
    get_service: Callable[[str, str], Any],
    fetch_thread_ids_fn: Callable[[Any, int, int], list[str]],
    process_thread_id_fn: Callable[[Any, str], tuple],
    shuffle_fn: Callable[[list], None],
) -> str:
    svc = get_service("gmail", "v1")
    print(f"[sample] 抓最近 {days_back} 天的 thread ...", flush=True)
    ids = fetch_thread_ids_fn(svc, days_back, max_threads=500)
    shuffle_fn(ids)
    ids = ids[:sample_size]
    print(f"[sample] 隨機挑 {len(ids)} 個跑抽取", flush=True)

    results = []
    for index, tid in enumerate(ids, 1):
        _, row, ok, err = process_thread_id_fn(svc, tid)
        print(f"[sample] {index}/{len(ids)} {tid}: {'ok' if ok else 'FAIL'} {err}", flush=True)
        if row:
            results.append(row)

    lines = [
        f"📋 Sample 抽取結果（{len(results)}/{len(ids)} 成功）",
        "=" * 60,
    ]
    for row in results[:5]:
        try:
            ents = json.loads(row["entities_json"])
            tags = ", ".join(json.loads(row["topic_tags"]))
        except Exception:
            ents, tags = {}, ""
        lines.append("")
        lines.append(f"📧 [{row['primary_dept'] or '(no dept)'} / {row['direction']}] {row['subject'][:80]}")
        lines.append(f"   {row['date']} from {row['sender'][:60]}")
        lines.append(f"   tags: {tags}")
        lines.append(f"   📝 {row['summary']}")
        if ents.get("customers"):
            lines.append(f"   客戶: {', '.join(ents['customers'][:5])}")
        if ents.get("products"):
            lines.append(f"   產品: {', '.join(ents['products'][:5])}")
        if ents.get("po_numbers"):
            lines.append(f"   PO#: {', '.join(ents['po_numbers'][:5])}")
        if ents.get("amounts"):
            lines.append(f"   金額: {', '.join(ents['amounts'][:3])}")
        if ents.get("actions"):
            lines.append(f"   行動: {'; '.join(ents['actions'][:3])[:200]}")
    lines.append("")
    lines.append("⚙️ 若抽取品質 OK，說「開始全量」跑 ingest_internal_emails()。")
    lines.append("   sample 本身不寫 processed.json，所以同一批也會被全量重跑（正常）。")
    return "\n".join(lines)


def vectorize_internal_lake(
    *,
    batch_size: int = 50,
    skip_existing: bool = True,
    parquet_path: str,
    upsert_to_chroma_fn: Callable[[list[dict]], None],
    logger,
) -> str:
    try:
        import pandas as pd
    except ImportError:
        return "需要 pandas + pyarrow：pip install pandas pyarrow"

    if not os.path.exists(parquet_path):
        return "尚無 data_lake_internal/emails.parquet — 請先跑 ingest_internal_emails()"

    try:
        df = pd.read_parquet(parquet_path)
    except Exception as exc:
        return f"讀 parquet 失敗：{exc}"
    if df.empty:
        return "parquet 是空的"

    try:
        from agent_core.memory import _get_memory_collection

        col = _get_memory_collection()
        if col is None:
            return "❌ ChromaDB 不可用（_get_memory_collection 回 None）"
    except Exception as exc:
        return f"❌ chroma 連線失敗：{exc}"

    existing_ids = set()
    if skip_existing:
        try:
            got = col.get(where={"source": "dept_email"}, limit=1000000)
            existing_ids = set(got.get("ids") or [])
            print(f"[vectorize] chromadb 已有 {len(existing_ids)} 筆 dept_email")
        except Exception as exc:
            logger.debug("chroma 撈現有 id 失敗（%s）— 視為全都要寫", exc)

    total = len(df)
    done = 0
    skipped = 0
    errs = 0
    started = time.time()
    for start in range(0, total, batch_size):
        raw_batch = df.iloc[start:start + batch_size].to_dict("records")
        raw_size = len(raw_batch)
        if skip_existing and existing_ids:
            batch_rows = [row for row in raw_batch if row["thread_id"] not in existing_ids]
        else:
            batch_rows = raw_batch
        skipped += raw_size - len(batch_rows)
        if not batch_rows:
            continue
        try:
            upsert_to_chroma_fn(batch_rows)
            done += len(batch_rows)
        except Exception as exc:
            errs += len(batch_rows)
            logger.warning("vectorize batch %d-%d 失敗：%s", start, start + batch_size, exc)
        if (start + batch_size) % (batch_size * 5) == 0:
            elapsed = time.time() - started
            rate = done / max(0.01, elapsed) * 60
            print(
                f"[vectorize] {start + batch_size}/{total}  done={done} skip={skipped} err={errs}  "
                f"rate={rate:.0f}/min",
                flush=True,
            )
    elapsed = time.time() - started
    return (
        f"✅ vectorize 完成：parquet {total} rows\n"
        f"   新 upsert: {done}\n"
        f"   已存在跳過: {skipped}\n"
        f"   失敗（重跑可再試）: {errs}\n"
        f"   耗時: {elapsed/60:.1f} 分鐘"
    )


def ingest_daily_delta(
    *,
    days_back: int = 3,
    ingest_internal_emails_fn: Callable[..., str],
    vectorize_internal_lake_fn: Callable[..., str],
) -> str:
    print(f"[daily_delta] ▶️ 開始 @ {datetime.now().isoformat(timespec='seconds')}", flush=True)
    r1 = ingest_internal_emails_fn(
        days_back=days_back,
        max_threads=1000,
        parallel_workers=1,
        flush_every=20,
        retry_failed=True,
    )
    print(r1, flush=True)
    r2 = vectorize_internal_lake_fn(batch_size=50, skip_existing=True)
    print(r2, flush=True)
    print(f"[daily_delta] ✅ 完成 @ {datetime.now().isoformat(timespec='seconds')}", flush=True)
    return r1 + "\n\n" + r2
