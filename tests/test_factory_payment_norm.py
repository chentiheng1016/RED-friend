"""factory_payment_norm leaf：幣別/對方正規化 + 檔名回填（純函式，免網路）。"""
import unittest

from agent_core import factory_payment_norm as pn


class NormalizeCurrencyTests(unittest.TestCase):
    def test_usd_variants(self):
        for s in ("USD", "US$", "us$", " usd ", "US", "美金", "美元", "USD$"):
            self.assertEqual(pn.normalize_currency(s), "USD", msg=s)

    def test_other_currencies(self):
        self.assertEqual(pn.normalize_currency("EUR"), "EUR")
        self.assertEqual(pn.normalize_currency("€"), "EUR")
        self.assertEqual(pn.normalize_currency("RMB"), "CNY")
        self.assertEqual(pn.normalize_currency("人民幣"), "CNY")
        self.assertEqual(pn.normalize_currency("NT$"), "TWD")
        self.assertEqual(pn.normalize_currency("NTD"), "TWD")
        self.assertEqual(pn.normalize_currency("JPY"), "JPY")
        self.assertEqual(pn.normalize_currency("¥"), "CNY")    # 本廠脈絡：¥→人民幣
        self.assertEqual(pn.normalize_currency("EURO"), "EUR")

    def test_empty_and_unknown(self):
        self.assertEqual(pn.normalize_currency(""), "")
        self.assertEqual(pn.normalize_currency(None), "")
        self.assertEqual(pn.normalize_currency("XYZ"), "XYZ")   # 不臆測、回清過的原值


class NormalizeCounterpartyTests(unittest.TestCase):
    def test_case_and_space_merge(self):
        a = pn.normalize_counterparty("Jai Jye Corporation")
        b = pn.normalize_counterparty("JAI JYE  CORPORATION")
        self.assertEqual(a, b)
        self.assertEqual(a, "JAI JYE CORPORATION")

    def test_comma_spacing_merge(self):
        a = pn.normalize_counterparty("DAESUNG CO.,LTD.")
        b = pn.normalize_counterparty("DAESUNG CO., LTD.")
        self.assertEqual(a, b)

    def test_strip_trailing_punct(self):
        self.assertEqual(pn.normalize_counterparty(" 福群， "), "福群")

    def test_alias_map(self):
        self.assertEqual(pn.normalize_counterparty("Jai Jye Corp"), "JAI JYE CORPORATION")

    def test_empty(self):
        self.assertEqual(pn.normalize_counterparty(""), "")
        self.assertEqual(pn.normalize_counterparty(None), "")


class ParseAmountFromTitleTests(unittest.TestCase):
    def test_basic(self):
        amt, cur = pn.parse_amount_from_title("佳桀-匯款-COATS USD626.82-20260616.pdf")
        self.assertAlmostEqual(amt, 626.82, places=2)
        self.assertEqual(cur, "USD")

    def test_thousands_comma_and_eur(self):
        amt, cur = pn.parse_amount_from_title(
            "佳桀-匯款-MASTROTTO EUR16,006.89-LOT 310-2026--20260327.pdf")
        self.assertAlmostEqual(amt, 16006.89, places=2)
        self.assertEqual(cur, "EUR")

    def test_total_preferred_over_parts(self):
        # 多金額：取 TOTAL 後的總額，而非分項
        amt, cur = pn.parse_amount_from_title(
            "匯款水單 WAN HUA-- TOTAL US$41230.88--LOT 201( US$9359.48)+LOT 210")
        self.assertAlmostEqual(amt, 41230.88, places=2)
        self.assertEqual(cur, "USD")

    def test_no_amount(self):
        self.assertEqual(pn.parse_amount_from_title("匯款水單20260605.pdf"), (None, ""))


class ParseDateFromTitleTests(unittest.TestCase):
    def test_yyyymmdd(self):
        self.assertEqual(pn.parse_date_from_title("...--20260327.pdf"), "2026-03-27")

    def test_no_false_match_on_lot(self):
        # 'LOT 201-2026' 不該被當成日期（無 MMDD）
        self.assertEqual(pn.parse_date_from_title("WAN HUA--LOT 201-2026"), "")

    def test_no_date(self):
        self.assertEqual(pn.parse_date_from_title("invoice.pdf"), "")


class ParseCounterpartyFromTitleTests(unittest.TestCase):
    def test_after_remittance_dash(self):
        self.assertEqual(
            pn.parse_counterparty_from_title("佳桀-匯款-MASTROTTO EUR16,006.89.pdf"),
            "MASTROTTO")

    def test_noise_words_rejected(self):
        # '匯款水單'、'匯款通知單' 的 水單/通知單 不是對方
        self.assertEqual(pn.parse_counterparty_from_title("匯款水單20260605.pdf"), "")
        self.assertEqual(pn.parse_counterparty_from_title("匯款通知單 20260408.pdf"), "")


class EnrichPaymentRecordTests(unittest.TestCase):
    def test_filename_fallback_sets_source(self):
        out = pn.enrich_payment_record({
            "title": "佳桀-匯款-COATS USD626.82-20260616.pdf",
            "amount": None, "currency": "", "doc_date": "", "counterparty": "",
        })
        self.assertAlmostEqual(out["amount"], 626.82, places=2)
        self.assertEqual(out["currency"], "USD")
        self.assertEqual(out["doc_date"], "2026-06-16")
        self.assertEqual(out["counterparty"], "COATS")
        self.assertEqual(out["amount_source"], "filename")

    def test_content_amount_wins_and_normalizes(self):
        out = pn.enrich_payment_record({
            "title": "x", "amount": "16,006.89", "currency": "US$",
            "counterparty": "Jai Jye Corporation",
        })
        self.assertAlmostEqual(out["amount"], 16006.89, places=2)   # 字串千分位逗號 → float
        self.assertEqual(out["currency"], "USD")                    # 正規化
        self.assertEqual(out["amount_source"], "content")
        self.assertEqual(out["counterparty_norm"], "JAI JYE CORPORATION")

    def test_no_amount_anywhere(self):
        out = pn.enrich_payment_record({"title": "匯款水單.pdf", "amount": None})
        self.assertIsNone(out["amount"])
        self.assertEqual(out["amount_source"], "")

    def test_does_not_mutate_input(self):
        rec = {"title": "佳桀-匯款-COATS USD626.82-20260616.pdf", "amount": None}
        pn.enrich_payment_record(rec)
        self.assertIsNone(rec["amount"])           # 原物件不動
        self.assertNotIn("amount_source", rec)


if __name__ == "__main__":
    unittest.main()
