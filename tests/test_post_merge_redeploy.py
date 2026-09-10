"""Tests for the post-merge daemon-redeploy hook.

This hook prevents the failure mode that caused the production miss
described in the docstring of scripts/post_merge_redeploy.py: a `git pull`
on main brought new agent_core code, but the long-running telegram daemon
kept executing the pre-merge Python in memory, so a freshly-merged tool
(`search_drive_docs`) was invisible to the LLM until manual restart.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Import via path because scripts/ isn't a package.
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "post_merge_redeploy",
    os.path.join(_REPO_ROOT, "scripts", "post_merge_redeploy.py"),
)
post_merge_redeploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(post_merge_redeploy)


# ── helpers ─────────────────────────────────────────────────────────

def _fake_run(stdout_map: dict[str, str] | None = None,
              fail_for: list[str] | None = None,
              capture_calls: list | None = None):
    """Build a `run`-shaped callable that returns canned stdout per first
    argv token, raises CalledProcessError for tokens in fail_for, and
    optionally records every call into capture_calls."""
    stdout_map = stdout_map or {}
    fail_for = fail_for or []

    def fake(args, *, check=False, capture_output=False, text=False, **kwargs):
        if capture_calls is not None:
            capture_calls.append(list(args))
        # Match by joining args so we can key on e.g. "git rev-parse --git-dir"
        key = " ".join(args)
        for fail_pattern in fail_for:
            if fail_pattern in key:
                if check:
                    raise subprocess.CalledProcessError(1, args)
                return SimpleNamespace(returncode=1, stdout="", stderr="")
        for pattern, out in stdout_map.items():
            if pattern in key:
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
        # Default: success with empty stdout
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake


# ── pure logic: affects_daemons ─────────────────────────────────────

class AffectsDaemonsTests(unittest.TestCase):
    def test_agent_core_change_triggers(self):
        self.assertTrue(post_merge_redeploy.affects_daemons(
            ["agent_core/ingest/drive_search.py"]
        ))

    def test_skills_change_triggers(self):
        self.assertTrue(post_merge_redeploy.affects_daemons(
            ["skills/excel_ops.py"]
        ))

    def test_tool_registry_change_triggers(self):
        # The catalog is at agent_core/tool_registry_catalog.py — covered
        # by both the agent_core/ prefix AND the substring fallback.
        self.assertTrue(post_merge_redeploy.affects_daemons(
            ["agent_core/tool_registry_catalog.py"]
        ))

    def test_unrelated_change_skipped(self):
        self.assertFalse(post_merge_redeploy.affects_daemons([
            "README.md",
            "tests/test_smoke_imports.py",
            "launchd/templates/com.xiaohong.telegram.plist",  # plist alone — redeploy-daemons handles
        ]))

    def test_empty_list_returns_false(self):
        self.assertFalse(post_merge_redeploy.affects_daemons([]))

    def test_mixed_list_with_relevant_change_triggers(self):
        self.assertTrue(post_merge_redeploy.affects_daemons([
            "README.md",
            "agent_core/memory.py",
            "tests/foo.py",
        ]))


# ── is_main_worktree ────────────────────────────────────────────────

class IsMainWorktreeTests(unittest.TestCase):
    def test_main_worktree_returns_true(self):
        run = _fake_run(stdout_map={
            "rev-parse --git-dir":        "/Users/x/repo/.git",
            "rev-parse --git-common-dir": "/Users/x/repo/.git",
        })
        self.assertTrue(post_merge_redeploy.is_main_worktree(run=run))

    def test_secondary_worktree_returns_false(self):
        run = _fake_run(stdout_map={
            "rev-parse --git-dir":        "/Users/x/repo/.git/worktrees/feature",
            "rev-parse --git-common-dir": "/Users/x/repo/.git",
        })
        self.assertFalse(post_merge_redeploy.is_main_worktree(run=run))

    def test_git_failure_returns_false(self):
        # If git is broken/missing we choose to skip (treat as not-main)
        # rather than fire restarts.
        run = _fake_run(fail_for=["rev-parse --git-dir"])
        self.assertFalse(post_merge_redeploy.is_main_worktree(run=run))


# ── changed_files ───────────────────────────────────────────────────

class ChangedFilesTests(unittest.TestCase):
    def test_parses_git_diff_tree_output(self):
        run = _fake_run(stdout_map={
            "diff-tree": "agent_core/ingest/drive_search.py\nREADME.md\n\n",
        })
        self.assertEqual(
            post_merge_redeploy.changed_files("ORIG_HEAD", "HEAD", run=run),
            ["agent_core/ingest/drive_search.py", "README.md"],
        )

    def test_git_failure_returns_empty(self):
        run = _fake_run(fail_for=["diff-tree"])
        self.assertEqual(
            post_merge_redeploy.changed_files("ORIG_HEAD", "HEAD", run=run),
            [],
        )


# ── redeploy ────────────────────────────────────────────────────────

class RedeployTests(unittest.TestCase):
    def test_invokes_redeploy_with_force_flag(self):
        captured: list = []
        run = _fake_run(capture_calls=captured)
        with mock.patch.object(Path, "exists", return_value=True):
            rc = post_merge_redeploy.redeploy(
                "telegram", Path("/repo/bin/redeploy-daemons"), run=run,
            )
        self.assertEqual(rc, 0)
        # --force is mandatory: changing python code without --force only
        # redeploys plist diffs, which is not what we want.
        self.assertEqual(
            captured[0],
            ["/repo/bin/redeploy-daemons", "telegram", "--force"],
        )

    def test_redeploy_suppresses_per_daemon_smoke(self):
        envs: list[dict[str, str]] = []

        def run(args, *, check=False, **kwargs):
            envs.append(kwargs.get("env", {}))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(Path, "exists", return_value=True):
            rc = post_merge_redeploy.redeploy(
                "telegram", Path("/repo/bin/redeploy-daemons"), run=run,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(envs[0].get("RED_SKIP_POST_DEPLOY_SMOKE"), "1")

    def test_missing_redeploy_binary_warns_and_returns_nonzero(self):
        with mock.patch.object(Path, "exists", return_value=False):
            rc = post_merge_redeploy.redeploy(
                "telegram", Path("/missing/bin"), run=_fake_run(),
            )
        self.assertNotEqual(rc, 0)


class PostDeploySmokeTests(unittest.TestCase):
    def test_invokes_red_smoke(self):
        captured: list = []
        run = _fake_run(capture_calls=captured)
        with mock.patch.object(Path, "exists", return_value=True):
            rc = post_merge_redeploy.run_post_deploy_smoke(
                Path("/repo/bin/red-smoke"), run=run,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(captured[0], ["/repo/bin/red-smoke"])

    def test_skip_env_suppresses_red_smoke(self):
        captured: list = []
        run = _fake_run(capture_calls=captured)
        with mock.patch.dict(os.environ, {"RED_SKIP_POST_DEPLOY_SMOKE": "1"}), \
             mock.patch.object(Path, "exists", return_value=True):
            rc = post_merge_redeploy.run_post_deploy_smoke(
                Path("/repo/bin/red-smoke"), run=run,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(captured, [])


# ── main entry point ────────────────────────────────────────────────

class MainTests(unittest.TestCase):
    def setUp(self):
        # main() now redeploys under the shared deploy lock (var/run/deploy.lock).
        # Point it at a throwaway dir so the suite never contends with — or
        # writes into — the real runtime lock.
        import tempfile
        import agent_core.logging_and_paths as _paths
        self._tmp_run = tempfile.mkdtemp(prefix="postmerge_run_")
        self._run_dir_patch = mock.patch.object(_paths, "RUN_DIR", self._tmp_run)
        self._run_dir_patch.start()

    def tearDown(self):
        import shutil
        self._run_dir_patch.stop()
        shutil.rmtree(self._tmp_run, ignore_errors=True)

    def _setup_main_worktree(self, capture):
        return _fake_run(
            stdout_map={
                "rev-parse --git-dir":        "/repo/.git",
                "rev-parse --git-common-dir": "/repo/.git",
                "rev-parse --show-toplevel":  "/repo",
                # diff-tree output is set per-test below by patching
            },
            capture_calls=capture,
        )

    def test_short_circuits_in_worktree(self):
        captured: list = []
        run = _fake_run(
            stdout_map={
                "rev-parse --git-dir":        "/repo/.git/worktrees/feat",
                "rev-parse --git-common-dir": "/repo/.git",
            },
            capture_calls=captured,
        )
        rc = post_merge_redeploy.main(run=run)
        self.assertEqual(rc, 0)
        # Hook should ONLY have asked git about the worktree state — never
        # called git diff-tree, never invoked redeploy.
        for call in captured:
            self.assertNotIn("diff-tree", " ".join(call))
            self.assertNotIn("redeploy-daemons", " ".join(call))

    def test_short_circuits_when_no_relevant_changes(self):
        captured: list = []
        run = _fake_run(
            stdout_map={
                "rev-parse --git-dir":        "/repo/.git",
                "rev-parse --git-common-dir": "/repo/.git",
                "rev-parse --show-toplevel":  "/repo",
                "rev-parse --verify ORIG_HEAD": "abc123",
                "diff-tree": "README.md\nlaunchd/templates/foo.plist\n",
            },
            capture_calls=captured,
        )
        post_merge_redeploy.main(run=run)
        # diff-tree was checked, but redeploy was NOT invoked.
        self.assertTrue(any("diff-tree" in " ".join(c) for c in captured))
        self.assertFalse(any("redeploy-daemons" in " ".join(c) for c in captured))

    def test_short_circuits_when_orig_head_missing(self):
        """Squash-merge / first commit / weird state — bail out silently."""
        captured: list = []
        run = _fake_run(
            stdout_map={
                "rev-parse --git-dir":        "/repo/.git",
                "rev-parse --git-common-dir": "/repo/.git",
                "rev-parse --show-toplevel":  "/repo",
            },
            fail_for=["rev-parse --verify ORIG_HEAD"],
            capture_calls=captured,
        )
        rc = post_merge_redeploy.main(run=run)
        self.assertEqual(rc, 0)
        self.assertFalse(any("redeploy-daemons" in " ".join(c) for c in captured))

    def test_redeploys_when_agent_core_changed(self):
        captured: list = []
        run = _fake_run(
            stdout_map={
                "rev-parse --git-dir":        "/repo/.git",
                "rev-parse --git-common-dir": "/repo/.git",
                "rev-parse --show-toplevel":  "/repo",
                "rev-parse --verify ORIG_HEAD": "abc123",
                "diff-tree": "agent_core/ingest/drive_search.py\n",
            },
            capture_calls=captured,
        )
        with mock.patch.object(Path, "exists", return_value=True):
            rc = post_merge_redeploy.main(run=run)
        self.assertEqual(rc, 0)  # always 0 — merge already happened
        # Each watched daemon got a redeploy call.
        redeploys = [c for c in captured if "redeploy-daemons" in " ".join(c)]
        self.assertEqual(len(redeploys), len(post_merge_redeploy.RESTART_DAEMONS))
        for c in redeploys:
            self.assertIn("--force", c)
        smokes = [c for c in captured if "red-smoke" in " ".join(c)]
        self.assertEqual(smokes, [["/repo/bin/red-smoke"]])

    def test_main_never_returns_nonzero(self):
        """Even if redeploy itself fails, return 0 — the merge happened and
        we don't want the hook to surface a confusing error."""
        captured: list = []
        run = _fake_run(
            stdout_map={
                "rev-parse --git-dir":        "/repo/.git",
                "rev-parse --git-common-dir": "/repo/.git",
                "rev-parse --show-toplevel":  "/repo",
                "rev-parse --verify ORIG_HEAD": "abc123",
                "diff-tree": "agent_core/x.py\n",
            },
            fail_for=["redeploy-daemons"],
            capture_calls=captured,
        )
        with mock.patch.object(Path, "exists", return_value=True):
            rc = post_merge_redeploy.main(run=run)
        self.assertEqual(rc, 0)


class RestartDaemonsCoverageTests(unittest.TestCase):
    """Codex on PR #27 caught that `hotword` was missing from RESTART_DAEMONS
    — same long-running-daemon-with-stale-imports problem the hook is meant
    to solve. Pin the audit method here so future plist additions trigger a
    visible test failure instead of silently inheriting the gap."""

    @staticmethod
    def _is_long_running_with_agent_core(plist_path: str, repo_root: str) -> bool:
        import re
        with open(plist_path) as f:
            text = f.read()
        if "<key>RunAtLoad</key>" not in text or "<true/>" not in text.split("RunAtLoad")[1][:80]:
            return False
        # Cron-style schedule = not long-running for our purposes.
        if "<key>StartCalendarInterval</key>" in text or "<key>StartInterval</key>" in text:
            return False
        # Find the .py script (resolve @@REPO_ROOT@@ template var).
        py_match = re.search(r"<string>([^<]*\.py)</string>", text)
        if not py_match:
            return False
        py_path = py_match.group(1).replace("@@REPO_ROOT@@", repo_root)
        if not os.path.exists(py_path):
            return False
        with open(py_path) as f:
            script = f.read()
        # Direct import OR transitive via agent_daemon.py (which has 20+
        # agent_core imports).
        return ("agent_core" in script) or ("agent_daemon.py" in script
                                              or "import agent_daemon" in script)

    def test_restart_list_covers_every_long_running_agent_core_daemon(self):
        import glob
        plists = sorted(glob.glob(
            os.path.join(_REPO_ROOT, "launchd", "templates",
                         "com.xiaohong.*.plist")
        ))
        expected_set: set[str] = set()
        for plist in plists:
            name = os.path.basename(plist).replace("com.xiaohong.", "").replace(".plist", "")
            if self._is_long_running_with_agent_core(plist, _REPO_ROOT):
                expected_set.add(name)

        actual_set = set(post_merge_redeploy.RESTART_DAEMONS)
        missing = expected_set - actual_set
        self.assertFalse(
            missing,
            f"Long-running daemons that load agent_core but aren't in "
            f"RESTART_DAEMONS: {sorted(missing)}. If a new plist legitimately "
            f"shouldn't be restarted (e.g. cron-only), update the audit "
            f"helper instead of just adding it to the constant.",
        )


class InstallHooksScriptTests(unittest.TestCase):
    """End-to-end test of scripts/install-hooks.sh against synthetic git
    repos. The shell script is what users actually run, so any breakage
    there bypasses every Python test in this file."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="installhooks_")
        # Replicate just enough of the repo layout for the installer.
        scripts_hooks = os.path.join(self.tmp, "scripts", "hooks")
        os.makedirs(scripts_hooks)
        with open(os.path.join(scripts_hooks, "post-merge"), "w") as f:
            f.write("#!/bin/bash\nexit 0\n")
        os.chmod(os.path.join(scripts_hooks, "post-merge"), 0o755)
        # Copy the real installer in so we test the actual script.
        installer_src = os.path.join(_REPO_ROOT, "scripts", "install-hooks.sh")
        shutil.copy2(installer_src, os.path.join(self.tmp, "scripts", "install-hooks.sh"))
        # `git init` so git rev-parse works.
        subprocess.run(["git", "init", "-q", self.tmp], check=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_installer(self, repo_dir):
        return subprocess.run(
            ["bash", os.path.join(repo_dir, "scripts", "install-hooks.sh")],
            cwd=repo_dir, capture_output=True, text=True,
        )

    def test_installs_in_simple_path(self):
        result = self._run_installer(self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = os.path.join(self.tmp, ".git", "hooks", "post-merge")
        self.assertTrue(os.path.islink(target))
        # Must be a RELATIVE symlink so worktree removal can't break it.
        self.assertEqual(os.readlink(target), "../../scripts/hooks/post-merge")

    def test_preserves_users_existing_foreign_symlink(self):
        """Codex P3 on PR #27: if the user already has a custom symlink at
        .git/hooks/post-merge pointing somewhere else, the installer must
        NOT silently overwrite it (the previous logic only protected
        regular files, not foreign symlinks)."""
        # Pre-create a "user's custom" symlink target + symlink at hook dir.
        custom_hook = os.path.join(self.tmp, "my-custom-hook.sh")
        with open(custom_hook, "w") as f:
            f.write("#!/bin/bash\necho 'user custom'\n")
        os.chmod(custom_hook, 0o755)
        hook_dir = os.path.join(self.tmp, ".git", "hooks")
        os.makedirs(hook_dir, exist_ok=True)
        existing_link = os.path.join(hook_dir, "post-merge")
        os.symlink(custom_hook, existing_link)

        result = self._run_installer(self.tmp)
        self.assertEqual(result.returncode, 0)
        # Symlink must STILL point at the user's hook, not our managed target.
        self.assertEqual(os.readlink(existing_link), custom_hook)
        # And the warning message should appear so the user knows we backed off.
        self.assertIn("leaving it alone", result.stdout)

    def test_replaces_stale_managed_symlink(self):
        """Idempotency check: if our own symlink is already there pointing at
        the right relative target, the installer reports 'already installed'
        and doesn't churn it."""
        hook_dir = os.path.join(self.tmp, ".git", "hooks")
        os.makedirs(hook_dir, exist_ok=True)
        existing_link = os.path.join(hook_dir, "post-merge")
        os.symlink("../../scripts/hooks/post-merge", existing_link)

        result = self._run_installer(self.tmp)
        self.assertEqual(result.returncode, 0)
        self.assertIn("already installed", result.stdout)
        self.assertEqual(os.readlink(existing_link), "../../scripts/hooks/post-merge")

    def test_installs_in_path_with_spaces(self):
        """Codex finding on PR #27: xargs dirname word-splits paths with
        whitespace. Move the synthetic repo under a 'has space' directory
        and re-run; the installer must still resolve scripts/hooks/ and
        write the symlink."""
        import shutil
        spaced_root = os.path.join(self.tmp, "parent with space")
        os.makedirs(spaced_root)
        nested_repo = os.path.join(spaced_root, "repo")
        shutil.move(self.tmp + "/.git", nested_repo + "/.git")
        # Move scripts/ into the new repo location too.
        shutil.move(os.path.join(self.tmp, "scripts"), os.path.join(nested_repo, "scripts"))

        result = self._run_installer(nested_repo)
        self.assertEqual(result.returncode, 0,
                         f"stdout={result.stdout}\nstderr={result.stderr}")
        target = os.path.join(nested_repo, ".git", "hooks", "post-merge")
        self.assertTrue(os.path.islink(target),
                        f"hook not installed at {target}")


class MakeInstallHooksTargetTests(unittest.TestCase):
    def test_make_install_hooks_installs_pre_commit_and_versioned_hooks(self):
        """`make install-hooks` is the command documented for developers, so it
        must install both the pre-commit hook and our versioned post-merge hook.
        """
        makefile = Path(_REPO_ROOT, "Makefile").read_text()
        target = makefile.split("\ninstall-hooks:\n", 1)[1].split("\n\n", 1)[0]

        self.assertIn("pre_commit install", target)
        self.assertIn("scripts/install-hooks.sh", target)


if __name__ == "__main__":
    unittest.main()
