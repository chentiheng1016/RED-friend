"""Regression guard for the google-auth Regional Access Boundary (RAB) log filter.

google-auth 2.55+ fires an unconditional RAB lookup on every service-account
token refresh; RED's SA can't read its own allowedLocations, so each refresh
logs a benign 403 "Regional Access Boundary HTTP request failed" WARNING. The
lookup failure is non-fatal (Drive/Gmail still sync), so we suppress the line at
the logging layer via install_sdk_log_filters(). This test locks that behavior:
the noise line is dropped, real warnings on the same logger survive, and the
install is idempotent (no duplicate filters, works even with no handlers /
logging.lastResort — the daemon entrypoint case).
"""
import logging
import os
import unittest

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core.logging_and_paths import (
    _DropNoisySDKWarnings,
    install_sdk_log_filters,
)

_RAB_MSG = (
    "Regional Access Boundary HTTP request failed after retries: "
    "response_data={'error': {'code': 403, 'message': "
    "'Permission denied on the service account.'}}, retryable_error=False"
)
_RAB_LOGGERS = ("google.oauth2._client", "google.oauth2._client_async")


def _make_record(logger_name, msg):
    return logging.LogRecord(
        name=logger_name, level=logging.WARNING, pathname=__file__,
        lineno=1, msg=msg, args=(), exc_info=None,
    )


class RabLogFilterTests(unittest.TestCase):
    def setUp(self):
        # Snapshot + strip our filter so each test starts clean and we don't
        # leak filters into the rest of the suite (loggers are global).
        self._saved = {n: list(logging.getLogger(n).filters) for n in _RAB_LOGGERS}
        for n in _RAB_LOGGERS:
            lg = logging.getLogger(n)
            lg.filters = [f for f in lg.filters if not isinstance(f, _DropNoisySDKWarnings)]

    def tearDown(self):
        for n, saved in self._saved.items():
            logging.getLogger(n).filters = saved

    def test_rab_line_dropped_real_warning_survives(self):
        install_sdk_log_filters()
        for n in _RAB_LOGGERS:
            lg = logging.getLogger(n)
            self.assertFalse(
                lg.filter(_make_record(n, _RAB_MSG)),
                f"RAB noise line should be dropped on {n}",
            )
            self.assertTrue(
                lg.filter(_make_record(n, "a genuine warning that must survive")),
                f"unrelated warning should pass on {n}",
            )

    def test_install_is_idempotent(self):
        install_sdk_log_filters()
        install_sdk_log_filters()
        for n in _RAB_LOGGERS:
            count = sum(
                isinstance(f, _DropNoisySDKWarnings)
                for f in logging.getLogger(n).filters
            )
            self.assertEqual(count, 1, f"exactly one filter expected on {n}, got {count}")


if __name__ == "__main__":
    unittest.main()
