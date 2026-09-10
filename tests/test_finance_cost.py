"""agent_core/finance_cost —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿真實形狀的坑：①單位不同混比會出假降價 → 比價鎖 (料號,供應商,單位)；
②極端比率（>10 倍）是登打/單位異常不是價格；③基準只有 1 筆不進榜；④PO 表
沒幣別，靠標準價表 CURR_NO 反查、查無不硬換。PO 日期以 date.today() 相對值建。
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import agent_core.erp_stock_query as esq
from agent_core import finance_cost as fc

_M = 1_000_000


def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat() + " 00:00:00"


def _build_fixture_db(path: str) -> None:
    import duckdb
    con = duckdb.connect(path)
    # ── GL 面（expense_anomaly / 匯率 / 帳套）──
    con.execute(
        "CREATE TABLE GL00__GL_BOOK(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "DESC_S VARCHAR, DESC_T VARCHAR, MONEY_UNIT VARCHAR)")
    con.execute(
        "INSERT INTO GL00__GL_BOOK VALUES ('1', 'FC', 'fuchun2025', '福群2025', 'VND')")
    con.execute(
        "CREATE TABLE GL00__GL_ACCT_M(ORG_ID VARCHAR, ACCT_ID VARCHAR, "
        "NAME_S VARCHAR, NAME_T VARCHAR, ACCT_TYPE VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__GL_ACCT_M VALUES ('1', ?, NULL, ?, ?)",
        [
            ("5101", "銷貨成本", "5"), ("51010101", "銷貨成本-成品", "5"),
            ("6201", "管-薪資", "6"), ("62010101", "管-薪資費用", "6"),
            ("6202", "管-租金", "6"), ("62020101", "管-租金支出", "6"),
            ("6301", "管-顧問費", "6"), ("63010101", "管-顧問費用", "6"),
        ],
    )
    con.execute(
        "CREATE TABLE GL00__GL_VOUCH_M(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "VOUCH_ID VARCHAR, PERIOD_ID VARCHAR, VCH_TYPE VARCHAR, STATUS VARCHAR)")
    con.execute(
        "CREATE TABLE GL00__GL_VOUCH_D(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "VOUCH_ID VARCHAR, ACCT_ID VARCHAR, D_MONEY VARCHAR, C_MONEY VARCHAR)")
    # 歷史 4 個關帳月：6201=100M、6202=60M、5101=500M；本月 202604：
    # 6201=200M（異常）、5101=500M（正常）、6301=80M（新）、6202 歸零。
    periods = ["202512", "202601", "202602", "202603", "202604"]
    vm, vd = [], []
    for p in periods:
        vm.append((f"P{p}", p, "1", "7"))
        vm.append((f"Z{p}", p, "99", "7"))
        if p != "202604":
            vd += [(f"P{p}", "62010101", str(100 * _M), None),
                   (f"P{p}", "62020101", str(60 * _M), None),
                   (f"P{p}", "51010101", str(500 * _M), None)]
        else:
            vd += [(f"P{p}", "62010101", str(200 * _M), None),
                   (f"P{p}", "63010101", str(80 * _M), None),
                   (f"P{p}", "51010101", str(500 * _M), None)]
    con.executemany(
        "INSERT INTO GL00__GL_VOUCH_M VALUES ('1', 'FC', ?, ?, ?, ?)", vm)
    con.executemany(
        "INSERT INTO GL00__GL_VOUCH_D VALUES ('1', 'FC', ?, ?, ?, ?)", vd)
    con.execute(
        "CREATE TABLE GL00__GL_EXCHANGE(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "PERIOD_ID VARCHAR, FORN_CURR VARCHAR, LAST_RATE VARCHAR)")
    con.execute(
        "INSERT INTO GL00__GL_EXCHANGE VALUES ('1', 'FC', '202604', 'USD', '25000')")
    # ── PO 面 ──
    con.execute(
        "CREATE TABLE SC00__PO_ORDER_M(ORG_ID VARCHAR, ORDER_NO VARCHAR, "
        "VEND_NO VARCHAR, ORD_DATE VARCHAR, STATUS VARCHAR)")
    con.execute(
        "CREATE TABLE SC00__PO_ORDER_D(ORG_ID VARCHAR, ORDER_NO VARCHAR, "
        "ITEM_NO VARCHAR, PR_UNIT VARCHAR, ORD_QTY VARCHAR, PRICE VARCHAR)")
    po_m, po_d = [], []

    def buy(no, vend, days, item, unit, qty, price):
        po_m.append((no, vend, _d(days)))
        po_d.append((no, item, unit, str(qty), str(price)))

    # M1 VND 漲價：基準 2 筆 100,000 → 窗內 130,000×1,000 → 影響 30M
    buy("O1", "V1", -200, "M1", "M", 500, 100_000)
    buy("O2", "V1", -150, "M1", "M", 500, 100_000)
    buy("O3", "V1", -10, "M1", "M", 500, 130_000)
    buy("O4", "V1", -5, "M1", "M", 500, 130_000)
    # M2 疑似單位異常：基準 1,000,000×2 → 窗內 50（比率 0.00005）
    buy("O5", "V1", -180, "M2", "M", 10, 1_000_000)
    buy("O6", "V1", -160, "M2", "M", 10, 1_000_000)
    buy("O7", "V1", -8, "M2", "PR", 0, 50)  # 單位也變了 → 不同組
    buy("O8", "V1", -8, "M2", "M", 5, 50)   # 同單位組 → 比率異常區
    # M3 基準只有 1 筆：不進榜
    buy("O9", "V1", -100, "M3", "M", 10, 100_000)
    buy("O10", "V1", -9, "M3", "M", 1000, 200_000)
    # M4 USD 降價：10.0×2 → 8.0×200 → 影響 -2×200×25,000 = -10M
    buy("O11", "V2", -190, "M4", "M", 100, "10.0")
    buy("O12", "V2", -170, "M4", "M", 100, "10.0")
    buy("O13", "V2", -7, "M4", "M", 200, "8.0")
    # M5 幣別未知 +50%：other currency 區
    buy("O14", "V3", -190, "M5", "M", 100, 100)
    buy("O15", "V3", -170, "M5", "M", 100, 100)
    buy("O16", "V3", -6, "M5", "M", 100, 150)
    con.executemany("INSERT INTO SC00__PO_ORDER_M VALUES ('1', ?, ?, ?, '1')", po_m)
    con.executemany("INSERT INTO SC00__PO_ORDER_D VALUES ('1', ?, ?, ?, ?, ?)", po_d)
    # ── 標準價（M1 VND 舊價、M4 USD、M6 沒被買貴）──
    con.execute(
        "CREATE TABLE GL00__AP_STDPRICE_ITEM(AP_NO VARCHAR, ITEM_NO VARCHAR, "
        "AP_DATE VARCHAR, CURR_NO VARCHAR, PRICE VARCHAR, VEND_NO VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__AP_STDPRICE_ITEM VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("S1", "M1", "2025-10-01 00:00:00", "VND", "100000", "V1"),
            ("S2", "M4", "2026-01-01 00:00:00", "USD", "10.0", "V2"),
            ("S3", "M2", "2025-01-01 00:00:00", "VND", "1000000", "V1"),
        ],
    )
    con.execute(
        "CREATE TABLE SC00__PO_VENDER_M(ORG_ID VARCHAR, VEND_NO VARCHAR, "
        "SHORTNM_T VARCHAR, SHORTNM_E VARCHAR, FULLNM_T VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__PO_VENDER_M VALUES ('1', ?, ?, NULL, NULL)",
        [("V1", "龍毅欣"), ("V2", "富泰"), ("V3", "COATS")],
    )
    con.close()


class FinanceCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="finance_cost_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = os.path.join(self.tmp, "push_state.json")
        sp = mock.patch.object(fc, "_push_state_path", return_value=self.state)
        sp.start()
        self.addCleanup(sp.stop)
        if os.path.exists(self.state):
            os.remove(self.state)

    # ── 漲價偵測 ────────────────────────────────────────────────────

    def test_price_watch_rise_ranked_by_impact(self):
        out = fc.material_price_watch(3, 10)
        self.assertIn("M1（龍毅欣，VND）", out)
        self.assertIn("+30.0%", out)
        self.assertIn("影響 30.0 百萬", out)   # (130k-100k)×1,000

    def test_price_watch_absurd_ratio_goes_to_weird_not_board(self):
        out = fc.material_price_watch(3, 10)
        self.assertIn("疑似單位/登打異常", out)
        weird_idx = out.index("疑似單位")
        self.assertIn("M2", out[weird_idx:])
        # M2 不在漲/降榜（榜在異常區之前）
        self.assertNotIn("M2", out[:out.index("🔻")])

    def test_price_watch_single_baseline_excluded(self):
        out = fc.material_price_watch(3, 10)
        self.assertNotIn("M3", out)

    def test_price_watch_usd_down_and_unknown_currency(self):
        out = fc.material_price_watch(3, 10)
        self.assertIn("🔻 降價", out)
        self.assertIn("M4（富泰，USD）", out)
        self.assertIn("影響 -10.0 百萬", out)
        self.assertIn("其他幣別", out)
        self.assertIn("M5（COATS，?）", out)

    # ── 買貴清單 ────────────────────────────────────────────────────

    def test_overpriced_vs_standard(self):
        out = fc.overpriced_purchases(3, 10)
        self.assertIn("M1（龍毅欣）", out)
        self.assertIn("標準 100,000 [2025-10-01]", out)
        self.assertIn("+30.0%", out)
        # 多付 = 30,000×1,000 = 30M
        self.assertIn("多付 ≈ 30.0 百萬", out)
        self.assertIn("線索清單，不是結論", out)
        # M4 買得比標準便宜 → 不在清單
        self.assertNotIn("M4", out)

    # ── 費用異常 ────────────────────────────────────────────────────

    def test_expense_anomaly_flags_new_and_gone(self):
        out = fc.expense_anomaly("202604")
        self.assertIn("6201 管-薪資", out)
        self.assertIn("+100%", out)
        self.assertIn("高出 100.0", out)
        self.assertIn("🆕", out)
        self.assertIn("6301 管-顧問費", out)
        self.assertIn("歸零", out)
        self.assertIn("6202 管-租金", out)
        # 5101 穩定 → 不在異常清單（只出現在門檻說明之外）
        self.assertNotIn("5101 銷貨成本：", out)

    def test_expense_anomaly_rejects_unclosed_period(self):
        self.assertIn("不是已關帳月", fc.expense_anomaly("209901"))

    # ── 自動推播 ────────────────────────────────────────────────────

    def test_autopush_fires_once_per_closed_period(self):
        first = fc.cost_review_autopush()
        self.assertIn("2026-04 關帳，降成本月報", first)
        self.assertIn("費用/成本異常", first)
        self.assertIn("買貴清單", first)
        self.assertEqual(fc.cost_review_autopush(), "(無新發現)")
        with open(self.state, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["last_pushed_period"], "202604")

    def test_autopush_raises_when_mirror_missing(self):
        with mock.patch.object(esq, "_db_ready", return_value=False):
            with self.assertRaises(RuntimeError):
                fc.cost_review_autopush()


class CostExposureTests(unittest.TestCase):
    """財務全貌不進員工面 —— 同 Phase 1/2 守門慣例。"""

    _TOOLS = ("material_price_watch", "overpriced_purchases", "expense_anomaly",
              "cost_review_autopush")

    def test_cost_tools_never_in_employee_whitelists(self):
        from agent_core import dept_tool_scope as scope
        for name in self._TOOLS:
            self.assertNotIn(name, getattr(scope, "_COMMON_TOOLS", ()))
            for color, tools in getattr(scope, "_HOME_TOOLS", {}).items():
                self.assertNotIn(
                    name, tools,
                    f"財務工具 {name} 不得進 {color} 員工白名單（敏感財務全貌）")

    def test_stdprice_table_in_hot_tables(self):
        from agent_core import erp_mirror
        self.assertIn("GL00.AP_STDPRICE_ITEM", erp_mirror.HOT_TABLES)

    def test_skill_tools_are_background_safe(self):
        import importlib
        mod = importlib.import_module("skills.finance_cost")
        names = {f.__name__ for f in mod.SKILL_TOOLS}
        self.assertEqual(names, set(self._TOOLS))
        for f in mod.SKILL_TOOLS:
            self.assertTrue(getattr(f, "background_safe", False))


if __name__ == "__main__":
    unittest.main()
