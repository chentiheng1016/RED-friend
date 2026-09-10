"""Project Rainbow — 部門化 agent 框架（in-process，非分散式）.

這是 Phase 1 的純框架層，不影響任何現有 agent_core 行為。後續 Phase
會把 sample_tracker / customer_intel / quote* 等部門專屬模組搬進對應
color 子套件下。

公開介面：
  - permission_matrix.Agent           # 10 色 enum（含 RED super-admin / WHITE SoT）
  - permission_matrix.can_query       # 矩陣查詢
  - middleware.PermissionMiddleware   # 唯一允許的部門間呼叫管道
  - middleware.PermissionDenied
  - base_agent.BaseAgent              # 部門 agent 基底
  - registry.AgentRegistry            # color → instance 查表

設計原則：
  - 純 in-process，無 Redis / 無網路 IO（部門間呼叫就是 Python function call）
  - 不 import agent.py / 不 import 任何具體部門模組（避免循環）
  - dept_rules.py 的中文部門名透過 DEPT_TO_COLOR 對應到本檔的 Agent enum
"""
from agent_core.agents.permission_matrix import (
    Agent,
    QUERY_MATRIX,
    DEPT_TO_COLOR,
    can_query,
)
from agent_core.agents.middleware import (
    PermissionMiddleware,
    PermissionDenied,
    AgentRequest,
)
from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.registry import AgentRegistry
from agent_core.agents.wire import build_default_registry

__all__ = [
    "Agent",
    "QUERY_MATRIX",
    "DEPT_TO_COLOR",
    "can_query",
    "PermissionMiddleware",
    "PermissionDenied",
    "AgentRequest",
    "BaseAgent",
    "AgentRegistry",
    "build_default_registry",
]
