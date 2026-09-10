from collections import Counter

# 單一定義在 agent_core.provenance；re-export 讓 recall 的呼叫端沿用短名。
# gmail_ops.send_gmail 寄出當下就把 generated_by_red 寫進記憶 metadata，比夜間
# RAG 早得多 —— 記憶庫這條路一定要自己濾一次，不能只靠 gmail_sync 那邊擋。
from agent_core.provenance import is_red_generated_meta  # noqa: F401


def remember(text: str, tags: str = "", *, index_memory_fn) -> str:
    """Round 8: text 寫入向量庫前過 sanitize_untrusted_text — 同 C7-1 防
    persistent prompt-injection。recall 路徑也會 sanitize（雙層防護），但
    在寫入端就 redact 可避免污染向量索引（embedding 不會 'learn' injection）。
    """
    try:
        from agent_core.prompt_injection import sanitize_untrusted_text
        sanitized_text = sanitize_untrusted_text(text or "")
        token = "[REDACTED-INJECTION-ATTEMPT]"
        if (sanitized_text.count(token) >= 1 and
                len(sanitized_text.replace(token, "").strip()) < 4):
            return ("❌ 拒絕記憶 — 內容主要是 prompt-injection 嫌疑。\n"
                    "   若這是合理業務筆記被誤判，請改寫描述後再試。")
        text = sanitized_text
    except Exception:
        pass
    meta = {"source": "note"}
    if tags.strip():
        meta["tags"] = tags.strip()
    doc_id = index_memory_fn(text, source="note", metadata=meta)
    if doc_id:
        return f"已存入長期記憶（id={doc_id}）"
    return "記憶失敗（向量庫未就緒，請看 log）"


# Confidence tiers — used to label each recall hit so the LLM can tell
# "字面相似但不一定相關" apart from "高度語意命中". The thresholds are
# applied to the vector cosine similarity (or the normalized BM25 score
# for bm25-only mode); RRF aggregate scores are much smaller and not
# directly comparable, so we always tier by the underlying vector sim.
_RECALL_TIER_HIGH = 0.75
_RECALL_TIER_MEDIUM = 0.50


def _recall_tier(score: float) -> tuple[str, str]:
    if score >= _RECALL_TIER_HIGH:
        return "🟢", "high"
    if score >= _RECALL_TIER_MEDIUM:
        return "🟡", "medium"
    return "🔴", "weak"


def _tier_score(item: tuple, mode: str) -> float:
    """Pick the comparable similarity score for tiering.

    For hybrid (RRF) mode we keep the underlying vector similarity in
    item[4]; the RRF aggregate at item[3] is not comparable across queries.
    For pure vector / bm25 modes the score sits at item[3].
    """
    if mode == "hybrid" and len(item) >= 5:
        return float(item[4] or 0.0)
    return float(item[3] or 0.0)


def bm25_top_hits(
    idx: dict,
    tokens: list,
    *,
    col,
    limit: int,
    source: str = "",
    fetch_docs_by_ids_fn,
) -> list:
    """從緊湊 BM25 索引取 top-N 命中，補抓全文後回 [(id, doc, meta, score), ...]。

    語義對齊舊版「全量 docs/metas 在 cache」的行為：
      - source 過濾在截斷 **之前**（跟舊版 loop 內 continue 一致）
      - 穩定排序、分數高在前（含 0 分文件 — 舊版也把 0 分文件排進 top-N）
      - build 與 fetch 之間被刪掉的 id 直接跳過
    cache 不再保存全文（記憶體紀律，見 memory._bm25_cache 註解），所以這裡
    只對 top-N（≤20/30 筆）打一次 col.get 補抓。
    """
    scores = idx["bm25"].get_scores(tokens)
    sources_arr = idx["sources"]
    ids_arr = idx["ids"]
    want_source = (source or "").strip()
    cand = [
        i
        for i in range(len(ids_arr))
        if not want_source or sources_arr[i] == want_source
    ]
    # python sort 是 stable：同分維持 collection 順序，跟舊版 list.sort 一致
    cand.sort(key=lambda i: scores[i], reverse=True)
    top = cand[: max(1, int(limit))]
    fetched = fetch_docs_by_ids_fn(col, [ids_arr[i] for i in top])
    hits = []
    for i in top:
        got = fetched.get(ids_arr[i])
        if got is None:
            continue
        doc, meta = got
        hits.append((ids_arr[i], doc, meta, float(scores[i])))
    return hits


def recall(
    query: str,
    k: int = 5,
    source: str = "",
    mode: str = "hybrid",
    min_score: float = 0.0,
    *,
    get_memory_collection_fn,
    build_bm25_index_fn,
    simple_tokenize_fn,
    fetch_docs_by_ids_fn,
    logger_obj,
    include_generated: bool = False,
) -> str:
    """搜尋長期記憶（vector / bm25 / hybrid）。

    include_generated=False（預設）會濾掉小紅自產的內容。刻意在 Python 端過濾
    而不是下 Chroma where：這裡的 col 是原始 Chroma collection，沒有經過
    vector_store._eval_where，而 Chroma 的 $ne 對「欄位不存在」是**不匹配**的
    —— 直接下 where 會讓所有舊記憶（沒有這個欄位）一起消失。
    """
    col = get_memory_collection_fn()
    if col is None:
        return "向量資料庫不可用（chromadb 載入失敗或無 API 金鑰）。"
    where = {"source": source.strip()} if source and source.strip() else None
    k = max(1, min(int(k), 20))
    mode = (mode or "hybrid").lower().strip()

    vec_hits = []
    if mode in ("hybrid", "vector"):
        try:
            res = col.query(query_texts=[query], n_results=min(k * 4, 20), where=where)
            for doc, meta, doc_id, dist in zip(
                (res.get("documents") or [[]])[0],
                (res.get("metadatas") or [[]])[0],
                (res.get("ids") or [[]])[0],
                (res.get("distances") or [[]])[0],
            ):
                sim = max(0.0, 1.0 - float(dist)) if dist is not None else 0.0
                vec_hits.append((doc_id, doc, meta, sim))
        except Exception as e:
            if mode == "vector":
                return f"向量搜尋失敗：{e}"
            logger_obj.warning("向量搜尋失敗，只用 BM25：%s", e)

    bm25_hits = []
    if mode in ("hybrid", "bm25"):
        idx = build_bm25_index_fn()
        if idx is not None:
            tokens = simple_tokenize_fn(query)
            if tokens:
                try:
                    bm25_hits = bm25_top_hits(
                        idx,
                        tokens,
                        col=col,
                        limit=max(k * 4, 20),
                        source=source,
                        fetch_docs_by_ids_fn=fetch_docs_by_ids_fn,
                    )
                except Exception as e:
                    if mode == "bm25":
                        return f"BM25 搜尋失敗：{e}"
                    logger_obj.warning("BM25 搜尋失敗，只用向量：%s", e)

    # 小紅自產內容（排程報表）預設不參與召回。兩條路都要濾：vector 走 Chroma、
    # BM25 走自建索引，任一條漏掉都會讓報表從另一條回到 LLM 面前。兩邊都已經
    # 撈了 k*4（上限 20）的池子再切 k，所以濾掉幾筆不會讓結果變空。
    if not include_generated:
        vec_hits = [h for h in vec_hits if not is_red_generated_meta(h[2])]
        bm25_hits = [h for h in bm25_hits if not is_red_generated_meta(h[2])]

    if mode == "vector":
        final = [(item[0], item[1], item[2], item[3]) for item in vec_hits[:k]]

        def label(item):
            return f"向量 {item[3]:.2f}"
    elif mode == "bm25":
        top = bm25_hits[:k]
        max_b = max((item[3] for item in top), default=1.0) or 1.0
        final = [(item[0], item[1], item[2], item[3] / max_b) for item in top]

        def label(item):
            return f"BM25 {item[3]:.2f}"
    else:
        k0 = 60
        rrf = {}
        for rank, (doc_id, doc, meta, score) in enumerate(vec_hits):
            rrf.setdefault(doc_id, {"doc": doc, "meta": meta, "score": 0.0, "v": 0.0, "b": 0.0})
            rrf[doc_id]["score"] += 1.0 / (k0 + rank)
            rrf[doc_id]["v"] = score
        for rank, (doc_id, doc, meta, score) in enumerate(bm25_hits):
            rrf.setdefault(doc_id, {"doc": doc, "meta": meta, "score": 0.0, "v": 0.0, "b": 0.0})
            rrf[doc_id]["score"] += 1.0 / (k0 + rank)
            rrf[doc_id]["b"] = score
        sorted_ids = sorted(rrf.keys(), key=lambda doc_id: rrf[doc_id]["score"], reverse=True)[:k]
        final = [
            (doc_id, rrf[doc_id]["doc"], rrf[doc_id]["meta"], rrf[doc_id]["score"], rrf[doc_id]["v"], rrf[doc_id]["b"])
            for doc_id in sorted_ids
        ]

        def label(item):
            return f"RRF {item[3]:.3f}（向量 {item[4]:.2f} / BM25 {item[5]:.1f}）"

    # Tier each surviving hit and (optionally) drop weak-similarity noise.
    # Threshold filter runs AFTER mode-specific scoring so it works for
    # vector / bm25 / hybrid uniformly.
    if min_score and min_score > 0:
        final = [item for item in final if _tier_score(item, mode) >= float(min_score)]

    if not final:
        if min_score and min_score > 0:
            return (
                f"沒找到相似度 ≥ {min_score:.2f} 的記憶。"
                f"可降低 min_score 或改用 query_email_lake / query_bom 等"
                f"結構化工具直接查。"
            )
        return "沒找到相關記憶。"

    tier_counter = Counter(_recall_tier(_tier_score(item, mode))[1] for item in final)
    high = tier_counter.get("high", 0)
    med = tier_counter.get("medium", 0)
    weak = tier_counter.get("weak", 0)
    header_breakdown = f"🟢 高信心 {high} / 🟡 中信心 {med} / 🔴 弱信心 {weak}"

    lines = [
        f"找到 {len(final)} 筆（{mode} 模式）— {header_breakdown}",
        "⚠️ 回答時記得標引用 [thread_id=xxx]，讓大王可追溯。",
    ]
    if weak and not min_score:
        lines.append(
            "⚠️ 🔴 弱信心命中只代表字面/向量相似（cosine < 0.5），"
            "**不是事實證據**。要下結論請改用 query_bom / query_email_lake "
            "等結構化工具直接查；或重跑 recall 時加 min_score=0.5 過濾。"
        )
    # Round 8 補丁（review 找到）：bare recall 路徑漏 sanitize_for_llm 蓋。
    # C4 只蓋了 rerank.format_reranked / citation / email_timeline。recall 本身
    # 也會把 doc snippet 送 LLM — attacker 透過 +確認 remember/save_memory
    # 注入 prompt-injection 文本後，下次 recall 命中即注入 LLM context。
    try:
        from agent_core.prompt_injection import sanitize_for_llm
    except Exception:
        sanitize_for_llm = lambda x: x  # noqa: E731
    for i, item in enumerate(final, 1):
        doc_id, doc, meta = item[0], item[1], item[2]
        src = (meta or {}).get("source", "?")
        ts = ((meta or {}).get("ts") or "")[:16]
        tags = (meta or {}).get("tags", "")
        snippet = sanitize_for_llm((doc or "").replace("\n", " ")[:240])
        tag_str = f" [tags={tags}]" if tags else ""
        tier_emoji, _ = _recall_tier(_tier_score(item, mode))
        # 把 thread_id 擺在**最前面**、用明顯 tag 包起來，讓 LLM 更容易 citation。
        # tier emoji 放在 thread_id 前讓信心強弱一眼可見。
        lines.append(
            f"{i}. {tier_emoji} 🆔 [thread_id={doc_id}]"
            f"  [{src} | {ts} | {label(item)}]{tag_str}\n   {snippet}"
        )
    lines.append(
        "\n💡 用 fetch_email_by_thread_id(thread_id) 看完整 email；"
        "多筆用 fetch_emails_by_thread_ids([...])。"
    )
    return "\n".join(lines)


def forget_memory(memory_id: str, *, get_memory_collection_fn) -> str:
    col = get_memory_collection_fn()
    if col is None:
        return "向量資料庫不可用。"
    try:
        col.delete(ids=[memory_id])
        return f"已刪除 {memory_id}"
    except Exception as e:
        return f"刪除失敗：{e}"


def memory_stats(*, get_memory_collection_fn, fetch_all_metadatas_fn=None) -> str:
    col = get_memory_collection_fn()
    if col is None:
        return "向量資料庫不可用。"
    try:
        total = col.count()
        if fetch_all_metadatas_fn is not None:
            metas = fetch_all_metadatas_fn() or []
        else:
            all_items = col.get(include=["metadatas"], limit=10000)
            metas = all_items.get("metadatas") or []
        src_count = Counter(meta.get("source", "?") for meta in metas)
        src_line = "、".join(f"{key}={value}" for key, value in src_count.most_common())
        return f"向量記憶庫：共 {total} 筆（{src_line}）"
    except Exception as e:
        return f"查詢失敗：{e}"
