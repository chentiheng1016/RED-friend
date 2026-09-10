"""Query expansion（R9）— 搜尋前先用 LLM 擴充同義詞 + 翻譯。

Why:
  使用者問「工作靴」但檔案裡可能是「工作鞋」/「safety boot」/
  「labor footwear」。現在的 hybrid retrieval 只比對字面形式，
  擴充後 recall 覆蓋面立刻變大。

How:
  1. 使用者 query: "工作靴要求 PFAS 合規"
  2. LLM 擴：同義詞 + 翻譯 + 產業術語
     → ["工作靴", "工作鞋", "safety boot", "safety shoes",
        "PFAS", "全氟化合物", "PFOA", "合規", "compliance"]
  3. 組成「擴充 query」丟進 recall

成本：~1 次 flash-lite call，$0.00005 per expansion。
     超過 2 字元的 query 才做擴充（短查詢無意義）。

Cache:
  同樣的 query 2 次不重跑 LLM（thread-local 快取，TTL 10 分鐘）。
"""
import json
import re
import threading
import time
from typing import Any

from agent_core.gemini_client import _gemini_generate
from agent_core.logging_and_paths import logger


_EXPAND_MODEL = "gemini-2.5-flash-lite"
_CACHE_TTL_SEC = 600
_MIN_QUERY_LEN = 3
# 快取上限：過期項寫入時順手清 + FIFO 淘汰保險。舊版過期項永不清除，
# 長駐 daemon 每個獨特 query 都留一筆 → 無上限記憶體洩漏。
_CACHE_MAX_ENTRIES = 512

# Thread-local cache
_cache_lock = threading.Lock()
_expand_cache: dict[str, tuple[str, float]] = {}  # query → (expanded, ts)


_EXPAND_PROMPT = """你是搜尋 query 的擴充器。幫以下 query 擴出同義詞、英中互譯、
產業（台灣鞋廠）常用術語。

規則：
  1. **只擴確定等價或高度相關的詞**，不要亂加
  2. 中文 query 要加英文翻譯；英文 query 要加中文翻譯
  3. 鞋廠領域：鞋款款式（工作鞋/童鞋/涼鞋）、合規縮寫（PFAS/REACH/PFOA/PFOS）、
     incoterm（FOB/CIF/EXW）、業務動作（報價/議價/PO/出貨/對帳）等都可加
  4. 公司名不擴（那個 entity_resolver 會處理）
  5. 最多 8 個擴充詞

原始 query:
{query}

只輸出 JSON（不要 markdown）：
{{"original": "原 query", "expanded": ["詞 1", "詞 2", ...]}}
"""


def expand_query(query: str, disable: bool = False) -> str:
    """擴充 query 成含同義詞 + 翻譯的搜尋字串。

    Args:
        query: 原始使用者 query。
        disable: True 時不擴充、直接回原 query（給測試 / 避免額外成本）。
    Returns:
        擴充後的字串，形如「原 query 擴 1 擴 2 擴 3」，直接丟給 recall。
        失敗或 query 太短時回原 query 不擴。
    """
    q = (query or "").strip()
    if not q or len(q) < _MIN_QUERY_LEN or disable:
        return q

    # Cache check
    with _cache_lock:
        cached = _expand_cache.get(q)
        if cached and (time.time() - cached[1]) < _CACHE_TTL_SEC:
            return cached[0]

    prompt = _EXPAND_PROMPT.format(query=q)
    try:
        resp = _gemini_generate(model=_EXPAND_MODEL, contents=[prompt])
        text = (resp.text or "").strip()
    except Exception as e:
        logger.debug("query_expansion 呼叫失敗：%s", e)
        return q

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return q
    try:
        parsed = json.loads(m.group(0))
        expanded_terms = parsed.get("expanded") or []
        if not isinstance(expanded_terms, list):
            return q
    except Exception:
        return q

    # 清洗：去空、去完全等於原 query 的詞、限長度
    seen = {q.lower()}
    clean = [q]
    for term in expanded_terms[:10]:
        t = str(term or "").strip()
        if not t or t.lower() in seen or len(t) > 60:
            continue
        seen.add(t.lower())
        clean.append(t)

    # 用空格拼起來丟 recall（embed + BM25 都能吃到擴充詞的字面）
    expanded = " ".join(clean)

    with _cache_lock:
        now = time.time()
        # 順手掃掉已過期項（不然它們永遠留在 dict 裡）
        for k in [k for k, (_, ts) in _expand_cache.items()
                  if now - ts >= _CACHE_TTL_SEC]:
            _expand_cache.pop(k, None)
        _expand_cache[q] = (expanded, now)
        # 上限保險：仍超過就 FIFO 淘汰最舊（dict 保插入序）
        while len(_expand_cache) > _CACHE_MAX_ENTRIES:
            _expand_cache.pop(next(iter(_expand_cache)), None)

    return expanded


def preview_expansion(query: str) -> str:
    """給大王查看擴充會生出什麼（debug 用）。"""
    q = (query or "").strip()
    if not q:
        return "❌ query 不能空"
    if len(q) < _MIN_QUERY_LEN:
        return f"⚠️ query 太短（< {_MIN_QUERY_LEN} 字），不擴充；原樣使用"

    expanded = expand_query(q)
    if expanded == q:
        return f"🔍 無擴充（可能 LLM 判斷沒可擴詞）\n原 query: {q}"

    extras = expanded[len(q):].strip().split()
    return (
        f"🔍 Query 擴充預覽\n"
        f"  原 query:  {q}\n"
        f"  +{len(extras)} 個擴充詞: {', '.join(extras)}\n"
        f"  完整擴充:  {expanded}"
    )
