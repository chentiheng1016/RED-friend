"""工廠世界模型（Phase 5）— 一次呼叫拿到「工廠現在的狀態」快照。

RAG 是被動全文索引：查詢時才挖歷史文件，回答「為什麼／過去怎樣」。
營運的「現在」（今天產量、誰落後、料到了沒、樣品死線）散在多個結構化
讀表工具裡，每個都要單獨呼叫、單獨等 Drive 下載。世界模型把它們聚合成
一個帶 TTL 快取的快照：**世界模型回答「現在」、RAG 回答「為什麼」**。

⚠️ 分支現實（刻意的防禦式設計）：除 samples（sample_tracker，main 也有）
外，其餘節的結構化工具只存在於 css 部署分支（factory_production_report /
production_schedule / factory_warehouse_stock / material_arrival 都不在
origin/main）。本模組用 importlib 延遲載入 + 逐節容錯——css（live 艦隊）
上全功能；main（CI / 新 worktree）上對應節顯示「模組不在此分支」，
不炸 import、不掛測試。

快取：var/state/factory_world_model.json（locked_json），逐節 TTL。
節失敗時保留上一次成功快照並標注過期（stale-if-error）——Drive 偶發
403/timeout 不該讓整個「現在」變空白。
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime, timezone
from typing import Any, Callable

from agent_core.logging_and_paths import STATE_DIR, logger
from agent_core.state_io import locked_json

CACHE_FILE = os.path.join(STATE_DIR, "factory_world_model.json")

_MAX_SECTION_CHARS = 3500  # 單節上限——五節全開也不該炸掉 LLM context


def _css_tool(module: str, func: str, **kwargs) -> Callable[[], str]:
    """回傳「延遲 import 再呼叫」的 loader。模組不在（main 分支）→
    ImportError 由呼叫端統一轉成人類可讀的分支說明。"""
    def _loader() -> str:
        mod = importlib.import_module(module)
        return str(getattr(mod, func)(**kwargs))
    return _loader


# key → (中文標題, loader, ttl 秒, 是否進預設集)
_SECTIONS: dict[str, tuple[str, Callable[[], str], int, bool]] = {
    "production": (
        "生產進度（生管日報）",
        _css_tool("agent_core.factory_production_report", "read_production_progress_sheet"),
        1800, True,
    ),
    "schedule": (
        "生產排程看板",
        _css_tool("agent_core.production_schedule", "production_dashboard"),
        1800, True,
    ),
    "alerts": (
        "生產落後示警",
        _css_tool("agent_core.production_schedule", "production_alert"),
        1800, True,
    ),
    "material": (
        "料批到貨現況",
        _css_tool("agent_core.material_arrival", "query_material_arrival"),
        1800, True,
    ),
    "samples": (
        "樣品死線",
        _css_tool(
            "agent_core.agents.green_sample_dev.sample_tracker",
            "check_sample_deadlines",
            auto_draft_followup=False,  # 快照唯讀：不建 draft、不推播
            push_telegram=False,
        ),
        1800, True,
    ),
    # 倉庫最重（最多掃 400 個料分頁）——不進預設集，要看要明點或用 "all"
    "warehouse": (
        "倉庫庫存",
        _css_tool("agent_core.factory_warehouse_stock", "read_warehouse_stock"),
        7200, False,
    ),
}


def _resolve_sections(raw: str) -> list[str]:
    s = (raw or "").strip().lower()
    if not s:
        return [k for k, (_t, _l, _ttl, default) in _SECTIONS.items() if default]
    if s == "all":
        return list(_SECTIONS.keys())
    picked = [p.strip() for p in s.split(",") if p.strip()]
    return [p for p in picked if p in _SECTIONS]


def _age_label(at_iso: str, now: datetime) -> str:
    try:
        at = datetime.fromisoformat(at_iso)
        mins = int(max(0.0, (now - at).total_seconds()) // 60)
    except (ValueError, TypeError):
        return "時間未知"
    if mins < 1:
        return "剛更新"
    if mins < 60:
        return f"{mins} 分鐘前"
    return f"{mins // 60} 小時 {mins % 60} 分前"


def _load_section(
    key: str, refresh: bool, now: datetime, deadline_monotonic: float | None = None,
) -> tuple[str, str]:
    """回 (內容, 狀態行)。快取新鮮直接用；否則現抓；抓失敗回舊快取標 stale。

    deadline_monotonic：時間預算（time.monotonic 基準）。超過預算就不再
    現抓——回舊快照或「時間用盡」——把 factory_now 的最壞耗時鎖在 tool
    timeout 之下（冷呼叫多節序列下載 Drive 報表可能各要數十秒）。"""
    title, loader, ttl, _default = _SECTIONS[key]
    cached: dict[str, Any] = {}
    try:
        # except Exception 非 OSError：快取檔若被寫成「合法 JSON 但不是
        # dict of dict」（手改壞/半寫），cache.get / dict() 會丟
        # AttributeError/TypeError/ValueError——不能讓工具從此永久炸掉。
        with locked_json(CACHE_FILE, default={}) as cache:
            raw = cache.get(key) if isinstance(cache, dict) else None
            cached = dict(raw) if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.debug("世界模型快取讀取失敗（%s）：%s", key, exc)
        cached = {}

    fresh_enough = False
    if cached.get("text") and not refresh:
        try:
            at = datetime.fromisoformat(str(cached.get("at") or ""))
            fresh_enough = (now - at).total_seconds() < ttl
        except (ValueError, TypeError):
            fresh_enough = False
    if fresh_enough:
        return str(cached["text"]), f"快取（{_age_label(str(cached.get('at')), now)}）"

    import time as _time
    if deadline_monotonic is not None and _time.monotonic() > deadline_monotonic:
        if cached.get("text"):
            return (
                str(cached["text"]),
                f"⏱️ 時間預算用盡，顯示舊快照（{_age_label(str(cached.get('at')), now)}）",
            )
        return "（時間預算用盡、無舊快照——單獨呼叫本節對應工具，或稍後 refresh）", "⏱️ 略過"

    try:
        text = (loader() or "").strip()
        if len(text) > _MAX_SECTION_CHARS:
            text = text[:_MAX_SECTION_CHARS] + "…（節錄，要完整內容請直接呼叫對應工具）"
        try:
            with locked_json(CACHE_FILE, default={}) as cache:
                if not isinstance(cache, dict):
                    raise TypeError("cache file is not a dict")
                cache[key] = {"text": text, "at": now.isoformat(timespec="seconds"), "ok": True}
        except Exception as exc:
            logger.warning("世界模型快取寫入失敗（%s）：%s", key, exc)
        return text, "剛更新"
    except ImportError:
        return (
            "（此節的結構化工具只存在於 css 部署分支——目前程式碼分支沒有"
            "對應模組，live 艦隊上不會看到這則訊息）",
            "模組不在此分支",
        )
    except Exception as exc:
        logger.warning("世界模型節載入失敗（%s）：%s", key, exc)
        if cached.get("text"):
            return (
                str(cached["text"]),
                f"⚠️ 本次更新失敗（{type(exc).__name__}），顯示舊快照"
                f"（{_age_label(str(cached.get('at')), now)}）",
            )
        return f"（載入失敗：{type(exc).__name__}: {str(exc)[:200]}）", "⚠️ 失敗且無舊快照"


def factory_now(sections: str = "", refresh: bool = False) -> str:
    """【工廠世界模型】一次拿到工廠「現在」的營運狀態快照——生產進度、排程
    看板、落後示警、料批到貨、樣品死線（各節帶快取時戳）。

    什麼時候用：被問「**現在／今天／目前**工廠狀況如何」「有什麼要注意的」
    「XX 進度到哪了」這類**當下狀態**問題時，先呼叫這個拿全貌，再視需要
    用單項工具深挖。歷史脈絡與「為什麼」仍用 search_drive_docs /
    search_reflections 等 RAG 工具——世界模型答「現在」、RAG 答「為什麼」。

    sections: 逗號分隔的節名，空=預設集（production,schedule,alerts,material,
              samples）。"warehouse"（倉庫庫存，最重、掃數百分頁）不在預設集，
              要看明點或用 "all"。
    refresh:  True=無視快取強制重抓（各節要重新下載 Drive 報表，可能等
              1-2 分鐘）。預設 False——30 分鐘內的快取直接用，幾乎即時。

    回傳：markdown 快照，每節帶「多久前更新」；某節暫時抓不到會顯示舊快照
    並標注。引用數字時請一併講快照時間（「30 分鐘前的快照顯示…」）。
    """
    keys = _resolve_sections(sections)
    if not keys:
        valid = "、".join(_SECTIONS.keys())
        return f"錯誤：sections 沒有任何有效節名。可用：{valid}，或 'all'。"
    now = datetime.now(timezone.utc)
    # 時間預算：冷呼叫時每節都要下載 Drive 報表（生管日報 8MB+ 且
    # production 與 schedule/alerts 分屬不同模組、會各下載一份），最壞
    # 疊起來可能撞 tool timeout。預算內先抓、超過的節回舊快照/略過。
    import time as _time
    from agent_core.env_utils import env_int
    budget_s = env_int("RED_WORLD_MODEL_TIME_BUDGET_S", 180, min_value=10, max_value=3600)
    deadline = _time.monotonic() + budget_s
    local_stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"🏭 工廠現況快照（{local_stamp}）", ""]
    for key in keys:
        title = _SECTIONS[key][0]
        text, status = _load_section(key, refresh, now, deadline_monotonic=deadline)
        lines.append(f"## {title}｜{status}")
        lines.append(text)
        lines.append("")
    lines.append(
        "💡 世界模型答「現在」；歷史與「為什麼」用 search_drive_docs / "
        "search_reflections。要最新數字加 refresh=True（會重新下載報表）。"
    )
    return "\n".join(lines).rstrip()
