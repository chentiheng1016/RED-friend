"""Telegram 多使用者區隔（owner vs 員工）測試。

涵蓋：
  - telegram_actor_scope：separation 判定、owner-only 工具封鎖、env 旋鈕
  - tg_build_chat：非 owner session 移除 owner-only 工具 + 區隔 addendum
  - tg_handle_message：per-chat session 隔離（大王與員工不共用 in-memory
    chat）、發訊者標記、actor 變更強制重建、非 owner 跳過媒體 fastpath
  - telegram_agent_config：employee registry 條目覆蓋 env 條目（帶 email）
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

OWNER_ACTOR = {
    "chat_id": "111",
    "color": "red",
    "email": "",
    "name": "Red owner",
    "source": "telegram-chat-id",
    "is_owner": "true",
}
SPENCER_ACTOR = {
    "chat_id": "9990000003",
    "color": "red",
    "email": "gm@company.example",
    "name": "gm",
    "source": "employee_registry",
}
ENV_ACTOR = {
    "chat_id": "222",
    "color": "green",
    "email": "",
    "name": "",
    "source": "RED_TELEGRAM_AGENT_CHATS",
}


class ActorScopeTests(unittest.TestCase):
    def test_separation_flag(self):
        from agent_core.telegram_actor_scope import actor_requires_separation
        self.assertFalse(actor_requires_separation(None))
        self.assertFalse(actor_requires_separation({}))
        self.assertFalse(actor_requires_separation(OWNER_ACTOR))
        self.assertTrue(actor_requires_separation(SPENCER_ACTOR))
        self.assertTrue(actor_requires_separation(ENV_ACTOR))

    def test_owner_only_tools_blocked_for_non_owner(self):
        from agent_core.telegram_actor_scope import is_tool_blocked_for_non_owner
        # sensitive（以大王身分產生副作用）
        for name in ("send_gmail", "run_shell", "telegram_push",
                     "manage_files", "write_file", "create_calendar_event"):
            self.assertTrue(is_tool_blocked_for_non_owner(name), name)
        # 大王個人帳號唯讀
        for name in ("search_gmail", "read_gmail", "summarize_inbox",
                     "list_calendar_events", "fetch_email_by_thread_id",
                     "prioritized_inbox"):
            self.assertTrue(is_tool_blocked_for_non_owner(name), name)

    def test_mcp_tools_blocked_by_prefix_and_attribute(self):
        # gemini-code-assist review：MCP filesystem 唯讀工具沒被列舉到
        # → 非 owner 可讀大王 Mac 任意檔案。修法 = mcp_ 前綴整批封 +
        # _is_mcp_tool 屬性雙保險（server 清單可任意擴充，列名列不全）。
        from agent_core.telegram_actor_scope import (
            filter_tools_for_non_owner, is_tool_blocked_for_non_owner,
        )
        for name in ("mcp_filesystem_read_file", "mcp_filesystem_list_directory",
                     "mcp_filesystem_glob", "mcp_filesystem_get_file_info",
                     "mcp_anyserver_anytool"):
            self.assertTrue(is_tool_blocked_for_non_owner(name), name)

        def oddly_named_tool():
            pass

        oddly_named_tool._is_mcp_tool = True
        kept, removed = filter_tools_for_non_owner([oddly_named_tool])
        self.assertEqual(kept, [])
        self.assertEqual(removed, ["oddly_named_tool"])
        # 明確放行仍可用 env 開回來
        with mock.patch.dict(os.environ, {
            "RED_TG_NONOWNER_TOOL_ALLOW": "mcp_filesystem_read_file",
        }, clear=False):
            self.assertFalse(
                is_tool_blocked_for_non_owner("mcp_filesystem_read_file"))

    def test_company_tools_stay_available(self):
        from agent_core.telegram_actor_scope import is_tool_blocked_for_non_owner
        for name in ("query_po_timeline", "list_customer_pos", "search_drive_docs"):
            self.assertFalse(is_tool_blocked_for_non_owner(name), name)

    def test_memory_read_tools_blocked_for_non_owner(self):
        # 政策反轉（學習迴圈 PR 審查）：recall 的 where 只支援 source 過濾、
        # 不看 visibility_scope——confirmed_fact/note/行為準則都是大王私有
        # 記憶，非 owner 整顆移除（fail-closed，與 search_google_chat 同理）。
        from agent_core.telegram_actor_scope import is_tool_blocked_for_non_owner
        for name in ("recall", "load_memory", "memory_stats",
                     "list_behaviors", "memory_governance_report"):
            self.assertTrue(is_tool_blocked_for_non_owner(name), name)

    def test_env_allow_and_block_override(self):
        from agent_core.telegram_actor_scope import is_tool_blocked_for_non_owner
        with mock.patch.dict(os.environ, {
            "RED_TG_NONOWNER_TOOL_ALLOW": "search_gmail,recall",
            "RED_TG_NONOWNER_TOOL_BLOCK": "search_drive_docs",
        }, clear=False):
            self.assertFalse(is_tool_blocked_for_non_owner("search_gmail"))
            self.assertFalse(is_tool_blocked_for_non_owner("recall"))
            self.assertTrue(is_tool_blocked_for_non_owner("search_drive_docs"))

    def test_filter_tools_for_non_owner(self):
        from agent_core.telegram_actor_scope import filter_tools_for_non_owner

        def send_gmail():  # noqa: D401 — 假 tool
            pass

        def search_drive_docs():
            pass

        kept, removed = filter_tools_for_non_owner([send_gmail, search_drive_docs])
        self.assertEqual([f.__name__ for f in kept], ["search_drive_docs"])
        self.assertEqual(removed, ["send_gmail"])

    def test_addendum_and_prefix_content(self):
        from agent_core.telegram_actor_scope import (
            actor_message_prefix, actor_system_addendum,
        )
        addendum = actor_system_addendum(SPENCER_ACTOR)
        self.assertIn("gm", addendum)
        self.assertIn("gm@company.example", addendum)
        self.assertIn("不是大王", addendum)
        # 最高指導原則（2026-07-31）：非 owner 一律載明「指錯→重新查核」
        self.assertIn("最高指導原則", addendum)
        self.assertIn("重查", addendum)
        prefix = actor_message_prefix(SPENCER_ACTOR)
        self.assertIn("gm@company.example", prefix)
        self.assertIn("非大王", prefix)
        # owner / 舊呼叫者 → 空字串（行為不變）
        self.assertEqual(actor_system_addendum(OWNER_ACTOR), "")
        self.assertEqual(actor_message_prefix(None), "")


def _fake_types_factory():
    return types.SimpleNamespace(
        GenerateContentConfig=lambda **kw: kw,
        AutomaticFunctionCallingConfig=lambda **kw: kw,
    )


class _FakeChat:
    def __init__(self, create_kwargs):
        self.create_kwargs = create_kwargs
        self.sent: list[str] = []

    def send_message(self, message):
        self.sent.append(message)
        return types.SimpleNamespace(text="ok")


class _FakeClient:
    def __init__(self, created):
        self._created = created
        self.chats = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        chat = _FakeChat(kwargs)
        self._created.append(chat)
        return chat


class BuildChatActorGateTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "RED_TOOL_RPC_TELEGRAM": "0",   # 不掛 RPC proxy，看原始 fn 名
            "RED_INTENT_ROUTING": "",
            # live checkout 有真實 service_account.json → 非 owner actor 預設會
            # 被掛上 actor-scoped Gmail/行事曆工具，污染「只剩 search_drive_docs」的斷言
            # （CI 沒 SA 才會過）。關掉這個 feature，讓本類聚焦在 owner-only
            # 工具移除邏輯。test_actor_scoped_google_tools_wired_in 會自行
            # mock.patch actor_google_tools_available→True，不受此旋鈕影響。
            "RED_TG_ACTOR_GOOGLE_TOOLS": "0",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _build(self, actor):
        from agent_core.daemon_telegram import tg_build_chat

        def send_gmail():
            pass

        def search_gmail():
            pass

        def search_drive_docs():
            pass

        created: list[_FakeChat] = []
        chat = tg_build_chat(
            agent_persona="persona",
            tools_list=[send_gmail, search_gmail, search_drive_docs],
            gemini_model="m",
            agent_client_factory=lambda: _FakeClient(created),
            agent_types_factory=_fake_types_factory,
            telegram_actor=actor,
        )
        return chat.create_kwargs["config"]

    def test_non_owner_loses_owner_only_tools_and_gets_addendum(self):
        cfg = self._build(SPENCER_ACTOR)
        names = [f.__name__ for f in cfg["tools"]]
        self.assertEqual(names, ["search_drive_docs"])
        self.assertIn("使用者區隔", cfg["system_instruction"])
        self.assertIn("gm@company.example", cfg["system_instruction"])

    def test_owner_keeps_full_toolset_no_addendum(self):
        cfg = self._build(OWNER_ACTOR)
        names = [f.__name__ for f in cfg["tools"]]
        self.assertEqual(names, ["send_gmail", "search_gmail", "search_drive_docs"])
        self.assertNotIn("使用者區隔", cfg["system_instruction"])

    def test_legacy_callers_without_actor_unchanged(self):
        cfg = self._build(None)
        names = [f.__name__ for f in cfg["tools"]]
        self.assertEqual(names, ["send_gmail", "search_gmail", "search_drive_docs"])
        self.assertNotIn("使用者區隔", cfg["system_instruction"])

    def test_actor_scoped_google_tools_wired_in(self):
        # UserS 有委派信箱 → owner 版 send_gmail/search_gmail 移除後，
        # 接上 actor-scoped 版本（以他自己身分），addendum 改口「自己的帳號」。
        import agent_core.actor_google_tools as agt

        def _actor_send_gmail():
            pass

        def _actor_search_gmail():
            pass

        _actor_send_gmail.__name__ = "send_gmail"
        _actor_search_gmail.__name__ = "search_gmail"
        for f in (_actor_send_gmail, _actor_search_gmail):
            f._actor_scoped = True
            f._tg_auth_wrapped = True
            f._actor_email = "gm@company.example"

        with mock.patch.object(agt, "actor_google_tools_available", return_value=True), \
                mock.patch.object(agt, "build_actor_google_tools",
                                  return_value=[_actor_send_gmail, _actor_search_gmail]):
            cfg = self._build(SPENCER_ACTOR)
        names = [f.__name__ for f in cfg["tools"]]
        # 公司域 search_drive_docs 仍在；send_gmail/search_gmail 是 actor-scoped 版本
        self.assertIn("search_drive_docs", names)
        self.assertIn("send_gmail", names)
        self.assertIn("search_gmail", names)
        actor_sends = [f for f in cfg["tools"]
                       if f.__name__ == "send_gmail" and getattr(f, "_actor_scoped", False)]
        self.assertEqual(len(actor_sends), 1)
        # addendum 改口：以他自己的帳號操作
        self.assertIn("自己的帳號", cfg["system_instruction"])


class PerChatSessionIsolationTests(unittest.TestCase):
    """大王跟員工各自一份 in-memory session — 不能互看 context。"""

    def setUp(self):
        from agent_core import daemon_telegram as dt
        self.dt = dt
        dt._tg_chat_states.clear()
        dt._tg_chat_state.update({"chat": None, "turns": 0, "last_msg_ts": 0.0})
        self._env = mock.patch.dict(os.environ, {
            "RED_TOOL_RPC_TELEGRAM": "0",
            "RED_INTENT_ROUTING": "",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        # 不碰磁碟上的對話歷史（var/state） — 測的是 in-memory 隔離
        for fn_name in ("_record_tg_chat_turn",):
            patcher = mock.patch.object(dt, fn_name, lambda *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            dt, "_load_tg_chat_history_for_rebuild", lambda *a, **k: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _handle(self, text, chat_id, actor, created):
        return self.dt.tg_handle_message(
            text,
            agent_persona="p",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=lambda: _FakeClient(created),
            agent_types_factory=_fake_types_factory,
            chat_id=chat_id,
            telegram_actor=actor,
        )

    def test_chat_state_for_keys_by_chat_id(self):
        s1 = self.dt._chat_state_for("111")
        s2 = self.dt._chat_state_for("222")
        self.assertIsNot(s1, s2)
        self.assertIs(self.dt._chat_state_for("111"), s1)
        # 空 chat_id → legacy 全域 state
        self.assertIs(self.dt._chat_state_for(""), self.dt._tg_chat_state)

    def test_two_users_get_two_sessions(self):
        created: list[_FakeChat] = []
        reply1 = self._handle("大王訊息", "111", OWNER_ACTOR, created)
        reply2 = self._handle("員工訊息", "9990000003", SPENCER_ACTOR, created)
        self.assertEqual(reply1, "ok")
        self.assertEqual(reply2, "ok")
        # 兩個 chat_id → 兩個獨立 Gemini session（修掉共用 _tg_chat_state 的洩漏）
        self.assertEqual(len(created), 2)
        state_owner = self.dt._tg_chat_states["111"]
        state_emp = self.dt._tg_chat_states["9990000003"]
        self.assertIsNot(state_owner["chat"], state_emp["chat"])
        # 員工 session 看不到大王那輪：他自己的 chat 只收到他的訊息
        emp_chat = state_emp["chat"]
        self.assertEqual(len(emp_chat.sent), 1)
        self.assertIn("員工訊息", emp_chat.sent[0])
        self.assertNotIn("大王訊息", emp_chat.sent[0])

    def test_non_owner_message_carries_sender_line(self):
        created: list[_FakeChat] = []
        self._handle("我的信有什麼", "9990000003", SPENCER_ACTOR, created)
        sent = created[0].sent[0]
        self.assertIn("[發訊者：gm (gm@company.example)", sent)
        # owner 沒有發訊者標記
        created2: list[_FakeChat] = []
        self._handle("hello", "111", OWNER_ACTOR, created2)
        self.assertNotIn("[發訊者：", created2[0].sent[0])

    def test_actor_change_forces_session_rebuild(self):
        # 注意：非 red 色員工在更早的閘門就被擋在全工具對話外（只開放
        # /dept 查詢），所以 fingerprint 測試用兩個 red 身分的變化：
        # 同一個 chat_id 從 gm 重綁成另一個 GM email。
        created: list[_FakeChat] = []
        self._handle("hi", "333", SPENCER_ACTOR, created)
        self.assertEqual(len(created), 1)
        rebound = dict(SPENCER_ACTOR, email="other-gm@company.example", name="gm2")
        self._handle("hi again", "333", rebound, created)
        # 身分 fingerprint 變了 → 不能沿用舊 session（舊 addendum/工具集
        # 是上一個人的）
        self.assertEqual(len(created), 2)

    def test_non_red_employee_never_gets_full_tool_session(self):
        # 既有閘門：非 red 色員工不進全工具 Gemini 對話 — 區隔層不該
        # 意外把這條路打開。freeform 關 + 私訊 → 自由文字走唯讀 dept_nlp_query
        # 引擎（query.* + ACL RAG），仍然不建全工具 session。
        created: list[_FakeChat] = []
        with mock.patch.dict(os.environ,
                             {"RED_TG_EMPLOYEE_FREEFORM": ""}, clear=False), \
             mock.patch(
                 "agent_core.dept_nlp_query.answer_dept_question",
                 return_value="NL 回覆",
             ) as nlp:
            reply = self.dt.tg_handle_message(
                "hi", agent_persona="p", tools_list=[], gemini_model="m",
                agent_client_factory=lambda: _FakeClient(created),
                agent_types_factory=_fake_types_factory,
                chat_id="444", telegram_actor=ENV_ACTOR,
                telegram_message={"chat": {"id": 444, "type": "private"}},
            )
        self.assertEqual(len(created), 0)
        self.assertEqual(reply, "NL 回覆")
        self.assertEqual(nlp.call_args[0][0], "green")  # ENV_ACTOR 的色

    def test_non_owner_skips_media_fastpath(self):
        created: list[_FakeChat] = []
        with mock.patch.object(
            self.dt, "_try_direct_media_download_fastpath",
            return_value=None,
        ) as fastpath:
            self._handle("https://example.com/v.mp4", "9990000003",
                         SPENCER_ACTOR, created)
            fastpath.assert_not_called()
            self._handle("https://example.com/v.mp4", "111",
                         OWNER_ACTOR, created)
            fastpath.assert_called_once()


class EmployeeActorOverridesEnvTests(unittest.TestCase):
    """employee registry 條目（帶 email）覆蓋同 chat_id 的 env 條目。"""

    def test_registry_wins_over_env(self):
        from agent_core import telegram_agent_config as cfg
        with mock.patch.dict(os.environ, {
            "RED_TELEGRAM_AGENT_CHATS": "red:9990000003",
        }, clear=False), mock.patch.object(
            cfg, "employee_telegram_actors",
            return_value={"9990000003": dict(SPENCER_ACTOR)},
        ):
            actor = cfg.telegram_actors().get("9990000003")
        self.assertIsNotNone(actor)
        self.assertEqual(actor["source"], "employee_registry")
        self.assertEqual(actor["email"], "gm@company.example")
        # 員工 actor 沒有 is_owner → 會套區隔
        from agent_core.telegram_actor_scope import actor_requires_separation
        self.assertTrue(actor_requires_separation(actor))


class ResolveInboundActorTests(unittest.TestCase):
    """綁定群不以 chat.id 當身分：只有已註冊員工(from.id)放行，未註冊丟棄。"""

    def _actors(self):
        # 綁定群 -100111 → white；註冊員工 user 555 → green（本人身分）。
        return {
            "-100111": {"chat_id": "-100111", "color": "white", "is_owner": "false"},
            "555": {"chat_id": "555", "color": "green", "email": "g@x", "is_owner": "false"},
            "999": {"chat_id": "999", "color": "red", "is_owner": "true"},
        }

    def test_group_member_registered_gets_own_identity(self):
        from agent_core.daemon_telegram import _resolve_inbound_actor
        chat = {"id": "-100111", "type": "supergroup"}
        msg = {"chat": chat, "from": {"id": 555}}
        actor, reason = _resolve_inbound_actor(chat, msg, self._actors())
        self.assertEqual(reason, "group_member_registered")
        self.assertEqual(actor["color"], "green")  # 本人色，非群綁定的 white

    def test_group_member_unregistered_is_rejected(self):
        from agent_core.daemon_telegram import _resolve_inbound_actor
        chat = {"id": "-100111", "type": "group"}
        msg = {"chat": chat, "from": {"id": 424242}}  # 不在 registry
        actor, reason = _resolve_inbound_actor(chat, msg, self._actors())
        self.assertIsNone(actor)  # 不繼承群綁定的 white
        self.assertEqual(reason, "group_member_not_registered")

    def test_private_chat_still_resolves_by_chat_id(self):
        from agent_core.daemon_telegram import _resolve_inbound_actor
        chat = {"id": "999", "type": "private"}
        msg = {"chat": chat, "from": {"id": 999}}
        actor, reason = _resolve_inbound_actor(chat, msg, self._actors())
        self.assertEqual(reason, "chat_id")
        self.assertEqual(actor.get("is_owner"), "true")

    def test_group_missing_from_id_rejected(self):
        from agent_core.daemon_telegram import _resolve_inbound_actor
        chat = {"id": "-100111", "type": "group"}
        actor, reason = _resolve_inbound_actor(chat, {"chat": chat}, self._actors())
        self.assertIsNone(actor)
        self.assertEqual(reason, "group_member_not_registered")


class ConfirmScopePerUserTests(unittest.TestCase):
    """#32：+確認 窗口在綁定群裡以 from.id 區隔，A 的確認不能 arm B 的敏感請求。"""

    def test_scope_key_accepts_raw_and_composite_rejects_garbage(self):
        from agent_core.tg_auth import _confirm_scope_key
        self.assertEqual(_confirm_scope_key("12345"), "12345")          # 純 chat_id（向後相容）
        self.assertEqual(_confirm_scope_key("-100111:555"), "-100111:555")  # per-user 複合鍵
        for bad in ("", "a", "1:2:3", "12345:abc", "12345:", ":555", "  ", "0"):
            self.assertIsNone(_confirm_scope_key(bad), bad)

    def test_confirm_scope_for_group_vs_private(self):
        from agent_core.daemon_telegram import _confirm_scope_for
        grp = {"chat": {"id": "-100", "type": "supergroup"}, "from": {"id": 555}}
        self.assertEqual(_confirm_scope_for(grp, "-100"), "-100:555")
        pvt = {"chat": {"id": "999", "type": "private"}, "from": {"id": 999}}
        self.assertEqual(_confirm_scope_for(pvt, "999"), "999")   # 私訊：chat_id==from_id
        self.assertEqual(_confirm_scope_for(None, "999"), "999")  # 直呼/測試：退回 chat_id

    def test_confirm_window_isolated_per_user(self):
        from agent_core import tg_auth
        a, b, chat = "700100200300:111", "700100200300:222", "700100200300"
        try:
            self.assertTrue(tg_auth.mark_confirmed(a))   # 使用者 A 打了 +確認
            ok_a, _ = tg_auth.check_confirmed(a)
            ok_b, _ = tg_auth.check_confirmed(b)
            ok_chat, _ = tg_auth.check_confirmed(chat)
            self.assertTrue(ok_a)      # A 自己在窗內
            self.assertFalse(ok_b)     # B 不因 A 的確認被放行（#32 核心）
            self.assertFalse(ok_chat)  # 群 chat 層級也不放行
        finally:
            tg_auth.revoke_after_use(a)
            tg_auth.revoke_after_use(b)


class MediaFastpathConfirmScopeTests(unittest.TestCase):
    """健檢 Medium（PR #210 後遺）：媒體 fastpath 的 +確認 check/revoke 必須用
    mark 端同一把 scope 鍵（綁定群 "<chat_id>:<from_id>"），否則群組內 owner 的
    +確認 永遠死鎖。私訊（confirm_scope 留空）零行為改變。"""

    def setUp(self):
        from agent_core import daemon_telegram as dt
        self.dt = dt

    def test_tg_handle_message_passes_group_scope_to_fastpath(self):
        # owner 在綁定群發訊息：fastpath 要拿到群組 scope（chat:from），
        # 不是裸 chat_id。
        dt = self.dt
        dt._tg_chat_states.clear()
        grp_msg = {
            "chat": {"id": "-100", "type": "supergroup"},
            "from": {"id": 111},
        }
        with mock.patch.dict(os.environ, {"RED_INTENT_ROUTING": ""}, clear=False), \
                mock.patch.object(dt, "_record_tg_chat_turn", lambda *a, **k: None), \
                mock.patch.object(dt, "_load_tg_chat_history_for_rebuild", lambda *a, **k: []), \
                mock.patch.object(dt, "_try_direct_media_download_fastpath",
                                  return_value="done") as fastpath:
            reply = dt.tg_handle_message(
                "https://example.com/v.mp4",
                agent_persona="p",
                tools_list=[],
                gemini_model="m",
                agent_client_factory=lambda: None,
                agent_types_factory=lambda: None,
                chat_id="-100",
                telegram_actor=OWNER_ACTOR,
                telegram_message=grp_msg,
            )
        self.assertEqual(reply, "done")
        fastpath.assert_called_once()
        self.assertEqual(fastpath.call_args.kwargs.get("confirm_scope"), "-100:111")
        self.assertEqual(fastpath.call_args.kwargs.get("chat_id"), "-100")

    def test_fastpath_consumes_token_under_group_scope(self):
        """端到端（真 tg_auth state）：token 記在群組 scope，fastpath 帶
        confirm_scope 才放行、one-shot 消費也在同一把 key 上。"""
        from agent_core import tg_auth
        from agent_core.daemon_telegram import _TG_PENDING_MEDIA_DOWNLOAD_KEY
        dt = self.dt
        scope = "-100777:333"
        chat_state = {
            _TG_PENDING_MEDIA_DOWNLOAD_KEY: {
                "url": "https://www.youtube.com/watch?v=abc123",
                "ts": __import__("time").time(),
                "tool": "download_online_video",
                "args": {"url": "https://www.youtube.com/watch?v=abc123"},
            },
        }
        try:
            self.assertTrue(tg_auth.mark_confirmed(scope))
            with mock.patch("agent_core.tool_runner.call_tool",
                            return_value="downloaded") as call_tool, \
                    mock.patch.object(dt, "_append_telegram_delivery_for_download",
                                      side_effect=lambda r, **k: str(r)):
                reply = dt._try_direct_media_download_fastpath(
                    "+確認",
                    chat_state=chat_state,
                    chat_id="-100777",
                    confirm_scope=scope,
                )
            self.assertEqual(reply, "downloaded")
            call_tool.assert_called_once()
            confirmed, _ = tg_auth.check_confirmed(scope)
            self.assertFalse(confirmed, "one-shot：revoke 也要在 scope 鍵上")
        finally:
            tg_auth.revoke_after_use(scope)

    def test_fastpath_group_token_not_armed_by_bare_chat_id(self):
        """舊 bug 路徑：token 在 scope 下、fastpath 只拿裸 chat_id → 不放行。"""
        from agent_core import tg_auth
        from agent_core.daemon_telegram import _TG_PENDING_MEDIA_DOWNLOAD_KEY
        dt = self.dt
        scope = "-100778:334"
        chat_state = {
            _TG_PENDING_MEDIA_DOWNLOAD_KEY: {
                "url": "https://www.youtube.com/watch?v=abc123",
                "ts": __import__("time").time(),
                "tool": "download_online_video",
                "args": {"url": "https://www.youtube.com/watch?v=abc123"},
            },
        }
        try:
            self.assertTrue(tg_auth.mark_confirmed(scope))
            with mock.patch("agent_core.tool_runner.call_tool") as call_tool:
                reply = dt._try_direct_media_download_fastpath(
                    "+確認",
                    chat_state=chat_state,
                    chat_id="-100778",  # 沒帶 confirm_scope → 裸 chat_id
                )
            call_tool.assert_not_called()
            self.assertIsInstance(reply, str)  # 回確認提示，不執行
        finally:
            tg_auth.revoke_after_use(scope)


class FailClosedActorColorTests(unittest.TestCase):
    """健檢 Medium：actor color 無效時 fail-closed，不 fallback Agent.RED
    （RED = SUPER_ADMIN 查詢面，fail-open 等於把設定打錯的員工升成管理員）。"""

    def setUp(self):
        from agent_core import daemon_telegram as dt
        self.dt = dt
        dt._tg_chat_states.clear()

    def test_invalid_color_refused_no_session_built(self):
        created: list[_FakeChat] = []
        bad_actor = {"chat_id": "666", "color": "hotpink", "email": "",
                     "name": "typo employee", "source": "employee_registry"}
        reply = self.dt.tg_handle_message(
            "hi",
            agent_persona="p",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=lambda: _FakeClient(created),
            agent_types_factory=_fake_types_factory,
            chat_id="666",
            telegram_actor=bad_actor,
        )
        self.assertIn("身分設定異常", reply)
        self.assertEqual(created, [], "無效 color 不得建 session（更不得以 RED 面建）")

    def test_valid_actor_unaffected(self):
        created: list[_FakeChat] = []
        reply = self.dt.tg_handle_message(
            "hi",
            agent_persona="p",
            tools_list=[],
            gemini_model="m",
            agent_client_factory=lambda: _FakeClient(created),
            agent_types_factory=_fake_types_factory,
            chat_id="444",
            telegram_actor=ENV_ACTOR,  # green — 合法色，走 /dept 提示
        )
        self.assertNotIn("身分設定異常", reply)


class ApprovalActorColorValidationTests(unittest.TestCase):
    """健檢 Medium 後半：approval 記錄讀入時用 Agent() 驗 color，無效略過。"""

    def test_local_file_invalid_color_skipped(self):
        import json
        import tempfile
        from agent_core import daemon_telegram as dt

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "approvals.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"approvals": {
                    "111": {"status": "approved", "color": "green", "name": "ok"},
                    "222": {"status": "approved", "color": "hotpink", "name": "typo"},
                }}, f)
            with mock.patch.dict(os.environ, {
                "RED_TELEGRAM_PRIVATE_APPROVALS_FILE": path,
            }, clear=False), \
                    mock.patch.object(dt, "_telegram_approval_store", return_value=None):
                os.environ.pop("RED_TELEGRAM_DEFAULT_ACTOR_COLOR", None)
                actors = dt._telegram_private_approval_actors()
        self.assertIn("111", actors)
        self.assertEqual(actors["111"]["color"], "green")
        self.assertNotIn("222", actors, "無效 color 的 approval 記錄必須被略過")

    def test_store_backend_invalid_color_skipped(self):
        from agent_core import daemon_telegram as dt

        store_rows = {
            "111": {"chat_id": "111", "color": "green", "is_owner": "false"},
            "222": {"chat_id": "222", "color": "hotpink", "is_owner": "false"},
        }
        fake_store = mock.MagicMock()
        fake_store.list_approved_actors.return_value = store_rows
        with mock.patch.object(dt, "_telegram_approval_store", return_value=fake_store):
            actors = dt._telegram_private_approval_actors()
        self.assertIn("111", actors)
        self.assertNotIn("222", actors)


if __name__ == "__main__":
    unittest.main()
