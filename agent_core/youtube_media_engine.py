"""Asynchronous YouTube media processing engine.

This module extracts YouTube audio or video with the ``yt-dlp`` CLI and
processes the resulting media with FFmpeg. It is designed for headless Linux
systems such as Docker containers, cloud workers, and CI jobs: no GUI, browser,
or desktop APIs are required.

The public entry point is :func:`process_media`.
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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, Field


COMPLIANCE_NOTE = "Processed for personal/educational use."
DEFAULT_CHUNK_SECONDS = 20 * 60
VIDEO_FORMAT_TEMPLATE = (
    "bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
    "best[ext=mp4]/best"
)


class TaskType(str, Enum):
    """Supported extraction task types."""

    AUDIO = "audio"
    VIDEO = "video"


class AudioMode(str, Enum):
    """Scenario modes for audio post-processing."""

    MEETING = "meeting"
    PODCAST = "podcast"
    MUSIC = "music"


class MusicOutputFormat(str, Enum):
    """Supported music-oriented audio containers."""

    M4A = "m4a"
    FLAC = "flac"


class MediaProcessingError(Exception):
    """Base exception for expected media processing failures."""

    code = "media_processing_error"


class DependencyMissingError(MediaProcessingError):
    """Raised when yt-dlp, ffmpeg, or ffprobe cannot be found."""

    code = "dependency_missing"


class InvalidConfigurationError(MediaProcessingError):
    """Raised when caller configuration cannot be applied safely."""

    code = "invalid_configuration"


class InvalidTaskTypeError(MediaProcessingError):
    """Raised when task_type is not audio or video."""

    code = "invalid_task_type"


class InvalidModeError(MediaProcessingError):
    """Raised when audio mode is not meeting, podcast, or music."""

    code = "invalid_mode"


class PrivateVideoError(MediaProcessingError):
    """Raised when YouTube reports a private or login-only video."""

    code = "private_video"


class AgeRestrictedVideoError(MediaProcessingError):
    """Raised when YouTube requires age verification."""

    code = "age_restricted"


class GeoBlockedVideoError(MediaProcessingError):
    """Raised when playback is blocked from the current region."""

    code = "geo_blocked"


class VideoUnavailableError(MediaProcessingError):
    """Raised when YouTube reports that the video is unavailable."""

    code = "video_unavailable"


class DownloadError(MediaProcessingError):
    """Raised when yt-dlp fails to download a requested media stream."""

    code = "download_failed"


class FFmpegError(MediaProcessingError):
    """Raised when FFmpeg or FFprobe exits with an error."""

    code = "ffmpeg_failed"


class MediaMetadata(BaseModel):
    """Cleaned YouTube metadata returned by the engine."""

    title: str
    uploader: str
    duration: int = 0


class FileInfo(BaseModel):
    """Final output file information."""

    paths: List[str] = Field(default_factory=list)
    pitch_ratio: float = 1.0
    codec: str = ""


class MediaProcessingResult(BaseModel):
    """Structured JSON-compatible response for process_media."""

    status: str
    task_type: str
    metadata: MediaMetadata
    file_info: FileInfo
    compliance_note: str = COMPLIANCE_NOTE
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dictionary."""

        if hasattr(self, "model_dump"):
            return self.model_dump(mode="json", exclude_none=True)
        return json.loads(self.json(exclude_none=True))


class PitchAdjustment(BaseModel):
    """Pitch shift normalized to semitones and frequency ratio."""

    semitones: int
    ratio: float = Field(..., gt=0)


@dataclass(frozen=True)
class MediaEngineConfig:
    """Runtime configuration for YouTubeMediaProcessor.

    Args:
        output_dir: Directory for final processed outputs.
        split_long_content: Split outputs longer than chunk_duration_seconds.
        chunk_duration_seconds: Segment size for long-form content.
        music_format: M4A or FLAC for audio music mode.
        podcast_bitrate_kbps: MP3 bitrate for podcast mode, clamped to 128-192.
        prefer_rubberband: Use FFmpeg's rubberband filter when available.
        max_abs_pitch_semitones: Safety limit for pitch shifts.
        yt_dlp_path: Optional explicit yt-dlp executable path.
        ffmpeg_path: Optional explicit ffmpeg executable path.
        ffprobe_path: Optional explicit ffprobe executable path.
        subprocess_timeout_seconds: Optional per-process timeout.
        ytdlp_extra_args: Additional yt-dlp CLI flags, e.g. cookies.
    """

    output_dir: Union[str, Path] = field(
        default_factory=lambda: Path.cwd() / "youtube_media_output"
    )
    split_long_content: bool = True
    chunk_duration_seconds: int = DEFAULT_CHUNK_SECONDS
    music_format: MusicOutputFormat = MusicOutputFormat.M4A
    podcast_bitrate_kbps: int = 192
    prefer_rubberband: bool = True
    max_abs_pitch_semitones: int = 24
    yt_dlp_path: Optional[Union[str, Path]] = None
    ffmpeg_path: Optional[Union[str, Path]] = None
    ffprobe_path: Optional[Union[str, Path]] = None
    subprocess_timeout_seconds: Optional[float] = None
    ytdlp_extra_args: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class SegmentPlan:
    """One segment to be generated from an input media file."""

    index: int
    start_seconds: float
    duration_seconds: Optional[float]


@dataclass(frozen=True)
class MediaSources:
    """Downloaded source files before final FFmpeg processing."""

    audio_path: Optional[Path] = None
    video_path: Optional[Path] = None
    thumbnail_path: Optional[Path] = None


class FilenameSanitizer:
    """Create portable filenames from YouTube titles."""

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
    _illegal_pattern = re.compile(r"[^\w.\-]+", flags=re.UNICODE)
    _underscore_pattern = re.compile(r"_+")

    @classmethod
    def clean(cls, text: Optional[str], fallback: str = "youtube_media") -> str:
        """Strip emojis, whitespace, and illegal path characters."""

        value = unicodedata.normalize("NFKC", text or "")
        value = cls._emoji_pattern.sub("", value)
        value = value.replace(os.sep, "_")
        if os.altsep:
            value = value.replace(os.altsep, "_")
        value = re.sub(r"[\x00-\x1f\x7f]", "", value)
        value = cls._illegal_pattern.sub("_", value.strip())
        value = cls._underscore_pattern.sub("_", value).strip("._-")
        return (value or fallback)[:160].strip("._-") or fallback


class PitchShifter:
    """Standalone pitch-shift utility for FFmpeg filter generation.

    Musical pitch is measured in semitones. Equal temperament maps semitones to
    a frequency multiplier with ``ratio = 2 ** (semitones / 12)``. Raising by a
    full tone is +2 semitones; lowering by a half tone is -1 semitone.

    The rubberband filter directly changes pitch at constant tempo:
    ``rubberband=pitch=<ratio>:tempo=1.0``.

    The fallback chain simulates pitch shift with sample-rate relabeling:
    ``asetrate=sample_rate*ratio`` changes pitch and speed together, then
    ``aresample=sample_rate`` returns to the original sample rate. That would
    alter duration, so a compensatory ``atempo=1/ratio`` is mandatory to restore
    speed to 1.0x and prevent audio-video desync. For quality, SWR resampling is
    configured with a larger filter size and high cutoff.
    """

    _term_patterns: Tuple[Tuple[re.Pattern, int], ...] = (
        (re.compile(r"^(?:full|whole)\s*(?:tone|step)\s*up$", re.I), 2),
        (re.compile(r"^(?:full|whole)\s*(?:tone|step)\s*down$", re.I), -2),
        (re.compile(r"^half\s*(?:tone|step)\s*up$|^semitone\s*up$", re.I), 1),
        (re.compile(r"^half\s*(?:tone|step)\s*down$|^semitone\s*down$", re.I), -1),
        (re.compile(r"^全音(?:上|升)$"), 2),
        (re.compile(r"^全音(?:下|降)$"), -2),
        (re.compile(r"^半音(?:上|升)$"), 1),
        (re.compile(r"^半音(?:下|降)$"), -1),
    )

    @classmethod
    def parse(cls, pitch_adjust: Union[int, float, str, PitchAdjustment]) -> PitchAdjustment:
        """Normalize a pitch term or integer semitone value."""

        if isinstance(pitch_adjust, PitchAdjustment):
            return pitch_adjust
        if isinstance(pitch_adjust, bool):
            raise InvalidConfigurationError("pitch_adjust must not be bool.")

        semitones: int
        if isinstance(pitch_adjust, str):
            normalized = re.sub(r"[_\-]+", " ", pitch_adjust.strip())
            normalized = re.sub(r"\s+", " ", normalized)
            for pattern, value in cls._term_patterns:
                if pattern.fullmatch(normalized):
                    semitones = value
                    break
            else:
                try:
                    numeric = float(normalized)
                except ValueError as exc:
                    raise InvalidConfigurationError(
                        "pitch_adjust must be semitones or a term like Full Tone Up."
                    ) from exc
                if not numeric.is_integer():
                    raise InvalidConfigurationError("pitch_adjust must be whole semitones.")
                semitones = int(numeric)
        elif isinstance(pitch_adjust, (int, float)):
            numeric = float(pitch_adjust)
            if not numeric.is_integer():
                raise InvalidConfigurationError("pitch_adjust must be whole semitones.")
            semitones = int(numeric)
        else:
            raise InvalidConfigurationError("Unsupported pitch_adjust type.")
        return PitchAdjustment(semitones=semitones, ratio=2 ** (semitones / 12.0))

    @classmethod
    def atempo_chain(cls, tempo: float) -> List[float]:
        """Split a tempo ratio into FFmpeg atempo factors within [0.5, 2.0]."""

        if tempo <= 0:
            raise InvalidConfigurationError("atempo ratio must be positive.")
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

    @classmethod
    def filters(
        cls,
        pitch: PitchAdjustment,
        *,
        source_sample_rate: int,
        use_rubberband: bool,
        target_duration_seconds: Optional[float] = None,
    ) -> List[str]:
        """Build FFmpeg filters for constant-speed pitch shifting.

        Args:
            pitch: Normalized semitone/ratio object.
            source_sample_rate: Audio stream sample rate used by asetrate fallback.
            use_rubberband: True to use the high-quality rubberband filter.
            target_duration_seconds: Optional pad/trim length used for chunk
                boundaries and video sync.
        """

        if not pitch.semitones:
            return []
        if use_rubberband:
            parts = ["rubberband=pitch=%.12f:tempo=1.0" % pitch.ratio]
        else:
            if source_sample_rate <= 0:
                raise FFmpegError("Pitch fallback needs a valid source sample rate.")
            parts = [
                "asetrate=%d*%.12f" % (source_sample_rate, pitch.ratio),
                "aresample=%d:filter_size=64:cutoff=0.97" % source_sample_rate,
            ]
            parts.extend(
                "atempo=%.12f" % value for value in cls.atempo_chain(1.0 / pitch.ratio)
            )
        if target_duration_seconds and target_duration_seconds > 0:
            parts.extend(
                [
                    "apad",
                    "atrim=duration=%.6f" % target_duration_seconds,
                    "asetpts=N/SR/TB",
                ]
            )
        return parts


class AsyncSubprocessRunner:
    """Small wrapper around asyncio.create_subprocess_exec."""

    def __init__(self, timeout_seconds: Optional[float] = None) -> None:
        self.timeout_seconds = timeout_seconds

    async def run(self, command: Sequence[str], name: str) -> Tuple[str, str]:
        """Run a process without a shell and return stdout/stderr text."""

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            if name == "yt-dlp":
                raise DownloadError("%s timed out." % name) from exc
            raise FFmpegError("%s timed out." % name) from exc

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise _map_process_error(name=name, stderr=stderr)
        return stdout, stderr


class YouTubeMediaProcessor:
    """Async processor for YouTube audio and video workflows."""

    def __init__(self, config: Optional[MediaEngineConfig] = None) -> None:
        self.config = config or MediaEngineConfig()
        self.output_dir = Path(self.config.output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.yt_dlp_path = self._resolve_binary("yt-dlp", self.config.yt_dlp_path)
        self.ffmpeg_path = self._resolve_binary("ffmpeg", self.config.ffmpeg_path)
        self.ffprobe_path = self._resolve_binary("ffprobe", self.config.ffprobe_path)
        self.runner = AsyncSubprocessRunner(self.config.subprocess_timeout_seconds)
        self._rubberband_available: Optional[bool] = None
        try:
            self.music_format = MusicOutputFormat(self.config.music_format)
        except ValueError as exc:
            raise InvalidConfigurationError("music_format must be m4a or flac.") from exc

        if int(self.config.chunk_duration_seconds) <= 0:
            raise InvalidConfigurationError("chunk_duration_seconds must be positive.")
        if int(self.config.max_abs_pitch_semitones) < 0:
            raise InvalidConfigurationError("max_abs_pitch_semitones must be >= 0.")

    async def process_media(
        self,
        url: str,
        task_type: Union[TaskType, str],
        mode: Union[AudioMode, str] = AudioMode.MUSIC,
        pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
        resolution: str = "1080p",
    ) -> MediaProcessingResult:
        """Process YouTube media and return a structured result.

        Video mode downloads stable video/audio streams and muxes them with
        FFmpeg. Audio mode applies one of the scenario profiles. Temporary
        fragments, thumbnails, and ``.part`` files live under a TemporaryDirectory
        that is removed after final output generation.
        """

        try:
            task = TaskType(task_type)
            audio_mode = AudioMode(mode)
            pitch = self._coerce_pitch(pitch_adjust)
            with tempfile.TemporaryDirectory(prefix="yt_media_engine_") as temp_name:
                temp_dir = Path(temp_name)
                raw_info = await self._fetch_metadata(url)
                metadata = self._metadata_from_info(raw_info)
                if task is TaskType.AUDIO:
                    paths, codec = await self._process_audio(url, audio_mode, pitch, metadata, temp_dir)
                else:
                    paths, codec = await self._process_video(url, pitch, metadata, temp_dir, resolution)
                return MediaProcessingResult(
                    status="success",
                    task_type=task.value,
                    metadata=metadata,
                    file_info=FileInfo(
                        paths=[str(path.resolve()) for path in paths],
                        pitch_ratio=round(pitch.ratio, 5),
                        codec=codec,
                    ),
                )
        except (ValueError, MediaProcessingError) as exc:
            mapped = self._normalize_exception(exc, task_type=task_type, mode=mode)
            pitch = _safe_pitch(pitch_adjust)
            return MediaProcessingResult(
                status="error",
                task_type=_enum_label(task_type),
                metadata=MediaMetadata(title="", uploader="", duration=0),
                file_info=FileInfo(paths=[], pitch_ratio=round(pitch.ratio, 5), codec=""),
                error_code=getattr(mapped, "code", "media_processing_error"),
                error_message=str(mapped),
            )

    async def _process_audio(
        self,
        url: str,
        mode: AudioMode,
        pitch: PitchAdjustment,
        metadata: MediaMetadata,
        temp_dir: Path,
    ) -> Tuple[List[Path], str]:
        """Download and process audio according to the selected scenario mode."""

        source = await self._download_one(
            url=url,
            fmt="bestaudio[ext=m4a]/bestaudio/best",
            temp_dir=temp_dir,
            stem="audio_source",
        )
        thumbnail = await self._download_thumbnail(url, temp_dir) if mode is AudioMode.MUSIC else None
        sample_rate = 0
        use_rubberband = False
        if pitch.semitones:
            sample_rate = await self._probe_sample_rate(source)
            use_rubberband = await self._should_use_rubberband()
        segments = self._segments(metadata.duration)
        outputs: List[Path] = []
        for segment in segments:
            output = self._output_path(metadata.title, mode.value, self._audio_extension(mode), segment, len(segments))
            filters = self._audio_filters(
                mode=mode,
                pitch=pitch,
                source_sample_rate=sample_rate,
                use_rubberband=use_rubberband,
                segment=segment,
            )
            command = self._audio_ffmpeg_command(source, output, mode, metadata, segment, filters, thumbnail)
            await self.runner.run(command, "ffmpeg")
            outputs.append(output)
        return outputs, self._audio_codec_label(mode)

    async def _process_video(
        self,
        url: str,
        pitch: PitchAdjustment,
        metadata: MediaMetadata,
        temp_dir: Path,
        resolution: str,
    ) -> Tuple[List[Path], str]:
        """Download video/audio streams and mux them with FFmpeg."""

        height = self._parse_resolution(resolution)
        video_format = VIDEO_FORMAT_TEMPLATE.format(height=height)
        audio_format = "bestaudio[ext=m4a]/bestaudio/best"
        video_source = await self._download_one(url, video_format, temp_dir, "video_source")
        audio_source: Optional[Path] = None
        if not await self._has_audio_stream(video_source):
            audio_source = await self._download_one(url, audio_format, temp_dir, "video_audio")
        thumbnail = await self._download_thumbnail(url, temp_dir)

        merged = temp_dir / "merged_video.mp4"
        filters: List[str] = []
        if pitch.semitones:
            sample_rate = await self._probe_sample_rate(audio_source or video_source)
            use_rubberband = await self._should_use_rubberband()
            filters = PitchShifter.filters(
                pitch,
                source_sample_rate=sample_rate,
                use_rubberband=use_rubberband,
                target_duration_seconds=float(metadata.duration) if metadata.duration else None,
            )
        await self.runner.run(
            self._video_mux_command(
                video_source=video_source,
                audio_source=audio_source,
                thumbnail=thumbnail,
                output=merged,
                metadata=metadata,
                filters=filters,
            ),
            "ffmpeg",
        )
        outputs = await self._finalize_segments(
            source=merged,
            title=metadata.title,
            label="video",
            extension="mp4",
            duration=metadata.duration,
        )
        return outputs, "h264/aac"

    async def _fetch_metadata(self, url: str) -> Mapping[str, Any]:
        """Fetch YouTube metadata without downloading media."""

        command = [
            self.yt_dlp_path,
            "--dump-single-json",
            "--no-playlist",
            "--no-warnings",
            "--skip-download",
            *self._ytdlp_stability_args(),
            *self.config.ytdlp_extra_args,
            url,
        ]
        stdout, _stderr = await self.runner.run(command, "yt-dlp")
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DownloadError("yt-dlp returned invalid metadata JSON.") from exc
        if "entries" in data and data.get("entries"):
            data = data["entries"][0]
        return data

    async def _download_one(self, url: str, fmt: str, temp_dir: Path, stem: str) -> Path:
        """Download one stream selection with yt-dlp."""

        output_template = str(temp_dir / ("%s.%%(ext)s" % stem))
        command = [
            self.yt_dlp_path,
            "-f",
            fmt,
            "--no-playlist",
            "--no-warnings",
            "--paths",
            str(temp_dir),
            "-o",
            output_template,
            *self._ytdlp_stability_args(),
            *self.config.ytdlp_extra_args,
            url,
        ]
        await self.runner.run(command, "yt-dlp")
        candidates = [
            path
            for path in temp_dir.glob("%s.*" % stem)
            if path.is_file() and path.suffix not in {".part", ".ytdl", ".json"}
        ]
        if not candidates:
            raise DownloadError("yt-dlp did not produce %s." % stem)
        return max(candidates, key=lambda item: item.stat().st_size)

    async def _download_thumbnail(self, url: str, temp_dir: Path) -> Optional[Path]:
        """Download a thumbnail with yt-dlp for cover art embedding."""

        stem = "thumbnail"
        command = [
            self.yt_dlp_path,
            "--skip-download",
            "--write-thumbnail",
            "--no-playlist",
            "--no-warnings",
            "--paths",
            str(temp_dir),
            "-o",
            str(temp_dir / ("%s.%%(ext)s" % stem)),
            *self._ytdlp_stability_args(),
            *self.config.ytdlp_extra_args,
            url,
        ]
        try:
            await self.runner.run(command, "yt-dlp")
        except MediaProcessingError:
            return None
        candidates = [
            path
            for path in temp_dir.glob("%s.*" % stem)
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        return max(candidates, key=lambda item: item.stat().st_size) if candidates else None

    def _audio_ffmpeg_command(
        self,
        source: Path,
        output: Path,
        mode: AudioMode,
        metadata: MediaMetadata,
        segment: SegmentPlan,
        filters: Sequence[str],
        thumbnail: Optional[Path],
    ) -> List[str]:
        """Build an audio FFmpeg command for one final output."""

        command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-y"]
        if segment.start_seconds:
            command.extend(["-ss", self._seconds(segment.start_seconds)])
        command.extend(["-i", str(source)])
        use_cover = mode is AudioMode.MUSIC and thumbnail is not None
        if use_cover:
            command.extend(["-i", str(thumbnail)])
        if segment.duration_seconds:
            command.extend(["-t", self._seconds(segment.duration_seconds)])
        command.extend(["-map", "0:a:0"])
        if use_cover:
            command.extend(["-map", "1:v:0"])
        if filters:
            command.extend(["-af", ",".join(filters)])
        command.extend(self._audio_codec_args(mode, use_cover))
        command.extend(self._metadata_args(metadata))
        command.append(str(output))
        return command

    def _video_mux_command(
        self,
        video_source: Path,
        audio_source: Optional[Path],
        thumbnail: Optional[Path],
        output: Path,
        metadata: MediaMetadata,
        filters: Sequence[str],
    ) -> List[str]:
        """Build a video muxing command.

        With no pitch filters, audio/video streams are copied with ``-c copy``
        to avoid CPU-heavy re-encoding. With pitch filters, the video stream is
        still copied, while the selected audio stream is encoded to AAC after
        the constant-speed pitch chain.
        """

        command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-y", "-i", str(video_source)]
        audio_input_index = 0
        if audio_source is not None:
            audio_input_index = 1
            command.extend(["-i", str(audio_source)])
        thumb_index: Optional[int] = None
        if thumbnail is not None:
            thumb_index = 2 if audio_source is not None else 1
            command.extend(["-i", str(thumbnail)])

        command.extend(["-map", "0:v:0", "-map", "%d:a:0" % audio_input_index])
        if thumb_index is not None:
            command.extend(["-map", "%d:v:0" % thumb_index])

        command.extend(["-c:v:0", "copy"])
        if filters:
            command.extend(["-af", ",".join(filters), "-c:a", "aac", "-b:a", "192k"])
        else:
            command.extend(["-c:a", "copy"])
        if thumb_index is not None:
            command.extend(["-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"])
        command.extend(["-movflags", "+faststart"])
        command.extend(self._metadata_args(metadata))
        command.append(str(output))
        return command

    async def _finalize_segments(
        self,
        source: Path,
        title: str,
        label: str,
        extension: str,
        duration: int,
    ) -> List[Path]:
        """Copy or split a processed source into final output files."""

        segments = self._segments(duration)
        outputs: List[Path] = []
        if len(segments) == 1:
            output = self._output_path(title, label, extension, segments[0], 1)
            shutil.copy2(source, output)
            return [output]
        for segment in segments:
            output = self._output_path(title, label, extension, segment, len(segments))
            command = [
                self.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-ss",
                self._seconds(segment.start_seconds),
                "-i",
                str(source),
                "-t",
                self._seconds(segment.duration_seconds or self.config.chunk_duration_seconds),
                "-c",
                "copy",
                str(output),
            ]
            await self.runner.run(command, "ffmpeg")
            outputs.append(output)
        return outputs

    def _audio_filters(
        self,
        mode: AudioMode,
        pitch: PitchAdjustment,
        source_sample_rate: int,
        use_rubberband: bool,
        segment: SegmentPlan,
    ) -> List[str]:
        """Return scenario and pitch filters for audio workflows."""

        filters = PitchShifter.filters(
            pitch,
            source_sample_rate=source_sample_rate,
            use_rubberband=use_rubberband,
            target_duration_seconds=segment.duration_seconds,
        )
        if mode is AudioMode.MEETING:
            filters.extend(["highpass=f=80", "lowpass=f=7600"])
        elif mode is AudioMode.PODCAST:
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
        return filters

    def _audio_codec_args(self, mode: AudioMode, use_cover: bool) -> List[str]:
        """Return codec/container arguments for audio output."""

        if mode is AudioMode.MEETING:
            return ["-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-c:a", "pcm_s16le"]
        if mode is AudioMode.PODCAST:
            bitrate = max(128, min(int(self.config.podcast_bitrate_kbps), 192))
            return ["-ac", "2", "-c:a", "libmp3lame", "-b:a", "%dk" % bitrate]
        if self.music_format is MusicOutputFormat.FLAC:
            args = ["-ac", "2", "-c:a", "flac", "-compression_level", "8"]
        else:
            args = ["-ac", "2", "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"]
        if use_cover:
            args.extend(["-c:v", "mjpeg", "-disposition:v:0", "attached_pic"])
        return args

    async def _probe_sample_rate(self, path: Path) -> int:
        """Return the first audio stream sample rate from ffprobe."""

        stdout, _stderr = await self.runner.run(
            [
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
            ],
            "ffprobe",
        )
        try:
            return int(float(stdout.strip()))
        except (TypeError, ValueError) as exc:
            raise FFmpegError("Could not read audio sample rate.") from exc

    async def _has_audio_stream(self, path: Path) -> bool:
        """Return true when a media file has at least one audio stream."""

        stdout, _stderr = await self.runner.run(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            "ffprobe",
        )
        return bool(stdout.strip())

    async def _should_use_rubberband(self) -> bool:
        """Probe FFmpeg filter availability once per processor."""

        if not self.config.prefer_rubberband:
            return False
        if self._rubberband_available is not None:
            return self._rubberband_available
        stdout, _stderr = await self.runner.run([self.ffmpeg_path, "-hide_banner", "-filters"], "ffmpeg")
        self._rubberband_available = bool(
            re.search(r"^\s*..?\s+rubberband\s", stdout, flags=re.MULTILINE)
        )
        return self._rubberband_available

    def _segments(self, duration: int) -> List[SegmentPlan]:
        """Return one segment or 20-minute chunk plans."""

        if (
            not self.config.split_long_content
            or duration <= 0
            or duration <= int(self.config.chunk_duration_seconds)
        ):
            return [SegmentPlan(index=1, start_seconds=0.0, duration_seconds=None)]
        chunk = float(int(self.config.chunk_duration_seconds))
        count = int(math.ceil(duration / chunk))
        return [
            SegmentPlan(
                index=index + 1,
                start_seconds=index * chunk,
                duration_seconds=min(chunk, duration - index * chunk),
            )
            for index in range(count)
        ]

    def _output_path(
        self,
        title: str,
        label: str,
        extension: str,
        segment: SegmentPlan,
        segment_count: int,
    ) -> Path:
        """Build a collision-safe final output path."""

        stem = "%s_%s" % (title, label)
        if segment_count > 1:
            stem = "%s_segment_%03d" % (stem, segment.index)
        path = self.output_dir / ("%s.%s" % (stem, extension))
        if not path.exists():
            return path.resolve()
        for number in range(1, 10_000):
            candidate = path.with_name("%s_%d%s" % (path.stem, number, path.suffix))
            if not candidate.exists():
                return candidate.resolve()
        raise InvalidConfigurationError("Could not create a unique output filename.")

    def _metadata_from_info(self, info: Mapping[str, Any]) -> MediaMetadata:
        """Convert yt-dlp JSON metadata to public metadata."""

        duration = info.get("duration") or info.get("duration_float") or 0
        try:
            duration_int = int(round(float(duration)))
        except (TypeError, ValueError):
            duration_int = 0
        return MediaMetadata(
            title=FilenameSanitizer.clean(str(info.get("title") or "youtube_media")),
            uploader=FilenameSanitizer.clean(
                str(info.get("uploader") or info.get("channel") or "Unknown"),
                fallback="Unknown",
            ),
            duration=duration_int,
        )

    def _metadata_args(self, metadata: MediaMetadata) -> List[str]:
        """Build FFmpeg metadata arguments."""

        return ["-metadata", "title=%s" % metadata.title, "-metadata", "artist=%s" % metadata.uploader]

    def _audio_extension(self, mode: AudioMode) -> str:
        """Return the extension for an audio mode."""

        if mode is AudioMode.MEETING:
            return "wav"
        if mode is AudioMode.PODCAST:
            return "mp3"
        return self.music_format.value

    def _audio_codec_label(self, mode: AudioMode) -> str:
        """Return a concise codec label for output JSON."""

        if mode is AudioMode.MEETING:
            return "pcm_s16le"
        if mode is AudioMode.PODCAST:
            return "mp3"
        return "flac" if self.music_format is MusicOutputFormat.FLAC else "aac"

    def _coerce_pitch(self, pitch_adjust: Union[int, float, str, PitchAdjustment]) -> PitchAdjustment:
        """Validate pitch adjustment against configured bounds."""

        pitch = PitchShifter.parse(pitch_adjust)
        if abs(pitch.semitones) > int(self.config.max_abs_pitch_semitones):
            raise InvalidConfigurationError(
                "pitch_adjust exceeds +/- %s semitones." % self.config.max_abs_pitch_semitones
            )
        return pitch

    def _parse_resolution(self, resolution: str) -> int:
        """Parse strings such as 1080p into a video height."""

        match = re.search(r"(\d{3,4})", resolution or "")
        if not match:
            return 1080
        return max(144, min(int(match.group(1)), 4320))

    def _resolve_binary(self, name: str, configured_path: Optional[Union[str, Path]]) -> str:
        """Resolve an executable from config or PATH."""

        if configured_path:
            path = Path(configured_path).expanduser()
            if not path.is_absolute() and len(path.parts) == 1:
                found_configured = shutil.which(str(path))
                if found_configured:
                    return found_configured
            if path.exists() and os.access(str(path), os.X_OK):
                return str(path)
            raise DependencyMissingError("%s not found or not executable: %s" % (name, path))
        found = shutil.which(name)
        if found:
            return found
        sibling = Path(sys.executable).with_name(name)
        if sibling.exists() and os.access(str(sibling), os.X_OK):
            return str(sibling)
        raise DependencyMissingError("%s is required but was not found on PATH." % name)

    def _ytdlp_stability_args(self) -> List[str]:
        """Return stable yt-dlp retry/network flags."""

        return [
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--socket-timeout",
            "30",
            "--no-mtime",
        ]

    def _normalize_exception(
        self,
        exc: Exception,
        *,
        task_type: Union[TaskType, str],
        mode: Union[AudioMode, str],
    ) -> Exception:
        """Map ValueError and broad process errors to domain exceptions."""

        if isinstance(exc, ValueError):
            task_text = _enum_label(task_type)
            mode_text = _enum_label(mode)
            if task_text not in {TaskType.AUDIO.value, TaskType.VIDEO.value}:
                return InvalidTaskTypeError("task_type must be audio or video.")
            if mode_text not in {item.value for item in AudioMode}:
                return InvalidModeError("mode must be meeting, podcast, or music.")
        return exc

    def _seconds(self, seconds: float) -> str:
        """Format seconds for FFmpeg CLI arguments."""

        return "%.3f" % max(0.0, float(seconds))


def _map_process_error(name: str, stderr: str) -> MediaProcessingError:
    """Map yt-dlp/FFmpeg stderr to actionable exceptions."""

    message = stderr.strip() or "%s failed without stderr." % name
    lower = message.lower()
    if name == "yt-dlp":
        if "private video" in lower or "video is private" in lower:
            return PrivateVideoError(message)
        if "age-restricted" in lower or "confirm your age" in lower or "sign in to confirm your age" in lower:
            return AgeRestrictedVideoError(message)
        if "geo" in lower or "not available in your country" in lower or "not available in your region" in lower:
            return GeoBlockedVideoError(message)
        if "unavailable" in lower or "removed" in lower or "does not exist" in lower:
            return VideoUnavailableError(message)
        return DownloadError(message)
    return FFmpegError(message)


def _safe_pitch(value: Union[int, float, str, PitchAdjustment]) -> PitchAdjustment:
    """Best-effort pitch parser for error responses."""

    try:
        return PitchShifter.parse(value)
    except MediaProcessingError:
        return PitchAdjustment(semitones=0, ratio=1.0)


def _enum_label(value: Any) -> str:
    """Return enum.value when available, else str(value)."""

    return str(value.value) if isinstance(value, Enum) else str(value)


def _resolve_binary(name: str, configured_path: Optional[Union[str, Path]] = None) -> str:
    """Resolve an executable from an explicit path, PATH, or current venv.

    The venv sibling lookup matters for LaunchAgents: macOS launchd often
    starts daemons with a narrow PATH, while dependencies installed into
    ``.venv/bin`` still sit next to ``sys.executable``.
    """

    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute() and len(path.parts) == 1:
            found_configured = shutil.which(str(path))
            if found_configured:
                return found_configured
        if path.exists() and os.access(str(path), os.X_OK):
            return str(path)
        raise DependencyMissingError("%s not found or not executable: %s" % (name, path))
    found = shutil.which(name)
    if found:
        return found
    sibling = Path(sys.executable).with_name(name)
    if sibling.exists() and os.access(str(sibling), os.X_OK):
        return str(sibling)
    raise DependencyMissingError("%s is required but was not found on PATH." % name)


async def _probe_audio_duration(
    runner: AsyncSubprocessRunner,
    ffprobe_path: str,
    source: Path,
) -> float:
    """Return media duration in seconds using ffprobe."""

    stdout, _stderr = await runner.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        "ffprobe",
    )
    try:
        return max(0.0, float(stdout.strip()))
    except (TypeError, ValueError) as exc:
        raise FFmpegError("Could not read audio duration.") from exc


async def _probe_audio_sample_rate(
    runner: AsyncSubprocessRunner,
    ffprobe_path: str,
    source: Path,
) -> int:
    """Return the first audio stream sample rate from ffprobe."""

    stdout, _stderr = await runner.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        "ffprobe",
    )
    try:
        return int(float(stdout.strip()))
    except (TypeError, ValueError) as exc:
        raise FFmpegError("Could not read audio sample rate.") from exc


async def _ffmpeg_has_rubberband(runner: AsyncSubprocessRunner, ffmpeg_path: str) -> bool:
    """Return true when this FFmpeg build exposes the rubberband filter."""

    stdout, _stderr = await runner.run([ffmpeg_path, "-hide_banner", "-filters"], "ffmpeg")
    return bool(re.search(r"^\s*..?\s+rubberband\s", stdout, flags=re.MULTILINE))


def _unique_output_path(output_dir: Path, stem: str, extension: str) -> Path:
    """Build a collision-safe output path."""

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ("%s.%s" % (stem, extension))
    if not path.exists():
        return path.resolve()
    for number in range(1, 10_000):
        candidate = path.with_name("%s_%d%s" % (path.stem, number, path.suffix))
        if not candidate.exists():
            return candidate.resolve()
    raise InvalidConfigurationError("Could not create a unique output filename.")


def _local_audio_codec_args(output_format: str) -> Tuple[List[str], str]:
    """Return FFmpeg codec args and a public codec label for local pitch output."""

    extension = output_format.lower().lstrip(".")
    if extension == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"], "mp3"
    if extension == "m4a":
        return ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"], "aac"
    if extension == "flac":
        return ["-c:a", "flac", "-compression_level", "8"], "flac"
    if extension == "wav":
        return ["-c:a", "pcm_s16le"], "pcm_s16le"
    raise InvalidConfigurationError("output_format must be mp3, m4a, flac, or wav.")


async def process_local_audio_pitch(
    file_path: Union[str, Path],
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    output_dir: Optional[Union[str, Path]] = None,
    output_format: str = "mp3",
    prefer_rubberband: bool = True,
    max_abs_pitch_semitones: int = 24,
    ffmpeg_path: Optional[Union[str, Path]] = None,
    ffprobe_path: Optional[Union[str, Path]] = None,
    subprocess_timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Pitch-shift an existing local audio file without invoking a shell.

    This is the same standalone pitch math used by YouTube processing:
    ``ratio = 2 ** (semitones / 12)``. When FFmpeg has ``rubberband``, the
    filter is ``rubberband=pitch=<ratio>:tempo=1.0``. Otherwise the fallback
    uses ``asetrate`` plus ``aresample`` and the mandatory compensatory
    ``atempo=1/ratio`` so the rendered song keeps its original duration.
    """

    try:
        source = Path(file_path).expanduser().resolve()
        if not source.exists() or not source.is_file():
            raise InvalidConfigurationError("Audio file not found: %s" % source)
        pitch = PitchShifter.parse(pitch_adjust)
        if abs(pitch.semitones) > int(max_abs_pitch_semitones):
            raise InvalidConfigurationError(
                "pitch_adjust exceeds +/- %s semitones." % max_abs_pitch_semitones
            )
        extension = output_format.lower().lstrip(".")
        codec_args, codec = _local_audio_codec_args(extension)
        target_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir is not None
            else Path.home().joinpath("Downloads").resolve()
        )
        direction = "original" if pitch.semitones == 0 else (
            "up_%d_semitones" % pitch.semitones
            if pitch.semitones > 0
            else "down_%d_semitones" % abs(pitch.semitones)
        )
        stem = "%s_%s" % (FilenameSanitizer.clean(source.stem, "audio"), direction)
        output = _unique_output_path(target_dir, stem, extension)

        resolved_ffmpeg = _resolve_binary("ffmpeg", ffmpeg_path)
        resolved_ffprobe = _resolve_binary("ffprobe", ffprobe_path)
        runner = AsyncSubprocessRunner(subprocess_timeout_seconds)
        duration = await _probe_audio_duration(runner, resolved_ffprobe, source)
        sample_rate = 0
        if pitch.semitones:
            sample_rate = await _probe_audio_sample_rate(runner, resolved_ffprobe, source)
        use_rubberband = bool(prefer_rubberband and pitch.semitones)
        if use_rubberband:
            use_rubberband = await _ffmpeg_has_rubberband(runner, resolved_ffmpeg)
        filters = PitchShifter.filters(
            pitch,
            source_sample_rate=sample_rate,
            use_rubberband=use_rubberband,
            target_duration_seconds=duration if duration else None,
        )
        command = [
            resolved_ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
        ]
        if filters:
            command.extend(["-af", ",".join(filters)])
        command.extend(codec_args)
        command.append(str(output))
        await runner.run(command, "ffmpeg")
        return {
            "status": "success",
            "source_path": str(source),
            "file_info": {
                "paths": [str(output)],
                "pitch_ratio": round(pitch.ratio, 5),
                "codec": codec,
            },
            "processing_details": {
                "semitones": pitch.semitones,
                "ratio": pitch.ratio,
                "rubberband": use_rubberband,
                "duration": duration,
            },
            "compliance_note": COMPLIANCE_NOTE,
        }
    except (ValueError, MediaProcessingError) as exc:
        pitch = _safe_pitch(pitch_adjust)
        return {
            "status": "error",
            "source_path": str(file_path),
            "file_info": {
                "paths": [],
                "pitch_ratio": round(pitch.ratio, 5),
                "codec": "",
            },
            "processing_details": {
                "semitones": pitch.semitones,
                "ratio": pitch.ratio,
                "rubberband": False,
                "duration": 0,
            },
            "error_code": getattr(exc, "code", "media_processing_error"),
            "error_message": str(exc),
            "compliance_note": COMPLIANCE_NOTE,
        }


def process_local_audio_pitch_sync(
    file_path: Union[str, Path],
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    output_dir: Optional[Union[str, Path]] = None,
    output_format: str = "mp3",
    prefer_rubberband: bool = True,
    max_abs_pitch_semitones: int = 24,
    ffmpeg_path: Optional[Union[str, Path]] = None,
    ffprobe_path: Optional[Union[str, Path]] = None,
    subprocess_timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Synchronous wrapper for process_local_audio_pitch."""

    return asyncio.run(
        process_local_audio_pitch(
            file_path=file_path,
            pitch_adjust=pitch_adjust,
            output_dir=output_dir,
            output_format=output_format,
            prefer_rubberband=prefer_rubberband,
            max_abs_pitch_semitones=max_abs_pitch_semitones,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            subprocess_timeout_seconds=subprocess_timeout_seconds,
        )
    )


async def process_media(
    url: str,
    task_type: Union[TaskType, str],
    mode: Union[AudioMode, str] = AudioMode.MUSIC,
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    resolution: str = "1080p",
    output_dir: Optional[Union[str, Path]] = None,
    split_long_content: bool = True,
    **config_overrides: Any,
) -> Dict[str, Any]:
    """Process YouTube video or audio and return the required JSON schema.

    Args:
        url: YouTube URL.
        task_type: "audio" or "video".
        mode: Audio scenario mode: meeting, podcast, or music. Video mode still
            validates this for a stable public signature.
        pitch_adjust: Semitones or friendly terms such as "Full Tone Up".
        resolution: Video height target, e.g. "1080p".
        output_dir: Optional final output directory.
        split_long_content: Split media longer than 20 minutes by default.
        **config_overrides: Additional MediaEngineConfig fields.
    """

    kwargs = dict(config_overrides)
    if output_dir is not None:
        kwargs["output_dir"] = output_dir
    kwargs["split_long_content"] = split_long_content
    try:
        processor = YouTubeMediaProcessor(MediaEngineConfig(**kwargs))
        result = await processor.process_media(
            url=url,
            task_type=task_type,
            mode=mode,
            pitch_adjust=pitch_adjust,
            resolution=resolution,
        )
    except TypeError as exc:
        pitch = _safe_pitch(pitch_adjust)
        result = MediaProcessingResult(
            status="error",
            task_type=str(task_type),
            metadata=MediaMetadata(title="", uploader="", duration=0),
            file_info=FileInfo(paths=[], pitch_ratio=round(pitch.ratio, 5), codec=""),
            error_code=InvalidConfigurationError.code,
            error_message=str(exc),
        )
    except (ValueError, MediaProcessingError) as exc:
        pitch = _safe_pitch(pitch_adjust)
        mapped: Exception
        if isinstance(exc, MediaProcessingError):
            mapped = exc
        else:
            mapped = InvalidConfigurationError(str(exc))
        result = MediaProcessingResult(
            status="error",
            task_type=_enum_label(task_type),
            metadata=MediaMetadata(title="", uploader="", duration=0),
            file_info=FileInfo(paths=[], pitch_ratio=round(pitch.ratio, 5), codec=""),
            error_code=getattr(mapped, "code", "media_processing_error"),
            error_message=str(mapped),
        )
    return result.to_dict()


def process_media_sync(
    url: str,
    task_type: Union[TaskType, str],
    mode: Union[AudioMode, str] = AudioMode.MUSIC,
    pitch_adjust: Union[int, float, str, PitchAdjustment] = 0,
    resolution: str = "1080p",
    output_dir: Optional[Union[str, Path]] = None,
    split_long_content: bool = True,
    **config_overrides: Any,
) -> Dict[str, Any]:
    """Synchronous wrapper for process_media."""

    return asyncio.run(
        process_media(
            url=url,
            task_type=task_type,
            mode=mode,
            pitch_adjust=pitch_adjust,
            resolution=resolution,
            output_dir=output_dir,
            split_long_content=split_long_content,
            **config_overrides,
        )
    )


__all__ = [
    "AgeRestrictedVideoError",
    "AudioMode",
    "AsyncSubprocessRunner",
    "DependencyMissingError",
    "DownloadError",
    "FFmpegError",
    "FileInfo",
    "FilenameSanitizer",
    "GeoBlockedVideoError",
    "InvalidConfigurationError",
    "InvalidModeError",
    "InvalidTaskTypeError",
    "MediaEngineConfig",
    "MediaMetadata",
    "MediaProcessingError",
    "MediaProcessingResult",
    "MusicOutputFormat",
    "PitchAdjustment",
    "PitchShifter",
    "PrivateVideoError",
    "TaskType",
    "VIDEO_FORMAT_TEMPLATE",
    "VideoUnavailableError",
    "YouTubeMediaProcessor",
    "process_media",
    "process_media_sync",
    "process_local_audio_pitch",
    "process_local_audio_pitch_sync",
]
