"""Fresh-process wrappers for high-signal diagnostic tools.

Long-lived daemons keep imported function objects in memory. For cheap
observability tools, a short subprocess is worth the small latency: it imports
the latest code from disk and avoids stale dashboard logic inside Telegram.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from agent_core.env_utils import env_float as _env_float
from agent_core.logging_and_paths import REPO_ROOT

_FRESH_TIMEOUT_S = _env_float("RED_FRESH_DIAGNOSTICS_TIMEOUT_S", 25, min_value=1, max_value=300)


def _fresh_call(module: str, func: str, arg: Any, fallback) -> str:
    """Run `module.func(arg)` in a new Python process, with safe fallback."""
    code = (
        "import importlib, json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "arg = json.loads(sys.argv[1])\n"
        "mod = importlib.import_module(sys.argv[2])\n"
        "fn = getattr(mod, sys.argv[3])\n"
        "out = fn(arg)\n"
        "sys.stdout.write(str(out))\n"
    )
    env = os.environ.copy()
    env.setdefault("AGENT_DAEMON_MODE", "1")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, json.dumps(arg, ensure_ascii=False), module, func],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=_FRESH_TIMEOUT_S,
        )
        if proc.returncode == 0:
            return proc.stdout
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"fresh subprocess exit {proc.returncode}: {err}")
    except Exception as exc:
        return (
            f"⚠️ fresh diagnostic subprocess failed: {type(exc).__name__}: {exc}\n"
            "Fallback in-process result follows.\n\n"
            f"{fallback(arg)}"
        )


def system_status(sections: str = "") -> str:
    """🖥️ RED 統一控制台：一覽系統健康（fresh subprocess 版本）。

    Args:
        sections: 逗號分隔的 section name；空字串=全部。

    Returns:
        formatted dashboard 文字。
    """
    from agent_core.dashboard import system_status as _fallback
    return _fresh_call("agent_core.dashboard", "system_status", sections, _fallback)


def system_alerts(min_level: str = "warn") -> str:
    """🚨 RED 警示總覽（fresh subprocess 版本）。

    Args:
        min_level: 最低顯示級別，'warn' 或 'crit'。

    Returns:
        formatted alert list。
    """
    from agent_core.dashboard_alerts import system_alerts as _fallback
    return _fresh_call("agent_core.dashboard_alerts", "system_alerts", min_level, _fallback)
