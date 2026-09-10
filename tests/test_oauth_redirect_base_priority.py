"""Tests for app._get_oauth_redirect_base() — Codex P1 finding on PR #33.

The original PR plist hardcoded OAUTH_REDIRECT_BASE=https://portal.company.example.
That made app.py ignore tunnel_url.txt, so when the keyring token was
missing and cloudflared fell back to Quick Tunnel, OAuth stayed broken
(app told Google a Named-Tunnel URL that wasn't actually live) instead
of degrading to the Quick Tunnel URL that cloudflared HAD just written.

Pin the priority contract here so a future plist tweak can't regress it
silently:
  1. OAUTH_REDIRECT_BASE env       — only when explicitly set (escape hatch)
  2. var/data/tunnel_url.txt       — source of truth, written by both modes
  3. http://localhost:8080         — local dev fallback
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")


class OAuthRedirectBasePriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="oauth_redirect_test_")
        self.tunnel_file = os.path.join(self.tmpdir, "tunnel_url.txt")
        # Patch DATA_DIR so the priority-2 path reads from our tmpdir.
        # _get_oauth_redirect_base lazy-imports DATA_DIR inside the function,
        # so we patch the agent_core.logging_and_paths module attribute.
        from agent_core import logging_and_paths
        self._orig_data_dir = logging_and_paths.DATA_DIR
        logging_and_paths.DATA_DIR = self.tmpdir
        # Save + clear env to isolate tests.
        self._orig_env = os.environ.pop("OAUTH_REDIRECT_BASE", None)

    def tearDown(self):
        from agent_core import logging_and_paths
        logging_and_paths.DATA_DIR = self._orig_data_dir
        if self._orig_env is not None:
            os.environ["OAUTH_REDIRECT_BASE"] = self._orig_env
        else:
            os.environ.pop("OAUTH_REDIRECT_BASE", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_tunnel_url(self, url: str) -> None:
        with open(self.tunnel_file, "w", encoding="utf-8") as f:
            f.write(url)

    def _call(self) -> str:
        from agent_core.web_server.app import _get_oauth_redirect_base
        return _get_oauth_redirect_base()

    # ── env-var path ────────────────────────────────────────────────

    def test_env_var_wins_over_tunnel_file(self):
        """Explicit override still works as escape hatch."""
        os.environ["OAUTH_REDIRECT_BASE"] = "https://override.example.com"
        self._write_tunnel_url("https://portal.company.example")
        self.assertEqual(self._call(), "https://override.example.com")

    def test_env_trailing_slash_stripped(self):
        os.environ["OAUTH_REDIRECT_BASE"] = "https://override.example.com/"
        self.assertEqual(self._call(), "https://override.example.com")

    # ── tunnel-file path (the path Codex's fix relies on) ───────────

    def test_tunnel_file_used_when_env_unset(self):
        """The contract that the plist fix depends on: when no env override
        is set, app reads whatever cloudflare_tunnel.py wrote to
        tunnel_url.txt (Named Tunnel hostname OR Quick Tunnel URL — both
        modes write here)."""
        self._write_tunnel_url("https://portal.company.example")
        self.assertEqual(self._call(), "https://portal.company.example")

    def test_quick_tunnel_url_picked_up_when_named_tunnel_unavailable(self):
        """The exact scenario that motivates the Codex fix: keyring token
        missing → cloudflared falls back to Quick Tunnel → tunnel_url.txt
        gets the rotating URL → app must use that, not a stale Named
        Tunnel domain that isn't actually live."""
        self._write_tunnel_url("https://random-name-x9k.trycloudflare.com")
        self.assertEqual(self._call(),
                         "https://random-name-x9k.trycloudflare.com")

    def test_empty_tunnel_file_falls_through_to_localhost(self):
        """Don't return an empty string; fall through to the local-dev
        default so calling code sees a usable URL."""
        self._write_tunnel_url("")
        self.assertEqual(self._call(), "http://localhost:8080")

    def test_tunnel_file_trailing_slash_stripped(self):
        self._write_tunnel_url("https://portal.company.example/")
        self.assertEqual(self._call(), "https://portal.company.example")

    # ── localhost fallback ──────────────────────────────────────────

    def test_localhost_when_neither_env_nor_file(self):
        # No env var, tmpdir has no tunnel_url.txt.
        self.assertFalse(os.path.exists(self.tunnel_file))
        self.assertEqual(self._call(), "http://localhost:8080")

    def test_unreadable_tunnel_file_falls_through(self):
        """File-read exceptions must be swallowed — a corrupted state file
        shouldn't crash the OAuth callback."""
        self._write_tunnel_url("https://portal.company.example")
        # Patch open() to raise inside _get_oauth_redirect_base only.
        # Most surgical: patch os.path.exists to raise, simulating a transient
        # filesystem issue.
        with mock.patch("agent_core.web_server.app.os.path.exists",
                        side_effect=OSError("transient")):
            # Should return localhost fallback, not raise.
            self.assertEqual(self._call(), "http://localhost:8080")


if __name__ == "__main__":
    unittest.main()
