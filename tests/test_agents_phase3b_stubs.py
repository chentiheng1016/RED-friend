"""Project Rainbow registry + stub/real agent smoke tests.

驗證：
  - build_default_registry() 拼出來的 registry 含 9 個部門（9 real）
  - Real impl (Green / White / Orange / Yellow / Blue / Indigo / Purple / Gray / Black) 不會回 status=stub
  - Red 不註冊（daemon orchestrator）
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class TestWireHelper(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import build_default_registry, Agent
        self.Agent = Agent
        self.registry, self.middleware = build_default_registry()

    def test_registry_has_nine_agents(self):
        # 9 real (Green, White, Orange, Yellow, Blue, Indigo, Purple, Gray, Black)
        self.assertEqual(len(self.registry), 9)

    def test_real_colors_registered(self):
        for color in (
            self.Agent.GREEN, self.Agent.WHITE, self.Agent.ORANGE,
            self.Agent.YELLOW, self.Agent.BLUE, self.Agent.INDIGO,
            self.Agent.PURPLE, self.Agent.GRAY, self.Agent.BLACK,
        ):
            self.assertIn(color, self.registry, f"{color.value} 應在 registry")

    def test_red_not_registered(self):
        # Red 是 daemon orchestrator，不註冊
        self.assertNotIn(self.Agent.RED, self.registry)


class TestStubBehavior(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import build_default_registry, Agent
        self.Agent = Agent
        self.registry, self.middleware = build_default_registry()

    def _dispatch(self, caller, target, intent="query.x", payload=None):
        from agent_core.agents import AgentRequest
        return self.middleware.dispatch(AgentRequest(
            caller=caller, target=target, intent=intent,
            payload=payload if payload is not None else {},
        ))

    def test_no_stub_agents_registered(self):
        from agent_core.agents._stub import StubDepartmentAgent

        self.assertFalse(
            any(isinstance(agent, StubDepartmentAgent) for agent in self.registry._agents.values())
        )

    def test_real_agents_do_not_return_stub_status(self):
        # Green: query.list_samples 本身吐 text — 不會有 status=stub
        green_result = self._dispatch(
            self.Agent.RED, self.Agent.GREEN,
            intent="query.list_samples", payload={"status": "all"},
        )
        self.assertNotEqual(green_result.get("status"), "stub")

        # White: query.list_specs 吐 text
        white_result = self._dispatch(
            self.Agent.RED, self.Agent.WHITE,
            intent="query.list_specs", payload={},
        )
        self.assertNotEqual(white_result.get("status"), "stub")

        yellow_result = self._dispatch(
            self.Agent.RED, self.Agent.YELLOW,
            intent="query.profile", payload={},
        )
        self.assertNotEqual(yellow_result.get("status"), "stub")

        blue_result = self._dispatch(
            self.Agent.RED, self.Agent.BLUE,
            intent="query.profile", payload={},
        )
        self.assertNotEqual(blue_result.get("status"), "stub")

        indigo_result = self._dispatch(
            self.Agent.RED, self.Agent.INDIGO,
            intent="query.profile", payload={},
        )
        self.assertNotEqual(indigo_result.get("status"), "stub")

        purple_result = self._dispatch(
            self.Agent.RED, self.Agent.PURPLE,
            intent="query.profile", payload={},
        )
        self.assertNotEqual(purple_result.get("status"), "stub")

        gray_result = self._dispatch(
            self.Agent.RED, self.Agent.GRAY,
            intent="query.profile", payload={},
        )
        self.assertNotEqual(gray_result.get("status"), "stub")

        black_result = self._dispatch(
            self.Agent.RED, self.Agent.BLACK,
            intent="query.profile", payload={},
        )
        self.assertNotEqual(black_result.get("status"), "stub")


class TestMatrixReachabilityThroughStubs(unittest.TestCase):
    """每條矩陣允許的邊（caller→registered_target）都能跑得通，
    每條被禁的邊都被 PermissionDenied 擋。"""

    def setUp(self):
        from agent_core.agents import (
            build_default_registry, Agent, QUERY_MATRIX,
        )
        self.Agent = Agent
        self.QUERY_MATRIX = QUERY_MATRIX
        self.registry, self.middleware = build_default_registry()

    def test_every_allowed_edge_passes_permission(self):
        # 只驗 permission middleware：允許的 caller→target 不該被 PermissionDenied
        # 擋下；下游 agent 收到未知 intent 而 raise ValueError 反而證明
        # permission 已通過。
        from agent_core.agents import AgentRequest, PermissionDenied
        for caller, allowed in self.QUERY_MATRIX.items():
            for target in allowed:
                if target not in self.registry:
                    continue  # Orange / Red 沒註冊，跳過
                try:
                    self.middleware.dispatch(AgentRequest(
                        caller=caller, target=target,
                        intent="query.smoke", payload={},
                    ))
                except PermissionDenied:
                    self.fail(f"{caller.value} → {target.value} 應通過 permission")
                except (ValueError, KeyError):
                    pass  # 真實 agent 拒絕未知 intent，是正確行為

    def test_every_disallowed_edge_blocked(self):
        from agent_core.agents import AgentRequest, PermissionDenied
        for caller in self.Agent:
            if caller is self.Agent.RED:
                continue  # Red SUPER_ADMIN，無禁區
            allowed = self.QUERY_MATRIX[caller]
            for target in self.Agent:
                if target is caller:
                    continue  # 自己查自己是 Telegram 部門入口的基本能力
                if target in allowed:
                    continue
                if target not in self.registry:
                    continue  # 沒註冊的 target 會 raise LookupError 而非 PermissionDenied，跳過
                with self.assertRaises(
                    PermissionDenied,
                    msg=f"{caller.value} → {target.value} 應被擋",
                ):
                    self.middleware.dispatch(AgentRequest(
                        caller=caller, target=target,
                        intent="query.smoke", payload={},
                    ))


if __name__ == "__main__":
    unittest.main()
