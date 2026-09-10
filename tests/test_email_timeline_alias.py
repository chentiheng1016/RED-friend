"""timeline_by_customer 的 alias 比對回歸：反向 substring（rc in alias）要有最短
長度門檻 —— 抽取出的超短客戶名（'LAK'、'AB'）幾乎必然是別家名字的子字串，
雙向包含會把不相干客戶的信全掃進來。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _row(thread_id, customers, subject="日常往來", summary="內容", date="2026-06-01"):
    return {
        "thread_id": thread_id, "date": date, "last_message_date": date,
        "sender": "x@company.example", "subject": subject, "summary": summary,
        "raw_body_preview": "", "primary_dept": "業務", "direction": "internal",
        "state": "進行中", "message_count": 1,
        "entities_json": json.dumps({"customers": customers}),
    }


class TimelineAliasMatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._parquet = os.path.join(self._tmp.name, "emails.parquet")
        from agent_core import email_timeline
        self._patch = mock.patch.object(email_timeline, "_INTERNAL_PARQUET", self._parquet)
        self._patch.start()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)

    def tearDown(self):
        from agent_core import email_timeline
        self._patch.stop()
        email_timeline._df_cache.update(df=None, ts=0.0, mtime=0.0)
        self._tmp.cleanup()

    def _write(self, rows):
        import pandas as pd
        pd.DataFrame(rows).to_parquet(self._parquet)

    def _query(self, cust):
        from agent_core.email_timeline import timeline_by_customer
        with mock.patch("agent_core.entity_resolver.resolve_entity",
                        return_value={"found": True, "canonical": cust.upper(),
                                      "aliases": [cust.upper()]}):
            return timeline_by_customer(cust, days_back=None)

    def test_short_extracted_customer_does_not_cross_match(self):
        self._write([
            _row("t1", ["AB BLAKLADER"]),   # 正向 alias in rc → 命中
            _row("t2", ["LAK"]),            # 3 字短名是 BLAKLADER 的子字串 → 不得反向誤中
        ])
        r = self._query("Blaklader")
        self.assertEqual(r["total_hits"], 1)
        self.assertEqual(r["events"][0]["thread_id"], "t1")

    def test_reverse_containment_still_works_for_meaningful_names(self):
        # rc >= 4 字時反向包含仍有效（'BLAK' 縮寫命中 'BLAKLADER'）
        self._write([_row("t1", ["BLAK"])])
        r = self._query("Blaklader")
        self.assertEqual(r["total_hits"], 1)


if __name__ == "__main__":
    unittest.main()
