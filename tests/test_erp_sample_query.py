"""agent_core/erp_stock_query.erp_sample_lookup —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿 2026-07-29 UserC PU468 案的真實形狀：PU468（庫存編號舊短碼）對應
PUB014T1718D05400000-A020，掛在 SR 樣品單（SP00 樣品開發域）的樣品 BOM 內——
freeform 因工具盲區幻覺「ERP 無獨立 SR 樣品單」。反查要能：短碼→料號→SR 單、
標出「最新版已無此料」的迭代汰換、樣品單方向要如實標快照落後主檔版次。
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
        "CREATE TABLE SP00__SP_PROD_SP(ORG_ID VARCHAR, PROD_NO VARCHAR, "
        "VER VARCHAR, SAMPLE_NO VARCHAR, SAMPLE_SEQ VARCHAR, STATUS VARCHAR, "
        "LAST_DATE VARCHAR)")
    con.executemany(
        "INSERT INTO SP00__SP_PROD_SP VALUES ('1', ?, ?, ?, '1', ?, ?)",
        [
            # UserC 案主角：SR2511001×JA1065，主檔已有 v3（樣品 BOM 快照只到 v2）
            ("JA1065 BK-RED", "1", "SR2511001", "7", "2026-05-02 10:00:00"),
            ("JA1065 BK-RED", "2", "SR2511001", "7", "2026-06-26 18:48:44"),
            ("JA1065 BK-RED", "3", "SR2511001", "7", "2026-07-16 15:55:47"),
            # 迭代汰換款：v1 用過 PU468 料、v2（最新）已拿掉
            ("FE9999 ICE", "1", "SR2209013", "7", "2023-09-01 09:00:00"),
            ("FE9999 ICE", "2", "SR2209013", "7", "2023-10-01 09:00:00"),
            # 無生效版樣品單（STATUS=1 新建）
            ("XW0001 GREY", "1", "SR2607001", "1", "2026-07-20 09:00:00"),
        ],
    )
    con.execute(
        "CREATE TABLE SP00__SP_PROD_SPBOM(ORG_ID VARCHAR, PROD_NO VARCHAR, "
        "VER VARCHAR, PART_NO VARCHAR, ITEM_NO VARCHAR, ITEM_NAME VARCHAR, "
        "UNIT_QTY VARCHAR, UNIT VARCHAR, VEND_NO VARCHAR)")
    con.executemany(
        "INSERT INTO SP00__SP_PROD_SPBOM VALUES ('1', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # JA1065 v1、v2 都含 PU468 料（快照最新 v2 仍在用）
            ("JA1065 BK-RED", "1", "A001", "PUB014T1718D05400000-A020",
             "PU超纖-13080819 Microfiber Suede", ".0968", "54/m2", "HUPU0001"),
            ("JA1065 BK-RED", "2", "A001", "PUB014T1718D05400000-A020",
             "PU超纖-13080819 Microfiber Suede", ".0968", "54/m2", "HUPU0001"),
            # v2 其他料（供 BOM 明細列出＋zero-width 淨化驗證）
            ("JA1065 BK-RED", "2", "BA02", "BXD00700T0000D044-A020",
             "JERSEY​ 300", ".0174", "44/m2", "TFD10001"),
            # FE9999：v1 用過該料、v2（快照最新）已拿掉——反查要標 ⚠️
            ("FE9999 ICE", "1", "A001", "PUB014T1718D05400000-A020",
             "PU超纖-13080819 Microfiber Suede", ".05", "54/m2", "HUPU0001"),
            ("FE9999 ICE", "2", "A001", "ACA007A00T1618000-A020",
             "MATRIZ SLIDE NERO", ".5612", "SF", "MAA10001"),
        ],
    )
    # 樣品域唯一客戶名來源（非全量：只補 SR2511001）
    con.execute(
        "CREATE TABLE SP00__SP_EXCEL_M(ORG_ID VARCHAR, SAMPLE_NO VARCHAR, "
        "CUSTNM VARCHAR, BRANDNM VARCHAR)")
    con.execute(
        "INSERT INTO SP00__SP_EXCEL_M VALUES ('1', 'SR2511001', 'JALAS', '')")
    con.execute(
        "CREATE TABLE SC00__PO_VENDER_M(ORG_ID VARCHAR, VEND_NO VARCHAR, "
        "SHORTNM_T VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__PO_VENDER_M VALUES ('1', ?, ?)",
        [("HUPU0001", "華峰"), ("TFD10001", "統發"), ("MAA10001", "MASTROTTO")],
    )
    con.execute(
        'CREATE TABLE "_item_alias"(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)')
    con.execute(
        'INSERT INTO "_item_alias" VALUES '
        "('PUB014T1718D05400000-A020', 'PU468')")
    # 料品主檔「來源樣品單號」對照（2026-07-30 GRANT 後入鏡像的治本表）：
    # PU468 料的來源=SR2511002（≠使用清單字典序首張 SR2511001——防冒充案例）；
    # ACA007 料無記載（此欄非必填，要照實講）。
    con.execute(
        "CREATE TABLE SP00__SP_ITEM_RDITEM(ORG_ID VARCHAR, SP_ITEMNO VARCHAR, "
        "RD_ITEMNO VARCHAR, SRC_SPNO VARCHAR)")
    con.execute(
        "INSERT INTO SP00__SP_ITEM_RDITEM VALUES "
        "('1', 'PUB014T1718D05400000-A020', 'PUB014T1718D05400000-A020', "
        "'SR2511002')")
    con.close()


class ErpSampleLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_sample_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 反查：庫存編號/料號 → SR 樣品單（UserC 案主場景）──────────────────

    def test_where_used_by_alias_lists_sr_orders(self):
        out = esq.erp_sample_lookup("PU468")
        self.assertIn("庫存編號（ERP 舊短碼）", out)
        self.assertIn("PUB014T1718D05400000-A020", out)
        self.assertIn("SR2511001", out)
        self.assertIn("JA1065 BK-RED", out)
        self.assertIn("客戶 JALAS", out)
        self.assertIn("華峰(HUPU0001)", out)
        self.assertIn("2 個版次含此料", out)
        self.assertIn("用量 0.0968 54/m2", out)

    def test_where_used_flags_dropped_in_latest_version(self):
        out = esq.erp_sample_lookup("PUB014T1718D05400000")
        # FE9999 只有 v1 用過、快照最新 v2 已拿掉 → 要列但明講已無
        self.assertIn("SR2209013", out)
        self.assertIn("鏡像最新版 v2 已無此料", out)
        # JA1065 最新 v2 仍在用 → 不得掛此警語（警語只出現一次）
        self.assertEqual(out.count("已無此料"), 1)

    def test_where_used_by_item_no_needs_no_alias(self):
        out = esq.erp_sample_lookup("ACA007A00T1618000")
        self.assertIn("SR2209013", out)
        self.assertIn("FE9999 ICE", out)
        self.assertNotIn("庫存編號（ERP 舊短碼）", out)

    def test_where_used_reads_source_sample_from_master(self):
        # 來源樣品單號照料品主檔對照表（SP_ITEM_RDITEM）念：PU468 的來源是
        # SR2511002——**不是**使用清單字典序首張的 SR2511001（防冒充案例）。
        out = esq.erp_sample_lookup("PU468")
        self.assertIn("📌 來源樣品單號（料品主檔）：SR2511002", out)
        self.assertIn("兩者語意不同", out)

    def test_where_used_source_absent_is_stated(self):
        # 對照表可讀、但此料無記載 → 照實講「未記載」，不外推。
        out = esq.erp_sample_lookup("ACA007A00T1618000")
        self.assertIn("料品主檔未記載", out)

    def test_where_used_source_table_missing_falls_back(self):
        # 對照表不可用（舊鏡像/授權被撤）→ 退回「開 ERP 畫面」提示。
        with mock.patch.object(esq, "_sample_sources", return_value=None):
            out = esq.erp_sample_lookup("PU468")
        self.assertIn("本鏡像讀不到", out)
        self.assertIn("ERP 開發料品畫面", out)

    # ── 樣品單號 / 鞋款 → 樣品 BOM ────────────────────────────────────────

    def test_sample_no_lists_bom_and_flags_snapshot_lag(self):
        out = esq.erp_sample_lookup("SR2511001")
        self.assertIn("共 3 版（最新 v3）", out)
        self.assertIn("客戶 JALAS", out)
        # 樣品 BOM 快照只到 v2 → 照實標主檔較新
        self.assertIn("樣品 BOM v2（主檔已有 v3，鏡像未含）", out)
        self.assertIn("PUB014T1718D05400000-A020", out)
        self.assertIn("統發(TFD10001)", out)
        # ERP 自由文字欄的 zero-width 要被洗掉
        self.assertNotIn("​", out)

    def test_prod_no_hits_sample_detail(self):
        out = esq.erp_sample_lookup("FE9999")
        self.assertIn("SR2209013", out)
        self.assertIn("共 2 版（最新 v2）", out)
        # v2 BOM 不含 PU468 料
        self.assertIn("ACA007A00T1618000-A020", out)

    def test_no_effective_version_is_labeled(self):
        out = esq.erp_sample_lookup("SR2607001")
        self.assertIn("（無生效版）", out)
        self.assertIn("（樣品 BOM 鏡像無此鞋款明細", out)

    # ── 查無 ────────────────────────────────────────────────────────────

    def test_not_found_mentions_snapshot_and_neighbors(self):
        out = esq.erp_sample_lookup("ZZZZ9999")
        self.assertIn("查無符合", out)
        self.assertIn("query_erp_purchase_orders", out)
        self.assertIn("鏡像時間", out)

    def test_short_keyword_usage(self):
        self.assertIn("用法", esq.erp_sample_lookup("P"))


if __name__ == "__main__":
    unittest.main()
