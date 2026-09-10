#!/usr/bin/env python3
"""Create/upgrade RED's Postgres operational database schema."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.operational_db import SCHEMA_VERSION, ensure_schema, enabled


def main() -> int:
    if not enabled():
        print("RED_OPERATIONAL_DB_URL or RED_DATABASE_URL is required", file=sys.stderr)
        return 2
    ensure_schema()
    print(f"RED operational DB schema is ready at version {SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
