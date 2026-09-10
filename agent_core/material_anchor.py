"""材質錨定：把樣品單的材質詞對應到真實材質特寫圖，當額外輸入圖餵圖生圖。

Phase B（2026-09-01 大王「材質也要模擬出來」）：材質以文字進 prompt，模型只能
靠想像補紋理；附一張該料的真實特寫，紋理就有實物可抄（同色票條的思路：能給
圖就不要給字）。

圖庫是 runtime 資產、不入 git：`var/data/material_swatches/<key>.jpg|png|webp`，
key 見 agent_core/data/material_aliases.json（例：麂皮 → suede → suede.jpg）。
特寫圖拍/裁的要領：平拍、填滿畫面、光線均勻——只當紋理參考，顏色一律以色票
為準（prompt 會明講），所以特寫圖是什麼顏色無所謂。
庫裡沒有的料就不附圖（行為同 Phase B 之前），空庫零風險。
"""
import json
import os

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_SWATCH_EXTS = (".jpg", ".jpeg", ".png", ".webp")

_alias_cache: list | None = None


def _alias_pairs() -> list:
    """[(alias_lower, canonical_key)]，alias 長度降冪——「三明治網布」要贏過「網布」。"""
    global _alias_cache
    if _alias_cache is None:
        try:
            with open(os.path.join(_DATA_DIR, "material_aliases.json"), encoding="utf-8") as f:
                table = json.load(f)
        except (OSError, ValueError):
            table = {}
        pairs = []
        for key, terms in table.items():
            for t in [key, *list(terms or [])]:
                t = str(t).strip().lower()
                if t:
                    pairs.append((t, key))
        _alias_cache = sorted(pairs, key=lambda p: -len(p[0]))
    return _alias_cache


def material_key(text: str) -> str:
    """材質敘述 → 圖庫 canonical key；對不上回 ""。"""
    from agent_core.color_anchor import match_term
    low = str(text or "").strip().lower()
    if not low:
        return ""
    for alias, key in _alias_pairs():
        if match_term(low, alias):
            return key
    return ""


def materials_in_text(text: str) -> list:
    """文字裡提到的所有材質 → [{"key","term"}]（term=命中的別名；同 key 只取最長者）。

    給修正指示（extra_note）用（2026-09-02 PSS 6 案）：「皮料改羊巴戈」只活在
    修正指示、不在逐部位規格裡，點名的料也要能對到特寫圖。
    """
    from agent_core.color_anchor import match_term
    low = str(text or "").strip().lower()
    if not low:
        return []
    seen: set = set()
    out: list = []
    for alias, key in _alias_pairs():  # 已按別名長度降冪
        if key not in seen and match_term(low, alias):
            seen.add(key)
            out.append({"key": key, "term": alias})
    return out


def _swatch_dir() -> str:
    from agent_core.logging_and_paths import DATA_DIR
    return os.path.join(DATA_DIR, "material_swatches")


def swatch_path(key: str) -> str:
    """canonical key → 圖庫檔路徑；庫裡沒這張回 ""。"""
    if not key:
        return ""
    d = _swatch_dir()
    for ext in _SWATCH_EXTS:
        p = os.path.join(d, key + ext)
        if os.path.exists(p):
            return p
    return ""


def material_refs(parts: list, cap: int = 3) -> list:
    """逐部位規格 → 可附的材質特寫清單 [{"key","label","path","parts":[部位…]}]。

    同料多部位去重（一張特寫服務所有用該料的部位）；上限 cap 張——輸入圖太多
    會稀釋線稿與色票的權重。label 取第一個用到該料的部位的原文（prompt 用原文
    比用英文 key 讓模型好對上規格描述）。
    """
    by_key: dict = {}
    order: list = []
    for p in parts or []:
        mat = str(p.get("material", "")).strip()
        key = material_key(mat)
        if not key:
            continue
        path = swatch_path(key)
        if not path:
            continue
        if key not in by_key:
            if len(order) >= cap:
                continue
            by_key[key] = {"key": key, "label": mat, "path": path, "parts": []}
            order.append(key)
        by_key[key]["parts"].append(str(p.get("part", "?")))
    return [by_key[k] for k in order]
