"""Google Cloud Video Intelligence 包裝 — 鏡頭切換 + 畫面文字的機器標註。

定位：video_understanding 深度管線的**低階證據源**，不是教學整理器。
Gemini 原生影片理解負責語意（旁白×動作融合）；本模組提供機器精度的
時間錨點 — SHOT_CHANGE_DETECTION 給鏡頭邊界（修正 LLM 時間戳漂移）、
TEXT_DETECTION 給畫面文字的精確出現區間（OCR 證據）。

可用性是三層檢查、全部優雅降級（回傳 dict 帶 available/reason，
絕不 raise 到呼叫端）：
1. lib：google-cloud-videointelligence 沒裝 → 不可用
2. 憑證：依序 RED_VIDEO_INTEL_SA_FILE → GOOGLE_APPLICATION_CREDENTIALS
   （ADC）→ repo 的 var/state/google/service_account.json
3. API：GCP 專案沒啟用 Video Intelligence API / 沒權限 → 把 Google
   回的錯誤原文（內含啟用 URL）放進 reason，使用者照著開即可

成本：shot detection 約 $0.05/分鐘、text detection 約 $0.15/分鐘，
比 Gemini 貴 — 所以預設關（RED_VIDEO_INTEL_ENABLE=0），深度管線
只在顯式開啟時才用。inline bytes 上限 RED_VIDEO_INTEL_MAX_BYTES
（預設 128 MB）；更大的檔要走 GCS URI，目前不支援。
"""
from __future__ import annotations

import os

from agent_core.env_utils import env_bool, env_int
from agent_core.logging_and_paths import logger

VIDEO_INTEL_ENABLED = env_bool("RED_VIDEO_INTEL_ENABLE", False)
_VIDEO_INTEL_MAX_BYTES = env_int(
    "RED_VIDEO_INTEL_MAX_BYTES", 128 * 1024 * 1024, min_value=0
)
_VIDEO_INTEL_TIMEOUT_S = env_int(
    "RED_VIDEO_INTEL_TIMEOUT_S", 600, min_value=30
)

_REPO_SA_RELPATH = os.path.join("var", "state", "google", "service_account.json")


def _unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "shots": [], "texts": []}


def _find_credentials_file() -> str:
    """依優先序找 SA 金鑰檔；ADC 環境變數存在時回空字串（讓 client 走 ADC）。"""
    explicit = (os.environ.get("RED_VIDEO_INTEL_SA_FILE") or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    if (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip():
        return ""  # ADC 自己會讀
    from agent_core.logging_and_paths import _SCRIPT_DIR

    repo_sa = os.path.join(_SCRIPT_DIR, _REPO_SA_RELPATH)
    if os.path.isfile(repo_sa):
        return repo_sa
    return ""


def _seconds(duration) -> float:
    """時間 offset → float 秒。

    新版 client（proto-plus）把 protobuf Duration 映射成 datetime.timedelta —
    它有 .seconds 但**沒有 .nanos**，直接讀 .seconds/.nanos 會把毫秒丟光
    （且 timedelta.seconds 只是「不含天」的秒數部分）。先走 total_seconds()，
    拿不到再退回 raw protobuf Duration 的 seconds/nanos。
    """
    try:
        return float(duration.total_seconds())
    except AttributeError:
        pass
    except Exception:
        return 0.0
    try:
        return float(duration.seconds) + float(getattr(duration, "nanos", 0)) / 1e9
    except Exception:
        return 0.0


def annotate_video(path: str, *, want_text: bool = True) -> dict:
    """跑 Video Intelligence 的 shot（+可選 text）標註。

    回傳：
      {"available": bool, "reason": str,
       "shots": [{"start_s": float, "end_s": float}, ...],
       "texts": [{"text": str, "start_s": float, "end_s": float}, ...]}
    任何失敗都回 available=False + reason，不往外丟例外 —
    深度管線把它當可有可無的證據源。
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return _unavailable(f"讀不到檔案：{exc}")
    if _VIDEO_INTEL_MAX_BYTES > 0 and size > _VIDEO_INTEL_MAX_BYTES:
        return _unavailable(
            f"檔案 {size / 1024 / 1024:.0f} MB 超過 inline 上限"
            f"（RED_VIDEO_INTEL_MAX_BYTES={_VIDEO_INTEL_MAX_BYTES}）；"
            "大檔需 GCS URI，尚未支援"
        )

    try:
        from google.cloud import videointelligence
    except ImportError:
        return _unavailable(
            "google-cloud-videointelligence 未安裝"
            "（pip install google-cloud-videointelligence）"
        )

    try:
        sa_file = _find_credentials_file()
        if sa_file:
            client = (
                videointelligence.VideoIntelligenceServiceClient
                .from_service_account_file(sa_file)
            )
        else:
            client = videointelligence.VideoIntelligenceServiceClient()
    except Exception as exc:
        return _unavailable(f"建立 Video Intelligence client 失敗：{exc}")

    features = [videointelligence.Feature.SHOT_CHANGE_DETECTION]
    if want_text:
        features.append(videointelligence.Feature.TEXT_DETECTION)

    try:
        with open(path, "rb") as f:
            content = f.read()
        operation = client.annotate_video(
            request={"features": features, "input_content": content}
        )
        result = operation.result(timeout=_VIDEO_INTEL_TIMEOUT_S)
        annotation = result.annotation_results[0]
    except Exception as exc:
        # API 未啟用 / 權限不足時 Google 的錯誤原文含啟用 URL，原樣帶出
        return _unavailable(f"Video Intelligence 標註失敗：{exc}")

    shots = [
        {
            "start_s": _seconds(s.start_time_offset),
            "end_s": _seconds(s.end_time_offset),
        }
        for s in (annotation.shot_annotations or [])
    ]
    texts = []
    for t in (annotation.text_annotations or []):
        try:
            seg = t.segments[0].segment
            texts.append({
                "text": t.text,
                "start_s": _seconds(seg.start_time_offset),
                "end_s": _seconds(seg.end_time_offset),
            })
        except Exception:
            logger.debug("Video Intelligence text annotation 缺 segment，略過")
    return {"available": True, "reason": "", "shots": shots, "texts": texts}
