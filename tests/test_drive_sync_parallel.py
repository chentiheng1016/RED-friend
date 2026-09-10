"""Phase 1：_process_file_list 的 gated 平行路徑。

核心保證：worker 執行緒只跑 _prepare_file（讀/網路/CPU、各持自己的 service），
**所有 ChromaDB 寫（_commit_file）只在主執行緒** → ChromaDB 永遠單一 writer
（多 writer = HNSW 腐壞/SIGSEGV）。平行結果與序列完全等價（ex.map 保序），單檔
錯誤隔離不炸整批。預設 fetch_workers=1 = 純序列（見 test_drive_sync_shared_drive）。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from agent_core.ingest import drive_sync


def _files(n: int) -> list[dict]:
    return [{"id": f"f{i}", "name": f"n{i}", "modifiedTime": "t", "parents": ["p"]}
            for i in range(n)]


class ProcessFileListParallelTests(unittest.TestCase):
    def setUp(self):
        # 隔離 skip-state（平行只是要驗 thread 分工，不碰 marker 落盤）。
        self._defer = mock.patch.object(drive_sync, "_defer_skip_state_writes")
        self._defer.start()
        self.addCleanup(self._defer.stop)
        self._store = mock.patch.object(drive_sync, "get_store", return_value=mock.MagicMock())
        self._store.start()
        self.addCleanup(self._store.stop)

    def test_commits_on_main_thread_prepares_on_workers(self):
        main = threading.current_thread()
        prepare_threads: list = []
        commit_threads: list = []

        def fake_prepare(file_id, **kw):
            prepare_threads.append(threading.current_thread())
            return drive_sync._FilePlan(file_id, {"file_id": file_id, "chunks": 1},
                                        upsert=([f"{file_id}__c0"], ["d"], [{}], 1))

        def fake_commit(store, plan):
            commit_threads.append(threading.current_thread())
            return plan.result

        builder_calls: list = []

        def fake_builder():
            builder_calls.append(threading.current_thread())
            return mock.MagicMock()

        with mock.patch.object(drive_sync, "_prepare_file", side_effect=fake_prepare), \
                mock.patch.object(drive_sync, "_commit_file", side_effect=fake_commit):
            results = drive_sync._process_file_list(
                _files(8), folder_id_fn=lambda f: "F",
                prefetched={}, used_prefetch=False,
                service_builder=fake_builder, fetch_workers=4)

        # 單 writer：所有 commit 在主執行緒
        self.assertTrue(commit_threads and all(t is main for t in commit_threads),
                        "all _commit_file calls must run on the main thread")
        # prepare 確實跑在 worker（至少一個非主執行緒）
        self.assertTrue(any(t is not main for t in prepare_threads),
                        "_prepare_file should run on worker threads")
        # per-worker service：builder 呼叫數不超過 worker 數（thread-local 各建一次）
        self.assertLessEqual(len(builder_calls), 4)
        self.assertGreaterEqual(len(builder_calls), 1)
        self.assertTrue(all(t is not main for t in builder_calls),
                        "service_builder runs inside workers")
        # ex.map 保序 + 全處理
        self.assertEqual([r["file_id"] for r in results], [f"f{i}" for i in range(8)])

    def test_parallel_results_match_serial(self):
        files = _files(10)

        def fake_prepare(file_id, **kw):
            return drive_sync._FilePlan(file_id, {"file_id": file_id, "chunks": 1})

        with mock.patch.object(drive_sync, "_prepare_file", side_effect=fake_prepare), \
                mock.patch.object(drive_sync, "_commit_file", side_effect=lambda s, p: p.result), \
                mock.patch.object(drive_sync, "_sync_file_with_optional_prefetch",
                                  side_effect=lambda fid, **kw: {"file_id": fid, "chunks": 1}):
            serial = drive_sync._process_file_list(
                files, folder_id_fn=lambda f: "F", prefetched={}, used_prefetch=False,
                fetch_workers=1)
            parallel = drive_sync._process_file_list(
                files, folder_id_fn=lambda f: "F", prefetched={}, used_prefetch=False,
                service_builder=lambda: mock.MagicMock(), fetch_workers=4)
        self.assertEqual(serial, parallel)  # 保序 → 逐筆完全相同

    def test_worker_error_isolates_to_that_file(self):
        files = _files(5)

        def fake_prepare(file_id, **kw):
            if file_id == "f2":
                raise RuntimeError("boom")
            return drive_sync._FilePlan(file_id, {"file_id": file_id, "chunks": 1})

        with mock.patch.object(drive_sync, "_prepare_file", side_effect=fake_prepare), \
                mock.patch.object(drive_sync, "_commit_file", side_effect=lambda s, p: p.result):
            results = drive_sync._process_file_list(
                files, folder_id_fn=lambda f: "F", prefetched={}, used_prefetch=False,
                service_builder=lambda: mock.MagicMock(), fetch_workers=3)
        by_id = {r["file_id"]: r for r in results}
        self.assertIn("error: boom", by_id["f2"]["reason"])   # 壞檔變 error verdict
        self.assertEqual(by_id["f0"]["chunks"], 1)            # 其餘照常
        self.assertEqual(len(results), 5)

    def test_default_env_is_serial(self):
        # 不傳 fetch_workers/service_builder → 讀 RED_DRIVE_FETCH_WORKERS（預設 1）→ 序列
        calls: list = []
        with mock.patch.object(drive_sync, "_sync_file_with_optional_prefetch",
                               side_effect=lambda fid, **kw: calls.append(fid) or {"file_id": fid}), \
                mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("RED_DRIVE_FETCH_WORKERS", None)
            drive_sync._process_file_list(
                _files(3), folder_id_fn=lambda f: "F", prefetched={}, used_prefetch=False)
        self.assertEqual(calls, ["f0", "f1", "f2"])  # 序列、走原 sync_file 路徑


if __name__ == "__main__":
    unittest.main()
