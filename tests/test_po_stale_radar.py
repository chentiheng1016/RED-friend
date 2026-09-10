"""Tests for the stale-active-PO radar (email_timeline.stale_active_pos /
check_stale_pos).

營運守則的 stale-data 警示資料層：「最近還有往來、但斷訊超過 N 天」的 PO。
關鍵性質：
  - 結案已久（超出活躍視窗）的 PO 自然掉出，不誤報
  - 只被提過一次的 PO 號（誤抓料號/引用舊單）被 min_threads 濾掉
  - 全部健康時回 ✅ 開頭（daemon 靠這個決定安靜退出）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _row(po_numbers, last_date, customers=None, thread_id="t1", state="進行中",
         promised=None, subject="PO 往來"):
    return {
        "thread_id": thread_id,
        "date": last_date,
        "last_message_date": last_date,
        "state": state,
        "subject": subject,
        "entities_json": json.dumps(
            {"po_numbers": po_numbers, "customers": customers or [],
             "promised_dates": promised or []}
        ),
    }


class StaleActivePosTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._parquet = os.path.join(self._tmp.name, "emails.parquet")
        from agent_core import email_timeline
        self._patch = mock.patch.object(
            email_timeline, "_INTERNAL_PARQUET", self._parquet
        )
        self._patch.start()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)

    def tearDown(self):
        from agent_core import email_timeline
        self._patch.stop()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)
        self._tmp.cleanup()

    def _write_parquet(self, rows):
        import pandas as pd
        pd.DataFrame(rows).to_parquet(self._parquet)

    def test_stale_po_detected_with_customer_and_thread_count(self):
        self._write_parquet([
            _row(["JF0P26040054"], _days_ago(12), ["Blaklader"], thread_id="t1"),
            _row(["JF0P26040054"], _days_ago(10), ["Blaklader"], thread_id="t2"),
        ])
        from agent_core.email_timeline import stale_active_pos
        r = stale_active_pos(stale_days=7, active_window_days=45)
        self.assertEqual(r["stale_total"], 1)
        self.assertEqual(r["active_total"], 1)
        s = r["stale"][0]
        self.assertEqual(s["po"], "JF0P26040054")
        self.assertEqual(s["last_date"], _days_ago(10))
        self.assertEqual(s["threads"], 2)
        self.assertEqual(s["customer"], "Blaklader")
        self.assertGreaterEqual(s["days_quiet"], 9)

    def test_recent_traffic_is_healthy_not_stale(self):
        self._write_parquet([
            _row(["71608"], _days_ago(20), thread_id="t1"),
            _row(["71608"], _days_ago(2), thread_id="t2"),  # 最近有往來
        ])
        from agent_core.email_timeline import stale_active_pos
        r = stale_active_pos(stale_days=7, active_window_days=45)
        self.assertEqual(r["stale_total"], 0)
        self.assertEqual(r["active_total"], 1)

    def test_long_quiet_po_treated_as_closed_and_excluded(self):
        """出貨結案的 PO：沉寂超過活躍視窗 → 不列入 active 也不報 stale。"""
        self._write_parquet([
            _row(["PRJ24100005"], _days_ago(60), thread_id="t1"),
            _row(["PRJ24100005"], _days_ago(70), thread_id="t2"),
        ])
        from agent_core.email_timeline import stale_active_pos
        r = stale_active_pos(stale_days=7, active_window_days=45)
        self.assertEqual(r["stale_total"], 0)
        self.assertEqual(r["active_total"], 0)

    def test_single_mention_po_filtered_by_min_threads(self):
        """只出現一串的 PO 多半是誤抓/順帶引用 — 不值得叫人跟催。"""
        self._write_parquet([
            _row(["LJF999"], _days_ago(10), thread_id="t1"),
        ])
        from agent_core.email_timeline import stale_active_pos
        r = stale_active_pos(stale_days=7, active_window_days=45, min_threads=2)
        self.assertEqual(r["stale_total"], 0)
        # 但它仍算活躍（有 45 天內的往來）
        self.assertEqual(r["active_total"], 1)

    def test_worst_quiet_first_ordering(self):
        self._write_parquet([
            _row(["PO-A"], _days_ago(9), thread_id="a1"),
            _row(["PO-A"], _days_ago(9), thread_id="a2"),
            _row(["PO-B"], _days_ago(20), thread_id="b1"),
            _row(["PO-B"], _days_ago(25), thread_id="b2"),
        ])
        from agent_core.email_timeline import stale_active_pos
        r = stale_active_pos(stale_days=7, active_window_days=45)
        self.assertEqual([s["po"] for s in r["stale"]], ["PO-B", "PO-A"])

    def test_completed_latest_thread_marks_po_closed(self):
        """剛出完貨的單：最新一串 state=已完成 → 視窗內也不誤報。"""
        self._write_parquet([
            _row(["JF0P26040054"], _days_ago(15), thread_id="t1"),
            _row(["JF0P26040054"], _days_ago(10), thread_id="t2", state="已完成"),
        ])
        from agent_core.email_timeline import stale_active_pos
        r = stale_active_pos(stale_days=7, active_window_days=45)
        self.assertEqual(r["stale_total"], 0)
        self.assertEqual(r["active_total"], 0)
        self.assertEqual(r["closed_skipped"], 1)

    def test_old_completed_thread_does_not_close_a_reopened_po(self):
        """歷史串標過已完成、但後來又有進行中往來 → 以最新串為準，照常追。"""
        self._write_parquet([
            _row(["71608"], _days_ago(30), thread_id="t1", state="已完成"),
            _row(["71608"], _days_ago(10), thread_id="t2", state="進行中"),
        ])
        from agent_core.email_timeline import stale_active_pos
        r = stale_active_pos(stale_days=7, active_window_days=45)
        self.assertEqual(r["stale_total"], 1)
        self.assertEqual(r["stale"][0]["po"], "71608")


class CheckStalePosToolTests(unittest.TestCase):
    """LLM-facing formatter — daemon 也靠開頭字元決定要不要推 Telegram。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._parquet = os.path.join(self._tmp.name, "emails.parquet")
        from agent_core import email_timeline
        self._patch = mock.patch.object(
            email_timeline, "_INTERNAL_PARQUET", self._parquet
        )
        self._patch.start()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)

    def tearDown(self):
        from agent_core import email_timeline
        self._patch.stop()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)
        self._tmp.cleanup()

    def _write_parquet(self, rows):
        import pandas as pd
        pd.DataFrame(rows).to_parquet(self._parquet)

    def test_findings_render_po_customer_and_advice(self):
        self._write_parquet([
            _row(["JF0P26040054"], _days_ago(12), ["Blaklader"], thread_id="t1"),
            _row(["JF0P26040054"], _days_ago(10), ["Blaklader"], thread_id="t2"),
        ])
        from agent_core.email_timeline import check_stale_pos
        out = check_stale_pos()
        self.assertTrue(out.startswith("📡"))
        self.assertIn("JF0P26040054", out)
        self.assertIn("Blaklader", out)
        self.assertIn("query_po_timeline", out)  # 建議下一步

    def test_all_healthy_returns_quiet_marker(self):
        self._write_parquet([
            _row(["71608"], _days_ago(1), thread_id="t1"),
            _row(["71608"], _days_ago(2), thread_id="t2"),
        ])
        from agent_core.email_timeline import check_stale_pos
        out = check_stale_pos()
        self.assertTrue(out.startswith("✅"), out)

    def test_missing_lake_returns_warning_not_crash(self):
        # 不寫 parquet → FileNotFoundError 路徑
        from agent_core.email_timeline import check_stale_pos
        out = check_stale_pos()
        self.assertTrue(out.startswith("⚠️"), out)


class OverduePromisesTests(unittest.TestCase):
    """交期逾期雷達 — 承諾日已過但 thread 還在進行中才報；
    已完成/取消/未到期/解析不出 都安靜。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._parquet = os.path.join(self._tmp.name, "emails.parquet")
        from agent_core import email_timeline
        self._patch = mock.patch.object(
            email_timeline, "_INTERNAL_PARQUET", self._parquet
        )
        self._patch.start()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)

    def tearDown(self):
        from agent_core import email_timeline
        self._patch.stop()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)
        self._tmp.cleanup()

    def _write_parquet(self, rows):
        import pandas as pd
        pd.DataFrame(rows).to_parquet(self._parquet)

    def test_overdue_promise_on_active_thread_is_flagged(self):
        due = _days_ago(10)  # 承諾日 = 10 天前（完整日期 → 不需 anchor 推年）
        self._write_parquet([
            _row(["JF0P26040054"], _days_ago(8), ["Blaklader"], thread_id="t1",
                 promised=[f"{due} 出貨"]),
        ])
        from agent_core.email_timeline import overdue_promises
        r = overdue_promises(grace_days=1)
        self.assertEqual(r["overdue_total"], 1)
        o = r["overdue"][0]
        self.assertEqual(o["po"], "JF0P26040054")
        self.assertEqual(o["customer"], "Blaklader")
        self.assertEqual(o["due"], due)
        self.assertGreaterEqual(o["days_overdue"], 9)

    def test_future_promise_is_quiet(self):
        self._write_parquet([
            _row(["71608"], _days_ago(2), thread_id="t1",
                 promised=[f"{(datetime.now() + timedelta(days=14)).strftime('%Y-%m-%d')} 出貨"]),
        ])
        from agent_core.email_timeline import overdue_promises
        self.assertEqual(overdue_promises()["overdue_total"], 0)

    def test_completed_thread_not_flagged_even_if_overdue(self):
        self._write_parquet([
            _row(["71608"], _days_ago(5), thread_id="t1", state="已完成",
                 promised=[f"{_days_ago(10)} 出貨"]),
        ])
        from agent_core.email_timeline import overdue_promises
        self.assertEqual(overdue_promises()["overdue_total"], 0)

    def test_unparseable_promise_is_skipped(self):
        self._write_parquet([
            _row(["71608"], _days_ago(2), thread_id="t1", promised=["盡快"]),
        ])
        from agent_core.email_timeline import overdue_promises
        r = overdue_promises()
        self.assertEqual(r["overdue_total"], 0)
        self.assertEqual(r["scanned_threads"], 1)

    def test_same_po_same_due_deduped_across_threads(self):
        due = _days_ago(7)
        self._write_parquet([
            _row(["PO-X"], _days_ago(3), thread_id="t1", promised=[f"{due} 出貨"]),
            _row(["PO-X"], _days_ago(2), thread_id="t2", promised=[f"{due} 出貨"]),
        ])
        from agent_core.email_timeline import overdue_promises
        self.assertEqual(overdue_promises()["overdue_total"], 1)

    def test_legacy_rows_without_promised_dates_are_fine(self):
        self._write_parquet([
            {  # 2026-06 之前的舊列：entities_json 沒有 promised_dates key
                "thread_id": "old1", "date": _days_ago(3),
                "last_message_date": _days_ago(3), "state": "進行中",
                "subject": "舊信", "entities_json": json.dumps({"po_numbers": ["P1"]}),
            },
        ])
        from agent_core.email_timeline import check_overdue_promises
        out = check_overdue_promises()
        self.assertTrue(out.startswith("✅"), out)

    def test_formatter_lists_promise_and_days(self):
        due = _days_ago(6)
        self._write_parquet([
            _row(["JF0P1"], _days_ago(4), ["Richter"], thread_id="t1",
                 promised=[f"{due} 出貨"]),
        ])
        from agent_core.email_timeline import check_overdue_promises
        out = check_overdue_promises()
        self.assertTrue(out.startswith("⏰"), out)
        self.assertIn("JF0P1", out)
        self.assertIn("Richter", out)
        self.assertIn(due, out)
        self.assertIn("query_po_timeline", out)


class FormatTimelinePromisedDatesTests(unittest.TestCase):
    """promised_dates（2026-06 起的抽取欄位）要在 timeline 裡看得到；
    舊資料沒這欄 → 不顯示也不爆。"""

    @staticmethod
    def _result(promised_header=None, promised_event=None):
        return {
            "po": "JF0P26040054",
            "total_hits": 1,
            "date_range": ("2026-06-01", "2026-06-10"),
            "departments_touched": [("業務", 1)],
            "entities": {
                "customers": ["Blaklader"],
                **({"promised_dates": promised_header} if promised_header else {}),
            },
            "events": [{
                "date": "2026-06-10",
                "dept": "業務",
                "direction": "inbound",
                "sender": "a@company.example",
                "subject": "PO 確認",
                "summary": "確認出貨",
                **({"promised_dates": promised_event} if promised_event else {}),
            }],
        }

    def test_promised_dates_rendered_in_header_and_event(self):
        from agent_core.email_timeline import format_timeline
        out = format_timeline(self._result(
            promised_header=["6/30 出貨"], promised_event=["6/30 出貨"],
        ))
        self.assertIn("⏰ 承諾交期：6/30 出貨", out)
        self.assertEqual(out.count("⏰"), 2)  # header + event 各一

    def test_legacy_rows_without_field_render_clean(self):
        from agent_core.email_timeline import format_timeline
        out = format_timeline(self._result())
        self.assertNotIn("⏰", out)
        self.assertIn("PO 確認", out)


if __name__ == "__main__":
    unittest.main()
