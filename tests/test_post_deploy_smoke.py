"""Tests for post-deploy smoke result recording."""
from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_spec = importlib.util.spec_from_file_location(
    "post_deploy_smoke",
    os.path.join(_REPO_ROOT, "scripts", "post_deploy_smoke.py"),
)
post_deploy_smoke = importlib.util.module_from_spec(_spec)
sys.modules["post_deploy_smoke"] = post_deploy_smoke
_spec.loader.exec_module(post_deploy_smoke)


class CheckHealthSeverityTests(unittest.TestCase):
    """check_health：🔵 info 放行、只有 🔴 crit / 🟡 warn 才讓 smoke 紅（修 6/6 假性 FAILED）。"""

    def _ok_for(self, report):
        from unittest import mock
        with mock.patch("agent_core.health.health_check", return_value=report):
            return post_deploy_smoke.check_health().ok

    def test_clean_report_passes(self):
        self.assertTrue(self._ok_for("  ✅ 一切正常，沒有發現問題"))

    def test_info_only_passes(self):
        # 真實案例：chroma log 大小（🔵、將自動輪替）不該讓 smoke 紅
        self.assertTrue(self._ok_for(
            "發現 1 個問題：\n  1. 🔵 [log/daemon-chroma.log] 日誌檔 23.2 MB（將自動輪替）"))

    def test_warn_fails(self):
        self.assertFalse(self._ok_for("發現 1 個問題：\n  1. 🟡 某個警告"))

    def test_crit_fails(self):
        self.assertFalse(self._ok_for("發現 1 個問題：\n  1. 🔴 某個嚴重問題"))


class PostDeploySmokeLogTests(unittest.TestCase):
    def setUp(self):
        # main() 會 setdefault RED_CHROMA_HTTP_URL（對齊 launchd plist 行為）——
        # 測試程序是長壽的，這裡快照/還原，避免漏給套件裡其他測試。
        self._saved_chroma_url = os.environ.get("RED_CHROMA_HTTP_URL")

    def tearDown(self):
        if self._saved_chroma_url is None:
            os.environ.pop("RED_CHROMA_HTTP_URL", None)
        else:
            os.environ["RED_CHROMA_HTTP_URL"] = self._saved_chroma_url

    def _stub_unskippable_checks(self):
        """把 main() 無法用 --skip 跳過的三個 check 換成罐頭結果；回傳還原 callback。"""
        originals = (
            post_deploy_smoke.check_health,
            post_deploy_smoke.check_daemons,
            post_deploy_smoke.check_anti_hallucination,
        )
        post_deploy_smoke.check_health = lambda: post_deploy_smoke.SmokeResult(
            "health_check", True, "ok"
        )
        post_deploy_smoke.check_daemons = lambda: post_deploy_smoke.SmokeResult(
            "red_status_daemons", True, "ok"
        )
        post_deploy_smoke.check_anti_hallucination = lambda: post_deploy_smoke.SmokeResult(
            "anti_hallucination_canary", True, "ok"
        )

        def restore():
            (
                post_deploy_smoke.check_health,
                post_deploy_smoke.check_daemons,
                post_deploy_smoke.check_anti_hallucination,
            ) = originals

        return restore

    def test_main_defaults_chroma_url_to_shared_server_definition(self):
        """沒 export RED_CHROMA_HTTP_URL 的 shell 跑 smoke，要補上與 plist
        注入（bin/inject-plist-env）同一個共用 server 位址，否則 chroma_backend
        防護會拒開 PersistentClient、health_check 誤紅（2026-06-12 誤報）。"""
        from agent_core.chroma_backend import SHARED_SERVER_URL

        os.environ.pop("RED_CHROMA_HTTP_URL", None)
        restore = self._stub_unskippable_checks()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = post_deploy_smoke.main([
                    "--skip-tool-rpc",
                    "--skip-google",
                    "--skip-telegram",
                    "--skip-node",
                    "--no-log",
                ])
        finally:
            restore()

        self.assertEqual(rc, 0)
        self.assertEqual(os.environ.get("RED_CHROMA_HTTP_URL"), SHARED_SERVER_URL)

    def test_main_respects_exported_chroma_url(self):
        os.environ["RED_CHROMA_HTTP_URL"] = "http://example.test:9999"
        restore = self._stub_unskippable_checks()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                post_deploy_smoke.main([
                    "--skip-tool-rpc",
                    "--skip-google",
                    "--skip-telegram",
                    "--skip-node",
                    "--no-log",
                ])
        finally:
            restore()

        self.assertEqual(os.environ["RED_CHROMA_HTTP_URL"], "http://example.test:9999")

    def test_write_run_log_records_summary_and_step_results(self):
        with tempfile.TemporaryDirectory(prefix="red_smoke_log_") as tmp:
            results = [
                post_deploy_smoke.SmokeResult("health_check", True, "ok"),
                post_deploy_smoke.SmokeResult("tool_rpc", False, "down"),
            ]

            path = post_deploy_smoke._write_run_log(
                results,
                ok=False,
                log_dir=tmp,
            )

            self.assertEqual(path.parent, Path(tmp))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertIn("timestamp", payload)
            self.assertEqual(payload["results"][0]["name"], "health_check")
            self.assertEqual(payload["results"][1]["detail"], "down")

    def test_failure_message_includes_log_path_and_failed_steps(self):
        results = [
            post_deploy_smoke.SmokeResult("health_check", True, "ok"),
            post_deploy_smoke.SmokeResult("tool_rpc", False, "socket down"),
        ]

        message = post_deploy_smoke._failure_message(
            results,
            Path("/tmp/post_deploy_smoke.json"),
        )

        self.assertIn("FAILED", message)
        self.assertIn("/tmp/post_deploy_smoke.json", message)
        self.assertIn("tool_rpc", message)
        self.assertIn("socket down", message)

    def test_failure_message_includes_system_load_context(self):
        # 失敗通知附系統負載脈絡，讓 CPU 飢餓 vs 真失敗一眼可辨
        # （2026-06-14 tool_rpc 被鄰居 session 的 orphan yes 餓死整機事故）。
        # mock 掉 _system_load_context：測「失敗訊息會附上脈絡」這個拼接邏輯，
        # 而非賭執行測試的主機剛好有 os.getloadavg / 非零 load（跨平台確定性）。
        from unittest import mock

        results = [
            post_deploy_smoke.SmokeResult("tool_rpc", False, "tool worker timeout"),
        ]
        with mock.patch.object(
            post_deploy_smoke,
            "_system_load_context",
            return_value="load avg: 88.0 85.0 79.0 (1/5/15min)",
        ):
            message = post_deploy_smoke._failure_message(results, None)
        self.assertIn("System at failure", message)
        self.assertIn("load avg: 88.0", message)

    def test_main_writes_log_without_changing_json_stdout_shape(self):
        with tempfile.TemporaryDirectory(prefix="red_smoke_log_") as tmp:
            original_health = post_deploy_smoke.check_health
            original_daemons = post_deploy_smoke.check_daemons
            try:
                post_deploy_smoke.check_health = lambda: post_deploy_smoke.SmokeResult(
                    "health_check", True, "ok"
                )
                post_deploy_smoke.check_daemons = lambda: post_deploy_smoke.SmokeResult(
                    "red_status_daemons", True, "ok"
                )

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc = post_deploy_smoke.main([
                        "--json",
                        "--skip-tool-rpc",
                        "--skip-google",
                        "--skip-telegram",
                        "--skip-node",
                        "--log-dir",
                        tmp,
                    ])
            finally:
                post_deploy_smoke.check_health = original_health
                post_deploy_smoke.check_daemons = original_daemons

            self.assertEqual(rc, 0)
            logs = list(Path(tmp).glob("post_deploy_smoke-*.json"))
            self.assertEqual(len(logs), 1)
            self.assertIsInstance(json.loads(stdout.getvalue()), list)

    def test_main_sends_failure_notification_when_enabled(self):
        with tempfile.TemporaryDirectory(prefix="red_smoke_log_") as tmp:
            original_health = post_deploy_smoke.check_health
            original_daemons = post_deploy_smoke.check_daemons
            original_telegram = post_deploy_smoke.check_telegram
            original_notify = post_deploy_smoke._notify_failure
            notifications: list[tuple[list, Path | None]] = []
            try:
                post_deploy_smoke.check_health = lambda: post_deploy_smoke.SmokeResult(
                    "health_check", False, "bad"
                )
                post_deploy_smoke.check_daemons = lambda: post_deploy_smoke.SmokeResult(
                    "red_status_daemons", True, "ok"
                )
                post_deploy_smoke.check_telegram = lambda message: post_deploy_smoke.SmokeResult(
                    "telegram_push", True, "mocked"
                )
                post_deploy_smoke._notify_failure = lambda results, path: notifications.append(
                    (results, path)
                ) or "sent"

                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    rc = post_deploy_smoke.main([
                        "--skip-tool-rpc",
                        "--skip-google",
                        "--skip-node",
                        "--log-dir",
                        tmp,
                    ])
            finally:
                post_deploy_smoke.check_health = original_health
                post_deploy_smoke.check_daemons = original_daemons
                post_deploy_smoke.check_telegram = original_telegram
                post_deploy_smoke._notify_failure = original_notify

            self.assertEqual(rc, 1)
            self.assertEqual(len(notifications), 1)
            self.assertTrue(notifications[0][1])
            self.assertIn("failure notification", stderr.getvalue())


_REACHABLE = "🟢 tool_rpc reachable\n  worker : pid=42 elapsed=0.1s"
_UNREACHABLE = (
    "🔴 tool_rpc unreachable\n"
    "  socket: /tmp/tool_rpc.sock\n"
    "  error : ❌ tool worker timeout after 15s"
)


class _FakeClock:
    """Deterministic monotonic clock advanced only by sleep() — lets the grace
    loop be driven without real time or real socket I/O."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class ToolRpcWarmupGraceTests(unittest.TestCase):
    """tool_rpc 冷啟動暖機 >15s，consolidated smoke 緊接著跑會誤判剛重啟的
    tool_rpc 為 unreachable。check_tool_rpc 要在 grace window 內重試，撐過才算真失敗。"""

    def test_first_try_success_does_not_retry(self):
        clock = _FakeClock()
        result = post_deploy_smoke._probe_tool_rpc_with_grace(
            status_fn=lambda: _REACHABLE,
            grace_sec=45.0,
            backoff_sec=2.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.name, "tool_rpc")
        self.assertEqual(clock.sleeps, [])  # healthy immediately → no backoff sleeps
        self.assertNotIn("暖機", result.detail)

    def test_recovers_after_warmup_within_grace(self):
        statuses = iter([_UNREACHABLE, _UNREACHABLE, _REACHABLE])
        clock = _FakeClock()
        result = post_deploy_smoke._probe_tool_rpc_with_grace(
            status_fn=lambda: next(statuses),
            grace_sec=45.0,
            backoff_sec=2.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertTrue(result.ok)
        self.assertIn("暖機後第 3 次探測通過", result.detail)
        self.assertEqual(len(clock.sleeps), 2)  # slept between the 3 probes

    def test_genuine_fail_after_grace_window_exhausted(self):
        clock = _FakeClock()
        result = post_deploy_smoke._probe_tool_rpc_with_grace(
            status_fn=lambda: _UNREACHABLE,
            grace_sec=10.0,
            backoff_sec=2.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.detail)
        self.assertIn("判定真失敗", result.detail)
        self.assertIn("tool worker timeout after 15s", result.detail)  # last status kept
        # must have actually retried, not behaved single-shot
        self.assertGreaterEqual(len(clock.sleeps), 2)

    def test_grace_zero_is_single_shot(self):
        clock = _FakeClock()
        result = post_deploy_smoke._probe_tool_rpc_with_grace(
            status_fn=lambda: _UNREACHABLE,
            grace_sec=0.0,
            backoff_sec=2.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertFalse(result.ok)
        self.assertEqual(clock.sleeps, [])  # no grace → probe once, fail

    def test_check_tool_rpc_wires_probe_through(self):
        """Production no-arg entry: reads env grace, calls the real probe helper
        against a stubbed status_fn. Healthy-first-try so it never really sleeps."""
        original = post_deploy_smoke._tool_rpc_status_text
        try:
            post_deploy_smoke._tool_rpc_status_text = lambda: _REACHABLE
            result = post_deploy_smoke.check_tool_rpc()
        finally:
            post_deploy_smoke._tool_rpc_status_text = original
        self.assertTrue(result.ok)
        self.assertEqual(result.name, "tool_rpc")


if __name__ == "__main__":
    unittest.main()
