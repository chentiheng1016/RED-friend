"""color → agent instance 的查表 + middleware 注入點.

啟動流程（未來在 agent_daemon 裡）：
    registry = AgentRegistry()
    middleware = PermissionMiddleware(registry)
    registry.bind_middleware(middleware)

    registry.register(GreenSampleDevAgent())
    registry.register(OrangeSalesAgent())
    ...
"""
from __future__ import annotations

from typing import Dict, Iterator, Optional

from agent_core.agents.permission_matrix import Agent
from agent_core.agents.middleware import PermissionMiddleware


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: Dict[Agent, "BaseAgent"] = {}  # noqa: F821
        self._middleware: Optional[PermissionMiddleware] = None

    def bind_middleware(self, middleware: PermissionMiddleware) -> None:
        self._middleware = middleware
        # 已 register 的 agent 補綁
        for a in self._agents.values():
            a.bind_middleware(middleware)

    def register(self, agent: "BaseAgent") -> None:  # noqa: F821
        color = agent.identity
        if color in self._agents:
            raise ValueError(f"agent {color.value} already registered")
        self._agents[color] = agent
        if self._middleware is not None:
            agent.bind_middleware(self._middleware)

    def get(self, color: Agent) -> Optional["BaseAgent"]:  # noqa: F821
        return self._agents.get(color)

    def __contains__(self, color: Agent) -> bool:
        return color in self._agents

    def __iter__(self) -> Iterator[Agent]:
        return iter(self._agents)

    def __len__(self) -> int:
        return len(self._agents)
