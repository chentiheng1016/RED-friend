"""Cross-process safe read-modify-write for JSON state files.

`_atomic_write_text` in logging_and_paths.py prevents torn reads (a reader
never sees a half-written file) but it does NOT prevent **lost updates**:
two daemons can both read v1, both mutate to v2, and the last writer wins
— the first writer's change is silently gone.

`locked_json` wraps the whole read → mutate → write window in an `fcntl`
exclusive lock on a sibling `.lock` file, serializing concurrent writers
across processes (launchd daemons + the interactive REPL + the web
server). The lock file is created on demand and never deleted; an empty
sentinel file is the standard fcntl pattern.

Pattern matches what `daemon_helpers.update_state` already does for
`daemon_state.json`, but exposed as a generic context manager so any
state file can opt in with three lines.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from typing import Any, Iterator

from agent_core.logging_and_paths import _atomic_write_text, logger


@contextlib.contextmanager
def locked_json(path: str, *, default: Any = None) -> Iterator[Any]:
    """Hold an exclusive cross-process lock while reading-mutating-writing JSON.

    Usage:
        with locked_json(MEMORY_FILE, default={}) as mem:
            mem[key] = value
        # written back atomically on exit (only if no exception)

    Semantics:
      - On enter: acquires `path + ".lock"` with fcntl.LOCK_EX (blocks
        until other writers release).
      - Yields the parsed JSON (or `default` if the file is missing /
        corrupt — corrupt is logged at WARNING and treated as empty so
        one bad write doesn't permanently brick the file).
      - On normal exit: writes the (possibly mutated) value back via
        `_atomic_write_text` and releases the lock.
      - On exception inside the `with`: the lock is released but NO
        write happens — the on-disk file is unchanged. This is the
        safer default; if the mutation half-failed, don't persist a
        corrupt half-state.

    Args:
      path: Absolute path to the JSON file.
      default: Returned if the file doesn't exist or fails to parse.
        Pass `{}` for dict-shaped state, `[]` for list-shaped state.
        Must be JSON-serializable (we round-trip it on first write).

    Footgun:
      The yielded value MUST be mutated in place. Rebinding the local name
      (e.g. `data = new_list` inside the `with`) does NOT update what the
      helper writes back — the helper still holds the original reference.
      For dicts: `data["k"] = v` / `data.update(...)` / `data.clear()` —
      all fine. For lists: `data.append(...)` / `data[:] = new_list` — fine.
      Just don't `data = ...` reassign.

    Notes:
      - The lock file (`path + ".lock"`) is created next to the data
        file and never deleted — that's normal for fcntl locks (file
        existence is decoupled from lock state).
      - Read-only access doesn't need this helper; a plain
        `json.load(open(path))` is safe because writes are atomic.
        Use `locked_json` only when you're going to mutate.
    """
    if default is None:
        default = {}
    lock_path = path + ".lock"
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = _copy_default(default)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "[state_io] %s 解析失敗（%s），以 default 覆蓋", path, exc
            )
            data = _copy_default(default)

        yield data

        _atomic_write_text(
            path, json.dumps(data, ensure_ascii=False, indent=2)
        )
    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def _copy_default(default: Any) -> Any:
    """Return a fresh copy of `default` so callers can't mutate the shared instance."""
    if isinstance(default, (dict, list)):
        return json.loads(json.dumps(default))
    return default
