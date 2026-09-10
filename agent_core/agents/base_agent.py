"""部門 agent 基底類別 — Phase 1 最小可用形.

具體部門 agent（Phase 2+ 才會建）繼承這個 class，覆寫 handle_query。
跨部門查詢透過 self.query_peer()，自動套 permission matrix。

刻意不在這層綁 Telegram / RAG / Gemini — 那些是橫向能力，現有 agent_core
模組已經提供，部門 agent 直接 import 使用即可。BaseAgent 只負責：
  1. 標識自己是哪個 color
  2. 提供 query_peer 走 middleware
  3. 定義 handle_query 介面（被 middleware 呼叫）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional

from agent_core.agents.permission_matrix import Agent
from agent_core.agents.middleware import PermissionMiddleware, AgentRequest


class BaseAgent(ABC):
    identity: Agent  # 子類別 class-level 設定，例如 identity = Agent.GREEN

    def __init__(self, middleware: Optional[PermissionMiddleware] = None) -> None:
        if not hasattr(self, "identity") or not isinstance(self.identity, Agent):
            raise TypeError(
                f"{type(self).__name__} 必須在 class 層設定 identity: Agent"
            )
        self._middleware = middleware

    def bind_middleware(self, middleware: PermissionMiddleware) -> None:
        """registry 在 register() 時呼叫，把 middleware 注入。"""
        self._middleware = middleware

    def query_peer(
        self,
        target: Agent,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: Optional[str] = None,
    ) -> Any:
        if self._middleware is None:
            raise RuntimeError(
                f"{self.identity.value} agent 尚未綁 middleware — "
                f"通常代表沒走 AgentRegistry.register()"
            )
        req_kwargs = {
            "caller": self.identity,
            "target": target,
            "intent": intent,
            "payload": payload,
        }
        if trace_id is not None:
            req_kwargs["trace_id"] = trace_id
        return self._middleware.dispatch(AgentRequest(**req_kwargs))

    @abstractmethod
    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        """被 middleware 呼叫 — 子類別實作。"""
