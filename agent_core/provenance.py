"""小紅自產內容的出處標記 — 單一定義來源（leaf module，只 import stdlib）。

排程報表 / 報價單 / QC 報告這些都是小紅算出來的**衍生品**，寄出去之後會被
ingest 路徑當成一般公司信吃回來。再吃回去等於讓衍生品跟原始資料同級，錯的
數字下一輪會被檢索到、被覆述。

出處訊號分兩層，這個模組兩層都管：
  1. 信頭 `X-RED-Generated` —— 寄件端寫入（gmail_ops.send_gmail 的
     generated_by 參數），ingest 端讀取。
  2. metadata 欄位 `generated_by_red` —— 進了向量庫 / 記憶庫之後的旗標。

⚠️ 缺欄位一律視為「不是自產」。存量資料（15 萬+ gmail chunk、既有記憶）都沒有
這個欄位，反過來預設會讓整個既有語料在檢索中靜默消失。

刻意做成只依賴 stdlib 的 leaf：reflection_intake 是夜跑熱路徑、gmail_sync 是
ingest 內圈，它們都不該為了一個布林判斷把 bs4 / chromadb 拉進來。同一個判斷
散在各模組各寫一份正是 repo 鐵則點名要避免的（見 CLAUDE.md 的 env_int 條）。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

# 寄件端寫入的信頭名稱。偵測一律只看「在不在」，不比對值——值是任務標籤，
# 只供稽核（例：dispatcher:生產日報 / quote:Q2026-001）。
RED_GENERATED_HEADER = "X-RED-Generated"
_HEADER_LOWER = RED_GENERATED_HEADER.lower()

# 向量庫 / 記憶庫 metadata 的旗標欄位名。
METADATA_FIELD = "generated_by_red"

# metadata 可能被 Chroma 存成字串，這些值都算「否」。
_FALSEY_STRINGS = frozenset({"", "0", "false", "no"})


def is_red_generated_meta(meta: Mapping[str, Any] | None) -> bool:
    """metadata 是否標記為小紅自產。缺欄位＝舊資料＝False（保守放行）。"""
    value = (meta or {}).get(METADATA_FIELD, False)
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY_STRINGS
    return bool(value)


def headers_have_red_generated(header_names: Iterable[Any]) -> bool:
    """扁平 header 名稱集合（dict 或 list）裡有沒有這個標記。

    Gmail 回傳的信頭大小寫不保證，比對前一律 lower()。
    """
    return any(str(name).lower() == _HEADER_LOWER for name in (header_names or ()))


def message_has_red_generated(message: Mapping[str, Any] | None) -> bool:
    """Gmail API message 形狀（payload.headers 是 [{name, value}]）的偵測。"""
    headers = ((message or {}).get("payload") or {}).get("headers") or ()
    return headers_have_red_generated(h.get("name", "") for h in headers)


def thread_is_red_generated(messages: list[Mapping[str, Any]] | None) -> bool:
    """整串**每一封**都帶標記才算純小紅產出。

    只要有一封沒帶（真人回覆或轉寄進來），這串就含真人內容 → 照常索引。
    寧可留下一份衍生報表，也不要把同事的回覆連帶藏掉：漏抓的代價（一份報表
    進 RAG）遠小於誤殺（真人討論從檢索結果消失，而且是靜默的）。
    """
    return bool(messages) and all(message_has_red_generated(m) for m in messages)
