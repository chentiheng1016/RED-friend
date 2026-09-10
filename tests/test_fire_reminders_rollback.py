"""Regression (review finding #10): fire_due_reminders mutated the "fired"
state (cleared next_reminder_at / set escalation_fired_at) under the lock,
which auto-persists on exit — BEFORE the Telegram push, which runs outside the
lock and swallowed all exceptions. A failed push therefore lost a one-shot
reminder / escalation forever. Pin that a failed push rolls back so the
reminder re-fires on the next tick, while a successful push still clears it.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class FireRemindersRollbackTests(unittest.TestCase):
    def setUp(self):
        from agent_core import task_memory as tm

        self.tm = tm
        self._tmp = tempfile.mkdtemp(prefix="task_mem_rollback_")
        self._orig_dir = tm.STATE_DIR
        self._orig_file = tm._TASK_FILE
        tm.STATE_DIR = self._tmp
        tm._TASK_FILE = os.path.join(self._tmp, "task_memory.json")

        def restore():
            tm.STATE_DIR = self._orig_dir
            tm._TASK_FILE = self._orig_file
            shutil.rmtree(self._tmp, ignore_errors=True)

        self.addCleanup(restore)

    def _get(self, tid):
        data = self.tm._load_tasks()
        return next(t for t in data["tasks"] if t["id"] == tid)

    def _set_past_reminder(self, tid, when="2020-01-01T00:00:00"):
        with self.tm._locked_tasks() as data:
            for t in data["tasks"]:
                if t["id"] == tid:
                    t["next_reminder_at"] = when

    def test_failed_push_rolls_back_one_shot_reminder(self):
        tid = self.tm.add_task("test reminder").data["task_id"]
        self._set_past_reminder(tid)
        with mock.patch("agent_core.telegram.telegram_push", side_effect=RuntimeError("tg down")):
            n = self.tm.fire_due_reminders()
        self.assertEqual(n, 0)
        # The reminder must NOT be lost — still due, will retry next tick.
        self.assertEqual(self._get(tid)["next_reminder_at"], "2020-01-01T00:00:00")
        # And a later successful tick fires it and clears it.
        with mock.patch("agent_core.telegram.telegram_push", return_value="ok"):
            n2 = self.tm.fire_due_reminders()
        self.assertEqual(n2, 1)
        self.assertEqual(self._get(tid)["next_reminder_at"], "")

    def test_successful_push_still_clears(self):
        # Happy path unchanged.
        tid = self.tm.add_task("x").data["task_id"]
        self._set_past_reminder(tid)
        with mock.patch("agent_core.telegram.telegram_push", return_value="ok"):
            n = self.tm.fire_due_reminders()
        self.assertEqual(n, 1)
        self.assertEqual(self._get(tid)["next_reminder_at"], "")

    def test_failed_push_rolls_back_escalation(self):
        tid = self.tm.add_task("ship", deadline="2020-01-01").data["task_id"]
        with mock.patch("agent_core.telegram.telegram_push", side_effect=RuntimeError("down")):
            n = self.tm.fire_due_reminders()
        self.assertEqual(n, 0)
        # Not marked escalated → will re-escalate next tick.
        self.assertFalse(self._get(tid).get("escalation_fired_at"))
        with mock.patch("agent_core.telegram.telegram_push", return_value="ok"):
            n2 = self.tm.fire_due_reminders()
        self.assertEqual(n2, 1)
        self.assertTrue(self._get(tid).get("escalation_fired_at"))

    def test_failed_push_rolls_back_recurring_repeat_count(self):
        tid = self.tm.add_task("daily standup").data["task_id"]
        self.tm.set_recurring_reminder(tid, interval_days=1)
        self._set_past_reminder(tid)
        with mock.patch("agent_core.telegram.telegram_push", side_effect=RuntimeError("down")):
            self.tm.fire_due_reminders()
        t = self._get(tid)
        # next_reminder_at restored to the due time AND repeat_count rolled back
        # to 0 (the recurrence advance is undone, not just the timestamp).
        self.assertEqual(t["next_reminder_at"], "2020-01-01T00:00:00")
        self.assertEqual((t.get("recurrence") or {}).get("repeat_count", 0), 0)


if __name__ == "__main__":
    unittest.main()
