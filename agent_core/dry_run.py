"""Dry-run mode（T7）— 全域安全演練開關。

打開後所有 destructive tool 不會真的執行，只回報「如果真做會怎樣」。
關掉後恢復正常。

使用情境：
  - 大王要跑複雜 workflow 前先看一遍不要誤刪
  - 測試新 skill 到處點 / 寄信，不想真的發出去
  - Demo 給客戶看，全程 dry-run 避免誤觸

設計：
  - **全域旗標** `_STATE["enabled"]`（thread-safe via Lock）
  - 每個 destructive tool 包一層 decorator：flag=on → 呼叫 describe_fn、flag=off → 正常執行
  - `@respects_dry_run(describe=fn)` 應用在個別 tool 上
  - 包裝發生在 `tool_registry` import 時，不動各 tool 原始碼
  - dry-run 時每次「假裝執行」都記到 ring buffer，之後可以查

狀態可用 tool 控制：
  - enable_dry_run_mode() / disable_dry_run_mode()
  - dry_run_status()
  - last_dry_run_log(n)

⚠️ Dry-run 只防小紅自己呼叫的 tool — 不管 MCP server / 子代理外部呼叫 /
raw python 直接執行的 code。大王背景自動化可用但不是完整防護。
"""
from __future__ import annotations

import functools
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable


# 全域狀態（thread-safe）。Persisted to disk because RPC workers are fresh
# subprocesses; in-memory flags would not be visible across tool executions.
_LOCK = threading.Lock()
_PG_DRY_RUN_WARNING_UNTIL = 0.0


def _warn_pg_dry_run_fallback(exc: Exception) -> None:
    global _PG_DRY_RUN_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_DRY_RUN_WARNING_UNTIL:
        return
    _PG_DRY_RUN_WARNING_UNTIL = now + 30
    try:
        from agent_core.logging_and_paths import logger

        logger.warning("Postgres dry_run failed; falling back to JSON: %s", exc)
    except Exception:
        pass


def _pg_dry_run_store():
    try:
        from agent_core import operational_dry_run as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - dry-run must stay available
        _warn_pg_dry_run_fallback(exc)
    return None


def _state_path() -> str:
    from agent_core.logging_and_paths import STATE_DIR
    return os.path.join(STATE_DIR, "dry_run_state.json")


def _load_persisted_state() -> dict:
    store = _pg_dry_run_store()
    if store is not None:
        try:
            return store.load_state()
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_dry_run_fallback(exc)
    try:
        path = _state_path()
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _memory_payload_locked() -> dict:
    return {
        "enabled": bool(_STATE["enabled"]),
        "enabled_at": _STATE["enabled_at"],
        "simulated_calls": list(_STATE["simulated_calls"])[-200:],
    }


def _apply_persisted_state(persisted: dict) -> None:
    if not isinstance(persisted, dict):
        return
    _STATE["enabled"] = bool(persisted.get("enabled", False))
    _STATE["enabled_at"] = persisted.get("enabled_at")
    _STATE["simulated_calls"] = deque(
        persisted.get("simulated_calls") or [],
        maxlen=200,
    )


def _save_state_locked() -> None:
    global _STATE_FILE_MTIME, _STATE_REFRESHED_AT
    payload = _memory_payload_locked()
    store = _pg_dry_run_store()
    if store is not None:
        try:
            store.save_state(payload)
            _STATE_FILE_MTIME = None
            _STATE_REFRESHED_AT = time.monotonic()
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_dry_run_fallback(exc)
    try:
        from agent_core.logging_and_paths import _atomic_write_text
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        _STATE_FILE_MTIME = os.path.getmtime(path)
        _STATE_REFRESHED_AT = time.monotonic()
    except Exception:
        pass


_persisted = _load_persisted_state()
_STATE = {
    "enabled": bool(_persisted.get("enabled", False)),
    "enabled_at": _persisted.get("enabled_at"),
    "simulated_calls": deque(_persisted.get("simulated_calls") or [], maxlen=200),
}
try:
    _REFRESH_TTL_S = max(0.0, float(os.environ.get("RED_DRY_RUN_REFRESH_TTL_S", "0.5")))
except Exception:
    _REFRESH_TTL_S = 0.5
_STATE_REFRESHED_AT = 0.0
try:
    _STATE_FILE_MTIME: float | None = os.path.getmtime(_state_path())
except OSError:
    _STATE_FILE_MTIME = None


def _refresh_state_locked(force: bool = False) -> None:
    global _STATE_FILE_MTIME, _STATE_REFRESHED_AT
    now = time.monotonic()
    if not force and _STATE_REFRESHED_AT and now - _STATE_REFRESHED_AT < _REFRESH_TTL_S:
        return
    _STATE_REFRESHED_AT = now
    store = _pg_dry_run_store()
    if store is not None:
        try:
            _apply_persisted_state(store.load_state())
            _STATE_FILE_MTIME = None
        except Exception as exc:  # noqa: BLE001 - keep last known memory state
            _warn_pg_dry_run_fallback(exc)
        return
    try:
        mtime = os.path.getmtime(_state_path())
    except OSError:
        return
    if not force and _STATE_FILE_MTIME is not None and mtime == _STATE_FILE_MTIME:
        return
    persisted = _load_persisted_state()
    if not persisted:
        return
    _apply_persisted_state(persisted)
    _STATE_FILE_MTIME = mtime


def is_dry_run() -> bool:
    """目前在 dry-run mode 嗎？"""
    with _LOCK:
        _refresh_state_locked()
        return _STATE["enabled"]


def enable_dry_run_mode() -> str:
    """啟動 dry-run 模式：所有包了 @respects_dry_run 的 destructive tool
    都不會真的執行，只會報告「會做什麼」。

    ⚠️ 一直保持開啟狀態直到手動 disable_dry_run_mode() 或 agent 重啟。
    ⚠️ 不影響 MCP server / 子代理外部呼叫 / 直接執行的 Python code。
    """
    with _LOCK:
        store = _pg_dry_run_store()
        if store is not None:
            try:
                with store.locked_state() as state:
                    _apply_persisted_state(state)
                    if state.get("enabled"):
                        return (
                            "🧪 dry-run 模式本來就開著（自 "
                            + (str(state.get("enabled_at") or "?"))
                            + "）"
                        )
                    state["enabled"] = True
                    state["enabled_at"] = datetime.now().isoformat(timespec="seconds")
                    state["simulated_calls"] = []
                    _apply_persisted_state(state)
                return (
                    "🧪 Dry-run mode 已啟動 ✅\n"
                    "  所有 destructive tool（寄信、刪事件、點擊、生圖...）都只會演練，不實際執行。\n"
                    "  結束測試請呼叫 disable_dry_run_mode()。"
                )
            except Exception as exc:  # noqa: BLE001 - fall back to local state
                _warn_pg_dry_run_fallback(exc)
        if _STATE["enabled"]:
            return "🧪 dry-run 模式本來就開著（自 " + (_STATE["enabled_at"] or "?") + "）"
        _STATE["enabled"] = True
        _STATE["enabled_at"] = datetime.now().isoformat(timespec="seconds")
        _STATE["simulated_calls"].clear()
        _save_state_locked()
    return (
        "🧪 Dry-run mode 已啟動 ✅\n"
        "  所有 destructive tool（寄信、刪事件、點擊、生圖...）都只會演練，不實際執行。\n"
        "  結束測試請呼叫 disable_dry_run_mode()。"
    )


def disable_dry_run_mode() -> str:
    """關閉 dry-run 模式，恢復正常執行。"""
    with _LOCK:
        store = _pg_dry_run_store()
        if store is not None:
            try:
                with store.locked_state() as state:
                    _apply_persisted_state(state)
                    if not state.get("enabled"):
                        return "🧪 dry-run 本來就沒開"
                    count = len(state.get("simulated_calls") or [])
                    since = state.get("enabled_at")
                    state["enabled"] = False
                    _apply_persisted_state(state)
                return (
                    f"✅ Dry-run 已關閉。\n"
                    f"  這段期間（自 {since}）共演練了 {count} 次 destructive call。\n"
                    f"  要看清單可呼叫 last_dry_run_log()。"
                )
            except Exception as exc:  # noqa: BLE001 - fall back to local state
                _warn_pg_dry_run_fallback(exc)
        if not _STATE["enabled"]:
            return "🧪 dry-run 本來就沒開"
        count = len(_STATE["simulated_calls"])
        since = _STATE["enabled_at"]
        _STATE["enabled"] = False
        _save_state_locked()
    return (
        f"✅ Dry-run 已關閉。\n"
        f"  這段期間（自 {since}）共演練了 {count} 次 destructive call。\n"
        f"  要看清單可呼叫 last_dry_run_log()。"
    )


def dry_run_status() -> str:
    """查 dry-run 目前狀態（開/關、開啟時間、演練次數）。"""
    with _LOCK:
        _refresh_state_locked(force=True)
        enabled = _STATE["enabled"]
        since = _STATE["enabled_at"]
        count = len(_STATE["simulated_calls"])
    if enabled:
        return f"🧪 dry-run = ON（自 {since}，已演練 {count} 次）"
    return "⚡ dry-run = OFF（所有 tool 正常執行）"


def last_dry_run_log(limit: int = 20) -> str:
    """查 dry-run mode 開啟期間，有哪些 destructive tool 被「演練」過。

    Args:
        limit: 最多回幾筆（新到舊）。
    """
    with _LOCK:
        _refresh_state_locked(force=True)
        if not _STATE["simulated_calls"]:
            return "（dry-run log 是空的）"
        entries = list(_STATE["simulated_calls"])[-limit:][::-1]

    lines = [f"🧪 Dry-run 演練紀錄（最近 {len(entries)} 筆，新到舊）"]
    lines.append("-" * 60)
    for e in entries:
        lines.append(f"  {e['ts']}  {e['tool']}")
        if e.get("description"):
            lines.append(f"    → {e['description'][:200]}")
    return "\n".join(lines)


def _record_simulated(tool_name: str, description: str, kwargs: dict):
    """把一次 dry-run 演練記進 ring buffer。"""
    entry = {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "tool": tool_name,
        "description": description[:500],
        # 記少量 kwargs 預覽（不含完整 value）
        "args_preview": ", ".join(
            f"{k}={str(v)[:40]!r}" for k, v in list(kwargs.items())[:5]
        ),
    }
    with _LOCK:
        store = _pg_dry_run_store()
        if store is not None:
            try:
                with store.locked_state() as state:
                    calls = [
                        dict(item)
                        for item in state.get("simulated_calls", [])
                        if isinstance(item, dict)
                    ]
                    calls.append(entry)
                    state["simulated_calls"] = calls[-200:]
                    _apply_persisted_state(state)
                return
            except Exception as exc:  # noqa: BLE001 - fall back to local state
                _warn_pg_dry_run_fallback(exc)
        _STATE["simulated_calls"].append(entry)
        _save_state_locked()


def _safe_preview(value: Any, max_len: int = 120) -> str:
    """Short, redacted preview for dry-run descriptions."""
    try:
        from agent_core.log_redact import redact_log_line
        text = redact_log_line(str(value))
    except Exception:
        text = str(value)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _desc_generic_sensitive(*args, **kwargs):
    """Fallback description for sensitive tools without a custom describer."""
    parts = []
    if args:
        parts.append("args=" + ", ".join(_safe_preview(v, 60) for v in args[:3]))
    if kwargs:
        parts.append("kwargs=" + ", ".join(
            f"{k}={_safe_preview(v, 60)!r}" for k, v in list(kwargs.items())[:6]
        ))
    return "會執行一個敏感工具；" + ("；".join(parts) if parts else "未提供參數預覽")


def _is_sensitive_tool_name(tool_name: str) -> bool:
    """Single source hook: Telegram sensitive policy also drives dry-run wrapping."""
    try:
        from agent_core.tg_auth import is_sensitive
        return is_sensitive(tool_name)
    except Exception:
        return False


def get_dry_run_describer(tool_name: str) -> Callable | None:
    """Return a dry-run describer for custom or policy-sensitive tools."""
    if tool_name in DRY_RUN_DESCRIPTIONS:
        return DRY_RUN_DESCRIPTIONS[tool_name]
    if _is_sensitive_tool_name(tool_name):
        return _desc_generic_sensitive
    return None


# ────────────────────────────────────────────────────────────────────
# 核心 decorator — 各 destructive tool 包這個
# ────────────────────────────────────────────────────────────────────
def respects_dry_run(describe: Callable[..., str] = None):
    """裝飾器：被包的 tool 在 dry_run 模式會呼叫 describe(**kwargs) 取得
    人類可讀描述，不真正執行原函式。

    Args:
        describe: function(*args, **kwargs) -> str，回傳「如果真做會發生什麼」。
                  None 時用 default fallback（列 args/kwargs 不具可讀性但安全）。

    Usage:
        @respects_dry_run(describe=lambda to, subject, **_: f"會寄信給 {to}，主題={subject}")
        def send_gmail(to, subject, body, **kw):
            ...
    """
    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not is_dry_run():
                return fn(*args, **kwargs)

            # Dry-run path
            try:
                if describe:
                    desc = describe(*args, **kwargs)
                else:
                    desc = (f"會呼叫 {fn.__name__}(args={args[:3]}, "
                            f"kwargs={list(kwargs.keys())[:5]})")
            except Exception as e:
                desc = f"（describe fn 出錯：{e}）"

            _record_simulated(fn.__name__, desc, kwargs)
            return f"🧪 [DRY RUN] {fn.__name__}\n  {desc}"

        # 保留原 fn 的 custom markers（skill / mcp / audit 等）
        for attr in ("_is_skill", "_is_mcp_tool", "_mcp_server", "_mcp_tool",
                     "background_safe", "_audited"):
            if hasattr(fn, attr):
                setattr(wrapper, attr, getattr(fn, attr))
        wrapper._dry_run_wrapped = True
        return wrapper

    return deco


# ────────────────────────────────────────────────────────────────────
# Registry of describe-fns for destructive BUILTIN tools
# 這張表告訴 tool_registry 哪些 tool 要包 respects_dry_run，以及怎麼描述
# ────────────────────────────────────────────────────────────────────

# 寄信 / 日曆 / Drive — 外送、不可逆
def _desc_send_gmail(to="", subject="", body="", cc="", bcc="", attachments=None, **_):
    att = f"，附件 {len(attachments)} 個" if attachments else ""
    cc_info = f"，cc={cc}" if cc else ""
    return (f"會寄信給 {to}，主題「{subject}」，"
            f"內文 {len(body or '')} 字{cc_info}{att}。")


def _desc_reply_gmail(message_id="", body="", reply_all=False, **_):
    return f"會回覆 thread {message_id}（reply_all={reply_all}），內文 {len(body or '')} 字"


def _desc_create_calendar(summary="", start="", end="", **_):
    return f"會建行事曆事件「{summary}」（{start} → {end}）"


def _desc_delete_calendar(event_id="", **_):
    return f"會刪除行事曆事件 {event_id}"


def _desc_upload_drive(local_path="", folder_id="", **_):
    return f"會上傳 {local_path} 到 Drive folder {folder_id or '(root)'}"


def _desc_generate_image(prompt="", model="gemini-2.5-flash-image", **_):
    return f"會呼叫 {model} 生一張圖，prompt「{prompt[:100]}」"


def _desc_edit_image(image_path="", instruction="", **_):
    return f"會用 Gemini 編輯圖 {image_path}，指令「{instruction[:80]}」"


def _desc_click_screen(x=0, y=0, **_):
    return f"會在螢幕 ({x}, {y}) 位置點一下"


def _desc_type_text(text="", **_):
    return f"會輸入 {len(text or '')} 字：「{text[:50]}」"


def _desc_press_keys(keys="", **_):
    return f"會按組合鍵：{keys}"


def _desc_open_app(name="", **_):
    return f"會開啟 App「{name}」"


def _desc_close_app(name="", **_):
    return f"會關閉 App「{name}」"


def _desc_ax_click(app_name="", title="", role="", **_):
    return f"會在 {app_name} 點擊 title='{title}' role='{role or '任意'}'"


def _desc_ax_type(app_name="", field_title="", text="", **_):
    return f"會在 {app_name} 的 '{field_title}' 欄位輸入 {len(text or '')} 字"


def _desc_delegate_subagent(goal="", **_):
    return f"會啟動子代理執行：「{goal[:120]}」"


def _desc_delegate_parallel(goals=None, **_):
    n = len(goals) if goals else 0
    return f"會並行啟動 {n} 個子代理"


def _desc_learn_skill_from_video(video_path="", skill_name="", **_):
    return f"會上傳影片 {video_path} 給 Gemini 分析並產 skills/learned_{skill_name}.py.draft"


def _desc_run_shell(command="", timeout_sec=30, working_dir="", **_):
    cwd = f"（cwd={working_dir}）" if working_dir else ""
    return (f"會執行 shell 指令{cwd}（timeout={timeout_sec}s）：\n"
            f"    {command[:200]}{'...' if len(command) > 200 else ''}")


def _desc_run_python(code="", **_):
    # 從 code 抓前幾個關鍵動詞行，讓大王在 dry-run 看得懂
    snippet = (code or "").strip().split("\n")[:4]
    return "會在沙箱執行 Python（timeout=30s），前幾行：\n    " + "\n    ".join(snippet)


def _desc_excel_write(path="", data=None, **_):
    rows = len(data) if isinstance(data, list) else "?"
    return f"會寫入 Excel 檔 {path}（{rows} 列資料）"


def _desc_excel_query(path="", question="", **_):
    # excel_query 本身會叫 LLM 生 pandas code 再 exec — 算 medium-risk
    return f"會對 {path} 用 LLM 生 pandas 代碼並執行，問題：「{question[:120]}」"


# 輸出到這個 dict 給 tool_registry 吃
DRY_RUN_DESCRIPTIONS: dict[str, Callable] = {
    "send_gmail": _desc_send_gmail,
    "reply_gmail": _desc_reply_gmail,
    "create_calendar_event": _desc_create_calendar,
    "delete_calendar_event": _desc_delete_calendar,
    "upload_to_drive": _desc_upload_drive,
    "generate_image": _desc_generate_image,
    "edit_image": _desc_edit_image,
    "click_screen": _desc_click_screen,
    "type_text": _desc_type_text,
    "press_keys": _desc_press_keys,
    "open_application": _desc_open_app,
    "close_application": _desc_close_app,
    "ax_click": _desc_ax_click,
    "ax_type_in": _desc_ax_type,
    "delegate_to_sub_agent": _desc_delegate_subagent,
    "delegate_to_sub_agents_parallel": _desc_delegate_parallel,
    "learn_skill_from_video": _desc_learn_skill_from_video,
    # Shell / Python / Excel code-exec（V2 安全性修補：這些能改檔案或叫系統）
    "run_shell": _desc_run_shell,
    "run_python_code": _desc_run_python,
    "excel_write": _desc_excel_write,
    "excel_query": _desc_excel_query,
}
