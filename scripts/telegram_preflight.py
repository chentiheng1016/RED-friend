#!/usr/bin/env python3
"""Run Telegram setup preflight checks for RED."""
from __future__ import annotations

import argparse
import json
import os
import sys


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run RED Telegram preflight checks.")
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="Call Telegram getMe to verify the bot token against the live API.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a text report.",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    from agent_core.telegram_preflight import format_preflight_report, run_preflight

    result = run_preflight(check_api=args.check_api)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_preflight_report(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
