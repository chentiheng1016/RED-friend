"""Telegram 員工色 bot 自由文字 → dept_nlp_query 的路由測試。

員工（非 red actor）打非指令文字時，不再回「先開放部門查詢指令」的擋板，
改走唯讀 NL 查詢引擎；指令路徑（/dept、/start …）維持優先、不受影響。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

BLUE_EMPLOYEE = {
    "chat_id": "555001",
    "color": "blue",
    "email": "bob@company.example",
    "name": "Bob",
    "source": "employee_registry",
    "is_owner": "false",
}


def _boom_factory():
    raise AssertionError("employee NL path must not build a Gemini session")


class TelegramEmployeeNlpTests(unittest.TestCase):
    def setUp(self):
        import agent_core.daemon_telegram as dt
        self.dt = dt
        dt._tg_chat_states.clear()
        self._env = mock.patch.dict(os.environ, {
            "RED_TOOL_RPC_TELEGRAM": "0",
            "RED_TELEGRAM_DEFAULT_ACTOR_COLOR": "",
            # freeform 關 → 私訊自由文字落到唯讀 NL 引擎（不建全工具 session）。
            # 顯式關掉，免得 live env 的 RED_TG_EMPLOYEE_FREEFORM=all 洩入。
            "RED_TG_EMPLOYEE_FREEFORM": "",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _handle(self, text, actor=BLUE_EMPLOYEE):
        return self.dt.tg_handle_message(
            text,
            agent_persona="p",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=_boom_factory,
            agent_types_factory=_boom_factory,
            chat_id=str(actor.get("chat_id") or ""),
            telegram_actor=actor,
            telegram_message={"chat": {"id": int(actor.get("chat_id") or 0),
                                       "type": "private"}},
        )

    def test_freeform_routes_to_nlp_engine(self):
        with mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            return_value="LURCHI 最新出貨 ETA 是 08-15。",
        ) as m:
            reply = self._handle("LURCHI 的貨什麼時候到？")
        self.assertEqual(reply, "LURCHI 最新出貨 ETA 是 08-15。")
        args, kwargs = m.call_args
        self.assertEqual(args[0], "blue")          # caller = 員工自己的色
        self.assertEqual(args[1], "LURCHI 的貨什麼時候到？")
        self.assertEqual(kwargs.get("channel"), "telegram")
        self.assertEqual(kwargs.get("actor_name"), "Bob")

    def test_engine_crash_falls_back_to_command_hint(self):
        with mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            side_effect=RuntimeError("boom"),
        ):
            reply = self._handle("任何問題")
        self.assertIn("/dept blue", reply)

    def test_dept_command_still_takes_precedence(self):
        with mock.patch(
            "agent_core.agents.telegram_command.handle_dept_command",
            return_value="DEPT OK",
        ), mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
        ) as nlp:
            reply = self._handle('/dept blue query.profile')
        self.assertEqual(reply, "DEPT OK")
        nlp.assert_not_called()

    def test_start_reply_mentions_nlp_for_employee_color(self):
        reply = self.dt._telegram_start_reply({"color": "indigo"})
        self.assertIn("Indigo 倉庫部門", reply)
        self.assertIn("💬", reply)
        self.assertIn("中文問我", reply)

    def test_start_reply_no_hint_for_red(self):
        reply = self.dt._telegram_start_reply({"color": "red"})
        self.assertNotIn("💬", reply)


if __name__ == "__main__":
    unittest.main()
