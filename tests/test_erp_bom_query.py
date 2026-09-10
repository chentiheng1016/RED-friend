"""agent_core/erp_stock_query.erp_bom_lookup —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿 2026-07-27 UserA 案的真實形狀：DFDT7100145000-E040 是**顏色級**用料
（DJS336195 只有 -01GY.BU 在用；GREEN 用 G050-006），且 GREEN 的**舊版** BOM
曾用過 E040（生效版已拿掉）——反查必須只認生效版、並列出同型體不用此料的顏色。
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
        "CREATE TABLE SC00__RD_BOM_M(ORG_ID VARCHAR, PROD_NO VARCHAR, "
        "BOM_TYPE VARCHAR, BOM_VER VARCHAR, STATUS VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__RD_BOM_M VALUES ('1', ?, ?, ?, ?)",
        [
            ("DJS336189-02BEIGE", "A", "1", "6"),   # 舊版（已被 v2 取代）
            ("DJS336189-02BEIGE", "A", "2", "7"),
            ("DJS336195-01GY.BU", "A", "3", "7"),
            ("DJS336195 GREEN", "A", "13", "6"),    # 舊版曾用 E040
            ("DJS336195 GREEN", "A", "14", "7"),    # 生效版不用 E040
            ("DJS336195-01GREEN", "A", "4", "7"),
            ("DJS336195 GY.GN", "A", "2", "6"),     # 整組無生效版 → 退回最大版
        ],
    )
    con.execute(
        "CREATE TABLE SC00__RD_BOM_ITEM(ORG_ID VARCHAR, PROD_NO VARCHAR, "
        "BOM_TYPE VARCHAR, BOM_VER VARCHAR, PART_NO VARCHAR, ITEM_NO VARCHAR, "
        "UNIT_QTY VARCHAR, BOM_UNIT VARCHAR, VEND_NO VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__RD_BOM_ITEM VALUES ('1', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # 生效版在用 E040 的三個顏色
            ("DJS336189-02BEIGE", "A", "2", "A016",
             "DFDT7100145000-E040", ".0449", "M", "FTD10001"),
            ("DJS336189-02BEIGE", "A", "1", "A016",     # 舊版列（不該重複出現）
             "DFDT7100145000-E040", ".0449", "M", "FTD10001"),
            ("DJS336195-01GY.BU", "A", "3", "A016",
             "DFDT7100145000-E040", ".0241", "M", "FTD10001"),
            # GREEN 舊版 v13 用過 E040 —— 生效版 v14 已拿掉，反查不得列入
            ("DJS336195 GREEN", "A", "13", "A016",
             "DFDT7100145000-E040", ".0449", "M", "FTD10001"),
            ("DJS336195 GREEN", "A", "14", "A004",
             "DFDT7100145000-G050-006", ".0448", "M", "FDSM0001"),
            ("DJS336195-01GREEN", "A", "4", "A004",
             "DFDT7100145000-G050-006", ".0448", "M", "FDSM0001"),
            # 無生效版的顏色 → 退回最大版（v2）
            ("DJS336195 GY.GN", "A", "2", "A016",
             "DFDT7100145000-ZE060-001", ".0300", "M", "TED10001"),
            # G407 家族（2026-07-29 UserA 案）：生效版 BOM 只掛 +EP 版次料號，
            # 查裸短碼 G407 要能靠對照家族帶出
            ("DJS336189-02BEIGE", "A", "2", "A025",
             "DFD00400D05700-G050-005", ".0565", "M", "FDSM0001"),
        ],
    )
    con.execute(
        "CREATE TABLE SC00__SE_ORD_ITEM(ORG_ID VARCHAR, PROD_NO VARCHAR, "
        "CUST_LOT VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__SE_ORD_ITEM VALUES ('1', ?, ?)",
        [
            ("DJS336189-02BEIGE", "8668405"),
            ("DJS336195-01GY.BU", "8916446"),
            ("DJS336195 GREEN", "8916447"),
            ("DJS336195-01GREEN", "8916447"),
            # GY.GN 無客戶訂單 → 款號顯示 —
        ],
    )
    con.execute(
        "CREATE TABLE SY00__CD_CODE(ORG_ID VARCHAR, RULE_NO VARCHAR, "
        "CODE_NO VARCHAR, NAME_T VARCHAR)")
    con.execute(
        "INSERT INTO SY00__CD_CODE VALUES ('1', '1301', 'A', '面部材料')")
    con.execute(
        "CREATE TABLE SC00__PO_VENDER_M(ORG_ID VARCHAR, VEND_NO VARCHAR, "
        "SHORTNM_T VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__PO_VENDER_M VALUES ('1', ?, ?)",
        [
            ("FTD10001", "富泰"),
            ("FDSM0001", "福得"),
            # 供應商名夾 zero-width 驗 sanitize
            ("TED10001", "TESSI​TURA"),
        ],
    )
    # 庫存編號(舊碼)對照——仿 2026-07-28 UserA SF24 案：短碼一字之差=不同料，
    # SF24 只能命中自己、不得外推 SF24.5。
    con.execute(
        'CREATE TABLE "_item_alias"(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)')
    con.executemany(
        'INSERT INTO "_item_alias" VALUES (?, ?)',
        [
            ("DFDT7100145000-E040", "SF24"),
            ("DFDT7100145000-ZE060-001", "SF24.5"),
            # G407 家族：BOM 只掛 +EP 版次料號，裸短碼要靠家族展開
            ("DFD00400D05700-G050", "G407"),
            ("DFD00400D05700-G050-005", "G407+EP5"),
        ],
    )
    con.close()


class ErpBomLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_bom_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 反查：料號 → 使用款（UserA 案主場景）──────────────────────────

    def test_where_used_lists_only_effective_version_colorways(self):
        out = esq.erp_bom_lookup("DFDT7100145000-E040")
        self.assertIn("DJS336189-02BEIGE", out)
        self.assertIn("8668405", out)
        self.assertIn("DJS336195-01GY.BU", out)
        self.assertIn("8916446", out)
        # GREEN 只有舊版 v13 用過 → 不得列為使用款（只能出現在 ✗ 區）
        use_part = out.split("同型體")[0]
        self.assertNotIn("DJS336195 GREEN", use_part)
        self.assertNotIn("DJS336195-01GREEN", use_part)

    def test_where_used_sibling_section_blocks_extrapolation(self):
        out = esq.erp_bom_lookup("DFDT7100145000-E040")
        self.assertIn("同型體「不用」此料的顏色", out)
        self.assertIn("✗ DJS336195 GREEN｜客戶款號 8916447", out)
        self.assertIn("✗ DJS336195-01GREEN｜客戶款號 8916447", out)
        # GY.GN 無訂單 → 只列型體、無款號段
        self.assertIn("✗ DJS336195 GY.GN", out)

    def test_where_used_shows_usage_vendor_and_version(self):
        out = esq.erp_bom_lookup("DFDT7100145000-E040")
        self.assertIn("用量 0.0241 M", out)      # 4 位小數不得截成 0.02
        self.assertIn("富泰(FTD10001)", out)
        self.assertIn("BOM v3", out)
        self.assertIn("面部材料", out)
        # 同料號同款不因舊版列重複出現
        self.assertEqual(out.count("▪ DJS336189-02BEIGE"), 1)

    # ── 用料：款號/型體 → BOM 明細 ────────────────────────────────────

    def test_materials_by_customer_style(self):
        out = esq.erp_bom_lookup("8916447")
        self.assertIn("DJS336195 GREEN", out)
        self.assertIn("DFDT7100145000-G050-006", out)
        self.assertIn("福得(FDSM0001)", out)
        # 生效版 v14 不含 E040（v13 是舊版）
        self.assertNotIn("DFDT7100145000-E040", out)
        self.assertIn("v14", out)

    def test_materials_by_model_includes_no_effective_fallback(self):
        out = esq.erp_bom_lookup("DJS336195")
        # 無生效版的 GY.GN 退回最大版 v2，材料照列且 zero-width 被洗掉
        self.assertIn("DJS336195 GY.GN", out)
        self.assertIn("DFDT7100145000-ZE060-001", out)
        self.assertIn("TESSITURA", out)
        self.assertNotIn("​", out)

    def test_materials_model_query_also_reports_where_used_footer(self):
        out = esq.erp_bom_lookup("8916446")
        self.assertIn("DJS336195-01GY.BU", out)
        self.assertIn("DFDT7100145000-E040", out)
        self.assertIn("未經 AI 生成", out)

    # ── 庫存編號(舊碼)反查（2026-07-28 UserA SF24 案）────────────────

    def test_where_used_by_stock_alias_resolves_item(self):
        out = esq.erp_bom_lookup("SF24")
        # 對照註記 + 解出的料號 + 使用款（與直接查料號同結果）
        self.assertIn("庫存編號", out)
        self.assertIn("DFDT7100145000-E040", out)
        self.assertIn("DJS336189-02BEIGE", out)
        self.assertIn("DJS336195-01GY.BU", out)

    def test_stock_alias_exact_match_no_extrapolation(self):
        # SF24 不得掃到 SF24.5 的料（ZE060-001 屬 SF24.5）
        out = esq.erp_bom_lookup("SF24")
        self.assertNotIn("DFDT7100145000-ZE060-001", out)
        # 反向：SF24.5 解到自己的料
        out2 = esq.erp_bom_lookup("SF24.5")
        self.assertIn("DFDT7100145000-ZE060-001", out2)
        self.assertNotIn("DJS336189-02BEIGE", out2)

    def test_where_used_alias_plus_edition_family(self):
        # 2026-07-29 UserA G407 案：生效版 BOM 只掛 +EP 版次料號
        # （-G050-005＝G407+EP5），查裸短碼 G407 曾回「查無」→ freeform 退回
        # RAG 讀 Drive 成本表、給出與 ERP 不符的單一用量。家族展開後要照
        # 生效版 BOM 念出各型體×顏色各自的用量。
        out = esq.erp_bom_lookup("G407")
        self.assertIn("DFD00400D05700-G050-005", out)
        self.assertIn("（庫存編號 G407+EP5）", out)   # 料號行標註版次短碼
        self.assertIn("DJS336189-02BEIGE", out)
        self.assertIn("用量 0.0565 M", out)
        # 對照註記逐料標示版次短碼
        self.assertIn("DFD00400D05700-G050-005(G407+EP5)", out)

    def test_alias_note_in_no_match_message(self):
        # 對照解出料號、但生效版 BOM 真的沒有 → 查無訊息仍附對照註記，
        # 讓 LLM 不會憑空說「查無此庫存編號」。
        import duckdb
        con = duckdb.connect(self.db)
        con.execute("INSERT INTO \"_item_alias\" VALUES "
                    "('ZZZ-NOT-IN-BOM-A010', 'CL99')")
        con.close()

        def _cleanup():
            c = duckdb.connect(self.db)
            c.execute("DELETE FROM \"_item_alias\" WHERE O_ITEMNO = 'CL99'")
            c.close()
        self.addCleanup(_cleanup)
        out = esq.erp_bom_lookup("CL99")
        self.assertIn("庫存編號", out)
        self.assertIn("ZZZ-NOT-IN-BOM-A010", out)
        self.assertIn("查無", out)

    # ── 邊界 ─────────────────────────────────────────────────────────

    def test_short_or_empty_keyword_usage(self):
        self.assertIn("用法", esq.erp_bom_lookup("G"))
        self.assertIn("用法", esq.erp_bom_lookup(""))

    def test_no_match(self):
        out = esq.erp_bom_lookup("不存在的料號")
        self.assertIn("查無", out)
        self.assertIn("query_erp_stock", out)
        self.assertIn("明晨刷新後才查得到", out)   # OZ20 案：查無要講快照時滯

    def test_missing_mirror_db(self):
        with mock.patch.object(esq, "_db_path",
                               return_value=os.path.join(self.tmp, "nope.duckdb")):
            self.assertIn("鏡像倉不存在", esq.erp_bom_lookup("DFDT7100145000"))


class ErpBomWiringTests(unittest.TestCase):
    """佈線煙霧測試：skill 工具面 / yellow 白名單 / intent 分派都掛上了。"""

    def test_skill_tools_exports_query_erp_bom(self):
        from skills.erp_warehouse import SKILL_TOOLS
        self.assertIn("query_erp_bom", [t.__name__ for t in SKILL_TOOLS])

    def test_yellow_whitelist_contains_query_erp_bom(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        self.assertIn("query_erp_bom", allowed_tool_names_for_color("yellow"))

    def test_yellow_agent_dispatches_erp_bom_intent(self):
        from agent_core.agents.yellow_procurement.agent import (
            YellowProcurementAgent,
        )
        agent = YellowProcurementAgent()
        with mock.patch.object(esq, "_db_path", return_value="/nonexistent"):
            res = agent.handle_query(
                "query.erp_bom", {"keyword": "DFDT7100145000"}, trace_id="t")
        self.assertIn("text", res)
        self.assertIn("鏡像倉不存在", res["text"])


if __name__ == "__main__":
    unittest.main()
