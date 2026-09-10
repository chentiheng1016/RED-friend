"""agent_core/finance_statements —— fixture DuckDB，不碰真鏡像/Gemini。

fixture 仿 FC 福群帳套的真實形狀：金額 VARCHAR、借貸各在 D_MONEY/C_MONEY
一側（另一側 NULL）、每月一張 VCH_TYPE='99' 的 Income summary 結轉傳票把
4~9 類歸零 —— 損益必須排除結轉、必須 COALESCE，兩個都做錯會直接算出 0。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import agent_core.erp_stock_query as esq
from agent_core import finance_statements as fin


def _build_fixture_db(path: str) -> None:
    import duckdb
    con = duckdb.connect(path)
    con.execute(
        "CREATE TABLE GL00__GL_BOOK(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "DESC_S VARCHAR, DESC_T VARCHAR, MONEY_UNIT VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__GL_BOOK VALUES ('1', ?, ?, ?, ?)",
        [
            ("FC", "fuchun2025", "福群2025", "VND"),
            # 佳桀帳套零傳票 —— 活帳套判定要選 FC 不是它
            ("CJ", None, "佳桀有限公司", "NTD"),
        ],
    )
    con.execute(
        "CREATE TABLE GL00__GL_ACCT_M(ORG_ID VARCHAR, ACCT_ID VARCHAR, "
        "NAME_S VARCHAR, NAME_T VARCHAR, ACCT_TYPE VARCHAR)")
    con.executemany(
        "INSERT INTO GL00__GL_ACCT_M VALUES ('1', ?, ?, ?, ?)",
        [
            ("4101", "Doanh thu", "銷貨收入", "4"),
            ("41010101", "Xuat khau", "外銷-成品鞋", "4"),
            ("5101", "Gia von", "銷貨成本", "5"),
            ("51010101", "Gia von", "銷貨成本-成品鞋", "5"),
            ("6201", "Luong", "管-薪資", "6"),
            ("62010101", "Luong", "管-薪資費用", "6"),
            ("3301", "Loi nhuan", "本期損益", "3"),
        ],
    )
    con.execute(
        "CREATE TABLE GL00__GL_VOUCH_M(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "VOUCH_ID VARCHAR, PERIOD_ID VARCHAR, VCH_TYPE VARCHAR, STATUS VARCHAR)")
    vouchers = [
        # 去年同月（YoY）：關帳
        ("V2504A", "202504", "1", "7"), ("V2504Z", "202504", "99", "7"),
        # 上月：關帳
        ("V2603A", "202603", "1", "7"), ("V2603Z", "202603", "99", "7"),
        # 本月：關帳（含一張作廢單 V2604V —— 金額必須被排除）
        ("V2604A", "202604", "1", "7"), ("V2604B", "202604", "1", "7"),
        ("V2604V", "202604", "1", "0"),
        ("V2604Z", "202604", "99", "7"),
        # 次月：未關帳（未過帳草稿、無結轉）
        ("V2605A", "202605", "1", "1"),
    ]
    con.executemany(
        "INSERT INTO GL00__GL_VOUCH_M VALUES ('1', 'FC', ?, ?, ?, ?)", vouchers)
    con.execute(
        "CREATE TABLE GL00__GL_VOUCH_D(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "VOUCH_ID VARCHAR, ACCT_ID VARCHAR, D_MONEY VARCHAR, C_MONEY VARCHAR)")
    m = 1_000_000  # 金額以「百萬」為顯示單位，fixture 用真實量級
    lines = [
        # 202504（YoY）：營收 1,000M、成本 600M、費用 500M → 淨損 -100M
        ("V2504A", "41010101", None, str(1000 * m)),
        ("V2504A", "51010101", str(600 * m), None),
        ("V2504A", "62010101", str(500 * m), None),
        ("V2504Z", "41010101", str(1000 * m), None),   # 結轉：必須被排除
        ("V2504Z", "51010101", None, str(600 * m)),
        ("V2504Z", "62010101", None, str(500 * m)),
        ("V2504Z", "3301", None, None),
        # 202603：營收 1,000M、成本 600M、費用 100M → 淨利 300M
        ("V2603A", "41010101", None, str(1000 * m)),
        ("V2603A", "51010101", str(600 * m), None),
        ("V2603A", "62010101", str(100 * m), None),
        ("V2603Z", "41010101", str(1000 * m), None),
        ("V2603Z", "51010101", None, str(600 * m)),
        ("V2603Z", "62010101", None, str(100 * m)),
        # 202604：營收 2,100-100（銷退折讓走借方）=2,000M、成本 1,200M、
        # 費用 300M → 毛利 800M、淨利 500M
        ("V2604A", "41010101", None, str(2100 * m)),
        ("V2604B", "41010101", str(100 * m), None),
        ("V2604A", "51010101", str(1200 * m), None),
        ("V2604A", "62010101", str(300 * m), None),
        ("V2604Z", "41010101", str(2000 * m), None),
        ("V2604Z", "51010101", None, str(1200 * m)),
        ("V2604Z", "62010101", None, str(300 * m)),
        # 202604 作廢單：碰 5 類成本 —— 沒排除的話成本會多 77M
        ("V2604V", "51010101", str(77 * m), None),
        # 202605（未關帳）：營收 999M
        ("V2605A", "41010101", None, str(999 * m)),
    ]
    con.executemany(
        "INSERT INTO GL00__GL_VOUCH_D VALUES ('1', 'FC', ?, ?, ?, ?)", lines)
    con.execute(
        "CREATE TABLE GL00__GL_EXCHANGE(ORG_ID VARCHAR, BOOKS_NO VARCHAR, "
        "PERIOD_ID VARCHAR, FORN_CURR VARCHAR, LAST_RATE VARCHAR)")
    con.execute(
        "INSERT INTO GL00__GL_EXCHANGE VALUES ('1', 'FC', '202604', 'USD', '25000')")
    con.close()


class FinanceStatementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="finance_stmt_test_")
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
        sp = mock.patch.object(fin, "_push_state_path", return_value=self.state)
        sp.start()
        self.addCleanup(sp.stop)
        if os.path.exists(self.state):
            os.remove(self.state)

    # ── 口徑：結轉排除 + COALESCE ─────────────────────────────────────

    def test_compute_pnl_excludes_income_summary(self):
        r = fin.compute_pnl("FC", "202604")
        self.assertEqual(r["revenue"], 2000e6)  # 結轉沒排掉會是 0
        self.assertEqual(r["cogs"], 1200e6)     # 作廢單沒排掉會是 1277M
        self.assertEqual(r["gross"], 800e6)
        self.assertEqual(r["opex"], 300e6)
        self.assertEqual(r["net"], 500e6)

    def test_void_voucher_excluded_from_pnl(self):
        # V2604V（STATUS='0'）借 5 類 77M —— 排除後成本維持 1,200M。
        r = fin.compute_pnl("FC", "202604")
        self.assertEqual(r["cogs"], 1200e6)

    def test_closed_periods_via_income_summary_voucher(self):
        self.assertEqual(fin.closed_periods("FC"), ["202504", "202603", "202604"])

    # ── income_statement ────────────────────────────────────────────

    def test_default_period_is_latest_closed_not_open_month(self):
        out = fin.income_statement()
        self.assertIn("2026-04 月損益", out)
        self.assertIn("2,000.0", out)          # 營收（銷退已淨）
        self.assertIn("毛利率 40.0%", out)
        self.assertIn("福群2025", out)
        self.assertNotIn("2026-05 月損益", out)

    def test_mom_yoy_and_usd_reference(self):
        out = fin.income_statement("2026-04")
        self.assertIn("上月+100.0%", out)        # 營收 1,000 → 2,000
        self.assertIn("去年同月（2025-04）", out)
        self.assertIn("匯率 25,000", out)
        # 202504 淨損 100M → YoY 淨利翻正
        self.assertIn("-100.0", out)

    def test_open_period_gets_draft_warning(self):
        out = fin.income_statement("2026-05")
        self.assertIn("尚未關帳", out)
        self.assertIn("999.0", out)

    def test_unknown_period_and_bad_format(self):
        self.assertIn("查無任何傳票", fin.income_statement("2026-12"))
        self.assertIn("格式看不懂", fin.income_statement("abc"))

    def test_open_period_note_listed_on_default_report(self):
        out = fin.income_statement()
        self.assertIn("尚未關帳：2026-05", out)

    # ── profit_trend / expense_breakdown ────────────────────────────

    def test_profit_trend_lists_only_closed_periods(self):
        out = fin.profit_trend(12)
        self.assertIn("2025-04", out)
        self.assertIn("2026-03", out)
        self.assertIn("2026-04", out)
        self.assertNotIn("2026-05", out)

    def test_expense_breakdown_groups_and_share(self):
        out = fin.expense_breakdown("2026-04")
        self.assertIn("5101 銷貨成本", out)
        self.assertIn("6201 管-薪資", out)
        self.assertIn("100.0%", out)            # 兩區各只有一個群組
        self.assertIn("上月+200.0%", out)        # 費用 100 → 300

    # ── 自動推播狀態機 ───────────────────────────────────────────────

    def test_autopush_fires_once_per_closed_period(self):
        first = fin.income_statement_autopush()
        self.assertIn("2026-04 已關帳", first)
        self.assertIn("月損益", first)
        self.assertEqual(fin.income_statement_autopush(), "(無新發現)")
        with open(self.state, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["last_pushed_period"], "202604")

    def test_autopush_raises_when_mirror_missing(self):
        with mock.patch.object(esq, "_db_ready", return_value=False):
            with self.assertRaises(RuntimeError):
                fin.income_statement_autopush()

    def test_guard_message_when_mirror_missing(self):
        with mock.patch.object(esq, "_db_ready", return_value=False):
            self.assertIn("鏡像尚未建立", fin.income_statement())


class FinanceToolExposureTests(unittest.TestCase):
    """財務全貌不進員工面 —— 同 read_dept_email_timeline 的守門慣例。"""

    _TOOLS = ("income_statement", "profit_trend", "expense_breakdown",
              "income_statement_autopush")

    def test_finance_tools_never_in_employee_whitelists(self):
        from agent_core import dept_tool_scope as scope
        for name in self._TOOLS:
            self.assertNotIn(name, getattr(scope, "_COMMON_TOOLS", ()))
            for color, tools in getattr(scope, "_HOME_TOOLS", {}).items():
                self.assertNotIn(
                    name, tools,
                    f"財務工具 {name} 不得進 {color} 員工白名單（敏感財務全貌）")

    def test_gl_tables_in_hot_tables(self):
        from agent_core import erp_mirror
        for t in ("GL00.GL_VOUCH_M", "GL00.GL_VOUCH_D", "GL00.GL_ACCT_M",
                  "GL00.GL_BOOK", "GL00.GL_EXCHANGE", "GL00.GL_BALANCES_M",
                  "GL00.GL_ACCT_TYPE"):
            self.assertIn(t, erp_mirror.HOT_TABLES)

    def test_skill_tools_are_background_safe(self):
        import importlib
        mod = importlib.import_module("skills.finance_statements")
        names = {f.__name__ for f in mod.SKILL_TOOLS}
        self.assertEqual(names, set(self._TOOLS))
        for f in mod.SKILL_TOOLS:
            self.assertTrue(getattr(f, "background_safe", False))


if __name__ == "__main__":
    unittest.main()
