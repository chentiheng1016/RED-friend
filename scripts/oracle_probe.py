#!/usr/bin/env python3
"""Run RED's read-only Oracle inventory probe."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.oracle_probe import (  # noqa: E402
    DEFAULT_PASSWORD_ENV,
    OracleProbeConfig,
    OracleProbeError,
    config_from_env,
    run_probe,
    write_inventory,
)


def _split_list(values: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.replace(";", ",").split(",") if part.strip())
    return tuple(out)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Inventory Oracle schemas/tables for reviewed RED warehouse onboarding."
    )
    p.add_argument("--dsn", default=os.environ.get("RED_ORACLE_DSN", ""),
                   help="Oracle DSN, e.g. host:1521/service. Defaults to RED_ORACLE_DSN.")
    p.add_argument("--user", default=os.environ.get("RED_ORACLE_USER", ""),
                   help="Read-only Oracle user. Defaults to RED_ORACLE_USER.")
    p.add_argument("--password-env", default=os.environ.get("RED_ORACLE_PASSWORD_ENV", DEFAULT_PASSWORD_ENV),
                   help=f"Environment variable containing the password. Default: {DEFAULT_PASSWORD_ENV}.")
    p.add_argument("--mode", choices=("thin", "thick"), default=os.environ.get("RED_ORACLE_MODE", "thin"),
                   help="python-oracledb mode. Thin mode is the default.")
    p.add_argument("--client-lib-dir", default=os.environ.get("RED_ORACLE_CLIENT_LIB_DIR", ""),
                   help="Oracle Instant Client library directory for thick mode.")
    p.add_argument("--config-dir", default=os.environ.get("RED_ORACLE_CONFIG_DIR", ""),
                   help="Optional Oracle network config directory.")
    p.add_argument("--wallet-location", default=os.environ.get("RED_ORACLE_WALLET_LOCATION", ""),
                   help="Optional Oracle wallet location.")
    p.add_argument("--wallet-password-env", default="RED_ORACLE_WALLET_PASSWORD",
                   help="Environment variable containing wallet password, if needed.")
    p.add_argument("--schema", action="append", default=[],
                   help="Schema owner to include. May be repeated or comma-separated.")
    p.add_argument("--exclude-schema", action="append", default=[],
                   help="Additional schema owner to exclude. May be repeated or comma-separated.")
    p.add_argument("--table-limit", type=int, default=int(os.environ.get("RED_ORACLE_TABLE_LIMIT", "50")),
                   help="Maximum tables to inventory.")
    p.add_argument("--sample-rows", type=int, default=int(os.environ.get("RED_ORACLE_SAMPLE_ROWS", "3")),
                   help="Tiny sample rows per table. Use 0 to disable samples.")
    p.add_argument("--max-sample-columns", type=int,
                   default=int(os.environ.get("RED_ORACLE_MAX_SAMPLE_COLUMNS", "12")),
                   help="Maximum scalar columns to sample per table.")
    p.add_argument("--call-timeout-ms", type=int,
                   default=int(os.environ.get("RED_ORACLE_CALL_TIMEOUT_MS", "15000")),
                   help="Oracle call timeout in milliseconds.")
    p.add_argument("--exact-counts", action="store_true",
                   help="Run COUNT(*) per table. Off by default because it can be expensive.")
    p.add_argument("--output", default="",
                   help="Output JSON path. Defaults to var/data/oracle_probe/oracle-inventory-*.json.")
    p.add_argument("--stdout", action="store_true",
                   help="Print the full JSON report to stdout instead of the short summary.")
    return p


def _config_from_args(args: argparse.Namespace) -> OracleProbeConfig:
    env = dict(os.environ)
    env["RED_ORACLE_DSN"] = args.dsn
    env["RED_ORACLE_USER"] = args.user
    env["RED_ORACLE_PASSWORD_ENV"] = args.password_env
    env["RED_ORACLE_MODE"] = args.mode
    env["RED_ORACLE_CLIENT_LIB_DIR"] = args.client_lib_dir
    env["RED_ORACLE_CONFIG_DIR"] = args.config_dir
    env["RED_ORACLE_WALLET_LOCATION"] = args.wallet_location
    if args.wallet_password_env in os.environ:
        env["RED_ORACLE_WALLET_PASSWORD"] = os.environ[args.wallet_password_env]
    config = config_from_env(env)
    include_schemas = _split_list(args.schema) or config.include_schemas
    exclude_schemas = config.exclude_schemas + _split_list(args.exclude_schema)
    return OracleProbeConfig(
        dsn=config.dsn,
        user=config.user,
        password=config.password,
        mode=config.mode,
        client_lib_dir=config.client_lib_dir,
        config_dir=config.config_dir,
        wallet_location=config.wallet_location,
        wallet_password=config.wallet_password,
        include_schemas=include_schemas,
        exclude_schemas=exclude_schemas,
        table_limit=args.table_limit,
        sample_rows=args.sample_rows,
        max_sample_columns=args.max_sample_columns,
        call_timeout_ms=args.call_timeout_ms,
        exact_counts=bool(args.exact_counts or config.exact_counts),
    ).normalized()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_probe(_config_from_args(args))
    except OracleProbeError as exc:
        print(f"Oracle probe failed: {exc}", file=sys.stderr)
        return 2
    output = write_inventory(report, args.output or None)
    if args.stdout:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        summary = report["summary"]
        print(
            "Oracle inventory ready: "
            f"{summary['tables']} tables, {summary['columns']} columns, "
            f"{summary['sampled_tables']} sampled, {summary['sample_errors']} sample errors"
        )
        print(f"Output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
