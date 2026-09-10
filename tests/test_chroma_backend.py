"""Tests for the RED_CHROMA_HTTP_URL backend-selection logic.

build_chroma_client returns either an HttpClient (production, when
RED_CHROMA_HTTP_URL is set) or a PersistentClient — and the PersistentClient
fallback is guarded: if the shared chroma server answers on its canonical
address, direct-opening is refused with RuntimeError (the 2026-06 HNSW
corruption was exactly an env-less process direct-opening the live index).
RED_CHROMA_ALLOW_DIRECT=1 is the explicit override for offline maintenance.

We mock the chromadb module and the heartbeat probe here — actually spinning
up a Chroma server belongs in integration tests, not unit tests. (Stubbing the
probe also keeps these tests hermetic on the production Mac, where the real
server *is* listening on 127.0.0.1:8000.)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core import chroma_backend


class BuildChromaClientTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.persist_dir = os.path.join(self._tmp.name, "chroma_db")
        # Hermetic env: the dev shell may carry either variable.
        env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("RED_CHROMA_HTTP_URL", None)
        os.environ.pop("RED_CHROMA_ALLOW_DIRECT", None)

    def _patch_chromadb(self):
        """Patch the chromadb module (imported inside build_chroma_client)."""
        fake = mock.MagicMock()
        fake.PersistentClient = mock.MagicMock(return_value="PERSISTENT")
        fake.HttpClient = mock.MagicMock(return_value="HTTP")
        return mock.patch.dict(sys.modules, {"chromadb": fake}), fake

    def _patch_probe(self, alive: bool):
        return mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=alive
        )

    # --- backend selection -------------------------------------------------

    def test_default_no_server_returns_persistent_client(self):
        patcher, fake = self._patch_chromadb()
        with patcher, self._patch_probe(False):
            result = chroma_backend.build_chroma_client(self.persist_dir)
        self.assertEqual(result, "PERSISTENT")
        fake.PersistentClient.assert_called_once()
        fake.HttpClient.assert_not_called()

    def test_http_url_returns_http_client(self):
        patcher, fake = self._patch_chromadb()
        os.environ["RED_CHROMA_HTTP_URL"] = "http://127.0.0.1:8000"
        with patcher:
            result = chroma_backend.build_chroma_client(self.persist_dir)
        self.assertEqual(result, "HTTP")
        fake.HttpClient.assert_called_once_with(host="127.0.0.1", port=8000, ssl=False)
        fake.PersistentClient.assert_not_called()

    def test_http_url_skips_probe(self):
        """With the env var set we never reach the fallback — no probe."""
        patcher, _fake = self._patch_chromadb()
        os.environ["RED_CHROMA_HTTP_URL"] = "http://127.0.0.1:8000"
        with patcher, mock.patch.object(
            chroma_backend, "_shared_server_alive"
        ) as probe:
            chroma_backend.build_chroma_client(self.persist_dir)
        probe.assert_not_called()

    def test_https_url_sets_ssl_and_default_port(self):
        patcher, fake = self._patch_chromadb()
        os.environ["RED_CHROMA_HTTP_URL"] = "https://chroma.internal"
        with patcher:
            chroma_backend.build_chroma_client(self.persist_dir)
        fake.HttpClient.assert_called_once_with(
            host="chroma.internal", port=443, ssl=True
        )

    def test_custom_host_and_port_parsed(self):
        patcher, fake = self._patch_chromadb()
        os.environ["RED_CHROMA_HTTP_URL"] = "http://vector-db.lan:9001"
        with patcher:
            chroma_backend.build_chroma_client(self.persist_dir)
        fake.HttpClient.assert_called_once_with(
            host="vector-db.lan", port=9001, ssl=False
        )

    def test_bad_url_scheme_raises(self):
        patcher, _fake = self._patch_chromadb()
        os.environ["RED_CHROMA_HTTP_URL"] = "ftp://nope"
        with patcher:
            with self.assertRaises(ValueError):
                chroma_backend.build_chroma_client(self.persist_dir)

    def test_whitespace_env_value_treated_as_unset(self):
        """Defensive — `unset RED_CHROMA_HTTP_URL` and `set to ''` should both
        fall back to PersistentClient. Some launchd setups produce empty strings."""
        patcher, fake = self._patch_chromadb()
        os.environ["RED_CHROMA_HTTP_URL"] = "   "
        with patcher, self._patch_probe(False):
            chroma_backend.build_chroma_client(self.persist_dir)
        fake.PersistentClient.assert_called_once()
        fake.HttpClient.assert_not_called()

    # --- shared-server guard on the fallback path ---------------------------

    def test_server_alive_without_env_raises(self):
        patcher, fake = self._patch_chromadb()
        with patcher, self._patch_probe(True):
            with self.assertRaises(RuntimeError) as ctx:
                chroma_backend.build_chroma_client(self.persist_dir)
        msg = str(ctx.exception)
        self.assertIn("RED_CHROMA_HTTP_URL", msg)
        self.assertIn("RED_CHROMA_ALLOW_DIRECT", msg)
        fake.PersistentClient.assert_not_called()
        # Refusal must not touch the disk either.
        self.assertFalse(os.path.exists(self.persist_dir))

    def test_server_alive_with_allow_direct_returns_persistent(self):
        patcher, fake = self._patch_chromadb()
        os.environ["RED_CHROMA_ALLOW_DIRECT"] = "1"
        with patcher, self._patch_probe(True):
            result = chroma_backend.build_chroma_client(self.persist_dir)
        self.assertEqual(result, "PERSISTENT")
        fake.PersistentClient.assert_called_once()

    def test_allow_direct_with_server_stopped_returns_persistent(self):
        patcher, fake = self._patch_chromadb()
        os.environ["RED_CHROMA_ALLOW_DIRECT"] = "1"
        with patcher, self._patch_probe(False):
            result = chroma_backend.build_chroma_client(self.persist_dir)
        self.assertEqual(result, "PERSISTENT")
        fake.PersistentClient.assert_called_once()

    def test_memory_facade_still_guarded(self):
        """memory._build_chroma_client delegates here — the guard must hold
        through the facade too (that's the path daemons actually use)."""
        from agent_core import memory

        patcher, _fake = self._patch_chromadb()
        with patcher, self._patch_probe(True):
            with self.assertRaises(RuntimeError):
                memory._build_chroma_client()


class SharedServerProbeTests(unittest.TestCase):
    """_shared_server_alive fail-safe semantics: only 'nobody listening'
    (connection refused) may report not-alive; every ambiguous outcome must
    report alive, because a false 'alive' costs one env var while a false
    'not alive' corrupts the index."""

    def _probe(self, open_effect):
        # 探測走 build_opener(ProxyHandler({}))（繞過 proxy），所以攔
        # OpenerDirector.open 而不是 urlopen。
        with mock.patch("urllib.request.OpenerDirector.open", open_effect):
            return chroma_backend._shared_server_alive("http://127.0.0.1:8000")

    def test_heartbeat_ok_is_alive(self):
        ok = mock.MagicMock()  # context-manager response object
        self.assertTrue(self._probe(mock.MagicMock(return_value=ok)))

    def test_connection_refused_is_not_alive(self):
        refused = URLError(ConnectionRefusedError(61, "Connection refused"))
        self.assertFalse(self._probe(mock.MagicMock(side_effect=refused)))

    def test_http_error_is_alive(self):
        """Non-2xx means *something* is listening on the port — refuse to
        direct-open rather than guess what it is."""
        err = HTTPError("http://127.0.0.1:8000", 404, "Not Found", None, None)
        self.assertTrue(self._probe(mock.MagicMock(side_effect=err)))

    def test_timeout_is_alive(self):
        self.assertTrue(self._probe(mock.MagicMock(side_effect=TimeoutError())))

    def test_url_error_other_reason_is_alive(self):
        weird = URLError(OSError(64, "Host is down"))
        self.assertTrue(self._probe(mock.MagicMock(side_effect=weird)))

    def test_probe_hits_v2_heartbeat_with_timeout(self):
        opened = mock.MagicMock()
        with mock.patch("urllib.request.OpenerDirector.open", opened):
            chroma_backend._shared_server_alive("http://127.0.0.1:8000/", timeout=0.25)
        opened.assert_called_once_with(
            "http://127.0.0.1:8000/api/v2/heartbeat", timeout=0.25
        )

    def test_probe_disables_proxies(self):
        """探測必須用空 ProxyHandler 直連 loopback——http_proxy 環境變數若
        生效，proxy 掛掉時的 connection refused 會被誤讀成「chroma server
        沒在跑」而直開腐壞。"""
        from urllib.request import ProxyHandler

        with mock.patch("urllib.request.build_opener") as builder:
            self.assertTrue(
                chroma_backend._shared_server_alive("http://127.0.0.1:8000")
            )
        (handler,) = builder.call_args.args
        self.assertIsInstance(handler, ProxyHandler)
        self.assertEqual(handler.proxies, {})


class PreflightTests(unittest.TestCase):
    """preflight() is the non-raising contract probe used by monitoring
    (dashboard_alerts) and eager daemon-startup checks. It must classify the
    http/direct mode and shared-server reachability without ever raising."""

    def setUp(self):
        env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("RED_CHROMA_HTTP_URL", None)
        os.environ.pop("RED_CHROMA_ALLOW_DIRECT", None)

    def _patch_probe(self, alive: bool):
        # direct-mode fallback probe (fail-safe semantics)
        return mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=alive
        )

    def _patch_strict(self, ok: bool):
        # http-mode strict monitoring probe (2xx-only)
        return mock.patch.object(chroma_backend, "_heartbeat_ok", return_value=ok)

    def test_http_mode_server_alive_ok(self):
        os.environ["RED_CHROMA_HTTP_URL"] = "http://127.0.0.1:8000"
        with self._patch_strict(True):
            s = chroma_backend.preflight()
        self.assertEqual(s["mode"], "http")
        self.assertTrue(s["server_alive"])
        self.assertTrue(s["ok"])

    def test_http_mode_server_down_not_ok(self):
        os.environ["RED_CHROMA_HTTP_URL"] = "http://127.0.0.1:8000"
        with self._patch_strict(False):
            s = chroma_backend.preflight()
        self.assertEqual(s["mode"], "http")
        self.assertFalse(s["server_alive"])
        self.assertFalse(s["ok"])

    def test_http_mode_bad_scheme_not_ok(self):
        """Missing scheme (127.0.0.1:8000) must read as not-ok for monitoring,
        not be masked by a fail-safe probe (gemini-code-assist finding)."""
        os.environ["RED_CHROMA_HTTP_URL"] = "127.0.0.1:8000"
        s = chroma_backend.preflight()
        self.assertEqual(s["mode"], "http")
        self.assertFalse(s["ok"])

    def test_direct_mode_no_server_is_ok(self):
        """No env + no server = legitimate single-process (dev/CI/offline)."""
        with self._patch_probe(False):
            s = chroma_backend.preflight()
        self.assertEqual(s["mode"], "direct")
        self.assertTrue(s["ok"])

    def test_direct_mode_server_alive_would_refuse(self):
        """No env but server alive → build_chroma_client would RuntimeError."""
        with self._patch_probe(True):
            s = chroma_backend.preflight()
        self.assertEqual(s["mode"], "direct")
        self.assertFalse(s["ok"])

    def test_direct_mode_allow_direct_is_ok(self):
        os.environ["RED_CHROMA_ALLOW_DIRECT"] = "1"
        with self._patch_probe(True):
            s = chroma_backend.preflight()
        self.assertEqual(s["mode"], "direct")
        self.assertTrue(s["ok"])

    def test_never_raises(self):
        os.environ["RED_CHROMA_HTTP_URL"] = "http://127.0.0.1:8000"
        with mock.patch.object(
            chroma_backend, "_heartbeat_ok", side_effect=RuntimeError("boom")
        ):
            s = chroma_backend.preflight()  # must not propagate
        self.assertEqual(s["mode"], "unknown")
        self.assertTrue(s["ok"])


if __name__ == "__main__":
    unittest.main()
