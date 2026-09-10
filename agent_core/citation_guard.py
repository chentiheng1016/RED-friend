"""Pre-send citation guard — block fabricated business facts from reaching users.

The persona has 事實準確守則 rules instructing 小紅 to attach [證據：<id>]
or [查不到直接證據] markers whenever it makes a material/BOM/spec/customer
claim. The rules work most of the time, but the LLM still occasionally
forgets — and the Jalas-防水膜 incident showed that one slipped fact can
cost trust.

This module is the hard guard: scan outgoing text for the high-risk
patterns (customer name + material/spec/price keyword) and require an
explicit evidence marker. Caller decides whether to block, warn, or just
log; the check itself is side-effect-free.

Why keyword regex instead of an LLM judge:
- runs in <1ms on the message hot path
- deterministic and inspectable (we can show the user exactly which keyword
  triggered the warning)
- no extra inference cost
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# Customer names that appear in entity_aliases.json + obvious related forms.
# Hits are case-insensitive; we keep the canonical capitalization just for
# the trigger report so it reads naturally in the warning banner.
_CUSTOMER_NAMES: tuple[str, ...] = (
    "Jalas", "Lurchi", "Richter", "Blaklader", "Blåkläder", "Ejendals",
    "Decathlon", "Isco", "Bartek", "Fuchun",
    "Huafon", "華峰", "Mastrotto", "Toung Far", "Otto Stockmayer", "Biagioli",
    "Coats", "TecnoGi", "Sympatex", "Gore-Tex", "Goretex", "eVent", "OutDry",
)
_CUSTOMER_PATTERN = re.compile(
    "(" + "|".join(re.escape(name) for name in _CUSTOMER_NAMES) + ")",
    flags=re.IGNORECASE,
)

# Domain-specific factual-claim keywords. If the message mentions one of
# these alongside a customer name and no evidence marker, we treat it as a
# spec/material/price/认证 assertion that needs a citation.
_FACT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"防水膜|防水|撥水|waterproof|membrane", flags=re.IGNORECASE),
    re.compile(r"BOM\b|料號|規格|認證|材料(?!\s*$)|spec|certification",
               flags=re.IGNORECASE),
    re.compile(r"\d+(?:\.\d+)?\s*(?:EUR|USD|RMB|TWD|NTD|€|\$)\b",
               flags=re.IGNORECASE),
    re.compile(r"單價|報價|採購[^一二三四五六七八九十]*\d", flags=re.IGNORECASE),
    re.compile(r"PU468|PU\s?\d{2,4}|D\dDH\d{4}", flags=re.IGNORECASE),
)

# Any of these markers in the message counts as a satisfied citation
# requirement. The first is the persona-canonical form; the rest cover the
# usual variations the model produces in practice.
_EVIDENCE_PATTERN = re.compile(
    r"\[(?:證據|evidence|source|出處|查不到直接證據|no\s*direct\s*evidence)[:：]?",
    flags=re.IGNORECASE,
)

# Tool outputs that are themselves the ground truth do not need an extra
# evidence marker — query_bom / query_quote_history / query_email_lake
# already include source filenames or message_ids inline. Detect when the
# message is essentially a quoted tool result so we don't flag it.
_TOOL_OUTPUT_MARKERS: tuple[re.Pattern, ...] = (
    re.compile(r"📁\s*來源檔案[：:]"),
    re.compile(r"來源[：:].*\.xlsx"),
    re.compile(r"id=[0-9a-f]{10,}"),  # gmail message_id pattern
    re.compile(r"message_id\s*[:：=]"),
)


@dataclass
class CitationCheckResult:
    ok: bool
    matched_customers: list[str] = field(default_factory=list)
    matched_facts: list[str] = field(default_factory=list)
    reason: str = ""

    def banner(self) -> str:
        """Render a one-paragraph user-facing warning."""
        custs = "、".join(self.matched_customers[:3]) or "客戶"
        facts = "、".join(self.matched_facts[:3]) or "規格"
        return (
            f"⚠️ 未通過引用檢查：偵測到「{custs}」+「{facts}」相關宣稱，"
            f"但沒附 [證據：…] 引用。如果是關鍵業務事實，建議回「重查」"
            f"讓小紅用 query_bom / query_email_lake 重新確認。"
        )


def _strip_for_check(text: str) -> str:
    """Drop tool-output decorations / code fences before checking — the user
    pastes those verbatim sometimes and we don't want to re-flag them."""
    out = text or ""
    # Strip code fences so the content inside isn't accidentally scanned as a
    # bare claim; tool outputs typically aren't fenced anyway.
    out = re.sub(r"```.*?```", "", out, flags=re.DOTALL)
    return out


def check_citation(text: str) -> CitationCheckResult:
    """Return whether `text` carries a citation when it makes a factual claim.

    The rule (intentionally narrow to keep false-positive rate low):
      flag iff message mentions a customer name AND at least one
      material/spec/price keyword AND there is no [證據：…] marker
      anywhere in the message AND the message isn't essentially a
      structured tool output (which carries its own provenance).
    """
    if not text or not text.strip():
        return CitationCheckResult(ok=True)
    scanned = _strip_for_check(text)

    customers = sorted({m.group(1) for m in _CUSTOMER_PATTERN.finditer(scanned)})

    facts: list[str] = []
    for pat in _FACT_PATTERNS:
        m = pat.search(scanned)
        if m:
            facts.append(m.group(0))

    # Two trigger paths to require evidence:
    #   (a) customer name + fact keyword in the same reply, OR
    #   (b) a price/BOM/spec assertion strong enough to need a citation
    #       even without an explicit customer (e.g. "1155 報價 25 EUR" —
    #       the SKU + price + BOM-class word combo IS a factual claim).
    has_price = any("EUR" in f or "USD" in f or "RMB" in f or "TWD" in f
                    or "NTD" in f or "€" in f or "$" in f for f in facts)
    has_spec_word = any(
        re.search(r"BOM|料號|規格|認證|spec|單價|報價", f, flags=re.IGNORECASE)
        for f in facts
    )
    standalone_factual = has_price and has_spec_word

    if not customers and not standalone_factual:
        return CitationCheckResult(ok=True)
    if not facts:
        return CitationCheckResult(ok=True)

    if _EVIDENCE_PATTERN.search(scanned):
        return CitationCheckResult(
            ok=True,
            matched_customers=customers,
            matched_facts=facts,
        )

    # Structured tool output (query_bom / query_email_lake) carries its own
    # provenance — don't double-flag.
    if any(marker.search(scanned) for marker in _TOOL_OUTPUT_MARKERS):
        return CitationCheckResult(
            ok=True,
            matched_customers=customers,
            matched_facts=facts,
        )

    reason = (
        f"customers={customers}, facts={facts}, no evidence marker, "
        f"not a structured tool output"
    )
    return CitationCheckResult(
        ok=False,
        matched_customers=customers,
        matched_facts=facts,
        reason=reason,
    )


def annotate_with_warning(text: str, result: CitationCheckResult) -> str:
    """Prepend a citation-guard banner to `text` when the result failed."""
    if result.ok:
        return text
    banner = result.banner()
    return f"{banner}\n─────\n{text}"


# ─────────────────────────────────────────────────────────────────────────
# 完成度宣稱 × 實際工具軌跡
# ─────────────────────────────────────────────────────────────────────────
# 2026-08-17 UserAng Richter 案：小紅回「已將您上傳的 **10 份 Richter 規格單**
# 全部彙總整理成一份 Master 開發追蹤表」——那一輪 `parse_sample_order` 呼叫 0 次，
# 一份檔案都沒開，10 是它當下對話記憶裡還剩的份數（實際上傳 15 份）。同一天的
# 「完整納入母表中的全部 18 款樣品資料」也一樣：母表有 19 列。
#
# 上面那顆 check_citation 抓不到這種：它比對的是「客戶名 + 規格關鍵字 + 有沒有
# [證據：] 標記」，而這種宣稱的問題不在**有沒有引用**，在**有沒有真的去讀**。
# 純文字永遠分不出來——同一句話在「剛讀完 15 個檔」和「憑記憶湊」之下長得一模一樣。
#
# 唯一可靠的判準是 SDK 記的 automatic_function_calling_history（report_trail
# 已經在抽）：模型改不了它。所以這裡把「可核對的完成度宣稱」× 「這一輪到底跑過
# 什麼工具」對起來，零工具＝這些數字純粹是模型輸出。
#
# 為什麼只認「零工具」而不是「有沒有跑到讀取類工具」：後者要維護一份 reader 白名單，
# 漏一顆就變假警報；而零工具是無歧義的硬信號，也正是本案 16:32 那一輪的實況。
# 之後要收緊，再往「跑了工具但沒有一顆碰到來源」那層加。

# 完成度用語：宣稱「涵蓋了全部」的說法。
_COMPLETENESS_WORDS = re.compile(
    r"全部|完整|全數|悉數|都已|皆已|一併(?:納入|彙總|整理)|"
    r"彙總|彙整|納入|整理成|成功(?:讀取|解析)|"
    r"\ball\b|\bcomplete\b|\bcomprehensive\b|\bentire\b",
    flags=re.IGNORECASE,
)
# 可被對方逐一核對的數量：數字 + 量詞。這是「宣稱」與「閒聊」的分界——
# 沒有數字就沒有可以對帳的東西，也就沒有這條規則要防的傷害。
_COUNT_PATTERN = re.compile(
    r"(\d+)\s*(份|款|筆|項|列|個|件|張|頁|支|組|prs|pcs)",
    flags=re.IGNORECASE,
)


@dataclass
class CompletenessCheckResult:
    ok: bool
    claim: str = ""
    counts: list[str] = field(default_factory=list)
    tool_count: int = 0
    reason: str = ""

    def banner(self) -> str:
        shown = "、".join(self.counts[:3])
        return (
            f"🛑 完成度宣稱未經查證：這則回覆聲稱「{shown}」之類的完整結果，"
            f"但小紅這一輪**沒有呼叫任何工具** —— 數字與清單來自對話記憶，"
            f"不是重新讀取來源檔（記憶會被裁掉，被裁掉的部分會整批消失且看不出來）。"
            f"要確認完整性，請回「請逐檔重讀後再列一次，並對帳來源份數」。"
        )


def check_completeness_claim(
    text: str, tool_trail: list | None
) -> CompletenessCheckResult:
    """可核對的完成度宣稱，是不是在「這一輪什麼工具都沒跑」的情況下講出來的。

    Args:
        text: 準備送出的回覆。
        tool_trail: 這一輪實際的工具呼叫（`report_trail.extract_tool_trail`
            的回傳）。**抽不到軌跡時請傳 None**——None 代表「不知道」，一律
            放行；空 list 才代表「確定一顆都沒跑」。兩者混淆會讓抽取失敗變成
            對每則回覆亂噴警告。

    Returns:
        `CompletenessCheckResult`，ok=False 時 banner() 是給使用者看的警語。
        本函式無副作用。
    """
    if tool_trail is None:          # 不知道 ≠ 沒跑
        return CompletenessCheckResult(ok=True)
    if tool_trail:                  # 真的跑過工具就不是本規則要防的情況
        return CompletenessCheckResult(ok=True, tool_count=len(tool_trail))
    if not text or not text.strip():
        return CompletenessCheckResult(ok=True)

    scanned = _strip_for_check(text)
    if not _COMPLETENESS_WORDS.search(scanned):
        return CompletenessCheckResult(ok=True)

    counts = [m.group(0) for m in _COUNT_PATTERN.finditer(scanned)]
    if not counts:
        # 沒有數字＝沒有可對帳的宣稱（「已完整了解您的需求」這種不該被罵）。
        return CompletenessCheckResult(ok=True)

    claim = _COMPLETENESS_WORDS.search(scanned).group(0)
    return CompletenessCheckResult(
        ok=False,
        claim=claim,
        counts=counts,
        tool_count=0,
        reason=(f"completeness claim={claim!r}, counts={counts[:5]}, "
                f"tool_calls=0 this turn"),
    )


def annotate_with_completeness_warning(
    text: str, result: CompletenessCheckResult
) -> str:
    """完成度警語擺在最前面：它比一般引用警語更具體、也更該被讀到。"""
    if result.ok:
        return text
    return f"{result.banner()}\n─────\n{text}"


# ─────────────────────────────────────────────────────────────────────────
# verify_claim — agent-callable self-check tool
# ─────────────────────────────────────────────────────────────────────────

# Normalize before substring comparison: lowercase + strip common
# unicode width / dash variants. Material codes like "PU468" should match
# "pu468"; an em-dash in evidence shouldn't break "-" in claim.
_NORM_DASH = re.compile(r"[—–‑‐]")
_NORM_WHITESPACE = re.compile(r"\s+")


def _normalize_for_match(text: str) -> str:
    out = (text or "").lower()
    out = _NORM_DASH.sub("-", out)
    out = _NORM_WHITESPACE.sub(" ", out)
    return out.strip()


def verify_claim(facts_to_verify: list[str], evidence: str) -> str:
    """🔬 自驗工具 — 確認你引用的事實真的出現在 tool output 裡。

    送出含「客戶用 X」「報價 N」「料號 ABC」這類事實宣稱的回覆前**先呼叫
    這個工具**：把你打算說的關鍵詞（料號、客戶名、金額、供應商等）列成
    facts_to_verify，把剛剛 query_bom / query_email_lake / query_quote_history
    回的原始字串貼進 evidence，函式會逐項比對。

    Args:
      facts_to_verify: 你準備在回覆中宣稱的具體詞彙清單。例如
        ["Jalas", "CASPER 005", "IDROREPELLEN", "1.8528 EUR"]。**只列要驗證
        的硬事實**（料號、客戶、金額、供應商），不要列贅詞或形容詞。
      evidence: tool 剛剛回傳的完整字串。整段貼進來，不要自己剪。

    Returns:
      ✅ PASS：每個 fact 都在 evidence 裡（可放心回答）
      ❌ FAIL：列出哪幾個 fact 找不到（必須改寫回答或重查）

    比對採大小寫不敏感 + 全形/半形 dash 正規化。完全空白的 fact 會被跳過；
    若 facts_to_verify 為空、或 evidence 為空，回傳錯誤訊息。
    """
    if not facts_to_verify:
        return ("❌ verify_claim 需要至少 1 個 fact。把你準備宣稱的具體詞"
                "（料號 / 客戶 / 金額 / 供應商）列進 facts_to_verify。")
    if isinstance(facts_to_verify, str):
        # Common LLM mistake: passing a single string instead of a list.
        # Accept it but normalize to a one-item list.
        facts_to_verify = [facts_to_verify]
    if not evidence or not str(evidence).strip():
        return ("❌ verify_claim 需要 evidence — 把剛剛 tool 回傳的「完整字串」"
                "貼進來。空 evidence 等於沒查。")

    norm_evidence = _normalize_for_match(str(evidence))
    if not norm_evidence:
        return "❌ evidence 正規化後是空的，無法比對。"

    verified: list[str] = []
    missing: list[str] = []
    for raw in facts_to_verify:
        fact = _normalize_for_match(str(raw or ""))
        if not fact:
            continue
        if fact in norm_evidence:
            verified.append(str(raw).strip())
        else:
            missing.append(str(raw).strip())

    total = len(verified) + len(missing)
    if not missing:
        return (
            f"✅ PASS — {total}/{total} 個 fact 都在 evidence 裡，可以放心回答。\n"
            f"  已驗證：{', '.join(verified)}"
        )

    lines = [
        f"❌ FAIL — {len(missing)}/{total} 個 fact 在 evidence 裡找不到："
    ]
    for f in missing:
        lines.append(f"  ✗ 「{f}」未出現於 evidence")
    if verified:
        lines.append(f"\n（{len(verified)} 個 fact 有驗到：{', '.join(verified)}）")
    lines.append(
        "\n處理方式：(1) 把找不到的詞從回覆裡拿掉、改用『查不到直接證據』，"
        "或 (2) 重跑 query_bom / query_email_lake 用不同關鍵字再查一次。"
        "**禁止無證據硬答**。"
    )
    return "\n".join(lines)
