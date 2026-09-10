"""Proxy existing Gemini-callable tools through ToolRunner without losing schema."""
from __future__ import annotations

import functools
import inspect
import os
from typing import Any, Callable


_COPY_ATTRS = (
    "_is_skill",
    "_is_mcp_tool",
    "_mcp_server",
    "_mcp_tool",
    "background_safe",
    "_audited",
    "_dry_run_wrapped",
)


def _tool_name(fn: Callable) -> str:
    return getattr(fn, "__name__", "") or getattr(fn, "__qualname__", "") or ""


def _bind_kwargs(fn: Callable, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    try:
        sig = inspect.signature(fn)
        bound = sig.bind_partial(*args, **kwargs)
        return [], dict(bound.arguments)
    except Exception:
        return list(args), dict(kwargs)


def _should_proxy_tool(fn: Callable, mode: str) -> bool:
    name = _tool_name(fn)
    if not name:
        return False
    # actor-scoped 工具（telegram 多使用者區隔）綁定了「員工身分」的閉包，
    # 不能 by-name 派發到 worker — worker 會用同名的大王版本，等於破功。
    # 一律留在本地程序內執行，無視 proxy mode。
    if getattr(fn, "_actor_scoped", False):
        return False
    if os.environ.get("RED_TOOL_RPC_DISABLE_PROXY") == "1":
        return False
    mode = (mode or "none").strip().lower()
    if mode in {"0", "off", "none", "false"}:
        return False
    if mode in {"1", "all", "true"}:
        return True
    try:
        from agent_core.tool_tiers import get_tier, TIER_SAFE
        tier = get_tier(name)
    except Exception:
        tier = "safe"
    if mode == "safe":
        return tier == "safe"
    if mode == "nonlocked":
        return tier != "locked"
    if mode in {"diagnostic", "diagnostics"}:
        return name in {
            "system_status",
            "system_alerts",
            "list_runs",
            "find_past_actions",
            "run_history_stats",
            "tool_rpc_status",
            "tool_runner_status",
        }
    return False


def make_rpc_proxy(
    fn: Callable,
    *,
    caller: str = "",
    worker_channel: str = "daemon",
    timeout_sec: int | float | None = None,
) -> Callable:
    name = _tool_name(fn)
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def proxy(*args, **kwargs):
        call_args, call_kwargs = _bind_kwargs(fn, args, kwargs)
        from agent_core.tool_runner import call_tool
        return call_tool(
            name,
            call_kwargs,
            args=call_args,
            context={
                "caller": caller,
                "source_tool": name,
                "worker_channel": worker_channel,
            },
            timeout_sec=timeout_sec,
            prefer_rpc=True,
        )

    proxy.__name__ = name
    proxy.__qualname__ = name
    proxy.__doc__ = getattr(fn, "__doc__", None)
    proxy.__annotations__ = dict(getattr(fn, "__annotations__", {}) or {})
    proxy.__signature__ = sig  # type: ignore[attr-defined]
    proxy._tool_rpc_proxy = True  # type: ignore[attr-defined]
    proxy._tool_rpc_original = fn  # type: ignore[attr-defined]
    for attr in _COPY_ATTRS:
        if hasattr(fn, attr):
            setattr(proxy, attr, getattr(fn, attr))
    return proxy


def proxy_tools(
    tools: list[Callable],
    *,
    caller: str = "",
    worker_channel: str = "daemon",
    mode: str | None = None,
    timeout_sec: int | float | None = None,
) -> list[Callable]:
    proxy_mode = mode if mode is not None else os.environ.get("RED_TOOL_RPC_PROXY_MODE", "diagnostics")
    out: list[Callable] = []
    proxied = 0
    for fn in tools:
        if getattr(fn, "_tool_rpc_proxy", False):
            out.append(fn)
            continue
        if getattr(fn, "_tg_auth_wrapped", False):
            out.append(fn)
            continue
        if _should_proxy_tool(fn, proxy_mode):
            out.append(make_rpc_proxy(
                fn,
                caller=caller,
                worker_channel=worker_channel,
                timeout_sec=timeout_sec,
            ))
            proxied += 1
        else:
            out.append(fn)
    if proxied and os.environ.get("AGENT_DAEMON_MODE") != "1":
        print(f"[tool_proxy] proxied {proxied}/{len(tools)} tools via ToolRunner (mode={proxy_mode})")
    return out
