import unittest
import re
from unittest import mock
from unittest.mock import AsyncMock
from pathlib import Path

from agent_core.youtube import (
    adjust_audio_pitch,
    download_youtube_audio,
    download_online_video,
    download_youtube_video,
)
from agent_core.shell_python_web import _SHELL_HARD_BLOCKS
from agent_core.youtube_audio_engine import (
    AudioMetadata,
    AudioProcessingResult,
    FileArtifact,
    FilenameSanitizer,
    MusicOutputFormat,
    PitchAdjustment,
    ProcessingMode,
    SegmentPlan,
    YouTubeAudioProcessor,
    YouTubeAudioProcessorConfig,
    parse_pitch_adjustment,
    process_audio,
)


class YouTubeAudioEnginePureTests(unittest.TestCase):
    def _processor(self, config=None):
        processor = object.__new__(YouTubeAudioProcessor)
        processor.config = config or YouTubeAudioProcessorConfig()
        processor.output_dir = Path("/tmp/youtube_audio_engine_tests")
        processor.music_format = MusicOutputFormat.M4A
        processor.ffmpeg_path = "ffmpeg"
        processor.ffprobe_path = "ffprobe"
        processor._rubberband_available_cache = None
        return processor

    def test_filename_sanitizer_removes_emoji_and_special_characters(self):
        cleaned = FilenameSanitizer.clean("Hello 🎧 / bad:title? * 測試")
        self.assertEqual(cleaned, "Hello_badtitle_測試")

    def test_long_video_planning_uses_twenty_minute_chunks(self):
        processor = self._processor()
        segments = processor._plan_segments(2501)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0], SegmentPlan(1, 0.0, 1200.0))
        self.assertEqual(segments[1], SegmentPlan(2, 1200.0, 1200.0))
        self.assertEqual(segments[2], SegmentPlan(3, 2400.0, 101.0))

    def test_music_extension_respects_flac_config(self):
        processor = self._processor(
            YouTubeAudioProcessorConfig(music_format=MusicOutputFormat.FLAC)
        )
        processor.music_format = MusicOutputFormat.FLAC
        self.assertEqual(processor._extension_for_mode(ProcessingMode.MUSIC), "flac")

    def test_resolve_binary_checks_current_python_venv_sibling(self):
        processor = self._processor()
        with mock.patch("agent_core.youtube_audio_engine.shutil.which", return_value=None), \
             mock.patch("agent_core.youtube_audio_engine.Path.exists", return_value=True), \
             mock.patch("agent_core.youtube_audio_engine.os.access", return_value=True):
            path = processor._resolve_binary("ffmpeg", None)
        self.assertTrue(path.endswith("ffmpeg"))

    def test_resolve_binary_accepts_configured_command_name_from_path(self):
        processor = self._processor()
        with mock.patch("agent_core.youtube_audio_engine.shutil.which", return_value="/opt/bin/ffmpeg"):
            path = processor._resolve_binary("ffmpeg", "ffmpeg")
        self.assertEqual(path, "/opt/bin/ffmpeg")

    def test_processing_result_serializes_to_public_contract(self):
        result = AudioProcessingResult(
            status="success",
            mode_applied="meeting",
            files=[FileArtifact(path="/tmp/segment_1.wav", duration=1200)],
            metadata=AudioMetadata(
                title="Cleaned_Title",
                uploader="Channel_Name",
                thumbnail_url="https://example.com/thumb.jpg",
            ),
        )
        payload = result.to_dict()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["mode_applied"], "meeting")
        self.assertEqual(payload["files"][0]["duration"], 1200)
        self.assertEqual(payload["compliance"], "Personal/Educational Use Only")

    def test_download_youtube_audio_returns_readable_file_paths(self):
        fake_result = {
            "status": "success",
            "mode_applied": "podcast",
            "files": [{"path": "/tmp/song.mp3", "duration": 123.456}],
            "metadata": {"title": "Cleaned_Title"},
            "compliance": "Personal/Educational Use Only",
        }
        with mock.patch(
            "agent_core.youtube_audio_engine.process_youtube_audio_sync",
            return_value=fake_result,
        ) as process:
            message = download_youtube_audio("https://youtu.be/abc123")

        self.assertIn("✅ 已下載並轉檔 YouTube 音訊：Cleaned_Title", message)
        self.assertIn("/tmp/song.mp3（123.5 秒）", message)
        process.assert_called_once()

    def test_download_youtube_audio_dependency_error_is_actionable(self):
        fake_result = {
            "status": "error",
            "error_code": "dependency_missing",
            "error_message": "ffmpeg is required but was not found on PATH.",
        }
        with mock.patch(
            "agent_core.youtube_audio_engine.process_youtube_audio_sync",
            return_value=fake_result,
        ):
            message = download_youtube_audio("https://youtu.be/abc123")

        self.assertIn("缺少執行環境依賴", message)
        self.assertIn("make install", message)

    def test_download_youtube_video_uses_media_engine_wrapper(self):
        fake_result = {
            "status": "success",
            "metadata": {"title": "Cleaned_Title", "uploader": "Channel", "duration": 60},
            "file_info": {"paths": ["/tmp/video.mp4"], "pitch_ratio": 1.0, "codec": "h264/aac"},
            "compliance_note": "Processed for personal/educational use.",
        }
        with mock.patch(
            "agent_core.youtube_media_engine.process_media_sync",
            return_value=fake_result,
        ) as process:
            message = download_youtube_video("https://youtu.be/abc123")

        self.assertIn("✅ 已下載 YouTube 影片：Cleaned_Title", message)
        self.assertIn("/tmp/video.mp4", message)
        self.assertTrue(getattr(message, "ok", False))
        self.assertEqual(getattr(message, "artifacts", []), ["/tmp/video.mp4"])
        process.assert_called_once()
        self.assertEqual(process.call_args.kwargs["task_type"], "video")

    def test_download_online_video_labels_facebook_sources(self):
        fake_result = {
            "status": "success",
            "metadata": {"title": "Cleaned_Title", "uploader": "Channel", "duration": 60},
            "file_info": {"paths": ["/tmp/reel.mp4"], "pitch_ratio": 1.0, "codec": "h264/aac"},
            "compliance_note": "Processed for personal/educational use.",
        }
        with mock.patch(
            "agent_core.youtube_media_engine.process_media_sync",
            return_value=fake_result,
        ):
            message = download_online_video("https://www.facebook.com/share/r/abc/")

        self.assertIn("✅ 已下載 Facebook 影片：Cleaned_Title", message)
        self.assertIn("/tmp/reel.mp4", message)

    def test_download_online_video_blocks_protected_streaming_services(self):
        with mock.patch("agent_core.youtube_media_engine.process_media_sync") as process:
            message = download_online_video("https://www.netflix.com/watch/82018723")

        self.assertIn("Netflix", message)
        self.assertIn("DRM", message)
        self.assertIn("不能協助下載完整影片", message)
        process.assert_not_called()

    def test_download_online_video_explains_facebook_parse_failures(self):
        fake_result = {
            "status": "error",
            "error_code": "download_failed",
            "error_message": "ERROR: [facebook] 123: Cannot parse data",
            "file_info": {"paths": []},
        }
        with mock.patch(
            "agent_core.youtube_media_engine.process_media_sync",
            return_value=fake_result,
        ):
            message = download_online_video("https://www.facebook.com/share/r/abc/")

        self.assertIn("Facebook 這次回傳的頁面 yt-dlp 解析不到影片資料", message)
        self.assertIn("facebook.com/reel/<數字>", message)

    def test_adjust_audio_pitch_uses_local_pitch_wrapper(self):
        fake_result = {
            "status": "success",
            "file_info": {"paths": ["/tmp/song_down_2.mp3"], "pitch_ratio": 0.8909, "codec": "mp3"},
            "processing_details": {"semitones": -2, "ratio": 0.8909},
            "compliance_note": "Processed for personal/educational use.",
        }
        with mock.patch(
            "agent_core.youtube_media_engine.process_local_audio_pitch_sync",
            return_value=fake_result,
        ) as process:
            message = adjust_audio_pitch("/tmp/song.mp3", pitch_adjust="Full Tone Down")

        self.assertIn("✅ 已完成音檔升降 key", message)
        self.assertIn("/tmp/song_down_2.mp3", message)
        process.assert_called_once()
        self.assertEqual(process.call_args.kwargs["pitch_adjust"], "Full Tone Down")

    def test_shell_format_guard_does_not_block_audio_format_option(self):
        command = 'yt-dlp -x --audio-format mp3 -o "~/Downloads/%(title)s.%(ext)s" URL'
        reasons = [
            reason
            for pattern, reason in _SHELL_HARD_BLOCKS
            if re.search(pattern, command, re.IGNORECASE)
        ]
        self.assertNotIn("格式化指令", reasons)

    def test_pitch_adjustment_terms_map_to_semitones_and_ratio(self):
        pitch = parse_pitch_adjustment("Full Tone Up")
        self.assertEqual(pitch.semitones, 2)
        self.assertAlmostEqual(pitch.ratio, 2 ** (2 / 12), places=12)
        self.assertEqual(parse_pitch_adjustment("Half Tone Down").semitones, -1)

    def test_pitch_filters_prefer_rubberband_when_available(self):
        processor = self._processor()
        filters = processor._pitch_filters(
            PitchAdjustment(semitones=2, ratio=2 ** (2 / 12)),
            source_sample_rate=44100,
            use_rubberband=True,
            target_duration_seconds=120,
        )
        self.assertTrue(filters[0].startswith("rubberband=pitch="))
        self.assertIn("tempo=1.0", filters[0])
        self.assertIn("atrim=duration=120.000000", filters)

    def test_pitch_filters_fallback_uses_asetrate_aresample_and_atempo(self):
        processor = self._processor()
        filters = processor._pitch_filters(
            PitchAdjustment(semitones=-2, ratio=2 ** (-2 / 12)),
            source_sample_rate=48000,
            use_rubberband=False,
            target_duration_seconds=60,
        )
        self.assertTrue(filters[0].startswith("asetrate=48000*"))
        self.assertIn("aresample=48000:filter_size=64:cutoff=0.97", filters)
        self.assertTrue(any(item.startswith("atempo=") for item in filters))

    def test_process_audio_returns_pro_contract(self):
        fake_result = AudioProcessingResult(
            status="success",
            mode_applied="music",
            files=[FileArtifact(path="/tmp/chunk_1.m4a", duration=1200)],
            metadata=AudioMetadata(
                title="Cleaned_Title",
                uploader="Channel_Name",
                duration=1200,
                thumbnail_url="https://example.com/thumb.jpg",
            ),
        )
        with mock.patch.object(
            YouTubeAudioProcessor,
            "process_url",
            new=AsyncMock(return_value=fake_result),
        ), mock.patch.object(YouTubeAudioProcessor, "_resolve_binary", side_effect=lambda name, _: name):
            payload = __import__("asyncio").run(
                process_audio(
                    "https://youtu.be/abc123",
                    mode="music",
                    pitch_adjust="Full Tone Up",
                    output_dir="/tmp",
                )
            )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["metadata"]["duration"], 1200)
        self.assertEqual(payload["processing_details"]["mode"], "music")
        self.assertEqual(payload["processing_details"]["semitones"], 2)
        self.assertEqual(payload["processing_details"]["chunks"], ["/tmp/chunk_1.m4a"])


if __name__ == "__main__":
    unittest.main()
