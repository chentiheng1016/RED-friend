"""Backward-compat shim — 實作已搬到 agent_core.agents.orange_sales.customer_intel.

外部既有的 `from agent_core.customer_intel import ...` 不必改。
新程式請改 import 真實位置：
    from agent_core.agents.orange_sales.customer_intel import ...
"""
from agent_core.agents.orange_sales.customer_intel import (  # noqa: F401
    customer_360,
    list_active_customers,
    customer_alerts,
)
