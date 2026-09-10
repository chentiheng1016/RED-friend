"""Small JSON protocol shared by the tool RPC client/server/worker."""
from __future__ import annotations

import math
import uuid
from typing import Any


PROTOCOL_VERSION = 1
MIN_TIMEOUT_SEC = 1
MAX_TIMEOUT_SEC = 7200


def new_request_id() -> str:
    return "tr_" + uuid.uuid4().hex[:16]


def make_request(
    tool: str,
    kwargs: dict[str, Any] | None = None,
    *,
    args: list[Any] | None = None,
    context: dict[str, Any] | None = None,
    timeout_sec: int | float | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "request_id": request_id or new_request_id(),
        "tool": tool,
        "args": list(args or []),
        "kwargs": dict(kwargs or {}),
        "context": dict(context or {}),
        "timeout_sec": timeout_sec,
    }


def make_response(
    request: dict[str, Any],
    *,
    transport_ok: bool,
    tool_ok: bool = False,
    text: str = "",
    error_code: str = "",
    recoverable: bool | None = None,
    suggested_fix: str = "",
    data: Any = None,
    warnings: list[str] | None = None,
    artifacts: list[str] | None = None,
    worker_pid: int | None = None,
    elapsed_sec: float | None = None,
    source_version: str = "",
) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "request_id": request.get("request_id", ""),
        "tool": request.get("tool", ""),
        "transport_ok": bool(transport_ok),
        "tool_ok": bool(tool_ok),
        "text": text,
        "error_code": error_code,
        "recoverable": recoverable,
        "suggested_fix": suggested_fix,
        "data": data,
        "warnings": list(warnings or []),
        "artifacts": list(artifacts or []),
        "worker_pid": worker_pid,
        "elapsed_sec": elapsed_sec,
        "source_version": source_version,
    }


def validate_request(request: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(request, dict):
        return False, "request must be an object"
    if request.get("protocol") != PROTOCOL_VERSION:
        return False, f"unsupported protocol: {request.get('protocol')!r}"
    tool = request.get("tool")
    if not isinstance(tool, str) or not tool:
        return False, "tool must be a non-empty string"
    if not tool.replace("_", "").isalnum() or tool[0].isdigit():
        return False, f"invalid tool name: {tool!r}"
    if not isinstance(request.get("args", []), list):
        return False, "args must be a list"
    if not isinstance(request.get("kwargs", {}), dict):
        return False, "kwargs must be an object"
    if not isinstance(request.get("context", {}), dict):
        return False, "context must be an object"
    timeout = request.get("timeout_sec")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            return False, "timeout_sec must be a number"
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value):
            return False, "timeout_sec must be finite"
        if timeout_value < MIN_TIMEOUT_SEC or timeout_value > MAX_TIMEOUT_SEC:
            return False, f"timeout_sec must be between {MIN_TIMEOUT_SEC} and {MAX_TIMEOUT_SEC}"
    return True, ""
