#!/usr/bin/env python3
"""Phase 4 — Employee web portal via FastAPI + uvicorn.

Triggered by: launchd com.xiaohong.web_server (port 8080)
Also callable via: python launchd/scripts/web_server.py
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import rotate_log

def main():
    rotate_log("web_server")
    import uvicorn
    port = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8080")))
    uvicorn.run(
        "agent_core.web_server.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=port,
        log_level="info",
    )

if __name__ == "__main__":
    main()
