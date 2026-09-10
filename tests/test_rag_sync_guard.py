"""rag_sync 部署誤觸發守門（sync_guard）的行為合約。

KeepAlive={SuccessfulExit=>false} 隱含 RunAtLoad → 每次 redeploy 都會拉起
launchd/scripts/rag_sync.py。守門必須擋掉「窗口外 + 上次成功還新鮮」的
啟動，但絕不能擋：03:00 排程窗口、失敗重試（KeepAlive 的本意）、中途被
殺（status=running）、無記錄首跑、RAG_SYNC_FORCE 逃生口。
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import plistlib
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.ingest.sync_guard import (  # noqa: E402
    LAST_RUN_BASENAME,
    read_last_run,
    should_skip_start,
    write_last_run,
)

_ENV_KEYS = ("RAG_SYNC_FORCE", "RAG_SYNC_SCHEDULE_HOUR", "RAG_SYNC_FRESH_HOURS")


def _local_ts(hour: int, minute: int = 0) -> float:
    """組出「今天本地時間 hour:minute」的 epoch — 跟測試機時區無關。"""
    lt = time.localtime()
    return time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1)
    )


class ShouldSkipStartTests(unittest.TestCase):
    def setUp(self):
        # make test-quiet 走 unittest discover，conftest 的 pytest fixture
        # 不會生效 — 環境隔離必須放在 setUp/tearDown。
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    tearDown = setUp

    def test_deploy_start_with_fresh_success_skips(self):
        # 2026-06-10 實況：13:00 左右成功收工，22:30 redeploy 又被拉起
        now = _local_ts(22, 30)
        state = {"status": "success", "finished_at": now - 9.5 * 3600}
        skip, reason = should_skip_start(now=now, state=state)
        self.assertTrue(skip)
        self.assertIn("跳過", reason)

    def test_schedule_window_runs_even_when_fresh(self):
        # 03:00 排程：距上次成功 ~14h < 16h，必須靠窗口放行
        now = _local_ts(3, 0)
        state = {"status": "success", "finished_at": now - 14 * 3600}
        skip, reason = should_skip_start(now=now, state=state)
        self.assertFalse(skip)
        self.assertIn("窗口", reason)

    def test_window_edges(self):
        state_fresh = {"status": "success"}
        for hour, minute, expect_window in (
            (2, 50, True),   # 窗口前緣
            (3, 45, True),   # 窗口後緣
            (2, 49, False),  # 剛好在窗口外
            (3, 46, False),
        ):
            now = _local_ts(hour, minute)
            state = dict(state_fresh, finished_at=now - 1 * 3600)
            skip, _ = should_skip_start(now=now, state=state)
            self.assertEqual(
                skip, not expect_window, f"{hour:02d}:{minute:02d} 窗口判定錯誤"
            )

    def test_midnight_window_wraparound(self):
        os.environ["RAG_SYNC_SCHEDULE_HOUR"] = "0"
        now = _local_ts(23, 55)  # 00:00 窗口的前 10 分鐘，跨午夜
        state = {"status": "success", "finished_at": now - 1 * 3600}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)

    def test_schedule_hour_env_moves_window(self):
        # 2026-07-04 事故：plist 排程改 01:00 但守門窗口留在預設 03:00 →
        # 每天 01:00 的正常 calendar fire 被當 deploy 誤觸發跳過。
        # 設了 RAG_SYNC_SCHEDULE_HOUR=1 後 01:00 必須放行、03:00 反過來要擋。
        os.environ["RAG_SYNC_SCHEDULE_HOUR"] = "1"

        def state_at(now):
            return {"status": "success", "finished_at": now - 14 * 3600}

        now = _local_ts(1, 0)
        skip, reason = should_skip_start(now=now, state=state_at(now))
        self.assertFalse(skip)
        self.assertIn("窗口", reason)

        now = _local_ts(3, 0)  # 窗口已搬走，03:00 變成窗口外 + 新鮮 → 跳過
        skip, _ = should_skip_start(now=now, state=state_at(now))
        self.assertTrue(skip)

    def test_failed_state_runs(self):
        # KeepAlive 失敗重試：上次 failed → 即使窗口外也要跑
        now = _local_ts(5, 30)
        state = {"status": "failed", "finished_at": now - 0.5 * 3600}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)

    def test_running_state_runs(self):
        # SIGTERM/斷電後狀態停在 running → 視同失敗重試
        now = _local_ts(12, 0)
        state = {"status": "running", "started_at": now - 2 * 3600}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)

    def test_no_state_runs(self):
        skip, _ = should_skip_start(now=_local_ts(22, 30), state={})
        self.assertFalse(skip)

    def test_stale_success_runs(self):
        # 睡過 03:00、清晨補打的 calendar fire：距上次成功 19.5h ≥ 16h
        now = _local_ts(8, 30)
        state = {"status": "success", "finished_at": now - 19.5 * 3600}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)

    def test_future_finished_at_runs(self):
        now = _local_ts(22, 30)
        state = {"status": "success", "finished_at": now + 3600}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)

    def test_missing_finished_at_runs(self):
        now = _local_ts(22, 30)
        state = {"status": "success"}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)

    def test_force_env_overrides_skip(self):
        os.environ["RAG_SYNC_FORCE"] = "1"
        now = _local_ts(22, 30)
        state = {"status": "success", "finished_at": now - 1 * 3600}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)

    def test_fresh_hours_env_tunable(self):
        os.environ["RAG_SYNC_FRESH_HOURS"] = "4"
        now = _local_ts(22, 30)
        state = {"status": "success", "finished_at": now - 9.5 * 3600}
        skip, _ = should_skip_start(now=now, state=state)
        self.assertFalse(skip)  # 9.5h ≥ 4h → 不算新鮮，照常執行


class PlistTemplateContractTests(unittest.TestCase):
    """plist 排程時刻 ↔ sync_guard 窗口的防漂移合約。

    2026-07-04 事故：StartCalendarInterval 改 01:00、EnvironmentVariables 沒跟著
    注入 RAG_SYNC_SCHEDULE_HOUR（守門窗口留在預設 03:00）→ 每天 01:00 的正常
    排程被 guard 跳過，夜跑退化成靠「上次成功 ≥16h」後門每 ~36h 才跑一輪。
    """

    def test_schedule_hour_env_matches_calendar_interval(self):
        path = os.path.join(
            _REPO_ROOT, "launchd", "templates", "com.xiaohong.rag_sync_daily.plist"
        )
        with open(path, "rb") as f:
            data = plistlib.load(f)
        calendar_hour = data["StartCalendarInterval"]["Hour"]
        env = data.get("EnvironmentVariables", {})
        self.assertIn(
            "RAG_SYNC_SCHEDULE_HOUR", env,
            "plist 一定要注入 RAG_SYNC_SCHEDULE_HOUR，讓守門窗口跟排程時刻一致",
        )
        self.assertEqual(
            int(env["RAG_SYNC_SCHEDULE_HOUR"]), calendar_hour,
            "RAG_SYNC_SCHEDULE_HOUR 必須等於 StartCalendarInterval.Hour（兩處要一起改）",
        )


class LastRunStateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_reads_empty(self):
        self.assertEqual(read_last_run(self.state_dir), {})

    def test_corrupt_file_reads_empty(self):
        path = os.path.join(self.state_dir, LAST_RUN_BASENAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(read_last_run(self.state_dir), {})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        self.assertEqual(read_last_run(self.state_dir), {})

    def test_running_then_success_preserves_started_at(self):
        write_last_run("running", self.state_dir)
        st = read_last_run(self.state_dir)
        self.assertEqual(st["status"], "running")
        self.assertIn("started_at", st)
        started = st["started_at"]

        write_last_run("success", self.state_dir, errors=2)
        st = read_last_run(self.state_dir)
        self.assertEqual(st["status"], "success")
        self.assertEqual(st["errors"], 2)
        self.assertEqual(st["started_at"], started)
        self.assertIn("finished_at", st)


class LaunchdWrapperTests(unittest.TestCase):
    """launchd 薄殼 main() 的守門/狀態流轉 — 全程 mock，不碰真實 FS/同步。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(_REPO_ROOT, "launchd", "scripts", "rag_sync.py")
        spec = importlib.util.spec_from_file_location("rag_sync_launchd_test", path)
        cls.wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.wrapper)

    def setUp(self):
        self.wrapper.rotate_log = mock.Mock()
        self.writes = []
        self._patches = [
            mock.patch(
                "agent_core.ingest.sync_guard.write_last_run",
                side_effect=lambda status, *a, **k: self.writes.append(status),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _stub_runner(self, run_sync, lock_held=False):
        return mock.patch.dict(
            sys.modules,
            {
                "agent_core.ingest.rag_runner": types.SimpleNamespace(
                    run_sync=run_sync, sync_lock_is_held=lambda: lock_held
                )
            },
        )

    def test_skip_path_never_touches_runner(self):
        boom = mock.Mock(side_effect=AssertionError("不該跑 run_sync"))
        with mock.patch(
            "agent_core.ingest.sync_guard.should_skip_start",
            return_value=(True, "測試跳過"),
        ), self._stub_runner(boom):
            self.wrapper.main()
        boom.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_success_path_writes_running_then_success(self):
        with mock.patch(
            "agent_core.ingest.sync_guard.should_skip_start",
            return_value=(False, "測試放行"),
        ), self._stub_runner(lambda: {"errors": ["x"], "locked": False}):
            self.wrapper.main()
        self.assertEqual(self.writes, ["running", "success"])

    def test_failure_path_writes_failed_and_reraises(self):
        def _boom():
            raise ValueError("sync 爆炸")

        with mock.patch(
            "agent_core.ingest.sync_guard.should_skip_start",
            return_value=(False, "測試放行"),
        ), self._stub_runner(_boom):
            with self.assertRaises(ValueError):
                self.wrapper.main()
        self.assertEqual(self.writes, ["running", "failed"])

    def test_locked_result_does_not_claim_success(self):
        # 探測說鎖空但 run_sync 仍撞鎖（極小 TOCTOU 窗口）→ 回到舊行為：
        # 蓋了 running 但不冒領 success。
        with mock.patch(
            "agent_core.ingest.sync_guard.should_skip_start",
            return_value=(False, "測試放行"),
        ), self._stub_runner(lambda: {"errors": [], "locked": True}):
            self.wrapper.main()
        self.assertEqual(self.writes, ["running"])

    def test_lock_already_held_skips_running_stamp(self):
        # 2026-07-04 診斷教訓：鎖被手動 FORCE 補跑持有時，launchd instance
        # 不可先蓋 running/started_at 覆寫持鎖那輪的真實起跑時間。
        with mock.patch(
            "agent_core.ingest.sync_guard.should_skip_start",
            return_value=(False, "測試放行"),
        ), self._stub_runner(
            lambda: {"errors": [], "locked": True}, lock_held=True
        ):
            self.wrapper.main()
        self.assertEqual(self.writes, [])

    def test_run_sync_wrapped_in_wall_clock_deadline(self):
        """大王指示：01:00 起跑、最多跑 6 小時強制停止——run_sync 必須真的
        包進 run_with_deadline，不能只是改個註解。"""
        run_sync = mock.Mock(return_value={"errors": [], "locked": False})
        captured = {}

        def fake_deadline(fn, deadline_s, *, label=""):
            captured["fn"] = fn
            captured["deadline_s"] = deadline_s
            captured["label"] = label
            return fn()

        with mock.patch(
            "agent_core.ingest.sync_guard.should_skip_start",
            return_value=(False, "測試放行"),
        ), self._stub_runner(run_sync), mock.patch.object(
            self.wrapper, "run_with_deadline", side_effect=fake_deadline
        ):
            self.wrapper.main()

        self.assertIs(captured["fn"], run_sync)
        self.assertEqual(captured["label"], "rag_sync_daily")
        self.assertEqual(captured["deadline_s"], self.wrapper._RAG_SYNC_DEADLINE_S)
        run_sync.assert_called_once()

    def test_default_deadline_is_six_hours(self):
        self.assertEqual(self.wrapper._RAG_SYNC_DEADLINE_S, 21600)


class SyncLockProbeTests(unittest.TestCase):
    """sync_lock_is_held 探測要正確反映持鎖狀態，且探測本身不留鎖。"""

    def test_probe_reflects_lock_state(self):
        from agent_core.ingest import rag_runner

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("agent_core.logging_and_paths.STATE_DIR", tmp):
                self.assertFalse(rag_runner.sync_lock_is_held())
                holder = open(
                    os.path.join(tmp, "rag_sync.lock"), "a+", encoding="utf-8"
                )
                try:
                    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertTrue(rag_runner.sync_lock_is_held())
                finally:
                    holder.close()  # close 即釋放 flock
                self.assertFalse(rag_runner.sync_lock_is_held())


if __name__ == "__main__":
    unittest.main()
