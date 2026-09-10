"""教育訓練影片自動學習 — 新影片上傳即學會，學習成效不再靠人記得跑批次。

三件事：
1. `relearn_video()`：單支影片完整學習管線 — Drive 下載 → deep 深析（本機
   ASR＋OCR gating 由 env 決定）→ 覆寫 operation_sops SOP → 清該影片舊卡 →
   抽新知識卡入庫。批次腳本與 watcher 共用這一條。
2. `list_training_videos()`：列舉教學根資料夾（飛越erp使用教學）底下所有影片，
   部門由所在子資料夾名推斷（生管教學→生管）。
3. `check_and_learn_new()`：watcher 入口（launchd 每日跑）— 比對已學清單，
   新影片自動學習（每輪支數/檔案大小有閘門，deep 是付費 gemini-2.5-pro），
   完成後 Telegram 通知大王學了什麼。

已學狀態存 var/state/training_videos_learned.json；首次執行自動從
operation_sops collection 補種（既有 13 支不會被重學）。跳過的過大檔記進
state 的 skipped，不會每天重試轟炸 — 要學就手動跑 scripts/relearn_training_videos.py。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

from agent_core.env_utils import env_int
from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger

# 教學影片根資料夾（飛越erp使用教學，shared drive 0ACpPJFVoF53FUk9PVA）。
_TRAINING_FOLDER_ID = (
    os.environ.get("RED_TRAINING_VIDEO_FOLDER_ID", "").strip()
    or "1Yxt9N-pXyheZzEkAafz3bX9-Xp0fXMh6"
)
_STATE_PATH = os.path.join(STATE_DIR, "training_videos_learned.json")

# watcher 每輪閘門：deep 學習是付費 gemini-2.5-pro（大檔 ~$1.5/支、20+ 分鐘），
# 一天最多**嘗試** N 支（失敗也算 — 費用在 ingest 前就燒掉了）、
# 超大檔跳過留人工決定。
_MAX_PER_RUN = env_int("RED_TRAINING_WATCH_MAX_PER_RUN", 2, min_value=1, max_value=10)
_MAX_BYTES = env_int("RED_TRAINING_WATCH_MAX_BYTES", 1_500_000_000, min_value=1)
_WALK_DEPTH = 3  # 根夾→部門夾→（可能再一層）→影片


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dept_from_folder(folder_name: str) -> str:
    """資料夾名 → 部門名：生管教學→生管、會計部教學→會計、現場管理教學→現場；
    非部門夾（安裝手冊、ERP 上課紀錄…）原樣返回。"""
    name = (folder_name or "").strip()
    for suffix in ("教學",):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    for suffix in ("管理", "部"):
        if len(name) > 2 and name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


# ────────────────────────────────────────────────────────────────────
# 已學狀態
# ────────────────────────────────────────────────────────────────────

def _load_state() -> dict[str, Any]:
    if not os.path.exists(_STATE_PATH):
        return {"learned": {}, "skipped": {}}
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {"learned": dict(data.get("learned") or {}),
                "skipped": dict(data.get("skipped") or {})}
    except Exception as exc:
        logger.warning("training_videos state 讀取失敗（%s），視為空", exc)
        return {"learned": {}, "skipped": {}}


def _save_state(state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    _atomic_write_text(_STATE_PATH, json.dumps(
        {**state, "updated_at": _now()}, ensure_ascii=False, indent=1))


def _seed_from_collection(state: dict[str, Any]) -> dict[str, Any]:
    """首次執行：把 operation_sops 裡已學的影片補進 learned，避免重學既有 13 支。"""
    try:
        from agent_core.ingest.vector_store import get_store

        store = get_store("operation_sops")
        col = store._col or store._open_collection()
        data = col.get(include=["metadatas"])
        for m in data.get("metadatas") or []:
            vid = str(m.get("video_id") or "")
            if vid and vid not in state["learned"]:
                state["learned"][vid] = {
                    "name": str(m.get("video_name") or vid),
                    "at": "seeded", "source": "operation_sops",
                }
    except Exception as exc:
        logger.warning("training_videos 首次補種失敗（%s）", exc)
    return state


# ────────────────────────────────────────────────────────────────────
# Drive 列舉
# ────────────────────────────────────────────────────────────────────

def list_training_videos(folder_id: str | None = None) -> list[dict[str, Any]]:
    """列教學根夾下所有影片：[{id, name, dept, size, folder}]（遞迴至 _WALK_DEPTH）。"""
    from agent_core.google_auth import get_service

    svc = get_service("drive", "v3")
    root = folder_id or _TRAINING_FOLDER_ID
    videos: list[dict[str, Any]] = []
    queue: list[tuple[str, str, int]] = [(root, "", 0)]
    while queue:
        fid, fname, depth = queue.pop(0)
        page_token = None
        while True:
            resp = svc.files().list(
                q=f"'{fid}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,mimeType,size)",
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                pageSize=200, pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                mime = f.get("mimeType", "")
                if mime == "application/vnd.google-apps.folder":
                    if depth < _WALK_DEPTH:
                        queue.append((f["id"], f["name"], depth + 1))
                elif mime.startswith("video/"):
                    videos.append({
                        "id": f["id"], "name": f["name"],
                        "dept": _dept_from_folder(fname),
                        "folder": fname,
                        "size": int(f.get("size", 0) or 0),
                    })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return videos


# ────────────────────────────────────────────────────────────────────
# 單支學習（批次腳本與 watcher 共用）
# ────────────────────────────────────────────────────────────────────

def _dept_from_sops(video_id: str) -> str:
    """從 operation_sops 既有 metadata 回填 department。

    CLI 指定 video_id 重學（scripts/relearn_training_videos.py <id>）不帶部門，
    空字串會把該影片 SOP 既有的 department 洗掉（權限過濾與查詢分組都靠它）。
    查不到 / chroma 離線一律回空字串，不擋重學。"""
    vid = (video_id or "").strip()
    if not vid:
        return ""
    try:
        from agent_core.ingest.vector_store import get_store

        store = get_store("operation_sops")
        col = store._col or store._open_collection()
        data = col.get(where={"video_id": {"$eq": vid}},
                       include=["metadatas"], limit=1)
        for m in data.get("metadatas") or []:
            dept = str(m.get("department") or "").strip()
            if dept:
                return dept
    except Exception as exc:
        logger.warning("relearn department 回填失敗（%s），維持空值", exc)
    return ""


def relearn_video(video_id: str, video_name: str = "", department: str = "",
                  *, caller: str = "training_videos.relearn") -> dict[str, Any]:
    """一支影片的完整學習：下載 → deep 深析 → SOP 覆寫 → 清舊卡 → 抽新卡。
    回 {"ok", "video", "minutes", "sop_chunks", "cards", ...}；失敗回 ok=False。
    department 沒給時先從 operation_sops 既有 metadata 回填（重學不得洗掉部門）。"""
    from agent_core import local_asr
    from agent_core.erp import _download_drive_file
    from agent_core.operation_sops import ingest_operation_sop
    from agent_core.skill_cards import (
        extract_skill_cards, ingest_skill_cards, purge_video_cards,
    )
    from agent_core.video_understanding import (
        _deep_teaching_analysis, _drive_file_meta, _suffix_for_mime,
    )

    vid = (video_id or "").strip()
    if not vid:
        return {"ok": False, "error": "empty video_id"}
    department = (department or "").strip() or _dept_from_sops(vid)
    t0 = time.time()
    tmp_dir = tempfile.mkdtemp(prefix="relearn_")
    try:
        meta = _drive_file_meta(vid)
        mime = meta.get("mimeType", "video/mp4")
        name = (video_name or meta.get("name") or vid).strip()
        path = os.path.join(tmp_dir, "v" + _suffix_for_mime(mime))
        if not _download_drive_file(vid, path):
            return {"ok": False, "video": name, "error": "Drive 下載失敗"}

        analysis = _deep_teaching_analysis(
            path=path, mime_type=mime, focus="", model=None, caller=caller)
        if not analysis:
            return {"ok": False, "video": name, "error": "深度分析回空"}

        asr = "whisper.cpp" if local_asr.is_available() else ""
        r1 = ingest_operation_sop(vid, name, analysis,
                                  department=department, asr_engine=asr)
        ex = extract_skill_cards(vid, name, analysis, department=department)
        purged = purge_video_cards(vid) if ex.get("ok") else 0
        r2 = (ingest_skill_cards(ex["cards"]) if ex.get("ok")
              else {"ok": False, "reason": ex.get("reason")})
        return {
            "ok": True, "video": name, "dept": department,
            "minutes": round((time.time() - t0) / 60, 1),
            "sop_chunks": r1.get("chunks"),
            "cards": len(ex.get("cards") or []),
            "purged_old": purged,
            **{f"ingest_{k}": v for k, v in r2.items() if k != "ok"},
        }
    except Exception as exc:
        logger.warning("relearn_video %s 失敗（%s: %s）",
                       vid, type(exc).__name__, exc)
        return {"ok": False, "video": video_name or vid,
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────
# Watcher（launchd 每日）
# ────────────────────────────────────────────────────────────────────

def _chroma_ready() -> tuple[bool, str]:
    """chroma 可用性探測（沿用 chroma_backend.preflight 的 heartbeat 模式）。
    回 (ok, detail)。探測本身失敗不擋當輪（fail-open，同 preflight 慣例）。"""
    try:
        from agent_core.chroma_backend import preflight

        pf = preflight()
        return bool(pf.get("ok")), str(pf.get("detail", ""))
    except Exception as exc:
        return True, f"preflight 探測失敗（{exc}），不擋當輪"


def check_and_learn_new(*, max_videos: int | None = None,
                        max_bytes: int | None = None) -> dict[str, Any]:
    """發現教學資料夾裡的新影片並自動學習（每輪有支數/大小閘門）。

    學完 Telegram 通知；過大的記 skipped 不重試（手動跑 relearn 腳本處理）。
    """
    cap = max_videos if max_videos is not None else _MAX_PER_RUN
    size_cap = max_bytes if max_bytes is not None else _MAX_BYTES

    state = _seed_from_collection(_load_state())
    known = set(state["learned"]) | set(state["skipped"])
    # 列 Drive 教學夾要拿 Google 憑證 ＋ 打 Drive API：長輪跑到一半 access token
    # 到期需刷新，若此刻斷網 / DNS 解不出 oauth2.googleapis.com，get_service 會拋
    # （daemon 模式無法啟動互動式重新授權，直接 RuntimeError）。這類暫時性錯誤
    # 視同「本輪跳過、明天自動重試」——跟 chroma_unavailable 一致，回 summary 正常
    # 結束（exit 0），不讓單發 daemon crash 成 exit 1 驚動 fleet 狀態 / smoke。
    try:
        videos = list_training_videos()
    except Exception as exc:
        # exc_info=True：這裡刻意 catch 最寬的 Exception（DNS / token 刷新是常見
        # 暫時態，但也可能藏 Google API 變更或程式錯誤），留完整堆疊才能事後
        # 在 daemon log 裡區分「暫時性斷網」與「真的壞了」。
        logger.warning("training watcher：列 Drive 教學夾失敗（%s），本輪中止、"
                       "明天自動重試", exc, exc_info=True)
        _save_state(state)  # 首次補種結果仍要落盤
        return {"ok": False, "total_in_folder": 0, "new_found": 0,
                "learned": [], "skipped": [], "deferred": 0,
                "reason": "drive_unavailable", "detail": str(exc)}
    fresh = [v for v in videos if v["id"] not in known]
    summary: dict[str, Any] = {"ok": True, "total_in_folder": len(videos),
                               "new_found": len(fresh), "learned": [],
                               "skipped": [], "deferred": 0}
    if not fresh:
        _save_state(state)  # 補種結果也要落盤
        logger.info("training watcher：無新影片（資料夾共 %d 支）", len(videos))
        return summary

    # chroma 離線時每支新影片照樣先燒完整 deep 費用（~$1.5/支），到最後
    # ingest 才失敗、明天又重來 — 進迴圈前先探測，不可用直接中止當輪。
    ready, detail = _chroma_ready()
    if not ready:
        logger.warning("training watcher：chroma 不可用（%s），本輪中止、"
                       "不燒 deep 學習費用", detail)
        summary["ok"] = False
        summary["reason"] = "chroma_unavailable"
        summary["detail"] = detail
        return summary

    fresh.sort(key=lambda v: v["size"])  # 小的先學，一輪內學好學滿
    attempts = 0
    for v in fresh:
        if attempts >= cap:
            summary["deferred"] += 1  # 明天再學，不記 skipped
            continue
        if v["size"] > size_cap:
            state["skipped"][v["id"]] = {
                "name": v["name"], "reason": f"超過大小閘門（{v['size'] / 1e6:.0f} MB）",
                "at": _now()}
            summary["skipped"].append(v["name"])
            continue
        # cap 計「嘗試數」不計「成功數」：失敗的一支也燒了完整 deep 費用，
        # 只數成功會在連環失敗的晚上把整批新影片都各燒一輪。
        attempts += 1
        r = relearn_video(v["id"], v["name"], v["dept"])
        if r.get("ok"):
            state["learned"][v["id"]] = {"name": v["name"], "dept": v["dept"],
                                         "at": _now(), "source": "watcher"}
            summary["learned"].append(
                f"[{v['dept'] or '-'}] {v['name']}（{r['minutes']}min、"
                f"{r.get('cards', 0)} 卡）")
        else:
            # 失敗不記 skipped — 明天自動重試（暫時性網路錯誤是常態）
            summary["skipped"].append(f"{v['name']}（失敗：{r.get('error')}）")
        _save_state(state)

    _save_state(state)
    if summary["learned"] or summary["skipped"]:
        _notify(summary)
    return summary


def _notify(summary: dict[str, Any]) -> None:
    try:
        from agent_core.telegram import telegram_push

        lines = ["🎬 教學影片自動學習回報"]
        for item in summary["learned"]:
            lines.append(f"✅ 學會：{item}")
        for item in summary["skipped"]:
            lines.append(f"⏭ 跳過：{item}")
        if summary.get("deferred"):
            lines.append(f"⏳ 還有 {summary['deferred']} 支排明天（每日上限）")
        lines.append("查知識：search_skill_cards / search_operation_sops")
        telegram_push("\n".join(lines))
    except Exception as exc:
        logger.warning("training watcher 通知失敗（%s）", exc)
