"""REPL state + helpers: user input + send-with-retry.

agent.py's `_main` wires together module-startup state from half the
codebase. The REPL is keyboard-driven (the voice / meeting subsystem was
removed); `main_loop` reads typed input, sends it to Gemini, prints the reply.
"""
from __future__ import annotations

import time
from datetime import datetime

from agent_core.chat_session import chat_state, maybe_compact
from agent_core.logging_and_paths import logger


def _get_user_input() -> str:
    try:
        line = input("\n💬 [輸入模式] 大王：").strip()
        if line:
            print(f"-> 大王：{line}")
        return line
    except (EOFError, KeyboardInterrupt):
        return "退出"


def print_startup_banner(status: dict):
    """Print the pre-REPL diagnostic banner.

    status keys:
      os: str (e.g. "Darwin" / "Windows" / "Linux")
      cross_platform: list[tuple[str, bool]]  (package_name, available)
      tools_count: int
      mistake_counts: tuple[int, int]  (corrections, log)
      log_file: str | None
    """
    print("========================================")
    print(f"🏆 Agent 啟動（{status['os']}）")
    cp_status = [f"{name} {'✅' if ok else '❌'}" for name, ok in status['cross_platform']]
    print(f"跨平台套件：{' | '.join(cp_status)}")
    print(f"支援：智慧信箱、行事曆、報價、樣品追蹤、螢幕分析，共 {status['tools_count']} 項工具")
    corr_count, log_count = status['mistake_counts']
    print(f"犯錯學習：{corr_count} 條糾正規則 / {log_count} 筆錯誤歷史")
    if status.get('log_file'):
        print(f"📜 今日 log：{status['log_file']}")
    print("========================================\n")


def main_loop(send_with_retry_fn):
    """REPL main loop: get input → send to Gemini → print response.

    send_with_retry_fn: agent.py's wrapper that injects build_chat_fn for
    post-send chat compaction (see agent._send_with_retry).
    """
    from agent_core.logging_and_paths import EXIT_WORDS
    from agent_core.window_mgmt import bring_to_front

    try:
        while True:
            user_input = _get_user_input()

            if not user_input:
                continue

            if any(word in user_input.lower() for word in EXIT_WORDS):
                print("小紅先告退，隨時叫我！")
                break

            bring_to_front()
            try:
                response = send_with_retry_fn(user_input, max_attempts=2)
                if response and response.text:
                    print(f"小紅：{response.text}")
                else:
                    print("好的大王，已幫您處理完畢！")
            except Exception as e:
                logger.error("系統發生錯誤：%s", e)
                print("報告大王，系統發生了一點錯誤，請再說一次。")
    except KeyboardInterrupt:
        print("\n")
        print("好的大王，小紅先退下了！")


def _send_with_retry(user_text: str, max_attempts: int = 2, build_chat_fn=None):
    """Send a message via chat_state['chat'] with retry + post-send compaction.

    build_chat_fn: zero-arg callable used by maybe_compact when turn count
    crosses threshold. Required in practice (main loop passes agent._build_chat);
    kept optional so tests can exercise the retry path without wiring DI.
    """
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    wrapped = f"[系統時間：{now_str}]\n{user_text}"
    last_err = None
    for attempt in range(max_attempts):
        try:
            t0 = time.time()
            print("[系統日誌] ⏳ Gemini 思考中...(已送出，等 API 回覆)")
            response = chat_state["chat"].send_message(wrapped)
            # Record cost for the interactive chat path — chat.send_message
            # bypasses _gemini_generate's accounting. Best-effort.
            try:
                from agent_core import cost_tracker
                cost_tracker.record_chat_response(response, caller="repl_chat")
            except Exception:
                pass
            elapsed = time.time() - t0
            if elapsed > 5:
                print(f"[系統日誌] ⏱️ Gemini 共花了 {elapsed:.1f} 秒（若經常超過 30 秒，可能是 API 壅塞）")
            chat_state["turns"] += 1
            if build_chat_fn is not None:
                maybe_compact(build_chat_fn)
            return response
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                logger.warning("API 呼叫失敗（%s），1 秒後重試...", e)
                time.sleep(1)
    raise last_err
