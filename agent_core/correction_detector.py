"""Detect when 大王 is correcting a previous answer.

Used at the inbound side of daemon_telegram so the agent gets an explicit
"⚠️ you are being corrected" marker BEFORE it starts drafting the reply.
Without this signal the LLM tends to reflex-apologise ("您說得對！我剛剛
說錯了…") without re-querying — the so-called sycophancy mode that flipped
the original Jalas 防水膜 answer 180° on zero new evidence.

The detector is intentionally narrow: it only matches phrases that are
unambiguously corrective in factory chat context. False positives steer
the LLM to re-run a query for no reason, which is annoying; false negatives
just leave the existing 反翻供守則 in charge. Bias toward precision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Each entry is (regex, short_label). The label is what we surface to the
# LLM so it can describe what triggered the alert. Patterns are tested in
# order and the FIRST match wins (so put the most specific patterns first).
_CORRECTION_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # "我查過 X 不是 Y" / "我查過 X 沒有 Y" — the strongest signal: 大王 has
    # done independent verification and is reporting a contradiction.
    (re.compile(r"我(?:剛|剛剛|已經|早就)?查過.{0,20}(?:不是|沒有|沒用|並沒有|沒這)"),
     "user_independent_check"),
    # 你錯了 / 你搞錯了 / 你弄錯了 / 你搞混了 — direct flat assertion the
    # model was wrong.
    (re.compile(r"你(?:剛|剛剛)?(?:搞錯|弄錯|錯了|搞混|混淆|誤判)"),
     "user_direct_error_assertion"),
    # 你再確認 X / 你再查 X / 再次確認 — explicit demand to recheck. The
    # narrow form (must include 你 or 再次) avoids matching innocuous
    # "我會再確認會議時間" style sentences.
    (re.compile(r"(?:你|再次|麻煩你|請你)再?(?:確認|查|查證|查一下|查一次|查證一下|核對)"),
     "user_demand_recheck"),
    # 不對 / 不是這樣 / 不正確 — short flat denials, paired with material
    # / spec / customer / quantity keywords to avoid grabbing every "不對"
    # in casual chat. Match either order (denial-first OR keyword-first),
    # because both word orders show up in real corrections:
    #   "不對，料號是 X"           (denial-first)
    #   "1155 報價不對，再查"       (keyword-first)
    (re.compile(r"(?:不對|不正確|不是這樣|錯誤的|這不對|這是錯的)"
                r".{0,40}(?:BOM|料號|材料|供應商|報價|單價|防水膜|撥水|"
                r"客戶|數量|交期|規格|認證)"),
     "user_short_denial_with_fact"),
    (re.compile(r"(?:BOM|料號|材料|供應商|報價|單價|防水膜|撥水|客戶|"
                r"數量|交期|規格|認證)"
                r".{0,15}(?:不對|不正確|不是這樣|錯誤的|這不對|這是錯的)"),
     "user_short_denial_with_fact"),
    # Imperative recheck demand at end of message (一下/一次/清楚 sentence
    # tail) — covers bare "再確認一下" without a leading 你. The end-anchor
    # avoids matching narrative forms like "我會再確認會議時間然後跟你說".
    (re.compile(r"再(?:確認|查|查證|核對)(?:一下|一次|清楚)?"
                r"[，。!！.\s]*$"),
     "user_demand_recheck"),
)


@dataclass
class CorrectionDetection:
    is_correction: bool
    matched_pattern: str = ""
    matched_text: str = ""

    def hint(self, *, is_owner: bool = True) -> str:
        """Render the inline hint to prepend onto the LLM's user-message
        envelope. Speaks in 2nd person to mirror persona tone.

        is_owner=False renders the employee variant: same re-verify
        discipline (最高指導原則 — 被指錯必重查、查不到就老實說), but the
        sender is called 員工, tool suggestions stay generic (employees
        only see their per-color whitelist, not query_bom etc.), and the
        remember_correction_rule step is dropped (owner-only ledger).
        """
        if not self.is_correction:
            return ""
        if not is_owner:
            return (
                f"⚠️ 糾錯偵測：員工這則訊息匹配「{self.matched_pattern}」"
                f"（觸發片段：「{self.matched_text}」），指出你先前的回答有誤。\n"
                f"   【最高指導原則】員工指錯時必須**重新查核資料、照實回答**：\n"
                f"     1. 用你目前工具清單裡的唯讀查詢工具，把被質疑的事實**重查一次**\n"
                f"     2. 若新查到的資料推翻原答 → 引用新資料更正，並說明錯在哪\n"
                f"     3. 若資料仍支持原答 → 維持原答並列出資料出處\n"
                f"     4. 若重查後仍無法確認 → 老實說「查不到能確認的資料」，"
                f"絕不編造、不硬拗\n"
                f"   **禁止「您說得對」「我剛剛說錯了」這種沒重查就翻供的措詞**，"
                f"也禁止沒重查就堅持原答案。"
            )
        return (
            f"⚠️ 糾錯偵測：大王這則訊息匹配「{self.matched_pattern}」"
            f"（觸發片段：「{self.matched_text}」）。\n"
            f"   依【反翻供守則】**禁止無證據翻供**。標準流程：\n"
            f"     1. 重跑 query_bom / query_email_lake / query_quote_history 查當前事實\n"
            f"     2. 若新證據確實推翻原答 → 引用新證據更正\n"
            f"     3. 若新證據仍支持原答 → 維持原答並列證據說明\n"
            f"     4. 若雙方都無強證據 → 明說「目前查不到能拍板的證據」\n"
            f"     5. 更正確立後，若這次糾正代表「以後同類情境都適用」的行為\n"
            f"        規則（不是單次事實記錯），先用一句話向大王確認要記的規則\n"
            f"        內容，他同意後呼叫 remember_correction_rule 固化（工具會\n"
            f"        再要一次 +確認）。僅限大王本人的糾正；員工的糾正不要記。\n"
            f"   **禁止「您說得對」「我剛剛說錯了」這種沒重查的措詞。**"
        )


def detect_correction(user_text: str) -> CorrectionDetection:
    """Return whether `user_text` looks like a factual correction.

    Designed for the inbound message hot path — must be fast and have a
    low false-positive rate. The structured response gives the daemon
    enough info to print useful logs AND prepend a hint to the prompt.
    """
    text = (user_text or "").strip()
    if not text or len(text) > 2000:
        # Skip giant pastes (e.g. an email forwarded verbatim) — likely
        # not a correction, and scanning bogs the hot path.
        return CorrectionDetection(is_correction=False)

    for pattern, label in _CORRECTION_PATTERNS:
        m = pattern.search(text)
        if m:
            return CorrectionDetection(
                is_correction=True,
                matched_pattern=label,
                matched_text=m.group(0)[:50],
            )
    return CorrectionDetection(is_correction=False)
