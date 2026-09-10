"""會議錄影走本地 ASR 中文轉稿的單元測試（drive_sync._extract_meeting_transcript）。

不碰真實 whisper/drive/chroma：_execute_drive_request 與 local_asr/asr_glossary
全 mock。隔離放 setUp/tearDown（本 repo 跑 unittest discover，conftest fixture
不生效）。
"""
import unittest
from unittest import mock

from agent_core.ingest import drive_sync


class MeetingFolderWhitelistTests(unittest.TestCase):
    def setUp(self):
        self._orig = drive_sync._MEETING_FOLDER_IDS
        drive_sync._MEETING_FOLDER_IDS = frozenset({"MEET_FOLDER"})

    def tearDown(self):
        drive_sync._MEETING_FOLDER_IDS = self._orig

    def test_is_meeting_folder(self):
        self.assertTrue(drive_sync._is_meeting_folder("MEET_FOLDER"))
        self.assertFalse(drive_sync._is_meeting_folder("OTHER_FOLDER"))
        self.assertFalse(drive_sync._is_meeting_folder(""))


class MeetingFolderSkipMarkerTests(unittest.TestCase):
    """meeting_folder_non_video skip-marker 要被 _skip_marker_matches 認得，
    否則 marker 寫了也沒人認（死 marker）→ 每晚重下載同一批 Meet 英文轉稿。"""

    def setUp(self):
        self._orig = drive_sync._MEETING_FOLDER_IDS
        drive_sync._MEETING_FOLDER_IDS = frozenset({"MEET_FOLDER"})

    def tearDown(self):
        drive_sync._MEETING_FOLDER_IDS = self._orig

    @staticmethod
    def _marker():
        return {
            "reason": "meeting_folder_non_video",
            "modified_time": "2026-06-01T10:00:00.000Z",
            "folder_id": "MEET_FOLDER", "drive_id": "0AX",
            "title": "Transcript.docx", "mime_type": "application/vnd.google-apps.document",
        }

    def test_matches_when_modified_time_unchanged(self):
        self.assertTrue(drive_sync._skip_marker_matches(
            self._marker(), "2026-06-01T10:00:00.000Z", "MEET_FOLDER", "0AX"))

    def test_rejected_when_modified_time_changed(self):
        self.assertFalse(drive_sync._skip_marker_matches(
            self._marker(), "2026-06-02T10:00:00.000Z", "MEET_FOLDER", "0AX"))

    def test_self_clears_when_folder_no_longer_meeting(self):
        # 資料夾移出會議白名單 → marker 失效，檔案重新入索引。
        drive_sync._MEETING_FOLDER_IDS = frozenset()
        self.assertFalse(drive_sync._skip_marker_matches(
            self._marker(), "2026-06-01T10:00:00.000Z", "MEET_FOLDER", "0AX"))


class ExtractMeetingTranscriptTests(unittest.TestCase):
    def _run(self, *, available=True, transcribe=None, formatted="", corrected=None):
        if transcribe is None:
            transcribe = {"text": "大家好 開會了", "segments": [], "dropped": 0}
        patches = [
            mock.patch.object(drive_sync, "_execute_drive_request", return_value=b"fakevideobytes"),
            mock.patch("agent_core.local_asr.is_available", return_value=available),
            mock.patch("agent_core.local_asr.transcribe_local", return_value=transcribe),
            mock.patch("agent_core.local_asr.format_transcript", return_value=formatted),
        ]
        if corrected is not None:
            patches.append(mock.patch("agent_core.asr_glossary.correct_transcript", return_value=corrected))
        with patches[0], patches[1], patches[2], patches[3]:
            if corrected is not None:
                with patches[4]:
                    return drive_sync._extract_meeting_transcript(object(), "fid", "video/mp4")
            return drive_sync._extract_meeting_transcript(object(), "fid", "video/mp4")

    def test_happy_path_wraps_transcript(self):
        out = self._run(
            formatted="[00:00] 大家好\n[00:02] 開會了",
            corrected={"ok": True, "text": "[00:00] 大家好\n[00:02] 開會了"},
        )
        self.assertIn("【會議錄影中文轉稿】", out)
        self.assertIn("大家好", out)

    def test_glossary_failure_falls_back_to_raw(self):
        # correct_transcript 拋錯不該擋主轉稿
        with mock.patch.object(drive_sync, "_execute_drive_request", return_value=b"x"), \
             mock.patch("agent_core.local_asr.is_available", return_value=True), \
             mock.patch("agent_core.local_asr.transcribe_local",
                        return_value={"text": "有內容", "segments": [], "dropped": 0}), \
             mock.patch("agent_core.local_asr.format_transcript", return_value="[00:00] 原始稿"), \
             mock.patch("agent_core.asr_glossary.correct_transcript", side_effect=RuntimeError("boom")):
            out = drive_sync._extract_meeting_transcript(object(), "fid", "video/mp4")
        self.assertIn("原始稿", out)

    def test_asr_unavailable_raises(self):
        # whisper 暫時不可用是 transient → raise，別 return "" 落入 empty_text 永久 skip
        with self.assertRaises(RuntimeError) as ctx:
            self._run(available=False)
        self.assertIn("local_asr_unavailable", str(ctx.exception))

    def test_asr_failed_none_raises(self):
        # transcribe_local 回 None（ffmpeg/whisper 崩潰）→ raise，別永久 skip
        with mock.patch.object(drive_sync, "_execute_drive_request", return_value=b"x"), \
             mock.patch("agent_core.local_asr.is_available", return_value=True), \
             mock.patch("agent_core.local_asr.transcribe_local", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                drive_sync._extract_meeting_transcript(object(), "fid", "video/mp4")
        self.assertIn("local_asr_failed", str(ctx.exception))

    def test_empty_transcript_returns_empty(self):
        out = self._run(transcribe={"text": "", "segments": [], "dropped": 0}, formatted="")
        self.assertEqual(out, "")

    def test_oversize_returns_empty(self):
        with mock.patch.object(drive_sync, "_execute_drive_request", return_value=b"x" * 10), \
             mock.patch.object(drive_sync, "_MEDIA_MAX_BYTES", 5):
            out = drive_sync._extract_meeting_transcript(object(), "fid", "video/mp4")
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
