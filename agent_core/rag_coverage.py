"""RAG（Drive/Gmail/Chat）完成度報告 — 已索引 vs 來源總量，逐部門拆解 Drive。

取代這一輪對話裡反覆手寫的一次性量測腳本（distinct doc_id 分組、drive_id 對
部門名、chroma count）。跟 dashboard.py 的「RAG 向量庫」section 是兩件事——
那個 section 讀的是小紅自己的長期記憶 collection（xiaohong_memory），這裡讀的
才是 Drive/Gmail/Chat 三個資料源的 RAG 索引。

唯讀、不寫任何東西。Drive 逐部門總檔數需要即時打 Drive API 分頁列舉，大部門
（兩三萬檔）可能要數十秒——這是「偶爾深查」工具，不是高頻輪詢用的。
"""
from __future__ import annotations

# 不在 module top import vector_store：它連帶把整個 chromadb 拉進來（實測 ~208ms
# import + 數十 MB 常駐）。catalog 為註冊 rag_coverage_report eager import 本模組，
# 但多數 daemon 啟動不查 RAG，延後到 rag_coverage_report 首次呼叫才 import。
# 與 drive_search / chat_search 同款處理。
from agent_core.env_utils import env_float
from agent_core.ingest.sync_config import load_targets

# 自產內容佔已索引 threads 的比例上限，超過就在報告裡示警。排程報表相對整個
# 信箱應該是很小的一撮（實務上 <1%），會衝高幾乎只有一個原因：backfill 的
# 啟發式誤抓。門檻放寬到 5% 是為了不要一有風吹草動就叫。
_GENERATED_RATIO_WARN_PCT = env_float("RAG_GENERATED_RATIO_WARN_PCT", 5.0, min_value=0.0)


def _resolve_target_label(drive_service, target_id: str) -> tuple[str, bool]:
    """回 (顯示名稱, is_shared_drive)。plain 資料夾（如 Meet Recordings）不是
    shared drive，drives().get 會失敗，退而用 files().get 拿檔名。"""
    try:
        info = drive_service.drives().get(driveId=target_id).execute()
        return str(info.get("name") or target_id), True
    except Exception:  # noqa: BLE001 — 不是 shared drive，往下試 plain folder
        pass
    try:
        info = drive_service.files().get(
            fileId=target_id, fields="name", supportsAllDrives=True,
        ).execute()
        return str(info.get("name") or target_id), False
    except Exception:  # noqa: BLE001 — 兩者皆失敗（權限/已刪除）→ 顯示原始 ID
        return target_id, False


def _live_file_count(drive_service, target_id: str, is_shared_drive: bool) -> int | None:
    """分頁數 target 底下的檔案總數。plain folder 只算直屬子項（非遞迴，僅供參考）。

    失敗（權限問題等）回 None，呼叫端顯示 N/A 而非讓整份報告中斷。
    """
    total = 0
    page_token = None
    try:
        while True:
            if is_shared_drive:
                kwargs = dict(
                    q="trashed=false", corpora="drive", driveId=target_id,
                    includeItemsFromAllDrives=True, supportsAllDrives=True,
                )
            else:
                kwargs = dict(
                    q=f"'{target_id}' in parents and trashed=false",
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                )
            kwargs["pageSize"] = 1000
            kwargs["fields"] = "nextPageToken, files(id)"
            if page_token:
                kwargs["pageToken"] = page_token
            resp = drive_service.files().list(**kwargs).execute()
            total += len(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return total
    except Exception:  # noqa: BLE001 — 權限/API 問題不該讓整份報告中斷
        return None


def rag_coverage_report() -> str:
    """📊 RAG（Drive / Gmail / Chat）完成度報告：已索引 vs 來源總量。

    Drive 依 rag_sync_targets.json 設定的每個同步目標，用 chroma 的 SQL 快
    路徑數 distinct doc_id（已索引檔），對照即時 Drive API 分頁算來源總檔數，
    算出逐部門覆蓋率 %；plain folder 目標（例：Meet Recordings 遞迴資料夾）
    不是 shared drive，只做非遞迴計數並標註。Gmail / Chat 用同一套 distinct
    doc_id 機制算已索引 threads / spaces，但沒有可對照的來源總量分母，只列
    絕對值。

    覆蓋率是「有沒有索引條目」，不等於「內容全文可搜」——例如掃描版 PDF 若沒
    走 OCR，會被記成已索引但內文搜不到。

    唯讀、無副作用。會即時打 Drive API 逐部門列舉檔案，2-3 萬檔的大部門可能要
    數十秒；當「偶爾查一次進度」用，不要塞進高頻輪詢或排程 daemon。
    """
    # 人工從乾淨 shell 呼叫（bin/ 腳本、REPL）通常沒有 daemon 才有的
    # RED_CHROMA_HTTP_URL / RED_EMBED_DIM——沿用 dashboard.system_status() 同一顆
    # 修正 helper（未顯式設定時才補、已設一律尊重），否則會開到早已停用的舊
    # collection、覆蓋率算成 0。單一定義，避免兩處各修一次日後漂移。
    from agent_core.dashboard import _ensure_chroma_endpoint
    _ensure_chroma_endpoint()

    from agent_core.ingest.vector_store import get_store

    from agent_core.google_auth import get_service

    lines = ["📊 RAG 完成度報告", "─" * 50, ""]

    # ── Drive ──
    targets = load_targets()
    folder_ids = list(targets.get("drive_folder_ids") or [])
    if not folder_ids:
        lines.append("【Google Drive】未設定同步目標（rag_sync_targets.json 空）")
    else:
        drive_store = get_store("drive_docs")
        drive_svc = get_service("drive", "v3")
        lines.append(f"【Google Drive】{len(folder_ids)} 個同步目標")
        rows = []
        for fid in folder_ids:
            label, is_drive = _resolve_target_label(drive_svc, fid)
            indexed = len(drive_store.list_doc_ids_by_drive(fid))
            total = _live_file_count(drive_svc, fid, is_drive)
            rows.append((label, indexed, total, is_drive))
        # 總檔數大的部門看得到在前面；total 未知（None）排最後
        rows.sort(key=lambda r: (r[2] is None, -(r[2] or 0)))
        grand_indexed = 0
        grand_total = 0
        for label, indexed, total, is_drive in rows:
            kind = "" if is_drive else "（資料夾，非遞迴）"
            if total is None:
                lines.append(f"  索引 {indexed:>6,}   總檔 N/A          {kind}{label}")
                continue
            pct = (indexed / total * 100) if total else 100.0
            grand_indexed += indexed
            grand_total += total
            lines.append(
                f"  索引 {indexed:>6,} / 總檔 {total:>6,}  ({pct:5.1f}%)  {kind}{label}"
            )
        if grand_total:
            overall_pct = grand_indexed / grand_total * 100
            lines.append(
                f"  —合計— 索引 {grand_indexed:>6,} / 總檔 {grand_total:>6,}  ({overall_pct:5.1f}%)"
            )
        lines.append("")

    # ── Gmail ──
    gmail_store = get_store("gmail_threads")
    n_gmail = len(gmail_store.list_doc_ids())
    lines.append(f"【Gmail】已索引 threads：{n_gmail:,}（無單一來源分母，僅列絕對值）")
    # 小紅自產內容（排程報表等）的標記數量。這是過濾器唯一的觀測窗口——
    # 它的失敗模式是靜默的：backfill 若誤抓，真人的信會從語意檢索中消失，
    # 不會有任何錯誤訊息，只會覺得「最近怎麼查不到東西」。盯這個比例。
    try:
        n_generated = len(gmail_store.list_generated_doc_ids())
    except Exception as exc:  # noqa: BLE001 — 觀測失敗不該讓整份報告中斷
        lines.append(f"  ⚠️ 自產標記統計失敗：{type(exc).__name__}: {exc}")
    else:
        pct = (n_generated / n_gmail * 100) if n_gmail else 0.0
        lines.append(
            f"  其中標記為小紅自產（檢索預設濾掉）：{n_generated:,}（{pct:.2f}%）"
        )
        if pct > _GENERATED_RATIO_WARN_PCT:
            lines.append(
                f"  ⚠️ 自產佔比 {pct:.1f}% 高於預期（>{_GENERATED_RATIO_WARN_PCT}%）。"
                "若剛跑過 backfill，很可能是誤抓把真人信件標成自產 —— "
                "真人的信會從語意檢索靜默消失，建議立刻核對樣本。"
            )
    lines.append("")

    # ── Chat ──
    chat_store = get_store("google_chat_messages")
    n_chat_spaces = len(chat_store.list_doc_ids())
    lines.append(f"【Google Chat】已索引 space 數：{n_chat_spaces:,}")
    lines.append("")
    lines.append("⚠️ 覆蓋率＝「有索引條目」≠「內容全文可搜」（例：掃描 PDF 未走 OCR）")

    return "\n".join(lines)
