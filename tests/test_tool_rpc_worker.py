from __future__ import annotations

import inspect
import os
import signal
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _isolate_live_health_alerts(test: unittest.TestCase) -> None:
    """Stop live fleet health from leaking into RPC-plumbing E2E tests.

    The diagnostic tools these tests drive (system_alerts / fresh_diagnostics)
    execute in a *fresh subprocess* and read the main checkout's live var/
    (cost.jsonl / api_errors.jsonl) plus launchctl to compute health. When the
    live Gemini quota is depleted the error-rate check returns a crit alert and
    the string result classifies as a failure (ok=False) — a false negative for
    a test that only means to exercise worker-spawn → socket → tool-return
    plumbing. The kill-switch env makes check_alerts() return [] and rides into
    the subprocess via os.environ.copy(); patch once here on the main thread
    (never inside a worker thread) so the fake can't leak across tests.
    """
    patcher = mock.patch.dict(
        os.environ, {"RED_DISABLE_HEALTH_ALERTS": "1"}, clear=False
    )
    patcher.start()
    test.addCleanup(patcher.stop)


class ToolRPCProtocolTests(unittest.TestCase):
    def test_request_validation_rejects_bad_tool_name(self):
        from agent_core.tool_rpc_protocol import make_request, validate_request

        req = make_request("bad-name", {})
        ok, err = validate_request(req)

        self.assertFalse(ok)
        self.assertIn("invalid tool name", err)

    def test_request_validation_rejects_bad_timeout(self):
        from agent_core.tool_rpc_protocol import make_request, validate_request

        req = make_request("system_alerts", {}, timeout_sec="soon")
        ok, err = validate_request(req)

        self.assertFalse(ok)
        self.assertIn("timeout_sec must be a number", err)

        req = make_request("system_alerts", {}, timeout_sec=999999)
        ok, err = validate_request(req)

        self.assertFalse(ok)
        self.assertIn("timeout_sec must be between", err)

    def test_rpc_client_rejects_invalid_timeout_without_socket(self):
        from agent_core.tool_rpc_client import call_rpc
        from agent_core.tool_rpc_protocol import make_request

        with mock.patch("agent_core.tool_rpc_client.socket.socket") as socket_factory:
            response = call_rpc(make_request("system_alerts", {}, timeout_sec="soon"))

        self.assertEqual(response["error_code"], "invalid_input")
        socket_factory.assert_not_called()


class ToolProxyTests(unittest.TestCase):
    def test_proxy_preserves_signature_and_calls_runner(self):
        from agent_core.tool_proxy import make_rpc_proxy
        from agent_core.tool_result import ToolResult

        def sample_tool(query: str, limit: int = 5) -> str:
            """Search something."""
            return f"{query}:{limit}"

        proxy = make_rpc_proxy(sample_tool, caller="test", timeout_sec=7)

        self.assertEqual(proxy.__name__, "sample_tool")
        self.assertEqual(str(inspect.signature(proxy)), "(query: 'str', limit: 'int' = 5) -> 'str'")
        with mock.patch("agent_core.tool_runner.call_tool", return_value=ToolResult.success("OK")) as call:
            result = proxy("abc")

        self.assertEqual(str(result), "OK")
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0], "sample_tool")
        self.assertEqual(call.call_args.args[1], {"query": "abc"})

    def test_telegram_auth_wraps_outside_rpc_proxy(self):
        from agent_core.tool_proxy import proxy_tools
        from agent_core.tg_auth import filter_tools_for_telegram

        def send_gmail(to: str, subject: str, body: str) -> str:
            return "sent"

        proxied = proxy_tools([send_gmail], caller="test", mode="all")
        wrapped = filter_tools_for_telegram(proxied, get_chat_id=lambda: "9990000001")[0]

        with mock.patch("agent_core.tool_runner.call_tool") as call:
            result = wrapped("a@b.test", "s", "b")

        self.assertIn("需要大王確認", str(result))
        call.assert_not_called()

    def test_proxy_tools_does_not_replace_existing_auth_wrapper(self):
        from agent_core.tool_proxy import proxy_tools
        from agent_core.tg_auth import filter_tools_for_telegram

        def send_gmail(to: str, subject: str, body: str) -> str:
            return "sent"

        wrapped = filter_tools_for_telegram([send_gmail], get_chat_id=lambda: "9990000001")[0]
        proxied = proxy_tools([wrapped], caller="test", mode="all")[0]

        self.assertIs(proxied, wrapped)
        self.assertFalse(getattr(proxied, "_tool_rpc_proxy", False))


class ToolWorkerTests(unittest.TestCase):
    def setUp(self):
        # test_direct_worker_executes_fresh_diagnostic_tool runs the real
        # system_alerts diagnostic through a worker subprocess; isolate it from
        # live fleet health so it only verifies RPC plumbing. Harmless to the
        # other (fully-mocked) tests in this class.
        _isolate_live_health_alerts(self)

    def test_cancel_escalates_to_sigkill_when_worker_ignores_sigterm(self):
        from agent_core import tool_worker_exec
        from agent_core.tool_rpc_protocol import make_request

        class FakeProc:
            pid = 4321

            def poll(self):
                return None

            def terminate(self):
                raise AssertionError("terminate fallback should not be used")

            def kill(self):
                raise AssertionError("kill fallback should not be used")

            def communicate(self, _payload):
                raise AssertionError("fake thread should not call communicate")

        class FakeThread:
            def __init__(self, target, daemon=False):
                del target, daemon

            def start(self):
                pass

            def is_alive(self):
                return True

            def join(self, timeout=None):
                del timeout

        cancel_event = mock.Mock()
        cancel_event.is_set.return_value = True

        with mock.patch.object(tool_worker_exec.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(tool_worker_exec.threading, "Thread", FakeThread), \
             mock.patch.object(tool_worker_exec.os, "killpg") as killpg:
            response = tool_worker_exec.run_worker_subprocess(
                make_request("system_alerts", {}),
                cancel_event=cancel_event,
            )

        self.assertEqual(response["error_code"], "cancelled")
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(4321, signal.SIGTERM),
                mock.call(4321, signal.SIGKILL),
            ],
        )

    def test_direct_worker_rejects_invalid_timeout_without_spawning(self):
        from agent_core import tool_worker_exec
        from agent_core.tool_rpc_protocol import make_request

        with mock.patch.object(tool_worker_exec.subprocess, "Popen") as popen:
            response = tool_worker_exec.run_worker_subprocess(
                make_request("system_alerts", {}),
                timeout_sec="soon",
            )

        self.assertEqual(response["error_code"], "invalid_input")
        popen.assert_not_called()

    def test_worker_tool_resolution_caches_builtin_catalog(self):
        from agent_core import tool_worker_exec

        def sample_tool():
            return "ok"

        tool_worker_exec._worker_builtin_tools_by_name.cache_clear()
        try:
            with mock.patch(
                "agent_core.tool_registry_catalog.build_builtin_tools",
                return_value=[sample_tool],
            ) as build:
                self.assertIs(tool_worker_exec._resolve_tool("sample_tool"), sample_tool)
                self.assertIs(tool_worker_exec._resolve_tool("sample_tool"), sample_tool)

            self.assertEqual(build.call_count, 1)
        finally:
            tool_worker_exec._worker_builtin_tools_by_name.cache_clear()

    def test_direct_worker_executes_fresh_diagnostic_tool(self):
        from agent_core.tool_runner import call_tool

        with mock.patch.dict(os.environ, {"RED_ALERT_API_ERROR_MIN_CALLS": "999999"}, clear=False):
            result = call_tool(
                "system_alerts",
                {"min_level": "crit"},
                context={"caller": "test", "worker_channel": "daemon"},
                timeout_sec=30,
                prefer_rpc=False,
            )

        self.assertTrue(getattr(result, "ok", False), str(result))
        self.assertIn("crit", str(result).lower())


class ToolRPCServerFactoryTests(unittest.TestCase):
    def test_factory_uses_tcp_fallback_after_unix_permission_error(self):
        from agent_core import tool_rpc_server

        fake_server = object()
        socket_path = os.path.join(tempfile.gettempdir(), "red-tool-rpc-test.sock")

        with mock.patch.object(
            tool_rpc_server,
            "_ThreadingUnixServer",
            side_effect=PermissionError("unix unavailable"),
        ) as unix_server, \
             mock.patch.object(
                 tool_rpc_server,
                 "_ThreadingTCPFallbackServer",
                 return_value=fake_server,
             ) as tcp_server:
            server = tool_rpc_server.create_tool_rpc_server(socket_path, tool_rpc_server.ToolRPCHandler)

        self.assertIs(server, fake_server)
        unix_server.assert_called_once_with(os.path.realpath(socket_path), tool_rpc_server.ToolRPCHandler)
        tcp_server.assert_called_once_with(os.path.realpath(socket_path), tool_rpc_server.ToolRPCHandler)

    def test_factory_uses_in_process_fallback_after_tcp_permission_error(self):
        from agent_core import tool_rpc_server

        fake_server = object()
        socket_path = os.path.join(tempfile.gettempdir(), "red-tool-rpc-test.sock")

        with mock.patch.object(
            tool_rpc_server,
            "_ThreadingUnixServer",
            side_effect=PermissionError("unix unavailable"),
        ), \
             mock.patch.object(
                 tool_rpc_server,
                 "_ThreadingTCPFallbackServer",
                 side_effect=PermissionError("tcp unavailable"),
             ), \
             mock.patch.object(
                 tool_rpc_server,
                 "_InProcessFallbackServer",
                 return_value=fake_server,
             ) as in_process_server:
            server = tool_rpc_server.create_tool_rpc_server(socket_path, tool_rpc_server.ToolRPCHandler)

        self.assertIs(server, fake_server)
        in_process_server.assert_called_once_with(
            os.path.realpath(socket_path),
            tool_rpc_server.ToolRPCHandler,
        )


class TelegramToolRPCE2ESmokeTests(unittest.TestCase):
    def setUp(self):
        # The diagnostic runs end-to-end through a fresh worker subprocess that
        # reads live var/ health; isolate it so this smoke test only asserts the
        # proxy -> socket -> worker -> tool-return path, not fleet health.
        _isolate_live_health_alerts(self)

    def test_telegram_diagnostic_tool_runs_through_rpc_socket(self):
        """Telegram tool proxy -> tool_rpc socket -> fresh worker subprocess."""
        from agent_core import daemon_telegram, tool_rpc_client, tool_rpc_server
        from agent_core.fresh_diagnostics import system_alerts

        class FakeTypes:
            class AutomaticFunctionCallingConfig:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

            class GenerateContentConfig:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

        holder = {}

        class FakeChat:
            def __init__(self, config):
                self.config = config

            def send_message(self, _wrapped):
                tools = self.config.kwargs["tools"]
                tool = next(fn for fn in tools if getattr(fn, "__name__", "") == "system_alerts")
                holder["used_rpc_proxy"] = bool(getattr(tool, "_tool_rpc_proxy", False))
                result = tool("crit")
                holder["tool_result_ok"] = bool(getattr(result, "ok", False))
                holder["tool_result_text"] = str(result)
                return types.SimpleNamespace(text="telegram rpc e2e ok")

        class FakeClient:
            class _Chats:
                def create(self, *, model, config):
                    del model
                    return FakeChat(config)

            chats = _Chats()

        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, "tool_rpc.sock")
            tool_rpc_server._prepare_socket(socket_path)
            server = tool_rpc_server.create_tool_rpc_server(
                socket_path,
                tool_rpc_server.ToolRPCHandler,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(tool_rpc_client, "SOCKET_PATH", socket_path), \
                     mock.patch.dict(os.environ, {
                         "AGENT_DAEMON_MODE": "1",
                         "RED_TOOL_RPC_TELEGRAM": "1",
                         "RED_TOOL_RPC_PROXY_MODE": "diagnostics",
                         "RED_TOOL_RPC_FALLBACK_DIRECT": "0",
                         "RED_ALERT_API_ERROR_MIN_CALLS": "999999",
                     }, clear=False), \
                     mock.patch("agent_core.mode_manager.get_current_mode", return_value="normal"):
                    reply = daemon_telegram.tg_handle_message(
                        user_text="系統警示 smoke",
                        chat_id="9990000001",
                        agent_persona="persona",
                        tools_list=[system_alerts],
                        gemini_model="fake-model",
                        agent_client_factory=FakeClient,
                        agent_types_factory=FakeTypes,
                        chat_state={
                            "chat": None,
                            "turns": 0,
                            "last_msg_ts": 0.0,
                            "work_mode": "normal",
                        },
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(reply, "telegram rpc e2e ok")
        self.assertTrue(holder.get("used_rpc_proxy"), holder)
        self.assertTrue(holder.get("tool_result_ok"), holder.get("tool_result_text"))
        self.assertIn("crit", holder.get("tool_result_text", "").lower())


if __name__ == "__main__":
    unittest.main()
