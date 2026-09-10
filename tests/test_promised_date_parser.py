"""Tests for parse_promised_date — 承諾交期短語 → 具體日期。

測例直接取自 EXTRACT_PROMPT A/B 驗證跑出的真實 promised_dates 樣本
（scripts/validate_extract_prompt_ab.py，2026-06-11 100 串）。
解析原則：模糊粒度取最晚合理日（週→週日、月底→月末）、多日期取最晚、
解析不出回 None — 寧可漏報不誤報。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# anchor 固定 2026-06-11（星期四）— 相對詞測例的基準
_ANCHOR = datetime(2026, 6, 11)


class ParsePromisedDateTests(unittest.TestCase):
    def _check(self, phrase: str, expected: str | None, anchor: datetime = _ANCHOR):
        from agent_core.email_timeline import parse_promised_date
        got = parse_promised_date(phrase, anchor)
        if expected is None:
            self.assertIsNone(got, f"{phrase!r} 應解析不出，卻得到 {got}")
        else:
            self.assertIsNotNone(got, f"{phrase!r} 應解析出 {expected}，卻得到 None")
            self.assertEqual(got.strftime("%Y-%m-%d"), expected, f"phrase={phrase!r}")

    # ── 完整日期（含年）──
    def test_full_dates(self):
        self._check("2026/09/10 貨品準備好", "2026-09-10")
        self._check("2026.06.30 到貨", "2026-06-30")
        self._check("2026-06-30", "2026-06-30")
        self._check("2026 年 6 月 10 日前應到", "2026-06-10")
        self._check("付款日期: 2026/7/7", "2026-07-07")

    def test_range_takes_the_deadline_end(self):
        self._check("2026.07.01 ~ 2026.12.31", "2026-12-31")

    # ── 月/日無年 → 挑離 anchor 最近的年份 ──
    def test_month_day_year_inference(self):
        self._check("ETD 8/27", "2026-08-27")
        self._check("7/16", "2026-07-16")
        # 近期已過的交期保留在過去（逾期偵測要用）
        self._check("5/20 出貨", "2026-05-20")
        # 12 月信裡的 1/15 → 推成明年
        self._check("1/15 出貨", "2027-01-15", anchor=datetime(2026, 12, 20))

    # ── 英文月名 ──
    def test_english_months(self):
        self._check("JULY. 10.", "2026-07-10")
        self._check("JUN 18 , 2026", "2026-06-18")
        self._check("before 10th June 2026", "2026-06-10")
        self._check("June 30, 2026", "2026-06-30")

    # ── 中文月份粒度 ──
    def test_chinese_month_granularity(self):
        self._check("六月底出貨", "2026-06-30")
        self._check("6月底", "2026-06-30")
        self._check("7月中", "2026-07-15")
        self._check("8月初交 PP 樣", "2026-08-05")

    def test_end_of_month_english(self):
        self._check("end of June", "2026-06-30")

    # ── week N（取該 ISO 週週日）──
    def test_iso_week(self):
        expected = datetime.fromisocalendar(2026, 26, 7).strftime("%Y-%m-%d")
        self._check("ETD: week 26th", expected)
        self._check("week 26", expected)

    # ── 相對詞（anchor 2026-06-11 = 週四）──
    def test_relative_terms(self):
        self._check("今日內盡速提供材料的 ETA", "2026-06-11")
        self._check("明天中午前", "2026-06-12")
        self._check("本週五", "2026-06-12")
        self._check("下週一", "2026-06-15")
        self._check("本週內", "2026-06-14")   # 該週週日（最晚）
        self._check("下週初", "2026-06-15")   # 下週一

    # ── 解析不出 → None ──
    def test_unparseable_returns_none(self):
        self._check("盡快", None)
        self._check("隨新訂單一起出", None)
        self._check("Along with our new order, LOT 217-2026", None)
        self._check("", None)
        self._check("ASAP please", None)

    def test_multiple_dates_take_latest(self):
        # 「ETD: 4/8、ETA: 5/23、預計 5/27 可到」→ 期限是最後一個節點
        self._check("ETD: 4/8, ETA (CAT LAI): 5/23, 預計 5/27可到福群工廠",
                    "2026-05-27", anchor=datetime(2026, 4, 16))

    def test_invalid_calendar_dates_skipped(self):
        self._check("2/30 出貨", None)


if __name__ == "__main__":
    unittest.main()
