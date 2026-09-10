"""Email HTML 呈現 leaf 模組：markdown 通知信 → HTML alternative（表格對齊）。

為什麼需要：背景 dispatcher 任務結果是 Gemini 產的 markdown（表格/標題/粗體），
以前整包塞 text/plain 寄出 → Mail 客戶端把 `| :---: |` 原樣顯示、欄位完全沒對
齊（daily_production_8am 的生產明細表沒法讀）。Email 跟 Telegram 不同（那邊靠
<pre> 等寬，見 telegram_format），HTML 信才是正解：markdown 表格轉真 <table>，
客戶端自己排版，中文/emoji 寬度都不是問題。

契約：
  - `markdown_to_email_html(text)` 回完整 HTML 片段；文字裡**沒有任何** markdown
    結構（表格/標題/粗體斜體/``` 圍欄/分隔線）時回 None → 呼叫端維持純文字信
    （對不含 markdown 的通知＝零行為改變）。
  - 一律先 HTML-escape 再轉換：任務結果可能引用外部內容（信件/網頁），`<script>`
    之類要逐字呈現、不能被當標籤解析。
  - 樣式全 inline（多數 email 客戶端會剝 <style> 區塊）；不寫死文字顏色，讓
    客戶端 dark mode 自己反轉。

跟 telegram_format 一樣是 leaf 模組：只 import 標準庫，任何層都能安全引用。
"""
import html as _html
import re

# ```lang\n …內容… \n``` —— 成對圍欄才算（同 telegram_format._FENCE_RE 的語意）。
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*-{3,}\s*$")
# 表格分隔列的一格：`---` / `:---` / `:---:` / `---:`（寬鬆接受 1 個以上 dash）。
_SEP_CELL_RE = re.compile(r"^:?-+:?$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")

_FONT_STACK = (
    "-apple-system,'Helvetica Neue',Arial,"
    "'PingFang TC','Heiti TC','Microsoft JhengHei',sans-serif"
)
_CELL_BORDER = "border:1px solid #bbb;padding:4px 10px;"


def _esc(s: str) -> str:
    # quote=False：轉換後的文字只進元素內容、不進屬性，跟 telegram_format 同準則。
    return _html.escape(s, quote=False)


def _inline(escaped: str) -> tuple[str, int]:
    """把「已 escape」的單行文字做 **粗體** / *斜體* 替換，回 (html, 替換次數)。"""
    out, n_bold = _BOLD_RE.subn(r"<b>\1</b>", escaped)
    out, n_italic = _ITALIC_RE.subn(r"<i>\1</i>", out)
    return out, n_bold + n_italic


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _aligns_from_sep(sep_cells: list[str]) -> list[str]:
    out = []
    for c in sep_cells:
        if c.startswith(":") and c.endswith(":"):
            out.append("center")
        elif c.endswith(":"):
            out.append("right")
        else:
            out.append("left")
    return out


def _render_table(header_line: str, sep_cells: list[str], body_lines: list[str]) -> str:
    aligns = _aligns_from_sep(sep_cells)

    def cell(tag: str, idx: int, raw: str) -> str:
        align = aligns[idx] if idx < len(aligns) else "left"
        style = f"{_CELL_BORDER}text-align:{align};"
        if tag == "th":
            style += "background:#f2f2f2;"
        rendered, _ = _inline(_esc(raw))
        return f'<{tag} style="{style}">{rendered}</{tag}>'

    def row(tag: str, cells: list[str]) -> str:
        return "<tr>" + "".join(cell(tag, i, c) for i, c in enumerate(cells)) + "</tr>"

    parts = ['<table style="border-collapse:collapse;margin:8px 0;">']
    parts.append(row("th", _split_row(header_line)))
    for line in body_lines:
        parts.append(row("td", _split_row(line)))
    parts.append("</table>")
    return "\n".join(parts)


def _is_table_sep(line: str) -> list[str] | None:
    """整行都是分隔格（| :--- | :---: |…）→ 回格清單；否則 None。"""
    if "|" not in line:
        return None
    cells = _split_row(line)
    if cells and all(_SEP_CELL_RE.match(c) for c in cells):
        return cells
    return None


# 標題層級 → 字級（用 <div> 不用 <h*>：各家客戶端 h 標籤預設 margin 差很大）。
_HEADING_SIZES = {1: "20px", 2: "18px", 3: "16px", 4: "15px", 5: "14px", 6: "14px"}


def _render_text_segment(segment: str, out: list[str]) -> bool:
    """處理一段非圍欄文字（表格/標題/分隔線/行內格式），回「有沒有轉到東西」。"""
    found = False
    lines = segment.split("\n")
    buf: list[str] = []

    def flush():
        while buf and not buf[0]:
            buf.pop(0)
        while buf and not buf[-1]:
            buf.pop()
        if buf:
            out.append("<br>\n".join(buf))
        buf.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines):
            sep_cells = _is_table_sep(lines[i + 1])
            if sep_cells:
                flush()
                j = i + 2
                while j < len(lines) and "|" in lines[j]:
                    j += 1
                out.append(_render_table(line, sep_cells, lines[i + 2:j]))
                found = True
                i = j
                continue
        m = _HEADING_RE.match(line)
        if m:
            flush()
            size = _HEADING_SIZES[len(m.group(1))]
            rendered, _ = _inline(_esc(m.group(2)))
            out.append(
                f'<div style="font-weight:bold;font-size:{size};'
                f'margin:14px 0 6px;">{rendered}</div>'
            )
            found = True
            i += 1
            continue
        if _HR_RE.match(line):
            flush()
            out.append('<hr style="border:none;border-top:1px solid #ccc;margin:12px 0;">')
            found = True
            i += 1
            continue
        rendered, n = _inline(_esc(line))
        if n:
            found = True
        buf.append(rendered)
        i += 1
    flush()
    return found


def markdown_to_email_html(text: str) -> str | None:
    """把 markdown 通知文轉成 email 用 HTML；沒有 markdown 結構時回 None。"""
    s = text or ""
    if not s.strip():
        return None
    found = False
    out: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(s):
        found |= _render_text_segment(s[pos:m.start()], out)
        code = m.group(1)
        if code.endswith("\n"):
            code = code[:-1]
        out.append(
            '<pre style="background:#f6f6f6;padding:8px;border-radius:4px;'
            "overflow:auto;font-family:Menlo,Consolas,monospace;font-size:13px;\">"
            + _esc(code) + "</pre>"
        )
        found = True
        pos = m.end()
    found |= _render_text_segment(s[pos:], out)
    if not found:
        return None
    return (
        f'<div style="font-family:{_FONT_STACK};font-size:14px;line-height:1.6;">\n'
        + "\n".join(out)
        + "\n</div>"
    )
