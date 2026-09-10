"""training_videos：部門推斷、已學狀態、watcher 閘門與學習流程的單元測試。
Drive / Gemini / chroma 全 mock。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_core import training_videos as tv


class DeptTests(unittest.TestCase):
    def test_folder_to_dept(self):
        cases = {
            "生管教學": "生管", "會計部教學": "會計", "現場管理教學": "現場",
            "倉庫管理教學": "倉庫", "採購部教學": "採購", "業務部教學": "業務",
            "開發部教學": "開發", "ERP 上課紀錄": "ERP 上課紀錄",
            "安裝手冊": "安裝手冊", "": "",
        }
        for folder, dept in cases.items():
            self.assertEqual(tv._dept_from_folder(folder), dept, folder)


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(
            tv, "_STATE_PATH", os.path.join(self._tmp.name, "state.json"))
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_roundtrip_and_corrupt(self):
        state = {"learned": {"v1": {"name": "a.mp4"}}, "skipped": {}}
        tv._save_state(state)
        loaded = tv._load_state()
        self.assertEqual(loaded["learned"]["v1"]["name"], "a.mp4")
        with open(tv._STATE_PATH, "w") as f:
            f.write("not json")
        self.assertEqual(tv._load_state(), {"learned": {}, "skipped": {}})

    def test_seed_from_collection(self):
        store = mock.MagicMock()
        col = mock.MagicMock()
        store._col = col
        col.get.return_value = {"metadatas": [
            {"video_id": "vidA", "video_name": "a.mp4"},
            {"video_id": "vidA", "video_name": "a.mp4"},  # 多 chunk 去重
            {"video_id": "vidB", "video_name": "b.mp4"},
        ]}
        with mock.patch("agent_core.ingest.vector_store.get_store",
                        return_value=store):
            state = tv._seed_from_collection({"learned": {}, "skipped": {}})
        self.assertEqual(set(state["learned"]), {"vidA", "vidB"})
        self.assertEqual(state["learned"]["vidA"]["at"], "seeded")


class RelearnVideoTests(unittest.TestCase):
    def _flow_patches(self, **overrides):
        """完整學習流程的標準 mock 組（個別測試可覆寫）。"""
        return {
            "meta": mock.patch(
                "agent_core.video_understanding._drive_file_meta",
                return_value={"mimeType": "video/mp4", "name": "新片.mp4"}),
            "download": mock.patch("agent_core.erp._download_drive_file",
                                   return_value=True),
            "deep": mock.patch(
                "agent_core.video_understanding._deep_teaching_analysis",
                return_value="【00:05】步驟一"),
            "asr": mock.patch("agent_core.local_asr.is_available",
                              return_value=True),
            "sop": mock.patch("agent_core.operation_sops.ingest_operation_sop",
                              return_value={"ok": True, "chunks": 3}),
            "extract": mock.patch(
                "agent_core.skill_cards.extract_skill_cards",
                return_value={"ok": True, "cards": [{"claim": "x"}]}),
            "purge": mock.patch("agent_core.skill_cards.purge_video_cards",
                                return_value=2),
            "ingest": mock.patch("agent_core.skill_cards.ingest_skill_cards",
                                 return_value={"ok": True, "likely": 1}),
            **overrides,
        }

    def test_full_flow(self):
        p = self._flow_patches()
        with p["meta"], p["download"], p["deep"] as deep, p["asr"], \
                p["sop"] as sop, p["extract"], p["purge"] as purge, \
                p["ingest"], \
                mock.patch.object(tv, "_dept_from_sops") as backfill:
            r = tv.relearn_video("vidX", department="生管")
        self.assertTrue(r["ok"])
        self.assertEqual(r["video"], "新片.mp4")
        self.assertEqual(r["sop_chunks"], 3)
        self.assertEqual(r["purged_old"], 2)          # 先清舊卡再入新卡
        self.assertEqual(sop.call_args.kwargs["asr_engine"], "whisper.cpp")
        self.assertEqual(deep.call_args.kwargs["caller"],
                         "training_videos.relearn")
        purge.assert_called_once_with("vidX")
        backfill.assert_not_called()  # 有給 department 就不用回填

    def test_empty_department_backfilled_from_sops(self):
        # CLI 指定 video_id 重學不帶部門 — 不得把既有 department 洗成空字串。
        p = self._flow_patches()
        with p["meta"], p["download"], p["deep"], p["asr"], \
                p["sop"] as sop, p["extract"], p["purge"], p["ingest"], \
                mock.patch.object(tv, "_dept_from_sops",
                                  return_value="倉庫") as backfill:
            r = tv.relearn_video("vidX")
        self.assertTrue(r["ok"])
        self.assertEqual(r["dept"], "倉庫")
        self.assertEqual(sop.call_args.kwargs["department"], "倉庫")
        backfill.assert_called_once_with("vidX")

    def test_download_failure(self):
        with mock.patch("agent_core.video_understanding._drive_file_meta",
                        return_value={"mimeType": "video/mp4", "name": "n"}), \
                mock.patch("agent_core.erp._download_drive_file",
                           return_value=False), \
                mock.patch.object(tv, "_dept_from_sops", return_value=""):
            r = tv.relearn_video("vidX")
        self.assertFalse(r["ok"])
        self.assertIn("下載失敗", r["error"])

    def test_exception_returns_error(self):
        with mock.patch("agent_core.video_understanding._drive_file_meta",
                        side_effect=RuntimeError("boom")), \
                mock.patch.object(tv, "_dept_from_sops", return_value=""):
            r = tv.relearn_video("vidX")
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])


class DeptBackfillTests(unittest.TestCase):
    def test_reads_existing_sop_metadata(self):
        store = mock.MagicMock()
        col = mock.MagicMock()
        store._col = col
        col.get.return_value = {"metadatas": [{"department": "倉庫"}]}
        with mock.patch("agent_core.ingest.vector_store.get_store",
                        return_value=store):
            self.assertEqual(tv._dept_from_sops("vidA"), "倉庫")
        self.assertEqual(col.get.call_args.kwargs["where"],
                         {"video_id": {"$eq": "vidA"}})

    def test_missing_or_failed_lookup_returns_empty(self):
        store = mock.MagicMock()
        col = mock.MagicMock()
        store._col = col
        col.get.return_value = {"metadatas": []}
        with mock.patch("agent_core.ingest.vector_store.get_store",
                        return_value=store):
            self.assertEqual(tv._dept_from_sops("vidA"), "")
        with mock.patch("agent_core.ingest.vector_store.get_store",
                        side_effect=RuntimeError("chroma down")):
            self.assertEqual(tv._dept_from_sops("vidA"), "")  # 不擋重學
        self.assertEqual(tv._dept_from_sops("  "), "")


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(tv, "_STATE_PATH",
                              os.path.join(self._tmp.name, "state.json")),
            mock.patch.object(tv, "_seed_from_collection",
                              side_effect=lambda s: s),
            # 免疫宿主的活 chroma 狀態（tests-immune-to-live-state）
            mock.patch.object(tv, "_chroma_ready", return_value=(True, "")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.notify = mock.patch.object(tv, "_notify").start()
        self.addCleanup(mock.patch.stopall)

    def test_no_new_videos(self):
        tv._save_state({"learned": {"v1": {}}, "skipped": {}})
        with mock.patch.object(tv, "list_training_videos",
                               return_value=[{"id": "v1", "name": "a", "dept": "",
                                              "folder": "", "size": 1}]):
            s = tv.check_and_learn_new()
        self.assertEqual(s["new_found"], 0)
        self.notify.assert_not_called()

    def test_drive_listing_transient_error_returns_ok_false_not_crash(self):
        # 真實事故（2026-07-16）：長輪跑到一半 access token 到期，此刻斷網 / DNS
        # 解不出 oauth2.googleapis.com，get_service 拋 RuntimeError（daemon 模式
        # 無法互動式重新授權）。這類暫時性錯誤不該讓單發 daemon crash 成 exit 1
        # 驚動 fleet 狀態 / smoke，應回 ok=False、明天自動重試。
        with mock.patch.object(
            tv, "list_training_videos",
            side_effect=RuntimeError("daemon 模式下無法啟動互動式 OAuth 授權"),
        ):
            s = tv.check_and_learn_new()   # 不得拋
        self.assertFalse(s["ok"])
        self.assertEqual(s["reason"], "drive_unavailable")
        self.assertIn("detail", s)
        self.notify.assert_not_called()

    def test_learns_small_first_caps_and_defers(self):
        vids = [
            {"id": "big", "name": "big.mp4", "dept": "生管", "folder": "", "size": 900},
            {"id": "s1", "name": "s1.mp4", "dept": "倉庫", "folder": "", "size": 10},
            {"id": "s2", "name": "s2.mp4", "dept": "採購", "folder": "", "size": 20},
        ]
        learned_calls = []

        def fake_relearn(vid, name, dept, **kw):
            learned_calls.append(vid)
            return {"ok": True, "video": name, "minutes": 1.0, "cards": 5}

        with mock.patch.object(tv, "list_training_videos", return_value=vids), \
                mock.patch.object(tv, "relearn_video", side_effect=fake_relearn):
            s = tv.check_and_learn_new(max_videos=2, max_bytes=10_000)
        self.assertEqual(learned_calls, ["s1", "s2"])   # 小的先學
        self.assertEqual(s["deferred"], 1)              # 超過每輪上限 → 明天
        state = tv._load_state()
        self.assertEqual(set(state["learned"]), {"s1", "s2"})
        self.assertNotIn("big", state["skipped"])       # deferred ≠ skipped
        self.notify.assert_called_once()

    def test_oversize_skipped_and_recorded(self):
        vids = [{"id": "huge", "name": "huge.mp4", "dept": "會計",
                 "folder": "", "size": 5_000_000_000}]
        with mock.patch.object(tv, "list_training_videos", return_value=vids), \
                mock.patch.object(tv, "relearn_video") as rl:
            s = tv.check_and_learn_new(max_videos=2, max_bytes=1_000)
        rl.assert_not_called()
        self.assertIn("huge", tv._load_state()["skipped"])  # 不會每天重試
        self.assertEqual(len(s["skipped"]), 1)

    def test_failure_not_recorded_retries_tomorrow(self):
        vids = [{"id": "v1", "name": "a.mp4", "dept": "", "folder": "", "size": 1}]
        with mock.patch.object(tv, "list_training_videos", return_value=vids), \
                mock.patch.object(tv, "relearn_video",
                                  return_value={"ok": False, "error": "503"}):
            s = tv.check_and_learn_new(max_videos=2, max_bytes=10_000)
        state = tv._load_state()
        self.assertEqual(state["learned"], {})
        self.assertEqual(state["skipped"], {})           # 明天自動重試
        self.assertEqual(len(s["skipped"]), 1)           # 但通知裡有講

    def test_cap_counts_attempts_not_successes(self):
        # deep 費用在 ingest 前就燒掉了 — 連環失敗的晚上不得把整批新影片
        # 都各燒一輪（舊行為只數成功、失敗不佔額度）。
        vids = [
            {"id": "s1", "name": "s1.mp4", "dept": "", "folder": "", "size": 10},
            {"id": "s2", "name": "s2.mp4", "dept": "", "folder": "", "size": 20},
            {"id": "s3", "name": "s3.mp4", "dept": "", "folder": "", "size": 30},
        ]
        calls = []

        def fake_relearn(vid, name, dept, **kw):
            calls.append(vid)
            return {"ok": False, "error": "503"}

        with mock.patch.object(tv, "list_training_videos", return_value=vids), \
                mock.patch.object(tv, "relearn_video", side_effect=fake_relearn):
            s = tv.check_and_learn_new(max_videos=1, max_bytes=10_000)
        self.assertEqual(calls, ["s1"])       # 失敗也佔額度，第二支不再嘗試
        self.assertEqual(s["deferred"], 2)

    def test_chroma_down_aborts_before_burning_deep(self):
        vids = [{"id": "v1", "name": "a.mp4", "dept": "", "folder": "", "size": 1}]
        with mock.patch.object(tv, "list_training_videos", return_value=vids), \
                mock.patch.object(tv, "_chroma_ready",
                                  return_value=(False, "heartbeat 無回應")), \
                mock.patch.object(tv, "relearn_video") as rl:
            s = tv.check_and_learn_new(max_videos=2, max_bytes=10_000)
        rl.assert_not_called()                # 一支都不燒
        self.assertFalse(s["ok"])
        self.assertEqual(s["reason"], "chroma_unavailable")
        self.assertIn("heartbeat", s["detail"])
        self.notify.assert_not_called()

    def test_chroma_probe_skipped_when_no_new_videos(self):
        tv._save_state({"learned": {"v1": {}}, "skipped": {}})
        with mock.patch.object(tv, "list_training_videos",
                               return_value=[{"id": "v1", "name": "a", "dept": "",
                                              "folder": "", "size": 1}]), \
                mock.patch.object(tv, "_chroma_ready") as probe:
            s = tv.check_and_learn_new()
        probe.assert_not_called()             # 沒事就別打 heartbeat
        self.assertTrue(s["ok"])


class PurgeVideoCardsTests(unittest.TestCase):
    def test_purge_solely_sourced_keeps_corroborated(self):
        from agent_core import skill_cards as sc
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(sc, "_CARDS_PATH",
                               os.path.join(tmp.name, "cards.json")), \
                mock.patch.object(sc, "_PENDING_PATH",
                                  os.path.join(tmp.name, "pending.json")), \
                mock.patch.object(sc, "_delete_from_store") as dels:
            sc._save(sc._CARDS_PATH, {
                "sc_only": {"claim": "獨源卡",
                            "sources": [{"video_id": "vidA"}]},
                "sc_both": {"claim": "跨影片卡",
                            "sources": [{"video_id": "vidA"},
                                        {"video_id": "vidB"}]},
            })
            sc._save(sc._PENDING_PATH, {
                "sc_pend": {"claim": "待核獨源",
                            "sources": [{"video_id": "vidA"}]},
            })
            removed = sc.purge_video_cards("vidA")
            ledger = sc._load(sc._CARDS_PATH)
            pending = sc._load(sc._PENDING_PATH)
        self.assertEqual(removed, 2)
        self.assertNotIn("sc_only", ledger)
        self.assertIn("sc_both", ledger)                 # 跨影片佐證的卡保留
        self.assertEqual(pending, {})
        dels.assert_called_once_with("sc_only")          # pending 卡不在索引、不刪

    def test_empty_video_id(self):
        from agent_core import skill_cards as sc
        self.assertEqual(sc.purge_video_cards("  "), 0)


if __name__ == "__main__":
    unittest.main()
