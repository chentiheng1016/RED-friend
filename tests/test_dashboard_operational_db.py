"""Dashboard coverage for operational Postgres status."""
from __future__ import annotations

import unittest
from unittest import mock


class DashboardOperationalDbTests(unittest.TestCase):
    def test_operational_section_reports_local_fallback_without_url(self):
        from agent_core import dashboard

        report = {
            "operational_db": {
                "enabled": False,
                "ok": True,
                "schema_version": None,
                "schema_expected": None,
            },
            "schema_ok": True,
            "enabled_backends": [],
            "configured_backends": [],
            "local_fallback_backends": ["audit", "task_queue", "gemini_circuit"],
            "backends": [],
        }
        with mock.patch("agent_core.operational_health.health_report", return_value=report):
            text = dashboard._section_operational_db()

        self.assertIn("未啟用", text)
        self.assertIn("fallback backends：3", text)
        self.assertIn("task_queue", text)
        self.assertNotIn("postgresql://", text)

    def test_operational_section_reports_enabled_schema_and_backends(self):
        from agent_core import dashboard
        from agent_core import operational_db

        report = {
            "operational_db": {
                "enabled": True,
                "ok": True,
                "schema_version": operational_db.SCHEMA_VERSION,
                "schema_expected": operational_db.SCHEMA_VERSION,
                "pool_enabled": True,
                "pool_active": True,
            },
            "schema_ok": True,
            "enabled_backends": ["audit", "task_queue", "gemini_circuit"],
            "configured_backends": ["audit", "task_queue", "gemini_circuit"],
            "local_fallback_backends": [],
            "backends": [],
        }
        with mock.patch("agent_core.operational_health.health_report", return_value=report):
            text = dashboard._section_operational_db()

        self.assertIn("enabled / ok=True", text)
        self.assertIn(
            f"schema: {operational_db.SCHEMA_VERSION}/{operational_db.SCHEMA_VERSION}",
            text,
        )
        self.assertIn("pool: enabled=True active=True", text)
        self.assertIn("enabled=3 / configured=3 / fallback=0", text)
        self.assertIn("gemini_circuit", text)

    def test_system_status_supports_operational_filter(self):
        from agent_core import dashboard

        report = {
            "operational_db": {
                "enabled": False,
                "ok": True,
                "schema_version": None,
                "schema_expected": None,
            },
            "schema_ok": True,
            "enabled_backends": [],
            "configured_backends": [],
            "local_fallback_backends": ["audit"],
            "backends": [],
        }
        with mock.patch.object(dashboard, "_ensure_chroma_endpoint"), \
                mock.patch("agent_core.operational_health.health_report", return_value=report):
            text = dashboard.system_status("operational")

        self.assertIn("Operational DB", text)
        self.assertIn("fallback backends", text)
        self.assertNotIn("Daemon 健康", text)


if __name__ == "__main__":
    unittest.main()
