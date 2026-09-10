"""Single mutual-exclusion lock for the daemon-redeploy critical section.

Why this exists (2026-06-13 near-miss): two Claude sessions operated on the
main checkout at once — one mid `git merge origin/main` whose post-merge hook
fired `scripts/post_merge_redeploy.py` (a whole-fleet redeploy), the other
about to run `bin/redeploy-daemons` / another merge. Two redeploys racing on
launchctl unload/load plus a working tree being rewritten underneath the
daemons = files vanishing, HEAD jumping between commits, corrupted state.

The fix: every redeploy entrypoint takes THE SAME advisory lock first, so at
most one merge/redeploy critical section runs against the main checkout at a
time. Both entrypoints must agree on the lock, but one is bash and one is
Python:

  * Python (post_merge_redeploy.py)  -> `with deploy_lock.acquire(): ...`
  * bash   (bin/redeploy-daemons)    -> opens var/run/deploy.lock on a fixed
    fd it keeps open, then calls `python -m agent_core.deploy_lock hold-fd <n>`
    to take the flock on that inherited fd.

Both end up calling flock(2) on the SAME inode (var/run/deploy.lock), so they
interoperate regardless of language. flock(2) locks live on the open file
description, not the fd or the process: a child that flocks an fd inherited
from bash and then exits does NOT release the lock, because bash still holds
the same open file description. That property is exactly what lets a
short-lived Python helper lock on behalf of the long-running bash script.

Semantics:
  * Default is fail-fast: if another holder has the lock, raise DeployLockBusy
    (Python) / exit non-zero (CLI) immediately, with a clear message. Callers
    refuse rather than silently serialize, so a second deploy is visibly
    rejected instead of mysteriously hanging.
  * Optional bounded wait: pass wait_seconds > 0 (Python) or --wait SECONDS
    (CLI) to poll until the lock frees or the timeout elapses.

Scope: the lock guards only the redeploy critical section. A normal single
manual redeploy takes the lock, runs, and releases it — it never blocks on
itself. When post_merge_redeploy.py holds the fleet-wide lock and invokes
bin/redeploy-daemons per daemon, it sets RED_DEPLOY_LOCK_HELD=1 so the child
trusts the parent's lock instead of fail-fasting on the very lock its parent
holds.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import os
import sys
import time
from typing import Iterator

# var/run/<this>. The bash side (bin/redeploy-daemons) hard-codes the same
# basename under the same var/run dir; keep them in sync.
LOCK_FILENAME = "deploy.lock"

# Distinct from argparse's exit 2 and a generic exit 1 so a caller can tell
# "lock busy" apart from "bad usage" / "crashed".
BUSY_EXIT_CODE = 3

# flock(2) LOCK_NB failure surfaces as one of these errnos depending on libc.
_WOULDBLOCK_ERRNOS = (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)


class DeployLockBusy(RuntimeError):
    """Raised when the deploy lock is already held by another process."""


def deploy_lock_path() -> str:
    """Absolute path of the shared deploy lock.

    Resolved from logging_and_paths.RUN_DIR (imported lazily) so we honor the
    var/ convention and RED_RUNTIME_DIR instead of hand-joining paths. The
    import is deferred to keep this module cheap to import for the bash
    `hold-fd` path, which operates on an already-open fd and never needs it.
    """
    from agent_core.logging_and_paths import RUN_DIR

    return os.path.join(RUN_DIR, LOCK_FILENAME)


def _flock_until(fd: int, wait_seconds: float) -> None:
    """Take an exclusive flock on ``fd``.

    Non-blocking when ``wait_seconds <= 0`` (raise DeployLockBusy on the first
    contention); otherwise poll until acquired or the deadline passes, then
    raise DeployLockBusy.
    """
    deadline = time.monotonic() + wait_seconds if wait_seconds > 0 else None
    poll = 0.1
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in _WOULDBLOCK_ERRNOS:
                raise
            now = time.monotonic()
            if deadline is None or now >= deadline:
                raise DeployLockBusy(
                    "另一個部署正在進行中（deploy lock 已被持有）"
                ) from exc
            time.sleep(min(poll, deadline - now))


def _write_holder(fd: int, pid: int) -> None:
    """Record the holder pid in the lock file for operator diagnostics.

    Best-effort only: a write hiccup must never sink an already-acquired lock.
    """
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{pid}\n".encode())
        os.fsync(fd)
    except OSError:
        pass


@contextlib.contextmanager
def acquire(*, wait_seconds: float = 0.0,
            lock_path: str | None = None) -> Iterator[int]:
    """Hold the exclusive deploy lock for the body of the ``with`` block.

    Args:
      wait_seconds: 0 (default) fails fast; > 0 polls up to that many seconds.
      lock_path: override the lock file (tests); defaults to deploy_lock_path().

    Raises:
      DeployLockBusy: another process holds the lock (immediately, or after the
        wait timeout).

    Yields the held fd (handy for tests). On exit the lock is released and the
    fd closed; the holder pid written on entry is truncated away.
    """
    if lock_path is None:
        lock_path = deploy_lock_path()
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    # O_CLOEXEC: this fd belongs to us for our whole lifetime — never let it
    # leak into children (subprocess redeploys) and silently extend the lock.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        _flock_until(fd, wait_seconds)
    except BaseException:
        os.close(fd)
        raise
    try:
        _write_holder(fd, os.getpid())
        yield fd
    finally:
        try:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _cmd_hold_fd(fd: int, wait_seconds: float) -> int:
    """Take the lock on an fd inherited from the (bash) caller, then exit.

    The caller (bin/redeploy-daemons) opened var/run/deploy.lock on this fd and
    keeps it open for the whole redeploy; we only need to flock it here. We do
    NOT unlock or close on the way out: the lock rides on the open file
    description the caller still holds, so it stays held until the caller's fd
    closes (script exit). Calling LOCK_UN here would wrongly drop the caller's
    lock.
    """
    try:
        _flock_until(fd, wait_seconds)
    except DeployLockBusy:
        return BUSY_EXIT_CODE
    # The real holder is our parent (bash), not this short-lived helper.
    _write_holder(fd, os.getppid())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_core.deploy_lock",
        description="Shared deploy mutual-exclusion lock helper.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_hold = sub.add_parser(
        "hold-fd",
        help="flock an already-open fd inherited from the caller "
             "(used by bin/redeploy-daemons)",
    )
    p_hold.add_argument("fd", type=int, help="inherited file descriptor number")
    p_hold.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="seconds to wait for the lock before giving up (default: 0 = fail fast)",
    )

    sub.add_parser("path", help="print the deploy lock file path")

    args = parser.parse_args(argv)
    if args.cmd == "hold-fd":
        return _cmd_hold_fd(args.fd, max(0.0, args.wait))
    if args.cmd == "path":
        print(deploy_lock_path())
        return 0
    return 2  # unreachable: subparser is required


if __name__ == "__main__":
    sys.exit(main())
