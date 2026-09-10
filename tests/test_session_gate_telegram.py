"""daemon 端 session gate 整合測試（暫停短路 / 遠端重置清歷史）。

放獨立檔：這裡會 import daemon_telegram（牽動較重的 import 鏈），讓
tests/test_session_registry.py 維持只依賴 session_registry 的輕量可跑性。

重置那條專測 PR #246 review 抓到的真 bug：光把 chat_state["chat"] 設 None
不夠，rebuild 會從磁碟重載歷史（_load_tg_chat_history_for_rebuild），舊
context 復活、reset 形同無效。修法比照 /new：turns 歸零 + _clear_tg_chat_history。
"""
import os
import tempfile
import unittest
from unittest import mock

from agent_core import state_io


class _RegistryIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "reg.json")
        self._prev = os.environ.get("RED_SESSION_REGISTRY_FILE")
        os.environ["RED_SESSION_REGISTRY_FILE"] = self._path

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RED_SESSION_REGISTRY_FILE", None)
        else:
            os.environ["RED_SESSION_REGISTRY_FILE"] = self._prev
        self._tmp.cleanup()

    def _kwargs(self, chat_state, actor=None):
        return dict(
            chat_id="9990000001",
            agent_persona="x",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=lambda: mock.MagicMock(),
            agent_types_factory=lambda: mock.MagicMock(),
            chat_state=chat_state,
            telegram_actor=actor,
        )

    def _fresh_chat_state(self, **over):
        cs = {"chat": mock.MagicMock(), "turns": 5, "last_msg_ts": 0.0, "work_mode": "normal"}
        cs.update(over)
        return cs


class RemoteResetClearsHistoryTests(_RegistryIsolation):
    def test_reset_clears_turns_and_persisted_history(self):
        from agent_core import daemon_telegram, session_registry
        sid = "telegram:9990000001"
        session_registry.touch_session(sid, actor=None)
        session_registry.reset_session(sid)
        chat_state = self._fresh_chat_state()
        # telegram_actor=None → owner-legacy 路徑（與既有 media 測試同場景）。
        with mock.patch.object(daemon_telegram, "_clear_tg_chat_history") as clr, \
                mock.patch.object(daemon_telegram, "_send_message_with_timeout"):
            daemon_telegram.tg_handle_message(
                "https://www.instagram.com/reel/DYDAU67g9KG/ 幫我下載影片",
                **self._kwargs(chat_state),
            )
        clr.assert_called_once_with("9990000001")       # 持久化歷史被清
        self.assertIsNone(chat_state["chat"])            # in-memory handle 清掉
        self.assertEqual(chat_state["turns"], 0)         # turns 歸零
        # reset 旗標 one-shot 消費，不會每則訊息重複清
        self.assertFalse(session_registry.get_session(sid)["reset_pending"])

    def test_no_reset_flag_leaves_history_untouched(self):
        from agent_core import daemon_telegram, session_registry
        sid = "telegram:9990000001"
        session_registry.touch_session(sid, actor=None)   # 沒有 reset
        chat_state = self._fresh_chat_state()
        with mock.patch.object(daemon_telegram, "_clear_tg_chat_history") as clr, \
                mock.patch.object(daemon_telegram, "_send_message_with_timeout"):
            daemon_telegram.tg_handle_message(
                "https://www.instagram.com/reel/DYDAU67g9KG/ 幫我下載影片",
                **self._kwargs(chat_state),
            )
        clr.assert_not_called()                           # 沒重置就不動歷史


class PauseGateTests(_RegistryIsolation):
    NON_OWNER = {"is_owner": "false", "color": "blue", "name": "王小明"}
    OWNER = {"is_owner": "true", "color": "red", "name": "大王"}

    def test_paused_non_owner_short_circuits_before_processing(self):
        from agent_core import daemon_telegram, session_registry
        sid = "telegram:9990000001"
        session_registry.touch_session(sid, actor=self.NON_OWNER)
        session_registry.pause_session(sid, reason="test")
        chat_state = self._fresh_chat_state()
        with mock.patch.object(daemon_telegram, "_clear_tg_chat_history") as clr, \
                mock.patch.object(daemon_telegram, "_send_message_with_timeout") as send:
            reply = daemon_telegram.tg_handle_message(
                "https://www.instagram.com/reel/DYDAU67g9KG/ 幫我下載影片",
                **self._kwargs(chat_state, self.NON_OWNER),
            )
        self.assertEqual(reply, session_registry.paused_notice())
        clr.assert_not_called()
        send.assert_not_called()

    def test_owner_never_blocked_even_if_registry_flagged_paused(self):
        # pause_session 本就拒絕暫停 owner；這裡直接竄改 registry 塞 paused，
        # 驗證 daemon gate 的 is_owner 二次防線仍放行（不會把大王鎖在門外）。
        from agent_core import daemon_telegram, session_registry
        sid = "telegram:9990000001"
        session_registry.touch_session(sid, actor=self.OWNER)
        with state_io.locked_json(self._path, default={}) as reg:
            reg[sid]["status"] = "paused"
        chat_state = self._fresh_chat_state(chat=None)
        with mock.patch.object(daemon_telegram, "_send_message_with_timeout"):
            reply = daemon_telegram.tg_handle_message(
                "https://www.instagram.com/reel/DYDAU67g9KG/ 幫我下載影片",
                **self._kwargs(chat_state, self.OWNER),
            )
        # 沒被短路 → 走到 media 確認路徑（而非暫停通知）
        self.assertNotEqual(reply, session_registry.paused_notice())
        self.assertIn("回覆", reply)


if __name__ == "__main__":
    unittest.main()
