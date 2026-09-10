"""Cloud Run entrypoint for RED container images."""
from __future__ import annotations

import os
import sys

# 刻意不 clamp：PORT 由部署環境全權決定（共用版不帶 min/max 即無 clamp）。
from agent_core.env_utils import env_int as _int_env


def _run_web() -> None:
    import uvicorn

    uvicorn.run(
        "agent_core.web_server.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=_int_env("PORT", 8080),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


def _run_daemon_task(task_name: str) -> None:
    os.environ.setdefault("AGENT_DAEMON_MODE", "1")
    import agent_daemon

    sys.argv = ["agent_daemon.py", "--task", task_name]
    agent_daemon.main()


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    role = (os.environ.get("RED_CLOUD_ROLE") or (args[0] if args else "web")).strip().lower()

    os.environ.setdefault("RED_CLOUD_MODE", "1")
    os.environ.setdefault("RED_EXCHANGE_MODE", "telegram_only")
    os.environ.setdefault("AGENT_DAEMON_MODE", "1")

    if role in {"web", "server", "portal"}:
        _run_web()
        return

    if role in {"telegram", "telegram_bot"}:
        _run_daemon_task("telegram_bot")
        return

    if role in {"task", "daemon-task", "daemon_task"}:
        task = os.environ.get("RED_DAEMON_TASK") or (args[1] if len(args) > 1 else "")
        if not task:
            raise SystemExit("RED_DAEMON_TASK is required when RED_CLOUD_ROLE=task")
        _run_daemon_task(task)
        return

    raise SystemExit(f"Unknown RED_CLOUD_ROLE: {role}")


if __name__ == "__main__":
    main()
