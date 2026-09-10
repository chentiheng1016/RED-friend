#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 執行方式（按推薦順序）：
#   1. ./bin/agent          ← 最佳。launcher 會自動挑 repo 內的 .venv/bin/python
#   2. .venv/bin/python agent.py    ← 手動指定 venv
#   3. python3 agent.py     ← 只在 venv 已 activate 時才推薦，系統 Python 3.9 EOL 會壞
#
# 為何不用絕對路徑 shebang：這個檔要給別人 clone 到任意路徑用，hardcode
# /Users/xxx/RED/.venv 不可攜。請走 bin/agent launcher（它會用相對路徑找 venv）。
#
# --- 抑制套件棄用警告（log 乾淨多了）---
# urllib3 v2 vs LibreSSL 的警告用 message 字串過濾（不用 category class）
# 才不會因 import urllib3 時觸發警告。
import warnings as _warnings
_warnings.filterwarnings("ignore", category=FutureWarning)
_warnings.filterwarnings("ignore", category=DeprecationWarning)
_warnings.filterwarnings("ignore", message=".*OpenSSL 1\\.1\\.1.*")
_warnings.filterwarnings("ignore", message=".*LibreSSL.*")
_warnings.filterwarnings("ignore", message=".*past its end of life.*")
_warnings.filterwarnings("ignore", message=".*non-supported Python version.*")
# Gemini 3 回 response 常附 thought_signature，SDK 的提醒純雜訊
_warnings.filterwarnings("ignore", message=".*non-text parts in the response.*")
# --- 結束抑制 ---

import os
import platform
import time  # kept: tests patch self.agent.time
import base64  # kept: tests patch self.agent.base64
import numpy as np  # kept: tests patch self.agent.np

# ==========================================
# 🌐 跨平台偵測與套件懶載入
# ==========================================
_OS = platform.system()          # "Darwin" / "Windows" / "Linux"
_IS_MAC = _OS == "Darwin"
_IS_WIN = _OS == "Windows"
_IS_LINUX = _OS == "Linux"
_IS_DAEMON_MODE = os.environ.get("AGENT_DAEMON_MODE") == "1"

try:
    import keyring
except ImportError:
    keyring = None
try:
    import pyperclip
except ImportError:
    pyperclip = None
try:
    import mss
    import mss.tools  # noqa: F401
except ImportError:
    mss = None
try:
    import pygetwindow
except ImportError:
    pygetwindow = None
try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
except ImportError:
    pyautogui = None

from email.mime.text import MIMEText  # kept: tests patch self.agent.MIMEText

# ==========================================
# ⚙️ 核心配置（已搬至 agent_core/gemini_client.py — 下列 import 為相容層）
# ==========================================
from agent_core.gemini_client import (
    GEMINI_MODEL,
    _get_genai_types,
    _get_gemini_client,
    _gemini_generate,
)

# ==========================================
# 📜 日誌系統 + 路徑常數（已搬至 agent_core/logging_and_paths.py — 下列為相容 re-export）
# ==========================================
from agent_core.logging_and_paths import (
    LOG_FILE,
    MEMORY_FILE,
    install_tee_and_configure_logging,
    logger,
    startup_print as _startup_print,
)

# Tee 必須在 basicConfig 之前安裝，才能讓 StreamHandler 捕捉被 Tee 包裝的 stderr。
install_tee_and_configure_logging(install_tee=(__name__ == "__main__"))

# ==========================================
# ⌨️ REPL（鍵盤輸入）
# ==========================================
import agent_core.repl as _repl_mod
from agent_core.window_mgmt import bring_to_front

# ==========================================
# 🧠 犯錯學習 / 事實糾正記錄
# ==========================================
from agent_core.mistake_ledger import (
    _mistake_ledger, _load_mistake_ledger,
    correct_mistake, list_mistakes, delete_correction,  # tools (compat for Gemini tool list)
)

# ==========================================
# 🔑 Google 授權 + Drive / 行事曆 / 會議 briefing
# ==========================================
from agent_core.google_auth import get_google_credentials, get_service
from agent_core.google_suite import _find_next_meeting, search_drive_files, upload_to_drive
from agent_core.briefing import meeting_briefing, briefing_next_meeting

# ==========================================
# 🌐 YouTube
# ==========================================
from agent_core.youtube import get_youtube_transcript

# ==========================================
# 💻 系統 / 應用程式 / 剪貼簿
# ==========================================
from agent_core.apps import open_url, close_application
from agent_core.system_ctl import (
    control_mac_system,
    set_system_volume, show_notification, read_mac_clipboard,
)

# ==========================================
# 📧 Gmail + Email 分類 + 報價歷史 + 樣品追蹤
# ==========================================
from agent_core.gmail import (
    _extract_body, _gmail_create_draft,
    search_gmail, read_gmail, send_gmail, reply_gmail,
    download_gmail_attachment, summarize_inbox,
)
from agent_core.email_classify import (
    _classify_email_raw, _classify_email_for_lake, prioritized_inbox,
)
# 內部部門信件機器學習 (phase 78+) — milestone 1 為乾跑預覽
from agent_core.internal_emails import preview_internal_ingest  # noqa: F401
from agent_core.email_lake import _EMAIL_LAKE_DIR, _lake_load_df, _lake_append
from agent_core.quote import (
    _extract_quote_from_thread, extract_quote_from_email, build_quote_history,
)
from agent_core.sample_tracker import check_sample_deadlines

# ==========================================
# 🧠 長期記憶 + 行為準則 + 排程 + 健康檢查
# ==========================================
from agent_core.memory import (
    _migrate_memory_json_to_vector, _compile_behavior_policies,
    save_memory, load_memory, remember, recall, forget_memory, memory_stats,
    sync_memory_seed as _sync_memory_seed,
)
from agent_core.scheduler import _load_daemon_tasks, _save_daemon_tasks
from agent_core.health import health_check

# ==========================================
# 🌐 瀏覽器 + Telegram
# ==========================================
from agent_core.browser import (
    _BrowserSession,
    browser_open, browser_read, browser_click, browser_fill,
    browser_type, browser_press,
)
from agent_core.telegram import telegram_push

# ==========================================
# 🧩 Skills + 工具註冊
# ==========================================
from agent_core.skills import list_skills  # noqa: F401  (compat re-export for daemon / tools list)
from agent_core.tool_registry import (
    BUILTIN_TOOLS as _BUILTIN_TOOLS,
    tools_list,
    set_build_chat_fn as _tool_registry_set_build_chat_fn,
)
if len(tools_list) > len(_BUILTIN_TOOLS):
    from agent_core.mcp_bridge import MCP_TOOLS as _MCP_TOOLS
    _skill_count = len(tools_list) - len(_BUILTIN_TOOLS) - len(_MCP_TOOLS)
    _mcp_count = len(_MCP_TOOLS)
    parts = []
    if _skill_count:
        parts.append(f"{_skill_count} skill")
    if _mcp_count:
        parts.append(f"{_mcp_count} MCP")
    _startup_print(f"[tools] {' + '.join(parts)} = 外掛工具 {_skill_count + _mcp_count} 個；總工具數 {len(tools_list)}")

from agent_core.chat_session import (
    load_startup_memory as _load_startup_memory,
    chat_state as _chat_state,
    maybe_compact as _chat_session_maybe_compact,
)
startup_memory = _load_startup_memory()

from agent_core.persona import build_persona_text as _build_persona_text, build_chat as _persona_build_chat
agent_persona = _build_persona_text(startup_memory)

# Public compat surface:
# Keep this list intentionally small and explicit. These names are the symbols
# downstream modules/tests are allowed to rely on from `import agent`.
_PUBLIC_COMPAT_EXPORTS = [
    "MEMORY_FILE",
    "GEMINI_MODEL",
    "get_service",
    "send_gmail",
    "summarize_inbox",
    "search_gmail",
    "_gemini_generate",
    "_classify_email_raw",
    "_load_daemon_tasks",
    "_save_daemon_tasks",
    "tools_list",
    "_BUILTIN_TOOLS",
    "agent_persona",
    "recall",
    "telegram_push",
    "check_sample_deadlines",
    "health_check",
]


def _build_chat():
    # 每次重建都把已學習的行為準則自動附加到 system prompt，讓新規則立即生效
    policies = _compile_behavior_policies()
    return _persona_build_chat(tools_list, agent_persona + policies)


# Let tool_registry.reload_skills rebuild the chat with the fresh tools_list.
_tool_registry_set_build_chat_fn(_build_chat)


def _maybe_compact_chat():
    _chat_session_maybe_compact(_build_chat)

# REPL helpers + main_loop 都住在 agent_core/repl.py。agent._send_with_retry
# 是薄 wrapper，把 _build_chat 作為 build_chat_fn 注入給底層重試邏輯。
from agent_core.repl import _send_with_retry as _repl_send_with_retry


def _send_with_retry(user_text: str, max_attempts: int = 2):
    return _repl_send_with_retry(user_text, max_attempts=max_attempts, build_chat_fn=_build_chat)


# `agent.py` is now primarily a compatibility facade over `agent_core.*`.
# Export the explicit public surface above plus the runtime entry helpers that
# existing callers legitimately patch or invoke.
__all__ = _PUBLIC_COMPAT_EXPORTS + [
    "_build_chat",
    "_maybe_compact_chat",
    "_send_with_retry",
    "_main",
]


def _main():
    _load_mistake_ledger()
    _migrate_memory_json_to_vector()
    # 把版本控管的 seed 事實（memory_seed.json）也索引進向量庫，供 recall 用。
    # KV 合併在 load_startup_memory 已先做；這裡帶 index fn 補向量索引。
    try:
        _sync_memory_seed()
    except Exception as _e:
        logger.warning("memory_seed 向量同步略過：%s", _e)

    _chat_state["chat"] = _build_chat()

    bring_to_front()
    _repl_mod.print_startup_banner({
        "os": _OS,
        "cross_platform": [
            ("keyring", keyring is not None),
            ("pyperclip", pyperclip is not None),
            ("mss", mss is not None),
            ("pygetwindow", pygetwindow is not None),
            ("pyautogui", pyautogui is not None),
        ],
        "tools_count": len(tools_list),
        "mistake_counts": (len(_mistake_ledger["corrections"]), len(_mistake_ledger["log"])),
        "log_file": LOG_FILE,
    })
    print("大王，小紅已準備就緒，視窗已為您提到最前，請盡情吩咐！")

    _repl_mod.main_loop(_send_with_retry)

if __name__ == "__main__":
    _main()
