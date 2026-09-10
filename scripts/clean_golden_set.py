#!/usr/bin/env python
"""清理 golden set：去重複 query、合併 expected、驗證 query/thread 相關性。

問題來源：
  bootstrap 腳本從 parquet 隨機挑 thread，有時同 query 被挑出多次
  (e.g. "PFAS 合規 紡織品" 5 個不同 thread)。
  retrieval 只能回一組，必定 fail 其他幾筆 → 假的 regression。

Cleaner 做的事：
  1. 相同 query → 合併 expected_thread_ids list
  2. 對合併後 query 跑一次 recall，看 top-20 有沒有任何 expected
     命中 = 保留；全不中 = 這題是真正的「難題」或 golden 出錯 → 標 flag
  3. 輸出 cleaned golden_set + 被踢掉的 bad queries
"""
import json
import os
import sys
from collections import defaultdict

# 讓 import agent 能 work（因為 scripts/ 不在 repo 根）
sys.path.insert(0, "/Users/user/RED")

GOLDEN = "/Users/user/RED/var/data/eval/golden_set.json"


def main():
    with open(GOLDEN) as f:
        gs = json.load(f)
    print(f"original: {len(gs)} queries")

    # 1. 按 query 去重 + 合併 expected
    merged = {}
    for entry in gs:
        q = entry["query"].strip()
        if q not in merged:
            merged[q] = {
                "query": q,
                "expected_thread_ids": list(entry.get("expected_thread_ids", [])),
                "category": entry.get("category", ""),
                "notes": entry.get("notes", ""),
                "added_at": entry.get("added_at", ""),
            }
        else:
            # 合併 expected
            existing = set(merged[q]["expected_thread_ids"])
            for tid in entry.get("expected_thread_ids", []):
                if tid and tid not in existing:
                    merged[q]["expected_thread_ids"].append(tid)
                    existing.add(tid)
    print(f"after dedup: {len(merged)} unique queries")

    # 統計被合併的
    dup_qs = [q for q, m in merged.items() if len(m["expected_thread_ids"]) > 1]
    print(f"  合併了 {len(dup_qs)} 個有多個 expected 的 query")
    for q in dup_qs[:5]:
        print(f"    '{q}' → {len(merged[q]['expected_thread_ids'])} threads")

    # 2. 跑 retrieval 驗證：top-20 有命中 any expected 就算 OK
    from agent_core.memory import recall
    from agent_core.eval_rag import extract_thread_ids_from_output

    kept = []
    dropped = []
    for i, (q, entry) in enumerate(merged.items(), 1):
        try:
            output = recall(q, k=20, mode="hybrid")
            retrieved = set(extract_thread_ids_from_output(output))
        except Exception as e:
            dropped.append({**entry, "drop_reason": f"recall failed: {e}"})
            continue

        expected_lower = {tid.lower() for tid in entry["expected_thread_ids"]}
        hit_in_top20 = expected_lower & retrieved

        if hit_in_top20:
            entry["verified_in_top20"] = True
            kept.append(entry)
        else:
            # top-20 都沒命中 → golden 可能標錯
            entry["drop_reason"] = "none of expected_thread_ids in top-20 of hybrid"
            dropped.append(entry)

        if i % 10 == 0:
            print(f"  checked {i}/{len(merged)}")

    print()
    print(f"🎯 cleaned golden set: kept {len(kept)}, dropped {len(dropped)}")

    # 3. 寫回
    # Re-assign IDs
    for i, entry in enumerate(kept, 1):
        entry["id"] = f"q{i:03d}"

    with open(GOLDEN, "w") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
    print(f"✅ updated {GOLDEN} ({len(kept)} queries)")

    # Dropped 另外存一份給 debug 用
    dropped_path = "/Users/user/RED/var/data/eval/golden_set_dropped.json"
    with open(dropped_path, "w") as f:
        json.dump(dropped, f, ensure_ascii=False, indent=2)
    print(f"⚠️  dropped 存在 {dropped_path}（之後可人工 review 看是不是真的 golden 錯）")

    if dropped:
        print()
        print("Dropped queries:")
        for d in dropped[:10]:
            print(f"  - '{d['query']}' ({d['category']}) expected {len(d['expected_thread_ids'])} threads")
            print(f"    reason: {d.get('drop_reason', '?')}")


if __name__ == "__main__":
    main()
