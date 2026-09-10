"""Tests for the SIGTERM handler, Gemini-call timeout, and net-error
session reset added on top of PR #28.

Each one closes a real production failure mode the previous architecture
left open even after the pooled-session work:

- SIGTERM during message handling → reply lost, session leaked
- Gemini SDK hung indefinitely → user stares at no reply (heartbeat
  pulse keeps the watchdog alive, so the process never recovers either)
- Connection-pool corruption inside the cached Session → only escape was
  the full-process restart at 20 errors
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── SIGTERM handler ─────────────────────────────────────────────────

class SigtermHandlerTests(unittest.TestCase):
    def setUp(self):
        from agent_core import daemon_telegram
        daemon_telegram._reset_shutdown_state()
        self.addCleanup(daemon_telegram._reset_shutdown_state)

    def test_install_returns_true_when_signal_works(self):
        from agent_core import daemon_telegram
        with mock.patch("agent_core.daemon_telegram.signal.signal") as sig:
            ok = daemon_telegram._install_sigterm_handler()
        self.assertTrue(ok)
        sig.assert_called_once()
        # First positional must be SIGTERM specifically — install on
        # the wrong signal would silently fail to catch launchctl unload.
        self.assertEqual(sig.call_args.args[0], signal.SIGTERM)

    def test_install_returns_false_when_not_main_thread(self):
        """signal.signal raises ValueError off the main thread. Production
        always installs from the main thread, but tests in worker threads
        must not crash — return False instead."""
        from agent_core import daemon_telegram
        with mock.patch(
            "agent_core.daemon_telegram.signal.signal",
            side_effect=ValueError("only from main thread"),
        ):
            ok = daemon_telegram._install_sigterm_handler()
        self.assertFalse(ok)

    def test_handler_sets_shutdown_event(self):
        """The handler is what flips the flag; the loop checks the flag.
        Verify the registered callable mutates the right Event."""
        from agent_core import daemon_telegram
        captured: dict = {}

        def capture_handler(signum, handler):
            captured["handler"] = handler

        with mock.patch("agent_core.daemon_telegram.signal.signal",
                        side_effect=capture_handler):
            daemon_telegram._install_sigterm_handler()

        self.assertFalse(daemon_telegram._shutdown_requested.is_set())
        captured["handler"](signal.SIGTERM, None)
        self.assertTrue(daemon_telegram._shutdown_requested.is_set())

    def test_reset_shutdown_state_clears_flag(self):
        from agent_core import daemon_telegram
        daemon_telegram._shutdown_requested.set()
        daemon_telegram._reset_shutdown_state()
        self.assertFalse(daemon_telegram._shutdown_requested.is_set())

    def test_handler_interrupts_when_blocked_in_long_poll(self):
        """卡在長輪詢時，光設旗標不夠 —— 要當場打斷。

        2026-08-26 實測：旗標只在迴圈頂端被檢查，SIGTERM 打在輪詢中就得等它跑完
        （RED_TG_LONGPOLL_TIMEOUT_S 預設 8 秒），而 launchd 只給 5 秒就 SIGKILL。
        live fleet 三天內 7 次「did not exit 5 seconds after SIGTERM」。
        """
        from agent_core import daemon_telegram as dt
        dt._in_long_poll[0] = True
        with self.assertRaises(dt._ShutdownInterrupt):
            dt._handle_sigterm(signal.SIGTERM, None)
        self.assertTrue(dt._shutdown_requested.is_set(),
                        "打斷之外，關機旗標一樣要設起來")

    def test_handler_does_not_interrupt_while_handling_a_message(self):
        """不在輪詢中就只設旗標 —— 不准打斷處理到一半的訊息。

        這正是 handler 當初存在的理由：中途被砍會讓使用者的回覆整個掉。
        """
        from agent_core import daemon_telegram as dt
        dt._in_long_poll[0] = False
        dt._handle_sigterm(signal.SIGTERM, None)   # 不得 raise
        self.assertTrue(dt._shutdown_requested.is_set())

    def test_shutdown_interrupt_is_baseexception(self):
        """長輪詢外圈把所有例外當暫時性網路錯誤重試 —— 中斷例外若繼承
        Exception 就會被吞掉，關機又拖回原點。"""
        from agent_core import daemon_telegram as dt
        self.assertTrue(issubclass(dt._ShutdownInterrupt, BaseException))
        self.assertFalse(issubclass(dt._ShutdownInterrupt, Exception))

    def test_sleep_returns_immediately_when_already_shutting_down(self):
        from agent_core import daemon_telegram as dt
        dt._shutdown_requested.set()
        started = time.monotonic()
        woke_for_shutdown = dt._sleep_unless_shutdown(60)
        self.assertTrue(woke_for_shutdown)
        self.assertLess(time.monotonic() - started, 0.5,
                        "旗標已設就不該睡；60 秒退避會吃光 launchd 的 5 秒寬限")

    def test_sleep_serves_the_full_duration_when_not_shutting_down(self):
        """沒關機時要真的睡滿 —— 否則退避失效，網路一抖就變成打爆 Telegram。"""
        from agent_core import daemon_telegram as dt
        started = time.monotonic()
        woke_for_shutdown = dt._sleep_unless_shutdown(0.6)
        self.assertFalse(woke_for_shutdown)
        self.assertGreaterEqual(time.monotonic() - started, 0.55)

    def test_sleep_wakes_shortly_after_a_late_sigterm(self):
        """訊號打在退避中途也要醒 —— 這是 60 秒那一格的實際情境。"""
        from agent_core import daemon_telegram as dt
        timer = threading.Timer(0.2, dt._shutdown_requested.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        woke_for_shutdown = dt._sleep_unless_shutdown(30)
        elapsed = time.monotonic() - started
        self.assertTrue(woke_for_shutdown)
        self.assertLess(elapsed, 2.0, f"等了 {elapsed:.1f}s，超過 launchd 的 5 秒寬限就會被 SIGKILL")

    def test_reset_clears_long_poll_flag(self):
        from agent_core import daemon_telegram as dt
        dt._in_long_poll[0] = True
        dt._reset_shutdown_state()
        self.assertFalse(dt._in_long_poll[0],
                         "旗標沒清會讓下一個測試的 handler 亂丟例外")

    def test_env_parsers_fall_back_for_bad_values(self):
        from agent_core import daemon_telegram

        with mock.patch.dict(os.environ, {
            "RED_TG_FILE_UPLOAD_TIMEOUT_S": "soon",
            "RED_TG_CODE_RELOAD_CHECK_INTERVAL_S": "NaN",
        }):
            self.assertEqual(
                daemon_telegram._env_int(
                    "RED_TG_FILE_UPLOAD_TIMEOUT_S",
                    300,
                    min_value=1,
                    max_value=7200,
                ),
                300,
            )
            self.assertEqual(
                daemon_telegram._env_float(
                    "RED_TG_CODE_RELOAD_CHECK_INTERVAL_S",
                    30,
                    min_value=1,
                    max_value=3600,
                ),
                30,
            )


# ── Code reload fingerprint ─────────────────────────────────────────

class CodeReloadSnapshotTests(unittest.TestCase):
    def test_snapshot_changes_when_python_file_changes(self):
        from agent_core import daemon_telegram

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "worker.py"
            p.write_text("x = 1\n", encoding="utf-8")
            before = daemon_telegram._code_reload_snapshot([d])
            time.sleep(0.01)
            p.write_text("x = 2\n", encoding="utf-8")
            after = daemon_telegram._code_reload_snapshot([d])

        self.assertNotEqual(before, after)

    def test_snapshot_ignores_non_python_files(self):
        from agent_core import daemon_telegram

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "worker.txt"
            p.write_text("x = 1\n", encoding="utf-8")
            before = daemon_telegram._code_reload_snapshot([d])
            p.write_text("x = 2\n", encoding="utf-8")
            after = daemon_telegram._code_reload_snapshot([d])

        self.assertEqual(before, after)


# ── _send_message_with_timeout ──────────────────────────────────────

class SendMessageWithTimeoutTests(unittest.TestCase):
    def test_fast_call_returns_value(self):
        from agent_core import daemon_telegram
        chat = mock.MagicMock()
        chat.send_message.return_value = "ok"
        self.assertEqual(
            daemon_telegram._send_message_with_timeout(chat, "hi", timeout_s=1),
            "ok",
        )
        chat.send_message.assert_called_once_with("hi")

    def test_hung_call_raises_timeout_error(self):
        """Verify the wrapper aborts a wedged Gemini call. Without this
        the user waits forever and the daemon-thread pulse keeps the
        watchdog alive so process restart never kicks in."""
        from agent_core import daemon_telegram
        never_returns = threading.Event()
        self.addCleanup(never_returns.set)

        chat = mock.MagicMock()
        chat.send_message.side_effect = lambda *a, **k: never_returns.wait(10)

        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(chat, "hi", timeout_s=0.1)

    def test_inner_exception_propagates_unchanged(self):
        """A real bug in send_message must NOT be swallowed by the
        timeout helper — let the caller see the actual exception."""
        from agent_core import daemon_telegram
        chat = mock.MagicMock()
        chat.send_message.side_effect = ValueError("real bug")
        with self.assertRaises(ValueError) as cm:
            daemon_telegram._send_message_with_timeout(chat, "hi", timeout_s=1)
        self.assertEqual(str(cm.exception), "real bug")


# ── Net-error session reset threshold ───────────────────────────────

class NetErrorSessionResetThresholdTests(unittest.TestCase):
    """Pin the constants so a future refactor doesn't accidentally
    reorder them. RESET_AT < RESTART_AFTER is the whole point — without
    that ordering the session-reset path is unreachable."""

    def test_reset_threshold_below_restart_threshold(self):
        from agent_core import daemon_telegram
        self.assertLess(
            daemon_telegram._TG_RESET_SESSION_AT_NET_ERRORS,
            daemon_telegram._TG_RESTART_AFTER_NET_ERRORS,
            "Session reset must be reachable before the full process "
            "restart kicks in — otherwise the cheap recovery path is dead "
            "code.",
        )

    def test_thresholds_are_positive_integers(self):
        from agent_core import daemon_telegram
        self.assertIsInstance(
            daemon_telegram._TG_RESET_SESSION_AT_NET_ERRORS, int)
        self.assertIsInstance(
            daemon_telegram._TG_RESTART_AFTER_NET_ERRORS, int)
        self.assertGreater(
            daemon_telegram._TG_RESET_SESSION_AT_NET_ERRORS, 0)


# ── Self-restart must exit non-zero (KeepAlive SuccessfulExit:false) ─

class _FakeRequestsExceptions:
    class ReadTimeout(Exception):
        pass

    class ConnectionError(Exception):
        pass

    class RequestException(Exception):
        pass

    class Timeout(Exception):
        pass


class _NetDownRequests:
    """無 .Session 屬性 → _get_tg_session 原樣回傳；每次 get 都斷線。"""

    exceptions = _FakeRequestsExceptions

    def get(self, *a, **k):
        raise _FakeRequestsExceptions.ConnectionError("net down")


class _OneMessageRequests:
    """回一則授權訊息的長輪詢 stub。"""

    exceptions = _FakeRequestsExceptions

    class _Resp:
        def json(self):
            return {"ok": True, "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 999, "type": "private"},
                    "from": {"id": 999},
                    "text": "hi",
                },
            }]}

    def get(self, *a, **k):
        return self._Resp()


class _SigtermDuringPollRequests:
    """長輪詢進行中收到 SIGTERM。

    直接呼叫真正的 handler —— production 在呼叫 session.get 前已經把
    _in_long_poll 開起來，所以 handler 會丟 _ShutdownInterrupt 打斷這次輪詢。
    用真 handler 而不是自己 raise，是為了讓「旗標有沒有被正確圍住」也一起受測。
    """

    exceptions = _FakeRequestsExceptions

    class _Resp:
        def json(self):
            return {"ok": True, "result": []}

    def __init__(self, dt):
        self.dt = dt
        self.calls = 0
        self.poll_ran_to_completion = False

    def get(self, *a, **k):
        self.calls += 1
        # 模擬訊號在阻塞的 socket read 途中送達：CPython 會在那裡跑 handler。
        self.dt._handle_sigterm(signal.SIGTERM, None)
        # 走到這行＝handler 沒打斷這次呼叫。真實世界裡這代表輪詢會把剩下的
        # 秒數跑完才回應 —— 也就是超過 launchd 5 秒寬限的那條路。
        self.poll_ran_to_completion = True
        return self._Resp()


class SelfRestartExitCodeTests(unittest.TestCase):
    """健檢 High：全部 telegram plist 是 KeepAlive={Crashed:true,
    SuccessfulExit:false} — 自主重啟路徑（每 30 則訊息防記憶體累積 / 連續
    網路錯誤）以前 `return`（exit 0）→ launchd **不會** respawn，bot 斷線
    最長 30 分鐘等 health_check 撿屍。必須非零退出（sys.exit(75)，對齊
    run_with_deadline 慣例），且 SystemExit 不能被主迴圈 except Exception 吞掉。
    """

    _OWNER = {"chat_id": "999", "color": "red", "is_owner": "true",
              "name": "Red owner", "email": "", "source": "telegram-chat-id"}

    def setUp(self):
        import shutil
        from agent_core import daemon_telegram as dt
        import agent_core.daemon_watchdog as wd
        self.dt = dt
        dt._reset_shutdown_state()
        self.addCleanup(dt._reset_shutdown_state)
        self.tmp = tempfile.mkdtemp(prefix="tg_exit_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # heartbeat 檔導到 tmp（不污染 live var/state）
        p = mock.patch.object(wd, "STATE_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def _common_patches(self, requests_stub):
        dt = self.dt
        return [
            mock.patch.object(dt, "tg_get_token_and_chat", return_value=("tok", "999")),
            mock.patch.object(dt, "_telegram_inbound_actors", return_value={"999": dict(self._OWNER)}),
            mock.patch.object(dt, "_telegram_approval_owner_chat_id", return_value=""),
            mock.patch.object(dt, "_telegram_default_actor_log_summary", return_value=""),
            mock.patch.object(dt, "_code_reload_snapshot", return_value=(0, 0, 0)),
            mock.patch.object(dt, "_install_sigterm_handler", return_value=True),
            # 不起 in-process watchdog 執行緒（sleep 被 patch 後會空轉）
            mock.patch.object(dt.threading, "Thread"),
            mock.patch.object(dt.time, "sleep"),
            # 退避睡眠現在走 _sleep_unless_shutdown（短睡＋讀旗標）。只 patch
            # time.sleep 不夠：它內部還會用 time.monotonic 算剩餘時間，sleep 被
            # 空掉就變成真實秒數的忙迴圈。
            mock.patch.object(dt, "_sleep_unless_shutdown", lambda *a, **k: False),
            mock.patch.object(dt, "_telegram_audit_log", lambda **k: None),
        ]

    def _run_bot(self, requests_stub):
        return self.dt.task_telegram_bot(
            agent_persona="p",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=lambda: None,
            agent_types_factory=lambda: None,
            load_state=lambda: {},
            update_state=lambda mutate: None,
            requests_module=requests_stub,
        )

    def test_net_error_restart_exits_75(self):
        dt = self.dt
        stub = _NetDownRequests()
        patches = self._common_patches(stub) + [
            mock.patch.object(dt, "_TG_RESTART_AFTER_NET_ERRORS", 5),
        ]
        with self._nested(patches):
            with self.assertRaises(SystemExit) as cm:
                self._run_bot(stub)
        self.assertEqual(cm.exception.code, 75)

    def test_message_counter_restart_exits_75(self):
        dt = self.dt
        stub = _OneMessageRequests()
        patches = self._common_patches(stub) + [
            mock.patch.object(dt, "_TG_RESTART_AFTER_MSGS", 1),
            mock.patch.object(dt, "tg_handle_message", return_value="ok"),
            mock.patch.object(dt, "tg_send", return_value=True),
            mock.patch("agent_core.tg_auth.check_message_rate_limit",
                       return_value=(True, 0.0, 1)),
        ]
        with self._nested(patches):
            with self.assertRaises(SystemExit) as cm:
                self._run_bot(stub)
        self.assertEqual(cm.exception.code, 75)

    def test_net_error_backoff_aborts_on_shutdown_instead_of_sleeping_it_out(self):
        """退避睡眠必須走 _sleep_unless_shutdown —— #441 只顧到長輪詢。

        主迴圈第二種等待是網路錯誤退避 min(5+n, 60)，**最長 60 秒**，而 launchd
        只給 5 秒。退避不是罕見狀態：live log 裡 `🌐 網路暫時問題` 一整片。
        這裡讓退避一被呼叫就設關機旗標，然後斷言 bot 乾淨返回；若有人把它改回
        time.sleep，spy 不會被呼叫、旗標永遠不設，迴圈會一路錯到重啟門檻而
        SystemExit(75)，測試就紅。
        """
        dt = self.dt
        stub = _NetDownRequests()
        calls = []

        def _spy(seconds):
            calls.append(seconds)
            dt._shutdown_requested.set()
            return True

        patches = [p for p in self._common_patches(stub)
                   if getattr(p, "attribute", "") != "_sleep_unless_shutdown"]
        patches += [
            mock.patch.object(dt, "_sleep_unless_shutdown", _spy),
            mock.patch.object(dt, "_TG_RESTART_AFTER_NET_ERRORS", 5),
            mock.patch.object(dt, "_reset_shutdown_state", lambda: None),
        ]
        with self._nested(patches):
            result = self._run_bot(stub)      # 不該 raise SystemExit(75)
        self.assertIsNone(result)
        self.assertTrue(calls, "退避沒有走 _sleep_unless_shutdown")

    def test_sigterm_shutdown_still_exits_cleanly(self):
        """SIGTERM 的刻意停機仍是 clean exit（launchctl unload 不該被 respawn）。"""
        dt = self.dt
        stub = _NetDownRequests()
        patches = self._common_patches(stub) + [
            mock.patch.object(dt, "_reset_shutdown_state", lambda: None),
        ]
        dt._shutdown_requested.set()
        with self._nested(patches):
            result = self._run_bot(stub)  # 不 raise SystemExit
        self.assertIsNone(result)

    def test_sigterm_during_long_poll_shuts_down_on_the_first_poll(self):
        """SIGTERM 打在輪詢中 → 當場打斷那次呼叫，而不是等輪詢自己跑完。

        `poll_ran_to_completion is False` 是這裡唯一能分辨新舊行為的斷言：
        測試裡的輪詢不會真的阻塞 8 秒，所以「有沒有乾淨關機」「跑了幾次輪詢」
        兩種行為都一樣，只有「那次 get 有沒有被中途打斷」看得出差別。

        另外注意迴圈底部有個廣捕 `except Exception as exc` —— 中斷例外若繼承
        Exception 就會被它吞掉、退回原本的等待行為，所以才用 BaseException。
        """
        dt = self.dt
        stub = _SigtermDuringPollRequests(dt)
        with self._nested(self._common_patches(stub)):
            result = self._run_bot(stub)
        self.assertFalse(
            stub.poll_ran_to_completion,
            "SIGTERM 沒打斷長輪詢 —— 真實環境會拖到 launchd 5 秒寬限之外被 SIGKILL",
        )
        self.assertIsNone(result, "刻意停機要 clean exit，不能是 SystemExit")
        self.assertEqual(stub.calls, 1, "第一次輪詢就該收工")

    def _nested(self, patches):
        from contextlib import ExitStack

        stack = ExitStack()
        for p in patches:
            stack.enter_context(p)
        return stack


class StartupHeartbeatResetTests(unittest.TestCase):
    """健檢 High 2a：bot 啟動要先把 heartbeat 檔覆寫成 idle。

    TelegramHeartbeat 建構不寫檔（第一次寫要等 begin()）：前一個 process 在
    active 中被殺留下的殘檔會讓看門狗每 15 分鐘誤殺剛重啟的健康 bot，直到
    下一則訊息才覆寫。啟動即 idle() 讓殘檔立刻失效（state=idle + 新 pid）。
    """

    def test_startup_overwrites_stale_active_heartbeat(self):
        import json
        import shutil
        from agent_core import daemon_telegram as dt
        import agent_core.daemon_watchdog as wd

        tmp = tempfile.mkdtemp(prefix="tg_hb_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        dt._reset_shutdown_state()
        self.addCleanup(dt._reset_shutdown_state)

        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("RED_TELEGRAM_STATE_SUFFIX", None)

        # 模擬上一個 process 的殘留 active heartbeat（舊 pid）
        wd.write_telegram_heartbeat("red", state="active", task="wedged",
                                    state_dir=tmp, now=time.time() - 9999)
        hb_path = wd.telegram_heartbeat_path("red", state_dir=tmp)
        with open(hb_path, encoding="utf-8") as f:
            stale = json.load(f)
        stale["pid"] = 424242
        with open(hb_path, "w", encoding="utf-8") as f:
            json.dump(stale, f)

        owner = {"chat_id": "999", "color": "red", "is_owner": "true",
                 "name": "Red owner", "email": "", "source": "telegram-chat-id"}
        dt._shutdown_requested.set()  # 進主迴圈第一圈就退出
        with mock.patch.object(wd, "STATE_DIR", tmp), \
                mock.patch.object(dt, "tg_get_token_and_chat", return_value=("tok", "999")), \
                mock.patch.object(dt, "_telegram_inbound_actors", return_value={"999": owner}), \
                mock.patch.object(dt, "_telegram_approval_owner_chat_id", return_value=""), \
                mock.patch.object(dt, "_telegram_default_actor_log_summary", return_value=""), \
                mock.patch.object(dt, "_code_reload_snapshot", return_value=(0, 0, 0)), \
                mock.patch.object(dt, "_install_sigterm_handler", return_value=True), \
                mock.patch.object(dt, "_reset_shutdown_state", lambda: None), \
                mock.patch.object(dt.threading, "Thread"):
            dt.task_telegram_bot(
                agent_persona="p",
                tools_list=[],
                gemini_model="m",
                agent_client_factory=lambda: None,
                agent_types_factory=lambda: None,
                load_state=lambda: {},
                update_state=lambda mutate: None,
                requests_module=_NetDownRequests(),
            )

        with open(hb_path, encoding="utf-8") as f:
            hb = json.load(f)
        self.assertEqual(hb["state"], "idle", "啟動必須把殘留 active 覆寫成 idle")
        self.assertEqual(hb["pid"], os.getpid(), "覆寫後 pid 應是現任 process")


# ── Gemini timeout fallback in tg_handle_message ────────────────────

class GeminiTimeoutFallbackTests(unittest.TestCase):
    """End-to-end: when send_message times out, tg_handle_message must
    return a friendly user-facing message AND reset chat_state so the
    NEXT message rebuilds a fresh session (the timed-out one might be
    in a bad state)."""

    def _run_handler_with_timeout(self):
        from agent_core import daemon_telegram
        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 5,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        with mock.patch.object(
            daemon_telegram, "_send_message_with_timeout",
            side_effect=TimeoutError("hung"),
        ):
            reply = daemon_telegram.tg_handle_message(
                user_text="anything",
                chat_id="0",
                agent_persona="x",
                tools_list=[],
                gemini_model="m",
                agent_client_factory=lambda: mock.MagicMock(),
                agent_types_factory=lambda: mock.MagicMock(),
                chat_state=chat_state,
            )
        return reply, chat_state

    def test_timeout_returns_warning_and_resets_chat_state(self):
        reply, chat_state = self._run_handler_with_timeout()
        # Surfaces the timeout in human terms.
        self.assertIn("超過", reply)
        self.assertIn("背景動作可能仍在進行", reply)
        # Chat state cleared so next message rebuilds. Otherwise we'd keep
        # slamming the same wedged Gemini chat object.
        self.assertIsNone(chat_state["chat"])
        self.assertEqual(chat_state["turns"], 0)

    def test_timeout_message_does_not_encourage_blind_retry(self):
        """Codex finding on PR #29: the daemon thread is still alive after
        TimeoutError raises, so the SDK call CAN finish in the background.
        If the user blindly retries a non-idempotent action (send_gmail,
        run_shell, browser_*), the same side effect can fire twice.

        Pin that the message warns against retry on side-effecting tools
        instead of saying 'please retry' / '請再說一次'."""
        reply, _ = self._run_handler_with_timeout()
        # Anti-retry phrasing: must NOT contain words that nudge the user
        # to immediately re-issue the same command.
        for forbidden in ("再說一次", "再試一次", "重新嘗試", "please retry"):
            self.assertNotIn(
                forbidden, reply,
                f"Reply contains '{forbidden}' which nudges blind retry "
                f"of potentially side-effecting actions: {reply!r}",
            )
        # Pro-verify phrasing: must explicitly call out side-effecting
        # tools and tell the user to verify before retrying them.
        self.assertIn("確認", reply)         # "先到對應地方確認"
        self.assertIn("send_gmail", reply)  # name of side-effecting tool
        self.assertIn("副作用", reply)


# ── Direct media-download fast-path ─────────────────────────────────

class TelegramMediaDownloadFastPathTests(unittest.TestCase):
    def setUp(self):
        from agent_core import tg_auth
        tg_auth._confirm_state.clear()
        tg_auth._dangerous_confirm_state.clear()
        tg_auth._confirm_history.clear()
        tg_auth._lockout_until.clear()

    def _base_kwargs(self, chat_state):
        return dict(
            chat_id="9990000001",
            agent_persona="x",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=lambda: mock.MagicMock(),
            agent_types_factory=lambda: mock.MagicMock(),
            chat_state=chat_state,
        )

    def test_video_url_download_request_asks_confirmation_without_gemini(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        with mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message(
                "https://www.instagram.com/reel/DYDAU67g9KG/ 幫我下載影片",
                **self._base_kwargs(chat_state),
            )

        self.assertIn("回覆 `c`", reply)
        self.assertIn("DYDAU67g9KG", reply)
        self.assertIn(daemon_telegram._TG_PENDING_MEDIA_DOWNLOAD_KEY, chat_state)
        send.assert_not_called()

    def test_video_url_can_touch_chinese_download_phrase(self):
        from agent_core import daemon_telegram

        url = daemon_telegram._extract_direct_media_download_url(
            "https://www.youtube.com/watch?v=abc123&start_radio=1幫我下載影片"
        )

        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123&start_radio=1")

    def test_youtube_audio_download_request_asks_confirmation_without_gemini(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        with mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message(
                "https://www.youtube.com/watch?v=abc123&start_radio=1幫我下載聲音檔",
                **self._base_kwargs(chat_state),
            )

        pending = chat_state[daemon_telegram._TG_PENDING_MEDIA_DOWNLOAD_KEY]
        self.assertIn("回覆 `c`", reply)
        self.assertEqual(pending["url"], "https://www.youtube.com/watch?v=abc123&start_radio=1")
        self.assertEqual(pending["tool"], "download_youtube_audio")
        self.assertEqual(pending["args"], {"url": "https://www.youtube.com/watch?v=abc123&start_radio=1"})
        send.assert_not_called()

    def test_unsupported_piracy_source_is_rejected_without_confirmation_or_gemini(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        with mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message(
                "https://jable.tv/videos/fns-191/1幫我下載影片",
                **self._base_kwargs(chat_state),
            )

        self.assertIn("不能下載", reply)
        self.assertIn("yt-dlp", reply)
        self.assertIn("piracy", reply)
        self.assertNotIn(daemon_telegram._TG_PENDING_MEDIA_DOWNLOAD_KEY, chat_state)
        send.assert_not_called()

    def test_protected_streaming_url_is_rejected_without_gemini(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        with mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message(
                "https://www.netflix.com/watch/82018723?trackId=14170286",
                **self._base_kwargs(chat_state),
            )

        self.assertIn("Netflix", reply)
        self.assertIn("DRM", reply)
        self.assertIn("不能", reply)
        self.assertIn(daemon_telegram._TG_PROTECTED_MEDIA_URL_KEY, chat_state)
        self.assertNotIn(daemon_telegram._TG_PENDING_MEDIA_DOWNLOAD_KEY, chat_state)
        send.assert_not_called()

    def test_followup_download_after_protected_url_is_rejected_without_gemini(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        with mock.patch.object(daemon_telegram, "_send_message_with_timeout"):
            daemon_telegram.tg_handle_message(
                "https://www.netflix.com/watch/82018723",
                **self._base_kwargs(chat_state),
            )

        with mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message(
                "幫我下載影片",
                **self._base_kwargs(chat_state),
            )

        self.assertIn("Netflix", reply)
        self.assertIn("不能", reply)
        send.assert_not_called()

    def test_confirmation_runs_pending_download_without_gemini(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        daemon_telegram.tg_handle_message(
            "https://www.instagram.com/reel/DYDAU67g9KG/ 幫我下載影片",
            **self._base_kwargs(chat_state),
        )

        with mock.patch("agent_core.tool_runner.call_tool", return_value="OK downloaded") as dl, \
             mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message("c", **self._base_kwargs(chat_state))

        self.assertEqual(reply, "OK downloaded")
        dl.assert_called_once()
        self.assertEqual(dl.call_args.args[0], "download_online_video")
        self.assertEqual(dl.call_args.args[1], {"url": "https://www.instagram.com/reel/DYDAU67g9KG/"})
        self.assertEqual(dl.call_args.kwargs["context"]["caller"], "telegram_media_fastpath")
        self.assertNotIn(daemon_telegram._TG_PENDING_MEDIA_DOWNLOAD_KEY, chat_state)
        send.assert_not_called()

    def test_confirmation_runs_pending_youtube_audio_without_gemini(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        daemon_telegram.tg_handle_message(
            "https://www.youtube.com/watch?v=abc123&start_radio=1幫我下載聲音檔",
            **self._base_kwargs(chat_state),
        )

        with mock.patch("agent_core.tool_runner.call_tool", return_value="OK audio") as dl, \
             mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message("c", **self._base_kwargs(chat_state))

        self.assertEqual(reply, "OK audio")
        dl.assert_called_once()
        self.assertEqual(dl.call_args.args[0], "download_youtube_audio")
        self.assertEqual(dl.call_args.args[1], {"url": "https://www.youtube.com/watch?v=abc123&start_radio=1"})
        self.assertEqual(dl.call_args.kwargs["context"]["caller"], "telegram_media_fastpath")
        self.assertNotIn(daemon_telegram._TG_PENDING_MEDIA_DOWNLOAD_KEY, chat_state)
        send.assert_not_called()

    def test_m3u8_download_fastpath_uses_n_m3u8dl_with_user_agent(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        daemon_telegram.tg_handle_message(
            "https://cdn.example.test/master.m3u8 幫我下載影片\nUA=Mozilla/5.0 Test-UA",
            **self._base_kwargs(chat_state),
        )

        with mock.patch("agent_core.tool_runner.call_tool", return_value="OK hls") as dl, \
             mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message("c", **self._base_kwargs(chat_state))

        self.assertEqual(reply, "OK hls")
        dl.assert_called_once()
        self.assertEqual(dl.call_args.args[0], "download_hls_with_n_m3u8dl")
        self.assertEqual(
            dl.call_args.args[1],
            {
                "manifest_url": "https://cdn.example.test/master.m3u8",
                "user_agent": "Mozilla/5.0 Test-UA",
            },
        )
        send.assert_not_called()

    def test_m3u8_download_fastpath_honors_ffmpeg_engine_hint(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        daemon_telegram.tg_handle_message(
            "用 ffmpeg 下載 https://cdn.example.test/master.m3u8 幫我下載影片",
            **self._base_kwargs(chat_state),
        )

        with mock.patch("agent_core.tool_runner.call_tool", return_value="OK hls") as dl, \
             mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message("c", **self._base_kwargs(chat_state))

        self.assertEqual(reply, "OK hls")
        self.assertEqual(dl.call_args.args[0], "download_hls_with_ffmpeg_copy")
        self.assertEqual(
            dl.call_args.args[1],
            {"manifest_url": "https://cdn.example.test/master.m3u8"},
        )
        send.assert_not_called()

    def test_fastpath_pulses_heartbeat_while_media_tool_runs(self):
        from agent_core import daemon_telegram

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        daemon_telegram.tg_handle_message(
            "https://www.youtube.com/watch?v=abc123 幫我下載影片",
            **self._base_kwargs(chat_state),
        )
        pulses = []

        def slow_call(*args, **kwargs):
            time.sleep(0.05)
            return "OK downloaded"

        with mock.patch("agent_core.tool_runner.call_tool", side_effect=slow_call), \
             mock.patch.object(daemon_telegram, "_send_message_with_timeout"):
            reply = daemon_telegram.tg_handle_message(
                "c",
                heartbeat_touch=lambda: pulses.append(time.time()),
                heartbeat_interval_s=0.01,
                **self._base_kwargs(chat_state),
            )

        self.assertEqual(reply, "OK downloaded")
        self.assertGreaterEqual(len(pulses), 3)

    def test_confirmation_uploads_downloaded_artifacts_to_same_chat(self):
        from agent_core import daemon_telegram
        from agent_core.tool_result import ToolResult

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        daemon_telegram.tg_handle_message(
            "https://www.youtube.com/watch?v=abc123 幫我下載影片",
            **self._base_kwargs(chat_state),
        )

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake-video")
            video_path = f.name
        video_name = os.path.basename(video_path)
        download_result = ToolResult.success(
            f"✅ 已下載 YouTube 影片：Demo\n檔案：\n1. {video_path}",
            artifacts=[video_path],
        )
        upload_result = ToolResult.success(f"✅ 已傳送 {video_name} (1KB) 到 Telegram")
        try:
            with mock.patch(
                "agent_core.tool_runner.call_tool",
                side_effect=[download_result, upload_result],
            ) as runner, \
                 mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
                reply = daemon_telegram.tg_handle_message("c", **self._base_kwargs(chat_state))
        finally:
            if os.path.exists(video_path):
                os.unlink(video_path)

        self.assertIn("✅ 已下載 YouTube 影片：Demo", reply)
        self.assertIn("📤 Telegram 回傳", reply)
        self.assertIn(f"✅ 已傳送 {video_name}", reply)
        self.assertIn("已刪除本機下載檔", reply)
        self.assertFalse(os.path.exists(video_path))
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0], "download_online_video")
        self.assertEqual(runner.call_args_list[1].args[0], "telegram_send_file")
        self.assertEqual(
            runner.call_args_list[1].args[1],
            {
                "file_path": video_path,
                "caption": f"小紅下載好的影片：{video_name}",
                "chat_id": "9990000001",
            },
        )
        self.assertEqual(
            runner.call_args_list[1].kwargs["context"]["caller"],
            "telegram_media_fastpath_delivery",
        )
        send.assert_not_called()

    def test_confirmation_uploads_youtube_audio_artifact_to_same_chat(self):
        from agent_core import daemon_telegram
        from agent_core.tool_result import ToolResult

        chat_state = {
            "chat": mock.MagicMock(),
            "turns": 0,
            "last_msg_ts": 0.0,
            "work_mode": "normal",
        }
        daemon_telegram.tg_handle_message(
            "https://www.youtube.com/watch?v=abc123 幫我下載聲音檔",
            **self._base_kwargs(chat_state),
        )

        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
            f.write(b"fake-audio")
            audio_path = f.name
        audio_name = os.path.basename(audio_path)
        download_result = (
            "✅ 已下載並轉檔 YouTube 音訊：Demo\n"
            "模式：podcast\n"
            "檔案：\n"
            f"1. {audio_path}（123.4 秒）"
        )
        upload_result = ToolResult.success(f"✅ 已傳送 {audio_name} (1KB) 到 Telegram")
        try:
            with mock.patch(
                "agent_core.tool_runner.call_tool",
                side_effect=[download_result, upload_result],
            ) as runner, \
                 mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
                reply = daemon_telegram.tg_handle_message("c", **self._base_kwargs(chat_state))
        finally:
            if os.path.exists(audio_path):
                os.unlink(audio_path)

        self.assertIn("✅ 已下載並轉檔 YouTube 音訊：Demo", reply)
        self.assertIn("📤 Telegram 回傳", reply)
        self.assertIn(f"✅ 已傳送 {audio_name}", reply)
        self.assertIn("已刪除本機下載檔", reply)
        self.assertFalse(os.path.exists(audio_path))
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0], "download_youtube_audio")
        self.assertEqual(runner.call_args_list[1].args[0], "telegram_send_file")
        self.assertEqual(
            runner.call_args_list[1].args[1],
            {
                "file_path": audio_path,
                "caption": f"小紅下載好的音訊：{audio_name}",
                "chat_id": "9990000001",
            },
        )
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
