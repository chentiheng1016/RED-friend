"""ffmpeg 取樣工具 — 影片抽幀與音軌抽取（純本地，不打 Gemini）。

drive_sync 的影片 ingest 用：本地均勻抽 N 幀給 Gemini Vision 做帶時間戳
的語意描述、抽音軌（mono mp3）給轉錄 — 取代「整檔 bytes inline 上傳」
（受 Gemini 20MB request 上限卡死，長片的畫面描述又淺）。

Gemini 呼叫**刻意留在** drive_sync._extract_media — cost_tracker 以呼叫
stack 推 caller，每日預算斷路器 _get_media_cost_budget_message() 查的就
是 "drive_sync._extract_media" 這個 caller；搬出來會逃出預算閘。

所有 subprocess 都帶 timeout 與 -nostdin；ffmpeg/ffprobe 不在 PATH 時
全部優雅回空值，呼叫端 fallback 到舊的整檔路徑。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from agent_core.logging_and_paths import logger

_PROBE_TIMEOUT_S = 30
_FRAME_TIMEOUT_S = 60
_AUDIO_TIMEOUT_S = 300

# 給 Gemini 的幀不需要原解析度 — 縮到寬 ≤1280 省上傳與 token。
# 註：filtergraph 內的逗號要跳脫（min(1280\,iw)），因為逗號是 filter 分隔符。
_SCALE_FILTER = "scale=min(1280\\,iw):-2"


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe_duration_s(video_path: str) -> float:
    """影片長度（秒）；探測失敗回 0.0（呼叫端視為不可取樣）。"""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, timeout=_PROBE_TIMEOUT_S, check=True,
        )
        return max(0.0, float(out.stdout.decode("utf-8", "replace").strip()))
    except Exception as exc:
        logger.debug("ffprobe duration 失敗（%s）：%s", video_path, exc)
        return 0.0


def sample_frames(
    video_path: str,
    max_frames: int = 8,
    *,
    max_width: int = 1280,
) -> list[tuple[float, bytes]]:
    """均勻取樣 N 幀，回 [(秒數, jpeg bytes), ...]。

    取樣點在 (i+0.5)/n 的位置 — 避開片頭黑幀與片尾淡出。
    任一幀失敗就跳過該幀（部分成功仍有價值）；全失敗回 []。
    """
    if not ffmpeg_available():
        return []
    duration = probe_duration_s(video_path)
    if duration <= 0:
        return []

    n = max(1, int(max_frames))
    scale = _SCALE_FILTER.replace("1280", str(int(max_width)))
    frames: list[tuple[float, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="red_vframes_") as tmpdir:
        for i in range(n):
            ts = duration * (i + 0.5) / n
            out_path = os.path.join(tmpdir, f"f{i:02d}.jpg")
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-ss", f"{ts:.2f}", "-i", video_path,
                        "-frames:v", "1", "-q:v", "3", "-vf", scale,
                        "-y", out_path,
                    ],
                    capture_output=True, timeout=_FRAME_TIMEOUT_S, check=True,
                )
                with open(out_path, "rb") as f:
                    jpg = f.read()
                if jpg:
                    frames.append((ts, jpg))
            except Exception as exc:
                logger.debug("抽幀失敗（%s @ %.1fs）：%s", video_path, ts, exc)
    return frames


def extract_audio_track(video_path: str, max_minutes: int = 30) -> bytes:
    """抽音軌成 mono 16kHz 48kbps mp3（轉錄夠用、體積小）。

    超過 max_minutes 只取前段（-t 截斷）；沒有音軌或失敗回 b""。
    48kbps mono ≈ 0.36 MB/分鐘 → 30 分鐘 ≈ 11MB，在 Gemini inline 上限內。
    """
    if not ffmpeg_available():
        return b""
    with tempfile.TemporaryDirectory(prefix="red_vaudio_") as tmpdir:
        out_path = os.path.join(tmpdir, "audio.mp3")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-i", video_path, "-vn",
                    "-ac", "1", "-ar", "16000", "-b:a", "48k",
                    "-t", str(int(max_minutes) * 60),
                    "-f", "mp3", "-y", out_path,
                ],
                capture_output=True, timeout=_AUDIO_TIMEOUT_S, check=True,
            )
            with open(out_path, "rb") as f:
                return f.read()
        except Exception as exc:
            logger.debug("抽音軌失敗（%s）：%s", video_path, exc)
            return b""


def format_ts(seconds: float) -> str:
    """93.4 → '01:33'，給幀標籤用。"""
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"
