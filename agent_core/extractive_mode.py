"""Extractive-mode addendum for pure fact-lookup user messages.

When 大王 asks a question that's nothing more than "what does X have / use /
cost / equal", the LLM's freedom to synthesise is the exact attack surface
for hallucination — give it a 0.42-similarity tangentially-related snippet
and it'll wrap a confident answer around it. The fix at this layer is to
recognise those questions on arrival and append a hard "extractive only,
quote tool output verbatim, never paraphrase your way to an answer" rule
into the user-message envelope.

This is per-turn persona reinforcement. The base persona already discourages
fabrication; the addendum is the louder, situation-specific reminder that
fires only when the question is structurally a fact lookup.

Patterns are tuned for the rare-precision direction: false positives push
the LLM toward a more conservative answer (mostly fine), false negatives
just leave the default persona in charge (no regression). We err on the
side of triggering the addendum.
"""
from __future__ import annotations

import re


# Heuristic patterns that mark a message as a pure factual lookup. Each
# entry is just a regex; we don't need labels because the action (append
# the same addendum) is identical regardless of which pattern fired.
_FACT_QUERY_PATTERNS: tuple[re.Pattern, ...] = (
    # 「X 用什麼 Y」/「X 用的是」/「X 用 Y 嗎」/「X 有沒有用 Y」/「X 有用 Y 嗎」
    re.compile(r"(用什麼|用的(?:是|為)|是不是用|有(?:沒有|無)?用|採用什麼|採用的是)"),
    # 「X 是誰」/「X 是什麼」/「X 是哪」
    re.compile(r"(是誰|是什麼|是哪|哪個供應商|哪家供應商|供應商是)"),
    # 「X 多少」/「報價多少」/「單價是」/「報價是」
    re.compile(r"(多少錢|多少 ?usd|多少 ?eur|多少 ?ntd|多少 ?rmb|"
               r"報價(?:多少|是|為|金額)|單價(?:是|多少|為)|"
               r"售價(?:多少|是|為)|價格(?:是|多少|為))"),
    # 「X 有沒有 Y」/「X 是不是 Y」 — yes/no lookups
    re.compile(r"(有沒有|是不是|有無).{0,30}(防水膜|撥水|防水|認證|BOM|料號|"
               r"規格|供應商|報價|庫存)"),
    # 「X 的 BOM/規格/料號/認證/材料」
    re.compile(r".{1,40}的\s*(BOM|規格|料號|認證|材料|清單|單價|報價|庫存|交期)"),
    # 「查 X 的 Y」/「查一下 X」 — explicit lookup verbs
    re.compile(r"(查|查詢|查一下|找|找一下|看一下)"
               r".{0,30}(BOM|規格|料號|認證|材料|供應商|報價|單價|庫存|交期|客戶)"),
    # English equivalents — match "what is X", "what's the Y for Z",
    # "what supplier/material/price...", "does X use/have Y"
    re.compile(
        r"\bwhat(?:'s|\s+is|\s+are|\s+does|\s+do)?\s+(?:the\s+)?"
        r"(?:material|spec|price|BOM|vendor|supplier|certif|cost|currency)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:does|do)\s+\w+\s+(?:use|have)\b", re.IGNORECASE),
)


_ADDENDUM_TEXT = (
    "🔬 **萃取模式（這則訊息是純查詢類問題）**：\n"
    "  1. 先用 **query_bom / query_email_lake / query_quote_history** 拿原始資料\n"
    "  2. 從工具回傳裡 **逐字 quote** 料號 / 客戶 / 金額 / 供應商 / 來源檔\n"
    "  3. 結尾必附 `[證據：<source_file 或 message_id>]`\n"
    "  4. 工具找不到 → 直接說「BOM 庫 / lake 裡查不到 X」+ 建議下一步；\n"
    "     **禁止**用「應該」「通常」「可能」「相關材料」拼湊\n"
    "  5. 出文前用 verify_claim(facts, evidence) 自驗，✅ PASS 才送出"
)


def is_fact_lookup(user_text: str) -> bool:
    """Return True when `user_text` looks like a pure fact-lookup question."""
    text = (user_text or "").strip()
    if not text or len(text) > 600:
        # Long pastes are usually email forwards / log dumps, not lookups.
        return False
    return any(p.search(text) for p in _FACT_QUERY_PATTERNS)


def extractive_addendum(user_text: str) -> str:
    """Return the extractive-mode instruction block, or empty when the
    message is not a fact-lookup question. Caller prepends this onto the
    user-message envelope before sending to the LLM."""
    if not is_fact_lookup(user_text):
        return ""
    return _ADDENDUM_TEXT
