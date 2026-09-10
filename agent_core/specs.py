"""Backward-compat shim — 實作已搬到 agent_core.agents.white_legal.specs.

外部既有的 `from agent_core.specs import ...` 不必改，仍可運作。
新程式請改 import 真實位置：
    from agent_core.agents.white_legal.specs import ...
"""
from agent_core.agents.white_legal.specs import (  # noqa: F401
    _SPECS_DIR,
    _specs_key,
    _specs_dir_for,
    _parse_spec_with_gemini,
    _load_spec_version,
    parse_spec_sheet,
    list_specs,
    compare_specs,
)
