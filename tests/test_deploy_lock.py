"""Tests for the shared deploy mutual-exclusion lock (agent_core/deploy_lock.py).

Background: on 2026-06-13 two sessions nearly ran a `git merge`+post-merge
redeploy and a manual redeploy against the main checkout at once — two
redeploys racing on launchctl and a working tree rewritten underneath the
daemons. These tests pin the invariant that fixes it: at most one redeploy
holds the lock; a second is refused (or waits, if asked).

Coverage:
  * Python acquire(): free → succeeds, second concurrent → DeployLockBusy,
    release frees it, wait-timeout, wait-then-acquire.
  * The `hold-fd` CLI that bin/redeploy-daemons uses, including the crucial
    property that the lock survives the short-lived helper's exit as long as
    the parent keeps the fd open (the exact bash idiom).
  * The bash bin/redeploy-daemons entrypoint end to end, driven entirely
    against temp dirs + a stub launchctl so it never touches the real fleet.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import deploy_lock  # noqa: E402
from agent_core.path_safety import _REPO_ROOT as REPO_ROOT  # noqa: E402


def _flock_held_externally(path: str):
    """Open `path` and take a non-blocking exclusive flock, returning the fd.

    Simulates "another process holds the deploy lock". Caller must close the fd
    (which releases the lock) in tearDown.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


# ── Python acquire() ────────────────────────────────────────────────

class DeployLockAcquireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploylock_")
        self.lock = os.path.join(self.tmp, "deploy.lock")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_succeeds_and_records_pid_when_free(self):
        with deploy_lock.acquire(lock_path=self.lock, wait_seconds=0) as fd:
            self.assertIsInstance(fd, int)
            with open(self.lock, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), str(os.getpid()))

    def test_second_acquire_refused_while_held(self):
        with deploy_lock.acquire(lock_path=self.lock, wait_seconds=0):
            with self.assertRaises(deploy_lock.DeployLockBusy):
                with deploy_lock.acquire(lock_path=self.lock, wait_seconds=0):
                    self.fail("second acquire must not enter while the first holds")

    def test_lock_released_after_context_exit(self):
        with deploy_lock.acquire(lock_path=self.lock, wait_seconds=0):
            pass
        # A fresh acquire must now succeed (and the pid line is truncated away).
        with deploy_lock.acquire(lock_path=self.lock, wait_seconds=0):
            pass
        with open(self.lock, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "")

    def test_wait_times_out_when_lock_stays_held(self):
        held = _flock_held_externally(self.lock)
        try:
            start = time.monotonic()
            with self.assertRaises(deploy_lock.DeployLockBusy):
                with deploy_lock.acquire(lock_path=self.lock, wait_seconds=0.5):
                    self.fail("must not acquire while externally held")
            elapsed = time.monotonic() - start
            # It waited roughly the timeout before giving up (loose bounds to
            # avoid CI flakiness).
            self.assertGreaterEqual(elapsed, 0.4)
            self.assertLess(elapsed, 5.0)
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)

    def test_wait_acquires_once_holder_releases(self):
        holder_fd = _flock_held_externally(self.lock)
        release_after = 0.4

        def _release():
            time.sleep(release_after)
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

        releaser = threading.Thread(target=_release)
        releaser.start()
        try:
            start = time.monotonic()
            with deploy_lock.acquire(lock_path=self.lock, wait_seconds=5.0):
                elapsed = time.monotonic() - start
            # Acquired shortly after the holder let go, well within the budget.
            self.assertGreaterEqual(elapsed, release_after - 0.1)
            self.assertLess(elapsed, 5.0)
        finally:
            releaser.join()


# ── `hold-fd` CLI (what bin/redeploy-daemons calls) ─────────────────

class HoldFdHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploylock_holdfd_")
        self.lock = os.path.join(self.tmp, "deploy.lock")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hold_fd(self, fd: int, *extra):
        """Run `python -m agent_core.deploy_lock hold-fd <fd>` with fd inherited."""
        return subprocess.run(
            [sys.executable, "-m", "agent_core.deploy_lock",
             "hold-fd", str(fd), *extra],
            cwd=REPO_ROOT, pass_fds=[fd],
            capture_output=True, text=True,
        )

    def test_hold_fd_acquires_when_free(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            result = self._run_hold_fd(fd)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.close(fd)

    def test_hold_fd_refused_when_another_holds(self):
        held = _flock_held_externally(self.lock)
        contender = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            result = self._run_hold_fd(contender, "--wait", "0")
            self.assertEqual(result.returncode, deploy_lock.BUSY_EXIT_CODE,
                             result.stderr)
        finally:
            os.close(contender)
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)

    def test_hold_fd_lock_persists_after_helper_exits(self):
        """The bash idiom: a short-lived helper flocks an inherited fd and
        exits, yet the lock stays held because the parent keeps the fd open
        (flock lives on the open file description, freed only on last close)."""
        parent_fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            result = self._run_hold_fd(parent_fd)
            self.assertEqual(result.returncode, 0, result.stderr)

            # Helper has exited, but parent_fd is still open here → an
            # independent fd must NOT be able to take the lock.
            other = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
        finally:
            os.close(parent_fd)

        # Once the parent closes its fd, the lock is finally free.
        freed = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(freed, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(freed, fcntl.LOCK_UN)
        finally:
            os.close(freed)


# ── bin/redeploy-daemons end to end (stubbed launchd) ───────────────

class RedeployDaemonsLockTests(unittest.TestCase):
    """Drive the real bash entrypoint against temp dirs + a stub launchctl so
    the lock behavior is exercised without touching the live fleet."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redeploy_lock_")
        self.runtime = os.path.join(self.tmp, "var")
        self.launchagents = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.launchagents, exist_ok=True)
        self.lock = os.path.join(self.runtime, "run", "deploy.lock")
        self.bin = os.path.join(REPO_ROOT, "bin", "redeploy-daemons")

        # Stub launchctl: record every call; answer `list` with a line that
        # makes _reload_daemon's "is it loaded?" grep succeed.
        self.rec = os.path.join(self.tmp, "launchctl_calls.txt")
        self.stub = os.path.join(self.tmp, "launchctl_stub.sh")
        with open(self.stub, "w") as f:
            f.write(
                "#!/bin/bash\n"
                f'echo "$@" >> "{self.rec}"\n'
                'if [ "$1" = "list" ]; then\n'
                '  printf "\\t12345\\t0\\tcom.xiaohong.telegram\\n"\n'
                "fi\n"
                "exit 0\n"
            )
        os.chmod(self.stub, 0o755)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self):
        env = os.environ.copy()
        env["RED_RUNTIME_DIR"] = self.runtime
        env["RED_LAUNCHCTL_BIN"] = self.stub
        env["RED_LAUNCHAGENTS_DIR"] = self.launchagents
        env["RED_SKIP_POST_DEPLOY_SMOKE"] = "1"
        return env

    def _run(self, *args):
        return subprocess.run(
            [self.bin, *args], env=self._env(),
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

    def _launchctl_calls(self) -> str:
        if not os.path.exists(self.rec):
            return ""
        with open(self.rec, encoding="utf-8") as f:
            return f.read()

    def test_help_needs_no_lock(self):
        result = self._run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dry_run_not_blocked_by_held_lock(self):
        """Read-only --dry-run must run even while a deploy holds the lock."""
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        held = _flock_held_externally(self.lock)
        try:
            result = self._run("--dry-run", "telegram")
            self.assertEqual(result.returncode, 0,
                             f"stdout={result.stdout}\nstderr={result.stderr}")
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)

    def test_real_redeploy_refused_when_lock_held(self):
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        held = _flock_held_externally(self.lock)
        try:
            result = self._run("telegram")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("另一個部署正在進行中", result.stderr)
            # It bailed before touching launchd — the stub was never called.
            self.assertFalse(
                os.path.exists(self.rec),
                f"launchctl stub should not have run; got: {self._launchctl_calls()!r}",
            )
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)

    def test_real_redeploy_succeeds_and_releases_when_free(self):
        result = self._run("telegram")
        self.assertEqual(result.returncode, 0,
                         f"stdout={result.stdout}\nstderr={result.stderr}")
        # The deploy actually ran (lock did not block a normal single redeploy).
        calls = self._launchctl_calls()
        self.assertIn("unload", calls)
        self.assertIn("load", calls)
        # And the lock was released on exit — a fresh acquire must succeed.
        with deploy_lock.acquire(lock_path=self.lock, wait_seconds=0):
            pass


if __name__ == "__main__":
    unittest.main()
