#!/usr/bin/env python3
"""Fast pre-commit smoke suite.

Runs the smallest test subset that still catches common breakage:
  - module import / compat surface regressions
  - daemon helper path/state regressions
"""
from __future__ import annotations

import os
import subprocess
import sys


TEST_MODULES = [
    "tests.test_smoke_imports",
    "tests.test_daemon_helpers",
]


def main() -> int:
    env = dict(os.environ)
    env.setdefault("AGENT_DAEMON_MODE", "1")
    cmd = [sys.executable, "-m", "unittest", "-q", *TEST_MODULES]
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
