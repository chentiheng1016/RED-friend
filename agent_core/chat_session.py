"""Chat session state + startup-memory loader.

Holds the per-process Gemini chat handle,
its turn counter, and the MEMORY_FILE → startup_memory reader that feeds
build_persona_text.

Reference stability: `chat_state` is mutated in place (never reassigned)
so callers can take a snapshot once (e.g. `cs = chat_state`) and read
the current chat handle later without going stale.

Usage:
  import agent_core.chat_session as cs
  cs.chat_state["chat"] = build_chat_fn()
  cs.chat_state["turns"] += 1
  cs.maybe_compact(build_chat_fn)   # rebuild if threshold crossed
  mem = cs.load_startup_memory()    # for persona template
"""
import json
import os
import sys

from agent_core.logging_and_paths import MEMORY_FILE, logger


CHAT_HISTORY_COMPACT_THRESHOLD = 20

chat_state = {"chat": None, "turns": 0}


def maybe_compact(build_chat_fn):
    """Rebuild chat + reset turn counter if threshold crossed.

    build_chat_fn: zero-arg callable returning a fresh chat handle. Stays
    injected (rather than imported) because the real builder closes over
    agent.py's tools_list + agent_persona, which would create a circular
    import if we pulled them in here.
    """
    if chat_state["turns"] < CHAT_HISTORY_COMPACT_THRESHOLD:
        return
    logger.info("對話輪數達 %d，重建 chat 以釋放 context。", chat_state["turns"])
    chat_state["chat"] = build_chat_fn()
    chat_state["turns"] = 0


def load_startup_memory() -> str:
    """Read MEMORY_FILE (JSON dict) and render as bullet list for persona."""
    # 先合併版本控管的 seed 事實（memory_seed.json），讓「跟著 repo 走」的更正
    # 在每台 pull 過的機器上都進 persona。這裡只做 KV 合併（cheap、不碰向量庫）；
    # 向量索引由 agent._main() 帶 index fn 另外做。
    try:
        from agent_core.memory_seed import sync_memory_seed
        sync_memory_seed()
    except Exception:
        logger.debug("memory_seed 同步略過：%s", sys.exc_info()[1])
    if not os.path.exists(MEMORY_FILE):
        return "目前無記憶。"
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            mem_data = json.load(f)
        if isinstance(mem_data, dict) and mem_data:
            return "\n".join(f"- {k}: {v}" for k, v in mem_data.items())
    except Exception:
        logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])
    return "目前無記憶。"
