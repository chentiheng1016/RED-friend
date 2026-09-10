import importlib
import os
import unittest
from unittest import mock


class TelegramEnvConfigTests(unittest.TestCase):
    def test_telegram_credentials_can_come_from_env(self):
        from agent_core import telegram

        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "env-token",
                "TELEGRAM_CHAT_ID": "12345",
            },
            clear=False,
        ):
            self.assertEqual(telegram._get_telegram_token(), "env-token")
            self.assertEqual(telegram._get_telegram_chat_id(), "12345")

    def test_telegram_credentials_can_use_custom_secret_names(self):
        from agent_core import daemon_telegram, telegram

        with mock.patch.dict(
            os.environ,
            {
                "RED_TELEGRAM_BOT_TOKEN_SECRET_NAME": "orange-telegram-bot-token",
                "RED_SECRET_ORANGE_TELEGRAM_BOT_TOKEN": "orange-token",
                "RED_TELEGRAM_CHAT_ID_SECRET_NAME": "orange-telegram-chat-id",
                "RED_SECRET_ORANGE_TELEGRAM_CHAT_ID": "24680",
            },
            clear=True,
        ):
            self.assertEqual(telegram._get_telegram_token(), "orange-token")
            self.assertEqual(telegram._get_telegram_chat_id(), "24680")
            self.assertEqual(
                daemon_telegram.tg_get_token_and_chat(),
                ("orange-token", "24680"),
            )

    def test_telegram_state_offset_is_namespaced_for_custom_bot(self):
        from agent_core import daemon_telegram

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                daemon_telegram._telegram_state_key_offset(),
                "telegram_offset",
            )

        with mock.patch.dict(
            os.environ,
            {"RED_TELEGRAM_BOT_TOKEN_SECRET_NAME": "orange-telegram-bot-token"},
            clear=True,
        ):
            self.assertEqual(
                daemon_telegram._telegram_state_key_offset(),
                "telegram_offset:orange-telegram-bot-token",
            )

        with mock.patch.dict(
            os.environ,
            {
                "RED_TELEGRAM_BOT_TOKEN_SECRET_NAME": "orange-telegram-bot-token",
                "RED_TELEGRAM_STATE_SUFFIX": "orange",
            },
            clear=True,
        ):
            self.assertEqual(
                daemon_telegram._telegram_state_key_offset(),
                "telegram_offset:orange",
            )

    def test_default_private_actor_routes_only_private_chats(self):
        from agent_core import daemon_telegram

        env = {
            "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS": "1",
            "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "orange",
            "RED_TELEGRAM_DEFAULT_ACTOR_NAME": "Orange Telegram Bot",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            actor = daemon_telegram._telegram_default_actor_for_message({
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 67890, "username": "alice"},
            })

            self.assertEqual(actor["chat_id"], "12345")
            self.assertEqual(actor["telegram_user_id"], "67890")
            self.assertEqual(actor["color"], "orange")
            self.assertEqual(actor["name"], "Orange Telegram Bot")
            self.assertEqual(actor["source"], "RED_TELEGRAM_DEFAULT_ACTOR_COLOR")

            self.assertEqual(
                daemon_telegram._telegram_default_actor_for_message({
                    "chat": {"id": -10012345, "type": "supergroup"},
                    "from": {"id": 67890},
                }),
                {},
            )

        with mock.patch.dict(
            os.environ,
            {
                "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS": "1",
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "not-a-color",
            },
            clear=True,
        ):
            self.assertEqual(
                daemon_telegram._telegram_default_actor_for_message({
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 67890},
                }),
                {},
            )

    def test_default_private_actor_can_require_owner_approval(self):
        from agent_core import daemon_telegram

        env = {
            "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS": "1",
            "RED_TELEGRAM_REQUIRE_OWNER_APPROVAL_FOR_DEFAULT_PRIVATE_CHATS": "1",
            "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "orange",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                daemon_telegram._telegram_default_actor_for_message({
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 67890, "username": "alice"},
                }),
                {},
            )
            self.assertEqual(
                daemon_telegram._telegram_default_actor_log_summary(),
                "default_private=orange:owner_approval",
            )

    def test_owner_approval_gate_keeps_employee_registry_bindings(self):
        """registry 顯式 telegram_user_id 綁定＝管理員已核准，approval 閘不得丟棄。"""
        import tempfile

        from agent_core import daemon_telegram

        fake_actors = {
            "999": {
                "chat_id": "999", "color": "red", "email": "",
                "name": "Red owner", "source": "telegram-chat-id",
                "is_owner": "true",
            },
            "9990000002": {
                "chat_id": "9990000002", "color": "indigo",
                "email": "warehouse-mgr@example.com", "name": "UserY",
                "source": "employee_registry",
            },
            # registry 身分是 fleet-wide：綁別色的員工在本 bot 也放行
            #（權限隨 actor color，不隨 bot 色）。
            "555": {
                "chat_id": "555", "color": "green",
                "email": "green@example.com", "name": "Green employee",
                "source": "employee_registry",
            },
            "777": {
                "chat_id": "777", "color": "indigo", "email": "", "name": "",
                "source": "RED_TELEGRAM_AGENT_CHATS",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS": "1",
                "RED_TELEGRAM_REQUIRE_OWNER_APPROVAL_FOR_DEFAULT_PRIVATE_CHATS": "1",
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "indigo",
                "RED_TELEGRAM_PRIVATE_APPROVALS_FILE": os.path.join(tmp, "approvals.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                "agent_core.telegram_agent_config.telegram_actors",
                return_value=fake_actors,
            ):
                actors = daemon_telegram._telegram_inbound_actors("999")

        self.assertIn("9990000002", actors)
        self.assertEqual(actors["9990000002"]["color"], "indigo")
        self.assertEqual(actors["9990000002"]["source"], "employee_registry")
        self.assertIn("555", actors)
        self.assertEqual(actors["555"]["color"], "green")
        # 非 registry 的隱式綁定照舊被閘掉
        self.assertNotIn("777", actors)
        self.assertEqual(actors["999"]["is_owner"], "true")

    def test_owner_approval_gate_owner_wins_over_registry_reuse(self):
        """registry 記錄誤用 owner chat_id 時，gated 路徑仍以 owner 身分為準。"""
        import tempfile

        from agent_core import daemon_telegram

        fake_actors = {
            "999": {
                "chat_id": "999", "color": "green", "email": "x@example.com",
                "name": "Mislabeled", "source": "employee_registry",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS": "1",
                "RED_TELEGRAM_REQUIRE_OWNER_APPROVAL_FOR_DEFAULT_PRIVATE_CHATS": "1",
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "indigo",
                "RED_TELEGRAM_PRIVATE_APPROVALS_FILE": os.path.join(tmp, "approvals.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                "agent_core.telegram_agent_config.telegram_actors",
                return_value=fake_actors,
            ):
                actors = daemon_telegram._telegram_inbound_actors("999")

        self.assertEqual(actors["999"]["is_owner"], "true")
        self.assertEqual(actors["999"]["color"], "red")

    def test_no_approval_gate_keeps_all_binding_sources(self):
        """沒開 approval 閘時，registry 與 env 綁定都要照常保留（回歸守門）。"""
        import tempfile

        from agent_core import daemon_telegram

        fake_actors = {
            "9990000002": {
                "chat_id": "9990000002", "color": "indigo",
                "email": "warehouse-mgr@example.com", "name": "UserY",
                "source": "employee_registry",
            },
            "777": {
                "chat_id": "777", "color": "indigo", "email": "", "name": "",
                "source": "RED_TELEGRAM_AGENT_CHATS",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS": "1",
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "indigo",
                "RED_TELEGRAM_PRIVATE_APPROVALS_FILE": os.path.join(tmp, "approvals.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                "agent_core.telegram_agent_config.telegram_actors",
                return_value=fake_actors,
            ):
                actors = daemon_telegram._telegram_inbound_actors("999")

        self.assertIn("9990000002", actors)
        self.assertIn("777", actors)
        self.assertEqual(actors["999"]["is_owner"], "true")

    def test_private_approval_actors_are_namespaced_per_bot(self):
        from agent_core import daemon_telegram

        with mock.patch.dict(
            os.environ,
            {
                "RED_TELEGRAM_STATE_SUFFIX": "orange",
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "orange",
            },
            clear=True,
        ):
            self.assertEqual(daemon_telegram._telegram_agent_namespace(), "orange")

        with mock.patch.dict(
            os.environ,
            {
                "RED_TELEGRAM_STATE_SUFFIX": "green",
                "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "green",
            },
            clear=True,
        ):
            self.assertEqual(daemon_telegram._telegram_agent_namespace(), "green")

    def test_invalid_upload_env_values_fall_back_on_import(self):
        from agent_core import telegram

        self.addCleanup(importlib.reload, telegram)
        with mock.patch.dict(
            os.environ,
            {
                "RED_TG_SPLIT_TARGET_BYTES": "not-an-int",
                "RED_TG_UPLOAD_RETRIES": "not-an-int",
                "RED_TG_UPLOAD_RETRY_SLEEP_S": "nan",
                "RED_TG_MAX_SPLIT_PARTS": "not-an-int",
            },
            clear=False,
        ):
            reloaded = importlib.reload(telegram)

        self.assertEqual(reloaded._TG_SPLIT_TARGET_BYTES, 20 * 1024 * 1024)
        self.assertEqual(reloaded._TG_UPLOAD_RETRIES, 3)
        self.assertEqual(reloaded._TG_UPLOAD_RETRY_SLEEP_S, 2.0)
        self.assertEqual(reloaded._TG_MAX_SPLIT_PARTS, 128)

    def test_upload_env_values_are_clamped(self):
        from agent_core import telegram

        self.addCleanup(importlib.reload, telegram)
        with mock.patch.dict(
            os.environ,
            {
                "RED_TG_SPLIT_TARGET_BYTES": "-1",
                "RED_TG_UPLOAD_RETRIES": "999",
                "RED_TG_UPLOAD_RETRY_SLEEP_S": "999",
                "RED_TG_MAX_SPLIT_PARTS": "9999",
            },
            clear=False,
        ):
            reloaded = importlib.reload(telegram)

        self.assertEqual(reloaded._TG_SPLIT_TARGET_BYTES, 1)
        self.assertEqual(reloaded._TG_UPLOAD_RETRIES, 10)
        self.assertEqual(reloaded._TG_UPLOAD_RETRY_SLEEP_S, 60.0)
        self.assertEqual(reloaded._TG_MAX_SPLIT_PARTS, 1000)

    def test_multipart_non_json_response_is_reported_cleanly(self):
        from agent_core import telegram

        class FakeResponse:
            status_code = 502
            reason = "Bad Gateway"
            text = "<html>bad gateway</html>"

            def json(self):
                raise ValueError("not json")

        with mock.patch.object(telegram, "_get_telegram_token", return_value="tok"), \
             mock.patch.object(telegram.requests, "post", return_value=FakeResponse()):
            result, err = telegram._telegram_call_multipart(
                "sendDocument",
                files={},
                data={"chat_id": "123"},
            )

        self.assertIsNone(result)
        self.assertIn("非 JSON", err)
        self.assertIn("502", err)

    def test_attachment_bad_path_returns_failure_instead_of_crashing(self):
        from agent_core import telegram

        result = telegram.telegram_send_attachment(None)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_input")

    def test_large_file_split_refuses_excessive_part_count(self):
        import tempfile

        from agent_core import telegram

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
            handle.write(b"abcdef")
            path = handle.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        with mock.patch.dict(os.environ, {"RED_EXCHANGE_MODE": "local"}, clear=False), \
             mock.patch.object(telegram, "_TG_DOC_MAX_BYTES", 1), \
             mock.patch.object(telegram, "_TG_MAX_SPLIT_PARTS", 2), \
             mock.patch.object(telegram, "_resolve_chat_id", return_value=("123", "")), \
             mock.patch.object(telegram, "_telegram_call_multipart") as multipart:
            result = telegram.telegram_send_file(path)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_input")
        self.assertIn("安全上限", str(result))
        multipart.assert_not_called()

    def test_telegram_push_agent_sends_to_configured_department_chats(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        # live checkout 的 employee registry 可能把某個真實 chat_id 綁成 orange，
        # 會混進 chat_ids_for_agent("orange") 讓斷言失敗（CI/乾淨 worktree 沒這份
        # registry 才會過）。中性化 registry 來源，只驗 env binding 解析邏輯。
        with mock.patch.dict(
            os.environ,
            {"RED_TELEGRAM_AGENT_CHATS": "orange:222|333"},
            clear=False,
        ), mock.patch.object(
            telegram, "_get_telegram_chat_id", return_value="111"
        ), mock.patch.object(
            telegram, "_telegram_call", return_value=({"message_id": 1}, None)
        ) as call, mock.patch.object(
            cfg, "employee_telegram_actors", return_value={}
        ):
            result = telegram.telegram_push_agent("orange", "hello")

        self.assertIn("✅", result)
        self.assertIn("orange", result)
        sent_chat_ids = {args.args[1]["chat_id"] for args in call.call_args_list}
        self.assertEqual(sent_chat_ids, {"222", "333"})

    def test_telegram_push_agent_falls_back_to_owner_when_unconfigured(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        # 同上一測：中性化 employee registry，免得日後 registry 裡冒出一個
        # green 員工帶 telegram_user_id 就把「未設定」的前提悄悄推翻。
        with mock.patch.dict(os.environ, {"RED_TELEGRAM_AGENT_CHATS": ""}, clear=False), \
             mock.patch.object(telegram, "_get_telegram_chat_id", return_value="111"), \
             mock.patch.object(
                 telegram, "_telegram_call", return_value=({"message_id": 1}, None)
             ) as call, \
             mock.patch.object(cfg, "employee_telegram_actors", return_value={}):
            result = telegram.telegram_push_agent("green", "hello")

        self.assertIn("fallback", result)
        call.assert_called_once()
        self.assertEqual(call.call_args.args[1]["chat_id"], "111")

    def test_telegram_push_agent_prefers_color_bot_token(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        with mock.patch.dict(
            os.environ, {"RED_TELEGRAM_AGENT_CHATS": "indigo:555"}, clear=False,
        ), mock.patch.object(
            telegram, "_get_telegram_chat_id", return_value="111"
        ), mock.patch.object(
            telegram, "_get_agent_bot_token", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_telegram_call", return_value=({"message_id": 1}, None)
        ) as call, mock.patch.object(
            cfg, "employee_telegram_actors", return_value={}
        ):
            result = telegram.telegram_push_agent("indigo", "hello")

        self.assertIn("✅", result)
        self.assertIn("經 indigo bot", result)
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs.get("token"), "COLOR_TOKEN")

    def test_telegram_push_agent_falls_back_to_main_bot_on_permanent_error(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        # 色 bot 403（對方沒 start 過該色 bot / 封鎖）→ 換主 bot token 重試同一 chat。
        def fake_call(method, payload=None, timeout=10, token=None):
            if token == "COLOR_TOKEN":
                return None, "Telegram API 錯誤：HTTP 403: Forbidden: bot was blocked by the user"
            return {"message_id": 1}, None

        with mock.patch.dict(
            os.environ, {"RED_TELEGRAM_AGENT_CHATS": "indigo:555"}, clear=False,
        ), mock.patch.object(
            telegram, "_get_telegram_chat_id", return_value="111"
        ), mock.patch.object(
            telegram, "_get_agent_bot_token", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_telegram_call", side_effect=fake_call
        ) as call, mock.patch.object(
            cfg, "employee_telegram_actors", return_value={}
        ):
            result = telegram.telegram_push_agent("indigo", "hello")

        self.assertIn("✅", result)
        self.assertIn("改由主 bot 送達", result)
        tokens = [c.kwargs.get("token") for c in call.call_args_list]
        self.assertEqual(tokens, ["COLOR_TOKEN", None])

    def test_telegram_push_agent_no_bot_switch_on_transient_error(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        # 暫時性 5xx：照常在色 bot 上重試耗盡即回報失敗，**不能**換主 bot
        # （5xx 下訊息可能已送達，換 bot 重送＝雙訊息風險）。
        def fake_call(method, payload=None, timeout=10, token=None):
            return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"

        with mock.patch.dict(
            os.environ, {"RED_TELEGRAM_AGENT_CHATS": "indigo:555"}, clear=False,
        ), mock.patch.object(
            telegram, "_get_telegram_chat_id", return_value="111"
        ), mock.patch.object(
            telegram, "_get_agent_bot_token", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_TG_UPLOAD_RETRY_SLEEP_S", 0
        ), mock.patch.object(
            telegram, "_telegram_call", side_effect=fake_call
        ) as call, mock.patch.object(
            cfg, "employee_telegram_actors", return_value={}
        ):
            result = telegram.telegram_push_agent("indigo", "hello")

        self.assertIn("部分失敗", result)
        tokens = {c.kwargs.get("token") for c in call.call_args_list}
        self.assertEqual(tokens, {"COLOR_TOKEN"})

    def test_get_agent_bot_token_red_and_empty_use_default(self):
        from agent_core import telegram

        self.assertEqual(telegram._get_agent_bot_token("red"), "")
        self.assertEqual(telegram._get_agent_bot_token(""), "")

    def test_bot_token_for_target_probes_color_bot(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        with mock.patch.object(
            cfg, "telegram_actors",
            return_value={"555": {"chat_id": "555", "color": "indigo"}},
        ), mock.patch.object(
            telegram, "_get_agent_bot_token", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_telegram_call", return_value=({"ok": True}, None)
        ) as call:
            self.assertEqual(telegram._bot_token_for_target("555"), "COLOR_TOKEN")
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0], "sendChatAction")
        self.assertEqual(call.call_args.kwargs.get("token"), "COLOR_TOKEN")

    def test_bot_token_for_target_falls_back_when_probe_fails(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        # 探測 403（對方沒 start 過該色 bot）→ 回空 = 照走主 bot。
        with mock.patch.object(
            cfg, "telegram_actors",
            return_value={"555": {"chat_id": "555", "color": "indigo"}},
        ), mock.patch.object(
            telegram, "_get_agent_bot_token", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_telegram_call",
            return_value=(None, "Telegram API 錯誤：HTTP 403: Forbidden"),
        ):
            self.assertEqual(telegram._bot_token_for_target("555"), "")

    def test_bot_token_for_target_unbound_or_red_skips_probe(self):
        from agent_core import telegram
        from agent_core import telegram_agent_config as cfg

        with mock.patch.object(cfg, "telegram_actors", return_value={}), \
             mock.patch.object(telegram, "_telegram_call") as call:
            self.assertEqual(telegram._bot_token_for_target("999"), "")
        call.assert_not_called()

    def test_telegram_push_uses_dept_bot_token_for_bound_chat(self):
        from agent_core import telegram

        with mock.patch.object(
            telegram, "_resolve_chat_id", return_value=("555", "")
        ), mock.patch.object(
            telegram, "_bot_token_for_target", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_telegram_call", return_value=({"message_id": 1}, None)
        ) as call:
            result = telegram.telegram_push("hello", chat_id="555")

        self.assertIn("✅", result)
        self.assertIn("經部門 bot", result)
        self.assertEqual(call.call_args.kwargs.get("token"), "COLOR_TOKEN")

    def test_telegram_push_owner_default_keeps_main_bot(self):
        from agent_core import telegram

        with mock.patch.object(
            telegram, "_resolve_chat_id", return_value=("111", "")
        ), mock.patch.object(
            telegram, "_bot_token_for_target", return_value=""
        ), mock.patch.object(
            telegram, "_telegram_call", return_value=({"message_id": 1}, None)
        ) as call:
            result = telegram.telegram_push("hello")

        self.assertIn("✅", result)
        self.assertNotIn("經部門 bot", result)
        self.assertIsNone(call.call_args.kwargs.get("token"))

    def test_telegram_send_file_uses_dept_bot_token(self):
        import tempfile

        from agent_core import telegram

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", dir="/tmp", delete=False
        ) as f:
            f.write("hi")
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        with mock.patch.object(
            telegram, "_resolve_chat_id", return_value=("555", "")
        ), mock.patch.object(
            telegram, "_bot_token_for_target", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_telegram_call_multipart",
            return_value=({"message_id": 1}, None),
        ) as multipart:
            result = telegram.telegram_send_file(path)

        self.assertTrue(result.ok)
        multipart.assert_called_once()
        self.assertEqual(multipart.call_args.kwargs.get("token"), "COLOR_TOKEN")

    def test_telegram_send_photo_uses_dept_bot_token(self):
        import tempfile

        from agent_core import telegram

        with tempfile.NamedTemporaryFile(
            "wb", suffix=".png", dir="/tmp", delete=False
        ) as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        with mock.patch.object(
            telegram, "_resolve_chat_id", return_value=("555", "")
        ), mock.patch.object(
            telegram, "_bot_token_for_target", return_value="COLOR_TOKEN"
        ), mock.patch.object(
            telegram, "_telegram_call_multipart",
            return_value=({"message_id": 1}, None),
        ) as multipart:
            result = telegram.telegram_send_photo(path)

        self.assertTrue(result.ok)
        multipart.assert_called_once()
        self.assertEqual(multipart.call_args.kwargs.get("token"), "COLOR_TOKEN")


if __name__ == "__main__":
    unittest.main()
