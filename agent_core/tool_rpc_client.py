"""Client for the local tool RPC Unix socket."""
from __future__ import annotations

import json
import math
import os
import socket
from typing import Any

from agent_core.env_utils import env_float as _env_float, env_int as _env_int
from agent_core.logging_and_paths import RUNTIME_ROOT
from agent_core.tool_rpc_protocol import MAX_TIMEOUT_SEC, MIN_TIMEOUT_SEC, make_response


SOCKET_PATH = os.environ.get(
    "RED_TOOL_RPC_SOCKET",
    os.path.join(RUNTIME_ROOT, "run", "tool_rpc.sock"),
)
_TCP_FALLBACKS: dict[str, tuple[str, int]] = {}
_IN_PROCESS_FALLBACKS: set[str] = set()


def _register_tcp_fallback(socket_path: str, address: tuple[str, int]) -> None:
    _TCP_FALLBACKS[os.path.realpath(socket_path)] = address


def _unregister_tcp_fallback(socket_path: str) -> None:
    _TCP_FALLBACKS.pop(os.path.realpath(socket_path), None)


def _register_in_process_fallback(socket_path: str) -> None:
    _IN_PROCESS_FALLBACKS.add(os.path.realpath(socket_path))


def _unregister_in_process_fallback(socket_path: str) -> None:
    _IN_PROCESS_FALLBACKS.discard(os.path.realpath(socket_path))


MAX_RESPONSE_BYTES = _env_int(
    "RED_TOOL_RPC_MAX_RESPONSE_BYTES",
    16 * 1024 * 1024,
    min_value=1024,
    max_value=128 * 1024 * 1024,
)
CLIENT_TIMEOUT_GRACE_SEC = _env_float("RED_TOOL_RPC_CLIENT_GRACE_S", 10, min_value=0, max_value=300)


class ToolRPCError(RuntimeError):
    pass


def _resolve_timeout(request: dict[str, Any], timeout_sec: int | float | None) -> tuple[float | None, str]:
    timeout_source = timeout_sec if timeout_sec is not None else request.get("timeout_sec") or 300
    if isinstance(timeout_source, bool):
        return None, "timeout_sec must be a number"
    try:
        timeout = float(timeout_source)
    except Exception as exc:
        return None, f"timeout_sec must be a number ({type(exc).__name__}: {exc})"
    if not math.isfinite(timeout):
        return None, "timeout_sec must be finite"
    if timeout < MIN_TIMEOUT_SEC or timeout > MAX_TIMEOUT_SEC:
        return None, f"timeout_sec must be between {MIN_TIMEOUT_SEC} and {MAX_TIMEOUT_SEC}"
    return timeout, ""


def call_rpc(request: dict[str, Any], *, timeout_sec: int | float | None = None) -> dict[str, Any]:
    timeout, timeout_error = _resolve_timeout(request, timeout_sec)
    if timeout is None:
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ invalid RPC timeout: {timeout_error}",
            error_code="invalid_input",
            recoverable=False,
        )
    resolved_socket_path = os.path.realpath(SOCKET_PATH)
    if resolved_socket_path in _IN_PROCESS_FALLBACKS:
        from agent_core.tool_worker_exec import run_worker_subprocess
        response = run_worker_subprocess(request, timeout_sec=timeout)
        response["rpc_elapsed_sec"] = response.get("elapsed_sec")
        return response
    socket_timeout = timeout + CLIENT_TIMEOUT_GRACE_SEC
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
    # 錯誤分兩類（給 tool_runner 決定能不能安全 fallback 直跑）：
    #   1. 「連不上」（socket 建立 / connect 失敗）— request 從未抵達 server，
    #      worker 一定沒開跑 → 標 rpc_unreachable=True，fallback 重跑安全。
    #   2. 「連上之後才失敗」（send / recv 逾時、server 中途斷線、回應非
    #      JSON）— worker 可能已開跑、副作用可能已發生（例 telegram_send_file
    #      檔已送達）→ 不標 unreachable，呼叫端**不得**重跑。
    try:
        tcp_fallback = _TCP_FALLBACKS.get(resolved_socket_path)
        family = socket.AF_INET if tcp_fallback else socket.AF_UNIX
        target = tcp_fallback or resolved_socket_path
        sock = socket.socket(family, socket.SOCK_STREAM)
    except Exception as exc:
        response = make_response(
            request,
            transport_ok=False,
            text=f"❌ tool RPC unavailable: {type(exc).__name__}: {exc}",
            error_code="network",
            recoverable=True,
        )
        response["rpc_unreachable"] = True
        return response
    with sock:
        sock.settimeout(socket_timeout)
        try:
            sock.connect(target)
        except Exception as exc:
            response = make_response(
                request,
                transport_ok=False,
                text=f"❌ tool RPC unavailable: {type(exc).__name__}: {exc}",
                error_code="network",
                recoverable=True,
            )
            response["rpc_unreachable"] = True
            return response
        try:
            sock.sendall(payload)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ToolRPCError("response too large")
                if b"\n" in chunk:
                    break
        except Exception as exc:
            # 已連上後才失敗：worker 可能已在執行 → 不可重跑。
            return make_response(
                request,
                transport_ok=False,
                text=f"❌ tool RPC failed after connect: {type(exc).__name__}: {exc}",
                error_code="network",
                recoverable=True,
            )
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        # server 有回東西但壞掉：request 已抵達、worker 可能已跑完 → 同樣
        # 不標 unreachable。
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ tool RPC returned invalid JSON: {type(exc).__name__}: {exc}",
            error_code="internal",
            recoverable=True,
        )
