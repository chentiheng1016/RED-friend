"""Backward-compat shim — 實作已搬到 agent_core.agents.orange_sales.quote.

外部既有的 `from agent_core.quote import ...` 不必改，仍可運作。
新程式請改 import 真實位置：
    from agent_core.agents.orange_sales.quote import ...
"""
from agent_core.agents.orange_sales.quote import (  # noqa: F401
    _QUOTE_DIR,
    _QUOTE_CSV,
    _QUOTE_EXTRACTED_IDS,
    _QUOTE_CSV_FIELDS,
    _ensure_quote_dir,
    _load_extracted_ids,
    _save_extracted_ids,
    _append_quote_rows,
    _quote_extract_prompt,
    _parse_quote_json,
    _extract_quote_raw,
    _extract_quote_from_thread,
    _flatten_quote_to_rows,
    extract_quote_from_email,
    build_quote_history,
    query_quote_history,
)
