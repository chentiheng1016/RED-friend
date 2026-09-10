"""Concurrency tests for agent_core.scheduler.

What we're protecting against:
  1. Two user-facing operations racing → lost update. E.g. two threads each
     calling add_scheduled_task could read the same task list, append their
     own task, and the second writer's save overwrites the first.
  2. Dispatcher's slow cycle clobbering user mid-run adds. The dispatcher
     loads at T0, runs for minutes, and used to save its T0 view at the
     end — anything the user added/removed in between was lost.

The fix is fcntl.flock around the whole read-modify-write
(update_daemon_tasks) plus a merge-save (save_dispatcher_run) that only
touches runtime fields on tasks that still exist by name.

These tests use real files (in tmp dir) and real flocks — not mocks — because
the bug they protect against is in the file-level concurrency primitive itself.
"""
import importlib
import json
import os
import threading
import unittest


class SchedulerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        # Redirect DAEMON_TASKS_FILE to a tmpdir before importing scheduler,
        # so we don't touch the real var/data file.
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmpdir, "daemon_tasks.json")

        # Force a fresh import so module-level DAEMON_TASKS_FILE is re-read.
        # We can't override _SCRIPT_DIR cleanly, so we patch the module's
        # path constants after import.
        import agent_core.scheduler as sched
        importlib.reload(sched)
        self.sched = sched
        self.sched.DAEMON_TASKS_FILE = self.tasks_file
        self.sched.DAEMON_TASKS_LOCK = self.tasks_file + ".lock"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_tasks(self):
        with open(self.tasks_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_concurrent_adds_dont_lose_each_other(self):
        """Two threads adding different tasks at the same time → both survive.

        Without the fcntl lock, a stress run of this test reliably loses
        one of the two adds (read-modify-write race in update_daemon_tasks).
        """
        # Seed file
        self.sched._save_daemon_tasks({"version": 1, "tasks": []})

        # Bypass add_scheduled_task's sanitization (we just need two
        # different mutations racing on the same file).
        N = 20
        errors = []

        def adder(start: int):
            try:
                for i in range(start, start + N):
                    def m(d, name=f"t{i}"):
                        d["tasks"].append({"name": name, "prompt": "x"})
                    self.sched.update_daemon_tasks(m)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=adder, args=(0,))
        t2 = threading.Thread(target=adder, args=(N,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(errors, [])
        final = self._read_tasks()
        names = sorted(t["name"] for t in final["tasks"])
        expected = sorted(f"t{i}" for i in range(2 * N))
        self.assertEqual(names, expected,
                         f"lost updates: got {len(names)} tasks, expected {2*N}")

    def test_save_dispatcher_run_preserves_user_adds(self):
        """Dispatcher loaded [A], user added B during run, save merges → [A,B].

        Reproduces the bug we're fixing: pre-flock, the dispatcher's final
        save would overwrite B with its [A]-only view.
        """
        # Initial state: just task A
        a_task = {"name": "A", "prompt": "a", "run_count": 0,
                  "dedup_hashes": []}
        self.sched._save_daemon_tasks({"version": 1, "tasks": [a_task]})

        # Dispatcher's view at cycle start
        dispatcher_data = self.sched._load_daemon_tasks()

        # Dispatcher runs A, mutates its runtime fields (simulating
        # mark_dispatcher_task_succeeded)
        dispatcher_data["tasks"][0]["run_count"] = 1
        dispatcher_data["tasks"][0]["last_run_at"] = "2026-05-22T16:00:00"

        # Meanwhile, user adds B (via the locked path)
        def add_b(d):
            d["tasks"].append({"name": "B", "prompt": "b"})
        self.sched.update_daemon_tasks(add_b)

        # Dispatcher commits its run
        self.assertTrue(self.sched.save_dispatcher_run(dispatcher_data))

        final = self._read_tasks()
        names = {t["name"]: t for t in final["tasks"]}
        self.assertIn("A", names, "A should still be there")
        self.assertIn("B", names, "B was added by user mid-cycle — must survive merge")
        self.assertEqual(names["A"]["run_count"], 1,
                         "dispatcher's runtime mutation should apply to A")
        self.assertEqual(names["A"]["last_run_at"], "2026-05-22T16:00:00")

    def test_save_dispatcher_run_doesnt_resurrect_removed_tasks(self):
        """User removed task C during dispatcher run → dispatcher's stale view
        of C is dropped, not re-inserted."""
        c_task = {"name": "C", "prompt": "c", "run_count": 0}
        self.sched._save_daemon_tasks({"version": 1, "tasks": [c_task]})

        dispatcher_data = self.sched._load_daemon_tasks()
        dispatcher_data["tasks"][0]["run_count"] = 5  # dispatcher ran C
        dispatcher_data["tasks"][0]["last_run_at"] = "2026-05-22T16:00:00"

        # User removes C
        def remove_c(d):
            d["tasks"] = [t for t in d["tasks"] if t["name"] != "C"]
        self.sched.update_daemon_tasks(remove_c)

        # Dispatcher commits — C should NOT come back
        self.sched.save_dispatcher_run(dispatcher_data)

        final = self._read_tasks()
        names = [t["name"] for t in final["tasks"]]
        self.assertNotIn("C", names,
                         "removed task should NOT be resurrected by dispatcher merge")

    def test_save_dispatcher_run_doesnt_overwrite_user_edits(self):
        """User edited task A's prompt during run → merge keeps the new prompt,
        only updates A's runtime fields."""
        a_old = {"name": "A", "prompt": "old prompt", "run_count": 0,
                 "interval_minutes": 60}
        self.sched._save_daemon_tasks({"version": 1, "tasks": [a_old]})

        dispatcher_data = self.sched._load_daemon_tasks()
        dispatcher_data["tasks"][0]["run_count"] = 1  # dispatcher ran A
        # dispatcher's view still has the OLD prompt — that's the danger zone

        # User edits A's prompt (e.g. via a future edit_scheduled_task)
        def edit_a(d):
            for t in d["tasks"]:
                if t["name"] == "A":
                    t["prompt"] = "NEW PROMPT"
                    t["interval_minutes"] = 30
        self.sched.update_daemon_tasks(edit_a)

        self.sched.save_dispatcher_run(dispatcher_data)

        final = self._read_tasks()
        a = next(t for t in final["tasks"] if t["name"] == "A")
        self.assertEqual(a["prompt"], "NEW PROMPT",
                         "user prompt edit must survive dispatcher merge")
        self.assertEqual(a["interval_minutes"], 30,
                         "user interval edit must survive dispatcher merge")
        self.assertEqual(a["run_count"], 1,
                         "dispatcher's run_count update must apply")


if __name__ == "__main__":
    unittest.main()
