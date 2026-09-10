"""Policy engine — 單一可解釋的「這個 tool call 是否該執行」決策入口。

問題：
  目前的允/拒邏輯散落在四個地方：
    tool_tiers.get_check_method  channel × tier
    tg_auth.wrap_sensitive_tool  +確認 / +雙確認 / one-shot
    tool_budgets.check_budget    日 / 時 上限
    dry_run                      destructive op preview

  每個地方各自決策、各自寫訊息。當 LLM 被擋下，要追「為什麼擋」常要看 4 個
  log file。也沒有單一 env override 入口給 sysadmin（e.g. 想暫時把 run_shell
  封掉、即使在 REPL 也禁，沒地方設）。

設計：
  純決策層 — 不執行任何 side effect、不消耗 confirm token、不 record audit。
  輸入 context dict，輸出 PolicyDecision（allow/refuse + 多層 reason）。
  caller（wrap_sensitive_tool）拿這個結果決定下一步。

  policy_engine 看以下訊號：
    1. 環境變數 override（最高優先 — sysadmin 凌駕任何 tier）
       RED_BLOCK_TOOL=run_shell,delete_*    永久封某些 tool（即使 REPL 也擋）
       RED_FORCE_DRY_RUN_FOR=delete_*        強制 dry-run 該類 tool
       RED_RAISE_TIER_TO_DANGEROUS=batch_*   把該類 tool 升級到要 +雙確認
    2. tool_tiers tier × channel 矩陣
    3. risk_guard content-level 偵測（args 含「rm -rf」等樣式）
    4. dry_run mode 狀態
    5. （call site 提供）confirmation 狀態 + budget 狀態（不在這裡查；
       caller 已經查過會把結果一起塞 context）

  輸出 PolicyDecision：
    allow: bool
    reason_layer: 'env_override' / 'tier' / 'risk_guard' / 'dry_run' /
                  'confirmation' / 'budget' / ...
    reason: 人類可讀解釋
    suggested_action: 「要怎麼做才能跑起來」
    risk_score: 0-100（從 risk_guard 拿）

每個被處理的 call 都寫一筆到 var/state/policy_decisions.jsonl 給 dashboard
聚合（最近被擋幾次 / 哪個 layer 最常擋）。
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger


# ────────────────────────────────────────────────────────────────────
# PolicyDecision dataclass
# ────────────────────────────────────────────────────────────────────
@dataclass
class PolicyDecision:
    allow: bool
    reason_layer: str = ""           # 'env_override' / 'tier' / 'risk' / ...
    reason: str = ""                 # 人類可讀
    suggested_action: str = ""       # 「該怎麼讓它過」
    risk_score: int = 0              # 0-100
    risk_signals: list = field(default_factory=list)
    forced_dry_run: bool = False     # env 要求強制走 dry-run

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "reason_layer": self.reason_layer,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "risk_score": self.risk_score,
            "risk_signals_count": len(self.risk_signals),
            "forced_dry_run": self.forced_dry_run,
        }


# ────────────────────────────────────────────────────────────────────
# Env override helpers
# ────────────────────────────────────────────────────────────────────
def _matches_glob(name: str, patterns: list[str]) -> str:
    """檢查 name 是否 match 任一 pattern（支援 * glob）。回 matched pattern 或空字串。"""
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        if fnmatch.fnmatch(name, p):
            return p
    return ""


def _env_list(env_var: str) -> list[str]:
    """讀 env，逗號切分，去空白 / 空字串。"""
    raw = os.environ.get(env_var, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_blocked_by_env(tool_name: str) -> str:
    """RED_BLOCK_TOOL 含 tool_name 嗎？回 matched pattern 或空字串。"""
    return _matches_glob(tool_name, _env_list("RED_BLOCK_TOOL"))


def _is_forced_dry_run(tool_name: str) -> str:
    """RED_FORCE_DRY_RUN_FOR 含此 tool 嗎？"""
    return _matches_glob(tool_name, _env_list("RED_FORCE_DRY_RUN_FOR"))


def _is_raised_to_dangerous(tool_name: str) -> str:
    """RED_RAISE_TIER_TO_DANGEROUS 含此 tool 嗎？"""
    return _matches_glob(tool_name, _env_list("RED_RAISE_TIER_TO_DANGEROUS"))


# ────────────────────────────────────────────────────────────────────
# Decision log
# ────────────────────────────────────────────────────────────────────
_LOG_FILE = os.path.join(STATE_DIR, "policy_decisions.jsonl")
_LOG_LOCK = threading.Lock()
_LOG_MAX_LINES = 1000
# evaluate_policy logs on every tool call, and ~10 bot processes all append to
# the SAME _LOG_FILE. A per-process line counter can't see the other processes'
# writes (the shared file would grow ~N× before any single process rotates, then
# pay a huge readlines()), so gate rotation on the REAL shared file size with a
# cheap os.stat() — the expensive readlines()/truncate only runs once the file
# actually crosses the byte budget. ~512KB ≈ a few thousand decision lines.
_LOG_ROTATE_BYTES = 512 * 1024
_PG_POLICY_WARNING_UNTIL = 0.0


def _warn_pg_policy_fallback(exc: Exception) -> None:
    global _PG_POLICY_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_POLICY_WARNING_UNTIL:
        return
    _PG_POLICY_WARNING_UNTIL = now + 30
    logger.warning("Postgres policy_engine failed; falling back to JSONL: %s", exc)


def _pg_policy_store():
    try:
        from agent_core import operational_policy_engine as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - policy decisions must stay best-effort
        _warn_pg_policy_fallback(exc)
    return None


def _log_decision(
    tool_name: str,
    channel: str,
    decision: PolicyDecision,
    *,
    source: str = "",
    user: str = "",
    mode: str = "",
) -> None:
    """append 一筆到 jsonl（檔案超過 _LOG_ROTATE_BYTES 才 rotate 到尾 _LOG_MAX_LINES 行）；Postgres 可用時優先寫 PG。失敗 silent。"""
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "tool": tool_name,
        "channel": channel,
        "source": source,
        "user": user,
        "mode": mode,
        **decision.to_dict(),
    }
    store = _pg_policy_store()
    if store is not None:
        try:
            store.write_decision(entry)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local decision log
            _warn_pg_policy_fallback(exc)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _LOG_LOCK:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            try:
                if os.stat(_LOG_FILE).st_size > _LOG_ROTATE_BYTES:
                    with open(_LOG_FILE, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    if len(lines) > _LOG_MAX_LINES:
                        _atomic_write_text(_LOG_FILE,
                                            "".join(lines[-_LOG_MAX_LINES:]))
            except Exception:
                pass
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# Main entry
# ────────────────────────────────────────────────────────────────────
def evaluate_policy(tool_name: str, *,
                     channel: str = "telegram",
                     mode: str = "live",          # 'live' / 'dry_run'
                     source: str = "user_message",
                     user: str = "default",
                     kwargs: dict | None = None) -> PolicyDecision:
    """🟢 中央決策：這個 tool call 該不該跑？回 PolicyDecision。

    優先序（從上往下，第一個會 return 的 layer 即定案）：
      1. env override RED_BLOCK_TOOL → refuse
      2. content-level risk_guard score ≥ 90 → refuse（critical）
      3. tier == LOCKED + channel 不允許 → refuse
      4. tier × channel 矩陣決定要 token / token+warn / allow
      5. env RED_FORCE_DRY_RUN_FOR → forced_dry_run = True（caller 處理）
      6. env RED_RAISE_TIER_TO_DANGEROUS → 升級為 token+warn 路徑
    注：tier/method（下方標「Layer 2」的區塊）在 risk_guard 前就先算好，但
        LOCKED 觸發 refuse 的判斷實際落在 risk_guard 之後 — 故 return 優先序如上。

    這函式**不查** confirm token / budget — 那些是 stateful 由 caller 自己
    跑（context 經 wrap_sensitive_tool 已知）。policy_engine 只回「依規則
    應該走哪條路」+ risk_score 給 caller 參考。

    Args:
        tool_name: 工具名
        channel: 'telegram' / 'voice' / 'daemon' / 'repl'
        mode: 'live' / 'dry_run'
        source: 'user_message' / 'scheduled_task' / 'sub_agent' 等
        user: chat_id 或 username（暫保留欄位，多用戶才有意義）
        kwargs: tool 即將傳入的 kwargs（給 risk_guard 掃）
    """
    kwargs = kwargs or {}

    # ── Layer 1: env override block（最強）──
    blocked = _is_blocked_by_env(tool_name)
    if blocked:
        d = PolicyDecision(
            allow=False, reason_layer="env_override",
            reason=f"環境變數 RED_BLOCK_TOOL 含 '{blocked}' — 連 REPL 也禁",
            suggested_action="unset RED_BLOCK_TOOL 或編輯該變數",
        )
        _log_decision(tool_name, channel, d, source=source, user=user, mode=mode)
        return d

    # ── Layer 2: tier × channel ──
    try:
        from agent_core.tool_tiers import (
            get_tier, get_check_method, refusal_message,
            TIER_LOCKED, TIER_DANGEROUS,
        )
    except Exception as e:
        # 模組壞掉 → fail-safe 拒
        return PolicyDecision(
            allow=False, reason_layer="internal",
            reason=f"tool_tiers 讀取失敗：{e}",
        )
    tier = get_tier(tool_name)
    method = get_check_method(channel, tier)

    # ── Layer 3: content-level risk_guard ──
    risk_score = 0
    risk_signals: list = []
    try:
        from agent_core.risk_guard import scan_for_risk
        a = scan_for_risk(tool_name, kwargs)
        risk_score = a.score
        risk_signals = a.signals
    except Exception:
        pass

    # critical risk → 直接拒（即使有兩道確認）
    if risk_score >= 90:
        top = risk_signals[0] if risk_signals else None
        d = PolicyDecision(
            allow=False, reason_layer="risk_guard",
            reason=f"args 含 critical 風險樣式（score={risk_score}）"
                   + (f"：{top.pattern}" if top else ""),
            suggested_action="檢查 LLM 提供的 args — 可能被 prompt-injection",
            risk_score=risk_score, risk_signals=risk_signals,
        )
        _log_decision(tool_name, channel, d, source=source, user=user, mode=mode)
        return d

    # ── Channel refuse（LOCKED on Telegram / DANGEROUS on voice）──
    if method == "refuse":
        d = PolicyDecision(
            allow=False, reason_layer="tier",
            reason=refusal_message(channel, tool_name),
            suggested_action=("LOCKED tool 請改用 mac REPL"
                              if tier == TIER_LOCKED
                              else "voice 拒絕 DANGEROUS — 改用 Telegram"),
            risk_score=risk_score, risk_signals=risk_signals,
        )
        _log_decision(tool_name, channel, d, source=source, user=user, mode=mode)
        return d

    # ── Layer 4: forced dry-run ──
    forced_dr = bool(_is_forced_dry_run(tool_name))

    # ── Layer 5: 升級到 DANGEROUS ──
    raised = _is_raised_to_dangerous(tool_name)
    if raised and method == "token":
        method = "token+warn"

    # 若已是 'allow' 直接過；'token' / 'token+warn' caller 會跑 confirm 邏輯
    d = PolicyDecision(
        allow=True,  # 從 policy 角度說「可以走 token gate」— caller 還要跑 confirm
        reason_layer="tier",
        reason=f"tier={tier}, channel={channel}, method={method}"
               + (f", risk_score={risk_score}" if risk_score else ""),
        suggested_action="",
        risk_score=risk_score, risk_signals=risk_signals,
        forced_dry_run=forced_dr,
    )
    _log_decision(tool_name, channel, d, source=source, user=user, mode=mode)
    return d


# ────────────────────────────────────────────────────────────────────
# Dashboard / introspection helpers
# ────────────────────────────────────────────────────────────────────
def policy_summary(hours: int = 24) -> dict:
    """過去 N 小時的 policy decision 分布（給 dashboard）。"""
    store = _pg_policy_store()
    if store is not None:
        try:
            return _summary_from_records(store.load_decisions(hours=hours, limit=50000))
        except Exception as exc:  # noqa: BLE001 - fall back to local dashboard log
            _warn_pg_policy_fallback(exc)
    if not os.path.isfile(_LOG_FILE):
        return {"total": 0, "allowed": 0, "refused": 0,
                 "by_layer": {}, "by_tool": {}}
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    total = allowed = refused = 0
    by_layer: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except (ValueError, TypeError):
                    continue
                if (r.get("at") or "") < cutoff:
                    continue
                total += 1
                if r.get("allow"):
                    allowed += 1
                else:
                    refused += 1
                    layer = r.get("reason_layer") or "?"
                    by_layer[layer] = by_layer.get(layer, 0) + 1
                tool = r.get("tool", "?")
                by_tool[tool] = by_tool.get(tool, 0) + 1
    except (ValueError, TypeError):
        pass
    return {
        "total": total, "allowed": allowed, "refused": refused,
        "by_layer": by_layer, "by_tool": by_tool,
    }


def _summary_from_records(records: list[dict[str, Any]]) -> dict:
    total = allowed = refused = 0
    by_layer: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    for r in records:
        total += 1
        if r.get("allow"):
            allowed += 1
        else:
            refused += 1
            layer = r.get("reason_layer") or "?"
            by_layer[layer] = by_layer.get(layer, 0) + 1
        tool = r.get("tool", "?")
        by_tool[tool] = by_tool.get(tool, 0) + 1
    return {
        "total": total, "allowed": allowed, "refused": refused,
        "by_layer": by_layer, "by_tool": by_tool,
    }


def policy_recent(hours: int = 24, limit: int = 20) -> str:
    """🟢 看最近 N 小時 policy 決策（被擋的優先 + 統計）。"""
    store = _pg_policy_store()
    records: list[dict[str, Any]] | None = None
    if store is not None:
        try:
            records = store.load_decisions(hours=hours, limit=50000)
        except Exception as exc:  # noqa: BLE001 - fall back to local dashboard log
            _warn_pg_policy_fallback(exc)
    if not os.path.isfile(_LOG_FILE):
        if records is None:
            return "  （尚無 policy_decisions.jsonl — 還沒任何 decision）"
        if not records:
            return f"  （過去 {hours}h 沒有 decision）"
    s = _summary_from_records(records) if records is not None else policy_summary(hours)
    if s["total"] == 0:
        return f"  （過去 {hours}h 沒有 decision）"
    out = [f"🛡️ Policy 決策 — 過去 {hours}h"]
    out.append("─" * 60)
    out.append(f"  共 {s['total']} 次  allow={s['allowed']}  refuse={s['refused']}")
    if s["by_layer"]:
        out.append("  refuse 來源（哪一層擋的）：")
        for layer, n in sorted(s["by_layer"].items(), key=lambda kv: -kv[1]):
            out.append(f"    {layer:20s} {n}")
    # 最近被擋
    refusals = []
    if records is not None:
        refusals = [r for r in records if not r.get("allow")]
    else:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        try:
            with open(_LOG_FILE, "r", encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except (ValueError, TypeError):
                        continue
                    if (r.get("at") or "") < cutoff:
                        continue
                    if not r.get("allow"):
                        refusals.append(r)
        except (ValueError, TypeError):
            pass
    if refusals:
        out.append("")
        out.append(f"  最近被擋（取最後 {min(limit, len(refusals))} 筆）：")
        for r in refusals[-limit:]:
            when = (r.get("at") or "")[5:16]
            tool = r.get("tool", "?")[:30]
            layer = r.get("reason_layer", "?")
            reason = (r.get("reason") or "")[:60]
            out.append(f"    [{when}] {tool:30s} layer={layer:14s} {reason}")
    return "\n".join(out)


def env_override_status() -> str:
    """🟢 看當前 env override 設了什麼（給大王 / sysadmin 確認）。"""
    out = ["🛠️ Policy env override 當前狀態"]
    out.append("─" * 60)
    for var, label in (
        ("RED_BLOCK_TOOL", "永久封"),
        ("RED_FORCE_DRY_RUN_FOR", "強制 dry-run"),
        ("RED_RAISE_TIER_TO_DANGEROUS", "升級 tier"),
    ):
        val = os.environ.get(var, "")
        if val:
            out.append(f"  {var}={val}  ({label})")
        else:
            out.append(f"  {var}=（未設） ({label})")
    out.append("")
    out.append("💡 設定例：export RED_BLOCK_TOOL=run_shell,delete_*")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Public LLM-facing tool — 看單一 call 會不會過
# ────────────────────────────────────────────────────────────────────
def evaluate_policy_text(tool_name: str, channel: str = "telegram",
                          kwargs_json: str = "") -> str:
    """🟢 模擬「這個 tool call 會被 policy_engine 怎麼判」(不真執行)。

    給 LLM / 大王 dry-run 看：「我打算叫 run_shell('rm -rf /tmp/x')，會過嗎？」
    """
    if not tool_name:
        return "❌ tool_name 必填"
    try:
        kwargs = json.loads(kwargs_json) if kwargs_json else {}
        if not isinstance(kwargs, dict):
            return "❌ kwargs_json 須為 dict 樣式 JSON"
    except json.JSONDecodeError as e:
        return f"❌ kwargs_json 不是合法 JSON：{e}"
    d = evaluate_policy(tool_name, channel=channel, kwargs=kwargs)
    icon = "✅" if d.allow else "❌"
    out = [
        f"{icon} policy decision for `{tool_name}` on {channel}",
        f"   allow: {d.allow}",
        f"   layer: {d.reason_layer}",
        f"   reason: {d.reason[:200]}",
    ]
    if d.suggested_action:
        out.append(f"   suggested: {d.suggested_action}")
    if d.risk_score:
        out.append(f"   risk_score: {d.risk_score}")
    if d.forced_dry_run:
        out.append("   ⚠️ forced_dry_run（env 要求）")
    return "\n".join(out)
