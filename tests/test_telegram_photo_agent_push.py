"""telegram_send_photo_agent — 部門推播的圖片端（telegram_push_agent 的對稱路徑）。

2026-08-04 UserC 案收尾：要把修好的成品鞋圖傳給 UserC 時發現**文字推得出去、
圖片推不出去** —— `telegram_push_agent` 從員工 registry 解析收件人，而
`telegram_send_photo` 走 `_resolve_chat_id`，只認 `RED_AUTHORIZED_CHAT_IDS`，
而各色 plist 根本沒設這個鍵。等於部門推播的附件端是半殘的（同 #342 UserJ/UserL
收不到主動推播的病，只是犯在圖片端、而且十色都中）。

隔離：全程 mock `_telegram_call_multipart`（不連 Telegram）、mock
`chat_ids_for_agent` / `_get_telegram_chat_id` / `_get_agent_bot_token`
（不讀 live registry 與 keyring）。不 hardcode 任何 /Users/... 路徑。
"""
import os
import tempfile
import unittest
from unittest import mock

from agent_core import telegram as tg


def _png(dirpath: str, name: str = "shoe.png") -> str:
    path = os.path.join(dirpath, name)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return path


class PhotoValidationSharedTests(unittest.TestCase):
    """_validate_photo_for_send 是工具版與推播版共用的那一份規則。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.png = _png(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_png(self):
        abs_path, err, fix = tg._validate_photo_for_send(self.png)
        self.assertEqual((err, fix), ("", ""))
        self.assertEqual(abs_path, os.path.realpath(self.png))

    def test_rejects_non_image_with_fix_hint(self):
        txt = os.path.join(self.tmp.name, "a.txt")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("x")
        abs_path, err, fix = tg._validate_photo_for_send(txt)
        self.assertEqual(abs_path, "")
        self.assertIn(".txt", err)
        self.assertIn("telegram_send_file", fix)

    def test_rejects_oversize_with_fix_hint(self):
        with mock.patch.object(tg, "_TG_PHOTO_MAX_BYTES", 8):
            abs_path, err, fix = tg._validate_photo_for_send(self.png)
        self.assertEqual(abs_path, "")
        self.assertIn("10MB 上限", err)
        self.assertIn("telegram_send_file", fix)

    def test_tool_version_still_returns_toolresult_with_fix(self):
        """共用抽取不能把工具版的 ToolResult 契約改掉（suggested_fix 要還在）。"""
        txt = os.path.join(self.tmp.name, "b.txt")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("x")
        out = tg.telegram_send_photo(txt)
        self.assertFalse(out.ok)
        self.assertIn("telegram_send_file", str(out.suggested_fix))


class SendPhotoAgentTests(unittest.TestCase):
    """收件人只從本地可信設定來；色 bot 優先、永久錯誤才換主 bot。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.png = _png(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *, targets=("9990000004",), color_token="green-tok",
             owner="999", responder=None, **kwargs):
        """responder(token) -> (result, err)；預設一律成功。"""
        def fake_call(method, files, data, timeout=60, token=None, **_kw):
            self.calls.append({"method": method, "chat_id": data.get("chat_id"),
                               "caption": data.get("caption"), "token": token})
            if responder is None:
                return {"message_id": 1}, None
            return responder(token)

        with mock.patch("agent_core.telegram_agent_config.chat_ids_for_agent",
                        return_value=list(targets)), \
             mock.patch.object(tg, "_get_telegram_chat_id", return_value=owner), \
             mock.patch.object(tg, "_get_agent_bot_token", return_value=color_token), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRIES", 1), \
             mock.patch.object(tg, "_TG_UPLOAD_RETRY_SLEEP_S", 0), \
             mock.patch.object(tg.time, "sleep"), \
             mock.patch.object(tg, "_telegram_call_multipart", side_effect=fake_call):
            return tg.telegram_send_photo_agent("green", self.png, **kwargs)

    def test_sends_without_authorized_chat_ids_env(self):
        """本案真因：收件人來自 registry，不該再被 RED_AUTHORIZED_CHAT_IDS 擋。"""
        with mock.patch.dict(os.environ, {"RED_AUTHORIZED_CHAT_IDS": ""}), \
                mock.patch.object(tg, "_resolve_chat_id") as resolve:
            out = self._run()
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual(self.calls[0]["chat_id"], "9990000004")
        resolve.assert_not_called()   # 這條路徑刻意不經授權白名單閘

    def test_uses_color_bot_token(self):
        out = self._run()
        self.assertEqual(self.calls[0]["token"], "green-tok")
        self.assertIn("經 green bot", out)

    def test_caption_and_filename_forwarded(self):
        self._run(caption="修好了，鞋面沒有數字了")
        self.assertEqual(self.calls[0]["caption"], "修好了，鞋面沒有數字了")

    def test_caption_truncated_to_telegram_limit(self):
        self._run(caption="安" * 3000)
        self.assertEqual(len(self.calls[0]["caption"]), tg._TG_CAPTION_MAX)

    def test_falls_back_to_main_bot_on_permanent_error(self):
        """403 = 對方沒 start 過色 bot → 換主 bot 對同一 chat 再試一次。"""
        def responder(token):
            if token == "green-tok":
                return None, "Telegram API 錯誤：HTTP 403: Forbidden: bot was blocked by the user"
            return {"message_id": 2}, None

        out = self._run(responder=responder)
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual([c["token"] for c in self.calls], ["green-tok", None])
        self.assertIn("改由主 bot 送達", out)

    def test_no_main_bot_retry_on_transient_error(self):
        """暫時性錯誤由 _send_photo_with_retries 自己重試，不換手（防雙訊息）。"""
        def responder(_token):
            return None, "Telegram API 錯誤：HTTP 502: Bad Gateway"

        out = self._run(responder=responder)
        self.assertTrue(out.startswith("部分失敗"), out)
        self.assertEqual([c["token"] for c in self.calls], ["green-tok"])

    def test_multi_chat_partial_failure_reported(self):
        def responder(_token):
            # 第一個 chat 成功、第二個永久失敗（且無主 bot 可換 → color_token 空）
            return (({"message_id": 1}, None) if len(self.calls) == 1
                    else (None, "Telegram API 錯誤：HTTP 400: Bad Request: chat not found"))

        out = self._run(targets=("111", "222"), color_token="", responder=responder)
        self.assertTrue(out.startswith("部分失敗"), out)
        self.assertIn("1/2", out)
        self.assertIn("222", out)

    def test_fallback_to_owner_when_color_has_no_chat(self):
        out = self._run(targets=())
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual(self.calls[0]["chat_id"], "999")
        self.assertIn("fallback 到 Red owner", out)

    def test_no_targets_and_no_owner_is_an_error_not_a_silent_noop(self):
        out = self._run(targets=(), owner="")
        self.assertTrue(out.startswith("錯誤"), out)
        self.assertEqual(self.calls, [])

    def test_fallback_can_be_disabled(self):
        out = self._run(targets=(), fallback_to_owner=False)
        self.assertTrue(out.startswith("錯誤"), out)
        self.assertEqual(self.calls, [])

    def test_blank_color_rejected(self):
        self.assertTrue(tg.telegram_send_photo_agent("", self.png).startswith("錯誤"))

    def test_bad_path_rejected_before_resolving_targets(self):
        out = tg.telegram_send_photo_agent("green", "/etc/passwd")
        self.assertTrue(out.startswith("錯誤"), out)

    def test_upload_exception_does_not_escape(self):
        """開檔/網路例外要收成字串回覆，不能炸掉排程 caller。"""
        with mock.patch("agent_core.telegram_agent_config.chat_ids_for_agent",
                        return_value=["111"]), \
                mock.patch.object(tg, "_get_telegram_chat_id", return_value="999"), \
                mock.patch.object(tg, "_get_agent_bot_token", return_value=""), \
                mock.patch.object(tg, "_send_photo_with_retries",
                                  side_effect=OSError("disk gone")):
            out = tg.telegram_send_photo_agent("green", self.png)
        self.assertTrue(out.startswith("部分失敗"), out)
        self.assertIn("OSError", out)


class TextPushAgentUnchangedTests(unittest.TestCase):
    """抽 _agent_push_targets 出來後，文字版行為不能變。"""

    def _push(self, *, targets, owner="999", **kwargs):
        with mock.patch("agent_core.telegram_agent_config.chat_ids_for_agent",
                        return_value=list(targets)), \
             mock.patch.object(tg, "_get_telegram_chat_id", return_value=owner), \
             mock.patch.object(tg, "_get_agent_bot_token", return_value=""), \
             mock.patch.object(tg, "_send_text_to_resolved_chat",
                               return_value=(1, 1, "")):
            return tg.telegram_push_agent("green", "hi", **kwargs)

    def test_normal_push_still_ok(self):
        self.assertTrue(self._push(targets=("111",)).startswith("✅"))

    def test_owner_fallback_note_still_present(self):
        self.assertIn("fallback 到 Red owner", self._push(targets=()))

    def test_no_target_still_errors(self):
        self.assertTrue(self._push(targets=(), owner="").startswith("錯誤"))

    def test_empty_message_still_rejected(self):
        self.assertTrue(tg.telegram_push_agent("green", "  ").startswith("錯誤"))


class NotExposedAsToolTests(unittest.TestCase):
    """⚠️ 部門白名單是唯一閘門、不過 policy_engine —— 這顆不可以進工具目錄。"""

    def test_absent_from_tool_catalog_and_dept_whitelists(self):
        import inspect
        from agent_core import dept_tool_scope, tool_registry_catalog
        for mod in (tool_registry_catalog, dept_tool_scope):
            self.assertNotIn("telegram_send_photo_agent", inspect.getsource(mod),
                             f"{mod.__name__} 不該掛這顆")

    def test_text_sibling_is_also_not_a_tool(self):
        """對照組：telegram_push_agent 一直都只給 daemon 呼叫，不是工具。"""
        import inspect
        from agent_core import tool_registry_catalog
        self.assertNotIn("telegram_push_agent", inspect.getsource(tool_registry_catalog))

    def test_not_registered_in_live_registry(self):
        """最終把關：實際組出來的工具清單裡不能有這兩顆。"""
        from agent_core.tool_registry import tools_list
        names = {getattr(t, "__name__", "") for t in tools_list}
        self.assertNotIn("telegram_send_photo_agent", names)
        self.assertNotIn("telegram_push_agent", names)


if __name__ == "__main__":
    unittest.main()
