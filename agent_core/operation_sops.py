"""操作 SOP 知識庫 — 從 ERP 教學影片深度分析產出的逐步操作 SOP。

存在獨立的 `operation_sops` ChromaDB collection（**刻意**跟 drive_docs 分開：
drive_sync 每日 sync_all_drives 會把 drive_docs 裡「不在 Drive 清單」的 doc_id
清掉，手動寫的 SOP 放 drive_docs 會被夜跑清除；獨立 collection 不在 reconcile
範圍內，安全）。

- `ingest_operation_sop()`：把一支影片整理出的 SOP 切塊、嵌入、寫入（給批次腳本/
  未來的「學會這支影片」工具用）。
- `search_operation_sops()`：給 LLM 的查詢工具，問「某操作怎麼做」直接命中影片步驟。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

# 不在 module top import vector_store（連帶拉整個 chromadb，~208ms + 數十 MB 常駐）。
# catalog 為註冊 search_operation_sops eager import 本模組，但多數 daemon 啟動不查
# SOP，延後到首次呼叫才 import。與 drive_search/chat_search 同款（#182）。
from agent_core.rag_gateway import (
    access_where,
    current_request_caller,
    current_request_trace_id,
    log_rag_access_event,
    metadata_access_fields,
)

_COLLECTION = "operation_sops"
_MAX_CHUNK_CHARS = 700  # 每筆命中內容上限，避免 k 筆塞爆 LLM context

# SOP 文字裡的時間戳標記 — 抽 chunk 級 start_s 溯源用。半形 [mm:ss] 與全形
# 【mm:ss】都收（deep 模式的 SOP 整合 prompt 用 **【時間】** 格式輸出）；
# [h:mm:ss] 亦可；冒號半形/全形（【00：58】）皆可；時間**區間**
# （【00:58–01:30】，dash 各款＋~〜至）取起始時間。
_TS_MARK_RE = re.compile(
    r"[\[【](?:(\d{1,2})[:：])?(\d{1,2})[:：](\d{2})"
    r"(?:\s*[–\-~〜～至]\s*(?:\d{1,2}[:：])?\d{1,2}[:：]\d{2})?[\]】]"
)


def _first_timestamp_s(text: str) -> int | None:
    """chunk 文字裡第一個 [mm:ss]/[h:mm:ss] 標記換算秒數；沒有回 None。"""
    m = _TS_MARK_RE.search(text or "")
    if not m:
        return None
    h, mm, ss = m.groups()
    return (int(h) if h else 0) * 3600 + int(mm) * 60 + int(ss)


def ingest_operation_sop(
    video_id: str,
    video_name: str,
    sop_text: str,
    department: str = "",
    *,
    confidence: float | None = None,
    tier: str = "",
    asr_engine: str = "",
) -> dict[str, Any]:
    """把一支教學影片深析出的 SOP 文字切塊、嵌入、寫進 operation_sops collection。

    doc_id 用 `sop_<video_id>` 穩定鍵 → 同一支影片重跑覆蓋同 chunk_id、不重複堆積。
    回 {"ok": bool, "doc_id", "chunks", ...}。

    選填品質欄位（都給了才寫進 metadata，舊呼叫端行為不變）：
    - confidence / tier：這份 SOP 的產出信度（供查詢端分級過濾）
    - asr_engine：旁白轉錄引擎（例 "mlx-whisper"，追溯管線版本用）
    另外每個 chunk 會抽文字裡第一個 [mm:ss] 標記存 start_s — 搜尋命中後
    能回指影片時間點。
    """
    from agent_core.ingest.drive_sync import _chunk_text

    text = (sop_text or "").strip()
    vid = (video_id or "").strip()
    if not text or not vid:
        return {"ok": False, "reason": "empty_text_or_video_id"}

    name = (video_name or vid).strip()
    title = f"[操作SOP] {name}"
    # 用影片名當 chunk 前綴，讓 embedding 帶文件級語境（同 drive_sync 慣例）。
    chunks = _chunk_text(text, title=name)
    if not chunks:
        return {"ok": False, "reason": "no_chunks"}

    doc_id = f"sop_{vid}"
    now = datetime.now(timezone.utc).isoformat()
    today = now[:10]
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    access = metadata_access_fields("sop", owner_color="red", department=department)

    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, Any]] = []
    quality: dict[str, Any] = {}
    if confidence is not None:
        try:
            quality["confidence"] = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            pass
    if (tier or "").strip():
        quality["tier"] = tier.strip()
    if (asr_engine or "").strip():
        quality["asr_engine"] = asr_engine.strip()

    for i, chunk in enumerate(chunks):
        ids.append(f"{doc_id}__c{i}")
        docs.append(chunk)
        meta: dict[str, Any] = {
            "doc_id": doc_id,
            "title": title,
            "mime_type": "text/x-operation-sop",
            "chunk_index": i,
            "synced_at": now,
            "modified_time": today,
            "content_hash": content_hash,
            "video_id": vid,
            "video_name": name,
            "sync_complete": True,
            **quality,
            **access,
        }
        start_s = _first_timestamp_s(chunk)
        if start_s is not None:
            meta["start_s"] = start_s
        metas.append(meta)

    from agent_core.ingest.vector_store import get_store
    store = get_store(_COLLECTION)
    store.upsert_batch(ids, docs, metas)
    # 同影片重灌且新版 chunks 較少時，upsert 只覆蓋同 id — 舊的高編號 chunk 會
    # 殘留成「檔尾舊版內容」（2026-07-06 重學 13 支實際踩到：6 支殘留 9 塊）。
    store.delete_stale_chunks(doc_id, len(chunks))
    return {"ok": True, "doc_id": doc_id, "chunks": len(chunks),
            "department": department, "title": title}


def _format_hits(query: str, hits: list[dict[str, Any]]) -> str:
    # SOP 內容源頭是影片（外部資料）— 跟 drive_search 同規格先淨化再回。
    from agent_core.prompt_injection import sanitize_for_llm

    lines = [f"找到 {len(hits)} 筆相關操作 SOP（query: {query!r}）", ""]
    for i, h in enumerate(hits, 1):
        meta = h.get("metadata") or {}
        title = sanitize_for_llm(str(meta.get("title", "(no title)")))
        dept = str(meta.get("department") or "")
        vid = str(meta.get("video_id") or "")
        sim = max(0.0, 1.0 - float(h.get("distance", 1.0)))
        prov: list[str] = []
        if dept:
            prov.append(f"部門={dept}")
        prov.append(f"chunk={meta.get('chunk_index', 0)}")
        if vid:
            prov.append(f"影片id={vid}")
        lines.append(f"【{i}】 sim={sim:.2f}  {title}")
        lines.append(f"    {' / '.join(prov)}")
        text = (h.get("text") or "").strip()
        truncated = len(text) > _MAX_CHUNK_CHARS
        if truncated:
            text = text[:_MAX_CHUNK_CHARS]
        text = sanitize_for_llm(text)
        if truncated:
            text = text + "…"
        lines.append("    " + text.replace("\n", "\n    "))
        lines.append("")
    return "\n".join(lines).rstrip()


def search_operation_sops(query: str, k: int = 5) -> str:
    """查「某個 ERP 操作怎麼做」——從教學影片整理出的逐步操作 SOP 知識庫。

    內容是把教育訓練影片（採購下單、業務訂單、生管指令單、領料、收櫃出櫃、MRP…）
    深度分析後逐欄位逐步驟整理出的 SOP（含畫面代碼、欄位名＝填入值、按鈕、易錯點）。
    被問到「XX 怎麼操作 / XX 系統怎麼建 / XX 流程有哪些步驟」時**先查這個工具**，
    查到就照步驟回答並標來源影片；查不到再老實說沒有、**別憑記憶亂編**。

    跟 search_drive_docs 不同：那個查 Drive 原始文件，這個查影片整理出的操作步驟。

    query: 自然語言問題，例如「收櫃資料怎麼建立」「採購單怎麼開」「指令單到領料的流程」
    k:     回傳前幾筆相關結果（1-20，預設 5）

    回傳：markdown，每筆含 SOP 標題、相似度、部門、來源影片 id、步驟內容。
    """
    q = (query or "").strip()
    if not q:
        return "錯誤：query 不能為空。"
    n = max(1, min(20, int(k)))

    from agent_core.ingest.vector_store import get_store
    store = get_store(_COLLECTION)
    if store.count() == 0:
        return "操作 SOP 知識庫還是空的——還沒有教學影片被整理進來。"

    caller = current_request_caller()
    where = access_where(caller)
    hits = store.query(q, n_results=n, where=where)
    log_rag_access_event(
        caller=caller, collection=_COLLECTION, query=q, status="ok",
        n_results=n, hit_count=len(hits), where=where,
        trace_id=current_request_trace_id(),
    )
    if not hits:
        return f"操作 SOP 知識庫裡沒找到跟「{q}」相關的步驟。"
    return _format_hits(q, hits)
