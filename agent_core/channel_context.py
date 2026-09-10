"""當前互動通道 context — 工具端判斷「這次呼叫來自哪個前台」。

為什麼要有這顆（2026-09-02 green 聊天室 5004 樣品單案）：generate_from_order
在 owner context 一律 telegram_send_photo(chat_id="") 出站推送，而那條路只認
keyring 預設 chat（紅 bot 主對話）—— 大王在 green agent 聊天室發起渲染，圖卻
從 small red 主對話冒出來。交付面工具需要知道「這次呼叫來自 Telegram 對話」
才能改走 [[TG_PHOTO:]] 回覆附圖（daemon 用當前 bot token + 當前 chat_id 把圖
送回發問的那個對話）；REPL / 背景 daemon / web 沒有會消費標記的回覆送出點，
必須維持推送 —— 所以預設空通道＝推送，行為不變。

契約：只有「回覆送出點會走 daemon_telegram.tg_send_with_photos（會抽
[[TG_PHOTO:]] / [[TG_FILE:]] 標記）」的前台才可以把通道標成 telegram。目前唯
一產生者是 daemon_telegram.tg_build_chat（10 色 bot 主迴圈與軟超時
late_notify 的回覆都消費標記）。在別的前台亂標 telegram ＝ 標記無人消費、圖
誰都收不到。

工具實際執行點在 genai AFC 的 worker thread —— contextvar 不會從主緒自動傳
播，所以比照 dept_tool_scope.wrap_tools_with_agent_caller：包在工具本體上
（執行當下才 set/reset），不管誰在哪條線程呼叫都保證生效。

葉模組：只 import 標準庫，skills / agent_core 任何模組都能安全 import。
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
from typing import Any, Callable, Iterable

CHANNEL_TELEGRAM = "telegram"

_CURRENT_CHANNEL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "red_current_channel", default="")


def current_channel() -> str:
    """目前互動通道；"" ＝ 未知（REPL / 背景 daemon / 測試）。"""
    return str(_CURRENT_CHANNEL.get() or "")


def reply_consumes_tg_markers() -> bool:
    """這輪的回覆送出點會不會消費 [[TG_PHOTO:]]/[[TG_FILE:]] 標記？

    交付面工具（sample_order / image_gen）拿它決定：圖跟著回覆走（標記，
    送回當前對話）還是 telegram_send_photo 出站推送（keyring 預設 chat）。
    """
    return current_channel() == CHANNEL_TELEGRAM


@contextlib.contextmanager
def channel_context(channel: str):
    token = _CURRENT_CHANNEL.set(str(channel or ""))
    try:
        yield
    finally:
        _CURRENT_CHANNEL.reset(token)


def wrap_tools_with_channel(
    tools: Iterable[Callable[..., Any]], channel: str
) -> list:
    """把每顆工具包進 channel context（執行當下才 set/reset）。

    functools.wraps 保 __name__/__doc__/__annotations__/__dict__（含
    _actor_scoped、background_safe 等 marker attrs），__signature__ 另外釘
    （genai 從 signature 建 function declaration，比照
    daemon_telegram._make_deadline_gated_tool）—— 後續 mode/intent/tg_auth
    各層 by-name 過濾與 by-attr 判斷都不受影響。
    """
    normalized = str(channel or "")
    wrapped: list = []
    for fn in tools:
        def _make(inner: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(inner)
            def in_channel(*args, **kwargs):
                with channel_context(normalized):
                    return inner(*args, **kwargs)

            try:
                in_channel.__signature__ = inspect.signature(inner)  # type: ignore[attr-defined]
            except (TypeError, ValueError):
                pass
            in_channel._channel_context = normalized  # type: ignore[attr-defined]
            return in_channel

        wrapped.append(_make(fn))
    return wrapped
