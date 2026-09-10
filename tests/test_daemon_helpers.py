"""Unit tests for agent_core.daemon_helpers.

This module is the single source of truth for state locking, log rotation
and notification used by both agent_daemon.py and launchd/scripts/*.py.
A subtle bug here would silently corrupt daemon_state.json or spam emails,
so it's worth explicit coverage.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import daemon_helpers


def load_launchd_ponder():
    module_name = "_test_launchd_ponder"
    sys.modules.pop(module_name, None)
    path = os.path.join(_REPO_ROOT, "launchd", "scripts", "ponder.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TsTests(unittest.TestCase):
    def test_ts_matches_expected_format(self):
        self.assertRegex(daemon_helpers.ts(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class GetMyEmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(lambda: os.path.exists(self.tmp.name) and os.remove(self.tmp.name))

    def _patch_memory(self, payload):
        if payload is not None:
            self.tmp.write(json.dumps(payload))
            self.tmp.close()
        else:
            self.tmp.close()
            os.remove(self.tmp.name)
        return mock.patch.object(daemon_helpers, "MEMORY_FILE", self.tmp.name)

    def test_uses_user_email_from_memory(self):
        with self._patch_memory({"user_email": "boss@example.com"}):
            self.assertEqual(daemon_helpers.get_my_email(), "boss@example.com")

    def test_falls_back_when_memory_missing(self):
        with self._patch_memory(None):
            self.assertEqual(daemon_helpers.get_my_email(), daemon_helpers.FALLBACK_EMAIL)

    def test_falls_back_when_field_invalid(self):
        with self._patch_memory({"user_email": "not-an-email"}):
            self.assertEqual(daemon_helpers.get_my_email(), daemon_helpers.FALLBACK_EMAIL)

    def test_falls_back_when_file_unparseable(self):
        self.tmp.write("<<<not json>>>")
        self.tmp.close()
        with mock.patch.object(daemon_helpers, "MEMORY_FILE", self.tmp.name):
            self.assertEqual(daemon_helpers.get_my_email(), daemon_helpers.FALLBACK_EMAIL)


class UpdateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: self._rmdir(self.tmpdir))
        self.state = os.path.join(self.tmpdir, "state.json")
        self._patchers = [
            mock.patch.object(daemon_helpers, "STATE_FILE", self.state),
            mock.patch.object(daemon_helpers, "STATE_LOCK", self.state + ".lock"),
            mock.patch.object(daemon_helpers, "STATE_BAK", self.state + ".bak"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    @staticmethod
    def _rmdir(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def test_update_state_creates_file_on_first_write(self):
        daemon_helpers.update_state(lambda s: s.__setitem__("k", "v"))
        self.assertTrue(os.path.exists(self.state))
        with open(self.state) as f:
            self.assertEqual(json.load(f), {"k": "v"})

    def test_update_state_preserves_existing_keys(self):
        daemon_helpers.update_state(lambda s: s.__setitem__("a", 1))
        daemon_helpers.update_state(lambda s: s.__setitem__("b", 2))
        with open(self.state) as f:
            self.assertEqual(json.load(f), {"a": 1, "b": 2})

    def test_update_state_creates_bak_after_second_write(self):
        daemon_helpers.update_state(lambda s: s.__setitem__("a", 1))
        # first write: no .bak yet (copy2 only happens when STATE_FILE exists
        # before the write begins)
        self.assertFalse(os.path.exists(self.state + ".bak"))
        daemon_helpers.update_state(lambda s: s.__setitem__("b", 2))
        # second write should have copied the v1 state to .bak
        self.assertTrue(os.path.exists(self.state + ".bak"))
        with open(self.state + ".bak") as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_load_state_falls_back_to_bak_when_main_corrupt(self):
        daemon_helpers.update_state(lambda s: s.__setitem__("a", 1))
        daemon_helpers.update_state(lambda s: s.__setitem__("b", 2))
        # Corrupt main
        with open(self.state, "w") as f:
            f.write("<<<bad>>>")
        restored = daemon_helpers.load_state()
        # Should recover v1 from .bak (contains only "a": 1)
        self.assertEqual(restored, {"a": 1})

    def test_load_state_returns_empty_dict_when_nothing_exists(self):
        self.assertEqual(daemon_helpers.load_state(), {})

    def test_load_state_is_pure_read_no_write_no_lock(self):
        """健檢 Low：load_state 以前 = update_state(no-op)，每次「讀」都做
        .bak 全檔複製 + 全檔重寫 + 獨佔鎖。現在必須是純讀：不建檔、不碰
        .bak、不動 mtime。"""
        daemon_helpers.update_state(lambda s: s.__setitem__("k", "v"))
        # 清掉 update_state 可能留下的 lock 檔，驗證 load 不會重建
        for suffix in (".bak", ".lock"):
            p = self.state + suffix
            if os.path.exists(p):
                os.remove(p)
        before = os.stat(self.state).st_mtime_ns
        self.assertEqual(daemon_helpers.load_state(), {"k": "v"})
        self.assertEqual(os.stat(self.state).st_mtime_ns, before,
                         "load_state 不該重寫 state 檔")
        self.assertFalse(os.path.exists(self.state + ".bak"),
                         "load_state 不該產生 .bak")
        self.assertFalse(os.path.exists(self.state + ".lock"),
                         "load_state 不該開鎖檔")

    def test_load_state_does_not_create_missing_file(self):
        self.assertEqual(daemon_helpers.load_state(), {})
        self.assertFalse(os.path.exists(self.state),
                         "load_state 不該有「讀取會建檔」副作用")


class NotifyTests(unittest.TestCase):
    def test_notify_prepends_task_header_and_timestamp(self):
        calls = {}

        def fake_send_gmail(**kwargs):
            calls.update(kwargs)
            return "ok"

        # notify 統一走 send_gmail_internal（非 LLM 工具入口），才能一併帶
        # generated_by 出處標記；send_gmail 的簽名綁著 Gemini function
        # declaration 不能動。
        fake_gmail_mod = mock.MagicMock()
        fake_gmail_mod.send_gmail_internal = fake_send_gmail

        with mock.patch.dict(sys.modules, {"agent_core.gmail": fake_gmail_mod}), \
             mock.patch.object(daemon_helpers, "get_my_email", return_value="x@y.com"):
            ok = daemon_helpers.notify("subj", "body-text", "mytask")

        self.assertIs(ok, True)  # 寄送成功 → True（caller 才能決定記不記 dedup）
        self.assertEqual(calls["to"], "x@y.com")
        self.assertEqual(calls["subject"], "subj")
        self.assertIn("[daemon task: mytask]", calls["body"])
        self.assertIn("body-text", calls["body"])
        # Header should contain a valid timestamp
        self.assertRegex(calls["body"], r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    def test_notify_swallows_send_failure_and_returns_false(self):
        def boom(**_kwargs):
            raise RuntimeError("gmail down")

        fake_gmail_mod = mock.MagicMock()
        fake_gmail_mod.send_gmail_internal = boom

        with mock.patch.dict(sys.modules, {"agent_core.gmail": fake_gmail_mod}):
            # Must not raise; prints error internally — but MUST return False
            # （健檢 Medium：以前吞掉失敗回 None，mailcheck/ponder 以為送到了）
            self.assertIs(daemon_helpers.notify("s", "b", "t"), False)

    def test_notify_detects_string_style_send_failure(self):
        """gmail_ops.send_gmail 失敗時不 raise、回「發信失敗：...」字串 —
        notify 必須把它轉成 False，不能當成功。"""
        fake_gmail_mod = mock.MagicMock()
        fake_gmail_mod.send_gmail_internal = lambda **_kw: "發信失敗：HttpError 500"

        with mock.patch.dict(sys.modules, {"agent_core.gmail": fake_gmail_mod}), \
             mock.patch.object(daemon_helpers, "get_my_email", return_value="x@y.com"):
            self.assertIs(daemon_helpers.notify("s", "b", "t"), False)


class RotateLogTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: self._rmdir(self.tmpdir))
        self._patcher = mock.patch.object(daemon_helpers, "LOG_DIR", self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    @staticmethod
    def _rmdir(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def test_rotate_log_noop_when_small(self):
        path = os.path.join(self.tmpdir, "daemon-small.log")
        payload = "hello\n" * 100
        with open(path, "w") as f:
            f.write(payload)
        daemon_helpers.rotate_log("small", max_mb=2)
        with open(path) as f:
            self.assertEqual(f.read(), payload)

    def test_rotate_log_truncates_when_large(self):
        path = os.path.join(self.tmpdir, "daemon-big.log")
        # ~3 MB of distinguishable lines (each line ~28 bytes incl. newline)
        with open(path, "w") as f:
            for i in range(120_000):
                f.write(f"line-{i:06d}-padding-xx\n")
        size_before = os.path.getsize(path)
        self.assertGreater(size_before, 2 * 1024 * 1024)

        daemon_helpers.rotate_log("big", max_mb=2)

        size_after = os.path.getsize(path)
        self.assertLess(size_after, size_before)
        # Should keep ~1 MB of tail + header
        self.assertLess(size_after, 1.3 * 1024 * 1024)
        # Last line should still be present (tail preserved)
        with open(path) as f:
            content = f.read()
        self.assertIn("line-119999", content)
        # Rotation header should be present
        self.assertTrue(re.search(r"# \[rotate @", content))

    def test_rotate_log_missing_file_is_silent(self):
        # Must not raise when the log file doesn't exist yet
        daemon_helpers.rotate_log("never-existed", max_mb=2)


class LaunchdPonderWrapperTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("_test_launchd_ponder", None)

    def test_remember_wrapper_supplies_update_state(self):
        mod = load_launchd_ponder()
        calls = {}

        def fake_remember(seen_hashes, fresh, *, update_state):
            calls["seen_hashes"] = seen_hashes
            calls["fresh"] = fresh
            calls["update_state"] = update_state

        fake_update_state = object()
        with mock.patch.object(mod, "remember_ponder_insights", side_effect=fake_remember), \
             mock.patch.object(mod, "update_state", fake_update_state):
            mod._remember_ponder_insights({"old"}, ["new"])

        self.assertEqual(calls["seen_hashes"], {"old"})
        self.assertEqual(calls["fresh"], ["new"])
        self.assertIs(calls["update_state"], fake_update_state)


class RunWithDeadlineTests(unittest.TestCase):
    """run_with_deadline：單發 cron daemon 的整輪 wall-clock 看門狗。

    2026-06-15 email_ingest 卡 poll() 5.4h（per-request timeout 被半關閉 TCP
    漏接）→ StartInterval 不起新輪、郵件停止進 lake。鎖住：正常完成取消看門狗、
    fn 例外也取消、到期 dump 堆疊 + os._exit(75)=EX_TEMPFAIL 強退。
    """

    def test_returns_value_and_cancels_watchdog(self):
        with mock.patch.object(daemon_helpers.threading, "Timer") as MockTimer:
            timer = MockTimer.return_value
            result = daemon_helpers.run_with_deadline(
                lambda: 42, 1200, label="email_ingest"
            )
        self.assertEqual(result, 42)
        # 以設定的 deadline 武裝、啟動，且成功後取消
        self.assertEqual(MockTimer.call_args.args[0], 1200)
        timer.start.assert_called_once()
        timer.cancel.assert_called_once()

    def test_cancels_watchdog_even_when_fn_raises(self):
        def boom():
            raise ValueError("task blew up")

        with mock.patch.object(daemon_helpers.threading, "Timer") as MockTimer:
            timer = MockTimer.return_value
            with self.assertRaises(ValueError):
                daemon_helpers.run_with_deadline(boom, 1200)
        timer.cancel.assert_called_once()

    def test_deadline_exceeded_dumps_stack_then_hard_exits(self):
        # 到期動作：dump 全線程堆疊（定位卡點）後 os._exit(75)。mock os._exit
        # 才不會真的殺掉測試 process。
        with mock.patch.object(daemon_helpers.os, "_exit") as m_exit, \
             mock.patch.object(
                 daemon_helpers.faulthandler, "dump_traceback"
             ) as m_dump, \
             mock.patch.object(daemon_helpers.sys, "stderr"):
            daemon_helpers._deadline_exceeded("email_ingest", 1200)
        m_dump.assert_called_once()
        m_exit.assert_called_once_with(75)


class RunTaskWithDeadlineTests(unittest.TestCase):
    """run_task_with_deadline：可逐項中止的 per-task 看門狗（dispatcher 用）。

    與 run_with_deadline（到期 os._exit 整個 process）的關鍵差別：到期只放棄
    「這一項」、raise TaskDeadlineExceeded，**不殺 process** —— dispatcher 一輪裡
    某個慢任務卡死，不該連累同批其他正常任務。
    """

    def test_returns_value_when_under_deadline(self):
        self.assertEqual(
            daemon_helpers.run_task_with_deadline(lambda: 7, 5, label="t"), 7
        )

    def test_reraises_fn_exception_unchanged(self):
        # fn 自身的錯誤要原樣冒出去（走 dispatcher 既有的 mark_failed 路徑），
        # 不可被誤包成 timeout。
        def boom():
            raise ValueError("inner blew up")

        with self.assertRaises(ValueError):
            daemon_helpers.run_task_with_deadline(boom, 5, label="t")

    def test_subclasses_timeout_error_for_existing_except(self):
        # dispatcher 迴圈用 `except Exception` 接住失敗 → TaskDeadlineExceeded 必須
        # 落在 Exception 體系內，否則會逃出迴圈、炸掉整輪 dispatcher。
        self.assertTrue(issubclass(daemon_helpers.TaskDeadlineExceeded, TimeoutError))
        self.assertTrue(issubclass(daemon_helpers.TaskDeadlineExceeded, Exception))

    def test_times_out_raises_and_returns_fast(self):
        # Event gate（cleanup 才開）確定性模擬卡死：worker 在閘門前永遠 set 不了
        # done，done.wait(deadline) 保證到期。比賭 time.sleep 穩，CI 高載也不翻。
        gate = threading.Event()
        self.addCleanup(gate.set)

        t0 = time.time()
        with self.assertRaises(daemon_helpers.TaskDeadlineExceeded), \
                mock.patch.object(daemon_helpers.faulthandler, "dump_traceback"), \
                mock.patch.object(daemon_helpers.sys, "stderr"):
            daemon_helpers.run_task_with_deadline(
                lambda: gate.wait(10), 0.1, label="hang"
            )
        # 到期就放手，不等卡死的 worker 跑完。
        self.assertLess(time.time() - t0, 1.5)

    def test_timeout_does_not_kill_process_but_dumps_stack(self):
        # 逐任務看門狗到期：要 dump 堆疊留證、但絕不 os._exit —— 那會打斷同批其他
        # 任務。這正是它跟 run_with_deadline 的分界線。
        gate = threading.Event()
        self.addCleanup(gate.set)

        with mock.patch.object(daemon_helpers.os, "_exit") as m_exit, \
                mock.patch.object(
                    daemon_helpers.faulthandler, "dump_traceback"
                ) as m_dump, \
                mock.patch.object(daemon_helpers.sys, "stderr"):
            with self.assertRaises(daemon_helpers.TaskDeadlineExceeded):
                daemon_helpers.run_task_with_deadline(
                    lambda: gate.wait(10), 0.1, label="hang"
                )
        m_dump.assert_called_once()
        m_exit.assert_not_called()

    def test_abandoned_worker_is_daemon_thread(self):
        # 被放棄的 worker 必須是 daemon thread，否則它會卡住整個 process 退出
        # （單發 cron 永遠收不了工、堵住 launchd 下一輪）。
        gate = threading.Event()
        self.addCleanup(gate.set)

        with mock.patch.object(daemon_helpers.faulthandler, "dump_traceback"), \
                mock.patch.object(daemon_helpers.sys, "stderr"):
            with self.assertRaises(daemon_helpers.TaskDeadlineExceeded):
                daemon_helpers.run_task_with_deadline(
                    lambda: gate.wait(10), 0.1, label="daemoncheck"
                )
        leaked = [t for t in threading.enumerate() if t.name == "deadline:daemoncheck"]
        self.assertTrue(leaked, "找不到 leaked worker（名稱應為 deadline:daemoncheck）")
        self.assertTrue(all(t.daemon for t in leaked), "leaked worker 必須是 daemon thread")


class CronDeadlineCoverageTests(unittest.TestCase):
    """防漂移：打 Gemini/Google 的單發 cron daemon entry script 都必須有整輪
    wall-clock 看門狗（run_with_deadline）。少了它，某次呼叫漏接 per-request
    timeout 就會卡死整輪、堵住 launchd 下一輪（email_ingest 2026-06-15 卡 5.4h
    事故）。新增這類 cron daemon 時，把名字加進 GUARDED 並在 entry script 包上
    run_with_deadline，否則本測試會擋下。

    刻意未列入（不適用整輪 run_with_deadline）：
      - dispatcher（agent_daemon.py）：跑異質排程任務，整輪 hard-kill 會打斷整批。
        已改走「逐任務」wall-clock deadline（run_task_with_deadline，到期只放棄該
        項、不殺 process）；見 test_dispatcher_send_timeout.DispatcherPerTaskDeadlineTests。
      - rag_sync：合法跑 7-15h，整輪 cap 會誤殺；靠細粒度內部 timeout 保護。
      - telegram 等長駐 keepalive：由 daemon watchdog（active-heartbeat 停滯）兜底。
    """

    GUARDED = ("email_ingest", "mailcheck", "morning", "ponder",
               "briefing_15min", "sample_check",
               # 健檢 Medium：health_check 是斷線 bot 的自動救援者，chroma 半死
               # 時 col.count() 卡住會讓救援永久停擺 → 也要包整輪看門狗。
               "health_check")

    def test_guarded_cron_scripts_wrap_run_with_deadline(self):
        scripts_dir = os.path.join(_REPO_ROOT, "launchd", "scripts")
        missing = []
        for name in self.GUARDED:
            path = os.path.join(scripts_dir, f"{name}.py")
            self.assertTrue(os.path.isfile(path), f"找不到 {path}")
            with open(path, encoding="utf-8") as f:
                if "run_with_deadline" not in f.read():
                    missing.append(name)
        self.assertEqual(
            missing, [],
            f"這些單發 cron daemon 漏接整輪 wall-clock 看門狗 run_with_deadline：{missing}",
        )


class TransientNetworkRetryTests(unittest.TestCase):
    """暫時性網路錯誤判定＋退避重試（2026-08-03 morning DNS 瞬斷案）。"""

    def _classify(self, exc):
        from agent_core.daemon_helpers import is_transient_network_error
        return is_transient_network_error(exc)

    def test_httplib2_dns_error_is_transient(self):
        # ⚠️ 本案主角：ServerNotFoundError 的繼承鏈是 HttpLib2Error → Exception，
        # 不是 ConnectionError/OSError 子類 —— 只靠 isinstance 判定會漏掉它。
        import httplib2
        exc = httplib2.error.ServerNotFoundError(
            "Unable to find the server at www.googleapis.com")
        self.assertNotIsInstance(exc, (ConnectionError, OSError, TimeoutError))
        self.assertTrue(self._classify(exc))

    def test_socket_and_connection_errors_are_transient(self):
        import socket
        for exc in (socket.gaierror(8, "nodename nor servname provided"),
                    ConnectionResetError("Connection reset by peer"),
                    TimeoutError("timed out")):
            self.assertTrue(self._classify(exc), exc)

    def test_programming_and_permission_errors_are_not_transient(self):
        for exc in (ValueError("bad input"), KeyError("items"),
                    PermissionError("insufficient scope")):
            self.assertFalse(self._classify(exc), exc)

    def test_http_5xx_transient_but_4xx_not(self):
        def _http(status):
            resp = type("R", (), {"status": status})()
            return type("HttpError", (Exception,), {"resp": resp})("boom")
        for status in (429, 500, 502, 503, 504):
            self.assertTrue(self._classify(_http(status)), status)
        for status in (400, 401, 403, 404):
            self.assertFalse(self._classify(_http(status)), status)

    def test_retry_returns_value_without_sleeping_on_success(self):
        from agent_core.daemon_helpers import retry_on_transient_network
        sleeps = []
        out = retry_on_transient_network(lambda: "ok", sleep=sleeps.append)
        self.assertEqual(out, "ok")
        self.assertEqual(sleeps, [])

    def test_retry_recovers_and_backs_off_exponentially(self):
        from agent_core.daemon_helpers import retry_on_transient_network
        calls = {"n": 0}
        sleeps = []

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timed out")
            return "recovered"

        out = retry_on_transient_network(
            flaky, attempts=3, base_delay_s=2.0, sleep=sleeps.append)
        self.assertEqual(out, "recovered")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2.0, 4.0])       # 指數退避

    def test_non_transient_raises_immediately_without_retry(self):
        from agent_core.daemon_helpers import retry_on_transient_network
        calls = {"n": 0}
        sleeps = []

        def boom():
            calls["n"] += 1
            raise ValueError("bad input")

        with self.assertRaises(ValueError):
            retry_on_transient_network(boom, sleep=sleeps.append)
        self.assertEqual(calls["n"], 1)            # 只試一次
        self.assertEqual(sleeps, [])

    def test_exhausted_retries_raise_last_error(self):
        from agent_core.daemon_helpers import retry_on_transient_network
        calls = {"n": 0}
        sleeps = []

        def always_fail():
            calls["n"] += 1
            raise ConnectionResetError("Connection reset by peer")

        with self.assertRaises(ConnectionResetError):
            retry_on_transient_network(
                always_fail, attempts=3, sleep=sleeps.append)
        self.assertEqual(calls["n"], 3)            # 用滿 3 次
        self.assertEqual(len(sleeps), 2)           # 之間睡 2 次

    def test_attempts_one_means_no_retry(self):
        from agent_core.daemon_helpers import retry_on_transient_network
        calls = {"n": 0}

        def always_fail():
            calls["n"] += 1
            raise TimeoutError("timed out")

        with self.assertRaises(TimeoutError):
            retry_on_transient_network(always_fail, attempts=1, sleep=lambda _s: None)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
