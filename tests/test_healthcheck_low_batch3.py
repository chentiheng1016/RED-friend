"""健檢 Low batch 3（cleanup/ops）：
- employee get_employee 對空 color fail-closed（兩後端對齊；color-less doc 不再 truthy 過閘）
- 死碼移除：gemini_client._load_api_key_from_secure_store / cost_tracker '_Pricing' 幻名
（briefing_15min/sample_check 的 run_with_deadline 覆蓋由 test_daemon_helpers.CronDeadlineCoverageTests 守。）
"""
import inspect
import unittest
from unittest import mock


class EmployeeFailClosedTests(unittest.TestCase):
    def test_empty_color_treated_as_not_registered(self):
        from agent_core.web_server import employee_registry as er
        with mock.patch.object(er, "_backend", return_value="file"):
            with mock.patch.object(er, "load_registry",
                                   return_value={"x@y.com": {"email": "x@y.com", "color": ""}}):
                self.assertIsNone(er.get_employee("x@y.com"))
            with mock.patch.object(er, "load_registry",
                                   return_value={"x@y.com": {"email": "x@y.com", "color": "green"}}):
                self.assertEqual(er.get_employee("x@y.com")["color"], "green")
            with mock.patch.object(er, "load_registry", return_value={}):
                self.assertIsNone(er.get_employee("missing@y.com"))


class DeadCodeRemovedTests(unittest.TestCase):
    def test_unused_api_key_loader_removed(self):
        from agent_core import gemini_client
        self.assertFalse(hasattr(gemini_client, "_load_api_key_from_secure_store"))

    def test_pricing_phantom_name_removed(self):
        from agent_core import cost_tracker
        self.assertNotIn("_Pricing", inspect.getsource(cost_tracker))


if __name__ == "__main__":
    unittest.main()
