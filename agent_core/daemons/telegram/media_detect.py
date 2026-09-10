"""Telegram media-download URL/verb detection — pure helpers, no I/O.

This module owns the "what kind of media URL is this and how do we route
it to a downloader tool" logic. It does NOT:
  - touch the Telegram HTTP API (that's transport.py / daemon_telegram.tg_send)
  - actually call any downloader (that's _try_direct_media_download_fastpath
    in daemon_telegram.py, which still depends on tg_send + chat_state)

Kept here because all 19 helpers below are pure functions of text/URL input
— moving them out shrinks daemon_telegram.py without crossing the
transport boundary, and lets each be unit-tested in isolation.

Constants exposed here also intentionally model "domains we recognize"
and "domains we explicitly refuse" — see _PROTECTED_MEDIA_DOMAINS
(streaming services with DRM) and _UNSUPPORTED_MEDIA_DOMAINS (yt-dlp
opted out for legal/policy reasons).
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse


# Chat-state keys (read+written by daemon_telegram.tg_handle_message).
# Kept as strings here so daemon_telegram and the fastpath helpers can
# share one source of truth.
_TG_PENDING_MEDIA_DOWNLOAD_KEY = "pending_media_download"
_TG_PROTECTED_MEDIA_URL_KEY = "last_protected_media_url"
_TG_LAST_MEDIA_URL_KEY = "last_media_url_seen"
_TG_PENDING_MEDIA_DOWNLOAD_TTL_S = 10 * 60

# Auto-deliver scan picks up files created during agent inference that the
# user obviously wanted (download_online_video, download_youtube_audio, or
# any future media-producing tool). Extension whitelist avoids false positives
# from agents writing unrelated artifacts.
_AUTO_DELIVER_MEDIA_EXTS = (
    ".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".opus",
)
_AUTO_DELIVER_AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".opus")

_MEDIA_DOWNLOAD_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MEDIA_DOWNLOAD_VERBS = (
    "下載影片",
    "幫我下載影片",
    "我要影片",
    "存成mp4",
    "存成 mp4",
    "download video",
)
_MEDIA_AUDIO_DOWNLOAD_VERBS = (
    "下載音訊",
    "下載音訊檔",
    "下載音檔",
    "下載聲音",
    "下載聲音檔",
    "下載音樂",
    "幫我下載音訊",
    "幫我下載音檔",
    "幫我下載聲音",
    "幫我下載聲音檔",
    "幫我下載音樂",
    "存成mp3",
    "存成 mp3",
    "轉成mp3",
    "轉成 mp3",
    "download audio",
    "save audio",
)
_MEDIA_DOWNLOAD_DOMAINS = (
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "youtube.com",
    "youtu.be",
)
_PROTECTED_MEDIA_DOMAINS: dict[str, str] = {
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
_PROTECTED_MEDIA_PATH_DOMAINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "amazon.com": ("Prime Video", ("/gp/video", "/video/detail", "/primevideo")),
}
_UNSUPPORTED_MEDIA_DOMAINS: dict[str, str] = {
    "jable.tv": (
        "yt-dlp 已將此站標記為主要涉及 piracy 並停止支援，"
        "小紅不能協助繞過這個封鎖下載。"
    ),
}


# ── URL parsing / cleaning ─────────────────────────────────────────────────

def _clean_url_token(value: str) -> str:
    cleaned = (value or "").strip().rstrip("。.,，、)）]】>」\"'")
    lowered = cleaned.lower()
    cut_at = len(cleaned)
    for verb in (*_MEDIA_DOWNLOAD_VERBS, *_MEDIA_AUDIO_DOWNLOAD_VERBS):
        for marker in {verb.lower(), verb.lower().replace(" ", "")}:
            if not marker:
                continue
            idx = lowered.find(marker)
            if idx > 0:
                cut_at = min(cut_at, idx)
    return cleaned[:cut_at].rstrip("。.,，、)）]】>」\"'")


def _extract_media_urls(text: str) -> list[str]:
    """Return cleaned URL tokens from a Telegram message."""
    return [_clean_url_token(raw_url) for raw_url in _MEDIA_DOWNLOAD_URL_RE.findall(text or "")]


def _is_hls_manifest_url(url: str) -> bool:
    lowered = (url or "").lower()
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ".m3u8" in lowered
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    return ".m3u8" in path or ".m3u8" in query or "m3u8" in query


def _extract_user_agent_override(text: str) -> str:
    """Parse an optional Telegram inline UA override.

    Expected forms:
      User-Agent: Mozilla/5.0 ...
      UA=Mozilla/5.0 ...
    """
    for line in (text or "").splitlines():
        match = re.search(r"(?:user[-_ ]?agent|ua)\s*[:=：]\s*(.+)$", line, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip().strip("`").strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end > 0:
                value = value[1:end]
            else:
                value = value[1:]
        for marker in (" 幫我", " 下載影片", " 我要影片", " 存成"):
            idx = value.find(marker)
            if idx > 0:
                value = value[:idx].strip()
        if "\r" in value or "\n" in value:
            return ""
        return value[:512]
    return ""


# ── Verb / domain detection ────────────────────────────────────────────────

def _has_media_download_verb(text: str) -> bool:
    """Return True when the text plainly asks to download or save media."""
    lowered = (text or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    return any(
        verb in lowered or verb.replace(" ", "") in compact
        for verb in (*_MEDIA_DOWNLOAD_VERBS, *_MEDIA_AUDIO_DOWNLOAD_VERBS)
    )


def _has_audio_download_verb(text: str) -> bool:
    """Return True when the text asks specifically for audio."""
    lowered = (text or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    return any(
        verb in lowered or verb.replace(" ", "") in compact
        for verb in _MEDIA_AUDIO_DOWNLOAD_VERBS
    )


def _is_youtube_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(dom in lowered for dom in ("youtube.com", "youtu.be"))


def _host_matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _protected_media_provider(url: str) -> str:
    """Return the protected streaming provider for URLs that must not enter
    downloader automation."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    host = (parsed.hostname or "").lower().strip(".")
    path = (parsed.path or "").lower()
    if not host:
        return ""
    for domain, provider in _PROTECTED_MEDIA_DOMAINS.items():
        if _host_matches_domain(host, domain):
            return provider
    for domain, (provider, markers) in _PROTECTED_MEDIA_PATH_DOMAINS.items():
        if _host_matches_domain(host, domain) and any(marker in path for marker in markers):
            return provider
    return ""


def _protected_media_download_reply(url: str) -> str:
    """Explain why protected streaming services are not accepted for downloads."""
    provider = _protected_media_provider(url) or "受保護串流平台"
    return (
        f"❌ {provider} 影片不能由小紅協助下載完整內容。\n"
        "這類平台通常使用 DRM 與授權播放流程；我不能協助下載正片、擷取金鑰，"
        "或繞過授權限制。\n"
        "我可以改幫您處理公開預告片、您自有影片、您提供的合法片段/字幕，"
        "或檢查合法 EME/DRM 播放流程。"
    )


def _unsupported_media_reason(url: str) -> str:
    """Return a deterministic unsupported-source reason for blocked extractors."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        return ""
    for domain, reason in _UNSUPPORTED_MEDIA_DOMAINS.items():
        if _host_matches_domain(host, domain):
            return reason
    return ""


def _unsupported_media_download_reply(url: str) -> str:
    reason = _unsupported_media_reason(url) or "目前下載工具不支援這個來源。"
    return (
        f"❌ 這個影片來源目前不能下載：{url}\n"
        f"{reason}\n"
        "可以改貼 YouTube、Instagram、Facebook 公開影片連結，"
        "或把您有權使用的影片檔直接上傳給我處理。"
    )


# ── Chat-state URL memory ─────────────────────────────────────────────────

def _remember_media_url_in_chat(chat_state: dict[str, Any] | None, text: str) -> None:
    """Cache the most recent media URL the user mentioned in this chat.

    Lets the fast-path resume from a URL the user sent in a previous message
    when the current message is just a verb ("下載這個", "幫我下載"). Without
    this, the agent path is the only fallback and that path doesn't auto-
    deliver to Telegram — files get stranded in ~/Downloads.
    """
    if not isinstance(chat_state, dict) or not text:
        return
    for url in _extract_media_urls(text):
        if _is_hls_manifest_url(url) or any(
            domain in url.lower() for domain in _MEDIA_DOWNLOAD_DOMAINS
        ):
            chat_state[_TG_LAST_MEDIA_URL_KEY] = {"url": url, "ts": time.time()}
            break


def _recent_media_url_from_chat(chat_state: dict[str, Any] | None) -> str:
    """Return cached URL if within TTL, expire it otherwise."""
    if not isinstance(chat_state, dict):
        return ""
    entry = chat_state.get(_TG_LAST_MEDIA_URL_KEY)
    if not isinstance(entry, dict):
        return ""
    try:
        ts = float(entry.get("ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if time.time() - ts > _TG_PENDING_MEDIA_DOWNLOAD_TTL_S:
        chat_state.pop(_TG_LAST_MEDIA_URL_KEY, None)
        return ""
    return str(entry.get("url", "") or "")


# ── Tool routing ──────────────────────────────────────────────────────────

def _extract_direct_media_download_url(text: str) -> str:
    """Extract a video URL when the Telegram text is an obvious download ask.

    This keeps deterministic media downloads out of the Gemini planning path:
    if the user sends "Instagram URL + 幫我下載影片", there is no ambiguity and
    waiting for a model/tool-call turn only adds latency and a failure mode.
    """
    if not _has_media_download_verb(text):
        return ""
    for url in _extract_media_urls(text):
        if _is_hls_manifest_url(url) or any(domain in url.lower() for domain in _MEDIA_DOWNLOAD_DOMAINS):
            return url
    return ""


def _hls_download_tool_for_text(text: str) -> str:
    lowered = (text or "").lower()
    if "yt-dlp" in lowered or "ytdlp" in lowered:
        return "download_hls_with_ytdlp"
    if "ffmpeg" in lowered:
        return "download_hls_with_ffmpeg_copy"
    return "download_hls_with_n_m3u8dl"


def _media_download_tool_for_url(url: str, user_text: str = "") -> str:
    if _is_hls_manifest_url(url):
        return _hls_download_tool_for_text(user_text)
    if _is_youtube_url(url) and _has_audio_download_verb(user_text):
        return "download_youtube_audio"
    return "download_online_video"


def _media_download_args_for_url(url: str, user_text: str = "") -> dict[str, Any]:
    if _is_hls_manifest_url(url):
        args: dict[str, Any] = {"manifest_url": url}
        user_agent = _extract_user_agent_override(user_text)
        if user_agent:
            args["user_agent"] = user_agent
        return args
    return {"url": url}


def _confirmation_help_for_media_download(url: str) -> str:
    return (
        "✅ 已準備好下載這個媒體。\n"
        "完成後會直接把檔案回傳到這個 Telegram chat；上傳成功後會清掉本機下載檔。\n"
        "這需要確認一次。\n"
        "請在 90 秒內回覆 `c`，我就直接開始下載：\n"
        f"{url}"
    )
