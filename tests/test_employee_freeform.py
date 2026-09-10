"""員工自由對話（RED_TG_EMPLOYEE_FREEFORM）測試。

覆蓋：dept_tool_scope 純函式、RAG caller context 綁定、tg_handle_message
閘門（flag OFF / ON×私訊 / ON×群組 / ON×未開放色）、tg_build_chat 的
per-color 工具白名單與 Google 工具排除。

注意（unittest discover）：conftest fixture 不生效，env 隔離全在 setUp；
tg_handle_message 測試比照 test_telegram_user_separation 的
PerChatSessionIsolationTests 樣式（清 in-memory chat states、斷開 var/state
歷史讀寫、RED_TOOL_RPC_TELEGRAM=0 看原始工具名）。
"""
import os
import sys
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from tests.test_telegram_user_separation import (  # noqa: E402
    _FakeChat, _FakeClient, _fake_types_factory,
)

INDIGO_ACTOR = {
    "chat_id": "9990000002",
    "color": "indigo",
    "email": "warehouse-mgr@company.example",
    "name": "井戶良枝 (UserY)",
    "source": "telegram_private_approval:indigo",
}
GREEN_ACTOR = {
    "chat_id": "222",
    "color": "green",
    "email": "",
    "name": "",
    "source": "RED_TELEGRAM_AGENT_CHATS",
}
PRIVATE_MSG = {"chat": {"id": 9990000002, "type": "private"}}
GROUP_MSG = {"chat": {"id": -100123, "type": "group"}}


class DeptToolScopeTests(unittest.TestCase):
    def _colors(self, raw):
        from agent_core.dept_tool_scope import employee_freeform_colors
        with mock.patch.dict(os.environ, {"RED_TG_EMPLOYEE_FREEFORM": raw}, clear=False):
            return employee_freeform_colors()

    def test_flag_default_off(self):
        self.assertEqual(self._colors(""), frozenset())

    def test_flag_all_excludes_red(self):
        colors = self._colors("all")
        self.assertIn("indigo", colors)
        self.assertIn("gray", colors)
        self.assertNotIn("red", colors)

    def test_flag_color_list_ignores_invalid_and_red(self):
        self.assertEqual(self._colors("indigo, red, bogus"), frozenset({"indigo"}))

    def test_indigo_allowed_tools_include_home_matrix_and_common(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        allowed = allowed_tool_names_for_color("indigo")
        # 本色 home
        self.assertIn("read_warehouse_stock", allowed)
        # QUERY_MATRIX indigo→gray：拿得到生管 home 工具
        self.assertIn("read_production_progress_sheet", allowed)
        # 共用查詢面
        self.assertIn("search_drive_docs", allowed)
        self.assertIn("search_operation_sops", allowed)

    def test_green_allowed_tools_include_home_matrix_and_common(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        allowed = allowed_tool_names_for_color("green")
        # 本色 home（樣品室）
        self.assertIn("read_sample_status", allowed)
        self.assertIn("read_sample_bom", allowed)
        self.assertIn("search_product_photos", allowed)
        self.assertIn("list_tracked_samples", allowed)
        self.assertIn("check_sample_deadlines", allowed)
        # QUERY_MATRIX green→indigo：拿得到倉庫 home 工具
        self.assertIn("read_warehouse_stock", allowed)
        # green 不能查 gray：生管 home 工具不得出現
        self.assertNotIn("read_production_progress_sheet", allowed)
        # 共用查詢面
        self.assertIn("search_drive_docs", allowed)

    def test_orange_allowed_tools_include_home_matrix_and_common(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        allowed = allowed_tool_names_for_color("orange")
        # 本色 home（業務）
        self.assertIn("read_customer_order_pos", allowed)
        self.assertIn("query_po_timeline", allowed)
        self.assertIn("customer_360", allowed)
        self.assertIn("query_quote_history", allowed)
        # QUERY_MATRIX orange→yellow / green / indigo
        self.assertIn("query_material_arrival", allowed)
        self.assertIn("read_sample_status", allowed)
        self.assertIn("read_warehouse_stock", allowed)
        # orange 不能查 gray：生管 home 工具不得出現
        self.assertNotIn("read_production_progress_sheet", allowed)

    def test_yellow_allowed_tools_include_home_matrix_and_common(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        allowed = allowed_tool_names_for_color("yellow")
        # 本色 home（採購）
        self.assertIn("query_material_arrival", allowed)
        self.assertIn("material_arrival_overview", allowed)
        self.assertIn("check_material_readiness", allowed)
        self.assertIn("get_model_bom", allowed)
        self.assertIn("query_bom", allowed)
        # QUERY_MATRIX yellow→orange / gray
        self.assertIn("read_customer_order_pos", allowed)
        self.assertIn("read_production_progress_sheet", allowed)
        # yellow 不能查 purple（無 home 亦驗證非空集合語意即可）
        self.assertIn("search_drive_docs", allowed)
        # 鞋圖直傳（2026-07-28 UserA 案）：yellow home 明列（矩陣不含 green）
        self.assertIn("fetch_shoe_photos", allowed)
        from agent_core.tool_tiers import TIER_SAFE, get_tier
        self.assertEqual(get_tier("fetch_shoe_photos"), TIER_SAFE)

    def test_purple_inherits_via_matrix_without_home(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        allowed = allowed_tool_names_for_color("purple")
        # purple 無本色 home，靠 QUERY_MATRIX 繼承 orange/yellow/green/indigo
        self.assertIn("read_customer_order_pos", allowed)
        self.assertIn("query_material_arrival", allowed)
        self.assertIn("read_sample_status", allowed)
        self.assertIn("read_warehouse_stock", allowed)
        # purple 不能查 gray：生管 home 工具不得出現
        self.assertNotIn("read_production_progress_sheet", allowed)
        # 共用查詢面
        self.assertIn("search_drive_docs", allowed)

    def test_black_inherits_via_matrix_without_home(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        allowed = allowed_tool_names_for_color("black")
        # black（出納）無本色 home，靠矩陣繼承 orange/yellow/green/indigo
        self.assertIn("read_customer_order_pos", allowed)
        self.assertIn("query_material_arrival", allowed)
        self.assertIn("read_warehouse_stock", allowed)
        # black 不能查 gray：生管 home 工具不得出現
        self.assertNotIn("read_production_progress_sheet", allowed)
        self.assertIn("search_drive_docs", allowed)

    def test_white_gets_common_tools_only(self):
        from agent_core.dept_tool_scope import (
            _COMMON_TOOLS, allowed_tool_names_for_color,
        )
        # white（法務）矩陣為空（SoT 不主動查）＋無 home → 恰好只有共用查詢面
        self.assertEqual(allowed_tool_names_for_color("white"), _COMMON_TOOLS)

    def test_all_colors_get_uploaded_image_tool(self):
        # 2026-07-29 UserA OZ18/OZ19 案：員工通道無看圖工具，「依圖列出
        # 總需求量」只能腦補數量。analyze_uploaded_image 在共用面（路徑
        # 限縮在上傳目錄），每個開放色都要拿得到且是 SAFE。
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        from agent_core.tool_tiers import TIER_SAFE, get_tier
        for color in ("yellow", "indigo", "green", "orange", "white"):
            self.assertIn("analyze_uploaded_image",
                          allowed_tool_names_for_color(color), color)
        self.assertEqual(get_tier("analyze_uploaded_image"), TIER_SAFE)
        # 無路徑限縮的 analyze_image 不得混進員工白名單（任意本機路徑）
        for color in ("yellow", "indigo", "green", "orange", "white"):
            self.assertNotIn("analyze_image",
                             allowed_tool_names_for_color(color), color)

    def test_addendum_carries_correction_reverify_directive(self):
        # 最高指導原則（2026-07-31）：員工指錯必須重新查核照實答、查不到
        # 老實說。九色 freeform 的 system instruction 常駐段要載明，不能
        # 只靠逐輪的 correction_detector hint。
        from agent_core.dept_tool_scope import dept_scope_addendum
        addendum = dept_scope_addendum("yellow")
        self.assertIn("最高指導原則", addendum)
        self.assertIn("重查", addendum)
        self.assertIn("查不到能確認的資料", addendum)
        # 空色 fail-safe：不渲染任何段落
        self.assertEqual(dept_scope_addendum(""), "")

    def test_dept_email_timeline_never_whitelisted(self):
        # read_dept_email_timeline 的 dept 是自由參數（含「老闆」）——
        # 任何色的員工白名單都不得出現，防跨部門信件時間軸越權。
        from agent_core.agents.permission_matrix import Agent
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        for agent in Agent:
            self.assertNotIn(
                "read_dept_email_timeline",
                allowed_tool_names_for_color(agent.value),
                f"read_dept_email_timeline leaked into {agent.value}")

    def test_invalid_color_allows_nothing(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        self.assertEqual(allowed_tool_names_for_color("bogus"), frozenset())

    def test_filter_keeps_only_safe_whitelisted(self):
        from agent_core.dept_tool_scope import filter_tools_for_color

        def read_warehouse_stock():
            pass

        def search_drive_docs():
            pass

        def send_gmail():  # 不在白名單
            pass

        kept, removed = filter_tools_for_color(
            [read_warehouse_stock, search_drive_docs, send_gmail], "indigo")
        self.assertEqual([f.__name__ for f in kept],
                         ["read_warehouse_stock", "search_drive_docs"])
        self.assertEqual([f.__name__ for f in removed], ["send_gmail"])

    def test_filter_drops_non_safe_tier_even_if_whitelisted(self):
        from agent_core.dept_tool_scope import filter_tools_for_color

        def read_warehouse_stock():
            pass

        with mock.patch("agent_core.tool_tiers.get_tier", return_value="dangerous"):
            kept, removed = filter_tools_for_color([read_warehouse_stock], "indigo")
        self.assertEqual(kept, [])
        self.assertEqual([f.__name__ for f in removed], ["read_warehouse_stock"])

    def test_wrap_binds_rag_caller_during_execution_only(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.dept_tool_scope import wrap_tools_with_agent_caller
        from agent_core.rag_gateway import current_request_caller

        seen = {}

        def read_warehouse_stock():
            seen["caller"] = current_request_caller()
            return "ok"

        wrapped = wrap_tools_with_agent_caller([read_warehouse_stock], "indigo")
        self.assertEqual(wrapped[0].__name__, "read_warehouse_stock")
        self.assertEqual(wrapped[0](), "ok")
        # 執行期間 caller=INDIGO；執行完外面回到預設 RED
        self.assertIs(seen["caller"], Agent.INDIGO)
        self.assertIs(current_request_caller(), Agent.RED)

    def test_wrap_invalid_color_returns_empty(self):
        from agent_core.dept_tool_scope import wrap_tools_with_agent_caller

        def read_warehouse_stock():
            pass

        self.assertEqual(wrap_tools_with_agent_caller([read_warehouse_stock], "?"), [])


class EmployeeFreeformGateTests(unittest.TestCase):
    """tg_handle_message 閘門：flag×chat_type×色 的放行組合。"""

    def setUp(self):
        from agent_core import daemon_telegram as dt
        self.dt = dt
        dt._tg_chat_states.clear()
        dt._tg_chat_state.update({"chat": None, "turns": 0, "last_msg_ts": 0.0})
        self._env = mock.patch.dict(os.environ, {
            "RED_TOOL_RPC_TELEGRAM": "0",   # 看原始工具名
            "RED_INTENT_ROUTING": "",
            "RED_TG_ACTOR_GOOGLE_TOOLS": "0",  # live SA 檔不得污染工具面斷言
            "RED_TG_EMPLOYEE_FREEFORM": "",    # 各測試自行覆寫
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for fn_name in ("_record_tg_chat_turn",):
            patcher = mock.patch.object(dt, fn_name, lambda *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            dt, "_load_tg_chat_history_for_rebuild", lambda *a, **k: [])
        patcher.start()
        self.addCleanup(patcher.stop)
        # live var/state 的 work mode 不得洩入（meeting/dev 模式會 narrow 工具）
        patcher = mock.patch(
            "agent_core.mode_manager.get_current_mode", return_value="normal")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _handle(self, text, actor, message, created, tools_list=None):
        def read_warehouse_stock():
            return "stock"

        def search_drive_docs():
            return "docs"

        def send_gmail():
            return "mail"

        return self.dt.tg_handle_message(
            text,
            agent_persona="p",
            tools_list=(tools_list if tools_list is not None
                        else [read_warehouse_stock, search_drive_docs, send_gmail]),
            gemini_model="m",
            agent_client_factory=lambda: _FakeClient(created),
            agent_types_factory=_fake_types_factory,
            chat_id=str(actor.get("chat_id") or ""),
            telegram_actor=actor,
            telegram_message=message,
        )

    def test_flag_off_private_routes_to_nlp(self):
        # freeform 未開該色 + 私訊 → 落到唯讀 NL 查詢引擎（非全工具 session）。
        created: list[_FakeChat] = []
        with mock.patch(
            "agent_core.dept_nlp_query.answer_dept_question",
            return_value="NL 回覆",
        ) as nlp:
            reply = self._handle("查庫存", INDIGO_ACTOR, PRIVATE_MSG, created)
        self.assertEqual(len(created), 0)       # 不建 freeform Gemini session
        self.assertEqual(reply, "NL 回覆")
        self.assertEqual(nlp.call_args[0][0], "indigo")

    def test_flag_on_private_builds_scoped_session(self):
        created: list[_FakeChat] = []
        with mock.patch.dict(os.environ,
                             {"RED_TG_EMPLOYEE_FREEFORM": "indigo"}, clear=False):
            reply = self._handle("查庫存", INDIGO_ACTOR, PRIVATE_MSG, created)
        self.assertEqual(len(created), 1)
        self.assertEqual(reply, "ok")
        cfg = created[0].create_kwargs["config"]
        names = [f.__name__ for f in cfg["tools"]]
        # 白名單 ∩ SAFE：留查詢、去寄信
        self.assertIn("read_warehouse_stock", names)
        self.assertIn("search_drive_docs", names)
        self.assertNotIn("send_gmail", names)
        # 區隔 + 部門範圍說明都要進 system instruction
        self.assertIn("使用者區隔", cfg["system_instruction"])
        self.assertIn("部門查詢範圍", cfg["system_instruction"])

    def test_flag_on_group_still_notice(self):
        created: list[_FakeChat] = []
        with mock.patch.dict(os.environ,
                             {"RED_TG_EMPLOYEE_FREEFORM": "indigo"}, clear=False):
            reply = self._handle("查庫存", INDIGO_ACTOR, GROUP_MSG, created)
        self.assertEqual(len(created), 0)
        self.assertIn("indigo Telegram Agent 入口", reply)

    def test_flag_on_other_color_private_routes_to_nlp(self):
        # freeform 開 indigo，但 actor 是 green（未開放色）+ 私訊 → NL 引擎 fallback。
        created: list[_FakeChat] = []
        with mock.patch.dict(os.environ,
                             {"RED_TG_EMPLOYEE_FREEFORM": "indigo"}, clear=False), \
             mock.patch(
                 "agent_core.dept_nlp_query.answer_dept_question",
                 return_value="NL 回覆",
             ) as nlp:
            reply = self._handle("hi", GREEN_ACTOR,
                                 {"chat": {"id": 222, "type": "private"}}, created)
        self.assertEqual(len(created), 0)
        self.assertEqual(reply, "NL 回覆")
        self.assertEqual(nlp.call_args[0][0], "green")

    def test_flag_on_green_private_builds_scoped_session(self):
        created: list[_FakeChat] = []
        with mock.patch.dict(os.environ,
                             {"RED_TG_EMPLOYEE_FREEFORM": "indigo,gray,blue,green"},
                             clear=False):
            reply = self._handle("查SS501進度", GREEN_ACTOR,
                                 {"chat": {"id": 222, "type": "private"}}, created)
        self.assertEqual(len(created), 1)
        self.assertEqual(reply, "ok")
        cfg = created[0].create_kwargs["config"]
        names = [f.__name__ for f in cfg["tools"]]
        # green 經 QUERY_MATRIX 可查 indigo home；共用查詢面也在；寄信被移除
        self.assertIn("read_warehouse_stock", names)
        self.assertIn("search_drive_docs", names)
        self.assertNotIn("send_gmail", names)

    def test_flag_on_no_message_context_fails_closed(self):
        created: list[_FakeChat] = []
        with mock.patch.dict(os.environ,
                             {"RED_TG_EMPLOYEE_FREEFORM": "indigo"}, clear=False):
            reply = self._handle("hi", INDIGO_ACTOR, None, created)
        self.assertEqual(len(created), 0)
        self.assertIn("indigo Telegram Agent 入口", reply)

    def test_employee_model_override_applies_to_colored_actor(self):
        created: list[_FakeChat] = []
        with mock.patch.dict(os.environ, {
            "RED_TG_EMPLOYEE_FREEFORM": "indigo",
            "RED_TG_EMPLOYEE_MODEL": "cheap-model",
        }, clear=False):
            self._handle("查庫存", INDIGO_ACTOR, PRIVATE_MSG, created)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].create_kwargs["model"], "cheap-model")


class BuildChatColorScopeTests(unittest.TestCase):
    """tg_build_chat：colored 員工的白名單、Google 工具排除、red GM 不受影響。"""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "RED_TOOL_RPC_TELEGRAM": "0",
            "RED_INTENT_ROUTING": "",
            "RED_TG_ACTOR_GOOGLE_TOOLS": "0",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _build(self, actor):
        from agent_core.daemon_telegram import tg_build_chat

        def read_warehouse_stock():
            pass

        def search_drive_docs():
            pass

        def search_gmail():
            pass

        created: list[_FakeChat] = []
        chat = tg_build_chat(
            agent_persona="persona",
            tools_list=[read_warehouse_stock, search_drive_docs, search_gmail],
            gemini_model="m",
            agent_client_factory=lambda: _FakeClient(created),
            agent_types_factory=_fake_types_factory,
            telegram_actor=actor,
        )
        return chat.create_kwargs["config"]

    def test_colored_actor_gets_whitelist_intersection(self):
        cfg = self._build(INDIGO_ACTOR)
        names = [f.__name__ for f in cfg["tools"]]
        self.assertEqual(names, ["read_warehouse_stock", "search_drive_docs"])
        self.assertIn("部門查詢範圍", cfg["system_instruction"])

    def test_colored_actor_never_gets_actor_google_tools(self):
        # 即使 feature 開著（env=1）＋ SA 可用，colored 員工也不掛寄信工具。
        with mock.patch.dict(os.environ, {"RED_TG_ACTOR_GOOGLE_TOOLS": "1"}, clear=False), \
             mock.patch("agent_core.actor_google_tools.actor_google_tools_available",
                        return_value=True) as available:
            cfg = self._build(INDIGO_ACTOR)
        available.assert_not_called()  # colored 分支根本不該問
        names = [f.__name__ for f in cfg["tools"]]
        self.assertEqual(names, ["read_warehouse_stock", "search_drive_docs"])

    def test_red_gm_employee_unaffected_by_color_scope(self):
        from tests.test_telegram_user_separation import SPENCER_ACTOR
        cfg = self._build(SPENCER_ACTOR)
        names = [f.__name__ for f in cfg["tools"]]
        # red GM：非 owner 過濾照舊（search_gmail 是 owner-private、被移除），
        # 但不套 per-color 白名單 —— search_drive_docs 與 warehouse 工具都留。
        self.assertEqual(names, ["read_warehouse_stock", "search_drive_docs"])
        self.assertNotIn("部門查詢範圍", cfg["system_instruction"])


if __name__ == "__main__":
    unittest.main()
