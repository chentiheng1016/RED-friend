"""Tests for cloudflare_tunnel.py token resolution.

Why a dedicated test: the token decides whether main() runs Named Tunnel
(stable hostname → OAuth works) or falls back to Quick Tunnel (rotating
URL → OAuth breaks every restart). Getting the precedence wrong silently
moves the user back to the broken path.

The plist lives in git, so the token MUST come from somewhere not-git.
We mirror the telegram-bot-token / gemini API key pattern: macOS keyring
first, env var second.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_cloudflare_tunnel_module():
    """Load launchd/scripts/cloudflare_tunnel.py via path. It's a script,
    not a package member, so import-by-name doesn't work."""
    sys.modules.pop("_test_cloudflare_tunnel", None)
    path = os.path.join(_REPO_ROOT, "launchd", "scripts", "cloudflare_tunnel.py")
    spec = importlib.util.spec_from_file_location("_test_cloudflare_tunnel", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GetTunnelTokenTests(unittest.TestCase):
    def setUp(self):
        # Prevent ambient env from leaking into tests.
        self._orig_env = os.environ.pop("CLOUDFLARE_TUNNEL_TOKEN", None)
        self.mod = _load_cloudflare_tunnel_module()

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["CLOUDFLARE_TUNNEL_TOKEN"] = self._orig_env
        else:
            os.environ.pop("CLOUDFLARE_TUNNEL_TOKEN", None)
        sys.modules.pop("_test_cloudflare_tunnel", None)

    def test_keyring_value_wins_over_env(self):
        """Keyring first, env second — production should never need to
        export CLOUDFLARE_TUNNEL_TOKEN if the macOS keychain has it."""
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.return_value = "FROM_KEYRING"
        os.environ["CLOUDFLARE_TUNNEL_TOKEN"] = "FROM_ENV"
        with mock.patch.object(self.mod, "keyring", fake_keyring):
            self.assertEqual(self.mod._get_tunnel_token(), "FROM_KEYRING")
        # Confirm we asked the keyring with the right service/user identifiers.
        fake_keyring.get_password.assert_called_once_with(
            self.mod._KEYRING_SERVICE, self.mod._KEYRING_TOKEN_USER,
        )

    def test_env_used_when_keyring_returns_none(self):
        """Keyring miss → fall through to env. Used by non-mac dev
        environments and CI smoke tests."""
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.return_value = None
        os.environ["CLOUDFLARE_TUNNEL_TOKEN"] = "FROM_ENV"
        with mock.patch.object(self.mod, "keyring", fake_keyring):
            self.assertEqual(self.mod._get_tunnel_token(), "FROM_ENV")

    def test_empty_keyring_string_treated_as_miss(self):
        """An empty string saved to keyring (e.g. from a botched setup)
        must NOT be returned — the env fallback is more useful in that
        case than launching cloudflared with an empty token."""
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.return_value = "   "
        os.environ["CLOUDFLARE_TUNNEL_TOKEN"] = "FROM_ENV"
        with mock.patch.object(self.mod, "keyring", fake_keyring):
            self.assertEqual(self.mod._get_tunnel_token(), "FROM_ENV")

    def test_keyring_failure_falls_back_to_env_silently(self):
        """Keyring lookup can raise (locked Keychain, missing entitlement,
        etc.). Must not crash the daemon — fall through to env."""
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.side_effect = RuntimeError("keychain locked")
        os.environ["CLOUDFLARE_TUNNEL_TOKEN"] = "FROM_ENV"
        with mock.patch.object(self.mod, "keyring", fake_keyring):
            self.assertEqual(self.mod._get_tunnel_token(), "FROM_ENV")

    def test_no_keyring_no_env_returns_empty(self):
        """Neither source has the token → empty string, which main() then
        treats as 'fall back to Quick Tunnel'. Don't accidentally return
        None — string-typed contract throughout the script."""
        fake_keyring = mock.MagicMock()
        fake_keyring.get_password.return_value = None
        with mock.patch.object(self.mod, "keyring", fake_keyring):
            self.assertEqual(self.mod._get_tunnel_token(), "")

    def test_keyring_module_unavailable_falls_back_to_env(self):
        """The `keyring` package is optional (try/except ImportError at
        the top of cloudflare_tunnel.py). On systems without it, env
        must still work."""
        os.environ["CLOUDFLARE_TUNNEL_TOKEN"] = "FROM_ENV"
        with mock.patch.object(self.mod, "keyring", None):
            self.assertEqual(self.mod._get_tunnel_token(), "FROM_ENV")


if __name__ == "__main__":
    unittest.main()
