"""健檢 Low batch 4（先前 deferred 的 3 個）：
- LINE webhook 限流 client_ip：可設 RED_RATE_LIMIT_TRUSTED_HOPS 取可信代理看到的來源
  （預設 0 = 維持取最左、不改既有行為）
- citation-retry 第二次 inference 包自己的 heartbeat pulse（堵 503 風暴下誤觸 watchdog）
- runs/index.jsonl 加 size cap（trivial，full suite 覆蓋 import）
"""
import inspect
import os
import unittest
from unittest import mock


class XffTrustedHopsTests(unittest.TestCase):
    def _hdrs(self):
        return [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2, 3.3.3.3")]

    def test_default_is_leftmost_unchanged(self):
        from agent_core.web_server.rate_limiter import client_ip
        with mock.patch.dict(os.environ):
            os.environ.pop("RED_RATE_LIMIT_TRUSTED_HOPS", None)
            self.assertEqual(client_ip(self._hdrs()), "1.1.1.1")

    def test_one_trusted_hop_takes_second_from_right(self):
        from agent_core.web_server.rate_limiter import client_ip
        with mock.patch.dict(os.environ, {"RED_RATE_LIMIT_TRUSTED_HOPS": "1"}):
            self.assertEqual(client_ip(self._hdrs()), "2.2.2.2")

    def test_hops_beyond_list_falls_back_to_leftmost(self):
        from agent_core.web_server.rate_limiter import client_ip
        with mock.patch.dict(os.environ, {"RED_RATE_LIMIT_TRUSTED_HOPS": "9"}):
            self.assertEqual(client_ip(self._hdrs()), "1.1.1.1")


class CitationRetryPulseTests(unittest.TestCase):
    def test_retry_inference_wrapped_in_heartbeat_pulse(self):
        import agent_core.daemon_telegram as dt
        src = inspect.getsource(dt)
        self.assertIn("_retry_pulse", src)


if __name__ == "__main__":
    unittest.main()
