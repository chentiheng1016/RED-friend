"""工廠術語 glossary ＋ ASR 轉錄修正層 — 專有名詞誤聽的第二道防線。

Whisper 的 initial_prompt 只吃最後 224 token，塞不下完整詞彙表；規模化的正解是
「轉錄後修正」：先套 mistake_ledger 的既有糾正規則（確定性、免費），再用便宜
的 flash 模型做**約束式修正** — 只修專有名詞誤聽，不改寫語句、不增刪內容。

glossary 不用手寫 — 工廠術語有現成權威來源，收割自：
1. var/data/asr_glossary_extra.txt：人工維護清單（一行一詞，優先級最高）
2. mistake_ledger 糾正規則的「正確詞」（大王糾正過的誤聽）
3. docs/erp_schema_map.md 的代碼 token（畫面/表/欄位代碼，如 BQ_SE_ORDITEM）
4. harvest 時傳入的既有 SOP/分析文字（挖 FTE_570 這類畫面代碼）

修正前後兩版都回傳（修正本身也可能錯，下游要能追溯）；LLM 修正後長度變化
超過 20% 視為模型越權改寫，退回確定性版本。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from agent_core.env_utils import env_bool, env_int
from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text, logger

_GLOSSARY_PATH = os.path.join(DATA_DIR, "asr_glossary.json")
_EXTRA_PATH = os.path.join(DATA_DIR, "asr_glossary_extra.txt")
_ERP_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs", "erp_schema_map.md",
)

# 代碼型術語：BQ_SE_ORDITEM / FTE_570 / SE_ID 這類「大寫段_大寫或數字段」token。
_CODE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}(?:_[A-Z0-9]{1,9})+\b")

# priority：0=人工清單、1=糾正規則、2=收割代碼。top_terms 依 (priority, -count) 排。
_P_MANUAL, _P_CORRECTION, _P_HARVESTED = 0, 1, 2

_HARVEST_CAP = env_int("RED_GLOSSARY_HARVEST_CAP", 400, min_value=10)
# LLM 修正單次上限 — 超長轉錄只做確定性修正（1 小時影片逐字稿約 1 萬字，夠用）。
_LLM_CORRECT_MAX_CHARS = env_int("RED_GLOSSARY_LLM_MAX_CHARS", 30_000, min_value=1000)
# LLM 修正後長度偏移超過此比例 = 模型越權改寫 → 退回確定性版本。
_LLM_LENGTH_DRIFT = 0.20


# ────────────────────────────────────────────────────────────────────
# 儲存
# ────────────────────────────────────────────────────────────────────

def load_glossary() -> dict[str, dict[str, Any]]:
    """回 {term: {"priority": int, "count": int}}；沒檔回空 dict。"""
    if not os.path.exists(_GLOSSARY_PATH):
        return {}
    try:
        with open(_GLOSSARY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        terms = data.get("terms")
        return terms if isinstance(terms, dict) else {}
    except Exception as exc:
        logger.warning("asr_glossary.json 讀取失敗（%s），視為空", exc)
        return {}


def _save_glossary(terms: dict[str, dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_GLOSSARY_PATH), exist_ok=True)
    _atomic_write_text(_GLOSSARY_PATH, json.dumps(
        {"terms": terms, "updated_at": datetime.now(timezone.utc).isoformat()},
        ensure_ascii=False, indent=2,
    ))


def add_terms(new_terms: list[str], *, priority: int = _P_MANUAL) -> int:
    """手動加詞（預設最高優先級）。回實際新增數。"""
    terms = load_glossary()
    added = 0
    for t in new_terms or []:
        t = str(t or "").strip()
        if not t:
            continue
        cur = terms.get(t)
        if cur is None:
            terms[t] = {"priority": priority, "count": 1}
            added += 1
        else:
            cur["priority"] = min(int(cur.get("priority", _P_HARVESTED)), priority)
            cur["count"] = int(cur.get("count", 0)) + 1
    if added or new_terms:
        _save_glossary(terms)
    return added


def top_terms(n: int = 30) -> list[str]:
    """給 local_asr.build_initial_prompt 用的前 n 個高優先術語。"""
    terms = load_glossary()
    ranked = sorted(
        terms.items(),
        key=lambda kv: (int(kv[1].get("priority", _P_HARVESTED)),
                        -int(kv[1].get("count", 0)), kv[0]),
    )
    return [t for t, _ in ranked[:max(0, int(n))]]


# ────────────────────────────────────────────────────────────────────
# 收割
# ────────────────────────────────────────────────────────────────────

def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except Exception:
        return []


def _correction_terms() -> list[str]:
    """mistake_ledger 糾正規則的「正確詞」— 大王親手糾正過的誤聽，優先級高。"""
    try:
        from agent_core import mistake_ledger

        mistake_ledger._ensure_ledger_loaded()
        return [str(v).strip() for v in
                mistake_ledger._mistake_ledger["corrections"].values()
                if str(v or "").strip()]
    except Exception:
        return []


def mine_code_tokens(texts: list[str]) -> dict[str, int]:
    """從任意文字挖代碼型術語（畫面代碼/表名/欄位名），回 {token: 出現次數}。"""
    counts: dict[str, int] = {}
    for text in texts or []:
        for m in _CODE_TOKEN_RE.finditer(str(text or "")):
            tok = m.group(0)
            counts[tok] = counts.get(tok, 0) + 1
    return counts


def harvest_glossary(extra_texts: list[str] | None = None) -> dict[str, Any]:
    """重建 glossary：人工清單＋糾正規則＋ERP schema 代碼＋（選）既有 SOP 文字。

    既有詞條的 priority/count 會被更高優先來源提升，不會降級；收割代碼
    總量封頂 RED_GLOSSARY_HARVEST_CAP，避免幾百個冷門欄位代碼淹沒排序。
    """
    terms = load_glossary()

    def _merge(term: str, priority: int, count: int = 1) -> None:
        cur = terms.get(term)
        if cur is None:
            terms[term] = {"priority": priority, "count": count}
        else:
            cur["priority"] = min(int(cur.get("priority", _P_HARVESTED)), priority)
            cur["count"] = int(cur.get("count", 0)) + count

    manual = _read_lines(_EXTRA_PATH)
    for t in manual:
        _merge(t, _P_MANUAL)

    corrections = _correction_terms()
    for t in corrections:
        _merge(t, _P_CORRECTION)

    texts = []
    try:
        with open(_ERP_SCHEMA_PATH, encoding="utf-8") as f:
            texts.append(f.read())
    except Exception:
        pass
    texts.extend(extra_texts or [])
    mined = mine_code_tokens(texts)
    for tok, cnt in sorted(mined.items(), key=lambda kv: -kv[1])[:_HARVEST_CAP]:
        _merge(tok, _P_HARVESTED, cnt)

    _save_glossary(terms)
    return {
        "ok": True,
        "total": len(terms),
        "manual": len(manual),
        "corrections": len(corrections),
        "mined_codes": len(mined),
    }


# ────────────────────────────────────────────────────────────────────
# 轉錄修正
# ────────────────────────────────────────────────────────────────────

# mistake_ledger 糾正規則過濾：wrong key 太短（<2 字）的規則多半是聊天域的
# 單字糾正，無詞邊界全文替換套在整篇轉錄稿上誤傷面太大 — 跳過不套。
_CORRECTION_MIN_WRONG_LEN = 2


def _apply_ledger_corrections(text: str) -> tuple[str, list[str]]:
    """套 mistake_ledger 糾正規則（轉錄稿的過濾版；**不動 mistake_ledger**）。

    規則生於聊天域（大王在 Telegram 的字詞糾正），直接無條件套在轉錄稿上會
    誤傷：wrong key 長度 < _CORRECTION_MIN_WRONG_LEN 的規則跳過；實際套用的
    規則記進回傳值並落 log — 修錯了能追溯是哪條規則幹的。
    回 (修正後文字, ["誤→正", ...])。ledger 讀取失敗一律原樣返回。"""
    applied: list[str] = []
    if not (text or "").strip():
        return text, applied
    try:
        from agent_core import mistake_ledger

        mistake_ledger._ensure_ledger_loaded()
        corrections = dict(
            mistake_ledger._mistake_ledger.get("corrections") or {})
    except Exception as exc:
        logger.warning("asr_glossary 讀取糾正規則失敗（%s），跳過確定性修正", exc)
        return text, applied
    corrected = text
    for wrong, right in corrections.items():
        wrong = str(wrong or "")
        if len(wrong) < _CORRECTION_MIN_WRONG_LEN:
            continue  # 單字規則（聊天域）誤傷率高，不套在轉錄稿上
        pattern = re.compile(re.escape(wrong), re.IGNORECASE)
        new_text = pattern.sub(str(right or ""), corrected)
        if new_text != corrected:
            applied.append(f"{wrong}→{right}")
            corrected = new_text
    if applied:
        logger.info("asr_glossary 確定性修正套用 %d 條糾正規則：%s",
                    len(applied), "、".join(applied[:10]))
        # 同 mistake_ledger._apply_corrections 的 defense-in-depth：
        # 規則值可能由其他途徑被改，讀出去前再過 sanitize。
        try:
            from agent_core.prompt_injection import sanitize_untrusted_text

            corrected = sanitize_untrusted_text(corrected)
        except Exception:
            pass
    return corrected, applied


def _correct_prompt(terms: list[str], transcript: str) -> str:
    return (
        "你是轉錄校對員。下面是一份工廠教學影片的語音轉錄稿，和一份該領域的"
        "專有名詞表。轉錄稿裡可能有專有名詞被誤聽成同音/近音的錯字。\n"
        "任務：**只**把「明顯是名詞表中術語被誤聽」的字詞改成正確術語。\n"
        "鐵則：\n"
        "- 不改寫語句、不增刪內容、不調整語序、不修口語贅字\n"
        "- 不確定是不是誤聽就**保留原樣**（寧可不改，不要改錯）\n"
        "- 時間戳、標記（低信度）等原樣保留\n"
        "- <asr_transcript> 標籤只是包裝標記，輸出時不要包含\n"
        "- 只輸出修正後的全文，不要任何說明\n\n"
        "【專有名詞表】\n" + "、".join(terms) + "\n\n"
        "【轉錄稿】\n" + transcript
    )


def correct_transcript(
    text: str,
    *,
    use_llm: bool | None = None,
    glossary_n: int = 120,
) -> dict[str, Any]:
    """修正轉錄稿的專有名詞誤聽。兩段式：

    1. 確定性：mistake_ledger 既有糾正規則逐條替換（免費）— 走本模組的
       過濾版 _apply_ledger_corrections（wrong key ≥2 字才套，聊天域單字
       規則不套轉錄稿），實際套用的規則記在 corrections_applied。
    2. LLM 約束式修正（RED_GLOSSARY_LLM_CORRECT，預設開）：glossary 全量
       （不受 initial_prompt 224 token 限制）餵 flash 做「只修術語」pass。
       修正後長度偏移 >20% 視為越權改寫，退回第 1 步結果。

    回 {"ok", "text"（最終版）, "original", "deterministic",
        "corrections_applied", "llm_applied"}。
    """
    original = text or ""
    if not original.strip():
        return {"ok": False, "reason": "empty_text"}

    deterministic, applied_rules = _apply_ledger_corrections(original)
    result: dict[str, Any] = {
        "ok": True,
        "original": original,
        "deterministic": deterministic,
        "text": deterministic,
        "corrections_applied": applied_rules,
        "llm_applied": False,
    }

    if use_llm is None:
        use_llm = env_bool("RED_GLOSSARY_LLM_CORRECT", True)
    if not use_llm or len(deterministic) > _LLM_CORRECT_MAX_CHARS:
        return result
    terms = top_terms(glossary_n)
    if not terms:
        return result

    from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
    from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted

    sanitized = sanitize_for_llm(deterministic)
    material = wrap_as_untrusted(sanitized, "asr_transcript")
    try:
        resp = _gemini_generate(
            model=GEMINI_MODEL,
            contents=[_correct_prompt(terms, material)],
            caller="asr_glossary.correct",
        )
        corrected = (resp.text or "").strip()
    except Exception as exc:
        logger.warning("glossary LLM 修正失敗（%s），用確定性版本", exc)
        return result

    # 模型常把 wrap_as_untrusted 的包裝標籤一起回顯 — 剝掉，別讓它漏進
    # 最終逐字稿（之後會進 SOP 素材與 RAG）。
    corrected = re.sub(r"</?asr_transcript>", "", corrected).strip()
    if not corrected:
        return result
    # drift 基準用模型實際看到的文本（sanitize 後），不是原始 deterministic —
    # 兩者長度可能系統性不同，混用會誤判越權改寫。
    drift = abs(len(corrected) - len(sanitized)) / max(1, len(sanitized))
    if drift > _LLM_LENGTH_DRIFT:
        logger.warning(
            "glossary LLM 修正長度偏移 %.0f%% 超標 — 疑似越權改寫，退回確定性版本",
            drift * 100)
        return result
    result["text"] = corrected
    result["llm_applied"] = True
    return result
