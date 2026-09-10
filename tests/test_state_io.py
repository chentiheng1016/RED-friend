"""Tests for agent_core.state_io.locked_json.

The whole point of locked_json is to prevent the "lost update" race that
_atomic_write_text doesn't cover: two writers each read v1, each mutate
to v2, last one wins. The interesting tests here use real subprocesses
to prove fcntl actually serializes across processes (threading.Lock
inside one process couldn't catch this regression).
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core.state_io import locked_json


def _writer_increment(path: str, key: str, sleep_inside: float) -> None:
    """Worker for the lost-update test: RMW that holds the lock briefly.

    Sleeping inside the `with` simulates real mutation work and forces
    the second process to actually wait on the lock — without a sleep,
    the operations are too fast to demonstrate the race even when broken.
    """
    from agent_core.state_io import locked_json as li
    with li(path, default={}) as data:
        data[key] = data.get(key, 0) + 1
        time.sleep(sleep_inside)


class LockedJsonBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.path = os.path.join(self.tmpdir, "state.json")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_file_from_default_when_missing(self):
        with locked_json(self.path, default={}) as data:
            self.assertEqual(data, {})
            data["k"] = "v"
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"k": "v"})

    def test_round_trips_existing_content(self):
        with open(self.path, "w") as f:
            json.dump({"a": 1}, f)
        with locked_json(self.path, default={}) as data:
            self.assertEqual(data, {"a": 1})
            data["b"] = 2
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"a": 1, "b": 2})

    def test_corrupt_file_falls_back_to_default(self):
        with open(self.path, "w") as f:
            f.write("<<not json>>")
        with locked_json(self.path, default={"reset": True}) as data:
            self.assertEqual(data, {"reset": True})
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"reset": True})

    def test_exception_inside_block_does_not_persist(self):
        with open(self.path, "w") as f:
            json.dump({"keep": "me"}, f)
        with self.assertRaises(RuntimeError):
            with locked_json(self.path, default={}) as data:
                data["lost"] = "yes"
                raise RuntimeError("boom")
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"keep": "me"})

    def test_lock_file_is_created_and_persists(self):
        with locked_json(self.path, default={}) as data:
            data["x"] = 1
        self.assertTrue(os.path.exists(self.path + ".lock"))

    def test_default_is_deep_copied(self):
        """Mutating the yielded value must not poison the default for next call."""
        shared = {"counter": 0}
        with locked_json(self.path, default=shared) as data:
            data["counter"] = 99
        # On a fresh path the default is used again; should still see 0.
        path2 = os.path.join(self.tmpdir, "fresh.json")
        with locked_json(path2, default=shared) as data:
            self.assertEqual(data["counter"], 0)

    def test_list_default_in_place_append_persists(self):
        """Pattern used by production_tracker — list-shaped state, append in place."""
        with locked_json(self.path, default=[]) as data:
            data.append({"event": "a"})
        with locked_json(self.path, default=[]) as data:
            data.append({"event": "b"})
        with open(self.path) as f:
            self.assertEqual(json.load(f), [{"event": "a"}, {"event": "b"}])

    def test_rebinding_local_name_does_not_persist(self):
        """Documented footgun: `data = new` inside the with block does NOT
        propagate to the helper's reference. Mutate in place instead."""
        with open(self.path, "w") as f:
            json.dump({"keep": "me"}, f)
        with locked_json(self.path, default={}) as data:
            data = {"lost": "yes"}  # noqa: F841 — local rebind, intentional
        # The helper wrote back the ORIGINAL dict, not the rebound one.
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"keep": "me"})


class LockedJsonCrossProcessTests(unittest.TestCase):
    """The race-condition coverage: only meaningful across real processes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.path = os.path.join(self.tmpdir, "state.json")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_writers_all_increments_preserved(self):
        """Without the lock, this fails: N writers race, final count < N.

        With locked_json, all increments are serialized — count == N.
        Hold the lock for 30ms inside each worker so the race window is
        big enough to be observable.
        """
        n_workers = 8
        ctx = mp.get_context("spawn")  # avoid fork-related side effects
        procs = [
            ctx.Process(target=_writer_increment, args=(self.path, "n", 0.03))
            for _ in range(n_workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=15)
            self.assertEqual(p.exitcode, 0, f"worker crashed: {p}")
        with open(self.path) as f:
            final = json.load(f)
        self.assertEqual(
            final.get("n"), n_workers,
            f"lost update! expected {n_workers}, got {final.get('n')}"
        )


if __name__ == "__main__":
    unittest.main()
