"""Drive 漏抽偵測 — 找出「應入 RAG 但未入庫」的 Drive 檔（doc_id 級，非只數量）。

跟 [[rag_coverage]] 互補：`rag_coverage_report` 只算 indexed vs live 的**數量**
缺口（算不出是哪些檔漏了、且來源分母口徑不一致）；本模組做**集合差**
（listing − indexed − skip-marker − 政策忽略），列出確切的漏抽 file_id，供人
判讀或後續補抽。

方向剛好是 `drive_sync._purge_absent_docs` 的反向：purge 用 `indexed − listing`
（要刪的），漏抽偵測用 `listing − indexed`（要補的），共用同兩個 helper。

**唯讀、無副作用**——只列出漏抽，不補抽（補抽是另一個會寫 live 的動作）。

**過濾三關**（缺一就誤報，證據見各 helper）：
1. 已入庫（`list_doc_ids_by_drive`）。
2. 有 skip-marker（`drive_sync_skip_state.json`）——已知無法處理/政策略過並記錄
   在案（empty_text / extract_error / too_large / duplicate / ignored…）。
3. `_is_drive_ignored_file_type`——政策忽略的 MIME/副檔名，涵蓋「新檔還沒被夜跑
   掃到、還沒記 marker」的情況（與 ingest 端同一判定，避免邏輯漂移）。

**listing 不完整時是保守的**：漏抽 = `listing − indexed`，清單少列只會**少報**
（漏掉的檔可能只是沒被列到），不會把現存檔誤報成漏抽；輸出會標註 `listing_complete`。

即時打 Drive API 全量列舉，一個大 shared drive（兩三萬檔）要數十秒、14 個要數
分鐘——當「偶爾深查」用，別塞進高頻輪詢或排程 daemon（同 rag_coverage）。
"""
from __future__ import annotations

from typing import Any

# 不在 top import vector_store / drive_sync：兩者連帶把 chromadb / 整個 ingest
# 拉進來（數百 ms + 數十 MB 常駐）。catalog 為註冊本模組 eager import，故延後到
# 首次呼叫（同 rag_coverage / drive_search 的處理）。
from agent_core.ingest.sync_config import load_targets

# 預設每個 drive 最多列幾筆漏抽範例（避免超大缺口把報告灌爆；完整清單走
# detect_all_drive_gaps() 拿結構化 dict）。
_DEFAULT_SAMPLE = 15


def detect_drive_gaps(drive_id: str, service=None) -> dict[str, Any]:
    """單一 Shared Drive 的漏抽偵測。

    回 dict：``drive_id`` / ``listing_complete`` / ``total_listed`` /
    ``indexed_count`` / ``skipped_count`` / ``gap_count`` / ``gaps``
    （每筆 gap 是 ``{file_id, name, mime_type, modified_time, parents}``，
    依 modified_time 新→舊排序）。

    ``service``：可傳入共用的 Drive service 省重建；None 則自取。
    """
    from agent_core.ingest.drive_sync import (
        _is_drive_ignored_file_type,
        _list_drive_files,
        _load_skip_state,
    )
    from agent_core.ingest.vector_store import get_store

    if service is None:
        from agent_core.google_auth import get_service
        service = get_service("drive", "v3")

    files, listing_complete = _list_drive_files(service, drive_id)
    indexed = get_store("drive_docs").list_doc_ids_by_drive(drive_id)
    skipped = set(_load_skip_state().get("files") or {})

    gaps: list[dict[str, Any]] = []
    for f in files:
        fid = f.get("id")
        if not fid or fid in indexed or fid in skipped:
            continue
        if _is_drive_ignored_file_type(f.get("name", ""), f.get("mimeType", "")):
            continue
        gaps.append(
            {
                "file_id": fid,
                "name": f.get("name", ""),
                "mime_type": f.get("mimeType", ""),
                "modified_time": f.get("modifiedTime", ""),
                "parents": f.get("parents") or [],
            }
        )

    gaps.sort(key=lambda g: g["modified_time"], reverse=True)
    return {
        "drive_id": drive_id,
        "listing_complete": listing_complete,
        "total_listed": len(files),
        "indexed_count": len(indexed),
        "skipped_count": len(skipped),
        "gap_count": len(gaps),
        "gaps": gaps,
    }


def detect_all_drive_gaps(drive_ids: list[str] | None = None) -> dict[str, Any]:
    """對多個 Shared Drive 跑漏抽偵測並彙總。

    ``drive_ids`` 預設取 ``rag_sync_targets.json`` 的 ``drive_folder_ids``。非
    Shared Drive 的 target（plain 資料夾，如 Meet Recordings）**本階段不涵蓋**
    （它們用 folder_id 而非 drive_id、走遞迴列舉，另案），列在 ``skipped_targets``。

    回 dict：``per_drive``（label→單 drive 結果）/ ``skipped_targets`` /
    ``total_gap_count`` / ``any_incomplete``。
    """
    from agent_core.dashboard import _ensure_chroma_endpoint
    _ensure_chroma_endpoint()

    from agent_core.google_auth import get_service
    from agent_core.rag_coverage import _resolve_target_label

    service = get_service("drive", "v3")
    if drive_ids is None:
        drive_ids = list(load_targets().get("drive_folder_ids") or [])

    per_drive: dict[str, dict[str, Any]] = {}
    skipped_targets: list[str] = []
    total_gap = 0
    any_incomplete = False
    for tid in drive_ids:
        label, is_shared = _resolve_target_label(service, tid)
        if not is_shared:
            skipped_targets.append(label)
            continue
        result = detect_drive_gaps(tid, service=service)
        result["label"] = label
        per_drive[label] = result
        total_gap += result["gap_count"]
        any_incomplete = any_incomplete or not result["listing_complete"]

    return {
        "per_drive": per_drive,
        "skipped_targets": skipped_targets,
        "total_gap_count": total_gap,
        "any_incomplete": any_incomplete,
    }


def rag_gap_report(sample: int = _DEFAULT_SAMPLE) -> str:
    """🔍 Drive 漏抽報告：列出「應入 RAG 但未入庫」的檔（doc_id 級集合差）。

    對 rag_sync_targets.json 每個 Shared Drive，取 Drive 全量 listing 減去
    已索引、減去 skip-marker（已知無法處理/略過在案）、減去政策忽略的 MIME/
    副檔名，剩下的就是真漏抽。跟 rag_coverage_report 的差別：那個只給覆蓋率
    百分比（數量缺口），這個給出**確切是哪些檔漏了**。

    唯讀、無副作用。即時全量列舉 Drive，14 個 drive 要數分鐘；偶爾深查用，
    別塞進高頻輪詢。每個 drive 最多列 ``sample`` 筆範例。

    listing 不完整（Drive API incompleteSearch）時漏抽數可能低估、不會誤報，
    報告會標註 ⚠️。
    """
    summary = detect_all_drive_gaps()
    per_drive = summary["per_drive"]

    lines = ["🔍 Drive 漏抽報告（應入庫但未入庫）", "─" * 50, ""]
    if not per_drive and not summary["skipped_targets"]:
        lines.append("未設定同步目標（rag_sync_targets.json 空）")
        return "\n".join(lines)

    # 漏抽多的排前面
    for result in sorted(per_drive.values(), key=lambda r: -r["gap_count"]):
        flag = "" if result["listing_complete"] else "  ⚠️清單不完整(漏抽數可能低估)"
        lines.append(
            f"【{result['label']}】漏抽 {result['gap_count']}"
            f"（listing {result['total_listed']:,} / 索引 {result['indexed_count']:,}）{flag}"
        )
        for g in result["gaps"][:sample]:
            when = (g["modified_time"] or "")[:10]
            lines.append(f"    · {g['name']}  [{when}]  {g['file_id']}")
        if result["gap_count"] > sample:
            lines.append(f"    …另有 {result['gap_count'] - sample} 筆（完整清單走 detect_all_drive_gaps）")
        lines.append("")

    lines.append(f"—合計漏抽 {summary['total_gap_count']} 筆—")
    if summary["skipped_targets"]:
        lines.append(
            f"（未涵蓋 {len(summary['skipped_targets'])} 個 plain 資料夾目標："
            f"{'、'.join(summary['skipped_targets'])}）"
        )
    if summary["any_incomplete"]:
        lines.append("⚠️ 有 drive 的 listing 不完整，漏抽為保守低估（不會誤報現存檔）")
    lines.append("ℹ️ 漏抽＝有檔無索引；補抽是另一動作（唯讀報告不自動補）")
    return "\n".join(lines)
