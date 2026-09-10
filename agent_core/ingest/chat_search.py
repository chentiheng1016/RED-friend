"""LLM-callable semantic search over indexed Google Chat history.

The mirror image of drive_search for Google Chat: chat_sync backs up every
Workspace user's Chat spaces daily and ingests the *new* messages into the
`google_chat_messages` ChromaDB collection. Without this tool that index is
write-only — the data is captured but unreachable to the assistant, so "誰在
群組講過 X" / "某專案在 chat 上的結論" still gets answered from email or from
nothing. This closes the down-half of the loop.

Each ingested chunk is a run of `[time] sender: body` lines prefixed with the
space label (see chat_sync.ingest_space_messages), so a single hit already
carries who-said-what-and-when; the metadata adds the space, its type, and the
batch's latest message time for recency ranking.

Access (two layers, defense-in-depth):
  1. `access_where`: chat chunks are stamped owner-only (`access_red`) at
     ingest, so the RED owner channel sees everything while a departmental
     caller's filter (`access_<color>`) matches nothing — same gate drive/gmail
     recall enforce.
  2. OWNER_PRIVATE_READ_TOOLS: this tool is also removed wholesale from
     non-owner Telegram actor sessions. The conversational separation path
     filters tools by removal and does NOT reliably set the rag caller, so a
     non-owner invocation would fall back to `current_request_caller()` ==
     Agent.RED and read ALL chat. Unlike search_drive_docs (company data behind
     allowed_colors), chat has no per-color ACL — it is owner-private comms
     (cross-dept groups, DMs, the owner's own threads), so it is fail-closed for
     non-owners rather than relying on the caller being set.
"""
from __future__ import annotations

from typing import Any

# 不在 module top import vector_store（連帶拉整個 chromadb ~208ms + 數十 MB 常駐）。
# catalog 為註冊 search_google_chat eager import 本模組，但多數 daemon 啟動不查
# RAG，延後到 search_google_chat 首次呼叫才 import。與 drive_search 同款處理。
from agent_core.rag_gateway import (
    access_where,
    current_request_caller,
    current_request_trace_id,
    log_rag_access_event,
)

_COLLECTION = "google_chat_messages"
# Chat chunks pack several `[ts] sender: body` lines, so allow a bit more than
# drive's 600 — a single hit is a small conversation excerpt, not one sentence.
_MAX_CHUNK_CHARS = 700


def _format_hits(query: str, hits: list[dict[str, Any]], *, prefer_recent: bool = False) -> str:
    # Chat messages are user-generated and untrusted — a colleague (or an
    # outside guest in a space) can paste prompt-injection text or a secret.
    # Sanitize the space label and the body before they reach the LLM, exactly
    # as drive/email recall do.
    from agent_core.prompt_injection import sanitize_for_llm

    head = f"找到 {len(hits)} 筆相關 Google Chat 對話（query: {query!r}）"
    if prefer_recent:
        head += "（已套用時間加權：新訊息優先）"
    lines = [head, ""]
    for i, h in enumerate(hits, 1):
        meta = h.get("metadata") or {}
        label = sanitize_for_llm(str(meta.get("display_name") or "(未命名 space)"))
        chunk_idx = meta.get("chunk_index", 0)
        # ChromaDB returns cosine distance (0 = identical); flip to similarity
        # so the LLM reads "0.87 similar" instead of "0.13 distant". Guard a
        # missing/None distance — float(None) would TypeError; treat as farthest.
        dist_val = h.get("distance")
        dist = float(dist_val) if dist_val is not None else 1.0
        sim = max(0.0, 1.0 - dist)
        # last_message_time = 這批新訊息裡最新一則的時間，當證據新舊判斷
        # （chat ingest 不存 per-chunk modifiedTime，這是最接近的時間戳）。
        when = str(meta.get("last_message_time") or "")[:10]
        space_type = str(meta.get("space_type") or "")
        provenance: list[str] = []
        if when:
            provenance.append(f"最後訊息={when}")
        if space_type:
            provenance.append(f"類型={space_type}")
        if meta.get("space_name"):
            # space resource id（spaces/AAQA…）— 不透明但唯一，方便對帳
            provenance.append(f"space={meta['space_name']}")
        provenance.append(f"chunk={chunk_idx}")
        date_suffix = f"（{when}）" if when else ""
        rec = h.get("_recency")
        rec_suffix = f"  新鮮度={rec['freshness']:.2f}" if rec else ""
        lines.append(f"【{i}】 sim={sim:.2f}{rec_suffix}  {label}{date_suffix}")
        lines.append(f"    {' / '.join(provenance)}")
        text = (h.get("text") or "").strip()
        truncated = len(text) > _MAX_CHUNK_CHARS
        if truncated:
            text = text[:_MAX_CHUNK_CHARS]
        # sanitize first; appending the ellipsis afterwards keeps it intact
        # (sanitize_for_llm runs NFKC which would fold '…' → '...').
        text = sanitize_for_llm(text)
        if truncated:
            text = text + "…"
        lines.append("    " + text.replace("\n", "\n    "))
        lines.append("")
    return "\n".join(lines).rstrip()


def search_google_chat(query: str, k: int = 5, prefer_recent: bool = False) -> str:
    """搜尋已索引的 Google Chat 對話紀錄（公司 Workspace 聊天室／群組／私訊）。

    每天 rag_sync 會把全公司各 space 的新訊息備份進向量庫；這個工具用語意
    向量搜尋，找出「誰在哪個聊天室講過什麼」並回傳對話片段。被問到 chat 上
    的討論、群組決議、某人在聊天室提到的事，**先呼叫這個工具查一下**再回答，
    查不到再老實說查不到，**不要憑記憶亂編避免幻覺**。

    跟 search_gmail（查 email）、search_drive_docs（查 Drive 檔）並列：這個
    專查 Google Chat 即時通訊的歷史。

    query: 自然語言問題，例如「樣品室群組討論的交期」、「UserS 在 chat 提的報價」
    k:     回傳前幾筆相關結果（1-20，預設 5）
    prefer_recent: 選填，預設 False＝純語意排序。問句帶「最新／最近／目前／剛剛／
           這幾天」這類重視新舊的意圖時設 True：先撈較大候選池，再用「語意相似度 ×
           訊息新鮮度（last_message_time 指數衰減）」重排，讓最近的對話優先。

    回傳：人類可讀的 markdown，每筆含 space 名稱、最後訊息日期、相似度分數，
    以及對話片段（內含每則訊息的時間與發話者）。若 collection 還是空的
    （備份 daemon 還沒跑完）會明確告訴你。
    """
    q = (query or "").strip()
    if not q:
        return "錯誤：query 不能為空。"

    # k 通常是 int，但 LLM / API gateway 偶爾把它當浮點字串傳（"5.0"）→
    # int("5.0") 會 ValueError；先過 float 再 int，壞值退回預設 5。
    try:
        n = max(1, min(20, int(float(k))))
    except (ValueError, TypeError):
        n = 5

    from agent_core.ingest.vector_store import get_store
    store = get_store(_COLLECTION)
    if store.is_empty():  # 便宜的空集合判斷（別用 count()==0：大段全掃拖慢每次查詢）
        return (
            "google_chat_messages collection 還是空的——可能 chat 備份還沒跑完，"
            "或同步流程出錯。\n"
            "用 `tail -200 ~/RED/var/logs/daemon-rag_sync.log` 查最近一次 sync。"
        )

    caller = current_request_caller()
    # No base_where: chat 不像 drive 有 drive_id/folder_id 可 scope；ACL 是
    # 唯一的過濾條件（RED 看全部、部門色看不到沒授權的 chat）。
    filtered_where = access_where(caller)
    # 排序信號鏈同 drive_search：prefer_recent 新鮮度 blend + citation
    # feedback 微幅引用加權（chat chunk 的 doc_id 就是 space_name，
    # 對齊回覆裡 space=<spaces/...> 的引用 token）。預設路徑維持
    # 「純語意、正好取 n 筆」契約——引用加權只在回傳集合內重排、不擴池。
    if prefer_recent:
        from agent_core.ingest.recency import pool_size, rerank_by_recency
        pool = store.query(q, n_results=pool_size(n), where=filtered_where)
        hits = rerank_by_recency(pool, "last_message_time", max(1, len(pool)))
    else:
        hits = store.query(q, n_results=n, where=filtered_where)
    from agent_core.citation_feedback import (
        citation_feedback_enabled, note_retrieved_keys, rerank_with_citations,
    )
    if citation_feedback_enabled():
        hits = rerank_with_citations(hits, n)
        note_retrieved_keys([
            str((h.get("metadata") or {}).get("doc_id") or "") for h in hits
        ])
    else:
        hits = hits[:n]
    log_rag_access_event(
        caller=caller,
        collection=_COLLECTION,
        query=q,
        status="ok",
        n_results=n,
        hit_count=len(hits),
        where=filtered_where,
        trace_id=current_request_trace_id(),
    )
    if not hits:
        return f"沒找到跟「{q}」相關的 Google Chat 對話。"

    return _format_hits(q, hits, prefer_recent=prefer_recent)
