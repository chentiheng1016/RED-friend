"""Concurrency test for agent_core.alert_pusher.

The race we're protecting against: two concurrent runs of push_pending_alerts
(e.g. launchd alert_check fires at the same time a user calls push_alerts_now
from telegram) could both see "alert X not yet pushed" in their respective
loaded state, both push to Telegram, both write the state back — DOUBLE
push, possibly DOUBLE spam.

After the locked_json migration, the whole push-decision + state-update
is wrapped in fcntl.flock, so the second invocation blocks until the first
finishes (~seconds) and reads the updated state with "already pushed."
"""
import importlib
import json
import os
import threading
import unittest
from unittest import mock


class AlertPusherConcurrencyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmpdir, "alert_push_state.json")

        import agent_core.alert_pusher as ap
        importlib.reload(ap)
        self.ap = ap
        # Point state file to tmpdir
        self.ap._PUSH_STATE_FILE = self.state_file

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_state(self):
        if not os.path.isfile(self.state_file):
            return {}
        with open(self.state_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_concurrent_push_pending_alerts_dedupes(self):
        """Two threads each call push_pending_alerts with the same alert →
        Telegram is pushed exactly ONCE, not twice.

        Pre-flock: both threads read state={}, both decide should_push=True,
        both call telegram_push, both write state[aid] = {...}. Result:
        2 telegram pushes for the same alert.

        Post-flock: thread A acquires the lock, pushes, writes state. Thread B
        waits, then reads state (alert is now in it), sees should_push=False
        (recently pushed), skips. Total: 1 push.
        """
        fake_alerts = [{"id": "boom", "level": "crit", "title": "service down",
                        "detail": "exit 1"}]
        push_count = [0]
        push_lock = threading.Lock()

        def fake_push(_msg=None, *args, **kwargs):
            # Slow push to widen the race window — without the flock, both
            # threads would call this in the gap between read and write.
            # Accept *args/**kwargs so this fake stays signature-compatible
            # with telegram_push(message, chat_id=...) regardless of how the
            # caller invokes it.
            import time
            time.sleep(0.05)
            with push_lock:
                push_count[0] += 1
            return "✅ sent"

        results = []
        results_lock = threading.Lock()

        def runner():
            r = self.ap.push_pending_alerts()
            with results_lock:
                results.append(r)

        # Install the patches ONCE in the main thread, around both worker
        # threads — NOT inside each runner. mock.patch saves "the original" on
        # __enter__ and restores it on __exit__; two threads each entering the
        # same patch race such that the second thread saves the *first thread's
        # mock* as the original and restores it on exit, permanently leaking
        # the fake telegram_push into the rest of the suite (it then ERRORs
        # later tests that call telegram_push(chat_id=...)). Patching once here
        # avoids the race while still exercising concurrent push_pending_alerts.
        with mock.patch("agent_core.dashboard_alerts.check_alerts",
                        return_value=fake_alerts), \
            mock.patch("agent_core.telegram.telegram_push",
                        side_effect=fake_push):
            t1 = threading.Thread(target=runner)
            t2 = threading.Thread(target=runner)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(push_count[0], 1,
                         f"alert pushed {push_count[0]} times — expected 1")
        # Exactly one run reports pushed=1; the other reports 0 (dedupe).
        pushed_counts = sorted(r["pushed"] for r in results)
        self.assertEqual(pushed_counts, [0, 1],
                         f"expected one run to push and one to skip; got {pushed_counts}")
        # State has the alert recorded
        state = self._read_state()
        self.assertIn("boom", state)
        self.assertEqual(state["boom"]["title"], "service down")


if __name__ == "__main__":
    unittest.main()
