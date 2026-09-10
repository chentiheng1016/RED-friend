"""Phase 4 本機 gating：vision_ocr 共用入口、關鍵幀 OCR gating、
轉錄觸發補抽幀、本機逐字稿進 SOP 整合的單元測試。全部 mock、不依賴 macOS。"""
import os
import tempfile
import unittest
from unittest import mock

from agent_core import video_understanding as vu
from agent_core import vision_ocr


def _resp(text: str):
    r = mock.MagicMock()
    r.text = text
    return r


class VisionOcrModuleTests(unittest.TestCase):
    def _fake_deps(self, annotations):
        img = mock.MagicMock()
        img.mode = "RGB"
        Image = mock.MagicMock()
        Image.open.return_value = img
        ocr = mock.MagicMock()
        ocr.OCR.return_value.recognize.return_value = annotations
        return Image, ocr

    def test_unavailable_returns_none(self):
        with mock.patch.object(vision_ocr, "_import_deps",
                               return_value=(None, None)):
            self.assertIsNone(vision_ocr.ocr_image(b"png"))

    def test_text_join_and_avg_confidence(self):
        deps = self._fake_deps([("INVOICE", 0.9, None), ("279.41", 0.7, None),
                                ("", 0.5, None)])
        with mock.patch.object(vision_ocr, "_import_deps", return_value=deps):
            out = vision_ocr.ocr_image(b"png")
        self.assertEqual(out["text"], "INVOICE 279.41")
        self.assertAlmostEqual(out["confidence"], 0.8)

    def test_no_text_returns_none(self):
        deps = self._fake_deps([])
        with mock.patch.object(vision_ocr, "_import_deps", return_value=deps):
            self.assertIsNone(vision_ocr.ocr_image(b"png"))

    def test_path_input_and_ocr_error(self):
        Image, ocr = self._fake_deps([("hi", 0.9, None)])
        with mock.patch.object(vision_ocr, "_import_deps",
                               return_value=(Image, ocr)):
            out = vision_ocr.ocr_image("/tmp/frame.png")
        Image.open.assert_called_with("/tmp/frame.png")
        self.assertEqual(out["text"], "hi")
        ocr.OCR.side_effect = RuntimeError("boom")
        with mock.patch.object(vision_ocr, "_import_deps",
                               return_value=(Image, ocr)):
            self.assertIsNone(vision_ocr.ocr_image("/tmp/frame.png"))

    def test_langs_env_override(self):
        with mock.patch.dict(os.environ, {"RAG_VISION_OCR_LANGS": "ja-JP, en-US"}):
            self.assertEqual(vision_ocr._langs(), ["ja-JP", "en-US"])
        with mock.patch.dict(os.environ, {"RAG_VISION_OCR_LANGS": ""}):
            self.assertEqual(vision_ocr._langs(), list(vision_ocr._DEFAULT_LANGS))


class DriveSyncDelegationTests(unittest.TestCase):
    def test_delegates_to_shared_entry(self):
        from agent_core.ingest import drive_sync
        with mock.patch("agent_core.vision_ocr.ocr_image",
                        return_value={"text": "INVOICE 279.41",
                                      "confidence": 0.9}) as shared:
            text = drive_sync._extract_image_vision(b"png", "image/png")
        self.assertEqual(text, "INVOICE 279.41")
        self.assertEqual(shared.call_args.kwargs["languages"],
                         list(drive_sync._VISION_OCR_LANGS))

    def test_none_passthrough(self):
        from agent_core.ingest import drive_sync
        with mock.patch("agent_core.vision_ocr.ocr_image", return_value=None):
            self.assertIsNone(drive_sync._extract_image_vision(b"png", "image/png"))


class ActionTimestampTests(unittest.TestCase):
    def test_verbs_kept_only_dedup_and_cap(self):
        segments = [
            {"start": 5.0, "text": "先開啟收櫃畫面", "kept": True},
            {"start": 5.5, "text": "然後點這裡", "kept": True},      # 1 秒內去重
            {"start": 20.0, "text": "這段是幻覺點按", "kept": False},  # 被濾段不觸發
            {"start": 30.0, "text": "今天天氣不錯", "kept": True},     # 無動詞
            {"start": 40.0, "text": "在欄位輸入數量", "kept": True},
        ]
        out = vu._action_timestamps(segments)
        self.assertEqual(out, [5.0, 40.0])
        with mock.patch.object(vu, "_ASR_TRIGGER_MAX", 1):
            self.assertEqual(vu._action_timestamps(segments), [5.0])

    def test_empty(self):
        self.assertEqual(vu._action_timestamps([]), [])


class ExtractFramesAtTests(unittest.TestCase):
    def test_no_ffmpeg_returns_empty(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(vu._extract_frames_at("/v.mp4", [1.0], "/tmp", []), [])

    def test_skips_near_existing_and_extracts_rest(self):
        tmp = tempfile.TemporaryDirectory()

        def fake_run(cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"png")
            return mock.MagicMock(returncode=0)

        existing = [(10.0, "/tmp/kf1.png")]
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("subprocess.run", side_effect=fake_run):
            added = vu._extract_frames_at(
                "/v.mp4", [10.5, 30.0], tmp.name, existing)
        self.assertEqual([t for t, _ in added], [30.0])  # 10.5 離既有幀 <2s 被跳過
        self.assertTrue(os.path.exists(added[0][1]))
        tmp.cleanup()

    def test_ffmpeg_failure_skips_point(self):
        tmp = tempfile.TemporaryDirectory()
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("subprocess.run",
                           side_effect=RuntimeError("boom")):
            self.assertEqual(
                vu._extract_frames_at("/v.mp4", [30.0], tmp.name, []), [])
        tmp.cleanup()


class SplitFramesByLocalOcrTests(unittest.TestCase):
    def test_rich_frames_become_blocks_poor_frames_remain(self):
        frames = [(5.0, "/tmp/kf1.png"), (12.0, "/tmp/kf2.png"),
                  (20.0, "/tmp/kf3.png")]

        def fake_ocr(path):
            if path == "/tmp/kf1.png":
                return {"text": "F" * 100, "confidence": 0.93}
            if path == "/tmp/kf2.png":
                return {"text": "短", "confidence": 0.5}
            return None

        with mock.patch("agent_core.vision_ocr.ocr_image", side_effect=fake_ocr):
            blocks, remaining = vu._split_frames_by_local_ocr(frames)
        self.assertIn("【畫面 @ 00:05（本機OCR，OCR信心 0.93）】", blocks)
        self.assertIn("F" * 100, blocks)
        self.assertEqual([t for t, _ in remaining], [12.0, 20.0])

    def test_ocr_error_keeps_frame_for_gemini(self):
        with mock.patch("agent_core.vision_ocr.ocr_image",
                        side_effect=RuntimeError("boom")):
            blocks, remaining = vu._split_frames_by_local_ocr(
                [(5.0, "/tmp/kf1.png")])
        self.assertEqual(blocks, "")
        self.assertEqual(len(remaining), 1)


class LocalTranscriptTests(unittest.TestCase):
    def test_unavailable_returns_empty(self):
        with mock.patch("agent_core.local_asr.is_available", return_value=False):
            self.assertEqual(vu._local_transcript("/v.mp4"), ("", []))

    def test_transcribe_correct_and_segments(self):
        asr_result = {
            "text": "開啟收貴畫面",
            "segments": [{"start": 5.0, "text": "開啟收貴畫面", "kept": True}],
        }
        with mock.patch("agent_core.local_asr.is_available", return_value=True), \
                mock.patch("agent_core.local_asr.transcribe_local",
                           return_value=asr_result) as trans, \
                mock.patch("agent_core.local_asr.format_transcript",
                           return_value="[00:05] 開啟收貴畫面"), \
                mock.patch("agent_core.asr_glossary.top_terms",
                           return_value=["收櫃"]), \
                mock.patch("agent_core.asr_glossary.correct_transcript",
                           return_value={"ok": True,
                                         "text": "[00:05] 開啟收櫃畫面"}):
            text, segs = vu._local_transcript("/v.mp4")
        self.assertEqual(text, "[00:05] 開啟收櫃畫面")  # glossary 修正後版本
        self.assertEqual(len(segs), 1)
        self.assertEqual(trans.call_args.kwargs["glossary_terms"], ["收櫃"])

    def test_correction_failure_uses_raw_transcript(self):
        asr_result = {"text": "旁白", "segments": [{"start": 1.0, "kept": True}]}
        with mock.patch("agent_core.local_asr.is_available", return_value=True), \
                mock.patch("agent_core.local_asr.transcribe_local",
                           return_value=asr_result), \
                mock.patch("agent_core.local_asr.format_transcript",
                           return_value="[00:01] 旁白"), \
                mock.patch("agent_core.asr_glossary.top_terms", return_value=[]), \
                mock.patch("agent_core.asr_glossary.correct_transcript",
                           side_effect=RuntimeError("503")):
            text, _segs = vu._local_transcript("/v.mp4")
        self.assertEqual(text, "[00:01] 旁白")


class DeepGatingIntegrationTests(unittest.TestCase):
    """deep 模式整合：flags 開時三份素材進整合 prompt、關時行為與現況相同。"""

    def _run(self, *, local_asr_on=False, ocr_gate_on=False, merge=None,
             transcript=("", [])):
        frames = [(0.0, "/tmp/kf1.png"), (12.0, "/tmp/kf2.png")]
        if merge is None:
            merge = _resp("整合後的 SOP 文件")
        gen = mock.Mock(side_effect=[merge])
        read_screens = mock.Mock(return_value="雲端螢幕讀取")
        local_split = mock.Mock(
            return_value=("### 【畫面 @ 00:00（本機OCR）】\nFTE_570 收櫃",
                          [(12.0, "/tmp/kf2.png")]))
        local_trans = mock.Mock(return_value=transcript)
        extract_at = mock.Mock(return_value=[(5.0, "/tmp/tg1.png")])
        with mock.patch.object(vu, "_ffprobe_duration_seconds", return_value=600.0), \
                mock.patch.object(vu, "_extract_keyframes", return_value=frames), \
                mock.patch.object(vu, "_read_screens", read_screens), \
                mock.patch.object(vu, "_narration_and_flow",
                                  return_value="旁白與流程B"), \
                mock.patch.object(vu, "_local_transcript", local_trans), \
                mock.patch.object(vu, "_split_frames_by_local_ocr", local_split), \
                mock.patch.object(vu, "_extract_frames_at", extract_at), \
                mock.patch.object(vu, "_LOCAL_ASR", local_asr_on), \
                mock.patch.object(vu, "_LOCAL_OCR_GATE", ocr_gate_on), \
                mock.patch("agent_core.gemini_client._gemini_generate", gen):
            out = vu._deep_teaching_analysis(
                path="/tmp/fake.mp4", mime_type="video/mp4",
                focus="", model=None, caller="t.c")
        return out, gen, {"read_screens": read_screens, "split": local_split,
                          "local_trans": local_trans, "extract_at": extract_at}

    def test_flags_off_keeps_current_behavior(self):
        out, gen, mocks = self._run()
        self.assertEqual(out, "整合後的 SOP 文件")
        mocks["local_trans"].assert_not_called()
        mocks["split"].assert_not_called()
        mocks["extract_at"].assert_not_called()
        # 沒逐字稿 → prompt 不出現素材C
        self.assertNotIn("素材C", gen.call_args.kwargs["contents"][0])

    def test_asr_on_adds_transcript_and_trigger_frames(self):
        transcript = ("[00:05] 開啟收櫃畫面",
                      [{"start": 5.0, "text": "點開啟", "kept": True}])
        out, gen, mocks = self._run(local_asr_on=True, transcript=transcript)
        self.assertEqual(out, "整合後的 SOP 文件")
        mocks["extract_at"].assert_called_once()
        prompt = gen.call_args.kwargs["contents"][0]
        self.assertIn("素材C", prompt)
        self.assertIn("[00:05] 開啟收櫃畫面", prompt)
        # 補抽的幀（5.0s）要混入送 _read_screens 的清單
        sent_frames = mocks["read_screens"].call_args[0][0]
        self.assertIn((5.0, "/tmp/tg1.png"), sent_frames)

    def test_ocr_gate_on_prepends_local_blocks_and_trims_cloud_frames(self):
        out, gen, mocks = self._run(ocr_gate_on=True)
        self.assertEqual(out, "整合後的 SOP 文件")
        sent_frames = mocks["read_screens"].call_args[0][0]
        self.assertEqual(sent_frames, [(12.0, "/tmp/kf2.png")])  # 只送剩餘幀
        prompt = gen.call_args.kwargs["contents"][0]
        self.assertIn("FTE_570 收櫃", prompt)     # 本機 OCR 區塊進素材A
        self.assertIn("雲端螢幕讀取", prompt)

    def test_merge_failure_assembles_transcript_locally(self):
        transcript = ("[00:05] 開啟收櫃畫面",
                      [{"start": 5.0, "text": "點開啟", "kept": True}])
        out, _gen, _ = self._run(local_asr_on=True, transcript=transcript,
                                 merge=RuntimeError("503"))
        self.assertIn("本機旁白逐字稿", out)
        self.assertIn("[00:05] 開啟收櫃畫面", out)


if __name__ == "__main__":
    unittest.main()
