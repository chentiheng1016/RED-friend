"""Entity canonicalization（R3）— 把「Blaklader / BLAKLADER / AB Blåkläder /
blaklader.com」這種同一個實體的多種寫法歸一。

沒歸一前：
  timeline_by_customer("Blaklader") 只抓 customer 欄位等於「Blaklader」的列，
  等於「BLAKLADER」的被漏掉、等於「AB Blåkläder」也被漏掉

歸一後：
  → resolve_customer("Blaklader") 回 ["Blaklader", "BLAKLADER", "AB Blåkläder",
     "blaklader.com", "Blaklader Sweden"] 等全部 alias
  → timeline_by_customer 用 alias 集合 match，抓得更完整

Alias table 存 `var/data/entity_aliases.json`，結構：
  {
    "customers": {
      "blaklader": {  # canonical (lowercase slug)
        "display": "Blaklader",  # 人類可讀
        "aliases": ["Blaklader", "BLAKLADER", "AB Blåkläder", "blaklader.com"],
        "count": 357  # 出現總次數
      },
      "lurchi": {...},
      ...
    },
    "products": {...},
    "suppliers": {...}
  }

建表有 2 個策略：
  1. 規則式 normalize（lowercase、拿掉 ".com"、拿掉公司後綴如 Ltd. / 有限公司 等）
  2. 手動 override（人工看過列表、合併邊界案例）

表一次性 build 好存檔，之後讀檔用。daily_delta 不觸發重建（表變得很慢時才需要）。
"""
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from agent_core.logging_and_paths import INTERNAL_LAKE_DIR, logger, _atomic_write_text


_INTERNAL_PARQUET = os.path.join(INTERNAL_LAKE_DIR, "emails.parquet")
_ALIAS_FILE = os.path.join(INTERNAL_LAKE_DIR, "entity_aliases.json")


# 公司後綴：歸一化時移除
_COMPANY_SUFFIXES = [
    # 英文
    r"\s+(ltd|limited|co\s*,?\s*ltd|corp|corporation|inc|incorporated|"
    r"llc|gmbh|s\.?a\.?|s\.?r\.?l\.?|b\.?v\.?|pvt\.?\s*ltd)\.?$",
    # 中文
    r"\s*(股份有限公司|有限公司|股份公司|集團|企業|實業|公司)$",
    # Domain
    r"\.com$", r"\.com\.tw$", r"\.co$", r"\.tw$", r"\.net$", r"\.org$", r"\.hk$",
]


def _normalize_entity_name(name: str) -> str:
    """把 entity name 壓成 canonical slug（用來 group variants）。

    規則：
      1. NFKC normalize (全形半形、unicode 規範化)
      2. lowercase
      3. 拿掉標點
      4. 拿掉公司後綴（見 _COMPANY_SUFFIXES）
      5. 壓縮空白
    """
    if not name:
        return ""
    s = str(name).strip()
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    # 先拿掉後綴（在去標點前，後綴常含標點）
    for pattern in _COMPANY_SUFFIXES:
        s = re.sub(pattern, "", s, flags=re.IGNORECASE)
    # 重音字 → ASCII（Blåkläder → Blaklader）
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    # 移除剩下的標點 / 多餘空白
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_alias_table(min_count: int = 2, entity_types: list[str] = None) -> str:
    """掃 parquet 內所有 entities，按 normalized slug 分組，輸出 alias table。

    這是**一次性**離線操作，建完存檔之後讀檔即可。新資料進來後
    可偶爾重跑這個 function 更新（daily_delta 不會自動觸發）。

    Args:
        min_count: 某個 entity 總次數要 ≥ 這個數才進表（避免 noise）。
        entity_types: 哪幾類 entity 要處理；空 = 全部（customers, suppliers, products, people）。
                      一般建議只對 customers/suppliers 做，people 太吵。

    Returns:
        human-readable summary（建完多少組、存哪裡）。
    """
    import pandas as pd

    if not os.path.exists(_INTERNAL_PARQUET):
        return f"❌ 找不到 parquet：{_INTERNAL_PARQUET}"

    types_to_process = entity_types or ["customers", "suppliers", "products"]
    df = pd.read_parquet(_INTERNAL_PARQUET)

    # entity_type → slug → [原始名稱 list]
    buckets: dict[str, dict[str, list]] = {t: defaultdict(list) for t in types_to_process}
    raw_counts: dict[str, Counter] = {t: Counter() for t in types_to_process}

    for _, row in df.iterrows():
        try:
            ents = json.loads(row.get("entities_json") or "{}")
        except (ValueError, TypeError):
            continue
        for etype in types_to_process:
            items = ents.get(etype) or []
            if not isinstance(items, list):
                continue
            for name in items:
                name = str(name or "").strip()
                if not name or len(name) < 2:
                    continue
                slug = _normalize_entity_name(name)
                if not slug:
                    continue
                buckets[etype][slug].append(name)
                raw_counts[etype][name] += 1

    # 組出最終表
    out: dict[str, dict] = {}
    for etype, groups in buckets.items():
        out[etype] = {}
        for slug, names in groups.items():
            # 計算 group 總次數
            total = sum(raw_counts[etype][n] for n in names)
            if total < min_count:
                continue
            # 選 display name：原始中出現最多次 且 最長的
            name_counts = Counter(names)
            # 先按出現次數排，同次數選最長的
            sorted_names = sorted(
                name_counts.keys(),
                key=lambda n: (-name_counts[n], -len(n), n)
            )
            display = sorted_names[0]
            unique_aliases = sorted(set(names),
                                      key=lambda n: (-name_counts[n], n))
            out[etype][slug] = {
                "display": display,
                "aliases": unique_aliases,
                "count": total,
            }

    # 存檔
    os.makedirs(os.path.dirname(_ALIAS_FILE), exist_ok=True)
    # 原子寫：直寫被 SIGKILL/斷電撕裂 → _load_aliases 靜默回空 dict →
    # resolve_entity 全部查不到，要等下次有人重跑 build_alias_table 才復原。
    _atomic_write_text(_ALIAS_FILE, json.dumps(out, ensure_ascii=False, indent=2))

    # 報告
    lines = [f"✅ Alias table 建完 → {_ALIAS_FILE}"]
    for etype, groups in out.items():
        total_groups = len(groups)
        total_aliases = sum(len(g["aliases"]) for g in groups.values())
        compression = (1 - total_groups / max(1, total_aliases)) * 100
        lines.append(f"  • {etype}: {total_groups} 組（涵蓋 {total_aliases} 個 variants，"
                     f"壓縮 {compression:.0f}%）")
        # top 5 最多 alias 的
        sorted_groups = sorted(groups.items(), key=lambda kv: -len(kv[1]["aliases"]))[:3]
        for slug, info in sorted_groups:
            if len(info["aliases"]) >= 3:
                lines.append(f"     例：{info['display']} → "
                             + ", ".join(info["aliases"][:6])
                             + (" ..." if len(info["aliases"]) > 6 else ""))
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 讀 alias table + resolve API
# ────────────────────────────────────────────────────────────────────
_alias_cache: dict = {"data": None, "mtime": 0.0}


def _load_aliases() -> dict:
    """讀 alias table，檔不存在就回空 dict。Mtime 變了自動重載。"""
    if not os.path.isfile(_ALIAS_FILE):
        return {}
    mtime = os.path.getmtime(_ALIAS_FILE)
    if _alias_cache["data"] is not None and _alias_cache["mtime"] == mtime:
        return _alias_cache["data"]
    try:
        with open(_ALIAS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _alias_cache.update(data=data, mtime=mtime)
        return data
    except Exception as e:
        logger.warning("讀 alias table 失敗：%s", e)
        return {}


def resolve_entity(name: str, entity_type: str = "customers") -> dict:
    """查詢一個 entity 的所有 alias 與 canonical display name。

    Args:
        name: 使用者問的名字（任何 variant 都 OK）。
        entity_type: customers / suppliers / products。

    Returns:
        {
          "canonical": "Blaklader",
          "aliases": ["Blaklader", "BLAKLADER", "AB Blåkläder", ...],
          "count": 357,
          "found": True
        }
        找不到時 found=False，aliases 只含原名。
    """
    table = _load_aliases()
    bucket = (table.get(entity_type) or {})
    slug = _normalize_entity_name(name)
    if slug and slug in bucket:
        info = bucket[slug]
        return {
            "canonical": info["display"],
            "aliases": info["aliases"],
            "count": info["count"],
            "found": True,
        }
    # Fallback：回原名
    return {
        "canonical": name,
        "aliases": [name],
        "count": 0,
        "found": False,
    }


def list_entity_aliases(entity_type: str = "customers",
                         top_n: int = 20, min_aliases: int = 2) -> str:
    """列 entity alias table 裡 variant 最多的幾個 group（看 canonicalization 有沒有用）。

    Args:
        entity_type: customers / suppliers / products。
        top_n: 顯示前幾名（按 alias 數排，多 alias 代表這個 entity 在資料裡寫法最亂）。
        min_aliases: 只列 ≥ N 個 variants 的 group（1 個沒必要列）。
    """
    table = _load_aliases()
    if not table:
        return ("❌ Alias table 還沒建。先跑 build_alias_table() 一次。"
                f"\n（預期檔案：{_ALIAS_FILE}）")
    bucket = table.get(entity_type) or {}
    if not bucket:
        return f"❌ 沒有 {entity_type} 的 alias 資料。可用 types: {list(table.keys())}"

    # 篩 + 排
    filtered = [(slug, info) for slug, info in bucket.items()
                if len(info["aliases"]) >= min_aliases]
    filtered.sort(key=lambda kv: -kv[1]["count"])

    lines = [f"📚 {entity_type} alias groups（alias ≥ {min_aliases} 的 top {top_n}）"]
    for slug, info in filtered[:top_n]:
        lines.append(f"\n🔗 {info['display']}（共 {info['count']} 次）")
        for alias in info["aliases"][:8]:
            lines.append(f"     • {alias}")
        if len(info["aliases"]) > 8:
            lines.append(f"     ...（還有 {len(info['aliases']) - 8} 個 variant）")
    return "\n".join(lines)


def entity_stats() -> str:
    """Alias table 總覽：各 entity_type 幾組、總涵蓋 variants、壓縮率。"""
    table = _load_aliases()
    if not table:
        return "❌ Alias table 還沒建。跑 build_alias_table() 先。"
    lines = [f"📊 Entity alias table ({_ALIAS_FILE})"]
    for etype, groups in table.items():
        total_groups = len(groups)
        total_aliases = sum(len(g["aliases"]) for g in groups.values())
        total_occ = sum(g.get("count", 0) for g in groups.values())
        comp = (1 - total_groups / max(1, total_aliases)) * 100
        lines.append(
            f"  • {etype}: {total_groups} 組  |  "
            f"{total_aliases} variants  |  {total_occ} 總出現次數  |  "
            f"壓縮 {comp:.0f}%"
        )
    return "\n".join(lines)
