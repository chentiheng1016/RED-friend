"""Concurrency test for agent_core.tg_auth._persist_message_history.

The race we're protecting against: 11 telegram color daemons each have their
own in-memory _message_history dict (loaded once at module import). Pre-flock,
each call to _persist_message_history would atomically overwrite the whole
file with that one process's view. Daemon A's chat_X entries get clobbered
by daemon B's write of chat_Y. After daemon A restarts, it loads from disk
and finds chat_X missing — a flooder bypasses the rate-limit window.

Post-flock + merge: each process reads disk fresh under lock and overlays
its own chat_id entries; other daemons' chat_id entries are preserved.
We simulate two "daemons" with separate _message_history views and verify
both sets of chat_ids survive concurrent writes.
"""
import importlib
import json
import os
import threading
import unittest


class TgAuthPersistenceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.hist_file = os.path.join(self.tmpdir, "tg_message_history.json")

        import agent_core.tg_auth as ta
        importlib.reload(ta)
        self.ta = ta
        # Redirect the path resolver to our tmpdir
        ta._message_history_path = lambda: self.hist_file

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_disk(self):
        if not os.path.isfile(self.hist_file):
            return {}
        with open(self.hist_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_concurrent_persist_preserves_disjoint_chat_ids(self):
        """Simulate 2 daemons each calling _persist_message_history with
        different chat_ids. Disk should end up with BOTH sets of chat_ids,
        not just whoever wrote last."""
        # Daemon A's view: handles chat_a1, chat_a2
        daemon_a_view = {"chat_a1": [1000.0, 1001.0], "chat_a2": [1002.0]}
        # Daemon B's view: handles chat_b1, chat_b2
        daemon_b_view = {"chat_b1": [2000.0, 2001.0], "chat_b2": [2002.0]}

        errors = []

        def daemon_writer(view: dict, n: int):
            try:
                for _ in range(n):
                    # Each "daemon" mutates the module-level dict to its view
                    # then persists. Under flock, the per-daemon writes merge
                    # instead of clobbering.
                    #
                    # IMPORTANT: in real life each daemon has its OWN module
                    # state in its OWN process, so the dicts don't share
                    # memory. We simulate that by swapping the module-level
                    # dict per call (under the daemon's _state_lock).
                    with self.ta._state_lock:
                        self.ta._message_history.clear()
                        self.ta._message_history.update(view)
                        self.ta._persist_message_history()
            except Exception as e:
                errors.append(e)

        N = 20
        t1 = threading.Thread(target=daemon_writer, args=(daemon_a_view, N))
        t2 = threading.Thread(target=daemon_writer, args=(daemon_b_view, N))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(errors, [])

        disk = self._read_disk()
        # All 4 chat_ids must be on disk regardless of who wrote last
        for cid in ("chat_a1", "chat_a2", "chat_b1", "chat_b2"):
            self.assertIn(cid, disk,
                          f"{cid} missing — last writer clobbered other daemon's chat")
        # Values match expected
        self.assertEqual(disk["chat_a1"], [1000.0, 1001.0])
        self.assertEqual(disk["chat_b1"], [2000.0, 2001.0])


if __name__ == "__main__":
    unittest.main()
