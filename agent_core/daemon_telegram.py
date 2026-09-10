"""Telegram bot helpers extracted from agent_daemon.

Keep the daemon entrypoints thin so the remaining refactors can continue with
smaller, easier-to-test modules.
"""

from __future__ import annotations

import functools
import gc
import inspect
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests as _requests

# Re-exports for the split-out daemons/telegram subpackage. Kept here so the
# in-file fastpath / tg_handle_message / task_telegram_bot can keep referring
# to these symbols by their original (unprefixed) names without changing call
# sites in 50+ places. See each submodule for the real definitions + rationale.
#
# Future cleanup: once tg_handle_message and the fastpath also move out of
# this file, most of these re-exports can be deleted entirely (the only
# remaining consumers will be the subpackage itself).
from agent_core.daemons.telegram.media_detect import (
    # Constants
    _TG_PENDING_MEDIA_DOWNLOAD_KEY,
    _TG_PROTECTED_MEDIA_URL_KEY,
    _TG_LAST_MEDIA_URL_KEY,
    _TG_PENDING_MEDIA_DOWNLOAD_TTL_S,
    _AUTO_DELIVER_MEDIA_EXTS,
    _AUTO_DELIVER_AUDIO_EXTS,
    _MEDIA_DOWNLOAD_URL_RE,
    _MEDIA_DOWNLOAD_VERBS,
    _MEDIA_AUDIO_DOWNLOAD_VERBS,
    _MEDIA_DOWNLOAD_DOMAINS,
    _PROTECTED_MEDIA_DOMAINS,
    _PROTECTED_MEDIA_PATH_DOMAINS,
    _UNSUPPORTED_MEDIA_DOMAINS,
    # URL parsing / cleaning
    _clean_url_token,
    _extract_media_urls,
    _is_hls_manifest_url,
    _extract_user_agent_override,
    # Verb / domain detection
    _has_media_download_verb,
    _has_audio_download_verb,
    _is_youtube_url,
    _host_matches_domain,
    _protected_media_provider,
    _protected_media_download_reply,
    _unsupported_media_reason,
    _unsupported_media_download_reply,
    # Chat-state URL memory
    _remember_media_url_in_chat,
    _recent_media_url_from_chat,
    # Tool routing
    _extract_direct_media_download_url,
    _hls_download_tool_for_text,
    _media_download_tool_for_url,
    _media_download_args_for_url,
    _confirmation_help_for_media_download,
)
from agent_core.daemons.telegram.history import (
    # Constants + lock
    _TG_CHAT_HISTORY_MAX_TURNS_PER_CHAT,
    _TG_CHAT_HISTORY_MAX_CHARS_EMPLOYEE,
    _TG_CHAT_HISTORY_TTL_S,
    _TG_CHAT_HISTORY_DIR,
    _TG_CHAT_HISTORY_FILE_LEGACY,
    _tg_chat_history_lock,
    _tg_chat_history_legacy_cache,
    # File helpers
    _chat_history_path,
    _atomic_write_json,
    # Load / persist / migrate
    _load_tg_chat_history_entry,
    _load_tg_chat_histories_legacy,
    _clear_tg_chat_history_legacy,
    _migrate_legacy_entry_if_present,
    _persist_tg_chat_history_entry,
    # Public API used by tg_handle_message + tg_build_chat
    _record_tg_chat_turn,
    _load_tg_chat_history_for_rebuild,
    _clear_tg_chat_history,
)
from agent_core.env_utils import env_float as _env_float, env_int as _env_int
from agent_core.log_redact import redact_log_line
from agent_core.telegram_format import markdown_to_telegram_html, telegram_text_chunks
# 系統層停滯看門狗的「任務心跳」寫入端：任務執行期間 begin()→active、過程
# pulse()、結束 idle()。看門狗（agent_core.daemon_watchdog）只在 active 且
# ts 停滯超過門檻才判定卡死，idle 永不誤觸。是 PR #116 應用層 deadline 的
# belt-and-suspenders（in-process 機制全失效時仍能由系統層 kickstart 救回）。
from agent_core.daemon_watchdog import TelegramHeartbeat

# launchctl unload sends SIGTERM. Without a handler, the daemon dies
# mid-message — the user's reply is lost and the cached HTTP session
# leaks until the OS reclaims it. The handler flips an Event the main
# loop checks each iteration, so the next bottom-of-loop pass cleanly
# saves offset and closes the session before returning.
_shutdown_requested = threading.Event()


class _ShutdownInterrupt(BaseException):
    """Raised in the main thread by the SIGTERM handler to break out of a
    blocking long-poll.

    Inherits **BaseException**, not Exception, on purpose: the poll sits inside
    a try whose except clauses treat everything as a transient network error
    and retry. An Exception subclass would be swallowed there and the shutdown
    would be delayed all over again.
    """


# True only while the main thread is blocked in the getUpdates long-poll.
# A one-element list, not an Event: this is read from inside a signal handler,
# where anything that takes a lock can deadlock against the very thread the
# handler interrupted. Bare list-item read/write is atomic under the GIL.
_in_long_poll = [False]


def _handle_sigterm(signum, frame):
    """SIGTERM: ask for shutdown, and cut the long-poll short if we're in one.

    Why the raise (2026-08-26): the flag alone is only *checked* at the top of
    the loop, so a SIGTERM arriving mid-poll waits out the rest of the poll —
    up to RED_TG_LONGPOLL_TIMEOUT_S (default 8s). launchd only grants 5s before
    SIGKILL, so roughly a third of restarts killed the bot before it could save
    its offset and close the session. Measured on the live fleet: 7×
    "Service did not exit 5 seconds after SIGTERM. Sending SIGKILL." in 3 days,
    across 6 different colour bots.

    The raise is deliberately scoped to the poll. A SIGTERM arriving while a
    message is being handled must NOT abort it mid-reply — that is the exact
    data loss the handler was written to prevent — so there we still just set
    the flag and let the current message finish.
    """
    _shutdown_requested.set()
    if _in_long_poll[0]:
        raise _ShutdownInterrupt()


def _install_sigterm_handler() -> bool:
    """Register the SIGTERM handler.

    Returns True on success. signal.signal can only be called from the
    main thread; in test environments running in a worker thread we
    silently skip — the production daemon always runs as the main
    thread of its launchd-spawned process so the install succeeds.
    """
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
        return True
    except (ValueError, OSError):
        return False


def _reset_shutdown_state() -> None:
    """Test seam — clear the shutdown flag between unit tests so a
    previous test's set() doesn't poison the next run."""
    _shutdown_requested.clear()
    _in_long_poll[0] = False


# 關機時的最長反應延遲：退避睡眠切成這麼大一塊、每塊之間看一次關機旗標。
_SHUTDOWN_POLL_SLICE_S = 0.25


def _sleep_unless_shutdown(seconds: float) -> bool:
    """睡 seconds 秒，但 SIGTERM 一到最多 0.25 秒就醒。回 True 表示是被關機叫醒的。

    為什麼不是 ``time.sleep``（2026-08-26，#441 的同一個失效模式、不同等待點）：
    #441 讓 SIGTERM 能當場打斷 getUpdates 長輪詢，但主迴圈還有第二種等待 ——
    網路錯誤退避 ``min(5 + consecutive_net_errors, 60)``，**最長 60 秒**。
    ``time.sleep`` 在 PEP 475 下被訊號中斷後會用剩餘時間重睡（handler 沒有丟例外
    時就是這樣），所以 SIGTERM 打在退避中一樣得等它睡完，launchd 只給 5 秒。
    而退避不是罕見狀態：live log 裡 ``🌐 網路暫時問題`` 一整片，連續錯誤越多睡越久。

    為什麼不用 ``_shutdown_requested.wait(timeout)``：Event 內部要拿 Condition 的
    鎖，而 handler 是在**同一條**主執行緒上跑的（訊號中斷了誰就在誰身上執行），
    在那裡拿鎖有機會跟被中斷的自己對撞 —— 這正是 #441 的 ``_in_long_poll`` 寧可
    用一元素 list 也不用 Event 的理由。這裡用「短睡＋讀旗標」完全不碰 handler：
    handler 維持 #441 的樣子（只在長輪詢中丟例外），是否醒來由這一端決定。
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not _shutdown_requested.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_SHUTDOWN_POLL_SLICE_S, remaining))
    return True


# Heartbeat pulse keeps the watchdog alive during long Gemini calls,
# but a stuck SDK call still leaves the user staring at silence forever.
# Bound the chat.send_message wait so a hang surfaces as a clear error
# instead of an indefinite no-reply.
#
# Why 180s, not 90s: the SDK's send_message can legitimately spend that
# long executing automatic function calls — uploading a big Gmail
# attachment, scanning many files via run_shell, etc. Going short would
# false-positive on legitimate slow tools. Codex review on PR #29 noted
# the daemon thread is still alive after the deadline — the SDK call
# CAN finish in the background — so the user-facing reply also has to
# warn against blind retry on side-effecting actions.
_GEMINI_INFERENCE_TIMEOUT_S = 180


# Transient-error classification is shared with _gemini_generate via
# gemini_client._is_transient_error (single source of truth — see that module).
_GEMINI_SEND_MESSAGE_MAX_ATTEMPTS = 3


# ── 任務整體 deadline（2026-06-13 紅 bot 卡死事故）────────────────────
# 180s 軟超時只是把對話讓出來：推理線程（_send_message_with_timeout 裡的
# daemon thread）仍在背景跑 AFC 工具迴圈。事故當晚 Google 503 風暴讓該線程
# 卡死在無 timeout 的等待上（對 Google 的連線全部 CLOSE_WAIT 沒人收屍），
# 任務「永遠不會完成」、之後的長任務全部排不進去。三道防線：
#   1. gemini_client 的 http_options timeout — 單一 HTTP 呼叫有界
#   2. 整體 deadline + 監看線程（_spawn_task_abandon_monitor）— 軟超時後
#      繼續盯著背景任務：期限內完成就補送結果、失敗就回報例外、超過
#      deadline 就放棄並告訴使用者「請重問」
#   3. 工具 deadline 閘門（_wrap_tools_with_deadline_gate）— 放棄後殘餘
#      線程的每個後續 tool call 立即失敗。genai SDK 會把工具例外包成
#      function response 回給模型（不會中斷 send_message），但配合第 1 道
#      的有界 HTTP 與 AFC maximum_remote_calls 上限，線程必然在有限時間內
#      收斂終結。Python 殺不了 thread，這是唯一合法的「牙齒」。
_TG_TASK_DEADLINE_DEFAULT_S = 900.0


def _tg_inference_soft_timeout_s() -> float:
    """軟超時：主迴圈最多同步等推理這麼久，之後讓出對話、任務轉背景。"""
    return _env_float(
        "RED_TG_INFERENCE_TIMEOUT_S",
        float(_GEMINI_INFERENCE_TIMEOUT_S),
        min_value=0.01,
        max_value=3600.0,
    )


def _tg_task_deadline_s() -> float:
    """任務整體 deadline：超過就真正放棄（通知使用者 + 閘門斷頭）。"""
    return _env_float(
        "RED_TG_TASK_DEADLINE_S",
        _TG_TASK_DEADLINE_DEFAULT_S,
        min_value=0.05,
        max_value=86400.0,
    )


# 推理線程在自己的 thread-local 記下絕對 deadline（time.monotonic 基準）。
# 閘門讀「呼叫它的那條線程」自己的值 — 被放棄的舊線程看到的是自己的舊
# deadline（已過期 → 拒絕），新任務的新線程有自己的新值，互不干擾。
_task_deadline_local = threading.local()


class TelegramTaskAbandoned(RuntimeError):
    """任務超過整體 deadline 後，工具閘門拒絕執行時拋出。"""


def _check_task_deadline(tool_name: str) -> None:
    deadline = getattr(_task_deadline_local, "deadline", None)
    if deadline is not None and time.monotonic() > deadline:
        raise TelegramTaskAbandoned(
            f"task abandoned: 任務已超過整體 deadline，{tool_name} 拒絕執行。"
            "請直接回覆目前已知的內容並結束，不要再呼叫任何工具。"
        )


def _make_deadline_gated_tool(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def gated(*args, **kwargs):
        _check_task_deadline(getattr(fn, "__name__", "tool"))
        return fn(*args, **kwargs)

    # genai SDK 從 signature 建 function declaration — 必須原樣保留。
    # functools.wraps 已複製 __name__/__doc__/__annotations__/__dict__
    # （含 background_safe、_tg_auth_wrapped 等 marker attrs）。
    try:
        gated.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        pass
    gated._tg_deadline_gated = True  # type: ignore[attr-defined]
    return gated


def _wrap_tools_with_deadline_gate(tools: list) -> list:
    """把 deadline 閘門包在每個 tool 最外層（先於 tg_auth 確認門）。

    最外層的理由：被放棄的殘餘線程連「跟使用者要 +確認」都不該發生。
    """
    out = []
    for fn in tools:
        if not callable(fn) or getattr(fn, "_tg_deadline_gated", False):
            out.append(fn)
            continue
        out.append(_make_deadline_gated_tool(fn))
    return out


def _finalize_late_reply(
    raw_text: str,
    *,
    chat_id: str,
    user_text: str,
    downloads_before: set | frozenset | None,
    inference_start_ts: float,
) -> str:
    """遲到結果送出前走跟前景回覆一樣的收尾（Codex P2 on PR #116）：

    citation guard 加註 → 媒體補送 → 對話歷史記錄。沒有這層的話，最容易
    含客戶/規格/價格事實宣稱的長查詢（正是會超時轉背景的那種）反而繞過
    guard 裸送。跟前景唯一的差異：guard 不做 RED_CITATION_RETRY 的 LLM
    重答（會話已在軟超時時重置，沒有可用 chat），一律 banner-only。
    """
    text = (raw_text or "").strip()
    if not text:
        return text
    try:
        from agent_core.citation_guard import (
            annotate_with_warning,
            check_citation,
        )
        guard = check_citation(text)
        if not guard.ok:
            print(f"[citation_guard] flagged late reply: {guard.reason}")
            text = annotate_with_warning(text, guard)
    except Exception as exc:
        print(f"[citation_guard] ⚠️ late guard 自己失敗（送原回覆）：{exc}")
    # 引用回饋（Phase 3）：遲到回覆同樣記引用（跟主回覆同一 ledger）。
    try:
        from agent_core.citation_feedback import record_citations_from_reply
        record_citations_from_reply(text)
    except Exception as exc:
        print(f"[citation_feedback] ⚠️ late 記錄失敗（不影響回覆）：{exc}")
    # 背景任務下載的檔不能擱淺在 ~/Downloads。前景已送出的檔送完即刪，
    # 所以這裡再掃一次不會重複遞送。
    auto_delivery = ""
    if chat_id:
        try:
            new_files = _scan_new_media_downloads_since(
                inference_start_ts, downloads_before or set()
            )
            if new_files:
                auto_delivery = _deliver_new_downloads_via_telegram(
                    new_files, str(chat_id)
                )
        except Exception as exc:
            print(f"[tg bot] ⚠️ late 媒體補送失敗：{exc}")
    # 記錄這輪問答，下次 session rebuild 才看得到背景跑完的答案。
    if chat_id:
        try:
            _record_tg_chat_turn(str(chat_id), user_text, text)
        except Exception as exc:
            print(f"[tg bot] ⚠️ late chat 記憶寫入失敗（不影響送出）：{exc}")
    return text + auto_delivery


def _spawn_task_abandon_monitor(
    *,
    done: threading.Event,
    holder: dict,
    deadline_mono: float,
    started_mono: float,
    deadline_s: float,
    caller: str,
    late_notify: Callable[[str], Any] | None,
    late_finalize: Callable[[str], str] | None = None,
) -> threading.Thread:
    """軟超時後盯著仍在跑的推理線程，三種結局都有人收尾：

    1. deadline 內完成 → late_finalize 收尾（citation guard / 媒體補送 /
       歷史記錄）後把遲到的結果補送給使用者（並記成本）
    2. deadline 內拋例外 → 把例外回報給使用者（runner 死了不能沒人知道）
    3. 超過 deadline → 通知「已放棄，請重問」；此後閘門讓殘餘線程的每個
       tool call 立即失敗，線程在有限時間內自行終結
    """

    def _notify(message: str) -> None:
        if late_notify is None:
            return
        try:
            late_notify(message)
        except Exception as exc:
            print(f"[tg bot] ⚠️ 背景任務通知送出失敗：{exc}", flush=True)

    def _monitor() -> None:
        remaining = deadline_mono - time.monotonic()
        finished = done.wait(timeout=max(0.0, remaining))
        elapsed = time.monotonic() - started_mono
        if not finished:
            print(
                f"[tg bot] ⏰ 任務超過整體 deadline（{deadline_s:.0f}s），已放棄"
                f"（caller={caller}）",
                flush=True,
            )
            _notify(
                f"⏰ 小紅這次的任務超過 {deadline_s / 60:.0f} 分鐘整體上限，"
                "已放棄、不會再有結果。\n"
                "  • 查詢類請直接重問\n"
                "  • 若任務含會產生副作用的動作（send_gmail / run_shell / browser_*），\n"
                "    重發前先到對應地方確認是否已執行過，避免重複。"
            )
            return
        if "exc" in holder:
            exc = holder["exc"]
            print(
                f"[tg bot] ❌ 背景任務最後失敗（{elapsed:.0f}s）："
                f"{type(exc).__name__}: {str(exc)[:200]}",
                flush=True,
            )
            _notify(
                f"❌ 剛剛超時轉背景的任務最後失敗（跑了 {elapsed:.0f}s）：\n"
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
            return
        resp = holder.get("value")
        try:
            from agent_core import cost_tracker
            cost_tracker.record_chat_response(resp, caller=caller)
        except Exception:
            pass
        text = (getattr(resp, "text", "") or "").strip()
        if text and late_finalize is not None:
            try:
                text = late_finalize(text)
            except Exception as exc:
                print(f"[tg bot] ⚠️ late 收尾失敗（送原文）：{exc}", flush=True)
        text = text or "（任務完成，但沒有文字結果）"
        print(f"[tg bot] ✅ 背景任務完成（{elapsed:.0f}s），補送結果", flush=True)
        _notify(f"✅ 剛剛超時轉背景的任務完成了（跑了 {elapsed:.0f}s）：\n\n{text}")

    monitor = threading.Thread(
        target=_monitor, daemon=True, name="tg-task-abandon-monitor",
    )
    monitor.start()
    return monitor


_TG_GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
_TG_ID_COMMANDS = frozenset({"/whoami", "/tgid", "/id"})
_TG_SIMPLE_COMMANDS = frozenset({
    "/start", "/help", "/用法", "/new", "/reset", "/新對話", "/whoami", "/tgid", "/id",
})
_TG_JOIN_APPROVE_COMMANDS = frozenset({"/approve_tg", "/tgapprove", "/核准加入"})
_TG_JOIN_REJECT_COMMANDS = frozenset({"/reject_tg", "/tgreject", "/拒絕加入"})
_TG_JOIN_PENDING_COMMANDS = frozenset({"/pending_tg", "/待審加入"})
_TG_JOIN_COMMANDS = (
    _TG_JOIN_APPROVE_COMMANDS | _TG_JOIN_REJECT_COMMANDS | _TG_JOIN_PENDING_COMMANDS
)
_TG_JOIN_CALLBACK_PREFIX = "tgjoin"
_TG_JOIN_NOTIFY_INTERVAL_S = 5 * 60
_TG_JOIN_TTL_S = 7 * 24 * 60 * 60
_TG_JOIN_EMAIL_DOMAIN = "telegram.local"


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {
        "1", "true", "yes", "y", "on",
    }


def _telegram_bot_username() -> str:
    """Optional bot username used for group mentions and /cmd@bot commands."""
    for name in ("RED_TELEGRAM_BOT_USERNAME", "TELEGRAM_BOT_USERNAME"):
        raw = os.environ.get(name, "")
        username = raw.strip().lstrip("@")
        if username:
            return username
    return ""


def _telegram_start_reply(telegram_actor: Mapping[str, Any] | None = None) -> str:
    """Return a deterministic welcome for Telegram bot start/help commands."""
    actor_color = ""
    if isinstance(telegram_actor, Mapping):
        actor_color = str(telegram_actor.get("color") or "").strip().lower()
    default_color = str(os.environ.get("RED_TELEGRAM_DEFAULT_ACTOR_COLOR") or "").strip().lower()
    color = default_color or actor_color

    reply = _telegram_start_reply_base(color)
    if color and color != "red":
        # 員工色 bot 已開放唯讀自然語言查詢 — /start 文案補提示。
        try:
            from agent_core.dept_nlp_query import capability_hint, nlp_query_enabled
            hint = capability_hint(color) if nlp_query_enabled() else ""
        except Exception:
            hint = ""
        if hint:
            reply += "\n\n💬 " + hint
    return reply


def _telegram_start_reply_base(color: str) -> str:

    if color == "purple":
        return (
            "Purple 會計部門 Agent 已啟動。\n\n"
            "常用指令：\n"
            "  /whoami\n"
            "  /accounting summary\n"
            "  /accounting records <關鍵字>\n"
            "  /accounting invoices <對象或關鍵字>\n"
            "  /accounting payments <PO 或關鍵字>\n"
            "  /payment <PO 或關鍵字>\n"
            "  /dept purple query.profile\n\n"
            "這個入口只做會計資料查詢，不付款、不寄信、不寫入 ERP。"
        )

    if color == "gray":
        return (
            "Gray 生產管理 Agent 已啟動。\n\n"
            "常用指令：\n"
            "  /whoami\n"
            "  /production profile\n"
            "  /production status\n"
            "  /production history\n"
            "  /production report <json_payload> +確認\n"
            "  /dept gray query.profile\n\n"
            "回報異常會觸發跨部門查詢與通知；未加 +確認 時只會預覽。"
        )

    if color == "black":
        return (
            "Black 出納部門 Agent 已啟動。\n\n"
            "常用指令：\n"
            "  /whoami\n"
            "  /cashier profile\n"
            "  /cashier summary\n"
            "  /cashier payments <對象或關鍵字>\n"
            "  /cashier receipts <對象或關鍵字>\n"
            "  /cashier alerts\n"
            "  /dept black query.profile\n\n"
            "這個入口只做出納資料查詢，不付款、不寄信、不寫入 ERP。"
        )

    if color == "white":
        return (
            "White 法務 SoT Agent 已啟動。\n\n"
            "常用指令：\n"
            "  /whoami\n"
            "  /legal profile\n"
            "  /legal specs\n"
            "  /legal spec <customer> <product_model>\n"
            "  /legal search <query>\n"
            "  /dept white query.profile\n\n"
            "這個入口只做法務/規格/合約查詢；解析與同步 Drive 請走 /ingest white ... +確認。"
        )

    if color == "green":
        return "Green 樣品室 Agent 已啟動。\n\n常用指令：/dev profile、/dev samples open、/dev sample <sample_id>、/whoami"
    if color == "orange":
        return "Orange 業務部門 Agent 已啟動。\n\n常用指令：/sales customer <客戶>、/sales active、/sales alerts、/whoami"
    if color == "blue":
        return "Blue 船務部門 Agent 已啟動。\n\n常用指令：/shipping profile、/shipping eta <PO|AWB>、/shipping alerts、/whoami"
    if color == "yellow":
        return "Yellow 採購部門 Agent 已啟動。\n\n常用指令：/purchase profile、/purchase pos、/purchase eta <PO|材料>、/whoami"
    if color == "indigo":
        return "Indigo 倉庫部門 Agent 已啟動。\n\n常用指令：/warehouse profile、/warehouse inventory、/warehouse stock <材料>、/whoami"

    return (
        "小紅 Telegram 入口已啟動。\n\n"
        "常用指令：\n"
        "  /whoami\n"
        "  /dept <color> query.<name> [json_payload]\n"
        "  /new\n"
    )


def _normalize_telegram_command_text(text: str, *, bot_username: str = "") -> str:
    """Strip Telegram's /command@BotName suffix when it targets this bot.

    In groups, Telegram commonly sends commands as `/dept@MyBot ...`.
    Command handlers in this codebase expect plain `/dept ...`.
    """
    s = (text or "").strip()
    if not s.startswith("/"):
        return s
    head, sep, rest = s.partition(" ")
    if "@" not in head:
        return s
    command, mention = head.split("@", 1)
    mention = mention.strip().lstrip("@").lower()
    expected = (bot_username or "").strip().lstrip("@").lower()
    if expected and mention != expected:
        return s
    return command + (sep + rest if sep else "")


def _telegram_text_mentions_bot(text: str, *, bot_username: str = "") -> bool:
    username = (bot_username or "").strip().lstrip("@").lower()
    if not username:
        return False
    return f"@{username}" in (text or "").lower()


def _telegram_is_explicit_command(text: str, *, bot_username: str = "") -> bool:
    normalized = _normalize_telegram_command_text(text, bot_username=bot_username)
    head = (normalized.strip().split(maxsplit=1) or [""])[0].lower()
    if head in _TG_JOIN_COMMANDS:
        return True
    if head in _TG_SIMPLE_COMMANDS:
        return True
    try:
        from agent_core.agents.telegram_command import (
            is_dept_command,
            is_black_cashier_command,
            is_blue_shipping_command,
            is_green_agent_command,
            is_gray_production_command,
            is_indigo_warehouse_command,
            is_ingest_command,
            is_orange_sales_command,
            is_purple_accounting_command,
            is_white_legal_command,
            is_yellow_procurement_command,
        )
        return (
            is_dept_command(normalized)
            or is_black_cashier_command(normalized)
            or is_blue_shipping_command(normalized)
            or is_green_agent_command(normalized)
            or is_gray_production_command(normalized)
            or is_indigo_warehouse_command(normalized)
            or is_orange_sales_command(normalized)
            or is_purple_accounting_command(normalized)
            or is_white_legal_command(normalized)
            or is_yellow_procurement_command(normalized)
            or is_ingest_command(normalized)
        )
    except Exception:
        return head in {
            "/dept", "/dev", "/green", "/sample", "/sampledev", "/sampleroom",
            "/shipping", "/ship", "/blue", "/logistics", "/船務", "/sales", "/orange",
            "/biz", "/purchase", "/procurement", "/yellow", "/po", "/採購",
            "/warehouse", "/stock", "/inventory", "/indigo", "/倉庫",
            "/accounting", "/acct", "/purple", "/invoice", "/payment", "/會計",
            "/production", "/prod", "/factory", "/gray", "/anomaly", "/生產",
            "/cashier", "/cash", "/black", "/treasury", "/expense", "/出納", "/收支",
            "/legal", "/white", "/sot", "/spec", "/specs", "/contract", "/docs",
            "/法務", "/規格", "/合約",
            "/approve_tg", "/tgapprove", "/核准加入", "/reject_tg", "/tgreject",
            "/拒絕加入", "/pending_tg", "/待審加入",
            "/ingest",
        }


def _telegram_should_ignore_group_message(
    message: Mapping[str, Any],
    *,
    bot_username: str = "",
) -> bool:
    """Return True when a group update is ordinary chatter.

    Authorized department group chats should not burn rate-limit quota,
    download attachments, or wake Gemini unless the message is an explicit
    command or tags the bot.
    """
    chat = message.get("chat") if isinstance(message, Mapping) else {}
    chat_type = str((chat or {}).get("type") or "").lower()
    if chat_type not in _TG_GROUP_CHAT_TYPES:
        return False
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        return True
    return not (
        _telegram_is_explicit_command(text, bot_username=bot_username)
        or _telegram_text_mentions_bot(text, bot_username=bot_username)
    )


def _telegram_whoami_reply(
    *,
    chat_id: str = "",
    telegram_actor: Mapping[str, Any] | None = None,
    telegram_message: Mapping[str, Any] | None = None,
) -> str:
    msg = telegram_message if isinstance(telegram_message, Mapping) else {}
    chat = msg.get("chat") if isinstance(msg.get("chat"), Mapping) else {}
    sender = msg.get("from") if isinstance(msg.get("from"), Mapping) else {}
    actor = telegram_actor if isinstance(telegram_actor, Mapping) else {}

    resolved_chat_id = str(chat_id or chat.get("id") or "").strip()
    chat_type = str(chat.get("type") or "unknown")
    from_id = str(sender.get("id") or "")
    username = str(sender.get("username") or "")
    name_parts = [str(sender.get("first_name") or ""), str(sender.get("last_name") or "")]
    full_name = " ".join(p for p in name_parts if p).strip()
    actor_color = str(actor.get("color") or "red")
    actor_source = str(actor.get("source") or "default")
    actor_label = str(actor.get("name") or actor.get("email") or "").strip() or "-"
    owner = "yes" if str(actor.get("is_owner") or "").lower() == "true" else "no"
    bind_hint = resolved_chat_id or from_id or "(unknown)"

    lines = [
        "🧾 Telegram identity",
        f"chat.id: {resolved_chat_id or '-'}",
        f"chat.type: {chat_type}",
        f"from.id: {from_id or '-'}",
        f"from.username: @{username}" if username else "from.username: -",
        f"from.name: {full_name or '-'}",
        f"actor.color: {actor_color}",
        f"actor.source: {actor_source}",
        f"actor.label: {actor_label}",
        f"actor.owner: {owner}",
        "",
        "綁定時請使用：",
        f"  {bind_hint}",
    ]
    if chat_type in _TG_GROUP_CHAT_TYPES:
        lines.append("群組請綁 chat.id；私人對話 chat.id 通常等於 from.id。")
    return "\n".join(lines)


def _telegram_audit_reply_status(reply: str) -> str:
    text = (reply or "").strip()
    if text.startswith("🔒"):
        return "blocked"
    if text.startswith("🚦"):
        return "rate_limited"
    if text.startswith("⚠️") or text.startswith("❌"):
        return "error"
    return "ok"


def _telegram_audit_log(**kwargs: Any) -> None:
    try:
        from agent_core.telegram_audit import log_telegram_event
        log_telegram_event(**kwargs)
    except Exception:
        return


def _check_gemini_chat_circuit(gemini_model: str) -> None:
    model = (gemini_model or "").strip()
    if not model:
        return
    from agent_core.gemini_client import _check_gemini_circuit

    _check_gemini_circuit(model)


def _record_gemini_chat_success(gemini_model: str) -> None:
    model = (gemini_model or "").strip()
    if not model:
        return
    try:
        from agent_core.gemini_client import _record_gemini_circuit_success

        _record_gemini_circuit_success(model)
    except Exception:
        pass


def _record_gemini_chat_final_failure(
    gemini_model: str,
    exc: BaseException,
    *,
    transient: bool,
    non_retryable: bool,
) -> None:
    model = (gemini_model or "").strip()
    if not model:
        return
    try:
        from agent_core.gemini_client import (
            _classify_api_error,
            _record_gemini_circuit_failure,
        )
        status = _classify_api_error(str(exc))
    except Exception:
        status = "other"
        _record_gemini_circuit_failure = None  # type: ignore[assignment]
    try:
        from agent_core import cost_tracker

        cost_tracker.record_api_error(
            "gemini",
            status,
            model=model,
            detail=str(exc),
        )
    except Exception:
        pass
    if transient and not non_retryable and _record_gemini_circuit_failure is not None:
        try:
            _record_gemini_circuit_failure(model, status, str(exc))
        except Exception:
            pass


def _gemini_chat_fallback_model(gemini_model: str) -> str:
    model = (gemini_model or "").strip()
    if not model:
        return ""
    try:
        from agent_core.gemini_client import _gemini_fallback_model

        return _gemini_fallback_model(model)
    except Exception:
        return ""


def _should_try_gemini_chat_fallback(exc: BaseException) -> bool:
    # A soft timeout means the original Gemini thread may still be running and
    # may still call tools. Retrying on a different model could duplicate
    # side effects, so keep the existing "background may still finish" path.
    if isinstance(exc, TimeoutError):
        return False
    try:
        from agent_core.gemini_client import _should_try_gemini_fallback

        return _should_try_gemini_fallback(exc)
    except Exception:
        return False


def _send_message_with_timeout(
    chat_obj: Any,
    wrapped: str,
    *,
    timeout_s: float = _GEMINI_INFERENCE_TIMEOUT_S,
    caller: str = "telegram_chat",
    gemini_model: str = "",
    deadline_s: float | None = None,
    late_notify: Callable[[str], Any] | None = None,
    late_finalize: Callable[[str], str] | None = None,
) -> Any:
    """Run ``chat_obj.send_message(wrapped)`` with a hard wall-clock deadline
    and transparent retry on Gemini transient errors (503/429/timeout).

    Same daemon-thread pattern as vector_store._gemini_embed_with_timeout
    (see commit d2ee943): Python can't kill a thread, so a hung SDK call
    leaks one daemon thread until process exit. The next attempt builds a
    fresh thread; we never wait on the wedged one.

    The Gemini SDK's `chat.send_message()` does NOT auto-retry on 503
    (unlike `_gemini_generate` which has its own retry loop). When the
    model is overloaded the user used to see a raw `ServerError: 503
    UNAVAILABLE` reply. We now wrap with the same transient-error
    heuristic so the bot rides out short Google-side spikes silently.
    The total wall-clock budget across attempts is bounded by timeout_s
    of the LAST attempt — we don't re-extend the deadline on each retry
    because the watchdog still pulses the heartbeat.

    2026-06-13 起的整體 deadline（事故修復）：timeout_s 只是「軟超時」——
    超過時這裡 raise TimeoutError 讓對話讓出來，但同時掛一條監看線程
    （_spawn_task_abandon_monitor）盯著仍在跑的推理線程：deadline 內完成
    就透過 late_notify 補送結果、失敗就回報例外、超過 deadline_s（預設
    RED_TG_TASK_DEADLINE_S=900）就放棄並通知使用者。推理線程自己帶
    thread-local deadline，工具閘門據此讓被放棄線程的後續 tool call 全部
    立即失敗 → 線程必然終結，不再無限佔資源。

    Raises:
        TimeoutError: when the call exceeds timeout_s on the final attempt.
        Whatever exception the SDK raised on the last attempt for
        non-transient or attempts-exhausted cases.
    """
    started_mono = time.monotonic()
    deadline_budget = deadline_s if deadline_s is not None else _tg_task_deadline_s()
    # deadline 不得短於軟超時，否則監看線程一出生就宣告放棄。
    deadline_budget = max(float(deadline_budget), float(timeout_s))
    deadline_mono = started_mono + deadline_budget
    last_err: BaseException | None = None
    _check_gemini_chat_circuit(gemini_model)
    for attempt in range(_GEMINI_SEND_MESSAGE_MAX_ATTEMPTS):
        holder: dict[str, Any] = {}
        done = threading.Event()

        # holder/done 用 default-arg 綁定：閉包抓變數不抓值，萬一這條線程
        # 活過下一輪 retry 的重綁，寫到的必須仍是自己那一輪的物件。
        def target(holder: dict[str, Any] = holder, done: threading.Event = done) -> None:
            _task_deadline_local.deadline = deadline_mono
            try:
                holder["value"] = chat_obj.send_message(wrapped)
            except BaseException as exc:
                holder["exc"] = exc
            finally:
                done.set()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        if not done.wait(timeout=timeout_s):
            _spawn_task_abandon_monitor(
                done=done,
                holder=holder,
                deadline_mono=deadline_mono,
                started_mono=started_mono,
                deadline_s=deadline_budget,
                caller=caller,
                late_notify=late_notify,
                late_finalize=late_finalize,
            )
            raise TimeoutError(
                f"chat.send_message exceeded {timeout_s}s — possible Gemini SDK hang"
            )
        if "exc" not in holder:
            resp = holder["value"]
            # Record cost for the interactive chat path — chat.send_message
            # bypasses _gemini_generate's accounting. Best-effort. `caller`
            # lets non-telegram callers (e.g. the dispatcher) attribute spend
            # correctly instead of everything landing under telegram_chat.
            try:
                from agent_core import cost_tracker
                cost_tracker.record_chat_response(resp, caller=caller)
            except Exception:
                pass
            _record_gemini_chat_success(gemini_model)
            return resp

        exc = holder["exc"]
        last_err = exc
        msg = str(exc).lower()
        # Fail FAST on non-retryable quota errors (prepayment depleted /
        # monthly spend cap). _gemini_generate already raises immediately on
        # these; the chat path must match, or the transient test below would
        # catch "429"/"quota" and burn ~6s + 3 spurious failure logs retrying
        # something a retry can't fix.
        non_retryable = False
        is_transient = False
        try:
            from agent_core.gemini_client import (
                _is_non_retryable_quota_error,
                _is_transient_error,
            )
            non_retryable = _is_non_retryable_quota_error(msg)
            is_transient = _is_transient_error(msg)
        except Exception:
            non_retryable = False
            is_transient = False
        if non_retryable:
            _record_gemini_chat_final_failure(
                gemini_model,
                exc,
                transient=is_transient,
                non_retryable=True,
            )
            raise exc
        transient = is_transient
        if not transient or attempt >= _GEMINI_SEND_MESSAGE_MAX_ATTEMPTS - 1:
            _record_gemini_chat_final_failure(
                gemini_model,
                exc,
                transient=transient,
                non_retryable=False,
            )
            raise exc
        # Exponential backoff, capped — give Google a moment to recover.
        wait_s = min(2 ** attempt, 8)
        print(
            f"[tg bot] ⚠️ Gemini chat.send_message 暫時失敗 ({type(exc).__name__}: "
            f"{str(exc)[:120]}…)，{wait_s}s 後重試（{attempt + 1}/{_GEMINI_SEND_MESSAGE_MAX_ATTEMPTS}）",
            flush=True,
        )
        time.sleep(wait_s)
    # Unreachable because either we return early or re-raise in the loop,
    # but mypy/static-analyzers like the explicit raise.
    raise last_err if last_err else RuntimeError("send_message retry exhausted")


def _telegram_email_lake_rebuild_queued(days_back: int = 30, max_emails: int = 200):
    """Queue Email Lake rebuild instead of running it inside Telegram chat."""
    from agent_core.tool_result import ToolResult, ErrorCode

    try:
        days_back_i = max(1, min(int(days_back), 1825))
        max_emails_i = max(1, min(int(max_emails), 2000))
    except (TypeError, ValueError) as exc:
        return ToolResult.failure(
            f"email_lake_rebuild 參數格式錯誤：{exc}",
            error_code=ErrorCode.INVALID_INPUT,
            recoverable=False,
            suggested_fix="days_back / max_emails 請使用整數",
        )

    from agent_core.task_queue import find_live_task, submit_task

    existing = find_live_task("email_lake_rebuild", "data_lake_writer")
    if existing is not None:
        task_id = str(existing.get("id") or "")
        state = str(existing.get("state") or "?")
        return ToolResult.success(
            "⏳ Email Lake rebuild 已經在背景隊列中，這次不重複啟動。\n"
            f"   task_id: {task_id}\n"
            f"   state: {state}\n"
            "   可用 task_status(task_id) 查狀態；不用重送 `cc`。",
            data={
                "task_id": task_id,
                "state": state,
                "tool": "email_lake_rebuild",
                "deduped": True,
            },
            artifacts=[task_id] if task_id else [],
        )

    timeout_sec = _env_int(
        "RED_TG_EMAIL_LAKE_REBUILD_QUEUE_TIMEOUT_S",
        3600,
        min_value=5,
        max_value=3600,
    )
    max_retries = _env_int(
        "RED_TG_EMAIL_LAKE_REBUILD_QUEUE_RETRIES",
        2,
        min_value=0,
        max_value=10,
    )
    result = submit_task(
        "email_lake_rebuild",
        {"days_back": days_back_i, "max_emails": max_emails_i},
        priority=5,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        mutex_group="data_lake_writer",
    )
    if not getattr(result, "ok", True):
        return result
    task_id = ""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        task_id = str(data.get("task_id") or "")
    return ToolResult.success(
        "✅ Email Lake rebuild 已排入背景隊列，不會再卡住 Telegram 180 秒等待。\n"
        f"   task_id: {task_id or '（未回傳）'}\n"
        f"   範圍: 過去 {days_back_i} 天，最多 {max_emails_i} 封\n"
        "   後台 worker 會用 data_lake_writer mutex 跑；可用 task_status(task_id) 查狀態。",
        data={
            "task_id": task_id,
            "tool": "email_lake_rebuild",
            "days_back": days_back_i,
            "max_emails": max_emails_i,
            "timeout_sec": timeout_sec,
            "max_retries": max_retries,
            "deduped": False,
        },
        artifacts=[task_id] if task_id else [],
    )


_telegram_email_lake_rebuild_queued.__name__ = "email_lake_rebuild"
_telegram_email_lake_rebuild_queued.__qualname__ = "email_lake_rebuild"
_telegram_email_lake_rebuild_queued.__doc__ = (
    "批次處理過去 N 天的信進 Email Data Lake。Telegram 中會排入背景隊列，"
    "避免長時間 rebuild 卡住 180 秒對話等待。\n"
    "- days_back：往前幾天（上限 1825=5 年）\n"
    "- max_emails：單次最多處理幾封（上限 2000）"
)
# 本模組有 from __future__ import annotations → 這個 wrapper 的 annotations
# 是字串，genai 派發參數時 isinstance(30, "int") 直接 TypeError（#113 同型）。
# tool_registry 組裝點的解析摸不到這個在 tg_build_chat 才 swap 進來的
# wrapper，要自己給真型別 — 否則 LLM 一帶參數呼叫 email_lake_rebuild 就炸。
_telegram_email_lake_rebuild_queued.__annotations__ = {
    "days_back": int, "max_emails": int,
}


def _queue_telegram_long_running_tools(tools_list: list[Any]) -> list[Any]:
    """Replace Telegram-only long jobs with queue-submitting wrappers."""
    out: list[Any] = []
    replaced = 0
    for fn in tools_list:
        if getattr(fn, "__name__", "") == "email_lake_rebuild":
            out.append(_telegram_email_lake_rebuild_queued)
            replaced += 1
        else:
            out.append(fn)
    if replaced:
        print(
            f"[tg bot] 🧵 queued long-running tools for Telegram: {replaced}",
            flush=True,
        )
    return out


# (Media-detection helpers re-exported at the top of this file.)


def _download_result_artifact_paths(result: Any) -> list[str]:
    """Extract local media paths from a downloader ToolResult.

    Newer downloader wrappers return ToolResult.artifacts; the text fallback
    keeps older worker responses usable during rolling restarts.
    """
    paths: list[str] = []
    for value in getattr(result, "artifacts", []) or []:
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    if not paths:
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            file_info = data.get("file_info")
            if isinstance(file_info, dict):
                for value in file_info.get("paths") or []:
                    if isinstance(value, str) and value.strip():
                        paths.append(value.strip())
    if not paths:
        for line in str(result).splitlines():
            # Downloader summaries use lines like
            # "1. /Users/.../video.mp4" or "1. /Users/.../audio.mp3（123 秒）".
            match = re.search(
                r"(?:^|\s)(/[^ \t\r\n]+?\.(?:mp4|mov|mkv|webm|mp3|m4a|wav|flac|aac|opus))"
                r"(?:$|[\s（()）)])",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                paths.append(match.group(1))
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def _delete_delivered_download_artifact(path: str) -> str:
    """Delete a media file after Telegram delivery, within conservative roots."""
    if not path:
        return "未清理：空路徑"
    try:
        import tempfile as _tempfile
        real_path = os.path.realpath(os.path.expanduser(path))
        allowed_roots = [
            os.path.realpath(os.path.expanduser("~/Downloads")),
            os.path.realpath(_tempfile.gettempdir()),
        ]
        if not any(real_path == root or real_path.startswith(root + os.sep) for root in allowed_roots):
            return "保留本機檔：不在自動清理允許範圍"
        if not os.path.isfile(real_path):
            return "未清理：檔案已不存在"
        os.remove(real_path)
        return "已刪除本機下載檔"
    except Exception as exc:
        return f"清理失敗：{type(exc).__name__}: {exc}"


def _append_telegram_delivery_for_download(
    result: Any,
    *,
    chat_id: str,
    source_tool: str = "download_online_video",
) -> str:
    """Send freshly downloaded media artifacts back to the same Telegram chat."""
    text = str(result)
    if getattr(result, "ok", True) is False:
        return text
    paths = _download_result_artifact_paths(result)
    if not paths:
        return text

    try:
        from agent_core.tool_runner import call_tool
    except Exception as exc:
        return (
            f"{text}\n\n"
            f"📤 Telegram 回傳失敗：載入檔案傳送工具失敗（{type(exc).__name__}: {exc}）。"
        )

    timeout = _env_int("RED_TG_FILE_UPLOAD_TIMEOUT_S", 300, min_value=1, max_value=7200)
    lines = ["", "📤 Telegram 回傳："]
    for index, path in enumerate(paths, start=1):
        filename = os.path.basename(path)
        caption = _download_delivery_caption(filename, source_tool=source_tool)
        try:
            send_result = call_tool(
                "telegram_send_file",
                {"file_path": path, "caption": caption, "chat_id": chat_id},
                context={
                    "caller": "telegram_media_fastpath_delivery",
                    "source_tool": source_tool,
                    "chat_id": chat_id,
                    "worker_channel": "daemon",
                },
                timeout_sec=timeout,
                prefer_rpc=True,
            )
            cleanup = ""
            if getattr(send_result, "ok", False):
                cleanup = f"；{_delete_delivered_download_artifact(path)}"
            lines.append(f"{index}. {send_result}{cleanup}")
        except Exception as exc:
            lines.append(
                f"{index}. ❌ {filename} 傳送失敗：{type(exc).__name__}: {exc}"
            )
    return text + "\n".join(lines)


def _download_delivery_caption(filename: str, *, source_tool: str) -> str:
    kind = "音訊" if source_tool == "download_youtube_audio" else "影片"
    return f"小紅下載好的{kind}：{filename}"[:900]


# (Chat-state URL memory helpers re-exported at the top of this file.)


def _snapshot_downloads_paths() -> set[str]:
    """Snapshot ~/Downloads contents so the post-agent scan ignores
    files that were already there before the message handler ran.

    Without this, every message would re-deliver stale Downloads/* files
    on every turn.
    """
    downloads = os.path.expanduser("~/Downloads")
    if not os.path.isdir(downloads):
        return set()
    try:
        return {os.path.join(downloads, name) for name in os.listdir(downloads)}
    except OSError:
        return set()


def _scan_new_media_downloads_since(start_ts: float, ignore_paths: set[str]) -> list[str]:
    """Find media files in ~/Downloads modified after start_ts.

    `ignore_paths` is the pre-message snapshot — anything in there is filtered
    out even if it was touched (we only want files the current handler newly
    created, not ones the user dropped in via Finder).
    """
    # Safety: scanning global ~/Downloads can pick up unrelated files created
    # by other apps during inference and then auto-send + delete them. Keep
    # this feature opt-in; prefer explicit tool-returned paths or a dedicated
    # output directory for auto-delivery.
    if os.environ.get("RED_TG_AUTO_DELIVER_SCAN_DOWNLOADS", "0").lower() in {"0", "false", "no"}:
        return []
    downloads = os.path.expanduser("~/Downloads")
    if not os.path.isdir(downloads):
        return []
    fresh: list[str] = []
    try:
        for name in os.listdir(downloads):
            path = os.path.join(downloads, name)
            if path in ignore_paths or not os.path.isfile(path):
                continue
            if not name.lower().endswith(_AUTO_DELIVER_MEDIA_EXTS):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            # 1s slack: handler can write a file slightly before start_ts on
            # clock jitter; we still want it.
            if mtime >= start_ts - 1.0:
                fresh.append(path)
    except OSError:
        return []
    return sorted(fresh, key=lambda p: os.path.getmtime(p))


def _deliver_new_downloads_via_telegram(paths: list[str], chat_id: str) -> str:
    """Send each new media file via Telegram and clean up local copy.

    Returns a summary string to append to the agent's reply. Empty string
    if there's nothing to deliver.
    """
    if not paths or not chat_id:
        return ""
    try:
        from agent_core.tool_runner import call_tool
    except Exception as exc:
        return (
            f"\n\n📤 Telegram 自動回傳失敗：載入檔案傳送工具失敗"
            f"（{type(exc).__name__}: {exc}）。"
        )

    timeout = _env_int("RED_TG_FILE_UPLOAD_TIMEOUT_S", 300, min_value=1, max_value=7200)
    delete_on_success = os.environ.get("RED_TG_AUTO_DELIVER_DELETE", "0").lower() not in {"0", "false", "no"}
    lines = ["", "📤 Telegram 自動回傳新下載檔："]
    for index, path in enumerate(paths, start=1):
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1].lower()
        kind = "音訊" if ext in _AUTO_DELIVER_AUDIO_EXTS else "影片"
        caption = f"小紅下載好的{kind}：{filename}"[:900]
        try:
            send_result = call_tool(
                "telegram_send_file",
                {"file_path": path, "caption": caption, "chat_id": chat_id},
                context={
                    "caller": "telegram_auto_deliver_post_agent",
                    "chat_id": chat_id,
                    "worker_channel": "daemon",
                },
                timeout_sec=timeout,
                prefer_rpc=True,
            )
            cleanup = ""
            if getattr(send_result, "ok", False):
                if delete_on_success:
                    cleanup = f"；{_delete_delivered_download_artifact(path)}"
            lines.append(f"{index}. {send_result}{cleanup}")
        except Exception as exc:
            lines.append(
                f"{index}. ❌ {filename} 傳送失敗：{type(exc).__name__}: {exc}"
            )
    return "\n".join(lines)


# ── 回覆附圖（[[TG_PHOTO:...]] 標記 → sendPhoto 回當前對話）─────────────
# 2026-07-28 UserA 案：員工要鞋款「照片本體」，但員工 chat 不在出站推送
# 授權名單（RED_AUTHORIZED_CHAT_IDS 是 telegram_send_photo 那條路的閘），
# 工具端主動推不出去。解法：照片跟著**回覆**走——fetch_shoe_photos 這類工具
# 把下載好的路徑用 [[TG_PHOTO:...]] 標記寫進回傳文字，回覆送出點抽標記、
# 用「當前 bot token + 當前 chat_id」sendPhoto。誰在跟 bot 講話照片就回給誰，
# chat_id 不經 LLM 之手，出站授權閘不必動。
_TG_REPLY_PHOTO_RE = re.compile(r"\[\[TG_PHOTO:([^\[\]\n]+)\]\]")
_TG_REPLY_PHOTO_MAX = 5
_TG_REPLY_PHOTO_MAX_BYTES = 10 * 1024 * 1024   # Telegram sendPhoto 上限
_TG_REPLY_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _reply_photo_allowed_roots(color: str = "") -> tuple[str, ...]:
    """標記路徑白名單：只放行 bot 自己產出的圖（防注入標記夾帶任意本機檔）。

    color 非空（部門員工）時，生成圖那條收窄成 **generated_images/dept/<色>**
    —— 比照 [[TG_FILE:]] 的 per-color 分艙理由（PR #341）：generated_images 頂層
    混著大王自己生的圖，只認整個目錄的話，一次 prompt injection 就能讓員工的
    LLM 吐出別人的舊圖標記把圖要走。抓取的產品照（product_photos/fetched）是
    共用暫存區、內容就是各色都查得到的 Drive 鞋圖，維持共用。
    """
    from agent_core.logging_and_paths import DATA_DIR, GENERATED_IMAGES_DIR
    normalized = str(color or "").strip().lower()
    generated = (GENERATED_IMAGES_DIR if normalized in ("", "red")
                 else os.path.join(GENERATED_IMAGES_DIR, "dept", normalized))
    return (
        os.path.realpath(os.path.join(DATA_DIR, "product_photos", "fetched")),
        os.path.realpath(generated),
    )


def _extract_reply_photos(text: str, color: str = "") -> tuple[str, list[str]]:
    """抽出回覆裡的 [[TG_PHOTO:path]] 標記 → (去標記文字, 合格照片路徑)。

    驗證逐條 fail-closed：realpath 必須落在白名單目錄底下、副檔名是圖片、
    檔案存在且 ≤10MB；不合格的靜默丟棄（只 log）。上限 5 張、去重保序。

    color：發話者部門色（空 = 大王/未知），決定生成圖白名單收不收窄。
    """
    raw = text or ""
    if "[[TG_PHOTO:" not in raw:
        return raw, []
    roots = _reply_photo_allowed_roots(color)
    photos: list[str] = []
    for m in _TG_REPLY_PHOTO_RE.finditer(raw):
        candidate = m.group(1).strip()
        if not candidate or len(photos) >= _TG_REPLY_PHOTO_MAX:
            continue
        # 合法標記一定是絕對路徑；相對片段（如工具說明文字裡的「...」佔位符）
        # 靜默跳過，不進白名單比對也不留噪音 log。
        if not os.path.isabs(candidate):
            continue
        try:
            real = os.path.realpath(candidate)
            if not any(real == r or real.startswith(r + os.sep) for r in roots):
                print(f"[tg bot] ⚠️ 回覆附圖標記路徑不在白名單，忽略：{candidate[:80]}")
                continue
            if not real.lower().endswith(_TG_REPLY_PHOTO_EXTS):
                continue
            if not os.path.isfile(real) or os.path.getsize(real) > _TG_REPLY_PHOTO_MAX_BYTES:
                continue
        except OSError:
            continue
        if real not in photos:
            photos.append(real)
    clean = _TG_REPLY_PHOTO_RE.sub("", raw)
    # 標記獨佔行移除後會留連續空行，收斂一下
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, photos


# ── 回覆附檔（[[TG_FILE:...]] 標記 → sendDocument 回當前對話）─────────
# 2026-08-03 UserC 案：green 員工說「請生成 excel 表」，freeform 只回得出 CSV
# 文字 —— export_report 不在部門白名單裡，而且就算放行，它的自動傳送走
# telegram_send_file → _resolve_chat_id 只認大王 keyring chat，員工要的檔會
# 傳到大王手機。解法同 TG_PHOTO：檔案跟著**回覆**走，用當前 bot token + 當前
# chat_id sendDocument。
# ⚠️ 白名單刻意是 **per-color 子目錄**（exports/dept/<色>），不是整個
# EXPORTS_DIR —— EXPORTS_DIR 頂層混著大王自己的匯出報表，只認目錄的話一次
# prompt injection 就能讓 LLM 吐出別人的舊檔標記把檔案要走。
_TG_REPLY_FILE_RE = re.compile(r"\[\[TG_FILE:([^\[\]\n]+)\]\]")
_TG_REPLY_FILE_MAX = 3
_TG_REPLY_FILE_MAX_BYTES = 50 * 1024 * 1024   # Telegram sendDocument 上限
_TG_REPLY_FILE_EXTS = (".xlsx", ".docx", ".pdf", ".csv")


def _reply_file_allowed_roots(color: str) -> tuple[str, ...]:
    """標記路徑白名單：只放行**該部門色自己**的匯出目錄。

    空色 / red（大王）→ 回空 tuple：大王路徑不用標記機制（export_report 直接
    telegram_send_file 傳到 keyring chat），沒有白名單＝標記一律拒收。
    """
    normalized = str(color or "").strip().lower()
    if not normalized or normalized == "red":
        return ()
    from agent_core.logging_and_paths import EXPORTS_DIR
    return (os.path.realpath(os.path.join(EXPORTS_DIR, "dept", normalized)),)


def _extract_reply_files(text: str, color: str) -> tuple[str, list[str]]:
    """抽出回覆裡的 [[TG_FILE:path]] 標記 → (去標記文字, 合格檔案路徑)。

    驗證逐條 fail-closed，比照 _extract_reply_photos：realpath 必須落在該色
    白名單目錄底下、副檔名在允許清單、檔案存在且 ≤50MB；不合格的靜默丟棄
    （只 log）。上限 3 個、去重保序。

    ⚠️ 標記語法**一律**從文字裡剝除（即使該色沒有白名單、或全數被拒）——
    否則原始 [[TG_FILE:/絕對路徑]] 會直接顯示給使用者、洩漏本機路徑。
    """
    raw = text or ""
    if "[[TG_FILE:" not in raw:
        return raw, []
    roots = _reply_file_allowed_roots(color)
    files: list[str] = []
    for m in _TG_REPLY_FILE_RE.finditer(raw):
        candidate = m.group(1).strip()
        if not candidate or not roots or len(files) >= _TG_REPLY_FILE_MAX:
            continue
        # 合法標記一定是絕對路徑；相對片段（工具說明文字裡的佔位符）靜默跳過。
        if not os.path.isabs(candidate):
            continue
        try:
            real = os.path.realpath(candidate)
            if not any(real == r or real.startswith(r + os.sep) for r in roots):
                print(f"[tg bot] ⚠️ 回覆附檔標記路徑不在白名單，忽略：{candidate[:80]}")
                continue
            if not real.lower().endswith(_TG_REPLY_FILE_EXTS):
                continue
            if not os.path.isfile(real) or os.path.getsize(real) > _TG_REPLY_FILE_MAX_BYTES:
                continue
        except OSError:
            continue
        if real not in files:
            files.append(real)
    clean = _TG_REPLY_FILE_RE.sub("", raw)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, files


def _reply_attachment_color(actor: Mapping[str, Any] | None) -> str:
    """回覆附檔白名單用的部門色：只有 colored 員工才回非空色。

    大王（is_owner）/ 無 actor / red → ""，等同關閉附檔機制。
    """
    try:
        from agent_core.telegram_actor_scope import actor_requires_separation
        if not actor_requires_separation(actor):
            return ""
    except Exception:
        return ""
    color = str((actor or {}).get("color") or "").strip().lower()
    return "" if color in ("", "red") else color


def _reply_photo_caption(path: str) -> str:
    name = os.path.basename(path)
    # fetch_shoe_photos 存檔加了 8 碼 file_id 前綴，caption 還原原始檔名
    return re.sub(r"^[A-Za-z0-9_-]{8}_", "", name)[:200]


def tg_send_with_photos(
    token: str,
    chat_id: str,
    text: str,
    *,
    requests_module=_requests,
    heartbeat_touch: Callable[[], None] | None = None,
    reply_markup: Mapping[str, Any] | None = None,
    actor_color: str = "",
) -> bool:
    """回覆送出（文字 + [[TG_PHOTO:...]] 附圖 + [[TG_FILE:...]] 附檔）。

    文字照 tg_send 走；照片用同一個 bot token 對同一個 chat 逐張 sendPhoto
    （telegram._send_photo_with_retries，含暫時性錯誤重試）。照片失敗不影響
    文字回覆結果——回傳值仍是「文字有沒有送達」。

    actor_color：發話者的部門色（_reply_attachment_color 的結果）。附檔的路徑
    白名單綁這個色（exports/dept/<色>）；留空 = 大王/未知 → 附檔機制關閉，
    標記照樣剝除但不傳任何檔。附圖白名單同樣吃這個色（生成圖收窄成
    generated_images/dept/<色>，抓取的產品照仍共用）。
    """
    clean, photos = _extract_reply_photos(text, actor_color)
    clean, files = _extract_reply_files(clean, actor_color)
    # 有標記語法就一律送去標記後的文字（Codex P2, PR #305）——標記全數被拒
    # （檔案被清、超額、白名單外）時也不能把原始 [[TG_PHOTO:...]] 丟給使用者。
    had_markers = clean != (text or "")
    body = clean
    if not body and not photos and not files:
        body = ("⚠️ 照片附件未通過驗證，這次沒有可傳送的圖。"
                if had_markers else (text or ""))
    delivered = True
    # 無標記的空回覆維持 tg_send 原本的「(空回覆)」語意；有附件可送時才略過空文字。
    if body or (not had_markers and not photos and not files):
        delivered = tg_send(
            token, chat_id, body,
            requests_module=requests_module,
            heartbeat_touch=heartbeat_touch,
            reply_markup=reply_markup,
        )
    for path in photos:
        if heartbeat_touch:
            heartbeat_touch()
        try:
            from agent_core.telegram import _send_photo_with_retries
            _result, err = _send_photo_with_retries(
                target=str(chat_id), abs_path=path,
                filename=os.path.basename(path),
                caption=_reply_photo_caption(path), token=token,
            )
            if err:
                print(f"[tg bot] ⚠️ 回覆附圖失敗（{os.path.basename(path)}）：{err}")
            else:
                print(f"[tg bot] 📷 回覆附圖已送：{os.path.basename(path)}")
        except Exception as exc:
            print(f"[tg bot] ⚠️ 回覆附圖例外（{os.path.basename(path)}）："
                  f"{type(exc).__name__}: {exc}")
    for path in files:
        if heartbeat_touch:
            heartbeat_touch()
        name = os.path.basename(path)
        try:
            from agent_core.telegram import _send_document_part_with_retries
            _result, err, _attempts = _send_document_part_with_retries(
                target=str(chat_id), part_path=path, part_name=name,
                caption=name[:200], token=token,
            )
            if err:
                print(f"[tg bot] ⚠️ 回覆附檔失敗（{name}）：{err}")
            else:
                print(f"[tg bot] 📎 回覆附檔已送：{name}")
        except Exception as exc:
            print(f"[tg bot] ⚠️ 回覆附檔例外（{name}）："
                  f"{type(exc).__name__}: {exc}")
    return delivered


# (Media-tool routing helpers re-exported at the top of this file.)


def _try_direct_media_download_fastpath(
    user_text: str,
    *,
    chat_state: dict[str, Any],
    chat_id: str,
    confirm_scope: str = "",
    heartbeat_touch: Callable[[], None] | None = None,
    heartbeat_interval_s: float = 15.0,
) -> str | None:
    """Handle obvious media-download Telegram requests without Gemini.

    The actual downloader is still protected by the same one-shot Telegram
    confirmation gate used for sensitive tools. First URL message records a
    pending request; the following `c` consumes the confirmation and runs
    download_online_video directly.

    confirm_scope：+確認 窗口的 scope 鍵（_confirm_scope_for 的結果 —— 綁定群為
    "<chat_id>:<from_id>"、私訊==chat_id）。mark 端（tg_handle_message）用它記
    token，這裡的 check/is_locked_out/revoke 必須用**同一把 key**，否則群組內
    +確認 永遠對不起來（PR #210 後遺）。留空退回 chat_id（私訊零行為改變）。
    """
    if not chat_id:
        return None
    confirm_scope = confirm_scope or chat_id

    # Always remember any media URL the user mentioned, even when the rest of
    # the message wouldn't trigger fast-path on its own — this is what lets
    # the next verb-only message ("下載這個", "也給我音檔") still pick up the
    # previously-pasted link instead of falling through to the agent path.
    _remember_media_url_in_chat(chat_state, user_text)

    now = time.time()
    pending = chat_state.get(_TG_PENDING_MEDIA_DOWNLOAD_KEY)
    if isinstance(pending, dict) and now - float(pending.get("ts", 0.0) or 0.0) > _TG_PENDING_MEDIA_DOWNLOAD_TTL_S:
        chat_state.pop(_TG_PENDING_MEDIA_DOWNLOAD_KEY, None)
        pending = None
    protected_pending = chat_state.get(_TG_PROTECTED_MEDIA_URL_KEY)
    if (
        isinstance(protected_pending, dict)
        and now - float(protected_pending.get("ts", 0.0) or 0.0) > _TG_PENDING_MEDIA_DOWNLOAD_TTL_S
    ):
        chat_state.pop(_TG_PROTECTED_MEDIA_URL_KEY, None)
        protected_pending = None

    for candidate_url in _extract_media_urls(user_text):
        if _unsupported_media_reason(candidate_url):
            chat_state.pop(_TG_PENDING_MEDIA_DOWNLOAD_KEY, None)
            return _unsupported_media_download_reply(candidate_url)
        if _protected_media_provider(candidate_url):
            chat_state.pop(_TG_PENDING_MEDIA_DOWNLOAD_KEY, None)
            chat_state[_TG_PROTECTED_MEDIA_URL_KEY] = {"url": candidate_url, "ts": now}
            return _protected_media_download_reply(candidate_url)

    if isinstance(protected_pending, dict) and _has_media_download_verb(user_text):
        protected_url = str(protected_pending.get("url", "") or "")
        if protected_url:
            return _protected_media_download_reply(protected_url)

    url = _extract_direct_media_download_url(user_text)
    if not url and not pending and _has_media_download_verb(user_text):
        # Verb-only message ("幫我下載這個影片") — recover the URL from earlier
        # in this chat session if we've still got it cached. This is the path
        # the user asked us to enable: previously this fell through to the
        # Gemini agent, which doesn't auto-deliver, so files sat in ~/Downloads.
        cached_url = _recent_media_url_from_chat(chat_state)
        if cached_url and not _unsupported_media_reason(cached_url) and not _protected_media_provider(cached_url):
            url = cached_url
            print(
                f"[tg bot] 🎬 fast-path 從 chat 記憶恢復 URL: {cached_url[:120]}",
                flush=True,
            )
    if url:
        pending = {
            "url": url,
            "ts": now,
            "tool": _media_download_tool_for_url(url, user_text),
            "args": _media_download_args_for_url(url, user_text),
        }
        chat_state[_TG_PENDING_MEDIA_DOWNLOAD_KEY] = pending
    elif not pending:
        return None

    try:
        from agent_core.tg_auth import (
            check_confirmed,
            is_locked_out,
            message_grants_confirmation,
            revoke_after_use,
        )
        confirmed, elapsed = check_confirmed(confirm_scope)
        if not confirmed:
            if not url and message_grants_confirmation(user_text):
                chat_state.pop(_TG_PENDING_MEDIA_DOWNLOAD_KEY, None)
                return "⚠️ 上一個影片下載請求已過期或不存在，請重貼影片網址。"
            locked, remain = is_locked_out(confirm_scope)
            if locked:
                return f"🔒 確認次數太多，已暫時鎖定；約 {int(remain)} 秒後再試。"
            return _confirmation_help_for_media_download(str(pending.get("url", url)))

        selected_url = str(pending.get("url", url))
        tool_name = str(pending.get("tool") or _media_download_tool_for_url(selected_url, user_text))
        tool_args = pending.get("args")
        if not isinstance(tool_args, dict):
            tool_args = _media_download_args_for_url(selected_url, user_text)
        chat_state.pop(_TG_PENDING_MEDIA_DOWNLOAD_KEY, None)
        revoke_after_use(confirm_scope)
        from agent_core.tool_runner import call_tool
        print(f"[tg bot] 🎬 direct media download fast-path via {tool_name}: {selected_url[:120]}", flush=True)
        stop_pulse = threading.Event() if heartbeat_touch else None
        pulse_thread = None
        if heartbeat_touch:
            heartbeat_touch()

            def _pulse_while_media_download_runs() -> None:
                while stop_pulse is not None and not stop_pulse.wait(heartbeat_interval_s):
                    heartbeat_touch()

            pulse_thread = threading.Thread(
                target=_pulse_while_media_download_runs,
                name="tg-media-fastpath-heartbeat",
                daemon=True,
            )
            pulse_thread.start()
            # Pulse once more after the worker thread starts to reduce race
            # conditions in very short tool runs (tests use 10ms interval).
            heartbeat_touch()
        try:
            result = call_tool(
                tool_name,
                tool_args,
                context={
                    "caller": "telegram_media_fastpath",
                    "chat_id": chat_id,
                    "worker_channel": "daemon",
                },
                timeout_sec=_env_int("RED_TG_MEDIA_DOWNLOAD_TIMEOUT_S", 900, min_value=1, max_value=7200),
                prefer_rpc=True,
            )
        finally:
            if stop_pulse is not None:
                stop_pulse.set()
            if pulse_thread is not None:
                pulse_thread.join(timeout=1)
            if heartbeat_touch:
                heartbeat_touch()
        return _append_telegram_delivery_for_download(result, chat_id=chat_id, source_tool=tool_name)
    except Exception as exc:
        chat_state.pop(_TG_PENDING_MEDIA_DOWNLOAD_KEY, None)
        return f"❌ 影片下載 fast-path 失敗：{type(exc).__name__}: {exc}"

_TG_STATE_KEY_OFFSET = "telegram_offset"

# Shared HTTP session for all Telegram API calls. Without this, every
# getUpdates iteration of the long-poll loop opened a fresh TCP+TLS
# connection — wasteful in steady state, AND multiplied failure surface
# area on flaky links (HiNet IPv6 + Telegram 502 BadGateway cycles).
#
# The session also mounts an HTTPAdapter that transparently retries on
# transient 5xx with backoff. Bursts that previously bubbled up as
# "consecutive_net_errors → process restart" are now absorbed at the
# urllib3 layer, so the heartbeat watchdog only fires on genuinely
# extended outages instead of small gateway hiccups.
_TG_SESSION: Any = None
_TG_SESSION_LOCK = threading.Lock()


def _get_tg_session(requests_module=_requests):
    """Lazy-init shared session. The `requests_module` arg lets tests
    inject a mock (matching the rest of this module's pattern).

    Backward-compat: a thin test stub that exposes only ``.get`` / ``.post``
    (no ``.Session`` factory) is returned as-is and bypasses caching, so
    pre-existing tests that don't know about Session keep working. The
    production path always passes the real ``requests`` module, which has
    Session, so production traffic always gets the retry adapter."""
    if not hasattr(requests_module, "Session"):
        return requests_module

    global _TG_SESSION
    if _TG_SESSION is not None:
        return _TG_SESSION
    with _TG_SESSION_LOCK:
        if _TG_SESSION is not None:
            return _TG_SESSION
        from urllib3.util.retry import Retry
        session = requests_module.Session()
        # GET-only retry: getUpdates / getFile / file download are all
        # idempotent (offset-keyed long-poll, content fetch). POST is
        # explicitly EXCLUDED — Telegram's sendMessage has no idempotency
        # key, so a 502 returned after the message landed would be
        # re-POSTed and the user would see the reply twice. The outer
        # tg_send() loop handles 5xx for POSTs at one-attempt-per-iter
        # granularity, which is correct for a non-idempotent endpoint.
        retry = Retry(
            total=3,
            backoff_factor=0.5,             # sleeps 0s, 0.5s, 1s
            status_forcelist=(502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,          # let caller see status code
            respect_retry_after_header=True,
        )
        adapter = requests_module.adapters.HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        _TG_SESSION = session
        return session


def _reset_tg_session() -> None:
    """Drop the cached session (used by tests and after process restart
    signals — launchd respawns clear globals naturally, so this is mainly
    a test seam)."""
    global _TG_SESSION
    with _TG_SESSION_LOCK:
        if _TG_SESSION is not None:
            try:
                _TG_SESSION.close()
            except Exception:
                pass
            _TG_SESSION = None
# 提示裡明列「免確認直接做」的 SAFE 工具。抽成常數而不是寫死在提示字串裡，是為了
# 讓測試能斷言「每個名字都真的存在、而且 tier 真的是 safe」——
# 2026-06-16 (a1a6312f) 這串裡混進了 `update_calendar_event`，那支工具**從來沒有
# 存在過**（真正註冊的只有 create / delete / list_calendar_events），卻被寫進提示
# 告訴模型它可用且免確認，一躺就是兩個半月。tool_tiers.get_tier() 對不存在的名字
# 也回 "safe"（未知一律預設 safe），所以分級系統本身抓不到這種幽靈。
# 排程任務那條路早有 daemon_dispatcher.audit_task_tool_refs 守著，前台 bot 的提示
# 沒有 —— 這個常數 + tests/test_telegram_prompt_tool_refs.py 就是補上那道門。
_NO_CONFIRM_TOOL_NAMES = (
    "add_task",
    "create_calendar_event",
    "update_calendar_event",
    "set_task_reminder",
    "set_recurring_reminder",
    "clear_recurring_reminder",
    "complete_task",
    "update_task_status",
)
# 同一段提示反過來說「這些才要確認」——一起釘住，免得哪天 tier 調動了提示卻沒跟上。
_CONFIRM_REQUIRED_TOOL_NAMES = ("delete_task", "delete_calendar_event")

_TG_BOT_SYSTEM_INSTRUCTION_APPEND = (
    "\n\n【當前情境：Telegram 對話】\n"
    "  ⚠️ 重要：這是獨立 chat session，**沒有之前對話的記憶**。\n"
    "      大王若提到「剛剛那個」、「前面那首歌」、「上次那個訂單」這類**指涉式詞彙**，\n"
    "      你無法從 context 得知具體是什麼。**不要猜、不要瘋狂呼叫工具尋找**。\n"
    "      直接回：「大王，因為 Telegram 每則是獨立對話，我看不到您說的『XX』是指哪個，\n"
    "      請您提供具體檔名/客戶名/ID 我再處理。」\n"
    "      若大王有提供向量記憶線索，用 recall(query) 查可能對得上的。\n\n"
    "  - 回覆務必簡短、分段、口語；可用 emoji。\n"
    "  - 避免超過 500 字；需要長報告時用條列 + 每行一句。\n"
    "    ⚠️ 例外：訂單 / 材料 / 庫存 / 樣品 / 生產 / 交期這類**營運分析**問題，\n"
    "    照【鞋廠營運分析守則】的輸出格式走 — 【結論】先單獨發、詳細分析表格\n"
    "    與風險建議接著分段補上，整體可超過 500 字，但每一段仍保持精簡。\n"
    "  - 可以呼叫所有工具，包括 run_shell / browser_*，但寄信、動桌面這種『需要 Mac 前景』的動作，\n"
    "    先口頭確認大王真的要做再執行。\n"
    "  - 回答完直接結束；不要問「還有什麼需要幫忙嗎」這種客套問句。\n"
    "  - 🛑 不確定就直接問大王，不要連續呼叫 5+ 個工具瞎猜 — 每次呼叫都耗時。\n"
    "\n  📎 **附件檔案放哪**：大王從 Telegram 上傳的檔案預設會自動存到\n"
    "      `~/Downloads/小紅-uploads/<日期>/<原檔名>`；雲端 Telegram-only 模式\n"
    "      會改用 exchange artifact store。這個目錄在 path_safety\n"
    "      允許範圍內，你可以**直接** excel_read / pdf_extract_text /\n"
    "      read_document 等讀，**不需要先 +確認移檔**。批次比對 / 摘要 /\n"
    "      抽取請直接動手，再把結果整理成表格回給大王。\n"
    "\n  📄 **整理成 Excel / Word / PDF 檔給大王**：\n"
    "    大王說「這份資料整理成 excel 給我」「給我 word / pdf」「做成檔案 / 表格檔」時，\n"
    "    請直接呼叫 `export_report` — 它會真的產出檔案並自動傳到 Telegram，**不要只回文字**。\n"
    "    content_json 兩種寫法：純資料表給 list of dict；要排版報告給含 title/subtitle/blocks\n"
    "    的 dict（blocks 用 heading / paragraph / table / list / keyvalue 組）。\n"
    "    ⚠️ formats **只給大王明確要的那一種**：說 excel 就 \"excel\"、word 就 \"word\"、pdf 就\n"
    "    \"pdf\"；沒指明預設只給 \"excel\"。**別自作主張多給格式** — 只有大王明講「都要 / 三種 /\n"
    "    全部」才用 \"all\"。\n"
    "    🖼️ **表格要放圖**（規格單/型錄 PDF、或客人做好的 Excel 母表 → 有產品圖的追蹤表）：\n"
    "    PDF 用 `pdf_extract_images`、Excel 母表用 `xlsx_extract_images`（後者會一併回報\n"
    "    每張圖原本錨在哪一列與該列款號，用它對回品項，別照順序硬配），再把回傳路徑填進\n"
    "    該格，寫成 `{\"image\": \"<路徑>\"}` — 圖會**嵌進儲存格**。不要回「AI 無法自行\n"
    "    抓圖進 Excel」或改用檔名/頁碼做文字對照；路徑一律用工具回傳的原字串。\n"
    "\n  🎧 **YouTube 音訊下載**：\n"
    "    大王說「截取聲音檔」「下載 YouTube 音訊」「轉 MP3」「抓這首歌」時，\n"
    "    請直接呼叫 `download_youtube_audio`。不要用 `run_shell` 組 `yt-dlp`\n"
    "    指令，也不要要求大王手動 `pip install`；缺依賴時照 tool 的錯誤訊息回報。\n"
    "    一般聆聽用 `processing_mode=\"podcast\"`，轉錄用 `meeting`，音樂保存用 `music`。\n"
    "  🎬 **YouTube 影片下載**：\n"
    "    大王說「下載影片」「我要影片」「存成 MP4」時，請直接呼叫\n"
    "    `download_youtube_video`。不要用 `run_shell` 組 `yt-dlp` 指令。\n"
    "  🎞️ **Facebook / 其他線上影片下載**：\n"
    "    大王貼 Facebook Reels、fb.watch、Instagram 或其他公開、非 DRM 影片連結\n"
    "    要下載時，請直接呼叫 `download_online_video`。Netflix、Disney+、Hulu、\n"
    "    Max、Prime Video、Apple TV 等受保護串流平台不得下載，不要改用 browser_* 或\n"
    "    `run_shell` 繞過；若 tool 回報 Facebook 解析失敗，照錯誤訊息請大王改貼\n"
    "    實際 `facebook.com/reel/<數字>` 網址或確認影片是公開的。\n"
    "  📺 **HLS/m3u8 下載**：\n"
    "    大王貼公開、未加密 m3u8 並要求下載，或指定 User-Agent/Referer 下載合法 HLS 時，\n"
    "    預設呼叫 `download_hls_with_n_m3u8dl`；若大王指定 ffmpeg，呼叫\n"
    "    `download_hls_with_ffmpeg_copy`；指定 yt-dlp，呼叫 `download_hls_with_ytdlp`。\n"
    "    這些工具會帶自訂 User-Agent 並在下載前檢查 manifest；若有 DRM / EXT-X-KEY / 加密宣告，\n"
    "    不要改用 cookie、key 或 shell 繞過。\n"
    "  🎼 **本機音檔升降 key**：\n"
    "    大王上傳音檔後說「升半音 / 降半音 / 降全音」時，請直接呼叫\n"
    "    `adjust_audio_pitch`，file_path 用最近那個 Telegram 上傳路徑。\n"
    "    不要用 `run_shell` 組 `ffmpeg` 指令。\n"
    "\n  📢 **Tool 結果回報的鐵律 — 不准美化失敗訊息**（防 hallucination）：\n"
    "    判斷一個 tool **沒真執行**的標準（任一條成立即算）：\n"
    "      A. 回傳字串**以**這幾個 emoji 開頭（前面只允許空白）：⚠️ / ❌ / 🔒\n"
    "      B. 回傳字串**含**下列任一明確 phrase（**只認這個列表，不靠關鍵字感覺**）：\n"
    "         「需要確認」「需要 +確認」「需要 c」「需要 cc」\n"
    "         「not permitted」「permission_denied」「未授權」\n"
    "         「已被擋」「已被攔下」「rate-limit」「lockout」\n"
    "         「DANGEROUS 級」「二次確認」\n"
    "    符合上面任一條時：\n"
    "      → 你必須**逐字 echo** tool 回傳的關鍵句（或中文翻譯）給大王，\n"
    "         不准改寫成「已完成」/「已關閉」/「已寄出」之類完成語。\n"
    "      → 可加一句後續建議（「請打 +確認 / c 後再叫我重試」），但動作\n"
    "         結果部分必須跟 tool 實際 return 一致。\n"
    "    **不算失敗**：觀測類 tool 的計數摘要（system_status / workflow_stats /\n"
    "    metrics_overview 等）裡的「成功 N / 失敗 M」這種**有數字伴隨**的\n"
    "    column label 是健康狀態報告，**不是**動作失敗。同理「Token used: N」\n"
    "    這種 cost 摘要也不是 token 失效。判斷時看是不是上面 A/B 條件，\n"
    "    不要光看到「失敗」「Token」就 panic。\n"
    "    歷史教訓：stop_hotword_daemon 因為 +雙確認 缺對應的 +確認 而被\n"
    "    wrap_sensitive_tool 攔下，回「⚠️ 需要 +確認」，但 LLM 寫了\n"
    "    「✅ 已徹底關閉」— 大王 1 分鐘後才從 launchctl 發現 daemon 還在跑。\n"
    "    這是嚴重可信度損傷，不要再發生。\n"
    "\n  🔒 **V4 安全閘（重要）**：\n"
    "    寄信 / 跑 shell / 刪檔 / 動桌面 / 燒錢生圖 等 sensitive 動作有確認門：\n"
    "      → 必須大王在最近 90 秒內發過含「+確認」「確認執行」「c」之類關鍵字的訊息，\n"
    "         tool 才會真執行；否則 tool 回「需要確認」而**不會執行**。\n"
    "    所以遇到大王要寄信 / 動桌面 / 刪資料時，你的標準流程：\n"
    "      1. 草擬 / 描述你要做什麼（誰收信 / 主旨 / 內容前 50 字）\n"
    "      2. 問：「大王要這樣寄出嗎？回『+確認』『確認執行』或單獨打『c』我就動手」\n"
    "      3. 大王回確認 → 你再呼叫 send_gmail / run_shell / 等等\n"
    "    若大王已經一開頭就含確認字眼（包含單字 c），你可以直接動手不需再問。\n"
    "    DANGEROUS 級（刪除類 / run_shell 等）要**兩層**：一般 +確認(c) 之後 30 秒內\n"
    "    再補二次確認『+雙確認』『EXEC』或『cc』才會放行。\n"
    "    ⚠️ 單獨的『cc』在沒先打過 c 的情況下**無效**（防注入預先武裝第二層）。\n"
    "    💡 **兩層可以一則搞定**：一則訊息寫『c cc』（或『cc c』，空白分隔）就同時\n"
    "    授權兩層。所以你要呼叫 DANGEROUS 工具時，**直接請大王打『c cc』**——\n"
    "    不要先要 c、等他打完再要 cc。那會多一輪 LLM 來回（實測約 40 秒），你的\n"
    "    「請補 cc」常常比大王自己送出的 cc 還晚到，看起來像被忽略（2026-08-28\n"
    "    取消兩場會議時實際發生過，大王多打了兩次確認）。\n"
    "    ⚠️ 確認是 one-shot：**每個** DANGEROUS 動作各要一組。要刪三場會議就是三組\n"
    "    『c cc』——這是刻意的（一組 token 不該授權無限次刪除）。事先講清楚總共要\n"
    "    幾組，不要讓大王以為壞掉了。\n"
    "\n  ✅ **免確認直接做（提醒 / 行事曆 / 任務記事類）**：\n"
    "    " + "、".join(f"`{n}`" for n in _NO_CONFIRM_TOOL_NAMES) + " 已改為 SAFE\n"
    "    免確認工具。大王說\n"
    "    「提醒我 / 記下來 / 等下要做 X / 設行事曆 / 排提醒」時，**直接呼叫工具\n"
    "    辦好、不要問 +確認 / c**，做完直接回報結果（建了什麼行程、幾點提醒）。\n"
    "    **不要**再說「這是確認級工具，請打 c」——那已過時。只有刪除類\n"
    "    （" + " / ".join(_CONFIRM_REQUIRED_TOOL_NAMES) + "）跟寄信 / 跑 shell / 動桌面 / 金鑰\n"
    "    才需要確認。\n"
    "    📅 改既有行程：先用 `list_calendar_events` 拿 event_id，再用\n"
    "    `update_calendar_event` 只傳要改的欄位（沒傳的保持原樣）。只改開始時間時\n"
    "    結束時間會依原時長一併平移，不用自己算。**不要**用刪掉重建的方式改行程。\n"
)
_TG_RESTART_AFTER_MSGS = 30
_TG_RESTART_AFTER_NET_ERRORS = 20
# At this threshold, drop the cached HTTPS session before counter climbs
# all the way to _TG_RESTART_AFTER_NET_ERRORS — usually a transient socket
# issue resolves with a fresh pool, no need to bounce the whole process.
_TG_RESET_SESSION_AT_NET_ERRORS = 5
_TG_MAX_TURNS_BEFORE_REBUILD = 15
_TG_SESSION_IDLE_SEC = 30 * 60
# Persistent chat history — when the in-memory chat session is rebuilt
# (daemon restart, idle > 30 min, turns >= 15), we still want 小紅 to
# remember earlier conversation. Factory communication isn't one-shot.
# We persist user/model text turns to disk; on rebuild, the new chat
# session starts with this history pre-loaded so context survives.
#
# Function-call / function-response parts are intentionally NOT persisted —
# they're fragile across SDK versions and the user-facing text turn alone
# carries enough context for follow-up. The model can re-call any tool if
# it needs to.
# (Chat history constants + lock re-exported at the top of this file.)
_TG_CODE_RELOAD_CHECK_INTERVAL_S = _env_float(
    "RED_TG_CODE_RELOAD_CHECK_INTERVAL_S",
    30,
    min_value=1,
    max_value=3600,
)
# NOTE：刻意跟 agent_core.chat_session.chat_state 分開 — telegram 多了
# `last_msg_ts` (idle session reset) 跟 work_mode/intent 切換需要，且
# `_TG_MAX_TURNS_BEFORE_REBUILD=15` 比 chat_session 的
# `CHAT_HISTORY_COMPACT_THRESHOLD=20` 嚴（telegram context 比 REPL 短）。
# 未來若改 chat_session.maybe_compact 加安全 patch，記得 grep 檢查
# 這裡的 inline compaction 邏輯（line ~488 的 turns >= MAX 條件）也要
# 同步補。drift 風險：MED。
_tg_chat_state = {"chat": None, "turns": 0, "last_msg_ts": 0.0}

# 多使用者區隔：每個 chat_id 一份獨立 session state。沒有這層，大王跟
# 員工（例如 gm）會共用同一個 in-memory Gemini chat — 員工看得到
# 大王前幾輪講過的內容（context 直接外洩），confirmation closure 也會
# 抓到別人的 chat_id。`_tg_chat_state`（上面那個全域）保留給 chat_id=""
# 的舊呼叫者（tests / REPL bridge）當 fallback。
# 單執行緒 polling loop 使用，不需要鎖。
_tg_chat_states: dict[str, dict[str, Any]] = {}
_TG_MAX_TRACKED_CHAT_STATES = 64


def _chat_state_for(chat_id: str) -> dict[str, Any]:
    """Return the per-chat session state, creating it on first use."""
    cid = str(chat_id or "").strip()
    if not cid:
        return _tg_chat_state
    state = _tg_chat_states.get(cid)
    if state is None:
        if len(_tg_chat_states) >= _TG_MAX_TRACKED_CHAT_STATES:
            # 砍最久沒講話的一半 — 授權 chat 數量級只有十幾個，這裡只是
            # 防 join 流程 / 群組造成無上限累積。
            oldest = sorted(
                _tg_chat_states.items(),
                key=lambda kv: float(kv[1].get("last_msg_ts") or 0.0),
            )
            for stale_cid, _ in oldest[: max(1, len(oldest) // 2)]:
                _tg_chat_states.pop(stale_cid, None)
        state = {"chat": None, "turns": 0, "last_msg_ts": 0.0}
        _tg_chat_states[cid] = state
    return state

# (Chat history functions re-exported at the top of this file.)


# 檔案上傳上限（Telegram bot api 預設 20MB；超過要本地 Bot API server 才行）
_TG_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024


def _iter_code_reload_files(roots: list[str | os.PathLike[str]] | None = None):
    """Yield (path_str, stat_result) for Python sources whose changes should
    reload the Telegram daemon.

    Runs every 30s in the long-poll loop (×10 bot processes), so it uses
    os.scandir instead of Path.rglob: no per-file Path object / set(parts)
    allocation, __pycache__/.venv pruned at directory level (never descended),
    and the DirEntry's cached stat() is handed back so callers needn't stat
    the file a second time.
    """
    if roots is None:
        from agent_core.logging_and_paths import REPO_ROOT
        roots = [
            os.path.join(REPO_ROOT, "agent_daemon.py"),
            os.path.join(REPO_ROOT, "agent_core"),
            os.path.join(REPO_ROOT, "launchd", "scripts"),
        ]
    for root in roots:
        root = os.fspath(root)
        if os.path.isfile(root):
            if root.endswith(".py"):
                try:
                    yield root, os.stat(root)
                except OSError:
                    pass
            continue
        if not os.path.isdir(root):
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in ("__pycache__", ".venv"):
                                stack.append(entry.path)
                        elif entry.name.endswith(".py"):
                            try:
                                yield entry.path, entry.stat()
                            except OSError:
                                continue
            except OSError:
                continue


def _code_reload_snapshot(roots: list[str | os.PathLike[str]] | None = None) -> tuple[int, int, int]:
    """Return a compact source-tree fingerprint: max mtime, count, total size."""
    max_mtime_ns = 0
    count = 0
    total_size = 0
    for _path, st in _iter_code_reload_files(roots):
        count += 1
        mtime_ns = int(st.st_mtime_ns)
        if mtime_ns > max_mtime_ns:
            max_mtime_ns = mtime_ns
        total_size += int(st.st_size)
    return max_mtime_ns, count, total_size


# git 合併衝突標記（opener / diff3 base / closer）— 都帶尾隨內容，正常 Python
# 原始碼不會出現，誤判率近零。用來判斷工作樹是否還在 merge 半成品。
_CONFLICT_MARKERS = ("<<<<<<< ", "||||||| ", ">>>>>>> ")


def _conflict_marked_reload_files(
    roots: list[str | os.PathLike[str]] | None = None,
) -> list[str]:
    """回傳目前帶有 git 衝突標記的 reload .py 檔。

    並行 session 在共用部署分支 `git merge` 時，衝突檔會被寫入
    `<<<<<<< / ======= / >>>>>>>` 標記並留在工作樹直到解完。這段期間檔案不是
    合法 Python，import 會 SyntaxError——用這個在 self-exec 前先擋掉。
    """
    bad: list[str] = []
    for path, _st in _iter_code_reload_files(roots):
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith(_CONFLICT_MARKERS):
                bad.append(path)
                break
    return bad


def _exec_self_for_code_reload() -> None:
    """Replace this process with a fresh copy so imports use latest source.

    Guard：若工作樹還在 merge 半成品（有未解衝突標記），**跳過** self-exec、
    繼續跑當前這份能動的舊碼；caller 下個輪詢會再檢查，等樹收斂乾淨才真的
    exec（零停機）。擋掉並行 `git merge` 把衝突標記寫進待 import 檔時，熱重載
    self-exec 進壞樹 → import SyntaxError → launchd KeepAlive 重啟 → 再 exec
    的 crash-loop（2026-06-15：white_legal/specs.py `<<<<<<< HEAD` 把 10 色
    艦隊打掛約 2 分鐘）。
    """
    broken = _conflict_marked_reload_files()
    if broken:
        shown = "、".join(Path(p).name for p in broken[:3])
        more = f" 等共 {len(broken)} 檔" if len(broken) > 3 else ""
        print(
            f"[tg bot] ⏸️ 偵測到程式碼更新，但工作樹有未解 merge 衝突標記"
            f"（{shown}{more}）→ 暫緩 self-exec、續跑舊碼，待收斂後再載入。",
            flush=True,
        )
        return
    argv = [sys.executable] + sys.argv
    print(f"[tg bot] 🔄 偵測到程式碼更新，self-exec 載入新碼：{' '.join(sys.argv)}", flush=True)
    os.execv(sys.executable, argv)


def _safe_filename(name: str) -> str:
    """大王 client 提供的 filename 不可信任 — 清掉危險字元。"""
    from agent_core.path_safety import safe_cjk_filename
    return safe_cjk_filename(name, max_len=200, fallback="untitled")


def _extract_telegram_attachment(msg: dict) -> dict | None:
    """從 Telegram message 抽附件 metadata。回 None 表沒附件。

    支援：document / photo / voice / audio / video
    回 dict：{file_id, file_name, mime_type, size, kind}
    """
    if not isinstance(msg, dict):
        return None
    # document：有完整 metadata
    doc = msg.get("document")
    if doc:
        return {
            "kind": "document",
            "file_id": doc.get("file_id", ""),
            "file_name": doc.get("file_name", "document.bin"),
            "mime_type": doc.get("mime_type", "application/octet-stream"),
            "size": int(doc.get("file_size", 0)),
        }
    # photo：array of sizes，取最大
    photos = msg.get("photo")
    if photos and isinstance(photos, list):
        biggest = max(photos, key=lambda p: int(p.get("file_size", 0)))
        return {
            "kind": "photo",
            "file_id": biggest.get("file_id", ""),
            "file_name": f"photo_{biggest.get('file_unique_id', 'x')}.jpg",
            "mime_type": "image/jpeg",
            "size": int(biggest.get("file_size", 0)),
        }
    # voice
    v = msg.get("voice")
    if v:
        return {
            "kind": "voice",
            "file_id": v.get("file_id", ""),
            "file_name": f"voice_{int(time.time())}.ogg",
            "mime_type": v.get("mime_type", "audio/ogg"),
            "size": int(v.get("file_size", 0)),
        }
    # audio
    a = msg.get("audio")
    if a:
        return {
            "kind": "audio",
            "file_id": a.get("file_id", ""),
            "file_name": a.get("file_name") or f"audio_{int(time.time())}.mp3",
            "mime_type": a.get("mime_type", "audio/mpeg"),
            "size": int(a.get("file_size", 0)),
        }
    # video
    vid = msg.get("video")
    if vid:
        return {
            "kind": "video",
            "file_id": vid.get("file_id", ""),
            "file_name": vid.get("file_name") or f"video_{int(time.time())}.mp4",
            "mime_type": vid.get("mime_type", "video/mp4"),
            "size": int(vid.get("file_size", 0)),
        }
    return None


def _download_telegram_attachment(token: str, attachment: dict,
                                    requests_module=_requests) -> tuple[str, str]:
    """下載附件到 ~/Downloads/小紅-uploads/{YYYY-MM-DD}/{safe_name}。

    可用 env `RED_TELEGRAM_UPLOAD_DIR` 覆寫上傳根目錄。

    Returns (saved_path, error_msg)。error_msg 非空表失敗。
    """
    file_id = attachment.get("file_id", "")
    if not file_id:
        return "", "缺 file_id"
    size = attachment.get("size", 0)
    if size > _TG_DOWNLOAD_MAX_BYTES:
        return "", (f"檔案 {size / 1024 / 1024:.1f}MB 超過 Telegram bot api "
                    f"20MB 下載上限")

    session = _get_tg_session(requests_module)

    # Step 1: getFile API → file_path
    try:
        r = session.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id}, timeout=(15, 15),
        )
        data = r.json()
        if not data.get("ok"):
            return "", f"getFile 失敗：{data.get('description', '?')[:200]}"
        file_path = (data.get("result") or {}).get("file_path", "")
        if not file_path:
            return "", "getFile 沒給 file_path"
    except Exception as e:
        return "", f"getFile 例外：{type(e).__name__}: {e}"

    # Step 2: 真下載
    try:
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        r = session.get(url, timeout=(15, 60), stream=True)
        if r.status_code != 200:
            return "", f"下載失敗 HTTP {r.status_code}"
        content = r.content
    except Exception as e:
        return "", f"下載例外：{type(e).__name__}: {e}"

    # Step 3: 存檔到 Telegram inbound upload root.
    #
    # 為什麼不存 var/data/telegram_uploads/：那條路徑被 path_safety 列入
    # _PROTECTED_PROJECT_DIRS（同保護 chroma_db / parquet 索引那條規則），
    # 導致小紅後續 read 自己剛收到的附件都會被擋，每次只好叫大王 +確認 才能
    # 「複製到 Downloads」— 三檔三次確認的糟糕 UX。直接存進使用者 Downloads
    # 區就解決：path_safety 不擋 user space、Excel/PDF 等 read-only tool 不
    # 是 sensitive，所以小紅可以馬上比對 / 摘要。
    #
    # 安全層面沒退步：此函式只下載**使用者剛剛主動上傳**到自己的 Telegram
    # chat 的內容，跟「小紅自己跑去抓任意 URL」不同；落地後也沒人會去自動
    # 執行（小紅讀檔走 path_safety / sensitive list；使用者自己雙擊 Finder
    # 的風險本來就存在，跟我們存哪無關）。
    # Codex P2 (PR #23): expand ~ and resolve to absolute path. Without
    # this, `RED_TELEGRAM_UPLOAD_DIR=~/MyUploads` literally creates a
    # `~/MyUploads/` directory relative to the daemon's cwd; the path
    # we hand back to the LLM still starts with `~/...`, but downstream
    # readers (excel_read / pdf_extract_text / read_document) call
    # os.path.expanduser themselves and look at $HOME/MyUploads/...,
    # missing the freshly downloaded file.
    try:
        from agent_core.exchange_policy import telegram_upload_root
        upload_root = telegram_upload_root()
    except Exception:
        upload_root = os.environ.get("RED_TELEGRAM_UPLOAD_DIR", "").strip()
        if not upload_root:
            upload_root = os.path.join(os.path.expanduser("~"), "Downloads", "小紅-uploads")
        else:
            upload_root = os.path.abspath(os.path.expanduser(upload_root))
    today = datetime.now().strftime("%Y-%m-%d")
    save_dir = os.path.join(upload_root, today)
    try:
        os.makedirs(save_dir, exist_ok=True)
    except Exception as e:
        return "", f"建 dir 失敗：{e}"

    safe_name = _safe_filename(attachment.get("file_name", "untitled"))
    save_path = os.path.join(save_dir, safe_name)
    # 若同名已存在，append timestamp
    if os.path.exists(save_path):
        stem, ext = os.path.splitext(safe_name)
        ts = datetime.now().strftime("%H%M%S")
        save_path = os.path.join(save_dir, f"{stem}_{ts}{ext}")
    try:
        with open(save_path, "wb") as f:
            f.write(content)
    except Exception as e:
        return "", f"寫檔失敗：{e}"

    return save_path, ""


def extract_empty_reason(resp) -> str:
    """Explain why Gemini returned no text for Telegram."""
    try:
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            return ""
        c0 = cands[0]
        parts = getattr(c0.content, "parts", None) or []
        fn_names = []
        for part in parts:
            fn = getattr(part, "function_call", None)
            if fn and getattr(fn, "name", None):
                fn_names.append(fn.name)
        finish = getattr(c0, "finish_reason", None)
        finish_str = str(finish).split(".")[-1] if finish else ""

        msg_parts = []
        if fn_names:
            unique = list(dict.fromkeys(fn_names))[:5]
            msg_parts.append(f"我嘗試呼叫了工具：{', '.join(unique)}")
        if finish_str and finish_str not in ("STOP", "1"):
            msg_parts.append(f"停止原因：{finish_str}")
        if not msg_parts:
            return ""
        return (
            "（小紅卡住了 — " + "；".join(msg_parts) + "）\n"
            "這通常表示：\n"
            "  1️⃣ 大王說的東西我沒有 context（Telegram 每則獨立對話）\n"
            "  2️⃣ 或工具呼叫 25 次上限用完還沒找到答案\n"
            "請大王提供更具體的檔名 / 客戶名 / ID，我再處理。"
        )
    except Exception:
        return ""


def tg_get_token_and_chat() -> tuple[str, str]:
    # get_secret() has its own internal try/except and returns a SecretLookup
    # with value="" on any failure, so an outer try/except here would just
    # be unreachable dead code. Keep this path lean and predictable.
    from agent_core.secret_provider import get_secret

    token_secret_name = (
        os.environ.get("RED_TELEGRAM_BOT_TOKEN_SECRET_NAME")
        or "telegram-bot-token"
    ).strip() or "telegram-bot-token"
    token_keyring_name = (
        os.environ.get("RED_TELEGRAM_BOT_TOKEN_KEYRING_NAME")
        or token_secret_name
    ).strip() or token_secret_name
    chat_secret_name = (
        os.environ.get("RED_TELEGRAM_CHAT_ID_SECRET_NAME")
        or "telegram-chat-id"
    ).strip() or "telegram-chat-id"
    chat_keyring_name = (
        os.environ.get("RED_TELEGRAM_CHAT_ID_KEYRING_NAME")
        or chat_secret_name
    ).strip() or chat_secret_name

    token = get_secret(
        token_secret_name,
        env_names=("TELEGRAM_BOT_TOKEN", "RED_TELEGRAM_BOT_TOKEN"),
        keyring_service="xiaohong-agent",
        keyring_name=token_keyring_name,
    ).value
    chat_id = get_secret(
        chat_secret_name,
        env_names=("TELEGRAM_CHAT_ID", "RED_TELEGRAM_CHAT_ID"),
        keyring_service="xiaohong-agent",
        keyring_name=chat_keyring_name,
    ).value
    return token, chat_id


def _telegram_inbound_actors(owner_chat_id: str) -> dict[str, dict[str, str]]:
    try:
        from agent_core.telegram_agent_config import telegram_actors
        actors = telegram_actors(owner_chat_id)
    except Exception as exc:
        print(f"[tg bot] ⚠️ Telegram agent config 載入失敗（只允許 owner）：{exc}")
        actors = {}
    if owner_chat_id and owner_chat_id not in actors:
        actors[owner_chat_id] = {
            "chat_id": owner_chat_id,
            "color": "red",
            "email": "",
            "name": "Red owner",
            "source": "telegram-chat-id",
            "is_owner": "true",
        }
    approval_owner_chat_id = _telegram_approval_owner_chat_id(owner_chat_id)
    if approval_owner_chat_id:
        actors[approval_owner_chat_id] = {
            "chat_id": approval_owner_chat_id,
            "color": "red",
            "email": "",
            "name": "Red approval owner",
            "source": "telegram-approval-owner",
            "is_owner": "true",
        }
    if _telegram_default_private_requires_owner_approval():
        actor_color = _telegram_default_actor_color()
        if actor_color:
            # 這個閘擋的是「陌生私訊自動取得部門色身分」—— 未核准的 chat 一律
            # 走 join-request。employee registry 的 telegram_user_id 綁定是管理
            # 員在 web 管理頁的顯式授權，語義上等同 owner 已核准，必須放行
            # （2026-07-18 前被這裡靜默丟棄，registry 綁好的員工仍卡 join
            # 流程）。RED_TELEGRAM_AGENT_CHATS 等其餘來源照舊清掉。
            keep: dict[str, dict[str, str]] = {
                chat_id: actor
                for chat_id, actor in actors.items()
                if str(actor.get("source") or "") == "employee_registry"
            }
            if owner_chat_id and owner_chat_id in actors:
                keep[owner_chat_id] = actors[owner_chat_id]
            if approval_owner_chat_id and approval_owner_chat_id in actors:
                keep[approval_owner_chat_id] = actors[approval_owner_chat_id]
            actors = keep
    actors.update(_telegram_private_approval_actors())
    if owner_chat_id:
        actors[owner_chat_id] = {
            "chat_id": owner_chat_id,
            "color": "red",
            "email": "",
            "name": "Red owner",
            "source": "telegram-chat-id",
            "is_owner": "true",
        }
    if approval_owner_chat_id:
        actors[approval_owner_chat_id] = {
            "chat_id": approval_owner_chat_id,
            "color": "red",
            "email": "",
            "name": "Red approval owner",
            "source": "telegram-approval-owner",
            "is_owner": "true",
        }
    return actors


def _resolve_inbound_actor(chat, msg, telegram_actors):
    """解析授權訊息的 actor，回 (actor_or_None, reason)。

    群組/超級群組**不以 chat.id 當身分**：RED_TELEGRAM_AGENT_CHATS 可把一個群綁到
    某部門色，若用 chat.id 當 actor，群裡任何成員（可能被任意人拉進、未經 RED 審核）
    都會繼承該色、能讀該部門 RAG/信件。改認發訊者 from.id —— 只有已註冊員工
    （telegram_actors 有其 telegram_user_id）以其本人身分放行，未註冊回
    (None, 'group_member_not_registered')。私訊 chat.id==from.id 不受影響，仍以
    chat.id 解析。（callback 路徑早已用 from.id。）"""
    chat_type = str((chat or {}).get("type") or "")
    if chat_type in ("group", "supergroup"):
        from_id = str(((msg or {}).get("from") or {}).get("id") or "").strip()
        actor = telegram_actors.get(from_id) if from_id else None
        if not actor:
            return None, "group_member_not_registered"
        return actor, "group_member_registered"
    sender_id = str((chat or {}).get("id") or "")
    return telegram_actors.get(sender_id, {}), "chat_id"


def _confirm_scope_for(msg, chat_id) -> str:
    """+確認 窗口的 scope：綁定群/超級群組回 "<chat_id>:<from_id>"（per-user，避免
    同群裡 A 打的 +確認 arm 到 B 的敏感請求）；私訊/其他回 chat_id（chat_id==from_id，
    行為不變）。mark 與 check 兩側都經此函式，確保同一 confirm 事件用同一把 key。"""
    cid = str(chat_id or "")
    chat = (msg or {}).get("chat") or {}
    if str(chat.get("type") or "") in ("group", "supergroup"):
        fid = str(((msg or {}).get("from") or {}).get("id") or "").strip()
        if fid:
            return f"{cid}:{fid}"
    return cid


def _telegram_actor_log_summary(owner_chat_id: str) -> str:
    try:
        actors = _telegram_inbound_actors(owner_chat_id)
        by_color: dict[str, int] = {}
        for actor in actors.values():
            color = str(actor.get("color") or "unknown")
            by_color[color] = by_color.get(color, 0) + 1
        parts = ", ".join(f"{color}:{count}" for color, count in sorted(by_color.items()))
        return f"{len(actors)} chat(s)" + (f" ({parts})" if parts else "")
    except Exception:
        count = 1 if owner_chat_id else 0
        return f"{count} chat(s) (red:{count})" if count else "0 chat(s)"


def _telegram_default_actor_color() -> str:
    raw = os.environ.get("RED_TELEGRAM_DEFAULT_ACTOR_COLOR", "").strip().lower()
    if not raw:
        return ""
    try:
        from agent_core.agents.permission_matrix import Agent
        return Agent(raw).value
    except Exception:
        return ""


def _telegram_default_private_actor_enabled() -> bool:
    return _env_truthy("RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS")


def _telegram_default_private_requires_owner_approval() -> bool:
    return _env_truthy("RED_TELEGRAM_REQUIRE_OWNER_APPROVAL_FOR_DEFAULT_PRIVATE_CHATS")


def _telegram_agent_namespace() -> str:
    suffix = os.environ.get("RED_TELEGRAM_STATE_SUFFIX", "").strip()
    if not suffix:
        suffix = _telegram_default_actor_color()
    if not suffix:
        token_secret_name = (
            os.environ.get("RED_TELEGRAM_BOT_TOKEN_SECRET_NAME")
            or "telegram-bot-token"
        ).strip()
        if token_secret_name and token_secret_name != "telegram-bot-token":
            suffix = token_secret_name
    if not suffix:
        suffix = "red"
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", suffix).strip("-_.").lower()
    return safe_suffix or "red"


def _telegram_approval_owner_chat_id(fallback_owner_chat_id: str = "") -> str:
    from agent_core.secret_provider import get_secret

    secret_name = (
        os.environ.get("RED_TELEGRAM_APPROVAL_OWNER_CHAT_ID_SECRET_NAME")
        or ""
    ).strip()
    keyring_name = (
        os.environ.get("RED_TELEGRAM_APPROVAL_OWNER_CHAT_ID_KEYRING_NAME")
        or secret_name
        or ""
    ).strip()
    env_names = (
        "RED_TELEGRAM_APPROVAL_OWNER_CHAT_ID",
        "TELEGRAM_APPROVAL_OWNER_CHAT_ID",
    )
    if secret_name or keyring_name:
        lookup = get_secret(
            secret_name or "telegram-chat-id",
            env_names=env_names,
            keyring_service="xiaohong-agent",
            keyring_name=keyring_name or secret_name,
        )
        if lookup.value:
            return lookup.value
    for env_name in env_names:
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return raw
    return str(fallback_owner_chat_id or "").strip()


def _telegram_private_approvals_file() -> str:
    override = os.environ.get("RED_TELEGRAM_PRIVATE_APPROVALS_FILE", "").strip()
    if override:
        return override
    from agent_core.logging_and_paths import DATA_DIR
    return os.path.join(
        DATA_DIR,
        f"telegram_private_approvals_{_telegram_agent_namespace()}.json",
    )


def _telegram_approval_store():
    try:
        from agent_core import operational_telegram_approvals
    except Exception as exc:  # noqa: BLE001 - optional cloud backend
        print(f"[tg bot] ⚠️ Telegram approval DB backend 載入失敗：{exc}")
        return None
    if not operational_telegram_approvals.enabled():
        return None
    return operational_telegram_approvals


def _drop_invalid_approval_actor_colors(
    actors: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """讀入 approval 記錄時就用 Agent() 驗 color，無效記 warning 並略過該筆。

    無效 color 若放進 inbound actors，下游 tg_handle_message 的 Agent(color)
    會失敗 —— 舊行為 fail-open 成 RED（SUPER_ADMIN），新行為 fail-closed 拒絕。
    兩者都不該發生：壞記錄在源頭就擋掉，讓該 chat 回到「未授權」路徑。
    """
    try:
        from agent_core.agents.permission_matrix import Agent
    except Exception:
        return actors
    valid: dict[str, dict[str, str]] = {}
    for chat_id, record in (actors or {}).items():
        color = str((record or {}).get("color") or "").strip().lower()
        try:
            Agent(color)
        except Exception:
            print(
                f"[tg bot] ⚠️ approval 記錄 color 無效（chat {chat_id}: {color!r}）"
                "— 略過該筆，請管理員修正"
            )
            continue
        valid[chat_id] = record
    return valid


def _telegram_private_approval_actors() -> dict[str, dict[str, str]]:
    store = _telegram_approval_store()
    namespace = _telegram_agent_namespace()
    if store:
        try:
            return _drop_invalid_approval_actor_colors(store.list_approved_actors(
                namespace=namespace,
                default_color=_telegram_default_actor_color(),
            ))
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            print(f"[tg bot] ⚠️ Telegram approval DB 讀取失敗，改用本機檔案：{exc}")

    from agent_core.state_io import locked_json

    path = _telegram_private_approvals_file()
    with locked_json(path, default={}) as data:
        approvals = data.get("approvals") if isinstance(data, dict) else {}
        if not isinstance(approvals, dict):
            return {}
        rows = {
            str(chat_id): dict(record)
            for chat_id, record in approvals.items()
            if isinstance(record, dict)
            and str(record.get("status") or "approved") == "approved"
        }
    actors: dict[str, dict[str, str]] = {}
    for chat_id, record in rows.items():
        color = str(record.get("color") or _telegram_default_actor_color() or "").strip()
        if not color:
            continue
        actors[chat_id] = {
            "chat_id": chat_id,
            "telegram_user_id": str(record.get("telegram_user_id") or ""),
            "color": color,
            "email": str(record.get("email") or ""),
            "name": str(record.get("name") or f"{color} Telegram user"),
            "source": f"telegram_private_approval:{namespace}",
            "is_owner": "false",
        }
    return _drop_invalid_approval_actor_colors(actors)


def _telegram_default_actor_for_message(
    message: Mapping[str, Any],
) -> dict[str, str]:
    """Return a fallback actor for standalone department bots.

    A per-agent Telegram bot such as Orange does not know each employee's
    chat.id before they message it. This opt-in path treats private messages
    to that bot as the configured default color, while still keeping groups
    closed unless their chat.id is explicitly bound.
    """
    if not _telegram_default_private_actor_enabled():
        return {}
    if _telegram_default_private_requires_owner_approval():
        return {}
    color = _telegram_default_actor_color()
    if not color:
        return {}
    if not isinstance(message, Mapping):
        return {}
    chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
    if str(chat.get("type") or "").lower() != "private":
        return {}
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return {}
    sender = message.get("from") if isinstance(message.get("from"), Mapping) else {}
    display_name = os.environ.get("RED_TELEGRAM_DEFAULT_ACTOR_NAME", "").strip()
    if not display_name:
        name_parts = [
            str(sender.get("first_name") or ""),
            str(sender.get("last_name") or ""),
        ]
        display_name = " ".join(p for p in name_parts if p).strip()
    if not display_name and sender.get("username"):
        display_name = f"@{sender.get('username')}"
    if not display_name:
        display_name = f"{color} private chat"
    return {
        "chat_id": chat_id,
        "telegram_user_id": str(sender.get("id") or "").strip(),
        "color": color,
        "email": "",
        "name": display_name,
        "source": "RED_TELEGRAM_DEFAULT_ACTOR_COLOR",
        "is_owner": "false",
    }


def _telegram_unbound_private_chat_reply(message: Mapping[str, Any]) -> str:
    """Explain why an otherwise valid private Telegram message was ignored."""
    if not isinstance(message, Mapping):
        return ""
    chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
    if str(chat.get("type") or "").lower() != "private":
        return ""
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return ""

    identity = _telegram_whoami_reply(
        chat_id=chat_id,
        telegram_actor={
            "color": "-",
            "source": "unbound",
            "name": "尚未綁定",
            "is_owner": "false",
        },
        telegram_message=message,
    )
    return (
        "小紅有收到你的訊息，但這個 Telegram 帳號尚未綁定公司身份，"
        "所以目前不會處理內容。\n\n"
        "我已經把加入申請送給管理員；管理員在 Telegram 核准後，"
        "你下一則訊息就可以使用。\n\n"
        "如果管理員需要手動綁定，請把下面這段資料傳給他：\n\n"
        f"{identity}"
    )


def _telegram_join_requests_file() -> str:
    override = os.environ.get("RED_TELEGRAM_JOIN_REQUESTS_FILE", "").strip()
    if override:
        return override
    from agent_core.logging_and_paths import DATA_DIR
    return os.path.join(
        DATA_DIR,
        f"telegram_join_requests_{_telegram_agent_namespace()}.json",
    )


def _telegram_message_chat_id(message: Mapping[str, Any]) -> str:
    chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
    return str(chat.get("id") or "").strip()


def _telegram_message_sender(message: Mapping[str, Any]) -> Mapping[str, Any]:
    sender = message.get("from") if isinstance(message.get("from"), Mapping) else {}
    return sender


def _telegram_join_display_name(message: Mapping[str, Any]) -> str:
    sender = _telegram_message_sender(message)
    name_parts = [
        str(sender.get("first_name") or ""),
        str(sender.get("last_name") or ""),
    ]
    name = " ".join(part for part in name_parts if part).strip()
    if name:
        return name
    username = str(sender.get("username") or "").strip()
    if username:
        return f"@{username}"
    chat_id = _telegram_message_chat_id(message)
    return f"Telegram {chat_id}" if chat_id else "Telegram user"


def _telegram_pending_employee_email(chat_id: str) -> str:
    raw = str(chat_id or "").strip()
    safe = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    if raw.startswith("-"):
        safe = f"neg-{safe}"
    return f"telegram-{safe or 'unknown'}@{_TG_JOIN_EMAIL_DOMAIN}"


def _telegram_trim_preview(text: str, *, limit: int = 200) -> str:
    preview = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return preview[:limit]


def _telegram_record_join_request(
    message: Mapping[str, Any],
    *,
    text: str = "",
    update_id: str | int = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Persist a pending private-chat join request and throttle owner prompts."""
    if not isinstance(message, Mapping):
        return {}
    chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
    if str(chat.get("type") or "").lower() != "private":
        return {}
    chat_id = _telegram_message_chat_id(message)
    if not chat_id:
        return {}

    from agent_core.state_io import locked_json

    sender = _telegram_message_sender(message)
    now_ts = time.time() if now is None else float(now)
    now_iso = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()

    store = _telegram_approval_store()
    if store:
        try:
            return store.record_join_request(
                namespace=_telegram_agent_namespace(),
                chat_id=chat_id,
                telegram_user_id=str(sender.get("id") or "").strip(),
                username=str(sender.get("username") or "").strip(),
                name=_telegram_join_display_name(message),
                chat_type=str(chat.get("type") or ""),
                text=_telegram_trim_preview(text),
                update_id=update_id,
                now_ts=now_ts,
                notify_interval_s=_TG_JOIN_NOTIFY_INTERVAL_S,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            print(f"[tg bot] ⚠️ Telegram join-request DB 寫入失敗，改用本機檔案：{exc}")

    path = _telegram_join_requests_file()

    with locked_json(path, default={}) as data:
        requests = data.setdefault("requests", {})
        if not isinstance(requests, dict):
            requests = {}
            data["requests"] = requests
        existing = requests.get(chat_id)
        if not isinstance(existing, dict):
            existing = {}
        last_notified = float(existing.get("last_notified_at") or 0)
        should_notify = (
            str(existing.get("status") or "pending") != "pending"
            or now_ts - last_notified >= _TG_JOIN_NOTIFY_INTERVAL_S
        )
        record = {
            **existing,
            "chat_id": chat_id,
            "telegram_user_id": str(sender.get("id") or "").strip(),
            "username": str(sender.get("username") or "").strip(),
            "name": _telegram_join_display_name(message),
            "chat_type": str(chat.get("type") or ""),
            "status": "pending",
            "requested_at": existing.get("requested_at") or now_iso,
            "last_seen_at": now_iso,
            "last_seen_ts": now_ts,
            "last_text": _telegram_trim_preview(text),
            "update_id": str(update_id or ""),
        }
        if should_notify:
            record["last_notified_at"] = now_ts
            record["last_notified_at_iso"] = now_iso
        requests[chat_id] = record
        data["latest_chat_id"] = chat_id
    return {**record, "should_notify": should_notify}


def _telegram_pending_join_requests() -> list[dict[str, Any]]:
    cutoff = time.time() - _TG_JOIN_TTL_S
    store = _telegram_approval_store()
    if store:
        try:
            return store.pending_join_requests(
                namespace=_telegram_agent_namespace(),
                cutoff_ts=cutoff,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            print(f"[tg bot] ⚠️ Telegram join-request DB 讀取失敗，改用本機檔案：{exc}")

    from agent_core.state_io import locked_json

    path = _telegram_join_requests_file()
    with locked_json(path, default={}) as data:
        requests = data.get("requests") if isinstance(data, dict) else {}
        if not isinstance(requests, dict):
            return []
        pending = [
            record
            for record in requests.values()
            if (
                isinstance(record, dict)
                and str(record.get("status") or "pending") == "pending"
                and float(record.get("last_seen_ts") or 0) >= cutoff
            )
        ]
    return sorted(pending, key=lambda item: float(item.get("last_seen_ts") or 0), reverse=True)


def _telegram_latest_pending_join_chat_id() -> str:
    pending = _telegram_pending_join_requests()
    return str(pending[0].get("chat_id") or "") if pending else ""


def _telegram_join_agent_label(color: str = "") -> str:
    raw = str(color or _telegram_default_join_color() or _telegram_agent_namespace()).strip()
    labels = {
        "orange": "Orange 業務",
        "yellow": "Yellow 採購",
        "green": "Green 樣品",
        "blue": "Blue 船務",
        "indigo": "Indigo 倉庫",
        "purple": "Purple 會計",
        "gray": "Gray",
        "black": "Black",
        "white": "White",
    }
    return labels.get(raw.lower(), raw or "此 Agent")


def _telegram_join_callback_data(action: str, chat_id: str) -> str:
    action_key = "a" if str(action).lower().startswith("approve") else "r"
    safe_chat_id = re.sub(r"[^0-9-]+", "", str(chat_id or ""))
    return f"{_TG_JOIN_CALLBACK_PREFIX}:{action_key}:{_telegram_agent_namespace()}:{safe_chat_id}"


def _telegram_join_approval_reply_markup(record: Mapping[str, Any]) -> dict[str, Any]:
    chat_id = str(record.get("chat_id") or "").strip()
    if not chat_id:
        return {}
    return {
        "inline_keyboard": [[
            {
                "text": "同意",
                "callback_data": _telegram_join_callback_data("approve", chat_id),
            },
            {
                "text": "不同意",
                "callback_data": _telegram_join_callback_data("reject", chat_id),
            },
        ]]
    }


def _telegram_join_owner_prompt(record: Mapping[str, Any]) -> str:
    chat_id = str(record.get("chat_id") or "").strip()
    name = str(record.get("name") or f"Telegram {chat_id}").strip()
    username = str(record.get("username") or "").strip()
    user_line = f"{name}" + (f" (@{username})" if username and not name.startswith("@") else "")
    preview = str(record.get("last_text") or "").strip() or "-"
    default_color = _telegram_default_join_color()
    agent_label = _telegram_join_agent_label(default_color)
    color_line = f"同意後會加入: {agent_label}\n" if default_color else ""
    return (
        f"{agent_label} 有人要加入，同意嗎？\n\n"
        f"申請人: {user_line}\n"
        f"chat.id: {chat_id}\n"
        f"from.id: {record.get('telegram_user_id') or '-'}\n"
        f"他剛剛說: {preview}\n\n"
        f"{color_line}"
        "請直接按下面的「同意」或「不同意」。"
    )


def _telegram_join_pending_summary() -> str:
    pending = _telegram_pending_join_requests()
    if not pending:
        return "目前沒有待審 Telegram 加入申請。"
    lines = ["🟡 待審 Telegram 加入申請"]
    for record in pending[:10]:
        chat_id = str(record.get("chat_id") or "")
        name = str(record.get("name") or f"Telegram {chat_id}")
        username = str(record.get("username") or "").strip()
        label = f"{name}" + (f" (@{username})" if username and not name.startswith("@") else "")
        default_color = _telegram_default_join_color() or "green"
        lines.append(f"- {chat_id}: {label}，核准：/approve_tg {chat_id} {default_color} {name}")
    return "\n".join(lines)


def _telegram_actor_is_owner(actor: Mapping[str, Any] | None) -> bool:
    if not isinstance(actor, Mapping):
        return False
    return str(actor.get("is_owner") or "").strip().lower() == "true"


def _telegram_parse_join_target_and_color(rest: str) -> tuple[str, str, str]:
    pieces = (rest or "").strip().split(maxsplit=2)
    if not pieces:
        return "", "", ""
    first = pieces[0].strip()
    try:
        from agent_core.agents.permission_matrix import Agent
        Agent(first.lower())
        return "", first.lower(), pieces[1].strip() if len(pieces) >= 2 else ""
    except Exception:
        pass
    chat_id = first
    color = pieces[1].strip().lower() if len(pieces) >= 2 else ""
    name = pieces[2].strip() if len(pieces) >= 3 else ""
    return chat_id, color, name


def _telegram_default_join_color() -> str:
    return _telegram_default_actor_color()


def _telegram_validate_join_color(color: str) -> tuple[str, str]:
    try:
        from agent_core.agents.permission_matrix import Agent
        agent = Agent(str(color or "").strip().lower())
    except Exception:
        return "", "部門顏色無效。可用: orange, yellow, green, blue, indigo, purple, gray, black, white"
    if agent.value == "red":
        return "", "不能透過 Telegram 快速核准建立 red 管理員。請到 web 管理頁處理。"
    return agent.value, ""


def _telegram_save_private_approval(
    *,
    chat_id: str,
    color: str,
    name: str,
    telegram_user_id: str = "",
    username: str = "",
    approved_by: str = "",
) -> str:
    email = _telegram_pending_employee_email(chat_id)
    store = _telegram_approval_store()
    if store:
        try:
            return store.save_private_approval(
                namespace=_telegram_agent_namespace(),
                chat_id=chat_id,
                color=color,
                name=name,
                email=email,
                telegram_user_id=telegram_user_id,
                username=username,
                approved_by=approved_by,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            print(f"[tg bot] ⚠️ Telegram approval DB 寫入失敗，改用本機檔案：{exc}")

    from agent_core.state_io import locked_json

    now_iso = datetime.now(timezone.utc).isoformat()
    path = _telegram_private_approvals_file()
    with locked_json(path, default={}) as data:
        approvals = data.setdefault("approvals", {})
        if not isinstance(approvals, dict):
            approvals = {}
            data["approvals"] = approvals
        approvals[chat_id] = {
            "chat_id": chat_id,
            "telegram_user_id": str(telegram_user_id or "").strip(),
            "username": str(username or "").strip(),
            "name": name,
            "color": color,
            "email": email,
            "status": "approved",
            "approved_by": str(approved_by or "").strip(),
            "approved_at": now_iso,
            "namespace": _telegram_agent_namespace(),
        }
    return email


def _telegram_update_join_status(
    chat_id: str,
    *,
    status: str,
    color: str = "",
    email: str = "",
    approved_by: str = "",
) -> dict[str, Any]:
    store = _telegram_approval_store()
    if store:
        try:
            return store.update_join_status(
                namespace=_telegram_agent_namespace(),
                chat_id=chat_id,
                status=status,
                color=color,
                email=email,
                approved_by=approved_by,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            print(f"[tg bot] ⚠️ Telegram join-request DB 更新失敗，改用本機檔案：{exc}")

    from agent_core.state_io import locked_json

    now_iso = datetime.now(timezone.utc).isoformat()
    path = _telegram_join_requests_file()
    with locked_json(path, default={}) as data:
        requests = data.setdefault("requests", {})
        if not isinstance(requests, dict):
            requests = {}
            data["requests"] = requests
        record = requests.get(chat_id)
        if not isinstance(record, dict):
            record = {"chat_id": chat_id}
        record.update({
            "status": status,
            "resolved_at": now_iso,
            "resolved_by": approved_by,
        })
        if color:
            record["approved_color"] = color
        if email:
            record["employee_email"] = email
        requests[chat_id] = record
    return record


def _telegram_approve_join_request(
    *,
    chat_id: str,
    color: str,
    name: str = "",
    approved_by: str = "",
) -> str:
    if not chat_id:
        chat_id = _telegram_latest_pending_join_chat_id()
    if not chat_id:
        return (
            "沒有可核准的待審 Telegram 申請。\n"
            "用法：/approve_tg 或 /approve_tg <chat_id> <部門顏色> <姓名>"
        )
    color = color or _telegram_default_join_color()
    color, color_err = _telegram_validate_join_color(color)
    if color_err:
        return f"❌ {color_err}"

    pending = {str(item.get("chat_id") or ""): item for item in _telegram_pending_join_requests()}
    record = pending.get(chat_id, {})
    display_name = (name or str(record.get("name") or "") or f"Telegram {chat_id}").strip()
    email = _telegram_save_private_approval(
        chat_id=chat_id,
        color=color,
        name=display_name,
        telegram_user_id=str(record.get("telegram_user_id") or ""),
        username=str(record.get("username") or ""),
        approved_by=approved_by,
    )

    _telegram_update_join_status(
        chat_id,
        status="approved",
        color=color,
        email=email,
        approved_by=approved_by,
    )
    return (
        f"✅ 已核准 Telegram 員工加入\n"
        f"- 姓名: {display_name}\n"
        f"- 部門: {color}\n"
        f"- chat.id: {chat_id}\n"
        f"- bot: {_telegram_agent_namespace()}\n"
        f"- approval id: {email}\n\n"
        "他下一則訊息就會用這個 Agent 的核准身份進入小紅。"
    )


def _telegram_reject_join_request(chat_id: str, *, rejected_by: str = "") -> str:
    if not chat_id:
        chat_id = _telegram_latest_pending_join_chat_id()
    if not chat_id:
        return "目前沒有可拒絕的待審 Telegram 申請。"
    _telegram_update_join_status(chat_id, status="rejected", approved_by=rejected_by)
    return f"✅ 已拒絕 Telegram 加入申請：{chat_id}"


def _telegram_parse_join_callback_data(data: str) -> tuple[str, str, str]:
    prefix, sep, rest = str(data or "").partition(":")
    if prefix != _TG_JOIN_CALLBACK_PREFIX or not sep:
        return "", "", ""
    action_key, sep, rest = rest.partition(":")
    if not sep:
        return "", "", ""
    namespace, sep, chat_id = rest.partition(":")
    if not sep:
        return "", "", ""
    action = {"a": "approve", "r": "reject"}.get(action_key, "")
    if not action:
        return "", "", ""
    return action, namespace, chat_id.strip()


def _telegram_handle_join_callback(
    data: str,
    *,
    chat_id: str = "",
    telegram_actor: Mapping[str, Any] | None = None,
) -> tuple[str, str, str]:
    action, namespace, target_chat_id = _telegram_parse_join_callback_data(data)
    if not action:
        return "這個審核按鈕已失效，請讓同事再傳一則訊息重新送審。", "", ""
    if not _telegram_actor_is_owner(telegram_actor):
        return "🔒 只有 Red owner Telegram 可以核准或拒絕員工加入。", target_chat_id, action
    current_namespace = _telegram_agent_namespace()
    if namespace != current_namespace:
        return (
            f"這個審核按鈕屬於 {namespace} bot，不是目前的 {current_namespace} bot。"
            "請在對應 Agent 的 Telegram 裡審核。"
        ), target_chat_id, action

    approver = str(chat_id or (telegram_actor or {}).get("chat_id") or "").strip()
    if action == "reject":
        return (
            _telegram_reject_join_request(target_chat_id, rejected_by=approver),
            target_chat_id,
            action,
        )
    return (
        _telegram_approve_join_request(
            chat_id=target_chat_id,
            color=_telegram_default_join_color(),
            approved_by=approver,
        ),
        target_chat_id,
        action,
    )


def _telegram_join_callback_ack(reply: str, action: str) -> str:
    text = str(reply or "")
    if text.startswith("✅ 已核准"):
        return "已同意。"
    if text.startswith("✅ 已拒絕"):
        return "已不同意。"
    if text.startswith("🔒"):
        return "你沒有審核權限。"
    if text.startswith("❌"):
        return "審核失敗。"
    return "已處理。"


def _telegram_join_applicant_resolution_notice(action: str, reply: str) -> str:
    if not str(reply or "").startswith("✅"):
        return ""
    agent_label = _telegram_join_agent_label()
    if action == "approve":
        return (
            f"管理員已同意你加入 {agent_label}。\n"
            "你現在可以直接傳訊息給我。"
        )
    if action == "reject":
        return (
            f"管理員目前沒有同意你加入 {agent_label}。\n"
            "你暫時還不能使用這個 Agent。"
        )
    return ""


def _telegram_handle_join_command(
    text: str,
    *,
    chat_id: str = "",
    telegram_actor: Mapping[str, Any] | None = None,
) -> str | None:
    command_text = _normalize_telegram_command_text(text, bot_username=_telegram_bot_username())
    head, _sep, rest = command_text.strip().partition(" ")
    head = head.lower()
    if head not in _TG_JOIN_COMMANDS:
        return None
    if not _telegram_actor_is_owner(telegram_actor):
        return "🔒 只有 Red owner Telegram 可以核准或拒絕員工加入。"
    approver = str(chat_id or (telegram_actor or {}).get("chat_id") or "").strip()
    if head in _TG_JOIN_PENDING_COMMANDS:
        return _telegram_join_pending_summary()
    if head in _TG_JOIN_REJECT_COMMANDS:
        target = rest.strip().split(maxsplit=1)[0] if rest.strip() else ""
        return _telegram_reject_join_request(target, rejected_by=approver)
    target, color, name = _telegram_parse_join_target_and_color(rest)
    return _telegram_approve_join_request(
        chat_id=target,
        color=color,
        name=name,
        approved_by=approver,
    )


def _telegram_default_actor_log_summary() -> str:
    if not _telegram_default_private_actor_enabled():
        return ""
    color = _telegram_default_actor_color()
    if not color:
        return "default_private=invalid"
    if _telegram_default_private_requires_owner_approval():
        return f"default_private={color}:owner_approval"
    return f"default_private={color}"


def _telegram_state_key_offset() -> str:
    suffix = os.environ.get("RED_TELEGRAM_STATE_SUFFIX", "").strip()
    if not suffix:
        token_secret_name = (
            os.environ.get("RED_TELEGRAM_BOT_TOKEN_SECRET_NAME")
            or "telegram-bot-token"
        ).strip()
        if token_secret_name and token_secret_name != "telegram-bot-token":
            suffix = token_secret_name
    if not suffix:
        return _TG_STATE_KEY_OFFSET
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", suffix).strip("-_.")
    if not safe_suffix:
        return _TG_STATE_KEY_OFFSET
    return f"{_TG_STATE_KEY_OFFSET}:{safe_suffix}"


def tg_send(
    token: str,
    chat_id: str,
    text: str,
    *,
    requests_module=_requests,
    heartbeat_touch: Callable[[], None] | None = None,
    reply_markup: Mapping[str, Any] | None = None,
) -> bool:
    """Send a potentially long reply to Telegram in chunks.

    短回覆若含 ``` code fence，用 HTML <pre> 等寬渲染 —— Telegram 不渲染 Markdown
    表格、預設也不把任何文字當等寬字，<pre> 是讓欄位（庫存表/生產日報）對齊的唯一辦法。
    跨段會切壞 <pre>，故僅「單段」時啟用；HTML 解析被拒（entities 錯）會自動退回純文字
    重送，所以對含 fence 的訊息頂多回到現況、對不含 fence 的訊息＝零行為改變。
    """
    limit = 4096
    raw = text or ""
    # 分段走共用 telegram_text_chunks：以 UTF-16 code unit 計長（Telegram 的
    # 4096 算法；emoji 佔 2），多段時每段預留 [i/n]\n 前綴空間 —— 之前用
    # code point 切滿 4096，加前綴就爆上限、該段被 Telegram 400 打回丟失。
    parts = telegram_text_chunks(raw, limit=limit) or ["(空回覆)"]
    html_text: str | None = None
    html_pm: str | None = None
    if "```" in raw and len(parts) == 1:
        rendered, pm = markdown_to_telegram_html(raw)
        if pm:
            html_text, html_pm = rendered, pm
    for index, part in enumerate(parts):
        body = part if len(parts) == 1 else f"[{index + 1}/{len(parts)}]\n{part}"
        use_html = bool(html_pm) and len(parts) == 1
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html_text if use_html else body,
            "disable_web_page_preview": True,
        }
        if use_html:
            payload["parse_mode"] = html_pm
        if reply_markup and index == len(parts) - 1:
            payload["reply_markup"] = dict(reply_markup)
        delivered = False
        for attempt in range(3):
            try:
                if heartbeat_touch:
                    heartbeat_touch()
                session = _get_tg_session(requests_module)
                response = session.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json=payload,
                    timeout=(15, 30),
                )
                # NOTE: keep the request `payload` intact across retries — do
                # NOT overwrite it with the parsed response, or a 429/5xx retry
                # would re-POST the response body (no chat_id/text) and fail.
                try:
                    resp_json = response.json()
                except Exception:
                    resp_json = None
                if response.status_code == 429:
                    try:
                        wait = int((resp_json or {}).get("parameters", {}).get("retry_after", 2 ** attempt))
                    except Exception:
                        wait = 2 ** attempt
                    wait = min(wait, 30)
                    print(f"[tg] 429 rate limit，等 {wait}s 後重試…")
                    if heartbeat_touch:
                        heartbeat_touch()
                    time.sleep(wait)
                    continue
                if response.status_code >= 500:
                    wait = 2 ** attempt
                    print(f"[tg] {response.status_code} 伺服器錯誤，{wait}s 後重試…")
                    if heartbeat_touch:
                        heartbeat_touch()
                    time.sleep(wait)
                    continue
                if 200 <= response.status_code < 300 and (
                    resp_json is None or resp_json.get("ok", True)
                ):
                    delivered = True
                    break
                desc = resp_json.get("description", "") if isinstance(resp_json, dict) else ""
                tail = f" | {desc}" if desc else ""
                print(
                    f"[tg] sendMessage 失敗：HTTP {response.status_code}{tail} "
                    f"{response.text[:300]}"
                )
                break
            except Exception as exc:
                # exc 內含 https://api.telegram.org/bot<TOKEN>/... 的 URL；print 直上
                # launchd stdout（不過 redact filter），故先手動 redact bot token。
                _e = redact_log_line(str(exc))
                if attempt < 2:
                    wait = 2 ** attempt
                    print(f"[tg] 送訊息失敗（{_e}），{wait}s 後重試…")
                    if heartbeat_touch:
                        heartbeat_touch()
                    time.sleep(wait)
                else:
                    print(f"[tg] 送訊息最終失敗：{_e}")
        if not delivered and use_html:
            # HTML 渲染送失敗（多半是 entities 解析錯）→ 退回純文字重送一次，
            # 確保「對齊」這件加分功能永遠不會反而把訊息弄丟。
            print("[tg] HTML 渲染送出失敗，退回純文字重送")
            plain_payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": body,
                "disable_web_page_preview": True,
            }
            if reply_markup and index == len(parts) - 1:
                plain_payload["reply_markup"] = dict(reply_markup)
            try:
                if heartbeat_touch:
                    heartbeat_touch()
                session = _get_tg_session(requests_module)
                r2 = session.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json=plain_payload,
                    timeout=(15, 30),
                )
                if 200 <= r2.status_code < 300:
                    delivered = True
            except Exception as exc:
                print(f"[tg] 純文字退回也失敗：{redact_log_line(str(exc))}")
        if not delivered:
            return False
    return True


def tg_answer_callback_query(
    token: str,
    callback_query_id: str,
    text: str = "",
    *,
    requests_module=_requests,
    heartbeat_touch: Callable[[], None] | None = None,
) -> bool:
    if not callback_query_id:
        return False
    payload = {
        "callback_query_id": callback_query_id,
        "text": str(text or "")[:180],
        "show_alert": False,
    }
    try:
        if heartbeat_touch:
            heartbeat_touch()
        session = _get_tg_session(requests_module)
        response = session.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json=payload,
            timeout=(15, 30),
        )
        try:
            data = response.json()
        except Exception:
            data = None
        return 200 <= response.status_code < 300 and (
            data is None or data.get("ok", True)
        )
    except Exception as exc:
        print(f"[tg] answerCallbackQuery 失敗：{redact_log_line(str(exc))}")
        return False


def tg_clear_inline_keyboard(
    token: str,
    chat_id: str,
    message_id: str | int,
    *,
    requests_module=_requests,
    heartbeat_touch: Callable[[], None] | None = None,
) -> bool:
    if not chat_id or not message_id:
        return False
    try:
        if heartbeat_touch:
            heartbeat_touch()
        session = _get_tg_session(requests_module)
        response = session.post(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=(15, 30),
        )
        try:
            data = response.json()
        except Exception:
            data = None
        return 200 <= response.status_code < 300 and (
            data is None or data.get("ok", True)
        )
    except Exception as exc:
        print(f"[tg] editMessageReplyMarkup 失敗：{redact_log_line(str(exc))}")
        return False


def tg_build_chat(
    *,
    agent_persona: str,
    tools_list: list[Any],
    gemini_model: str,
    agent_client_factory: Callable[[], Any],
    agent_types_factory: Callable[[], Any],
    chat_id_getter: Callable[[], str] | None = None,
    intent_filter: str = "",
    work_mode: str = "",
    history: list[dict[str, Any]] | None = None,
    telegram_actor: Mapping[str, Any] | None = None,
):
    """Create a fresh Telegram chat session.

    V4: 若提供 chat_id_getter，會把 sensitive tool 包確認門。
    intent_filter（opt-in，由 caller 在 RED_INTENT_ROUTING=1 時帶入）：
      非空字串 → 用 intent_router.filter_tools_by_intent narrow tools_list
    work_mode（caller 自動讀 mode_manager.get_current_mode()）：
      meeting / sales / dev → narrow tools + append persona addendum
      normal / 空字串 → 不變

    intent + mode 兩個都設時 → 取**交集**（更嚴格 — meeting + write_email
    = 既在 meeting tool subset 又在 write_email bucket 的 tool）

    telegram_actor（多使用者區隔）：非 owner actor（員工 / 群組）→ 先把
    owner-only 工具（sensitive + 大王個人帳號唯讀）整顆移除，並在 system
    instruction 附上區隔規則。owner / None → 行為不變。
    """
    # ── 通道 context：讓工具端知道「這次呼叫來自 Telegram 對話」──────────
    # 交付面工具（generate_from_order / generate_product_concept …）靠它決定
    # 圖走 [[TG_PHOTO:]] 回覆附圖（送回當前對話）還是出站推送（REPL / 背景
    # daemon 維持推送；2026-09-02 green 聊天室 5004 案）。這裡是唯一產生者：
    # 本 daemon 的回覆（主迴圈與軟超時 late_notify）都走 tg_send_with_photos，
    # 標記一定有人消費。fail-open —— 包覆失敗只是交付面退回推送，工具照常。
    try:
        from agent_core.channel_context import (
            CHANNEL_TELEGRAM, wrap_tools_with_channel,
        )
        tools_list = wrap_tools_with_channel(tools_list, CHANNEL_TELEGRAM)
    except Exception as exc:
        print(f"[tg bot] ⚠️ 通道 context 包覆失敗（交付面退回出站推送）：{exc}")

    # ── 使用者區隔 narrow（最先做 — 安全縮減優先於任何便利縮減）──
    actor_addendum = ""
    from agent_core.telegram_actor_scope import (
        actor_label,
        actor_requires_separation,
        actor_system_addendum,
        filter_tools_for_non_owner,
    )
    if actor_requires_separation(telegram_actor):
        kept, removed = filter_tools_for_non_owner(tools_list)
        print(
            f"[tg bot] 🔐 非大王 actor「{actor_label(telegram_actor)}」→ "
            f"移除 {len(removed)} 個 owner-only 工具（剩 {len(kept)} 個）"
        )
        tools_list = kept
        _actor_color = str((telegram_actor or {}).get("color") or "").strip().lower()
        _colored_employee = bool(_actor_color) and _actor_color != "red"
        if _colored_employee:
            # colored 員工（RED_TG_EMPLOYEE_FREEFORM 放行才會走到這）：
            # ① per-color 白名單 ∩ SAFE tier（owner/非owner 過濾之上再縮）
            # ② 每顆工具包 AgentRequest(caller=<色>) —— RAG 資料層 ACL 生效，
            #    否則 rag_gateway 預設 RED 視野＝越權查全部 collection。
            try:
                from agent_core.dept_tool_scope import (
                    dept_scope_addendum,
                    filter_tools_for_color,
                    wrap_tools_with_agent_caller,
                )
                kept_c, removed_c = filter_tools_for_color(tools_list, _actor_color)
                tools_list = wrap_tools_with_agent_caller(kept_c, _actor_color)
                print(
                    f"[tg bot] 🎨 {_actor_color} 員工工具白名單 → "
                    f"移除 {len(removed_c)} 個（剩 {len(tools_list)} 個，RAG caller 已綁 {_actor_color}）"
                )
            except Exception as exc:
                # fail-closed：色過濾失敗就不給任何工具，不能退回全 kept。
                print(f"[tg bot] ⛔ per-color 工具白名單失敗（工具面清空）：{exc}")
                tools_list = []
            # ③ persona 精簡：桌面自動化 / shell / 瀏覽器 / 排程那幾段講的工具
            #    員工一顆都拿不到（②已經濾掉），卻是每一則訊息都在付的固定
            #    成本。2026-08-04 查帳：persona 19,680 字元 ≈ 同量 token。
            #    fail-open —— 精簡失敗就用完整 persona（只是比較貴，不掉守則）。
            try:
                from agent_core.persona import trim_persona_for_employee
                _slim = trim_persona_for_employee(agent_persona)
                if _slim and len(_slim) < len(agent_persona):
                    print(
                        f"[tg bot] ✂️ {_actor_color} 員工 persona 精簡 "
                        f"{len(agent_persona):,}→{len(_slim):,} 字元"
                        f"（-{100 - len(_slim) * 100 // len(agent_persona)}%）"
                    )
                    agent_persona = _slim
            except Exception as exc:
                print(f"[tg bot] ⚠️ persona 精簡失敗（用完整版）：{exc}")
        # actor-scoped Gmail/行事曆：以員工**自己的**公司信箱身分操作，補回
        # 被移除的（大王帳號）寄信/回信/排會議能力，但完全不碰大王帳號。
        # 只給 red GM 員工 —— colored 員工的 freeform 是唯讀查詢面，不給寄信。
        has_own_google_tools = False
        if not _colored_employee:
            try:
                from agent_core.actor_google_tools import (
                    actor_google_tools_available, build_actor_google_tools,
                )
                if actor_google_tools_available(telegram_actor):
                    actor_email = str((telegram_actor or {}).get("email") or "").strip()
                    actor_tools = build_actor_google_tools(actor_email)
                    if actor_tools:
                        tools_list = tools_list + actor_tools
                        has_own_google_tools = True
                        print(
                            f"[tg bot] 📧 加 {len(actor_tools)} 個 actor-scoped "
                            f"Gmail/行事曆工具（as {actor_email}，免確認）"
                        )
            except Exception as exc:
                print(f"[tg bot] ⚠️ actor-scoped Google 工具載入失敗（略過）：{exc}")
        actor_addendum = actor_system_addendum(
            telegram_actor, has_own_google_tools=has_own_google_tools
        )
        if _colored_employee:
            try:
                actor_addendum += dept_scope_addendum(_actor_color)
            except Exception:
                pass

    try:
        from agent_core.exchange_policy import filter_tools_for_exchange_mode
        filtered = filter_tools_for_exchange_mode(tools_list)
        if len(filtered) != len(tools_list):
            print(f"[tg bot] 🔁 exchange policy → narrowed {len(tools_list)} → {len(filtered)} tools")
        tools_list = filtered
    except Exception as exc:
        print(f"[tg bot] ⚠️ exchange policy tool filter 失敗（用原 tool list）：{exc}")

    # Mode narrow（先做 — 比 intent narrow 範圍更穩定，先窄到 mode 再窄到 intent）
    persona_addendum = ""
    if work_mode and work_mode != "normal":
        try:
            from agent_core.mode_policy import filter_tools_by_mode
            from agent_core.persona_profiles import persona_for
            narrowed = filter_tools_by_mode(tools_list, work_mode)
            print(f"[tg bot] 🎯 work_mode={work_mode} → "
                  f"narrowed {len(tools_list)} → {len(narrowed)} tools")
            tools_list = narrowed
            persona_addendum = persona_for(work_mode)
        except Exception as exc:
            print(f"[tg bot] ⚠️ work_mode 套用失敗（用全 tool list）：{exc}")

    # Intent narrow（再做交集，視 mode 結果為 base）
    if intent_filter:
        try:
            from agent_core.intent_router import filter_tools_by_intent
            narrowed = filter_tools_by_intent(tools_list, intent_filter)
            print(f"[tg bot] 🎯 intent={intent_filter} → "
                  f"narrowed {len(tools_list)} → {len(narrowed)} tools")
            tools_list = narrowed
        except Exception as exc:
            print(f"[tg bot] ⚠️ intent_filter 套用失敗（用全 tool list）：{exc}")

    # Tool RPC proxy must sit INSIDE the Telegram confirmation wrapper:
    # Gemini -> tg_auth wrapper -> RPC proxy -> fresh worker. Reversing that
    # order would bypass +確認 because the proxy would replace the auth wrapper.
    if os.environ.get("RED_TOOL_RPC_TELEGRAM", "1") != "0":
        try:
            from agent_core.tool_proxy import proxy_tools
            before = len(tools_list)
            tools_list = proxy_tools(
                tools_list,
                caller="telegram",
                worker_channel="daemon",
                mode=os.environ.get("RED_TOOL_RPC_PROXY_MODE", "diagnostics"),
                timeout_sec=_env_int("RED_TOOL_RPC_TOOL_TIMEOUT_S", 300, min_value=1, max_value=7200),
            )
            proxied = sum(1 for t in tools_list if getattr(t, "_tool_rpc_proxy", False))
            if proxied:
                print(f"[tg bot] 🧰 tool RPC proxy enabled: {proxied}/{before} tools")
        except Exception as exc:
            print(f"[tg bot] ⚠️ tool RPC proxy 套用失敗（用原 tool list）：{exc}")

    # Telegram should never run long rebuild/batch jobs inline inside the
    # Gemini chat turn. Keep the public tool name and confirmation tier, but
    # enqueue the actual work so the 180s chat wait cannot interrupt it.
    tools_list = _queue_telegram_long_running_tools(tools_list)

    # V4: 包確認門。沒提供 chat_id_getter（測試 / 舊呼叫者）就退回原 list。
    if chat_id_getter is not None:
        from agent_core.tg_auth import filter_tools_for_telegram
        effective_tools = filter_tools_for_telegram(tools_list, get_chat_id=chat_id_getter)
    else:
        effective_tools = tools_list
    # 任務 deadline 閘門 — 一定要在最外層（先於 tg_auth 確認門），被放棄的
    # 殘餘線程連向使用者要 +確認 都不該發生。詳見 _check_task_deadline。
    effective_tools = _wrap_tools_with_deadline_gate(effective_tools)
    exchange_addendum = ""
    try:
        from agent_core.exchange_policy import exchange_system_instruction
        exchange_addendum = exchange_system_instruction()
    except Exception as exc:
        print(f"[tg bot] ⚠️ exchange policy instruction 載入失敗：{exc}")

    # behavior_policy 注入 — 以前只有 agent.py 的 REPL _build_chat 會呼叫
    # _compile_behavior_policies，Telegram 艦隊（10 色 daemon）完全吃不到大王
    # learn_behavior 教過的規則。這裡不額外做 TTL cache — tg_build_chat 本來
    # 就只在 chat session rebuild 時（閒置逾時 / 輪數上限 / daemon 重啟）才
    # 呼叫，已有天然的重新整理節奏。
    #
    # ⚠️ 非 owner（separated）actor 一律**不注入**，原因（Codex P2, PR #202）：
    # 非 red 員工在 tg_handle_message 就被擋在部門指令模式、到不了這裡；能到
    # 這裡的 separated actor 只有 color=red（GM office 員工），而
    # caller_scope="red" 在 _compile_behavior_policies 的語意是 owner 全視野
    # —— 傳下去會把 owner_only 規則外洩給非 owner。等部門 agent 路徑
    # （telegram_command / dept dispatch）要接規則注入時，再以
    # caller_scope=<該部門 color> 接進去（owner_only 天然被過濾）。
    behavior_addendum = ""
    if not actor_requires_separation(telegram_actor):
        try:
            from agent_core.memory import _compile_behavior_policies
            behavior_addendum = _compile_behavior_policies(caller_scope="red")
        except Exception as exc:
            print(f"[tg bot] ⚠️ behavior_policy 注入失敗（略過）：{exc}")

    cfg_kwargs = {
        "tools": effective_tools,
        "system_instruction": (agent_persona + _TG_BOT_SYSTEM_INSTRUCTION_APPEND
                               + exchange_addendum + persona_addendum
                               + actor_addendum + behavior_addendum),
    }
    try:
        afc_cfg = agent_types_factory().AutomaticFunctionCallingConfig(maximum_remote_calls=25)
        cfg_kwargs["automatic_function_calling"] = afc_cfg
    except Exception as exc:
        print(f"[tg bot] AFC config 設定失敗（用預設值）：{exc}")
    create_kwargs: dict[str, Any] = {
        "model": gemini_model,
        "config": agent_types_factory().GenerateContentConfig(**cfg_kwargs),
    }
    if history:
        # Best-effort: SDKs vary on whether history= accepts plain dicts vs
        # Content objects. We pass dicts and let the SDK coerce; if its
        # version rejects them, fall back to a fresh chat so the user at
        # least gets a working session.
        try:
            create_kwargs["history"] = history
            return agent_client_factory().chats.create(**create_kwargs)
        except Exception as exc:
            print(
                f"[tg bot] ⚠️ history 載入失敗（{type(exc).__name__}: {exc}），"
                "改用全新 chat 起步"
            )
            create_kwargs.pop("history", None)
    return agent_client_factory().chats.create(**create_kwargs)


def tg_handle_message(
    user_text: str,
    *,
    agent_persona: str,
    tools_list: list[Any],
    gemini_model: str,
    agent_client_factory: Callable[[], Any],
    agent_types_factory: Callable[[], Any],
    heartbeat_touch: Callable[[], None] | None = None,
    heartbeat_interval_s: float = 15.0,
    chat_state: dict[str, Any] | None = None,
    chat_id: str = "",
    telegram_actor: Mapping[str, Any] | None = None,
    telegram_message: Mapping[str, Any] | None = None,
    late_notify: Callable[[str], Any] | None = None,
) -> str:
    """Send one Telegram message into Gemini and return plain text.

    V4: 若 message 含確認 token（"+確認" / "/confirm" / 等），記下時戳；
        90 秒內 sensitive tool 才會真執行。

    late_notify：軟超時把對話讓出來之後，背景任務的後續（遲到的結果 /
        失敗 / 超過整體 deadline 放棄）由監看線程透過這個 callback 補送
        給使用者（polling loop 傳 tg_send 閉包）。None = 只寫 log。
    """
    # 多使用者區隔：state 以 chat_id 分艙（explicit chat_state 參數優先，
    # 留給 tests / 特殊呼叫者）。chat_id="" 落回全域 _tg_chat_state。
    chat_state = chat_state or _chat_state_for(str(chat_id) if chat_id else "")
    resp = None
    stop_pulse = None
    pulse_thread = None
    from agent_core.agents.permission_matrix import Agent
    actor_color = "red"
    actor_name = ""
    if isinstance(telegram_actor, Mapping):
        actor_color = str(telegram_actor.get("color") or "red").strip().lower()
        actor_name = str(telegram_actor.get("name") or telegram_actor.get("email") or "").strip()
    try:
        actor_agent = Agent(actor_color)
    except Exception:
        # Fail-closed（健檢 High）：actor color 無效不能 fallback 成 Agent.RED
        # —— RED 是 SUPER_ADMIN 查詢面，等於把設定打錯的員工升成管理員。
        # 拒絕處理、請管理員修 config；不猜任何身分。
        print(f"[tg bot] ⛔ actor color 無效（{actor_color!r}）— fail-closed 拒絕處理")
        return (
            "⚠️ 身分設定異常：無法辨識你的部門顏色設定，已拒絕處理這則訊息。\n"
            "請聯絡管理員檢查 Telegram 綁定（employee registry / approval 記錄）的 color 欄位。"
        )

    # 非 owner actor（員工 / 群組 / default-private）→ 套使用者區隔。
    from agent_core.telegram_actor_scope import (
        actor_label as _actor_label,
        actor_message_prefix as _actor_message_prefix,
        actor_requires_separation as _actor_requires_separation,
    )
    separated_actor = (
        telegram_actor if _actor_requires_separation(telegram_actor) else None
    )
    # colored 員工（freeform 路徑）：可選平價模型覆寫 + cost 歸戶標籤。
    # 在最上游改 gemini_model local，session build / chat_state / 503 fallback
    # 全程一致；chat_caller 讓 cost.jsonl 把員工開銷跟大王分開記。
    chat_caller = "telegram_chat"
    if separated_actor is not None and actor_color != "red":
        chat_caller = f"telegram_chat.{actor_color}"
        _employee_model = os.environ.get("RED_TG_EMPLOYEE_MODEL", "").strip()
        if _employee_model:
            gemini_model = _employee_model
    # actor 身分（色 / owner / email）變了就強制重建 session — 否則大王在
    # admin 後台改了某人的權限後，舊 session 的工具集還會再活 30 分鐘。
    actor_fp = ""
    if isinstance(telegram_actor, Mapping):
        actor_fp = "|".join((
            str(telegram_actor.get("color") or ""),
            str(telegram_actor.get("is_owner") or ""),
            str(telegram_actor.get("email") or ""),
        ))
    if chat_state.get("actor_fp", "") != actor_fp:
        if chat_state.get("chat") is not None:
            print(f"[tg bot] 🔁 actor 身分變更（{actor_fp or 'owner-legacy'}）→ force rebuild session")
        chat_state["chat"] = None
        chat_state["actor_fp"] = actor_fp

    # ── Owner session 主控台：登記本次對話 + 套用暫停/重置 gate ──
    # 每則 inbound 訊息都 touch 一次 registry（更新 last_seen / turn_count），
    # 讓大王能在任一 chat 用 list_sessions 綜覽並 pause/resume/reset 任一
    # session。owner 本人永不被暫停（雙保險：pause_session 也拒絕暫停 owner）。
    # 直呼 / 測試 / REPL bridge 傳 chat_id="" → 跳過（無 session 身分）。
    if chat_id:
        try:
            from agent_core import session_registry
            _sid = f"telegram:{chat_id}"
            _sess = session_registry.touch_session(
                _sid, channel="telegram", actor=telegram_actor,
            )
            if _sess.get("reset_pending"):
                # 與 /new 同步：只清 in-memory handle 不夠 — rebuild 會走
                # _load_tg_chat_history_for_rebuild 從磁碟重載舊 turns，reset
                # 形同無效。連 turns 歸零 + 清持久化歷史才是真重置。
                chat_state["chat"] = None
                chat_state["turns"] = 0
                _clear_tg_chat_history(str(chat_id))
                session_registry.consume_reset(_sid)
                print(f"[session] ♻️ chat {chat_id} 收到 owner reset — 已清空對話 session")
            _is_owner_actor = str(
                (telegram_actor or {}).get("is_owner") or ""
            ).strip().lower() == "true"
            if _sess.get("status") == "paused" and not _is_owner_actor:
                print(f"[session] ⏸️ chat {chat_id} 已被 owner 暫停 — 短路不進 LLM")
                return session_registry.paused_notice(_sess.get("paused_reason") or "")
        except Exception as exc:
            print(f"[session] ⚠️ registry 更新/gate 失敗（不影響回覆）: {exc}")

    # P2 rate-limit gate runs in the polling loop (task_telegram_bot)
    # BEFORE attachment download — see Codex P2 (PR #17) note there.
    # By the time tg_handle_message is invoked the message has already
    # passed the quota check, so we don't re-check here (would double-
    # count the same accept). Direct callers (tests, REPL bridge) pass
    # `chat_id=""` so per-chat tracking is irrelevant for them.

    # V4: 偵測確認訊息，記入 tg_auth state（before chat reset 等流程，免得早回）
    # C6: rate-limit 觸發時 mark_confirmed 回 False，提早把鎖定資訊回給 user。
    # +確認 窗口 scope：綁定群裡以 from.id 區隔（#32），私訊==chat_id。mark 與
    # check（wrap_sensitive_tool 的 get_chat_id thunk，見下方 session build）兩側
    # 都用同一把 scope，確保同一 confirm 事件對得起來。
    confirm_scope = _confirm_scope_for(telegram_message, chat_id)
    if chat_id:
        try:
            from agent_core.tg_auth import (
                message_grants_confirmation, mark_confirmed, is_locked_out,
                message_grants_dangerous_confirmation, mark_dangerous_confirmed,
            )
            if message_grants_confirmation(user_text):
                accepted = mark_confirmed(confirm_scope)
                if accepted:
                    print(f"[tg auth] ✅ chat {chat_id} 在 90s 確認窗內 — sensitive tool 可執行")
                else:
                    locked, remain = is_locked_out(confirm_scope)
                    if locked:
                        return (
                            f"🔒 此 chat 已被 rate-limit 鎖定（5 分鐘 5 次 +確認 上限超過）。\n"
                            f"   還有 {int(remain)} 秒解鎖。若是大王本人請等等再試。\n"
                            f"   若不是您本人發出 confirmation，建議立刻檢查 Telegram 帳號是否被盜。"
                        )
                    else:
                        print(f"[tg auth] ⚠️ chat {chat_id} 未通過 rate-limit（原因不明）")
            # DANGEROUS 二次確認 — 跟一般 +確認 互不衝突，可同訊息打兩個或分兩則
            # 注意：bare `cc` 短碼現在要求 chat_id 已有 active +確認 才算
            # （Codex P2 — 防 cc → c 順序意外把 sensitive 動作授權掉）。
            # 長碼 `+雙確認` / `EXEC` 不受此限。
            if message_grants_dangerous_confirmation(user_text, chat_id=confirm_scope):
                d_ok = mark_dangerous_confirmed(confirm_scope)
                if d_ok:
                    print(f"[tg auth] 🔴 chat {chat_id} 在 30s DANGEROUS 確認窗內")
                else:
                    print(f"[tg auth] ⚠️ chat {chat_id} DANGEROUS 確認被擋（lockout/格式）")
        except Exception as exc:
            print(f"[tg auth] ⚠️ 確認偵測失敗（不影響回覆）: {exc}")

    command_text = _normalize_telegram_command_text(
        user_text,
        bot_username=_telegram_bot_username(),
    )
    stripped = command_text.strip().lower()
    command_head = (stripped.split(maxsplit=1) or [""])[0]
    if command_head in {"/start", "/help", "/用法"}:
        return _telegram_start_reply(telegram_actor)
    if command_head in _TG_ID_COMMANDS:
        return _telegram_whoami_reply(
            chat_id=str(chat_id) if chat_id else "",
            telegram_actor=telegram_actor,
            telegram_message=telegram_message,
        )

    join_command_result = _telegram_handle_join_command(
        command_text,
        chat_id=str(chat_id) if chat_id else "",
        telegram_actor=telegram_actor,
    )
    if join_command_result is not None:
        return join_command_result

    if stripped in ("/new", "/reset", "/新對話", "新對話", "重置對話"):
        chat_state["chat"] = None
        chat_state["turns"] = 0
        # Also wipe persisted memory so the next session truly starts blank
        # (without this, the rebuild would re-load the history we just asked
        # to throw away).
        if chat_id:
            _clear_tg_chat_history(str(chat_id))
        return "✅ 已開新對話，前面的 context 已清空。"

    # /dev / /green — development department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_green_agent_command, handle_green_agent_command,
        )
        if is_green_agent_command(command_text):
            return handle_green_agent_command(
                command_text,
                chat_id=str(chat_id) if chat_id else "",
                caller=actor_agent,
                confirm_scope=confirm_scope,
            )
    except Exception as exc:
        print(f"[tg bot] ⚠️ /dev 指令處理失敗（fall back 一般流程）：{exc}")

    # /shipping / /blue — shipping department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_blue_shipping_command, handle_blue_shipping_command,
        )
        if is_blue_shipping_command(command_text):
            return handle_blue_shipping_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /shipping 指令處理失敗（fall back 一般流程）：{exc}")

    # /sales / /orange — sales department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_orange_sales_command, handle_orange_sales_command,
        )
        if is_orange_sales_command(command_text):
            return handle_orange_sales_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /sales 指令處理失敗（fall back 一般流程）：{exc}")

    # /purchase / /yellow — procurement department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_yellow_procurement_command, handle_yellow_procurement_command,
        )
        if is_yellow_procurement_command(command_text):
            return handle_yellow_procurement_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /purchase 指令處理失敗（fall back 一般流程）：{exc}")

    # /warehouse / /indigo — warehouse department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_indigo_warehouse_command, handle_indigo_warehouse_command,
        )
        if is_indigo_warehouse_command(command_text):
            return handle_indigo_warehouse_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /warehouse 指令處理失敗（fall back 一般流程）：{exc}")

    # /accounting / /purple — accounting department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_purple_accounting_command, handle_purple_accounting_command,
        )
        if is_purple_accounting_command(command_text):
            return handle_purple_accounting_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /accounting 指令處理失敗（fall back 一般流程）：{exc}")

    # /production / /gray — production management department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_gray_production_command, handle_gray_production_command,
        )
        if is_gray_production_command(command_text):
            return handle_gray_production_command(
                command_text,
                chat_id=str(chat_id) if chat_id else "",
                caller=actor_agent,
                confirm_scope=confirm_scope,
            )
    except Exception as exc:
        print(f"[tg bot] ⚠️ /production 指令處理失敗（fall back 一般流程）：{exc}")

    # /cashier / /black — cashier department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_black_cashier_command, handle_black_cashier_command,
        )
        if is_black_cashier_command(command_text):
            return handle_black_cashier_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /cashier 指令處理失敗（fall back 一般流程）：{exc}")

    # /legal / /white — legal SoT department agent shortcut.
    try:
        from agent_core.agents.telegram_command import (
            is_white_legal_command, handle_white_legal_command,
        )
        if is_white_legal_command(command_text):
            return handle_white_legal_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /legal 指令處理失敗（fall back 一般流程）：{exc}")

    # /dept — read-only agent query（Project Rainbow framework）
    try:
        from agent_core.agents.telegram_command import (
            is_dept_command, handle_dept_command,
        )
        if is_dept_command(command_text):
            return handle_dept_command(command_text, caller=actor_agent)
    except Exception as exc:
        print(f"[tg bot] ⚠️ /dept 指令處理失敗（fall back 一般流程）：{exc}")

    # /ingest — RAG index sync（Drive / Gmail → ChromaDB，write-allowed）
    try:
        from agent_core.agents.telegram_command import (
            is_ingest_command, handle_ingest_command,
        )
        if is_ingest_command(command_text):
            return handle_ingest_command(
                command_text,
                chat_id=str(chat_id) if chat_id else "",
                caller=actor_agent,
                confirm_scope=confirm_scope,
            )
    except Exception as exc:
        print(f"[tg bot] ⚠️ /ingest 指令處理失敗（fall back 一般流程）：{exc}")

    if actor_agent is not Agent.RED:
        # 員工自由對話（RED_TG_EMPLOYEE_FREEFORM，預設關）：開了該色且私訊 →
        # 走下方 separated-actor 的受限 Gemini 對話（per-color 工具白名單 +
        # RAG caller 綁定，比唯讀 NL 引擎更強）。群組不放行（session/history 以
        # chat_id 共艙，多名員工輪替會互相污染上下文）。
        _chat_type = ""
        _freeform_ok = False
        try:
            from agent_core.dept_tool_scope import employee_freeform_enabled_for
            _chat_type = str(
                ((telegram_message or {}).get("chat") or {}).get("type") or ""
            ).lower()
            _freeform_ok = (
                _chat_type == "private"
                and employee_freeform_enabled_for(actor_agent.value)
            )
        except Exception as exc:
            print(f"[tg bot] ⚠️ employee freeform 開關判斷失敗（維持關閉）：{exc}")
        if not _freeform_ok:
            label = actor_name or actor_agent.value
            # 私訊但該色沒開 freeform → 唯讀自然語言查詢引擎（dept_nlp_query：只開
            # query.* + ACL RAG，不進全工具 Gemini session），比舊的「先開放部門
            # 查詢指令」死路好。單一員工、無狀態、read-only，安全。
            # 群組 / 無法判定 chat 類型 → 保留 notice：群組多人不宜自動打 LLM、
            # 無 context 時 fail-closed。寫入/同步/寄信/檔案仍保留給 Red。
            if _chat_type == "private":
                from agent_core.dept_nlp_query import answer_dept_question
                try:
                    return answer_dept_question(
                        actor_agent.value,
                        user_text,
                        actor_name=label,
                        channel="telegram",
                    )
                except Exception as exc:
                    # 引擎內部已把錯誤收斂成訊息；最後保險，不讓員工訊息炸 bot。
                    print(f"[tg bot] ⚠️ 員工 NL 查詢失敗（{type(exc).__name__}: {exc}）")
                    return (
                        "⚠️ 查詢失敗，請稍後再試，或改用部門指令：\n"
                        f"  /dept {actor_agent.value} query.<name> [json_payload]"
                    )
            return (
                f"✅ {label} 已連到 {actor_agent.value} Telegram Agent 入口。\n"
                "目前員工 Telegram 先開放部門查詢指令，不走全工具 Gemini 對話：\n"
                f"  /dept {actor_agent.value} query.<name> [json_payload]\n"
                "也可查權限矩陣允許的其他部門 query.*。\n"
                "寫入、同步、寄信與檔案操作先保留給 Red 管理員。"
            )

    # 媒體下載 fastpath 不打 Gemini、直接執行下載工具（寫檔到大王的 Mac）。
    # 非 owner 一律跳過 — 走一般 agent 路徑，該路徑的工具集已套區隔閘門。
    if separated_actor is None:
        fastpath_reply = _try_direct_media_download_fastpath(
            user_text,
            chat_state=chat_state,
            chat_id=str(chat_id) if chat_id else "",
            confirm_scope=confirm_scope,
            heartbeat_touch=heartbeat_touch,
            heartbeat_interval_s=heartbeat_interval_s,
        )
        if fastpath_reply is not None:
            return fastpath_reply

    # ── Work mode 偵測（每則訊息檢查，mode 切換立刻生效）──
    # 不需 env flag — work mode 預設 normal，沒切換就等於沒效果
    current_mode = "normal"
    try:
        from agent_core.mode_manager import get_current_mode
        current_mode = get_current_mode()
        prev_mode = chat_state.get("work_mode", "")
        if prev_mode and prev_mode != current_mode:
            print(f"[tg bot] 🔁 work_mode {prev_mode} → {current_mode}，"
                  f"force rebuild session")
            chat_state["chat"] = None
        chat_state["work_mode"] = current_mode
    except Exception as exc:
        print(f"[tg bot] ⚠️ work_mode 讀取失敗（用 normal）：{exc}")
        current_mode = "normal"

    # ── Intent routing（opt-in via env RED_INTENT_ROUTING=1）──
    # 在 need_new 決策前跑：若 intent 比上一輪換了一個 bucket，強制 rebuild
    # session，這樣新訊息看到對應 narrowed tool catalog（避免「先寄信再查訂單」
    # 用著 email-only tool 找訂單找不到）。
    routed_intent = ""
    if os.environ.get("RED_INTENT_ROUTING") == "1":
        try:
            from agent_core.intent_router import classify
            # allow_llm=False — heuristic 路徑只，避免每則訊息打 Gemini Flash
            # 燒額度（heuristic 已涵蓋 ~70% 命中率）
            r = classify(user_text, allow_llm=False)
            # 只有 confidence ≥ 0.6 且非 chat / unknown 才做路由
            # 0.6 = 單一 0.9 權重 rule 命中（heuristic raw_score=0.9 → conf 0.64）
            if r.confidence >= 0.6 and r.intent not in ("unknown", "chat"):
                routed_intent = r.intent
                prev = chat_state.get("intent", "")
                if prev and prev != routed_intent:
                    print(f"[tg bot] 🔄 intent {prev} → {routed_intent}，"
                          f"force rebuild session")
                    chat_state["chat"] = None
                chat_state["intent"] = routed_intent
        except Exception as exc:
            print(f"[tg bot] ⚠️ intent routing 失敗（fall back 全 tool）：{exc}")
            routed_intent = ""

    try:
        now = time.time()
        idle = now - (chat_state["last_msg_ts"] or 0)
        need_new = (
            chat_state["chat"] is None
            or idle > _TG_SESSION_IDLE_SEC
            or chat_state["turns"] >= _TG_MAX_TURNS_BEFORE_REBUILD
        )
        if need_new:
            reason = (
                "啟動"
                if chat_state["chat"] is None
                else ("閒置 %.0f 分" % (idle / 60))
                if idle > _TG_SESSION_IDLE_SEC
                else f"已達 {chat_state['turns']} 輪上限"
            )
            # Load any persisted history so the new chat session picks up
            # where the previous one left off — survives daemon restart,
            # idle > 30 min, and 15-turn rebuild. Empty list = first chat
            # ever for this user, no memory yet.
            # 部門員工的歷史預算收一半（每輪成本跟大王一樣貴，但需要的上下文
            # 短得多）—— 2026-08-04 查帳：yellow 一天 $75.92/17 次呼叫，底盤就是
            # 這段餵回去的歷史。見 history._TG_CHAT_HISTORY_MAX_CHARS 註解。
            _history_budget = (
                _TG_CHAT_HISTORY_MAX_CHARS_EMPLOYEE
                if _reply_attachment_color(telegram_actor) else None
            )
            resumed_history = _load_tg_chat_history_for_rebuild(
                str(chat_id) if chat_id else "", max_chars=_history_budget)
            if resumed_history:
                print(
                    f"[tg bot] 🧠 載入過去對話記憶 {len(resumed_history) // 2} 輪"
                    f"（{reason}）",
                    flush=True,
                )
            else:
                print(f"[tg bot] 🆕 建新 chat session（{reason}）")
            # V4 thunk；#32：回 confirm_scope（綁定群含 from.id），讓
            # wrap_sensitive_tool 的 check_confirmed / revoke 與 mark 端用同一把 key。
            _captured_chat_id = confirm_scope

            def _get_chat_id() -> str:
                return _captured_chat_id

            chat_state["chat"] = tg_build_chat(
                agent_persona=agent_persona,
                tools_list=tools_list,
                gemini_model=gemini_model,
                agent_client_factory=agent_client_factory,
                agent_types_factory=agent_types_factory,
                chat_id_getter=_get_chat_id,
                intent_filter=routed_intent,  # 空字串 = 不 narrow
                work_mode=current_mode,       # normal = 不變
                history=resumed_history,
                telegram_actor=telegram_actor,
            )
            chat_state["gemini_model"] = gemini_model
            chat_state["turns"] = 0

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Defense-in-depth #7：偵測 injection 跡象但不阻擋（大王 chat_id 已
        # whitelist，這層只是 alert — 例如大王可能複製貼上被攻擊的 email 內
        # 文進 Telegram）。實際 sanitize 該在 fetch_email_by_thread_id 等
        # untrusted-content tool 入口做（已做）。
        # 注意：sanitize_untrusted_text 會 NFKC normalize（全形 → 半形），中
        # 文標點會被改 → 不能用 `sanitized != user_text` 判斷。要看 redact
        # token 是否真的出現。
        try:
            from agent_core.prompt_injection import sanitize_untrusted_text
            sanitized = sanitize_untrusted_text(user_text)
            if "[REDACTED-INJECTION-ATTEMPT]" in sanitized:
                print(f"[tg bot] ⚠️ user 輸入含 injection 跡象（仍照常處理）："
                      f"{user_text[:60]}...")
        except Exception:
            pass  # 偵測失敗不影響主流程
        # Detect explicit corrections so the LLM sees a hard "you are being
        # corrected, do not reflex-apologise" marker BEFORE it drafts a
        # reply. Pairs with 反翻供守則 in persona.py: persona is the soft
        # rule, this is the visible context inject that nudges every
        # relevant turn.
        correction_hint = ""
        try:
            from agent_core.correction_detector import detect_correction
            correction = detect_correction(user_text)
            if correction.is_correction:
                print(
                    f"[correction] 偵測到糾錯訊號："
                    f"{correction.matched_pattern} → "
                    f"「{correction.matched_text}」"
                )
                # hint 依身分換版型：員工版走「最高指導原則」措詞、不提
                # owner-only 工具與 remember_correction_rule。
                correction_hint = correction.hint(
                    is_owner=_telegram_actor_is_owner(telegram_actor)
                ) + "\n\n"
                # Persist as a mistake-ledger event so future turns on the
                # same topic surface the historical correction via
                # recent_factual_corrections(). We pull the prior model
                # reply from chat history (best-effort — empty if first
                # turn or history isn't there yet).
                # 只有大王本人的糾正才寫進共享 mistakes.json —— 這份 ledger 只給
                # owner 看（OWNER_PRIVATE_READ_TOOLS）且會逐字注入每日 reflection
                # prompt。非 owner（員工/群成員）若匹配糾錯 regex 就寫入，等於能污染
                # 大王的世界模型（correction_detector 亦註明「僅限大王本人的糾正」）。
                # correction_hint 只影響當下這輪，可保留給所有人；持久寫入必須 owner-only。
                if _telegram_actor_is_owner(telegram_actor):
                    try:
                        prior_reply = ""
                        if chat_id:
                            history = _load_tg_chat_history_entry(str(chat_id))
                            for turn in reversed(history.get("turns") or []):
                                if turn.get("role") == "model" and turn.get("text"):
                                    prior_reply = turn["text"]
                                    break
                        from agent_core.mistake_ledger import record_factual_correction
                        record_factual_correction(
                            user_correction=user_text,
                            prior_model_reply=prior_reply,
                            matched_pattern=correction.matched_pattern,
                        )
                    except Exception as exc:
                        print(f"[correction] ⚠️ ledger 寫入失敗（仍照常處理）：{exc}")
        except Exception as exc:
            print(f"[correction] ⚠️ 偵測器失敗（照常處理）：{exc}")
        # Detect pure fact-lookup questions and prepend an extractive-mode
        # instruction block — forces the LLM into "quote tool output
        # verbatim, never synthesise" discipline for material/spec/price
        # queries (the exact attack surface for hallucination).
        extractive_block = ""
        try:
            from agent_core.extractive_mode import extractive_addendum
            extractive_block = extractive_addendum(user_text)
            if extractive_block:
                print("[extractive] 偵測到純查詢類問題 → 啟用萃取模式")
                extractive_block = extractive_block + "\n\n"
        except Exception as exc:
            print(f"[extractive] ⚠️ 偵測器失敗（照常處理）：{exc}")
        sender_line = _actor_message_prefix(separated_actor) if separated_actor else ""
        wrapped = (
            f"[系統時間：{now_str}]\n"
            f"{sender_line}{extractive_block}{correction_hint}{user_text}"
        )
        stop_pulse = threading.Event() if heartbeat_touch else None
        if heartbeat_touch:
            heartbeat_touch()

            def _pulse():
                while not stop_pulse.wait(15):
                    heartbeat_touch()

            pulse_thread = threading.Thread(target=_pulse, daemon=True)
            pulse_thread.start()
        # Snapshot ~/Downloads BEFORE inference so we can detect any media
        # file the agent's tools land there during this turn — the post-agent
        # scan delivers those to Telegram and cleans up the local copy. Mirrors
        # what fast-path already does, but covers the Gemini-planned path too
        # (where the agent picks `download_online_video` from its tool list).
        downloads_before_inference = _snapshot_downloads_paths()
        agent_inference_start_ts = time.time()
        # Bound the inference call. Pulse thread keeps the watchdog alive
        # but a hung Gemini SDK call would otherwise leave the user waiting
        # forever. CRITICAL: the daemon thread is still alive after the
        # deadline — Python can't safely kill threads holding C extensions
        # like grpc — so the SDK call CAN complete in the background after
        # we return. The user-facing reply MUST NOT suggest blind retry
        # for that reason: a slow send_gmail / run_shell / browser_* tool
        # might fire twice if the user re-issues the same command (Codex
        # review on PR #29).
        soft_timeout_s = _tg_inference_soft_timeout_s()
        task_deadline_s = max(_tg_task_deadline_s(), soft_timeout_s)

        # 遲到結果送出前的收尾（Codex P2）：citation guard / 媒體補送 /
        # 歷史記錄 — 跟前景回覆同一套，閉包捕捉本輪的快照與識別。
        def _late_finalize(text: str) -> str:
            return _finalize_late_reply(
                text,
                chat_id=str(chat_id) if chat_id else "",
                user_text=user_text,
                downloads_before=downloads_before_inference,
                inference_start_ts=agent_inference_start_ts,
            )

        active_gemini_model = str(chat_state.get("gemini_model") or gemini_model)

        def _rebuild_chat_for_model(model_name: str) -> Any:
            resumed = _load_tg_chat_history_for_rebuild(str(chat_id) if chat_id else "")
            _captured_chat_id = confirm_scope  # #32：per-user +確認 scope（見上）

            def _get_chat_id() -> str:
                return _captured_chat_id

            chat = tg_build_chat(
                agent_persona=agent_persona,
                tools_list=tools_list,
                gemini_model=model_name,
                agent_client_factory=agent_client_factory,
                agent_types_factory=agent_types_factory,
                chat_id_getter=_get_chat_id,
                intent_filter=routed_intent,
                work_mode=current_mode,
                history=resumed,
                telegram_actor=telegram_actor,
            )
            chat_state["chat"] = chat
            chat_state["gemini_model"] = model_name
            chat_state["turns"] = 0
            return chat

        def _timeout_reply() -> str:
            if stop_pulse:
                stop_pulse.set()
            if pulse_thread:
                pulse_thread.join(timeout=1)
            if heartbeat_touch:
                heartbeat_touch()
            chat_state["chat"] = None
            chat_state["turns"] = 0
            return (
                f"⚠️ 小紅這次處理超過 {soft_timeout_s:.0f}s 仍未完成，先把對話讓出來。\n"
                "**背景動作可能仍在進行中**（例如寄信、寫檔、跑指令）——\n"
                f"  • 任務轉背景繼續跑：完成或失敗都會再回報一則；超過 "
                f"{task_deadline_s / 60:.0f} 分鐘整體上限就放棄並通知\n"
                "  • 若是會產生副作用的動作（send_gmail / run_shell / browser_*）\n"
                "    **先到對應地方確認**（Gmail Sent / 對應檔案 / browser tab）\n"
                "    再決定是否重發指令，避免重複執行造成大王困擾。"
            )

        try:
            resp = _send_message_with_timeout(
                chat_state["chat"],
                wrapped,
                timeout_s=soft_timeout_s,
                gemini_model=active_gemini_model,
                deadline_s=task_deadline_s,
                late_notify=late_notify,
                late_finalize=_late_finalize,
                caller=chat_caller,
            )
        except TimeoutError:
            return _timeout_reply()
        except Exception as exc:
            fallback_model = _gemini_chat_fallback_model(active_gemini_model)
            if not fallback_model or not _should_try_gemini_chat_fallback(exc):
                raise
            print(
                "[tg bot] ⚠️ Gemini 主模型失敗，切換備援模型 "
                f"{active_gemini_model} -> {fallback_model}: {type(exc).__name__}: "
                f"{str(exc)[:120]}",
                flush=True,
            )
            try:
                resp = _send_message_with_timeout(
                    _rebuild_chat_for_model(fallback_model),
                    wrapped,
                    timeout_s=soft_timeout_s,
                    gemini_model=fallback_model,
                    deadline_s=task_deadline_s,
                    late_notify=late_notify,
                    late_finalize=_late_finalize,
                    caller=chat_caller,
                )
            except TimeoutError:
                return _timeout_reply()
        if stop_pulse:
            stop_pulse.set()
        if pulse_thread:
            pulse_thread.join(timeout=1)
        if heartbeat_touch:
            heartbeat_touch()
        chat_state["turns"] += 1
        chat_state["last_msg_ts"] = now
        text = (resp.text or "").strip()
        # Hard guard against the persona's 事實準確守則 being ignored —
        # if the reply makes a customer-material/spec/price claim without
        # a [證據：…] marker, prepend a visible warning so the user sees
        # exactly what slipped past the citation rule. We log the trigger
        # to stdout so dashboard / red-smoke can surface trends later.
        if text:
            try:
                from agent_core.citation_guard import (
                    annotate_with_warning,
                    check_citation,
                )
                guard = check_citation(text)
                if not guard.ok:
                    print(
                        f"[citation_guard] flagged outgoing reply: "
                        f"{guard.reason}"
                    )
                    retry_text = ""
                    # Opt-in block-and-retry: when RED_CITATION_RETRY=1 the
                    # daemon goes one more LLM round to try to extract a
                    # proper citation before sending. Costs one extra
                    # inference per flagged turn, so default-off — the
                    # banner is enough for most cases.
                    retry_enabled = os.environ.get(
                        "RED_CITATION_RETRY", ""
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    if retry_enabled:
                        try:
                            print("[citation_guard] retry：請模型補上引用")
                            retry_prompt = (
                                "⚙️ 你剛剛的回答未通過 citation_guard 引用檢查"
                                f"（觸發：customers={guard.matched_customers}, "
                                f"facts={guard.matched_facts}）。\n"
                                "請直接重寫一次回答，遵守：\n"
                                "  1. 用 query_bom / query_email_lake 等結構化"
                                "工具拿到的具體事實重答\n"
                                "  2. 結尾必附 `[證據：<message_id 或 source>]`\n"
                                "  3. 如果工具查不到 → 明說「BOM 庫 / lake 裡查"
                                "不到 X」+ `[查不到直接證據]`\n"
                                "不要道歉、不要解釋，直接給新答案。"
                            )
                            # 健檢 Low：retry 是第二次 inference，但上方已停主 pulse、期間不
                            # touch heartbeat。503 風暴下 _send_message_with_timeout 多次重試
                            # 可累積 >300s、誤觸主迴圈 watchdog os._exit(99) 中斷回覆。包一條
                            # 臨時 pulse 保活（鏡像第一次 inference 的 pulse）。
                            _rt_stop = threading.Event() if heartbeat_touch else None
                            _rt_pulse = None
                            if heartbeat_touch:
                                heartbeat_touch()

                                def _retry_pulse() -> None:
                                    while not _rt_stop.wait(15):
                                        heartbeat_touch()

                                _rt_pulse = threading.Thread(target=_retry_pulse, daemon=True)
                                _rt_pulse.start()
                            try:
                                retry_resp = _send_message_with_timeout(
                                    chat_state["chat"],
                                    retry_prompt,
                                    gemini_model=str(
                                        chat_state.get("gemini_model")
                                        or active_gemini_model
                                    ),
                                )
                            finally:
                                if _rt_stop:
                                    _rt_stop.set()
                                if _rt_pulse:
                                    _rt_pulse.join(timeout=1)
                                if heartbeat_touch:
                                    heartbeat_touch()
                            chat_state["turns"] += 1
                            retry_text = (retry_resp.text or "").strip()
                            if retry_text:
                                retry_guard = check_citation(retry_text)
                                if retry_guard.ok:
                                    print(
                                        "[citation_guard] retry ✅ 通過，"
                                        "送 retry 版本"
                                    )
                                    text = retry_text
                                else:
                                    print(
                                        f"[citation_guard] retry 仍未通過"
                                        f"（{retry_guard.reason}），"
                                        f"送 retry+banner"
                                    )
                                    text = annotate_with_warning(
                                        retry_text, retry_guard
                                    )
                        except Exception as exc:
                            print(
                                f"[citation_guard] ⚠️ retry 失敗"
                                f"（送原回覆+banner）：{exc}"
                            )
                            retry_text = ""
                    if not retry_text:
                        text = annotate_with_warning(text, guard)
            except Exception as exc:
                print(f"[citation_guard] ⚠️ guard 自己失敗（送原回覆）：{exc}")
            # 完成度宣稱 × 這一輪的實際工具軌跡（2026-08-17 UserAng 案）。
            # 上面那顆比對的是「有沒有附引用」，抓不到「已將您上傳的 10 份規格單
            # 全部彙總」這種——它的問題不在有沒有引用，在那一輪根本沒開過任何檔。
            # 判準取 SDK 記的 AFC history（模型改不了）：
            #   attribute 是 list（可能空）→ 確知這輪呼叫了幾顆
            #   attribute 是 None         → 不知道（AFC history 被關掉），一律放行
            # 兩者絕不能混為一談：把「抽不到」當成「零呼叫」＝對每則回覆亂噴警告。
            try:
                from agent_core.citation_guard import (
                    annotate_with_completeness_warning,
                    check_completeness_claim,
                )
                from agent_core.report_trail import extract_tool_trail
                afc_history = getattr(
                    resp, "automatic_function_calling_history", None)
                turn_trail = (extract_tool_trail(resp)
                              if afc_history is not None else None)
                comp = check_completeness_claim(text, turn_trail)
                if not comp.ok:
                    print(f"[citation_guard] 🛑 完成度宣稱無工具軌跡：{comp.reason}")
                    text = annotate_with_completeness_warning(text, comp)
                elif turn_trail is None:
                    # 罕見（AFC history 被關）。留一行，免得這顆守門靜默失效沒人發現。
                    print("[citation_guard] ℹ️ 取不到 AFC 軌跡，完成度檢查略過")
            except Exception as exc:
                print(f"[citation_guard] ⚠️ 完成度檢查失敗（送原回覆）：{exc}")
        # 引用回饋（Phase 3）：回覆裡帶 id=<doc_id> / space=<...> 出處代表
        # 該文件真的被用上了——記進 ledger，未來檢索微幅加權。絕不影響回覆。
        try:
            from agent_core.citation_feedback import record_citations_from_reply
            record_citations_from_reply(text)
        except Exception as exc:
            print(f"[citation_feedback] ⚠️ 記錄失敗（不影響回覆）：{exc}")
        # Auto-deliver any media file the agent dropped in ~/Downloads. This
        # is the "wrap download to always send back via Telegram" behaviour
        # the user asked for — fast-path already does this for the obvious
        # URL+verb-in-one-message case, but the Gemini-planned path used to
        # leave files stranded locally.
        auto_delivery = ""
        if chat_id:
            new_files = _scan_new_media_downloads_since(
                agent_inference_start_ts, downloads_before_inference
            )
            if new_files:
                auto_delivery = _deliver_new_downloads_via_telegram(
                    new_files, str(chat_id)
                )
        # Persist this turn so the next chat-session rebuild (idle > 30 min,
        # turns >= 15, or daemon restart) can resume with the conversation
        # context loaded. Only record if we got a real text reply — empty
        # diagnostic messages would just bloat the persisted history.
        if chat_id and text:
            try:
                _record_tg_chat_turn(str(chat_id), user_text, text)
            except Exception as exc:
                print(f"[tg bot] ⚠️ chat 記憶寫入失敗（不影響回覆）：{exc}")
        if text:
            return text + auto_delivery
        diag = extract_empty_reason(resp)
        fallback = diag or (
            "（小紅沒產出文字回覆）可能是 Gemini 3 Flash Preview 卡住。\n"
            "大王可以換個說法再試，或提供更具體的指令（檔名、客戶、工具名）。"
        )
        return fallback + auto_delivery
    except Exception as exc:
        if stop_pulse:
            stop_pulse.set()
        if pulse_thread:
            pulse_thread.join(timeout=1)
        if heartbeat_touch:
            heartbeat_touch()
        chat_state["chat"] = None
        chat_state["turns"] = 0
        return f"⚠️ 小紅處理時出錯：{type(exc).__name__}: {exc}"
    finally:
        if stop_pulse:
            stop_pulse.set()
        if pulse_thread:
            pulse_thread.join(timeout=1)
        try:
            del resp
        except Exception:
            pass
        gc.collect()


def task_telegram_bot(
    *,
    agent_persona: str,
    tools_list: list[Any],
    gemini_model: str,
    agent_client_factory: Callable[[], Any],
    agent_types_factory: Callable[[], Any],
    load_state: Callable[[], dict[str, Any]],
    update_state: Callable[[Callable[[dict[str, Any]], None]], None],
    requests_module=_requests,
) -> None:
    """Long-poll Telegram and respond with the agent."""
    token, allow_chat_id = tg_get_token_and_chat()
    telegram_actors = _telegram_inbound_actors(allow_chat_id)
    authorized_chat_ids = set(telegram_actors)
    approval_owner_chat_id = _telegram_approval_owner_chat_id(allow_chat_id)
    default_private_actor = _telegram_default_actor_log_summary()
    default_private_actor_active = default_private_actor and default_private_actor != "default_private=invalid"
    if not token:
        print("[tg bot] 未設定 token，結束（請先跑 telegram_setup.py）")
        return
    if not authorized_chat_ids and not default_private_actor_active:
        print("[tg bot] 未設定授權 chat_id，結束（請先跑 telegram_setup.py）")
        return
    if default_private_actor == "default_private=invalid":
        print("[tg bot] RED_TELEGRAM_DEFAULT_ACTOR_COLOR 無效，default private actor 已停用")
        default_private_actor = ""

    # Telegram long-poll duration. Larger values reduce request volume but
    # increase worst-case message latency (a message arriving just after we
    # begin a long-poll won't be processed until the poll returns).
    tg_long_poll_timeout_s = _env_int(
        "RED_TG_LONGPOLL_TIMEOUT_S",
        8,
        min_value=1,
        max_value=30,
    )

    # Idempotent shutdown setup. The Event is module-global so it survives
    # any internal restart of this loop within a test run.
    _reset_shutdown_state()
    _install_sigterm_handler()

    def _rss_mb():
        """當前 RSS（MB）。

        ⚠️ 不能用 resource.ru_maxrss — 那是「歷史峰值」，單調遞增永不下降。
        2026-06-11 之前 log 印的就是它，造成「每輪 +220MB 且不回收」的假象
        （實際是 BM25 cache 重建的暫態峰值 + 常駐 cache，當前 RSS 低得多）。
        macOS 沒有 /proc，stdlib 拿不到當前 RSS，走 ps；失敗就退回峰值。
        """
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if out:
                return int(out) / 1024
        except (ValueError, TypeError):
            pass
        return _peak_rss_mb()

    def _peak_rss_mb():
        """進程生命週期 RSS 峰值（MB）— 抓暫態尖峰用，只升不降。"""
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        except Exception:
            return -1

    print(
        f"[tg bot] ▶️ 啟動，授權 chat_id={allow_chat_id}，"
        f"inbound={_telegram_actor_log_summary(allow_chat_id)}"
        f"{'，' + default_private_actor if default_private_actor else ''}，"
        f"approval_owner={'yes' if approval_owner_chat_id else 'no'}，"
        f"RSS={_rss_mb():.0f}MB，PID={os.getpid()}"
    )
    state_key_offset = _telegram_state_key_offset()
    offset = int(load_state().get(state_key_offset, 0))
    msg_counter = 0
    consecutive_net_errors = 0
    reload_snapshot = _code_reload_snapshot()
    last_reload_check = time.monotonic()
    bot_username = _telegram_bot_username()

    def _save_offset(new_offset: int):
        def mutate(state):
            state[state_key_offset] = new_offset

        update_state(mutate)

    heartbeat = [time.time()]
    # Watchdog: detect genuine main-loop stalls without masking logical hangs.
    # We keep the deadline generous to avoid false positives during slow
    # networks, and we explicitly "touch" heartbeat inside known long-running
    # operations (Gemini inference pulse thread + tg_send retry sleeps).
    tg_stall_deadline_sec = 300

    def _heartbeat_watchdog():
        while True:
            time.sleep(30)
            idle = time.time() - heartbeat[0]
            if idle > tg_stall_deadline_sec:
                print(
                    f"[tg bot] 💀 主迴圈卡死 {idle:.0f}s（>{tg_stall_deadline_sec}s），"
                    "os._exit 強制重啟",
                    flush=True,
                )
                os._exit(99)

    threading.Thread(target=_heartbeat_watchdog, daemon=True).start()

    # 任務心跳檔（給系統層看門狗看的）。key 由 RED_TELEGRAM_STATE_SUFFIX 推導
    # （紅 bot 為 "red"），與看門狗從 launchd label 推出的 key 對齊。_touch_heartbeat
    # 同時餵 in-memory 主迴圈 watchdog（heartbeat[0]）與檔案心跳（task_hb.pulse，
    # 受節流且只在 active 時寫）；begin/idle 由下方訊息處理段控制。
    task_hb = TelegramHeartbeat()
    # 啟動即把心跳檔覆寫成 idle（帶本 process 的 pid）：TelegramHeartbeat 建構
    # 不寫檔，第一次寫檔要等 begin()。若前一個 process 在 active 中途被殺，殘留
    # 的 active heartbeat 會讓看門狗每 15 分鐘誤殺剛重啟的健康 bot（直到下一則
    # 訊息才會覆寫）。
    task_hb.idle()

    def _touch_heartbeat():
        heartbeat[0] = time.time()
        task_hb.pulse()

    while True:
        # Check shutdown FIRST so a SIGTERM during the previous iter's
        # message handling exits before we long-poll again. Long-poll
        # blocks for up to 30s, which is too long to wait on shutdown.
        if _shutdown_requested.is_set():
            print(
                "[tg bot] 🛑 SIGTERM received — saving offset, closing session, exiting cleanly",
                flush=True,
            )
            _save_offset(offset)
            _reset_tg_session()
            return
        if (
            _TG_CODE_RELOAD_CHECK_INTERVAL_S > 0
            and time.monotonic() - last_reload_check >= _TG_CODE_RELOAD_CHECK_INTERVAL_S
        ):
            last_reload_check = time.monotonic()
            current_snapshot = _code_reload_snapshot()
            if current_snapshot != reload_snapshot:
                _save_offset(offset)
                _reset_tg_session()
                _exec_self_for_code_reload()
        heartbeat[0] = time.time()
        try:
            session = _get_tg_session(requests_module)
            # 旗標只圍住這一個呼叫：SIGTERM 打在長輪詢上就當場打斷，打在其他
            # 地方（處理訊息中）維持原本「做完這則再走」的行為。
            _in_long_poll[0] = True
            try:
                response = session.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={
                        "offset": offset + 1,
                        "timeout": tg_long_poll_timeout_s,
                        "allowed_updates": '["message","callback_query"]',
                    },
                    # Connect bumped 10→15 for slow TLS handshakes on flaky links;
                    # read stays comfortably above the long-poll duration.
                    timeout=(15, max(45, tg_long_poll_timeout_s + 15)),
                )
            except _ShutdownInterrupt:
                # 回迴圈頂端走那條唯一的關機路徑（存 offset、關 session、return），
                # 不要在這裡再複製一份。
                continue
            finally:
                _in_long_poll[0] = False
            heartbeat[0] = time.time()
            consecutive_net_errors = 0
            data = response.json()
            if not data.get("ok"):
                print(f"[tg bot] getUpdates 失敗：{data}")
                _sleep_unless_shutdown(5)
                continue

            for upd in data.get("result", []) or []:
                telegram_actors = _telegram_inbound_actors(allow_chat_id)
                authorized_chat_ids = set(telegram_actors)
                upd_id = upd.get("update_id", 0)
                offset = max(offset, upd_id)
                _save_offset(offset)

                callback_query = upd.get("callback_query") or {}
                if callback_query:
                    callback_id = str(callback_query.get("id") or "")
                    callback_from = callback_query.get("from") or {}
                    callback_from_id = str(callback_from.get("id") or "").strip()
                    callback_msg = callback_query.get("message") or {}
                    callback_chat = callback_msg.get("chat") or {}
                    callback_chat_id = str(callback_chat.get("id") or "").strip()
                    callback_message_id = callback_msg.get("message_id") or ""
                    callback_actor = (
                        telegram_actors.get(callback_from_id)
                        if callback_from_id else None
                    )
                    if callback_actor is None and not callback_from_id:
                        callback_actor = telegram_actors.get(callback_chat_id)
                    callback_data = str(callback_query.get("data") or "")
                    reply, target_chat_id, callback_action = _telegram_handle_join_callback(
                        callback_data,
                        chat_id=callback_from_id or callback_chat_id,
                        telegram_actor=callback_actor,
                    )
                    tg_answer_callback_query(
                        token,
                        callback_id,
                        _telegram_join_callback_ack(reply, callback_action),
                        requests_module=requests_module,
                        heartbeat_touch=_touch_heartbeat,
                    )
                    if _telegram_actor_is_owner(callback_actor) and callback_message_id:
                        tg_clear_inline_keyboard(
                            token,
                            callback_chat_id,
                            callback_message_id,
                            requests_module=requests_module,
                            heartbeat_touch=_touch_heartbeat,
                        )
                    if callback_chat_id:
                        tg_send(
                            token,
                            callback_chat_id,
                            reply,
                            requests_module=requests_module,
                            heartbeat_touch=_touch_heartbeat,
                        )
                    notice = _telegram_join_applicant_resolution_notice(
                        callback_action,
                        reply,
                    )
                    if target_chat_id and notice:
                        tg_send(
                            token,
                            target_chat_id,
                            notice,
                            requests_module=requests_module,
                            heartbeat_touch=_touch_heartbeat,
                        )
                    _telegram_audit_log(
                        event="telegram_join_callback",
                        status=_telegram_audit_reply_status(reply),
                        chat_id=callback_from_id or callback_chat_id,
                        text=callback_data,
                        reply=reply,
                        actor=callback_actor,
                        message=callback_query,
                        update_id=upd_id,
                    )
                    continue

                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                sender_id = str(chat.get("id", ""))
                text = (msg.get("text") or "").strip()

                if sender_id not in authorized_chat_ids:
                    telegram_actor = _telegram_default_actor_for_message(msg)
                    if not telegram_actor:
                        # 健檢 Medium：未授權路徑原本沒 rate limit（下方 L~3940 的閘只有
                        # 授權/有 actor 的 sender 會到）。先做廉價 per-sender 限流，否則洪水
                        # 可在單線程 loop 上逼出無限 flock'd 寫檔 + 1:1 回覆放大。超限即丟，
                        # 不寫 join-request、不回覆。
                        from agent_core.tg_auth import check_message_rate_limit
                        rl_ok, _rl_retry, rl_count = check_message_rate_limit(sender_id)
                        if not rl_ok:
                            print(f"[tg bot] 🚦 非授權 chat {sender_id} 洪水限流（{rl_count}/min），丟棄")
                            _telegram_audit_log(
                                event="telegram_update",
                                status="unauthorized_rate_limited",
                                chat_id=sender_id,
                                text=text,
                                message=msg,
                                reason=f"unauthorized flood {rl_count}/min",
                                update_id=upd_id,
                            )
                            continue
                        join_record = _telegram_record_join_request(
                            msg,
                            text=text,
                            update_id=upd_id,
                        )
                        # 回覆本身也節流：只在 join record 說該通知時送（與 owner-notify 同一個
                        # 300s 窗口）—— 否則每則未授權訊息都換來一則回覆＝攻擊者可控的反射放大。
                        should_notify_unbound = bool(join_record and join_record.get("should_notify"))
                        unbound_reply = (
                            _telegram_unbound_private_chat_reply(msg)
                            if should_notify_unbound else None
                        )
                        if unbound_reply:
                            tg_send(
                                token,
                                sender_id,
                                unbound_reply,
                                requests_module=requests_module,
                                heartbeat_touch=_touch_heartbeat,
                            )
                        if (
                            approval_owner_chat_id
                            and should_notify_unbound
                        ):
                            tg_send(
                                token,
                                approval_owner_chat_id,
                                _telegram_join_owner_prompt(join_record),
                                requests_module=requests_module,
                                heartbeat_touch=_touch_heartbeat,
                                reply_markup=_telegram_join_approval_reply_markup(join_record),
                            )
                        print(f"[tg bot] 🚫 非授權 chat_id ({sender_id}) 傳了訊息，忽略")
                        _telegram_audit_log(
                            event="telegram_update",
                            status="unauthorized",
                            chat_id=sender_id,
                            text=text,
                            reply=unbound_reply,
                            message=msg,
                            reason="chat_id_not_authorized",
                            update_id=upd_id,
                        )
                        continue
                else:
                    telegram_actor, _actor_reason = _resolve_inbound_actor(
                        chat, msg, telegram_actors
                    )
                    if telegram_actor is None:
                        # 綁定群裡的未註冊成員 —— 不授予部門身分，當未授權丟棄。
                        _from = str((msg.get("from") or {}).get("id") or "?")
                        print(
                            f"[tg bot] 🚫 群組 {sender_id} 未註冊成員 from={_from} 傳訊，"
                            f"不授予部門身分，忽略"
                        )
                        _telegram_audit_log(
                            event="telegram_update",
                            status="unauthorized_group_member",
                            chat_id=sender_id,
                            text=text,
                            message=msg,
                            reason=_actor_reason,
                            update_id=upd_id,
                        )
                        continue
                if _telegram_should_ignore_group_message(msg, bot_username=bot_username):
                    print(f"[tg bot] 🔇 群組 chat {sender_id} 普通訊息已忽略")
                    _telegram_audit_log(
                        event="telegram_update",
                        status="ignored_group_noise",
                        chat_id=sender_id,
                        text=text or (msg.get("caption") or ""),
                        actor=telegram_actor,
                        message=msg,
                        reason="group_message_without_command_or_mention",
                        update_id=upd_id,
                    )
                    continue
                text = _normalize_telegram_command_text(text, bot_username=bot_username)

                # Codex P2 (PR #17): rate-limit MUST run before attachment
                # download. Earlier the gate was inside tg_handle_message,
                # but the polling loop called _download_telegram_attachment
                # first — so an over-quota compromised chat could still
                # force Telegram getFile + ~20MB disk writes on every
                # flooded message before the rate-limit reply went out.
                # Move the gate up here so a rejected message skips ALL
                # network/disk side effects.
                try:
                    from agent_core.tg_auth import check_message_rate_limit
                    rl_allowed, rl_retry, rl_count = check_message_rate_limit(sender_id)
                    if not rl_allowed:
                        print(
                            f"[tg bot] 🚦 chat {sender_id} rate-limit 觸發 "
                            f"({rl_count} 條/min；下個 quota {int(rl_retry)}s 後)"
                        )
                        tg_send(
                            token, sender_id,
                            (
                                "🚦 訊息頻率過高，已暫時不處理。\n"
                                f"   每分鐘上限 20 條（你已發 {rl_count} 條）。\n"
                                f"   約 {int(rl_retry)} 秒後可再試。\n\n"
                                "   若不是您本人在連發訊息，建議立刻檢查 "
                                "Telegram 帳號是否被盜。"
                            ),
                            requests_module=requests_module,
                            heartbeat_touch=_touch_heartbeat,
                        )
                        _telegram_audit_log(
                            event="telegram_update",
                            status="rate_limited",
                            chat_id=sender_id,
                            text=text,
                            reply="訊息頻率過高，已暫時不處理。",
                            actor=telegram_actor,
                            message=msg,
                            reason=f"{rl_count} messages/min; retry={int(rl_retry)}s",
                            update_id=upd_id,
                        )
                        # No getFile, no download, no Gemini, no file write.
                        # Move on to next update.
                        continue
                except Exception as exc:
                    # Fail-open: rate-limit module breakage must not stop
                    # processing legitimate user messages. Log and continue.
                    print(f"[tg bot] ⚠️ rate-limit 檢查失敗（fall back 放行）：{exc}")

                # 處理檔案附件 — 之前 bug：text 為空就 continue，導致大王
                # 寄 .doc / 圖片 / 語音都被默默吃掉。改成：偵測附件 → 下載 →
                # 把路徑跟 caption 包進 user_text 給 LLM 看。
                attachment = _extract_telegram_attachment(msg)
                if attachment:
                    print(f"[tg bot] 📎 收到 {attachment['kind']}: "
                          f"{attachment['file_name']} ({attachment['size']} bytes)")
                    saved_path, dl_err = _download_telegram_attachment(
                        token, attachment, requests_module=requests_module,
                    )
                    if dl_err:
                        # 下載失敗 → 通知大王
                        tg_send(
                            token,
                            sender_id,
                            f"⚠️ 收到您寄的 {attachment['kind']} 「{attachment['file_name']}」"
                            f"但下載失敗：{dl_err}",
                            requests_module=requests_module,
                            heartbeat_touch=_touch_heartbeat,
                        )
                        continue
                    print(f"[tg bot] 💾 已存到 {saved_path}")
                    # 把附件資訊包進 user_text，讓 LLM 看得到「大王剛剛上傳了這檔」
                    caption = _normalize_telegram_command_text(
                        (msg.get("caption") or "").strip(),
                        bot_username=bot_username,
                    )
                    # 多使用者區隔：上傳者標籤照 actor 身分，別把員工傳的
                    # 檔案標成「大王上傳」誤導 LLM。
                    uploader = "大王"
                    try:
                        from agent_core.telegram_actor_scope import (
                            actor_label, actor_requires_separation,
                        )
                        if actor_requires_separation(telegram_actor):
                            uploader = actor_label(telegram_actor)
                    except Exception:
                        pass
                    attach_block = (
                        f"[{uploader}上傳檔案]\n"
                        f"  種類: {attachment['kind']}\n"
                        f"  檔名: {attachment['file_name']}\n"
                        f"  路徑: {saved_path}\n"
                        f"  類型: {attachment['mime_type']}\n"
                        f"  大小: {attachment['size']} bytes"
                    )
                    if caption:
                        text = f"{attach_block}\n\n{uploader}附訊息：{caption}"
                    elif text:
                        text = f"{attach_block}\n\n{text}"
                    else:
                        text = (
                            f"{attach_block}\n\n"
                            f"（{uploader}沒附文字 — 請判斷是否要：\n"
                            f"  - 用 read_file/pdf_extract_text/excel_read 讀內容\n"
                            f"  - 等{uploader}下個訊息給指示）"
                        )

                if not text:
                    continue

                rss_before = _rss_mb()
                # V9: redact 後再 print（避免 launchd log 撈到 user 講的 secret）
                from agent_core.log_redact import redact_log_line
                _safe_text = redact_log_line(text)
                print(f"[tg bot] 📥 收到：{_safe_text[:120]}  (RSS={rss_before:.0f}MB)")
                # 任務心跳：begin→active 涵蓋整段 tg_handle_message（含媒體
                # fast-path / Gemini 推理 / 工具迴圈，正是 2026-06-12 卡死處）。
                # idle 放 finally 確保任何例外路徑都復位，否則殘留 active 會在
                # 之後 idle 期間被看門狗誤判停滯而誤重啟。回覆 tg_send 不在 active
                # 窗口內（自帶超時、非 wedge 風險），刻意讓它在 idle 下送出。
                # 回覆附檔（[[TG_FILE:]]）的路徑白名單綁發話者部門色；大王回 ""
                # （大王的 export_report 走 telegram_send_file，不用標記機制）。
                reply_color = _reply_attachment_color(telegram_actor)
                task_hb.begin(task=f"msg#{msg_counter} len={len(text)}")
                try:
                    reply = tg_handle_message(
                        text,
                        agent_persona=agent_persona,
                        tools_list=tools_list,
                        gemini_model=gemini_model,
                        agent_client_factory=agent_client_factory,
                        agent_types_factory=agent_types_factory,
                        heartbeat_touch=_touch_heartbeat,
                        chat_id=sender_id,  # V4: 傳給 tg_auth 用
                        telegram_actor=telegram_actor,
                        telegram_message=msg,
                        # 軟超時轉背景後，監看線程用這個把遲到結果 / 失敗 /
                        # 放棄通知補送回原 chat。刻意不傳 heartbeat_touch —
                        # 監看線程不該替主迴圈續命。
                        late_notify=(
                            lambda message, _cid=sender_id, _color=reply_color:
                            tg_send_with_photos(
                                token, _cid, message,
                                requests_module=requests_module,
                                actor_color=_color,
                            )
                        ),
                    )
                except Exception as exc:
                    reply = f"⚠️ 出錯：{exc}"
                finally:
                    task_hb.idle()
                _telegram_audit_log(
                    event="telegram_update",
                    status=_telegram_audit_reply_status(reply),
                    chat_id=sender_id,
                    text=text,
                    reply=reply,
                    actor=telegram_actor,
                    message=msg,
                    update_id=upd_id,
                )
                rss_after = _rss_mb()
                print(
                    f"[tg bot] 📤 回覆（{len(reply)} 字）  "
                    f"(RSS={rss_after:.0f}MB, {rss_after - rss_before:+.0f}MB, "
                    f"峰值 {_peak_rss_mb():.0f}MB)"
                )
                if not tg_send_with_photos(
                    token,
                    sender_id,
                    reply,
                    requests_module=requests_module,
                    heartbeat_touch=_touch_heartbeat,
                    actor_color=reply_color,
                ):
                    print("[tg bot] ⚠️ Telegram 回覆未成功送達")

                msg_counter += 1
                if msg_counter >= _TG_RESTART_AFTER_MSGS:
                    print(
                        f"[tg bot] 🔄 已處理 {msg_counter} 則訊息，主動退出讓 launchd 重啟"
                        f"（防記憶體累積，RSS={_rss_mb():.0f}MB）",
                        flush=True,
                    )
                    # 必須**非零**退出：telegram plist 是 KeepAlive={Crashed:true,
                    # SuccessfulExit:false}，exit 0 不會被 respawn，bot 會斷線直到
                    # health_check 撿屍（最長 30 分鐘）。75 對齊 run_with_deadline
                    # 的「自主重啟」慣例。SystemExit 是 BaseException，不會被本迴圈
                    # 的 except Exception 吞掉，可一路傳到 process exit。
                    sys.exit(75)

        except requests_module.exceptions.ReadTimeout:
            heartbeat[0] = time.time()
            consecutive_net_errors = 0
            continue
        except (
            requests_module.exceptions.ConnectionError,
            requests_module.exceptions.RequestException,
            ConnectionError,
            OSError,
            requests_module.exceptions.Timeout,
        ) as exc:
            heartbeat[0] = time.time()
            consecutive_net_errors += 1
            if consecutive_net_errors <= 3:
                print(
                    f"[tg bot] 🌐 網路暫時問題（{type(exc).__name__}），5s 後重試 "
                    f"(連續 {consecutive_net_errors} 次)"
                )
            elif consecutive_net_errors == 4:
                print("[tg bot] 🌐 網路持續不穩（>3 次），之後每 10 次才 log 一條避免 log 爆量")
            elif consecutive_net_errors % 10 == 0:
                print(f"[tg bot] 🌐 連續 {consecutive_net_errors} 次網路失敗")
            # Cheap recovery before going full-restart at 20: a poisoned
            # connection pool inside the cached Session can only recover
            # by closing it. Process restart works too but costs ~5s of
            # agent_core re-import; this is ~50ms.
            if consecutive_net_errors == _TG_RESET_SESSION_AT_NET_ERRORS:
                print(
                    f"[tg bot] 🔁 連續 {consecutive_net_errors} 次網路錯誤，"
                    "重置 HTTP session（避免 connection pool 中毒）"
                )
                _reset_tg_session()
            if consecutive_net_errors >= _TG_RESTART_AFTER_NET_ERRORS:
                print(
                    f"[tg bot] 🔄 連續 {consecutive_net_errors} 次網路錯誤，主動退出讓 launchd 重啟",
                    flush=True,
                )
                # 非零退出（同上）：exit 0 在 SuccessfulExit:false 下不會 respawn。
                sys.exit(75)
            _sleep_unless_shutdown(min(5 + consecutive_net_errors, 60))
        except Exception as exc:
            heartbeat[0] = time.time()
            consecutive_net_errors += 1
            print(f"[tg bot] ❌ 主迴圈例外：{type(exc).__name__}: {exc}")
            if consecutive_net_errors >= _TG_RESTART_AFTER_NET_ERRORS:
                print(
                    f"[tg bot] 🔄 累計 {consecutive_net_errors} 次失敗，退出讓 launchd 重啟",
                    flush=True,
                )
                # 非零退出（同上）：exit 0 在 SuccessfulExit:false 下不會 respawn。
                sys.exit(75)
            _sleep_unless_shutdown(min(5 + consecutive_net_errors, 60))
