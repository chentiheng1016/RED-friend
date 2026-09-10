"""報價歷史（orange_sales quote / quote_gen）的資料正確性回歸：

  - query_quote_history 價格統計依幣別分組（USD 跟 TWD 混平均是無意義數字）
  - _suggest_price_from_history 只取同幣別歷史；無同幣別明說、note 不寫死 '$'
  - extract_quote_from_email 去重帳本以 thread 為單位（message_id/thread_id 混用
    會讓同一串抽兩次、統計灌水）
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.agents.orange_sales import quote as q
from agent_core.agents.orange_sales import quote_gen as qg


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=q._QUOTE_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _row(customer, sku, price, currency, direction="out", email_date="2026-06-01"):
    return {
        "extracted_at": "2026-06-01T00:00:00", "message_id": "t", "email_date": email_date,
        "direction": direction, "customer": customer, "sku": sku, "qty": 100,
        "unit_price": price, "currency": currency, "incoterm": "FOB",
        "delivery_date": "", "subject": "s", "notes": "",
    }


class QueryQuoteHistoryCurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._csv = os.path.join(self._tmp.name, "auto_extracted.csv")

    def tearDown(self):
        self._tmp.cleanup()

    def test_stats_grouped_by_currency_no_blended_mean(self):
        _write_csv(self._csv, [
            _row("PAX", "S1", 10.0, "USD"),
            _row("PAX", "S1", 20.0, "USD"),
            _row("PAX", "S1", 100.0, "EUR"),
        ])
        with mock.patch.object(q, "_QUOTE_CSV", self._csv):
            out = q.query_quote_history(recent_months=0, expand_aliases=False)
        self.assertIn("USD", out)
        self.assertIn("EUR", out)
        self.assertIn("依幣別分組", out)
        self.assertIn("平均 15.00", out)      # USD 組
        self.assertIn("平均 100.00", out)     # EUR 組
        self.assertNotIn("43.33", out)        # (10+20+100)/3 的混幣平均不得出現

    def test_missing_currency_grouped_as_unknown(self):
        _write_csv(self._csv, [
            _row("PAX", "S1", 10.0, "USD"),
            _row("PAX", "S2", 7.0, ""),
        ])
        with mock.patch.object(q, "_QUOTE_CSV", self._csv):
            out = q.query_quote_history(recent_months=0, expand_aliases=False)
        self.assertIn("(無幣別)", out)
        self.assertIn("平均 7.00", out)


class SuggestPriceCurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._csv = os.path.join(self._tmp.name, "auto_extracted.csv")
        _write_csv(self._csv, [
            _row("PAX", "S1", 10.0, "USD"),
            _row("PAX", "S1", 12.0, "USD"),
            _row("PAX", "S1", 99.0, "EUR"),
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_currency_only(self):
        with mock.patch.object(qg, "_QUOTE_CSV", self._csv):
            price, note = qg._suggest_price_from_history("PAX", "S1", "USD")
        self.assertAlmostEqual(price, 11.0)   # (10+12)/2，EUR 99 不得混入
        self.assertIn("USD", note)
        self.assertNotIn("$", note)           # note 不寫死 '$'

    def test_eur_request_uses_eur_rows_only(self):
        with mock.patch.object(qg, "_QUOTE_CSV", self._csv):
            price, note = qg._suggest_price_from_history("PAX", "S1", "EUR")
        self.assertAlmostEqual(price, 99.0)
        self.assertIn("EUR", note)

    def test_no_same_currency_history_says_so(self):
        with mock.patch.object(qg, "_QUOTE_CSV", self._csv):
            price, note = qg._suggest_price_from_history("PAX", "S1", "TWD")
        self.assertEqual(price, 0.0)          # 不給混幣平均
        self.assertIn("TWD", note)
        self.assertIn("混幣", note)


class ExtractQuoteThreadDedupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._csv = os.path.join(self._tmp.name, "auto_extracted.csv")
        self._ids = os.path.join(self._tmp.name, "extracted_ids.json")
        self._patches = [
            mock.patch.object(q, "_QUOTE_DIR", self._tmp.name),
            mock.patch.object(q, "_QUOTE_CSV", self._csv),
            mock.patch.object(q, "_QUOTE_EXTRACTED_IDS", self._ids),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def _extracted(thread_id):
        return {
            "parsed": {"direction": "out", "customer": "PAX",
                       "items": [{"sku": "S1", "qty": 100, "unit_price": 10.0,
                                  "currency": "USD"}]},
            "sender": "a@b.c", "subject": "quote", "email_date": "2026-06-01",
            "message_id": thread_id, "thread_id": thread_id, "msg_count": 2,
        }

    def _csv_rows(self):
        if not os.path.exists(self._csv):
            return []
        with open(self._csv, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _saved_ids(self):
        with open(self._ids, encoding="utf-8") as f:
            return set(json.load(f))

    def test_same_thread_via_second_message_id_not_extracted_twice(self):
        with mock.patch.object(q, "_resolve_thread_id", return_value="thr1"), \
                mock.patch.object(q, "_extract_quote_from_thread",
                                  side_effect=lambda tid: self._extracted(tid)) as m_ex:
            r1 = q.extract_quote_from_email("m1")
            r2 = q.extract_quote_from_email("m2")   # 同一串、不同 message id
        self.assertIn("✅", r1)
        self.assertIn("已抽過", r2)
        self.assertEqual(m_ex.call_count, 1)        # 只抽一次
        self.assertEqual(len(self._csv_rows()), 1)  # CSV 不灌水
        self.assertLessEqual({"m1", "m2", "thr1"}, self._saved_ids())  # 兩個 mid 與 thread 都入帳

    def test_thread_id_recorded_after_build_style_id(self):
        # build_quote_history 先用 thread id 入帳 → 之後從 message id 進來也要擋
        q._save_extracted_ids({"thr1"})
        with mock.patch.object(q, "_resolve_thread_id", return_value="thr1"), \
                mock.patch.object(q, "_extract_quote_from_thread") as m_ex:
            r = q.extract_quote_from_email("m9")
        self.assertIn("已抽過", r)
        m_ex.assert_not_called()

    def test_no_items_marks_both_ids(self):
        empty = {"parsed": {"items": [], "direction": "unknown"}, "sender": "", "subject": "",
                 "email_date": "", "message_id": "thr2", "thread_id": "thr2", "msg_count": 1}
        with mock.patch.object(q, "_resolve_thread_id", return_value="thr2"), \
                mock.patch.object(q, "_extract_quote_from_thread", return_value=empty):
            r = q.extract_quote_from_email("m1")
        self.assertIn("無報價資訊", r)
        self.assertLessEqual({"m1", "thr2"}, self._saved_ids())


if __name__ == "__main__":
    unittest.main()
