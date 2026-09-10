"""一次組好整個 agent registry — daemon 啟動時呼叫.

Red 不在這裡註冊：Red = orchestrator / daemon 本身，使用 registry 而不是
被註冊進 registry。其餘 9 色目前 Green / White / Orange / Yellow / Blue / Indigo
/ Purple / Gray 為 real agent，Black 仍為 stub，走同一個 lifecycle。

用法：
    from agent_core.agents.wire import build_default_registry
    registry, middleware = build_default_registry()
    # daemon 之後就用 middleware.dispatch / registry.get
"""
from __future__ import annotations

import threading
from typing import Tuple

from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.middleware import PermissionMiddleware
from agent_core.agents.registry import AgentRegistry


def build_default_registry() -> Tuple[AgentRegistry, PermissionMiddleware]:
    """建好 registry + middleware 並註冊所有已實作 / stub 的部門 agent.

    回傳 (registry, middleware)。Red 由呼叫端自行接 daemon 入口。
    """
    # Lazy import — 避免 stub agent 被 daemon 不需要的環境意外載入
    from agent_core.agents.green_sample_dev import GreenSampleDevAgent
    from agent_core.agents.white_legal import WhiteLegalAgent
    from agent_core.agents.orange_sales import OrangeSalesAgent
    from agent_core.agents.yellow_procurement import YellowProcurementAgent
    from agent_core.agents.blue_shipping import BlueShippingAgent
    from agent_core.agents.indigo_warehouse import IndigoWarehouseAgent
    from agent_core.agents.purple_accounting import PurpleAccountingAgent
    from agent_core.agents.gray_production import GrayProductionAgent
    from agent_core.agents.black_cashier import BlackCashierAgent

    registry = AgentRegistry()
    middleware = PermissionMiddleware(registry)
    registry.bind_middleware(middleware)

    agent_classes: tuple[type[BaseAgent], ...] = (
        GreenSampleDevAgent,       # real (Phase 2)
        WhiteLegalAgent,            # real (Phase 3a)
        OrangeSalesAgent,           # real (Phase 3c)
        YellowProcurementAgent,     # real (Procurement)
        BlueShippingAgent,          # real (Shipping)
        IndigoWarehouseAgent,       # real (Warehouse)
        PurpleAccountingAgent,     # real (Accounting)
        GrayProductionAgent,        # real (Gray Trigger)
        BlackCashierAgent,          # stub
    )
    for cls in agent_classes:
        registry.register(cls())

    return registry, middleware


_shared_lock = threading.Lock()
_shared_registry: Tuple[AgentRegistry, PermissionMiddleware] | None = None


def get_default_registry() -> Tuple[AgentRegistry, PermissionMiddleware]:
    """Process-wide 快取的 build_default_registry() 單例（thread-safe）。

    registry 無狀態，一份可安全共用於所有請求。web portal 的 /api/dept/* handler
    原本每個 request 都重 build（實例化 9 個部門 agent + middleware wiring，且在
    async handler 內同步阻塞 event loop）；改用此單例消除重建。
    """
    global _shared_registry
    if _shared_registry is None:
        with _shared_lock:
            if _shared_registry is None:
                _shared_registry = build_default_registry()
    return _shared_registry
