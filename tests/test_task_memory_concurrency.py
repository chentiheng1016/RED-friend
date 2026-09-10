"""Concurrency tests for agent_core.task_memory.

Before the locked_json migration, task_memory used a threading.Lock — which
serializes within ONE process but lets 11 telegram daemons + the REPL race
on task_memory.json. Two daemons could each add a task; whoever wrote last
wins; the other's task silently vanishes.

These tests use real files (in tmp) and real fcntl locks to verify the
cross-process serialization. They start multiple THREADS — not processes —
because each call to locked_json opens its own lock fd, and fcntl.flock(2)
serializes on the fd not the pid, so threads see the same behavior as
separate processes for this primitive.
"""
import importlib
import json
import os
import threading
import unittest


class TaskMemoryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmpdir, "task_memory.json")

        import agent_core.task_memory as tm
        importlib.reload(tm)
        self.tm = tm
        # Re-point the module-level path constant. _locked_tasks closes over
        # _TASK_FILE via its locked_json call, so we patch the module attribute.
        self.tm._TASK_FILE = self.tasks_file

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_tasks(self):
        with open(self.tasks_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_concurrent_adds_dont_lose_each_other(self):
        """Two threads adding tasks via _locked_tasks → both survive.

        Stress: 30 ops per thread × 2 threads = 60 expected, all preserved.
        Without the fcntl lock this test reliably loses ~half the ops.
        """
        N = 30
        errors = []

        def adder(prefix: str):
            try:
                for i in range(N):
                    tid = f"{prefix}_{i}"
                    with self.tm._locked_tasks() as data:
                        data["tasks"].append({"id": tid, "status": "pending"})
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=adder, args=("a",))
        t2 = threading.Thread(target=adder, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(errors, [])
        final = self._read_tasks()
        ids = sorted(t["id"] for t in final["tasks"])
        expected = sorted([f"a_{i}" for i in range(N)] + [f"b_{i}" for i in range(N)])
        self.assertEqual(ids, expected,
                         f"lost {len(expected) - len(ids)} tasks under concurrent add")

    def test_concurrent_status_updates_serialize(self):
        """Two threads each completing a different task → both end up as done.

        Without serialization, one thread's RMW can clobber the other's,
        leaving one task stuck in 'pending'.
        """
        # Seed with two tasks
        with self.tm._locked_tasks() as data:
            data["tasks"].extend([
                {"id": "t1", "status": "pending", "log": []},
                {"id": "t2", "status": "pending", "log": []},
            ])

        errors = []

        def completer(task_id: str):
            try:
                for _ in range(20):
                    with self.tm._locked_tasks() as data:
                        for t in data["tasks"]:
                            if t["id"] == task_id:
                                t["status"] = "done"
                                t.setdefault("log", []).append("done")
                                break
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=completer, args=("t1",))
        t2 = threading.Thread(target=completer, args=("t2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(errors, [])
        final = self._read_tasks()
        by_id = {t["id"]: t for t in final["tasks"]}
        self.assertEqual(by_id["t1"]["status"], "done")
        self.assertEqual(by_id["t2"]["status"], "done")

    def test_locked_tasks_yields_shape_on_missing_file(self):
        """Fresh tmpdir → no file yet → _locked_tasks yields {"version": 1,
        "tasks": []} not raw {}. Verifies the setdefault guard in the wrapper."""
        self.assertFalse(os.path.exists(self.tasks_file))
        with self.tm._locked_tasks() as data:
            self.assertIn("tasks", data)
            self.assertEqual(data["tasks"], [])
            data["tasks"].append({"id": "first", "status": "pending"})

        final = self._read_tasks()
        self.assertEqual(len(final["tasks"]), 1)
        self.assertEqual(final["tasks"][0]["id"], "first")

    def test_locked_tasks_skips_write_on_exception(self):
        """If the with-block raises, locked_json must NOT persist the partial
        mutation. Otherwise a half-failed operation poisons the file."""
        # Seed
        with self.tm._locked_tasks() as data:
            data["tasks"].append({"id": "good", "status": "pending"})

        # Attempt a mutation that raises mid-way
        with self.assertRaises(RuntimeError):
            with self.tm._locked_tasks() as data:
                data["tasks"].append({"id": "should-not-persist",
                                       "status": "pending"})
                raise RuntimeError("simulated mid-RMW failure")

        # The failed mutation should NOT be on disk
        final = self._read_tasks()
        ids = [t["id"] for t in final["tasks"]]
        self.assertIn("good", ids)
        self.assertNotIn("should-not-persist", ids,
                         "exception inside with-block must not leak partial state")


if __name__ == "__main__":
    unittest.main()
