"""Preview helpers for internal email ingestion."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Callable


def preview_internal_ingest(
    *,
    days_back: int = 365,
    max_threads: int = 5000,
    get_service: Callable[[str, str], Any],
    header_fn: Callable[[dict, str], str],
    classify_dept: Callable[[str, str], tuple[str, list[str]]],
    classify_direction: Callable[[str], str],
    detect_brands: Callable[[str], list[str]],
    should_exclude: Callable[[str, str], bool],
    dept_rules: dict,
    brand_keywords: list[str],
    logger,
) -> str:
    """Dry-run internal email report without writing parquet or calling Gemini."""
    days_back = max(1, min(int(days_back), 3650))
    max_threads = max(100, min(int(max_threads), 50000))

    try:
        svc = get_service("gmail", "v1")
    except Exception as exc:
        return f"Gmail 連線失敗：{exc}"

    query = f"from:@company.example newer_than:{days_back}d"
    print(f"[preview] Gmail query: {query}  (max_threads={max_threads})")

    threads = []
    req = svc.users().threads().list(userId="me", q=query, maxResults=500)
    while req is not None and len(threads) < max_threads:
        try:
            resp = req.execute()
        except Exception as exc:
            return f"列 thread 失敗（抓到 {len(threads)} 個後）：{exc}"
        threads.extend(resp.get("threads") or [])
        req = svc.users().threads().list_next(req, resp)
    threads = threads[:max_threads]
    print(f"[preview] 共 {len(threads)} 個 thread 要掃")

    dept_thread_counts = Counter()
    brand_thread_counts = Counter()
    direction_counts = Counter()
    unmatched_internal = Counter()
    unmatched_external = Counter()
    overlap_senders = Counter()
    excluded_count = 0
    scanned = 0
    errors = 0

    def _clean_addr(value: str) -> str:
        low = value.lower()
        if "<" in low and ">" in low:
            low = low[low.find("<") + 1:low.find(">")]
        return low.strip()

    for index, thread_ref in enumerate(threads):
        try:
            thread = svc.users().threads().get(
                userId="me",
                id=thread_ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
        except Exception as exc:
            errors += 1
            if errors < 5:
                logger.debug("preview thread get 失敗 %s: %s", thread_ref.get("id"), exc)
            continue

        messages = thread.get("messages") or []
        if not messages:
            continue
        first = messages[0]
        sender = header_fn(first, "From")
        subject = header_fn(first, "Subject")
        scanned += 1

        if should_exclude(sender, subject):
            excluded_count += 1
            continue

        direction = classify_direction(sender)
        direction_counts[direction] += 1
        primary, all_depts = classify_dept(sender, subject)

        if not primary:
            clean = _clean_addr(sender)
            if direction == "internal":
                unmatched_internal[clean] += 1
            else:
                unmatched_external[clean] += 1
            continue

        dept_thread_counts[primary] += 1
        if len(all_depts) > 1:
            overlap_senders[f"{_clean_addr(sender)} → {'/'.join(all_depts)}"] += 1

        for brand in detect_brands(subject):
            brand_thread_counts[brand] += 1

        if (index + 1) % 500 == 0:
            print(f"[preview] 已掃 {index + 1}/{len(threads)} ...")

    lines = [
        f"📊 Internal Email Ingest 乾跑報表（{datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        f"   query=`{query}`；thread 總數 {len(threads)}；成功掃 {scanned}；讀失敗 {errors}；排除 {excluded_count}",
        f"   方向：internal {direction_counts.get('internal', 0)}｜external {direction_counts.get('external', 0)}",
        "",
        "【各部門 thread 數】（以 primary_dept 計）",
    ]
    max_cnt = max(dept_thread_counts.values() or [1])
    for dept in dept_rules.keys():
        count = dept_thread_counts.get(dept, 0)
        bar = "█" * min(40, count * 40 // max_cnt)
        lines.append(f"  {dept:<6s}  {count:>6d}  {bar}")
    matched_total = sum(dept_thread_counts.values())
    lines.append(f"  {'小計':<6s}  {matched_total:>6d}  （{matched_total * 100 // max(1, scanned)}% of scanned）")

    lines.append("")
    lines.append(f"【未匹配 - 內部 sender（@company.example）】 {sum(unmatched_internal.values())} 封")
    if unmatched_internal:
        lines.append("  ⚠️ 這些是「自家人但沒貼標籤」—— 通常代表規則遺漏。前 10：")
        for addr, count in unmatched_internal.most_common(10):
            lines.append(f"    {count:>5d}  {addr}")

    lines.append("")
    lines.append(f"【未匹配 - 外部 sender】 {sum(unmatched_external.values())} 封")
    if unmatched_external:
        lines.append("  （客戶 / 供應商直接寄來且不匹配任何部門；保留當 context）；前 10：")
        for addr, count in unmatched_external.most_common(10):
            lines.append(f"    {count:>5d}  {addr}")

    lines.append("")
    lines.append(f"【同時 match 多部門（tiebreaker 啟用）】 {sum(overlap_senders.values())} 封")
    if overlap_senders:
        for desc, count in overlap_senders.most_common(10):
            lines.append(f"    {count:>5d}  {desc}")

    lines.append("")
    lines.append("【品牌偵測分佈】（subject 出現品牌名的 thread 數）")
    if brand_thread_counts:
        for brand in brand_keywords:
            count = brand_thread_counts.get(brand, 0)
            if count:
                lines.append(f"  {brand:<15s}  {count}")
        unhit = [brand for brand in brand_keywords if brand not in brand_thread_counts]
        if unhit:
            lines.append(f"  （subject 中 0 封：{', '.join(unhit)}；M2 會掃 body 應該全部露出來）")

    lines.append("")
    lines.append("⚙️ 下一步：看了報表覺得 OK，跟我說「開始 ingest」就進 Milestone 2 跑 Gemini 抽取。")
    lines.append("   覺得哪條規則不對，告訴我要加/刪哪個 sender 或 keyword。")
    return "\n".join(lines)
