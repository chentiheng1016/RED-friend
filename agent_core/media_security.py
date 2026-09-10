"""Media security engineering helpers.

These tools support legal media platform development and security audits:
ISO-BMFF metadata inspection, CENC key material generation, FFmpeg command
construction, and license-challenge simulation. They intentionally do not
extract decryption keys from third-party DRM systems or bypass CDMs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
# defusedxml：DASH manifest 可來自遠端 URL / caller 輸入（untrusted）。stdlib
# ElementTree 會展開 DTD 內部實體 → billion-laughs 記憶體炸彈。defusedxml 只換
# 解析函式（fromstring/parse），forbid_entities 預設擋掉實體展開。本檔只用 fromstring。
import defusedxml.ElementTree as ET
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from agent_core.env_utils import env_int as _env_int
from agent_core.logging_and_paths import REPO_ROOT

_BOX_HEADER_SIZE = 8
_MAX_INSPECT_BYTES = 256 * 1024 * 1024
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HLS_MAX_VARIANT_CHECKS = _env_int("RED_HLS_MAX_VARIANT_CHECKS", 128, min_value=1, max_value=4096)
_N_M3U8DL_OUTPUT_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".mkv", ".ts", ".m2ts",
    ".m4a", ".aac", ".mp3", ".vtt", ".srt",
}
_HLS_DOWNLOAD_ALLOWED_URI_SCHEMES = {"http", "https"}
_CONTAINER_BOXES = {
    "moov", "trak", "mdia", "minf", "dinf", "stbl", "edts", "udta",
    "mvex", "moof", "traf", "mfra", "skip", "meta", "ipro", "sinf",
    "schi", "tref", "iref", "iprp", "grpl", "strk",
}
_DRM_SYSTEM_IDS = {
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "Widevine",
    "9a04f079-9840-4286-ab92-e65be0885f95": "PlayReady",
    "94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "FairPlay",
    "e2719d58-a985-b3c9-781a-b030af78d30e": "ClearKey",
}
_EME_KEY_SYSTEMS = {
    "widevine": "com.widevine.alpha",
    "playready": "com.microsoft.playready",
    "fairplay": "com.apple.fps.1_0",
    "clearkey": "org.w3.clearkey",
}
_PROHIBITED_DRM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("cdm_bypass", r"\b(?:bypass|crack|dump|extract|exfiltrate)\b.*\b(?:cdm|widevine|playready|fairplay|drm|key)\b"),
    ("cdm_reverse_engineering", r"(?:逆向|反編譯|hook|frida|ida|ghidra|trace).*(?:CDM|Widevine|PlayReady|FairPlay|金鑰|key)"),
    ("key_extraction", r"(?:抽|取|抓|dump|提取|導出).*(?:content\s*key|私鑰|金鑰|key|L3)"),
    ("tee_or_hardware_bypass", r"(?:繞過|攻破|exploit).*(?:TEE|TrustZone|SVP|HDCP|Secure\s*Video\s*Path|L1|OTP)"),
    ("fault_or_side_channel_attack", r"(?:DFA|差分故障|故障注入|side[- ]?channel|側信道|側寫|功耗|電磁).*(?:攻擊|逆推|key|金鑰)"),
)
_SAMPLE_ENTRY_HEADER_BYTES = {
    "encv": 78,
    "avc1": 78,
    "hvc1": 78,
    "hev1": 78,
    "av01": 78,
    "vp09": 78,
    "mp4v": 78,
    "enca": 28,
    "mp4a": 28,
    "ac-3": 28,
    "ec-3": 28,
}


@dataclass
class BoxRecord:
    """One ISO-BMFF box discovered in a file."""

    box_type: str
    offset: int
    size: int
    header_size: int
    depth: int
    details: dict[str, object] = field(default_factory=dict)

    @property
    def end(self) -> int:
        return self.offset + self.size


def _read_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "big")


def _read_u64(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 8], "big")


def _read_box_header(data: bytes, offset: int, limit: int) -> tuple[str, int, int] | None:
    """Return (type, size, header_size) for a box header.

    ISO-BMFF boxes start with uint32 size + 4-byte type. A size of 1 means
    "largesize" follows as uint64. A size of 0 extends to the parent boundary.
    """
    if offset + _BOX_HEADER_SIZE > limit:
        return None
    size32 = _read_u32(data, offset)
    try:
        box_type = data[offset + 4:offset + 8].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r"[\x20-\x7e]{4}", box_type):
        return None
    header_size = _BOX_HEADER_SIZE
    if size32 == 1:
        if offset + 16 > limit:
            return None
        size = _read_u64(data, offset + 8)
        header_size = 16
    elif size32 == 0:
        size = limit - offset
    else:
        size = size32
    if size < header_size or offset + size > limit:
        return None
    return box_type, size, header_size


def _parse_pssh_payload(payload: bytes) -> dict[str, object]:
    """Parse a Protection System Specific Header payload."""
    if len(payload) < 24:
        return {"error": "pssh payload too short"}
    version = payload[0]
    flags = int.from_bytes(payload[1:4], "big")
    system_id = str(uuid.UUID(bytes=payload[4:20]))
    cursor = 20
    kids: list[str] = []
    if version > 0:
        if cursor + 4 > len(payload):
            return {"version": version, "flags": flags, "system_id": system_id, "error": "missing KID_count"}
        kid_count = _read_u32(payload, cursor)
        cursor += 4
        for _ in range(kid_count):
            if cursor + 16 > len(payload):
                return {
                    "version": version,
                    "flags": flags,
                    "system_id": system_id,
                    "kids": kids,
                    "error": "truncated KID list",
                }
            kids.append(str(uuid.UUID(bytes=payload[cursor:cursor + 16])))
            cursor += 16
    if cursor + 4 > len(payload):
        return {"version": version, "flags": flags, "system_id": system_id, "kids": kids, "error": "missing data_size"}
    data_size = _read_u32(payload, cursor)
    cursor += 4
    data = payload[cursor:cursor + data_size]
    return {
        "version": version,
        "flags": flags,
        "system_id": system_id,
        "kids": kids,
        "data_size": data_size,
        "data_sha256": hashlib.sha256(data).hexdigest() if data else "",
        "data_base64_prefix": base64.b64encode(data[:48]).decode("ascii") if data else "",
    }


def _parse_tenc_payload(payload: bytes) -> dict[str, object]:
    """Parse a TrackEncryptionBox payload.

    The useful operational field is default_KID: it links encrypted samples to
    a license-server key record. Version 1 also carries cbcs pattern encryption
    information in the high/low nibbles of the first byte after FullBox.
    """
    if len(payload) < 23:
        return {"error": "tenc payload too short"}
    version = payload[0]
    flags = int.from_bytes(payload[1:4], "big")
    cursor = 4
    pattern = payload[cursor]
    cursor += 1
    crypt_byte_block = pattern >> 4 if version > 0 else 0
    skip_byte_block = pattern & 0x0F if version > 0 else 0
    default_is_protected = payload[cursor]
    cursor += 1
    default_iv_size = payload[cursor]
    cursor += 1
    default_kid = str(uuid.UUID(bytes=payload[cursor:cursor + 16]))
    cursor += 16
    details: dict[str, object] = {
        "version": version,
        "flags": flags,
        "default_is_protected": default_is_protected,
        "default_per_sample_iv_size": default_iv_size,
        "default_kid": default_kid,
    }
    if version > 0:
        details["default_crypt_byte_block"] = crypt_byte_block
        details["default_skip_byte_block"] = skip_byte_block
    if default_is_protected == 1 and default_iv_size == 0 and cursor < len(payload):
        constant_iv_size = payload[cursor]
        cursor += 1
        details["default_constant_iv_size"] = constant_iv_size
        details["default_constant_iv_hex"] = payload[cursor:cursor + constant_iv_size].hex()
    return details


def _box_details(box_type: str, payload: bytes) -> dict[str, object]:
    if box_type == "pssh":
        return _parse_pssh_payload(payload)
    if box_type == "tenc":
        return _parse_tenc_payload(payload)
    if box_type == "schm" and len(payload) >= 12:
        return {
            "version": payload[0],
            "scheme_type": payload[4:8].decode("ascii", errors="replace"),
            "scheme_version": _read_u32(payload, 8),
        }
    return {}


def _walk_boxes(
    data: bytes,
    start: int,
    end: int,
    *,
    depth: int = 0,
    max_depth: int = 8,
) -> list[BoxRecord]:
    records: list[BoxRecord] = []
    offset = start
    while offset + _BOX_HEADER_SIZE <= end:
        header = _read_box_header(data, offset, end)
        if header is None:
            break
        box_type, size, header_size = header
        payload_start = offset + header_size
        payload_end = offset + size
        payload = data[payload_start:payload_end]
        record = BoxRecord(
            box_type=box_type,
            offset=offset,
            size=size,
            header_size=header_size,
            depth=depth,
            details=_box_details(box_type, payload),
        )
        records.append(record)

        if depth < max_depth:
            if box_type in _CONTAINER_BOXES:
                child_start = payload_start + (4 if box_type == "meta" and len(payload) >= 4 else 0)
                records.extend(_walk_boxes(data, child_start, payload_end, depth=depth + 1, max_depth=max_depth))
            elif box_type == "stsd" and len(payload) >= 8:
                records.extend(_walk_sample_entries(data, payload_start + 8, payload_end, depth + 1, max_depth))
        offset += size
    return records


def _walk_sample_entries(
    data: bytes,
    start: int,
    end: int,
    depth: int,
    max_depth: int,
) -> list[BoxRecord]:
    records: list[BoxRecord] = []
    offset = start
    while offset + _BOX_HEADER_SIZE <= end:
        header = _read_box_header(data, offset, end)
        if header is None:
            break
        box_type, size, header_size = header
        record = BoxRecord(box_type=box_type, offset=offset, size=size, header_size=header_size, depth=depth)
        records.append(record)
        child_start = offset + header_size + _SAMPLE_ENTRY_HEADER_BYTES.get(box_type, 0)
        child_end = offset + size
        if child_start + _BOX_HEADER_SIZE <= child_end and depth < max_depth:
            records.extend(_walk_boxes(data, child_start, child_end, depth=depth + 1, max_depth=max_depth))
        offset += size
    return records


def _load_file_prefix(path: Path, max_bytes: int = _MAX_INSPECT_BYTES) -> bytes:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file too large for in-memory inspection: {size} bytes > {max_bytes}")
    return path.read_bytes()


def _read_exact(handle, offset: int, size: int) -> bytes:
    handle.seek(offset)
    return handle.read(size)


def _read_box_header_from_file(handle, offset: int, limit: int) -> tuple[str, int, int] | None:
    header = _read_exact(handle, offset, _BOX_HEADER_SIZE)
    if len(header) < _BOX_HEADER_SIZE:
        return None
    size32 = int.from_bytes(header[:4], "big")
    try:
        box_type = header[4:8].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r"[\x20-\x7e]{4}", box_type):
        return None
    header_size = _BOX_HEADER_SIZE
    if size32 == 1:
        largesize = _read_exact(handle, offset + 8, 8)
        if len(largesize) < 8:
            return None
        size = int.from_bytes(largesize, "big")
        header_size = 16
    elif size32 == 0:
        size = limit - offset
    else:
        size = size32
    if size < header_size or offset + size > limit:
        return None
    return box_type, size, header_size


def _file_box_details(handle, box_type: str, payload_start: int, payload_size: int) -> dict[str, object]:
    if box_type not in {"pssh", "tenc", "schm"}:
        return {}
    if payload_size > 1024 * 1024:
        return {"warning": f"{box_type} payload unexpectedly large: {payload_size} bytes"}
    payload = _read_exact(handle, payload_start, payload_size)
    return _box_details(box_type, payload)


def _walk_boxes_file(
    handle,
    start: int,
    end: int,
    *,
    depth: int = 0,
    max_depth: int = 8,
) -> list[BoxRecord]:
    records: list[BoxRecord] = []
    offset = start
    while offset + _BOX_HEADER_SIZE <= end:
        header = _read_box_header_from_file(handle, offset, end)
        if header is None:
            break
        box_type, size, header_size = header
        payload_start = offset + header_size
        payload_end = offset + size
        record = BoxRecord(
            box_type=box_type,
            offset=offset,
            size=size,
            header_size=header_size,
            depth=depth,
            details=_file_box_details(handle, box_type, payload_start, payload_end - payload_start),
        )
        records.append(record)

        if depth < max_depth:
            if box_type in _CONTAINER_BOXES:
                child_start = payload_start
                if box_type == "meta":
                    child_start += 4
                records.extend(
                    _walk_boxes_file(handle, child_start, payload_end, depth=depth + 1, max_depth=max_depth)
                )
            elif box_type == "stsd" and payload_end - payload_start >= 8:
                records.extend(
                    _walk_sample_entries_file(handle, payload_start + 8, payload_end, depth + 1, max_depth)
                )
        offset += size
    return records


def _walk_sample_entries_file(handle, start: int, end: int, depth: int, max_depth: int) -> list[BoxRecord]:
    records: list[BoxRecord] = []
    offset = start
    while offset + _BOX_HEADER_SIZE <= end:
        header = _read_box_header_from_file(handle, offset, end)
        if header is None:
            break
        box_type, size, header_size = header
        record = BoxRecord(box_type=box_type, offset=offset, size=size, header_size=header_size, depth=depth)
        records.append(record)
        child_start = offset + header_size + _SAMPLE_ENTRY_HEADER_BYTES.get(box_type, 0)
        child_end = offset + size
        if child_start + _BOX_HEADER_SIZE <= child_end and depth < max_depth:
            records.extend(
                _walk_boxes_file(handle, child_start, child_end, depth=depth + 1, max_depth=max_depth)
            )
        offset += size
    return records


def _records_to_text(records: Iterable[BoxRecord], *, target_boxes: set[str]) -> str:
    lines = ["ISO-BMFF box inspection", "─" * 72]
    for rec in records:
        if target_boxes and rec.box_type not in target_boxes:
            continue
        indent = "  " * rec.depth
        lines.append(f"{indent}{rec.box_type} offset={rec.offset} size={rec.size} end={rec.end}")
        for key, value in rec.details.items():
            lines.append(f"{indent}  - {key}: {value}")
    if len(lines) == 2:
        lines.append("(no matching boxes found)")
    return "\n".join(lines)


def inspect_iso_bmff(file_path: str, target_boxes: str = "moov,pssh,tenc,schm,schi", max_depth: int = 8) -> str:
    """Inspect ISO-BMFF/MP4 box offsets and selected DRM metadata.

    Args:
        file_path: Absolute or user-relative path to an MP4/fMP4/CMAF file.
        target_boxes: Comma-separated box types to show. Empty string shows all
            parsed boxes.
        max_depth: Recursive parsing depth. Increase only for unusual files.

    Returns:
        Human-readable offsets, sizes, and parsed pssh/tenc/schm fields.
    """
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return f"❌ 找不到檔案：{path}"
    try:
        targets = {part.strip() for part in target_boxes.split(",") if part.strip()}
        with path.open("rb") as handle:
            records = _walk_boxes_file(
                handle,
                0,
                path.stat().st_size,
                max_depth=max(0, int(max_depth)),
            )
        return _records_to_text(records, target_boxes=targets)
    except Exception as exc:
        return f"❌ ISO-BMFF 解析失敗：{type(exc).__name__}: {exc}"


def parse_pssh_box(pssh_data: str) -> str:
    """Parse a Base64 or hex-encoded PSSH box.

    This extracts metadata only: System ID, KID list, and opaque payload hash.
    It does not derive or request content keys.
    """
    raw = (pssh_data or "").strip()
    if not raw:
        return "❌ 請提供 Base64 或 hex 格式的 pssh box。"
    try:
        compact = re.sub(r"\s+", "", raw)
        if re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", compact) and len(compact.replace("0x", "")) % 2 == 0:
            data = bytes.fromhex(compact[2:] if compact.startswith("0x") else compact)
        else:
            data = base64.b64decode(compact, validate=True)
        header = _read_box_header(data, 0, len(data))
        if header is None or header[0] != "pssh":
            return "❌ 這段資料不是完整的 pssh box（需要包含 size/type header）。"
        _box_type, size, header_size = header
        details = _parse_pssh_payload(data[header_size:size])
        return json.dumps(details, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"❌ PSSH 解析失敗：{type(exc).__name__}: {exc}"


def generate_cenc_key_material(label: str = "") -> str:
    """Generate fresh CENC key material for owned test content.

    Returns a 128-bit AES content key and a 128-bit KID. Keep the key secret;
    the KID is safe to place in manifests/pssh as an identifier.
    """
    kid = uuid.uuid4().hex
    key = secrets.token_hex(16)
    iv = secrets.token_hex(16)
    payload = {
        "label": label or "media-asset",
        "kid_hex": kid,
        "kid_uuid": str(uuid.UUID(hex=kid)),
        "content_key_hex": key,
        "iv_hex": iv,
        "ffmpeg_cenc_flags": f"-encryption_scheme cenc-aes-ctr -encryption_key {key} -encryption_kid {kid}",
        "warning": "Store content_key_hex in a KMS/license server; do not commit it to source control.",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_hex(name: str, value: str, length: int) -> str:
    compact = (value or "").strip().replace("-", "")
    if len(compact) != length or not re.fullmatch(r"[0-9a-fA-F]+", compact):
        raise ValueError(f"{name} must be {length} hex characters")
    return compact.lower()


def build_ffmpeg_cenc_command(
    input_path: str,
    output_path: str,
    encryption_key_hex: str,
    encryption_kid_hex: str,
    video_codec: str = "copy",
    audio_codec: str = "copy",
) -> str:
    """Build an FFmpeg command that writes CENC metadata into an MP4.

    FFmpeg's MP4 muxer supports CENC AES-CTR via -encryption_scheme,
    -encryption_key, and -encryption_kid. CTR mode is seek-friendly because
    each block is effectively encrypted with a counter-derived keystream, so
    playback can decrypt random access ranges without decrypting every prior
    block as CBC would require.
    """
    try:
        key = _validate_hex("encryption_key_hex", encryption_key_hex, 32)
        kid = _validate_hex("encryption_kid_hex", encryption_kid_hex, 32)
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(Path(input_path).expanduser()),
            "-map",
            "0",
            "-c:v",
            video_codec,
            "-c:a",
            audio_codec,
            "-movflags",
            "+faststart",
            "-encryption_scheme",
            "cenc-aes-ctr",
            "-encryption_key",
            key,
            "-encryption_kid",
            kid,
            str(Path(output_path).expanduser()),
        ]
        return " ".join(shlex.quote(part) for part in cmd)
    except Exception as exc:
        return f"❌ FFmpeg CENC command 產生失敗：{type(exc).__name__}: {exc}"


def simulate_license_challenge(key_id_hex: str = "", content_key_hex: str = "") -> str:
    """Simulate a signed CDM-to-license-server challenge and wrapped key.

    The simulation models three primitives used in real systems:
    - ECDSA signs the client challenge, proving message integrity.
    - RSA-OAEP wraps the content key to the client's public key.
    - HMAC-SHA256 authenticates the license response body.

    This is for architecture testing only. Real Widevine/PlayReady/FairPlay
    license messages are CDM-specific and protected by vendor trust chains.
    """
    try:
        from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    except Exception as exc:
        return f"❌ 缺少 cryptography 套件：{exc}"

    try:
        kid = _validate_hex("key_id_hex", key_id_hex or uuid.uuid4().hex, 32)
        key = _validate_hex("content_key_hex", content_key_hex or secrets.token_hex(16), 32)
    except Exception as exc:
        return f"❌ 參數錯誤：{exc}"

    client_signing_key = ec.generate_private_key(ec.SECP256R1())
    client_public_pem = client_signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    client_rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    challenge = {
        "kid": kid,
        "nonce": secrets.token_urlsafe(18),
        "timestamp": int(time.time()),
        "requested_protection": "CDM simulation; no third-party key extraction",
        "client_capabilities": ["cenc-aes-ctr", "hls-aes128", "secure-clock"],
        "client_signing_key_sha256": hashlib.sha256(client_public_pem).hexdigest(),
    }
    canonical = json.dumps(challenge, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = client_signing_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    wrapped_key = client_rsa_key.public_key().encrypt(
        bytes.fromhex(key),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=b"media-license-demo",
        ),
    )

    response_body = {
        "kid": kid,
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "policy": {"hdcp": "optional", "rental_seconds": 3600, "renewal": False},
    }
    response_canonical = json.dumps(response_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    response_mac_key = secrets.token_bytes(32)
    mac = crypto_hmac.HMAC(response_mac_key, hashes.SHA256())
    mac.update(response_canonical)
    response_hmac = mac.finalize()

    result = {
        "challenge": challenge,
        "challenge_signature_ecdsa_der_b64": base64.b64encode(signature).decode("ascii"),
        "license_response": response_body,
        "license_response_hmac_sha256_b64": base64.b64encode(response_hmac).decode("ascii"),
        "explanation": (
            "Client signs the challenge so the license server can detect tampering. "
            "The server wraps the content key with RSA-OAEP so only the holder of "
            "the matching private key can unwrap it. HMAC protects response integrity "
            "in this simulation; production CDMs use vendor-specific authenticated "
            "license containers and hardware-backed trust where available."
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def media_security_blueprint() -> str:
    """Return a high-level DRM/HLS delivery blueprint."""
    return (
        "DRM-protected media delivery blueprint\n"
        "─────────────────────────────────────\n"
        "1. Ingest mezzanine → transcode ABR ladder with aligned GOPs.\n"
        "2. Package CMAF/fMP4 or HLS segments. For CENC, write KID/tenc/pssh metadata; "
        "for simple HLS AES-128, write EXT-X-KEY with an authenticated key URI.\n"
        "3. Store content keys in KMS. Manifests may expose KID, never raw keys.\n"
        "4. Player loads manifest → EME/CDM receives init data (pssh/skd).\n"
        "5. CDM creates signed challenge containing KID, nonce, session, capabilities.\n"
        "6. License server validates user/session/device policy, wraps key to CDM/client, "
        "and returns a signed/authenticated license.\n"
        "7. CDM decrypts samples inside its protected boundary. Hardware root of trust "
        "reduces key exposure and memory-dump risk; key rotation limits blast radius."
    )


def package_hls_aes128(
    input_path: str,
    key_uri: str,
    output_dir: str = "",
    key_id: str = "",
    segment_seconds: int = 6,
) -> str:
    """Package owned content into AES-128 encrypted multi-bitrate HLS.

    This wraps scripts/hls_aes_packager.py and writes output under output_dir
    or ~/Downloads/hls_<input_stem>. It does not fetch or decrypt third-party
    protected media.
    """
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        return f"❌ 找不到輸入影片：{source}"
    target = Path(output_dir).expanduser().resolve() if output_dir else Path.home() / "Downloads" / f"hls_{source.stem}"
    script = Path(REPO_ROOT) / "scripts" / "hls_aes_packager.py"
    cmd = [
        sys.executable,
        str(script),
        str(source),
        "--output",
        str(target),
        "--key-uri",
        key_uri,
        "--segment-seconds",
        str(int(segment_seconds)),
    ]
    if key_id:
        cmd.extend(["--key-id", key_id])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except Exception as exc:
        return f"❌ HLS AES-128 打包失敗：{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return (
            "❌ HLS AES-128 打包失敗\n"
            f"exit={result.returncode}\n"
            f"stderr:\n{result.stderr[-3000:]}"
        )
    return (
        "✅ HLS AES-128 打包完成\n"
        f"輸出：{target}\n"
        f"Master manifest：{target / 'master.m3u8'}\n"
        f"{result.stdout.strip()}"
    )


def _parse_hls_attributes(line: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    if ":" not in line:
        return attrs
    body = line.split(":", 1)[1]
    pattern = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
    for match in pattern.finditer(body):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1)] = value
    return attrs


def _redact_uri(uri: str) -> str:
    cleaned = (uri or "").strip()
    if not cleaned:
        return ""
    split = urllib.parse.urlsplit(cleaned)
    if not split.scheme:
        return re.sub(
            r"([?&](?:token|sig|signature|key|auth|policy|expires|x-amz-[^=]+)=)[^&]+",
            r"\1<redacted>",
            cleaned,
            flags=re.IGNORECASE,
        )
    redacted_query = "redacted=true" if split.query else ""
    return urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, redacted_query, ""))


def _manifest_source_with_headers(
    manifest: str,
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    raw = manifest or ""
    stripped = raw.strip()
    if not stripped:
        raise ValueError("manifest input is empty")
    if re.match(r"^https?://", stripped, flags=re.IGNORECASE):
        request_headers = {"User-Agent": "xiaohong-media-security/1.0"}
        request_headers.update(headers or {})
        req = urllib.request.Request(stripped, headers=request_headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read(_MAX_MANIFEST_BYTES + 1)
        if len(data) > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest is too large")
        return f"url:{_redact_uri(stripped)}", data.decode("utf-8", errors="replace")
    if "\n" not in stripped and "\r" not in stripped and len(stripped) < 1024:
        candidate = Path(stripped).expanduser()
        try:
            if candidate.exists() and candidate.is_file():
                data = candidate.read_bytes()
                if len(data) > _MAX_MANIFEST_BYTES:
                    raise ValueError("manifest is too large")
                return f"file:{candidate.resolve()}", data.decode("utf-8", errors="replace")
        except OSError:
            pass
    return "inline", raw


def _manifest_source(manifest: str) -> tuple[str, str]:
    return _manifest_source_with_headers(manifest)


def _infer_drm_from_hls_key(attrs: dict[str, str]) -> str:
    keyformat = attrs.get("KEYFORMAT", "").lower()
    method = attrs.get("METHOD", "").upper()
    if "apple" in keyformat or "skd" in attrs.get("URI", "").lower():
        return "FairPlay"
    if "widevine" in keyformat:
        return "Widevine"
    if "playready" in keyformat:
        return "PlayReady"
    if method == "AES-128":
        return "HLS AES-128"
    if method == "SAMPLE-AES":
        return "Sample AES"
    return method or "unknown"


def _analyze_hls_manifest(text: str, source: str) -> dict[str, object]:
    keys: list[dict[str, object]] = []
    variants = 0
    media_segments = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            variants += 1
        elif line and not line.startswith("#"):
            media_segments += 1
        elif line.startswith("#EXT-X-KEY") or line.startswith("#EXT-X-SESSION-KEY"):
            attrs = _parse_hls_attributes(line)
            key_record = {
                "tag": line.split(":", 1)[0],
                "method": attrs.get("METHOD", ""),
                "keyformat": attrs.get("KEYFORMAT", "identity"),
                "uri": _redact_uri(attrs.get("URI", "")),
                "iv_present": bool(attrs.get("IV")),
                "drm_system": _infer_drm_from_hls_key(attrs),
            }
            keys.append(key_record)
    drm_systems = sorted({str(key["drm_system"]) for key in keys if key.get("method") != "NONE"})
    return {
        "status": "success",
        "manifest_type": "hls",
        "source": source,
        "encrypted": bool(drm_systems),
        "drm_systems": drm_systems,
        "keys": keys,
        "variant_count": variants,
        "media_segment_reference_count": media_segments,
        "notes": [
            "This scanner identifies manifest-declared encryption only.",
            "It does not fetch key URIs, license URLs, or derive decryption keys.",
        ],
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _analyze_dash_manifest(text: str, source: str) -> dict[str, object]:
    root = ET.fromstring(text)
    protections: list[dict[str, object]] = []
    pssh_records: list[dict[str, object]] = []
    representations = 0
    periods = 0
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "Period":
            periods += 1
        elif name == "Representation":
            representations += 1
        elif name == "ContentProtection":
            attrs = {k.rsplit("}", 1)[-1]: v for k, v in element.attrib.items()}
            scheme = attrs.get("schemeIdUri", "")
            system_id = ""
            drm_name = "unknown"
            match = re.search(r"urn:uuid:([0-9a-fA-F-]{36})", scheme)
            if match:
                system_id = str(uuid.UUID(match.group(1))).lower()
                drm_name = _DRM_SYSTEM_IDS.get(system_id, "unknown")
            protection = {
                "scheme_id_uri": scheme,
                "drm_system": drm_name,
                "system_id": system_id,
                "default_kid": attrs.get("default_KID", attrs.get("default_Kid", "")),
                "value": attrs.get("value", ""),
            }
            protections.append(protection)
            for child in element:
                if _local_name(child.tag) == "pssh" and child.text:
                    compact = re.sub(r"\s+", "", child.text)
                    pssh_records.append({
                        "system_id": system_id,
                        "drm_system": drm_name,
                        "size_base64_chars": len(compact),
                        "sha256": hashlib.sha256(compact.encode("ascii", errors="ignore")).hexdigest(),
                        "base64_prefix": compact[:32],
                    })
    drm_systems = sorted({str(item["drm_system"]) for item in protections if item.get("drm_system")})
    return {
        "status": "success",
        "manifest_type": "dash",
        "source": source,
        "encrypted": bool(protections),
        "drm_systems": drm_systems,
        "content_protection": protections,
        "pssh": pssh_records,
        "period_count": periods,
        "representation_count": representations,
        "notes": [
            "PSSH is reported as metadata hashes/prefixes for auditability.",
            "This tool does not contact a license server or recover content keys.",
        ],
    }


def analyze_drm_manifest(manifest: str) -> str:
    """Analyze an HLS or MPEG-DASH manifest for legal DRM/compliance review.

    The scanner answers questions such as "does this playlist contain
    #EXT-X-KEY?", "which DRM System IDs are declared?", and "which KIDs are
    visible in DASH ContentProtection?". It deliberately does not fetch key
    URIs, call license servers, unwrap licenses, or decrypt media.
    """
    try:
        source, text = _manifest_source(manifest)
        stripped = text.lstrip("\ufeff\r\n\t ")
        if stripped.startswith("#EXTM3U") or "#EXT-X-KEY" in stripped or "#EXT-X-STREAM-INF" in stripped:
            report = _analyze_hls_manifest(text, source)
        elif "<MPD" in stripped[:500] or "<mpd" in stripped[:500].lower():
            report = _analyze_dash_manifest(text, source)
        else:
            report = {
                "status": "unknown",
                "source": source,
                "manifest_type": "unknown",
                "encrypted": False,
                "notes": ["Input did not look like HLS (#EXTM3U) or MPEG-DASH (<MPD>)."],
            }
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"❌ DRM manifest 分析失敗：{type(exc).__name__}: {exc}"


def _clean_header_value(name: str, value: str, *, max_len: int = 512) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if "\r" in cleaned or "\n" in cleaned:
        raise ValueError(f"{name} header 不能包含換行字元")
    if len(cleaned) > max_len:
        raise ValueError(f"{name} header 太長")
    return cleaned


def _clean_optional_url_header(name: str, value: str) -> str:
    cleaned = _clean_header_value(name, value)
    if not cleaned:
        return ""
    if not re.match(r"^https?://", cleaned, flags=re.IGNORECASE):
        raise ValueError(f"{name} 必須是 http/https URL")
    return cleaned


def _n_m3u8dl_headers(
    user_agent: str = "",
    referer: str = "",
    origin: str = "",
    accept_language: str = "",
) -> dict[str, str]:
    headers = {
        "User-Agent": _clean_header_value(
            "User-Agent",
            user_agent or _DEFAULT_BROWSER_USER_AGENT,
        )
    }
    ref = _clean_optional_url_header("Referer", referer)
    if ref:
        headers["Referer"] = ref
    org = _clean_optional_url_header("Origin", origin)
    if org:
        headers["Origin"] = org
    lang = _clean_header_value("Accept-Language", accept_language)
    if lang:
        headers["Accept-Language"] = lang
    return headers


def _safe_n_m3u8dl_save_name(value: str) -> str:
    name = (value or "").strip()
    if not name:
        name = f"hls_{time.time_ns()}"
    name = name.replace("..", "_").replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w一-鿿.\- ]+", "_", name, flags=re.UNICODE).strip(" .")
    return name[:120] or f"hls_{int(time.time())}"


def _hls_variant_playlist_urls(text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    expect_variant_uri = False

    def _add_url(raw_url: str) -> None:
        child_url = urllib.parse.urljoin(base_url, raw_url)
        parsed = urllib.parse.urlsplit(child_url)
        if parsed.scheme not in {"http", "https"} or child_url in urls:
            return
        urls.append(child_url)
        if len(urls) > _HLS_MAX_VARIANT_CHECKS:
            raise ValueError(
                f"HLS variant playlist 數量超過安全檢查上限 {_HLS_MAX_VARIANT_CHECKS}，已拒絕下載。"
            )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA") or line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            uri = _parse_hls_attributes(line).get("URI", "")
            if uri:
                _add_url(uri)
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            expect_variant_uri = True
            continue
        if line.startswith("#"):
            continue
        is_playlist = expect_variant_uri or ".m3u8" in line.lower()
        expect_variant_uri = False
        if not is_playlist:
            continue
        _add_url(line)
    return urls


def _disallowed_hls_uri(text: str, base_url: str) -> str:
    """Return the first HLS URI that resolves outside http(s), if any."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        uri = ""
        if line.startswith("#"):
            if line.startswith(("#EXT-X-MAP", "#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF")):
                uri = _parse_hls_attributes(line).get("URI", "")
        else:
            uri = line
        if not uri:
            continue
        resolved = urllib.parse.urljoin(base_url, uri)
        scheme = urllib.parse.urlsplit(resolved).scheme.lower()
        if scheme not in _HLS_DOWNLOAD_ALLOWED_URI_SCHEMES:
            return _redact_uri(resolved)
    return ""


def _scan_clear_hls_manifest_for_download(
    manifest_url: str,
    text: str,
    headers: dict[str, str],
) -> tuple[bool, dict[str, object], list[str]]:
    source = f"url:{_redact_uri(manifest_url)}"
    checked = [source]
    report = _analyze_hls_manifest(text, source)
    if report.get("encrypted"):
        return False, report, checked
    disallowed = _disallowed_hls_uri(text, manifest_url)
    if disallowed:
        raise ValueError(f"manifest 內含非 http(s) URI，已拒絕：{disallowed}")

    for child_url in _hls_variant_playlist_urls(text, manifest_url):
        try:
            child_source, child_text = _manifest_source_with_headers(child_url, headers)
        except Exception as exc:
            raise ValueError(
                f"無法安全檢查變體 playlist：{_redact_uri(child_url)} "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        checked.append(child_source)
        child_report = _analyze_hls_manifest(child_text, child_source)
        if child_report.get("encrypted"):
            return False, child_report, checked
        disallowed = _disallowed_hls_uri(child_text, child_url)
        if disallowed:
            raise ValueError(f"變體 playlist 內含非 http(s) URI，已拒絕：{disallowed}")

    return True, report, checked


def _resolve_download_binary(name: str, *fallbacks: str) -> str:
    exe = shutil.which(name)
    if exe:
        return exe
    sibling = Path(sys.executable).with_name(name)
    if sibling.is_file() and os.access(str(sibling), os.X_OK):
        return str(sibling)
    for value in fallbacks:
        fallback = Path(value)
        if fallback.is_file() and os.access(str(fallback), os.X_OK):
            return str(fallback)
    raise FileNotFoundError(f"找不到 {name}，請先安裝到 PATH 或常用工具目錄")


def _n_m3u8dl_executable() -> str:
    try:
        return _resolve_download_binary("N_m3u8DL-RE", "/opt/homebrew/bin/N_m3u8DL-RE")
    except FileNotFoundError as exc:
        raise FileNotFoundError("找不到 N_m3u8DL-RE，請先安裝到 PATH 或 /opt/homebrew/bin") from exc


def _ffmpeg_executable() -> str:
    try:
        return _resolve_download_binary("ffmpeg", "/opt/homebrew/bin/ffmpeg")
    except FileNotFoundError as exc:
        raise FileNotFoundError("找不到 ffmpeg，請先安裝到 PATH 或 /opt/homebrew/bin") from exc


def _yt_dlp_executable() -> str:
    try:
        return _resolve_download_binary(
            "yt-dlp",
            str(Path(REPO_ROOT) / ".venv" / "bin" / "yt-dlp"),
            "/opt/homebrew/bin/yt-dlp",
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError("找不到 yt-dlp，請先安裝到 PATH 或目前 Python venv") from exc


def _ffmpeg_executable_for_n_m3u8dl() -> str:
    try:
        return _ffmpeg_executable()
    except FileNotFoundError as exc:
        raise FileNotFoundError("找不到 ffmpeg，N_m3u8DL-RE 需要 ffmpeg 合併輸出") from exc


def _build_n_m3u8dl_command(
    manifest_url: str,
    output_dir: Path,
    save_name: str,
    headers: dict[str, str],
    thread_count: int,
) -> list[str]:
    ffmpeg_path = _ffmpeg_executable_for_n_m3u8dl()
    cmd = [
        _n_m3u8dl_executable(),
        manifest_url,
        "--save-dir",
        str(output_dir),
        "--save-name",
        save_name,
        "--thread-count",
        str(thread_count),
        "--download-retry-count",
        "5",
        "--http-request-timeout",
        "60",
        "--auto-select",
        "--del-after-done",
        "--no-ansi-color",
        "--no-log",
        "--log-level",
        "INFO",
        "--ffmpeg-binary-path",
        ffmpeg_path,
        "-M",
        "format=mp4:muxer=ffmpeg",
    ]
    for name in ("User-Agent", "Referer", "Origin", "Accept-Language"):
        value = headers.get(name)
        if value:
            cmd.extend(["-H", f"{name}: {value}"])
    return cmd


def _hls_target_mp4_path(output_dir: Path, save_name: str) -> Path:
    stem = _safe_n_m3u8dl_save_name(save_name)
    if stem.lower().endswith(".mp4"):
        filename = stem
    else:
        filename = f"{Path(stem).stem}.mp4"
    return (output_dir / filename).resolve()


def _unique_hls_target_mp4_path(output_dir: Path, save_name: str) -> Path:
    target = _hls_target_mp4_path(output_dir, save_name)
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix or ".mp4"
    for index in range(2, 1000):
        candidate = (target.parent / f"{stem}_{index}{suffix}").resolve()
        if not candidate.exists():
            return candidate
    return (target.parent / f"{stem}_{time.time_ns()}{suffix}").resolve()


def _ffmpeg_header_blob(headers: dict[str, str]) -> str:
    return "".join(
        f"{name}: {headers[name]}\r\n"
        for name in ("Referer", "Origin", "Accept-Language")
        if headers.get(name)
    )


def _build_ffmpeg_hls_copy_command(
    manifest_url: str,
    output_path: Path,
    headers: dict[str, str],
    thread_count: int,
) -> list[str]:
    del thread_count
    cmd = [
        _ffmpeg_executable(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,http,https,tcp,tls",
        "-user_agent",
        headers["User-Agent"],
    ]
    header_blob = _ffmpeg_header_blob(headers)
    if header_blob:
        cmd.extend(["-headers", header_blob])
    cmd.extend([
        "-i",
        manifest_url,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ])
    return cmd


def _build_ytdlp_hls_command(
    manifest_url: str,
    output_path: Path,
    headers: dict[str, str],
    thread_count: int,
) -> list[str]:
    del thread_count
    cmd = [
        _yt_dlp_executable(),
        manifest_url,
        "-o",
        str(output_path),
        "--merge-output-format",
        "mp4",
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--socket-timeout",
        "30",
        "--no-mtime",
        "--user-agent",
        headers["User-Agent"],
    ]
    if headers.get("Referer"):
        cmd.extend(["--referer", headers["Referer"]])
    for name in ("Origin", "Accept-Language"):
        if headers.get(name):
            cmd.extend(["--add-header", f"{name}: {headers[name]}"])
    return cmd


def _snapshot_output_files(output_dir: Path) -> set[Path]:
    if not output_dir.exists():
        return set()
    return {path.resolve() for path in output_dir.rglob("*") if path.is_file()}


def _new_hls_download_artifacts(
    output_dir: Path,
    before: set[Path],
    expected_output: Path | None = None,
) -> list[str]:
    candidates: list[Path] = []
    if expected_output and expected_output.is_file():
        candidates.append(expected_output.resolve())
    if not output_dir.exists():
        return [str(path) for path in candidates]
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in candidates:
            continue
        if resolved in before:
            continue
        if path.suffix.lower() in _N_M3U8DL_OUTPUT_SUFFIXES:
            candidates.append(resolved)
    candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name))
    return [str(path) for path in candidates]


def _download_clear_hls_with_engine(
    engine: str,
    engine_label: str,
    manifest_url: str,
    output_dir: str = "",
    save_name: str = "",
    user_agent: str = "",
    referer: str = "",
    origin: str = "",
    accept_language: str = "",
    thread_count: int = 10,
    timeout_seconds: int = 1800,
) -> str:
    """Download an authorized clear HLS/m3u8 stream with the selected engine."""
    from agent_core.tool_result import ErrorCode, ToolResult

    url = (manifest_url or "").strip()
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return ToolResult.failure(
            "manifest_url 必須是 http/https m3u8 URL。",
            error_code=ErrorCode.INVALID_INPUT,
            recoverable=False,
        )

    try:
        headers = _n_m3u8dl_headers(
            user_agent=user_agent,
            referer=referer,
            origin=origin,
            accept_language=accept_language,
        )
        workers = max(1, min(int(thread_count), 32))
        timeout = max(60, min(int(timeout_seconds), 7200))
    except Exception as exc:
        return ToolResult.failure(
            f"下載參數不合法：{type(exc).__name__}: {exc}",
            error_code=ErrorCode.INVALID_INPUT,
            recoverable=False,
        )

    target_dir = Path(output_dir).expanduser().resolve() if output_dir else Path.home() / "Downloads"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_n_m3u8dl_save_name(save_name)
    expected_output: Path | None = None

    try:
        _, manifest_text = _manifest_source_with_headers(url, headers)
    except Exception as exc:
        return ToolResult.failure(
            f"讀取 m3u8 manifest 失敗：{type(exc).__name__}: {exc}",
            error_code=ErrorCode.NETWORK,
            suggested_fix="確認網址可公開存取，或提供合法授權播放用的 m3u8 URL。",
        )

    stripped = manifest_text.lstrip("\ufeff\r\n\t ")
    if not stripped.startswith("#EXTM3U"):
        return ToolResult.failure(
            "目前這個工具只支援 HLS/m3u8 manifest；輸入內容不是 #EXTM3U。",
            error_code=ErrorCode.UNSUPPORTED,
            recoverable=False,
        )

    try:
        is_clear, drm_report, checked_sources = _scan_clear_hls_manifest_for_download(
            url,
            manifest_text,
            headers,
        )
    except Exception as exc:
        return ToolResult.failure(
            f"下載前安全檢查失敗：{type(exc).__name__}: {exc}",
            error_code=ErrorCode.NETWORK,
            suggested_fix="我需要先確認 manifest 沒有宣告加密/DRM，才能自動下載。",
        )
    if not is_clear:
        systems = ", ".join(str(x) for x in drm_report.get("drm_systems", []) or ["unknown"])
        return ToolResult.failure(
            f"這個 HLS manifest 宣告了加密/DRM（{systems}），我不能協助下載或解密完整內容。",
            error_code=ErrorCode.UNSUPPORTED,
            recoverable=False,
            suggested_fix="可以改提供公開未加密 m3u8，或您自有內容的原始影片檔讓我處理。",
        )

    try:
        if engine == "n_m3u8dl":
            cmd = _build_n_m3u8dl_command(url, target_dir, safe_name, headers, workers)
        elif engine == "ffmpeg":
            expected_output = _unique_hls_target_mp4_path(target_dir, safe_name)
            cmd = _build_ffmpeg_hls_copy_command(url, expected_output, headers, workers)
        elif engine == "yt_dlp":
            expected_output = _unique_hls_target_mp4_path(target_dir, safe_name)
            cmd = _build_ytdlp_hls_command(url, expected_output, headers, workers)
        else:
            raise ValueError(f"不支援的 HLS 下載引擎：{engine}")
    except Exception as exc:
        return ToolResult.failure(
            f"{engine_label} 執行環境未就緒：{type(exc).__name__}: {exc}",
            error_code=ErrorCode.NOT_FOUND,
            recoverable=False,
        )

    before = _snapshot_output_files(target_dir)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult.failure(
            f"{engine_label} 下載超過 {timeout} 秒未完成。",
            error_code=ErrorCode.TIMEOUT,
            suggested_fix="可以降低解析度/改短片段，或稍後網路較穩時重試。",
        )
    except Exception as exc:
        return ToolResult.failure(
            f"{engine_label} 啟動失敗：{type(exc).__name__}: {exc}",
            error_code=ErrorCode.INTERNAL,
        )

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-3000:]
        stdout_tail = (result.stdout or "").strip()[-2000:]
        detail = stderr_tail or stdout_tail or "未提供錯誤輸出"
        return ToolResult.failure(
            f"{engine_label} 下載失敗\n"
            f"exit={result.returncode}\n"
            f"{detail}",
            error_code=ErrorCode.NETWORK,
            suggested_fix="確認 manifest、User-Agent、Referer/Origin 是否為您有權使用的播放資訊。",
        )

    artifacts = _new_hls_download_artifacts(target_dir, before, expected_output)
    if not artifacts:
        return ToolResult.failure(
            f"{engine_label} 回報成功，但沒有找到新產生的媒體檔。",
            error_code=ErrorCode.INTERNAL,
            suggested_fix=f"請檢查輸出資料夾：{target_dir}",
        )

    lines = [
        f"✅ HLS/m3u8 下載完成（{engine_label}）",
        f"輸出資料夾：{target_dir}",
        f"User-Agent：{headers['User-Agent']}",
        f"已檢查 playlist：{len(checked_sources)} 個，未偵測到 manifest 宣告加密",
        "檔案：",
    ]
    for index, path in enumerate(artifacts, start=1):
        lines.append(f"{index}. {path}")
    return ToolResult.success(
        "\n".join(lines),
        data={
            "manifest_url": _redact_uri(url),
            "engine": engine,
            "output_dir": str(target_dir),
            "save_name": safe_name,
            "checked_sources": checked_sources,
            "headers": {key: ("<redacted>" if key.lower() in {"cookie", "authorization"} else value)
                        for key, value in headers.items()},
        },
        artifacts=artifacts,
    )


def download_hls_with_n_m3u8dl(
    manifest_url: str,
    output_dir: str = "",
    save_name: str = "",
    user_agent: str = "",
    referer: str = "",
    origin: str = "",
    accept_language: str = "",
    thread_count: int = 10,
    timeout_seconds: int = 1800,
) -> str:
    """Download an authorized clear HLS/m3u8 stream with N_m3u8DL-RE.

    The tool supports a browser-like User-Agent and a small allowlist of
    ordinary HTTP headers (Referer, Origin, Accept-Language). It intentionally
    refuses manifests that declare HLS/DRM encryption and never passes
    decryption keys, cookies, Authorization headers, or DRM bypass options to
    N_m3u8DL-RE.
    """
    return _download_clear_hls_with_engine(
        "n_m3u8dl",
        "N_m3u8DL-RE",
        manifest_url,
        output_dir=output_dir,
        save_name=save_name,
        user_agent=user_agent,
        referer=referer,
        origin=origin,
        accept_language=accept_language,
        thread_count=thread_count,
        timeout_seconds=timeout_seconds,
    )


def download_hls_with_ffmpeg_copy(
    manifest_url: str,
    output_dir: str = "",
    save_name: str = "",
    user_agent: str = "",
    referer: str = "",
    origin: str = "",
    accept_language: str = "",
    thread_count: int = 10,
    timeout_seconds: int = 1800,
) -> str:
    """Download an authorized clear HLS/m3u8 stream using FFmpeg stream copy.

    Equivalent safe shape:
    ffmpeg -protocol_whitelist file,http,https,tcp,tls -i manifest.m3u8 -c copy output.mp4
    """
    return _download_clear_hls_with_engine(
        "ffmpeg",
        "ffmpeg copy",
        manifest_url,
        output_dir=output_dir,
        save_name=save_name,
        user_agent=user_agent,
        referer=referer,
        origin=origin,
        accept_language=accept_language,
        thread_count=thread_count,
        timeout_seconds=timeout_seconds,
    )


def download_hls_with_ytdlp(
    manifest_url: str,
    output_dir: str = "",
    save_name: str = "",
    user_agent: str = "",
    referer: str = "",
    origin: str = "",
    accept_language: str = "",
    thread_count: int = 10,
    timeout_seconds: int = 1800,
) -> str:
    """Download an authorized clear HLS/m3u8 stream using yt-dlp."""
    return _download_clear_hls_with_engine(
        "yt_dlp",
        "yt-dlp",
        manifest_url,
        output_dir=output_dir,
        save_name=save_name,
        user_agent=user_agent,
        referer=referer,
        origin=origin,
        accept_language=accept_language,
        thread_count=thread_count,
        timeout_seconds=timeout_seconds,
    )


def assess_drm_request_safety(request_text: str) -> str:
    """Classify whether a DRM research request stays inside legal boundaries.

    Allowed: manifest inspection, PSSH metadata parsing, owned-content
    packaging, EME playback integration, license-server simulations, and
    defensive threat modeling.

    Blocked: CDM bypass, extracting third-party keys, HDCP/TEE circumvention,
    exploit steps, or instructions for white-box/side-channel key recovery.
    """
    text = request_text or ""
    blocked = []
    for category, pattern in _PROHIBITED_DRM_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            blocked.append(category)
    status = "blocked" if blocked else "allowed"
    result = {
        "status": status,
        "blocked_topics": sorted(set(blocked)),
        "allowed_capabilities": [
            "analyze HLS/DASH manifests and report declared encryption",
            "parse MP4/CMAF pssh/tenc metadata",
            "generate keys only for owned test content",
            "build EME playback integration for authorized streams",
            "simulate a license handshake without vendor CDM secrets",
            "produce defensive threat models and hardening checklists",
        ],
        "safe_redirect": (
            "我可以幫你做合法播放代理、manifest 合規掃描、CENC/HLS 打包、或防禦性威脅模型；"
            "不協助繞過 Widevine/PlayReady/FairPlay、提取第三方內容金鑰、偽造 CDM 或規避 HDCP/TEE。"
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def drm_chain_of_trust_model(detail_level: str = "standard") -> str:
    """Explain DRM's defensive chain of trust from a builder's perspective.

    The model is intentionally defensive: it describes why license handshakes,
    TEEs, secure video paths, and HDCP exist, then maps AI-agent capabilities to
    safe engineering tasks. It avoids operational bypass instructions.
    """
    verbose = (detail_level or "").lower() in {"deep", "advanced", "full", "verbose"}
    lines = [
        "DRM chain-of-trust model",
        "────────────────────────",
        "1. License handshake: the client/CDM sends signed init data, KID, nonce, session, and capability claims. The license server validates identity and policy, then returns an authenticated license with keys wrapped to the authorized client boundary.",
        "2. TEE / secure video path: on capable devices, key use and decoded frames remain inside a hardware-backed protected path, reducing exposure to normal-world memory inspection.",
        "3. Output protection: HDCP and platform output policies keep protected frames from leaving the device over untrusted display links.",
        "4. AI-agent role: inspect metadata, verify manifest policy, generate owned-content packaging commands, produce EME integration code, and audit configuration drift.",
        "5. Boundary: no CDM bypass, no third-party key extraction, no forged device identities, no HDCP/TEE circumvention.",
    ]
    if verbose:
        lines.extend([
            "",
            "Advanced defensive notes",
            "- Key rotation limits blast radius if an entitlement or packaging key is mishandled.",
            "- Secure clocks prevent replay of expired licenses when wall-clock time is attacker-controlled.",
            "- White-box and side-channel research can be discussed as risk categories for hardening, but not as extraction procedures.",
            "- CI/CD should scan manifests for accidental clear segments, tokenized key URIs in source control, missing KID mapping, and mismatched HDCP policy.",
        ])
    return "\n".join(lines)


def build_eme_player_template(
    manifest_url: str,
    license_server_url: str,
    drm_system: str = "widevine",
    content_type: str = "",
    autoplay: bool = False,
    video_content_type: str = 'video/mp4; codecs="avc1.640028"',
    audio_content_type: str = 'audio/mp4; codecs="mp4a.40.2"',
) -> str:
    """Generate a legal EME playback template for authorized streams.

    EME does not expose raw content keys to JavaScript. The browser passes init
    data from encrypted media to the CDM, the CDM creates a license challenge,
    and JavaScript only relays opaque bytes between the CDM and license server.
    That preserves the constant rule for a compliant player: application code
    orchestrates policy and transport, while key handling remains in the CDM or
    platform-protected path.
    """
    drm_key = (drm_system or "widevine").strip().lower()
    key_system = _EME_KEY_SYSTEMS.get(drm_key)
    if not key_system:
        return f"❌ 不支援的 DRM system：{drm_system}。可用：{', '.join(sorted(_EME_KEY_SYSTEMS))}"
    manifest = manifest_url.strip()
    license_url = license_server_url.strip()
    if not re.match(r"^https?://", manifest, flags=re.IGNORECASE):
        return "❌ manifest_url 必須是 http/https URL。"
    if not re.match(r"^https?://", license_url, flags=re.IGNORECASE):
        return "❌ license_server_url 必須是 http/https URL。"
    if content_type:
        video_content_type = content_type
    autoplay_attr = " autoplay muted" if autoplay else ""
    template = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorized EME Playback</title>
</head>
<body>
  <video id="player" controls playsinline{autoplay_attr} style="width:100%;max-width:960px;background:#000"></video>
  <script>
    const manifestUrl = {json.dumps(manifest)};
    const licenseServerUrl = {json.dumps(license_url)};
    const keySystem = {json.dumps(key_system)};
    const videoContentType = {json.dumps(video_content_type)};
    const audioContentType = {json.dumps(audio_content_type)};

    async function setupEme(video) {{
      const config = [{{
        initDataTypes: ["cenc", "keyids", "sinf"],
        audioCapabilities: [{{ contentType: audioContentType }}],
        videoCapabilities: [{{ contentType: videoContentType }}],
        distinctiveIdentifier: "optional",
        persistentState: "optional",
        sessionTypes: ["temporary"]
      }}];

      const access = await navigator.requestMediaKeySystemAccess(keySystem, config);
      const mediaKeys = await access.createMediaKeys();
      await video.setMediaKeys(mediaKeys);

      video.addEventListener("encrypted", async (event) => {{
        const session = mediaKeys.createSession();
        session.addEventListener("message", async (messageEvent) => {{
          const response = await fetch(licenseServerUrl, {{
            method: "POST",
            credentials: "include",
            headers: {{ "content-type": "application/octet-stream" }},
            body: messageEvent.message
          }});
          if (!response.ok) throw new Error(`License failed: ${{response.status}}`);
          await session.update(await response.arrayBuffer());
        }});
        await session.generateRequest(event.initDataType, event.initData);
      }});
    }}

    (async () => {{
      const video = document.getElementById("player");
      await setupEme(video);
      video.src = manifestUrl;
    }})().catch((error) => {{
      console.error(error);
      document.body.insertAdjacentHTML("beforeend", `<pre>${{error.stack || error}}</pre>`);
    }});
  </script>
</body>
</html>"""
    return template


__all__ = [
    "inspect_iso_bmff",
    "parse_pssh_box",
    "generate_cenc_key_material",
    "build_ffmpeg_cenc_command",
    "simulate_license_challenge",
    "media_security_blueprint",
    "package_hls_aes128",
    "analyze_drm_manifest",
    "download_hls_with_n_m3u8dl",
    "download_hls_with_ffmpeg_copy",
    "download_hls_with_ytdlp",
    "assess_drm_request_safety",
    "drm_chain_of_trust_model",
    "build_eme_player_template",
]
