"""rag_sync_health：夜間整理健康摘要工具的單元測試。

只讀 rag_sync_last_run.json + daemon-rag_sync.log、不碰 ChromaDB。鎖定狀態分級
（成功/進行中/久跑/失敗）、503 統計、各硬碟結果解析、缺檔不炸。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from agent_core import rag_sync_health as rh


def _run(last_run: dict, log_text: str = "") -> str:
    with tempfile.TemporaryDirectory() as d:
        logp = os.path.join(d, "rag.log")
        with open(logp, "w", encoding="utf-8") as f:
            f.write(log_text)
        with mock.patch("agent_core.ingest.sync_guard.read_last_run",
                        return_value=last_run), \
                mock.patch.object(rh, "_rag_log_path", return_value=logp):
            return rh.rag_sync_health()


class RagSyncHealthTests(unittest.TestCase):
    def test_success_clean_is_green(self):
        out = _run({"status": "success", "finished_at": time.time() - 3600},
                   "處理一些檔\n")
        self.assertIn("上次成功跑完", out)
        self.assertIn("無 503 過載", out)
        self.assertIn("🟢", out)

    def test_running_over_15h_is_red(self):
        out = _run({"status": "running", "started_at": time.time() - 16 * 3600}, "")
        self.assertIn("正在跑", out)
        self.assertIn("🔴", out)
        self.assertIn("可能卡住", out)

    def test_heavy_503_is_yellow(self):
        out = _run({"status": "running", "started_at": time.time() - 3600},
                   "503 UNAVAILABLE\n" * 300)
        self.assertIn("Gemini 過載", out)
        self.assertIn("約 300 次", out)
        self.assertIn("🟡", out)

    def test_failed_is_red(self):
        out = _run({"status": "failed", "finished_at": time.time() - 100}, "")
        self.assertIn("上次失敗", out)
        self.assertIn("🔴", out)

    def test_drive_results_parsed(self):
        log = ("[rag_sync] SharedDrive 0ABC123XYZ: {'drive_id': '0ABC123XYZ', "
               "'total': 100, 'synced': 5, 'skipped': 95, 'purged': 0, "
               "'listing_complete': True, 'skip_reasons': {'unchanged': 95}}\n")
        out = _run({"status": "success", "finished_at": time.time()}, log)
        self.assertIn("新整理 5 檔", out)
        self.assertIn("跳過 95", out)
        self.assertIn("✅", out)

    def test_incomplete_drive_marked(self):
        log = ("SharedDrive 0AD: {'synced': 1, 'skipped': 2, "
               "'listing_complete': False}\n")
        out = _run({"status": "running", "started_at": time.time() - 600}, log)
        self.assertIn("⏳", out)

    def test_connection_and_quota_failures_surfaced(self):
        log = ("Unable to find the server at www.googleapis.com\n" * 3
               + "RESOURCE_EXHAUSTED\n" * 2)
        out = _run({"status": "running", "started_at": time.time() - 600}, log)
        self.assertIn("連 Google 失敗：3 次", out)
        self.assertIn("配額/預付不足：2 次", out)

    def test_missing_files_no_crash(self):
        with mock.patch("agent_core.ingest.sync_guard.read_last_run", return_value={}), \
                mock.patch.object(rh, "_rag_log_path", return_value="/no/such/x.log"):
            out = rh.rag_sync_health()
        self.assertIn("夜間整理", out)  # 不炸、仍回摘要骨架

    def test_registered_as_safe_background_tool(self):
        # 必須在 dispatcher 白名單(背景任務才用得到)
        from agent_core.daemon_dispatcher import _SAFE_TOOL_NAMES
        self.assertIn("rag_sync_health", _SAFE_TOOL_NAMES)
        # 必須在 builtin 工具目錄
        from agent_core.tool_registry_catalog import BASE_BUILTIN_TOOLS
        names = {getattr(t, "__name__", "") for t in BASE_BUILTIN_TOOLS}
        self.assertIn("rag_sync_health", names)


if __name__ == "__main__":
    unittest.main()
