"""Per-tool daily / hourly rate limits.

問題（之前的 V4 + tier 系統還擋不到的場景）：
  - 攻擊者拿到大王 Telegram + 看到大王打 +確認，他能在 90s 內串連發
    `send_gmail`、`send_gmail`、`send_gmail`...（雖然 one-shot，但他可以
    每寄一封信都騙大王再 +確認一次「不是說好要寄 5 封嗎」）
  - 或大王自己一時手抖串太多次（誤觸） — 缺一個「就算授權，也有上限」的
    第二層保險。
  - 燒錢類 tool（generate_image, edit_image, delegate_to_sub_agents_parallel）
    沒上限就是攻擊者拿到帳號可以一夜燒光 quota。

設計：
  - 每個工具有可選 daily / hourly budget。
  - 每次成功執行後 record 一次（counter ++）。檢查在 wrap_sensitive_tool
    成功 confirm 後、呼叫 fn() 之前。
  - 超過 daily 直接拒絕到隔天；超過 hourly 拒到下個整點。
  - 大王可用 env `RED_BUDGET_<TOOL>_DAILY=N` 蓋過預設。
  - 狀態 persisted 到 var/data/tool_budgets/YYYY-MM-DD.json，每天一份。
    舊檔超過 30 天自動清。

設計權衡：
  - 為什麼日 / 時兩個 window，不只日？send_gmail 一天 30 封是合理量，但
    「30 封都在同一小時內寄完」幾乎一定是 bug 或攻擊。雙 window 同擋。
  - 為什麼 file-based 不 in-memory？daemon restart / process crash 不能
    reset budget — 那會變成「攻擊者讓 process crash 來繞過」。
  - 為什麼不用 sqlite？JSON 一天一檔，每個 tool 就一個 key，counter 累加，
    讀寫都 O(1)。少一個 dependency 永遠是好事。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, date, timedelta
from typing import Any

from agent_core.logging_and_paths import DATA_DIR
from agent_core.state_io import locked_json


# ────────────────────────────────────────────────────────────────────
# 預設 budgets — None = 無上限
# 原則：燒錢 / 對外副作用大 / 不可逆 → 設較緊；read-only 類 → 不設
# ────────────────────────────────────────────────────────────────────
_DEFAULT_BUDGETS: dict[str, dict[str, int]] = {
    # 對外通訊 — 寄錯量大會傷信譽
    "send_gmail":              {"daily": 30, "hourly": 10},
    "reply_gmail":             {"daily": 30, "hourly": 10},
    "telegram_push":           {"daily": 100, "hourly": 30},
    # 檔案傳送限緊一點（每天 50 個，防被 inject 後狂傳檔案外洩）
    "telegram_send_file":      {"daily": 50, "hourly": 20},
    "telegram_send_photo":     {"daily": 50, "hourly": 20},
    "telegram_send_attachment":{"daily": 50, "hourly": 20},
    # Vision RPA engine — 燒 LLM API 額度（每步 1 call），限緊
    "fill_form":               {"daily": 5, "hourly": 3},
    # IDP — extract / auto_fill 燒 LLM；fill 純寫檔但仍限速防 abuse
    "extract_document_fields": {"daily": 30, "hourly": 10},
    "fill_document":           {"daily": 50, "hourly": 20},
    "auto_fill_document":      {"daily": 10, "hourly": 5},
    "create_draft":            {"daily": 30, "hourly": 10},

    # Calendar / Drive — 改錯日曆騷擾合作對象
    "create_calendar_event":   {"daily": 30, "hourly": 10},
    "update_calendar_event":   {"daily": 30, "hourly": 10},
    "delete_calendar_event":   {"daily": 20, "hourly": 5},
    "respond_to_event":        {"daily": 30, "hourly": 10},
    "upload_to_drive":         {"daily": 50, "hourly": 15},

    # AI 燒錢
    "generate_image":          {"daily": 20, "hourly": 5},
    "edit_image":              {"daily": 20, "hourly": 5},

    # 糾正固化 — Telegram 可達的永久 persona 寫入，限流防被 inject 後
    # 短時間灌爆規則庫（正常使用一天不會固化超過個位數條）
    "remember_correction_rule": {"daily": 10, "hourly": 5},
    # 確認推論事實 — 同為 Telegram 可達的永久記憶寫入，同款限流
    "confirm_inferred_fact": {"daily": 15, "hourly": 8},

    # Code-exec / sub-agents — 不限會被瘋狂 spawn
    "run_shell":               {"daily": 200, "hourly": 60},
    "run_python_code":         {"daily": 200, "hourly": 60},
    "delegate_to_sub_agent":   {"daily": 50, "hourly": 15},
    "delegate_to_sub_agents_parallel": {"daily": 20, "hourly": 5},

    # 桌面操控 — 大量點按螢幕通常是 LLM 失控 loop
    "click_screen":            {"daily": 200, "hourly": 60},
    "type_text":               {"daily": 200, "hourly": 60},
    "press_keys":              {"daily": 200, "hourly": 60},

    # 樣品追蹤 — 大量 update 不正常
    "track_sample":            {"daily": 100, "hourly": 30},
    "update_sample_status":    {"daily": 100, "hourly": 30},
    "close_sample":            {"daily": 50, "hourly": 15},

    # Browser 自動化（任意 web action）
    "browser_eval":            {"daily": 100, "hourly": 30},
    "browser_click":           {"daily": 200, "hourly": 60},
    "browser_fill":            {"daily": 200, "hourly": 60},
    "browser_type":            {"daily": 200, "hourly": 60},

    # 對外 HTTP egress — V4 round 8 C8-1 的相對應 rate-limit
    "read_website_content":    {"daily": 200, "hourly": 60},
    "search_the_web":          {"daily": 200, "hourly": 60},
    "mcp_fetch_fetch":         {"daily": 200, "hourly": 60},

    # Briefing skill — 內部會 send_gmail / telegram_push
    "send_briefing_email":     {"daily": 10, "hourly": 5},
    "push_briefing_telegram":  {"daily": 30, "hourly": 10},
}


# ────────────────────────────────────────────────────────────────────
# State 路徑 / lock
# ────────────────────────────────────────────────────────────────────
_BUDGET_DIR = os.path.join(DATA_DIR, "tool_budgets")
_state_lock = threading.Lock()
_RETAIN_DAYS = 30
_PG_BUDGET_WARNING_UNTIL = 0.0


def _warn_pg_budget_fallback(exc: Exception) -> None:
    global _PG_BUDGET_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_BUDGET_WARNING_UNTIL:
        return
    _PG_BUDGET_WARNING_UNTIL = now + 30
    try:
        from agent_core.logging_and_paths import logger

        logger.warning("tool budget Postgres state failed; using file fallback: %s", exc)
    except Exception:
        pass


def _pg_budget_store():
    try:
        from agent_core import operational_tool_budgets
        if not operational_tool_budgets.enabled():
            return None
        return operational_tool_budgets
    except Exception as exc:  # noqa: BLE001 - optional cloud backend
        _warn_pg_budget_fallback(exc)
        return None


def _ensure_dir() -> None:
    try:
        os.makedirs(_BUDGET_DIR, exist_ok=True)
    except Exception:
        pass


def _today_str() -> str:
    return date.today().isoformat()


def _state_path(day_str: str | None = None) -> str:
    return os.path.join(_BUDGET_DIR, f"{day_str or _today_str()}.json")


def _load_today() -> dict[str, Any]:
    store = _pg_budget_store()
    if store:
        try:
            return store.load_day(_today_str())
        except Exception as exc:  # noqa: BLE001 - fail open to local state
            _warn_pg_budget_fallback(exc)
    _ensure_dir()
    p = _state_path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 損毀就當空（不要因為一個壞檔擋掉所有 tool）
        return {}


def _gc_old() -> None:
    """每 ~64 次 record 跑一次：刪掉 _RETAIN_DAYS 之外的舊檔。"""
    if not os.path.isdir(_BUDGET_DIR):
        return
    cutoff = (date.today() - timedelta(days=_RETAIN_DAYS)).isoformat()
    try:
        for name in os.listdir(_BUDGET_DIR):
            if not name.endswith(".json"):
                continue
            day = name[:-5]
            if day < cutoff:
                try:
                    os.remove(os.path.join(_BUDGET_DIR, name))
                except Exception:
                    pass
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────
def get_budget(tool_name: str) -> dict[str, int]:
    """回傳 tool 的 budget dict（含 env override）。

    優先序：env > _DEFAULT_BUDGETS > {} (無上限)。

    Keys:
        daily / hourly                       : global cap shared by all callers
        per_caller_daily / per_caller_hourly : optional per-caller cap (P4)

    P4 (per-caller budget): adding the per-caller keys is opt-in. Setting them
    in _DEFAULT_BUDGETS or via env enables a separate counter that prevents a
    single caller (e.g. one Telegram chat_id) from draining the global quota
    on its own.
    """
    base = dict(_DEFAULT_BUDGETS.get(tool_name, {}))
    # env override
    safe_name = tool_name.upper()
    for key, env_var in (
        ("daily",             f"RED_BUDGET_{safe_name}_DAILY"),
        ("hourly",            f"RED_BUDGET_{safe_name}_HOURLY"),
        ("per_caller_daily",  f"RED_BUDGET_{safe_name}_PER_CALLER_DAILY"),
        ("per_caller_hourly", f"RED_BUDGET_{safe_name}_PER_CALLER_HOURLY"),
    ):
        env_val = os.environ.get(env_var)
        if env_val:
            try:
                base[key] = int(env_val)
            except ValueError:
                pass
    return base


def _normalize_caller_id(caller_id: str) -> str:
    """Sanitize the caller_id used as a per-caller bucket key — collision-free.

    Why sanitize: caller_id is sometimes a chat_id (numeric, safe), but other
    callers may pass arbitrary strings (e.g. "telegram:9990000001",
    "agent:gray", "daemon:dispatcher"). We use this string as a JSON key
    inside `var/data/tool_budgets/<date>.json`, so:
      • neutralize control / path / quote chars (file-safety + JSON safety)
      • cap printed length to keep state files small
      • empty / whitespace-only → "" → no per-caller tracking

    Codex P2: previous version did `c → '_'` in place and then truncated to
    80 chars. That made `team/a` and `team_a` collide into `team_a`, plus
    any caller_id longer than 80 with shared 80-char prefix would collide
    too. With per-caller limits enabled that means caller A could exhaust
    caller B's quota, defeating the isolation we just added.

    Fix: append a stable hash suffix derived from the **raw** caller_id
    so collisions are cryptographically rare even when the human-readable
    portion is identical post-sanitization. Format:

        <sanitized-prefix>#<10-hex-digits>

    The prefix stays human-readable (great for tool_budget_status output);
    the suffix is the determining bit for bucket lookup. Hash is sha1(raw)
    truncated — sha1 is fine here since we don't need cryptographic
    security, only collision resistance for typical inputs.
    """
    if not caller_id:
        return ""
    raw = str(caller_id).strip()
    if not raw:
        return ""
    # Hash the raw string FIRST so the suffix uniquely identifies the
    # original caller, regardless of what the prefix collapses to.
    import hashlib
    suffix = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    # Sanitize for human-readable prefix: control chars / slashes / quotes /
    # backslashes / whitespace inside → "_". Keep only printable, non-quote,
    # non-separator chars.
    bad_chars = set('/\\\'"`\n\r\t\x00')
    prefix = "".join(
        c if (c.isprintable() and c not in bad_chars) else "_"
        for c in raw
    )
    # Cap prefix at 64 so total stays ~80 incl. "#" + 10-hex.
    prefix = prefix[:64]
    return f"{prefix}#{suffix}"


def _current_hour() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H")


def check_budget(tool_name: str, caller_id: str = "") -> tuple[bool, str]:
    """還能跑這個 tool 嗎？

    Args:
        tool_name: 要檢查的 tool 名稱。
        caller_id: 可選 — 標識 caller 的字串（例如 "telegram:<chat_id>"
                   或 "agent:gray"）。指定時會額外檢查 per-caller 配額：
                   即使全域 quota 還夠，單一 caller 也不能用超過自己的份額。
                   空字串 = 不做 per-caller 檢查（向後相容）。

    Returns:
        (True, "")            — 可以跑
        (False, reason_text)  — 超出 global 或 per-caller daily/hourly budget

    P4 設計理由：tool_budgets.check_budget 之前是純全域配額；一個被劫持
    的 chat_id 可以把 send_gmail 當天的 30 次全用光，影響其他 caller / agent。
    現在 caller_id 帶進來時，**先檢查全域**（fail-fast），通過後再檢查
    per-caller。per-caller 上限沒設定就跳過 (opt-in)。
    """
    budget = get_budget(tool_name)
    if not budget:
        return True, ""
    cid = _normalize_caller_id(caller_id)
    with _state_lock:
        state = _load_today()
        rec = state.get(tool_name) or {}

        # ── 全域 quota（既有行為）──
        daily_used = int(rec.get("daily", 0))
        hourly_bucket = rec.get("hour", "")
        hourly_used = int(rec.get("hour_count", 0))
        if hourly_bucket != _current_hour():
            hourly_used = 0
        d_max = budget.get("daily")
        if d_max is not None and daily_used >= d_max:
            return False, (
                f"⏸ `{tool_name}` 今日已執行 {daily_used} 次（上限 {d_max}）— "
                f"暫停到明天 00:00。\n"
                f"   若大王確定需要解鎖：手動編輯 var/data/tool_budgets/"
                f"{_today_str()}.json，或設 env "
                f"`RED_BUDGET_{tool_name.upper()}_DAILY=<更大數>` 重啟。"
            )
        h_max = budget.get("hourly")
        if h_max is not None and hourly_used >= h_max:
            now = datetime.now()
            next_hour = (now.replace(minute=0, second=0, microsecond=0)
                         + timedelta(hours=1))
            mins_to_wait = max(1, int((next_hour - now).total_seconds() / 60))
            return False, (
                f"⏸ `{tool_name}` 本小時已執行 {hourly_used} 次（上限 {h_max}）— "
                f"請等 {mins_to_wait} 分鐘後再試。\n"
                f"   今日累計 {daily_used} / {d_max if d_max else '∞'}。"
            )

        # ── Per-caller quota（P4，opt-in）──
        # 沒指定 caller_id 或 budget 沒設 per-caller 上限 → 跳過。
        pc_d_max = budget.get("per_caller_daily")
        pc_h_max = budget.get("per_caller_hourly")
        if cid and (pc_d_max is not None or pc_h_max is not None):
            by_caller = rec.get("by_caller", {}) or {}
            cr = by_caller.get(cid) or {}
            pc_daily_used = int(cr.get("daily", 0))
            pc_hour_bucket = cr.get("hour", "")
            pc_hourly_used = int(cr.get("hour_count", 0))
            if pc_hour_bucket != _current_hour():
                pc_hourly_used = 0
            if pc_d_max is not None and pc_daily_used >= pc_d_max:
                return False, (
                    f"⏸ `{tool_name}` per-caller 今日上限 (caller={cid})："
                    f"已執行 {pc_daily_used} / {pc_d_max} 次。\n"
                    f"   全域配額仍剩 {(d_max - daily_used) if d_max else '∞'}，"
                    f"但單一 caller 用量已達上限以防止獨佔。"
                )
            if pc_h_max is not None and pc_hourly_used >= pc_h_max:
                now = datetime.now()
                next_hour = (now.replace(minute=0, second=0, microsecond=0)
                             + timedelta(hours=1))
                mins_to_wait = max(1, int((next_hour - now).total_seconds() / 60))
                return False, (
                    f"⏸ `{tool_name}` per-caller 本小時上限 (caller={cid})："
                    f"已執行 {pc_hourly_used} / {pc_h_max} 次，請等 {mins_to_wait} 分鐘。"
                )
    return True, ""


def record_use(tool_name: str, caller_id: str = "") -> None:
    """成功跑完一次 tool，counter ++。

    在 wrap_sensitive_tool 內 fn() 成功 return 後呼叫。
    失敗（exception）不 record — 失敗的呼叫不該吃 budget，否則攻擊者可以
    「故意讓 tool fail」來反向操作。

    P4: 帶 caller_id 進來時，**同時**更新全域 + per-caller 兩個 counter。
    呼叫端應跟 check_budget 帶相同 caller_id；不一致會造成 quota 失準。
    caller_id="" 維持原行為（只更新全域）。

    跨行程安全：檔案後端走 state_io.locked_json（fcntl 跨行程鎖）做
    read-modify-write。舊版「_load_today → mutate → 固定 .tmp 覆寫」在
    多 daemon 同寫時會 lost-update、且兩行程同寫同一個 .tmp 會產生壞
    JSON → _load_today 把損毀當空 → 當日全部計數歸零（= 攻擊者可靠
    並發把 budget 洗掉）。
    """
    budget = get_budget(tool_name)
    if not budget:
        return  # 不在 budget 表 → 不追蹤
    cid = _normalize_caller_id(caller_id)
    store = _pg_budget_store()
    if store:
        try:
            store.record_use(
                tool_name,
                caller_key=cid,
                cur_hour=_current_hour(),
                now_iso=datetime.now().isoformat(timespec="seconds"),
                retain_days=_RETAIN_DAYS,
            )
            return
        except Exception as exc:  # noqa: BLE001 - fail open to local state
            _warn_pg_budget_fallback(exc)
    with _state_lock:
        # 進鎖後重讀磁碟（locked_json 內建），不吃任何 in-memory 快照。
        # 注意 locked_json footgun：yielded dict 要就地 mutate。
        with locked_json(_state_path(), default={}) as state:
            rec = state.get(tool_name) or {"daily": 0, "hour": "", "hour_count": 0}
            cur_hour = _current_hour()

            # ── 全域 counter ──
            rec["daily"] = int(rec.get("daily", 0)) + 1
            if rec.get("hour") != cur_hour:
                rec["hour"] = cur_hour
                rec["hour_count"] = 0
            rec["hour_count"] = int(rec.get("hour_count", 0)) + 1
            rec["last_at"] = datetime.now().isoformat(timespec="seconds")

            # ── Per-caller counter（P4，只在 cid 非空時更新）──
            # 即使該 tool 沒設 per_caller_*  上限也記錄，方便事後審計
            # （tool_budget_status 可顯示哪個 caller 用量最大）。
            if cid:
                by_caller = rec.get("by_caller", {}) or {}
                cr = by_caller.get(cid) or {"daily": 0, "hour": "", "hour_count": 0}
                cr["daily"] = int(cr.get("daily", 0)) + 1
                if cr.get("hour") != cur_hour:
                    cr["hour"] = cur_hour
                    cr["hour_count"] = 0
                cr["hour_count"] = int(cr.get("hour_count", 0)) + 1
                cr["last_at"] = rec["last_at"]
                by_caller[cid] = cr
                rec["by_caller"] = by_caller

            state[tool_name] = rec
        # 偶爾 GC（每 ~64 次 record 跑一次）— 在 locked_json 寫回之後做，
        # 避免拉長持鎖時間。
        if (rec["daily"] & 0x3F) == 0:
            _gc_old()


def reset_budget(tool_name: str) -> bool:
    """大王手動 reset：把這個 tool 今日 counter 歸零。回 True 表成功。"""
    store = _pg_budget_store()
    if store:
        try:
            return store.reset_budget(tool_name)
        except Exception as exc:  # noqa: BLE001 - fail open to local state
            _warn_pg_budget_fallback(exc)
    with _state_lock:
        # 同 record_use：locked_json 跨行程 R-M-W，避免 lost-update /
        # 固定 .tmp 互踩。
        with locked_json(_state_path(), default={}) as state:
            removed = tool_name in state
            state.pop(tool_name, None)
        return removed


def tool_budget_status(tool_name: str = "") -> str:
    """🔍 看工具今日用量 / 餘額。

    Args:
        tool_name: 空字串 = 列所有有 budget 的工具今日用量；
                   指定名 = 只看該工具細節。

    Returns:
        formatted 文字 — Telegram / REPL 可讀。
    """
    state = _load_today()
    if tool_name:
        budget = get_budget(tool_name)
        if not budget:
            return (f"🟢 `{tool_name}` 沒有設 budget — 無上限。\n"
                    f"   要加上限：設 env `RED_BUDGET_{tool_name.upper()}_DAILY=N`")
        rec = state.get(tool_name) or {}
        daily_used = int(rec.get("daily", 0))
        d_max = budget.get("daily", "∞")
        h_max = budget.get("hourly", "∞")
        h_used = int(rec.get("hour_count", 0)) if rec.get("hour") == _current_hour() else 0
        last = rec.get("last_at", "—")
        out_lines = [
            f"🔍 `{tool_name}` 今日用量",
            f"   日：{daily_used} / {d_max}",
            f"   時：{h_used} / {h_max}（本小時）",
            f"   最後跑：{last}",
        ]
        # P4: per-caller breakdown — 顯示 top 5 用量最大的 caller。
        by_caller = rec.get("by_caller") or {}
        if by_caller:
            pc_d = budget.get("per_caller_daily", "—")
            pc_h = budget.get("per_caller_hourly", "—")
            out_lines.append(f"   per-caller 上限：日={pc_d} / 時={pc_h}")
            ranked = sorted(
                by_caller.items(),
                key=lambda kv: -int((kv[1] or {}).get("daily", 0)),
            )
            out_lines.append("   ── 各 caller 用量（top 5） ──")
            for caller, cr in ranked[:5]:
                cd = int((cr or {}).get("daily", 0))
                ch = int((cr or {}).get("hour_count", 0)) if (cr or {}).get("hour") == _current_hour() else 0
                out_lines.append(f"     {caller:30s} 日 {cd:4d}   時 {ch:3d}")
        return "\n".join(out_lines)
    # 全部摘要
    out = ["🔍 Tool budgets — 今日用量摘要", "─" * 60]
    if not _DEFAULT_BUDGETS:
        return "（_DEFAULT_BUDGETS 為空）"
    rows = []
    for name, budget in sorted(_DEFAULT_BUDGETS.items()):
        # apply env override
        budget = get_budget(name)
        rec = state.get(name) or {}
        daily_used = int(rec.get("daily", 0))
        d_max = budget.get("daily", 0)
        # 標示色
        ratio = (daily_used / d_max) if d_max else 0
        if d_max and daily_used >= d_max:
            mark = "🔴"
        elif ratio >= 0.8:
            mark = "🟡"
        elif daily_used > 0:
            mark = "🟢"
        else:
            mark = "  "
        rows.append((daily_used, name, mark, d_max, budget.get("hourly", "∞")))
    # 排序：用得多的在前
    rows.sort(key=lambda r: -r[0])
    for daily_used, name, mark, d_max, h_max in rows:
        out.append(f"  {mark} {name:38s} {daily_used:4d} / {d_max:<4} 日   "
                   f"(時上限 {h_max})")
    out.append("─" * 60)
    out.append("💡 看單一工具：tool_budget_status('send_gmail')")
    out.append("   reset：reset_budget('send_gmail')")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Maintenance
# ────────────────────────────────────────────────────────────────────
def _self_test() -> None:
    """快速 sanity check（純 in-memory，不寫檔）。"""
    assert get_budget("send_gmail") == {"daily": 30, "hourly": 10}
    assert get_budget("nonexistent_xyz") == {}
