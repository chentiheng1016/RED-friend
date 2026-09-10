#!/usr/bin/env python3
"""Run a disposable local Postgres rehearsal for RED operational DB.

The rehearsal starts a temporary local Postgres cluster, runs the operational
schema migration, performs a dry-run count, optionally writes a bounded
backfill, prints table counts, and then removes the cluster.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
if not (PYTHON.is_file() and os.access(PYTHON, os.X_OK)):
    PYTHON = Path(sys.executable)
POSTGRES_BACKEND_ENVS = {
    "RED_OPERATIONAL_DB_POOL": "1",
    "RED_OPERATIONAL_DB_POOL_MIN_SIZE": "0",
    "RED_OPERATIONAL_DB_POOL_MAX_SIZE": "4",
    "RED_OPERATIONAL_DB_CONNECT_TIMEOUT_SEC": "2",
    "RED_TASK_QUEUE_BACKEND": "postgres",
    "RED_TELEGRAM_APPROVALS_BACKEND": "postgres",
    "RED_TELEGRAM_AUTH_BACKEND": "postgres",
    "RED_TOOL_BUDGETS_BACKEND": "postgres",
    "RED_COST_TRACKER_BACKEND": "postgres",
    "RED_RUN_HISTORY_BACKEND": "postgres",
    "RED_TASK_MEMORY_BACKEND": "postgres",
    "RED_EDGE_TASKS_BACKEND": "postgres",
    "RED_POLICY_ENGINE_BACKEND": "postgres",
    "RED_WORK_MODE_BACKEND": "postgres",
    "RED_DRY_RUN_BACKEND": "postgres",
    "RED_WEB_DOMAIN_POLICY_BACKEND": "postgres",
    "RED_INTENT_ROUTER_BACKEND": "postgres",
    "RED_ALERT_PUSH_BACKEND": "postgres",
    "RED_GEMINI_CIRCUIT_BACKEND": "postgres",
    "RED_GEMINI_CIRCUIT_BREAKER": "1",
}


def _postgres_bin_candidates() -> list[Path]:
    candidates: list[Path] = []
    for value in (
        os.environ.get("RED_POSTGRES_BIN"),
        "/opt/homebrew/opt/postgresql@16/bin",
        "/opt/homebrew/opt/postgresql/bin",
        "/usr/local/opt/postgresql@16/bin",
        "/usr/local/opt/postgresql/bin",
    ):
        if value:
            candidates.append(Path(value))
    initdb = shutil.which("initdb")
    if initdb:
        candidates.append(Path(initdb).resolve().parent)
    return candidates


def _find_postgres_bin(explicit: str) -> Path:
    candidates = [Path(explicit)] if explicit else _postgres_bin_candidates()
    required = ("initdb", "pg_ctl", "createdb")
    for candidate in candidates:
        if all((candidate / name).is_file() and os.access(candidate / name, os.X_OK) for name in required):
            return candidate
    searched = ", ".join(str(item) for item in candidates) or "(none)"
    raise SystemExit(
        "Could not find Postgres binaries. Install postgresql@16 or pass "
        f"--postgres-bin. Searched: {searched}"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    quiet: bool = False,
    capture: bool = False,
) -> str:
    if capture:
        completed = subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout
    stdout = subprocess.DEVNULL if quiet else None
    subprocess.run(cmd, cwd=ROOT, env=env, check=True, stdout=stdout)
    return ""


def _print_header(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def _env_for_rehearsal(port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(POSTGRES_BACKEND_ENVS)
    env["RED_OPERATIONAL_DB_URL"] = f"postgresql://red@127.0.0.1:{port}/red"
    return env


def _health_report(env: dict[str, str], *, json_mode: bool = False) -> dict[str, object] | None:
    if json_mode:
        code = """
import json
from agent_core.operational_health import health_report, health_issues
r = health_report()
issues = health_issues(cloud_runtime=True)
print(json.dumps({
    "operational_db": r.get("operational_db") or {},
    "schema_ok": r.get("schema_ok"),
    "enabled_backends": r.get("enabled_backends") or [],
    "enabled_backend_count": len(r.get("enabled_backends") or []),
    "configured_backends": r.get("configured_backends") or [],
    "local_fallback_backends": r.get("local_fallback_backends") or [],
    "issues": issues,
}, ensure_ascii=False, default=str))
"""
        return json.loads(_run([str(PYTHON), "-c", code], env=env, capture=True))

    code = """
from agent_core.operational_health import health_report, health_issues
r = health_report()
db = r["operational_db"]
print("db_enabled", db.get("enabled"))
print("db_ok", db.get("ok"))
print("schema", f"{db.get('schema_version')}/{db.get('schema_expected')}")
print("schema_ok", r.get("schema_ok"))
print("enabled_backend_count", len(r.get("enabled_backends") or []))
issues = health_issues(cloud_runtime=True)
print("issues", len(issues))
for item in issues:
    print(item["severity"], item["area"], item["msg"])
"""
    _run([str(PYTHON), "-c", code], env=env)
    return None


def _table_counts(env: dict[str, str], *, json_mode: bool = False) -> dict[str, object] | None:
    if json_mode:
        code = """
import json
import os
import psycopg

url = os.environ["RED_OPERATIONAL_DB_URL"]
with psycopg.connect(url, connect_timeout=2) as conn:
    with conn.cursor() as cur:
        cur.execute("select max(version), count(*) from red_schema_migrations")
        max_version, migration_rows = cur.fetchone()
        cur.execute(\"\"\"
            select table_name
            from information_schema.tables
            where table_schema = 'public' and table_name like 'red_%'
            order by table_name
        \"\"\")
        tables = [row[0] for row in cur.fetchall()]
        counts = {}
        for table in tables:
            cur.execute(f"select count(*) from {table}")
            counts[table] = cur.fetchone()[0]
print(json.dumps({
    "schema_migrations": {"max_version": max_version, "rows": migration_rows},
    "table_count": len(tables),
    "tables": counts,
}, ensure_ascii=False, default=str))
"""
        return json.loads(_run([str(PYTHON), "-c", code], env=env, capture=True))

    code = """
import os
import psycopg

url = os.environ["RED_OPERATIONAL_DB_URL"]
with psycopg.connect(url, connect_timeout=2) as conn:
    with conn.cursor() as cur:
        cur.execute("select max(version), count(*) from red_schema_migrations")
        max_version, migration_rows = cur.fetchone()
        print(f"schema_migrations max_version={max_version} rows={migration_rows}")
        cur.execute(\"\"\"
            select table_name
            from information_schema.tables
            where table_schema = 'public' and table_name like 'red_%'
            order by table_name
        \"\"\")
        tables = [row[0] for row in cur.fetchall()]
        print("table_count", len(tables))
        for table in tables:
            cur.execute(f"select count(*) from {table}")
            print(f"{table} {cur.fetchone()[0]}")
"""
    _run([str(PYTHON), "-c", code], env=env)
    return None


def _backfill_args(*, dry_run: bool, only: str, limit: int, json_mode: bool = False) -> list[str]:
    args = [str(PYTHON), "scripts/backfill_operational_db.py"]
    if dry_run:
        args.append("--dry-run")
    if json_mode:
        args.append("--json")
    if only and only != "all":
        args.extend(["--only", only])
    if limit > 0:
        args.extend(["--limit", str(limit)])
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", default="", help="directory containing initdb/pg_ctl/createdb")
    parser.add_argument("--only", default="all", help="backfill source list, or all")
    parser.add_argument("--dry-run-limit", type=int, default=0, help="dry-run row limit; 0 = all")
    parser.add_argument("--write-limit", type=int, default=5000, help="bounded write limit; 0 = all")
    parser.add_argument("--skip-write", action="store_true", help="run migration and dry-run only")
    parser.add_argument("--keep-temp", action="store_true", help="leave the temporary cluster on disk")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON summary")
    args = parser.parse_args(argv)

    pg_bin = _find_postgres_bin(args.postgres_bin)
    tmp_root = Path(tempfile.mkdtemp(prefix="red-opdb."))
    data_dir = tmp_root / "data"
    port = _free_port()
    env = _env_for_rehearsal(port)
    server_started = False
    summary: dict[str, object] = {
        "postgres_bin": str(pg_bin),
        "python": str(PYTHON),
        "temp_root": str(tmp_root),
        "port": port,
        "only": args.only,
        "dry_run_limit": max(0, args.dry_run_limit),
        "write_limit": max(0, args.write_limit),
        "skip_write": bool(args.skip_write),
        "kept_temp": bool(args.keep_temp),
    }

    if not args.json:
        print(f"postgres_bin={pg_bin}")
        print(f"python={PYTHON}")
        print(f"temp_root={tmp_root}")
        print(f"port={port}")

    try:
        _run([str(pg_bin / "initdb"), "-D", str(data_dir), "-A", "trust", "-U", "red", "--no-instructions"], quiet=True)
        _run([
            str(pg_bin / "pg_ctl"),
            "-D",
            str(data_dir),
            "-l",
            str(tmp_root / "postgres.log"),
            "-o",
            f"-F -p {port} -k {tmp_root} -h 127.0.0.1",
            "-w",
            "start",
        ], quiet=True)
        server_started = True
        _run([str(pg_bin / "createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "red", "red"], quiet=True)

        if args.json:
            migrate_out = _run(
                [str(PYTHON), "scripts/migrate_operational_db.py"],
                env=env,
                capture=True,
            )
            summary["migrate"] = {"ok": True, "stdout": migrate_out.strip()}
            summary["health"] = _health_report(env, json_mode=True)
            dry_run_out = _run(
                _backfill_args(
                    dry_run=True,
                    only=args.only,
                    limit=max(0, args.dry_run_limit),
                    json_mode=True,
                ),
                env=env,
                capture=True,
            )
            summary["dry_run"] = json.loads(dry_run_out)
        else:
            _print_header("migrate")
            _run([str(PYTHON), "scripts/migrate_operational_db.py"], env=env)

            _print_header("health")
            _health_report(env)

            _print_header("dry-run counts")
            _run(_backfill_args(dry_run=True, only=args.only, limit=max(0, args.dry_run_limit)), env=env)

        if not args.skip_write:
            if args.json:
                write_out = _run(
                    _backfill_args(
                        dry_run=False,
                        only=args.only,
                        limit=max(0, args.write_limit),
                        json_mode=True,
                    ),
                    env=env,
                    capture=True,
                )
                summary["write_backfill"] = json.loads(write_out)
                summary["table_counts"] = _table_counts(env, json_mode=True)
            else:
                _print_header(f"bounded write backfill limit={max(0, args.write_limit)}")
                _run(_backfill_args(dry_run=False, only=args.only, limit=max(0, args.write_limit)), env=env)

                _print_header("red table counts")
                _table_counts(env)
        else:
            summary["write_backfill"] = {"skipped": True}
            if args.json:
                summary["table_counts"] = _table_counts(env, json_mode=True)
    finally:
        if server_started:
            subprocess.run(
                [str(pg_bin / "pg_ctl"), "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if args.keep_temp:
            if not args.json:
                print(f"kept_temp={tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
