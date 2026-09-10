"""channel_context：通道 contextvar、工具包覆、tg_build_chat 產生端。

2026-09-02 green 聊天室 5004 案：大王在部門色 agent 聊天室渲染樣品單，圖卻走
telegram_send_photo 出站推送、從紅 bot 主對話冒出來。交付面工具要靠這顆
contextvar 得知「這次呼叫來自 Telegram 對話」→ 改走 [[TG_PHOTO:]] 回覆附圖；
REPL / 背景 daemon 沒有消費標記的回覆送出點 → 預設空通道＝維持推送。

注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown；
mock.patch 一律在主執行緒。
"""
import os
import sys
import threading
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from tests.test_telegram_user_separation import (  # noqa: E402
    _FakeClient, _fake_types_factory,
)


class ChannelContextTests(unittest.TestCase):
    def test_default_channel_is_empty_and_markers_off(self):
        from agent_core.channel_context import (
            current_channel, reply_consumes_tg_markers,
        )
        self.assertEqual(current_channel(), "")
        self.assertFalse(reply_consumes_tg_markers())

    def test_context_sets_and_resets(self):
        from agent_core.channel_context import (
            channel_context, current_channel, reply_consumes_tg_markers,
        )
        with channel_context("telegram"):
            self.assertEqual(current_channel(), "telegram")
            self.assertTrue(reply_consumes_tg_markers())
        self.assertEqual(current_channel(), "")

    def test_context_resets_on_exception(self):
        from agent_core.channel_context import channel_context, current_channel
        with self.assertRaises(RuntimeError):
            with channel_context("telegram"):
                raise RuntimeError("boom")
        self.assertEqual(current_channel(), "")

    def test_non_telegram_channel_does_not_enable_markers(self):
        from agent_core.channel_context import (
            channel_context, reply_consumes_tg_markers,
        )
        with channel_context("repl"):
            self.assertFalse(reply_consumes_tg_markers())


class WrapToolsWithChannelTests(unittest.TestCase):
    def test_channel_active_only_during_call(self):
        from agent_core.channel_context import (
            current_channel, wrap_tools_with_channel,
        )
        seen: list[str] = []

        def probe(x: int) -> str:
            seen.append(current_channel())
            return f"got {x}"

        wrapped = wrap_tools_with_channel([probe], "telegram")[0]
        self.assertEqual(wrapped(7), "got 7")
        self.assertEqual(seen, ["telegram"])
        self.assertEqual(current_channel(), "")   # 呼叫外不殘留

    def test_wrapper_preserves_dispatch_surface(self):
        """genai 派發吃 __name__/signature/annotations；marker attrs 供各層過濾。"""
        import inspect

        from agent_core.channel_context import wrap_tools_with_channel

        def probe(x: int, y: str = "a") -> str:
            """docstring 本文"""
            return y * x

        probe._actor_scoped = True   # tool_proxy 的 by-attr 判斷要能穿透
        wrapped = wrap_tools_with_channel([probe], "telegram")[0]
        self.assertEqual(wrapped.__name__, "probe")
        self.assertEqual(wrapped.__doc__, "docstring 本文")
        self.assertEqual(str(inspect.signature(wrapped)),
                         str(inspect.signature(probe)))
        self.assertEqual(wrapped.__annotations__, probe.__annotations__)
        self.assertTrue(wrapped._actor_scoped)
        self.assertEqual(wrapped._channel_context, "telegram")

    def test_channel_visible_from_worker_thread(self):
        """genai AFC 在 worker thread 執行工具 —— context 綁在包裝上，跨線程也生效。"""
        from agent_core.channel_context import (
            current_channel, wrap_tools_with_channel,
        )
        seen: list[str] = []
        done = threading.Event()

        def probe():
            seen.append(current_channel())
            done.set()

        wrapped = wrap_tools_with_channel([probe], "telegram")[0]
        t = threading.Thread(target=wrapped)
        t.start()
        t.join(timeout=5)
        self.assertTrue(done.is_set())
        self.assertEqual(seen, ["telegram"])


class BuildChatChannelTests(unittest.TestCase):
    """tg_build_chat 是唯一產生端：出來的工具執行時通道＝telegram。"""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "RED_TOOL_RPC_TELEGRAM": "0",   # 不掛 RPC proxy，直接呼叫得到本體
            "RED_INTENT_ROUTING": "",
            "RED_TG_ACTOR_GOOGLE_TOOLS": "0",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _build_tools(self, tools, actor=None):
        from agent_core.daemon_telegram import tg_build_chat
        created: list = []
        chat = tg_build_chat(
            agent_persona="persona",
            tools_list=tools,
            gemini_model="m",
            agent_client_factory=lambda: _FakeClient(created),
            agent_types_factory=_fake_types_factory,
            telegram_actor=actor,
        )
        return chat.create_kwargs["config"]["tools"]

    def test_owner_tools_run_in_telegram_channel(self):
        from agent_core.channel_context import current_channel
        seen: list[str] = []

        def probe_tool():
            seen.append(current_channel())
            return "ok"

        tools = self._build_tools([probe_tool])
        fn = next(f for f in tools if f.__name__ == "probe_tool")
        self.assertEqual(fn(), "ok")
        self.assertEqual(seen, ["telegram"])
        self.assertEqual(current_channel(), "")   # 執行完不殘留

    def test_employee_tools_get_channel_and_color_context(self):
        """colored 員工：channel 與 AgentRequest(caller=色) 兩層 context 都要在。"""
        from agent_core.channel_context import current_channel
        from agent_core.dept_tool_scope import dept_scope_color
        seen: list[tuple] = []

        def read_warehouse_stock():
            seen.append((current_channel(), dept_scope_color()))
            return "ok"

        actor = {"chat_id": "9990000002", "color": "indigo",
                 "email": "", "name": "", "source": "employee_registry"}
        tools = self._build_tools([read_warehouse_stock], actor=actor)
        fn = next(f for f in tools if f.__name__ == "read_warehouse_stock")
        fn()
        self.assertEqual(seen, [("telegram", "indigo")])


if __name__ == "__main__":
    unittest.main()
