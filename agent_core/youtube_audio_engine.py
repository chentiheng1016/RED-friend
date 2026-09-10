"""Asynchronous YouTube audio extraction and processing engine.

This module downloads the best available YouTube audio with yt-dlp, then
processes it with FFmpeg for transcription, podcast listening, or music
archival workflows. It is intentionally headless and subprocess-based so it can
run in Docker, AWS Lambda layers, CI jobs, and other Linux server environments.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, Field

try:
    from agent_core.logging_and_paths import logger
except Exception:  # pragma: no cover - fallback for standalone reuse.
    import logging

    logger = logging.getLogger(__name__)


COMPLIANCE_NOTICE = "Personal/Educational Use Only"
DEFAULT_CHUNK_SECONDS = 20 * 60
DEFAULT_THUMBNAIL_LIMIT_BYTES = 20 * 1024 * 1024


class ProcessingMode(str, Enum):
    """Supported processing profiles for output audio."""

    MEETING = "meeting"
    PODCAST = "podcast"
    MUSIC = "music"


class MusicOutputFormat(str, Enum):
    """Supported high-fidelity music containers."""

    M4A = "m4a"
    FLAC = "flac"


class YouTubeAudioError(Exception):
    """Base exception for known YouTube audio processing failures."""

    code = "youtube_audio_error"


class DependencyMissingError(YouTubeAudioError):
    """Raised when a required runtime binary or Python package is missing."""

    code = "dependency_missing"


class InvalidConfigurationError(YouTubeAudioError):
    """Raised when processor configuration is invalid."""

    code = "invalid_configuration"


class InvalidProcessingModeError(YouTubeAudioError):
    """Raised when processing_mode is not meeting, podcast, or music."""

    code = "invalid_processing_mode"


class PrivateVideoError(YouTubeAudioError):
    """Raised when yt-dlp reports a private or login-restricted video."""

    code = "private_video"


class GeoRestrictedVideoError(YouTubeAudioError):
    """Raised when the video cannot be accessed from the current region."""

    code = "geo_restricted"


class GeoBlockedVideoError(GeoRestrictedVideoError):
    """Raised when YouTube blocks playback for the current geographic region."""

    code = "geo_blocked"


class AgeRestrictedVideoError(YouTubeAudioError):
    """Raised when the video requires age verification or account login."""

    code = "age_restricted"


class VideoUnavailableProcessingError(YouTubeAudioError):
    """Raised when YouTube reports that the video is unavailable."""

    code = "video_unavailable"


class DownloadProcessingError(YouTubeAudioError):
    """Raised when yt-dlp cannot download or locate the source audio."""

    code = "download_failed"


class FFmpegProcessingError(YouTubeAudioError):
    """Raised when FFmpeg or FFprobe fails during processing."""

    code = "ffmpeg_failed"


class ThumbnailProcessingError(YouTubeAudioError):
    """Raised when thumbnail download fails and the caller requires it."""

    code = "thumbnail_failed"


class FileArtifact(BaseModel):
    """A processed output file and its duration in seconds."""

    path: str = Field(..., description="Absolute path to the processed audio file.")
    duration: float = Field(..., ge=0, description="Duration in seconds.")


class AudioMetadata(BaseModel):
    """Sanitized metadata returned to the caller and embedded when possible."""

    title: str = Field(..., description="Filesystem-safe, cleaned title.")
    uploader: str = Field(..., description="YouTube uploader or channel name.")
    duration: Optional[int] = Field(None, description="Source duration in seconds.")
    thumbnail_url: Optional[str] = Field(None, description="Best thumbnail URL.")
    video_id: Optional[str] = Field(None, description="YouTube video id.")
    webpage_url: Optional[str] = Field(None, description="Canonical YouTube URL.")
    year: Optional[str] = Field(None, description="Best-effort release/upload year.")


class AudioProcessingResult(BaseModel):
    """Structured response for the complete YouTube audio pipeline."""

    status: str = Field(..., description="success or error")
    mode_applied: str = Field(..., description="meeting, podcast, or music")
    files: List[FileArtifact] = Field(default_factory=list)
    metadata: AudioMetadata
    compliance: str = Field(default=COMPLIANCE_NOTICE)
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible dictionary for API responses."""

        if hasattr(self, "model_dump"):
            return self.model_dump(mode="json")
        return json.loads(self.json())


class PitchAdjustment(BaseModel):
    """Normalized pitch adjustment in musical semitones."""

    semitones: int = Field(..., description="Pitch shift in semitones.")
    ratio: float = Field(..., gt=0, description="Frequency ratio: 2^(semitones/12).")


class ProAudioMetadata(BaseModel):
    """Metadata shape required by the Pro process_audio API."""

    title: str
    uploader: str
    duration: int


class ProProcessingDetails(BaseModel):
    """Processing details shape required by the Pro process_audio API."""

    mode: str
    semitones: int
    ratio: float
    chunks: List[str] = Field(default_factory=list)


class ProAudioProcessingResult(BaseModel):
    """Structured Pro response for process_audio."""

    status: str
    metadata: ProAudioMetadata
    processing_details: ProProcessingDetails
    disclaimer: str = "For personal/educational use only."
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible dictionary for API responses."""

        if hasattr(self, "model_dump"):
            return self.model_dump(mode="json", exclude_none=True)
        return json.loads(self.json(exclude_none=True))


@dataclass(frozen=True)
class YouTubeAudioProcessorConfig:
    """Configuration for YouTube audio extraction and FFmpeg processing.

    Attributes:
        output_dir: Directory where final audio files are written.
        chunk_duration_seconds: Segment length for long videos. Defaults to 20
            minutes, which is friendly for downstream ASR and AI pipelines.
        chunk_long_videos: Enables splitting when the source duration is longer
            than chunk_duration_seconds.
        overwrite_existing: If false, existing output names receive a numeric
            suffix instead of being replaced.
        podcast_bitrate_kbps: MP3 bitrate for podcast/speech mode.
        music_format: Container used by music mode. M4A uses AAC at a high
            bitrate; FLAC uses lossless compression.
        ffmpeg_path: Optional explicit FFmpeg binary path.
        ffprobe_path: Optional explicit FFprobe binary path.
        ffmpeg_timeout_seconds: Optional timeout for each FFmpeg/FFprobe call.
        ytdlp_extra_options: Additional yt-dlp options, such as cookies.
        thumbnail_limit_bytes: Maximum cover-art download size.
        prefer_rubberband: Prefer FFmpeg's rubberband filter for high-quality
            pitch shifting when the filter is available.
        max_abs_pitch_semitones: Guardrail against extreme pitch shifts that
            produce poor output or very long fallback filter chains.
        raise_on_error: If true, known errors are raised instead of returned as
            status="error" result objects.
    """

    output_dir: Union[str, Path] = field(
        default_factory=lambda: Path.cwd() / "youtube_audio_output"
    )
    chunk_duration_seconds: int = DEFAULT_CHUNK_SECONDS
    chunk_long_videos: bool = True
    overwrite_existing: bool = False
    podcast_bitrate_kbps: int = 192
    music_format: MusicOutputFormat = MusicOutputFormat.M4A
    ffmpeg_path: Optional[Union[str, Path]] = None
    ffprobe_path: Optional[Union[str, Path]] = None
    ffmpeg_timeout_seconds: Optional[float] = None
    ytdlp_extra_options: Mapping[str, Any] = field(default_factory=dict)
    thumbnail_limit_bytes: int = DEFAULT_THUMBNAIL_LIMIT_BYTES
    prefer_rubberband: bool = True
    max_abs_pitch_semitones: int = 24
    raise_on_error: bool = False


@dataclass(frozen=True)
class DownloadedAudio:
    """Source audio path plus raw yt-dlp metadata."""

    path: Path
    info: Mapping[str, Any]


@dataclass(frozen=True)
class SegmentPlan:
    """One output segment to be produced by FFmpeg."""

    index: int
    start_seconds: float
    duration_seconds: Optional[float]


_PITCH_TERM_PATTERNS: Tuple[Tuple[re.Pattern, int], ...] = (
    (
        re.compile(
            r"^(?:full|whole)\s*(?:tone|step)\s*up$|^tone\s*up$|^全音(?:上|升)$",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        re.compile(
            r"^(?:full|whole)\s*(?:tone|step)\s*down$|^tone\s*down$|^全音(?:下|降)$",
            re.IGNORECASE,
        ),
        -2,
    ),
    (
        re.compile(
            r"^half\s*(?:tone|step)\s*up$|^semi\s*tone\s*up$|^semitone\s*up$|^半音(?:上|升)$",
            re.IGNORECASE,
        ),
        1,
    ),
    (
        re.compile(
            r"^half\s*(?:tone|step)\s*down$|^semi\s*tone\s*down$|^semitone\s*down$|^半音(?:下|降)$",
            re.IGNORECASE,
        ),
        -1,
    ),
)


def parse_pitch_adjustment(
    pitch_adjust: Union[int, float, str, PitchAdjustment]
) -> PitchAdjustment:
    """Normalize musical pitch adjustment terms into semitones and ratio.

    Args:
        pitch_adjust: Integer semitones, numeric string, or a friendly term
            such as "Full Tone Up", "Full Tone Down", "Half Tone Up", or
            "Half Tone Down".

    Returns:
        PitchAdjustment where ratio is calculated as 2 ** (semitones / 12).

    Raises:
        InvalidConfigurationError: If the value cannot be represented as an
            integer number of semitones.
    """

    if isinstance(pitch_adjust, PitchAdjustment):
        return pitch_adjust

    semitones: int
    if isinstance(pitch_adjust, str):
        normalized = re.sub(r"[_\-]+", " ", pitch_adjust.strip())
        normalized = re.sub(r"\s+", " ", normalized)
        for pattern, value in _PITCH_TERM_PATTERNS:
            if pattern.fullmatch(normalized):
                semitones = value
                break
        else:
            try:
                numeric = float(normalized)
            except ValueError as exc:
                raise InvalidConfigurationError(
                    "Invalid pitch_adjust %r. Use integer semitones or terms like "
                    "'Full Tone Up', 'Full Tone Down', 'Half Tone Up', or "
                    "'Half Tone Down'." % pitch_adjust
                ) from exc
            if not numeric.is_integer():
                raise InvalidConfigurationError(
                    "pitch_adjust must resolve to an integer number of semitones."
                )
            semitones = int(numeric)
    elif isinstance(pitch_adjust, bool):
        raise InvalidConfigurationError("pitch_adjust must be semitones, not bool.")
    elif isinstance(pitch_adjust, (int, float)):
        numeric = float(pitch_adjust)
        if not numeric.is_integer():
            raise InvalidConfigurationError(
                "pitch_adjust must resolve to an integer number of semitones."
            )
        semitones = int(numeric)
    else:
        raise InvalidConfigurationError(
            "pitch_adjust must be an int, numeric string, friendly term, or PitchAdjustment."
        )

    return PitchAdjustment(semitones=semitones, ratio=2 ** (semitones / 12.0))


class FilenameSanitizer:
    """Create portable filenames from YouTube titles using regular expressions."""

    _emoji_pattern = re.compile(
        "["
        "\U0001f1e6-\U0001f1ff"
        "\U0001f300-\U0001f5ff"
        "\U0001f600-\U0001f64f"
        "\U0001f680-\U0001f6ff"
        "\U0001f700-\U0001f77f"
        "\U0001f780-\U0001f7ff"
        "\U0001f800-\U0001f8ff"
        "\U0001f900-\U0001f9ff"
        "\U0001fa00-\U0001fa6f"
        "\U0001fa70-\U0001faff"
        "\u2600-\u27bf"
        "]+",
        flags=re.UNICODE,
    )
    _disallowed_pattern = re.compile(r"[^\w\s.\-]", flags=re.UNICODE)
    _space_pattern = re.compile(r"\s+")
    _underscore_pattern = re.compile(r"_+")
    _reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }

    @classmethod
    def clean(cls, value: Optional[str], fallback: str = "youtube_audio") -> str:
        """Return a safe stem suitable for local filesystems.

        The sanitizer removes emoji, path separators, shell-hostile punctuation,
        control characters, and repeated whitespace. Unicode letters and numbers
        are preserved because modern Linux filesystems handle them reliably.
        """

        candidate = unicodedata.normalize("NFKC", value or "")
        candidate = cls._emoji_pattern.sub("", candidate)
        candidate = candidate.replace(os.sep, " ")
        if os.altsep:
            candidate = candidate.replace(os.altsep, " ")
        candidate = re.sub(r"[\x00-\x1f\x7f]", "", candidate)
        candidate = cls._disallowed_pattern.sub("", candidate)
        candidate = cls._space_pattern.sub("_", candidate.strip())
        candidate = cls._underscore_pattern.sub("_", candidate)
        candidate = candidate.strip("._- ")

        if not candidate:
            candidate = fallback
        if candidate.upper() in cls._reserved_names:
            candidate = "%s_audio" % candidate
        return candidate[:160].strip("._- ") or fallback


class YouTubeAudioProcessor:
    """Download and process YouTube audio with an async API.

    The public entry point is :meth:`process_url`, which returns a pydantic
    result model. Use :func:`process_youtube_audio` when a plain dictionary is
    preferred for API serialization.
    """

    def __init__(self, config: Optional[YouTubeAudioProcessorConfig] = None) -> None:
        """Create a processor and resolve FFmpeg binaries.

        Args:
            config: Optional runtime configuration. If omitted, final files are
                written to ./youtube_audio_output.

        Raises:
            DependencyMissingError: If ffmpeg or ffprobe cannot be found.
        """

        self.config = config or YouTubeAudioProcessorConfig()
        self.output_dir = Path(self.config.output_dir).expanduser().resolve()
        self.music_format = self._coerce_music_format(self.config.music_format)
        try:
            chunk_seconds = int(self.config.chunk_duration_seconds)
        except (TypeError, ValueError) as exc:
            raise InvalidConfigurationError(
                "chunk_duration_seconds must be an integer."
            ) from exc
        if chunk_seconds <= 0:
            raise InvalidConfigurationError(
                "chunk_duration_seconds must be greater than zero."
            )
        try:
            max_pitch = int(self.config.max_abs_pitch_semitones)
        except (TypeError, ValueError) as exc:
            raise InvalidConfigurationError(
                "max_abs_pitch_semitones must be an integer."
            ) from exc
        if max_pitch < 0:
            raise InvalidConfigurationError(
                "max_abs_pitch_semitones must be zero or greater."
            )
        self.ffmpeg_path = self._resolve_binary("ffmpeg", self.config.ffmpeg_path)
        self.ffprobe_path = self._resolve_binary("ffprobe", self.config.ffprobe_path)
        self._rubberband_available_cache: Optional[bool] = None

    async def process_url(
        self,
        url: str,
        processing_mode: Union[ProcessingMode, str] = ProcessingMode.MEETING,
        pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    ) -> AudioProcessingResult:
        """Download a YouTube video's audio and process it for the selected mode.

        Args:
            url: YouTube watch, share, Shorts, embed, or music URL.
            processing_mode: One of "meeting", "podcast", or "music".
            pitch_adjust: Integer semitones or friendly pitch term. The audio
                duration is preserved at 1.0x speed.

        Returns:
            AudioProcessingResult with status="success" and output files, or a
            structured status="error" result when raise_on_error is false.
        """

        empty_metadata = AudioMetadata(title="", uploader="", thumbnail_url=None)
        try:
            mode = self._coerce_mode(processing_mode)
            pitch = self._coerce_pitch_adjustment(pitch_adjust)
            return await self._process_url_unsafe(url=url, mode=mode, pitch=pitch)
        except YouTubeAudioError as exc:
            if self.config.raise_on_error:
                raise
            return AudioProcessingResult(
                status="error",
                mode_applied=self._mode_label(processing_mode),
                files=[],
                metadata=empty_metadata,
                error_code=getattr(exc, "code", "youtube_audio_error"),
                error_message=str(exc),
            )

    async def _process_url_unsafe(
        self, url: str, mode: ProcessingMode, pitch: PitchAdjustment
    ) -> AudioProcessingResult:
        """Run the end-to-end pipeline and raise typed errors on failure."""

        self.output_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="yt_audio_engine_") as temp_name:
            temp_dir = Path(temp_name)
            downloaded = await self._download_source_audio(url, temp_dir)
            metadata = self._extract_metadata(downloaded.info)
            source_duration = self._metadata_duration(downloaded.info)
            if not source_duration:
                source_duration = await self._probe_duration(downloaded.path)
            metadata = self._metadata_with_duration(metadata, source_duration)

            segments = self._plan_segments(source_duration)
            if pitch.semitones and source_duration > 0:
                segments = self._ensure_segment_durations(segments, source_duration)
            source_sample_rate = 0
            use_rubberband = False
            if pitch.semitones:
                source_sample_rate = await self._probe_sample_rate(downloaded.path)
                use_rubberband = await self._should_use_rubberband()
            thumbnail_path: Optional[Path] = None
            if mode is ProcessingMode.MUSIC and metadata.thumbnail_url:
                thumbnail_path = await self._download_thumbnail_best_effort(
                    metadata.thumbnail_url, temp_dir
                )

            output_files: List[FileArtifact] = []
            for segment in segments:
                output_path = self._build_output_path(
                    title=metadata.title,
                    mode=mode,
                    extension=self._extension_for_mode(mode),
                    segment=segment,
                    segment_count=len(segments),
                )
                await self._run_ffmpeg(
                    source_path=downloaded.path,
                    output_path=output_path,
                    mode=mode,
                    metadata=metadata,
                    segment=segment,
                    thumbnail_path=thumbnail_path,
                    pitch=pitch,
                    source_sample_rate=source_sample_rate,
                    use_rubberband=use_rubberband,
                )
                duration = await self._probe_duration(output_path)
                if duration <= 0 and segment.duration_seconds:
                    duration = segment.duration_seconds
                output_files.append(
                    FileArtifact(path=str(output_path.resolve()), duration=round(duration, 3))
                )

            return AudioProcessingResult(
                status="success",
                mode_applied=mode.value,
                files=output_files,
                metadata=metadata,
                compliance=COMPLIANCE_NOTICE,
            )

    async def _download_source_audio(self, url: str, temp_dir: Path) -> DownloadedAudio:
        """Download best available audio with yt-dlp in a worker thread.

        yt-dlp is synchronous, so it is isolated with asyncio.to_thread to keep
        the event loop responsive while network IO and fragment downloads run.
        """

        return await asyncio.to_thread(self._download_source_audio_sync, url, temp_dir)

    def _download_source_audio_sync(self, url: str, temp_dir: Path) -> DownloadedAudio:
        """Synchronous yt-dlp implementation used by _download_source_audio."""

        try:
            import yt_dlp
            from yt_dlp.utils import (
                DownloadError,
                ExtractorError,
                GeoRestrictedError,
                UnavailableVideoError,
            )
        except ImportError as exc:
            raise DependencyMissingError(
                "Missing yt-dlp. Install it with: pip install yt-dlp"
            ) from exc

        source_template = str(temp_dir / "source.%(ext)s")
        options: Dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": source_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "continuedl": True,
            "overwrites": True,
            "windowsfilenames": True,
        }
        options.update(dict(self.config.ytdlp_extra_options))

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except GeoRestrictedError as exc:
            raise GeoBlockedVideoError(str(exc)) from exc
        except UnavailableVideoError as exc:
            raise VideoUnavailableProcessingError(str(exc)) from exc
        except (DownloadError, ExtractorError) as exc:
            raise self._map_ytdlp_error(exc) from exc

        if not isinstance(info, Mapping):
            raise DownloadProcessingError("yt-dlp returned no video metadata.")
        if "entries" in info and info.get("entries"):
            info = next(iter(info["entries"]))

        source_path = self._locate_downloaded_source(temp_dir)
        if source_path is None:
            raise DownloadProcessingError("yt-dlp completed but no audio file was found.")
        return DownloadedAudio(path=source_path, info=info)

    def _locate_downloaded_source(self, temp_dir: Path) -> Optional[Path]:
        """Find the downloaded yt-dlp source audio file in temp_dir."""

        ignored_suffixes = {".part", ".ytdl", ".json", ".description"}
        candidates = [
            path
            for path in temp_dir.glob("source.*")
            if path.is_file() and path.suffix not in ignored_suffixes
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_size)

    async def _download_thumbnail_best_effort(
        self, thumbnail_url: str, temp_dir: Path
    ) -> Optional[Path]:
        """Download thumbnail cover art, logging failures without aborting audio."""

        try:
            return await asyncio.to_thread(
                self._download_thumbnail_sync, thumbnail_url, temp_dir
            )
        except YouTubeAudioError as exc:
            logger.warning("Thumbnail download skipped: %s", exc)
            return None

    def _download_thumbnail_sync(self, thumbnail_url: str, temp_dir: Path) -> Path:
        """Download a thumbnail image for music cover-art embedding.

        Args:
            thumbnail_url: HTTP(S) thumbnail URL from yt-dlp metadata.
            temp_dir: Temporary directory where the image will be stored.

        Returns:
            Path to the downloaded image.

        Raises:
            ThumbnailProcessingError: If the URL is invalid or the image is too
                large to safely keep as cover art.
        """

        parsed = urllib.parse.urlparse(thumbnail_url)
        if parsed.scheme not in {"http", "https"}:
            raise ThumbnailProcessingError("Unsupported thumbnail URL scheme.")

        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"
        target = temp_dir / ("thumbnail%s" % suffix)
        request = urllib.request.Request(
            thumbnail_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; YouTubeAudioEngine/1.0)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read(self.config.thumbnail_limit_bytes + 1)
        except Exception as exc:
            raise ThumbnailProcessingError("Could not download thumbnail: %s" % exc) from exc

        if len(payload) > self.config.thumbnail_limit_bytes:
            raise ThumbnailProcessingError("Thumbnail exceeds configured size limit.")
        target.write_bytes(payload)
        return target

    async def _run_ffmpeg(
        self,
        source_path: Path,
        output_path: Path,
        mode: ProcessingMode,
        metadata: AudioMetadata,
        segment: SegmentPlan,
        thumbnail_path: Optional[Path],
        pitch: Optional[PitchAdjustment] = None,
        source_sample_rate: int = 0,
        use_rubberband: bool = False,
    ) -> None:
        """Run FFmpeg for one segment according to the selected processing mode."""

        pitch = pitch or parse_pitch_adjustment(0)
        command = self._build_ffmpeg_command(
            source_path=source_path,
            output_path=output_path,
            mode=mode,
            metadata=metadata,
            segment=segment,
            thumbnail_path=thumbnail_path,
            pitch=pitch,
            source_sample_rate=source_sample_rate,
            use_rubberband=use_rubberband,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await self._run_subprocess(command, "ffmpeg")

    def _build_ffmpeg_command(
        self,
        source_path: Path,
        output_path: Path,
        mode: ProcessingMode,
        metadata: AudioMetadata,
        segment: SegmentPlan,
        thumbnail_path: Optional[Path],
        pitch: PitchAdjustment,
        source_sample_rate: int,
        use_rubberband: bool,
    ) -> List[str]:
        """Build an FFmpeg command without invoking a shell."""

        command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-y"]
        if segment.start_seconds > 0:
            command.extend(["-ss", self._format_seconds(segment.start_seconds)])
        command.extend(["-i", str(source_path)])
        use_thumbnail = mode is ProcessingMode.MUSIC and thumbnail_path is not None
        if use_thumbnail:
            command.extend(["-i", str(thumbnail_path)])
        if segment.duration_seconds is not None:
            command.extend(["-t", self._format_seconds(segment.duration_seconds)])

        command.extend(["-map", "0:a:0"])
        if use_thumbnail:
            command.extend(["-map", "1:v:0"])

        filters = self._filters_for_mode(
            mode=mode,
            pitch=pitch,
            source_sample_rate=source_sample_rate,
            use_rubberband=use_rubberband,
            target_duration_seconds=segment.duration_seconds,
        )
        if filters:
            command.extend(["-af", ",".join(filters)])

        command.extend(self._codec_args_for_mode(mode, use_thumbnail=use_thumbnail))
        command.extend(self._metadata_args(metadata))
        command.append(str(output_path))
        return command

    def _filters_for_mode(
        self,
        mode: ProcessingMode,
        pitch: Optional[PitchAdjustment] = None,
        source_sample_rate: int = 0,
        use_rubberband: bool = False,
        target_duration_seconds: Optional[float] = None,
    ) -> Tuple[str, ...]:
        """Return FFmpeg audio filters for a processing mode."""

        filters: List[str] = []
        if pitch and pitch.semitones:
            filters.extend(
                self._pitch_filters(
                    pitch=pitch,
                    source_sample_rate=source_sample_rate,
                    use_rubberband=use_rubberband,
                    target_duration_seconds=target_duration_seconds,
                )
            )
        if mode is ProcessingMode.MEETING:
            filters.extend(["highpass=f=80", "lowpass=f=7600"])
            return tuple(filters)
        if mode is ProcessingMode.PODCAST:
            filters.extend(
                [
                    "silenceremove=start_periods=1:start_threshold=-50dB",
                    "areverse",
                    "silenceremove=start_periods=1:start_threshold=-50dB",
                    "areverse",
                    "highpass=f=80",
                    "loudnorm=I=-14:TP=-1.5:LRA=11",
                ]
            )
            return tuple(filters)
        return tuple(filters)

    def _pitch_filters(
        self,
        pitch: PitchAdjustment,
        source_sample_rate: int,
        use_rubberband: bool,
        target_duration_seconds: Optional[float],
    ) -> List[str]:
        """Return pitch-shifting filters that preserve 1.0x playback speed.

        FFmpeg's rubberband filter is preferred for music-quality pitch
        shifting. When unavailable, the fallback uses asetrate + high-quality
        SWR resampling + atempo compensation, then pads/trims to the segment
        duration so downstream chunks remain duration-stable.
        """

        if use_rubberband:
            filters = ["rubberband=pitch=%.12f:tempo=1.0" % pitch.ratio]
        else:
            if source_sample_rate <= 0:
                raise FFmpegProcessingError(
                    "Cannot use pitch fallback without a valid source sample rate."
                )
            filters = [
                "asetrate=%d*%.12f" % (source_sample_rate, pitch.ratio),
                "aresample=%d:filter_size=64:cutoff=0.97" % source_sample_rate,
            ]
            filters.extend(
                "atempo=%.12f" % value
                for value in self._atempo_chain(1.0 / pitch.ratio)
            )

        if target_duration_seconds and target_duration_seconds > 0:
            filters.extend(
                [
                    "apad",
                    "atrim=duration=%.6f" % target_duration_seconds,
                    "asetpts=N/SR/TB",
                ]
            )
        return filters

    def _codec_args_for_mode(
        self, mode: ProcessingMode, use_thumbnail: bool
    ) -> List[str]:
        """Return FFmpeg codec/container arguments for a processing mode."""

        if mode is ProcessingMode.MEETING:
            return [
                "-ac",
                "1",
                "-ar",
                "16000",
                "-sample_fmt",
                "s16",
                "-c:a",
                "pcm_s16le",
            ]

        if mode is ProcessingMode.PODCAST:
            bitrate = self._podcast_bitrate()
            return ["-ac", "2", "-c:a", "libmp3lame", "-b:a", "%dk" % bitrate]

        args: List[str]
        if self.music_format is MusicOutputFormat.FLAC:
            args = ["-ac", "2", "-c:a", "flac", "-compression_level", "8"]
        else:
            args = ["-ac", "2", "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"]

        if use_thumbnail:
            args.extend(
                [
                    "-c:v",
                    "mjpeg",
                    "-disposition:v:0",
                    "attached_pic",
                    "-metadata:s:v",
                    "title=Album cover",
                    "-metadata:s:v",
                    "comment=Cover (front)",
                ]
            )
        return args

    def _metadata_args(self, metadata: AudioMetadata) -> List[str]:
        """Return FFmpeg metadata arguments for container-native tags."""

        args = [
            "-metadata",
            "title=%s" % metadata.title,
            "-metadata",
            "artist=%s" % metadata.uploader,
        ]
        if metadata.year:
            args.extend(["-metadata", "date=%s" % metadata.year])
            args.extend(["-metadata", "year=%s" % metadata.year])
        return args

    async def _probe_duration(self, path: Path) -> float:
        """Return media duration in seconds using ffprobe."""

        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        stdout = await self._run_subprocess(command, "ffprobe")
        try:
            return max(0.0, float(stdout.strip()))
        except (TypeError, ValueError):
            return 0.0

    async def _probe_sample_rate(self, path: Path) -> int:
        """Return the first audio stream sample rate using ffprobe."""

        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        stdout = await self._run_subprocess(command, "ffprobe")
        try:
            sample_rate = int(float(stdout.strip()))
        except (TypeError, ValueError) as exc:
            raise FFmpegProcessingError(
                "Could not determine source audio sample rate."
            ) from exc
        if sample_rate <= 0:
            raise FFmpegProcessingError("Source audio sample rate is invalid.")
        return sample_rate

    async def _should_use_rubberband(self) -> bool:
        """Return true when FFmpeg has the rubberband filter and config prefers it."""

        if not self.config.prefer_rubberband:
            return False
        if self._rubberband_available_cache is not None:
            return self._rubberband_available_cache
        stdout = await self._run_subprocess(
            [self.ffmpeg_path, "-hide_banner", "-filters"], "ffmpeg"
        )
        self._rubberband_available_cache = bool(
            re.search(r"^\s*..?\s+rubberband\s", stdout, flags=re.MULTILINE)
        )
        return self._rubberband_available_cache

    async def _run_subprocess(self, command: Sequence[str], name: str) -> str:
        """Run a subprocess asynchronously and return decoded stdout.

        Args:
            command: Argument vector. The shell is never used.
            name: Human-readable subprocess name for error messages.

        Raises:
            FFmpegProcessingError: If the process exits non-zero or times out.
        """

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.config.ffmpeg_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise FFmpegProcessingError("%s timed out." % name) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            excerpt = stderr[-4000:] if stderr else "no stderr captured"
            raise FFmpegProcessingError("%s failed: %s" % (name, excerpt))
        return stdout

    def _extract_metadata(self, info: Mapping[str, Any]) -> AudioMetadata:
        """Build sanitized API metadata from yt-dlp metadata."""

        raw_title = str(info.get("title") or "youtube_audio")
        title = FilenameSanitizer.clean(raw_title)
        uploader = str(
            info.get("uploader")
            or info.get("channel")
            or info.get("artist")
            or "Unknown"
        )
        return AudioMetadata(
            title=title,
            uploader=FilenameSanitizer.clean(uploader, fallback="Unknown"),
            duration=None,
            thumbnail_url=self._select_best_thumbnail(info),
            video_id=self._optional_str(info.get("id")),
            webpage_url=self._optional_str(info.get("webpage_url") or info.get("original_url")),
            year=self._extract_year(info),
        )

    def _metadata_with_duration(
        self, metadata: AudioMetadata, duration_seconds: float
    ) -> AudioMetadata:
        """Return metadata with integer source duration attached."""

        if hasattr(metadata, "model_dump"):
            data = metadata.model_dump(mode="json")
        else:
            data = json.loads(metadata.json())
        data["duration"] = int(round(duration_seconds)) if duration_seconds > 0 else 0
        return AudioMetadata(**data)

    def _select_best_thumbnail(self, info: Mapping[str, Any]) -> Optional[str]:
        """Select the highest-resolution thumbnail URL from yt-dlp metadata."""

        thumbnails = info.get("thumbnails") or []
        usable = [
            item
            for item in thumbnails
            if isinstance(item, Mapping) and isinstance(item.get("url"), str)
        ]
        if usable:
            best = max(
                usable,
                key=lambda item: (
                    int(item.get("width") or 0) * int(item.get("height") or 0),
                    int(item.get("preference") or 0),
                ),
            )
            return str(best["url"])
        thumbnail = info.get("thumbnail")
        return str(thumbnail) if thumbnail else None

    def _extract_year(self, info: Mapping[str, Any]) -> Optional[str]:
        """Extract release/upload year from yt-dlp metadata when available."""

        for key in ("release_year", "release_date", "upload_date", "modified_date"):
            value = info.get(key)
            if value is None:
                continue
            text = str(value)
            match = re.search(r"\d{4}", text)
            if match:
                return match.group(0)

        timestamp = info.get("timestamp") or info.get("release_timestamp")
        try:
            if timestamp is not None:
                return str(datetime.utcfromtimestamp(float(timestamp)).year)
        except (TypeError, ValueError, OSError):
            return None
        return None

    def _metadata_duration(self, info: Mapping[str, Any]) -> float:
        """Extract source duration from yt-dlp metadata."""

        duration = info.get("duration") or info.get("duration_float")
        try:
            return max(0.0, float(duration))
        except (TypeError, ValueError):
            return 0.0

    def _plan_segments(self, duration_seconds: float) -> List[SegmentPlan]:
        """Create one segment for short videos or 20-minute chunks for long ones."""

        if (
            not self.config.chunk_long_videos
            or duration_seconds <= 0
            or duration_seconds <= int(self.config.chunk_duration_seconds)
        ):
            return [SegmentPlan(index=1, start_seconds=0.0, duration_seconds=None)]

        chunk_seconds = float(int(self.config.chunk_duration_seconds))
        segment_count = int(math.ceil(duration_seconds / chunk_seconds))
        return [
            SegmentPlan(
                index=index + 1,
                start_seconds=index * chunk_seconds,
                duration_seconds=min(chunk_seconds, duration_seconds - index * chunk_seconds),
            )
            for index in range(segment_count)
        ]

    def _ensure_segment_durations(
        self, segments: Sequence[SegmentPlan], total_duration_seconds: float
    ) -> List[SegmentPlan]:
        """Ensure every segment has a duration for exact pitch-shift trimming."""

        if not segments:
            return []
        if len(segments) == 1:
            segment = segments[0]
            if segment.duration_seconds is None:
                return [
                    SegmentPlan(
                        index=segment.index,
                        start_seconds=segment.start_seconds,
                        duration_seconds=total_duration_seconds,
                    )
                ]
        return list(segments)

    def _build_output_path(
        self,
        title: str,
        mode: ProcessingMode,
        extension: str,
        segment: SegmentPlan,
        segment_count: int,
    ) -> Path:
        """Return a deterministic, collision-safe absolute output path."""

        stem = "%s_%s" % (title, mode.value)
        if segment_count > 1:
            stem = "%s_segment_%03d" % (stem, segment.index)
        candidate = self.output_dir / ("%s.%s" % (stem, extension))
        if self.config.overwrite_existing:
            return candidate.resolve()
        return self._dedupe_path(candidate).resolve()

    def _dedupe_path(self, candidate: Path) -> Path:
        """Append a numeric suffix when candidate already exists."""

        if not candidate.exists():
            return candidate
        for suffix in range(1, 10_000):
            next_candidate = candidate.with_name(
                "%s_%d%s" % (candidate.stem, suffix, candidate.suffix)
            )
            if not next_candidate.exists():
                return next_candidate
        raise DownloadProcessingError("Could not find an available output filename.")

    def _extension_for_mode(self, mode: ProcessingMode) -> str:
        """Return the final file extension for a processing mode."""

        if mode is ProcessingMode.MEETING:
            return "wav"
        if mode is ProcessingMode.PODCAST:
            return "mp3"
        return self.music_format.value

    def _coerce_mode(self, processing_mode: Union[ProcessingMode, str]) -> ProcessingMode:
        """Validate and normalize the user-selected processing mode."""

        try:
            return ProcessingMode(processing_mode)
        except ValueError as exc:
            raise InvalidProcessingModeError(
                "Invalid processing_mode %r. Use meeting, podcast, or music."
                % processing_mode
            ) from exc

    def _mode_label(self, processing_mode: Union[ProcessingMode, str]) -> str:
        """Return a stable mode label for success and error results."""

        if isinstance(processing_mode, ProcessingMode):
            return processing_mode.value
        return str(processing_mode)

    def _coerce_music_format(
        self, music_format: Union[MusicOutputFormat, str]
    ) -> MusicOutputFormat:
        """Validate and normalize music output format."""

        try:
            return MusicOutputFormat(music_format)
        except ValueError as exc:
            raise InvalidConfigurationError(
                "Invalid music_format %r. Use m4a or flac." % music_format
            ) from exc

    def _coerce_pitch_adjustment(
        self, pitch_adjust: Union[int, float, str, PitchAdjustment]
    ) -> PitchAdjustment:
        """Validate pitch adjustment and enforce the configured safety range."""

        pitch = parse_pitch_adjustment(pitch_adjust)
        max_pitch = int(self.config.max_abs_pitch_semitones)
        if abs(pitch.semitones) > max_pitch:
            raise InvalidConfigurationError(
                "pitch_adjust %s exceeds configured limit of +/- %s semitones."
                % (pitch.semitones, max_pitch)
            )
        return pitch

    def _atempo_chain(self, tempo: float) -> List[float]:
        """Split atempo compensation into factors supported by FFmpeg."""

        if tempo <= 0:
            raise InvalidConfigurationError("Pitch tempo compensation must be positive.")
        factors: List[float] = []
        remaining = float(tempo)
        while remaining < 0.5:
            factors.append(0.5)
            remaining /= 0.5
        while remaining > 2.0:
            factors.append(2.0)
            remaining /= 2.0
        factors.append(remaining)
        return factors

    def _podcast_bitrate(self) -> int:
        """Return podcast bitrate clamped to the requested 128-192 kbps range."""

        try:
            bitrate = int(self.config.podcast_bitrate_kbps)
        except (TypeError, ValueError) as exc:
            raise InvalidConfigurationError(
                "podcast_bitrate_kbps must be an integer."
            ) from exc
        return max(128, min(bitrate, 192))

    def _map_ytdlp_error(self, exc: Exception) -> YouTubeAudioError:
        """Convert yt-dlp's broad errors into actionable domain exceptions."""

        message = str(exc)
        lower = message.lower()
        if "private video" in lower or "video is private" in lower:
            return PrivateVideoError(message)
        if (
            "age-restricted" in lower
            or "age restricted" in lower
            or "confirm your age" in lower
            or "sign in to confirm your age" in lower
        ):
            return AgeRestrictedVideoError(message)
        if (
            "geo" in lower
            or "geo-blocked" in lower
            or "not available in your country" in lower
            or "not available in your region" in lower
        ):
            return GeoBlockedVideoError(message)
        if (
            "unavailable" in lower
            or "removed" in lower
            or "does not exist" in lower
            or "copyright" in lower
        ):
            return VideoUnavailableProcessingError(message)
        return DownloadProcessingError(message)

    def _resolve_binary(
        self, binary_name: str, configured_path: Optional[Union[str, Path]]
    ) -> str:
        """Resolve an executable path from config or PATH."""

        if configured_path:
            resolved = Path(configured_path).expanduser()
            if not resolved.is_absolute() and len(resolved.parts) == 1:
                found_configured = shutil.which(str(resolved))
                if found_configured:
                    return found_configured
            if resolved.exists() and os.access(str(resolved), os.X_OK):
                return str(resolved)
            raise DependencyMissingError(
                "%s not found or not executable: %s" % (binary_name, configured_path)
            )

        found = shutil.which(binary_name)
        if found:
            return found
        sibling = Path(sys.executable).with_name(binary_name)
        if sibling.exists() and os.access(str(sibling), os.X_OK):
            return str(sibling)
        raise DependencyMissingError(
            "%s is required but was not found on PATH." % binary_name
        )

    def _optional_str(self, value: Any) -> Optional[str]:
        """Return a non-empty string or None."""

        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _format_seconds(self, seconds: float) -> str:
        """Format seconds for FFmpeg arguments."""

        return "%.3f" % max(0.0, seconds)


async def process_youtube_audio(
    url: str,
    processing_mode: Union[ProcessingMode, str] = ProcessingMode.MEETING,
    output_dir: Optional[Union[str, Path]] = None,
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    **config_overrides: Any,
) -> Dict[str, Any]:
    """Process YouTube audio and return a JSON-compatible dictionary.

    Example:
        result = await process_youtube_audio(
            "https://www.youtube.com/watch?v=...",
            processing_mode="podcast",
            output_dir="/tmp/processed_audio",
        )

    Args:
        url: YouTube URL to download.
        processing_mode: "meeting", "podcast", or "music".
        output_dir: Optional directory for final files.
        pitch_adjust: Integer semitones or friendly pitch term. Defaults to 0.
        **config_overrides: Any YouTubeAudioProcessorConfig field override.

    Returns:
        Dictionary matching the public API contract, including status, files,
        metadata, and compliance fields.
    """

    config_kwargs = dict(config_overrides)
    if output_dir is not None:
        config_kwargs["output_dir"] = output_dir
    try:
        config = YouTubeAudioProcessorConfig(**config_kwargs)
    except TypeError as exc:
        result = AudioProcessingResult(
            status="error",
            mode_applied=(
                processing_mode.value
                if isinstance(processing_mode, ProcessingMode)
                else str(processing_mode)
            ),
            files=[],
            metadata=AudioMetadata(title="", uploader="", thumbnail_url=None),
            error_code=InvalidConfigurationError.code,
            error_message=str(exc),
        )
        return result.to_dict()
    try:
        processor = YouTubeAudioProcessor(config)
        result = await processor.process_url(
            url,
            processing_mode=processing_mode,
            pitch_adjust=pitch_adjust,
        )
    except YouTubeAudioError as exc:
        if config.raise_on_error:
            raise
        result = AudioProcessingResult(
            status="error",
            mode_applied=(
                processing_mode.value
                if isinstance(processing_mode, ProcessingMode)
                else str(processing_mode)
            ),
            files=[],
            metadata=AudioMetadata(title="", uploader="", thumbnail_url=None),
            error_code=getattr(exc, "code", "youtube_audio_error"),
            error_message=str(exc),
        )
    return result.to_dict()


async def process_audio(
    url: str,
    mode: Union[ProcessingMode, str],
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    output_dir: Optional[Union[str, Path]] = None,
    **config_overrides: Any,
) -> Dict[str, Any]:
    """Pro API: process YouTube audio with adaptive modes and pitch shifting.

    Args:
        url: YouTube URL to download.
        mode: "meeting", "podcast", or "music".
        pitch_adjust: Semitone integer or friendly term such as "Full Tone Up".
        output_dir: Optional final output directory.
        **config_overrides: Any YouTubeAudioProcessorConfig field override.

    Returns:
        JSON-compatible dictionary:
        {
          "status": "success",
          "metadata": {"title": "str", "uploader": "str", "duration": int},
          "processing_details": {
            "mode": "str", "semitones": int, "ratio": float,
            "chunks": ["path_1.ext"]
          },
          "disclaimer": "For personal/educational use only."
        }
    """

    try:
        pitch = parse_pitch_adjustment(pitch_adjust)
        mode_value = ProcessingMode(mode).value
        config_kwargs = dict(config_overrides)
        if output_dir is not None:
            config_kwargs["output_dir"] = output_dir
        config = YouTubeAudioProcessorConfig(**config_kwargs)
        processor = YouTubeAudioProcessor(config)
        result = await processor.process_url(
            url=url,
            processing_mode=mode_value,
            pitch_adjust=pitch,
        )
    except ValueError as exc:
        error = InvalidProcessingModeError(
            "Invalid mode %r. Use meeting, podcast, or music." % mode
        )
        return _pro_error_result(
            mode=str(mode),
            pitch=parse_pitch_adjustment(0),
            exc=error,
            message=str(error.__cause__ or error or exc),
        ).to_dict()
    except (TypeError, YouTubeAudioError) as exc:
        try:
            pitch = parse_pitch_adjustment(pitch_adjust)
        except YouTubeAudioError:
            pitch = parse_pitch_adjustment(0)
        error_exc: Exception
        if isinstance(exc, TypeError):
            error_exc = InvalidConfigurationError(str(exc))
        else:
            error_exc = exc
        return _pro_error_result(
            mode=str(mode),
            pitch=pitch,
            exc=error_exc,
            message=str(error_exc),
        ).to_dict()

    if result.status != "success":
        return ProAudioProcessingResult(
            status="error",
            metadata=ProAudioMetadata(
                title=result.metadata.title,
                uploader=result.metadata.uploader,
                duration=int(result.metadata.duration or 0),
            ),
            processing_details=ProProcessingDetails(
                mode=mode_value,
                semitones=pitch.semitones,
                ratio=round(pitch.ratio, 12),
                chunks=[],
            ),
            error_code=result.error_code,
            error_message=result.error_message,
        ).to_dict()

    chunks = [file.path for file in result.files]
    return ProAudioProcessingResult(
        status="success",
        metadata=ProAudioMetadata(
            title=result.metadata.title,
            uploader=result.metadata.uploader,
            duration=int(result.metadata.duration or 0),
        ),
        processing_details=ProProcessingDetails(
            mode=mode_value,
            semitones=pitch.semitones,
            ratio=round(pitch.ratio, 12),
            chunks=chunks,
        ),
    ).to_dict()


def _pro_error_result(
    mode: str,
    pitch: PitchAdjustment,
    exc: Exception,
    message: str,
) -> ProAudioProcessingResult:
    """Build a structured Pro error result."""

    return ProAudioProcessingResult(
        status="error",
        metadata=ProAudioMetadata(title="", uploader="", duration=0),
        processing_details=ProProcessingDetails(
            mode=mode,
            semitones=pitch.semitones,
            ratio=round(pitch.ratio, 12),
            chunks=[],
        ),
        error_code=getattr(exc, "code", "youtube_audio_error"),
        error_message=message,
    )


def process_youtube_audio_sync(
    url: str,
    processing_mode: Union[ProcessingMode, str] = ProcessingMode.MEETING,
    output_dir: Optional[Union[str, Path]] = None,
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    **config_overrides: Any,
) -> Dict[str, Any]:
    """Synchronous wrapper for scripts, cron jobs, and simple integrations."""

    return asyncio.run(
        process_youtube_audio(
            url=url,
            processing_mode=processing_mode,
            output_dir=output_dir,
            pitch_adjust=pitch_adjust,
            **config_overrides,
        )
    )


def process_audio_sync(
    url: str,
    mode: Union[ProcessingMode, str],
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    output_dir: Optional[Union[str, Path]] = None,
    **config_overrides: Any,
) -> Dict[str, Any]:
    """Synchronous wrapper for the Pro process_audio API."""

    return asyncio.run(
        process_audio(
            url=url,
            mode=mode,
            pitch_adjust=pitch_adjust,
            output_dir=output_dir,
            **config_overrides,
        )
    )


__all__ = [
    "AgeRestrictedVideoError",
    "AudioMetadata",
    "AudioProcessingResult",
    "DependencyMissingError",
    "DownloadProcessingError",
    "FFmpegProcessingError",
    "FileArtifact",
    "FilenameSanitizer",
    "GeoBlockedVideoError",
    "GeoRestrictedVideoError",
    "InvalidConfigurationError",
    "InvalidProcessingModeError",
    "MusicOutputFormat",
    "PitchAdjustment",
    "PrivateVideoError",
    "ProAudioMetadata",
    "ProAudioProcessingResult",
    "ProProcessingDetails",
    "ProcessingMode",
    "VideoUnavailableProcessingError",
    "YouTubeAudioError",
    "YouTubeAudioProcessor",
    "YouTubeAudioProcessorConfig",
    "parse_pitch_adjustment",
    "process_audio",
    "process_audio_sync",
    "process_youtube_audio",
    "process_youtube_audio_sync",
]
