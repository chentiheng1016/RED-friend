"""agent_core/erp_stock_query.erp_ap_lookup —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿 2026-07-27 三寶案的真實形狀：帳單（應付請款單）＝貨款＋不掛採購單的
運輸費；「帳單總金額」要以 NET_MONEY 為準——採購單金額加總會漏費用（當時答
111,502.20，正確 116,642.20，差額即運輸費 5,140）。
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
        "CREATE TABLE GL00__AP_APPLY_M(ORG_ID VARCHAR, APPLY_ID VARCHAR, "
        "APPLY_NO VARCHAR, APPLY_DATE VARCHAR, PAY_PERIOD VARCHAR, "
        "VEND_NO VARCHAR, MONEY_UNIT VARCHAR, NET_MONEY VARCHAR, "
        "STATUS VARCHAR, REMARK VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__AP_APPLY_M VALUES ('1', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # 三寶 2026：貨款 3 筆 + 運輸費 1 筆 = 116642.2（NET_MONEY 是文字欄）
            ("A1", "JFPA2605007", "2026-05-07 00:00:00", "202607", "H1L10001",
             "CNY", "116642.2", "2", "LOT 101-2026(三寶) 付款期限: 7/15"),
            # 同供應商 2025 舊單：date filter 要濾得掉
            ("A2", "JFPA2505001", "2025-04-01 00:00:00", "202506", "H1L10001",
             "CNY", "999.99", "99", ""),
            # 別家 USD 單：幣別分列合計
            ("A3", "JFPA2606001", "2026-06-01 00:00:00", "202608", "FTD10001",
             "USD", "1234.5", "99", "富泰​面料"),
        ],
    )
    con.execute(
        "CREATE TABLE GL00__AP_APPLY_D(ORG_ID VARCHAR, APPLY_ID VARCHAR, "
        "ITEM_ID VARCHAR, AP_MONEY VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__AP_APPLY_D VALUES ('1', ?, ?, ?)",
        [
            ("A1", "I1", "93802.5"),
            ("A1", "I2", "7106.4"),
            ("A1", "I3", "10593.3"),
            ("A1", "I4", "5140"),      # 運輸費（AP_DUE_D 的 PO_ORDERNO='0'）
        ],
    )
    con.execute(
        "CREATE TABLE GL00__AP_DUE_D(ORG_ID VARCHAR, AP_ID VARCHAR, "
        "PO_ORDERNO VARCHAR, ITEM_NAME VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__AP_DUE_D VALUES ('1', ?, ?, ?)",
        [
            ("I1", "J0M26030125", "電子LED-F3白圓燈"),
            ("I2", "J0M26030140", "電子LED-F3白圓燈"),
            ("I3", "J0M26030173", "電子LED-F3白圓燈"),
            ("I4", "0", "運輸費"),
        ],
    )
    con.execute(
        "CREATE TABLE SC00__PO_VENDER_M(ORG_ID VARCHAR, VEND_NO VARCHAR, "
        "SHORTNM_T VARCHAR, FULLNM_T VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__PO_VENDER_M VALUES ('1', ?, ?, ?)",
        [
            ("H1L10001", "泉州三寶電子", "泉州三寶電子有限公司"),
            ("FTD10001", "富泰", "富泰企業股份有限公司"),
        ],
    )
    con.close()


class ErpApLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_ap_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 三寶案主場景 ─────────────────────────────────────────────────

    def test_bill_total_includes_non_po_freight(self):
        out = esq.erp_ap_lookup("三寶", date_from="2026", date_to="2026")
        self.assertIn("JFPA2605007", out)
        self.assertIn("應付淨額(含稅) 116,642.20 CNY", out)   # 不是 111,502.20
        self.assertIn("應付淨額 116,642.20 CNY", out)         # 合計也對
        self.assertNotIn("111,502.20 CNY\n", out.split("貨款")[0])  # 主額不是漏費版

    def test_breakdown_shows_goods_and_fee_lines(self):
        out = esq.erp_ap_lookup("三寶", date_from="2026", date_to="2026")
        self.assertIn("貨款 3 筆合計 111,502.20 CNY", out)
        self.assertIn("＋費用（不掛採購單）：運輸費 5,140.00 CNY", out)
        self.assertIn("J0M26030125", out)

    def test_date_filter_excludes_old_bill(self):
        out = esq.erp_ap_lookup("三寶", date_from="2026", date_to="2026")
        self.assertNotIn("JFPA2505001", out)
        out_all = esq.erp_ap_lookup("三寶")
        self.assertIn("JFPA2505001", out_all)
        self.assertIn("結案", out_all)          # STATUS 99 → 結案
        self.assertIn("狀態2", out_all)         # 未確認碼值照碼顯示，不猜語意

    # ── 關鍵字/幣別/邊界 ─────────────────────────────────────────────

    def test_match_by_apply_no_and_vend_no(self):
        self.assertIn("JFPA2605007", esq.erp_ap_lookup("JFPA2605007"))
        self.assertIn("JFPA2605007", esq.erp_ap_lookup("H1L10001"))

    def test_currency_totals_are_split(self):
        out = esq.erp_ap_lookup("JFPA26")     # 命中 CNY＋USD 兩張
        self.assertIn("116,642.20 CNY", out)
        self.assertIn("1,234.50 USD", out)
        self.assertNotIn("​", out)            # zero-width 供應商名被 sanitize

    def test_short_or_empty_keyword_usage(self):
        self.assertIn("用法", esq.erp_ap_lookup("三"))
        self.assertIn("用法", esq.erp_ap_lookup(""))

    def test_no_match_hints_po_tool(self):
        out = esq.erp_ap_lookup("不存在的供應商")
        self.assertIn("查無", out)
        self.assertIn("query_erp_purchase_orders", out)
        self.assertIn("明晨刷新後才查得到", out)   # OZ20 案：查無要講快照時滯

    def test_bad_date(self):
        self.assertIn("日期格式錯誤", esq.erp_ap_lookup("三寶", date_from="26"))

    def test_missing_mirror_db(self):
        with mock.patch.object(esq, "_db_path",
                               return_value=os.path.join(self.tmp, "nope.duckdb")):
            self.assertIn("鏡像倉不存在", esq.erp_ap_lookup("三寶"))


class ErpApWiringTests(unittest.TestCase):
    """佈線煙霧測試：skill 工具面 / yellow 白名單 / intent 分派 / PO 工具交叉指引。"""

    def test_skill_tools_exports_query_erp_payables(self):
        from skills.erp_warehouse import SKILL_TOOLS
        self.assertIn("query_erp_payables", [t.__name__ for t in SKILL_TOOLS])

    def test_yellow_whitelist_contains_query_erp_payables(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        self.assertIn("query_erp_payables", allowed_tool_names_for_color("yellow"))

    def test_yellow_agent_dispatches_erp_ap_intent(self):
        from agent_core.agents.yellow_procurement.agent import (
            YellowProcurementAgent,
        )
        agent = YellowProcurementAgent()
        with mock.patch.object(esq, "_db_path", return_value="/nonexistent"):
            res = agent.handle_query(
                "query.erp_ap", {"keyword": "三寶", "year": "2026"}, trace_id="t")
        self.assertIn("text", res)
        self.assertIn("鏡像倉不存在", res["text"])

    def test_po_lookup_footer_cross_references_payables(self):
        # 採購單工具要明講「合計不含非採購單費用」並指路 query_erp_payables，
        # 否則 LLM 又會拿它回答帳單總額。
        import inspect
        src = inspect.getsource(esq.erp_po_lookup)
        self.assertIn("query_erp_payables", src)


if __name__ == "__main__":
    unittest.main()
