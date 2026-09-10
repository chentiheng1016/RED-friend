"""共用的環境變數解析 helper（env_int / env_float / env_bool）。

鐵則：本模組只准 import os——它是零依賴葉模組，任何模組（含 daemon
入口、ingest、telegram shim）都能在頂層 import 而不可能引發循環匯入。
連 math 都不引（isfinite 用比較式代替），別在這裡加任何 import。

統一語意：
- 未設定、空字串、純空白 → 回傳 default（原樣，不 clamp——default 由
  呼叫端負責，一律視為合法值）
- 無法解析（非數字）→ 回傳 default
- float 的 nan / ±inf → 回傳 default
- 解析成功的值才套 clamp：min_value / max_value 為 None 表示該側無界
- bool：1/true/yes/on（不分大小寫）為 True，0/false/no/off 為 False，
  其他寫法回傳 default
"""

# future import 是編譯指示、不是模組依賴，不違反「只准 import os」——
# 沒有它，`int | None` 標註會讓系統 python3（3.9，bin/ 腳本 shebang 的底線）
# 在 def 時就 TypeError，連帶炸掉所有想 import 本模組的 stdlib-only 工具鏈
# （bin/inject-plist-env 經 chroma_backend 取 SHARED_SERVER_URL）。
from __future__ import annotations

import os


def env_int(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return bool(default)


def env_float(
    name: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    # math-free isfinite：nan 與 ±inf 的比較都是 False
    if not (float("-inf") < value < float("inf")):
        return float(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value
