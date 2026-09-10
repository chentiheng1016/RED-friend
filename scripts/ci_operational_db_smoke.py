#!/usr/bin/env python3
"""CI-friendly smoke check for RED's operational Postgres schema.

This check starts a disposable local Postgres through rehearse_operational_db.py,
runs migration + health + dry-run backfill counts, and validates the machine
readable summary.  It never talks to cloud services.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PYTHON = ROOT / ".venv" / "bin" / "python"
if not PYTHON.is_file():
    PYTHON = Path(sys.executable)


def _postgres_available(postgres_bin: str) -> tuple[bool, str]:
    from scripts import rehearse_operational_db as rehearse

    try:
        rehearse._find_postgres_bin(postgres_bin)
    except SystemExit as exc:
        return False, str(exc)
    return True, ""


def _build_rehearsal_cmd(*, postgres_bin: str, only: str, dry_run_limit: int) -> list[str]:
    cmd = [
        str(PYTHON),
        "scripts/rehearse_operational_db.py",
        "--skip-write",
        "--json",
        "--dry-run-limit",
        str(max(0, dry_run_limit)),
    ]
    if postgres_bin:
        cmd.extend(["--postgres-bin", postgres_bin])
    if only and only != "all":
        cmd.extend(["--only", only])
    return cmd


def _validate_summary(summary: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    health = summary.get("health") if isinstance(summary.get("health"), dict) else {}
    db = health.get("operational_db") if isinstance(health.get("operational_db"), dict) else {}
    if not db.get("enabled"):
        issues.append("operational DB was not enabled during rehearsal")
    if not db.get("ok"):
        issues.append(f"operational DB health failed: {db.get('error') or 'unknown error'}")
    if health.get("schema_ok") is not True:
        issues.append("operational health reported schema_ok=false")

    version = db.get("schema_version")
    expected = db.get("schema_expected")
    if not isinstance(version, int) or not isinstance(expected, int) or version < expected:
        issues.append(f"schema version mismatch: {version}/{expected}")

    health_issues = health.get("issues") or []
    if health_issues:
        issues.append(f"operational health issues: {len(health_issues)}")

    dry_run = summary.get("dry_run") if isinstance(summary.get("dry_run"), dict) else {}
    dry_results = dry_run.get("results") if isinstance(dry_run.get("results"), list) else []
    error_sources = [
        str(row.get("source") or "?")
        for row in dry_results
        if isinstance(row, dict) and row.get("status") == "error"
    ]
    if error_sources:
        issues.append(f"dry-run source errors: {', '.join(error_sources)}")

    counts = summary.get("table_counts") if isinstance(summary.get("table_counts"), dict) else {}
    tables = counts.get("tables") if isinstance(counts.get("tables"), dict) else {}
    required_tables = {
        "red_schema_migrations",
        "red_backfill_events",
        "red_gemini_circuit_state",
    }
    missing = sorted(required_tables - set(tables))
    if missing:
        issues.append(f"missing expected table(s): {', '.join(missing)}")

    migrations = counts.get("schema_migrations") if isinstance(counts.get("schema_migrations"), dict) else {}
    max_version = migrations.get("max_version")
    if isinstance(expected, int) and max_version != expected:
        issues.append(f"schema_migrations max_version mismatch: {max_version}/{expected}")
    return issues


def _print_json(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", default="", help="directory containing initdb/pg_ctl/createdb")
    parser.add_argument("--only", default="all", help="backfill source list for dry-run counts")
    parser.add_argument("--dry-run-limit", type=int, default=100, help="dry-run row limit; 0 = all")
    parser.add_argument("--require-postgres", action="store_true", help="fail instead of skip when Postgres binaries are missing")
    parser.add_argument("--json", action="store_true", help="emit machine-readable smoke result")
    args = parser.parse_args(argv)

    available, reason = _postgres_available(args.postgres_bin)
    if not available:
        result = {
            "status": "skipped",
            "reason": reason,
            "required": bool(args.require_postgres),
        }
        if args.json:
            _print_json(result)
        else:
            print(f"operational DB smoke: skipped ({reason})")
        return 2 if args.require_postgres else 0

    cmd = _build_rehearsal_cmd(
        postgres_bin=args.postgres_bin,
        only=args.only,
        dry_run_limit=args.dry_run_limit,
    )
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        result = {
            "status": "failed",
            "phase": "rehearsal",
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if args.json:
            _print_json(result)
        else:
            print("operational DB smoke: rehearsal failed", file=sys.stderr)
            if completed.stdout:
                print(completed.stdout, file=sys.stderr)
            if completed.stderr:
                print(completed.stderr, file=sys.stderr)
        return completed.returncode or 1

    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        result = {
            "status": "failed",
            "phase": "parse",
            "error": str(exc),
            "stdout": completed.stdout,
        }
        if args.json:
            _print_json(result)
        else:
            print(f"operational DB smoke: invalid rehearsal JSON: {exc}", file=sys.stderr)
        return 1

    issues = _validate_summary(summary)
    result = {
        "status": "failed" if issues else "passed",
        "issues": issues,
        "summary": summary,
    }
    if args.json:
        _print_json(result)
    elif issues:
        print("operational DB smoke: failed", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
    else:
        db = summary.get("health", {}).get("operational_db", {})
        counts = summary.get("table_counts", {})
        print(
            "operational DB smoke: ok "
            f"schema={db.get('schema_version')}/{db.get('schema_expected')} "
            f"tables={counts.get('table_count')}"
        )
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
