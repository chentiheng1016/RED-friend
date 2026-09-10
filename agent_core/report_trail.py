"""排程報表的資料來源足跡 — 讓報表上的數字可以往回查。

出處標記（agent_core.provenance）解決的是「別把自己的輸出當成新證據」；
這個模組解決的是另一半：**這份報表的數字到底哪來的**。

小紅的排程報表是 Gemini 帶著唯讀工具跑出來的自由文字。數字算錯時，光看報表
分不出是「工具撈到的資料本來就錯」還是「模型自己編的」—— 而這兩者的處理方式
完全不同。所以報表尾巴附上這一輪實際呼叫過的工具（名稱 + 參數），收到的人可以
自己去把同一顆工具再跑一次對答案。

刻意取自 SDK 的 automatic_function_calling_history，而不是叫模型「請附上出處」：
後者是模型自述，會漏、會編；前者是 SDK 記下來的實際呼叫，模型改不了。

最重要的一行其實是「沒有呼叫任何工具」那個警告：那代表整份內容純粹是模型輸出、
沒有碰過任何真實資料 —— 正是最該被懷疑的情況，但從報表外觀完全看不出來。
"""
from __future__ import annotations

import json
from typing import Any

from agent_core.env_utils import env_int
from agent_core.log_redact import redact_log_line

# 單一參數值在足跡裡的長度上限。工具參數可能塞進整段查詢字串或檔案 ID 清單，
# 原樣附進報表會把信塞爆；足跡的用途是「知道查了什麼、能重跑」，不是完整存證。
_MAX_ARG_CHARS = env_int("RED_TRAIL_MAX_ARG_CHARS", 120, min_value=16)
# 足跡最多列幾筆。AFC 上限是 25 次呼叫，正常報表 3-8 顆工具。
_MAX_TRAIL_ROWS = env_int("RED_TRAIL_MAX_ROWS", 25, min_value=1)

_FOOTER_HEADING = "── 資料來源（小紅查詢足跡，可自行重跑對帳）──"
# 措辭要同時適用於「報表沒查資料」和「本來就不需查資料的提醒類排程」——
# 前者是嚴重問題，後者只是常態。所以講事實（沒查任何來源、未經驗證），
# 不預設內容一定含數字。
_NO_TOOL_WARNING = (
    "⚠️ 這份內容沒有查詢任何資料來源（純模型輸出，未經真實資料驗證）。"
)


def _short(value: Any) -> str:
    """把單一參數值壓成一行短字串，並且過 secret redaction。

    參數會原樣進到寄給員工的信裡，所以一定要洗過：工具參數塞得進 file_id、
    查詢字串，也塞得進不該外流的東西。
    """
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = redact_log_line(text).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > _MAX_ARG_CHARS:
        text = text[:_MAX_ARG_CHARS] + "…"
    return text


def extract_tool_trail(response: Any) -> list[dict[str, Any]]:
    """從 Gemini 回應裡抽出這一輪實際跑過的工具呼叫。

    來源是 SDK 的 automatic_function_calling_history（AFC 自己執行工具時記的
    帳），不是模型自述。取不到就回空 list —— 足跡是附加資訊，絕不能讓報表因為
    抽不到足跡而發不出去。
    """
    trail: list[dict[str, Any]] = []
    try:
        history = getattr(response, "automatic_function_calling_history", None) or []
        for content in history:
            for part in (getattr(content, "parts", None) or []):
                call = getattr(part, "function_call", None)
                if call is None:
                    continue
                name = str(getattr(call, "name", "") or "").strip()
                if not name:
                    continue
                args = getattr(call, "args", None) or {}
                if not isinstance(args, dict):
                    args = {}
                trail.append({"tool": name, "args": args})
    except Exception:
        # 足跡抽取失敗絕不能擋住報表本身。
        return []
    return trail


def format_source_footer(trail: list[dict[str, Any]]) -> str:
    """把足跡排版成附在報表尾巴的區塊。空足跡回傳警告而不是空字串。"""
    if not trail:
        return _NO_TOOL_WARNING
    rows = trail[:_MAX_TRAIL_ROWS]
    dropped = len(trail) - len(rows)
    lines = [_FOOTER_HEADING]
    for i, entry in enumerate(rows, 1):
        args = entry.get("args") or {}
        rendered = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        lines.append(f"{i}. {entry.get('tool', '?')}({rendered})")
    if dropped:
        lines.append(f"（另有 {dropped} 筆呼叫未列出）")
    return "\n".join(lines)


def append_source_footer(result: str, response: Any) -> str:
    """報表正文 + 資料來源足跡。任何失敗都退回原文。

    正文為空時**不加**足跡：空結果代表「這輪沒東西可報」，dispatcher 靠
    dispatcher_result_is_empty 把它攔下來不寄信。加了足跡就變成非空字串，
    本來該安靜跳過的排程會每輪寄一封只有足跡的信出去。
    """
    body = (result or "").rstrip()
    if not body:
        return result
    try:
        footer = format_source_footer(extract_tool_trail(response))
    except Exception:
        return result
    if not footer:
        return result
    return f"{body}\n\n{footer}"


def strip_source_footer(text: str) -> str:
    """拆掉足跡、回傳純正文。

    dedup 雜湊一定要算在正文上：足跡帶著工具參數，而參數常含日期／時間戳，
    同一份「今天沒有變化」的報表每天算出來的雜湊都不一樣 → dedup 失效 →
    同樣的內容天天重寄。
    """
    if not text:
        return text
    found = [i for i in (text.find(_FOOTER_HEADING), text.find(_NO_TOOL_WARNING)) if i != -1]
    if not found:
        return text
    return text[:min(found)].rstrip()
