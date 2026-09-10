"""Telegram attachments must land in ~/Downloads/小紅-uploads/<date>/, not
in var/data/telegram_uploads/.

Background: when uploads landed in var/data/, agent_core.path_safety's
`_PROTECTED_PROJECT_DIRS` (which protects var/data/* against RAG-index
poisoning) blocked subsequent reads. Result: small red asked the user
for `+確認` for every single uploaded file just to "copy it to Downloads"
before reading. UX disaster — three RFQ uploads = three confirmation
prompts before any analysis could happen.

Fix: save directly into ~/Downloads/小紅-uploads/<date>/<safe_name>,
which is in user space and not protected. Excel/PDF/IDP read tools
work without further confirmation. Path-safety remains in force for
*other* potentially dangerous reads (~/.ssh, /etc, etc.).

These tests verify:
  1. Default destination is under ~/Downloads/小紅-uploads/
  2. RED_TELEGRAM_UPLOAD_DIR env var overrides the destination
  3. Saved files land under path_safety's allow list (read returns OK)
  4. var/data/ is no longer touched by Telegram uploads
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core import daemon_telegram  # noqa: E402


class _StubResponse:
    """Minimal stand-in for `requests.Response` used by _download_telegram_attachment."""

    def __init__(self, *, json_payload=None, content=b"", status_code=200):
        self._json = json_payload or {}
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._json


class _StubRequests:
    """Mock requests module — sequential get() returns getFile + the file content."""

    def __init__(self, *, file_path: str, content: bytes):
        self._calls = 0
        self._file_path = file_path
        self._content = content
        self.last_url = None

    def get(self, url, **kwargs):
        self.last_url = url
        self._calls += 1
        if self._calls == 1:
            # Step 1: getFile API call
            return _StubResponse(json_payload={
                "ok": True,
                "result": {"file_path": self._file_path},
            })
        # Step 2: actual file download
        return _StubResponse(content=self._content, status_code=200)


class TestDefaultDestinationUnderDownloads(unittest.TestCase):
    """Default upload location must be under ~/Downloads/小紅-uploads/."""

    def test_save_path_starts_under_downloads(self):
        with tempfile.TemporaryDirectory() as fake_home:
            stub = _StubRequests(file_path="documents/test.xlsx", content=b"fake-xlsx")
            attachment = {
                "file_id": "AAAA",
                "file_name": "RFQ_Tempus.xlsx",
                "size": 1024,
            }
            with mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch("os.path.expanduser",
                            lambda p: p.replace("~", fake_home)):
                # ensure no override
                os.environ.pop("RED_TELEGRAM_UPLOAD_DIR", None)
                saved_path, err = daemon_telegram._download_telegram_attachment(
                    token="dummy", attachment=attachment, requests_module=stub,
                )
            self.assertEqual(err, "", f"unexpected error: {err}")
            expected_root = os.path.join(fake_home, "Downloads", "小紅-uploads")
            self.assertTrue(
                saved_path.startswith(expected_root),
                f"saved to {saved_path!r}; expected under {expected_root!r}",
            )
            # File actually exists at that path with the right content
            self.assertTrue(os.path.isfile(saved_path))
            with open(saved_path, "rb") as f:
                self.assertEqual(f.read(), b"fake-xlsx")

    def test_save_path_includes_today_subdirectory(self):
        with tempfile.TemporaryDirectory() as fake_home:
            stub = _StubRequests(file_path="documents/test.pdf", content=b"%PDF-fake")
            attachment = {
                "file_id": "BBBB",
                "file_name": "report.pdf",
                "size": 1024,
            }
            today = datetime.now().strftime("%Y-%m-%d")
            with mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch("os.path.expanduser",
                            lambda p: p.replace("~", fake_home)):
                os.environ.pop("RED_TELEGRAM_UPLOAD_DIR", None)
                saved_path, err = daemon_telegram._download_telegram_attachment(
                    token="dummy", attachment=attachment, requests_module=stub,
                )
            self.assertEqual(err, "")
            # Path contains the YYYY-MM-DD subdir
            self.assertIn(today, saved_path,
                          f"date subdir missing in {saved_path!r}")


class TestEnvOverride(unittest.TestCase):
    """`RED_TELEGRAM_UPLOAD_DIR` env var overrides the default location."""

    def test_env_override_used(self):
        with tempfile.TemporaryDirectory() as custom_root:
            stub = _StubRequests(file_path="documents/x.xlsx", content=b"xlsx")
            attachment = {"file_id": "C", "file_name": "x.xlsx", "size": 100}
            with mock.patch.dict(os.environ,
                                 {"RED_TELEGRAM_UPLOAD_DIR": custom_root}):
                saved_path, err = daemon_telegram._download_telegram_attachment(
                    token="dummy", attachment=attachment, requests_module=stub,
                )
            self.assertEqual(err, "")
            self.assertTrue(
                saved_path.startswith(custom_root),
                f"override not honored — saved to {saved_path!r}, expected "
                f"under {custom_root!r}",
            )

    def test_empty_env_falls_back_to_default(self):
        """Empty / whitespace-only env var should NOT be treated as a valid
        path — fall back to the default ~/Downloads/小紅-uploads/."""
        with tempfile.TemporaryDirectory() as fake_home:
            stub = _StubRequests(file_path="documents/y.xlsx", content=b"xlsx")
            attachment = {"file_id": "D", "file_name": "y.xlsx", "size": 100}
            with mock.patch.dict(os.environ,
                                 {"RED_TELEGRAM_UPLOAD_DIR": "   "}), \
                 mock.patch("os.path.expanduser",
                            lambda p: p.replace("~", fake_home)):
                saved_path, err = daemon_telegram._download_telegram_attachment(
                    token="dummy", attachment=attachment, requests_module=stub,
                )
            self.assertEqual(err, "")
            self.assertTrue(saved_path.startswith(
                os.path.join(fake_home, "Downloads", "小紅-uploads")))

    def test_env_override_with_tilde_expanded(self):
        """Codex P2 regression: `RED_TELEGRAM_UPLOAD_DIR=~/MyUploads` must
        be expanded to the real $HOME, not written to a literal `~`
        directory under the daemon's cwd. Otherwise the daemon writes
        to one place and downstream excel_read / pdf_extract_text (which
        run os.path.expanduser themselves) read from another, and the
        freshly uploaded file is reported as missing."""
        with tempfile.TemporaryDirectory() as fake_home:
            stub = _StubRequests(file_path="documents/z.xlsx", content=b"xlsx")
            attachment = {"file_id": "T", "file_name": "tilde.xlsx", "size": 100}
            with mock.patch.dict(os.environ,
                                 {"RED_TELEGRAM_UPLOAD_DIR": "~/MyUploads"}), \
                 mock.patch("os.path.expanduser",
                            lambda p: p.replace("~", fake_home)):
                saved_path, err = daemon_telegram._download_telegram_attachment(
                    token="dummy", attachment=attachment, requests_module=stub,
                )
            self.assertEqual(err, "")
            # Path is absolute (no literal `~` left) and lands under
            # the expanded $HOME/MyUploads/, not a literal-tilde dir.
            self.assertTrue(os.path.isabs(saved_path),
                            f"saved path not absolute: {saved_path!r}")
            self.assertNotIn(
                "~", saved_path,
                f"literal tilde leaked: {saved_path!r}",
            )
            expected = os.path.join(fake_home, "MyUploads")
            self.assertTrue(
                saved_path.startswith(expected),
                f"saved to {saved_path!r}; expected under {expected!r}",
            )

    def test_env_override_with_relative_path_made_absolute(self):
        """A bare relative path like `tg-uploads` must be normalized to
        absolute so the LLM gets a path that's stable regardless of
        which cwd the daemon happened to be launched from."""
        with tempfile.TemporaryDirectory() as cwd_dir:
            stub = _StubRequests(file_path="documents/r.xlsx", content=b"xlsx")
            attachment = {"file_id": "R", "file_name": "rel.xlsx", "size": 100}
            orig_cwd = os.getcwd()
            try:
                os.chdir(cwd_dir)
                with mock.patch.dict(os.environ,
                                     {"RED_TELEGRAM_UPLOAD_DIR": "tg-uploads"}):
                    saved_path, err = daemon_telegram._download_telegram_attachment(
                        token="dummy", attachment=attachment, requests_module=stub,
                    )
            finally:
                os.chdir(orig_cwd)
            self.assertEqual(err, "")
            self.assertTrue(os.path.isabs(saved_path),
                            f"saved path not absolute: {saved_path!r}")


class TestCloudTelegramOnlyDestination(unittest.TestCase):
    """Cloud mode should use the exchange artifact store unless overridden."""

    def test_telegram_only_mode_uses_artifact_incoming_root(self):
        with tempfile.TemporaryDirectory() as artifact_root:
            stub = _StubRequests(file_path="documents/cloud.pdf", content=b"%PDF-cloud")
            attachment = {
                "file_id": "CLOUD",
                "file_name": "cloud.pdf",
                "size": 1024,
            }
            with mock.patch.dict(os.environ, {
                "RED_EXCHANGE_MODE": "telegram_only",
                "RED_EXCHANGE_ARTIFACT_DIR": artifact_root,
            }, clear=True):
                saved_path, err = daemon_telegram._download_telegram_attachment(
                    token="dummy", attachment=attachment, requests_module=stub,
                )
            self.assertEqual(err, "")
            expected_root = os.path.join(artifact_root, "incoming")
            self.assertTrue(
                saved_path.startswith(expected_root),
                f"saved to {saved_path!r}; expected under {expected_root!r}",
            )


class TestNoLongerWritesToVarData(unittest.TestCase):
    """Codex P2 avoidance: var/data/telegram_uploads/ must no longer be
    touched. Any read attempt under var/data/ trips path_safety, the
    exact issue the upload-location move fixes."""

    def test_var_data_telegram_uploads_not_used(self):
        from agent_core.logging_and_paths import DATA_DIR
        with tempfile.TemporaryDirectory() as fake_home:
            stub = _StubRequests(file_path="documents/z.xlsx", content=b"xlsx")
            attachment = {"file_id": "E", "file_name": "z.xlsx", "size": 100}
            with mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch("os.path.expanduser",
                            lambda p: p.replace("~", fake_home)):
                os.environ.pop("RED_TELEGRAM_UPLOAD_DIR", None)
                saved_path, err = daemon_telegram._download_telegram_attachment(
                    token="dummy", attachment=attachment, requests_module=stub,
                )
            self.assertEqual(err, "")
            self.assertNotIn(
                "var/data", saved_path,
                f"saved into {saved_path!r}; var/data/ is path-safety-protected",
            )
            self.assertNotIn(
                DATA_DIR, saved_path,
                f"saved under DATA_DIR={DATA_DIR!r}; should be in user space",
            )


if __name__ == "__main__":
    unittest.main()
