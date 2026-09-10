"""agent_core/finance_taiwan —— 台灣金流台帳，不碰真 Gmail/鏡像。

fixture body 仿實測形狀（2026-09-07 lake 普查）：一銀匯入款「欄名 值」同列、
媒體劃撥批次表逐列、水單金額在主旨。三個口徑坑都有測：①FU CHUN 匯入款＝
關係企業不算營收；②信保/退稅＝非營業另列；③解析不出金額只計筆數不猜。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import agent_core.erp_stock_query as esq
from agent_core import finance_taiwan as ftw

_FCB_FOREIGN_BODY = (
    "客戶名稱：佳桀有限公司 系統通知時間：2026/09/04 下午 01:05:19\n"
    "統一編號：279***72\n國外匯入匯款通知\n帳號 159****6090\n"
    "匯入款編號 S6EJ012245\n銀行通知日期 2026/09/04\n"
    "匯款生效日期 2026/09/08\n匯款幣別 USD\n匯款金額 47,439.50\n"
    "受款人名稱 JAI JYE CORPORATION\n匯款人名稱 EJENDALS SUOMI OY\n"
    "匯款行名稱 DABAFIHHXXX\n付款明細 JL170826F/\n說明 以上通知僅供參考\n")

_FCB_INTERCO_BODY = _FCB_FOREIGN_BODY.replace(
    "EJENDALS SUOMI OY", "FU CHUN CORPORATION")

_MEDIA_BODY = (
    "客戶名稱：佳Ｏ有限公司 系統通知時間：2026/9/1 上午 05:52:57\n"
    "媒體劃撥轉帳代繳扣帳結果通知\n"
    "帳號 交易說明 交易日期 交易金額 委託人帳號 備註\n"
    "159****1210 勞保費 2026/09/01 35,301.00 00009330221234 056601010T\n"
    "159****1210 勞退提繳 2026/09/01 18,810.00 00009310125477 056601010T\n"
    "159****1210 健保費 2026/09/01 42,000.00 00009310125478 056601010T\n"
    "159****1210 北市水費 2026/09/01 389.00 00009310125479 056601010T\n"
    "159****1210 台北瓦斯 2026/09/01 243.00 00009310125480 056601010T\n"
    "159****1210 台電電費 2026/09/01 3,253.00 00009310125481 056601010T\n"
    "159****1210 退營所稅 2026/09/01 27,011.00 00009310125482 056601010T\n")


def _msg(key, date, subject, body="", sender="x@y"):
    return {"key": key, "date": date, "subject": subject,
            "sender": sender, "body": body}


class TaiwanParserTests(unittest.TestCase):
    def test_fcb_foreign_customer_payment(self):
        rows = ftw.parse_fcb_foreign(
            _msg("m1", "2026-09-04", "第一銀行 國外匯入匯款通知",
                 _FCB_FOREIGN_BODY))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["category"], "客戶匯入款")
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["amount"], 47439.50)
        self.assertEqual(r["company"], "佳桀")       # 受款人 JAI JYE → 佳桀
        self.assertEqual(r["date"], "2026-09-08")    # 生效日不是通知日
        self.assertFalse(r["interco"])
        self.assertIn("EJENDALS", r["counterparty"])

    def test_fcb_foreign_interco_flagged(self):
        r = ftw.parse_fcb_foreign(
            _msg("m2", "2026-09-04", "s", _FCB_INTERCO_BODY))[0]
        self.assertTrue(r["interco"])                # 福群匯回來≠營收

    def test_media_batch_rows(self):
        rows = ftw.parse_media_batch(
            _msg("m3", "2026-09-01", "媒體劃撥轉帳代繳扣帳結果通知", _MEDIA_BODY))
        self.assertEqual(len(rows), 7)
        by_cat = {r["counterparty"]: r["category"] for r in rows}
        self.assertEqual(by_cat["北市水費"], "水費")
        self.assertEqual(by_cat["台北瓦斯"], "瓦斯費")
        self.assertEqual(by_cat["台電電費"], "電費")
        self.assertEqual(by_cat["退營所稅"], "其他匯入款")  # 退稅是入帳不是支出
        self.assertEqual(by_cat["勞保費"], "勞健保費")
        self.assertTrue(all(r["currency"] == "TWD" for r in rows))
        self.assertTrue(all(r["date"] == "2026-09-01" for r in rows))

    def test_purchase_remit_amount_from_subject(self):
        r = ftw.parse_purchase_remit(
            _msg("m4", "2026-02-12",
                 "LOT 228-2025 –- US$ 438.61, 已付款, 匯款水單如附檔"))[0]
        self.assertEqual(r["category"], "採購付款")
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["amount"], 438.61)

    def test_purchase_remit_without_amount_counts_only(self):
        r = ftw.parse_purchase_remit(
            _msg("m5", "2026-03-24", "LOT 208-2026 付款水單, ETD: 3/26"))[0]
        self.assertIsNone(r["amount"])               # 不猜數字

    def test_cht_amount_from_big5_attachment(self):
        att_html = "<html><body>總計：<b>5072</b></body></html>".encode("cp950")
        msg = _msg("m6", "2026-08-28", "電子發票通知函", "公版說明無金額")
        msg["attachments"] = [("CM37348491.htm", att_html)]
        r = ftw.parse_cht_invoice(msg)[0]
        self.assertEqual((r["category"], r["amount"]), ("電信費", 5072.0))

    def test_purchase_remit_vn_paid_is_interco(self):
        r = ftw.parse_purchase_remit(_msg(
            "m9", "2026-08-10",
            "RE: 富泰 (LOT 218-2026) For Decathlon, 福群付款, USD 1,000"))[0]
        self.assertTrue(r["interco"])                # 越南付的不算台灣費用
        self.assertEqual(r["category"], "採購付款(福群支付)")
        self.assertEqual(r["amount"], 1000.0)

    def test_forwarder_counts_without_amount(self):
        r = ftw.parse_forwarder(_msg("m10", "2026-08-25", "沛華/佳桀 月結申請"))[0]
        self.assertEqual(r["category"], "貨代/快遞費")
        self.assertIsNone(r["amount"])

    def test_forwarder_promo_quote_amount_not_extracted(self):
        # 促銷報價（實測「6月限時大特價 海運費USD 75/150」）—— 主旨非電子發票
        # 就算 body 滿是金額也不入帳，抽了會把報價當費用。
        r = ftw.parse_forwarder(_msg(
            "m11", "2025-05-29", "*6月限時大特價 *台中到胡志明整櫃 S/佳桀有限公司",
            "海運費USD 75/150 PER 20/40&HQ THC:NTD 6160/7700+BL: NTD 2250"))[0]
        self.assertIsNone(r["amount"])

    def test_forwarder_einvoice_extracts_amount_and_ref(self):
        msg = _msg("m12", "2026-08-19",
                   "*電子發票*  沛華-佳桀 -- EX-WORK , LOT .JA5618-2026",
                   "請查收本票費用 NTD11,675，明細如附件。\n寄件者: 舊信引文 NTD99,999")
        msg["attachments"] = [("HNHOC2608073電子發票.pdf", b"%PDF")]
        r = ftw.parse_forwarder(msg)[0]
        self.assertEqual(r["amount"], 11675.0)       # 引文裡的 99,999 不抽
        self.assertEqual(r["currency"], "TWD")
        self.assertEqual(r["ref"], "HNHOC2608073")

    def test_forwarder_invoice_pdf_text_total_is_max_amount(self):
        # 實測 pypdf 版面亂跳：總計 11,554 出現在「銷售額合計 11,004」之前 ——
        # 錨「總計」旁的數字會抓到 550（稅額）。不變量：總計=頁面最大金額。
        text = ("沛華實業股份有限公司\n發票號碼：\nDE07829689\n"
                "文件費 1 1,613 1,613\n入關費 1 2,097 2,097\n"
                "11,554\n銷　售　額　合　計 11,004\n總　　計\n550\n"
                "壹 萬 壹 仟 伍 佰 伍 拾 肆 元整")
        p = ftw._parse_forwarder_invoice_text(text)
        self.assertEqual(p["amount"], 11554.0)
        self.assertEqual(p["ref"], "DE07829689")
        # 非發票頁（無總計）不亂抽
        self.assertIsNone(ftw._parse_forwarder_invoice_text("報價單 NTD 9,999"))

    def test_freight_drive_filename_amount(self):
        hits = ftw._parse_freight_hits(
            "【1】 sim=0.81  新鮮度=0.92  應付款申請-沛華海運HKxVN(NTD11,554).pdf（2026-08-17）\n"
            "【2】 sim=0.81  20_3沛華海運費帳單BL.pdf（2020-09-07）\n    合計 24, 051 元\n"
            "【3】 sim=0.80  LOT.9沛華海運費用.pdf（2021-08-04）\n    無金額\n")
        self.assertEqual((hits[0]["amount"], hits[0]["src"]), (11554.0, "檔名"))
        self.assertEqual((hits[1]["amount"], hits[1]["src"]), (24051.0, "OCR"))
        self.assertIsNone(hits[2]["amount"])

    def test_taipower_text_fullwidth_and_fields(self):
        text = ("電子帳單\n１１４年０７月 繳費憑證(金融機構代繳用戶)\n"
                "佳桀有限公司\n00-31-5455-42-8\n單據號碼：E-M9114072401773\n"
                "114/07/23 ＊＊＊＊２９０３ 元\n用戶營利事業統一編號：\n27996872\n"
                "流動電費\n稅前應繳總金額\n營業稅\n繳費總金額\n"
                "2913.0 元\n-10.0 元\n2765.0 元\n138.0 元\n2,903 元\n")
        p = ftw._parse_taipower_text(text)
        self.assertEqual(p["amount"], 2903.0)
        self.assertEqual(p["date"], "2025-07-28")     # 民國 114 → 2025
        self.assertEqual(p["ref"], "E-M9114072401773")
        self.assertEqual(p["meter"], "00-31-5455-42-8")

    def test_taipower_without_pdf_is_skipped(self):
        msg = _msg("mt", "2026-09-07", "Fwd: 台電e-Bill")
        msg["attachments"] = []
        self.assertEqual(ftw.parse_taipower_bill(msg), [])

    def test_cub_statement_text_and_tax_hits(self):
        parsed = ftw._parse_cub_statement_text(
            "國泰世華銀行2026年6月簡易對帳單\n對帳單期間：2026/06/01~2026/06/30\n"
            "綜合活期存款 121035****61 11,464.00\n合計 11,464.00\n")
        self.assertEqual(parsed["amount"], 11464.0)
        self.assertEqual(parsed["date"], "2026-06-28")
        hits = ftw._parse_tax_hits(
            "【1】 sim=0.85  新鮮度=0.81  佳桀-401繳款書-11506.pdf（2026-07-15）\n"
            "    …應納稅額合計 項目 24, 051 …\n"
            "【2】 sim=0.83  佳紘-營所稅繳款書.pdf（2026-06-10）\n    無金額字樣\n")
        self.assertEqual(hits[0]["amount"], 24051.0)
        self.assertEqual(hits[0]["file"], "佳桀-401繳款書-11506.pdf")
        self.assertIsNone(hits[1]["amount"])         # 抽不出不猜

    def test_cht_and_fubon(self):
        r = ftw.parse_cht_invoice(
            _msg("m6", "2026-08-28", "電子發票通知函", "本期應繳金額：NT$ 1,234 元"))[0]
        self.assertEqual((r["category"], r["amount"]), ("電信費", 1234.0))
        own = ftw.parse_fubon_transfer(_msg(
            "m7", "2026-09-07", "臺幣轉帳交易「成功」通知",
            "轉帳金額 \n 臺幣 349,655 元 \n 轉入帳號 \n 012(台北富邦)-006401****4340"
            " \n 手續費 \n 臺幣 0 元 \n 暱稱/轉入戶名 \n Owner 陳Ｏ亨 \n"))[0]
        self.assertTrue(own["interco"])              # 自己帳戶＝內部調撥
        self.assertEqual(own["amount"], 349655.0)
        emp = ftw.parse_fubon_transfer(_msg(
            "m8", "2026-09-07", "臺幣轉帳交易「成功」通知",
            "轉帳金額 \n 臺幣 45,000 元 \n 暱稱/轉入戶名 \n 王Ｏ明 \n"))[0]
        self.assertFalse(emp["interco"])             # 員工＝薪資/付款轉出
        self.assertEqual(emp["category"], "富邦轉出(薪資/付款)")
        self.assertEqual(emp["amount"], 45000.0)


class TaiwanLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tw_ledger_test_")
        self.path = os.path.join(self.tmp, "ledger.json")
        p = mock.patch.object(ftw, "_LEDGER_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_sync_dedupes_and_counts(self):
        def fake_fetch(mailbox, query, *args, **kwargs):
            if "fx-desk" in query:
                return [_msg("msgid:a", "2026-09-04", "匯入", _FCB_FOREIGN_BODY)]
            if "媒體劃撥" in query:
                return [_msg("msgid:b", "2026-09-01", "代繳", _MEDIA_BODY)]
            return []

        with mock.patch.object(ftw, "_fetch_stream", side_effect=fake_fetch):
            out1 = ftw.sync_taiwan_ledger(30)
            self.assertIn("新增 1 封", out1)
            out2 = ftw.sync_taiwan_ledger(30)
            self.assertIn("新增 0 封", out2)        # 增量：同信不重複入帳
        rows = ftw._all_rows()
        self.assertEqual(len(rows), 8)              # 1 匯入 + 7 批次列

    def test_summary_separates_interco_and_no_amount(self):
        entries = {
            "k1": ftw.parse_fcb_foreign(_msg("k1", "2026-04-10", "s", _FCB_FOREIGN_BODY.replace("2026/09/08", "2026/04/10"))),
            "k2": ftw.parse_fcb_foreign(_msg("k2", "2026-04-11", "s", _FCB_INTERCO_BODY.replace("2026/09/08", "2026/04/11"))),
            "k3": ftw.parse_media_batch(_msg("k3", "2026-04-01", "s", _MEDIA_BODY.replace("2026/09/01", "2026/04/01"))),
            "k4": ftw.parse_purchase_remit(_msg("k4", "2026-04-20", "付款水單")),
        }
        ftw._save_ledger({"entries": entries})
        with mock.patch.object(ftw, "_usd_twd", return_value=32.0):
            out = ftw.taiwan_cash_summary(6)
        self.assertIn("2026-04", out)
        self.assertIn("客戶匯入款：47,440 USD", out)
        self.assertIn("客戶匯入款·關係企業", out)     # interco 另列
        self.assertIn("勞健保費", out)
        self.assertIn("解析不出金額", out)            # k4 無金額只計筆數
        self.assertIn("採購付款 1 筆", out)
        self.assertIn("已知缺口", out)                # 覆蓋警語必帶

    def test_summary_empty_ledger_guides_sync(self):
        self.assertIn("sync_taiwan_ledger", ftw.taiwan_cash_summary())

    def test_forwarder_cross_stream_family_dedup(self):
        # 同一張發票在會計串流與 owner 串流各入一筆 → 家族鍵只算一次
        bill = {"date": "2026-08-19", "company": "台灣", "category": "貨代/快遞費",
                "currency": "TWD", "amount": 11675.0, "counterparty": "",
                "ref": "HNHOC2608073", "interco": False, "subject": "s"}
        ftw._save_ledger({"entries": {
            "k1": [dict(bill, stream="pacificstar")],
            "k2": [dict(bill, stream="pacificstar_dylan")],
        }})
        self.assertEqual(len(ftw._all_rows()), 1)

    def test_taipower_forwarded_copies_dedup_by_ref(self):
        # 同一張憑證被轉寄兩次（不同 Message-ID）→ 彙總只算一次 2,903
        bill = {"date": "2025-07-28", "company": "台灣",
                "category": "電費憑證(參考)",
                "currency": "TWD", "amount": 2903.0, "counterparty": "台電",
                "ref": "E-M9114072401773", "interco": False, "subject": "s",
                "stream": "taipower"}
        ftw._save_ledger({"entries": {"k1": [dict(bill)], "k2": [dict(bill)]}})
        rows = ftw._all_rows()
        self.assertEqual(len(rows), 1)
        with mock.patch.object(ftw, "_usd_twd", return_value=32.0):
            out = ftw.taiwan_cash_summary(24)
        # 參考件用 💼、不當支出（錢走一銀代繳批次，那邊才是現金事實）
        self.assertIn("💼 電費憑證(參考)：2,903 TWD", out)

    def test_autosync_quiet_on_success_raises_on_fetch_failure(self):
        with mock.patch.object(ftw, "_fetch_stream", return_value=[]):
            self.assertEqual(ftw.taiwan_ledger_autosync(), "(無新發現)")
        with mock.patch.object(ftw, "_fetch_stream",
                               side_effect=OSError("boom")):
            with self.assertRaises(RuntimeError):
                ftw.taiwan_ledger_autosync()


class ConsolidatedViewTests(unittest.TestCase):
    """合併視圖：ERP fixture（同 test_finance_statements 形狀）＋台帳 fixture。"""

    @classmethod
    def setUpClass(cls):
        import duckdb
        cls.tmp = tempfile.mkdtemp(prefix="consol_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        con = duckdb.connect(cls.db)
        con.execute("CREATE TABLE GL00__GL_BOOK(ORG_ID VARCHAR, BOOKS_NO VARCHAR,"
                    " DESC_S VARCHAR, DESC_T VARCHAR, MONEY_UNIT VARCHAR)")
        con.execute("INSERT INTO GL00__GL_BOOK VALUES "
                    "('1','FC','fuchun2025','福群2025','VND')")
        con.execute("CREATE TABLE GL00__GL_ACCT_M(ORG_ID VARCHAR, ACCT_ID VARCHAR,"
                    " NAME_S VARCHAR, NAME_T VARCHAR, ACCT_TYPE VARCHAR)")
        con.executemany("INSERT INTO GL00__GL_ACCT_M VALUES ('1', ?, NULL, ?, ?)",
                        [("41010101", "外銷", "4"), ("51010101", "成本", "5")])
        con.execute("CREATE TABLE GL00__GL_VOUCH_M(ORG_ID VARCHAR, BOOKS_NO VARCHAR,"
                    " VOUCH_ID VARCHAR, PERIOD_ID VARCHAR, VCH_TYPE VARCHAR,"
                    " STATUS VARCHAR)")
        con.executemany(
            "INSERT INTO GL00__GL_VOUCH_M VALUES ('1','FC',?,?,?,'7')",
            [("P1", "202604", "1"), ("Z1", "202604", "99")])
        con.execute("CREATE TABLE GL00__GL_VOUCH_D(ORG_ID VARCHAR, BOOKS_NO VARCHAR,"
                    " VOUCH_ID VARCHAR, ACCT_ID VARCHAR, D_MONEY VARCHAR,"
                    " C_MONEY VARCHAR)")
        con.executemany(
            "INSERT INTO GL00__GL_VOUCH_D VALUES ('1','FC',?,?,?,?)",
            [("P1", "41010101", None, str(2000 * 1_000_000)),
             ("P1", "51010101", str(1200 * 1_000_000), None)])
        con.execute("CREATE TABLE GL00__GL_EXCHANGE(ORG_ID VARCHAR, BOOKS_NO VARCHAR,"
                    " PERIOD_ID VARCHAR, FORN_CURR VARCHAR, LAST_RATE VARCHAR)")
        con.execute("INSERT INTO GL00__GL_EXCHANGE VALUES "
                    "('1','FC','202604','USD','25000')")
        con.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        p1 = mock.patch.object(esq, "_db_path", return_value=self.db)
        p1.start()
        self.addCleanup(p1.stop)
        self.ledger = os.path.join(self.tmp, "ledger.json")
        p2 = mock.patch.object(ftw, "_LEDGER_PATH", self.ledger)
        p2.start()
        self.addCleanup(p2.stop)
        if os.path.exists(self.ledger):
            os.remove(self.ledger)

    def test_consolidated_view_side_by_side(self):
        entries = {
            "k1": ftw.parse_fcb_foreign(_msg(
                "k1", "2026-04-10", "s",
                _FCB_FOREIGN_BODY.replace("2026/09/08", "2026/04/10"))),
            "k2": ftw.parse_fcb_foreign(_msg(
                "k2", "2026-04-11", "s",
                _FCB_INTERCO_BODY.replace("2026/09/08", "2026/04/11"))),
        }
        with open(self.ledger, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False)
        with mock.patch.object(ftw, "_usd_twd", return_value=32.0):
            out = ftw.consolidated_pnl_view("2026-04")
        self.assertIn("台越合併視圖 2026-04", out)
        self.assertIn("福群2025", out)
        self.assertIn("47,440 USD", out)             # 台灣客戶匯入
        self.assertIn("關係企業內部金流 1 筆已排除", out)
        self.assertIn("不能直接相加", out)            # 口徑警語
        self.assertIn("已知缺口", out)

    def test_consolidated_view_without_ledger_guides_sync(self):
        out = ftw.consolidated_pnl_view("2026-04")
        self.assertIn("sync_taiwan_ledger", out)


class TaiwanExposureTests(unittest.TestCase):
    _TOOLS = ("sync_taiwan_ledger", "taiwan_cash_summary", "consolidated_pnl_view",
              "taiwan_ledger_autosync", "taiwan_tax_from_drive",
              "freight_bills_from_drive")

    def test_taiwan_tools_never_in_employee_whitelists(self):
        from agent_core import dept_tool_scope as scope
        for name in self._TOOLS:
            self.assertNotIn(name, getattr(scope, "_COMMON_TOOLS", ()))
            for color, tools in getattr(scope, "_HOME_TOOLS", {}).items():
                self.assertNotIn(
                    name, tools,
                    f"財務工具 {name} 不得進 {color} 員工白名單（敏感財務全貌）")

    def test_skill_tools_are_background_safe(self):
        import importlib
        mod = importlib.import_module("skills.finance_taiwan")
        names = {f.__name__ for f in mod.SKILL_TOOLS}
        self.assertEqual(names, set(self._TOOLS))
        for f in mod.SKILL_TOOLS:
            self.assertTrue(getattr(f, "background_safe", False))


if __name__ == "__main__":
    unittest.main()
