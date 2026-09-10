# Oracle Read-Only Probe

RED should not read Oracle `.DBF` datafiles directly. The safe path is to connect
to a running Oracle instance with a read-only account, inventory the real schema,
and then whitelist tables for ETL into `factory_warehouse.duckdb` or Parquet.

## Install

```bash
./.venv/bin/pip install -r requirements-oracle.txt
```

## Minimum Oracle Account

Ask the DBA for a dedicated account, not `SYS`, `SYSTEM`, or an application
write account.

```sql
CREATE USER red_ro IDENTIFIED BY "<password>";
GRANT CREATE SESSION TO red_ro;
GRANT SELECT ON app_schema.some_table TO red_ro;
```

The first probe only needs `ALL_TABLES`, `ALL_TAB_COLUMNS`, and `SELECT` on the
business tables you want RED to inspect.

## Run A Probe

```bash
export RED_ORACLE_DSN='oracle-host:1521/service_name'
export RED_ORACLE_USER='red_ro'
export RED_ORACLE_PASSWORD='...'

./.venv/bin/python scripts/oracle_probe.py \
  --schema APP_SCHEMA \
  --table-limit 100 \
  --sample-rows 5
```

The report is written under `var/data/oracle_probe/`, which is ignored by git.
It contains no password, but it can contain sampled business data, so treat it as
internal data.

## Thick Mode

Use Thin mode first. Switch to Thick mode only when the Oracle environment
requires Instant Client behavior, such as older Oracle versions, native network
encryption, or enterprise `tnsnames.ora`/wallet constraints.

```bash
export RED_ORACLE_MODE=thick
export RED_ORACLE_CLIENT_LIB_DIR='/path/to/instantclient'
./.venv/bin/python scripts/oracle_probe.py --schema APP_SCHEMA
```

## Guardrails

- The script inventories metadata and small samples only.
- `COUNT(*)` is off by default; use `--exact-counts` only on a staging/read replica.
- LOB and binary columns are not sampled.
- The agent runtime does not get arbitrary Oracle SQL access from this probe.
- After review, add a narrow ETL whitelist into DuckDB/Parquet instead of routing
  live agent questions directly to production Oracle.
