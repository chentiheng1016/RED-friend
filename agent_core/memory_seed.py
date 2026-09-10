"""Version-controlled memory seed — curated facts that follow the repo.

問題
----
save_memory() / remember() 寫到 var/state/memory.json + var/data/chroma_db，
兩者都是 **gitignored 的執行期狀態**。大王在 Mac REPL 打的一條更正
（「JAIFUNG 中文名是『佳紘有限公司』，不是『佳豐』」）只活在那台機器上 —
fresh clone、另一台工作站、雲端 session 都看不到。以前沒有一個「版本控管、
會跟著 repo 走」的地方放這種應該永久跟著專案的事實。

解法
----
repo 根目錄的 ``memory_seed.json`` 放 curated 的 ``key -> value`` 事實，這個檔
**會被 commit**。啟動時把它合併進執行期 memory.json，並索引進向量庫，這樣
persona bullet（load_startup_memory）跟 recall() 在每台 pull 過 repo 的機器
上都抓得到。

語意
----
* **Seed 優先**：對它定義的 key，committed 檔是刻意的 source of truth。執行期
  才有的 key（live 用 save_memory 加的）保持不動。
* **變更才套用**：把 seed 檔的 hash 記在 per-machine marker
  （var/state/.memory_seed_synced，gitignored）。只有 seed hash 變了（也就是
  ``git pull`` 帶進新的/改過的更正）才會重跑合併。兩次 seed 變更之間，live
  save_memory 的編輯照常保留。
* **Fail open & quiet**：seed 檔不存在/壞掉 = no-op；向量索引是 best-effort
  （chromadb / API key 沒就緒時跳過，下次啟動重試）。

整合點
------
* ``chat_session.load_startup_memory()`` 先呼叫 ``sync_memory_seed()``（只做
  KV 合併，cheap、不碰向量庫）— 確保 persona 在每台機器都看到更正。
* ``agent._main()`` 在 migrate 之後呼叫 ``memory.sync_memory_seed()``（帶
  index fn）— 把 seed 也索引進向量庫供 recall。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable

from agent_core.logging_and_paths import (
    MEMORY_FILE,
    REPO_ROOT,
    STATE_DIR,
    logger,
)
from agent_core.state_io import locked_json

# repo 根的 committed seed 檔。env 可覆蓋（測試 / 多 profile 用）。
SEED_FILE = os.path.join(REPO_ROOT, "memory_seed.json")
# per-machine marker（gitignored）— 記錄上次同步的 seed hash，避免每次啟動重做。
_MARKER_FILE = os.path.join(STATE_DIR, ".memory_seed_synced")


def _seed_path() -> str:
    return os.environ.get("RED_MEMORY_SEED_FILE", SEED_FILE)


def _load_seed() -> dict[str, str]:
    """讀 memory_seed.json，回 {key: str_value}。壞檔 / 缺檔 / 非 dict = {}。"""
    path = _seed_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("memory_seed 讀取失敗（%s）— 跳過 seed 同步", e)
        return {}
    if not isinstance(data, dict):
        logger.warning("memory_seed.json top-level 須為 {key: value} dict — 跳過")
        return {}
    # 只收 str key；value 非 str 就 stringify（容忍 list/dict 值）。
    clean: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not k.strip():
            continue
        clean[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return clean


def _seed_hash(seed: dict[str, str]) -> str:
    blob = json.dumps(seed, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sync_memory_seed(
    index_memory_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """把 committed seed 事實合併進執行期記憶（+ 可選向量索引）。

    Args:
        index_memory_fn: 可選的 ``(text, source, metadata, doc_id) -> doc_id``
            （agent_core.memory._index_memory）。給了就把 seed 也索引進向量庫，
            讓 recall() 找得到。None 時只做 key-value 合併（cheap、無網路）。

    Returns:
        summary dict：kv_applied / vec_applied / skipped / seed_count / hash。

    併發安全（為什麼用 locked_json + 兩段式）：
      多個 daemon + REPL 可能同時啟動並呼叫此函式。marker 的 read-modify-write
      若不上鎖，兩個 writer 各讀舊 marker、各寫回，會 lost-update（例如帶向量
      索引的 _main 寫了 vec_hash，又被只做 KV 的 daemon 用舊 marker 蓋掉）。
      所以 marker 一律走 locked_json（fcntl 跨進程鎖）讀新鮮值再寫。

      但向量索引（index_memory_fn）會打 embedding 網路請求，可能慢甚至卡住。
      若在持有 marker 鎖時做，會卡死其他 daemon 的 load_startup_memory 啟動。
      所以拆兩段：(1) marker 短鎖做 KV 合併 + 判斷是否要索引；(2) 不持鎖做
      向量索引；(3) 成功後再短鎖、讀新鮮 marker 只更新 vec_hash。
    """
    seed = _load_seed()
    if not seed:
        return {"kv_applied": 0, "vec_applied": 0, "skipped": True,
                "reason": "no seed", "seed_count": 0}

    h = _seed_hash(seed)
    kv_applied = 0
    vec_applied = 0
    vec_needed = False

    # ── Phase 1: marker 短鎖 — KV 合併 + 判斷是否要索引（無網路 I/O）──
    try:
        with locked_json(_MARKER_FILE, default={}) as marker:
            if marker.get("kv_hash") != h:
                try:
                    with locked_json(MEMORY_FILE, default={}) as mem:
                        for key, value in seed.items():
                            if mem.get(key) != value:
                                mem[key] = value
                                kv_applied += 1
                    marker["kv_hash"] = h
                    if kv_applied:
                        logger.info("memory_seed：合併 %d 筆種子記憶到 memory.json",
                                    kv_applied)
                except Exception as e:
                    logger.warning("memory_seed KV 合併失敗：%s", e)
            vec_needed = (index_memory_fn is not None
                          and marker.get("vec_hash") != h)
    except Exception as e:
        logger.warning("memory_seed marker 鎖定失敗：%s", e)
        return {"kv_applied": kv_applied, "vec_applied": 0, "skipped": False,
                "seed_count": len(seed), "hash": h}

    # ── Phase 2: 向量索引（不持鎖 — 避免 embedding 網路 I/O 卡住其他啟動）──
    if vec_needed:
        all_ok = True
        for key, value in seed.items():
            doc_id = index_memory_fn(
                f"{key}: {value}",
                source="seed",
                metadata={"seed_key": key},
                doc_id=f"seed-{key}",
            )
            if doc_id:
                vec_applied += 1
            else:
                all_ok = False  # 向量庫沒就緒 → 不更新 vec_hash，下次啟動重試
        # ── Phase 3: 成功才再短鎖更新 vec_hash（讀新鮮 marker，不蓋他人更新）──
        if all_ok:
            try:
                with locked_json(_MARKER_FILE, default={}) as marker:
                    marker["vec_hash"] = h
                logger.info("memory_seed：索引 %d 筆種子記憶到向量庫", vec_applied)
            except Exception as e:
                logger.warning("memory_seed vec_hash 更新失敗：%s", e)

    return {"kv_applied": kv_applied, "vec_applied": vec_applied,
            "skipped": False, "seed_count": len(seed), "hash": h}
