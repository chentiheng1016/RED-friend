"""Tests for the video frame-sampling ingest path.

兩層：video_frames（純 ffmpeg 工具，mock subprocess）與
drive_sync._extract_video_via_frames / _extract_media 的路由
（抽幀優先、失敗 fallback 整檔 inline、預算斷路器照舊）。
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class SampleFramesTests(unittest.TestCase):
    def test_no_ffmpeg_returns_empty(self):
        from agent_core.ingest import video_frames as vf
        with mock.patch.object(vf, "ffmpeg_available", return_value=False):
            self.assertEqual(vf.sample_frames("/tmp/x.mp4"), [])

    def test_even_spacing_and_jpeg_bytes(self):
        """4 幀取樣 100s 影片 → 取樣點 12.5/37.5/62.5/87.5（避開頭尾）。"""
        from agent_core.ingest import video_frames as vf

        seen_ts: list[str] = []

        def fake_run(cmd, **kwargs):
            # ffmpeg 寫出檔案；-ss 在 -i 前（input seeking 才快）
            i_ss = cmd.index("-ss")
            self.assertLess(i_ss, cmd.index("-i"))
            seen_ts.append(cmd[i_ss + 1])
            out_path = cmd[-1]
            with open(out_path, "wb") as f:
                f.write(b"JPEGDATA")
            return mock.MagicMock(returncode=0)

        with mock.patch.object(vf, "ffmpeg_available", return_value=True), \
             mock.patch.object(vf, "probe_duration_s", return_value=100.0), \
             mock.patch.object(vf.subprocess, "run", side_effect=fake_run):
            frames = vf.sample_frames("/tmp/x.mp4", max_frames=4)

        self.assertEqual(seen_ts, ["12.50", "37.50", "62.50", "87.50"])
        self.assertEqual([ts for ts, _ in frames], [12.5, 37.5, 62.5, 87.5])
        self.assertTrue(all(jpg == b"JPEGDATA" for _, jpg in frames))

    def test_partial_frame_failure_keeps_the_rest(self):
        from agent_core.ingest import video_frames as vf

        calls = {"n": 0}

        def flaky_run(cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("seek failed")
            with open(cmd[-1], "wb") as f:
                f.write(b"OK")
            return mock.MagicMock(returncode=0)

        with mock.patch.object(vf, "ffmpeg_available", return_value=True), \
             mock.patch.object(vf, "probe_duration_s", return_value=60.0), \
             mock.patch.object(vf.subprocess, "run", side_effect=flaky_run):
            frames = vf.sample_frames("/tmp/x.mp4", max_frames=3)
        self.assertEqual(len(frames), 2)

    def test_zero_duration_returns_empty(self):
        from agent_core.ingest import video_frames as vf
        with mock.patch.object(vf, "ffmpeg_available", return_value=True), \
             mock.patch.object(vf, "probe_duration_s", return_value=0.0):
            self.assertEqual(vf.sample_frames("/tmp/x.mp4"), [])

    def test_audio_track_failure_returns_empty_bytes(self):
        from agent_core.ingest import video_frames as vf
        with mock.patch.object(vf, "ffmpeg_available", return_value=True), \
             mock.patch.object(vf.subprocess, "run",
                               side_effect=RuntimeError("no audio stream")):
            self.assertEqual(vf.extract_audio_track("/tmp/x.mp4"), b"")

    def test_format_ts(self):
        from agent_core.ingest import video_frames as vf
        self.assertEqual(vf.format_ts(93.4), "01:33")
        self.assertEqual(vf.format_ts(0), "00:00")


class ExtractVideoViaFramesTests(unittest.TestCase):
    """drive_sync 端：Gemini 呼叫留在 drive_sync（cost caller 歸戶 → 預算
    斷路器才管得到）；畫面描述 + 音軌轉錄合併；quota 錯誤照舊上拋。"""

    @staticmethod
    def _resp(text):
        return types.SimpleNamespace(text=text)

    def _run(self, *, frames, audio, gemini_side_effect):
        from agent_core.ingest import drive_sync, video_frames as vf
        gem = mock.MagicMock(side_effect=gemini_side_effect)
        with mock.patch.object(vf, "ffmpeg_available", return_value=True), \
             mock.patch.object(vf, "sample_frames", return_value=frames), \
             mock.patch.object(vf, "extract_audio_track", return_value=audio), \
             mock.patch("agent_core.gemini_client._gemini_generate", gem):
            out = drive_sync._extract_video_via_frames(b"VID", "video/mp4")
        return out, gem

    def test_visual_plus_audio_sections(self):
        out, gem = self._run(
            frames=[(1.0, b"F1"), (2.0, b"F2")],
            audio=b"MP3",
            gemini_side_effect=[self._resp("產線正在針車作業"), self._resp("主管說明交期")],
        )
        self.assertIn("【影片畫面描述（2 幀取樣）】", out)
        self.assertIn("產線正在針車作業", out)
        self.assertIn("【語音轉錄】", out)
        self.assertIn("主管說明交期", out)
        self.assertEqual(gem.call_count, 2)

    def test_no_audio_keeps_visual_only(self):
        out, gem = self._run(
            frames=[(1.0, b"F1")],
            audio=b"",
            gemini_side_effect=[self._resp("會議畫面")],
        )
        self.assertIn("【影片畫面描述（1 幀取樣）】", out)
        self.assertNotIn("【語音轉錄】", out)
        self.assertEqual(gem.call_count, 1)

    def test_silent_audio_marker_not_included(self):
        out, _ = self._run(
            frames=[(1.0, b"F1")],
            audio=b"MP3",
            gemini_side_effect=[self._resp("畫面"), self._resp("（無語音）")],
        )
        self.assertNotIn("【語音轉錄】", out)

    def test_no_frames_returns_empty_for_fallback(self):
        from agent_core.ingest import drive_sync, video_frames as vf
        with mock.patch.object(vf, "ffmpeg_available", return_value=True), \
             mock.patch.object(vf, "sample_frames", return_value=[]):
            self.assertEqual(drive_sync._extract_video_via_frames(b"VID", "video/mp4"), "")

    def test_no_ffmpeg_returns_empty_for_fallback(self):
        from agent_core.ingest import drive_sync, video_frames as vf
        with mock.patch.object(vf, "ffmpeg_available", return_value=False):
            self.assertEqual(drive_sync._extract_video_via_frames(b"VID", "video/mp4"), "")


class ExtractMediaRoutingTests(unittest.TestCase):
    """_extract_media 的路由：影片優先抽幀；回空才 fallback 整檔 inline；
    音訊檔不走抽幀。"""

    def test_video_uses_frames_path_first(self):
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_VIDEO_FRAMES", True), \
             mock.patch.object(drive_sync, "_get_media_stop_message", return_value=None), \
             mock.patch.object(drive_sync, "get_embedding_hard_quota_message", return_value=None), \
             mock.patch.object(drive_sync, "_extract_video_via_frames", return_value="幀描述") as fr:
            out = drive_sync._extract_media(b"VID", "video/mp4")
        self.assertEqual(out, "幀描述")
        fr.assert_called_once_with(b"VID", "video/mp4")

    def test_video_falls_back_to_inline_when_frames_unavailable(self):
        from agent_core.ingest import drive_sync
        resp = types.SimpleNamespace(text="inline transcript")
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_VIDEO_FRAMES", True), \
             mock.patch.object(drive_sync, "_get_media_stop_message", return_value=None), \
             mock.patch.object(drive_sync, "get_embedding_hard_quota_message", return_value=None), \
             mock.patch.object(drive_sync, "_extract_video_via_frames", return_value=""), \
             mock.patch("agent_core.gemini_client._gemini_generate", return_value=resp):
            out = drive_sync._extract_media(b"VID", "video/mp4")
        self.assertEqual(out, "inline transcript")

    def test_flag_off_skips_frames_path(self):
        from agent_core.ingest import drive_sync
        resp = types.SimpleNamespace(text="inline transcript")
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_VIDEO_FRAMES", False), \
             mock.patch.object(drive_sync, "_get_media_stop_message", return_value=None), \
             mock.patch.object(drive_sync, "get_embedding_hard_quota_message", return_value=None), \
             mock.patch.object(drive_sync, "_extract_video_via_frames") as fr, \
             mock.patch("agent_core.gemini_client._gemini_generate", return_value=resp):
            out = drive_sync._extract_media(b"VID", "video/mp4")
        self.assertEqual(out, "inline transcript")
        fr.assert_not_called()

    def test_audio_mime_never_touches_frames_path(self):
        from agent_core.ingest import drive_sync
        resp = types.SimpleNamespace(text="逐字稿")
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_VIDEO_FRAMES", True), \
             mock.patch.object(drive_sync, "_get_media_stop_message", return_value=None), \
             mock.patch.object(drive_sync, "get_embedding_hard_quota_message", return_value=None), \
             mock.patch.object(drive_sync, "_extract_video_via_frames") as fr, \
             mock.patch("agent_core.gemini_client._gemini_generate", return_value=resp):
            out = drive_sync._extract_media(b"AUD", "audio/mpeg")
        self.assertEqual(out, "逐字稿")
        fr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
