"""Health check daemon helper — 共用實作。

含 dedup 機制：只在問題集**不同於上次通知**時才寄信，避免持續性 🟡
警告每天造成 48 封一模一樣的通知。

之前 launchd/scripts/health_check.py 跟 agent_daemon.task_health_check
是雙份實作 — standalone 那份有 dedup，agent_daemon 那份沒有。drift 後
就靠運氣決定哪份生效。

統一改成這份共用實作。
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable


def _issues_hash(report: str) -> str:
    """Hash 只看 🔴/🟡 行，避免 timestamp / 數字 noise 干擾 dedup。"""
    issues = [line for line in report.split("\n") if "🔴" in line or "🟡" in line]
    return hashlib.sha1("\n".join(issues).encode("utf-8")).hexdigest()[:12]


def task_health_check(
    *,
    health_check_fn: Callable[..., str],
    load_state: Callable[[], dict[str, Any]],
    update_state: Callable[[Callable[[dict[str, Any]], None]], None],
    notify: Callable[..., Any],
) -> None:
    """跑健康檢查 + 自動修復；只在問題集**改變**時通知大王。

    Args:
        health_check_fn: agent_core.health.health_check
        load_state / update_state: agent_core.daemon_helpers
        notify: 寄 Gmail 通知（agent_core.daemon_helpers.notify）
    """
    # 行為政策信心衰減（每日一次，run_behavior_policy_decay 自己用 state 節流）。
    # 放在共用實作而非某個入口（健檢 Medium：以前只掛在 agent_daemon.py 的備用
    # 路徑，生產 launchd 跑的 launchd/scripts/health_check.py 拿不到 → 衰減
    # 從未在生產跑過）。兩個入口（standalone script / agent_daemon --task）
    # 都經這裡，自然都拿到。
    try:
        from agent_core.memory import run_behavior_policy_decay
        decay_note = run_behavior_policy_decay(load_state, update_state)
        if decay_note:
            print(decay_note)
    except Exception as e:
        print(f"[health_check] behavior_policy 衰減檢查失敗（略過）：{e}")

    report = health_check_fn(auto_repair=True)
    print(report)

    # 都健康 → 不通知
    if "🔴" not in report and "🟡" not in report:
        return

    h = _issues_hash(report)
    state = load_state()
    if state.get("health_check_last_notified_hash", "") == h:
        print("[health_check] 問題集跟上次通知一樣，skip 通知（dedup）")
        return

    notify(
        subject="【小紅健康檢查】發現問題並已嘗試修復",
        body=report,
        task_name="health_check",
    )
    update_state(lambda s: s.__setitem__("health_check_last_notified_hash", h))
