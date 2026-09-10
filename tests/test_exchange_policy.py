from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from agent_core import exchange_policy


class ExchangeModeTests(unittest.TestCase):
    def test_default_mode_is_local(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(exchange_policy.exchange_mode(), "local")
            self.assertFalse(exchange_policy.is_telegram_only_mode())

    def test_cloud_mode_implies_telegram_only(self):
        with mock.patch.dict(os.environ, {"RED_CLOUD_MODE": "1"}, clear=True):
            self.assertEqual(exchange_policy.exchange_mode(), "telegram_only")
            self.assertTrue(exchange_policy.is_telegram_only_mode())

    def test_explicit_telegram_only_mode(self):
        with mock.patch.dict(os.environ, {"RED_EXCHANGE_MODE": "telegram-only"}, clear=True):
            self.assertEqual(exchange_policy.exchange_mode(), "telegram_only")
            self.assertTrue(exchange_policy.is_telegram_only_mode())


class UploadRootTests(unittest.TestCase):
    def test_local_default_stays_under_downloads(self):
        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch("os.path.expanduser", lambda p: p.replace("~", fake_home)):
                root = exchange_policy.telegram_upload_root()
        self.assertEqual(
            root,
            os.path.join(fake_home, "Downloads", "小紅-uploads"),
        )

    def test_telegram_only_default_uses_artifact_incoming(self):
        with tempfile.TemporaryDirectory() as artifact_root:
            with mock.patch.dict(
                os.environ,
                {
                    "RED_EXCHANGE_MODE": "telegram_only",
                    "RED_EXCHANGE_ARTIFACT_DIR": artifact_root,
                },
                clear=True,
            ):
                root = exchange_policy.telegram_upload_root()
        self.assertEqual(root, os.path.join(artifact_root, "incoming"))

    def test_upload_override_wins_even_in_telegram_only_mode(self):
        with tempfile.TemporaryDirectory() as override:
            with mock.patch.dict(
                os.environ,
                {
                    "RED_EXCHANGE_MODE": "telegram_only",
                    "RED_TELEGRAM_UPLOAD_DIR": override,
                },
                clear=True,
            ):
                self.assertEqual(exchange_policy.telegram_upload_root(), override)


class ToolFilterTests(unittest.TestCase):
    def test_local_mode_leaves_tools_unchanged(self):
        def open_dashboard_in_browser():
            pass

        def telegram_push():
            pass

        tools = [open_dashboard_in_browser, telegram_push]
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(exchange_policy.filter_tools_for_exchange_mode(tools), tools)

    def test_telegram_only_mode_removes_alternate_exchange_surfaces(self):
        def open_dashboard_in_browser():
            pass

        def show_notification():
            pass

        def telegram_push():
            pass

        tools = [open_dashboard_in_browser, show_notification, telegram_push]
        with mock.patch.dict(os.environ, {"RED_EXCHANGE_MODE": "telegram_only"}, clear=True):
            filtered = exchange_policy.filter_tools_for_exchange_mode(tools)

        self.assertEqual(filtered, [telegram_push])


class ArtifactStagingTests(unittest.TestCase):
    def test_stage_artifact_records_public_url_and_manifest(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as src_dir:
            source = os.path.join(src_dir, "hello report.pdf")
            with open(source, "wb") as f:
                f.write(b"hello")
            with mock.patch.dict(
                os.environ,
                {
                    "RED_EXCHANGE_ARTIFACT_DIR": root,
                    "RED_OBJECT_BASE_URL": "https://files.example.test/base",
                },
                clear=True,
            ):
                artifact = exchange_policy.stage_artifact_for_exchange(
                    source,
                    purpose="telegram-outgoing",
                )
                manifest = os.path.join(root, "_manifest.jsonl")

            self.assertTrue(os.path.isfile(artifact.stored_path))
            self.assertEqual(artifact.size_bytes, 5)
            self.assertIn("telegram-outgoing", artifact.stored_path)
            self.assertTrue(artifact.public_url.startswith("https://files.example.test/base/"))
            self.assertIn("hello_report.pdf", artifact.public_url)
            self.assertTrue(os.path.isfile(manifest))
            with open(manifest, "r", encoding="utf-8") as f:
                self.assertIn(artifact.sha256, f.read())

    def test_status_warns_when_telegram_only_without_public_url(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(
                os.environ,
                {
                    "RED_EXCHANGE_MODE": "telegram_only",
                    "RED_EXCHANGE_ARTIFACT_DIR": root,
                },
                clear=True,
            ):
                status = exchange_policy.exchange_policy_status()
        self.assertIn("Telegram-only: yes", status)
        self.assertIn("warning", status)


class TelegramLargeFileDeliveryTests(unittest.TestCase):
    def test_large_file_in_telegram_only_mode_sends_object_link(self):
        from agent_core import telegram

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as src_dir:
            source = os.path.join(src_dir, "big.bin")
            with open(source, "wb") as f:
                f.write(b"large-enough-for-test")

            with mock.patch.dict(
                os.environ,
                {
                    "RED_EXCHANGE_MODE": "telegram_only",
                    "RED_EXCHANGE_ARTIFACT_DIR": root,
                    "RED_OBJECT_BASE_URL": "https://files.example.test/red",
                },
                clear=True,
            ), \
                 mock.patch.object(telegram, "_TG_DOC_MAX_BYTES", 1), \
                 mock.patch.object(telegram, "_resolve_chat_id", return_value=("123", "")), \
                 mock.patch.object(telegram, "_telegram_call", return_value=({"message_id": 1}, None)) as call, \
                 mock.patch.object(telegram, "_telegram_call_multipart") as multipart:
                result = telegram.telegram_send_file(source, caption="測試大檔")

            self.assertTrue(result.ok, result)
            self.assertEqual(result.data["delivery"], "object_link")
            self.assertTrue(result.data["url"].startswith("https://files.example.test/red/"))
            self.assertTrue(os.path.isfile(result.artifacts[1]))
            call.assert_called_once()
            multipart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
