"""Regression tests for the 2026-06-12/13 Telegram bot permanent-hang incident.

That night a production-quantity query ran multiple Drive/Gmail searches,
logged 「📖 讀郵件 …」 at 23:38:20, then never produced another line: the
inference worker thread wedged forever inside an unbounded wait (Google-side
503 storm left the connections in CLOSE_WAIT), the 180s watchdog only WARNED
the user without abandoning anything, and every later long task queued behind
the wedged shared state.

Fixes pinned here:
  1. Overall task deadline (RED_TG_TASK_DEADLINE_S, default 900s): a monitor
     thread watches the runner after the soft timeout and, at the deadline,
     tells the user the task was abandoned ("請重問") — the watchdog now has
     teeth instead of being notify-only.
  2. The result wait is a timeout-bounded Event wait, and a runner-thread
     exception after the soft timeout is reported to the waiter side via
     late_notify instead of dying silently.
  3. A late result (finishes after soft timeout but before the deadline) is
     delivered to the user instead of dropped.
  4. The deadline tool gate makes every tool call of an abandoned runner fail
     immediately, so the leaked thread converges and the worker keeps serving
     new tasks (the core regression: blocked tool must not wedge the bot).

Repo rules respected: no hardcoded absolute repo path (sys.path comes from
this file's location), all isolation in setUp/tearDown (unittest discover —
conftest autouse fixtures do NOT apply), and no mock.patch performed inside
worker threads (fakes are plain objects built on the main thread).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    """Poll until predicate() is true or timeout — keeps thread-timing tests
    deterministic without fixed sleeps sized for the slowest CI box."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return bool(predicate())


class _BlockingChat:
    """Fake Gemini chat whose send_message blocks like the wedged AFC loop
    did in the incident (tool stuck on a dead Google connection)."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def send_message(self, wrapped):
        self.calls += 1
        # Bounded so a broken test can't leak a thread for longer than this.
        self.release.wait(30)
        return None


class _GatedChat:
    """Fake Gemini chat whose send_message blocks until the test opens the
    gate, then returns a configured result (or raises). Replaces a fixed
    ``time.sleep`` so the soft timeout fires DETERMINISTICALLY: the worker
    thread cannot set ``done`` until the test calls ``release.set()`` (which it
    does only AFTER asserting the TimeoutError), so ``done.wait(timeout_s)`` is
    guaranteed to expire first. A sleep-vs-timeout ratio only shrinks the race
    window — a loaded CI box still loses it (2026-06-14 flaky-test fix for the
    intermittent `AssertionError: TimeoutError not raised`)."""

    def __init__(self, *, result=None, exc=None):
        self.release = threading.Event()
        self._result = result
        self._exc = exc

    def send_message(self, wrapped):
        # Bounded so a broken test can't leak this thread for longer.
        self.release.wait(30)
        if self._exc is not None:
            raise self._exc
        return self._result


# ── Requirement 4: blocked tool → deadline fires → worker serves next task ──

class BlockedTaskWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            "RED_TG_INFERENCE_TIMEOUT_S": "0.05",
            "RED_TG_TASK_DEADLINE_S": "0.3",
        })
        env.start()
        self.addCleanup(env.stop)
        self.notifications: list[str] = []
        self.blocked_chat = _BlockingChat()
        self.addCleanup(self.blocked_chat.release.set)

    def _handle(self, text, chat_state):
        from agent_core import daemon_telegram
        client = mock.MagicMock()
        client.chats.create.return_value.send_message.return_value.text = (
            "第二個任務完成"
        )
        return daemon_telegram.tg_handle_message(
            text,
            agent_persona="x",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=lambda: client,
            agent_types_factory=lambda: mock.MagicMock(),
            chat_state=chat_state,
            chat_id="",
            late_notify=self.notifications.append,
        )

    def test_blocked_task_times_out_then_worker_serves_next_task(self):
        chat_state = {
            "chat": self.blocked_chat,
            "turns": 1,
            "last_msg_ts": time.time(),
            "work_mode": "normal",
        }

        # 1. The incident query: tool blocks forever. The soft timeout must
        #    hand the conversation back instead of waiting indefinitely.
        reply = self._handle("幫我查6月10號所有客人的生產數量", chat_state)
        self.assertIn("超過", reply)
        self.assertIn("背景動作可能仍在進行", reply)
        self.assertIsNone(chat_state["chat"])

        # 2. The worker must keep serving while the old runner is still
        #    wedged — this is exactly what the incident broke.
        self.assertFalse(self.blocked_chat.release.is_set())
        reply2 = self._handle("第二個問題", chat_state)
        self.assertIn("第二個任務完成", reply2)

        # 3. At the overall deadline the abandoned task must be announced
        #    with "teeth" — the user is told to re-ask, not left in silence.
        self.assertTrue(
            _wait_until(lambda: any("放棄" in n for n in self.notifications)),
            f"deadline abandon notice never arrived: {self.notifications!r}",
        )
        abandon = next(n for n in self.notifications if "放棄" in n)
        self.assertIn("⏰", abandon)
        self.assertIn("重問", abandon)

    def test_soft_timeout_reply_promises_background_followup(self):
        chat_state = {
            "chat": self.blocked_chat,
            "turns": 1,
            "last_msg_ts": time.time(),
            "work_mode": "normal",
        }
        reply = self._handle("查個慢東西", chat_state)
        # The 180s watchdog message must describe the NEW semantics: the task
        # keeps running in the background and has a hard overall ceiling.
        self.assertIn("轉背景", reply)
        self.assertIn("整體上限", reply)
        # PR #29 pins carried over: never nudge a blind retry of
        # side-effecting actions.
        for forbidden in ("再說一次", "再試一次", "重新嘗試", "please retry"):
            self.assertNotIn(forbidden, reply)
        self.assertIn("send_gmail", reply)
        self.assertIn("副作用", reply)


# ── Requirements 2 + 3: bounded wait, exception propagation, late delivery ──

class AbandonMonitorTests(unittest.TestCase):
    def setUp(self):
        self.notifications: list[str] = []

    def test_late_result_is_delivered_after_soft_timeout(self):
        from agent_core import daemon_telegram

        resp = types.SimpleNamespace(text="慢任務的答案")
        chat = _GatedChat(result=resp)
        self.addCleanup(chat.release.set)

        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(
                chat, "hi",
                timeout_s=0.05, deadline_s=5,
                late_notify=self.notifications.append,
            )
        # Soft timeout fired with the worker still gated; now let it finish so
        # the monitor delivers the late result (well within deadline_s=5).
        chat.release.set()
        self.assertTrue(
            _wait_until(lambda: any("慢任務的答案" in n for n in self.notifications)),
            f"late result never delivered: {self.notifications!r}",
        )
        late = next(n for n in self.notifications if "慢任務的答案" in n)
        self.assertIn("✅", late)

    def test_runner_exception_after_soft_timeout_is_reported(self):
        from agent_core import daemon_telegram

        chat = _GatedChat(exc=ValueError("boom-after-timeout"))
        self.addCleanup(chat.release.set)

        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(
                chat, "hi",
                timeout_s=0.05, deadline_s=5,
                late_notify=self.notifications.append,
            )
        # Let the gated runner blow up only after the soft timeout handed the
        # conversation back: the runner died AFTER the waiter left — previously
        # nobody ever learned about it. The monitor must surface the exception.
        chat.release.set()
        self.assertTrue(
            _wait_until(lambda: any("boom-after-timeout" in n for n in self.notifications)),
            f"runner exception never reported: {self.notifications!r}",
        )
        failure = next(n for n in self.notifications if "boom-after-timeout" in n)
        self.assertIn("❌", failure)
        self.assertIn("ValueError", failure)

    def test_abandon_notice_fires_at_deadline_for_neverending_task(self):
        from agent_core import daemon_telegram

        chat = _BlockingChat()
        self.addCleanup(chat.release.set)
        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(
                chat, "hi",
                timeout_s=0.05, deadline_s=0.2,
                late_notify=self.notifications.append,
            )
        self.assertTrue(
            _wait_until(lambda: any("放棄" in n for n in self.notifications)),
            f"abandon notice never arrived: {self.notifications!r}",
        )

    def test_notify_failure_does_not_kill_monitor(self):
        from agent_core import daemon_telegram

        delivered = threading.Event()

        def _broken_notify(message):
            delivered.set()
            raise RuntimeError("telegram down")

        chat = _BlockingChat()
        self.addCleanup(chat.release.set)
        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(
                chat, "hi",
                timeout_s=0.05, deadline_s=0.2,
                late_notify=_broken_notify,
            )
        # Monitor must reach the notify call and swallow its failure (it only
        # logs) — a broken Telegram send must not raise in the daemon thread.
        self.assertTrue(delivered.wait(5))

    def test_fast_success_spawns_no_notifications(self):
        from agent_core import daemon_telegram

        chat = mock.MagicMock()
        chat.send_message.return_value = "ok"
        result = daemon_telegram._send_message_with_timeout(
            chat, "hi", timeout_s=5, deadline_s=10,
            late_notify=self.notifications.append,
        )
        self.assertEqual(result, "ok")
        # Give a hypothetical stray monitor a beat to misfire, then assert
        # silence — fast tasks must not produce background chatter.
        time.sleep(0.05)
        self.assertEqual(self.notifications, [])


# ── Requirement 1: the deadline gate has teeth ──────────────────────────────

class DeadlineGateTests(unittest.TestCase):
    def tearDown(self):
        from agent_core import daemon_telegram
        if hasattr(daemon_telegram._task_deadline_local, "deadline"):
            del daemon_telegram._task_deadline_local.deadline

    def test_gate_passes_without_deadline_and_before_deadline(self):
        from agent_core import daemon_telegram

        def my_tool(query: str) -> str:
            """搜尋工具"""
            return f"ran:{query}"

        gated = daemon_telegram._make_deadline_gated_tool(my_tool)
        # No deadline set (direct REPL/test callers) → passthrough.
        self.assertEqual(gated("a"), "ran:a")
        # Deadline in the future → still passes.
        daemon_telegram._task_deadline_local.deadline = time.monotonic() + 60
        self.assertEqual(gated(query="b"), "ran:b")

    def test_gate_blocks_tool_after_deadline(self):
        from agent_core import daemon_telegram

        calls = []

        def my_tool() -> str:
            calls.append(1)
            return "ran"

        gated = daemon_telegram._make_deadline_gated_tool(my_tool)
        daemon_telegram._task_deadline_local.deadline = time.monotonic() - 1
        with self.assertRaises(daemon_telegram.TelegramTaskAbandoned):
            gated()
        # The real tool must not run at all — no side effects post-abandon.
        self.assertEqual(calls, [])

    def test_gate_preserves_tool_schema_surface(self):
        """genai builds function declarations from __name__/__doc__/signature;
        marker attrs (background_safe, _tg_auth_wrapped, …) ride along in
        __dict__. Losing any of these would silently break tool dispatch."""
        import inspect

        from agent_core import daemon_telegram

        def my_tool(query: str, limit: int = 5) -> str:
            """搜尋 Drive 檔案"""
            return "x"

        my_tool.background_safe = True
        gated = daemon_telegram._make_deadline_gated_tool(my_tool)
        self.assertEqual(gated.__name__, "my_tool")
        self.assertEqual(gated.__doc__, "搜尋 Drive 檔案")
        self.assertEqual(
            str(inspect.signature(gated)), str(inspect.signature(my_tool))
        )
        self.assertTrue(gated.background_safe)
        # PR #113 lesson: annotation objects must ride through wrappers
        # unchanged, or genai's argument conversion explodes at dispatch.
        self.assertEqual(gated.__annotations__, my_tool.__annotations__)

    def test_genai_arg_conversion_accepts_gated_tool(self):
        """PR #113 教訓：直呼成功 ≠ SDK 派發正常。走 google-genai 真正的
        參數轉換路徑驗證閘門 wrapper —— wrapper 弄丟 signature/annotations
        的話，這裡會在 LLM 帶參數呼叫的同一條路上炸出來。"""
        try:
            from google.genai._extra_utils import convert_argument_from_function
        except Exception:  # SDK 私有 API — 版本變動時跳過而不是紅
            self.skipTest("google-genai private conversion API unavailable")

        from agent_core import daemon_telegram

        # 鏡像生產狀態：進到 tg_build_chat 的工具已被 tool_registry 組裝點
        # 解析成真型別（#113）。本測試檔有 future-annotations，直接寫
        # annotation 會得到字串 — 那是 #113 在上游修的問題，不是閘門的。
        def read_thing(file_id, max_chars=8000):
            """讀檔工具"""
            return file_id

        read_thing.__annotations__ = {"file_id": str, "max_chars": int, "return": str}

        gated = daemon_telegram._make_deadline_gated_tool(read_thing)
        converted = convert_argument_from_function(
            {"file_id": "abc", "max_chars": 42}, gated
        )
        self.assertEqual(converted["file_id"], "abc")
        self.assertEqual(converted["max_chars"], 42)

    def test_wrap_is_idempotent_and_skips_non_callables(self):
        from agent_core import daemon_telegram

        def my_tool() -> str:
            return "x"

        once = daemon_telegram._wrap_tools_with_deadline_gate([my_tool, "not-a-tool"])
        twice = daemon_telegram._wrap_tools_with_deadline_gate(once)
        self.assertIs(once[0], twice[0])  # second pass must not re-wrap
        self.assertEqual(once[1], "not-a-tool")
        self.assertTrue(once[0]._tg_deadline_gated)

    def test_runner_thread_carries_deadline_but_main_thread_does_not(self):
        from agent_core import daemon_telegram

        seen: dict = {}

        class _ProbeChat:
            def send_message(self, wrapped):
                seen["deadline"] = getattr(
                    daemon_telegram._task_deadline_local, "deadline", None
                )
                return types.SimpleNamespace(text="ok")

        before = time.monotonic()
        daemon_telegram._send_message_with_timeout(
            _ProbeChat(), "hi", timeout_s=5, deadline_s=42,
        )
        self.assertIsNotNone(seen["deadline"])
        # Roughly now+42s on the runner thread…
        self.assertGreater(seen["deadline"], before + 30)
        # …while the calling (main) thread's local stays untouched, so
        # foreground fastpath tool calls are never gated by stale deadlines.
        self.assertIsNone(
            getattr(daemon_telegram._task_deadline_local, "deadline", None)
        )


class LateReplyFinalizationTests(unittest.TestCase):
    """Codex P2 on PR #116: a late result must go through the same reply
    finalization as foreground replies — citation guard, stranded-media
    delivery, chat-history recording — instead of shipping raw resp.text.
    All patches here are applied on the main thread around synchronous calls
    (repo rule: never mock.patch inside worker threads)."""

    def test_finalize_applies_citation_guard_banner(self):
        from agent_core import citation_guard, daemon_telegram

        fake_guard = types.SimpleNamespace(ok=False, reason="no-evidence")
        with mock.patch.object(
            citation_guard, "check_citation", return_value=fake_guard,
        ), mock.patch.object(
            citation_guard, "annotate_with_warning",
            side_effect=lambda text, guard: "BANNER|" + text,
        ):
            out = daemon_telegram._finalize_late_reply(
                "A客戶的單價是 33 元",
                chat_id="", user_text="q",
                downloads_before=set(), inference_start_ts=0.0,
            )
        self.assertEqual(out, "BANNER|A客戶的單價是 33 元")

    def test_finalize_records_history_and_delivers_media(self):
        from agent_core import daemon_telegram

        recorded: list[tuple] = []
        with mock.patch.object(
            daemon_telegram, "_record_tg_chat_turn",
            side_effect=lambda *a: recorded.append(a),
        ), mock.patch.object(
            daemon_telegram, "_scan_new_media_downloads_since",
            return_value=["/tmp/x.mp4"],
        ) as scan, mock.patch.object(
            daemon_telegram, "_deliver_new_downloads_via_telegram",
            return_value="\n📤 delivered",
        ) as deliver:
            out = daemon_telegram._finalize_late_reply(
                "答案",
                chat_id="123", user_text="問題",
                downloads_before={"/tmp/old.mp4"}, inference_start_ts=42.0,
            )
        self.assertEqual(out, "答案\n📤 delivered")
        self.assertEqual(recorded, [("123", "問題", "答案")])
        scan.assert_called_once_with(42.0, {"/tmp/old.mp4"})
        deliver.assert_called_once_with(["/tmp/x.mp4"], "123")

    def test_finalize_without_chat_id_skips_history_and_delivery(self):
        from agent_core import daemon_telegram

        with mock.patch.object(
            daemon_telegram, "_record_tg_chat_turn",
        ) as record, mock.patch.object(
            daemon_telegram, "_scan_new_media_downloads_since",
        ) as scan:
            out = daemon_telegram._finalize_late_reply(
                "答案", chat_id="", user_text="問題",
                downloads_before=set(), inference_start_ts=0.0,
            )
        self.assertEqual(out, "答案")
        record.assert_not_called()
        scan.assert_not_called()

    def test_finalize_collaborator_failure_still_returns_text(self):
        from agent_core import daemon_telegram

        with mock.patch.object(
            daemon_telegram, "_scan_new_media_downloads_since",
            side_effect=RuntimeError("scan broke"),
        ), mock.patch.object(
            daemon_telegram, "_record_tg_chat_turn",
            side_effect=RuntimeError("history broke"),
        ):
            out = daemon_telegram._finalize_late_reply(
                "答案", chat_id="123", user_text="問題",
                downloads_before=set(), inference_start_ts=0.0,
            )
        self.assertEqual(out, "答案")

    def test_monitor_routes_late_result_through_finalizer(self):
        from agent_core import daemon_telegram

        notifications: list[str] = []
        resp = types.SimpleNamespace(text="慢任務的答案")
        chat = _GatedChat(result=resp)
        self.addCleanup(chat.release.set)

        # Plain closure handed to the monitor thread — no patching in-thread.
        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(
                chat, "hi",
                timeout_s=0.05, deadline_s=5,
                late_notify=notifications.append,
                late_finalize=lambda text: "FINAL|" + text,
            )
        chat.release.set()  # let the gated worker finish → monitor finalizes
        self.assertTrue(
            _wait_until(lambda: any("FINAL|慢任務的答案" in n for n in notifications)),
            f"finalized late result never delivered: {notifications!r}",
        )

    def test_monitor_finalizer_failure_falls_back_to_raw_text(self):
        from agent_core import daemon_telegram

        notifications: list[str] = []
        resp = types.SimpleNamespace(text="原始答案")
        chat = _GatedChat(result=resp)
        self.addCleanup(chat.release.set)

        def _broken_finalize(text):
            raise RuntimeError("finalize broke")

        with self.assertRaises(TimeoutError):
            daemon_telegram._send_message_with_timeout(
                chat, "hi",
                timeout_s=0.05, deadline_s=5,
                late_notify=notifications.append,
                late_finalize=_broken_finalize,
            )
        chat.release.set()  # let the gated worker finish → monitor falls back
        self.assertTrue(
            _wait_until(lambda: any("原始答案" in n for n in notifications)),
            f"raw fallback never delivered: {notifications!r}",
        )


class QueueSwappedToolDispatchTests(unittest.TestCase):
    """Found while pinning the gate: _telegram_email_lake_rebuild_queued is
    defined in a future-annotations module and swapped into the tool list at
    tg_build_chat — AFTER the registry's #113 string-annotation resolution.
    Its 'int' string annotations made every argument-bearing LLM call of
    email_lake_rebuild explode with the #113 isinstance TypeError."""

    def test_email_lake_rebuild_wrapper_dispatches_through_genai(self):
        try:
            from google.genai._extra_utils import convert_argument_from_function
        except Exception:
            self.skipTest("google-genai private conversion API unavailable")

        from agent_core import daemon_telegram

        def email_lake_rebuild(days_back: int = 30, max_emails: int = 200):
            return "real"

        swapped = daemon_telegram._queue_telegram_long_running_tools(
            [email_lake_rebuild]
        )[0]
        self.assertEqual(swapped.__name__, "email_lake_rebuild")
        self.assertIsNot(swapped, email_lake_rebuild)
        converted = convert_argument_from_function(
            {"days_back": 30, "max_emails": 100}, swapped
        )
        self.assertEqual(converted["days_back"], 30)
        self.assertEqual(converted["max_emails"], 100)

    def test_wrapper_annotations_are_real_types(self):
        from agent_core import daemon_telegram
        ann = daemon_telegram._telegram_email_lake_rebuild_queued.__annotations__
        self.assertFalse(
            any(isinstance(v, str) for v in ann.values()),
            f"string annotations would TypeError at genai dispatch: {ann}",
        )


# ── Requirement 1: env-configurable knobs (agent_core/env_utils) ────────────

class DeadlineEnvConfigTests(unittest.TestCase):
    def test_defaults(self):
        from agent_core import daemon_telegram
        self.assertEqual(daemon_telegram._tg_inference_soft_timeout_s(), 180.0)
        self.assertEqual(daemon_telegram._tg_task_deadline_s(), 900.0)

    def test_env_overrides(self):
        from agent_core import daemon_telegram
        with mock.patch.dict(os.environ, {
            "RED_TG_INFERENCE_TIMEOUT_S": "30",
            "RED_TG_TASK_DEADLINE_S": "120",
        }):
            self.assertEqual(daemon_telegram._tg_inference_soft_timeout_s(), 30.0)
            self.assertEqual(daemon_telegram._tg_task_deadline_s(), 120.0)

    def test_garbage_env_falls_back_to_defaults(self):
        from agent_core import daemon_telegram
        with mock.patch.dict(os.environ, {
            "RED_TG_INFERENCE_TIMEOUT_S": "soon",
            "RED_TG_TASK_DEADLINE_S": "NaN",
        }):
            self.assertEqual(daemon_telegram._tg_inference_soft_timeout_s(), 180.0)
            self.assertEqual(daemon_telegram._tg_task_deadline_s(), 900.0)


# ── Requirement 3: the shared genai client gets an HTTP timeout ─────────────

class GeminiHttpTimeoutTests(unittest.TestCase):
    """The incident's root wedge: genai.Client had NO http timeout, so a dead
    connection (CLOSE_WAIT after the 503 storm) blocked send_message forever.
    Pin that the singleton is now always built with http_options.timeout."""

    def setUp(self):
        from agent_core import gemini_client
        self._saved_client = gemini_client._gemini_client
        gemini_client._gemini_client = None

        def _restore():
            gemini_client._gemini_client = self._saved_client
        self.addCleanup(_restore)

        self.captured: list[dict] = []
        fake_module = types.SimpleNamespace(
            Client=lambda **kwargs: self.captured.append(kwargs) or "client",
        )
        for target, value in (
            ("_get_genai_module", lambda: fake_module),
            ("_get_gemini_api_key", lambda: "AIza" + "x" * 35),
        ):
            patcher = mock.patch.object(gemini_client, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_timeout_is_600s_in_milliseconds(self):
        from agent_core import gemini_client
        client = gemini_client._get_gemini_client()
        self.assertEqual(client, "client")
        self.assertEqual(
            self.captured[0]["http_options"], {"timeout": 600 * 1000}
        )

    def test_env_override_in_seconds_converts_to_milliseconds(self):
        from agent_core import gemini_client
        with mock.patch.dict(os.environ, {"RED_GEMINI_HTTP_TIMEOUT_S": "30"}):
            gemini_client._get_gemini_client()
        self.assertEqual(
            self.captured[0]["http_options"], {"timeout": 30 * 1000}
        )

    def test_singleton_is_cached(self):
        from agent_core import gemini_client
        first = gemini_client._get_gemini_client()
        second = gemini_client._get_gemini_client()
        self.assertIs(first, second)
        self.assertEqual(len(self.captured), 1)


if __name__ == "__main__":
    unittest.main()
