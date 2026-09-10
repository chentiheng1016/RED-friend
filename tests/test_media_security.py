from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _box(box_type: str, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type.encode("ascii") + payload


class MediaSecurityTests(unittest.TestCase):
    def test_parse_pssh_box_extracts_system_id_and_kids(self):
        from agent_core.media_security import parse_pssh_box

        system_id = uuid.UUID("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed")
        kid = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
        data = b"provider-data"
        payload = (
            b"\x01\x00\x00\x00"
            + system_id.bytes
            + (1).to_bytes(4, "big")
            + kid.bytes
            + len(data).to_bytes(4, "big")
            + data
        )
        encoded = base64.b64encode(_box("pssh", payload)).decode("ascii")

        parsed = json.loads(parse_pssh_box(encoded))
        self.assertEqual(parsed["system_id"], str(system_id))
        self.assertEqual(parsed["kids"], [str(kid)])
        self.assertEqual(parsed["data_size"], len(data))

    def test_inspect_iso_bmff_reports_offsets_and_tenc_kid(self):
        from agent_core.media_security import inspect_iso_bmff

        kid = uuid.UUID("11112222-3333-4444-5555-666677778888")
        tenc_payload = b"\x00\x00\x00\x00" + b"\x00" + b"\x01" + b"\x08" + kid.bytes
        fake_mp4 = _box("ftyp", b"isom" + b"\0" * 12) + _box("moov", _box("tenc", tenc_payload))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.mp4"
            path.write_bytes(fake_mp4)
            report = inspect_iso_bmff(str(path), target_boxes="moov,tenc")

        self.assertIn("moov offset=", report)
        self.assertIn("tenc offset=", report)
        self.assertIn(str(kid), report)

    def test_generate_cenc_key_material_shape(self):
        from agent_core.media_security import generate_cenc_key_material

        data = json.loads(generate_cenc_key_material("asset"))
        self.assertEqual(len(data["kid_hex"]), 32)
        self.assertEqual(len(data["content_key_hex"]), 32)
        self.assertIn("-encryption_scheme cenc-aes-ctr", data["ffmpeg_cenc_flags"])

    def test_build_ffmpeg_cenc_command_validates_and_quotes(self):
        from agent_core.media_security import build_ffmpeg_cenc_command

        command = build_ffmpeg_cenc_command(
            "/tmp/input file.mp4",
            "/tmp/out.mp4",
            "00" * 16,
            "11" * 16,
        )
        self.assertIn("-encryption_scheme cenc-aes-ctr", command)
        self.assertIn("'/tmp/input file.mp4'", command)
        self.assertIn("00000000000000000000000000000000", command)
        self.assertIn("11111111111111111111111111111111", command)

    def test_simulate_license_challenge_contains_wrapped_key(self):
        from agent_core.media_security import simulate_license_challenge

        data = json.loads(simulate_license_challenge("22" * 16, "33" * 16))
        self.assertEqual(data["challenge"]["kid"], "22" * 16)
        self.assertIn("challenge_signature_ecdsa_der_b64", data)
        self.assertIn("wrapped_key_b64", data["license_response"])
        self.assertIn("license_response_hmac_sha256_b64", data)

    def test_analyze_hls_manifest_redacts_key_uri_and_detects_encryption(self):
        from agent_core.media_security import analyze_drm_manifest

        manifest = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example.test/key.bin?token=secret",IV=0x00112233445566778899AABBCCDDEEFF
#EXTINF:6.0,
seg-1.ts
"""
        data = json.loads(analyze_drm_manifest(manifest))
        self.assertEqual(data["manifest_type"], "hls")
        self.assertTrue(data["encrypted"])
        self.assertEqual(data["drm_systems"], ["HLS AES-128"])
        self.assertEqual(data["keys"][0]["uri"], "https://cdn.example.test/key.bin?redacted=true")

    def test_analyze_dash_manifest_detects_content_protection(self):
        from agent_core.media_security import analyze_drm_manifest

        manifest = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns:cenc="urn:mpeg:cenc:2013">
  <Period>
    <AdaptationSet>
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" cenc:default_KID="00112233-4455-6677-8899-aabbccddeeff">
        <cenc:pssh>AAAA</cenc:pssh>
      </ContentProtection>
      <Representation id="v1" />
    </AdaptationSet>
  </Period>
</MPD>
"""
        data = json.loads(analyze_drm_manifest(manifest))
        self.assertEqual(data["manifest_type"], "dash")
        self.assertTrue(data["encrypted"])
        self.assertEqual(data["drm_systems"], ["Widevine"])
        self.assertEqual(data["content_protection"][0]["default_kid"], "00112233-4455-6677-8899-aabbccddeeff")

    def test_download_hls_with_n_m3u8dl_passes_user_agent_without_shell(self):
        from agent_core import media_security

        manifest = """#EXTM3U
#EXT-X-VERSION:6
#EXTINF:6.0,
seg-1.ts
#EXT-X-ENDLIST
"""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "demo.mp4"

            def fake_run(cmd, **kwargs):
                output.write_bytes(b"fake-video")
                return SimpleNamespace(returncode=0, stdout="done", stderr="")

            def fake_which(name):
                return f"/tools/{name}"

            with mock.patch.object(
                media_security,
                "_manifest_source_with_headers",
                return_value=("url:https://cdn.example.test/master.m3u8", manifest),
            ), mock.patch.object(media_security.shutil, "which", side_effect=fake_which), \
                 mock.patch.object(media_security.subprocess, "run", side_effect=fake_run) as run:
                result = media_security.download_hls_with_n_m3u8dl(
                    "https://cdn.example.test/master.m3u8",
                    output_dir=tmp,
                    save_name="demo",
                    user_agent="Mozilla/5.0 Test-UA",
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.artifacts, [str(output.resolve())])
        cmd = run.call_args.args[0]
        self.assertIn("-H", cmd)
        self.assertIn("User-Agent: Mozilla/5.0 Test-UA", cmd)
        self.assertIn("-M", cmd)
        self.assertIn("format=mp4:muxer=ffmpeg", cmd)
        self.assertNotIn("--key", cmd)
        self.assertNotIn("--custom-hls-key", cmd)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_download_hls_with_ffmpeg_copy_uses_protocol_whitelist(self):
        from agent_core import media_security

        manifest = """#EXTM3U
#EXT-X-VERSION:6
#EXTINF:6.0,
https://cdn.example.test/seg-1.ts
#EXT-X-ENDLIST
"""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"

            def fake_run(cmd, **kwargs):
                output.write_bytes(b"fake-video")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(
                media_security,
                "_manifest_source_with_headers",
                return_value=("url:https://cdn.example.test/master.m3u8", manifest),
            ), mock.patch.object(media_security.shutil, "which", return_value="/tools/ffmpeg"), \
                 mock.patch.object(media_security.subprocess, "run", side_effect=fake_run) as run:
                result = media_security.download_hls_with_ffmpeg_copy(
                    "https://cdn.example.test/master.m3u8",
                    output_dir=tmp,
                    save_name="out.mp4",
                    user_agent="Mozilla/5.0 Test-UA",
                    referer="https://cdn.example.test/watch",
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.artifacts, [str(output.resolve())])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "/tools/ffmpeg")
        self.assertIn("-protocol_whitelist", cmd)
        self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "file,http,https,tcp,tls")
        self.assertIn("-user_agent", cmd)
        self.assertEqual(cmd[cmd.index("-user_agent") + 1], "Mozilla/5.0 Test-UA")
        self.assertIn("-headers", cmd)
        self.assertIn("Referer: https://cdn.example.test/watch", cmd[cmd.index("-headers") + 1])
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_download_hls_with_ytdlp_uses_output_and_user_agent(self):
        from agent_core import media_security

        manifest = """#EXTM3U
#EXT-X-VERSION:6
#EXTINF:6.0,
seg-1.ts
#EXT-X-ENDLIST
"""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.mp4"

            def fake_run(cmd, **kwargs):
                output.write_bytes(b"fake-video")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(
                media_security,
                "_manifest_source_with_headers",
                return_value=("url:https://cdn.example.test/master.m3u8", manifest),
            ), mock.patch.object(media_security.shutil, "which", return_value="/tools/yt-dlp"), \
                 mock.patch.object(media_security.subprocess, "run", side_effect=fake_run) as run:
                result = media_security.download_hls_with_ytdlp(
                    "https://cdn.example.test/master.m3u8",
                    output_dir=tmp,
                    save_name="out.mp4",
                    user_agent="Mozilla/5.0 Test-UA",
                )

        self.assertTrue(result.ok)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "/tools/yt-dlp")
        self.assertIn("https://cdn.example.test/master.m3u8", cmd)
        self.assertIn("-o", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], str(output.resolve()))
        self.assertIn("--user-agent", cmd)
        self.assertEqual(cmd[cmd.index("--user-agent") + 1], "Mozilla/5.0 Test-UA")
        self.assertNotIn("--cookies", cmd)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_ffmpeg_hls_download_does_not_reuse_existing_output_file(self):
        from agent_core import media_security

        manifest = """#EXTM3U
#EXT-X-VERSION:6
#EXTINF:6.0,
seg-1.ts
#EXT-X-ENDLIST
"""
        with tempfile.TemporaryDirectory() as tmp:
            old_output = Path(tmp) / "out.mp4"
            old_output.write_bytes(b"old-video")
            new_output = Path(tmp) / "out_2.mp4"

            def fake_run(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b"new-video")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(
                media_security,
                "_manifest_source_with_headers",
                return_value=("url:https://cdn.example.test/master.m3u8", manifest),
            ), mock.patch.object(media_security.shutil, "which", return_value="/tools/ffmpeg"), \
                 mock.patch.object(media_security.subprocess, "run", side_effect=fake_run):
                result = media_security.download_hls_with_ffmpeg_copy(
                    "https://cdn.example.test/master.m3u8",
                    output_dir=tmp,
                    save_name="out.mp4",
                )

            self.assertEqual(old_output.read_bytes(), b"old-video")
            self.assertEqual(new_output.read_bytes(), b"new-video")
            self.assertEqual(result.artifacts, [str(new_output.resolve())])

    def test_download_hls_rejects_non_http_manifest_uris_before_ffmpeg(self):
        from agent_core import media_security

        manifest = """#EXTM3U
#EXT-X-VERSION:6
#EXTINF:6.0,
file:///etc/passwd
"""
        with mock.patch.object(
            media_security,
            "_manifest_source_with_headers",
            return_value=("url:https://cdn.example.test/master.m3u8", manifest),
        ), mock.patch.object(media_security.subprocess, "run") as run:
            result = media_security.download_hls_with_ffmpeg_copy(
                "https://cdn.example.test/master.m3u8",
            )

        self.assertFalse(result.ok)
        self.assertIn("非 http(s)", str(result))
        run.assert_not_called()

    def test_download_hls_with_n_m3u8dl_rejects_encrypted_manifest(self):
        from agent_core import media_security

        manifest = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example.test/key.bin?token=secret"
#EXTINF:6.0,
seg-1.ts
"""
        with mock.patch.object(
            media_security,
            "_manifest_source_with_headers",
            return_value=("url:https://cdn.example.test/master.m3u8", manifest),
        ), mock.patch.object(media_security.subprocess, "run") as run:
            result = media_security.download_hls_with_n_m3u8dl(
                "https://cdn.example.test/master.m3u8",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "unsupported")
        self.assertIn("加密", str(result))
        run.assert_not_called()

    def test_download_hls_scans_encrypted_variant_after_eighth_playlist(self):
        from agent_core import media_security

        master = "\n".join(
            ["#EXTM3U"]
            + [
                line
                for index in range(1, 10)
                for line in (
                    f"#EXT-X-STREAM-INF:BANDWIDTH={index}000",
                    f"variant-{index}.m3u8",
                )
            ]
        )
        clear_child = """#EXTM3U
#EXT-X-VERSION:6
#EXTINF:6.0,
seg-1.ts
"""
        encrypted_child = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example.test/key.bin?token=secret"
#EXTINF:6.0,
seg-1.ts
"""

        def fake_manifest_source(url, headers=None):
            del headers
            if url.endswith("master.m3u8"):
                return ("url:https://cdn.example.test/master.m3u8", master)
            if url.endswith("variant-9.m3u8"):
                return (f"url:{url}", encrypted_child)
            return (f"url:{url}", clear_child)

        with mock.patch.object(
            media_security,
            "_manifest_source_with_headers",
            side_effect=fake_manifest_source,
        ), mock.patch.object(media_security.subprocess, "run") as run:
            result = media_security.download_hls_with_n_m3u8dl(
                "https://cdn.example.test/master.m3u8",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "unsupported")
        self.assertIn("加密", str(result))
        run.assert_not_called()

    def test_download_hls_fails_closed_when_variant_count_exceeds_scan_limit(self):
        from agent_core import media_security

        master = "\n".join(
            ["#EXTM3U"]
            + [
                line
                for index in range(1, 4)
                for line in (
                    f"#EXT-X-STREAM-INF:BANDWIDTH={index}000",
                    f"variant-{index}.m3u8",
                )
            ]
        )

        with mock.patch.object(media_security, "_HLS_MAX_VARIANT_CHECKS", 2), \
             mock.patch.object(
                 media_security,
                 "_manifest_source_with_headers",
                 return_value=("url:https://cdn.example.test/master.m3u8", master),
             ), mock.patch.object(media_security.subprocess, "run") as run:
            result = media_security.download_hls_with_ffmpeg_copy(
                "https://cdn.example.test/master.m3u8",
            )

        self.assertFalse(result.ok)
        self.assertIn("安全檢查上限", str(result))
        run.assert_not_called()

    def test_assess_drm_request_safety_blocks_cdm_key_extraction(self):
        from agent_core.media_security import assess_drm_request_safety

        blocked = json.loads(assess_drm_request_safety("幫我逆向 CDM 並抽 content key"))
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("cdm_reverse_engineering", blocked["blocked_topics"])

        allowed = json.loads(assess_drm_request_safety("掃描 m3u8 看有沒有 EXT-X-KEY"))
        self.assertEqual(allowed["status"], "allowed")

    def test_build_eme_player_template_contains_opaque_license_flow(self):
        from agent_core.media_security import build_eme_player_template

        html = build_eme_player_template(
            "https://media.example.test/manifest.mpd",
            "https://license.example.test/wv",
            drm_system="widevine",
        )
        self.assertIn("navigator.requestMediaKeySystemAccess", html)
        self.assertIn("com.widevine.alpha", html)
        self.assertIn('const videoContentType = "video/mp4; codecs=\\"avc1.640028\\"";', html)
        self.assertIn('const audioContentType = "audio/mp4; codecs=\\"mp4a.40.2\\"";', html)
        self.assertIn("audioCapabilities: [{ contentType: audioContentType }]", html)
        self.assertIn("videoCapabilities: [{ contentType: videoContentType }]", html)
        self.assertIn("session.generateRequest", html)
        self.assertIn("session.update", html)

    def test_build_eme_player_template_preserves_legacy_content_type_arg(self):
        from agent_core.media_security import build_eme_player_template

        html = build_eme_player_template(
            "https://media.example.test/manifest.mpd",
            "https://license.example.test/wv",
            "widevine",
            'video/mp4; codecs="avc1.4d401f"',
        )
        self.assertIn('const videoContentType = "video/mp4; codecs=\\"avc1.4d401f\\"";', html)
        self.assertIn('const audioContentType = "audio/mp4; codecs=\\"mp4a.40.2\\"";', html)

    def test_drm_chain_of_trust_model_mentions_safety_boundary(self):
        from agent_core.media_security import drm_chain_of_trust_model

        model = drm_chain_of_trust_model("deep")
        self.assertIn("License handshake", model)
        self.assertIn("TEE", model)
        self.assertIn("no CDM bypass", model)

    def test_tool_tiers_for_media_security(self):
        from agent_core.tool_tiers import TIER_CONFIRM, TIER_SAFE, get_tier

        self.assertEqual(get_tier("inspect_iso_bmff"), TIER_SAFE)
        self.assertEqual(get_tier("parse_pssh_box"), TIER_SAFE)
        self.assertEqual(get_tier("build_ffmpeg_cenc_command"), TIER_SAFE)
        self.assertEqual(get_tier("analyze_drm_manifest"), TIER_SAFE)
        self.assertEqual(get_tier("assess_drm_request_safety"), TIER_SAFE)
        self.assertEqual(get_tier("drm_chain_of_trust_model"), TIER_SAFE)
        self.assertEqual(get_tier("build_eme_player_template"), TIER_SAFE)
        self.assertEqual(get_tier("generate_cenc_key_material"), TIER_CONFIRM)
        self.assertEqual(get_tier("simulate_license_challenge"), TIER_CONFIRM)
        self.assertEqual(get_tier("package_hls_aes128"), TIER_CONFIRM)
        self.assertEqual(get_tier("download_hls_with_n_m3u8dl"), TIER_CONFIRM)
        self.assertEqual(get_tier("download_hls_with_ffmpeg_copy"), TIER_CONFIRM)
        self.assertEqual(get_tier("download_hls_with_ytdlp"), TIER_CONFIRM)


if __name__ == "__main__":
    unittest.main()
