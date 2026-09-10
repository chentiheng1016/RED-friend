"""agent_core/finance_treasury —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿真實形狀的三個坑：①作廢傳票（STATUS='0'）要從水位排除；②帳戶互轉
（同傳票借貸皆現金科目）要軋掉，不然月現金流虛胖；③未付請示/付款排程有
2022-23 殭屍項（帳外付掉沒銷），要用日期窗濾。日期相關列全部以 date.today()
相對值建，測試不會過期。
"""
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import agent_core.erp_stock_query as esq
from agent_core import finance_treasury as ft

_M = 1_000_000


def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat() + " 00:00:00"


def _build_fixture_db(path: str) -> None:
    import duckdb
    con = duckdb.connect(path)
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
            ("11010101", "現金-VND", "1"), ("11020101", "銀行-農業", "1"),
            ("11030102", "應收帳款-外銷", "1"),
            ("21020101", "應付帳款", "2"), ("22020101", "應付薪資", "2"),
            ("21010101", "短期借款", "2"), ("23010103", "其他長期借款", "2"),
            ("41010101", "外銷-成品鞋", "4"), ("51010101", "銷貨成本", "5"),
        ],
    )
    con.execute(
        "CREATE TABLE GL00__GL_VOUCH_M(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "VOUCH_ID VARCHAR, PERIOD_ID VARCHAR, VCH_TYPE VARCHAR, STATUS VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__GL_VOUCH_M VALUES ('1', 'FC', ?, ?, '1', ?)",
        [
            ("P0", "202603", "7"),   # 期初建帳：AR/負債
            ("P1", "202603", "7"),   # 銀行流入 1,000M
            ("P2", "202603", "7"),   # 銀行流出 400M
            ("P3", "202604", "7"),   # 帳戶互轉 100M（要軋掉）
            ("P4", "202604", "7"),   # 付應付 200M
            ("V0", "202604", "0"),   # 作廢：借銀行 999M（必須排除）
            ("D1", "202605", "1"),   # 未過帳草稿：流入 50M（水位要含）
        ],
    )
    con.execute(
        "CREATE TABLE GL00__GL_VOUCH_D(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "VOUCH_ID VARCHAR, ACCT_ID VARCHAR, D_MONEY VARCHAR, C_MONEY VARCHAR)")
    lines = [
        # P0 期初：AR 1,080M / 負債 500+80+300+200
        ("P0", "11030102", str(1080 * _M), None),
        ("P0", "21020101", None, str(500 * _M)),
        ("P0", "22020101", None, str(80 * _M)),
        ("P0", "21010101", None, str(300 * _M)),
        ("P0", "23010103", None, str(200 * _M)),
        # P1 收款入銀行 1,000M（貸 AR）
        ("P1", "11020101", str(1000 * _M), None),
        ("P1", "11030102", None, str(1000 * _M)),
        # P2 付成本出銀行 400M
        ("P2", "51010101", str(400 * _M), None),
        ("P2", "11020101", None, str(400 * _M)),
        # P3 互轉：銀行→現金 100M（流入流出都要軋掉）
        ("P3", "11010101", str(100 * _M), None),
        ("P3", "11020101", None, str(100 * _M)),
        # P4 付應付 200M
        ("P4", "21020101", str(200 * _M), None),
        ("P4", "11020101", None, str(200 * _M)),
        # V0 作廢：借銀行 999M
        ("V0", "11020101", str(999 * _M), None),
        # D1 草稿：收款 50M
        ("D1", "11020101", str(50 * _M), None),
        ("D1", "11030102", None, str(50 * _M)),
    ]
    con.executemany(
        "INSERT INTO GL00__GL_VOUCH_D VALUES ('1', 'FC', ?, ?, ?, ?)", lines)
    con.execute(
        "CREATE TABLE GL00__GL_EXCHANGE(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "PERIOD_ID VARCHAR, FORN_CURR VARCHAR, LAST_RATE VARCHAR)")
    con.execute(
        "INSERT INTO GL00__GL_EXCHANGE VALUES ('1', 'FC', '202604', 'USD', '25000')")
    # 未付請示：活 VND 600M＋活 USD 1,000、殭屍 NTD、已付一張（不列）
    con.execute(
        "CREATE TABLE GL00__AP_APPLY_M(ORG_ID VARCHAR, APPLY_ID VARCHAR, "
        "APPLY_NO VARCHAR, APPLY_DATE VARCHAR, VEND_NO VARCHAR, GRT_DEPT VARCHAR, "
        "MONEY_UNIT VARCHAR, NET_MONEY VARCHAR, PAYMENT_ID VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__AP_APPLY_M VALUES ('1', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("A1", "JFPA2609001", _d(-10), "VF001", "VNP", "VND",
             str(600 * _M), None),
            ("A2", "JFPA2608001", _d(-30), "VF002", "TWP", "USD", "1000", None),
            ("A3", "JFPA2409001", _d(-400), "VF001", "VNS", "NTD", "5000", None),
            ("A4", "JFPA2609002", _d(-5), "VF002", "VNP", "VND",
             str(999 * _M), "PAY123"),
        ],
    )
    con.execute(
        "CREATE TABLE SC00__PO_VENDER_M(ORG_ID VARCHAR, VEND_NO VARCHAR, "
        "SHORTNM_T VARCHAR, SHORTNM_E VARCHAR, FULLNM_T VARCHAR)")
    con.executemany(
        "INSERT INTO SC00__PO_VENDER_M VALUES ('1', ?, ?, NULL, NULL)",
        [("VF001", "豐踩"), ("VF002", "三宏")],
    )
    # 付款排程：未來 3 天 VND 50M、逾期 10 天 NTD 3,000、殭屍 500 天前、已付
    con.execute(
        "CREATE TABLE GL00__AP_DUE_D(ORG_ID VARCHAR, AP_ID VARCHAR, "
        "VEND_NO VARCHAR, MONEY_UNIT VARCHAR, AP_MONEY VARCHAR, "
        "PL_PAY_DATE VARCHAR, PAY_NO VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__AP_DUE_D VALUES ('1', ?, ?, ?, ?, ?, ?)",
        [
            ("I1", "VF001", "VND", str(50 * _M), _d(3), None),
            ("I2", "VF002", "NTD", "3000", _d(-10), None),
            ("I3", "VF001", "VND", str(888 * _M), _d(-500), None),   # 殭屍
            ("I4", "VF001", "VND", str(777 * _M), _d(2), "PAYX"),    # 已付
        ],
    )
    con.close()


class FinanceTreasuryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="finance_treasury_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_fixture_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(esq, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 現金水位 ────────────────────────────────────────────────────

    def test_cash_position_math_and_void_exclusion(self):
        out = ft.cash_position()
        # 銀行 1000-400-100-200+50=350、現金 100 → 合計 450（作廢 999 沒混入）
        self.assertIn("可動用現金合計 450.0", out)
        self.assertIn("銀行-農業", out)
        self.assertIn("350.0", out)
        self.assertIn("100.0", out)
        # AR 1080-1000-50=30；負債 應付300+薪資80+短借300+長借200=880
        self.assertIn("應收帳款 30.0", out)
        self.assertIn("880.0", out)
        self.assertIn("作廢傳票已排除", out)

    def test_cash_position_usd_reference(self):
        out = ft.cash_position()
        self.assertIn("25,000", out)
        self.assertIn("現金 ≈ 18,000", out)   # 450M / 25,000 = 18,000 USD

    # ── 逐月現金流 ──────────────────────────────────────────────────

    def test_monthly_flows_net_internal_transfers(self):
        out = ft.cash_flow_monthly(6)
        # 202603：入 1,000 出 400；202604：互轉軋掉 → 入 0 出 200；202605：入 50
        self.assertIn("2026-03", out)
        self.assertIn("1,000.0", out)
        self.assertIn("-200.0", out)     # 202604 淨額（互轉沒軋掉會是 -100 或虛胖）
        self.assertIn("450.0", out)      # 期末現金
        flows = ft._monthly_cash_flows("FC", 6)
        self.assertEqual([(p, i, o) for p, i, o, _ in flows],
                         [("202603", 1000e6, 400e6), ("202604", 0.0, 200e6),
                          ("202605", 50e6, 0.0)])

    # ── 待付壓力 ────────────────────────────────────────────────────

    def test_payment_pressure_fresh_stale_split_and_gap(self):
        out = ft.payment_pressure()
        self.assertIn("VND 600,000,000（1 張）", out)
        self.assertIn("USD 1,000（1 張）", out)
        self.assertIn("豐踩", out)
        # 殭屍 NTD 5,000 在舊區、不進缺口
        self.assertIn("舊未付請示 1 張", out)
        self.assertIn("NTD 5,000", out)
        # 缺口：600M + 1,000×25,000=25M → 625M vs 現金 450M → 缺 175M
        self.assertIn("🚨 缺口 175.0", out)
        # 已付請示 A4 不出現
        self.assertNotIn("JFPA2609002", out)

    def test_payment_pressure_overdue_schedule(self):
        out = ft.payment_pressure()
        self.assertIn("已逾期未銷", out)
        self.assertIn("3,000 NTD", out)
        self.assertNotIn("888,000,000", out)   # 殭屍排程項不列
        self.assertNotIn("777,000,000", out)   # 已付排程項不列

    # ── 資金展望 ────────────────────────────────────────────────────

    def test_cash_outlook_weekly_buckets_and_verdict(self):
        out = ft.cash_outlook(4)
        self.assertIn("可動用現金 450.0", out)
        self.assertIn("50.0", out)             # 本週計畫付款 50M
        # 現金蓋得住排程（50M）但加請示 625M 後不足 → ⚠️ 分支
        self.assertIn("⚠️", out)
        self.assertIn("加計未付請示後差", out)
        self.assertIn("歷史算術，非預測", out)

    def test_guard_when_mirror_missing(self):
        with mock.patch.object(esq, "_db_ready", return_value=False):
            self.assertIn("鏡像尚未建立", ft.cash_position())
            self.assertIn("鏡像尚未建立", ft.cash_outlook())


class TreasuryExposureTests(unittest.TestCase):
    """財務全貌不進員工面 —— 同 Phase 1 守門慣例。"""

    _TOOLS = ("cash_position", "cash_flow_monthly", "payment_pressure",
              "cash_outlook")

    def test_treasury_tools_never_in_employee_whitelists(self):
        from agent_core import dept_tool_scope as scope
        for name in self._TOOLS:
            self.assertNotIn(name, getattr(scope, "_COMMON_TOOLS", ()))
            for color, tools in getattr(scope, "_HOME_TOOLS", {}).items():
                self.assertNotIn(
                    name, tools,
                    f"財務工具 {name} 不得進 {color} 員工白名單（敏感財務全貌）")

    def test_ap_due_tables_in_hot_tables(self):
        from agent_core import erp_mirror
        for t in ("GL00.AP_DUE_M", "GL00.AP_DUE_D"):
            self.assertIn(t, erp_mirror.HOT_TABLES)

    def test_skill_tools_are_background_safe(self):
        import importlib
        mod = importlib.import_module("skills.finance_treasury")
        names = {f.__name__ for f in mod.SKILL_TOOLS}
        self.assertEqual(names, set(self._TOOLS))
        for f in mod.SKILL_TOOLS:
            self.assertTrue(getattr(f, "background_safe", False))


if __name__ == "__main__":
    unittest.main()
