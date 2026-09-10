"""adjudicate_sop_conflicts 腳本：錨點定位、ffprobe 失敗防護、out.json 原子寫。
Drive / Gemini / chroma / ffmpeg 全 mock。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import scripts.adjudicate_sop_conflicts as adj


class NeedleAnchorTests(unittest.TestCase):
    def test_find_anchor_prefers_nearest_preceding_timestamp(self):
        text = "【00:10】前言\n【01:00】在 P4TF_560 建立出櫃申請單\n【02:00】收尾"
        self.assertEqual(adj.find_anchor([text], ["P4TF_560"]), 60)

    def test_find_anchor_none_when_absent(self):
        self.assertIsNone(adj.find_anchor(["【00:10】沒有那個值"], ["FTE_570"]))


class PersistOutTests(unittest.TestCase):
    def test_persist_out_atomic_valid_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_path = os.path.join(tmp.name, "out.json")
        with mock.patch.object(adj, "OUT", out_path):
            adj._persist_out([{"vid": "v1", "verdict": "unclear"}])
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data[0]["vid"], "v1")
        # 原子寫（tmp+rename）不留半截暫存檔
        leftovers = [fn for fn in os.listdir(tmp.name) if fn != "out.json"]
        self.assertEqual(leftovers, [])


class FfprobeFailureGuardTests(unittest.TestCase):
    """ffprobe 讀不到片長（duration<=0）：全片兜底抽幀會退化成 1fps 全片
    （幾千張幀塞單一 request）— 必須記 unclear 跳過、一張幀都不抽。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        results_path = os.path.join(self._tmp.name, "conflicts.json")
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump([{
                "vid": "v1", "name": "收櫃.mp4",
                "value_conflicts": [
                    {"item": "數量", "old": "120", "new": "150"},
                    {"item": "畫面", "old": "FTE_570", "new": "P4TF_560"},
                ],
            }], f, ensure_ascii=False)
        self.out_path = os.path.join(self._tmp.name, "out.json")
        for name, value in (("RESULTS", results_path),
                            ("SNAP", self._tmp.name),
                            ("OUT", self.out_path)):
            p = mock.patch.object(adj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_zero_duration_records_unclear_and_skips_frames(self):
        store = mock.MagicMock()
        col = mock.MagicMock()
        store._col = col
        col.get.return_value = {"documents": [], "metadatas": []}
        with mock.patch("agent_core.ingest.vector_store.get_store",
                        return_value=store), \
                mock.patch("agent_core.erp._download_drive_file",
                           return_value=True), \
                mock.patch("agent_core.video_understanding._drive_file_meta",
                           return_value={"mimeType": "video/mp4"}), \
                mock.patch(
                    "agent_core.video_understanding._ffprobe_duration_seconds",
                    return_value=None), \
                mock.patch.object(adj, "extract_frames") as frames, \
                mock.patch.object(adj, "adjudicate") as judge:
            adj.main(set())
        frames.assert_not_called()       # 一張幀都不抽
        judge.assert_not_called()        # 不燒模型費用
        with open(self.out_path, encoding="utf-8") as f:
            out = json.load(f)
        self.assertEqual(len(out), 2)    # 兩處衝突都有記錄（續跑不重試）
        for rec in out:
            self.assertEqual(rec["verdict"], "unclear")
            self.assertIn("片長", rec["evidence"])


if __name__ == "__main__":
    unittest.main()
