"""video_understanding 測試 — 教學影片「旁白語意×畫面動作」融合管線。

全部 mock Gemini client，不打真 API。重點：
  - 小檔 inline / 大檔 Files API 的分流與暫存檔清理
  - caller 成本歸戶標籤一路透傳（drive_sync 預算斷路器靠字串對帳）
  - drive_sync._extract_media 影片走融合 prompt、音檔走轉錄 prompt
  - watch_teaching_video 的來源解析（本機 / Drive 連結）與 untrusted 包裹
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


def _resp(text="分析結果"):
    return SimpleNamespace(text=text, usage_metadata=None)


def _active_upload(name="files/abc123"):
    return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))


class GenerateFromMediaTests(unittest.TestCase):
    def test_requires_exactly_one_source(self):
        from agent_core import video_understanding as vu

        with self.assertRaises(ValueError):
            vu.generate_from_media(mime_type="video/mp4", prompt="p")
        with self.assertRaises(ValueError):
            vu.generate_from_media(
                mime_type="video/mp4", prompt="p", data=b"x", path="/tmp/x.mp4"
            )

    def test_small_payload_stays_inline_with_caller(self):
        from agent_core import video_understanding as vu

        client = mock.MagicMock()
        with mock.patch(
            "agent_core.gemini_client._gemini_generate", return_value=_resp("inline ok")
        ) as gen, mock.patch(
            "agent_core.gemini_client._get_gemini_client", return_value=client
        ):
            out = vu.generate_from_media(
                mime_type="video/mp4", prompt="p", data=b"tiny", caller="a.b"
            )
        self.assertEqual(out, "inline ok")
        client.files.upload.assert_not_called()
        self.assertEqual(gen.call_args.kwargs.get("caller"), "a.b")

    def test_large_payload_goes_files_api_and_cleans_up(self):
        from agent_core import video_understanding as vu

        uploaded = _active_upload()
        client = mock.MagicMock()
        seen = {}

        def capture_upload(file, config=None):
            seen["path"] = file
            seen["config"] = config
            seen["existed_at_upload"] = os.path.exists(file)
            return uploaded

        client.files.upload.side_effect = capture_upload
        with mock.patch.object(vu, "_INLINE_MEDIA_MAX_BYTES", 4), mock.patch(
            "agent_core.gemini_client._gemini_generate", return_value=_resp("files ok")
        ) as gen, mock.patch(
            "agent_core.gemini_client._get_gemini_client", return_value=client
        ):
            out = vu.generate_from_media(
                mime_type="video/mp4", prompt="教學prompt", data=b"x" * 10, caller="c.d"
            )

        self.assertEqual(out, "files ok")
        self.assertTrue(seen["existed_at_upload"])
        self.assertFalse(os.path.exists(seen["path"]), "暫存檔用完必須刪掉")
        self.assertTrue(seen["path"].endswith(".mp4"))
        self.assertEqual(seen["config"]["mime_type"], "video/mp4")
        # 回歸（2026-06-14 rag_sync wedge 18h）：Files API 上傳必須帶 per-call
        # HTTP timeout，否則大檔上傳卡 SSL read 會無限期掛住。
        self.assertGreater(seen["config"]["http_options"]["timeout"], 0)
        client.files.delete.assert_called_once_with(name="files/abc123")
        contents = gen.call_args.kwargs["contents"]
        self.assertIs(contents[0], uploaded)
        self.assertEqual(contents[1], "教學prompt")
        self.assertEqual(gen.call_args.kwargs.get("caller"), "c.d")

    def test_uploaded_file_deleted_even_when_generate_fails(self):
        from agent_core import video_understanding as vu

        client = mock.MagicMock()
        client.files.upload.return_value = _active_upload("files/doomed")
        with mock.patch.object(vu, "_INLINE_MEDIA_MAX_BYTES", 4), mock.patch(
            "agent_core.gemini_client._gemini_generate",
            side_effect=RuntimeError("boom"),
        ), mock.patch(
            "agent_core.gemini_client._get_gemini_client", return_value=client
        ):
            with self.assertRaises(RuntimeError):
                vu.generate_from_media(
                    mime_type="video/mp4", prompt="p", data=b"x" * 10
                )
        client.files.delete.assert_called_once_with(name="files/doomed")

    def test_path_input_uses_files_api_and_keeps_callers_file(self):
        from agent_core import video_understanding as vu

        client = mock.MagicMock()
        client.files.upload.return_value = _active_upload()
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"VIDEO")
            path = f.name
        try:
            with mock.patch(
                "agent_core.gemini_client._gemini_generate", return_value=_resp("ok")
            ), mock.patch(
                "agent_core.gemini_client._get_gemini_client", return_value=client
            ):
                out = vu.generate_from_media(
                    mime_type="video/mp4", prompt="p", path=path
                )
            self.assertEqual(out, "ok")
            self.assertEqual(client.files.upload.call_args.kwargs["file"], path)
            self.assertTrue(os.path.exists(path), "呼叫端自己的檔案不能被刪")
        finally:
            os.unlink(path)


class PromptTests(unittest.TestCase):
    def test_video_ingest_prompt_fuses_narration_and_actions(self):
        from agent_core import video_understanding as vu

        prompt = vu.drive_ingest_prompt_for("video/mp4")
        self.assertIn("旁白", prompt)
        self.assertIn("畫面", prompt)
        self.assertIn("[mm:ss]", prompt)
        self.assertIn("流程總結", prompt)

    def test_audio_ingest_prompt_transcribes(self):
        from agent_core import video_understanding as vu

        prompt = vu.drive_ingest_prompt_for("audio/mpeg")
        self.assertIn("Transcribe", prompt)
        self.assertNotIn("畫面", prompt)

    def test_teaching_prompt_structure(self):
        from agent_core import video_understanding as vu

        prompt = vu.teaching_video_prompt()
        for marker in ("旁白講解", "畫面動作", "整合說明", "連貫流程總結", "時間戳"):
            self.assertIn(marker, prompt)

    def test_teaching_prompt_with_focus(self):
        from agent_core import video_understanding as vu

        prompt = vu.teaching_video_prompt("針車怎麼穿線")
        self.assertIn("針車怎麼穿線", prompt)
        self.assertIn("針對提問的回答", prompt)


class ResolveDriveFileIdTests(unittest.TestCase):
    def test_file_url(self):
        from agent_core import video_understanding as vu

        url = "https://drive.google.com/file/d/1AbC_d-90efGHIJklmnopQ/view?usp=sharing"
        self.assertEqual(vu._resolve_drive_file_id(url), "1AbC_d-90efGHIJklmnopQ")

    def test_open_id_url(self):
        from agent_core import video_understanding as vu

        url = "https://drive.google.com/open?id=1AbC_d-90efGHIJklmnopQ"
        self.assertEqual(vu._resolve_drive_file_id(url), "1AbC_d-90efGHIJklmnopQ")

    def test_bare_id(self):
        from agent_core import video_understanding as vu

        self.assertEqual(
            vu._resolve_drive_file_id("1AbC_d-90efGHIJklmnopQ"),
            "1AbC_d-90efGHIJklmnopQ",
        )

    def test_garbage_returns_empty(self):
        from agent_core import video_understanding as vu

        self.assertEqual(vu._resolve_drive_file_id("not a link"), "")
        self.assertEqual(vu._resolve_drive_file_id(""), "")


class WatchTeachingVideoTests(unittest.TestCase):
    def test_empty_source(self):
        from agent_core import video_understanding as vu

        self.assertIn("錯誤", vu.watch_teaching_video(""))

    def test_local_video_file_wraps_untrusted_and_passes_focus(self):
        from agent_core import video_understanding as vu

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"VIDEO")
            path = f.name
        try:
            # 注意：輸出會過 sanitize_for_llm（NFKC 會折全形標點），
            # 斷言內容避免用全形冒號。
            with mock.patch.object(
                vu, "generate_from_media", return_value="步驟一 穿線"
            ) as gen:
                out = vu.watch_teaching_video(path, focus="針車怎麼穿線")
            self.assertIn("<video_analysis>", out)
            self.assertIn("步驟一 穿線", out)
            self.assertIn(os.path.basename(path), out)
            kw = gen.call_args.kwargs
            # path-safety 會 resolve symlink（macOS /var → /private/var）
            self.assertEqual(kw["path"], os.path.realpath(path))
            self.assertEqual(kw["mime_type"], "video/mp4")
            self.assertIn("針車怎麼穿線", kw["prompt"])
            self.assertEqual(
                kw["caller"], "video_understanding.watch_teaching_video"
            )
        finally:
            os.unlink(path)

    def test_local_non_video_rejected(self):
        from agent_core import video_understanding as vu

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"text")
            path = f.name
        try:
            out = vu.watch_teaching_video(path)
            self.assertIn("錯誤", out)
        finally:
            os.unlink(path)

    def test_drive_url_downloads_to_temp_and_cleans_up(self):
        from agent_core import video_understanding as vu

        meta = {
            "id": "1AbC_d-90efGHIJklmnopQ",
            "name": "針車教學.mp4",
            "size": "123456",
            "mimeType": "video/mp4",
        }
        seen = {}

        def fake_download(file_id, dest_path):
            seen["file_id"] = file_id
            seen["dest"] = dest_path
            with open(dest_path, "wb") as f:
                f.write(b"VIDEO")
            return True

        with mock.patch.object(vu, "_drive_file_meta", return_value=meta), mock.patch(
            "agent_core.erp._download_drive_file", side_effect=fake_download
        ), mock.patch.object(
            vu, "generate_from_media", return_value="教學整理內容"
        ) as gen:
            out = vu.watch_teaching_video(
                "https://drive.google.com/file/d/1AbC_d-90efGHIJklmnopQ/view"
            )

        self.assertIn("針車教學.mp4", out)
        self.assertIn("教學整理內容", out)
        self.assertEqual(seen["file_id"], "1AbC_d-90efGHIJklmnopQ")
        self.assertTrue(seen["dest"].endswith(".mp4"))
        self.assertFalse(os.path.exists(seen["dest"]), "下載暫存目錄要清掉")
        self.assertEqual(gen.call_args.kwargs["mime_type"], "video/mp4")

    def test_drive_folder_link_rejected(self):
        from agent_core import video_understanding as vu

        meta = {"id": "x", "name": "教學影片資料夾",
                "mimeType": "application/vnd.google-apps.folder"}
        with mock.patch.object(vu, "_drive_file_meta", return_value=meta):
            out = vu.watch_teaching_video("1AbC_d-90efGHIJklmnopQ")
        self.assertIn("資料夾", out)

    def test_drive_meta_error_is_reported(self):
        from agent_core import video_understanding as vu

        with mock.patch.object(
            vu, "_drive_file_meta", side_effect=RuntimeError("403")
        ):
            out = vu.watch_teaching_video("1AbC_d-90efGHIJklmnopQ")
        self.assertIn("錯誤", out)
        self.assertIn("Drive", out)


class GeminiCallerPassthroughTests(unittest.TestCase):
    def test_caller_forwarded_to_cost_tracker(self):
        from agent_core import gemini_client as gc

        usage = SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=5,
            cached_content_token_count=0,
            total_token_count=15,
        )
        client = mock.MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            text="ok", usage_metadata=usage
        )
        with mock.patch.object(gc, "_get_gemini_client", return_value=client), \
             mock.patch("agent_core.cost_tracker.record_call") as rec:
            gc._gemini_generate(model="m", contents=["hi"], caller="x.y")
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["caller"], "x.y")


class ExtractMediaRoutingTests(unittest.TestCase):
    def test_video_uses_fusion_prompt_and_budget_caller(self):
        from agent_core.ingest import drive_sync

        with mock.patch.object(
            drive_sync, "get_embedding_hard_quota_message", return_value=None
        ), mock.patch.object(
            drive_sync, "_get_media_stop_message", return_value=None
        ), mock.patch(
            "agent_core.video_understanding.generate_from_media",
            return_value="融合文字",
        ) as gen:
            out = drive_sync._extract_media(b"MP4", "video/mp4")

        self.assertEqual(out, "融合文字")
        kw = gen.call_args.kwargs
        self.assertEqual(kw["caller"], "drive_sync._extract_media")
        self.assertIn("旁白", kw["prompt"])
        self.assertIn("畫面", kw["prompt"])

    def test_audio_uses_transcription_prompt(self):
        from agent_core.ingest import drive_sync

        with mock.patch.object(
            drive_sync, "get_embedding_hard_quota_message", return_value=None
        ), mock.patch.object(
            drive_sync, "_get_media_stop_message", return_value=None
        ), mock.patch(
            "agent_core.video_understanding.generate_from_media",
            return_value="逐字稿",
        ) as gen:
            out = drive_sync._extract_media(b"MP3", "audio/mpeg")

        self.assertEqual(out, "逐字稿")
        self.assertIn("Transcribe", gen.call_args.kwargs["prompt"])

    def test_oversized_media_returns_empty_before_gemini(self):
        from agent_core.ingest import drive_sync

        with mock.patch.object(drive_sync, "_MEDIA_MAX_BYTES", 4), mock.patch(
            "agent_core.video_understanding.generate_from_media",
            side_effect=AssertionError("不該打到 Gemini"),
        ):
            self.assertEqual(drive_sync._extract_media(b"x" * 5, "video/mp4"), "")

    def test_budget_stop_raises_before_gemini(self):
        from agent_core.ingest import drive_sync
        from agent_core.ingest.vector_store import GeminiHardQuotaError

        with mock.patch.object(
            drive_sync, "get_embedding_hard_quota_message", return_value=None
        ), mock.patch.object(
            drive_sync, "_get_media_stop_message",
            return_value="daily Drive media transcription budget exceeded",
        ):
            with self.assertRaises(GeminiHardQuotaError):
                drive_sync._extract_media(b"MP4", "video/mp4")


class EnvBoolTests(unittest.TestCase):
    def test_truthy_falsy_and_default(self):
        from agent_core.env_utils import env_bool

        cases = {
            "1": True, "true": True, "YES": True, "On": True,
            "0": False, "false": False, "No": False, "OFF": False,
        }
        for raw, expect in cases.items():
            with mock.patch.dict(os.environ, {"X_TEST_BOOL": raw}):
                self.assertEqual(env_bool("X_TEST_BOOL", not expect), expect, raw)
        with mock.patch.dict(os.environ, {"X_TEST_BOOL": "banana"}):
            self.assertTrue(env_bool("X_TEST_BOOL", True))
            self.assertFalse(env_bool("X_TEST_BOOL", False))
        os.environ.pop("X_TEST_BOOL", None)
        self.assertTrue(env_bool("X_TEST_BOOL", True))


class FfmpegHelperTests(unittest.TestCase):
    def test_missing_binaries_degrade_to_none(self):
        from agent_core import video_understanding as vu

        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(vu._ffprobe_duration_seconds("/tmp/x.mp4"))

    def test_duration_parsing(self):
        from agent_core import video_understanding as vu

        proc = SimpleNamespace(stdout="431.93\n", stderr="")
        with mock.patch("shutil.which", return_value="/opt/homebrew/bin/ffprobe"), \
             mock.patch("subprocess.run", return_value=proc):
            self.assertAlmostEqual(vu._ffprobe_duration_seconds("/tmp/x.mp4"), 431.93)


class DeepPipelineTests(unittest.TestCase):
    """混合關鍵幀深度分析：螢幕(關鍵幀高解析圖片) + 旁白(影片 pass) → SOP 整合。"""

    def _run_hybrid(self, *, frames=None, screens="螢幕內容A", narration="旁白與流程B",
                    merge=None, focus=""):
        from agent_core import video_understanding as vu

        if frames is None:
            frames = [(0.0, "/tmp/kf1.png"), (12.0, "/tmp/kf2.png")]
        if merge is None:
            merge = _resp("整合後的 SOP 文件")
        gen = mock.Mock(side_effect=[merge])
        with mock.patch.object(vu, "_ffprobe_duration_seconds", return_value=600.0), \
                mock.patch.object(vu, "_extract_keyframes", return_value=frames), \
                mock.patch.object(vu, "_read_screens", return_value=screens), \
                mock.patch.object(vu, "_narration_and_flow", return_value=narration), \
                mock.patch("agent_core.gemini_client._gemini_generate", gen):
            out = vu._deep_teaching_analysis(
                path="/tmp/fake.mp4", mime_type="video/mp4",
                focus=focus, model=None, caller="t.c",
            )
        return out, gen

    def test_screens_and_narration_merge_into_sop(self):
        out, gen = self._run_hybrid(screens="欄位X=9803", narration="講者點存檔")
        self.assertEqual(out, "整合後的 SOP 文件")
        self.assertEqual(gen.call_count, 1)
        merge_contents = gen.call_args_list[0].kwargs["contents"]
        self.assertEqual(len(merge_contents), 1)
        # 兩份素材都要進整合 prompt
        self.assertIn("欄位X=9803", merge_contents[0])
        self.assertIn("講者點存檔", merge_contents[0])

    def test_focus_steers_merge_prompt(self):
        _out, gen = self._run_hybrid(focus="領料怎麼填")
        self.assertIn("領料怎麼填", gen.call_args_list[0].kwargs["contents"][0])

    def test_no_keyframes_degrades_to_narration_only(self):
        # 沒 ffmpeg → 抽不到幀 → screens 空，仍以旁白整合出結果（不空手）
        _out, gen = self._run_hybrid(frames=[], screens="", narration="旁白流程")
        self.assertEqual(_out, "整合後的 SOP 文件")
        self.assertIn("（無螢幕截圖）", gen.call_args_list[0].kwargs["contents"][0])

    def test_merge_failure_assembles_locally(self):
        out, _gen = self._run_hybrid(
            screens="螢幕欄位內容", narration="旁白動作",
            merge=RuntimeError("503 UNAVAILABLE"),
        )
        # 整合 call 掛掉也別空手 → 本地併接兩份素材
        self.assertIn("旁白與操作流程", out)
        self.assertIn("旁白動作", out)
        self.assertIn("螢幕內容", out)
        self.assertIn("螢幕欄位內容", out)

    def test_empty_screens_and_narration_returns_empty(self):
        out, gen = self._run_hybrid(screens="", narration="")
        self.assertEqual(out, "")
        self.assertEqual(gen.call_count, 0)  # 兩者皆空 → 整合都不必呼叫


class KeyframeHelperTests(unittest.TestCase):
    def test_interval_scales_with_duration_and_clamps(self):
        from agent_core import video_understanding as vu

        # 短片 → 夾到下限；超長片 → 夾到上限；未知 → 上限
        self.assertEqual(vu._keyframe_interval(60), float(vu._SCREEN_MIN_INTERVAL_S))
        self.assertEqual(vu._keyframe_interval(99999), float(vu._SCREEN_MAX_INTERVAL_S))
        self.assertEqual(vu._keyframe_interval(None), float(vu._SCREEN_MAX_INTERVAL_S))
        mid = vu._keyframe_interval(600)
        self.assertTrue(vu._SCREEN_MIN_INTERVAL_S <= mid <= vu._SCREEN_MAX_INTERVAL_S)

    def test_read_screens_batches_high_res_images(self):
        import os as _os
        import shutil
        import tempfile

        from PIL import Image

        from agent_core import video_understanding as vu

        d = tempfile.mkdtemp()
        try:
            frames = []
            for i in range(2):
                p = _os.path.join(d, f"f{i}.png")
                Image.new("RGB", (32, 24), "white").save(p)
                frames.append((float(i * 10), p))
            gen = mock.Mock(return_value=_resp("### 00:00\n欄位=值"))
            with mock.patch("agent_core.gemini_client._gemini_generate", gen):
                out = vu._read_screens(frames, model="m", caller="t.c")
            self.assertIn("欄位=值", out)
            self.assertEqual(gen.call_count, 1)  # 2 張 < batch → 一次呼叫
            kw = gen.call_args_list[0].kwargs
            self.assertIn("HIGH", str(kw["config"].media_resolution))  # 圖片走 HIGH 解析
            self.assertEqual(len(kw["contents"]), 3)  # 2 圖片 Part + 1 prompt
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_read_screens_empty_frames_skips_api(self):
        from agent_core import video_understanding as vu

        gen = mock.Mock()
        with mock.patch("agent_core.gemini_client._gemini_generate", gen):
            self.assertEqual(vu._read_screens([], model="m", caller="c"), "")
        gen.assert_not_called()

    def test_frame_diff_distinguishes_screens_not_just_brightness(self):
        # 回歸守門：純色畫面舊 ahash 一律撞 hash 0（不同色被當重複丟掉）。
        # 改用 RGB 像素差後，不同色 → 大差值（保留）、同畫面 → 0（去重）。
        import shutil
        import tempfile

        from PIL import Image

        from agent_core import video_understanding as vu

        d = tempfile.mkdtemp()
        try:
            sigs = {}
            for name, color in [("red", (255, 0, 0)), ("green", (0, 255, 0)),
                                ("red2", (255, 0, 0))]:
                p = os.path.join(d, f"{name}.png")
                Image.new("RGB", (64, 48), color).save(p)
                sigs[name] = vu._frame_signature(p)
            self.assertEqual(vu._frame_diff(sigs["red"], sigs["red2"]), 0.0)  # 同色 → 去重
            self.assertGreater(vu._frame_diff(sigs["red"], sigs["green"]),
                               vu._SCREEN_DEDUP_DIFF)  # 異色 → 保留
        finally:
            shutil.rmtree(d, ignore_errors=True)


class WatchDeepRoutingTests(unittest.TestCase):
    def test_deep_flag_routes_video_to_deep_pipeline(self):
        from agent_core import video_understanding as vu

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"VIDEO")
            path = f.name
        try:
            with mock.patch.object(
                vu, "_deep_teaching_analysis", return_value="深度結果"
            ) as deep, mock.patch.object(
                vu, "generate_from_media",
                side_effect=AssertionError("deep=True 不該走 quick"),
            ):
                out = vu.watch_teaching_video(path, deep=True)
            self.assertIn("深度結果", out)
            self.assertEqual(
                deep.call_args.kwargs["caller"],
                "video_understanding.watch_teaching_video",
            )
        finally:
            os.unlink(path)

    def test_deep_flag_on_audio_falls_back_to_quick(self):
        from agent_core import video_understanding as vu

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"AUDIO")
            path = f.name
        try:
            with mock.patch.object(
                vu, "generate_from_media", return_value="轉錄"
            ) as quick, mock.patch.object(
                vu, "_deep_teaching_analysis",
                side_effect=AssertionError("audio 不該走深度管線"),
            ):
                out = vu.watch_teaching_video(path, deep=True)
            self.assertIn("轉錄", out)
            self.assertTrue(quick.called)
        finally:
            os.unlink(path)


class VideoIntelligenceTests(unittest.TestCase):
    def test_missing_file_unavailable(self):
        from agent_core import video_intelligence as vi

        result = vi.annotate_video("/nonexistent/x.mp4")
        self.assertFalse(result["available"])
        self.assertIn("讀不到檔案", result["reason"])

    def test_oversized_file_unavailable(self):
        from agent_core import video_intelligence as vi

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"x" * 32)
            path = f.name
        try:
            with mock.patch.object(vi, "_VIDEO_INTEL_MAX_BYTES", 4):
                result = vi.annotate_video(path)
            self.assertFalse(result["available"])
            self.assertIn("超過 inline 上限", result["reason"])
        finally:
            os.unlink(path)

    def test_api_error_degrades_with_reason(self):
        from agent_core import video_intelligence as vi

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"VIDEO")
            path = f.name
        try:
            with mock.patch(
                "google.cloud.videointelligence.VideoIntelligenceServiceClient"
            ) as client_cls, mock.patch.object(vi, "_find_credentials_file", return_value=""):
                client_cls.return_value.annotate_video.side_effect = RuntimeError(
                    "PERMISSION_DENIED: Cloud Video Intelligence API has not been used"
                )
                result = vi.annotate_video(path)
            self.assertFalse(result["available"])
            self.assertIn("PERMISSION_DENIED", result["reason"])
        finally:
            os.unlink(path)

    def test_seconds_handles_timedelta_and_raw_duration(self):
        from datetime import timedelta

        from agent_core import video_intelligence as vi

        # proto-plus 真實回的是 timedelta（沒有 .nanos！）
        self.assertEqual(vi._seconds(timedelta(seconds=12, milliseconds=500)), 12.5)
        # raw protobuf Duration 形狀（seconds/nanos）走 fallback
        self.assertEqual(
            vi._seconds(SimpleNamespace(seconds=3, nanos=250_000_000)), 3.25
        )
        self.assertEqual(vi._seconds(object()), 0.0)

    def test_successful_annotation_parses_shots_and_texts(self):
        from datetime import timedelta

        from agent_core import video_intelligence as vi

        def dur(s, n=0):
            # 對齊 proto-plus 實際行為：Duration → datetime.timedelta
            return timedelta(seconds=s, microseconds=n // 1000)

        shot = SimpleNamespace(start_time_offset=dur(0), end_time_offset=dur(12, 500_000_000))
        text_seg = SimpleNamespace(segment=SimpleNamespace(
            start_time_offset=dur(3), end_time_offset=dur(6)))
        text = SimpleNamespace(text="配合畫布大小", segments=[text_seg])
        annotation = SimpleNamespace(shot_annotations=[shot], text_annotations=[text])
        operation = mock.Mock()
        operation.result.return_value = SimpleNamespace(annotation_results=[annotation])

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"VIDEO")
            path = f.name
        try:
            with mock.patch(
                "google.cloud.videointelligence.VideoIntelligenceServiceClient"
            ) as client_cls, mock.patch.object(vi, "_find_credentials_file", return_value=""):
                client_cls.return_value.annotate_video.return_value = operation
                result = vi.annotate_video(path)
            self.assertTrue(result["available"])
            self.assertEqual(result["shots"], [{"start_s": 0.0, "end_s": 12.5}])
            self.assertEqual(result["texts"][0]["text"], "配合畫布大小")
            self.assertEqual(result["texts"][0]["start_s"], 3.0)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
