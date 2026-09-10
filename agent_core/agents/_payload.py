"""部門 agent 共用的 payload 取值 helper。

9 個 color agent 的 ``handle_query`` 反覆出現同一組「多 key fallback + 型別轉換
+ 預設值」idiom（``counterparty``/``po_number``/``days_back``…），改一個 key
別名要動 9 個檔。這裡抽成單一來源。

helper 的語義刻意逐一對齊原本各 agent 的寫法，是**行為等價**的抽取，不是正規化：

* :func:`pstr` 對應 ``str(payload.get(k1) or payload.get(k2) or ... or "")``
  —— 第一個 *truthy* 值。
* :func:`pint` 對應 ``int(payload.get(k1, payload.get(k2, N)) or N)``
  —— 第一個 *存在* 的 key（不是第一個 truthy），值 falsy 再退 default。
"""
from __future__ import annotations

from typing import Any, Mapping


def pstr(payload: Mapping[str, Any], *keys: str, default: str = "") -> str:
    """回傳 ``keys`` 中第一個 truthy 值的 ``str()``，全部空/缺則回 ``default``。

    等價於 ``str(payload.get(k1) or payload.get(k2) or ... or default)``。
    """
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return default


def pint(payload: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    """回傳 ``keys`` 中第一個*存在* key 的值轉 ``int``；該值 falsy 則回 ``default``。

    等價於巢狀預設 idiom ``int(payload.get(k1, payload.get(k2, default)) or default)``：
    取「第一個存在的 key」（不是第一個 truthy），所以 ``k1`` 存在但為 0 會落到
    ``default`` 而不會往 ``k2`` 找。額外比原 idiom 多一層保險：值無法轉 int
    （如 LLM 塞了非數字字串）時回 ``default`` 而非拋例外。
    """
    value: Any = default
    for key in keys:
        if key in payload:
            value = payload[key]
            break
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default
