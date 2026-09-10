from __future__ import annotations

import os
import unittest
from unittest import mock


class OperationalHealthTests(unittest.TestCase):
    def test_backends_default_to_local_fallback_when_db_unconfigured(self):
        from agent_core import operational_health

        with mock.patch.dict(os.environ, {}, clear=True):
            report = operational_health.health_report()

        self.assertFalse(report["operational_db"]["enabled"])
        self.assertIn("task_queue", report["local_fallback_backends"])
        self.assertIn("gemini_circuit", report["local_fallback_backends"])
        self.assertNotIn("task_queue", report["enabled_backends"])

    def test_cloud_runtime_warns_when_operational_db_missing(self):
        from agent_core import operational_health

        with mock.patch.dict(os.environ, {}, clear=True):
            issues = operational_health.health_issues(cloud_runtime=True)

        self.assertTrue(any(item["area"] == "operational_db" for item in issues))

    def test_configured_backend_reports_enabled(self):
        from agent_core import operational_health

        with mock.patch.dict(
            os.environ,
            {
                "RED_OPERATIONAL_DB_URL": "postgresql://red",
                "RED_TASK_QUEUE_BACKEND": "postgres",
            },
            clear=True,
        ), mock.patch.object(
            operational_health.operational_db,
            "health_status",
            return_value={
                "enabled": True,
                "ok": True,
                "schema_version": operational_health.operational_db.SCHEMA_VERSION,
                "schema_expected": operational_health.operational_db.SCHEMA_VERSION,
                "pool_active": False,
            },
        ):
            report = operational_health.health_report()

        self.assertIn("task_queue", report["enabled_backends"])
        task_queue = next(b for b in report["backends"] if b["name"] == "task_queue")
        self.assertEqual(task_queue["status"], "enabled")

    def test_gemini_circuit_backend_reports_enabled(self):
        from agent_core import operational_health

        with mock.patch.dict(
            os.environ,
            {
                "RED_OPERATIONAL_DB_URL": "postgresql://red",
                "RED_GEMINI_CIRCUIT_BACKEND": "postgres",
            },
            clear=True,
        ), mock.patch.object(
            operational_health.operational_db,
            "health_status",
            return_value={
                "enabled": True,
                "ok": True,
                "schema_version": operational_health.operational_db.SCHEMA_VERSION,
                "schema_expected": operational_health.operational_db.SCHEMA_VERSION,
                "pool_active": False,
            },
        ):
            report = operational_health.health_report()

        self.assertIn("gemini_circuit", report["enabled_backends"])
        circuit = next(b for b in report["backends"] if b["name"] == "gemini_circuit")
        self.assertEqual(circuit["status"], "enabled")

    def test_schema_lag_reports_warning(self):
        from agent_core import operational_health

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True), \
                mock.patch.object(
                    operational_health.operational_db,
                    "health_status",
                    return_value={
                        "enabled": True,
                        "ok": True,
                        "schema_version": 1,
                        "schema_expected": operational_health.operational_db.SCHEMA_VERSION,
                        "pool_active": False,
                    },
                ):
            issues = operational_health.health_issues()

        self.assertTrue(any(item["area"] == "operational_db/schema" for item in issues))


if __name__ == "__main__":
    unittest.main()
