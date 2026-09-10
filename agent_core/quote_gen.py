"""Backward-compat shim — 實作已搬到 agent_core.agents.orange_sales.quote_gen.

外部既有的 `from agent_core.quote_gen import ...` 不必改。
新程式請改 import 真實位置：
    from agent_core.agents.orange_sales.quote_gen import ...
"""
from agent_core.agents.orange_sales.quote_gen import (  # noqa: F401
    generate_quote,
)
