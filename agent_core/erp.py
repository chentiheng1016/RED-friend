"""ERP video learning + workflow automation.

Uses Gemini Vision on
tutorial videos (local or Drive-hosted), extracts structured workflow
JSON, and manages the library of learned workflows.
"""
import os
import re
import json
from datetime import datetime

from agent_core.file_ops import _clean_path
from agent_core.gemini_client import (
    GEMINI_MODEL,
    _get_gemini_client,
    _gemini_generate,
    _wait_for_file_ready,
    upload_file,
)
from agent_core.google_auth import get_service
from agent_core.logging_and_paths import logger, _SCRIPT_DIR

_ERP_WORKFLOW_DIR = os.path.join(_SCRIPT_DIR, "erp_workflows")
_ERP_VIDEO_CACHE_DIR = os.path.join(_ERP_WORKFLOW_DIR, "_video_cache")


def _ensure_erp_dirs():
    os.makedirs(_ERP_WORKFLOW_DIR, exist_ok=True)
    os.makedirs(_ERP_VIDEO_CACHE_DIR, exist_ok=True)


def _extract_drive_folder_id(url_or_id: str) -> str:
    """從 Drive URL 或 folder ID 字串抽出純 folder ID。"""
    s = (url_or_id or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"/file/d/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    if re.match(r"^[A-Za-z0-9_\-]{20,}$", s):
        return s
    return ""


def _list_drive_folder_videos(folder_url_or_id: str, recursive: bool = True,
                                 subfolder_filter: str = "") -> list:
    """列出 Drive 資料夾內所有影片檔，預設遞迴進子資料夾。
    - recursive: True 會進入所有子資料夾（深度無限）
    - subfolder_filter: 只進入名稱含此字串的子資料夾（例：「開發」只看開發部）
    回傳 list of (file_id, name, size, mimeType, folder_path)。
    自動支援共用雲端硬碟（Shared Drive）。"""
    folder_id = _extract_drive_folder_id(folder_url_or_id)
    if not folder_id:
        return []
    service = get_service("drive", "v3")
    video_mimes = {
        "video/mp4", "video/mpeg", "video/quicktime", "video/avi",
        "video/x-flv", "video/webm", "video/x-ms-wmv", "video/3gpp",
    }
    results = []

    def _walk(fid: str, path: str):
        page_token = None
        while True:
            try:
                resp = service.files().list(
                    q=f"'{fid}' in parents and trashed=false",
                    fields="nextPageToken, files(id, name, size, mimeType)",
                    pageSize=100, pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    corpora="allDrives",
                ).execute()
            except Exception as e:
                logger.warning("列資料夾 %s 失敗：%s", path, e)
                return
            for f in resp.get("files", []) or []:
                mime = f.get("mimeType", "")
                name = f["name"]
                if mime in video_mimes:
                    results.append((f["id"], name, int(f.get("size", 0)), mime, path))
                elif recursive and mime == "application/vnd.google-apps.folder":
                    sub_path = f"{path}/{name}" if path else name
                    if subfolder_filter and subfolder_filter not in name and subfolder_filter not in sub_path:
                        _walk(f["id"], sub_path)
                    else:
                        _walk(f["id"], sub_path)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    _walk(folder_id, "")
    if subfolder_filter:
        results = [r for r in results if subfolder_filter in r[4]]
    return sorted(results, key=lambda x: (x[4], x[1]))


def _download_drive_file(file_id: str, dest_path: str) -> bool:
    """從 Drive 下載檔案到本機。大檔分 chunk 下載。支援 Shared Drive。"""
    try:
        from googleapiclient.http import MediaIoBaseDownload
        service = get_service("drive", "v3")
        req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, req, chunksize=1024 * 1024 * 10)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    if pct % 20 == 0:
                        print(f"  下載中 {pct}% ({os.path.basename(dest_path)})", flush=True)
        return True
    except Exception as e:
        logger.warning("下載 Drive 檔失敗 %s：%s", file_id, e)
        return False


def _erp_video_prompt(task_name: str, video_index: int = 0, total_videos: int = 1) -> str:
    """給 Gemini 看 ERP 教學影片的 prompt。要求結構化 JSON 輸出。"""
    context = ""
    if total_videos > 1:
        context = (f"這是系列影片的第 {video_index+1}/{total_videos} 支，"
                   f"只抽這支影片涵蓋的步驟即可，稍後我會合併所有影片。\n\n")
    return (
        f"你正在學習一套 ERP 系統中「{task_name}」這個操作任務。\n"
        f"{context}請仔細看這支教學影片（含語音旁白與畫面操作），輸出結構化 JSON（只回純 JSON，不要 markdown 圍欄）：\n\n"
        "```\n"
        "{\n"
        '  "task_name": "任務名稱",\n'
        '  "video_summary": "30 字內影片重點",\n'
        '  "erp_name": "ERP 系統名稱或網址（從畫面判斷）",\n'
        '  "prerequisites": "開始前的假設（例如「已登入」「在主頁」）",\n'
        '  "menu_path": "從主頁到此功能的選單路徑，例如「主選單 → 開發管理 → 樣品單」",\n'
        '  "steps": [\n'
        "    {\n"
        '      "n": 1,\n'
        '      "action": "click" | "fill" | "select" | "upload" | "wait" | "observe",\n'
        '      "target_description": "描述這個按鈕/欄位的位置與外觀（例如「畫面右上角的『新增』按鈕」「客戶代號欄位」）",\n'
        '      "visual_cue": "文字 label、icon、顏色等視覺線索",\n'
        '      "example_value": "範例輸入值（如適用）",\n'
        '      "why": "這步的目的"\n'
        "    }\n"
        "  ],\n"
        '  "fields_schema": {\n'
        '    "客戶代號": {"type": "string", "required": true, "example": "NIKE001", "notes": "..." },\n'
        '    "交期": {"type": "date", "required": true, "example": "2026-06-15"}\n'
        "  },\n"
        '  "validation": "如何知道操作成功（例如「跳出成功訊息」「訂單列表多一筆」）",\n'
        '  "common_pitfalls": ["常見錯誤 1", "常見錯誤 2"],\n'
        '  "narrator_tips": ["旁白特別強調的重點"]\n'
        "}\n"
        "```\n\n"
        "要求：\n"
        "  - steps 按影片實際順序編號（n=1, 2, 3...）\n"
        "  - target_description 要讓之後的自動化能靠視覺找到該元素（說清楚顏色/位置/文字）\n"
        "  - 如果影片某段沒操作只是講解，steps 不要列\n"
        "  - fields_schema 只列真正要填的欄位\n"
        "  - 影片語音聽不清楚的地方標 note: \"unclear\"\n"
    )


def _analyze_video_with_gemini(video_path: str, task_name: str,
                                 video_index: int = 0, total_videos: int = 1) -> dict:
    """上傳本機影片檔到 Gemini Files API，分析出結構化工作流程。"""
    if not os.path.exists(video_path):
        return {"error": f"影片不存在：{video_path}"}
    size_mb = os.path.getsize(video_path) / 1024 / 1024
    if size_mb > 2000:
        return {"error": f"影片太大 ({size_mb:.0f} MB > 2 GB 上限)"}
    print(f"  ↑ 上傳 {os.path.basename(video_path)} ({size_mb:.0f} MB) 到 Gemini…")
    uploaded = None
    try:
        uploaded = upload_file(_get_gemini_client(), video_path)
        uploaded = _wait_for_file_ready(uploaded)
        print("  ✓ 上傳完成，開始分析（影片長度越長處理越久）")
        prompt = _erp_video_prompt(task_name, video_index, total_videos)
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[uploaded, prompt])
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"error": "Gemini 沒回 JSON", "raw": text[:500]}
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            return {"error": f"JSON 解析失敗：{e}", "raw": text[:500]}
        return parsed
    except Exception as e:
        return {"error": f"分析失敗：{type(e).__name__}: {e}"}
    finally:
        if uploaded is not None:
            try:
                _get_gemini_client().files.delete(name=uploaded.name)
            except Exception as _e:
                logger.warning("Gemini 上傳檔清理失敗（%s）", _e)


def learn_erp_from_video(video_source: str, task_name: str):
    """用 Gemini 看**一支**本機影片或 Drive 連結，學出 ERP 工作流程存成 JSON。
    - video_source：本機路徑、或 Drive file URL（支援 /file/d/... 格式）
    - task_name：任務名稱（會當成 JSON 檔名，例如「樣品開發」）
    結果存 ~/RED/erp_workflows/<task_name>.json。
    ⚠️ 影片最長 1 小時、最大 2 GB。沒字幕也能讀（Gemini 聽語音）。"""
    task_name = (task_name or "").strip()
    if not task_name:
        return "錯誤：task_name 不能空"
    _ensure_erp_dirs()

    local_path = None
    src = (video_source or "").strip()
    if src.startswith("http") and "drive.google.com" in src:
        file_id = _extract_drive_folder_id(src)
        if not file_id:
            return f"錯誤：解析 Drive URL 失敗：{src}"
        local_path = os.path.join(_ERP_VIDEO_CACHE_DIR, f"{file_id}.mp4")
        if not os.path.exists(local_path):
            print(f"從 Drive 下載 {file_id}…")
            if not _download_drive_file(file_id, local_path):
                return "錯誤：從 Drive 下載失敗"
    else:
        local_path = _clean_path(src)
        if not os.path.exists(local_path):
            return f"錯誤：本機影片不存在：{local_path}"

    result = _analyze_video_with_gemini(local_path, task_name)
    if "error" in result:
        return f"❌ {result['error']}"

    out_path = os.path.join(_ERP_WORKFLOW_DIR, f"{task_name}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    steps_n = len(result.get("steps", []))
    return (f"✅ 工作流程已學成並存檔\n"
            f"   任務：{task_name}\n"
            f"   抽出 {steps_n} 個操作步驟\n"
            f"   選單路徑：{result.get('menu_path', 'N/A')}\n"
            f"   檔案：{out_path}\n"
            f"   用 show_erp_workflow(\"{task_name}\") 查看詳情")


def learn_erp_from_drive_folder(folder_url: str, task_name: str,
                                   subfolder_filter: str = "", name_filter: str = "",
                                   exclude_name_filter: str = "",
                                   max_videos: int = 0):
    """從 Drive 資料夾學一整系列 ERP 教學影片（遞迴進子資料夾、合併成一份 workflow）。
    - folder_url：Drive 資料夾連結（支援 Shared Drive）
    - task_name：任務名稱
    - subfolder_filter：只看名稱含此字串的子資料夾
    - name_filter：只處理檔名含此字串的影片
    - exclude_name_filter：排除檔名含此字串的影片（例：已學過的用此參數跳過）
    - max_videos：0=全部，>0 限制最多幾支"""
    task_name = (task_name or "").strip()
    if not task_name:
        return "錯誤：task_name 不能空"
    _ensure_erp_dirs()

    videos = _list_drive_folder_videos(folder_url, recursive=True, subfolder_filter=subfolder_filter)
    if name_filter:
        videos = [v for v in videos if name_filter in v[1]]
    if exclude_name_filter:
        videos = [v for v in videos if exclude_name_filter not in v[1]]
    if not videos:
        hint = f"subfolder_filter={subfolder_filter!r}" if subfolder_filter else "請檢查 URL 或權限"
        return f"❌ 找不到任何影片（{hint}）"
    if max_videos > 0:
        videos = videos[:max_videos]

    total_mb = sum(v[2] for v in videos) / 1024 / 1024
    print(f"📂 找到 {len(videos)} 支影片（共 {total_mb:.0f} MB）：")
    for i, (fid, name, size, mime, path) in enumerate(videos, 1):
        print(f"   {i:2d}. [{path or '(根)'}] {name} ({size/1024/1024:.0f} MB)")

    per_video_dir = os.path.join(_ERP_WORKFLOW_DIR, f"{task_name}_raw")
    os.makedirs(per_video_dir, exist_ok=True)
    individual_results = []

    for i, (fid, name, size, mime, path) in enumerate(videos):
        print(f"\n▶️ [{i+1}/{len(videos)}] 處理：{name}")
        cache_path = os.path.join(_ERP_VIDEO_CACHE_DIR, f"{fid}.mp4")
        if not os.path.exists(cache_path):
            if not _download_drive_file(fid, cache_path):
                print("  ⚠️ 下載失敗，跳過")
                continue
        result = _analyze_video_with_gemini(cache_path, task_name, video_index=i, total_videos=len(videos))
        if "error" in result:
            print(f"  ⚠️ Gemini 分析失敗：{result['error']}")
            continue
        result["_video_name"] = name
        result["_video_path"] = path
        result["_video_index"] = i + 1
        safe_name = re.sub(r"[^\w\-\. ]", "_", name)[:100]
        single_path = os.path.join(per_video_dir, f"{i+1:02d}_{safe_name}.json")
        with open(single_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        individual_results.append(result)
        print(f"  ✓ 抽出 {len(result.get('steps', []))} 步驟")

    if not individual_results:
        return "❌ 所有影片都分析失敗"

    print(f"\n🔀 合併 {len(individual_results)} 份分析 → 完整 workflow…")
    merge_prompt = (
        f"以下是「{task_name}」任務的 {len(individual_results)} 支教學影片各自的分析結果。\n"
        "請合併成**一份完整的 workflow JSON**，格式跟個別分析一樣，但:\n"
        "  - 去除重複步驟\n"
        "  - 按合理執行順序重新排序 steps（n=1,2,3...）\n"
        "  - fields_schema 合併所有影片提到的欄位（同欄位取最詳細版本）\n"
        "  - video_summary 改成整體任務的 30 字摘要\n"
        "  - 新增欄位 source_videos: [影片名稱列表]\n"
        "只回純 JSON，不要 markdown 圍欄。\n\n"
        "個別分析：\n" + json.dumps(individual_results, ensure_ascii=False, indent=2)[:30000]
    )
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[merge_prompt])
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return f"❌ 合併失敗：Gemini 沒回 JSON。個別分析已存在 {per_video_dir}/"
        merged = json.loads(m.group(0))
    except Exception as e:
        return f"❌ 合併失敗：{e}。個別分析已存在 {per_video_dir}/"

    out_path = os.path.join(_ERP_WORKFLOW_DIR, f"{task_name}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return (f"✅ 從 {len(individual_results)} 支影片學會「{task_name}」\n"
            f"   合併後 {len(merged.get('steps', []))} 個步驟\n"
            f"   選單路徑：{merged.get('menu_path', 'N/A')}\n"
            f"   主檔：{out_path}\n"
            f"   個別影片分析：{per_video_dir}/\n"
            f"   用 show_erp_workflow(\"{task_name}\") 看詳情")


def list_erp_workflows():
    """列出所有已學的 ERP 工作流程。"""
    _ensure_erp_dirs()
    files = [f for f in os.listdir(_ERP_WORKFLOW_DIR)
             if f.endswith(".json") and not f.startswith(".")]
    if not files:
        return "目前還沒學任何 ERP 任務。用 learn_erp_from_drive_folder() 或 learn_erp_from_video() 學一個。"
    lines = [f"共 {len(files)} 個已學的 ERP 任務："]
    for f in sorted(files):
        path = os.path.join(_ERP_WORKFLOW_DIR, f)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            name = d.get("task_name", f[:-5])
            steps = len(d.get("steps", []))
            summary = d.get("video_summary", "")[:50]
            lines.append(f"  📋 {name}  ({steps} 步驟) — {summary}")
        except Exception:
            lines.append(f"  ⚠️ {f} 讀取失敗")
    return "\n".join(lines)


def show_erp_workflow(task_name: str):
    """顯示某 ERP 任務的完整工作流程細節。"""
    task_name = (task_name or "").strip()
    path = os.path.join(_ERP_WORKFLOW_DIR, f"{task_name}.json")
    if not os.path.exists(path):
        return f"找不到任務「{task_name}」。用 list_erp_workflows() 看有哪些。"
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return f"讀取失敗：{e}"
    lines = [
        f"📋 ERP 任務：{d.get('task_name', task_name)}",
        f"   系統：{d.get('erp_name', 'N/A')}",
        f"   摘要：{d.get('video_summary', 'N/A')}",
        f"   前置：{d.get('prerequisites', 'N/A')}",
        f"   選單路徑：{d.get('menu_path', 'N/A')}",
        "",
        f"📝 步驟（{len(d.get('steps', []))} 步）：",
    ]
    for s in d.get("steps", []):
        lines.append(f"   {s.get('n', '?')}. [{s.get('action', '?')}] {s.get('target_description', '')[:80]}")
        if s.get("example_value"):
            lines.append(f"       範例值：{s['example_value']}")
    if d.get("fields_schema"):
        lines.append(f"\n📋 欄位（{len(d['fields_schema'])} 個）：")
        for fname, info in d["fields_schema"].items():
            req = " *" if info.get("required") else ""
            lines.append(f"   {fname}{req}  ({info.get('type', 'str')})  例：{info.get('example', 'N/A')}")
    if d.get("validation"):
        lines.append(f"\n✅ 成功判定：{d['validation']}")
    if d.get("common_pitfalls"):
        lines.append("\n⚠️ 常見錯誤：")
        for p in d["common_pitfalls"]:
            lines.append(f"   - {p}")
    if d.get("source_videos"):
        lines.append("\n🎥 來源影片：")
        for v in d["source_videos"][:10]:
            lines.append(f"   - {v}")
    return "\n".join(lines)


def merge_erp_workflows(source_task_names: str, target_task_name: str):
    """把多個已學的 ERP workflow **合併成一份端到端主檔**。
    - source_task_names：要合併的任務名，逗號分隔（例：「樣品開發,開發部_周邊」）
    - target_task_name：新主檔名稱（例：「開發部完整」）
    內部會讀取每個 source 的 _raw/ 資料夾所有個別影片分析（而不是已合併的 summary），
    讓 Gemini 依業務邏輯重新排序、去重、合併 fields_schema，產出最終 workflow。"""
    target = (target_task_name or "").strip()
    if not target:
        return "錯誤：target_task_name 不能空"
    sources = [s.strip() for s in (source_task_names or "").split(",") if s.strip()]
    if len(sources) < 1:
        return "錯誤：source_task_names 至少要 1 個"
    _ensure_erp_dirs()

    all_raws = []
    sources_found = []
    for s in sources:
        raw_dir = os.path.join(_ERP_WORKFLOW_DIR, f"{s}_raw")
        if not os.path.isdir(raw_dir):
            main_path = os.path.join(_ERP_WORKFLOW_DIR, f"{s}.json")
            if os.path.exists(main_path):
                try:
                    with open(main_path, "r", encoding="utf-8") as _f:
                        all_raws.append(json.load(_f))
                    sources_found.append(s)
                except Exception as e:
                    logger.warning("讀 %s 失敗：%s", main_path, e)
            continue
        for f in sorted(os.listdir(raw_dir)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, f), "r", encoding="utf-8") as _f:
                        all_raws.append(json.load(_f))
                except Exception as _e:
                    logger.debug("讀 raw 失敗 %s：%s", f, _e)
        sources_found.append(s)

    if not all_raws:
        return f"❌ 沒有找到任何 raw 分析。來源：{sources}"
    print(f"[merge] 從 {len(sources_found)} 個任務讀到 {len(all_raws)} 份影片分析")

    merge_prompt = (
        f"以下是多份 ERP 教學影片的個別分析結果，都屬於「{target}」這個大流程。\n"
        f"請合併成一份**端到端完整** workflow JSON，代表從頭到尾的所有操作步驟。\n\n"
        "合併規則：\n"
        "  - 按**業務邏輯順序**重排步驟（例如：先建資料 → 建立單據 → 執行 → 完工 → 出貨）\n"
        "  - 去除重複步驟（多支影片都教同樣的動作合成一步）\n"
        "  - fields_schema 合併所有影片提到的欄位（同名欄位取最詳細版本）\n"
        "  - video_summary 改成整體流程的 40 字摘要\n"
        "  - menu_path 若多個路徑並存，列主流程 + 子流程\n"
        "  - source_videos：列所有來源影片名稱\n"
        f'  - task_name 設為「{target}」\n'
        "只回純 JSON，不要 markdown 圍欄。\n\n"
        "個別分析輸入：\n" + json.dumps(all_raws, ensure_ascii=False, indent=2)[:50000]
    )
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[merge_prompt])
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return f"❌ Gemini 沒回 JSON（回了 {len(text)} 字）"
        merged = json.loads(m.group(0))
    except Exception as e:
        return f"❌ 合併失敗：{type(e).__name__}: {e}"

    merged["task_name"] = target
    merged["_merged_from"] = sources_found
    merged["_merged_at"] = datetime.now().isoformat(timespec="seconds")
    merged["_total_source_analyses"] = len(all_raws)

    out_path = os.path.join(_ERP_WORKFLOW_DIR, f"{target}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return (f"✅ 合併完成：「{target}」\n"
            f"   來源：{', '.join(sources_found)}（{len(all_raws)} 份個別分析）\n"
            f"   最終 {len(merged.get('steps', []))} 個步驟\n"
            f"   fields_schema 含 {len(merged.get('fields_schema', {}))} 個欄位\n"
            f"   主檔：{out_path}\n"
            f"   用 show_erp_workflow(\"{target}\") 看詳情")


def delete_erp_workflow(task_name: str):
    """刪除某個已學的 ERP 任務。"""
    path = os.path.join(_ERP_WORKFLOW_DIR, f"{task_name}.json")
    if not os.path.exists(path):
        return f"找不到「{task_name}」"
    try:
        os.remove(path)
        raw_dir = os.path.join(_ERP_WORKFLOW_DIR, f"{task_name}_raw")
        if os.path.isdir(raw_dir):
            import shutil as _sh
            _sh.rmtree(raw_dir)
        return f"✅ 已刪除「{task_name}」"
    except Exception as e:
        return f"刪除失敗：{e}"
