"""agent_core/erp_stock_query.erp_allocation_lookup —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿 2026-07-30 UserA G407 案的真實形狀：庫存可用量分配（PO_ITEM_SELOT）
同一料號多筆分配、分配日期不同——JFC26563 是 07-23 分配、JFC26574 是 07-30 分配，
freeform 曾把前者當成後者回報。STATUS='1'（1-預購）時 STOC_NO 放轉出批號
（J0C…）、'3'/'9' 時放倉別；裸碼與 +EP 版次料號雙記同一筆要歸戶。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import agent_core.erp_stock_query as esq


def _build_fixture_db(path: str) -> None:
    import duckdb
    con = duckdb.connect(path)
    con.execute(
        "CREATE TABLE SC00__PO_ITEM_SELOT(ORG_ID VARCHAR, STOC_NO VARCHAR, "
        "ITEM_NO VARCHAR, LOT_SEQ VARCHAR, SE_ID VARCHAR, SE_SEQ VARCHAR, "
        "LOT_QTY VARCHAR, LOT_DATE VARCHAR, STATUS VARCHAR, MOVE_MARK VARCHAR, "
        "MOVE_NO VARCHAR, CANCEL_MARK VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__PO_ITEM_SELOT VALUES "
        "('1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # UserA 案主場景：同料同批（J0C26070005）兩筆分配、日期不同
            ("J0C26070005", "DFD00400D05700-G050", "60", "JFC26563", "1",
             "11.19", "2026-07-23 15:38:50", "1", "N", None, "N"),
            ("J0C26070005", "DFD00400D05700-G050", "61", "JFC26574", "1",
             "11.01", "2026-07-30 11:41:01", "1", "N", None, "N"),
            # 自倉別分配（STATUS 3：STOC_NO=倉別）
            ("UMW", "DFD00400D05700-G050", "55", "JFC26495", "1",
             "3.37", "2026-05-20 16:34:11", "3", "N", None, "N"),
            # 裸碼/版次雙記（真實 2025-01 形狀：-005=STATUS 3、裸碼=STATUS 9，
            # 同 LOT_SEQ×訂單×數量×時刻）→ 歸戶一列、類別碼取非 9
            ("PW", "DFD00400D05700-G050-005", "2", "JFC24572", "1",
             "6.6", "2025-01-24 13:01:30", "3", "N", None, "N"),
            ("PW", "DFD00400D05700-G050", "2", "JFC24572", "1",
             "6.6", "2025-01-24 13:01:30", "9", "N", None, "N"),
            # 已取消的分配要標示
            ("UMW", "DFD00400D05700-G050", "40", "JFC25555", "1",
             "2.5", "2026-01-05 09:00:00", "3", "N", None, "Y"),
            # 不相干料號——關鍵字不該掃到
            ("RFW", "XYZ-MAT-A010", "9", "JFC26999", "1",
             "7.7", "2026-07-30 10:00:00", "3", "N", None, "N"),
        ],
    )
    # 庫存主檔單位：家族內 M/Y 混用（真實 G407 形狀）→ 明講、不硬猜
    con.execute(
        "CREATE TABLE SC00__IV_STOC_M(ORG_ID VARCHAR, ITEM_NO VARCHAR, "
        "Q_UNIT VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__IV_STOC_M VALUES ('1', ?, ?)",
        [
            ("DFD00400D05700-G050", "M"),
            ("DFD00400D05700-G050-005", "Y"),
        ],
    )
    con.execute(
        'CREATE TABLE "_item_alias"(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)')
    con.executemany(
        'INSERT INTO "_item_alias" VALUES (?, ?)',
        [
            ("DFD00400D05700-G050", "G407"),
            ("DFD00400D05700-G050-005", "G407+EP5"),
        ],
    )
    con.close()


class ErpAllocationLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_alloc_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 主場景：料號×日期 → 那天分配給哪張指令訂單（UserA 案）──────────

    def test_alloc_by_alias_and_date_returns_only_that_day(self):
        out = esq.erp_allocation_lookup(
            "G407", date_from="2026-07-30", date_to="2026-07-30")
        self.assertIn("JFC26574", out)
        self.assertIn("11.01", out)
        self.assertIn("轉出批號 J0C26070005", out)
        self.assertIn("1-預購", out)
        self.assertIn("分配日期 2026-07-30", out)   # 範圍回顯，防外層改寫
        # 07-23 的舊分配不得混進當日答案（原始事故）
        self.assertNotIn("JFC26563", out)
        self.assertNotIn("11.19", out)

    def test_alloc_history_sorted_desc_and_family_merged(self):
        out = esq.erp_allocation_lookup("G407")
        self.assertIn("庫存編號 G407", out)
        self.assertIn("JFC26563", out)
        self.assertIn("JFC26574", out)
        # 新分配排前面
        self.assertLess(out.index("JFC26574"), out.index("JFC26563"))
        # 單位主檔混用要明講
        self.assertIn("主檔混用", out)

    def test_bare_and_edition_dual_rows_merged_once(self):
        out = esq.erp_allocation_lookup("G407")
        # 雙記歸戶：JFC24572 只列一次、雙碼並列、類別碼取非 9
        self.assertEqual(out.count("JFC24572"), 1)
        self.assertIn("雙記歸戶", out)
        self.assertIn("類別碼3", out)
        self.assertNotIn("類別碼9", out)

    def test_alloc_by_order_no(self):
        out = esq.erp_allocation_lookup("JFC26563")
        self.assertIn("11.19", out)
        self.assertIn("轉出批號 J0C26070005", out)
        self.assertIn("2026-07-23", out)
        self.assertNotIn("JFC26574", out)

    def test_alloc_by_lot_no_lists_all_bindings(self):
        out = esq.erp_allocation_lookup("J0C26070005")
        self.assertIn("JFC26563", out)
        self.assertIn("JFC26574", out)

    def test_status3_renders_source_warehouse(self):
        out = esq.erp_allocation_lookup("JFC26495")
        self.assertIn("自倉 UMW", out)
        self.assertIn("類別碼3", out)

    def test_cancelled_allocation_flagged(self):
        out = esq.erp_allocation_lookup("JFC25555")
        self.assertIn("⚠️已取消", out)

    def test_snapshot_caveat_always_in_footer(self):
        out = esq.erp_allocation_lookup("G407")
        self.assertIn("今天剛在 ERP 做的分配要明晨", out)
        self.assertIn("鏡像時間", out)

    def test_unrelated_item_not_matched(self):
        out = esq.erp_allocation_lookup("G407")
        self.assertNotIn("XYZ-MAT-A010", out)
        self.assertNotIn("JFC26999", out)

    # ── 邊界 ─────────────────────────────────────────────────────────

    def test_no_match_hints_snapshot_lag(self):
        out = esq.erp_allocation_lookup(
            "G407", date_from="2026-07-31", date_to="2026-07-31")
        self.assertIn("查無", out)
        self.assertIn("2026-07-31", out)
        self.assertIn("鏡像時間", out)   # _stale_hint：查無≠ERP 沒有

    def test_bad_date_arg(self):
        out = esq.erp_allocation_lookup("G407", date_from="7/30")
        self.assertIn("日期格式錯誤", out)

    def test_short_keyword_usage(self):
        self.assertIn("用法", esq.erp_allocation_lookup("G"))
        self.assertIn("用法", esq.erp_allocation_lookup(""))

    def test_like_wildcards_are_literal(self):
        out = esq.erp_allocation_lookup("%")
        self.assertIn("用法", out)  # 單字元先擋長度
        out = esq.erp_allocation_lookup("%%")
        self.assertIn("查無", out)

    def test_missing_db_graceful(self):
        with mock.patch.object(esq, "_db_path",
                               return_value=self.db + ".nope"):
            out = esq.erp_allocation_lookup("G407")
        self.assertIn("鏡像倉不存在", out)

    def test_deterministic_same_input_same_output(self):
        self.assertEqual(esq.erp_allocation_lookup("G407"),
                         esq.erp_allocation_lookup("G407"))


class YellowAllocIntentTests(unittest.TestCase):
    def test_intent_returns_text_and_single_date_expands(self):
        from agent_core.agents.yellow_procurement.agent import YellowProcurementAgent
        agent = YellowProcurementAgent()
        with mock.patch("agent_core.erp_stock_query.erp_allocation_lookup",
                        return_value="ALLOC-REPORT") as fn:
            result = agent.handle_query(
                intent="query.erp_alloc",
                payload={"keyword": "G407", "date": "2026-07-30"},
                trace_id="t-alloc-1",
            )
        self.assertEqual(result, {"text": "ALLOC-REPORT"})
        fn.assert_called_once_with("G407", date_from="2026-07-30",
                                   date_to="2026-07-30")

    def test_explicit_range_wins_over_date(self):
        from agent_core.agents.yellow_procurement.agent import YellowProcurementAgent
        agent = YellowProcurementAgent()
        with mock.patch("agent_core.erp_stock_query.erp_allocation_lookup",
                        return_value="ok") as fn:
            agent.handle_query(
                intent="query.erp_alloc",
                payload={"批號": "J0C26070005", "date_from": "2026-07-01",
                         "date_to": "2026-07-31"},
                trace_id="t-alloc-2",
            )
        fn.assert_called_once_with("J0C26070005", date_from="2026-07-01",
                                   date_to="2026-07-31")


class IndigoAllocIntentTests(unittest.TestCase):
    def test_intent_returns_text(self):
        from agent_core.agents.indigo_warehouse.agent import IndigoWarehouseAgent
        agent = IndigoWarehouseAgent()
        with mock.patch("agent_core.erp_stock_query.erp_allocation_lookup",
                        return_value="ALLOC-REPORT") as fn:
            result = agent.handle_query(
                intent="query.erp_alloc",
                payload={"keyword": "J0C26070005"},
                trace_id="t-alloc-3",
            )
        self.assertEqual(result, {"text": "ALLOC-REPORT"})
        fn.assert_called_once_with("J0C26070005", date_from="", date_to="")


class AllocShortcutTests(unittest.TestCase):
    def test_purchase_alloc_with_date_routes_to_intent(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            out = tc.handle_yellow_procurement_command(
                "/purchase alloc G407 2026-07-30")
        self.assertEqual(out, "ok")
        (cmd,), _ = fn.call_args
        self.assertIn("/dept yellow query.erp_alloc", cmd)
        self.assertIn('"keyword": "G407"', cmd)
        self.assertIn('"date": "2026-07-30"', cmd)

    def test_purchase_alloc_keyword_only(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            tc.handle_yellow_procurement_command("/purchase alloc JFC26574")
        (cmd,), _ = fn.call_args
        self.assertIn('"keyword": "JFC26574"', cmd)
        self.assertNotIn('"date"', cmd)

    def test_warehouse_alloc_routes_to_indigo(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            tc.handle_indigo_warehouse_command(
                "/warehouse alloc J0C26070005 2026-07")
        (cmd,), _ = fn.call_args
        self.assertIn("/dept indigo query.erp_alloc", cmd)
        self.assertIn('"date": "2026-07"', cmd)

    def test_usage_lines(self):
        from agent_core.agents import telegram_command as tc
        self.assertIn("用法",
                      tc.handle_yellow_procurement_command("/purchase alloc"))
        self.assertIn("用法",
                      tc.handle_indigo_warehouse_command("/warehouse alloc"))


class AllocScopeTests(unittest.TestCase):
    def test_tool_is_safe_tier(self):
        from agent_core.tool_tiers import get_tier, TIER_SAFE
        self.assertEqual(get_tier("query_erp_allocation"), TIER_SAFE)

    def test_in_yellow_and_indigo_whitelists(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        for color in ("yellow", "indigo"):
            self.assertIn("query_erp_allocation",
                          allowed_tool_names_for_color(color), color)

    def test_skill_exports_tool(self):
        import skills.erp_warehouse as ew
        self.assertIn(ew.query_erp_allocation, ew.SKILL_TOOLS)

    def test_selot_in_hot_tables(self):
        # 分配表凍在初鏡快照就查不到新分配——必須每日重刷
        from agent_core.erp_mirror import HOT_TABLES
        self.assertIn("SC00.PO_ITEM_SELOT", HOT_TABLES)


if __name__ == "__main__":
    unittest.main()
