"""工具函式 annotations 正規化（leaf util，只 import 標準庫）。

把 `from __future__ import annotations` 模組的工具函式 annotations 解回真型別。

google-genai 的自動 function calling 派發參數時，會對 `inspect.signature(fn)`
拿到的 annotation 做 `isinstance(value, annotation)`（_extra_utils 的最後一道
檢查）。future annotations 模組裡 annotation 是字串 `'str'`，isinstance 直接炸
`TypeError: isinstance() arg 2 must be a type, a tuple of types, or a union`。
陰險之處：`FunctionDeclaration.from_callable` 吃字串沒問題 — schema 建得出來、
工具上得了場，**LLM 一帶參數呼叫就死**（2026-06-12 read_drive_file / 2026-06-15
actor send_gmail 都實際踩到）。

**任何「組裝一份要交給 Gemini 的工具清單」的地方都得過這支**：
  - 靜態 catalog（builtin / skill / MCP）→ tool_registry 組裝點
  - 動態逐 actor 建的工具 → actor_google_tools.build_actor_google_tools
新增其他動態工具來源時也要記得套，否則就會重演這個雷。抽成 leaf util（只
import 標準庫 typing / inspect）讓動態來源能共用同一支、不必反向 import 重量級
的 tool_registry。
"""

from __future__ import annotations

import inspect
import typing


def resolve_string_annotations(tools: list) -> list:
    """原地把每個 tool 的字串 annotation 解回真型別，並釘住 `__signature__`。

    兩段獨立修復（缺一不可）：

    1. `fn.__annotations__` dict — 用 `typing.get_type_hints` 把字串解成型別。

    2. `fn.__signature__` — google-genai 派發參數時走 `inspect.signature(fn)`，
       而 `inspect.signature` 預設會沿 `__wrapped__` 鏈鑽到**最內層**函式讀它的
       annotation。若工具在進到這裡之前**已經被包過一層**（@audited / dry-run /
       tool_proxy 等都會留下 `__wrapped__`），第 1 步只改到外層 wrapper 的 dict，
       inspect.signature 仍讀到內層原始函式的字串 → `isinstance() arg 2` 照炸。
       對策：算出解析後的 signature 釘在外層 `fn.__signature__` 上；`inspect`
       的 unwrap 一遇到帶 `__signature__` 的物件就停手、不再往內鑽。
       （2026-06-16：add_task 等 45 個「先包後解」的工具實際全中此雷；舊版只比對
       `__annotations__` dict 的不變量測試因此漏看 — dict 修好了、signature 沒。）

    為什麼第 2 步要獨立判斷而非沿用第 1 步的「dict 有字串」：兩者會分歧 —— 外層
    dict 可能已被前一輪 resolve 解乾淨（無字串），但 signature 仍鑽到內層字串。

    原地改：tg_auth / tool_proxy 等 wrapper 用 `functools.wraps` 複製時會把
    `__signature__`（存在 `__dict__`）一併帶走，故 Telegram 包裝後的版本也吃得到。
    回傳同一個 list（方便 chain）。
    """
    for fn in tools:
        # ── 1. 解 __annotations__ dict ──
        ann = getattr(fn, "__annotations__", None)
        hints = None
        if ann and any(isinstance(v, str) for v in ann.values()):
            try:
                hints = typing.get_type_hints(fn)
                fn.__annotations__ = hints
            except Exception:
                # 解不開（前向引用到不存在的名稱等）就留原樣 — 行為跟修復前一致，
                # 至少不要在組裝期把整個 registry 炸掉。
                hints = None

        # ── 2. 釘 __signature__（擋 inspect.signature unwrap 到內層字串）──
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        if not any(isinstance(p.annotation, str) for p in sig.parameters.values()):
            continue  # signature 已乾淨（多數工具走這條）
        if hints is None:
            try:
                hints = typing.get_type_hints(fn)
            except Exception:
                continue  # 解不開就留原樣，至少不炸組裝
        new_params = [
            p.replace(annotation=hints.get(p.name, p.annotation))
            for p in sig.parameters.values()
        ]
        try:
            fn.__signature__ = sig.replace(
                parameters=new_params,
                return_annotation=hints.get("return", sig.return_annotation),
            )
        except (TypeError, ValueError):
            pass
    return tools
