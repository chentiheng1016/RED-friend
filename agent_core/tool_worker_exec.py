"""Execute one tool call in a fresh subprocess.

The RPC server intentionally stays thin. It delegates each call here so tool
imports, C-extension hangs, memory leaks, and source-code freshness are scoped
to one worker process.
"""
from __future__ import annotations

import contextlib
import functools
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from agent_core.env_utils import env_float as _env_float
from agent_core.tool_rpc_protocol import MAX_TIMEOUT_SEC, MIN_TIMEOUT_SEC, make_response, validate_request

DEFAULT_TIMEOUT_SEC = int(_env_float(
    "RED_TOOL_WORKER_TIMEOUT_S",
    300,
    min_value=MIN_TIMEOUT_SEC,
    max_value=MAX_TIMEOUT_SEC,
))
CANCEL_TERM_GRACE_SEC = _env_float("RED_TOOL_WORKER_CANCEL_TERM_GRACE_S", 2, min_value=0, max_value=60)
KILL_GRACE_SEC = _env_float("RED_TOOL_WORKER_KILL_GRACE_S", 2, min_value=0, max_value=60)


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[1])


def _source_version() -> str:
    """Compact source fingerprint for observability.

    Git may be unavailable in launchd environments, so this falls back to the
    newest agent_core mtime. It is not a security primitive.
    """
    root = _repo_root()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    newest = 0
    try:
        for p in Path(root, "agent_core").rglob("*.py"):
            try:
                newest = max(newest, p.stat().st_mtime_ns)
            except OSError:
                pass
    except Exception:
        pass
    return f"mtime:{newest}"


def _tool_result_meta(result: Any) -> dict[str, Any]:
    text = str(result)
    tool_ok = True
    error_code = ""
    recoverable = None
    suggested_fix = ""
    data = None
    warnings: list[str] = []
    artifacts: list[str] = []

    if hasattr(result, "ok"):
        tool_ok = bool(getattr(result, "ok", True))
        error_code = str(getattr(result, "error_code", "") or "")
        recoverable = bool(getattr(result, "recoverable", True))
        suggested_fix = str(getattr(result, "suggested_fix", "") or "")
        data = getattr(result, "data", None)
        warnings = list(getattr(result, "warnings", []) or [])
        artifacts = list(getattr(result, "artifacts", []) or [])
    else:
        try:
            from agent_core.tool_result import classify_string_result, ErrorCode
            tool_ok, error_code = classify_string_result(text)
            recoverable = ErrorCode.is_recoverable(error_code) if error_code else None
        except Exception:
            pass

    return {
        "tool_ok": tool_ok,
        "text": text,
        "error_code": error_code,
        "recoverable": recoverable,
        "suggested_fix": suggested_fix,
        "data": data,
        "warnings": warnings,
        "artifacts": artifacts,
    }


@functools.lru_cache(maxsize=1)
def _worker_builtin_tools_by_name() -> dict[str, Any]:
    from agent_core.skills import list_skills
    from agent_core.tool_registry_catalog import build_builtin_tools

    tools_by_name: dict[str, Any] = {}
    for fn in build_builtin_tools(extra_tools=[list_skills]):
        name = getattr(fn, "__name__", "")
        if name:
            tools_by_name.setdefault(name, fn)
    return tools_by_name


def _resolve_tool(tool_name: str):
    """Resolve from the raw registry inside the worker process."""
    try:
        fn = _worker_builtin_tools_by_name().get(tool_name)
        if fn is not None:
            return fn
    except Exception:
        pass

    from agent_core.tool_registry import tools_list

    for fn in tools_list:
        if getattr(fn, "__name__", "") == tool_name:
            return fn
    return None


def _apply_worker_policy(fn, request: dict[str, Any]):
    """Apply server-side policy without Telegram token checks.

    Telegram confirmation remains in the Telegram process. The worker uses the
    daemon channel to re-check LOCKED/policy/env/budget/audit rules and to keep
    queue execution behavior consistent with the old in-process queue worker.
    """
    tool_name = getattr(fn, "__name__", request.get("tool", ""))
    context = request.get("context") or {}
    channel = str(context.get("worker_channel") or "daemon")
    try:
        from agent_core.tg_auth import is_sensitive, wrap_sensitive_tool
        if is_sensitive(tool_name):
            return wrap_sensitive_tool(fn, get_chat_id=lambda: "", channel=channel)
    except Exception:
        pass
    return fn


def execute_request(request: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    ok, err = validate_request(request)
    if not ok:
        return make_response(
            request,
            transport_ok=False,
            tool_ok=False,
            text=f"❌ tool worker request invalid: {err}",
            error_code="invalid_input",
            recoverable=False,
            worker_pid=os.getpid(),
            elapsed_sec=round(time.time() - started, 3),
            source_version=_source_version(),
        )

    tool_name = request["tool"]
    try:
        fn = _resolve_tool(tool_name)
        if fn is None:
            return make_response(
                request,
                transport_ok=True,
                tool_ok=False,
                text=f"❌ tool `{tool_name}` not found in worker registry",
                error_code="not_found",
                recoverable=False,
                worker_pid=os.getpid(),
                elapsed_sec=round(time.time() - started, 3),
                source_version=_source_version(),
            )
        fn = _apply_worker_policy(fn, request)
        result = fn(*request.get("args", []), **request.get("kwargs", {}))
        meta = _tool_result_meta(result)
        return make_response(
            request,
            transport_ok=True,
            worker_pid=os.getpid(),
            elapsed_sec=round(time.time() - started, 3),
            source_version=_source_version(),
            **meta,
        )
    except BaseException as exc:
        try:
            from agent_core.tool_result import classify_exception, ErrorCode
            code = classify_exception(exc)
            recoverable = ErrorCode.is_recoverable(code)
        except Exception:
            code = "internal"
            recoverable = False
        tb = traceback.format_exc()[-1800:]
        return make_response(
            request,
            transport_ok=True,
            tool_ok=False,
            text=f"❌ tool `{tool_name}` crashed: {type(exc).__name__}: {exc}\n{tb}",
            error_code=code,
            recoverable=recoverable,
            worker_pid=os.getpid(),
            elapsed_sec=round(time.time() - started, 3),
            source_version=_source_version(),
        )


def _signal_worker_process(proc: subprocess.Popen, sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except Exception:
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            pass


def _finish_stopped_worker(
    proc: subprocess.Popen,
    th: threading.Thread,
    *,
    killed_reason: str,
) -> None:
    if killed_reason == "cancelled":
        th.join(timeout=CANCEL_TERM_GRACE_SEC)
        if proc.poll() is None:
            _signal_worker_process(proc, signal.SIGKILL)
    th.join(timeout=KILL_GRACE_SEC)


def run_worker_subprocess(
    request: dict[str, Any],
    *,
    timeout_sec: int | float | None = None,
    cancel_event: Any = None,
) -> dict[str, Any]:
    """Run this module as a subprocess and return its response dict."""
    ok, err = validate_request(request)
    if not ok:
        return make_response(
            request if isinstance(request, dict) else {"request_id": "", "tool": "", "protocol": 1},
            transport_ok=False,
            text=f"❌ invalid worker request: {err}",
            error_code="invalid_input",
            recoverable=False,
        )
    timeout_source = timeout_sec if timeout_sec is not None else request.get("timeout_sec") or DEFAULT_TIMEOUT_SEC
    if isinstance(timeout_source, bool):
        return make_response(
            request,
            transport_ok=False,
            text="❌ invalid worker timeout: timeout_sec must be a number",
            error_code="invalid_input",
            recoverable=False,
        )
    try:
        timeout = float(timeout_source)
    except Exception as exc:
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ invalid worker timeout: {type(exc).__name__}: {exc}",
            error_code="invalid_input",
            recoverable=False,
        )
    if not math.isfinite(timeout) or timeout < MIN_TIMEOUT_SEC or timeout > MAX_TIMEOUT_SEC:
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ invalid worker timeout: must be between {MIN_TIMEOUT_SEC} and {MAX_TIMEOUT_SEC}",
            error_code="invalid_input",
            recoverable=False,
        )
    env = os.environ.copy()
    env.setdefault("AGENT_DAEMON_MODE", "1")
    env["RED_TOOL_RPC_DISABLE_PROXY"] = "1"
    env["RED_TOOL_RPC_WORKER"] = "1"
    env["RED_TOOL_RPC_CONTEXT_JSON"] = json.dumps(request.get("context") or {}, ensure_ascii=False)
    cmd = [sys.executable, "-m", "agent_core.tool_worker_exec"]
    proc = subprocess.Popen(
        cmd,
        cwd=_repo_root(),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    payload = json.dumps(request, ensure_ascii=False)
    box: dict[str, Any] = {"stdout": "", "stderr": "", "exc": None}

    def _communicate() -> None:
        try:
            out, err = proc.communicate(payload)
            box["stdout"] = out or ""
            box["stderr"] = err or ""
        except BaseException as exc:  # pragma: no cover - defensive
            box["exc"] = exc

    th = threading.Thread(target=_communicate, daemon=True)
    th.start()
    deadline = time.time() + timeout
    killed_reason = ""
    while th.is_alive():
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            killed_reason = "cancelled"
            _signal_worker_process(proc, signal.SIGTERM)
            break
        if time.time() >= deadline:
            killed_reason = "timeout"
            _signal_worker_process(proc, signal.SIGKILL)
            break
        th.join(timeout=0.05)
    _finish_stopped_worker(proc, th, killed_reason=killed_reason)
    if killed_reason == "cancelled":
        return make_response(
            request,
            transport_ok=False,
            text="❌ tool worker cancelled by user",
            error_code="cancelled",
            recoverable=False,
            worker_pid=proc.pid,
        )
    if killed_reason == "timeout":
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ tool worker timeout after {int(timeout)}s",
            error_code="timeout",
            recoverable=True,
            worker_pid=proc.pid,
        )
    if box.get("exc") is not None:
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ tool worker communicate failed: {box['exc']}",
            error_code="internal",
            recoverable=True,
            worker_pid=proc.pid,
        )
    stdout = str(box.get("stdout") or "")
    stderr = str(box.get("stderr") or "")
    if proc.returncode != 0:
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ tool worker exited {proc.returncode}: {stderr[-1800:]}",
            error_code="internal",
            recoverable=True,
            worker_pid=proc.pid,
        )
    try:
        return json.loads(stdout)
    except Exception:
        return make_response(
            request,
            transport_ok=False,
            text=f"❌ tool worker returned non-JSON output: {stdout[-1000:]}\n{stderr[-1000:]}",
            error_code="internal",
            recoverable=True,
            worker_pid=proc.pid,
        )


def _dump_response(response: dict[str, Any]) -> str:
    """Worker 回應唯一的序列化點。

    default=str：ToolResult.data 可能夾非 JSON-serializable 的物件
    （datetime / Path / set…）。沒有 default 時 json.dumps 直接 raise →
    成功執行完的工具被回報成 transport 失敗（exit non-zero），還會疊加
    caller 端的 fallback 重跑副作用問題。"""
    return json.dumps(response, ensure_ascii=False, default=str)


def main() -> int:
    request_text = sys.stdin.read()
    try:
        request = json.loads(request_text)
    except Exception as exc:
        request = {"request_id": "", "tool": "", "protocol": 1}
        response = make_response(
            request,
            transport_ok=False,
            text=f"❌ invalid worker JSON: {exc}",
            error_code="invalid_input",
            recoverable=False,
            worker_pid=os.getpid(),
            source_version=_source_version(),
        )
    else:
        # Tool code may print; keep stdout reserved for the final JSON response.
        original_stdout = sys.stdout
        with contextlib.redirect_stdout(sys.stderr):
            response = execute_request(request)
        print(_dump_response(response), file=original_stdout)
        return 0
    print(_dump_response(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
