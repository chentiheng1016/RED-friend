"""Unix-socket RPC server for isolated tool execution."""
from __future__ import annotations

import json
import os
import signal
import socketserver
import threading
import time
from typing import Any

from agent_core.env_utils import env_int as _env_int
from agent_core.logging_and_paths import RUNTIME_ROOT
from agent_core.tool_rpc_client import SOCKET_PATH
from agent_core.tool_rpc_protocol import MAX_TIMEOUT_SEC, MIN_TIMEOUT_SEC, make_response, validate_request
from agent_core.tool_worker_exec import run_worker_subprocess

MAX_REQUEST_BYTES = _env_int(
    "RED_TOOL_RPC_MAX_REQUEST_BYTES",
    8 * 1024 * 1024,
    min_value=1024,
    max_value=128 * 1024 * 1024,
)
DEFAULT_TIMEOUT_SEC = _env_int(
    "RED_TOOL_RPC_DEFAULT_TIMEOUT_S",
    300,
    min_value=MIN_TIMEOUT_SEC,
    max_value=MAX_TIMEOUT_SEC,
)


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
        if isinstance(server_address, str):
            server_address = os.path.realpath(server_address)
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)


class _ThreadingTCPFallbackServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, RequestHandlerClass, bind_and_activate=True):
        self._tcp_fallback_socket_path = os.path.realpath(socket_path)
        self._in_process_fallback_socket_path = None
        self._in_process_shutdown = None
        super().__init__(("127.0.0.1", 0), RequestHandlerClass, bind_and_activate)
        from agent_core import tool_rpc_client
        tool_rpc_client._register_tcp_fallback(self._tcp_fallback_socket_path, self.server_address)

    def server_close(self):
        fallback_path = getattr(self, "_tcp_fallback_socket_path", None)
        try:
            super().server_close()
        finally:
            if fallback_path:
                from agent_core import tool_rpc_client
                tool_rpc_client._unregister_tcp_fallback(fallback_path)


class _InProcessFallbackServer(socketserver.BaseServer):
    def __init__(self, socket_path: str, RequestHandlerClass):
        self._tcp_fallback_socket_path = None
        self._in_process_fallback_socket_path = os.path.realpath(socket_path)
        self._in_process_shutdown = threading.Event()
        super().__init__(self._in_process_fallback_socket_path, RequestHandlerClass)
        from agent_core import tool_rpc_client
        tool_rpc_client._register_in_process_fallback(self._in_process_fallback_socket_path)

    def serve_forever(self, poll_interval=0.5):
        while not self._in_process_shutdown.wait(poll_interval):
            pass

    def shutdown(self):
        self._in_process_shutdown.set()

    def server_close(self):
        from agent_core import tool_rpc_client
        tool_rpc_client._unregister_in_process_fallback(self._in_process_fallback_socket_path)


class ToolRPCHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        started = time.time()
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            response = make_response(
                {"request_id": "", "tool": "", "protocol": 1},
                transport_ok=False,
                text="❌ tool RPC request too large",
                error_code="invalid_input",
                recoverable=False,
            )
            self._write_response(response)
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            response = make_response(
                {"request_id": "", "tool": "", "protocol": 1},
                transport_ok=False,
                text=f"❌ invalid RPC JSON: {type(exc).__name__}: {exc}",
                error_code="invalid_input",
                recoverable=False,
            )
            self._write_response(response)
            return
        ok, err = validate_request(request)
        if not ok:
            self._write_response(make_response(
                request,
                transport_ok=False,
                text=f"❌ invalid RPC request: {err}",
                error_code="invalid_input",
                recoverable=False,
            ))
            return
        timeout = request.get("timeout_sec") or DEFAULT_TIMEOUT_SEC
        try:
            response = run_worker_subprocess(request, timeout_sec=timeout)
        except Exception as exc:
            response = make_response(
                request,
                transport_ok=False,
                text=f"❌ tool RPC worker dispatch failed: {type(exc).__name__}: {exc}",
                error_code="internal",
                recoverable=True,
            )
        response["rpc_elapsed_sec"] = round(time.time() - started, 3)
        self._write_response(response)

    def _write_response(self, response: dict[str, Any]) -> None:
        data = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        self.wfile.write(data)


def create_tool_rpc_server(
    socket_path: str,
    handler_cls: type[socketserver.BaseRequestHandler] = ToolRPCHandler,
    *,
    allow_fallback: bool = True,
) -> socketserver.BaseServer:
    """Create the best available local RPC server transport."""
    socket_path = os.path.realpath(socket_path)
    try:
        return _ThreadingUnixServer(socket_path, handler_cls)
    except PermissionError:
        if not allow_fallback:
            raise
    try:
        return _ThreadingTCPFallbackServer(socket_path, handler_cls)
    except PermissionError:
        return _InProcessFallbackServer(socket_path, handler_cls)


def _prepare_socket(path: str) -> None:
    path = os.path.realpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def serve_forever(socket_path: str = SOCKET_PATH) -> None:
    _prepare_socket(socket_path)
    server = create_tool_rpc_server(socket_path, ToolRPCHandler, allow_fallback=False)
    os.chmod(socket_path, 0o600)
    print(f"[tool_rpc] listening on {socket_path}", flush=True)

    def _shutdown(signum, _frame):
        print(f"[tool_rpc] signal {signum}, shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
    except Exception:
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            os.unlink(socket_path)
        except OSError:
            pass


def tool_rpc_status() -> str:
    """Check whether the local tool RPC server is reachable."""
    from agent_core.tool_rpc_protocol import make_request
    from agent_core.tool_rpc_client import call_rpc

    request = make_request(
        "system_alerts",
        {"min_level": "crit"},
        context={"caller": "tool_rpc_status", "worker_channel": "daemon"},
        timeout_sec=15,
    )
    response = call_rpc(request, timeout_sec=15)
    if not response.get("transport_ok"):
        return (
            "🔴 tool_rpc unreachable\n"
            f"  socket: {SOCKET_PATH}\n"
            f"  error : {response.get('text')}"
        )
    return (
        "🟢 tool_rpc reachable\n"
        f"  socket : {SOCKET_PATH}\n"
        f"  worker : pid={response.get('worker_pid')} elapsed={response.get('elapsed_sec')}s\n"
        f"  source : {response.get('source_version') or '-'}"
    )


def main() -> int:
    os.environ.setdefault("AGENT_DAEMON_MODE", "1")
    os.makedirs(os.path.join(RUNTIME_ROOT, "run"), exist_ok=True)
    serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
