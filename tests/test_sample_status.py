"""read_sample_status 的樣品 thread 過濾/排序/格式（合成 DataFrame，免讀檔/網路）。"""
import unittest

import pandas as pd

from agent_core.sample_status import _format_timeline, _sample_timeline_from_df


class SampleStatusTests(unittest.TestCase):
    @staticmethod
    def _df():
        return pd.DataFrame([
            {"date": "2026-06-18", "subject": "FY26 JALAS 5618 樣品已備妥", "primary_dept": "樣品室",
             "summary": "JALAS #5618 樣品已備妥，待 FedEx", "brands": '["JALAS"]'},
            {"date": "2026-06-10", "subject": "LURCHI 打樣材料明細", "primary_dept": "樣品室",
             "summary": "Lurchi 樣品材料", "brands": '["Lurchi"]'},
            {"date": "2026-06-15", "subject": "5618 sample order confirm", "primary_dept": "業務",
             "summary": "sample 確認", "brands": '["JALAS"]'},   # 非樣品室、但主旨含 sample
            {"date": "2026-06-01", "subject": "採購 PO 一般料", "primary_dept": "採購",
             "summary": "無關", "brands": "[]"},
        ])

    def test_filter_includes_dept_and_keyword(self):
        sm = _sample_timeline_from_df(self._df())
        self.assertEqual(len(sm), 3)                      # 2 樣品室 + 1 主旨含 sample
        self.assertNotIn("採購 PO", " ".join(sm["subject"]))

    def test_newest_first(self):
        sm = _sample_timeline_from_df(self._df())
        self.assertIn("5618 樣品已備妥", sm.iloc[0]["subject"])  # 06-18 最新

    def test_query_filter(self):
        sm = _sample_timeline_from_df(self._df(), query="5618")
        self.assertEqual(len(sm), 2)                      # JALAS 5618 兩筆
        out = _format_timeline(sm, query="5618")
        self.assertIn("樣品室進度", out)
        self.assertIn("5618", out)

    def test_format_includes_summary(self):
        sm = _sample_timeline_from_df(self._df(), query="JALAS", limit=1)
        self.assertIn("已備妥", _format_timeline(sm))

    def test_no_sample_returns_empty(self):
        df = pd.DataFrame([{"date": "2026-06-01", "subject": "純採購", "primary_dept": "採購",
                            "summary": "x", "brands": "[]"}])
        self.assertTrue(_sample_timeline_from_df(df).empty)


if __name__ == "__main__":
    unittest.main()
