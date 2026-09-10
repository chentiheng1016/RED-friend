import json
import os
import tempfile
import unittest
from unittest import mock


class GcpSyncSecretsTests(unittest.TestCase):
    def test_oauth_client_config_reads_credentials_file(self):
        import scripts.gcp_sync_secrets as sync

        data = {
            "installed": {
                "client_id": "cid",
                "client_secret": "csecret",
            }
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(data, handle)
            path = handle.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        with mock.patch.object(sync, "CREDENTIALS_FILE", path), \
             mock.patch.object(sync, "_keyring_get", return_value=""), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sync._oauth_client_config(), ("cid", "csecret"))

    def test_secret_values_can_generate_web_secret(self):
        import scripts.gcp_sync_secrets as sync

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write('{"refresh_token":"rt"}')
            token_path = handle.name
        self.addCleanup(lambda: os.path.exists(token_path) and os.unlink(token_path))

        def fake_keyring(_service, name):
            return {
                "gemini-api-key": "AIza" + "x" * 30,
                "telegram-bot-token": "tok",
                "telegram-chat-id": "123",
            }.get(name, "")

        with mock.patch.object(sync, "TOKEN_FILE", token_path), \
             mock.patch.object(sync, "_oauth_client_config", return_value=("cid", "csecret")), \
             mock.patch.object(sync, "_keyring_get", side_effect=fake_keyring), \
             mock.patch.dict(os.environ, {}, clear=True):
            values = sync._secret_values(generate_web_secret=True)

        self.assertEqual(values["google-oauth-token-json"], '{"refresh_token":"rt"}')
        self.assertEqual(values["google-oauth-client-id"], "cid")
        self.assertEqual(values["google-oauth-client-secret"], "csecret")
        self.assertEqual(len(values["web-secret-key"]), 64)


if __name__ == "__main__":
    unittest.main()
