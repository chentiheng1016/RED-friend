"""agent_core/erp_stock_query.erp_demand_lookup —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿 2026-07-29 G407 案的真實形狀：訂單材料追蹤（SE_ITEMSCHE_M）同一筆
需求**同時**掛在裸碼（-G050）與 +EP 版次料號（-G050-005）兩列、需求值相同但
領料可能不同——彙總要去重取大、不能直接 SUM；每雙攤提＝需求÷訂單雙數
（12.04/221≈0.0545、無條件進位 0.06），≠BOM 標準用量。
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
        "CREATE TABLE SC00__SE_ITEMSCHE_M(ORG_ID VARCHAR, SE_ID VARCHAR, "
        "SE_SEQ VARCHAR, ITEM_NO VARCHAR, NEED_QTY VARCHAR, ISSUE_QTY VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__SE_ITEMSCHE_M VALUES ('1', ?, ?, ?, ?, ?)",
        [
            # 生效單 JFC25821（221 雙）：裸碼/版次雙記，需求同 12.04；
            # 領料裸碼列 12.04、版次列 11.91（裸碼列才含全部出庫，取大者）
            ("JFC25821", "1", "DFD00400D05700-G050", "12.04", "12.04"),
            ("JFC25821", "1", "DFD00400D05700-G050-005", "12.04", "11.91"),
            # 生效單 JFC25891（32 雙）：1.14/32≈0.0356 → 進位 0.04
            ("JFC25891", "1", "DFD00400D05700-G050", "1.14", "1.14"),
            ("JFC25891", "1", "DFD00400D05700-G050-005", "1.14", "1.12"),
            # 完工單（狀態 25）→ 不逐列、只計數
            ("JFC24432", "1", "DFD00400D05700-G050", "23.18", "23.18"),
            ("JFC24432", "1", "DFD00400D05700-G050-005", "23.18", "23.18"),
            # ERP 佔位垃圾列（無對應訂單）→ join 後自然消失
            ("0", "0", "DFD00400D05700-G050", "0", "0"),
            # 另一個不相干料號家族——關鍵字不該掃到
            ("JFC26999", "1", "XYZ-MAT-A010", "5.55", "0"),
        ],
    )
    con.execute(
        "CREATE TABLE SC00__SE_ORD_ITEM(ORG_ID VARCHAR, SE_ID VARCHAR, "
        "SE_SEQ VARCHAR, PROD_NO VARCHAR, CUST_LOT VARCHAR, SE_QTY VARCHAR, "
        "STATUS VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__SE_ORD_ITEM VALUES ('1', ?, ?, ?, ?, ?, ?)",
        [
            ("JFC25821", "1", "DJS336189-02GREEN", "8789351", "221", "7"),
            ("JFC25891", "1", "DJS336195-01GREEN", "8916447", "32", "7"),
            ("JFC24432", "1", "DJS336189-02GREEN", "8789351", "410", "25"),
            ("JFC26999", "1", "DJS999999-01RED", "1234567", "100", "7"),
        ],
    )
    # 訂單 BOM（每雙標準用量對照）：JFC25891 同料兩部位（A025＋A025.1）要相加，
    # 裸碼/版次雙記列以 DISTINCT 去重
    con.execute(
        "CREATE TABLE SC00__SE_BOM_PART(ORG_ID VARCHAR, SE_ID VARCHAR, "
        "SE_SEQ VARCHAR, ITEM_NO VARCHAR, PART_NO VARCHAR, UNIT_QTY VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__SE_BOM_PART VALUES ('1', ?, ?, ?, ?, ?)",
        [
            ("JFC25821", "1", "DFD00400D05700-G050", "A025", ".0565"),
            ("JFC25821", "1", "DFD00400D05700-G050-005", "A025", ".0565"),
            ("JFC25891", "1", "DFD00400D05700-G050", "A025", ".0161"),
            ("JFC25891", "1", "DFD00400D05700-G050", "A025.1", ".0171"),
            ("JFC25891", "1", "DFD00400D05700-G050-005", "A025", ".0161"),
            ("JFC25891", "1", "DFD00400D05700-G050-005", "A025.1", ".0171"),
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


class ErpDemandLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_demand_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 主場景：庫存編號 → 逐生效訂單需求/攤提（G407 案）─────────────

    def test_demand_by_stock_alias_lists_per_order_apportionment(self):
        out = esq.erp_demand_lookup("G407")
        self.assertIn("庫存編號", out)          # alias 註記
        self.assertIn("生效訂單 2 張", out)
        self.assertIn("STYLE# 8789351｜DJS336189-02GREEN", out)
        self.assertIn("JFC25821｜221雙｜需 12.04｜領 12.04", out)
        self.assertIn("攤 0.0545（進位 0.06）", out)
        self.assertIn("STYLE# 8916447｜DJS336195-01GREEN", out)
        self.assertIn("攤 0.0356（進位 0.04）", out)

    def test_dedup_base_and_edition_rows_not_double_counted(self):
        # 裸碼＋版次雙記：需求 12.04 不得翻倍成 24.08；領料取大者 12.04
        out = esq.erp_demand_lookup("G407")
        self.assertNotIn("24.08", out)
        self.assertIn("需求合計 12.04", out)
        self.assertNotIn("領 11.91", out)

    def test_order_bom_usage_comparison(self):
        # 訂單 BOM 每雙用量對照：單部位 .0565；兩部位 .0161+.0171=.0332
        out = esq.erp_demand_lookup("G407")
        self.assertIn("訂單BOM 0.0565/雙", out)
        self.assertIn("訂單BOM 0.0332/雙", out)

    def test_non_active_orders_counted_not_listed(self):
        # 完工單只計數不逐列（同 ERP 畫面預設只看 7-生效）
        out = esq.erp_demand_lookup("G407")
        self.assertNotIn("JFC24432", out)
        self.assertIn("完工 1 單", out)

    def test_mixed_master_units_flagged(self):
        # 主檔 M/Y 混用要明講、不硬猜單一單位
        out = esq.erp_demand_lookup("G407")
        self.assertIn("M/Y（主檔混用，以 ERP 畫面為準）", out)

    def test_demand_by_item_no_fragment(self):
        # 料號片段也走同一家族；不相干家族（XYZ-MAT）不得混入
        out = esq.erp_demand_lookup("DFD00400D05700")
        self.assertIn("JFC25821", out)
        self.assertNotIn("XYZ-MAT-A010", out)
        self.assertNotIn("JFC26999", out)

    def test_ceil2_erp_rounding_convention(self):
        # 無條件進位到小數 2 位；剛好 2 位不得再進位
        self.assertEqual(esq._ceil2(0.035625), 0.04)
        self.assertEqual(esq._ceil2(0.0545), 0.06)
        self.assertEqual(esq._ceil2(0.05), 0.05)

    # ── 邊界 ─────────────────────────────────────────────────────────

    def test_short_or_empty_keyword_usage(self):
        self.assertIn("用法", esq.erp_demand_lookup("G"))
        self.assertIn("用法", esq.erp_demand_lookup(""))

    def test_no_match(self):
        out = esq.erp_demand_lookup("完全不存在的料號")
        self.assertIn("查無", out)
        self.assertIn("query_erp_bom", out)
        self.assertIn("明晨刷新後才查得到", out)   # OZ20 案：查無要講快照時滯

    def test_missing_mirror_db(self):
        with mock.patch.object(esq, "_db_path",
                               return_value=os.path.join(self.tmp, "nope.duckdb")):
            self.assertIn("鏡像倉不存在", esq.erp_demand_lookup("G407"))


class ErpDemandWiringTests(unittest.TestCase):
    """佈線煙霧測試：skill 工具面 / yellow 白名單 / intent 分派都掛上了。"""

    def test_skill_tools_exports_query_erp_order_demand(self):
        from skills.erp_warehouse import SKILL_TOOLS
        self.assertIn("query_erp_order_demand", [t.__name__ for t in SKILL_TOOLS])

    def test_yellow_whitelist_contains_query_erp_order_demand(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        self.assertIn("query_erp_order_demand",
                      allowed_tool_names_for_color("yellow"))

    def test_yellow_agent_dispatches_erp_demand_intent(self):
        from agent_core.agents.yellow_procurement.agent import (
            YellowProcurementAgent,
        )
        agent = YellowProcurementAgent()
        with mock.patch.object(esq, "_db_path", return_value="/nonexistent"):
            res = agent.handle_query(
                "query.erp_demand", {"keyword": "G407"}, trace_id="t")
        self.assertIn("text", res)
        self.assertIn("鏡像倉不存在", res["text"])


if __name__ == "__main__":
    unittest.main()
