from __future__ import annotations

import json
import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_WEB_DOMAIN_POLICY_BACKEND": "postgres",
}


class OperationalWebDomainPolicyTests(unittest.TestCase):
    def setUp(self):
        from agent_core import web_access_guard

        web_access_guard._PG_WEB_POLICY_WARNING_UNTIL = 0.0

    def test_backend_requires_explicit_web_policy_switch(self):
        from agent_core import operational_web_domain_policy as store

        with mock.patch.dict(os.environ, {"RED_OPERATIONAL_DB_URL": "postgresql://red"}, clear=True):
            self.assertFalse(store.enabled())
        with mock.patch.dict(os.environ, _DB_ENV, clear=True):
            self.assertTrue(store.enabled())

    def test_set_policy_writes_postgres_backend(self):
        from agent_core.web_access_guard import web_domain_policy_set

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_web_domain_policy.set_policy",
                ) as set_policy:
            out = web_domain_policy_set(
                "https://Example.com:443/path",
                "needs_api",
                "Use vendor API.",
            )

        self.assertIn("已設定", out)
        set_policy.assert_called_once_with("example.com", "needs_api", "Use vendor API.")

    def test_list_policy_reads_postgres_backend(self):
        from agent_core.web_access_guard import web_domain_policy_list

        rows = {"example.com": {"policy": "blocked", "note": "No scraping."}}
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_web_domain_policy.load_policies",
                    return_value=rows,
                ) as load_policies:
            out = web_domain_policy_list()

        self.assertIn("example.com: blocked", out)
        self.assertIn("No scraping", out)
        load_policies.assert_called_once()

    def test_diagnose_blocks_from_postgres_policy_before_network(self):
        from agent_core.web_access_guard import web_access_diagnose_json

        rows = {"example.com": {"policy": "needs_api", "note": "Use vendor API only."}}
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_web_domain_policy.load_policies",
                    return_value=rows,
                ), \
                mock.patch("agent_core.web_access_guard.requests.get") as get:
            payload = json.loads(web_access_diagnose_json("https://sub.example.com/path"))

        get.assert_not_called()
        self.assertEqual(payload["status"], "needs_api")
        self.assertEqual(payload["domain_policy"], "needs_api")
        self.assertEqual(payload["policy_domain"], "example.com")
        self.assertIn("vendor API", payload["next_action"])

    def test_clear_policy_deletes_postgres_backend(self):
        from agent_core.web_access_guard import web_domain_policy_clear

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_web_domain_policy.clear_policy",
                    return_value=True,
                ) as clear_policy:
            out = web_domain_policy_clear("example.com")

        self.assertIn("已移除", out)
        clear_policy.assert_called_once_with("example.com")

    def test_clear_policy_reports_missing_from_postgres_backend(self):
        from agent_core.web_access_guard import web_domain_policy_clear

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_web_domain_policy.clear_policy",
                    return_value=False,
                ):
            out = web_domain_policy_clear("example.com")

        self.assertIn("沒有找到", out)


if __name__ == "__main__":
    unittest.main()
