"""Read-only Oracle inventory probe for RED warehouse onboarding.

This module deliberately stops at inventory: schemas, tables, columns, row
statistics, and tiny samples. It does not expose arbitrary Oracle querying to
the agent runtime; the next step is a reviewed whitelist ETL into DuckDB or
Parquet.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from agent_core.logging_and_paths import DATA_DIR


DEFAULT_PASSWORD_ENV = "RED_ORACLE_PASSWORD"  # pragma: allowlist secret
DEFAULT_OUTPUT_DIR = os.path.join(DATA_DIR, "oracle_probe")

_DEFAULT_SYSTEM_SCHEMAS = (
    "ANONYMOUS",
    "APEX_PUBLIC_USER",
    "APPQOSSYS",
    "AUDSYS",
    "CTXSYS",
    "DBSFWUSER",
    "DBSNMP",
    "DIP",
    "DVF",
    "DVSYS",
    "GGSYS",
    "GSMADMIN_INTERNAL",
    "LBACSYS",
    "MDDATA",
    "MDSYS",
    "OJVMSYS",
    "OLAPSYS",
    "ORDDATA",
    "ORDPLUGINS",
    "ORDSYS",
    "OUTLN",
    "REMOTE_SCHEDULER_AGENT",
    "SI_INFORMTN_SCHEMA",
    "SYS",
    "SYSBACKUP",
    "SYSDG",
    "SYSKM",
    "SYSRAC",
    "SYSTEM",
    "WMSYS",
    "XDB",
    "XS$NULL",
)

_UNSAFE_SAMPLE_TYPES = {
    "BFILE",
    "BLOB",
    "CLOB",
    "LONG",
    "LONG RAW",
    "NCLOB",
    "RAW",
    "ROWID",
    "UROWID",
    "XMLTYPE",
}


class OracleProbeError(RuntimeError):
    """Base error for Oracle probe setup and execution failures."""


class OracleDriverMissingError(OracleProbeError):
    """Raised when python-oracledb is not installed."""


@dataclass(frozen=True)
class OracleProbeConfig:
    dsn: str
    user: str
    password: str
    mode: str = "thin"
    client_lib_dir: str = ""
    config_dir: str = ""
    wallet_location: str = ""
    wallet_password: str = ""
    include_schemas: tuple[str, ...] = ()
    exclude_schemas: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_SYSTEM_SCHEMAS)
    table_limit: int = 50
    sample_rows: int = 3
    max_sample_columns: int = 12
    call_timeout_ms: int = 15000
    exact_counts: bool = False

    def normalized(self) -> "OracleProbeConfig":
        mode = self.mode.strip().lower() or "thin"
        if mode not in {"thin", "thick"}:
            raise OracleProbeError("RED_ORACLE_MODE must be 'thin' or 'thick'")
        if self.table_limit < 1:
            raise OracleProbeError("table_limit must be >= 1")
        if self.sample_rows < 0:
            raise OracleProbeError("sample_rows must be >= 0")
        if self.max_sample_columns < 1:
            raise OracleProbeError("max_sample_columns must be >= 1")
        if self.call_timeout_ms < 1000:
            raise OracleProbeError("call_timeout_ms must be >= 1000")
        return OracleProbeConfig(
            dsn=self.dsn.strip(),
            user=self.user.strip(),
            password=self.password,
            mode=mode,
            client_lib_dir=self.client_lib_dir.strip(),
            config_dir=self.config_dir.strip(),
            wallet_location=self.wallet_location.strip(),
            wallet_password=self.wallet_password,
            include_schemas=_normalize_schema_list(self.include_schemas),
            exclude_schemas=_normalize_schema_list(self.exclude_schemas),
            table_limit=self.table_limit,
            sample_rows=self.sample_rows,
            max_sample_columns=self.max_sample_columns,
            call_timeout_ms=self.call_timeout_ms,
            exact_counts=self.exact_counts,
        )


def _split_env_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.replace(";", ",").split(",") if part.strip())


def _env_int(env: Mapping[str, str], name: str, default: str) -> int:
    try:
        return int(env.get(name, default))
    except ValueError as exc:
        raise OracleProbeError(f"{name} must be an integer") from exc


def _normalize_schema_list(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(v).strip().upper() for v in values if str(v).strip()))


def config_from_env(environ: Mapping[str, str] | None = None) -> OracleProbeConfig:
    env = environ or os.environ
    password_env = env.get("RED_ORACLE_PASSWORD_ENV", DEFAULT_PASSWORD_ENV).strip() or DEFAULT_PASSWORD_ENV
    dsn = env.get("RED_ORACLE_DSN", "").strip()
    user = env.get("RED_ORACLE_USER", "").strip()
    password = env.get(password_env, "")
    missing = []
    if not dsn:
        missing.append("RED_ORACLE_DSN")
    if not user:
        missing.append("RED_ORACLE_USER")
    if not password:
        missing.append(password_env)
    if missing:
        raise OracleProbeError("Missing Oracle probe environment variables: " + ", ".join(missing))
    return OracleProbeConfig(
        dsn=dsn,
        user=user,
        password=password,
        mode=env.get("RED_ORACLE_MODE", "thin"),
        client_lib_dir=env.get("RED_ORACLE_CLIENT_LIB_DIR", ""),
        config_dir=env.get("RED_ORACLE_CONFIG_DIR", ""),
        wallet_location=env.get("RED_ORACLE_WALLET_LOCATION", ""),
        wallet_password=env.get("RED_ORACLE_WALLET_PASSWORD", ""),
        include_schemas=_split_env_list(env.get("RED_ORACLE_SCHEMAS")),
        exclude_schemas=_DEFAULT_SYSTEM_SCHEMAS + _split_env_list(env.get("RED_ORACLE_EXCLUDE_SCHEMAS")),
        table_limit=_env_int(env, "RED_ORACLE_TABLE_LIMIT", "50"),
        sample_rows=_env_int(env, "RED_ORACLE_SAMPLE_ROWS", "3"),
        max_sample_columns=_env_int(env, "RED_ORACLE_MAX_SAMPLE_COLUMNS", "12"),
        call_timeout_ms=_env_int(env, "RED_ORACLE_CALL_TIMEOUT_MS", "15000"),
        exact_counts=env.get("RED_ORACLE_EXACT_COUNTS", "").strip().lower() in {"1", "true", "yes", "on"},
    ).normalized()


def _load_driver():
    try:
        import oracledb  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise OracleDriverMissingError(
            "python-oracledb is not installed. Install the optional Oracle bundle with "
            "`./.venv/bin/pip install -r requirements-oracle.txt`."
        ) from exc
    return oracledb


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _to_jsonable(value: Any, *, max_chars: int = 160) -> Any:
    if value is None:
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "..."
    return text


def _execute_all(cursor: Any, sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    cursor.execute(sql, params)
    return list(cursor.fetchall())


def _owner_filters(config: OracleProbeConfig, params: dict[str, Any]) -> str:
    clauses = []
    if config.include_schemas:
        names = []
        for i, owner in enumerate(config.include_schemas):
            key = f"inc_owner_{i}"
            params[key] = owner
            names.append(f":{key}")
        clauses.append(f"owner IN ({', '.join(names)})")
    elif config.exclude_schemas:
        names = []
        for i, owner in enumerate(config.exclude_schemas):
            key = f"exc_owner_{i}"
            params[key] = owner
            names.append(f":{key}")
        clauses.append(f"owner NOT IN ({', '.join(names)})")
    return " AND ".join(clauses) if clauses else "1=1"


def _fetch_tables(cursor: Any, config: OracleProbeConfig) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": config.table_limit}
    owner_filter = _owner_filters(config, params)
    rows = _execute_all(
        cursor,
        f"""
        SELECT *
        FROM (
            SELECT owner, table_name, num_rows, last_analyzed, tablespace_name
            FROM all_tables
            WHERE {owner_filter}
            ORDER BY owner, table_name
        )
        WHERE ROWNUM <= :limit
        """,
        params,
    )
    tables: list[dict[str, Any]] = []
    for owner, table_name, num_rows, last_analyzed, tablespace_name in rows:
        tables.append(
            {
                "owner": str(owner),
                "table_name": str(table_name),
                "estimated_rows": _to_jsonable(num_rows),
                "last_analyzed": _to_jsonable(last_analyzed),
                "tablespace_name": str(tablespace_name or ""),
                "columns": [],
            }
        )
    return tables


def _fetch_columns(cursor: Any, tables: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if not tables:
        return {}
    owners = sorted({t["owner"] for t in tables})
    table_names = sorted({t["table_name"] for t in tables})
    params: dict[str, Any] = {}
    owner_params = []
    for i, owner in enumerate(owners):
        key = f"owner_{i}"
        params[key] = owner
        owner_params.append(f":{key}")
    table_params = []
    for i, table_name in enumerate(table_names):
        key = f"table_{i}"
        params[key] = table_name
        table_params.append(f":{key}")
    rows = _execute_all(
        cursor,
        f"""
        SELECT owner, table_name, column_id, column_name, data_type,
               data_length, data_precision, data_scale, nullable
        FROM all_tab_columns
        WHERE owner IN ({', '.join(owner_params)})
          AND table_name IN ({', '.join(table_params)})
        ORDER BY owner, table_name, column_id
        """,
        params,
    )
    wanted = {(t["owner"], t["table_name"]) for t in tables}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in wanted}
    for owner, table_name, column_id, column_name, data_type, data_length, precision, scale, nullable in rows:
        key = (str(owner), str(table_name))
        if key not in grouped:
            continue
        grouped[key].append(
            {
                "name": str(column_name),
                "position": int(column_id or 0),
                "data_type": str(data_type or ""),
                "data_length": _to_jsonable(data_length),
                "data_precision": _to_jsonable(precision),
                "data_scale": _to_jsonable(scale),
                "nullable": str(nullable or "").upper() == "Y",
            }
        )
    return grouped


def _sample_columns(columns: list[dict[str, Any]], limit: int) -> list[str]:
    names: list[str] = []
    for col in columns:
        dtype = str(col.get("data_type") or "").upper()
        if dtype in _UNSAFE_SAMPLE_TYPES:
            continue
        if dtype.startswith("INTERVAL") or dtype.startswith("TIMESTAMP WITH LOCAL TIME ZONE"):
            continue
        names.append(str(col["name"]))
        if len(names) >= limit:
            break
    return names


def _sample_table(cursor: Any, table: dict[str, Any], config: OracleProbeConfig) -> dict[str, Any]:
    if config.sample_rows <= 0:
        return {"requested_rows": 0, "columns": [], "rows": []}
    cols = _sample_columns(table["columns"], config.max_sample_columns)
    if not cols:
        return {"requested_rows": config.sample_rows, "columns": [], "rows": [], "skipped": "no scalar columns"}
    col_sql = ", ".join(_quote_ident(c) for c in cols)
    sql = (
        f"SELECT {col_sql} FROM "
        f"{_quote_ident(table['owner'])}.{_quote_ident(table['table_name'])} "
        "WHERE ROWNUM <= :limit"
    )
    try:
        rows = _execute_all(cursor, sql, {"limit": config.sample_rows})
    except Exception as exc:  # noqa: BLE001 - probe report should continue per table
        return {
            "requested_rows": config.sample_rows,
            "columns": cols,
            "rows": [],
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
    return {
        "requested_rows": config.sample_rows,
        "columns": cols,
        "rows": [
            {col: _to_jsonable(value) for col, value in zip(cols, row, strict=False)}
            for row in rows
        ],
    }


def _exact_count(cursor: Any, table: dict[str, Any]) -> int | None:
    sql = f"SELECT COUNT(*) FROM {_quote_ident(table['owner'])}.{_quote_ident(table['table_name'])}"
    try:
        rows = _execute_all(cursor, sql, {})
    except Exception:
        return None
    if not rows:
        return None
    try:
        return int(rows[0][0])
    except (TypeError, ValueError):
        return None


def _connect(driver: Any, config: OracleProbeConfig) -> Any:
    if config.mode == "thick":
        kwargs = {}
        if config.client_lib_dir:
            kwargs["lib_dir"] = config.client_lib_dir
        driver.init_oracle_client(**kwargs)
    connect_kwargs: dict[str, Any] = {
        "user": config.user,
        "password": config.password,
        "dsn": config.dsn,
    }
    if config.config_dir:
        connect_kwargs["config_dir"] = config.config_dir
    if config.wallet_location:
        connect_kwargs["wallet_location"] = config.wallet_location
    if config.wallet_password:
        connect_kwargs["wallet_password"] = config.wallet_password
    return driver.connect(**connect_kwargs)


def run_probe(config: OracleProbeConfig, *, driver: Any | None = None) -> dict[str, Any]:
    """Run a read-only Oracle inventory probe and return a JSON-safe report."""
    config = config.normalized()
    driver = driver or _load_driver()
    generated_at = _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")
    connection = _connect(driver, config)
    connection_thin = bool(getattr(connection, "thin", config.mode == "thin"))
    database_version = str(getattr(connection, "version", ""))
    try:
        try:
            connection.call_timeout = config.call_timeout_ms
        except Exception:
            pass
        with connection.cursor() as cursor:
            tables = _fetch_tables(cursor, config)
            grouped_columns = _fetch_columns(cursor, tables)
            for table in tables:
                key = (table["owner"], table["table_name"])
                table["columns"] = grouped_columns.get(key, [])
                if config.exact_counts:
                    table["exact_rows"] = _exact_count(cursor, table)
                table["sample"] = _sample_table(cursor, table, config)
    finally:
        try:
            connection.close()
        except Exception:
            pass

    sample_errors = sum(1 for t in tables if t.get("sample", {}).get("error"))
    sampled_tables = sum(1 for t in tables if t.get("sample", {}).get("rows"))
    return {
        "generated_at": generated_at,
        "driver": {
            "name": "oracledb",
            "version": str(getattr(driver, "__version__", "")),
            "thin": connection_thin,
            "database_version": database_version,
        },
        "connection": {
            "user": config.user,
            "dsn": config.dsn,
            "mode": config.mode,
            "include_schemas": list(config.include_schemas),
            "exclude_schemas": list(config.exclude_schemas if not config.include_schemas else ()),
        },
        "limits": {
            "table_limit": config.table_limit,
            "sample_rows": config.sample_rows,
            "max_sample_columns": config.max_sample_columns,
            "call_timeout_ms": config.call_timeout_ms,
            "exact_counts": config.exact_counts,
        },
        "summary": {
            "tables": len(tables),
            "columns": sum(len(t["columns"]) for t in tables),
            "sampled_tables": sampled_tables,
            "sample_errors": sample_errors,
        },
        "tables": tables,
    }


def default_output_path() -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(DEFAULT_OUTPUT_DIR, f"oracle-inventory-{ts}.json")


def write_inventory(report: Mapping[str, Any], output_path: str | None = None) -> str:
    path = output_path or default_output_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    latest = os.path.join(os.path.dirname(path), "latest.json")
    try:
        with open(latest, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError:
        pass
    return path
