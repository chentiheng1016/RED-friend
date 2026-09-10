"""agent_core/erp_stock_query 確定性庫存查詢 —— fixture DuckDB，不碰真鏡像/Gemini。"""
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
        "CREATE TABLE v_stock(料號 VARCHAR, 品名描述 VARCHAR, 材料類型 VARCHAR, "
        "倉庫代碼 VARCHAR, 倉庫名稱 VARCHAR, 單位 VARCHAR, 結存數量 DOUBLE)")
    con.executemany(
        "INSERT INTO v_stock VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("D1A0001D0580-G401", '佳積布 200g 58"', "布料", "UMW", "面料倉", "Y", 55.0),
            ("D1A0001D0580-G401", '佳積布 200g 58"', "布料", "PW", "加工料倉", "Y", 6.0),
            ("D1A0001D0580-G401", '佳積布 200g 58"', "布料", "SW", "廢料倉", "Y", 3.0),
            ("D1A0001D0580-G400", '佳積布 200g 58"', "布料", "UMW", "面料倉", "Y", 5.0),
            # 存在但全倉 0 結存的料號（≠ 查無此料）；品名夾 zero-width 驗 sanitize
            ("GL220B", "黃膠\u200b220B", "化工", "UMW", "面料倉", "KG", 0.0),
            # 小數結存
            ("TH-01", "縫線", "副料", "UMW", "面料倉", "M", 12.5),
            # G407 家族（2026-07-29 UserA 案）：庫存掛在 +EP 版次料號上，
            # 只查裸短碼會回 0 庫存漏掉 95Y
            ("DFD00400D05700-G050", "遮光布 57 吋", "布料", "UMW", "面料倉", "M", 0.0),
            ("DFD00400D05700-G050-005", "遮光布 57 吋", "布料", "PW", "加工料倉", "Y", 95.0),
        ],
    )
    con.execute(
        "CREATE TABLE v_stock_lot(料號 VARCHAR, 倉庫代碼 VARCHAR, 批號 VARCHAR, "
        "儲位 VARCHAR, 結存數量 DOUBLE)")
    con.executemany(
        "INSERT INTO v_stock_lot VALUES (?, ?, ?, ?, ?)",
        [
            # 批次語意（2026-07-29 UserA 案）：'0'=可用、'M01'=待倉確認、
            # 'J0C…'=預購批綁單；廢料倉批次不進分解
            ("D1A0001D0580-G401", "UMW", "0", "A-01", 50.0),
            ("D1A0001D0580-G401", "UMW", "M01", "A-02", 5.0),
            ("D1A0001D0580-G401", "PW", "0", "", 6.0),
            ("D1A0001D0580-G401", "UMW", "J0C26040015", "A-09", 0.0),  # 0 結存批不列
            ("D1A0001D0580-G401", "SW", "M01", "", 3.0),   # 廢料倉批次不進分解
        ],
    )
    # 採購單 fixture：仿 2026-07-26 UserA 案的真實形狀（數量小、金額大，
    # LLM 讀 PDF 曾把金額當數量——確定性查詢要分欄講清楚）。
    con.execute(
        "CREATE TABLE v_purchase_orders(採購單號 VARCHAR, 項次 VARCHAR, "
        "下單日期 VARCHAR, 供應商簡稱 VARCHAR, 供應商全名 VARCHAR, 料號 VARCHAR, "
        "品名描述 VARCHAR, 採購單位 VARCHAR, 訂購數量 DOUBLE, 已收數量 DOUBLE, "
        "單價 DOUBLE, 幣別 VARCHAR, 金額 DOUBLE, 計劃到貨日 VARCHAR, "
        "明細狀態 VARCHAR)")
    con.executemany(
        "INSERT INTO v_purchase_orders VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("JF0P25120001", "1", "2025-12-05 16:55:51", "富泰企業",
             "富泰企業股份有限公司", "DFDT7100145000-E040", "紡織布類 超纖布",
             "M", 2104.0, 2104.0, 6.78, "USD", 14265.12,
             "2026-02-04 00:00:00", "結案"),
            ("JF0P26060007", "1", "2026-06-05 09:46:48", "富泰企業",
             "富泰企業股份有限公司", "DFD00400D05700-ZE041", "其他布",
             "M", 500.0, None, 3.83, "USD", 1915.0, None, "生效"),
            ("JF0P26060007", "2", "2026-06-05 09:46:48", "富泰企業",
             "富泰企業股份有限公司", "DFDT7100145000-E040", "紡織布類 超纖布",
             "M", 500.0, None, 6.78, "USD", 3390.0,
             "2026-07-15 00:00:00", "生效"),
            # 取消項次：列出但不計入合計
            ("JF0P26070028", "1", "2026-07-25 16:22:01", "盛嘉輝", None,
             "BXDW0523T0002D044-A020", "模具", "SET", 51.0, None,
             24950.0, "USD", 1272450.0, None, "取消"),
        ],
    )
    # 庫存編號(舊碼)對照——2026-07-28 UserA SF24 案：畫面上的「庫存編號」是
    # SP_ITEM.O_ITEMNO 舊短碼，關鍵字不含於料號字串、必須靠對照表展開。
    con.execute(
        'CREATE TABLE "_item_alias"(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)')
    con.executemany(
        'INSERT INTO "_item_alias" VALUES (?, ?)',
        [
            ("D1A0001D0580-G401", "JB58"),
            ("TH-01", "JB58.5"),           # 一字之差=不同料，不得被 JB58 掃到
            ("DFDT7100145000-E040", "SF24"),
            # G407 家族：+版次變體要一併展開；G407.5 是不同料、不得被掃到
            ("DFD00400D05700-G050", "G407"),
            ("DFD00400D05700-G050-005", "G407+EP5"),
            ("D1A0001D0580-G400", "G407.5"),
        ],
    )
    con.close()


class ErpStockLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_stock_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_match_by_item_no_fragment(self):
        out = esq.erp_stock_lookup("G40")
        self.assertIn("D1A0001D0580-G401", out)
        self.assertIn("D1A0001D0580-G400", out)
        self.assertIn("2 個料號符合", out)
        self.assertIn("面料倉(UMW) 55 Y", out)
        self.assertIn("加工料倉(PW) 6 Y", out)
        self.assertIn("鏡像時間", out)
        # 廢料倉列出但標註、不進可用
        self.assertIn("廢料倉，不計入可用", out)
        # 批次三分解：可用=0批(50+6=56)、M01=5（SW 的 M01 3 不進分解）
        self.assertIn("可用(0批) 56 Y", out)
        self.assertIn("待倉確認(M01) 5 Y", out)
        # 無批次資料的料號退回全批合計口徑並註明
        self.assertIn("可用合計 5 Y（無批次資料", out)

    def test_match_by_description(self):
        out = esq.erp_stock_lookup("佳積布")
        self.assertIn("D1A0001D0580-G401", out)
        self.assertIn("D1A0001D0580-G400", out)

    def test_zero_balance_item_is_reported_not_missing(self):
        out = esq.erp_stock_lookup("GL220B")
        self.assertIn("各倉結存皆為 0", out)
        self.assertIn("可用合計 0 KG", out)
        self.assertNotIn("查無", out)

    def test_no_match_hints_warehouse_excel(self):
        out = esq.erp_stock_lookup("不存在的料")
        self.assertIn("查無", out)
        self.assertIn("read_warehouse_stock", out)
        self.assertIn("明晨刷新後才查得到", out)   # OZ20 案：查無要講快照時滯

    def test_short_or_empty_keyword_usage(self):
        self.assertIn("用法", esq.erp_stock_lookup("G"))
        self.assertIn("用法", esq.erp_stock_lookup(""))
        self.assertIn("用法", esq.erp_stock_lookup(None))

    def test_like_wildcards_are_literal(self):
        # '%' / '_' 當字面值：fixture 無含 '%' 的料號 → 查無，而非整表傾倒
        out = esq.erp_stock_lookup("%%")
        self.assertIn("查無", out)
        out = esq.erp_stock_lookup("__")
        self.assertIn("查無", out)

    def test_decimal_quantity_trimmed(self):
        out = esq.erp_stock_lookup("TH-01")
        self.assertIn("12.5 M", out)
        self.assertNotIn("12.50", out)

    def test_show_lots(self):
        out = esq.erp_stock_lookup("G401", show_lots=True)
        self.assertIn("批 0｜UMW", out)
        self.assertIn("批 M01｜UMW", out)
        self.assertIn("儲位A-01", out)
        self.assertNotIn("J0C26040015", out)  # 0 結存批號不列

    def test_free_text_sanitized_zero_width_stripped(self):
        out = esq.erp_stock_lookup("GL220B")
        self.assertNotIn("\u200b", out)
        self.assertIn("黃膠220B", out)

    def test_missing_db_graceful(self):
        with mock.patch.object(esq, "_db_path",
                               return_value=os.path.join(self.tmp, "nope.duckdb")):
            out = esq.erp_stock_lookup("G40")
        self.assertIn("鏡像倉不存在", out)

    def test_deterministic_same_input_same_output(self):
        self.assertEqual(esq.erp_stock_lookup("G40"), esq.erp_stock_lookup("G40"))

    def test_truncation_note_when_too_many_items(self):
        with mock.patch.object(esq, "_MAX_ITEMS", 1):
            out = esq.erp_stock_lookup("佳積布")
        self.assertIn("只列可用量最大的前 1 個", out)
        # 可用量大者（G401=61）留下，小者被截
        self.assertIn("D1A0001D0580-G401", out)
        self.assertNotIn("D1A0001D0580-G400", out)

    # ── 庫存編號(舊碼)展開（2026-07-28 UserA SF24 案）────────────────

    def test_stock_alias_resolves_to_item(self):
        # "JB58" 不含於任何料號字串——只有對照表能解
        out = esq.erp_stock_lookup("JB58")
        self.assertIn("庫存編號", out)
        self.assertIn("D1A0001D0580-G401", out)
        self.assertIn("可用(0批) 56 Y", out)
        # 精確比對：JB58 不得掃到 JB58.5 的 TH-01
        self.assertNotIn("TH-01", out)

    def test_stock_alias_exact_variant(self):
        out = esq.erp_stock_lookup("jb58.5")   # 大小寫不計
        self.assertIn("TH-01", out)
        self.assertNotIn("D1A0001D0580-G401", out)

    def test_stock_alias_plus_edition_family(self):
        # 2026-07-29 UserA G407 案：庫存掛 +EP 版次料號，查裸短碼要一併帶出，
        # 否則回 0 庫存漏掉 95Y
        out = esq.erp_stock_lookup("G407")
        self.assertIn("DFD00400D05700-G050-005", out)
        self.assertIn("加工料倉(PW) 95 Y", out)
        self.assertIn("(G407+EP5)", out)          # 對照註記標示版次短碼
        # G407.5 是不同料，不得被 G407 掃到
        self.assertNotIn("D1A0001D0580-G400", out)

    def test_stock_alias_absent_table_still_works(self):
        # 舊鏡像沒有 _item_alias 表 → 靜默退回一般關鍵字查詢，不炸
        import duckdb
        bare = os.path.join(self.tmp, "bare.duckdb")
        con = duckdb.connect(bare)
        con.execute("CREATE TABLE v_stock(料號 VARCHAR, 品名描述 VARCHAR, "
                    "材料類型 VARCHAR, 倉庫代碼 VARCHAR, 倉庫名稱 VARCHAR, "
                    "單位 VARCHAR, 結存數量 DOUBLE)")
        con.execute("INSERT INTO v_stock VALUES "
                    "('AAA-01', '測試料', '布料', 'UMW', '面料倉', 'Y', 9.0)")
        con.close()
        with mock.patch.object(esq, "_db_path", return_value=bare):
            out = esq.erp_stock_lookup("AAA-01")
        self.assertIn("AAA-01", out)
        self.assertNotIn("❌", out)


class ErpPoLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_po_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_po_alias_resolves_to_item(self):
        # 庫存編號 SF24 → DFDT7100145000-E040 的採購單（關鍵字不含於任何欄）
        out = esq.erp_po_lookup("SF24")
        self.assertIn("庫存編號", out)
        self.assertIn("JF0P25120001", out)
        self.assertIn("JF0P26060007", out)
        self.assertNotIn("BXDW0523T0002D044-A020", out)

    def test_quantity_and_amount_are_distinct_columns(self):
        # UserA 案核心：數量與金額必須分欄明示，且合計是數量合計、不是金額。
        out = esq.erp_po_lookup("DFDT7100145000-E040")
        self.assertIn("訂購數量 2104 M", out)
        self.assertIn("訂購數量 500 M", out)
        self.assertIn("金額 14265.12 USD", out)
        # 合計 = 2104+500（數量），絕不是 14265.12+3390（金額）
        self.assertIn("訂購數量 2604 M", out)
        self.assertIn("已收數量 2104 M", out)
        self.assertIn("採購金額 17655.12 USD", out)
        self.assertIn("金額是錢、不是數量", out)
        self.assertIn("鏡像時間", out)

    def test_match_by_supplier_and_po_no(self):
        out = esq.erp_po_lookup("富泰")
        self.assertIn("2 張單 / 3 個項次", out)
        out = esq.erp_po_lookup("JF0P26060007")
        self.assertIn("1 張單 / 2 個項次", out)
        self.assertIn("DFD00400D05700-ZE041", out)

    def test_year_filter_excludes_older_po(self):
        out = esq.erp_po_lookup("富泰", date_from="2026", date_to="2026")
        self.assertNotIn("JF0P25120001", out)
        self.assertIn("訂購數量 1000 M", out)
        self.assertIn("下單期間 2026-01-01 ～ 2026-12-31", out)

    def test_month_filter(self):
        out = esq.erp_po_lookup("富泰", date_from="2025-12", date_to="2025-12")
        self.assertIn("JF0P25120001", out)
        self.assertNotIn("JF0P26060007", out)

    def test_cancelled_line_listed_but_excluded_from_totals(self):
        out = esq.erp_po_lookup("BXDW0523T0002")
        self.assertIn("取消（不計入合計）", out)
        self.assertIn("訂購數量 0", out)
        self.assertNotIn("採購金額 1272450", out)

    def test_bad_date_arg(self):
        self.assertIn("日期格式錯誤", esq.erp_po_lookup("G40", date_from="abc"))
        self.assertIn("日期格式錯誤", esq.erp_po_lookup("G40", date_to="2026/01"))

    def test_short_keyword_usage_and_no_match(self):
        self.assertIn("用法", esq.erp_po_lookup("G"))
        self.assertIn("用法", esq.erp_po_lookup(""))
        out = esq.erp_po_lookup("不存在的東西")
        self.assertIn("查無符合", out)
        self.assertIn("明晨刷新後才查得到", out)   # OZ20 案：查無要講快照時滯
        self.assertIn("query_erp_stock", out)

    def test_truncation_totals_still_cover_all(self):
        with mock.patch.object(esq, "_MAX_PO_LINES", 1):
            out = esq.erp_po_lookup("富泰")
        self.assertIn("明細只列最近 1 個項次", out)
        # 只列最新一個項次（2026-06-05 的 #1），但合計仍含全部 3 項
        self.assertIn("訂購數量 3104 M", out)
        self.assertNotIn("JF0P25120001", out)

    def test_deterministic_same_input_same_output(self):
        self.assertEqual(esq.erp_po_lookup("富泰"), esq.erp_po_lookup("富泰"))

    def test_missing_db_graceful(self):
        with mock.patch.object(esq, "_db_path",
                               return_value=os.path.join(self.tmp, "nope.duckdb")):
            out = esq.erp_po_lookup("G40")
        self.assertIn("鏡像倉不存在", out)


class YellowIntentTests(unittest.TestCase):
    def test_query_erp_stock_intent_returns_text(self):
        from agent_core.agents.yellow_procurement.agent import YellowProcurementAgent
        agent = YellowProcurementAgent()
        with mock.patch("agent_core.erp_stock_query.erp_stock_lookup",
                        return_value="STOCK-REPORT") as fn:
            result = agent.handle_query(
                intent="query.erp_stock",
                payload={"keyword": "G40", "lots": True},
                trace_id="t-1",
            )
        self.assertEqual(result, {"text": "STOCK-REPORT"})
        fn.assert_called_once_with("G40", show_lots=True)

    def test_payload_key_aliases(self):
        from agent_core.agents.yellow_procurement.agent import YellowProcurementAgent
        agent = YellowProcurementAgent()
        with mock.patch("agent_core.erp_stock_query.erp_stock_lookup",
                        return_value="ok") as fn:
            agent.handle_query(intent="query.erp_stock",
                               payload={"料號": "GL220B"}, trace_id="t-2")
        fn.assert_called_once_with("GL220B", show_lots=False)

    def test_query_erp_po_intent(self):
        from agent_core.agents.yellow_procurement.agent import YellowProcurementAgent
        agent = YellowProcurementAgent()
        with mock.patch("agent_core.erp_stock_query.erp_po_lookup",
                        return_value="PO-REPORT") as fn:
            result = agent.handle_query(
                intent="query.erp_po",
                payload={"keyword": "G40", "year": "2026"},
                trace_id="t-po-1",
            )
        self.assertEqual(result, {"text": "PO-REPORT"})
        fn.assert_called_once_with("G40", date_from="2026", date_to="2026")

    def test_query_erp_po_explicit_dates_win_over_year(self):
        from agent_core.agents.yellow_procurement.agent import YellowProcurementAgent
        agent = YellowProcurementAgent()
        with mock.patch("agent_core.erp_stock_query.erp_po_lookup",
                        return_value="ok") as fn:
            agent.handle_query(
                intent="query.erp_po",
                payload={"po": "JF0P26060007", "date_from": "2026-01-01",
                         "date_to": "2026-06-30"},
                trace_id="t-po-2",
            )
        fn.assert_called_once_with("JF0P26060007", date_from="2026-01-01",
                                   date_to="2026-06-30")


class EmployeeScopeTests(unittest.TestCase):
    def test_tool_is_safe_tier(self):
        from agent_core.tool_tiers import get_tier, TIER_SAFE
        self.assertEqual(get_tier("query_erp_stock"), TIER_SAFE)
        self.assertEqual(get_tier("query_erp_purchase_orders"), TIER_SAFE)

    def test_in_yellow_whitelist_and_matrix_inheritance(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        self.assertIn("query_erp_stock", allowed_tool_names_for_color("yellow"))
        # QUERY_MATRIX 可查 yellow 的色也繼承（purple 會計對帳要看料況）
        self.assertIn("query_erp_stock", allowed_tool_names_for_color("purple"))
        # indigo 自 2026-07-26 起自有 curation（倉庫也要查 ERP 面料結存）
        self.assertIn("query_erp_stock", allowed_tool_names_for_color("indigo"))
        # green 矩陣含 indigo → 跟著繼承（樣品室查料況合理）
        self.assertIn("query_erp_stock", allowed_tool_names_for_color("green"))

    def test_po_tool_in_whitelists(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        for color in ("yellow", "indigo", "purple", "gray", "green"):
            self.assertIn("query_erp_purchase_orders",
                          allowed_tool_names_for_color(color), color)

    def test_filter_keeps_tool_for_yellow(self):
        from agent_core.dept_tool_scope import filter_tools_for_color

        def query_erp_stock(keyword):
            return ""

        def query_erp_purchase_orders(keyword):
            return ""

        kept, removed = filter_tools_for_color(
            [query_erp_stock, query_erp_purchase_orders], "yellow")
        self.assertEqual([f.__name__ for f in kept],
                         ["query_erp_stock", "query_erp_purchase_orders"])
        self.assertEqual(removed, [])

    def test_skill_exports_tool(self):
        import skills.erp_warehouse as ew
        self.assertIn(ew.query_erp_stock, ew.SKILL_TOOLS)
        self.assertIn(ew.query_erp_purchase_orders, ew.SKILL_TOOLS)

    def test_nl_tool_not_in_employee_whitelist(self):
        # text-to-SQL / raw-SQL 面維持 owner-only：不得因本次改動漏進員工白名單
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        for color in ("yellow", "purple", "orange", "gray", "blue"):
            allowed = allowed_tool_names_for_color(color)
            self.assertNotIn("query_erp_warehouse", allowed, color)
            self.assertNotIn("run_erp_warehouse_sql", allowed, color)


class IndigoIntentTests(unittest.TestCase):
    def test_query_erp_stock_intent_returns_text(self):
        from agent_core.agents.indigo_warehouse.agent import IndigoWarehouseAgent
        agent = IndigoWarehouseAgent()
        with mock.patch("agent_core.erp_stock_query.erp_stock_lookup",
                        return_value="STOCK-REPORT") as fn:
            result = agent.handle_query(
                intent="query.erp_stock",
                payload={"keyword": "G40", "lots": True},
                trace_id="t-3",
            )
        self.assertEqual(result, {"text": "STOCK-REPORT"})
        fn.assert_called_once_with("G40", show_lots=True)

    def test_query_erp_po_intent(self):
        from agent_core.agents.indigo_warehouse.agent import IndigoWarehouseAgent
        agent = IndigoWarehouseAgent()
        with mock.patch("agent_core.erp_stock_query.erp_po_lookup",
                        return_value="PO-REPORT") as fn:
            result = agent.handle_query(
                intent="query.erp_po",
                payload={"keyword": "富泰"},
                trace_id="t-po-3",
            )
        self.assertEqual(result, {"text": "PO-REPORT"})
        fn.assert_called_once_with("富泰", date_from="", date_to="")


class IndigoShortcutTests(unittest.TestCase):
    def test_warehouse_erp_routes_to_dept_intent(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            out = tc.handle_indigo_warehouse_command("/warehouse erp G40")
        self.assertEqual(out, "ok")
        (cmd,), _ = fn.call_args
        self.assertIn("/dept indigo query.erp_stock", cmd)
        self.assertIn('"keyword": "G40"', cmd)
        self.assertIn('"lots": false', cmd)

    def test_warehouse_erp_lots_suffix(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            tc.handle_indigo_warehouse_command("/warehouse erp G40 lots")
        (cmd,), _ = fn.call_args
        self.assertIn('"lots": true', cmd)

    def test_warehouse_erp_usage(self):
        from agent_core.agents import telegram_command as tc
        out = tc.handle_indigo_warehouse_command("/warehouse erp")
        self.assertIn("用法", out)

    def test_legacy_stock_action_unchanged(self):
        # 既有 /warehouse stock（結構化供應鏈庫存）不得被 erp 動作影響
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            tc.handle_indigo_warehouse_command("/warehouse stock NY276")
        (cmd,), _ = fn.call_args
        self.assertIn("query.stock_availability", cmd)


class YellowShortcutTests(unittest.TestCase):
    def test_purchase_stock_routes_to_dept_intent(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            out = tc.handle_yellow_procurement_command("/purchase stock G40")
        self.assertEqual(out, "ok")
        (cmd,), kwargs = fn.call_args
        self.assertIn("/dept yellow query.erp_stock", cmd)
        self.assertIn('"keyword": "G40"', cmd)
        self.assertIn('"lots": false', cmd)

    def test_purchase_stock_lots_suffix(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            tc.handle_yellow_procurement_command("/purchase stock G40 lots")
        (cmd,), _ = fn.call_args
        self.assertIn('"keyword": "G40"', cmd)
        self.assertIn('"lots": true', cmd)

    def test_purchase_stock_usage(self):
        from agent_core.agents import telegram_command as tc
        out = tc.handle_yellow_procurement_command("/purchase stock")
        self.assertIn("用法", out)


class ErpPoShortcutTests(unittest.TestCase):
    def test_purchase_erppo_routes_to_dept_intent(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            out = tc.handle_yellow_procurement_command("/purchase erppo G40")
        self.assertEqual(out, "ok")
        (cmd,), _ = fn.call_args
        self.assertIn("/dept yellow query.erp_po", cmd)
        self.assertIn('"keyword": "G40"', cmd)
        self.assertNotIn('"year"', cmd)

    def test_purchase_erppo_trailing_year(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            tc.handle_yellow_procurement_command("/purchase erppo 富泰 G40 2026")
        (cmd,), _ = fn.call_args
        self.assertIn('"keyword": "富泰 G40"', cmd)
        self.assertIn('"year": "2026"', cmd)

    def test_purchase_erppo_usage(self):
        from agent_core.agents import telegram_command as tc
        out = tc.handle_yellow_procurement_command("/purchase erppo")
        self.assertIn("用法", out)

    def test_warehouse_erppo_routes_to_indigo(self):
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            out = tc.handle_indigo_warehouse_command("/warehouse erppo JF0P26060007")
        self.assertEqual(out, "ok")
        (cmd,), _ = fn.call_args
        self.assertIn("/dept indigo query.erp_po", cmd)
        self.assertIn('"keyword": "JF0P26060007"', cmd)

    def test_legacy_purchase_po_action_unchanged(self):
        # 既有 /purchase po（供應鏈 JSON 存量的單筆 PO 查詢）不得被 erppo 影響
        from agent_core.agents import telegram_command as tc
        with mock.patch.object(tc, "handle_dept_command",
                               return_value="ok") as fn:
            tc.handle_yellow_procurement_command("/purchase po PO001")
        (cmd,), _ = fn.call_args
        self.assertIn("query.purchase_order", cmd)


if __name__ == "__main__":
    unittest.main()
