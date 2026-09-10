"""影片知識卡（skill cards）— 三層信度分級＋完整溯源的結構化知識層。

operation_sops 存「整支影片的 SOP 文章」；本層存從影片抽出的**一條條可獨立
成立的知識主張**（claim），每張卡帶：

- tier 三層分級：confirmed（旁白×畫面雙來源印證）/ likely（單來源但明確）/
  uncertain（素材不足或低信度）
- 完整溯源鏈：來源影片 id＋時間區間＋引句 — 答錯可以回放影片驗證
- 跨影片佐證數 corroborations：同一主張被幾支**不同**影片支持

治理鐵則（operation_sops 沒有、本層補上的品質閘門）：
- uncertain 卡**不進**搜尋卡庫（skill_cards collection），落 pending 佇列等
  人工核可 — list_pending_skill_cards 看、resolve_skill_card 核（CONFIRM 級，
  大王在 Telegram +確認 通道處理）。
- 修正可追溯：卡庫是 var/data/skill_cards/cards.json 純 JSON ledger（可人工
  開檔稽核），ChromaDB collection 只是它的搜尋索引。

搜尋 collection 獨立於 drive_docs（同 operation_sops 的理由：夜跑 reconcile
不會清掉它）。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from agent_core.env_utils import env_float
from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text, logger
from agent_core.rag_gateway import (
    access_where,
    current_request_caller,
    current_request_trace_id,
    log_rag_access_event,
    metadata_access_fields,
)

_COLLECTION = "skill_cards"
_DIR = os.path.join(DATA_DIR, "skill_cards")
_CARDS_PATH = os.path.join(_DIR, "cards.json")
_PENDING_PATH = os.path.join(_DIR, "pending.json")

_TIER_ORDER = {"uncertain": 0, "likely": 1, "confirmed": 2}
_TIER_ICON = {"confirmed": "✅", "likely": "🟡", "uncertain": "❓"}

# 分級門檻：confidence ≥ CONFIRMED_MIN 且旁白×畫面雙來源 → confirmed；
# ≥ LIKELY_MIN → likely；其餘 uncertain。
_CONFIRMED_MIN = env_float("RED_SKILL_CARD_CONFIRMED_MIN", 0.85,
                           min_value=0.0, max_value=1.0)
_LIKELY_MIN = env_float("RED_SKILL_CARD_LIKELY_MIN", 0.6,
                        min_value=0.0, max_value=1.0)
# 跨影片佐證自動升級門檻：同一主張被 ≥N 支**不同**影片支持 →
# likely 升 confirmed、pending 裡的 uncertain 升 likely 進卡庫。
_CORROBORATE_MIN = int(env_float("RED_SKILL_CARD_CORROBORATE_MIN", 2,
                                 min_value=2, max_value=10))
# 語意近似合併門檻（cosine 相似度）：新卡與卡庫既有卡相似 ≥ 此值視為同一主張、
# 併入舊卡（來源合併→佐證升級照走）。沒有它，跨影片佐證幾乎是死的 — claim_key
# 要「正規化後逐字相同」才合併，不同影片講同一件事措辭必然不同。設 0 關閉。
_SEM_SIM = env_float("RED_SKILL_CARD_SEM_SIM", 0.93, min_value=0.0, max_value=1.0)
_MAX_CARD_CHARS = 600
_MAX_SOURCE_CHARS = 60_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────────────
# Ledger 儲存
# ────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _ledger_lock():
    """跨行程互斥 ledger 寫入。watcher／CLI 重學／Telegram 核卡三個入口可能
    並發 R-M-W 同一份 ledger — 無鎖是 last-writer-wins（先寫的更新靜默消失）。
    ledger 是 cards.json＋pending.json 兩檔一體，state_io.locked_json 只鎖單一
    JSON 檔且要求就地 mutate，改用 sibling .lock 以 fcntl 圈住整段
    load→mutate→save（同款 flock 模式）。lock 檔不刪（fcntl 慣例）。"""
    lock_path = _CARDS_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _load(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cards = data.get("cards")
        return cards if isinstance(cards, dict) else {}
    except Exception as exc:
        logger.warning("skill_cards ledger %s 讀取失敗（%s），視為空", path, exc)
        return {}


def _save(path: str, cards: dict[str, dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_text(path, json.dumps(
        {"cards": cards, "updated_at": _now()}, ensure_ascii=False, indent=2))


_CLAIM_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def claim_key(claim: str) -> str:
    """主張的正規化鍵（去空白/標點、轉小寫）— 去重與跨影片佐證的比對鍵。"""
    return _CLAIM_NORM_RE.sub("", str(claim or "").lower())


def _card_id(claim: str) -> str:
    return "sc_" + hashlib.sha1(claim_key(claim).encode("utf-8")).hexdigest()[:16]


def assign_tier(confidence: float, evidence: str) -> str:
    """三層分級：雙來源（旁白×畫面）且高信度才 confirmed。"""
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.0
    if conf >= _CONFIRMED_MIN and str(evidence).strip().lower() == "both":
        return "confirmed"
    if conf >= _LIKELY_MIN:
        return "likely"
    return "uncertain"


def _fmt_ts(seconds) -> str:
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


# ────────────────────────────────────────────────────────────────────
# 抽取（LLM）
# ────────────────────────────────────────────────────────────────────

def _extract_prompt(material: str) -> str:
    return (
        "你在從一支工廠教學影片的分析素材抽「知識卡」— 之後能獨立拿來回答問題的"
        "單條知識主張。只輸出 JSON array，每張卡：\n"
        "{\n"
        '  "claim": "一句話主張（具體、可驗證，例：收櫃資料在 FTE_570 畫面建立）",\n'
        '  "detail": "細節與參數（欄位=值、數量、條件、前置）",\n'
        '  "kind": "param|step|term|causal|other",\n'
        '  "confidence": 0.0-1.0（這條主張在素材裡有多明確）,\n'
        '  "start_s": 對應影片秒數（整數；不知道給 null）,\n'
        '  "end_s": 同上,\n'
        '  "evidence": "narration|screen|both"（依據：旁白、畫面文字、或兩者互相印證）,\n'
        '  "quote": "素材原文引句（50 字內，供人工核對）"\n'
        "}\n"
        "鐵則：只根據素材、不要編造；數值/代碼一字不漏；旁白與畫面矛盾或素材"
        "含糊就老實給低 confidence（會進人工核可佇列，不會丟失）。\n\n"
        "【素材】\n" + material
    )


def _parse_json_array(text: str) -> list | None:
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (ValueError, TypeError):
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, list) else None
    except Exception:
        return None


def _to_int_or_none(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def extract_skill_cards(
    video_id: str,
    video_name: str,
    analysis_text: str,
    *,
    transcript: str = "",
    department: str = "",
) -> dict[str, Any]:
    """從影片分析文字（＋可選本機逐字稿）抽知識卡。只抽不落庫 — 落庫走
    ingest_skill_cards()（測試與批次腳本可分開驗證兩步）。"""
    vid = (video_id or "").strip()
    text = (analysis_text or "").strip()
    if not vid or not text:
        return {"ok": False, "reason": "empty_video_id_or_text"}

    from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
    from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted
    from google.genai import types

    material = text
    if transcript.strip():
        material += "\n\n【本機高精度逐字稿（時間戳與術語可信）】\n" + transcript.strip()
    material = wrap_as_untrusted(
        sanitize_for_llm(material[:_MAX_SOURCE_CHARS]), "video_analysis")

    resp = _gemini_generate(
        model=GEMINI_MODEL,
        contents=[_extract_prompt(material)],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
        caller="skill_cards.extract",
    )
    items = _parse_json_array(resp.text or "")
    if items is None:
        return {"ok": False, "reason": "llm_output_unparseable"}

    now = _now()
    cards: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        if not claim:
            continue
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        tier = assign_tier(conf, str(item.get("evidence", "")))
        kind = str(item.get("kind", "other")).strip()
        cards.append({
            "card_id": _card_id(claim),
            "claim": claim,
            "detail": str(item.get("detail", "")).strip(),
            "kind": kind if kind in ("param", "step", "term", "causal") else "other",
            "confidence": conf,
            "tier": tier,
            "department": (department or "").strip(),
            "sources": [{
                "video_id": vid,
                "video_name": (video_name or vid).strip(),
                "start_s": _to_int_or_none(item.get("start_s")),
                "end_s": _to_int_or_none(item.get("end_s")),
                "quote": str(item.get("quote", "")).strip()[:120],
            }],
            "corroborations": 1,
            "status": "pending" if tier == "uncertain" else "active",
            "created_at": now,
            "updated_at": now,
        })
    return {"ok": True, "cards": cards,
            "tiers": {t: sum(1 for c in cards if c["tier"] == t)
                      for t in _TIER_ORDER}}


# ────────────────────────────────────────────────────────────────────
# 落庫
# ────────────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# 代碼型術語（畫面代碼/表名/欄位名，如 FTE_570、BQ_SE_ORDITEM）—
# 同 asr_glossary._CODE_TOKEN_RE 的 pattern，語意合併硬閘用。
_CODE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}(?:_[A-Z0-9]{1,9})+\b")


def _mergeable_claims(a: str, b: str) -> bool:
    """語意近似合併的硬閘：兩句主張的代碼 token 集合與數字集合都必須一邊是
    另一邊的子集（含相等、一邊為空），否則不併。

    「同句型、不同畫面代碼」（收櫃在 FTE_570 建立 vs 收櫃在 P4TF_560 建立）
    的向量相似度可以極高，但不是同一主張 — 錯併會假造跨影片佐證、自動升
    confirmed；衝突偵測只比 detail 的數字、不查 claim 本文，只有這裡把得住。"""
    ca = set(_CODE_TOKEN_RE.findall(a or ""))
    cb = set(_CODE_TOKEN_RE.findall(b or ""))
    if not (ca <= cb or cb <= ca):
        return False
    na = set(_NUM_RE.findall(a or ""))
    nb = set(_NUM_RE.findall(b or ""))
    return na <= nb or nb <= na


def _details_conflict(old_detail: str, new_detail: str) -> bool:
    """兩份細節的數值是否矛盾（保守啟發式）：兩邊都有數字、且互不為子集
    才算衝突 — 一邊只是多講幾個數字（子集關係）不算矛盾。
    衝突的典型：舊卡「數量=120」、新影片說「數量=150」。"""
    na = set(_NUM_RE.findall(old_detail or ""))
    nb = set(_NUM_RE.findall(new_detail or ""))
    if not na or not nb:
        return False
    return not (na <= nb or nb <= na)


def _maybe_promote(card: dict[str, Any]) -> bool:
    """跨影片佐證自動升級：likely 卡被 ≥_CORROBORATE_MIN 支不同影片支持
    → confirmed。回傳有沒有升級。conflict 卡永不自動升級（人工裁決是唯一出口）。"""
    if (card.get("tier") == "likely"
            and card.get("status") != "conflict"
            and int(card.get("corroborations", 1)) >= _CORROBORATE_MIN):
        card["tier"] = "confirmed"
        card["promoted_by"] = "corroboration"
        card["updated_at"] = _now()
        return True
    return False


def _merge_card(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """同一主張再次出現：合併來源（video_id＋start_s 去重）、重算跨影片佐證數、
    信度取高、tier 只升不降。"""
    sources = list(old.get("sources") or [])
    seen = {(s.get("video_id"), s.get("start_s")) for s in sources}
    for s in new.get("sources") or []:
        key = (s.get("video_id"), s.get("start_s"))
        if key not in seen:
            sources.append(s)
            seen.add(key)
    old["sources"] = sources
    old["corroborations"] = len(
        {s.get("video_id") for s in sources if s.get("video_id")})
    old["confidence"] = max(
        float(old.get("confidence", 0.0)), float(new.get("confidence", 0.0)))
    if _TIER_ORDER.get(new.get("tier"), 0) > _TIER_ORDER.get(old.get("tier"), 0):
        old["tier"] = new["tier"]
    old["updated_at"] = _now()
    return old


def _upsert_store(card: dict[str, Any]) -> None:
    """把一張 active 卡寫進搜尋 collection（ledger 是真相、collection 是索引）。"""
    from agent_core.ingest.vector_store import get_store

    access = metadata_access_fields(
        "sop", owner_color="red", department=card.get("department", ""))
    src = (card.get("sources") or [{}])[0]
    now = _now()
    meta: dict[str, Any] = {
        "doc_id": card["card_id"],
        "title": f"[知識卡] {card['claim'][:80]}",
        "mime_type": "text/x-skill-card",
        "chunk_index": 0,
        "synced_at": now,
        "modified_time": now[:10],
        "tier": card.get("tier", "likely"),
        "confidence": float(card.get("confidence", 0.0)),
        "kind": card.get("kind", "other"),
        "video_id": str(src.get("video_id", "")),
        "video_name": str(src.get("video_name", "")),
        "corroborations": int(card.get("corroborations", 1)),
        "sync_complete": True,
        **access,
    }
    if src.get("start_s") is not None:
        meta["start_s"] = int(src["start_s"])
    if src.get("end_s") is not None:
        meta["end_s"] = int(src["end_s"])
    text = card["claim"]
    if card.get("detail"):
        text += "\n" + card["detail"]
    get_store(_COLLECTION).upsert_batch(
        [card["card_id"]], [text[:_MAX_CARD_CHARS]], [meta])


def _semantic_match(card: dict[str, Any]) -> str | None:
    """新主張與搜尋卡庫的向量最近鄰相似 ≥ _SEM_SIM 時，回既有卡的 card_id。
    只比對 active 卡（pending 不在索引裡）；查詢失敗一律回 None（不擋 ingest）。"""
    if _SEM_SIM <= 0:
        return None
    try:
        from agent_core.ingest.vector_store import get_store

        store = get_store(_COLLECTION)
        if store.count() == 0:
            return None
        hits = store.query(card["claim"], n_results=1)
        if not hits:
            return None
        h = hits[0]
        sim = 1.0 - float(h.get("distance", 1.0))
        cid = str((h.get("metadata") or {}).get("doc_id") or "")
        if cid and cid != card["card_id"] and sim >= _SEM_SIM:
            return cid
    except Exception as exc:
        logger.warning("skill_cards 語意比對失敗（%s），當新卡處理", exc)
    return None


def _delete_from_store(card_id: str) -> None:
    try:
        from agent_core.ingest.vector_store import get_store

        get_store(_COLLECTION).delete_by_doc_id(card_id)
    except Exception as exc:
        logger.warning("知識卡 %s 從搜尋索引下架失敗（%s）— ledger 已標 conflict，"
                       "索引殘留待下次覆寫", card_id, exc)


def purge_video_cards(video_id: str) -> int:
    """重學一支影片前的清場：把「唯一來源是這支影片」的卡從 ledger／pending／
    搜尋索引移除 — 舊版抽取可能帶著舊 SOP 的錯誤，不清會新舊並存。
    跨影片佐證的卡（還有其他影片來源）保留不動。回移除張數。"""
    vid = (video_id or "").strip()
    if not vid:
        return 0
    removed = 0
    with _ledger_lock():
        for path in (_CARDS_PATH, _PENDING_PATH):
            cards = _load(path)
            doomed = [cid for cid, c in cards.items()
                      if {s.get("video_id") for s in (c.get("sources") or [])}
                      == {vid}]
            for cid in doomed:
                del cards[cid]
                if path is _CARDS_PATH:
                    _delete_from_store(cid)
            if doomed:
                _save(path, cards)
            removed += len(doomed)
    return removed


def ingest_skill_cards(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """把抽出的知識卡落庫：confirmed/likely → ledger＋搜尋索引；
    uncertain → pending 佇列（不進索引，等人工核可）。同 card_id 合併來源。

    跨影片佐證（Phase 6）：
    - likely 卡被 ≥2 支不同影片支持 → 自動升 confirmed（promoted_by 記錄）。
    - pending 裡的 uncertain 被第二支影片獨立支持 → 自動升 likely 進卡庫。
    - **數值矛盾**（新影片的細節數字與既有卡互不為子集）→ 卡標 conflict、
      降回 pending 等人工裁決、從搜尋索引下架 — 矛盾的知識寧可查不到，
      也不要讓 LLM 引用到其中錯的一邊。
    """
    with _ledger_lock():
        return _ingest_skill_cards_locked(cards)


def _ingest_skill_cards_locked(cards: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = _load(_CARDS_PATH)
    pending = _load(_PENDING_PATH)
    counts = {"confirmed": 0, "likely": 0, "uncertain": 0, "merged": 0,
              "auto_confirmed": 0, "auto_promoted": 0, "conflict": 0,
              "semantic_merged": 0}
    to_store: list[dict[str, Any]] = []
    to_delete: list[str] = []

    def _new_videos(old: dict, new: dict) -> set:
        known = {s.get("video_id") for s in old.get("sources") or []}
        return {s.get("video_id") for s in new.get("sources") or []} - known

    for card in cards or []:
        if not isinstance(card, dict) or not str(card.get("claim", "")).strip():
            continue
        cid = card.get("card_id") or _card_id(card["claim"])
        card = {**card, "card_id": cid}

        # ── 語意近似合併：措辭不同的同一主張（跨影片常態）併入既有卡，
        # 讓佐證計數與衝突偵測作用在「同一件事」上而不是逐字相同的字串。
        # 硬閘 _mergeable_claims：代碼 token／數字不相容的近鄰不併 —
        # 「同句型、不同畫面代碼」相似度再高也是兩件事。
        if cid not in ledger and cid not in pending:
            match = _semantic_match(card)
            if match and match in ledger:
                if _mergeable_claims(str(ledger[match].get("claim", "")),
                                     str(card.get("claim", ""))):
                    cid = match
                    card["card_id"] = match
                    counts["semantic_merged"] = counts.get(
                        "semantic_merged", 0) + 1
                else:
                    logger.info(
                        "skill_cards 語意近鄰 %s 與新卡代碼/數字不相容，不併"
                        "（%r ≠ %r）", match,
                        str(ledger[match].get("claim", ""))[:40],
                        str(card.get("claim", ""))[:40])

        # ── pending 吸收（**不論新卡 tier**）：同主張已在待核佇列時，新卡一律
        # 先併進 pending 卡再決定去留 — 否則 likely 重跑會繞過 conflict 隔離、
        # 或漏算 pending 份的來源與佐證數。
        if cid not in ledger and cid in pending:
            old = pending[cid]
            fresh = _new_videos(old, card)
            clash = bool(fresh) and _details_conflict(
                old.get("detail", ""), card.get("detail", ""))
            merged = _merge_card(old, card)
            counts["merged"] += 1
            if old.get("status") == "conflict" or clash:
                # 衝突（既有或新發現）：一律留 pending，人工裁決是唯一出口。
                merged["status"] = "conflict"
                merged["tier"] = "uncertain"
                if clash:
                    merged.setdefault("conflicts", []).append({
                        "video_id": next(iter(fresh), ""),
                        "detail": card.get("detail", ""),
                        "at": _now(),
                    })
                    counts["conflict"] += 1
                pending[cid] = merged
                continue
            if (card.get("tier") != "uncertain"
                    or int(merged.get("corroborations", 1)) >= _CORROBORATE_MIN):
                # 新卡本身夠格（likely/confirmed），或第二支不同影片佐證了
                # uncertain → 進卡庫（帶著 pending 份的來源與佐證數）。
                promoted_from_uncertain = False
                if merged.get("tier") == "uncertain":
                    merged["tier"] = "likely"
                    merged["promoted_by"] = "corroboration"
                    counts["auto_promoted"] += 1
                    promoted_from_uncertain = True
                merged["status"] = "active"
                ledger[cid] = merged
                del pending[cid]
                # 升級階梯一次一階：uncertain 剛靠這份佐證升 likely，同一份
                # 佐證不得再直升 confirmed（要第三支影片才行）。
                if not promoted_from_uncertain and _maybe_promote(ledger[cid]):
                    counts["auto_confirmed"] += 1
                to_store.append(ledger[cid])
            else:
                pending[cid] = merged
            continue

        if card.get("tier") == "uncertain" and cid not in ledger:
            pending[cid] = card
            counts["uncertain"] += 1
            continue

        if cid in ledger:
            old = ledger[cid]
            fresh = _new_videos(old, card)
            # 只有「不同影片」講出矛盾數值才算衝突（同影片重跑的抽取抖動不算）。
            if fresh and _details_conflict(old.get("detail", ""),
                                           card.get("detail", "")):
                merged = _merge_card(old, card)
                merged["status"] = "conflict"
                merged["tier"] = "uncertain"
                merged.setdefault("conflicts", []).append({
                    "video_id": next(iter(fresh), ""),
                    "detail": card.get("detail", ""),
                    "at": _now(),
                })
                pending[cid] = merged
                del ledger[cid]
                to_delete.append(cid)
                counts["conflict"] += 1
                continue
            ledger[cid] = _merge_card(old, card)
            counts["merged"] += 1
        else:
            ledger[cid] = card
            counts[card.get("tier", "likely")] = counts.get(
                card.get("tier", "likely"), 0) + 1
        if _maybe_promote(ledger[cid]):
            counts["auto_confirmed"] += 1
        to_store.append(ledger[cid])

    _save(_CARDS_PATH, ledger)
    _save(_PENDING_PATH, pending)
    for cid in to_delete:
        _delete_from_store(cid)
    # 以 ledger 現況過濾：同批稍後被衝突降級的卡（to_store 裡的 stale 參照）
    # 不得重回索引；同 cid 只寫一次、取 ledger 最終狀態。
    upserted: set[str] = set()
    for card in to_store:
        cid = card["card_id"]
        if cid in upserted or cid not in ledger:
            continue
        upserted.add(cid)
        _upsert_store(ledger[cid])
    return {"ok": True, **counts,
            "total_active": len(ledger), "total_pending": len(pending)}


# ────────────────────────────────────────────────────────────────────
# LLM tools
# ────────────────────────────────────────────────────────────────────

def search_skill_cards(query: str, k: int = 5) -> str:
    """查影片知識卡 — 從教學影片抽出、帶信度分級與影片溯源的單條知識主張。

    跟 search_operation_sops 的分工：那個回「整段 SOP 文章」（完整步驟流程），
    這個回「一條條主張」（具體參數/欄位值/術語/單一步驟），查點狀事實用這個。
    每筆帶分級：✅confirmed（旁白×畫面雙印證或多影片佐證）/ 🟡likely（單來源
    但明確）。uncertain 的主張不在庫裡（等人工核可）— 查不到 ≠ 不存在，
    別因此編造答案。

    query: 自然語言問題，例如「收櫃單號欄位要填什麼」「FTE_570 是哪個畫面」
    k:     回傳前幾筆（1-20，預設 5）

    回傳：markdown，每筆含分級、信度、主張、細節、來源影片與時間點。
    """
    q = (query or "").strip()
    if not q:
        return "錯誤：query 不能為空。"
    n = max(1, min(20, int(k)))

    from agent_core.ingest.vector_store import get_store

    store = get_store(_COLLECTION)
    if store.count() == 0:
        return "知識卡庫還是空的 — 還沒有教學影片被抽成知識卡。"

    caller = current_request_caller()
    where = access_where(caller)
    hits = store.query(q, n_results=n, where=where)
    log_rag_access_event(
        caller=caller, collection=_COLLECTION, query=q, status="ok",
        n_results=n, hit_count=len(hits), where=where,
        trace_id=current_request_trace_id(),
    )
    if not hits:
        return f"知識卡庫裡沒找到跟「{q}」相關的主張。"

    from agent_core.prompt_injection import sanitize_for_llm

    lines = [f"找到 {len(hits)} 張相關知識卡（query: {q!r}）", ""]
    for i, h in enumerate(hits, 1):
        meta = h.get("metadata") or {}
        tier = str(meta.get("tier", "likely"))
        icon = _TIER_ICON.get(tier, "🟡")
        sim = max(0.0, 1.0 - float(h.get("distance", 1.0)))
        prov = [f"tier={tier}", f"conf={float(meta.get('confidence', 0)):.2f}",
                f"sim={sim:.2f}"]
        corr = int(meta.get("corroborations", 1) or 1)
        if corr > 1:
            prov.append(f"佐證影片×{corr}")
        vid = str(meta.get("video_id") or "")
        if vid:
            loc = f"影片id={vid}"
            if meta.get("start_s") is not None:
                loc += f" @{_fmt_ts(meta.get('start_s'))}"
            prov.append(loc)
        text = sanitize_for_llm((h.get("text") or "").strip()[:_MAX_CARD_CHARS])
        lines.append(f"【{i}】{icon} {' / '.join(prov)}")
        lines.append("    " + text.replace("\n", "\n    "))
        lines.append("")
    return "\n".join(lines).rstrip()


def list_pending_skill_cards(limit: int = 10) -> str:
    """列出等人工核可的 uncertain 知識卡（不在搜尋庫裡的那些）。

    每張卡印 card_id、主張、細節、信度、來源影片與時間點、原文引句。
    大王核完用 resolve_skill_card(card_id, "approve"/"reject") 放行或退回。
    limit: 1-50，預設 10。
    """
    from agent_core.prompt_injection import sanitize_for_llm

    pending = _load(_PENDING_PATH)
    if not pending:
        return "沒有待核可的知識卡。"
    n = max(1, min(50, int(limit)))
    items = sorted(pending.values(),
                   key=lambda c: str(c.get("created_at", "")))[:n]
    lines = [f"待人工核可的知識卡 {len(items)} 張（共 {len(pending)} 張）：", ""]
    for c in items:
        src = (c.get("sources") or [{}])[0]
        loc = str(src.get("video_name") or src.get("video_id") or "?")
        if src.get("start_s") is not None:
            loc += f" @{_fmt_ts(src.get('start_s'))}"
        lines.append(f"• {c.get('card_id')}  conf={float(c.get('confidence', 0)):.2f}"
                     f"  來源：{sanitize_for_llm(loc)}")
        lines.append(f"  主張：{sanitize_for_llm(str(c.get('claim', '')))}")
        if c.get("detail"):
            lines.append(f"  細節：{sanitize_for_llm(str(c.get('detail', ''))[:200])}")
        if src.get("quote"):
            lines.append(f"  引句：{sanitize_for_llm(str(src.get('quote', '')))}")
        if c.get("status") == "conflict":
            for cf in (c.get("conflicts") or [])[:3]:
                lines.append(
                    f"  ⚠️ 數值衝突：影片 {sanitize_for_llm(str(cf.get('video_id', '?')))} "
                    f"說「{sanitize_for_llm(str(cf.get('detail', ''))[:100])}」"
                    "（與上方細節矛盾，請裁決哪邊對）")
        lines.append("")
    lines.append("核可：resolve_skill_card(card_id, \"approve\")；"
                 "退回：resolve_skill_card(card_id, \"reject\")；"
                 "衝突卡採納新影片數值：resolve_skill_card(card_id, \"approve_new\")")
    return "\n".join(lines).rstrip()


def resolve_skill_card(card_id: str, decision: str) -> str:
    """人工核可/退回一張待核知識卡 — uncertain/conflict 卡進搜尋庫的唯一通道。

    decision="approve"：以卡上現有細節（衝突卡=舊影片那邊）升 likely 進卡庫；
    之後有第二支影片佐證會再自動升 confirmed。
    decision="approve_new"：**衝突卡專用** — 採納最新衝突紀錄那支影片的細節
    （覆寫卡上細節）後升 likely 進卡庫。
    decision="reject"：從待核佇列移除（主張錯誤或無價值）。

    card_id: list_pending_skill_cards 列出的 sc_ 開頭 id。
    """
    cid = (card_id or "").strip()
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "approve_new", "reject"):
        return "錯誤：decision 只接受 approve / approve_new / reject。"
    with _ledger_lock():
        return _resolve_skill_card_locked(cid, decision)


def _resolve_skill_card_locked(cid: str, decision: str) -> str:
    pending = _load(_PENDING_PATH)
    card = pending.pop(cid, None)
    if card is None:
        return f"待核佇列裡沒有 {cid} — 用 list_pending_skill_cards 看現有的。"

    if decision == "reject":
        _save(_PENDING_PATH, pending)
        logger.info("skill_card %s 被人工退回", cid)
        return f"已退回 {cid}（不進卡庫）。剩 {len(pending)} 張待核。"

    conflicts = card.pop("conflicts", None) or []
    if decision == "approve_new":
        if not conflicts:
            return (f"{cid} 沒有衝突紀錄，approve_new 無新值可採納 — "
                    "一般核可請用 approve。")
        card["detail"] = str(conflicts[-1].get("detail", "")).strip()
    card["tier"] = "likely"
    card["status"] = "active"
    card["updated_at"] = _now()
    ledger = _load(_CARDS_PATH)
    final = _merge_card(ledger[cid], card) if cid in ledger else card
    if decision == "approve_new":
        final["detail"] = card["detail"]  # _merge_card 保留舊細節，裁決結果要蓋回去
    final.pop("conflicts", None)  # 裁決完就清，別讓已解衝突誤導下次判讀

    # 先寫索引再存檔：索引失敗時所有檔案未動、卡留在待核佇列可重試
    # （反過來會產生「ledger 有、索引永遠沒有」的查不到卡）。
    try:
        _upsert_store(final)
    except Exception as exc:
        logger.warning("skill_card %s 核可時索引寫入失敗（%s），維持待核可重試",
                       cid, exc)
        return (f"❌ 搜尋索引寫入失敗（chroma 可能離線）：{exc}\n"
                f"{cid} 仍在待核佇列，稍後再核一次即可。")
    ledger[cid] = final
    _save(_CARDS_PATH, ledger)
    _save(_PENDING_PATH, pending)
    from agent_core.prompt_injection import sanitize_for_llm

    picked = "（採納新影片數值）" if decision == "approve_new" else ""
    return (f"已核可 {cid} → likely 進卡庫{picked}："
            f"{sanitize_for_llm(str(final.get('claim', '')))}\n"
            f"剩 {len(pending)} 張待核。")
