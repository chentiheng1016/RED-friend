from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class HlsAesPackagerTests(unittest.TestCase):
    def test_generate_content_key_writes_16_byte_key_and_key_info(self):
        from scripts.hls_aes_packager import generate_content_key

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            key_file, key_info, iv_hex = generate_content_key(
                out,
                "https://license.example.test/hls/key/asset-1",
                "asset-1",
            )

            self.assertEqual(key_file.read_bytes().__len__(), 16)
            lines = key_info.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "https://license.example.test/hls/key/asset-1")
            self.assertEqual(lines[1], str(key_file.resolve()))
            self.assertEqual(lines[2], iv_hex)
            self.assertEqual(len(iv_hex), 32)

    def test_build_command_contains_encryption_and_variant_map(self):
        from scripts.hls_aes_packager import DEFAULT_LADDER, build_ffmpeg_command

        command = build_ffmpeg_command(
            input_path=Path("/tmp/input.mov"),
            output_dir=Path("/tmp/hls"),
            key_info=Path("/tmp/hls/keys/asset.keyinfo"),
            ladder=DEFAULT_LADDER[:2],
            has_audio=True,
            segment_seconds=6,
            ffmpeg="ffmpeg",
        )
        joined = " ".join(command)

        self.assertIn("-hls_key_info_file /tmp/hls/keys/asset.keyinfo", joined)
        self.assertIn("-master_pl_name master.m3u8", joined)
        self.assertIn("-var_stream_map v:0,a:0,name:1080p v:1,a:1,name:720p", joined)
        self.assertIn("-hls_flags independent_segments", joined)


if __name__ == "__main__":
    unittest.main()
