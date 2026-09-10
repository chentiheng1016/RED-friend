"""Phase 1 框架測試 — 純邏輯，無 IO。

驗證：
  - permission matrix 完整性（每個 caller 都有 entry，矩陣值合法）
  - can_query 對 RED super-admin / WHITE SoT / 一般部門的行為
  - PermissionMiddleware 在違反矩陣時 raise PermissionDenied
  - middleware 把允許的呼叫真的轉到 target.handle_query
  - registry 重複 register 偵測 / late-bind middleware
  - DEPT_TO_COLOR 中文 → enum 對照
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core.agents import (  # noqa: E402
    Agent,
    AgentRegistry,
    AgentRequest,
    BaseAgent,
    DEPT_TO_COLOR,
    PermissionDenied,
    PermissionMiddleware,
    QUERY_MATRIX,
    can_query,
)


class _StubAgent(BaseAgent):
    def __init__(self, identity, response=None, error=None):
        self.identity = identity
        self._response = response if response is not None else {"ok": True}
        self._error = error
        self.calls = []
        super().__init__()

    def handle_query(self, intent, payload, trace_id):
        self.calls.append({"intent": intent, "payload": dict(payload), "trace_id": trace_id})
        if self._error is not None:
            raise self._error
        return {"from": self.identity.value, "intent": intent, "echo": dict(payload),
                "response": self._response}


class TestPermissionMatrix(unittest.TestCase):
    def test_every_agent_has_entry(self):
        for color in Agent:
            self.assertIn(color, QUERY_MATRIX, f"{color} 在 QUERY_MATRIX 缺 entry")

    def test_matrix_values_are_valid_agents(self):
        for caller, targets in QUERY_MATRIX.items():
            for t in targets:
                self.assertIsInstance(t, Agent, f"{caller} 矩陣含非 Agent: {t!r}")

    def test_red_is_super_admin(self):
        for color in Agent:
            self.assertTrue(can_query(Agent.RED, color), f"RED 應可查 {color}")

    def test_department_can_query_self(self):
        for color in Agent:
            if color is Agent.RED:
                continue
            self.assertTrue(can_query(color, color), f"{color.value} 應可查自己")

    def test_white_is_sot_no_outbound(self):
        self.assertEqual(QUERY_MATRIX[Agent.WHITE], frozenset())
        self.assertFalse(can_query(Agent.WHITE, Agent.ORANGE))

    def test_orange_matrix_matches_spec(self):
        # 規格：Orange → Yellow, Green, Blue, Indigo, White
        self.assertEqual(
            QUERY_MATRIX[Agent.ORANGE],
            frozenset({Agent.YELLOW, Agent.GREEN, Agent.BLUE, Agent.INDIGO, Agent.WHITE}),
        )
        self.assertTrue(can_query(Agent.ORANGE, Agent.WHITE))
        self.assertFalse(can_query(Agent.ORANGE, Agent.PURPLE))
        self.assertFalse(can_query(Agent.ORANGE, Agent.BLACK))
        self.assertFalse(can_query(Agent.ORANGE, Agent.GRAY))

    def test_purple_can_reach_black(self):
        # 規格：Purple → Black（會計可查出納）
        self.assertTrue(can_query(Agent.PURPLE, Agent.BLACK))
        # 反向：Black 可查 Purple（規格）
        self.assertTrue(can_query(Agent.BLACK, Agent.PURPLE))

    def test_green_minimal_access(self):
        # 規格：Green → Orange, Indigo, White only
        self.assertEqual(
            QUERY_MATRIX[Agent.GREEN],
            frozenset({Agent.ORANGE, Agent.INDIGO, Agent.WHITE}),
        )
        self.assertFalse(can_query(Agent.GREEN, Agent.YELLOW))
        self.assertFalse(can_query(Agent.GREEN, Agent.PURPLE))

    def test_dept_to_color_covers_dept_rules_keys(self):
        # dept_rules.py 目前的中文 key 都要能對到 color
        from agent_core.dept_rules import DEPT_RULES
        for dept_name in DEPT_RULES:
            self.assertIn(
                dept_name, DEPT_TO_COLOR,
                f"dept_rules 的 '{dept_name}' 在 DEPT_TO_COLOR 沒對應",
            )

    def test_dept_rules_classify_sales_to_orange_key(self):
        from agent_core.dept_rules import classify_dept

        primary, all_depts = classify_dept(
            "twsales@company.example",
            "Richter 報價 quotation follow-up",
        )

        self.assertEqual(primary, "業務")
        self.assertIn("業務", all_depts)

    def test_dept_rules_classify_sample_room_to_green_key(self):
        from agent_core.dept_rules import classify_dept

        primary, all_depts = classify_dept(
            "sampleroom@company.example",
            "SP-2026 樣品 sample update",
        )

        self.assertEqual(primary, "樣品室")
        self.assertIn("樣品室", all_depts)

    def test_twsales_shipping_subject_still_tiebreaks_to_shipping(self):
        from agent_core.dept_rules import classify_dept

        primary, all_depts = classify_dept(
            "twsales@company.example",
            "ETD / ETA shipping update",
        )

        self.assertEqual(primary, "船務")
        self.assertIn("業務", all_depts)
        self.assertIn("船務", all_depts)


class TestPermissionMiddleware(unittest.TestCase):
    def _build(self, *agents):
        registry = AgentRegistry()
        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)
        for a in agents:
            registry.register(a)
        return registry, mw

    def test_allowed_call_reaches_target(self):
        green = _StubAgent(Agent.GREEN)
        _, mw = self._build(green)
        result = mw.dispatch(AgentRequest(
            caller=Agent.ORANGE, target=Agent.GREEN,
            intent="query.sample_status", payload={"sp_id": "SP-1"},
        ))
        self.assertEqual(result["from"], "green")
        self.assertEqual(green.calls[0]["intent"], "query.sample_status")
        self.assertEqual(green.calls[0]["payload"], {"sp_id": "SP-1"})
        self.assertTrue(green.calls[0]["trace_id"])  # 自動產生

    def test_denied_call_raises(self):
        purple = _StubAgent(Agent.PURPLE)
        _, mw = self._build(purple)
        with self.assertRaises(PermissionDenied):
            # Orange → Purple 不在矩陣
            mw.dispatch(AgentRequest(
                caller=Agent.ORANGE, target=Agent.PURPLE,
                intent="query.invoice", payload={},
            ))
        self.assertEqual(purple.calls, [])  # target 沒被呼叫

    def test_red_can_call_anyone(self):
        white = _StubAgent(Agent.WHITE)
        _, mw = self._build(white)
        result = mw.dispatch(AgentRequest(
            caller=Agent.RED, target=Agent.WHITE,
            intent="query.contract", payload={"id": "C-1"},
        ))
        self.assertEqual(result["from"], "white")

    def test_unregistered_target_raises_lookup(self):
        registry = AgentRegistry()
        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)
        # GREEN 沒註冊
        with self.assertRaises(LookupError):
            mw.dispatch(AgentRequest(
                caller=Agent.RED, target=Agent.GREEN,
                intent="query.x", payload={},
            ))

    def test_target_exception_propagates(self):
        green = _StubAgent(Agent.GREEN, error=ValueError("boom"))
        _, mw = self._build(green)
        with self.assertRaises(ValueError):
            mw.dispatch(AgentRequest(
                caller=Agent.RED, target=Agent.GREEN,
                intent="x", payload={},
            ))

    def test_explicit_trace_id_preserved(self):
        green = _StubAgent(Agent.GREEN)
        _, mw = self._build(green)
        mw.dispatch(AgentRequest(
            caller=Agent.RED, target=Agent.GREEN,
            intent="x", payload={}, trace_id="trace-fixed-1",
        ))
        self.assertEqual(green.calls[0]["trace_id"], "trace-fixed-1")


class TestMiddlewareAudit(unittest.TestCase):
    """Verify command.* dispatches and permission denials emit BigQuery audit
    events via agent_core.exception_logger.log_event.

    Patching note: middleware does `from agent_core.exception_logger import
    log_event` lazily at call-time. We patch the attribute on the canonical
    module (not a shim); that's the path the lazy import resolves through.
    """

    def _build(self, *agents):
        registry = AgentRegistry()
        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)
        for a in agents:
            registry.register(a)
        return registry, mw

    def test_command_intent_emits_audit_event_on_success(self):
        from unittest import mock
        green = _StubAgent(Agent.GREEN)
        _, mw = self._build(green)
        with mock.patch("agent_core.exception_logger.log_event") as mlog:
            mw.dispatch(AgentRequest(
                caller=Agent.RED, target=Agent.GREEN,
                intent="command.update_sample", payload={"sp_id": "SP-1", "status": "ok"},
                trace_id="trace-cmd-1",
            ))
        self.assertEqual(mlog.call_count, 1, "exactly one audit emission expected")
        kwargs = mlog.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "command_audit")
        self.assertEqual(kwargs["severity"], "info")
        self.assertEqual(kwargs["source_agent"], "red")
        self.assertEqual(kwargs["trace_id"], "trace-cmd-1")
        self.assertIn("command_dispatched", kwargs["detail"])
        # Payload values must NOT leak — only top-level keys.
        self.assertEqual(set(kwargs["extra"]["payload_keys"]), {"sp_id", "status"})
        self.assertNotIn("SP-1", str(kwargs["extra"]))
        self.assertEqual(kwargs["extra"]["caller"], "red")
        self.assertEqual(kwargs["extra"]["target"], "green")
        self.assertEqual(kwargs["extra"]["intent"], "command.update_sample")
        self.assertIn("elapsed_ms", kwargs["extra"])

    def test_command_intent_emits_audit_event_on_failure(self):
        """Failure audit must capture exception **class** but NOT raw message.

        Exception messages can echo back user-controlled / sensitive payload
        content (e.g. file paths, customer fields). Class name is enough for
        BigQuery; full message stays in in-process logs joinable by trace_id.
        """
        from unittest import mock
        # Use a marker string in the exception that — if it ever leaks — is
        # easy to grep for. The test asserts it does NOT appear anywhere.
        leak_marker = "SECRET_PAYLOAD_DO_NOT_LOG_42"
        green = _StubAgent(Agent.GREEN, error=RuntimeError(leak_marker))
        _, mw = self._build(green)
        with mock.patch("agent_core.exception_logger.log_event") as mlog:
            with self.assertRaises(RuntimeError):
                mw.dispatch(AgentRequest(
                    caller=Agent.RED, target=Agent.GREEN,
                    intent="command.update_sample", payload={"sp_id": "SP-1"},
                    trace_id="trace-cmd-2",
                ))
        self.assertEqual(mlog.call_count, 1)
        kwargs = mlog.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "command_audit")
        self.assertEqual(kwargs["severity"], "error")
        self.assertIn("command_failed", kwargs["detail"])
        self.assertIn("RuntimeError", kwargs["detail"])  # class is OK
        self.assertEqual(kwargs["extra"]["error_type"], "RuntimeError")
        # The exception's raw message must NOT leak into BQ — neither
        # in detail nor in extra. trace_id is the join key for full text.
        self.assertNotIn(leak_marker, kwargs["detail"],
                         "raw exception message must not appear in audit detail")
        self.assertNotIn(leak_marker, str(kwargs["extra"]),
                         "raw exception message must not leak via extra dict")

    def test_query_intent_does_not_emit_audit(self):
        """Read-only query.* intents are too high-frequency to log; only
        command.* (state-changing) gets audited."""
        from unittest import mock
        green = _StubAgent(Agent.GREEN)
        _, mw = self._build(green)
        with mock.patch("agent_core.exception_logger.log_event") as mlog:
            mw.dispatch(AgentRequest(
                caller=Agent.RED, target=Agent.GREEN,
                intent="query.sample_status", payload={"sp_id": "SP-1"},
            ))
        mlog.assert_not_called()

    def test_permission_denied_emits_audit_for_any_intent(self):
        """Even a denied query.* is a security signal — log it."""
        from unittest import mock
        purple = _StubAgent(Agent.PURPLE)
        _, mw = self._build(purple)
        with mock.patch("agent_core.exception_logger.log_event") as mlog:
            with self.assertRaises(PermissionDenied):
                # ORANGE → PURPLE is not in the matrix.
                mw.dispatch(AgentRequest(
                    caller=Agent.ORANGE, target=Agent.PURPLE,
                    intent="query.invoice", payload={},
                    trace_id="trace-deny-1",
                ))
        self.assertEqual(mlog.call_count, 1)
        kwargs = mlog.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "permission_denied")
        self.assertEqual(kwargs["severity"], "warning")
        self.assertEqual(kwargs["trace_id"], "trace-deny-1")
        self.assertIn("permission_denied", kwargs["detail"])
        self.assertEqual(kwargs["extra"]["caller"], "orange")
        self.assertEqual(kwargs["extra"]["target"], "purple")

    def test_audit_failure_does_not_break_dispatch(self):
        """If exception_logger itself raises, dispatch must still return its
        result — auditing is best-effort and may not block the main path."""
        from unittest import mock
        green = _StubAgent(Agent.GREEN, response={"answer": 42})
        _, mw = self._build(green)
        with mock.patch(
            "agent_core.exception_logger.log_event",
            side_effect=RuntimeError("BigQuery unreachable"),
        ):
            result = mw.dispatch(AgentRequest(
                caller=Agent.RED, target=Agent.GREEN,
                intent="command.update_sample", payload={"sp_id": "SP-1"},
            ))
        # Dispatch result still propagates correctly.
        self.assertEqual(result["response"]["answer"], 42)
        # Target was actually called.
        self.assertEqual(len(green.calls), 1)


class TestBaseAgentAndRegistry(unittest.TestCase):
    def test_subclass_must_set_identity(self):
        class BadAgent(BaseAgent):
            def handle_query(self, intent, payload, trace_id):
                return None
        with self.assertRaises(TypeError):
            BadAgent()

    def test_query_peer_without_middleware_raises(self):
        # 直接 instantiate 沒走 registry → middleware 沒注入
        agent = _StubAgent(Agent.ORANGE)
        with self.assertRaises(RuntimeError):
            agent.query_peer(Agent.WHITE, "q", {})

    def test_query_peer_via_registry(self):
        orange = _StubAgent(Agent.ORANGE)
        green = _StubAgent(Agent.GREEN)

        registry = AgentRegistry()
        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)
        registry.register(orange)
        registry.register(green)

        result = orange.query_peer(
            target=Agent.GREEN,
            intent="query.sample_status",
            payload={"sp_id": "SP-2"},
        )
        self.assertEqual(result["from"], "green")
        self.assertEqual(green.calls[0]["payload"], {"sp_id": "SP-2"})

    def test_query_peer_denied_by_matrix(self):
        green = _StubAgent(Agent.GREEN)
        purple = _StubAgent(Agent.PURPLE)

        registry = AgentRegistry()
        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)
        registry.register(green)
        registry.register(purple)

        # Green → Purple 不在矩陣
        with self.assertRaises(PermissionDenied):
            green.query_peer(Agent.PURPLE, "q", {})

    def test_duplicate_register_raises(self):
        registry = AgentRegistry()
        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)
        registry.register(_StubAgent(Agent.GREEN))
        with self.assertRaises(ValueError):
            registry.register(_StubAgent(Agent.GREEN))

    def test_late_bind_middleware(self):
        # 先 register 再 bind — middleware 應補綁到既有 agent
        registry = AgentRegistry()
        orange = _StubAgent(Agent.ORANGE)
        green = _StubAgent(Agent.GREEN)
        registry.register(orange)
        registry.register(green)

        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)

        # Orange 應該能呼叫 Green（matrix 允許）
        result = orange.query_peer(Agent.GREEN, "q", {"k": "v"})
        self.assertEqual(result["from"], "green")

    def test_registry_protocol_methods(self):
        registry = AgentRegistry()
        mw = PermissionMiddleware(registry)
        registry.bind_middleware(mw)
        registry.register(_StubAgent(Agent.GREEN))
        self.assertEqual(len(registry), 1)
        self.assertIn(Agent.GREEN, registry)
        self.assertNotIn(Agent.PURPLE, registry)
        self.assertEqual(list(registry), [Agent.GREEN])


if __name__ == "__main__":
    unittest.main()
