"""Alert pusher — 把 dashboard alerts 主動推到大王 Telegram（+ email fallback）。

問題（大王 直接證據）：
  daemon 死了 4 天大王不知道，因為：
    1. dashboard_alerts.check_alerts() 是 LLM-facing query tool，沒人主動呼叫
    2. 即使 alert 觸發也只在 dashboard.system_status() 顯示，要大王主動看
    3. telegram daemon 死了之後沒回應，但大王以為小紅在那

解法：
  Tick-style daemon — 每 N 分鐘跑一次，掃描 alerts，新觸發的推到 Telegram。
  state 記在 var/state/alert_push_state.json，避免同 alert 連續 spam。

設計：
  - state schema: {alert_id: {first_seen_at, last_pushed_at, level, recovery_pushed}}
  - push 規則：
    1. 第一次看到 crit/warn alert → push
    2. 同 alert 連續觸發：每 _RESEND_INTERVAL_HOURS 重 push 一次（避免大王忘）
    3. alert 消失（recovered）→ push 「✅ 已恢復」訊息一次，清 state
  - 失敗 fallback：
    1. 先試 telegram_push
    2. 失敗 → 試 send_gmail（用 daemon_helpers.notify）
    3. 都失敗 → 寫 var/logs/alert_push_failed.log

不在這個 module 處理（避免 scope creep）：
  - SMS / phone call fallback（要付費 SMS gateway）
  - alert 升級 / 派發路由（big system 才需要）
  - 自動 daemon restart（單獨另一個 daemon 做）
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Iterator

from agent_core.logging_and_paths import STATE_DIR, _LOG_DIR, _atomic_write_text, logger
from agent_core.state_io import locked_json


# ────────────────────────────────────────────────────────────────────
# 設定
# ────────────────────────────────────────────────────────────────────
_PUSH_STATE_FILE = os.path.join(STATE_DIR, "alert_push_state.json")
_FAILED_LOG = os.path.join(_LOG_DIR or STATE_DIR, "alert_push_failed.log")
_RESEND_INTERVAL_HOURS = 6  # 同 alert 6 小時內不重 push（避免吵）
_PUSH_LEVELS = ("crit", "warn")  # 哪幾級 alert 要 push（warn 也算，daemon 死要知道）
_PG_ALERT_PUSH_WARNING_UNTIL = 0.0
_CLAIM_TTL_MINUTES = 10


# ────────────────────────────────────────────────────────────────────
# State helpers
# ────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _warn_pg_alert_fallback(exc: Exception) -> None:
    global _PG_ALERT_PUSH_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_ALERT_PUSH_WARNING_UNTIL:
        return
    _PG_ALERT_PUSH_WARNING_UNTIL = now + 30
    logger.warning("Postgres alert_pusher failed; falling back to JSON: %s", exc)


def _pg_alert_store():
    try:
        from agent_core import operational_alert_pusher as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - alert pusher must stay usable
        _warn_pg_alert_fallback(exc)
    return None


def _load_state_file() -> dict:
    if not os.path.isfile(_PUSH_STATE_FILE):
        return {}
    try:
        with open(_PUSH_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_state() -> dict:
    """Unlocked snapshot read. Safe for read-only callers (alert_push_status,
    tests). For read-modify-write, use `_locked_state` instead — see
    push_pending_alerts."""
    store = _pg_alert_store()
    if store is not None:
        try:
            return store.load_state()
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_alert_fallback(exc)
    return _load_state_file()


def _save_state_file(state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        _atomic_write_text(_PUSH_STATE_FILE,
                            json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _save_state(state: dict) -> None:
    """Unlocked atomic write. Kept for tests/pre-population; production R-M-W
    paths must use `_locked_state`."""
    store = _pg_alert_store()
    if store is not None:
        try:
            store.replace_state(state)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            _warn_pg_alert_fallback(exc)
    _save_state_file(state)


@contextlib.contextmanager
def _locked_state() -> Iterator[dict[str, Any]]:
    store = _pg_alert_store()
    if store is not None:
        pg_data: dict[str, Any] | None = None
        body_failed = False
        try:
            with store.locked_state() as data:
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
            _warn_pg_alert_fallback(exc)
            if pg_data is not None:
                _save_state_file(pg_data)
                return

    with locked_json(_PUSH_STATE_FILE, default={}) as state:
        yield state


def _hours_since(iso_str: str) -> float:
    if not iso_str:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_str)
        return (datetime.now() - dt).total_seconds() / 3600
    except Exception:
        return float("inf")


# Severity rank for escalation detection (higher = more urgent).
_LEVEL_RANK = {"warn": 1, "crit": 2}

# ────────────────────────────────────────────────────────────────────
# 舊 alert id 一次性遷移（2026-07 健檢）
# dashboard_alerts 把「warn/crit 拆成不同 id」收斂成單一穩定 id（level 表達
# 嚴重度），daemon_multi_fail 拆回個別 daemon_fail_<name>。state 檔裡殘留的
# 舊 id 若不遷移，改名後第一輪會被誤判成 recovered → 推假的「✅ 已恢復」，
# 且新 id 再推一次重複通知。
# ────────────────────────────────────────────────────────────────────
_LEGACY_ALERT_ID_MAP = {
    "cost_today_high": "cost_today",
    "cost_today_warn": "cost_today",
    "cost_ratio_crit": "cost_ratio",
    "cost_ratio_warn": "cost_ratio",
    "runs_err_pct_crit": "runs_err_pct",
    "runs_err_pct_warn": "runs_err_pct",
    "errors_log_crit": "errors_log",
    "errors_log_warn": "errors_log",
}
# 舊 summary id 無從對應回個別 daemon → 靜默丟棄（不推恢復；個別
# daemon_fail_<name> id 下一輪自然接手）。
_LEGACY_ALERT_IDS_DROP = frozenset({"daemon_multi_fail"})


def _migrate_legacy_alert_ids(state: dict) -> None:
    """In-place：state 裡的舊 id 換成新 id；新 id 已存在（或無從對應）就丟棄舊條目。"""
    for old, new in _LEGACY_ALERT_ID_MAP.items():
        if old not in state:
            continue
        entry = state.pop(old)
        if new not in state:
            state[new] = entry
    for old in _LEGACY_ALERT_IDS_DROP:
        state.pop(old, None)


def _escalated(prev_level: str, new_level: str) -> bool:
    """True when an alert's severity climbed since it was last pushed.

    Lets a warn→crit transition bypass the 6h re-send throttle so the
    escalation isn't suppressed (Codex review on PR #80)."""
    return _LEVEL_RANK.get(new_level, 0) > _LEVEL_RANK.get(prev_level, 0)


def _claim_token() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(4)


def _claim_fresh(state: dict) -> bool:
    token = state.get("_pending_token")
    pending_at = state.get("_pending_at")
    if not token or not pending_at:
        return False
    try:
        age_min = (datetime.now() - datetime.fromisoformat(pending_at)).total_seconds() / 60
    except Exception:
        return False
    return age_min < _CLAIM_TTL_MINUTES


def _clear_pending(state: dict) -> dict:
    clean = dict(state)
    for key in (
        "_pending_action",
        "_pending_token",
        "_pending_at",
        "_pending_reason",
        "_pending_alert",
    ):
        clean.pop(key, None)
    return clean


def _claim_push_actions(
    state: dict[str, Any],
    active_alerts: list[dict],
    active_ids: set[str],
    now: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []

    for alert in active_alerts:
        aid = alert["id"]
        prev = state.get(aid)
        if prev and _claim_fresh(prev):
            continue
        should_push = False
        if not prev:
            should_push = True
            reason = "first_seen"
        elif _escalated(prev.get("level", ""), alert.get("level", "")):
            should_push = True
            reason = "escalated"
        elif _hours_since(prev.get("last_pushed_at", "")) >= _RESEND_INTERVAL_HOURS:
            should_push = True
            reason = "resend_after_quiet"
        if not should_push:
            continue

        token = _claim_token()
        claimed = dict(prev or {})
        claimed.update({
            "first_seen_at": (prev or {}).get("first_seen_at", now),
            "title": alert.get("title", ""),
            "level": alert.get("level", ""),
            "_pending_action": "push",
            "_pending_token": token,
            "_pending_at": now,
            "_pending_reason": reason,
            "_pending_alert": {
                "id": aid,
                "level": alert.get("level", ""),
                "title": alert.get("title", ""),
            },
        })
        state[aid] = claimed
        actions.append({
            "action": "push",
            "alert_id": aid,
            "token": token,
            "reason": reason,
            "alert": dict(alert),
            "prev": dict(prev or {}),
            "claimed_new": not bool(prev),
        })

    for aid in list(state.keys()):
        if aid in active_ids:
            continue
        prev = state.get(aid) or {}
        if _claim_fresh(prev):
            continue
        token = _claim_token()
        claimed = dict(prev)
        claimed.update({
            "_pending_action": "recovery",
            "_pending_token": token,
            "_pending_at": now,
        })
        state[aid] = claimed
        actions.append({
            "action": "recovery",
            "alert_id": aid,
            "token": token,
            "prev": dict(prev),
        })

    return actions


def _finalize_push_action(action: dict[str, Any], *, ok: bool, now: str) -> None:
    aid = action["alert_id"]
    token = action["token"]
    with _locked_state() as state:
        current = state.get(aid)
        if not current or current.get("_pending_token") != token:
            return
        prev = action.get("prev") or {}
        if ok:
            alert = action["alert"]
            state[aid] = {
                "first_seen_at": prev.get("first_seen_at", now),
                "last_pushed_at": now,
                "title": alert.get("title", ""),
                "level": alert.get("level", ""),
                "reason": action.get("reason", ""),
            }
            return
        if prev:
            state[aid] = _clear_pending(prev)
        else:
            state.pop(aid, None)


def _finalize_recovery_action(action: dict[str, Any]) -> None:
    aid = action["alert_id"]
    token = action["token"]
    with _locked_state() as state:
        current = state.get(aid)
        if current and current.get("_pending_token") == token:
            state.pop(aid, None)


# ────────────────────────────────────────────────────────────────────
# Push channels (with fallback)
# ────────────────────────────────────────────────────────────────────
def _try_telegram_push(message: str) -> tuple[bool, str]:
    """先試 telegram。回 (ok, error)。"""
    try:
        from agent_core.telegram import telegram_push
        result = telegram_push(message)
        if "✅" in str(result):
            return True, ""
        return False, str(result)[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _try_email_fallback(subject: str, body: str) -> tuple[bool, str]:
    """telegram 失敗時走 email。回 (ok, error)。

    用 daemon_helpers.notify（內建 Gmail 寄到大王自己），不會被 wrap_sensitive_tool
    擋（因為 daemon channel 不需 +確認）。
    """
    try:
        from agent_core.daemon_helpers import notify
        ok = notify(subject=subject, body=body, task_name="alert_pusher")
        if ok is False:
            # notify 不 raise、回 False 表示 send_gmail 回報寄信失敗（健檢
            # Medium：以前這裡把「沒 raise」當成功，email fallback 也可能默默丟信）。
            return False, "notify 回報寄信失敗（見 daemon log）"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _is_cost_alert(alert: dict) -> bool:
    """成本家族告警：cost_today / cost_ratio / cost_monthly_cap / cost_high。"""
    return str(alert.get("id") or "").startswith("cost")


def _cost_alert_recipients() -> list[str]:
    """RED_ALERT_COST_EMAIL_TO 的收件人清單（逗號/分號/空白分隔）；沒設 = 空。"""
    raw = os.getenv("RED_ALERT_COST_EMAIL_TO", "") or ""
    return [tok for tok in re.split(r"[,;\s、，；]+", raw.strip()) if tok and "@" in tok]


def _try_cost_email(subject: str, body: str, recipients: list[str]) -> tuple[bool, str]:
    """成本告警**並行**寄 email（不是 telegram fallback）。回 (全部成功嗎, error)。

    為什麼不共用 _try_email_fallback：那條路寄給 get_my_email()（大王自己）、
    而且只在 telegram 掛掉時才走。大王 2026-08-04 要的是「成本不正常一定收得到
    信」——telegram 正常時也要寄，收件人另外指定。

    best-effort：任何一個地址失敗都回 False 讓 caller 記進 errors，但絕不 raise
    （告警管線自己炸掉比漏一封信更糟）。
    """
    try:
        from agent_core.gmail import send_gmail_internal
    except Exception as e:
        return False, f"import send_gmail_internal 失敗：{type(e).__name__}: {e}"
    failed: list[str] = []
    for addr in recipients:
        try:
            # 告警是小紅自己產的內容，帶出處標記免得被 RAG 當公司原始信吃回去。
            res = send_gmail_internal(
                to=addr, subject=subject, body=body, generated_by="alert:cost",
            )
            # gmail_ops.send_gmail 的失敗訊號是回傳字串（不 raise），比照 notify()。
            if str(res).startswith("發信失敗"):
                failed.append(f"{addr}: {res}")
        except Exception as e:
            failed.append(f"{addr}: {type(e).__name__}: {e}")
    if failed:
        return False, "; ".join(failed)[:300]
    return True, ""


def _log_failed(message: str, errors: list[str]) -> None:
    """所有 channel 都失敗 → 寫 log，後續可以在 dashboard / cron 撈來看。"""
    try:
        os.makedirs(os.path.dirname(_FAILED_LOG), exist_ok=True)
        with open(_FAILED_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] FAILED to push:\n")
            f.write(f"  message: {message[:400]}\n")
            for ch, err in errors:
                f.write(f"    via {ch}: {err}\n")
            f.write("\n")
    except Exception:
        pass


def _push_alert(alert: dict) -> tuple[bool, list[tuple[str, str]]]:
    """Push 單一 alert，回 (任一 channel 成功嗎, [(channel, error), ...])。"""
    icon = {"crit": "🔴", "warn": "🟡"}.get(alert.get("level"), "❓")
    title = (alert.get("title") or "")[:120]
    detail = (alert.get("detail") or "")[:300]
    advice = (alert.get("advice") or "")[:200]
    msg_lines = [
        f"{icon} {title}",
        "",
        detail,
    ]
    if advice:
        msg_lines += ["", f"💡 {advice}"]
    message = "\n".join(msg_lines)

    errors: list[tuple[str, str]] = []
    subject = f"【小紅 alert】{title[:60]}"

    # 成本告警並行寄 email（大王 2026-08-04 指定 owner@，走 plist 的
    # RED_ALERT_COST_EMAIL_TO）。刻意排在 telegram **之前**且不 early-return：
    # 當成 fallback 的話，telegram 正常時就永遠收不到成本異常信——而成本暴衝
    # 恰恰是 telegram 還活得好好的時候發生的。失敗只記 errors、不改變 telegram
    # 的成敗判定（email 掛掉不該讓 alert 被當成沒推出去而重試洗版）。
    if _is_cost_alert(alert):
        recipients = _cost_alert_recipients()
        if recipients:
            ok_mail, err_mail = _try_cost_email(subject, message, recipients)
            if not ok_mail:
                errors.append(("cost_email", err_mail))

    # Try telegram
    ok, err = _try_telegram_push(message)
    if ok:
        return True, errors
    errors.append(("telegram", err))

    # Try email fallback
    ok2, err2 = _try_email_fallback(subject, message)
    if ok2:
        return True, errors  # email 救回，但 telegram 失敗也記
    errors.append(("email", err2))
    return False, errors


def _push_recovery(alert_id: str, prev_state: dict) -> bool:
    """Alert 消失（恢復）→ push 一則「已恢復」訊息。"""
    title = prev_state.get("title", alert_id)
    message = f"✅ alert 已恢復：{title[:120]}\n  alert_id: {alert_id}"
    ok, _ = _try_telegram_push(message)
    if not ok:
        _try_email_fallback(f"【小紅 alert 恢復】{title[:60]}", message)
    return ok


# ────────────────────────────────────────────────────────────────────
# Main entry
# ────────────────────────────────────────────────────────────────────
def push_pending_alerts() -> dict:
    """掃 alerts，把新觸發的 / 重發週期到的 alert 推到 Telegram。

    Returns:
        dict 摘要 {checked, pushed, recovered, failed}
    """
    try:
        from agent_core.dashboard_alerts import check_alerts
    except Exception as e:
        return {"error": f"dashboard_alerts 讀取失敗：{e}",
                "checked": 0, "pushed": 0, "recovered": 0, "failed": 0}

    try:
        alerts = check_alerts()
    except Exception as e:
        return {"error": f"check_alerts 例外：{e}",
                "checked": 0, "pushed": 0, "recovered": 0, "failed": 0}

    now = _now_iso()
    pushed = 0
    recovered = 0
    failed = 0

    # 過濾：只看要 push 的 levels
    active_alerts = [a for a in alerts
                     if a.get("level") in _PUSH_LEVELS and a.get("id")]
    active_ids = {a["id"] for a in active_alerts}

    # Claim under lock, do slow Telegram/email I/O outside the lock, then
    # finalize under lock. This keeps multi-instance dedupe atomic without
    # holding a file/DB lock while external push channels are slow.
    with _locked_state() as state:
        _migrate_legacy_alert_ids(state)  # 舊 warn/crit 拆 id 的殘留條目換新 id
        actions = _claim_push_actions(state, active_alerts, active_ids, now)

    for action in actions:
        if action.get("action") == "push":
            alert = action["alert"]
            aid = action["alert_id"]
            ok, errors = _push_alert(alert)
            if ok:
                pushed += 1
            else:
                failed += 1
                _log_failed(alert.get("title", aid), errors)
            _finalize_push_action(action, ok=ok, now=now)
            continue

        if action.get("action") == "recovery":
            ok = _push_recovery(action["alert_id"], action.get("prev") or {})
            if ok:
                recovered += 1
            # Preserve old behavior: even failed recovery clears state so it
            # won't retry forever for an alert that already disappeared.
            _finalize_recovery_action(action)

    return {
        "checked": len(alerts),
        "active_pushable": len(active_alerts),
        "pushed": pushed,
        "recovered": recovered,
        "failed": failed,
    }


# ────────────────────────────────────────────────────────────────────
# Public LLM-facing tool
# ────────────────────────────────────────────────────────────────────
def alert_push_status() -> str:
    """🟢 看當前 alert push 狀態（哪些 alert 還在追蹤、上次推什麼時候）。"""
    state = _load_state()
    out = ["🚨 Alert push 狀態"]
    out.append("─" * 60)
    if not state:
        out.append("  （沒在追蹤的 alert — 系統健康）")
        return "\n".join(out)
    out.append(f"  追蹤中：{len(state)} 個 alert")
    for aid, info in sorted(state.items()):
        level = info.get("level", "?")
        icon = {"crit": "🔴", "warn": "🟡"}.get(level, "❓")
        title = info.get("title", aid)[:60]
        last = info.get("last_pushed_at", "")[:16]
        first = info.get("first_seen_at", "")[:16]
        hours = _hours_since(info.get("last_pushed_at", ""))
        out.append(f"  {icon} {aid:25s} [{level}]")
        out.append(f"     {title}")
        out.append(f"     首見 {first}  上次推 {last}  ({hours:.1f}h 前)")
    return "\n".join(out)


def push_alerts_now() -> str:
    """🟢 手動觸發一次 alert 推送（給 cron / 大王手動 debug 用）。

    通常由 launchd com.xiaohong.alert_check 每 N 分鐘自動跑，這個 tool
    讓大王也能手動觸發確認 push channel 有沒有壞。
    """
    result = push_pending_alerts()
    if "error" in result:
        return f"❌ {result['error']}"
    out = ["🚨 Alert push 結果"]
    out.append("─" * 60)
    out.append(f"  掃描 alerts:  {result['checked']}")
    out.append(f"  可推送級別:    {result['active_pushable']}")
    out.append(f"  本輪推送:      {result['pushed']}")
    out.append(f"  恢復通知:      {result['recovered']}")
    if result["failed"]:
        out.append(f"  ⚠️  push 失敗:  {result['failed']}（看 var/logs/alert_push_failed.log）")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Entry point for daemon mode
# ────────────────────────────────────────────────────────────────────
def main() -> int:
    """給 launchd 直接呼叫的 entry point。"""
    # 沒走 rotate_log 的入口要自己掛行首時間戳（見 daemon_helpers.rotate_log docstring）
    from agent_core.daemon_helpers import install_stdout_timestamps
    install_stdout_timestamps()
    result = push_pending_alerts()
    print(f"[alert_pusher] 掃 {result.get('checked', 0)} alerts；"
          f"pushed={result.get('pushed', 0)} "
          f"recovered={result.get('recovered', 0)} "
          f"failed={result.get('failed', 0)}")
    if "error" in result:
        print(f"[alert_pusher] ⚠️ {result['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
