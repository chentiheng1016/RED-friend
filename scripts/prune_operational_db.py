#!/usr/bin/env python3
"""Prune old rows from RED's Postgres operational database."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core import operational_db


@dataclass(frozen=True)
class PruneSpec:
    name: str
    table: str
    where: str
    days: int
    cutoff_type: str = "timestamp"


SPECS: tuple[PruneSpec, ...] = (
    PruneSpec("audit_events", "red_audit_events", "logged_at < %s", 180),
    PruneSpec(
        "completed_task_queue",
        "red_task_queue",
        "state IN ('done', 'cancelled') AND ended_at IS NOT NULL AND ended_at < %s",
        14,
    ),
    PruneSpec("task_dead_letters", "red_task_dead_letters", "moved_to_dlq_at < %s", 30),
    PruneSpec("telegram_auth_state", "red_tg_auth_state", "updated_at < %s", 30),
    PruneSpec(
        "resolved_join_requests",
        "red_telegram_join_requests",
        "status <> 'pending' AND resolved_at IS NOT NULL AND resolved_at < %s",
        180,
    ),
    PruneSpec("tool_budget_usage", "red_tool_budget_usage", "day < %s", 45, "date"),
    PruneSpec("cost_events", "red_cost_events", "ts < %s", 365),
    PruneSpec("api_error_events", "red_api_error_events", "ts < %s", 180),
    PruneSpec("run_history", "red_run_history", "started_at < %s", 180),
    PruneSpec("policy_decisions", "red_policy_decisions", "ts < %s", 180),
    PruneSpec("work_mode_history", "red_work_mode_history", "at < %s", 180),
    PruneSpec("intent_classifications", "red_intent_classifications", "ts < %s", 90),
)


def _cutoff(spec: PruneSpec, override_days: int | None) -> date | datetime:
    days = max(1, int(override_days or spec.days))
    if spec.cutoff_type == "date":
        return date.today() - timedelta(days=days)
    return datetime.now().astimezone() - timedelta(days=days)


def _selected(raw: str) -> list[PruneSpec]:
    if not raw or raw.strip().lower() == "all":
        return list(SPECS)
    names = [item.strip() for item in raw.split(",") if item.strip()]
    by_name = {spec.name: spec for spec in SPECS}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise SystemExit(f"unknown --only spec(s): {', '.join(unknown)}")
    return [by_name[name] for name in names]


def _count(cur, spec: PruneSpec, cutoff: Any) -> int:
    cur.execute(f"SELECT count(*) FROM {spec.table} WHERE {spec.where}", (cutoff,))
    row = cur.fetchone()
    return int((row or (0,))[0] or 0)


def _delete(cur, spec: PruneSpec, cutoff: Any) -> int:
    cur.execute(f"DELETE FROM {spec.table} WHERE {spec.where}", (cutoff,))
    return int(getattr(cur, "rowcount", 0) or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually delete rows; default is dry-run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force dry-run even if --execute is present",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=0,
        help="override the default retention window for selected specs",
    )
    parser.add_argument(
        "--only",
        default="all",
        help="comma-separated spec list, or all",
    )
    args = parser.parse_args(argv)
    dry_run = args.dry_run or not args.execute

    if not operational_db.enabled():
        print("RED_OPERATIONAL_DB_URL or RED_DATABASE_URL is required", file=sys.stderr)
        return 2

    operational_db.ensure_schema()
    selected = _selected(args.only)
    rows: list[dict[str, Any]] = []
    with operational_db.connect() as conn:
        with conn.cursor() as cur:
            for spec in selected:
                cutoff = _cutoff(spec, args.older_than_days or None)
                count = _count(cur, spec, cutoff)
                deleted = 0 if dry_run else _delete(cur, spec, cutoff)
                rows.append({
                    "name": spec.name,
                    "table": spec.table,
                    "cutoff": str(cutoff),
                    "matching_rows": count,
                    "deleted_rows": deleted,
                    "dry_run": dry_run,
                })

    print(json.dumps({"dry_run": dry_run, "results": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
