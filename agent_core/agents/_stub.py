"""空殼 (stub) 部門 agent — 共用基底.

對應 dept_rules 已有部門但 agent_core 尚無集中業務邏輯的 color；
Phase 3b 一次建好 6 個 stub，registry 立即可註冊全套，等業務邏輯
出現時再把對應子套件升級成 real impl（覆寫 agent.py）。

stub 行為：對任何 intent 回 {"status": "stub", "department": <color>,
"intent": intent, "echo": payload}；上游可用 status == "stub" 判斷是否
已升級成真實 impl。
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents.base_agent import BaseAgent


class StubDepartmentAgent(BaseAgent):
    """子類別只需設 identity，stub 行為由本類提供。"""

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        return {
            "status": "stub",
            "department": self.identity.value,
            "intent": intent,
            "echo": dict(payload),
            "trace_id": trace_id,
            "note": (
                f"{self.identity.value} agent 尚未實作 — 這是 stub 回應。"
                f" 升級時請覆寫 agent_core/agents/{self.identity.value}_*/agent.py"
            ),
        }
