"""Unified execution entry for tools.

Callers should go through this module when they want isolation. The runner
prefers the local RPC server, then falls back to a direct fresh subprocess so
Telegram remains useful even if launchd has not loaded tool_rpc yet.
"""
from __future__ import annotations

import os
from typing import Any

from agent_core.tool_rpc_protocol import make_request


def _response_to_tool_result(response: dict[str, Any]):
    from agent_core.tool_result import ToolResult, ErrorCode

    text = str(response.get("text") or "")
    if not response.get("transport_ok"):
        return ToolResult.failure(
            text or "tool transport failed",
            error_code=str(response.get("error_code") or ErrorCode.INTERNAL),
            recoverable=bool(response.get("recoverable", True)),
            suggested_fix=str(response.get("suggested_fix") or "檢查 tool_rpc daemon 或 worker subprocess log"),
        )
    if bool(response.get("tool_ok", False)):
        return ToolResult.success(
            text,
            data=response.get("data"),
            warnings=list(response.get("warnings") or []),
            artifacts=list(response.get("artifacts") or []),
        )
    return ToolResult.failure(
        text or "tool failed",
        error_code=str(response.get("error_code") or ErrorCode.INTERNAL),
        recoverable=bool(response.get("recoverable", False)),
        suggested_fix=str(response.get("suggested_fix") or ""),
    )


def call_tool(
    tool: str,
    kwargs: dict[str, Any] | None = None,
    *,
    args: list[Any] | None = None,
    context: dict[str, Any] | None = None,
    timeout_sec: int | float | None = None,
    prefer_rpc: bool = True,
    fallback_direct: bool | None = None,
    cancel_event: Any = None,
):
    request = make_request(
        tool,
        kwargs or {},
        args=args or [],
        context=context or {},
        timeout_sec=timeout_sec,
    )
    if fallback_direct is None:
        fallback_direct = os.environ.get("RED_TOOL_RPC_FALLBACK_DIRECT", "1") != "0"

    response = None
    if prefer_rpc and cancel_event is None:
        from agent_core.tool_rpc_client import call_rpc
        response = call_rpc(request, timeout_sec=timeout_sec)
        # 只在「連線層失敗（rpc_unreachable：request 從未抵達 server）」才
        # fallback 直跑。server 有收到 request 的失敗（worker timeout / crash
        # / 回應損毀）一律原樣返回 —— 無條件 fallback 會讓有副作用的工具
        # 重複執行（例：telegram_send_file 超時被殺但檔已送達，再直跑一次
        # 就送兩份）。
        if not (fallback_direct and response.get("rpc_unreachable")):
            return _response_to_tool_result(response)

    from agent_core.tool_worker_exec import run_worker_subprocess
    response = run_worker_subprocess(
        request,
        timeout_sec=timeout_sec,
        cancel_event=cancel_event,
    )
    return _response_to_tool_result(response)


def tool_runner_status() -> str:
    """Report the execution path used by ToolRunner."""
    from agent_core.tool_rpc_server import tool_rpc_status

    rpc = tool_rpc_status()
    fallback = os.environ.get("RED_TOOL_RPC_FALLBACK_DIRECT", "1") != "0"
    mode = os.environ.get("RED_TOOL_RPC_PROXY_MODE", "diagnostics")
    return (
        f"{rpc}\n"
        f"  fallback_direct: {'on' if fallback else 'off'}\n"
        f"  proxy_mode     : {mode}"
    )
