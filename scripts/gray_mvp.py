#!/usr/bin/env python3
"""Gray Trigger MVP — 可直接執行的生產異常聯防原型腳本.

用法：
    python scripts/gray_mvp.py                    # 互動式填報
    python scripts/gray_mvp.py --demo             # 使用內建 demo 資料，直接跑
    python scripts/gray_mvp.py --demo --no-notify # demo 但不發 Telegram

流程：
    ① 輸入異常資訊（訂單 / 產品 / 客戶 / 交期 / 原因 / 嚴重程度）
    ② Phase 1（並行）：查 Yellow 採購 ETA + Indigo 庫存與替代料
    ③ Phase 2（循序）：查 White 替代方案合規（由 Indigo 替代料驅動）
    ④ 計算新交期（ECD）
    ⑤ 輸出決策支援報告（+ 業務通知）
    ⑥ Telegram push（若已設定）
    ⑦ BigQuery exception log（若已設定）
    ⑧ 寫入本地 production_anomalies.json

Yellow / Indigo 已升級為 real agents；此腳本會透過 registry 自動使用目前
註冊的部門實作。若某部門暫時不可用，Gray workflow 仍會保守降級處理。
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import uuid
from datetime import date, timedelta
from pathlib import Path

# ── 讓 scripts/ 可以直接執行（repo root 在 sys.path）──────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── 顏色 / 排版輔助 ────────────────────────────────────────────────────────────
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_GRAY   = "\033[90m"
_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"


def _h(text: str, color: str = _CYAN) -> str:
    return f"{_BOLD}{color}{text}{_RESET}"


def _dim(text: str) -> str:
    return f"{_GRAY}{text}{_RESET}"


def _section(title: str) -> None:
    print(f"\n{_h('━'*50)}")
    print(_h(f"  {title}", _CYAN))
    print(_h('━'*50))


def _kv(key: str, value: object, color: str = "") -> None:
    val_str = str(value)
    if color:
        val_str = f"{color}{val_str}{_RESET}"
    print(f"  {_BOLD}{key:<22}{_RESET}{val_str}")


# ── Demo 資料 ──────────────────────────────────────────────────────────────────
_DEMO_PAYLOAD = {
    "order_id":     "ORD-2026-042",
    "product":      "PA-100",
    "customer":     "Decathlon Taiwan",
    "original_ecd": str(date.today() + timedelta(days=30)),
    "reason":       "主控 IC（U23）供應商停產，庫存僅剩 12 片，預計 4 週後斷料",
    "severity":     "high",
}

_REQUIRED_FIELDS: tuple[str, ...] = (
    "order_id", "product", "customer", "original_ecd", "reason", "severity",
)

# Mirrors trigger_gray's _SEVERITY_DELAY keys. Validating up front prevents
# trigger_gray's silent downgrade of unknown values to "medium" (which would
# yield a shorter delay / earlier ECD than the caller intended).
_SEVERITY_VALUES: frozenset = frozenset({"high", "medium", "low"})


def _validate_payload(payload: dict) -> None:
    """Raise ValueError if any required field is missing / null / non-string / blank,
    or if `original_ecd` is not a valid ISO date.

    Why include the ECD format check here:
      _calculate_ecd inside trigger_gray() validates the date too, but only AFTER
      Phase 1 (Yellow + Indigo) and Phase 2 (White — a real RAG search) have run.
      Validating up front avoids those external calls / log writes / potential
      Telegram side effects on input the script can never accept anyway.

    Earlier version coerced via `str(payload.get(f, ""))` and only checked emptiness;
    that let JSON `null` slip through (`str(None) == "None"`) and crash later at
    `payload["severity"].upper()`. We now check each failure mode separately so
    the user gets a precise error and never reaches the runtime trace.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"payload 必須是 dict，實際為 {type(payload).__name__}")

    bad: list[str] = []
    for f in _REQUIRED_FIELDS:
        if f not in payload:
            bad.append(f"{f}（缺少）")
            continue
        v = payload[f]
        if v is None:
            bad.append(f"{f}=null")
        elif not isinstance(v, str):
            bad.append(f"{f}={type(v).__name__}（必須是字串）")
        elif not v.strip():
            bad.append(f"{f}（空白）")

    # ECD must be ISO YYYY-MM-DD — only check when the basic field checks above
    # passed for original_ecd, so we don't double-report (`null` + `format invalid`).
    # Persist the stripped value back into payload so trigger_gray._calculate_ecd
    # (which calls date.fromisoformat() again) sees the same canonical form we
    # validated; otherwise " 2026-06-01 " passes preflight then dies in Phase 2
    # after peer queries have already executed.
    raw_ecd = payload.get("original_ecd")
    if isinstance(raw_ecd, str) and raw_ecd.strip():
        stripped = raw_ecd.strip()
        try:
            date.fromisoformat(stripped)
        except ValueError:
            bad.append(f"original_ecd={raw_ecd!r}（格式必須是 YYYY-MM-DD）")
        else:
            payload["original_ecd"] = stripped

    # Severity must normalize (strip + lower) to high / medium / low. trigger_gray
    # would silently downgrade anything else to "medium"; for --payload mode we
    # want a clear error instead of a wrong ECD. Normalize in place on success
    # so the rest of run() sees a canonical value.
    raw_sev = payload.get("severity")
    if isinstance(raw_sev, str) and raw_sev.strip():
        norm = raw_sev.strip().lower()
        if norm in _SEVERITY_VALUES:
            payload["severity"] = norm
        else:
            bad.append(f"severity={raw_sev!r}（必須是 high / medium / low）")

    if bad:
        raise ValueError(f"payload 必填欄位有問題：{', '.join(bad)}")

# ── 輸入收集 ────────────────────────────────────────────────────────────────────

def _prompt(label: str, default: str = "", required: bool = True) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        val = input(f"  {_BOLD}{label}{hint}{_RESET}: ").strip()
        if not val and default:
            return default
        if val or not required:
            return val
        print(f"  {_RED}必填，請重新輸入{_RESET}")


def collect_inputs() -> dict:
    print(_h("\n📋  請輸入生產異常資訊", _YELLOW))
    order_id     = _prompt("訂單編號")
    product      = _prompt("產品型號")
    customer     = _prompt("客戶名稱")
    today_str    = str(date.today())
    original_ecd = _prompt("原定交期 (YYYY-MM-DD)", default=today_str)
    reason       = _prompt("異常原因")
    print(f"  {_BOLD}嚴重程度{_RESET} [low/medium/high]", end="")
    sev = input(": ").strip().lower() or "medium"
    if sev not in ("low", "medium", "high"):
        sev = "medium"
    return dict(order_id=order_id, product=product, customer=customer,
                original_ecd=original_ecd, reason=reason, severity=sev)


# ── Phase progress hooks（monkey-patch _safe_query 以顯示進度）────────────────

def _wrap_safe_query_with_progress():
    """在 anomaly._safe_query 外層加 console 進度輸出（不影響業務邏輯）。

    回傳一個 callable — 呼叫即還原原始 _safe_query。把還原責任交給呼叫端
    （通常是 try/finally）以避免：
      • 同一 process 內多次 run() 造成 wrapper 疊層、重複輸出
      • 之後的 import 看到被汙染的 _an._safe_query
    """
    import agent_core.agents.gray_production.anomaly as _an
    original = _an._safe_query

    def _traced(agent, target, intent, payload, trace_id):
        print(_dim(f"    ↗  querying {target.value} / {intent} …"), end="", flush=True)
        result = original(agent, target, intent, payload, trace_id)
        status = result.get("status", "ok")
        icon = "✅" if status not in ("stub", "error") else ("🚧" if status == "stub" else "❌")
        print(f"  {icon}  {_dim(status)}")
        return result

    _an._safe_query = _traced

    def _restore() -> None:
        _an._safe_query = original

    return _restore


def _install_telegram_stub():
    """注入假 agent_core.telegram 進 sys.modules（不載入真的模組）。

    真的模組會 `import requests`，在沒裝 requests 的最小環境（CI / 測試）會炸；
    trigger_gray 內 `from agent_core.telegram import telegram_push` 會優先從
    sys.modules 取，所以塞 stub 即可繞過真實 import。

    回傳還原 callable —— 把汙染 sys.modules 的責任收緊到 try/finally 內，
    避免同 process 後續 run(notify=True) 沉默吞 Telegram。
    """
    import sys as _sys
    import types as _types
    _had_real = "agent_core.telegram" in _sys.modules
    _prev = _sys.modules.get("agent_core.telegram")
    _stub = _types.ModuleType("agent_core.telegram")
    _stub.telegram_push = lambda msg: "⚠ --no-notify (略過)"
    _sys.modules["agent_core.telegram"] = _stub

    def _restore() -> None:
        if _had_real:
            _sys.modules["agent_core.telegram"] = _prev
        else:
            _sys.modules.pop("agent_core.telegram", None)

    return _restore


# ── 執行流程 ────────────────────────────────────────────────────────────────────

def run(payload: dict, notify: bool = True) -> None:
    trace_id = uuid.uuid4().hex[:12]

    _section("Gray Trigger MVP — 生產異常聯防")
    print()
    _kv("訂單編號",  payload["order_id"],     _BOLD)
    _kv("產品型號",  payload["product"],       _BOLD)
    _kv("客戶",     payload["customer"])
    _kv("原定交期",  payload["original_ecd"])
    _kv("嚴重程度",  payload["severity"].upper(),
        _RED if payload["severity"] == "high" else
        _YELLOW if payload["severity"] == "medium" else _GREEN)
    _kv("異常原因",  textwrap.shorten(payload["reason"], 60, placeholder="…"))
    _kv("Trace ID", _dim(trace_id))

    # ── 建立 slim registry（只載 Gray / Yellow / Indigo / White，跳過 Gmail 相依）──
    print(f"\n{_dim('▸ 初始化 agent registry…')}")
    from agent_core.agents.registry import AgentRegistry
    from agent_core.agents.middleware import PermissionMiddleware
    from agent_core.agents.permission_matrix import Agent
    from agent_core.agents.gray_production import GrayProductionAgent
    from agent_core.agents.yellow_procurement import YellowProcurementAgent
    from agent_core.agents.indigo_warehouse import IndigoWarehouseAgent
    from agent_core.agents.white_legal import WhiteLegalAgent

    registry = AgentRegistry()
    middleware = PermissionMiddleware(registry)
    registry.bind_middleware(middleware)
    for cls in (GrayProductionAgent, YellowProcurementAgent,
                IndigoWarehouseAgent, WhiteLegalAgent):
        registry.register(cls())
    gray_agent = registry.get(Agent.GRAY)

    # ── Patch _safe_query 以顯示進度（restore 在 finally 收回，避免疊層）──────
    restore_safe_query = _wrap_safe_query_with_progress()

    # ── Phase 1 ────────────────────────────────────────────────────────────────
    _section("Phase 1 — Yellow (採購) + Indigo (倉庫)  [並行]")

    # ── Phase 2 + ECD + 報告（全走 trigger_gray）──────────────────────────────
    _section("Phase 2 — White (法務合規)  [循序，依 Indigo 替代料]")

    # ── --no-notify 注入 stub（同樣有 restore；避免 process-global 汙染）────────
    restore_telegram = None
    if not notify:
        restore_telegram = _install_telegram_stub()
        print(_dim("  ⚠  --no-notify：Telegram push 已停用（未載入 telegram 模組）"))

    # ── 呼叫真正的 trigger_gray ────────────────────────────────────────────────
    print(f"\n{_dim('▸ 執行 trigger_gray…')}")
    from agent_core.agents.gray_production import anomaly as _an
    try:
        result = _an.trigger_gray(
            agent=gray_agent,
            trace_id=trace_id,
            **payload,
        )
    except ValueError as exc:
        print(f"\n{_RED}❌  輸入錯誤：{exc}{_RESET}")
        sys.exit(1)
    finally:
        restore_safe_query()
        if restore_telegram is not None:
            restore_telegram()

    # ── 結果輸出 ────────────────────────────────────────────────────────────────
    _section("結果")
    _kv("新預計完工 (ECD)",  result["new_ecd"], _YELLOW)
    _kv("延誤天數",          result["delay_days"])
    _kv("Telegram 通知",    "✅ 已送出" if result["notifications_sent"] else "⚠  未送出")

    cq = result["cross_query"]
    print(f"\n  {_BOLD}跨部門查詢摘要{_RESET}")
    for dept, color in [("yellow", _YELLOW), ("indigo", _CYAN), ("white", _GREEN)]:
        st = cq[dept].get("status", "ok")
        icon = "🚧" if st == "stub" else ("❌" if st == "error" else "✅")
        extra = ""
        if dept == "yellow" and "eta" in cq[dept]:
            extra = f"  ETA={cq[dept]['eta']}"
        if dept == "indigo" and "in_stock" in cq[dept]:
            extra = f"  in_stock={cq[dept]['in_stock']}"
            alts = cq[dept].get("alternatives", [])
            if alts:
                extra += f"  alts={alts}"
        if dept == "white":
            extra = f"  alternatives_checked={cq[dept].get('alternatives_checked')}"
            if cq[dept].get("compliant_models"):
                extra += f"  compliant={cq[dept]['compliant_models']}"
        print(f"    {icon} {color}{dept:<8}{_RESET}  status={st}{_dim(extra)}")

    _section("決策支援報告")
    print(result["report"])

    _section("業務通知（Orange）")
    print(result["orange_notice"])

    _section("完成")
    print("  本地記錄: var/data/production_anomalies.json")
    if result["notifications_sent"]:
        print("  Telegram: ✅ Orange + Red 已收到通知")
    bq_state, bq_detail = _bq_status()
    if bq_state == "ok":
        # Codex P2 (round 2): only claim "已寫入" when we have proof the
        # insert ACTUALLY landed. flush_pending now returns a dict with
        # separate counters: joined (threads waited on), still_alive
        # (timed out — work incomplete), succeeded (rows accepted by BQ),
        # failed (insert/init errors). Earlier version printed ✅ purely
        # on thread-join — silently green even on timeouts and BQ errors.
        try:
            from agent_core.exception_logger import flush_pending
            stats = flush_pending(timeout=5.0)
        except Exception as exc:
            stats = {"joined": 0, "still_alive": 0, "succeeded": 0,
                     "failed": 0, "last_error": str(exc)}
            print(f"  {_dim(f'(flush_pending 失敗：{exc})')}")

        if stats["failed"] or stats["still_alive"]:
            # At least one row didn't land. Be loud — this is the
            # "silent telemetry loss" case Codex flagged.
            parts = []
            if stats["failed"]:
                parts.append(f"{stats['failed']} 條失敗")
            if stats["still_alive"]:
                parts.append(f"{stats['still_alive']} 條 timeout")
            why = "、".join(parts)
            err_tail = (f"（最後錯誤：{stats['last_error'][:80]}）"
                        if stats["last_error"] else "")
            print(f"  BigQuery:  {_RED}⚠️  {why} — 確認請查 {bq_detail}"
                  f"{err_tail}{_RESET}")
        elif stats["succeeded"]:
            print(f"  BigQuery:  ✅ 已寫入 {stats['succeeded']} 條 → {bq_detail}")
        else:
            # No threads + no results — log_event was a no-op path.
            # Conservative: don't claim success.
            print(f"  BigQuery:  ⚙️  已派送（未偵測到 in-flight 寫入） → {bq_detail}")
    elif bq_state == "skipped":
        print(f"  BigQuery:  {_dim('（' + bq_detail + '）')}")
    else:  # "error"
        print(f"  BigQuery:  {_RED}❌  {bq_detail}{_RESET}")
    print()


def _bq_status() -> tuple:
    """Probe BigQuery configuration synchronously. Returns (state, detail).

    state ∈ {"skipped", "ok", "error"}.

    Why probe instead of just checking env var:
      trigger_gray()'s exception_logger.log_event() spawns a daemon thread and
      catches all errors internally — so a wrong project ID, missing ADC, or
      revoked credentials still let the script finish "successfully" with our
      old "✅ 已記錄" line, hiding the fact that no event was actually logged.

      This probe synchronously initialises the client and ensures the dataset/
      table exist (idempotent — log_event would do the same if it ran first),
      which surfaces the common misconfiguration cases before we report status.
      It still cannot guarantee the actual insert row went through (that's the
      async thread's job), so success is reported as "已派送 (async)" rather
      than "已記錄".
    """
    import os
    if not os.environ.get("BIGQUERY_PROJECT_ID", "").strip():
        return ("skipped", "未設定 BIGQUERY_PROJECT_ID，略過")
    try:
        from agent_core.exception_logger import (
            _get_client, _ensure_table,
            _PROJECT_ID, _DATASET_ID, _TABLE_ID,
        )
        client = _get_client()
        if client is None:
            return ("error", "client 初始化失敗（檢查 ADC / GOOGLE_APPLICATION_CREDENTIALS）")
        _ensure_table(client)
        return ("ok", f"{_PROJECT_ID}.{_DATASET_ID}.{_TABLE_ID}")
    except Exception as exc:
        return ("error", f"{type(exc).__name__}: {exc}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Suppress noisy library warnings (e.g. "notification failed" in --no-notify mode).
    # Users who want verbose logs can set LOGLEVEL=DEBUG in the environment.
    import logging
    import os
    _lvl = os.environ.get("LOGLEVEL", "").upper()
    logging.basicConfig(level=getattr(logging, _lvl, logging.ERROR))

    parser = argparse.ArgumentParser(
        description="Gray Trigger MVP — 生產異常聯防腳本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            範例：
              python scripts/gray_mvp.py --demo             # 使用內建 demo 資料
              python scripts/gray_mvp.py --demo --no-notify # demo，不發 Telegram
              python scripts/gray_mvp.py                    # 互動式填報
        """),
    )
    parser.add_argument("--demo",      action="store_true", help="使用內建 demo 資料")
    parser.add_argument("--no-notify", action="store_true", help="略過 Telegram push")
    # 也允許直接傳 JSON（方便 CI / 測試）
    parser.add_argument("--payload",   type=str, default="",
                        help='JSON 字串，例如 \'{"order_id":"X","product":"Y",...}\'')
    args = parser.parse_args()

    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"{_RED}❌  --payload JSON 格式錯誤：{e}{_RESET}")
            sys.exit(1)
    elif args.demo:
        payload = _DEMO_PAYLOAD
        print(_dim("  [demo 模式] 使用內建資料"))
    else:
        payload = collect_inputs()

    try:
        _validate_payload(payload)
    except ValueError as exc:
        print(f"{_RED}❌  {exc}{_RESET}")
        sys.exit(1)

    run(payload, notify=not args.no_notify)


if __name__ == "__main__":
    main()
