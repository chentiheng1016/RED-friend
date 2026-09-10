"""Telegram 訊息格式 leaf 模組：``` code fence → HTML <pre>、長訊息安全分段。

為什麼需要：Telegram **完全不渲染 Markdown 表格**，預設也不會把任何文字當等寬字 ——
要讓欄位對齊（倉庫庫存表、每日生產回報這類），唯一辦法是送 `parse_mode=HTML` + `<pre>`。
這個 leaf 模組做兩件事：
  1. `markdown_to_telegram_html`：偵測成對的 ``` 圍欄、把圍欄內當等寬區塊、其餘文字做
     HTML 轉義，回 `(html, "HTML")`。沒有圍欄就原樣回 `(text, None)`，呼叫端照常走
     純文字（對不含圍欄的訊息＝零行為改變）。
  2. `telegram_text_chunks`：把長訊息切成 Telegram 可送的分段。Telegram 的 4096 上限
     算的是 **UTF-16 code unit**（emoji / 罕見字佔 2），不是 Python 的 code point 數；
     且多段時呼叫端會加 `[i/n]\\n` 前綴，每段要預留空間，否則切滿 4096 的段會被前綴
     推爆上限、整段被 Telegram 打回（超長回覆前段丟失）。

刻意只認 ``` 圍欄，不碰 **粗體** / _斜體_（維持與現況一致：那些本來在純文字模式就顯示
字面符號）。未閉合（奇數個 ```）視為非圍欄，安全退回純文字、絕不送出半截 <pre>。
"""
import html as _html
import re

# ```lang\n …內容… \n``` —— 需要成對圍欄。`.*?` 非貪婪：停在第一個閉合 ```。
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _esc(s: str) -> str:
    """Telegram HTML 只認 & < >；務必 quote=False —— 它不解 &quot;/&#x27;，會顯示字面。"""
    return _html.escape(s, quote=False)


def markdown_to_telegram_html(text: str) -> tuple[str, str | None]:
    """把含 ``` 圍欄的文字轉成 Telegram HTML。

    回 ``(rendered, parse_mode)``：
      - 有成對 ``` 圍欄 → ``("…<pre>…</pre>…", "HTML")``，圍欄內等寬、其餘已 HTML 轉義。
      - 沒有圍欄（或只有未閉合的 ```）→ ``(原文, None)``，呼叫端照常送純文字。
    """
    s = text or ""
    if "```" not in s:
        return s, None
    out: list[str] = []
    pos = 0
    found = False
    for m in _FENCE_RE.finditer(s):
        out.append(_esc(s[pos:m.start()]))
        code = m.group(1)
        if code.endswith("\n"):  # 去掉閉合 ``` 前那個換行，避免 <pre> 多一條空行
            code = code[:-1]
        out.append("<pre>" + _esc(code) + "</pre>")
        pos = m.end()
        found = True
    if not found:  # 有 ``` 但非成對圍欄（未閉合等）→ 不冒險，退回純文字
        return s, None
    out.append(_esc(s[pos:]))
    return "".join(out), "HTML"


def _utf16_units(ch: str) -> int:
    """單一 code point 的 UTF-16 code unit 數（BMP=1、astral/emoji=2）。"""
    return 2 if ord(ch) > 0xFFFF else 1


def utf16_len(s: str) -> int:
    """Telegram 計長用的 UTF-16 code unit 長度（≠ len(s) code point 數）。"""
    return sum(_utf16_units(ch) for ch in s)


def telegram_text_chunks(text: str, limit: int = 4096, reserve: int = 16) -> list[str]:
    """把訊息切成 Telegram sendMessage 可安全送出的分段。

    - 長度以 **UTF-16 code unit** 計（Telegram 的 4096 上限算法；emoji 佔 2）。
    - 整則放得進 ``limit`` → 單段原樣回（呼叫端單段不加前綴，不需預留）。
    - 需要多段時，每段最多 ``limit - reserve`` 個 code unit —— 預留給呼叫端的
      ``[i/n]\\n`` 前綴，確保「前綴 + 段落」仍 ≤ limit。
    - 逐 code point 切，天然不會把 astral 字元（surrogate pair）切半。
    - 空字串回 []（呼叫端自行決定空訊息語意）。
    """
    s = text or ""
    if not s:
        return []
    if utf16_len(s) <= limit:
        return [s]
    budget = max(2, limit - max(0, reserve))  # ≥2 保證裝得下單一 astral 字元
    chunks: list[str] = []
    start = 0
    units = 0
    for i, ch in enumerate(s):
        u = _utf16_units(ch)
        if units + u > budget:
            chunks.append(s[start:i])
            start = i
            units = 0
        units += u
    chunks.append(s[start:])
    return chunks
