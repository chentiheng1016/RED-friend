"""YouTube transcript extraction.

Tries captions first
via youtube-transcript-api; falls back to yt_dlp + Gemini audio listen.
"""
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from agent_core.gemini_client import (
    GEMINI_MODEL,
    _get_gemini_client,
    _gemini_generate,
    _wait_for_file_ready,
    upload_file,
)
from agent_core.logging_and_paths import logger

_MAX_AUDIO_SIZE_MB = 50

# When invoked from inside the Telegram daemon (or any other interactive
# daemon), don't let the audio fallback hang for minutes — the user is
# staring at an unresponsive chat window. Switch to short timeouts so
# the call fails fast and the bot can move on. The interactive REPL path
# keeps the longer defaults so summarizing a whole long lecture still works.
_IS_DAEMON_MODE = os.environ.get("AGENT_DAEMON_MODE") == "1"
_DAEMON_FILE_POLL_TIMEOUT_S = 30
_DAEMON_GENERATE_MAX_ATTEMPTS = 2
_DAEMON_TRANSCRIPT_HARD_CAP_S = 60

_PROTECTED_STREAMING_DOMAINS: dict[str, str] = {
    "netflix.com": "Netflix",
    "disneyplus.com": "Disney+",
    "hulu.com": "Hulu",
    "max.com": "Max",
    "hbomax.com": "HBO Max",
    "primevideo.com": "Prime Video",
    "tv.apple.com": "Apple TV",
    "paramountplus.com": "Paramount+",
    "peacocktv.com": "Peacock",
    "crunchyroll.com": "Crunchyroll",
}
_PROTECTED_STREAMING_PATH_DOMAINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "amazon.com": ("Prime Video", ("/gp/video", "/video/detail", "/primevideo")),
}


def _host_matches_domain(host: str, domain: str) -> bool:
    """Return True when host is exactly domain or one of its subdomains."""

    return host == domain or host.endswith(f".{domain}")


def _protected_streaming_provider(url: str) -> str:
    """Identify mainstream subscription streaming domains that should not be
    sent to yt-dlp.

    These services usually deliver copyrighted catalog content through DRM and
    license-server flows. Blocking at the wrapper level keeps the assistant from
    accidentally treating a preview, sign-in page, or DRM error as a successful
    "download" of protected content.
    """

    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    host = (parsed.hostname or "").lower().strip(".")
    path = (parsed.path or "").lower()
    if not host:
        return ""

    for domain, provider in _PROTECTED_STREAMING_DOMAINS.items():
        if _host_matches_domain(host, domain):
            return provider

    for domain, (provider, path_markers) in _PROTECTED_STREAMING_PATH_DOMAINS.items():
        if _host_matches_domain(host, domain) and any(marker in path for marker in path_markers):
            return provider
    return ""


def _protected_streaming_download_message(provider: str) -> str:
    """Return a concise, user-facing refusal for protected streaming downloads."""

    return (
        f"❌ {provider} 影片下載已被小紅安全規則攔下。\n"
        "這類串流平台通常受 DRM 與授權條款保護，我不能協助下載完整影片、"
        "擷取金鑰，或繞過授權限制。\n"
        "我可以改幫您處理：公開預告片、您自有影片、您提供的合法片段/字幕，"
        "或檢查合法的 EME/DRM 播放流程。"
    )


def _video_source_label(url: str) -> str:
    """Return a user-facing source label for a media URL."""

    lowered = (url or "").lower()
    provider = _protected_streaming_provider(url)
    if provider:
        return f"{provider} 影片"
    if "facebook.com" in lowered or "fb.watch" in lowered:
        return "Facebook 影片"
    if "instagram.com" in lowered:
        return "Instagram 影片"
    if "youtu.be" in lowered or "youtube.com" in lowered:
        return "YouTube 影片"
    return "線上影片"


def _friendly_video_error(source_label: str, code: str, message: str) -> str:
    """Make extractor errors actionable for Telegram-sized replies."""

    compact = re.sub(r"\s+", " ", message or "未提供錯誤訊息").strip()
    if "Cannot parse data" in compact and "Facebook" in source_label:
        return (
            f"❌ {source_label}下載失敗（{code}）：Facebook 這次回傳的頁面 yt-dlp 解析不到影片資料。\n"
            "   這通常是 share/reel 轉址、登入/權限、或 Facebook 頁面臨時變動造成。\n"
            "   請再試一次，或改貼瀏覽器實際打開後的 `facebook.com/reel/<數字>` 網址。"
        )
    return f"❌ {source_label}下載失敗（{code}）：{compact}"


def download_youtube_audio(
    url: str,
    processing_mode: str = "podcast",
    pitch_adjust: str = "0",
    output_dir: str = "",
    music_format: str = "m4a",
) -> str:
    """下載並轉檔 YouTube 音訊到本機資料夾。

    Args:
        url: YouTube URL。支援 watch / youtu.be / shorts / embed。
        processing_mode: meeting=16k mono WAV（轉錄用）、
            podcast=MP3 192k + loudnorm/highpass/silence trim（聆聽用）、
            music=M4A/FLAC + metadata/封面（音樂保存用）。
        pitch_adjust: 音高調整，單位為半音；也支援 Full Tone Up/Down、
            Half Tone Up/Down。預設 0 不變調。
        output_dir: 輸出資料夾。空字串預設為 ~/Downloads。
        music_format: music 模式使用 m4a 或 flac。

    Returns:
        使用者可讀的下載結果與絕對路徑清單。
    """
    target_dir = Path(output_dir).expanduser() if output_dir else Path.home() / "Downloads"
    try:
        from agent_core.youtube_audio_engine import process_youtube_audio_sync
        result = process_youtube_audio_sync(
            url=url,
            processing_mode=processing_mode,
            pitch_adjust=pitch_adjust,
            output_dir=target_dir,
            music_format=music_format,
        )
    except Exception as exc:
        return f"❌ YouTube 音訊下載失敗：{type(exc).__name__}: {exc}"

    if result.get("status") != "success":
        code = result.get("error_code") or "unknown_error"
        message = result.get("error_message") or "未提供錯誤訊息"
        if code == "dependency_missing":
            return (
                "❌ YouTube 音訊工具缺少執行環境依賴。\n"
                f"   {message}\n"
                "   請在 RED repo 執行 `make install` 或 `./setup.sh --no-plists` 後重試。"
            )
        return f"❌ YouTube 音訊下載失敗（{code}）：{message}"

    files = result.get("files") or []
    metadata = result.get("metadata") or {}
    title = metadata.get("title") or "YouTube 音訊"
    if not files:
        return f"❌ YouTube 音訊處理完成但沒有產生檔案：{title}"

    lines = [
        f"✅ 已下載並轉檔 YouTube 音訊：{title}",
        f"模式：{result.get('mode_applied', processing_mode)}",
        "檔案：",
    ]
    for index, file_info in enumerate(files, start=1):
        try:
            duration = float(file_info.get("duration", 0) or 0)
        except (TypeError, ValueError):
            duration = 0.0
        path = file_info.get("path", "")
        lines.append(f"{index}. {path}（{duration:.1f} 秒）")
    lines.append(result.get("compliance", "Personal/Educational Use Only"))
    return "\n".join(lines)


def download_youtube_video(
    url: str,
    resolution: str = "1080p",
    output_dir: str = "",
    pitch_adjust: str = "0",
    split_long_content: bool = False,
) -> str:
    """下載 YouTube/Facebook 等 yt-dlp 支援的影片到本機資料夾。

    Args:
        url: 影片 URL。支援 YouTube，也可處理公開 Facebook Reels 等
            yt-dlp 支援的來源。
        resolution: 目標解析度，例如 1080p、720p。
        output_dir: 輸出資料夾。空字串預設為 ~/Downloads。
        pitch_adjust: 選填，調整影片音軌音高；預設 0 不變調。
        split_long_content: 是否將超過 20 分鐘的影片切段。

    Returns:
        使用者可讀的下載結果與絕對路徑清單。
    """
    return download_online_video(
        url=url,
        resolution=resolution,
        output_dir=output_dir,
        pitch_adjust=pitch_adjust,
        split_long_content=split_long_content,
    )


def download_online_video(
    url: str,
    resolution: str = "1080p",
    output_dir: str = "",
    pitch_adjust: str = "0",
    split_long_content: bool = False,
) -> str:
    """下載 yt-dlp 支援的線上影片到本機資料夾，不透過任意 shell 指令。

    Args:
        url: 影片 URL。支援 YouTube、公開 Facebook Reels，以及其他
            yt-dlp extractor 支援的公開影片頁。
        resolution: 目標解析度，例如 1080p、720p。
        output_dir: 輸出資料夾。空字串預設為 ~/Downloads。
        pitch_adjust: 選填，調整影片音軌音高；預設 0 不變調。
        split_long_content: 是否將超過 20 分鐘的影片切段。

    Returns:
        使用者可讀的下載結果與絕對路徑清單。
    """
    source_label = _video_source_label(url)
    provider = _protected_streaming_provider(url)
    if provider:
        return _protected_streaming_download_message(provider)

    target_dir = Path(output_dir).expanduser() if output_dir else Path.home() / "Downloads"
    try:
        from agent_core.youtube_media_engine import process_media_sync
        result = process_media_sync(
            url=url,
            task_type="video",
            mode="music",
            pitch_adjust=pitch_adjust,
            resolution=resolution,
            output_dir=target_dir,
            split_long_content=split_long_content,
        )
    except Exception as exc:
        return f"❌ {source_label}下載失敗：{type(exc).__name__}: {exc}"

    if result.get("status") != "success":
        code = result.get("error_code") or "unknown_error"
        message = result.get("error_message") or "未提供錯誤訊息"
        if code == "dependency_missing":
            return (
                f"❌ {source_label}工具缺少執行環境依賴。\n"
                f"   {message}\n"
                "   請在 RED repo 執行 `make install` 或 `./setup.sh --no-plists` 後重試。"
            )
        return _friendly_video_error(source_label, code, message)

    metadata = result.get("metadata") or {}
    file_info = result.get("file_info") or {}
    paths = file_info.get("paths") or []
    title = metadata.get("title") or source_label
    if not paths:
        return f"❌ {source_label}處理完成但沒有產生檔案：{title}"

    lines = [
        f"✅ 已下載 {source_label}：{title}",
        f"解析度：{resolution}",
        f"Codec：{file_info.get('codec') or 'unknown'}",
        "檔案：",
    ]
    for index, path in enumerate(paths, start=1):
        lines.append(f"{index}. {path}")
    lines.append(result.get("compliance_note", "Processed for personal/educational use."))
    summary = "\n".join(lines)
    try:
        from agent_core.tool_result import ToolResult
        return ToolResult.success(
            summary,
            data={
                "source_label": source_label,
                "resolution": resolution,
                "metadata": metadata,
                "file_info": file_info,
            },
            artifacts=[str(path) for path in paths],
        )
    except Exception:
        return summary


def adjust_audio_pitch(
    file_path: str,
    pitch_adjust: str = "0",
    output_dir: str = "",
    output_format: str = "mp3",
) -> str:
    """調整本機音檔音高並保持速度不變。

    Args:
        file_path: 要處理的本機音檔路徑。Telegram 上傳檔通常在
            ~/Downloads/小紅-uploads/<日期>/。
        pitch_adjust: 半音數，或 Full Tone Up/Down、Half Tone Up/Down、
            全音上/全音降、半音上/半音降。
        output_dir: 輸出資料夾。空字串預設為 ~/Downloads。
        output_format: mp3、m4a、flac 或 wav。

    Returns:
        使用者可讀的處理結果與絕對路徑清單。
    """
    target_dir = Path(output_dir).expanduser() if output_dir else Path.home() / "Downloads"
    try:
        from agent_core.youtube_media_engine import process_local_audio_pitch_sync
        result = process_local_audio_pitch_sync(
            file_path=file_path,
            pitch_adjust=pitch_adjust,
            output_dir=target_dir,
            output_format=output_format,
        )
    except Exception as exc:
        return f"❌ 音檔升降 key 失敗：{type(exc).__name__}: {exc}"

    if result.get("status") != "success":
        code = result.get("error_code") or "unknown_error"
        message = result.get("error_message") or "未提供錯誤訊息"
        if code == "dependency_missing":
            return (
                "❌ 音檔處理工具缺少 FFmpeg/FFprobe。\n"
                f"   {message}\n"
                "   請安裝 FFmpeg 後重試。"
            )
        return f"❌ 音檔升降 key 失敗（{code}）：{message}"

    file_info = result.get("file_info") or {}
    details = result.get("processing_details") or {}
    paths = file_info.get("paths") or []
    if not paths:
        return "❌ 音檔處理完成但沒有產生檔案。"

    lines = [
        "✅ 已完成音檔升降 key",
        f"半音：{details.get('semitones', pitch_adjust)}",
        f"倍率：{file_info.get('pitch_ratio', 1.0)}",
        "檔案：",
    ]
    for index, path in enumerate(paths, start=1):
        lines.append(f"{index}. {path}")
    lines.append(result.get("compliance_note", "Processed for personal/educational use."))
    return "\n".join(lines)


def get_youtube_transcript(url: str):
    """抓 YouTube 影片字幕；沒字幕就下載音軌交給 Gemini 聽。支援 watch/shorts/embed 網址。

    Daemon-mode 短路保護：如果在 Telegram daemon 等互動式 daemon 環境下，
    整個流程硬卡 60s 上限。預設 5 分鐘 file-poll + 5 次 generate retry 在
    背景跑沒問題，但前景對話會讓使用者乾等到不耐煩。"""
    if not _IS_DAEMON_MODE:
        return _get_youtube_transcript_inner(url)

    # Daemon mode — wrap in a hard wall-clock cap via worker thread.
    # We can't kill the worker (Python threads can't be safely cancelled
    # through grpc), but we can stop blocking on it and let it die in the
    # background once Gemini finally responds (or doesn't).
    result_holder: dict[str, str] = {}
    exc_holder: dict[str, BaseException] = {}

    def _runner():
        try:
            result_holder["text"] = _get_youtube_transcript_inner(url)
        except BaseException as e:  # noqa: BLE001
            exc_holder["err"] = e

    worker = threading.Thread(
        target=_runner,
        name="youtube-transcript-runner",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=_DAEMON_TRANSCRIPT_HARD_CAP_S)
    if worker.is_alive():
        return (
            f"⚠️ YouTube 影片內容吸收超過 {_DAEMON_TRANSCRIPT_HARD_CAP_S}s 仍未完成，已跳過。\n"
            "可能原因：影片很長 / Gemini 慢 / 網路不穩。\n"
            "建議：直接告訴我『下載這個 URL 的音訊』或『下載這個 URL 的影片』，"
            "我會走較快的下載路徑而不嘗試摘要。"
        )
    if "err" in exc_holder:
        return f"音軌聽取失敗：{exc_holder['err']}"
    return result_holder.get("text", "錯誤：未取得結果")


def _get_youtube_transcript_inner(url: str):
    print("\n[系統日誌] 🎬 正在吸收 YouTube 影片內容...")

    video_id = None
    if "v=" in url:
        video_id = url.split("v=")[1].split("&")[0].split("#")[0]
    elif "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0].split("#")[0]
    elif "shorts/" in url:
        video_id = url.split("shorts/")[1].split("?")[0].split("#")[0]
    elif "embed/" in url:
        video_id = url.split("embed/")[1].split("?")[0].split("#")[0]

    if video_id:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            languages = ['zh-TW', 'zh', 'en', 'ja', 'ko']
            transcript_list = None
            try:
                api_instance = YouTubeTranscriptApi()
                fetched = api_instance.fetch(video_id, languages=languages)
                if hasattr(fetched, 'snippets'):
                    transcript_list = [{'text': s.text} for s in fetched.snippets]
                elif hasattr(fetched, '__iter__'):
                    transcript_list = [{'text': getattr(s, 'text', str(s))} for s in fetched]
            except (AttributeError, TypeError):
                pass
            if transcript_list is None and hasattr(YouTubeTranscriptApi, "get_transcript"):
                transcript_list = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=languages
                )
            if transcript_list is None:
                raise RuntimeError("youtube-transcript-api 無可用字幕抓取介面")
            full_text = " ".join([t['text'] for t in transcript_list])
            return f"【YouTube 影片字幕內容】：\n{full_text[:20000]}"
        except ImportError:
            print("[系統日誌] youtube-transcript-api 未安裝，跳過字幕。")
        except Exception as e:
            print(f"[系統日誌] 字幕抓取失敗（{e}），嘗試音軌...")

    print("\n[系統日誌] ⚠️ 啟動音軌備用方案...")
    temp_audio = os.path.join(tempfile.gettempdir(), f"xiaohong_yt_{os.getpid()}.m4a")
    uploaded_file = None
    try:
        import yt_dlp
        if os.path.exists(temp_audio):
            os.remove(temp_audio)
        ydl_opts = {'format': 'm4a/bestaudio/best', 'outtmpl': temp_audio, 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if not os.path.exists(temp_audio):
            return "錯誤：無法下載音訊。"
        file_size_mb = os.path.getsize(temp_audio) / (1024 * 1024)
        if file_size_mb > _MAX_AUDIO_SIZE_MB:
            return f"錯誤：音檔太大（{file_size_mb:.1f}MB），超過 {_MAX_AUDIO_SIZE_MB}MB 上限。"
        print(f"\n[系統日誌] 🎧 音軌（{file_size_mb:.1f}MB），分析中...")
        uploaded_file = upload_file(_get_gemini_client(), temp_audio)
        if _IS_DAEMON_MODE:
            # Short file-poll + few generate retries — under daemon mode the
            # outer hard cap (60s) backstops, but tightening these reduces
            # the chance we hit that cap at all.
            uploaded_file = _wait_for_file_ready(
                uploaded_file, timeout_sec=_DAEMON_FILE_POLL_TIMEOUT_S
            )
            response = _gemini_generate(
                model=GEMINI_MODEL,
                contents=[uploaded_file, "這是一段 YouTube 影片的音訊，請詳細總結重點內容。"],
                max_attempts=_DAEMON_GENERATE_MAX_ATTEMPTS,
            )
        else:
            uploaded_file = _wait_for_file_ready(uploaded_file)
            response = _gemini_generate(
                model=GEMINI_MODEL,
                contents=[uploaded_file, "這是一段 YouTube 影片的音訊，請詳細總結重點內容。"]
            )
        return f"【YouTube 音軌聽取結果】：\n{response.text}"
    except ImportError:
        return "錯誤：缺少套件。請執行：pip3 install youtube-transcript-api yt-dlp"
    except Exception as e:
        return f"音軌聽取失敗：{e}"
    finally:
        if os.path.exists(temp_audio):
            os.remove(temp_audio)
        if uploaded_file is not None:
            try:
                _get_gemini_client().files.delete(name=uploaded_file.name)
            except Exception as _e:
                logger.warning("Gemini 上傳檔清理失敗（%s）— 該檔將隨 48h 過期自動刪除", _e)
