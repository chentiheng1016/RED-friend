"""LLM-callable semantic search over indexed Google Drive content.

Distinct from agent_core.google_suite.search_drive_files (filename-only search
via the Drive API): this tool queries the ChromaDB index built daily by
drive_sync, so it finds matches by *meaning* across PDFs, Sheets, Docs, xlsx,
docx — including chunks deep inside long documents. The metadata stamped at
ingest (title, drive_id, folder_id, chunk_index) is returned so the LLM can
cite the actual source instead of confabulating.
"""
from __future__ import annotations

from typing import Any

# 不在 module top import vector_store：它連帶把整個 chromadb 拉進來（實測 ~208ms
# import + 數十 MB 常駐）。catalog 為了註冊這兩個工具 eager import 本模組，但 10
# 色 telegram bot / tool_rpc 等多數 daemon 啟動時不查 RAG，故延後到 search_drive_docs
# 首次呼叫才 import（read_drive_file 走 drive_sync、本就不碰 vector_store）。
from agent_core.rag_gateway import (
    access_where,
    current_request_caller,
    current_request_trace_id,
    log_rag_access_event,
)

_COLLECTION = "drive_docs"
_MAX_CHUNK_CHARS = 600  # cap per-hit so 5 results don't blow past LLM context


def _build_where(drive_id: str, folder_id: str) -> dict[str, Any] | None:
    if drive_id and folder_id:
        return {"$and": [
            {"drive_id":  {"$eq": drive_id}},
            {"folder_id": {"$eq": folder_id}},
        ]}
    if drive_id:
        return {"drive_id": {"$eq": drive_id}}
    if folder_id:
        return {"folder_id": {"$eq": folder_id}}
    return None


def _format_hits(query: str, hits: list[dict[str, Any]], *, prefer_recent: bool = False) -> str:
    # Drive content is untrusted — a shared doc can contain prompt-injection
    # text or pasted secrets. Both title and chunk body must be sanitized
    # before reaching the LLM, matching what rerank/citation already do for
    # email recall paths.
    from agent_core.prompt_injection import sanitize_for_llm

    head = f"找到 {len(hits)} 筆相關 Drive 內容（query: {query!r}）"
    if prefer_recent:
        head += "（已套用時間加權：新檔優先）"
    lines = [head, ""]
    for i, h in enumerate(hits, 1):
        meta = h.get("metadata") or {}
        title = sanitize_for_llm(str(meta.get("title", "(no title)")))
        chunk_idx = meta.get("chunk_index", 0)
        # ChromaDB returns cosine distance (0 = identical); flip to similarity
        # so the LLM reads "0.87 similar" instead of "0.13 distant".
        sim = max(0.0, 1.0 - float(h.get("distance", 1.0)))
        # 文件日期（Drive modifiedTime）— 證據新舊與「資料截止」都靠這個，
        # 沒有它 LLM 無從判斷兩份衝突報表哪份較新。
        doc_date = str(meta.get("modified_time") or "")[:10]
        provenance: list[str] = []
        if doc_date:
            provenance.append(f"文件日期={doc_date}")
        if meta.get("doc_id"):
            # file_id — 模型可拿去 read_drive_file(file_id) 讀整份內容
            provenance.append(f"id={meta['doc_id']}")
        if meta.get("drive_id"):
            provenance.append(f"drive={meta['drive_id']}")
        if meta.get("folder_id"):
            provenance.append(f"folder={meta['folder_id']}")
        provenance.append(f"chunk={chunk_idx}")
        date_suffix = f"（{doc_date}）" if doc_date else ""
        rec = h.get("_recency")
        rec_suffix = f"  新鮮度={rec['freshness']:.2f}" if rec else ""
        lines.append(f"【{i}】 sim={sim:.2f}{rec_suffix}  {title}{date_suffix}")
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


def search_drive_docs(
    query: str,
    k: int = 5,
    drive_id: str = "",
    folder_id: str = "",
    prefer_recent: bool = False,
) -> str:
    """搜尋 Google Drive 已索引的文件（合約、spec、報價單、PDF、Office 檔等）。

    用語意向量搜尋 + 出處 metadata 回傳「在哪個檔哪個資料夾說了什麼」。
    被問到客戶 / 合約 / 料號 / spec 的具體細節時，**先呼叫這個工具查一下**
    再回答；查不到再老實說查不到，**不要憑記憶亂編避免幻覺**。

    跟 search_drive_files 不同：那個只比對檔名，這個比對檔案內容語意。

    query:     自然語言問題，例如「ABC 客戶的保固條款」、「2026 春季新料號清單」
    k:         回傳前幾筆相關結果（1-20，預設 5）
    drive_id:  選填，限定 Shared Drive 根 ID（例：'0AF1sBHRGfyhkUk9PVA' 人事部）
    folder_id: 選填，限定特定子資料夾 ID
    prefer_recent: 選填，預設 False＝純語意相似度排序。問句帶「最新／最近／目前／
               現在／這版」這類**重視新舊**的意圖時設 True：先撈較大候選池，再用
               「語意相似度 × 文件新鮮度（modifiedTime 指數衰減）」重排，讓剛更新
               的檔優先。一般「查某條款／某料號是什麼」不必開，純語意更準。

    回傳：人類可讀的 markdown 文字，每筆含標題、相似度分數、出處、chunk 內容。
    若 collection 還是空的（cold-start daemon 還沒跑完）會明確告訴你。
    """
    q = (query or "").strip()
    if not q:
        return "錯誤：query 不能為空。"

    n = max(1, min(20, int(k)))
    where = _build_where(drive_id.strip(), folder_id.strip())

    from agent_core.ingest.vector_store import get_store
    store = get_store(_COLLECTION)
    if store.is_empty():  # 便宜的空集合判斷（別用 count()==0：大段全掃 ~400ms/次查詢）
        return (
            "drive_docs collection 還是空的——可能 daemon 第一次跑還沒完，"
            "或同步流程出錯。\n"
            "用 `tail -200 ~/RED/var/logs/daemon-rag_sync.log` 查最近一次 sync。"
        )

    caller = current_request_caller()
    filtered_where = access_where(caller, base_where=where)
    # 排序信號鏈（依序疊加，語意相似度永遠是主信號）：
    #   prefer_recent → 撈較大候選池（≤20）按「相似度 × 新鮮度」blend；
    #   citation feedback（預設開、權重小）→ 被答案反覆引用過的文件微幅
    #   boost（agent_core/citation_feedback.py，kill switch RED_CITATION_FEEDBACK=0）。
    # ⚠️ 預設路徑維持「純語意、正好取 n 筆」的既有契約（PR #193）——引用
    # 加權只在**回傳集合內**重排，不擴池；prefer_recent 已有池才疊上去。
    if prefer_recent:
        from agent_core.ingest.recency import pool_size, rerank_by_recency
        pool = store.query(q, n_results=pool_size(n), where=filtered_where)
        hits = rerank_by_recency(pool, "modified_time", max(1, len(pool)))
    else:
        hits = store.query(q, n_results=n, where=filtered_where)
    from agent_core.citation_feedback import (
        citation_feedback_enabled, note_retrieved_keys, rerank_with_citations,
    )
    if citation_feedback_enabled():
        hits = rerank_with_citations(hits, n)
        # 登記「本輪真的檢索到」的 key——record 端只採計白名單內的引用
        # （ledger 污染主防線）。絕不 raise、失敗只是這輪引用不入帳。
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
    if not hits and folder_id.strip():
        # folder 級零命中 → 自動放寬到 drive（或全庫）重查一次。同 drive 的
        # 位元組相同複本走 content-hash 去重（RAG_DRIVE_DEDUP_SCOPE=drive）後，
        # 內容只掛在正本 doc 上、可能不在被查的那個 folder——放寬讓內容仍可
        # 被找到，輸出註明範圍已放寬（每筆 hit 本就帶 folder 出處可自行判讀）。
        wider_where = access_where(
            caller, base_where=_build_where(drive_id.strip(), "")
        )
        hits = store.query(q, n_results=n, where=wider_where)[:n]
        if hits:
            note = (
                f"⚠️ 在 folder_id={folder_id!r} 內沒有命中，已自動放寬到"
                f"{'同 drive' if drive_id.strip() else '全庫'}搜尋：\n\n"
            )
            return note + _format_hits(q, hits, prefer_recent=prefer_recent)

    if not hits:
        scope = ""
        if drive_id or folder_id:
            scope = f"（限定 drive_id={drive_id!r} folder_id={folder_id!r}）"
        return f"沒找到跟「{q}」相關的內容{scope}。"

    return _format_hits(q, hits, prefer_recent=prefer_recent)


def read_drive_file(file_id: str, max_chars: int = 8000) -> str:
    """讀取單一 Google Drive 檔案的完整文字內容（按 file_id）。

    search_drive_docs 只回 top-k 的 600 字 chunk；要看「整份」最新庫存表 /
    生產排程 / 報價單時用這個。file_id 的來源：
      - search_drive_files(關鍵字)：檔名搜尋，結果含修改日期（新→舊）
      - search_drive_docs 命中行的 id=

    支援 Google Docs/Sheets（export 成文字/CSV）、xlsx、docx、PDF、圖片
    （OCR）等 — 與 rag_sync 同一套抽取器，解析在隔離 worker 跑，壞檔
    不會拖垮主程序。

    Args:
        file_id: Drive 檔案 ID（不是檔名）
        max_chars: 回傳內容上限（預設 8000，上限 30000；超過會截斷並標注）

    Returns:
        檔頭（檔名＋修改日期＋類型）+ 全文。引用時用「檔名＋修改日期」。
    """
    fid = (file_id or "").strip()
    if not fid:
        return "錯誤：file_id 不能為空。"
    n = max(500, min(30000, int(max_chars)))

    from agent_core.google_auth import get_service
    # 同 package 內複用 rag_sync 的抽取管線（含 _ExtractPool 隔離）。
    from agent_core.ingest import drive_sync as _ds
    from agent_core.prompt_injection import sanitize_for_llm

    try:
        service = get_service("drive", "v3")
        meta = service.files().get(
            fileId=fid,
            fields="id, name, mimeType, modifiedTime, size",
            supportsAllDrives=True,
        ).execute()
    except Exception as exc:
        return f"⚠️ Drive 讀取失敗（file_id={fid}）：{type(exc).__name__}: {exc}"

    name = str(meta.get("name", "(no name)"))
    mime = str(meta.get("mimeType", ""))
    size = int(meta.get("size") or 0)
    limit = _ds._download_limit_bytes_for_mime(mime)
    if size and limit and size > limit:
        return (
            f"⚠️ 「{name}」太大（{size / 1024 / 1024:.1f} MB，上限 "
            f"{limit / 1024 / 1024:.0f} MB），不適合整份讀進對話。"
            "改用 search_drive_docs 查相關段落。"
        )

    try:
        text = _ds._export_file_text_for_sync(service, fid, mime)
    except Exception as exc:
        return f"⚠️ 內容抽取失敗（{name}）：{type(exc).__name__}: {exc}"
    if text is None:
        return (
            f"⚠️ 不支援的檔案類型 {mime}（{name}）— 支援 Google Docs/"
            "Sheets、xlsx、docx、PDF、txt、圖片等。"
        )
    text = text.strip()
    if not text:
        return f"（「{name}」抽出來是空的 — 可能是空白檔或無文字內容）"

    doc_date = str(meta.get("modifiedTime") or "")[:10]
    header = (
        f"📄 {sanitize_for_llm(name)}"
        f"（修改日期 {doc_date or '未知'}｜{mime}）\n"
    )
    truncated = len(text) > n
    body = sanitize_for_llm(text[:n])
    tail = (
        f"\n…（已截斷：顯示前 {n} 字／全文 {len(text)} 字，"
        "要看後段可調高 max_chars）" if truncated else ""
    )
    return header + body + tail
