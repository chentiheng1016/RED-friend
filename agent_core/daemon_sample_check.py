"""Backward-compat shim — 實作已搬到 agent_core.agents.green_sample_dev.daemon_sample_check.

新程式請改 import 真實位置：
    from agent_core.agents.green_sample_dev.daemon_sample_check import task_sample_check
"""
from agent_core.agents.green_sample_dev.daemon_sample_check import (  # noqa: F401
    task_sample_check,
)
