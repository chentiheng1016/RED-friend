"""健檢 Medium regression: 未授權 Telegram 私訊路徑現在 (1) 先過 per-sender rate limit
（洪水即丟、不寫 join-request、不回覆）(2) unbound 回覆只在 join-record should_notify 的
300s 窗口送（非每則 1:1 反射放大）。rate-limit 本體已由 test_security_regressions 覆蓋；
這裡鎖 should_notify 節流——回覆與 owner-notify 現在都依它。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from agent_core import daemon_telegram as dt


class UnauthReplyThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_join_")
        self.jf = os.path.join(self.tmp, "join_requests.json")
        self._p = mock.patch.object(dt, "_telegram_join_requests_file", return_value=self.jf)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _msg(self, cid="900900"):
        return {"chat": {"id": cid, "type": "private"},
                "from": {"id": cid, "username": "stranger"}}

    def test_should_notify_throttles_to_one_per_window(self):
        m = self._msg()
        win = dt._TG_JOIN_NOTIFY_INTERVAL_S
        # first contact → True (reply + owner-notify go out)
        self.assertTrue(dt._telegram_record_join_request(m, text="hi", now=1000.0)["should_notify"])
        # within the window → False (reply now throttled, not 1:1)
        self.assertFalse(dt._telegram_record_join_request(m, text="hi", now=1000.0 + 60)["should_notify"])
        self.assertFalse(dt._telegram_record_join_request(m, text="hi", now=1000.0 + win - 1)["should_notify"])
        # after the window → True again
        self.assertTrue(dt._telegram_record_join_request(m, text="hi", now=1000.0 + win + 1)["should_notify"])

    def test_non_private_chat_records_nothing(self):
        m = {"chat": {"id": "1", "type": "group"}, "from": {"id": "1"}}
        self.assertEqual(dt._telegram_record_join_request(m, text="x", now=1.0), {})


if __name__ == "__main__":
    unittest.main()
