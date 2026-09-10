"""skills/warehouse_brief 倉庫每日 ERP 簡報測試 —— 臨時 DuckDB 假鏡像端到端。

不碰真 DB / Gemini。固定 today=2026-08-04 保證逾期/將到的分段可重現。
"""
import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

import skills.warehouse_brief as wb

_TODAY = datetime.date(2026, 8, 4)


def _build_db(path, *, po_view=(), po_m=(), cd_code=(), stock=()):
    """只建工具真的會摸到的欄位（欄名/型別跟正式鏡像一致）。"""
    import duckdb
    con = duckdb.connect(path)
    con.execute(
        'CREATE TABLE v_purchase_orders("組織" VARCHAR, "採購單號" VARCHAR, '
        '"供應商簡稱" VARCHAR, "料號" VARCHAR, "品名描述" VARCHAR, '
        '"採購單位" VARCHAR, "訂購數量" DOUBLE, "已收數量" DOUBLE, '
        '"計劃到貨日" VARCHAR, "明細狀態" VARCHAR, "單頭狀態" VARCHAR)'
    )
    con.execute(
        "CREATE TABLE SC00__PO_ORDER_M(ORG_ID VARCHAR, ORDER_NO VARCHAR, "
        "PAY_NO VARCHAR, SEND_TYPE VARCHAR, PR_WAY VARCHAR)"
    )
    con.execute(
        "CREATE TABLE SY00__CD_CODE(ORG_ID VARCHAR, RULE_NO VARCHAR, "
        "CODE_NO VARCHAR, NAME_T VARCHAR)"
    )
    con.execute(
        'CREATE TABLE v_stock("料號" VARCHAR, "倉庫名稱" VARCHAR, "結存數量" DOUBLE)'
    )
    for table, rows in (("v_purchase_orders", po_view), ("SC00__PO_ORDER_M", po_m),
                        ("SY00__CD_CODE", cd_code), ("v_stock", stock)):
        for r in rows:
            ph = ", ".join("?" for _ in r)
            con.execute(f"INSERT INTO {table} VALUES ({ph})", list(r))
    con.close()


# 兩張採購單：J1 已逾期（07-20）且未收，J2 將到（08-10）且部分已收。
# J3 已收齊（不該出現）、J4 明細狀態=取消（不該出現）。
_PO_VIEW = [
    ("1", "J1", "隆昌", "ITEM-A", "補強料 TRICOT 54\"", "M", 500.0, None,
     "2026-07-20 00:00:00", "生效", "生效"),
    ("1", "J2", "COSMO", "ITEM-B", "紡織布類 OLYMPIC", "Y", 300.0, 100.0,
     "2026-08-10 00:00:00", "生效", "生效"),
    ("1", "J3", "COATS", "ITEM-C", "縫線", "RL1", 60.0, 60.0,
     "2026-08-06 00:00:00", "生效", "生效"),
    ("1", "J4", "三芳", "ITEM-D", "PU 皮料", "Y", 900.0, None,
     "2026-08-08 00:00:00", "取消", "生效"),
]
_PO_M = [
    ("1", "J1", "004", "05", "03"),
    ("1", "J2", "001", "01", "03"),
    ("1", "J3", "001", "01", "03"),
    ("1", "J4", "001", "01", "03"),
]
_CD_CODE = [
    ("1", "2103", "001", "T/T"),
    ("1", "2103", "004", "T/T30天"),
    ("1", "2105", "01", "海運"),
    ("1", "2105", "05", "廠商自送"),
    ("1", "1303", "03", "越南採購"),
]
_STOCK = [
    ("ITEM-A", "副料倉", 12.0),
    ("ITEM-B", "面料倉", 40.0),
    ("ITEM-B", "副料倉", 5.0),
]


class ArrivalPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        db = os.path.join(cls.tmpdir, "erp.duckdb")
        _build_db(db, po_view=_PO_VIEW, po_m=_PO_M, cd_code=_CD_CODE, stock=_STOCK)
        for p in (mock.patch.object(wb, "_db_path", return_value=db),
                  mock.patch.object(wb, "_today", return_value=_TODAY)):
            p.start()
            cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_splits_overdue_and_upcoming(self):
        out = wb.warehouse_arrival_plan(days_ahead=14, days_overdue=30)
        self.assertIn("逾期未到 1 項", out)
        self.assertIn("近期將到 1 項", out)
        self.assertIn("已逾計劃到貨日 1 項", out)
        self.assertIn("近期將到 1 項 —— 排驗貨人力與倉儲空間", out)

    def test_excludes_fully_received_and_cancelled(self):
        out = wb.warehouse_arrival_plan()
        self.assertNotIn("ITEM-C", out)   # 已收齊
        self.assertNotIn("ITEM-D", out)   # 明細狀態=取消

    def test_null_received_counts_as_zero_not_null(self):
        """RCPT_QTY NULL 不 COALESCE 的話「未到」整列變 NULL、料會靜默消失。"""
        out = wb.warehouse_arrival_plan()
        self.assertIn("ITEM-A", out)
        self.assertRegex(out, r"ITEM-A\s+500\b")

    def test_partial_receipt_reports_remaining_only(self):
        out = wb.warehouse_arrival_plan()
        self.assertRegex(out, r"ITEM-B\s+200\b")   # 300 訂 − 100 收

    def test_decodes_vendor_terms(self):
        out = wb.warehouse_arrival_plan()
        self.assertIn("T/T30天", out)
        self.assertIn("廠商自送", out)
        self.assertIn("越南採購", out)

    def test_stock_summed_across_warehouses(self):
        out = wb.warehouse_arrival_plan()
        self.assertRegex(out, r"ITEM-B\s+200\s+Y\s+45\b")   # 庫存 40 + 5 合計
        self.assertRegex(out, r"面料倉/副料倉|副料倉/面料倉")  # 倉別要都列出來

    def test_states_missing_erp_fields_instead_of_faking_them(self):
        """ERP 沒有 lead time / MOQ / 替代料——報表要明講，不能留白讓人以為有。"""
        out = wb.warehouse_arrival_plan()
        self.assertIn("lead time", out)
        self.assertIn("替代料", out)

    def test_window_excludes_far_future(self):
        out = wb.warehouse_arrival_plan(days_ahead=1, days_overdue=30)
        self.assertIn("ITEM-A", out)      # 逾期仍在窗內
        self.assertNotIn("ITEM-B", out)   # 08-10 超出 +1 天

    def test_empty_window_returns_skip_marker(self):
        """dispatcher 用「(無新發現)」判斷不推播，不能回空字串或假標題。"""
        out = wb.warehouse_arrival_plan(days_ahead=0, days_overdue=0)
        self.assertEqual(out, "(無新發現)")


class StockWatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        db = os.path.join(cls.tmpdir, "erp.duckdb")
        _build_db(db, po_view=_PO_VIEW, po_m=_PO_M, cd_code=_CD_CODE, stock=_STOCK)
        p = mock.patch.object(wb, "_db_path", return_value=db)
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_flags_low_stock_against_incoming(self):
        """ITEM-A：庫存 12 < 在途 500×20%=100 → 要示警。"""
        out = wb.warehouse_stock_watch()
        self.assertIn("ITEM-A", out)

    def test_does_not_flag_adequately_stocked(self):
        """ITEM-B：庫存 45 > 在途 200×20%=40 → 不示警。"""
        out = wb.warehouse_stock_watch()
        self.assertNotIn("ITEM-B", out)

    def test_says_no_safety_stock_field_exists(self):
        out = wb.warehouse_stock_watch()
        self.assertIn("無安全庫存", out)


class BriefCompositionTests(unittest.TestCase):
    """組合報告：任一段失敗不得讓整份報告消失。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        db = os.path.join(cls.tmpdir, "erp.duckdb")
        _build_db(db, po_view=_PO_VIEW, po_m=_PO_M, cd_code=_CD_CODE, stock=_STOCK)
        for p in (mock.patch.object(wb, "_db_path", return_value=db),
                  mock.patch.object(wb, "_today", return_value=_TODAY)):
            p.start()
            cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_kitting_failure_does_not_kill_report(self):
        """齊套段掛掉時：其他段照常出，且那段要明著標失敗。

        靜默吞掉會更糟——收信的人會把「這段沒東西」讀成「今天沒缺料」。
        """
        with mock.patch("skills.erp_kitting.kitting_alert",
                        side_effect=RuntimeError("boom")):
            out = wb.warehouse_daily_brief()
        self.assertIn("進貨預告", out)          # 其他段照常出
        self.assertIn("庫存注意", out)
        self.assertIn("齊套預警載入失敗", out)  # 失敗要看得見，不能靜默消失

    def test_kitting_skip_marker_not_pasted_as_section(self):
        with mock.patch("skills.erp_kitting.kitting_alert", return_value="(無新發現)"):
            out = wb.warehouse_daily_brief()
        self.assertNotIn("齊套/缺料", out)

    def test_all_empty_returns_skip_marker(self):
        with mock.patch.object(wb, "warehouse_arrival_plan", return_value="(無新發現)"), \
             mock.patch.object(wb, "warehouse_stock_watch", return_value="(無新發現)"), \
             mock.patch("skills.erp_kitting.kitting_alert", return_value="(無新發現)"):
            self.assertEqual(wb.warehouse_daily_brief(), "(無新發現)")


class MissingDbTests(unittest.TestCase):
    def test_missing_db_reports_error_not_crash(self):
        with mock.patch.object(wb, "_db_path", return_value="/nonexistent/erp.duckdb"):
            self.assertIn("鏡像倉不存在", wb.warehouse_arrival_plan())
            self.assertIn("鏡像倉不存在", wb.warehouse_stock_watch())


class FormattingTests(unittest.TestCase):
    def test_qty_formatting_drops_trailing_zero(self):
        self.assertEqual(wb._n(500.0), "500")
        self.assertEqual(wb._n(3296.5), "3296.50")
        self.assertEqual(wb._n(None), "")

    def test_cell_sanitizes_untrusted_erp_text(self):
        """品名/供應商是 ERP 自由輸入欄——換行要壓平，注入樣式要被淨化。"""
        self.assertNotIn("\n", wb._cell("A\nB"))
        self.assertNotIn("\t", wb._cell("A\tB"))

    def test_cell_truncates_long_text(self):
        self.assertLessEqual(len(wb._cell("x" * 200)), wb._MAX_CELL)


if __name__ == "__main__":
    unittest.main()
