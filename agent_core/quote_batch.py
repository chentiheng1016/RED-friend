"""Backward-compat shim — 實作已搬到 agent_core.agents.orange_sales.quote_batch.

外部既有的 `from agent_core.quote_batch import ...` 不必改。
新程式請改 import 真實位置：
    from agent_core.agents.orange_sales.quote_batch import ...
"""
from agent_core.agents.orange_sales.quote_batch import (  # noqa: F401
    batch_extract_quotes_from_parquet,
)
