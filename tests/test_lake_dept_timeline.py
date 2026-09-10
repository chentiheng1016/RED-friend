"""read_dept_email_timeline 的部門過濾/排序/格式（合成 DataFrame，免讀檔/網路）。"""
import unittest

import pandas as pd

from agent_core.lake_dept_timeline import (
    _dept_timeline_from_df,
    _format_dept_timeline,
    read_dept_email_timeline,
)


class DeptTimelineTests(unittest.TestCase):
    @staticmethod
    def _df():
        return pd.DataFrame([
            {"date": "2026-06-18", "subject": "倉庫 6月庫存盤點", "primary_dept": "倉庫",
             "all_depts": "倉庫", "summary": "6月在庫盤點完成", "brands": "[]"},
            {"date": "2026-06-10", "subject": "出入庫 LOT307", "primary_dept": "倉庫",
             "all_depts": "倉庫,船務", "summary": "LOT307 入庫", "brands": "[]"},
            {"date": "2026-06-15", "subject": "付款通知 DECA", "primary_dept": "會計",
             "all_depts": "會計", "summary": "DECA 付款已匯", "brands": '["decathlon"]'},
            {"date": "2026-06-01", "subject": "業務報價", "primary_dept": "業務",
             "all_depts": "業務", "summary": "x", "brands": "[]"},
        ])

    def test_dept_filter(self):
        sm = _dept_timeline_from_df(self._df(), "倉庫")
        self.assertEqual(len(sm), 2)
        self.assertNotIn("付款", " ".join(sm["subject"]))

    def test_all_depts_fallback(self):
        sm = _dept_timeline_from_df(self._df(), "船務")   # 只在 all_depts、非 primary
        self.assertEqual(len(sm), 1)
        self.assertIn("LOT307", sm.iloc[0]["subject"])

    def test_newest_first_and_query(self):
        sm = _dept_timeline_from_df(self._df(), "倉庫")
        self.assertIn("盤點", sm.iloc[0]["subject"])      # 06-18 最新
        sm2 = _dept_timeline_from_df(self._df(), "會計", query="DECA")
        self.assertEqual(len(sm2), 1)
        self.assertIn("會計", _format_dept_timeline(sm2, "會計"))

    def test_invalid_dept_guard(self):
        out = read_dept_email_timeline("不存在")           # 早退、不讀檔
        self.assertIn("須為其中之一", out)


if __name__ == "__main__":
    unittest.main()
