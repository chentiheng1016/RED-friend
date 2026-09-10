"""Phase 4 起步 — `/dept` Telegram 指令測試.

驗證：
  - is_dept_command 正確辨識
  - help / 用法錯誤 / 未知 color / 壞 JSON / non-object payload 都回字串不 raise
  - 真的能 dispatch 到 stub / real agent
  - PermissionDenied / ValueError 都被吃掉並轉字串
  - /dept red 給友好錯誤（Red 本就不在 registry）
  - daemon_telegram.tg_handle_message 在收到 /dept 時短路到我們，不打 Gemini
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class TestIsDeptCommand(unittest.TestCase):
    def test_recognizes_dept(self):
        from agent_core.agents.telegram_command import is_dept_command
        self.assertTrue(is_dept_command("/dept"))
        self.assertTrue(is_dept_command("/dept green"))
        self.assertTrue(is_dept_command("  /dept green query.list_samples  "))
        self.assertTrue(is_dept_command("/dept green query.x {\"k\":\"v\"}"))

    def test_rejects_non_dept(self):
        from agent_core.agents.telegram_command import is_dept_command
        self.assertFalse(is_dept_command("/new"))
        self.assertFalse(is_dept_command("hello"))
        self.assertFalse(is_dept_command(""))
        self.assertFalse(is_dept_command("/department"))   # 不是 /dept
        self.assertFalse(is_dept_command("/deptx"))        # 不是 /dept


class TestGreenAgentTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._edge_env = mock.patch.dict(
            os.environ,
            {"RED_EDGE_TASKS_FILE": os.path.join(self._tmpdir.name, "edge_tasks.json")},
            clear=False,
        )
        self._edge_env.start()

    def tearDown(self):
        from agent_core.agents import telegram_command
        self._edge_env.stop()
        self._tmpdir.cleanup()
        telegram_command._reset_for_test()

    def test_is_green_agent_command(self):
        from agent_core.agents.telegram_command import is_green_agent_command

        self.assertTrue(is_green_agent_command("/dev"))
        self.assertTrue(is_green_agent_command("/dev profile"))
        self.assertTrue(is_green_agent_command("/green recipes"))
        self.assertTrue(is_green_agent_command("/sample samples open"))
        self.assertTrue(is_green_agent_command("/sampleroom profile"))
        self.assertTrue(is_green_agent_command("/樣品室 profile"))
        self.assertTrue(is_green_agent_command("/sampledev samples all"))
        self.assertFalse(is_green_agent_command("/dept green query.profile"))
        self.assertFalse(is_green_agent_command("樣品室"))

    def test_dev_help(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        result = handle_green_agent_command("/dev")
        self.assertIn("樣品室 Agent", result)
        self.assertIn("/dev validate", result)
        self.assertIn("/dev draft", result)

    def test_dev_profile_routes_to_green_agent(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        result = handle_green_agent_command("/dev profile")
        self.assertIn("green", result)
        self.assertIn("樣品室", result)
        self.assertIn("sample_master", result)

    def test_dev_samples_routes_to_list_samples(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        with mock.patch(
            "agent_core.agents.green_sample_dev.sample_tracker.list_tracked_samples",
            return_value="[dev samples]",
        ) as mock_list:
            result = handle_green_agent_command("/dev samples delayed")

        mock_list.assert_called_once_with(status="delayed")
        self.assertIn("[dev samples]", result)

    def test_dev_sample_routes_to_sample_status(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        result = handle_green_agent_command("/dev sample S-NOT-EXIST")
        self.assertIn("query.sample_status", result)
        self.assertIn("S-NOT-EXIST", result)
        self.assertIn('"found": false', result)

    def test_dev_validate_routes_to_rpa_validator(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        result = handle_green_agent_command(
            '/dev validate {"recipe_id":"green_update_sample_status",'
            '"data":{"sample_id":"S-DEV-1","status":"closed"}}'
        )

        self.assertIn("query.validate_rpa_payload", result)
        self.assertIn('"ok": true', result)

    def test_dev_draft_routes_to_edge_task_draft(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        result = handle_green_agent_command(
            '/dev draft {"recipe_id":"green_update_sample_status",'
            '"data":{"sample_id":"S-DEV-1","status":"closed"},'
            '"employee_email":"dev@company.example","device_id":"mac-dev-01"}'
        )

        self.assertIn("query.edge_task_draft", result)
        self.assertIn('"type": "edge_rpa"', result)
        self.assertIn('"status": "draft"', result)

    def test_dev_enqueue_without_confirm_previews(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        result = handle_green_agent_command(
            '/dev enqueue {"recipe_id":"green_update_sample_status",'
            '"data":{"sample_id":"S-DEV-1","status":"closed"},'
            '"employee_email":"dev@company.example","device_id":"mac-dev-01"}'
        )

        self.assertIn("即將把這筆樣品室 Edge 任務排進佇列", result)
        self.assertIn("+確認", result)
        self.assertIn("query.edge_task_draft", result)

    def test_dev_enqueue_with_confirm_queues_task(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        with mock.patch("agent_core.tg_auth.check_confirmed", return_value=(True, 0.5)), \
             mock.patch("agent_core.tg_auth.revoke_after_use") as revoke:
            result = handle_green_agent_command(
                '/dev enqueue {"recipe_id":"green_update_sample_status",'
                '"data":{"sample_id":"S-DEV-1","status":"closed"},'
                '"employee_email":"dev@company.example","device_id":"mac-dev-01"} +確認',
                chat_id="12345",
            )

        self.assertIn("command.enqueue_edge_task", result)
        self.assertIn('"status": "queued"', result)
        revoke.assert_called_once_with("12345")

    def test_dev_enqueue_with_confirm_but_no_tg_auth_is_blocked(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        with mock.patch("agent_core.tg_auth.check_confirmed", return_value=(False, -1.0)), \
             mock.patch("agent_core.tg_auth.is_locked_out", return_value=(False, 0)):
            result = handle_green_agent_command(
                '/dev enqueue {"recipe_id":"green_update_sample_status",'
                '"data":{"sample_id":"S-DEV-1","status":"closed"}} +確認',
                chat_id="12345",
            )

        self.assertIn("🔒", result)
        self.assertIn("tg_auth", result)

    def test_dev_queue_lists_edge_tasks(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        handle_green_agent_command(
            '/dev enqueue {"recipe_id":"green_update_sample_status",'
            '"data":{"sample_id":"S-DEV-1","status":"closed"},'
            '"employee_email":"dev@company.example","device_id":"mac-dev-01"} +確認'
        )
        result = handle_green_agent_command("/dev queue queued")

        self.assertIn("query.edge_tasks", result)
        self.assertIn('"total": 1', result)

    def test_dev_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        result = handle_green_agent_command(
            '/dev query.rpa_recipe {"recipe_id":"green_create_sample_record"}'
        )

        self.assertIn("query.rpa_recipe", result)
        self.assertIn("green_create_sample_record", result)


class TestOrangeSalesTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_is_orange_sales_command(self):
        from agent_core.agents.telegram_command import is_orange_sales_command

        self.assertTrue(is_orange_sales_command("/sales"))
        self.assertTrue(is_orange_sales_command("/sales customer PAX"))
        self.assertTrue(is_orange_sales_command("/orange alerts"))
        self.assertTrue(is_orange_sales_command("/biz active"))
        self.assertFalse(is_orange_sales_command("/dept orange query.customer_360"))
        self.assertFalse(is_orange_sales_command("業務部門"))

    def test_sales_help(self):
        from agent_core.agents.telegram_command import handle_orange_sales_command

        result = handle_orange_sales_command("/sales")
        self.assertIn("業務部門 Agent", result)
        self.assertIn("/sales customer", result)
        self.assertIn("/sales search", result)

    def test_sales_customer_routes_to_customer_360(self):
        from agent_core.agents.telegram_command import handle_orange_sales_command

        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.customer_360",
            return_value="[mock customer 360]",
        ) as mock_customer:
            result = handle_orange_sales_command("/sales customer PAX 60")

        mock_customer.assert_called_once_with(
            customer="PAX",
            days=60,
            push_telegram=False,
        )
        self.assertIn("[mock customer 360]", result)

    def test_sales_active_routes_to_active_customers(self):
        from agent_core.agents.telegram_command import handle_orange_sales_command

        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.list_active_customers",
            return_value="[mock active]",
        ) as mock_active:
            result = handle_orange_sales_command("/sales active 30 1")

        mock_active.assert_called_once_with(days=30, min_emails=1)
        self.assertIn("[mock active]", result)

    def test_sales_alerts_routes_to_customer_alerts(self):
        from agent_core.agents.telegram_command import handle_orange_sales_command

        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.customer_alerts",
            return_value="[mock alerts]",
        ) as mock_alerts:
            result = handle_orange_sales_command("/sales alerts 14")

        mock_alerts.assert_called_once_with(days=14)
        self.assertIn("[mock alerts]", result)

    def test_sales_quote_routes_to_quote_history(self):
        from agent_core.agents.telegram_command import handle_orange_sales_command

        with mock.patch(
            "agent_core.agents.orange_sales.quote.query_quote_history",
            return_value="[mock quotes]",
        ) as mock_quote:
            result = handle_orange_sales_command("/sales quote PAX")

        mock_quote.assert_called_once_with(
            customer="PAX",
            sku="",
            direction="",
            recent_months=24,
            expand_aliases=True,
        )
        self.assertIn("[mock quotes]", result)

    def test_sales_search_routes_to_email_search(self):
        from agent_core.agents.telegram_command import handle_orange_sales_command
        from unittest.mock import MagicMock, patch

        fake_store = MagicMock()
        fake_store.query.return_value = [{"text": "PAX quote"}]
        with patch("agent_core.ingest.vector_store.get_store", return_value=fake_store):
            result = handle_orange_sales_command("/sales search PAX outsole quote")

        fake_store.query.assert_called_once_with(
            "PAX outsole quote", n_results=5,
            where={"generated_by_red": {"$ne": True}},
        )
        self.assertIn("PAX quote", result)

    def test_sales_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_orange_sales_command

        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.customer_360",
            return_value="[mock advanced]",
        ):
            result = handle_orange_sales_command(
                '/sales query.customer_360 {"customer":"PAX","days":7}'
            )

        self.assertIn("query.customer_360", result)
        self.assertIn("[mock advanced]", result)


class TestBlueShippingTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_is_blue_shipping_command(self):
        from agent_core.agents.telegram_command import is_blue_shipping_command

        self.assertTrue(is_blue_shipping_command("/shipping"))
        self.assertTrue(is_blue_shipping_command("/shipping eta LJF26040067"))
        self.assertTrue(is_blue_shipping_command("/blue shipments"))
        self.assertTrue(is_blue_shipping_command("/ship alerts"))
        self.assertTrue(is_blue_shipping_command("/船務 profile"))
        self.assertFalse(is_blue_shipping_command("/dept blue query.profile"))
        self.assertFalse(is_blue_shipping_command("船務部門"))

    def test_shipping_help(self):
        from agent_core.agents.telegram_command import handle_blue_shipping_command

        result = handle_blue_shipping_command("/shipping")
        self.assertIn("船務部門 Agent", result)
        self.assertIn("/shipping eta", result)
        self.assertIn("/shipping records", result)

    def test_shipping_eta_routes_to_blue_agent(self):
        from agent_core.agents.telegram_command import handle_blue_shipping_command

        with mock.patch(
            "agent_core.agents.blue_shipping.shipping.shipping_eta",
            return_value={"text": "[mock shipping eta]", "eta": "4/28"},
        ) as mock_eta:
            result = handle_blue_shipping_command("/shipping eta LJF26040067")

        mock_eta.assert_called_once()
        self.assertIn("[mock shipping eta]", result)

    def test_shipping_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_blue_shipping_command

        with mock.patch(
            "agent_core.agents.blue_shipping.shipping.list_shipments",
            return_value={"text": "[mock shipments]", "shipments": [], "total": 0},
        ):
            result = handle_blue_shipping_command(
                '/shipping query.list_shipments {"customer":"JALAS"}'
            )

        self.assertIn("query.list_shipments", result)
        self.assertIn("[mock shipments]", result)


class TestIndigoWarehouseTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_is_indigo_warehouse_command(self):
        from agent_core.agents.telegram_command import is_indigo_warehouse_command

        self.assertTrue(is_indigo_warehouse_command("/warehouse"))
        self.assertTrue(is_indigo_warehouse_command("/warehouse stock EVA"))
        self.assertTrue(is_indigo_warehouse_command("/indigo inventory"))
        self.assertTrue(is_indigo_warehouse_command("/stock alerts"))
        self.assertTrue(is_indigo_warehouse_command("/倉庫 profile"))
        self.assertFalse(is_indigo_warehouse_command("/dept indigo query.profile"))
        self.assertFalse(is_indigo_warehouse_command("倉庫部門"))

    def test_warehouse_help(self):
        from agent_core.agents.telegram_command import handle_indigo_warehouse_command

        result = handle_indigo_warehouse_command("/warehouse")
        self.assertIn("倉庫部門 Agent", result)
        self.assertIn("/warehouse stock", result)
        self.assertIn("/warehouse records", result)

    def test_warehouse_stock_routes_to_indigo_agent(self):
        from agent_core.agents.telegram_command import handle_indigo_warehouse_command

        with mock.patch(
            "agent_core.agents.indigo_warehouse.warehouse.stock_availability",
            return_value={"text": "[mock stock]", "in_stock": True, "quantity": 10},
        ) as mock_stock:
            result = handle_indigo_warehouse_command("/warehouse stock EVA")

        mock_stock.assert_called_once()
        self.assertIn("[mock stock]", result)

    def test_warehouse_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_indigo_warehouse_command

        with mock.patch(
            "agent_core.agents.indigo_warehouse.warehouse.list_inventory",
            return_value={"text": "[mock inventory]", "items": [], "total": 0},
        ):
            result = handle_indigo_warehouse_command(
                '/warehouse query.list_inventory {"low_stock_only":true}'
            )

        self.assertIn("query.list_inventory", result)
        self.assertIn("[mock inventory]", result)


class TestPurpleAccountingTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_is_purple_accounting_command(self):
        from agent_core.agents.telegram_command import is_purple_accounting_command

        self.assertTrue(is_purple_accounting_command("/accounting"))
        self.assertTrue(is_purple_accounting_command("/accounting summary"))
        self.assertTrue(is_purple_accounting_command("/purple alerts"))
        self.assertTrue(is_purple_accounting_command("/invoice 中華電信"))
        self.assertTrue(is_purple_accounting_command("/payment JFPP2603009"))
        self.assertTrue(is_purple_accounting_command("/會計 profile"))
        self.assertFalse(is_purple_accounting_command("/dept purple query.profile"))
        self.assertFalse(is_purple_accounting_command("會計部門"))

    def test_accounting_help(self):
        from agent_core.agents.telegram_command import handle_purple_accounting_command

        result = handle_purple_accounting_command("/accounting")
        self.assertIn("會計部門 Agent", result)
        self.assertIn("/accounting invoices", result)
        self.assertIn("/accounting payments", result)

    def test_accounting_payment_routes_to_purple_agent(self):
        from agent_core.agents.telegram_command import handle_purple_accounting_command

        with mock.patch(
            "agent_core.agents.purple_accounting.accounting.payment_records",
            return_value={"text": "[mock payment]", "records": [], "total": 0},
        ) as mock_payment:
            result = handle_purple_accounting_command("/accounting payments Fulltide")

        mock_payment.assert_called_once()
        self.assertIn("[mock payment]", result)

    def test_accounting_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_purple_accounting_command

        with mock.patch(
            "agent_core.agents.purple_accounting.accounting.accounting_summary",
            return_value={"text": "[mock accounting summary]", "total": 0},
        ):
            result = handle_purple_accounting_command(
                '/accounting query.accounting_summary {"days_back":30}'
            )

        self.assertIn("query.accounting_summary", result)
        self.assertIn("[mock accounting summary]", result)


class TestGrayProductionTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_is_gray_production_command(self):
        from agent_core.agents.telegram_command import is_gray_production_command

        self.assertTrue(is_gray_production_command("/production"))
        self.assertTrue(is_gray_production_command("/production status"))
        self.assertTrue(is_gray_production_command("/prod history"))
        self.assertTrue(is_gray_production_command("/gray profile"))
        self.assertTrue(is_gray_production_command("/anomaly {\"order_id\":\"P1\"}"))
        self.assertTrue(is_gray_production_command("/生產 status"))
        self.assertFalse(is_gray_production_command("/dept gray query.profile"))
        self.assertFalse(is_gray_production_command("生產管理"))

    def test_production_help(self):
        from agent_core.agents.telegram_command import handle_gray_production_command

        result = handle_gray_production_command("/production")
        self.assertIn("生產管理 Agent", result)
        self.assertIn("/production status", result)
        self.assertIn("/production report", result)

    def test_production_profile_routes_to_gray_agent(self):
        from agent_core.agents.telegram_command import handle_gray_production_command

        result = handle_gray_production_command("/production profile")

        self.assertIn("gray", result)
        self.assertIn("Production Management", result)
        self.assertIn("command.report_anomaly", result)

    def test_production_status_routes_to_gray_agent(self):
        from agent_core.agents.telegram_command import handle_gray_production_command

        with mock.patch(
            "agent_core.agents.gray_production.production_tracker.list_anomalies",
            return_value=[{"order_id": "P-001", "product": "鞋底 A"}],
        ) as mock_list:
            result = handle_gray_production_command("/production status 5")

        mock_list.assert_called_once_with(5)
        self.assertIn("query.production_status", result)
        self.assertIn("P-001", result)

    def test_production_history_routes_to_gray_agent(self):
        from agent_core.agents.telegram_command import handle_gray_production_command

        with mock.patch(
            "agent_core.agents.gray_production.production_tracker.list_anomalies",
            return_value=[{"order_id": "P-002", "severity": "high"}],
        ) as mock_list:
            result = handle_gray_production_command("/prod history 3")

        mock_list.assert_called_once_with(3)
        self.assertIn("query.anomaly_history", result)
        self.assertIn("P-002", result)

    def test_production_report_without_confirm_previews(self):
        from agent_core.agents.telegram_command import handle_gray_production_command

        payload = (
            '{"order_id":"P-001","product":"鞋底 A","customer":"PAX",'
            '"original_ecd":"2026-05-30","reason":"機台故障","severity":"medium"}'
        )
        with mock.patch(
            "agent_core.agents.gray_production.anomaly.trigger_gray",
            side_effect=AssertionError("未確認不該觸發 Gray Trigger"),
        ):
            result = handle_gray_production_command(f"/production report {payload}")

        self.assertIn("即將回報生產異常", result)
        self.assertIn("+確認", result)
        self.assertIn("P-001", result)

    def test_production_report_with_confirm_dispatches(self):
        from agent_core.agents.telegram_command import handle_gray_production_command

        payload = (
            '{"order_id":"P-001","product":"鞋底 A","customer":"PAX",'
            '"original_ecd":"2026-05-30","reason":"機台故障","severity":"medium"}'
        )
        with mock.patch(
            "agent_core.agents.gray_production.anomaly.trigger_gray",
            return_value={"new_ecd": "2026-06-06", "delay_days": 7, "report": "[gray ok]"},
        ) as mock_trigger:
            result = handle_gray_production_command(
                f"/production report {payload} +確認"
            )

        mock_trigger.assert_called_once()
        self.assertIn("command.report_anomaly", result)
        self.assertIn("2026-06-06", result)

    def test_production_report_from_employee_requires_tg_auth(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.agents.telegram_command import handle_gray_production_command

        payload = (
            '{"order_id":"P-001","product":"鞋底 A","customer":"PAX",'
            '"original_ecd":"2026-05-30","reason":"機台故障","severity":"medium"}'
        )
        with mock.patch("agent_core.tg_auth.check_confirmed", return_value=(False, -1.0)), \
             mock.patch("agent_core.tg_auth.is_locked_out", return_value=(False, 0)):
            result = handle_gray_production_command(
                f"/production report {payload} +確認",
                chat_id="12345",
                caller=Agent.GRAY,
            )

        self.assertIn("🔒", result)
        self.assertIn("tg_auth", result)

    def test_production_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_gray_production_command

        with mock.patch(
            "agent_core.agents.gray_production.production_tracker.list_anomalies",
            return_value=[{"order_id": "P-003"}],
        ):
            result = handle_gray_production_command(
                '/production query.anomaly_history {"recent_n":2}'
            )

        self.assertIn("query.anomaly_history", result)
        self.assertIn("P-003", result)


class TestBlackCashierTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_is_black_cashier_command(self):
        from agent_core.agents.telegram_command import is_black_cashier_command

        self.assertTrue(is_black_cashier_command("/cashier"))
        self.assertTrue(is_black_cashier_command("/cashier summary"))
        self.assertTrue(is_black_cashier_command("/black payments Fulltide"))
        self.assertTrue(is_black_cashier_command("/cash receipts 第一銀行"))
        self.assertTrue(is_black_cashier_command("/出納 profile"))
        self.assertTrue(is_black_cashier_command("/收支 records"))
        self.assertFalse(is_black_cashier_command("/dept black query.profile"))
        self.assertFalse(is_black_cashier_command("出納部門"))

    def test_cashier_help(self):
        from agent_core.agents.telegram_command import handle_black_cashier_command

        result = handle_black_cashier_command("/cashier")
        self.assertIn("出納部門 Agent", result)
        self.assertIn("/cashier payments", result)
        self.assertIn("/cashier receipts", result)

    def test_cashier_profile_routes_to_black_agent(self):
        from agent_core.agents.telegram_command import handle_black_cashier_command

        result = handle_black_cashier_command("/cashier profile")

        self.assertIn("black", result)
        self.assertIn("Cashier", result)
        self.assertNotIn("stub agent", result)

    def test_cashier_summary_routes_to_black_agent(self):
        from agent_core.agents.telegram_command import handle_black_cashier_command

        with mock.patch(
            "agent_core.agents.black_cashier.cashier.cash_summary",
            return_value={"text": "[cash summary]", "total": 2},
        ) as mock_summary:
            result = handle_black_cashier_command("/cashier summary 45")

        mock_summary.assert_called_once_with(days_back=45, limit=5)
        self.assertIn("[cash summary]", result)

    def test_cashier_payments_routes_to_black_agent(self):
        from agent_core.agents.telegram_command import handle_black_cashier_command

        with mock.patch(
            "agent_core.agents.black_cashier.cashier.cash_payments",
            return_value={"text": "[cash payments]", "records": [], "total": 0},
        ) as mock_payments:
            result = handle_black_cashier_command("/cashier payments Fulltide")

        mock_payments.assert_called_once()
        self.assertIn("[cash payments]", result)

    def test_cashier_receipts_routes_to_black_agent(self):
        from agent_core.agents.telegram_command import handle_black_cashier_command

        with mock.patch(
            "agent_core.agents.black_cashier.cashier.cash_receipts",
            return_value={"text": "[cash receipts]", "records": [], "total": 0},
        ) as mock_receipts:
            result = handle_black_cashier_command("/cash receipts 第一銀行")

        mock_receipts.assert_called_once()
        self.assertIn("[cash receipts]", result)

    def test_cashier_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_black_cashier_command

        with mock.patch(
            "agent_core.agents.black_cashier.cashier.cash_records",
            return_value={"text": "[cash records]", "records": [], "total": 0},
        ):
            result = handle_black_cashier_command(
                '/cashier query.cash_records {"direction":"outbound"}'
            )

        self.assertIn("query.cash_records", result)
        self.assertIn("[cash records]", result)


class TestWhiteLegalTelegramShortcut(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_is_white_legal_command(self):
        from agent_core.agents.telegram_command import is_white_legal_command

        self.assertTrue(is_white_legal_command("/legal"))
        self.assertTrue(is_white_legal_command("/legal specs"))
        self.assertTrue(is_white_legal_command("/white search PFAS"))
        self.assertTrue(is_white_legal_command("/spec Richter 5001-4292"))
        self.assertTrue(is_white_legal_command("/contract 保固"))
        self.assertTrue(is_white_legal_command("/法務 profile"))
        self.assertFalse(is_white_legal_command("/dept white query.profile"))
        self.assertFalse(is_white_legal_command("法務部門"))

    def test_legal_help(self):
        from agent_core.agents.telegram_command import handle_white_legal_command

        result = handle_white_legal_command("/legal")
        self.assertIn("法務 SoT Agent", result)
        self.assertIn("/legal specs", result)
        self.assertIn("/legal search", result)

    def test_legal_profile_routes_to_white_agent(self):
        from agent_core.agents.telegram_command import handle_white_legal_command

        result = handle_white_legal_command("/legal profile")

        self.assertIn("white", result)
        self.assertIn("Legal SoT", result)
        self.assertNotIn("stub agent", result)

    def test_legal_specs_routes_to_list_specs(self):
        from agent_core.agents.telegram_command import handle_white_legal_command

        with mock.patch(
            "agent_core.agents.white_legal.specs.list_specs",
            return_value="[spec list]",
        ) as mock_list:
            result = handle_white_legal_command("/legal specs Richter 5001-4292")

        mock_list.assert_called_once_with(customer="Richter", product_model="5001-4292")
        self.assertIn("[spec list]", result)

    def test_direct_specs_command_lists_all_specs(self):
        from agent_core.agents.telegram_command import handle_white_legal_command

        with mock.patch(
            "agent_core.agents.white_legal.specs.list_specs",
            return_value="[all specs]",
        ) as mock_list:
            result = handle_white_legal_command("/specs")

        mock_list.assert_called_once_with(customer="", product_model="")
        self.assertIn("[all specs]", result)

    def test_legal_spec_routes_to_latest_spec(self):
        from agent_core.agents.telegram_command import handle_white_legal_command

        with mock.patch(
            "agent_core.agents.white_legal.specs._load_spec_version",
            return_value={"customer": "Richter", "product_model": "5001-4292"},
        ) as mock_load:
            result = handle_white_legal_command("/legal spec Richter 5001-4292")

        mock_load.assert_called_once_with("Richter", "5001-4292", "latest")
        self.assertIn("query.get_latest_spec", result)
        self.assertIn("Richter", result)

    def test_legal_search_routes_to_white_agent(self):
        from agent_core.agents.telegram_command import handle_white_legal_command

        with mock.patch(
            "agent_core.rag_gateway.semantic_search",
            return_value=[{"text": "PFAS clause"}],
        ) as mock_search:
            result = handle_white_legal_command("/legal search PFAS 保固")

        mock_search.assert_called_once()
        self.assertIn("query.search_docs", result)
        self.assertIn("PFAS clause", result)

    def test_legal_advanced_query_routes_to_dept(self):
        from agent_core.agents.telegram_command import handle_white_legal_command

        with mock.patch(
            "agent_core.agents.white_legal.specs.compare_specs",
            return_value="[compare ok]",
        ):
            result = handle_white_legal_command(
                '/legal query.compare_specs {"customer":"Richter","product_model":"5001-4292"}'
            )

        self.assertIn("query.compare_specs", result)
        self.assertIn("[compare ok]", result)


class TestHandleDeptCommand(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def tearDown(self):
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()

    def test_help_when_no_args(self):
        from agent_core.agents.telegram_command import handle_dept_command
        result = handle_dept_command("/dept")
        self.assertIn("/dept", result)
        self.assertIn("green", result)  # registered colors should appear
        self.assertIn("orange", result)

    def test_unknown_color(self):
        from agent_core.agents.telegram_command import handle_dept_command
        result = handle_dept_command("/dept fuchsia query.x")
        self.assertIn("未知 color", result)
        self.assertIn("fuchsia", result)

    def test_only_color_arg_shows_usage(self):
        from agent_core.agents.telegram_command import handle_dept_command
        result = handle_dept_command("/dept green")
        self.assertIn("用法", result)
        self.assertIn("green", result)

    def test_bad_json_payload(self):
        from agent_core.agents.telegram_command import handle_dept_command
        result = handle_dept_command("/dept green query.x {bad json")
        self.assertIn("JSON", result)
        self.assertIn("解析失敗", result)

    def test_non_object_payload(self):
        from agent_core.agents.telegram_command import handle_dept_command
        # 合法 JSON 但不是 object
        result = handle_dept_command("/dept green query.x [1,2,3]")
        self.assertIn("payload 必須是 JSON object", result)

    def test_dispatch_to_real_black(self):
        from agent_core.agents.telegram_command import handle_dept_command

        result = handle_dept_command("/dept black query.profile")

        self.assertIn("black", result)
        self.assertIn("Cashier", result)
        self.assertNotIn("stub agent", result)

    def test_dispatch_to_real_blue(self):
        from agent_core.agents.telegram_command import handle_dept_command

        result = handle_dept_command("/dept blue query.profile")

        self.assertIn("blue", result)
        self.assertIn("Shipping", result)
        self.assertNotIn("stub agent", result)

    def test_dispatch_to_real_yellow(self):
        from agent_core.agents.telegram_command import handle_dept_command

        result = handle_dept_command("/dept yellow query.profile")

        self.assertIn("yellow", result)
        self.assertIn("Procurement", result)
        self.assertNotIn("stub agent", result)

    def test_dispatch_to_real_indigo(self):
        from agent_core.agents.telegram_command import handle_dept_command

        result = handle_dept_command("/dept indigo query.profile")

        self.assertIn("indigo", result)
        self.assertIn("Warehouse", result)
        self.assertNotIn("stub agent", result)

    def test_dispatch_to_real_purple(self):
        from agent_core.agents.telegram_command import handle_dept_command

        result = handle_dept_command("/dept purple query.profile")

        self.assertIn("purple", result)
        self.assertIn("Accounting", result)
        self.assertNotIn("stub agent", result)

    def test_dispatch_to_real_gray(self):
        from agent_core.agents.telegram_command import handle_dept_command

        result = handle_dept_command("/dept gray query.profile")

        self.assertIn("gray", result)
        self.assertIn("Production Management", result)
        self.assertNotIn("stub agent", result)

    def test_dispatch_to_real_green(self):
        from agent_core.agents.telegram_command import handle_dept_command
        # mock list_tracked_samples 避免摸到真實 sample_tracker.json
        with mock.patch(
            "agent_core.agents.green_sample_dev.sample_tracker.list_tracked_samples",
            return_value="[mock samples]",
        ):
            result = handle_dept_command("/dept green query.list_samples")
        self.assertIn("green", result)
        self.assertIn("[mock samples]", result)

    def test_command_intent_blocked(self):
        # Codex round 2 P1：/dept 禁止 command.*（會繞過 tg_auth 確認窗）
        from agent_core.agents.telegram_command import handle_dept_command
        result = handle_dept_command(
            '/dept orange command.generate_quote {"customer":"X"}'
        )
        self.assertIn("🔒", result)
        self.assertIn("query.*", result)
        self.assertIn("command.generate_quote", result)
        # 確認真的沒呼到 generate_quote — 用 mock 守
        with mock.patch(
            "agent_core.agents.orange_sales.quote_gen.generate_quote",
            side_effect=AssertionError("不該被呼叫"),
        ):
            handle_dept_command(
                '/dept orange command.generate_quote {"customer":"X"}'
            )
            # 上面沒 raise = mock 沒被呼到 = 防線有效

    def test_arbitrary_non_query_intent_blocked(self):
        # 防呆：任何非 query.* 的 intent 都擋，包含 sync 寫操作
        from agent_core.agents.telegram_command import handle_dept_command
        for bad_intent in (
            "command.x", "mutate.y", "wipe", "do.something",
            "command.sync_drive", "command.sync_gmail",
        ):
            result = handle_dept_command(f"/dept green {bad_intent}")
            self.assertIn("🔒", result, f"應擋 {bad_intent!r}")

    def test_query_intent_still_works(self):
        # 確認 query.* 不被誤殺
        from agent_core.agents.telegram_command import handle_dept_command
        with mock.patch(
            "agent_core.agents.green_sample_dev.sample_tracker.list_tracked_samples",
            return_value="[ok]",
        ):
            result = handle_dept_command("/dept green query.list_samples")
        self.assertIn("[ok]", result)

    def test_employee_caller_can_query_own_department(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.agents.telegram_command import handle_dept_command

        with mock.patch(
            "agent_core.agents.green_sample_dev.sample_tracker.list_tracked_samples",
            return_value="[green own ok]",
        ):
            result = handle_dept_command(
                "/dept green query.list_samples",
                caller=Agent.GREEN,
            )

        self.assertIn("[green own ok]", result)

    def test_employee_caller_respects_permission_matrix(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.agents.telegram_command import handle_dept_command

        result = handle_dept_command(
            "/dept purple query.list_accounts",
            caller=Agent.ORANGE,
        )

        self.assertIn("🔒", result)
        self.assertIn("not permitted", result)

    def test_unknown_intent_on_real_agent(self):
        # 用 query. 開頭 — 過 allowlist，到 agent 才被認出 unknown intent
        from agent_core.agents.telegram_command import handle_dept_command
        result = handle_dept_command("/dept green query.totally_unknown")
        # GreenSampleDevAgent raises ValueError("unknown intent ...")
        self.assertIn("unknown intent", result)

    def test_red_target_friendly_error(self):
        # Red 不在 registry；給友好說明
        from agent_core.agents.telegram_command import handle_dept_command
        result = handle_dept_command("/dept red query.x")
        self.assertIn("Red", result)
        self.assertIn("daemon", result)

    def test_registry_init_failure_returns_string(self):
        # Codex P2：_get_dept() 失敗時 handle_dept_command 不該 raise
        from agent_core.agents.telegram_command import handle_dept_command
        with mock.patch(
            "agent_core.agents.wire.build_default_registry",
            side_effect=RuntimeError("boom-during-init"),
        ):
            result = handle_dept_command("/dept green query.list_samples")
        self.assertIsInstance(result, str)
        self.assertIn("registry 初始化失敗", result)
        self.assertIn("boom-during-init", result)

    def test_help_when_registry_fails(self):
        from agent_core.agents.telegram_command import handle_dept_command
        with mock.patch(
            "agent_core.agents.wire.build_default_registry",
            side_effect=RuntimeError("boom-help-init"),
        ):
            result = handle_dept_command("/dept")
        self.assertIsInstance(result, str)
        # help 主體仍要在
        self.assertIn("用法", result)
        # 但 registered 段降級提示
        self.assertIn("無法取得", result)

    def test_caller_is_super_admin_red(self):
        # /dept 用 RED 作 caller → 矩陣允許所有 target，連 White SoT 也能查
        from agent_core.agents.telegram_command import handle_dept_command
        with mock.patch(
            "agent_core.agents.white_legal.specs.list_specs",
            return_value="[mock specs]",
        ):
            result = handle_dept_command("/dept white query.list_specs")
        self.assertIn("[mock specs]", result)


class TestTgHandleMessageDeptShortCircuit(unittest.TestCase):
    """確認 daemon_telegram.tg_handle_message 在 /dept 訊息會短路，
    不會走到 Gemini。"""

    def test_dept_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        # 用 sentinel 確認不會碰 Gemini factory
        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/dept 訊息不該打 Gemini"
        ))

        with mock.patch(
            "agent_core.agents.green_sample_dev.sample_tracker.list_tracked_samples",
            return_value="[short-circuit ok]",
        ):
            result = daemon_telegram.tg_handle_message(
                "/dept green query.list_samples",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("[short-circuit ok]", result)
        sentinel_factory.assert_not_called()

    def test_dev_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/dev 訊息不該打 Gemini"
        ))

        result = daemon_telegram.tg_handle_message(
            "/dev profile",
            agent_persona="",
            tools_list=[],
            gemini_model="x",
            agent_client_factory=sentinel_factory,
            agent_types_factory=sentinel_factory,
        )

        self.assertIn("樣品室", result)
        sentinel_factory.assert_not_called()

    def test_sales_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/sales 訊息不該打 Gemini"
        ))

        with mock.patch(
            "agent_core.agents.orange_sales.customer_intel.customer_360",
            return_value="[sales short-circuit ok]",
        ):
            result = daemon_telegram.tg_handle_message(
                "/sales customer PAX",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("[sales short-circuit ok]", result)
        sentinel_factory.assert_not_called()

    def test_warehouse_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/warehouse 訊息不該打 Gemini"
        ))

        with mock.patch(
            "agent_core.agents.indigo_warehouse.warehouse.stock_availability",
            return_value={"text": "[warehouse short-circuit ok]"},
        ):
            result = daemon_telegram.tg_handle_message(
                "/warehouse stock EVA",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("[warehouse short-circuit ok]", result)
        sentinel_factory.assert_not_called()

    def test_accounting_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/accounting 訊息不該打 Gemini"
        ))

        with mock.patch(
            "agent_core.agents.purple_accounting.accounting.accounting_summary",
            return_value={"text": "[accounting short-circuit ok]"},
        ):
            result = daemon_telegram.tg_handle_message(
                "/accounting summary",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("[accounting short-circuit ok]", result)
        sentinel_factory.assert_not_called()

    def test_production_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/production 訊息不該打 Gemini"
        ))

        with mock.patch(
            "agent_core.agents.gray_production.production_tracker.list_anomalies",
            return_value=[{"order_id": "P-001"}],
        ):
            result = daemon_telegram.tg_handle_message(
                "/production status",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("P-001", result)
        sentinel_factory.assert_not_called()

    def test_cashier_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/cashier 訊息不該打 Gemini"
        ))

        with mock.patch(
            "agent_core.agents.black_cashier.cashier.cash_summary",
            return_value={"text": "[cashier short-circuit ok]", "total": 0},
        ):
            result = daemon_telegram.tg_handle_message(
                "/cashier summary",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("[cashier short-circuit ok]", result)
        sentinel_factory.assert_not_called()

    def test_legal_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/legal 訊息不該打 Gemini"
        ))

        with mock.patch(
            "agent_core.rag_gateway.semantic_search",
            return_value=[{"text": "[legal short-circuit ok]"}],
        ):
            result = daemon_telegram.tg_handle_message(
                "/legal search PFAS",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("[legal short-circuit ok]", result)
        sentinel_factory.assert_not_called()

    def test_purple_start_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/start 訊息不該打 Gemini"
        ))

        with mock.patch.dict(
            os.environ,
            {"RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "purple"},
            clear=False,
        ):
            result = daemon_telegram.tg_handle_message(
                "/start",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("Purple 會計部門 Agent", result)
        self.assertIn("/accounting summary", result)
        sentinel_factory.assert_not_called()

    def test_gray_start_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/start 訊息不該打 Gemini"
        ))

        with mock.patch.dict(
            os.environ,
            {"RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "gray"},
            clear=False,
        ):
            result = daemon_telegram.tg_handle_message(
                "/start",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("Gray 生產管理 Agent", result)
        self.assertIn("/production status", result)
        sentinel_factory.assert_not_called()

    def test_black_start_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/start 訊息不該打 Gemini"
        ))

        with mock.patch.dict(
            os.environ,
            {"RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "black"},
            clear=False,
        ):
            result = daemon_telegram.tg_handle_message(
                "/start",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("Black 出納部門 Agent", result)
        self.assertIn("/cashier summary", result)
        sentinel_factory.assert_not_called()

    def test_white_start_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/start 訊息不該打 Gemini"
        ))

        with mock.patch.dict(
            os.environ,
            {"RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "white"},
            clear=False,
        ):
            result = daemon_telegram.tg_handle_message(
                "/start",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("White 法務 SoT Agent", result)
        self.assertIn("/legal specs", result)
        sentinel_factory.assert_not_called()

    def test_bot_mentioned_command_short_circuits_before_gemini(self):
        from agent_core import daemon_telegram
        from agent_core.agents import telegram_command

        telegram_command._reset_for_test()
        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/dev@bot 訊息不該打 Gemini"
        ))

        with mock.patch.dict(os.environ, {"RED_TELEGRAM_BOT_USERNAME": "RedAgent"}):
            result = daemon_telegram.tg_handle_message(
                "/dev@RedAgent profile",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
            )

        self.assertIn("樣品室", result)
        sentinel_factory.assert_not_called()

    def test_whoami_reports_telegram_identity_without_gemini(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/whoami 訊息不該打 Gemini"
        ))

        result = daemon_telegram.tg_handle_message(
            "/whoami",
            agent_persona="",
            tools_list=[],
            gemini_model="x",
            agent_client_factory=sentinel_factory,
            agent_types_factory=sentinel_factory,
            chat_id="-100123",
            telegram_actor={
                "chat_id": "-100123",
                "color": "green",
                "name": "Dev group",
                "source": "RED_TELEGRAM_AGENT_CHATS",
            },
            telegram_message={
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": 456, "username": "alice", "first_name": "Alice"},
            },
        )

        self.assertIn("chat.id: -100123", result)
        self.assertIn("from.id: 456", result)
        self.assertIn("actor.color: green", result)
        sentinel_factory.assert_not_called()

    def test_unbound_private_chat_gets_binding_hint(self):
        from agent_core import daemon_telegram

        result = daemon_telegram._telegram_unbound_private_chat_reply({
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345, "username": "alice", "first_name": "Alice"},
        })

        self.assertIn("尚未綁定公司身份", result)
        self.assertIn("chat.id: 12345", result)
        self.assertIn("from.username: @alice", result)
        self.assertIn("加入申請送給管理員", result)

    def test_unbound_group_chat_stays_silent(self):
        from agent_core import daemon_telegram

        result = daemon_telegram._telegram_unbound_private_chat_reply({
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 12345, "username": "alice"},
        })

        self.assertEqual(result, "")

    def test_join_request_prompt_and_owner_approval(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "Telegram 加入核准不該打 Gemini"
        ))

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            os.environ,
            {
                "RED_TELEGRAM_JOIN_REQUESTS_FILE": os.path.join(tmpdir, "joins.json"),
                "RED_TELEGRAM_PRIVATE_APPROVALS_FILE": os.path.join(tmpdir, "approvals.json"),
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "green",
                "RED_TELEGRAM_STATE_SUFFIX": "green",
            },
            clear=False,
        ):
            record = daemon_telegram._telegram_record_join_request(
                {
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 12345, "username": "alice", "first_name": "Alice"},
                    "text": "我要加入",
                },
                text="我要加入",
                update_id=7,
            )
            prompt = daemon_telegram._telegram_join_owner_prompt(record)
            self.assertIn("Green 樣品 有人要加入，同意嗎？", prompt)
            self.assertIn("請直接按下面的「同意」或「不同意」", prompt)
            markup = daemon_telegram._telegram_join_approval_reply_markup(record)
            buttons = markup["inline_keyboard"][0]
            self.assertEqual(buttons[0]["text"], "同意")
            self.assertEqual(buttons[1]["text"], "不同意")
            self.assertIn(":a:", buttons[0]["callback_data"])
            self.assertIn(":r:", buttons[1]["callback_data"])

            result = daemon_telegram.tg_handle_message(
                "/approve_tg green",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
                chat_id="999",
                telegram_actor={
                    "chat_id": "999",
                    "color": "red",
                    "name": "Boss",
                    "source": "telegram-chat-id",
                    "is_owner": "true",
                },
            )
            actors = daemon_telegram._telegram_private_approval_actors()

        self.assertIn("已核准", result)
        self.assertIn("12345", actors)
        self.assertEqual(actors["12345"]["color"], "green")
        self.assertEqual(actors["12345"]["name"], "Alice")
        sentinel_factory.assert_not_called()

    def test_join_callback_approves_without_gemini(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "Telegram 按鈕核准不該打 Gemini"
        ))

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            os.environ,
            {
                "RED_TELEGRAM_JOIN_REQUESTS_FILE": os.path.join(tmpdir, "joins.json"),
                "RED_TELEGRAM_PRIVATE_APPROVALS_FILE": os.path.join(tmpdir, "approvals.json"),
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "orange",
                "RED_TELEGRAM_STATE_SUFFIX": "orange",
            },
            clear=False,
        ):
            record = daemon_telegram._telegram_record_join_request(
                {
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 12345, "username": "alice", "first_name": "Alice"},
                    "text": "我要加入",
                },
                text="我要加入",
                update_id=7,
            )
            data = daemon_telegram._telegram_join_approval_reply_markup(record)[
                "inline_keyboard"
            ][0][0]["callback_data"]
            result, target, action = daemon_telegram._telegram_handle_join_callback(
                data,
                chat_id="999",
                telegram_actor={
                    "chat_id": "999",
                    "color": "red",
                    "name": "Boss",
                    "source": "telegram-chat-id",
                    "is_owner": "true",
                },
            )
            actors = daemon_telegram._telegram_private_approval_actors()

        self.assertEqual(target, "12345")
        self.assertEqual(action, "approve")
        self.assertIn("已核准", result)
        self.assertEqual(actors["12345"]["color"], "orange")
        self.assertIn(
            "已同意",
            daemon_telegram._telegram_join_callback_ack(result, action),
        )
        self.assertIn(
            "可以直接傳訊息",
            daemon_telegram._telegram_join_applicant_resolution_notice(action, result),
        )
        sentinel_factory.assert_not_called()

    def test_join_approval_rejects_non_owner(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "未授權核准不該打 Gemini"
        ))

        result = daemon_telegram.tg_handle_message(
            "/approve_tg green",
            agent_persona="",
            tools_list=[],
            gemini_model="x",
            agent_client_factory=sentinel_factory,
            agent_types_factory=sentinel_factory,
            chat_id="12345",
            telegram_actor={
                "chat_id": "12345",
                "color": "orange",
                "name": "Sales",
                "source": "employee_registry",
            },
        )

        self.assertIn("只有 Red owner", result)
        sentinel_factory.assert_not_called()

    def test_group_noise_gate_only_allows_explicit_commands_or_mentions(self):
        from agent_core import daemon_telegram

        base = {"chat": {"id": -100123, "type": "supergroup"}}

        self.assertTrue(daemon_telegram._telegram_should_ignore_group_message({
            **base,
            "text": "大家早",
        }, bot_username="RedAgent"))
        self.assertFalse(daemon_telegram._telegram_should_ignore_group_message({
            **base,
            "text": "/dept green query.profile",
        }, bot_username="RedAgent"))
        self.assertFalse(daemon_telegram._telegram_should_ignore_group_message({
            **base,
            "text": "/dept@RedAgent green query.profile",
        }, bot_username="RedAgent"))
        self.assertTrue(daemon_telegram._telegram_should_ignore_group_message({
            **base,
            "text": "/dept@OtherBot green query.profile",
        }, bot_username="RedAgent"))
        self.assertFalse(daemon_telegram._telegram_should_ignore_group_message({
            **base,
            "text": "@RedAgent 幫我看一下",
        }, bot_username="RedAgent"))

    def test_employee_actor_does_not_fall_through_to_gemini(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "員工 Telegram 入口不該進全工具 Gemini"
        ))

        # freeform 關 + 私訊 → 自由文字改走唯讀 dept_nlp_query 引擎（mock 掉，
        # 別讓單測打真 Gemini）—— 但全工具 session factory 仍然絕不能被碰。
        with mock.patch.dict(os.environ,
                             {"RED_TG_EMPLOYEE_FREEFORM": ""}, clear=False), \
             mock.patch(
                 "agent_core.dept_nlp_query.answer_dept_question",
                 return_value="NL 回覆",
             ) as nlp:
            result = daemon_telegram.tg_handle_message(
                "hello",
                agent_persona="",
                tools_list=[],
                gemini_model="x",
                agent_client_factory=sentinel_factory,
                agent_types_factory=sentinel_factory,
                chat_id="12345",
                telegram_actor={
                    "chat_id": "12345",
                    "color": "green",
                    "name": "Alice",
                    "source": "employee_registry",
                },
                telegram_message={"chat": {"id": 12345, "type": "private"}},
            )

        self.assertEqual(result, "NL 回覆")
        self.assertEqual(nlp.call_args[0][0], "green")
        sentinel_factory.assert_not_called()

    def test_employee_actor_dept_command_uses_actor_permissions(self):
        from agent_core import daemon_telegram

        sentinel_factory = mock.Mock(side_effect=AssertionError(
            "/dept 訊息不該打 Gemini"
        ))

        result = daemon_telegram.tg_handle_message(
            "/dept purple query.list_accounts",
            agent_persona="",
            tools_list=[],
            gemini_model="x",
            agent_client_factory=sentinel_factory,
            agent_types_factory=sentinel_factory,
            chat_id="12345",
            telegram_actor={
                "chat_id": "12345",
                "color": "orange",
                "name": "Sales",
                "source": "employee_registry",
            },
        )

        self.assertIn("🔒", result)
        self.assertIn("not permitted", result)
        sentinel_factory.assert_not_called()


class TestIngestCommand(unittest.TestCase):
    """is_ingest_command + handle_ingest_command 測試。"""

    def test_is_ingest_command(self):
        from agent_core.agents.telegram_command import is_ingest_command
        self.assertTrue(is_ingest_command("/ingest"))
        self.assertTrue(is_ingest_command("/ingest white command.sync_drive"))
        self.assertFalse(is_ingest_command("/dept white query.x"))
        self.assertFalse(is_ingest_command("hello"))

    def test_ingest_help_no_args(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        result = handle_ingest_command("/ingest")
        self.assertIn("command.sync_drive", result)
        self.assertIn("command.sync_gmail", result)

    def test_ingest_blocks_non_allowlist(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        for bad in ("command.generate_quote", "query.list_specs", "command.x"):
            result = handle_ingest_command(f"/ingest orange {bad}")
            self.assertIn("🔒", result, f"應擋 {bad!r}")

    def test_ingest_requires_confirm_for_write(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        # sync_drive 有 payload（寫操作）但沒 +確認 → preview
        result = handle_ingest_command(
            '/ingest white command.sync_drive {"folder_id": "abc"}'
        )
        self.assertIn("⚠️", result)
        self.assertIn("+確認", result)

    def test_ingest_sync_gmail_no_payload_requires_confirm(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        # sync_gmail 空 payload 仍觸發 200-thread ingest，必須確認
        result = handle_ingest_command("/ingest orange command.sync_gmail")
        self.assertIn("⚠️", result)
        self.assertIn("+確認", result)

    def test_ingest_sync_drive_status_no_confirm_needed(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch

        # 空 payload = sync_status，不需確認
        with patch("agent_core.ingest.drive_sync.sync_status") as mock_status:
            mock_status.return_value = {"collection": "drive_docs", "total_chunks": 7}
            result = handle_ingest_command("/ingest white command.sync_drive")

        mock_status.assert_called_once()
        self.assertIn("drive_docs", result)

    def test_ingest_sync_drive_with_confirm(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch

        with patch("agent_core.ingest.drive_sync.sync_folder") as mock_sync:
            mock_sync.return_value = {"total": 3, "synced": 3, "skipped": 0, "purged": 0}
            result = handle_ingest_command(
                '/ingest white command.sync_drive {"folder_id": "abc"} +確認'
            )

        mock_sync.assert_called_once_with("abc", recursive=False)
        self.assertIn("synced", result)

    def test_ingest_sync_gmail_with_confirm(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch

        with patch("agent_core.ingest.gmail_sync.sync_query") as mock_sync:
            mock_sync.return_value = {"query": "newer_than:180d", "synced": 10}
            result = handle_ingest_command(
                '/ingest orange command.sync_gmail {"gmail_query": "newer_than:180d", "max_threads": 10} +確認'
            )

        mock_sync.assert_called_once_with("newer_than:180d", 10)
        self.assertIn("synced", result)

    def test_ingest_revokes_tg_auth_token_after_write(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch, call

        with patch("agent_core.ingest.drive_sync.sync_folder") as mock_sync, \
             patch("agent_core.tg_auth.revoke_after_use") as mock_revoke, \
             patch("agent_core.tg_auth.check_confirmed", return_value=(True, 0.5)), \
             patch("agent_core.tg_auth.is_locked_out", return_value=(False, 0.0)):
            mock_sync.return_value = {"total": 1, "synced": 1, "skipped": 0, "purged": 0}
            handle_ingest_command(
                '/ingest white command.sync_drive {"folder_id": "abc"} +確認',
                chat_id="12345",
            )

        mock_revoke.assert_called_once_with("12345")

    def test_ingest_status_does_not_revoke(self):
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch

        with patch("agent_core.ingest.drive_sync.sync_status") as mock_status, \
             patch("agent_core.tg_auth.revoke_after_use") as mock_revoke:
            mock_status.return_value = {"total_chunks": 0}
            handle_ingest_command("/ingest white command.sync_drive", chat_id="12345")

        mock_revoke.assert_not_called()

    def test_ingest_revokes_on_dispatch_exception(self):
        # Codex R20 P1: revoke must run even when dispatch raises
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch

        with patch("agent_core.ingest.drive_sync.sync_folder",
                   side_effect=RuntimeError("boom")), \
             patch("agent_core.tg_auth.revoke_after_use") as mock_revoke, \
             patch("agent_core.tg_auth.check_confirmed", return_value=(True, 0.5)), \
             patch("agent_core.tg_auth.is_locked_out", return_value=(False, 0.0)):
            result = handle_ingest_command(
                '/ingest white command.sync_drive {"folder_id": "abc"} +確認',
                chat_id="99999",
            )

        self.assertIn("boom", result)
        mock_revoke.assert_called_once_with("99999")

    def test_ingest_revokes_on_permission_denied(self):
        # Codex R20 P1: revoke must run on PermissionDenied too
        from agent_core.agents.telegram_command import handle_ingest_command
        from agent_core.agents import PermissionDenied
        from unittest.mock import patch, MagicMock

        mock_middleware = MagicMock()
        mock_middleware.dispatch.side_effect = PermissionDenied("no access")
        mock_registry = MagicMock()
        mock_registry.__contains__ = lambda self, x: True

        with patch("agent_core.agents.telegram_command._get_dept",
                   return_value=(mock_registry, mock_middleware)), \
             patch("agent_core.tg_auth.revoke_after_use") as mock_revoke, \
             patch("agent_core.tg_auth.check_confirmed", return_value=(True, 0.5)), \
             patch("agent_core.tg_auth.is_locked_out", return_value=(False, 0.0)):
            result = handle_ingest_command(
                '/ingest white command.sync_drive {"folder_id": "abc"} +確認',
                chat_id="77777",
            )

        self.assertIn("🔒", result)
        mock_revoke.assert_called_once_with("77777")

    def test_ingest_blocks_when_no_tg_auth_token(self):
        # Codex R21 P1: +確認 suffix alone is insufficient when chat_id present
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch

        with patch("agent_core.tg_auth.check_confirmed", return_value=(False, -1.0)), \
             patch("agent_core.tg_auth.is_locked_out", return_value=(False, 0.0)):
            result = handle_ingest_command(
                '/ingest white command.sync_drive {"folder_id": "abc"} +確認',
                chat_id="55555",
            )

        self.assertIn("🔒", result)
        self.assertIn("token", result)

    def test_ingest_blocks_when_chat_is_locked_out(self):
        # Codex R21 P1: locked-out chat cannot execute via +確認
        from agent_core.agents.telegram_command import handle_ingest_command
        from unittest.mock import patch

        with patch("agent_core.tg_auth.check_confirmed", return_value=(False, -1.0)), \
             patch("agent_core.tg_auth.is_locked_out", return_value=(True, 180.0)):
            result = handle_ingest_command(
                '/ingest white command.sync_drive {"folder_id": "abc"} +確認',
                chat_id="44444",
            )

        self.assertIn("🔒", result)
        self.assertIn("rate-limit", result)
        self.assertIn("180", result)


class TestConfirmScopeSymmetry(unittest.TestCase):
    """群組 +確認 scope 對稱（健檢 Medium，PR #210 後遺）。

    mark 端（daemon_telegram._confirm_scope_for）在綁定群用 "<chat_id>:<from_id>"
    記 token；之前 green/gray write 與 /ingest 的 check 與 revoke 用裸 chat_id →
    群組內 +確認 永遠對不起來（死鎖）。修法：handler 收 confirm_scope，check 與
    revoke 用同一把 key。私聊（confirm_scope 留空）行為零改變 —— 由既有測試
    （revoke.assert_called_once_with("12345")）續鎖。
    """

    _SCOPE = "-100111:555"
    _GREEN_PAYLOAD = (
        '{"recipe_id":"green_update_sample_status",'
        '"data":{"sample_id":"S-DEV-9","status":"closed"},'
        '"employee_email":"dev@company.example","device_id":"mac-dev-01"}'
    )
    _GRAY_PAYLOAD = (
        '{"order_id":"P-009","product":"鞋底 A","customer":"PAX",'
        '"original_ecd":"2026-05-30","reason":"機台故障","severity":"medium"}'
    )

    def setUp(self):
        import tempfile
        from agent_core.agents import telegram_command
        telegram_command._reset_for_test()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        env = mock.patch.dict(
            os.environ,
            {"RED_EDGE_TASKS_FILE": os.path.join(self._tmpdir.name, "edge_tasks.json")},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(telegram_command._reset_for_test)

    def test_green_enqueue_checks_and_revokes_with_scope(self):
        from agent_core.agents.telegram_command import handle_green_agent_command

        with mock.patch("agent_core.tg_auth.check_confirmed",
                        return_value=(True, 0.5)) as chk, \
             mock.patch("agent_core.tg_auth.revoke_after_use") as rvk:
            result = handle_green_agent_command(
                f"/dev enqueue {self._GREEN_PAYLOAD} +確認",
                chat_id="-100111",
                confirm_scope=self._SCOPE,
            )
        self.assertIn("command.enqueue_edge_task", result)
        chk.assert_called_once_with(self._SCOPE)   # check 用 scope，非裸 chat_id
        rvk.assert_called_once_with(self._SCOPE)   # revoke 同一把 key

    def test_gray_report_checks_and_revokes_with_scope(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.agents.telegram_command import handle_gray_production_command

        with mock.patch(
                "agent_core.agents.gray_production.anomaly.trigger_gray",
                return_value={"new_ecd": "2026-06-06", "delay_days": 7, "report": "[ok]"},
        ), mock.patch("agent_core.tg_auth.check_confirmed",
                      return_value=(True, 0.5)) as chk, \
             mock.patch("agent_core.tg_auth.revoke_after_use") as rvk:
            result = handle_gray_production_command(
                f"/production report {self._GRAY_PAYLOAD} +確認",
                chat_id="-100111",
                caller=Agent.GRAY,
                confirm_scope=self._SCOPE,
            )
        self.assertIn("command.report_anomaly", result)
        chk.assert_called_once_with(self._SCOPE)
        rvk.assert_called_once_with(self._SCOPE)

    def test_ingest_checks_and_revokes_with_scope(self):
        from agent_core.agents.telegram_command import handle_ingest_command

        with mock.patch("agent_core.ingest.drive_sync.sync_folder",
                        return_value={"total": 1, "synced": 1, "skipped": 0, "purged": 0}), \
             mock.patch("agent_core.tg_auth.check_confirmed",
                        return_value=(True, 0.5)) as chk, \
             mock.patch("agent_core.tg_auth.is_locked_out",
                        return_value=(False, 0.0)), \
             mock.patch("agent_core.tg_auth.revoke_after_use") as rvk:
            handle_ingest_command(
                '/ingest white command.sync_drive {"folder_id": "abc"} +確認',
                chat_id="-100111",
                confirm_scope=self._SCOPE,
            )
        chk.assert_called_once_with(self._SCOPE)
        rvk.assert_called_once_with(self._SCOPE)

    def test_group_token_end_to_end_with_real_tg_auth_state(self):
        """端到端（真 tg_auth state）：token 記在群組 scope 下，handler 帶
        confirm_scope 才對得起來；帶裸 chat_id 對不起來（舊 bug 的死鎖）。"""
        from agent_core import tg_auth
        from agent_core.agents.telegram_command import handle_green_agent_command

        scope = "-100999:777"
        try:
            self.assertTrue(tg_auth.mark_confirmed(scope))
            # 舊 bug 路徑：check 用裸 chat_id → 找不到 token → 死鎖
            blocked = handle_green_agent_command(
                f"/dev enqueue {self._GREEN_PAYLOAD} +確認",
                chat_id="-100999",
            )
            self.assertIn("🔒", blocked)
            # 修復路徑：同一把 scope → 放行 + one-shot 消費
            ok = handle_green_agent_command(
                f"/dev enqueue {self._GREEN_PAYLOAD} +確認",
                chat_id="-100999",
                confirm_scope=scope,
            )
            self.assertIn("command.enqueue_edge_task", ok)
            confirmed, _ = tg_auth.check_confirmed(scope)
            self.assertFalse(confirmed, "one-shot：用過即 revoke")
        finally:
            tg_auth.revoke_after_use(scope)


if __name__ == "__main__":
    unittest.main()
