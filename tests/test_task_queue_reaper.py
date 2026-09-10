"""Regression (review finding #8): a worker that died between claiming a task
(state=RUNNING, started_at set, mutex held) and writing back its result left
the task RUNNING forever. busy_groups only frees a lock whose holder is NOT
running, so the zombie pinned its mutex group permanently; _gc_done_tasks never
removed RUNNING; submit_task counted it against the 500 cap. Pin that stale
RUNNING tasks are reaped (re-queued or DLQ'd) and their mutex freed.
"""
from __future__ import annotations

import contextlib
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ReapStaleRunningTests(unittest.TestCase):
    def setUp(self):
        from agent_core import task_queue as tq

        self.tq = tq

    def _running(self, tid="t1", age_sec=0, timeout_sec=60, attempts=1, max_retries=3, mutex_group="g1"):
        started = (datetime.now() - timedelta(seconds=age_sec)).isoformat(timespec="seconds")
        return {
            "id": tid, "tool": "x", "kwargs": {}, "priority": 5,
            "state": self.tq.STATE_RUNNING, "submitted_at": started,
            "started_at": started, "ended_at": None,
            "attempts": attempts, "max_retries": max_retries,
            "timeout_sec": timeout_sec, "mutex_group": mutex_group,
            "next_run_at": started, "last_error": None,
        }

    def test_fresh_running_not_reaped(self):
        t = self._running(age_sec=5, timeout_sec=60)  # well within timeout + grace
        data = {"tasks": [t], "mutex_holders": {"g1": "t1"}}
        n = self.tq._reap_stale_running(data, datetime.now())
        self.assertEqual(n, 0)
        self.assertEqual(data["tasks"][0]["state"], self.tq.STATE_RUNNING)
        self.assertEqual(data["mutex_holders"], {"g1": "t1"})

    def test_stale_requeued_when_retries_left(self):
        # age = timeout(60) + grace(120) + 30 → past deadline
        t = self._running(age_sec=60 + 120 + 30, timeout_sec=60, attempts=1, max_retries=3)
        data = {"tasks": [t], "mutex_holders": {"g1": "t1"}}
        n = self.tq._reap_stale_running(data, datetime.now())
        self.assertEqual(n, 1)
        rt = data["tasks"][0]
        self.assertEqual(rt["state"], self.tq.STATE_PENDING)
        self.assertIsNone(rt["started_at"])
        self.assertNotIn("g1", data["mutex_holders"])  # mutex freed
        self.assertIn("reaped stale", rt["last_error"])

    def test_stale_dlq_when_retries_exhausted(self):
        t = self._running(age_sec=400, timeout_sec=60, attempts=4, max_retries=3)
        data = {"tasks": [t], "mutex_holders": {"g1": "t1"}}
        with mock.patch.object(self.tq, "_move_to_dlq") as dlq:
            n = self.tq._reap_stale_running(data, datetime.now())
        self.assertEqual(n, 1)
        dlq.assert_called_once()
        self.assertEqual(data["tasks"], [])  # removed from the active queue
        self.assertNotIn("g1", data["mutex_holders"])

    def test_no_started_at_not_reaped(self):
        t = self._running(age_sec=400)
        t["started_at"] = None
        data = {"tasks": [t], "mutex_holders": {}}
        self.assertEqual(self.tq._reap_stale_running(data, datetime.now()), 0)

    def test_only_frees_its_own_mutex(self):
        t = self._running(age_sec=400, mutex_group="g1")
        data = {"tasks": [t], "mutex_holders": {"g1": "t1", "g2": "other"}}
        self.tq._reap_stale_running(data, datetime.now())
        self.assertNotIn("g1", data["mutex_holders"])
        self.assertEqual(data["mutex_holders"].get("g2"), "other")  # untouched

    def test_run_worker_once_invokes_reaper_and_persists(self):
        tq = self.tq
        stale = self._running(tid="z1", age_sec=400, timeout_sec=60, attempts=1, max_retries=3, mutex_group="g1")
        data = {"version": 1, "tasks": [stale], "mutex_holders": {"g1": "z1"}}
        saved = {}

        @contextlib.contextmanager
        def fake_lock():
            yield

        with mock.patch.object(tq, "_queue_lock", fake_lock), \
             mock.patch.object(tq, "_load_queue", return_value=data), \
             mock.patch.object(tq, "_save_queue", side_effect=lambda d: saved.update(d)):
            n = tq.run_worker_once()

        # Reaped task is PENDING with a future next_run_at → not an immediate
        # candidate, so the tick returns 0 — but the reap DID happen and persist.
        self.assertEqual(n, 0)
        z = next(t for t in saved["tasks"] if t["id"] == "z1")
        self.assertEqual(z["state"], tq.STATE_PENDING)
        self.assertNotIn("g1", saved["mutex_holders"])  # group freed for others

    def test_submit_task_reaps_stale_running_before_enforcing_cap(self):
        # A queue full of zombie RUNNING tasks (worker died after claim) must
        # not permanently reject new submissions: submit_task reaps first, so
        # exhausted zombies are DLQ'd out of the active queue and a new task
        # still enqueues. Previously only run_worker_once reaped — which the
        # reaper's own docstring names as the gap.
        tq = self.tq
        zombies = [
            self._running(tid=f"z{i}", age_sec=400, timeout_sec=60,
                          attempts=4, max_retries=3, mutex_group=None)
            for i in range(3)
        ]
        data = {"version": 1, "tasks": zombies, "mutex_holders": {}}
        saved = {}

        @contextlib.contextmanager
        def fake_lock():
            yield

        with mock.patch.object(tq, "_QUEUE_MAX_SIZE", 3), \
             mock.patch.object(tq, "_queue_lock", fake_lock), \
             mock.patch.object(tq, "_load_queue", return_value=data), \
             mock.patch.object(tq, "_save_queue", side_effect=lambda d: saved.update(d)), \
             mock.patch.object(tq, "_move_to_dlq"):
            res = tq.submit_task("send_gmail", {"to": "x@y.z", "subject": "s", "body": "b"})

        self.assertTrue(getattr(res, "ok", False), f"submit should succeed after reap, got: {res}")
        ids = [t["id"] for t in saved["tasks"]]
        self.assertNotIn("z0", ids)  # exhausted zombies removed from active queue
        pending_new = [t for t in saved["tasks"] if t["state"] == tq.STATE_PENDING]
        self.assertEqual(len(pending_new), 1)  # only the freshly-enqueued task

    def test_submit_task_rejects_when_genuinely_full_of_live_tasks(self):
        # Control: a cap full of legit PENDING tasks (nothing to reap) still
        # rejects, so the reap didn't weaken the rate-limit.
        tq = self.tq
        live = [
            {"id": f"p{i}", "tool": "x", "kwargs": {}, "priority": 5,
             "state": tq.STATE_PENDING, "submitted_at": "2020", "started_at": None,
             "ended_at": None, "attempts": 0, "max_retries": 3, "timeout_sec": 60,
             "mutex_group": None, "next_run_at": "2020", "last_error": None}
            for i in range(3)
        ]
        data = {"version": 1, "tasks": live, "mutex_holders": {}}

        @contextlib.contextmanager
        def fake_lock():
            yield

        with mock.patch.object(tq, "_QUEUE_MAX_SIZE", 3), \
             mock.patch.object(tq, "_queue_lock", fake_lock), \
             mock.patch.object(tq, "_load_queue", return_value=data), \
             mock.patch.object(tq, "_save_queue", lambda d: None):
            res = tq.submit_task("send_gmail", {"to": "x@y.z", "subject": "s", "body": "b"})

        self.assertFalse(getattr(res, "ok", True))  # genuinely full → rejected


if __name__ == "__main__":
    unittest.main()
