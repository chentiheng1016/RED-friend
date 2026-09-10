import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_core.youtube_media_engine import (
    AudioMode,
    AsyncSubprocessRunner,
    DependencyMissingError,
    DownloadError,
    FileInfo,
    MediaEngineConfig,
    MediaMetadata,
    MediaProcessingResult,
    MusicOutputFormat,
    PitchAdjustment,
    PitchShifter,
    SegmentPlan,
    VIDEO_FORMAT_TEMPLATE,
    YouTubeMediaProcessor,
    process_local_audio_pitch,
    process_media,
)


class YouTubeMediaEnginePureTests(unittest.TestCase):
    def _processor(self):
        processor = object.__new__(YouTubeMediaProcessor)
        processor.config = MediaEngineConfig(output_dir="/tmp/youtube_media_tests")
        processor.output_dir = Path("/tmp/youtube_media_tests")
        processor.ffmpeg_path = "ffmpeg"
        processor.ffprobe_path = "ffprobe"
        processor.yt_dlp_path = "yt-dlp"
        processor.runner = None
        processor._rubberband_available = None
        processor.music_format = MusicOutputFormat.M4A
        return processor

    def test_video_format_template_matches_required_1080p_selector(self):
        self.assertEqual(
            VIDEO_FORMAT_TEMPLATE.format(height=1080),
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best",
        )

    def test_pitch_terms_and_ratio_follow_musical_standard(self):
        self.assertEqual(PitchShifter.parse("Full Tone Up").semitones, 2)
        self.assertEqual(PitchShifter.parse("Full Tone Down").semitones, -2)
        self.assertEqual(PitchShifter.parse("Half Tone Up").semitones, 1)
        pitch = PitchShifter.parse("Half Tone Down")
        self.assertEqual(pitch.semitones, -1)
        self.assertAlmostEqual(pitch.ratio, 2 ** (-1 / 12), places=12)

    def test_fallback_pitch_filters_include_compensatory_atempo(self):
        filters = PitchShifter.filters(
            PitchAdjustment(semitones=2, ratio=2 ** (2 / 12)),
            source_sample_rate=48000,
            use_rubberband=False,
            target_duration_seconds=120,
        )
        self.assertTrue(filters[0].startswith("asetrate=48000*"))
        self.assertIn("aresample=48000:filter_size=64:cutoff=0.97", filters)
        self.assertTrue(any(item.startswith("atempo=0.890898") for item in filters))
        self.assertIn("atrim=duration=120.000000", filters)

    def test_video_mux_without_pitch_uses_stream_copy(self):
        processor = self._processor()
        command = processor._video_mux_command(
            video_source=Path("/tmp/video.mp4"),
            audio_source=Path("/tmp/audio.m4a"),
            thumbnail=None,
            output=Path("/tmp/out.mp4"),
            metadata=MediaMetadata(title="Title", uploader="Channel", duration=60),
            filters=[],
        )
        self.assertIn("-c:v:0", command)
        self.assertIn("copy", command)
        self.assertIn("-c:a", command)
        audio_codec_index = command.index("-c:a") + 1
        self.assertEqual(command[audio_codec_index], "copy")

    def test_process_video_uses_required_ytdlp_video_selector(self):
        processor = self._processor()
        processor._parse_resolution = mock.Mock(return_value=1080)
        processor._download_one = mock.AsyncMock(
            side_effect=[Path("/tmp/video.mp4"), Path("/tmp/audio.m4a")]
        )
        processor._has_audio_stream = mock.AsyncMock(return_value=True)
        processor._download_thumbnail = mock.AsyncMock(return_value=None)
        processor._probe_sample_rate = mock.AsyncMock(return_value=48000)
        processor._should_use_rubberband = mock.AsyncMock(return_value=False)
        processor._video_mux_command = mock.Mock(return_value=["ffmpeg"])
        processor._finalize_segments = mock.AsyncMock(return_value=[Path("/tmp/out.mp4")])

        class FakeRunner:
            async def run(self, command, name):
                return "", ""

        processor.runner = FakeRunner()
        asyncio.run(
            processor._process_video(
                url="https://youtu.be/abc123",
                pitch=PitchAdjustment(semitones=0, ratio=1.0),
                metadata=MediaMetadata(title="Title", uploader="Channel", duration=60),
                temp_dir=Path("/tmp"),
                resolution="1080p",
            )
        )

        self.assertEqual(
            processor._download_one.call_args_list[0].args[1],
            VIDEO_FORMAT_TEMPLATE.format(height=1080),
        )

    def test_process_video_without_pitch_does_not_probe_sample_rate(self):
        processor = self._processor()
        processor._parse_resolution = mock.Mock(return_value=1080)
        processor._download_one = mock.AsyncMock(return_value=Path("/tmp/video.mp4"))
        processor._has_audio_stream = mock.AsyncMock(return_value=True)
        processor._download_thumbnail = mock.AsyncMock(return_value=None)
        processor._probe_sample_rate = mock.AsyncMock(return_value=48000)
        processor._should_use_rubberband = mock.AsyncMock(return_value=False)
        processor._video_mux_command = mock.Mock(return_value=["ffmpeg"])
        processor._finalize_segments = mock.AsyncMock(return_value=[Path("/tmp/out.mp4")])

        class FakeRunner:
            async def run(self, command, name):
                return "", ""

        processor.runner = FakeRunner()
        asyncio.run(
            processor._process_video(
                url="https://youtu.be/abc123",
                pitch=PitchAdjustment(semitones=0, ratio=1.0),
                metadata=MediaMetadata(title="Title", uploader="Channel", duration=60),
                temp_dir=Path("/tmp"),
                resolution="1080p",
            )
        )

        processor._probe_sample_rate.assert_not_awaited()
        processor._should_use_rubberband.assert_not_awaited()
        self.assertEqual(processor._video_mux_command.call_args.kwargs["filters"], [])

    def test_process_audio_non_music_without_pitch_skips_thumbnail_and_sample_rate(self):
        processor = self._processor()
        processor._download_one = mock.AsyncMock(return_value=Path("/tmp/audio.m4a"))
        processor._download_thumbnail = mock.AsyncMock(return_value=Path("/tmp/thumb.jpg"))
        processor._probe_sample_rate = mock.AsyncMock(return_value=48000)
        processor._should_use_rubberband = mock.AsyncMock(return_value=False)
        processor._audio_ffmpeg_command = mock.Mock(return_value=["ffmpeg"])

        class FakeRunner:
            async def run(self, command, name):
                return "", ""

        processor.runner = FakeRunner()
        outputs, codec = asyncio.run(
            processor._process_audio(
                url="https://youtu.be/abc123",
                mode=AudioMode.PODCAST,
                pitch=PitchAdjustment(semitones=0, ratio=1.0),
                metadata=MediaMetadata(title="Title", uploader="Channel", duration=60),
                temp_dir=Path("/tmp"),
            )
        )

        self.assertEqual(codec, "mp3")
        self.assertEqual(len(outputs), 1)
        processor._download_thumbnail.assert_not_awaited()
        processor._probe_sample_rate.assert_not_awaited()
        processor._should_use_rubberband.assert_not_awaited()

    def test_process_media_dependency_missing_returns_structured_error(self):
        with mock.patch.object(
            YouTubeMediaProcessor,
            "__init__",
            side_effect=DependencyMissingError("yt-dlp is missing"),
        ):
            payload = asyncio.run(
                process_media(
                    "https://youtu.be/abc123",
                    task_type="video",
                    output_dir="/tmp",
                )
            )

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "dependency_missing")
        self.assertEqual(payload["task_type"], "video")

    def test_process_media_invalid_music_format_is_structured_configuration_error(self):
        with mock.patch.object(YouTubeMediaProcessor, "_resolve_binary", side_effect=lambda name, _: name):
            payload = asyncio.run(
                process_media(
                    "https://youtu.be/abc123",
                    task_type="audio",
                    mode="music",
                    output_dir="/tmp",
                    music_format="ogg",
                )
            )

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "invalid_configuration")
        self.assertIn("music_format", payload["error_message"])

    def test_ytdlp_timeout_maps_to_download_error(self):
        runner = AsyncSubprocessRunner(timeout_seconds=0.01)
        with self.assertRaises(DownloadError):
            asyncio.run(
                runner.run(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    "yt-dlp",
                )
            )

    def test_segments_split_after_twenty_minutes(self):
        processor = self._processor()
        segments = processor._segments(2501)
        self.assertEqual(segments[0], SegmentPlan(1, 0.0, 1200.0))
        self.assertEqual(segments[1], SegmentPlan(2, 1200.0, 1200.0))
        self.assertEqual(segments[2], SegmentPlan(3, 2400.0, 101.0))

    def test_resolve_binary_checks_current_python_venv_sibling(self):
        processor = self._processor()
        with mock.patch("agent_core.youtube_media_engine.shutil.which", return_value=None), \
             mock.patch("agent_core.youtube_media_engine.Path.exists", return_value=True), \
             mock.patch("agent_core.youtube_media_engine.os.access", return_value=True):
            path = processor._resolve_binary("yt-dlp", None)
        self.assertTrue(path.endswith(os.path.join("bin", "yt-dlp")) or path.endswith("yt-dlp"))

    def test_resolve_binary_accepts_configured_command_name_from_path(self):
        processor = self._processor()
        with mock.patch("agent_core.youtube_media_engine.shutil.which", return_value="/usr/local/bin/ffmpeg"):
            path = processor._resolve_binary("ffmpeg", "ffmpeg")
        self.assertEqual(path, "/usr/local/bin/ffmpeg")

    def test_process_media_returns_required_schema(self):
        fake_result = MediaProcessingResult(
            status="success",
            task_type="audio",
            metadata=MediaMetadata(title="Cleaned_Title", uploader="Channel_Name", duration=3600),
            file_info=FileInfo(
                paths=["/tmp/segment_1.mp3", "/tmp/segment_2.mp3"],
                pitch_ratio=1.05946,
                codec="mp3",
            ),
        )

        async def fake_process(self, **kwargs):
            return fake_result

        with mock.patch.object(YouTubeMediaProcessor, "__init__", return_value=None), \
             mock.patch.object(YouTubeMediaProcessor, "process_media", fake_process):
            payload = asyncio.run(
                process_media(
                    "https://youtu.be/abc123",
                    task_type="audio",
                    mode=AudioMode.PODCAST,
                    pitch_adjust=1,
                    output_dir="/tmp",
                )
            )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["task_type"], "audio")
        self.assertEqual(payload["metadata"]["title"], "Cleaned_Title")
        self.assertEqual(payload["file_info"]["paths"][1], "/tmp/segment_2.mp3")
        self.assertEqual(payload["compliance_note"], "Processed for personal/educational use.")

    def test_process_local_audio_pitch_uses_exec_runner_and_fallback_atempo(self):
        commands = []

        class FakeRunner:
            def __init__(self, timeout_seconds=None):
                self.timeout_seconds = timeout_seconds

            async def run(self, command, name):
                commands.append((name, command))
                return "", ""

        async def fake_sample_rate(runner, ffprobe_path, source):
            return 48000

        async def fake_duration(runner, ffprobe_path, source):
            return 2.0

        async def fake_rubberband(runner, ffmpeg_path):
            return False

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "song.mp3"
            source.write_bytes(b"fake")
            with mock.patch("agent_core.youtube_media_engine._resolve_binary", side_effect=lambda name, _: name), \
                 mock.patch("agent_core.youtube_media_engine.AsyncSubprocessRunner", FakeRunner), \
                 mock.patch("agent_core.youtube_media_engine._probe_audio_sample_rate", fake_sample_rate), \
                 mock.patch("agent_core.youtube_media_engine._probe_audio_duration", fake_duration), \
                 mock.patch("agent_core.youtube_media_engine._ffmpeg_has_rubberband", fake_rubberband):
                payload = asyncio.run(
                    process_local_audio_pitch(
                        source,
                        pitch_adjust="Full Tone Down",
                        output_dir=tmp,
                        output_format="mp3",
                    )
                )

        self.assertEqual(payload["status"], "success")
        ffmpeg_command = commands[-1][1]
        self.assertEqual(ffmpeg_command[0], "ffmpeg")
        self.assertIn("-map", ffmpeg_command)
        self.assertIn("0:a:0", ffmpeg_command)
        filters = ffmpeg_command[ffmpeg_command.index("-af") + 1]
        self.assertIn("asetrate=48000*", filters)
        self.assertIn("atempo=1.122462", filters)

    def test_process_local_audio_pitch_without_pitch_skips_sample_rate_probe(self):
        commands = []

        class FakeRunner:
            def __init__(self, timeout_seconds=None):
                self.timeout_seconds = timeout_seconds

            async def run(self, command, name):
                commands.append((name, command))
                return "", ""

        async def fail_sample_rate(runner, ffprobe_path, source):
            raise AssertionError("sample rate should not be probed when pitch is zero")

        async def fake_duration(runner, ffprobe_path, source):
            return 2.0

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "song.mp3"
            source.write_bytes(b"fake")
            with mock.patch("agent_core.youtube_media_engine._resolve_binary", side_effect=lambda name, _: name), \
                 mock.patch("agent_core.youtube_media_engine.AsyncSubprocessRunner", FakeRunner), \
                 mock.patch("agent_core.youtube_media_engine._probe_audio_sample_rate", fail_sample_rate), \
                 mock.patch("agent_core.youtube_media_engine._probe_audio_duration", fake_duration):
                payload = asyncio.run(
                    process_local_audio_pitch(
                        source,
                        pitch_adjust=0,
                        output_dir=tmp,
                        output_format="mp3",
                    )
                )

        self.assertEqual(payload["status"], "success")
        ffmpeg_command = commands[-1][1]
        self.assertNotIn("-af", ffmpeg_command)


if __name__ == "__main__":
    unittest.main()
