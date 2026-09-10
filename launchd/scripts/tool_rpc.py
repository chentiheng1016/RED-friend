#!/usr/bin/env python3
"""Launchd entrypoint for the local tool RPC server."""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core.daemon_helpers import rotate_log
from agent_core.tool_rpc_server import serve_forever


def main() -> int:
    rotate_log("tool_rpc")
    serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
