"""Backward-compat shim — 實作已搬到 agent_core.agents.green_sample_dev.sample_tracker.

外部既有的 `from agent_core.sample_tracker import ...` 不必改，仍可運作。
新程式請改 import 真實位置以避免 shim 累積：
    from agent_core.agents.green_sample_dev.sample_tracker import ...
"""
from agent_core.agents.green_sample_dev.sample_tracker import (  # noqa: F401
    _SAMPLE_TRACKER_PATH,
    _load_sample_tracker,
    _save_sample_tracker,
    track_sample,
    list_tracked_samples,
    update_sample_status,
    close_sample,
    delete_tracked_sample,
    check_sample_deadlines,
)
