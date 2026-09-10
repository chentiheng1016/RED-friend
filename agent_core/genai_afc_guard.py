"""讓 google-genai 的自動函式呼叫（AFC）擋得住「模型呼叫不存在的工具」。

踩過的雷（這支就是為了修它）
------------------------------
genai SDK 的 AFC 派發（``_extra_utils.get_function_response_parts``）裡有一行
``func = function_map[func_name]``，**它在 try/except 之外**（try 只包住「呼叫
工具」那段，不包「查表」）。所以只要模型吐出一個不在工具清單裡的 function call
——例如非大王的 Telegram actor 被拔掉 owner-only 工具（``run_shell`` 等）後模型
還是去叫它，或純粹幻覺一個工具名——這個 ``function_map[func_name]`` 就 ``KeyError``，
往上炸穿整個 ``chat.send_message`` / ``generate_content``，**整輪任務直接死**。

正式環境真的中過：``❌ 背景任務最後失敗（347s）：KeyError: 'run_shell'`` ——
一個跑了 5 分鐘的工廠資料查詢，只因模型伸手去拿它無權使用的工具就整碗端走。

修法
----
我們把 SDK 派發收到的 ``function_map`` 換成一個「查不到 key 就回傳明確錯誤 stub」
的 dict 子類（靠 ``dict.__missing__``），不再 ``KeyError``。SDK 接著在它**自己**的
try/except 裡呼叫這個 stub、stub 丟出友善的 ValueError、SDK 把它包成正常的
``{'error': ...}`` function-response Part —— 跟它本來處理「工具執行中拋例外」的路徑
一模一樣。模型於是收到錯誤訊息、可以改用別的工具或改口，**整輪不會死**。

所有真正的邏輯都還是交給 SDK；我們只改「key 不存在」這一個行為。
``install_afc_unknown_tool_guard`` 具冪等性，由 ``gemini_client._get_genai_module``
在 import genai 後呼叫一次，覆蓋每一條 Gemini 路徑（telegram daemon / REPL /
背景任務）。SDK 升級若把這個接縫搬走，回傳 False 讓測試大聲抓到。
"""
from __future__ import annotations

from typing import Any, Mapping


def _unknown_tool_stub_factory(name: str):
    """回傳一個收到任何參數都立刻拋明確錯誤的 stub（讓 SDK 包成 error Part）。"""

    def _unknown_tool(**_kwargs: Any) -> Any:
        raise ValueError(
            f"工具 '{name}' 不存在或此身分無權使用。"
            "請改用其他可用工具，或直接用文字說明你想做的事，"
            "不要再次嘗試呼叫這個工具。"
        )

    _unknown_tool.__name__ = name or "unknown_tool"
    return _unknown_tool


class _GuardedFunctionMap(dict):
    """查不到的工具名回傳「明確錯誤 stub」而非 ``KeyError`` 的 function_map。

    既有的 key 行為完全不變（照常回傳真正的 callable）；只有 missing key 才走
    ``__missing__``，把一次走鐘的工具呼叫變成「可復原的錯誤」而不是「炸掉整輪」。
    """

    def __missing__(self, key: str):
        return _unknown_tool_stub_factory(str(key))


def install_afc_unknown_tool_guard() -> bool:
    """把 genai AFC 派發包起來，讓未知工具名不再 ``KeyError`` 炸掉整輪。

    具冪等性。已安裝或安裝成功回傳 True；若 SDK 內部結構與預期不符（接縫被搬走）
    回傳 False，讓呼叫端 / 測試能察覺 SDK 升級。
    """
    try:
        from google.genai import _extra_utils
    except Exception:
        return False

    if getattr(_extra_utils, "_red_unknown_tool_guard", False):
        return True

    sync_fn = getattr(_extra_utils, "get_function_response_parts", None)
    async_fn = getattr(_extra_utils, "get_function_response_parts_async", None)
    if sync_fn is None:
        # SDK 接縫變了 — 不亂 patch，回 False 讓不變量測試抓到。
        return False

    def _guarded_sync(response: Any, function_map: Mapping[str, Any]):
        return sync_fn(response, _GuardedFunctionMap(function_map))

    _extra_utils.get_function_response_parts = _guarded_sync

    if async_fn is not None:

        async def _guarded_async(response: Any, function_map: Mapping[str, Any]):
            return await async_fn(response, _GuardedFunctionMap(function_map))

        _extra_utils.get_function_response_parts_async = _guarded_async

    _extra_utils._red_unknown_tool_guard = True
    return True
