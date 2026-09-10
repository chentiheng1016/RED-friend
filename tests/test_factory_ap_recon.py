"""AP 對帳引擎（payable↔remittance 金額錨定配對 + 帳齡）測試（合成、決定性）。"""
import unittest

from agent_core.factory_ap_recon import reconcile_payments


def _pay(amount, cur="USD", date="2026-06-01", cp="統發興業有限公司"):
    return {"doc_type": "payable", "amount": amount, "currency": cur,
            "doc_date": date, "counterparty": cp, "counterparty_norm": cp.upper()}


def _rem(amount, cur="USD", date="2026-06-10", cp="統發"):
    return {"doc_type": "remittance", "amount": amount, "currency": cur,
            "doc_date": date, "counterparty": cp, "counterparty_norm": cp.upper()}


class ReconcileTests(unittest.TestCase):
    def _sup(self, res, cp):
        return next((s for s in res["suppliers"] if s["counterparty"] == cp), None)

    def test_matched_by_amount_marks_paid(self):
        # 應付 4959.66 + 同額匯款（不同名）→ 配對成功、該供應商無未付
        rows = [_pay(4959.66, cp="南亞塑膠工業(南通)有限公司"),
                _rem(4959.66, cp="NAN YA PLASTICS")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables_matched_paid"], 1)
        s = self._sup(res, "南亞塑膠工業(南通)有限公司")
        self.assertEqual(s["open_balance"], 0.0)
        self.assertEqual(s["n_paid"], 1)

    def test_unmatched_payable_is_open_with_aging(self):
        # 應付無對應匯款 → open；日期 100 天前 → 90+ 桶
        rows = [_pay(1000.0, date="2026-03-17", cp="華峰")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        s = self._sup(res, "華峰")
        self.assertEqual(s["open_balance"], 1000.0)
        self.assertEqual(s["aging"]["90+"], 1000.0)
        self.assertEqual(s["oldest_open"], "2026-03-17")

    def test_currency_aware_match(self):
        # 同金額不同幣別不配對
        rows = [_pay(500.0, cur="USD"), _rem(500.0, cur="EUR")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables_matched_paid"], 0)
        self.assertEqual(self._sup(res, "統發興業有限公司")["open_balance"], 500.0)

    def test_each_remittance_used_once(self):
        # 兩張同額 payable、只一張同額匯款 → 一付一欠
        rows = [_pay(800.0, date="2026-06-01"), _pay(800.0, date="2026-06-05"), _rem(800.0)]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables_matched_paid"], 1)
        s = self._sup(res, "統發興業有限公司")
        self.assertEqual(s["n_open"], 1)
        self.assertEqual(s["open_balance"], 800.0)

    def test_prefer_payment_after_payable_date(self):
        # 兩張同額匯款，偏好「日期>=應付日」者
        rows = [_pay(300.0, date="2026-06-10"),
                _rem(300.0, date="2026-05-01"),   # 早於應付日
                _rem(300.0, date="2026-06-12")]   # 應付之後 → 應選這張
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables_matched_paid"], 1)
        self.assertEqual(res["n_payments_unmatched"], 1)

    def test_aging_buckets(self):
        rows = [
            _pay(10, date="2026-06-20", cp="A有限公司"),   # 5 天 → 0-30
            _pay(20, date="2026-05-10", cp="A有限公司"),   # ~46 天 → 31-60
            _pay(40, date="2026-03-01", cp="A有限公司"),   # ~116 天 → 90+
            _pay(80, date="", cp="A有限公司"),              # 無日期 → undated
        ]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        a = self._sup(res, "A有限公司")["aging"]
        self.assertEqual(a["0-30"], 10.0)
        self.assertEqual(a["31-60"], 20.0)
        self.assertEqual(a["90+"], 40.0)
        self.assertEqual(a["undated"], 80.0)

    def test_invoice_and_receipt_excluded(self):
        # invoice/receipt/statement 不納入欠款或付款
        rows = [
            {"doc_type": "invoice", "amount": 9999, "currency": "USD", "counterparty": "客戶"},
            {"doc_type": "receipt", "amount": 8888, "currency": "USD", "counterparty": "X"},
            {"doc_type": "statement", "amount": 7777, "currency": "USD", "counterparty": "Y"},
            _pay(100.0),
        ]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables"], 1)
        self.assertEqual(len(res["suppliers"]), 1)
        self.assertEqual(res["suppliers"][0]["open_balance"], 100.0)

    def test_fee_tolerance_matches_remitted_after_bank_fee(self):
        # 請款 1000、實匯 982（扣 $18 匯費）→ 容差內視為已付
        rows = [_pay(1000.0, cp="X有限公司"), _rem(982.0, cp="X")]
        res = reconcile_payments(rows, as_of_date="2026-06-25", match_tolerance=20.0)
        self.assertEqual(res["n_payables_matched_paid"], 1)
        self.assertEqual(res["n_payables_matched_exact"], 0)   # 非精確、屬扣匯費配對
        self.assertEqual(self._sup(res, "X有限公司")["open_balance"], 0.0)

    def test_tolerance_zero_keeps_fee_varied_open(self):
        rows = [_pay(1000.0, cp="X有限公司"), _rem(982.0, cp="X")]
        res = reconcile_payments(rows, as_of_date="2026-06-25", match_tolerance=0.0)
        self.assertEqual(res["n_payables_matched_paid"], 0)    # 精確模式不配
        self.assertEqual(self._sup(res, "X有限公司")["open_balance"], 1000.0)

    def test_beyond_tolerance_no_false_match(self):
        # 差 $100 遠超容差 → 不配（不可把不相干款項當已付）
        rows = [_pay(1000.0, cp="X有限公司"), _rem(900.0, cp="X")]
        res = reconcile_payments(rows, as_of_date="2026-06-25", match_tolerance=20.0)
        self.assertEqual(res["n_payables_matched_paid"], 0)
        self.assertEqual(self._sup(res, "X有限公司")["open_balance"], 1000.0)

    def test_prefers_exact_over_fuzzy(self):
        # 同時有精確與扣匯費候選 → 選精確
        rows = [_pay(500.0, cp="X有限公司"),
                _rem(485.0, cp="X", date="2026-06-15"),
                _rem(500.0, cp="X", date="2026-06-20")]
        res = reconcile_payments(rows, as_of_date="2026-06-25", match_tolerance=20.0)
        self.assertEqual(res["n_payables_matched_exact"], 1)
        self.assertEqual(res["n_payments_unmatched"], 1)        # 485 那張沒被用

    def test_aggregated_one_remittance_covers_two_payables(self):
        # 同供應商 2 張請示單(5000+3000)，一張匯款 8000 → 合併沖、兩張皆已付
        rows = [_pay(5000.0, date="2026-06-01", cp="萬華新材料有限公司"),
                _pay(3000.0, date="2026-06-03", cp="萬華新材料有限公司"),
                _rem(8000.0, date="2026-06-10", cp="WAN HUA")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables_matched_aggregated"], 2)
        self.assertEqual(self._sup(res, "萬華新材料有限公司")["open_balance"], 0.0)
        self.assertEqual(res["n_payments_unmatched"], 0)

    def test_aggregated_respects_supplier_boundary(self):
        # 兩張不同供應商(5000+3000)、匯款 8000 → 不准跨供應商湊數 → 都未付
        rows = [_pay(5000.0, cp="供應商甲"), _pay(3000.0, cp="供應商乙"),
                _rem(8000.0, cp="某匯款")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables_matched_aggregated"], 0)
        self.assertEqual(res["n_payables_matched_paid"], 0)

    def test_aggregated_with_fee_tolerance(self):
        # 5000+3000 請示單、匯款 7985（扣 $15 匯費）→ 容差內合併沖
        rows = [_pay(5000.0, cp="萬華新"), _pay(3000.0, cp="萬華新"), _rem(7985.0, cp="WH")]
        res = reconcile_payments(rows, as_of_date="2026-06-25", match_tolerance=20.0)
        self.assertEqual(res["n_payables_matched_aggregated"], 2)

    def test_exact_1to1_runs_before_aggregation(self):
        # 請示單 8000 精確等於匯款 8000 → 走 1:1 精確；另張 3000 無配對 → open
        rows = [_pay(8000.0, date="2026-06-01", cp="萬華新"),
                _pay(3000.0, date="2026-06-02", cp="萬華新"),
                _rem(8000.0, date="2026-06-10", cp="WH")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["n_payables_matched_exact"], 1)
        self.assertEqual(res["n_payables_matched_aggregated"], 0)
        self.assertEqual(self._sup(res, "萬華新")["open_balance"], 3000.0)

    def test_own_company_payable_skipped(self):
        # 對方抽成自家公司「佳桀」→ 抽錯，不計入應付
        rows = [_pay(5000.0, cp="佳桀有限公司"), _pay(300.0, cp="真供應商")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual(res["skipped_own_company"], 1)
        self.assertEqual(res["n_payables"], 1)
        self.assertEqual([s["counterparty"] for s in res["suppliers"]], ["真供應商"])

    def test_sorted_by_open_balance(self):
        rows = [_pay(100, cp="小欠"), _pay(5000, cp="大欠"), _pay(900, cp="中欠")]
        res = reconcile_payments(rows, as_of_date="2026-06-25")
        self.assertEqual([s["counterparty"] for s in res["suppliers"]], ["大欠", "中欠", "小欠"])


if __name__ == "__main__":
    unittest.main()
