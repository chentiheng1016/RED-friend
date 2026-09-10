"""教學影片理解 — 把影片的「旁白語意」與「畫面動作」結合成連貫教學內容。

核心要求：不是語音逐字稿、也不是畫面描述，而是兩者**對齊融合** —
每個步驟都把「說了什麼」對應到「同時做了什麼」，整理出沒看過影片的人
也能照著做的教學內容。三個層次共用同一條 Gemini 多模態管線：

1. generate_from_media()：底層管線。小檔直接 inline bytes，大檔走
   Gemini Files API（上傳 → 等 ACTIVE → 生成 → 刪除）。修掉舊路徑
   一律 Part.from_bytes inline、撞 Gemini ~20 MB 請求上限的問題。
2. watch_teaching_video()：LLM tool。給本機路徑或 Drive 檔案連結，
   回傳時間戳對齊的連貫教學整理（可帶 focus 針對性提問）。
3. drive_ingest_prompt_for()：rag_sync 夜跑抽 Drive 影音時用的 prompt —
   影片用語意×動作融合版，音檔維持轉錄版，讓建進 RAG 的 chunk
   本身就有教學連貫性。

成本注意：影片 token 量大（預設解析度每秒約數百 token，30 分鐘影片
約數十萬 input token）。各呼叫端有自己的斷路器：drive_sync 走
RAG_MEDIA_DAILY_COST_LIMIT_USD 每日預算；tool 路徑記在 cost_tracker
（caller=video_understanding.watch_teaching_video）供看板與告警。

Module-level import 刻意保持輕量（os/re/tempfile + env_utils +
logging_and_paths）：drive_sync 的 _ExtractPool spawn worker 會 lazy
import 本模組，重依賴（gemini_client、google_auth、erp、file_ops、
prompt_injection）都留在函式內。
"""
from __future__ import annotations

import os
import re
import tempfile

from agent_core.env_utils import env_bool, env_int
from agent_core.logging_and_paths import logger

# Gemini inline 請求總上限約 20 MB；留 buffer 給 prompt 與 base64 膨脹。
_INLINE_MEDIA_MAX_BYTES = env_int(
    "RED_MEDIA_INLINE_MAX_BYTES", 15 * 1024 * 1024, min_value=0
)
# Gemini Files API 單檔上限 2 GB。
_FILES_API_MAX_BYTES = 2 * 1024 * 1024 * 1024

# ── 深度模式（_deep_teaching_analysis 混合關鍵幀路徑）用的模型 ──
# 深度模式用「最強的影片模型」：操作/螢幕理解比 flash 高一個等級。
# ⚠️ 別再釘具體版本：gemini-3-pro-preview（2026-06）、gemini-2.5-pro（2026-07-10
# 批次跑到一半）都被 Google 下架過，且「還列在 models.list」≠「呼叫不 404」。
# 用 gemini-pro-latest 別名追當前 pro（與艦隊 gemini-flash-latest 同一防退場慣例）。
_DEEP_MODEL = os.environ.get("RED_VIDEO_DEEP_MODEL", "").strip() or "gemini-pro-latest"

# 混合關鍵幀深度分析參數：螢幕細節靠「關鍵幀當高解析圖片」抽出（影片 frame 讀不清
# 小字、media_resolution 對影片無效；同畫面當圖片+HIGH 可切塊放大讀清欄位值）。
_SCREEN_MAX_KEYFRAMES = env_int("RED_VIDEO_SCREEN_MAX_KEYFRAMES", 150, min_value=10, max_value=600)
_SCREEN_BATCH = env_int("RED_VIDEO_SCREEN_BATCH", 8, min_value=1, max_value=24)
_SCREEN_MIN_INTERVAL_S = env_int("RED_VIDEO_SCREEN_MIN_INTERVAL_S", 4, min_value=1)
_SCREEN_MAX_INTERVAL_S = env_int("RED_VIDEO_SCREEN_MAX_INTERVAL_S", 15, min_value=2)
# 去重門檻：跟前一張保留幀的「平均每通道像素差」< 此值才算靜態重複而丟掉。
# 偏小（保守）→ 寧可多留幾張也別漏掉欄位變化的畫面（總數另有上限把關，不怕燒爆）。
_SCREEN_DEDUP_DIFF = env_int("RED_VIDEO_SCREEN_DEDUP_DIFF", 2, min_value=0, max_value=64)
_SCREEN_EXTRACT_TIMEOUT_S = env_int("RED_VIDEO_SCREEN_EXTRACT_TIMEOUT_S", 600, min_value=30)

# ── 本機優先 gating ──
# OCR gate（預設開）：關鍵幀先過 macOS Vision OCR（免費）。文字充足的幀直接用
# 本機 OCR 文字，只把「OCR 讀不出多少字、可能承載非文字資訊（滑鼠動作、實拍）」的
# 幀送 Gemini。ERP/Excel/生管日報畫面是純文字介面，省掉大部分螢幕讀取的雲端呼叫。
# Vision 不可用（非 macOS / 沒 ocrmac / 解圖失敗）時每幀自動退回送雲端 → 行為等同
# 關閉、絕不擋主流程（見 _split_frames_by_local_ocr 的 try/except）。要關掉設
# RED_VIDEO_LOCAL_OCR_GATE=0。
_LOCAL_OCR_GATE = env_bool("RED_VIDEO_LOCAL_OCR_GATE", True)
_OCR_RICH_MIN_CHARS = env_int("RED_VIDEO_OCR_RICH_MIN_CHARS", 80, min_value=1)
# 本機高精度 ASR（agent_core.local_asr，mlx-whisper）：可用時把逐字稿當第三份
# 素材餵 SOP 整合，並用「旁白講到操作動詞」的時間點觸發補抽幀（比均勻取樣準）。
_LOCAL_ASR = env_bool("RED_VIDEO_LOCAL_ASR", False)
_ASR_TRIGGER_MAX = env_int("RED_VIDEO_ASR_TRIGGER_MAX", 40, min_value=0, max_value=200)
_ASR_TRIGGER_NEAR_S = 2.0  # 觸發點離既有幀 <2s 就不補，避免重複畫面

# Files API 上傳用顯式 mime 設定，但暫存檔副檔名仍要對 — 部分工具鏈
# （與人類除錯）靠它判型。
_MIME_SUFFIX = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/webm": ".webm",
    "video/mpeg": ".mpeg",
    "video/mp2t": ".ts",
    "video/x-flv": ".flv",
    "video/x-ms-wmv": ".wmv",
    "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
}


def _suffix_for_mime(mime_type: str) -> str:
    suffix = _MIME_SUFFIX.get((mime_type or "").lower())
    if suffix:
        return suffix
    import mimetypes

    return mimetypes.guess_extension(mime_type or "") or ".bin"


# ────────────────────────────────────────────────────────────────────
# Prompts — 語意×動作融合是本模組存在的理由，集中在這裡好調
# ────────────────────────────────────────────────────────────────────

_TEACHING_OUTPUT_SPEC = (
    "請用繁體中文輸出 markdown，結構如下：\n\n"
    "# 影片教學內容整理\n\n"
    "## 一、影片總覽\n"
    "- 主題與教學目標（這支影片在教什麼）\n"
    "- 出現的工具／材料／機台／軟體\n"
    "- 適合對象與前置條件\n\n"
    "## 二、分段教學步驟\n"
    "依影片時間順序分段，每段格式：\n"
    "### 步驟 N［起始–結束時間戳］步驟名稱\n"
    "- **旁白講解**：這段話者說了什麼重點（保留原語言關鍵詞）\n"
    "- **畫面動作**：畫面實際展示的操作細節（位置、手法、參數、按了什麼）\n"
    "- **整合說明**：把語意與動作融合成一段連貫的教學指示\n"
    "- **注意**：旁白特別強調、或畫面有做但旁白沒講的細節（如有）\n\n"
    "## 三、關鍵細節與常見錯誤\n"
    "- 旁白強調的訣竅、容易做錯的地方、安全注意事項\n\n"
    "## 四、連貫流程總結\n"
    "用一段話把整個教學流程從頭到尾串起來，"
    "講清楚步驟之間的銜接邏輯（為什麼先做 A 再做 B）。\n\n"
    "要求：\n"
    "- 時間戳用 mm:ss，超過一小時用 h:mm:ss\n"
    "- 語音聽不清楚標註（聽不清）；畫面與旁白矛盾時兩者都寫出來並標註\n"
    "- 不要編造影片中沒有的內容\n"
)

_TEACHING_PROMPT = (
    "你是教學影片分析專家。請完整看這支影片，把「旁白／對話的語意」與"
    "「畫面中的實際動作」結合起來分析 — 不要只做語音轉錄，也不要只描述畫面："
    "每個步驟都要把『說了什麼』對應到『同時做了什麼』，"
    "整理成沒看過影片的人也能照著做的連貫教學內容。\n\n"
    + _TEACHING_OUTPUT_SPEC
)

_VIDEO_INGEST_PROMPT = (
    "這是一支要建入檢索系統的影片（可能是教學、操作示範或會議錄影）。\n"
    "請把「旁白與對話的語意」和「畫面中的實際動作」結合起來抽取內容，"
    "不要只做語音逐字稿，也不要只描述畫面。輸出純文字：\n"
    "1. 第一行：影片主題一句話。\n"
    "2. 然後依時間順序分段，每段開頭標 [mm:ss]（超過一小時用 h:mm:ss），"
    "用一段連貫文字寫出該段「說了什麼＋同時做了什麼」，"
    "保留原語言的人名、料號、工具／機台名、參數、畫面上可見文字等關鍵詞。\n"
    "3. 最後一段：整體流程總結，把步驟之間的銜接邏輯講清楚。\n"
    "語音聽不清楚標註（聽不清）；不要編造影片裡沒有的內容。"
)

_AUDIO_INGEST_PROMPT = (
    "Transcribe this audio file's spoken content as completely as possible, "
    "preserving original languages and names. If there is no speech, "
    "write a short factual description of what is heard instead."
)


def teaching_video_prompt(focus: str = "") -> str:
    """組出教學影片分析 prompt；focus 給了就在最前面加一節針對性回答。"""
    focus = (focus or "").strip()
    if not focus:
        return _TEACHING_PROMPT
    return (
        _TEACHING_PROMPT
        + "\n另外，使用者特別想知道：「" + focus + "」\n"
        "請在最開頭加一節「## 針對提問的回答」，引用影片時間戳直接回答這個問題，"
        "之後再附上完整教學整理。"
    )


def drive_ingest_prompt_for(mime_type: str) -> str:
    """rag_sync 抽 Drive 影音用的 prompt：影片走語意×動作融合，音檔走轉錄。"""
    if (mime_type or "").lower().startswith("video/"):
        return _VIDEO_INGEST_PROMPT
    return _AUDIO_INGEST_PROMPT


# ────────────────────────────────────────────────────────────────────
# 底層管線
# ────────────────────────────────────────────────────────────────────

def _overview_fps(duration_s: float | None) -> float:
    """全片 pass（分段地圖／逐字稿）的取樣率：依片長把總 frame 數控在 token 預算內，
    長片自動降到 <1 fps（fps 接受 float），避免整片 token 爆掉 400（教育訓練影片常
    1–2 小時）。音檔不受 fps 影響仍完整 → 逐字稿照樣一字不漏。片長未知回 1.0。
    例：2hr → ~0.3fps、1hr → ~0.6fps、<37min → 1.0。"""
    if not duration_s or duration_s <= 0:
        return 1.0
    return max(0.1, min(1.0, 2200.0 / float(duration_s)))


def generate_from_media(*, mime_type: str, prompt: str, data: bytes | None = None,
                        path: str | None = None, model: str | None = None,
                        caller: str = "") -> str:
    """用 Gemini 多模態讀一個影音檔，回傳生成文字（已 strip）。

    data / path 必須擇一：
      - data：小於 RED_MEDIA_INLINE_MAX_BYTES（預設 15 MB）直接 inline，
        否則落地暫存檔走 Files API（用完即刪）。
      - path：一律走 Files API，不把大檔整個讀進記憶體。
    caller：成本歸戶標籤，原樣傳給 cost_tracker — drive_sync 的每日預算
    斷路器按這個字串對帳，從 ingest 進來時不能省。
    """
    if (data is None) == (path is None):
        raise ValueError("generate_from_media: data 與 path 必須擇一提供")

    from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate

    use_model = model or GEMINI_MODEL

    if data is not None and len(data) <= _INLINE_MEDIA_MAX_BYTES:
        from google.genai import types

        part = types.Part.from_bytes(data=data, mime_type=mime_type)
        resp = _gemini_generate(model=use_model, contents=[part, prompt], caller=caller)
        return (resp.text or "").strip()

    tmp_path = None
    if path is None:
        if len(data) > _FILES_API_MAX_BYTES:
            raise ValueError(
                f"媒體檔 {len(data) / 1024 / 1024:.0f} MB 超過 Gemini Files API 2 GB 上限"
            )
        fd, tmp_path = tempfile.mkstemp(
            suffix=_suffix_for_mime(mime_type), prefix="red_media_"
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        path = tmp_path
    try:
        return _generate_via_files_api(path, mime_type, prompt, use_model, caller)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _upload_and_wait(client, path: str, mime_type: str):
    """上傳媒體檔到 Gemini Files API 並等到 ACTIVE。

    成功後呼叫端負責事後 delete；等待失敗（逾時/處理失敗）這裡自己清，
    不留孤兒檔占 Files API 配額。
    """
    from agent_core.gemini_client import _wait_for_file_ready, upload_file

    uploaded = upload_file(client, path, mime_type=mime_type)
    try:
        return _wait_for_file_ready(uploaded)
    except Exception:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass
        raise


def _uploaded_video_part(uploaded, mime_type: str, *, fps: float | None = None):
    """把已上傳的檔包成 Part；帶 fps 就以該取樣率看（長片降 fps 控 token 用）。"""
    from google.genai import types

    metadata = types.VideoMetadata(fps=fps) if fps else None
    return types.Part(
        file_data=types.FileData(
            file_uri=uploaded.uri,
            mime_type=(getattr(uploaded, "mime_type", None) or mime_type or None),
        ),
        video_metadata=metadata,
    )


def _generate_via_files_api(path: str, mime_type: str, prompt: str,
                            model: str, caller: str) -> str:
    from agent_core.gemini_client import _gemini_generate, _get_gemini_client

    size = os.path.getsize(path)
    if size > _FILES_API_MAX_BYTES:
        raise ValueError(
            f"媒體檔 {size / 1024 / 1024:.0f} MB 超過 Gemini Files API 2 GB 上限"
        )
    client = _get_gemini_client()
    uploaded = _upload_and_wait(client, path, mime_type)
    try:
        resp = _gemini_generate(model=model, contents=[uploaded, prompt], caller=caller)
        return (resp.text or "").strip()
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as exc:
            logger.warning("Gemini 上傳媒體檔清理失敗（%s）", exc)


# ────────────────────────────────────────────────────────────────────
# 本地媒體證據（ffmpeg/ffprobe — 沒裝就回 None，絕不擋主流程）
# ────────────────────────────────────────────────────────────────────

def _ffprobe_duration_seconds(path: str) -> float | None:
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────
# 深度模式（混合關鍵幀）— 螢幕細節走關鍵幀高解析圖片、旁白走影片 pass、整合成 SOP
# ────────────────────────────────────────────────────────────────────

def _fmt_ts(seconds) -> str:
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def _keyframe_interval(duration_s: float | None) -> float:
    """關鍵幀取樣間隔（秒）：把總幀數控在 _SCREEN_MAX_KEYFRAMES 附近，夾在 [min,max]。
    片長未知回 max（保守少抽）。"""
    if not duration_s or duration_s <= 0:
        return float(_SCREEN_MAX_INTERVAL_S)
    raw = duration_s / _SCREEN_MAX_KEYFRAMES
    return float(min(_SCREEN_MAX_INTERVAL_S, max(_SCREEN_MIN_INTERVAL_S, raw)))


def _frame_signature(png_path: str) -> list | None:
    """32×32 縮圖的 RGB 像素 — 給關鍵幀去重比對。比 1-bit ahash 穩：能分辨不同顏色
    （ahash 對純色畫面一律回 0、不同色撞同 hash），也保得住小區塊（單一欄位）的變化。"""
    try:
        from PIL import Image

        img = Image.open(png_path).convert("RGB").resize((32, 32))
        return list(img.getdata())
    except Exception:
        return None


def _frame_diff(a: list | None, b: list | None) -> float:
    """兩張 signature 的平均每通道絕對差（0–255）。任一缺就回 255（視為不同、不去重）。"""
    if not a or not b or len(a) != len(b):
        return 255.0
    total = 0
    for (r1, g1, b1), (r2, g2, b2) in zip(a, b):
        total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
    return total / (len(a) * 3)


def _extract_keyframes(path: str, duration_s: float | None,
                       out_dir: str) -> list[tuple[float, str]]:
    """週期抽幀 → 去近似重複 → 上限截斷。回 [(秒, png路徑)]。
    沒 ffmpeg／抽取失敗回 []（呼叫端退化成只看旁白，不空手）。"""
    import glob
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        return []
    interval = _keyframe_interval(duration_s)
    pat = os.path.join(out_dir, "kf_%05d.png")
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", path,
             "-vf", f"fps=1/{interval:.3f},scale='min(1920,iw)':-2",
             "-q:v", "3", pat],
            capture_output=True, timeout=_SCREEN_EXTRACT_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("關鍵幀抽取失敗（%s），退化為只看旁白", exc)
        return []
    files = sorted(glob.glob(os.path.join(out_dir, "kf_*.png")))
    kept: list[tuple[float, str]] = []
    last_sig = None
    for i, f in enumerate(files):
        sig = _frame_signature(f)
        if (last_sig is not None and sig is not None
                and _frame_diff(last_sig, sig) < _SCREEN_DEDUP_DIFF):
            continue  # 跟前一張保留幀幾乎一樣 → 靜態重複，丟掉
        kept.append((round((i + 0.5) * interval, 1), f))
        if sig is not None:
            last_sig = sig
    # 去重後仍超過上限 → 均勻抽稀，避免一支超長影片燒爆螢幕讀取批次。
    if len(kept) > _SCREEN_MAX_KEYFRAMES:
        step = len(kept) / _SCREEN_MAX_KEYFRAMES
        kept = [kept[int(i * step)] for i in range(_SCREEN_MAX_KEYFRAMES)]
    return kept


# ── 本機 gating helpers（Phase 4）────────────────────────────────────

# 旁白裡的操作動詞 — 講到這些的當下，畫面大概率正在示範該操作，值得補抽幀。
_ACTION_VERB_RE = re.compile(
    r"(點選|點擊|按下|按一下|雙擊|選擇|輸入|填入|填寫|勾選|開啟|打開|儲存|存檔|"
    r"送出|執行|切換|刪除|新增|查詢|列印|匯出|下載|上傳|掃描|確認|登入|點|按)"
)


def _action_timestamps(segments: list[dict]) -> list[float]:
    """本機轉錄段落裡出現操作動詞的時間點（觸發補抽幀用）。
    只看保留段（幻覺段別觸發）；1 秒內去重；封頂 _ASR_TRIGGER_MAX。"""
    times: list[float] = []
    for seg in segments or []:
        if not seg.get("kept"):
            continue
        if _ACTION_VERB_RE.search(str(seg.get("text") or "")):
            try:
                times.append(max(0.0, float(seg.get("start", 0.0))))
            except (TypeError, ValueError):
                continue
    times.sort()
    dedup: list[float] = []
    for t in times:
        if not dedup or t - dedup[-1] >= 1.0:
            dedup.append(t)
    return dedup[:_ASR_TRIGGER_MAX]


def _extract_frames_at(path: str, times: list[float], out_dir: str,
                       existing: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """在指定秒數補抽單幀（轉錄觸發）。離既有幀 <_ASR_TRIGGER_NEAR_S 的時間點跳過；
    沒 ffmpeg / 抽取失敗逐點略過，不擋主流程。回新增的 [(秒, png路徑)]。"""
    import shutil
    import subprocess

    if not times or not shutil.which("ffmpeg"):
        return []
    have = [t for t, _ in existing]
    added: list[tuple[float, str]] = []
    for i, t in enumerate(times):
        if any(abs(t - h) < _ASR_TRIGGER_NEAR_S for h in have):
            continue
        out = os.path.join(out_dir, f"tg_{i:05d}.png")
        try:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin",
                 "-ss", f"{max(0.0, t):.2f}", "-i", path,
                 "-frames:v", "1", "-vf", "scale='min(1920,iw)':-2",
                 "-q:v", "3", out],
                capture_output=True, timeout=30,
            )
        except Exception:
            continue
        if os.path.exists(out):
            added.append((round(t, 1), out))
            have.append(t)
    return added


def _split_frames_by_local_ocr(
    frames: list[tuple[float, str]],
) -> tuple[str, list[tuple[float, str]]]:
    """本機 OCR gating：逐幀先跑 macOS Vision OCR（免費）。文字充足（≥
    _OCR_RICH_MIN_CHARS）的幀就地轉成文字區塊、不送雲端；其餘留給 Gemini
    （可能是實拍動作、圖形介面、OCR 讀不動的畫面）。回 (本機區塊文字, 剩餘幀)。"""
    from agent_core.vision_ocr import ocr_image

    blocks: list[str] = []
    remaining: list[tuple[float, str]] = []
    for ts, fpath in frames:
        try:
            result = ocr_image(fpath)
        except Exception:
            result = None
        text = (result or {}).get("text") or ""
        if len(text) >= _OCR_RICH_MIN_CHARS:
            conf = (result or {}).get("confidence")
            conf_s = f"，OCR信心 {conf:.2f}" if isinstance(conf, (int, float)) else ""
            blocks.append(f"### 【畫面 @ {_fmt_ts(ts)}（本機OCR{conf_s}）】\n{text}")
        else:
            remaining.append((ts, fpath))
    return "\n\n".join(blocks), remaining


def _local_transcript(path: str) -> tuple[str, list[dict]]:
    """本機高精度逐字稿（mlx-whisper＋glossary 術語修正）。回 (逐行稿, segments)；
    不可用/失敗回 ("", [])，絕不擋雲端主流程。"""
    try:
        from agent_core import local_asr

        if not local_asr.is_available():
            return "", []
        terms: list[str] = []
        try:
            from agent_core.asr_glossary import top_terms

            terms = top_terms()
        except Exception:
            terms = []
        result = local_asr.transcribe_local(path, glossary_terms=terms)
        if not result or not (result.get("text") or "").strip():
            return "", []
        formatted = local_asr.format_transcript(result)
        try:
            from agent_core.asr_glossary import correct_transcript

            corrected = correct_transcript(formatted)
            if corrected.get("ok"):
                formatted = corrected["text"]
        except Exception as exc:
            logger.warning("glossary 術語修正失敗（%s），用原始逐字稿", exc)
        return formatted, result.get("segments") or []
    except Exception as exc:
        logger.warning("本機 ASR 整合失敗（%s），走純雲端深度分析", exc)
        return "", []


def _screen_read_prompt(timestamps: list[str]) -> str:
    n = len(timestamps)
    ts = "、".join(timestamps)
    return (
        f"這是同一支 ERP 操作教學影片在 {n} 個時間點（依序：{ts}）擷取的螢幕截圖。\n"
        "請**逐張**處理，每張對應一個時間點，各輸出一個 markdown ### 區塊：\n"
        "1. 標時間點\n"
        "2. 這是哪個功能／視窗（視窗標題、功能表路徑）\n"
        "3. **逐欄位、逐按鈕、逐表格儲存格**抄出畫面上所有可見文字與數值："
        "欄位名＝填入值、下拉選單選項、勾選狀態、訊息、表格每一列內容，含小字，一字不漏。\n"
        "鐵則：只抄看得見的，不要摘要、不要推測、不要編造；看不清標〔模糊〕。"
    )


def _read_screens(frames: list[tuple[float, str]], model: str, caller: str) -> str:
    """關鍵幀分批當「HIGH 解析圖片」讀 — 把螢幕欄位／數值逐一抄出（影片 pass 做不到：
    影片每 frame 壓到 ~263 token，同畫面當圖片+HIGH 切塊放大到 ~1825 token 才讀得清）。"""
    if not frames:
        return ""
    from google.genai import types

    from agent_core.gemini_client import _gemini_generate

    blocks: list[str] = []
    for i in range(0, len(frames), _SCREEN_BATCH):
        batch = frames[i:i + _SCREEN_BATCH]
        contents: list = []
        labels: list[str] = []
        for ts, fpath in batch:
            try:
                with open(fpath, "rb") as fh:
                    data = fh.read()
            except Exception:
                continue
            contents.append(types.Part.from_bytes(data=data, mime_type="image/png"))
            labels.append(_fmt_ts(ts))
        if not contents:
            continue
        contents.append(_screen_read_prompt(labels))
        try:
            resp = _gemini_generate(
                model=model,
                contents=contents,
                # 圖片 HIGH 解析會切塊放大、讀清螢幕小字（對影片無效、對圖片才有效）。
                config=types.GenerateContentConfig(
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH),
                caller=caller,
            )
            txt = (resp.text or "").strip()
            if txt:
                blocks.append(txt)
        except Exception as exc:
            logger.warning("螢幕讀取批次（%s 起）失敗（%s），略過", _fmt_ts(batch[0][0]), exc)
    return "\n\n".join(blocks)


def _narration_flow_prompt() -> str:
    return (
        "看這支 ERP 操作教學影片，輸出兩部分（螢幕欄位小字另有高解析截圖在處理，"
        "這裡不必逐字抄畫面小字，專心在『聲音』與『動作順序』）：\n\n"
        "## 旁白逐字稿\n講者說的話，一字不漏，每段標時間。聽不清標〔聽不清〕。\n\n"
        "## 操作流程\n講者依時間先後做了哪些操作：開哪個畫面、點哪個按鈕／選單、"
        "在哪個欄位輸入、按什麼鍵存檔送出，每個動作標時間，重在動作順序與前後因果。"
    )


def _narration_and_flow(path: str, mime_type: str, duration_s: float | None,
                        model: str, caller: str) -> str:
    """一趟影片 pass：拿旁白逐字稿 + 操作動作順序（影片擅長時序與音檔；fps 隨片長自動降）。"""
    from agent_core.gemini_client import _gemini_generate, _get_gemini_client

    client = _get_gemini_client()
    uploaded = _upload_and_wait(client, path, mime_type)
    try:
        part = _uploaded_video_part(uploaded, mime_type, fps=_overview_fps(duration_s))
        resp = _gemini_generate(
            model=model, contents=[part, _narration_flow_prompt()], caller=caller)
        return (resp.text or "").strip()
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as exc:
            logger.warning("Gemini 上傳媒體檔清理失敗（%s）", exc)


def _sop_merge_prompt(focus: str, screens: str, narration: str,
                      local_transcript: str = "") -> str:
    foc = f"\n\n**特別聚焦**：{focus}——優先且最詳細地回答這點。" if focus else ""
    transcript_block = ""
    transcript_rule = ""
    if local_transcript:
        transcript_block = (
            "\n\n【素材C — 本機高精度旁白逐字稿（時間戳精確、專有名詞已過術語修正；"
            "標（低信度）的段落僅供參考）】\n" + local_transcript
        )
        transcript_rule = (
            "\n4. 素材C 的專有名詞與時間戳比素材B 可信，兩者矛盾時以 C 為準；"
            "動作先後順序仍以素材B 為準；C 標（低信度）的內容別當唯一依據。"
        )
    return (
        "你要把一支 ERP 操作教學影片整理成「新人可以照著做的 SOP」。手上素材：\n\n"
        "【素材A — 各時間點的精確螢幕內容（高解析 OCR 等級，欄位名／數值／選單可信）】\n"
        f"{screens or '（無螢幕截圖）'}\n\n"
        "【素材B — 旁白逐字稿與操作流程（動作先後順序可信）】\n"
        f"{narration or '（無旁白）'}"
        f"{transcript_block}\n\n"
        "整合成 markdown SOP：\n"
        "1. 開頭一句：這支影片教的是什麼操作。\n"
        "2. 依**操作步驟**逐步寫，每步包含：**【時間】**、**做什麼動作**、"
        "**在哪個畫面／填哪些欄位＝什麼值**（用素材A的精確欄位與值補實素材B的動作）、"
        "必要的說明或注意事項。\n"
        "3. 結尾：①關鍵欄位／預設值速查表 ②易錯點或前置條件。"
        + transcript_rule + "\n"
        "鐵則：只根據素材，不要編造；素材沒提到的別硬補；看不清的標〔模糊〕。" + foc
    )


def _deep_teaching_analysis(path: str, mime_type: str, focus: str,
                            model: str | None, caller: str) -> str:
    """混合關鍵幀深度分析：螢幕細節（欄位／數值／選單）走「關鍵幀當高解析圖片」逐幀讀，
    旁白與操作流程走一趟影片 pass，再整合成可照做的 SOP。

    為何混合：影片每 frame 一律被壓到 ~263 token、media_resolution 對影片無效 →
    螢幕小字讀不清；同一畫面改當「圖片＋HIGH」送會切塊放大到 ~1825 token、欄位值
    讀得清。所以螢幕交給圖片、聲音與動作順序交給影片，各取所長且比純影片深析更省。
    沒 ffmpeg（抽不出幀）就退化成只看旁白，絕不空手而回。
    """
    from agent_core.gemini_client import _gemini_generate

    use_model = model or _DEEP_MODEL  # deep 用最強 pro（quick 路徑才用便宜 flash）
    duration = _ffprobe_duration_seconds(path)

    tmp_dir = tempfile.mkdtemp(prefix="red_kf_")
    try:
        frames = _extract_keyframes(path, duration, tmp_dir)

        # 本機高精度逐字稿（預設關）：可用時當第三份素材，且用「旁白講到操作
        # 動詞」的時間點補抽幀 — 比純週期取樣更容易抓到操作當下的畫面。
        transcript_text = ""
        if _LOCAL_ASR:
            transcript_text, transcript_segments = _local_transcript(path)
            if transcript_segments:
                extra = _extract_frames_at(
                    path, _action_timestamps(transcript_segments), tmp_dir, frames)
                if extra:
                    frames = sorted(frames + extra, key=lambda f: f[0])
                    logger.info("轉錄觸發補抽 %d 張關鍵幀", len(extra))
        logger.info("混合深度分析：去重後抽出 %d 張關鍵幀", len(frames))

        # 本機 OCR gating（預設開，RED_VIDEO_LOCAL_OCR_GATE=0 可關）：
        # 文字充足的幀就地讀取（免費），其餘才送雲端。
        local_blocks = ""
        if _LOCAL_OCR_GATE and frames:
            local_blocks, frames = _split_frames_by_local_ocr(frames)
            if local_blocks:
                logger.info("本機 OCR gating：%d 張幀就地讀取、%d 張送雲端",
                            local_blocks.count("### 【畫面"), len(frames))
        screens = _read_screens(frames, use_model, caller)
        if local_blocks:
            screens = (local_blocks + "\n\n" + screens).strip()
        narration = _narration_and_flow(path, mime_type, duration, use_model, caller)
        if not screens and not narration and not transcript_text:
            return ""

        # 整合成 SOP（純文字 call）；失敗（例：模型高峰連環 503）就本地併接素材，
        # 不空手而回。
        merged = ""
        try:
            mresp = _gemini_generate(
                model=use_model,
                contents=[_sop_merge_prompt(focus, screens, narration,
                                            transcript_text)],
                caller=caller,
            )
            merged = (mresp.text or "").strip()
        except Exception as exc:
            logger.warning("SOP 整合 pass 失敗（%s），改本地併接素材", exc)
        if merged:
            return merged
        parts = []
        if narration:
            parts.append("# 旁白與操作流程\n" + narration)
        if screens:
            parts.append("# 螢幕內容（逐關鍵幀）\n" + screens)
        if transcript_text:
            parts.append("# 本機旁白逐字稿（術語已修正）\n" + transcript_text)
        return "\n\n".join(parts)
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────
# LLM tool — 看教學影片
# ────────────────────────────────────────────────────────────────────

_DRIVE_FILE_URL_RE = re.compile(r"/file/d/([A-Za-z0-9_\-]+)")
_DRIVE_OPEN_URL_RE = re.compile(r"[?&]id=([A-Za-z0-9_\-]+)")
_DRIVE_ID_ONLY_RE = re.compile(r"^[A-Za-z0-9_\-]{20,}$")


def _resolve_drive_file_id(source: str) -> str:
    """從 Drive 檔案連結 / open?id= 連結 / 純 file ID 字串抽出 file ID。"""
    s = (source or "").strip()
    m = _DRIVE_FILE_URL_RE.search(s)
    if m:
        return m.group(1)
    m = _DRIVE_OPEN_URL_RE.search(s)
    if m:
        return m.group(1)
    if _DRIVE_ID_ONLY_RE.match(s):
        return s
    return ""


def _drive_file_meta(file_id: str) -> dict:
    from agent_core.google_auth import get_service

    service = get_service("drive", "v3")
    return service.files().get(
        fileId=file_id,
        fields="id, name, size, mimeType",
        supportsAllDrives=True,
    ).execute()


def watch_teaching_video(video_source: str, focus: str = "", deep: bool = False) -> str:
    """看懂一支教學／操作示範影片：把旁白語意與畫面動作結合分析，輸出連貫的教學內容整理。

    跟 learn_erp_from_video（學成 ERP 自動化 workflow JSON）不同：這個工具是
    「看影片 → 整理成人可讀的教學文件」。適合被問「這支影片在教什麼」、
    「幫我把這支 SOP 影片整理成文件」、「影片裡某個步驟怎麼做」時呼叫。

    video_source: 本機影片路徑，或 Google Drive 檔案連結
                  （https://drive.google.com/file/d/... 或 open?id=...）或 Drive file ID。
                  資料夾連結不行，要單一影片檔。
    focus:        選填 — 特別想了解的問題（例：「針車怎麼穿線」），會優先針對性回答。
    deep:         選填，預設 False。True 走**深度模式（最高細節）**：用最強的 pro 影片
                  模型，抽關鍵幀當高解析圖片逐幀抄錄螢幕文字（欄位/數值/選單/小字），
                  另跑一趟影片 pass 拿旁白逐字稿與操作順序，最後整合成可照做的 SOP。
                  操作細節最準，但多花數倍～十幾倍時間與 token 成本。**要學會操作步驟、
                  整理 SOP、或快速模式漏細節時開**；一般「這影片在講什麼」用預設快速
                  模式即可。

    支援 mp4 / mov / avi / webm 等常見格式，最大 2 GB、長度建議 1 小時內。
    影片越長分析越久（數十秒到數分鐘），且有 Gemini token 成本 — 同一支影片
    別重複呼叫，先用前一次的整理結果回答。

    回傳：markdown 教學整理（時間戳分段、旁白×畫面動作整合說明、流程總結）。
    """
    source = (video_source or "").strip()
    if not source:
        return "錯誤：請給影片的本機路徑或 Drive 檔案連結。"

    from agent_core.file_ops import _clean_path
    from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted

    tmp_dir = None
    try:
        # Drive 連結（含 ://）丟進 path-safety 會 raise — 那不是錯誤，
        # 只代表這不是本機路徑，往下走 Drive 分支。
        try:
            local = _clean_path(source)
        except ValueError:
            local = ""
        if local and os.path.isfile(local):
            path = local
            display_name = os.path.basename(local)
            import mimetypes

            mime = mimetypes.guess_type(local)[0] or ""
            if not (mime.startswith("video/") or mime.startswith("audio/")):
                return (
                    f"錯誤：「{display_name}」看不出是影音檔"
                    f"（mime={mime or '未知'}）。請確認副檔名（mp4/mov/avi/webm…）。"
                )
            if os.path.getsize(path) > _FILES_API_MAX_BYTES:
                return f"錯誤：「{display_name}」超過 2 GB 上限。"
        else:
            file_id = _resolve_drive_file_id(source)
            if not file_id:
                return (
                    f"錯誤：找不到本機檔案，也看不出是 Drive 檔案連結：{source}\n"
                    "請給本機路徑、或 https://drive.google.com/file/d/<id> 形式的連結。"
                )
            try:
                meta = _drive_file_meta(file_id)
            except Exception as exc:
                return f"錯誤：讀不到 Drive 檔案資訊（{exc}）。請確認連結正確且有權限。"
            mime = meta.get("mimeType", "") or ""
            display_name = sanitize_for_llm(str(meta.get("name", file_id)))
            if mime == "application/vnd.google-apps.folder":
                return "錯誤：這是資料夾連結。請給單一影片檔的連結（…/file/d/…）。"
            if not (mime.startswith("video/") or mime.startswith("audio/")):
                return f"錯誤：「{display_name}」不是影音檔（mimeType={mime}）。"
            size = int(meta.get("size", 0) or 0)
            if size > _FILES_API_MAX_BYTES:
                return (
                    f"錯誤：「{display_name}」{size / 1024 / 1024:.0f} MB "
                    "超過 Gemini 2 GB 上限。"
                )
            from agent_core.erp import _download_drive_file

            tmp_dir = tempfile.mkdtemp(prefix="red_video_")
            path = os.path.join(tmp_dir, "video" + _suffix_for_mime(mime))
            if not _download_drive_file(file_id, path):
                return f"錯誤：下載「{display_name}」失敗，請稍後再試。"

        try:
            if deep and mime.startswith("video/"):
                analysis = _deep_teaching_analysis(
                    path=path,
                    mime_type=mime,
                    focus=focus,
                    model=None,
                    caller="video_understanding.watch_teaching_video",
                )
            else:
                analysis = generate_from_media(
                    mime_type=mime,
                    prompt=teaching_video_prompt(focus),
                    path=path,
                    caller="video_understanding.watch_teaching_video",
                )
        except Exception as exc:
            return f"錯誤：影片分析失敗（{type(exc).__name__}: {exc}）"
        if not analysis:
            return f"「{display_name}」分析不出內容（可能無聲且畫面無資訊）。"

        # 影片內容是外部資料 — 跟 drive_search 同規格：先淨化再標 untrusted 邊界。
        body = wrap_as_untrusted(sanitize_for_llm(analysis), "video_analysis")
        return (
            f"已看完影片「{display_name}」，"
            f"以下是旁白語意×畫面動作的整合教學整理：\n\n{body}"
        )
    finally:
        if tmp_dir:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)
