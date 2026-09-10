import os
import unittest
from unittest import mock


class SecretProviderTests(unittest.TestCase):
    def test_env_secret_wins(self):
        from agent_core.secret_provider import get_secret

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "env-value"}, clear=False):
            lookup = get_secret(
                "gemini-api-key",
                env_names=("GEMINI_API_KEY",),
                keyring_name="gemini-api-key",
            )

        self.assertEqual(lookup.value, "env-value")
        self.assertEqual(lookup.source, "env:GEMINI_API_KEY")

    def test_red_secret_env_name_is_supported(self):
        from agent_core.secret_provider import get_secret

        with mock.patch.dict(os.environ, {"RED_SECRET_TELEGRAM_BOT_TOKEN": "tok"}, clear=False):
            lookup = get_secret("telegram-bot-token")

        self.assertEqual(lookup.value, "tok")
        self.assertEqual(lookup.source, "env:RED_SECRET_TELEGRAM_BOT_TOKEN")

    def test_secret_manager_resource_requires_opt_in(self):
        from agent_core import secret_provider

        with mock.patch.dict(os.environ, {"RED_SECRET_PROJECT": "proj"}, clear=True):
            self.assertEqual(secret_provider._secret_resource("gemini-api-key"), "")

        with mock.patch.dict(
            os.environ,
            {"RED_SECRET_MANAGER_ENABLED": "1", "RED_SECRET_PROJECT": "proj"},
            clear=True,
        ):
            self.assertEqual(
                secret_provider._secret_resource("gemini-api-key"),
                "projects/proj/secrets/gemini-api-key/versions/latest",
            )

    def test_cloud_runtime_detection(self):
        from agent_core.secret_provider import cloud_runtime_detected

        with mock.patch.dict(os.environ, {"K_SERVICE": "red-web"}, clear=True):
            self.assertTrue(cloud_runtime_detected())
        with mock.patch.dict(os.environ, {"RED_CLOUD_MODE": "1"}, clear=True):
            self.assertTrue(cloud_runtime_detected())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cloud_runtime_detected())


if __name__ == "__main__":
    unittest.main()
