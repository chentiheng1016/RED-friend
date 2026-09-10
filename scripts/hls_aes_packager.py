#!/usr/bin/env python3
"""Generate AES-128 encrypted multi-bitrate HLS with FFmpeg.

This script is for packaging content you own or are authorized to distribute.
It implements the HLS AES-128 key-info workflow supported by FFmpeg:

    key_info line 1: public key URI written into playlists
    key_info line 2: local filesystem path to the raw 16-byte key
    key_info line 3: 128-bit IV as hexadecimal

For CDM-backed commercial DRM (FairPlay/Widevine/PlayReady), use a packager
that emits SAMPLE-AES/CENC signaling and integrates with a license server.
The AES-128 HLS output here is useful for origin/CDN/key-service pipelines and
security testing, but it is not a DRM circumvention tool.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Rendition:
    """One ABR ladder rung."""

    name: str
    height: int
    video_bitrate: str
    maxrate: str
    bufsize: str
    audio_bitrate: str = "128k"


DEFAULT_LADDER: tuple[Rendition, ...] = (
    Rendition("1080p", 1080, "5000k", "5350k", "7500k"),
    Rendition("720p", 720, "2800k", "2996k", "4200k"),
    Rendition("480p", 480, "1400k", "1498k", "2100k", "96k"),
)


def require_binary(name: str) -> str:
    """Return an executable path or raise with an actionable message."""

    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is required on PATH.")
    return found


def generate_content_key(output_dir: Path, key_uri: str, key_id: str = "") -> tuple[Path, Path, str]:
    """Create a 128-bit AES content key and FFmpeg key_info file.

    Returns:
        (key_file_path, key_info_path, iv_hex)
    """

    keys_dir = output_dir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    resolved_key_id = key_id or secrets.token_hex(8)
    key_file = keys_dir / f"{resolved_key_id}.key"
    key_info = keys_dir / f"{resolved_key_id}.keyinfo"
    key_bytes = secrets.token_bytes(16)
    iv_hex = secrets.token_hex(16)

    key_file.write_bytes(key_bytes)
    os.chmod(key_file, 0o600)
    key_info.write_text(f"{key_uri}\n{key_file.resolve()}\n{iv_hex}\n", encoding="utf-8")
    os.chmod(key_info, 0o600)
    return key_file, key_info, iv_hex


def probe_has_audio(input_path: Path, ffprobe: str) -> bool:
    """Return True when the input contains at least one audio stream."""

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "json",
        str(input_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout or "{}")
    return bool(data.get("streams"))


def build_ffmpeg_command(
    input_path: Path,
    output_dir: Path,
    key_info: Path,
    ladder: Sequence[Rendition],
    *,
    has_audio: bool,
    segment_seconds: int,
    ffmpeg: str,
) -> list[str]:
    """Build a multi-variant encrypted HLS FFmpeg command.

    FFmpeg encrypts each MPEG-TS segment using AES-128-CBC and writes
    EXT-X-KEY metadata into the child playlists. A player downloads the key
    URI at playback time; production deployments should protect that endpoint
    with signed cookies/JWT/session authorization and short cache lifetimes.
    """

    if not ladder:
        raise ValueError("At least one rendition is required.")

    split_labels = "".join(f"[v{i}]" for i in range(len(ladder)))
    filters = [f"[0:v:0]split={len(ladder)}{split_labels}"]
    for i, rung in enumerate(ladder):
        filters.append(
            f"[v{i}]scale=w=-2:h={rung.height}:force_original_aspect_ratio=decrease,"
            f"format=yuv420p[v{i}out]"
        )

    cmd: list[str] = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(input_path),
        "-filter_complex",
        ";".join(filters),
    ]

    for i, rung in enumerate(ladder):
        cmd.extend(["-map", f"[v{i}out]"])
        if has_audio:
            cmd.extend(["-map", "0:a:0"])
        cmd.extend(
            [
                f"-c:v:{i}",
                "libx264",
                f"-b:v:{i}",
                rung.video_bitrate,
                f"-maxrate:v:{i}",
                rung.maxrate,
                f"-bufsize:v:{i}",
                rung.bufsize,
                f"-preset:v:{i}",
                "veryfast",
                f"-profile:v:{i}",
                "main",
                f"-g:v:{i}",
                str(segment_seconds * 30),
                f"-keyint_min:v:{i}",
                str(segment_seconds * 30),
                f"-sc_threshold:v:{i}",
                "0",
                f"-force_key_frames:v:{i}",
                f"expr:gte(t,n_forced*{segment_seconds})",
            ]
        )
        if has_audio:
            cmd.extend([f"-c:a:{i}", "aac", f"-b:a:{i}", rung.audio_bitrate, f"-ac:a:{i}", "2"])

    stream_maps = []
    for i, rung in enumerate(ladder):
        if has_audio:
            stream_maps.append(f"v:{i},a:{i},name:{rung.name}")
        else:
            stream_maps.append(f"v:{i},name:{rung.name}")

    cmd.extend(
        [
            "-f",
            "hls",
            "-hls_time",
            str(segment_seconds),
            "-hls_playlist_type",
            "vod",
            "-hls_flags",
            "independent_segments",
            "-hls_key_info_file",
            str(key_info),
            "-hls_segment_filename",
            str(output_dir / "%v" / "segment_%06d.ts"),
            "-master_pl_name",
            "master.m3u8",
            "-var_stream_map",
            " ".join(stream_maps),
            str(output_dir / "%v" / "playlist.m3u8"),
        ]
    )
    return cmd


def parse_ladder(value: str) -> tuple[Rendition, ...]:
    """Parse a compact ladder string.

    Format:
        name:height:vbitrate:maxrate:bufsize[:abitrate],...
    """

    if not value:
        return DEFAULT_LADDER
    rungs: list[Rendition] = []
    for raw in value.split(","):
        parts = [part.strip() for part in raw.split(":")]
        if len(parts) not in (5, 6):
            raise argparse.ArgumentTypeError(f"Invalid ladder rung: {raw!r}")
        audio = parts[5] if len(parts) == 6 else "128k"
        rungs.append(
            Rendition(
                name=parts[0],
                height=int(parts[1]),
                video_bitrate=parts[2],
                maxrate=parts[3],
                bufsize=parts[4],
                audio_bitrate=audio,
            )
        )
    return tuple(rungs)


def run_packager(args: argparse.Namespace) -> None:
    input_path = args.input.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = require_binary(args.ffmpeg)
    ffprobe = require_binary(args.ffprobe)
    key_file, key_info, iv_hex = generate_content_key(output_dir, args.key_uri, args.key_id)
    has_audio = probe_has_audio(input_path, ffprobe)
    command = build_ffmpeg_command(
        input_path=input_path,
        output_dir=output_dir,
        key_info=key_info,
        ladder=args.ladder,
        has_audio=has_audio,
        segment_seconds=args.segment_seconds,
        ffmpeg=ffmpeg,
    )

    print("AES key:", key_file)
    print("Key info:", key_info)
    print("IV:", iv_hex)
    print("Master manifest:", output_dir / "master.m3u8")
    subprocess.run(command, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input mezzanine video file.")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output HLS directory.")
    parser.add_argument(
        "--key-uri",
        required=True,
        help="Key URI to write into EXT-X-KEY, e.g. https://license.example.com/hls/key/asset123",
    )
    parser.add_argument("--key-id", default="", help="Optional stable key identifier for file naming.")
    parser.add_argument("--segment-seconds", type=int, default=6, help="HLS segment duration.")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg binary name/path.")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe binary name/path.")
    parser.add_argument("--ladder", type=parse_ladder, default=DEFAULT_LADDER, help=parse_ladder.__doc__)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    run_packager(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
