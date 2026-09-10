import json
import os
import tempfile
import unittest

from agent_core import oracle_probe as op


class FakeCursor:
    def __init__(self):
        self.rows = []
        self.sample_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        params = params or {}
        if "FROM all_tables" in sql:
            self.rows = [
                ("APP", "CUSTOMERS", 12, None, "USERS"),
                ("APP", "ORDERS", 34, None, "USERS"),
            ]
        elif "FROM all_tab_columns" in sql:
            self.rows = [
                ("APP", "CUSTOMERS", 1, "ID", "NUMBER", 22, 10, 0, "N"),
                ("APP", "CUSTOMERS", 2, "NAME", "VARCHAR2", 80, None, None, "Y"),
                ("APP", "CUSTOMERS", 3, "PHOTO", "BLOB", 4000, None, None, "Y"),
                ("APP", "ORDERS", 1, "ORDER_ID", "NUMBER", 22, 10, 0, "N"),
                ("APP", "ORDERS", 2, "NOTE", "CLOB", 4000, None, None, "Y"),
            ]
        elif 'FROM "APP"."CUSTOMERS"' in sql:
            self.sample_sql = sql
            self.rows = [(1, "Alice")]
        elif 'FROM "APP"."ORDERS"' in sql:
            self.sample_sql = sql
            self.rows = [(1001,)]
        else:
            raise AssertionError(f"unexpected SQL: {sql!r} params={params!r}")
        return self

    def fetchall(self):
        return self.rows


class FakeConnection:
    thin = True
    version = "19.0.0"

    def __init__(self):
        self.call_timeout = None
        self.cursor_obj = FakeCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class FakeDriver:
    __version__ = "4.0.1"

    def __init__(self):
        self.connection = FakeConnection()
        self.connected_with = None
        self.init_kwargs = None

    def connect(self, **kwargs):
        self.connected_with = kwargs
        return self.connection

    def init_oracle_client(self, **kwargs):
        self.init_kwargs = kwargs


class OracleProbeTests(unittest.TestCase):
    def test_config_from_env_uses_password_env_and_schema_list(self):
        cfg = op.config_from_env({
            "RED_ORACLE_DSN": "db:1521/prod",
            "RED_ORACLE_USER": "red_ro",
            "RED_ORACLE_PASSWORD_ENV": "ORACLE_SECRET",  # pragma: allowlist secret
            "ORACLE_SECRET": "pw",  # pragma: allowlist secret
            "RED_ORACLE_SCHEMAS": "app, sales",
        })
        self.assertEqual(cfg.include_schemas, ("APP", "SALES"))
        self.assertEqual(cfg.password, "pw")

    def test_config_from_env_reports_missing_values_without_secret(self):
        with self.assertRaises(op.OracleProbeError) as ctx:
            op.config_from_env({"RED_ORACLE_PASSWORD": "super-secret"})  # pragma: allowlist secret
        msg = str(ctx.exception)
        self.assertIn("RED_ORACLE_DSN", msg)
        self.assertNotIn("super-secret", msg)

    def test_run_probe_inventory_skips_lobs_and_hides_password(self):
        driver = FakeDriver()
        cfg = op.OracleProbeConfig(
            dsn="db:1521/prod",
            user="red_ro",
            password="super-secret",  # pragma: allowlist secret
            include_schemas=("APP",),
            table_limit=10,
            sample_rows=2,
        )
        report = op.run_probe(cfg, driver=driver)
        self.assertEqual(report["summary"]["tables"], 2)
        self.assertEqual(report["summary"]["columns"], 5)
        self.assertEqual(report["summary"]["sampled_tables"], 2)
        self.assertEqual(driver.connected_with["user"], "red_ro")
        self.assertNotIn("super-secret", json.dumps(report, ensure_ascii=False))
        customers = report["tables"][0]
        self.assertEqual(customers["sample"]["columns"], ["ID", "NAME"])
        orders = report["tables"][1]
        self.assertEqual(orders["sample"]["columns"], ["ORDER_ID"])
        self.assertNotIn("PHOTO", customers["sample"]["columns"])
        self.assertNotIn("NOTE", orders["sample"]["columns"])

    def test_thick_mode_initializes_client(self):
        driver = FakeDriver()
        cfg = op.OracleProbeConfig(
            dsn="db:1521/prod",
            user="red_ro",
            password="pw",  # pragma: allowlist secret
            mode="thick",
            client_lib_dir="/opt/instantclient",
            include_schemas=("APP",),
            sample_rows=0,
        )
        op.run_probe(cfg, driver=driver)
        self.assertEqual(driver.init_kwargs, {"lib_dir": "/opt/instantclient"})

    def test_write_inventory_creates_output_and_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "inventory.json")
            written = op.write_inventory({"summary": {"tables": 0}}, path)
            self.assertEqual(written, path)
            self.assertTrue(os.path.exists(path))
            self.assertTrue(os.path.exists(os.path.join(tmp, "latest.json")))


if __name__ == "__main__":
    unittest.main()
