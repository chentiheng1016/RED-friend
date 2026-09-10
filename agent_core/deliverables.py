"""排程任務產出檔案的側通道 —— 不經 LLM 之手。

為什麼需要（2026-08-12）：採購晨報的 Excel 附件原本**只**靠 `[[MAIL_FILE:路徑]]`
標記傳遞 —— 工具把標記放進回傳字串，指望 LLM 把那一行原樣抄進最終回覆，dispatcher
再從回覆裡撈出來當附件。實戰 8/8 都抄對了，但機制上它是脆的：模型哪次沒抄，信
照樣寄出、**只是沒有附件**，沒有例外、沒有錯誤、收件人也不會知道原本該有附件。
那正是這個 repo 今天修了一整天的那種靜默失敗。

改法：工具產檔時同時 `register()` 到這裡，dispatcher 跑完任務直接 `drain()`。
標記**保留不動**（對 LLM 說明「檔案已產出、別再自己編一份」仍有用，也維持既有
行為），dispatcher 取兩邊聯集 —— LLM 抄到就抄到，沒抄到側通道照樣補上。

併發模型：dispatcher 的任務迴圈是逐支循序跑的，且工具在同一個 process 內執行
（`run_one_dispatcher_task` 直接把 tools_list 交給 genai，不走 tool_rpc）。但
`run_task_with_deadline` 會把任務丟到**工作執行緒**，所以註冊與讀取分屬不同
thread —— 因此這裡用「模組級 list + Lock」而不是 thread-local（thread-local 會
讓 dispatcher 在主執行緒 drain 到空的）。

⚠️ 路徑安全不在這一層：這裡只負責傳遞，白名單仍由 dispatcher 的
`extract_mail_attachments` 統一把關（只認 EXPORTS_DIR 底下的檔）。側通道雖然不
經 LLM，但走同一道閘才不會出現「有一條路沒被檢查」的破口。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_pending: list[str] = []


def register(path: str) -> None:
    """工具產出一個要當附件寄的檔時呼叫。重複路徑只留一份。"""
    p = str(path or "").strip()
    if not p:
        return
    with _lock:
        if p not in _pending:
            _pending.append(p)


def drain() -> list[str]:
    """取出並清空目前累積的產出檔（dispatcher 在任務前後各呼叫一次）。"""
    with _lock:
        out = list(_pending)
        _pending.clear()
    return out


def peek() -> list[str]:
    """只看不清（測試與診斷用）。"""
    with _lock:
        return list(_pending)
