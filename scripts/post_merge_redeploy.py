"""Restart long-running daemons when a merge brought new agent_core / skills code.

Real production miss this fixes: PR #25 added a new LLM tool
(`search_drive_docs`), but the telegram daemon never picked it up after the
merge to main — its in-memory Python was still the pre-merge code. The LLM
fell back to filename-only `search_drive_files` and got stuck in a multi-cc
DANGEROUS-tool gate loop trying to parse Excel via `run_python_code`.

Run as a `post-merge` git hook. Decisions made here:

- Skip when invoked from a worktree. Each worktree has its own working tree,
  so a merge there doesn't change the daemon's source files (the daemon
  loads from the main repo's `agent_core/`).
- Skip when nothing under the watched prefixes changed.
- Restart only daemons that load Python *at startup* and stay long-running.
  Cron-driven daemons (`rag_sync_daily`, `email_ingest`, etc.) re-import
  fresh on every run, so they don't need a kick.
- Run one post-deploy smoke after all redeploys finish, not once per daemon.
- Never fail the merge. The merge already happened by the time the hook
  fires; warning loudly is better than confusing the user with an exit
  code that can't be undone.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Run as a git hook (`python scripts/post_merge_redeploy.py`), sys.path[0] is
# scripts/, not the repo root — so `from agent_core import deploy_lock` would
# fail. The script always lives at <repo>/scripts/, so the repo root is two
# directories up; put it on the path so the deploy-lock import below resolves.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Long-running daemons that hold agent_core / skills imports in memory.
# Audit method: launchd/templates/com.xiaohong.*.plist with RunAtLoad=true
# AND (KeepAlive=true OR no Calendar/Interval), cross-referenced against
# `import agent_core` either directly in the script or transitively via
# agent_daemon.py.
#
# Cron-driven daemons (alert_check, ponder, mailcheck, rag_sync_daily, etc.)
# reload Python on every invocation and are intentionally excluded — their
# next scheduled run already picks up the new code for free.
RESTART_DAEMONS = (
    "telegram",           # agent_daemon.py --task telegram_bot
    "telegram_black",     # Black standalone Telegram bot
    "telegram_blue",      # Blue standalone Telegram bot
    "telegram_gray",      # Gray standalone Telegram bot
    "telegram_green",     # Green standalone Telegram bot
    "telegram_indigo",    # Indigo standalone Telegram bot
    "telegram_orange",    # Orange standalone Telegram bot
    "telegram_purple",    # Purple standalone Telegram bot
    "telegram_white",     # White standalone Telegram bot
    "telegram_yellow",    # Yellow standalone Telegram bot
    "web_server",         # direct import of agent_core.daemon_helpers
    "cloudflare_tunnel",  # imports rotate_log + DATA_DIR + telegram_push
    "tool_rpc",           # long-running worker imports agent_core tool modules
)

# Path prefixes whose changes warrant a daemon restart.
WATCHED_PREFIXES = ("agent_core/", "skills/", "tool_registry_catalog")


def is_main_worktree(run=subprocess.run) -> bool:
    """A merge in a feature-branch worktree must NOT restart the main daemon
    — the worktree's working tree is independent from the main checkout that
    the daemon loads from."""
    try:
        git_dir = run(
            ["git", "rev-parse", "--git-dir"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        common = run(
            ["git", "rev-parse", "--git-common-dir"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return os.path.realpath(git_dir) == os.path.realpath(common)


def changed_files(orig_head: str, new_head: str = "HEAD",
                  run=subprocess.run) -> list[str]:
    """Files touched by the merge. Empty list on any git error so the hook
    silently skips when the repo is in an unusual state (first commit, etc)."""
    try:
        out = run(
            ["git", "diff-tree", "-r", "--name-only", "--no-commit-id",
             orig_head, new_head],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return []
    return [line for line in out.splitlines() if line]


def affects_daemons(files: list[str]) -> bool:
    """True iff any changed file lives under a watched prefix."""
    return any(
        f.startswith(prefix) or prefix in f
        for f in files
        for prefix in WATCHED_PREFIXES
    )


def redeploy(daemon: str, redeploy_bin: Path,
             run=subprocess.run, *, lock_held: bool = False) -> int:
    """Returns the redeploy script's exit code, or non-zero on failure to
    even invoke it. We surface but never raise — the merge has already
    landed and we don't want the hook to exit non-zero.

    When lock_held is True we already hold the fleet-wide deploy lock, so the
    child bin/redeploy-daemons must NOT try to take the same lock again (it
    would just fail-fast on the lock its own parent holds)."""
    if not redeploy_bin.exists():
        print(f"[post-merge] WARN: redeploy binary missing: {redeploy_bin}",
              file=sys.stderr)
        return 1
    try:
        env = os.environ.copy()
        # The post-merge hook redeploys daemons one-by-one, so suppress the
        # deploy script's own post-smoke and run one consolidated smoke below.
        env["RED_SKIP_POST_DEPLOY_SMOKE"] = "1"
        if lock_held:
            env["RED_DEPLOY_LOCK_HELD"] = "1"
        result = run(
            [str(redeploy_bin), daemon, "--force"],
            check=False,
            env=env,
        )
        return result.returncode
    except FileNotFoundError as exc:
        print(f"[post-merge] WARN: failed to invoke redeploy ({exc})",
              file=sys.stderr)
        return 1


def run_post_deploy_smoke(smoke_bin: Path,
                          run=subprocess.run) -> int:
    """Run the operator smoke suite once after a successful post-merge deploy.

    Returns the smoke exit code, but callers keep fail-open semantics because
    a post-merge hook cannot usefully undo an already-completed merge.
    """
    skip_smoke = os.environ.get("RED_SKIP_POST_DEPLOY_SMOKE", "").lower()
    if skip_smoke in {"1", "true", "yes"}:
        print("[post-merge] post-deploy smoke skipped by RED_SKIP_POST_DEPLOY_SMOKE",
              file=sys.stderr)
        return 0
    if not smoke_bin.exists():
        print(f"[post-merge] WARN: red-smoke binary missing: {smoke_bin}",
              file=sys.stderr)
        return 1
    try:
        result = run([str(smoke_bin)], check=False)
        return result.returncode
    except FileNotFoundError as exc:
        print(f"[post-merge] WARN: failed to invoke red-smoke ({exc})",
              file=sys.stderr)
        return 1


def main(argv: list[str] | None = None,
         run=subprocess.run) -> int:
    """Hook entry point. Always returns 0 — the merge happened regardless of
    whether daemon restart succeeded."""
    if not is_main_worktree(run=run):
        return 0  # silent on worktree merges

    try:
        repo_root = Path(run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        ).stdout.strip())
    except subprocess.CalledProcessError:
        return 0  # not a git repo somehow — give up silently

    redeploy_bin = repo_root / "bin" / "redeploy-daemons"
    smoke_bin = repo_root / "bin" / "red-smoke"

    # ORIG_HEAD points to the pre-merge HEAD. Git sets it during merge;
    # if absent we have no diff to inspect, so do nothing.
    orig_head = "ORIG_HEAD"
    try:
        run(["git", "rev-parse", "--verify", orig_head],
            check=True, capture_output=True)
    except subprocess.CalledProcessError:
        return 0

    files = changed_files(orig_head, "HEAD", run=run)
    if not affects_daemons(files):
        return 0

    print(
        f"[post-merge] agent_core/skills changed in {len(files)} file(s) "
        f"— redeploying daemons: {', '.join(RESTART_DAEMONS)}",
        file=sys.stderr,
    )
    return _redeploy_fleet(redeploy_bin, smoke_bin, run=run)


def _lock_wait_seconds() -> float:
    """How long to wait for the deploy lock, from RED_DEPLOY_LOCK_WAIT.

    Default 0 = fail fast (skip this round if a deploy is already running). An
    operator can set a positive number of seconds to make rapid back-to-back
    merges queue behind the in-flight deploy instead of skipping."""
    raw = os.environ.get("RED_DEPLOY_LOCK_WAIT", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _redeploy_fleet(redeploy_bin: Path, smoke_bin: Path,
                    run=subprocess.run) -> int:
    """Redeploy every watched daemon + one smoke under the shared deploy lock.

    The lock makes the whole fleet redeploy atomic with respect to any other
    redeploy on the main checkout — a concurrent manual bin/redeploy-daemons,
    or a second post-merge hook from a rapid back-to-back merge (the hook can
    double-fire). If the lock is already held we skip this round: a deploy is
    already in flight and will pick up the just-merged code. We still return 0
    so the hook never fails an already-completed merge.
    """
    try:
        from agent_core import deploy_lock
    except Exception as exc:  # main checkout always has it; stay fail-open
        print(f"[post-merge] WARN: deploy lock 模組不可用（{exc}），改走無鎖"
              "重啟（bin/redeploy-daemons 仍各自上鎖）", file=sys.stderr)
        return _redeploy_fleet_locked(redeploy_bin, smoke_bin, run=run,
                                      lock_held=False)

    try:
        with deploy_lock.acquire(wait_seconds=_lock_wait_seconds()):
            return _redeploy_fleet_locked(redeploy_bin, smoke_bin, run=run,
                                          lock_held=True)
    except deploy_lock.DeployLockBusy:
        print("[post-merge] 另一個部署正在進行中，略過本輪 daemon 重啟"
              "（進行中的部署會載入合併後的程式碼；要改成等待請設 "
              "RED_DEPLOY_LOCK_WAIT=<秒>）", file=sys.stderr)
        return 0


def _redeploy_fleet_locked(redeploy_bin: Path, smoke_bin: Path,
                           run=subprocess.run, *, lock_held: bool) -> int:
    """The redeploy loop proper. Assumes the caller decided about the lock."""
    ok_redeploys = 0
    for daemon in RESTART_DAEMONS:
        rc = redeploy(daemon, redeploy_bin, run=run, lock_held=lock_held)
        if rc == 0:
            ok_redeploys += 1
        status = "✓" if rc == 0 else f"✗ (rc={rc})"
        print(f"[post-merge]   {status} {daemon}", file=sys.stderr)

    if ok_redeploys:
        print("[post-merge] running one post-deploy smoke: ./bin/red-smoke",
              file=sys.stderr)
        smoke_rc = run_post_deploy_smoke(smoke_bin, run=run)
        status = "✓" if smoke_rc == 0 else f"✗ (rc={smoke_rc})"
        print(f"[post-merge]   {status} red-smoke", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
