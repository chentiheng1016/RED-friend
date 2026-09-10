"""Production anomaly log — lightweight JSON store.

Uses `agent_core.state_io.locked_json` for cross-process safe append. The
previous hand-rolled fcntl + tempfile.mkstemp + os.replace pattern is now
encapsulated in the shared helper, so all JSON state files in the repo use
the same lock semantics.

recent_n bounds:
  list_anomalies() clamps recent_n to [1, _MAX_LIST] before slicing so that
  0 / negative / huge values from API payloads do not return the full list or
  unexpected subsets.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agent_core.logging_and_paths import DATA_DIR
from agent_core.state_io import locked_json

_STORE = Path(DATA_DIR) / "production_anomalies.json"
_MAX_LIST = 500  # upper bound for recent_n


def append_anomaly(record: dict[str, Any]) -> None:
    """Append one anomaly record (with timestamp) to the JSON log.

    locked_json holds an exclusive fcntl lock across read → append → write,
    so concurrent Gray daemon ticks cannot lose records or interleave writes.
    """
    with locked_json(str(_STORE), default=[]) as records:
        if not isinstance(records, list):
            # locked_json already recovers from JSON-parse failures, so this
            # only hits if the file is valid JSON but the wrong shape
            # (manually edited / migrated from a different schema). Crash
            # loudly — rebinding `records = []` won't propagate to the helper
            # (it would silently lose the append).
            raise TypeError(
                f"{_STORE} shape is {type(records).__name__}, expected list"
            )
        records.append({"logged_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **record})


def list_anomalies(recent_n: int = 20) -> list[dict]:
    """Return the most recent N anomaly records (1 ≤ recent_n ≤ _MAX_LIST)."""
    # Clamp: recent_n=0 would return the full list (Python -0 == 0 slice);
    # negative values return unexpected tails; huge values waste memory.
    recent_n = max(1, min(int(recent_n), _MAX_LIST))
    if not _STORE.exists():
        return []
    try:
        records = json.loads(_STORE.read_text(encoding="utf-8"))
        return records[-recent_n:] if isinstance(records, list) else []
    except Exception:
        return []
