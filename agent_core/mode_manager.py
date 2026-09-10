"""Work mode 管理器 — 切換大王當前的工作情境。

Mode 跟其他維度的關係：
  channel  訊息來源（telegram / voice / daemon / repl）
  tier     工具破壞性（safe / confirm / dangerous / locked）
  intent   訊息語意分類（query_data / write_email / ...）
  mode     大王當前工作情境（normal / meeting / sales / dev / security）← 本檔

各維度 orthogonal — 大王在 meeting mode 用 telegram channel 問 query_data
intent 的訊息，4 個維度同時生效。

設計重點：
  - 持久化 — var/state/work_mode.json，重啟保留 mode
  - Lazy expiry — 不開背景 timer，每次 get_current_mode 才檢查 expires_at
  - Auto-fallback — expires_at 到期 / 未知 mode → normal
  - History — 切換歷史寫到 work_mode_history.jsonl 給 dashboard

安全考量：
  - set_work_mode / exit_work_mode = CONFIRM tier
    （攻擊者切到 dev mode 會解鎖更多 tool 行為，要 +確認）
  - mode 切換寫 audit log（同 vault）
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Iterator

from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger
from agent_core.state_io import locked_json


# ────────────────────────────────────────────────────────────────────
# 常數 / 路徑
# ────────────────────────────────────────────────────────────────────
DEFAULT_MODE = "normal"
_MODE_FILE = os.path.join(STATE_DIR, "work_mode.json")
_HISTORY_FILE = os.path.join(STATE_DIR, "work_mode_history.jsonl")
_HISTORY_MAX_LINES = 500
_MAX_DURATION_HOURS = 24  # 不允許 > 24h 的 duration（防 typo 設成 9999）
_PG_WORK_MODE_WARNING_UNTIL = 0.0


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
def _ensure_dir() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_mode_state() -> dict:
    return {"mode": DEFAULT_MODE, "set_at": "", "expires_at": "",
            "set_by": ""}


def _warn_pg_work_mode_fallback(exc: Exception) -> None:
    global _PG_WORK_MODE_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_WORK_MODE_WARNING_UNTIL:
        return
    _PG_WORK_MODE_WARNING_UNTIL = now + 30
    logger.warning("Postgres work_mode failed; falling back to JSON: %s", exc)


def _pg_work_mode_store():
    try:
        from agent_core import operational_work_mode as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - work mode should stay available
        _warn_pg_work_mode_fallback(exc)
    return None


def _load_mode_state_file() -> dict:
    _ensure_dir()
    if not os.path.isfile(_MODE_FILE):
        return _default_mode_state()
    try:
        with open(_MODE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_mode_state()


def _load_mode_state() -> dict:
    store = _pg_work_mode_store()
    if store is not None:
        try:
            return store.load_mode_state()
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_work_mode_fallback(exc)
    return _load_mode_state_file()


def _save_mode_state_file(state: dict) -> None:
    try:
        _atomic_write_text(_MODE_FILE,
                            json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _save_mode_state(state: dict) -> None:
    store = _pg_work_mode_store()
    if store is not None:
        try:
            store.save_mode_state(state)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_work_mode_fallback(exc)
    _save_mode_state_file(state)


def _append_history_file(entry: dict) -> None:
    """歷史紀錄寫 jsonl（rotated 至 _HISTORY_MAX_LINES）。"""
    try:
        _ensure_dir()
        with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # rotate
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > _HISTORY_MAX_LINES * 1.5:
                _atomic_write_text(_HISTORY_FILE,
                                    "".join(lines[-_HISTORY_MAX_LINES:]))
        except Exception:
            pass
    except Exception:
        pass


def _append_history(entry: dict) -> None:
    """歷史紀錄寫 Postgres（opt-in）或 jsonl fallback。"""
    store = _pg_work_mode_store()
    if store is not None:
        try:
            store.append_history(entry)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local history
            _warn_pg_work_mode_fallback(exc)
    _append_history_file(entry)


@contextlib.contextmanager
def _locked_mode_state() -> Iterator[dict]:
    store = _pg_work_mode_store()
    if store is not None:
        pg_data: dict | None = None
        body_failed = False
        try:
            with store.locked_mode_state() as data:
                pg_data = data
                try:
                    yield data
                except BaseException:
                    body_failed = True
                    raise
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            if body_failed:
                raise
            _warn_pg_work_mode_fallback(exc)
            if pg_data is not None:
                _save_mode_state_file(pg_data)
                return

    with locked_json(_MODE_FILE, default=dict(_DEFAULT_STATE)) as state:
        yield state


def _is_expired(state: dict) -> bool:
    expires = state.get("expires_at") or ""
    if not expires:
        return False
    return expires <= _now_iso()


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────
_DEFAULT_STATE = {"mode": DEFAULT_MODE, "set_at": "", "expires_at": "",
                  "set_by": ""}


def get_current_mode() -> str:
    """🟢 拿當前 work mode。

    Lazy expiry — 若 expires_at 已過，自動回 normal 並 update 檔案。

    Fast path: read once unlocked. If not expired, return without taking the
    fcntl lock (the common case — 大王 doesn't change mode often). Only the
    expire-and-rewrite path needs locking. Avoids per-call lock acquisition
    on a hot read path.
    """
    snapshot = _load_mode_state()
    if not (_is_expired(snapshot) and snapshot.get("mode") != DEFAULT_MODE):
        return snapshot.get("mode", DEFAULT_MODE)

    # Re-check under lock — another process may have just expired it.
    history_entry = None
    with _locked_mode_state() as state:
        if _is_expired(state) and state.get("mode") != DEFAULT_MODE:
            old_mode = state.get("mode", "?")
            # Mutate in place — rebinding `state = {...}` would NOT update
            # what locked_json writes back (see state_io footgun warning).
            state.clear()
            state.update({"mode": DEFAULT_MODE, "set_at": _now_iso(),
                          "expires_at": "", "set_by": "auto_expired"})
            history_entry = {
                "at": _now_iso(),
                "from_mode": old_mode,
                "to_mode": DEFAULT_MODE,
                "reason": "auto_expired",
            }
        current = state.get("mode", DEFAULT_MODE)
    if history_entry:
        _append_history(history_entry)
    return current


def get_mode_state() -> dict:
    """🟢 拿完整 mode state（含 set_at / expires_at / set_by）。"""
    state = _load_mode_state()
    # Trigger lazy expire if needed (writes via get_current_mode under lock)
    if _is_expired(state) and state.get("mode") != DEFAULT_MODE:
        get_current_mode()
        state = _load_mode_state()
    return dict(state)


def set_work_mode(mode: str, *, duration_minutes: int = 0,
                   set_by: str = "user_explicit") -> Any:
    """🟡 切換 work mode。CONFIRM tier — 要 +確認。

    Args:
        mode: 'normal' / 'meeting' / 'sales' / 'dev' / 'security' / 'quant' / 'cfo'
        duration_minutes: 0 = 不自動過期；> 0 = N 分鐘後自動回 normal
        set_by: 紀錄誰切的（預設 user_explicit；自動偵測時可填 auto_detect）

    Returns:
        ToolResult — 成功 / 失敗（unknown mode / duration 超過 24h）
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    from agent_core.persona_profiles import list_known_modes
    valid = list_known_modes()
    if mode not in valid:
        return ToolResult.failure(
            f"未知 mode '{mode}'。可選：{', '.join(valid)}",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False,
        )
    if duration_minutes < 0:
        return ToolResult.failure("duration_minutes 不能負數",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    if duration_minutes > _MAX_DURATION_HOURS * 60:
        return ToolResult.failure(
            f"duration_minutes 超過 {_MAX_DURATION_HOURS} 小時上限",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False,
        )

    now = datetime.now()
    expires_at = ""
    if duration_minutes > 0:
        expires_at = (now + timedelta(minutes=duration_minutes)).isoformat(
            timespec="seconds")

    prev_mode_outer = [DEFAULT_MODE]  # smuggle prev mode out for the return value
    history_entry = None
    with _locked_mode_state() as state:
        prev_mode_outer[0] = state.get("mode", DEFAULT_MODE)
        # Mutate in place (see state_io footgun warning)
        state.clear()
        state.update({
            "mode": mode,
            "set_at": _now_iso(),
            "expires_at": expires_at,
            "set_by": set_by,
        })
        history_entry = {
            "at": _now_iso(),
            "from_mode": prev_mode_outer[0],
            "to_mode": mode,
            "duration_minutes": duration_minutes,
            "set_by": set_by,
        }

    if history_entry:
        _append_history(history_entry)

    msg = f"✅ 切到 {mode} mode"
    if expires_at:
        msg += f"（{duration_minutes} 分鐘後自動回 normal）"
    return ToolResult.success(
        msg,
        data={"mode": mode, "expires_at": expires_at,
              "previous_mode": prev_mode_outer[0]},
    )


def exit_work_mode() -> Any:
    """🟡 退回 normal mode。CONFIRM tier。"""
    return set_work_mode("normal", duration_minutes=0,
                          set_by="user_exit")


# ────────────────────────────────────────────────────────────────────
# Public introspection — SAFE
# ────────────────────────────────────────────────────────────────────
def work_mode_status() -> str:
    """🟢 看當前 mode 詳細狀態 + 剩餘時間 + 該 mode 的 persona / tool 規則。"""
    state = get_mode_state()
    from agent_core.persona_profiles import persona_for
    from agent_core.mode_policy import get_mode_rules

    mode = state.get("mode", DEFAULT_MODE)
    rules = get_mode_rules(mode)
    out = [f"🎯 當前 work mode：{mode}"]
    out.append("─" * 60)
    out.append(f"  描述: {rules.get('description', '—')}")

    set_at = state.get("set_at") or ""
    if set_at:
        out.append(f"  切換於: {set_at[:16]}")
    set_by = state.get("set_by") or ""
    if set_by:
        out.append(f"  切換者: {set_by}")

    expires = state.get("expires_at") or ""
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires)
            now = datetime.now()
            if exp_dt > now:
                remaining = exp_dt - now
                mins = int(remaining.total_seconds() / 60)
                out.append(f"  剩餘: {mins} 分鐘（{exp_dt.isoformat(timespec='minutes')} 自動回 normal）")
            else:
                out.append("  ⚠️ 已過期（下次 get_current_mode 會 lazy-revert）")
        except Exception:
            out.append(f"  expires_at: {expires}")

    blocked = rules.get("blocked_tiers") or []
    if blocked:
        out.append(f"  封鎖 tier: {', '.join(blocked)}")
    max_chars = rules.get("max_response_chars", 0)
    if max_chars:
        out.append(f"  回應字數上限: {max_chars}")
    tool_names = rules.get("tool_names")
    if tool_names is not None:
        out.append(f"  可用工具: {len(tool_names)} 個（其餘隱藏）")
    else:
        out.append("  可用工具: 全集（不 narrow）")

    addendum = persona_for(mode)
    if addendum:
        out.append("")
        out.append("  Persona 補丁（追加在系統提示尾）:")
        for line in addendum.strip().split("\n")[:6]:
            out.append(f"    {line}")
    return "\n".join(out)


def list_work_modes() -> str:
    """🟢 列出所有可用 mode + 各自說明。"""
    from agent_core.mode_policy import known_modes, get_mode_rules
    out = ["🎯 可用 work modes"]
    out.append("─" * 60)
    current = get_current_mode()
    for m in known_modes():
        rules = get_mode_rules(m)
        marker = "👉" if m == current else "  "
        out.append(f"{marker} {m:10s} {rules.get('description', '')}")
        tn = rules.get("tool_names")
        if tn is not None:
            out.append(f"             工具: {len(tn)} 個 subset")
        else:
            out.append("             工具: 全集")
        bt = rules.get("blocked_tiers") or []
        if bt:
            out.append(f"             封鎖: {', '.join(bt)}")
    out.append("")
    out.append("💡 切換：set_work_mode('meeting', duration_minutes=120)")
    out.append("          set_work_mode('security', duration_minutes=120)")
    return "\n".join(out)


def mode_history(hours: int = 24, limit: int = 20) -> str:
    """🟢 看過去 N 小時的 mode 切換歷史。"""
    store = _pg_work_mode_store()
    if store is not None:
        try:
            rows = store.load_history(hours=hours, limit=50000)
            if not rows:
                return f"  （過去 {hours}h 沒有 mode 切換）"
            return _format_mode_history(
                rows,
                hours=hours,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to local history
            _warn_pg_work_mode_fallback(exc)
    if not os.path.isfile(_HISTORY_FILE):
        return "  （沒有 mode 切換歷史）"
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows: list[dict] = []
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except (ValueError, TypeError):
                    continue
                if (r.get("at") or "") < cutoff:
                    continue
                rows.append(r)
    except (ValueError, TypeError):
        pass
    if not rows:
        return f"  （過去 {hours}h 沒有 mode 切換）"
    return _format_mode_history(rows, hours=hours, limit=limit)


def _format_mode_history(rows: list[dict], *, hours: int, limit: int) -> str:
    out = [f"🎯 mode 切換歷史 — 過去 {hours}h，{len(rows)} 次"]
    out.append("─" * 60)
    for r in rows[-limit:]:
        when = (r.get("at") or "")[5:16]
        from_m = r.get("from_mode", "?")
        to_m = r.get("to_mode", "?")
        by = r.get("set_by", "")
        dur = r.get("duration_minutes", 0)
        dur_str = f" ({dur}m)" if dur else ""
        out.append(f"  [{when}] {from_m} → {to_m}{dur_str}  by {by}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Dashboard helper
# ────────────────────────────────────────────────────────────────────
def mode_summary() -> dict:
    """給 dashboard / status_center 的精簡摘要。"""
    state = get_mode_state()
    expires = state.get("expires_at") or ""
    remaining_min = 0
    if expires:
        try:
            exp = datetime.fromisoformat(expires)
            remaining_min = max(0, int((exp - datetime.now()).total_seconds() / 60))
        except (ValueError, TypeError):
            pass
    return {
        "current": state.get("mode", DEFAULT_MODE),
        "set_at": state.get("set_at", ""),
        "expires_at": expires,
        "remaining_minutes": remaining_min,
        "set_by": state.get("set_by", ""),
    }
