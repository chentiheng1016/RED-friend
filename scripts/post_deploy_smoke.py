#!/usr/bin/env python3
"""Post-deploy smoke checks for the local RED installation.

This is intentionally a thin orchestrator around the same checks operators run
manually after daemon changes:

* health_check(auto_repair=False)
* red-status daemons
* tool_rpc_status()
* Google Drive list ping
* Telegram push ping
* npm run smoke:node
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass
class SmokeResult:
    name: str
    ok: bool
    detail: str


def _now() -> datetime:
    return datetime.now().astimezone()


def _clip(text: str, limit: int = 1600) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... <truncated>"


def _run(cmd: list[str], *, timeout: int = 60, env: dict | None = None) -> tuple[bool, str]:
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    return proc.returncode == 0, _clip(output)


def _system_load_context() -> str:
    """Snapshot system load + top CPU consumers for failure diagnostics.

    A runaway neighbor process (2026-06-14 incident: 20 orphan `yes` pinning
    load at 90) starves every check's process spawn, so unrelated checks like
    tool_rpc time out and look broken. Attaching this on failure turns a
    multi-step diagnosis ("why is tool_rpc slow?") into one glance.
    """
    lines: list[str] = []
    try:
        one, five, fifteen = os.getloadavg()
        lines.append(f"load avg: {one:.1f} {five:.1f} {fifteen:.1f} (1/5/15min)")
    except (AttributeError, OSError, ValueError):
        # AttributeError: os.getloadavg() is absent on some platforms (Windows).
        # A diagnostics helper must never be the thing that crashes the smoke.
        pass
    ok, out = _run(["ps", "-Ao", "%cpu,comm", "-r"], timeout=5)
    if ok and out:
        rows = [ln.strip() for ln in out.splitlines()[1:4] if ln.strip()]
        if rows:
            lines.append("top CPU: " + " | ".join(rows))
    return "\n".join(lines)


def _default_log_dir() -> Path:
    env_dir = os.environ.get("RED_SMOKE_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    from agent_core.logging_and_paths import RUNS_DIR

    return Path(RUNS_DIR) / "post_deploy_smoke"


def _write_run_log(
    results: list[SmokeResult],
    *,
    ok: bool,
    log_dir: str | None = None,
    now: datetime | None = None,
) -> Path:
    now = now or _now()
    target_dir = Path(log_dir).expanduser() if log_dir else _default_log_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    path = target_dir / f"post_deploy_smoke-{stamp}-{os.getpid()}.json"
    payload = {
        "timestamp": now.isoformat(timespec="seconds"),
        "ok": ok,
        "results": [asdict(item) for item in results],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _failure_message(results: list[SmokeResult], log_path: Path | None) -> str:
    failed = [item for item in results if not item.ok]
    lines = [
        "RED post-deploy smoke FAILED",
        f"Time: {_now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if log_path:
        lines.append(f"Log: {log_path}")
    lines.append("Failed checks:")
    for item in failed[:8]:
        detail = _clip(item.detail, 500)
        lines.append(f"- {item.name}: {detail or '(no detail)'}")
    if len(failed) > 8:
        lines.append(f"... and {len(failed) - 8} more failed check(s)")
    ctx = _system_load_context()
    if ctx:
        lines.append("")
        lines.append("System at failure:")
        lines.append(ctx)
    return _clip("\n".join(lines), 3500)


def _notify_failure(results: list[SmokeResult], log_path: Path | None) -> str:
    from agent_core.telegram import telegram_push

    return str(telegram_push(_failure_message(results, log_path)))


def check_health() -> SmokeResult:
    from agent_core.health import health_check

    report = health_check(auto_repair=False)
    # 只有 🔴 crit / 🟡 warn 才讓 smoke 紅；🔵 info（如「log 將自動輪替」這種良性通知）放行。
    # 否則每次部署都因良性 info 假性 FAILED（狼來了，6/6 近期 smoke 全紅）→ 真失敗反而被淹沒。
    # 與 daemon_health_check.task_health_check 同一條判準（只看 🔴/🟡）。
    ok = "🔴" not in report and "🟡" not in report
    return SmokeResult("health_check", ok, _clip(report))


def check_daemons() -> SmokeResult:
    ok, output = _run(["./bin/red-status", "daemons"], timeout=30)
    ok = ok and "down+last_exit非0 0" in output and "🔴" not in output
    return SmokeResult("red_status_daemons", ok, output)


def _tool_rpc_status_text() -> str:
    from agent_core.tool_rpc_server import tool_rpc_status

    return tool_rpc_status()


def _is_tool_rpc_ok(status: str) -> bool:
    return "tool_rpc reachable" in status or status.startswith("🟢")


def _probe_tool_rpc_with_grace(
    *,
    status_fn: Callable[[], str],
    grace_sec: float,
    backoff_sec: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    backoff_max_sec: float = 5.0,
) -> SmokeResult:
    """Retry the tool_rpc reachability probe until it succeeds or the warmup
    grace window elapses. The deadline is checked right after each failed probe
    (before sleeping), so total overshoot is bounded by one in-flight probe."""
    deadline = monotonic() + grace_sec
    backoff = backoff_sec
    attempts = 0
    last_status = ""
    while True:
        attempts += 1
        last_status = status_fn()
        if _is_tool_rpc_ok(last_status):
            detail = last_status
            if attempts > 1:
                detail = f"{last_status}\n(暖機後第 {attempts} 次探測通過)"
            return SmokeResult("tool_rpc", True, _clip(detail))
        if monotonic() >= deadline:
            detail = (
                f"{last_status}\n"
                f"(暖機 grace {grace_sec:g}s 內共 {attempts} 次探測仍 unreachable"
                " — 判定真失敗)"
            )
            return SmokeResult("tool_rpc", False, _clip(detail))
        sleep(min(backoff, backoff_max_sec))
        backoff *= 1.5


def check_tool_rpc() -> SmokeResult:
    """tool_rpc reachability probe with a cold-start warmup grace window.

    tool_rpc 是 post-merge fleet redeploy 裡 RESTART_DAEMONS 的最後一個，剛重啟時
    要 import 整套 agent_core 工具模組，第一輪 worker round-trip 常 >15s 才暖好；
    consolidated smoke 緊接著就跑，單發探測會在這個暖機窗口把其實正常、幾秒後就會
    healthy 的 tool_rpc 誤判成 unreachable，還推假警報到 Telegram（每次 post-merge
    全艦隊重啟都復發）。對策：在 grace window 內重試 + backoff，撐過窗口仍不通才算
    真失敗（保留偵測真正掛掉的能力）。可用 RED_SMOKE_TOOL_RPC_GRACE_S 調整窗口、
    設 0 退回單發語意。"""
    from agent_core.env_utils import env_float

    grace_sec = env_float(
        "RED_SMOKE_TOOL_RPC_GRACE_S", 45.0, min_value=0.0, max_value=300.0
    )
    backoff_sec = env_float(
        "RED_SMOKE_TOOL_RPC_BACKOFF_S", 2.0, min_value=0.1, max_value=30.0
    )
    return _probe_tool_rpc_with_grace(
        status_fn=_tool_rpc_status_text,
        grace_sec=grace_sec,
        backoff_sec=backoff_sec,
        sleep=time.sleep,
        monotonic=time.monotonic,
    )


def check_google_drive() -> SmokeResult:
    from agent_core.google_auth import get_service

    service = get_service("drive", "v3")
    result = service.files().list(
        pageSize=1,
        fields="files(id,name),nextPageToken",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files") or []
    detail = {
        "ok": True,
        "files_seen": len(files),
        "has_next": bool(result.get("nextPageToken")),
    }
    return SmokeResult("google_drive", True, json.dumps(detail, ensure_ascii=False))


def check_telegram(message: str) -> SmokeResult:
    from agent_core.telegram import telegram_push

    result = telegram_push(message)
    return SmokeResult("telegram_push", "✅" in str(result), str(result))


def check_anti_hallucination() -> SmokeResult:
    """Canary — run the historical Jalas 防水膜 scenario through every
    deterministic defense layer (no Gemini call) and confirm each one
    engages. A regression here is the loudest signal that the
    anti-hallucination stack has lost coverage; see docs/anti_hallucination.md.
    """
    from agent_core.citation_guard import check_citation, verify_claim
    from agent_core.correction_detector import detect_correction
    from agent_core.extractive_mode import is_fact_lookup

    user_q = "jalas這個客戶有用防水膜嗎"
    bad_reply = (
        "大王，Jalas 確實有在使用防水相關材料，主要是華峰 (Huafon) 的 PU468，"
        "這類材料通常具備防水功能。"
    )
    good_reply = (
        "Jalas 1055/1065/1155/1165 的 BOM 沒有任何防水膜（Sympatex / "
        "Gore-Tex 類）。主撥水材料是 CASPER 005 GA IDROREPELLEN+TAC "
        "[證據：1_ Pricing BOM Q2 2026 2peak 1055_1165 _final.xlsx]"
    )
    user_correction = "我查過jalas沒有用防水膜,你再確認一下"
    real_evidence = (
        "🔍 查到 8 筆材料\n📁 來源檔案：1_ Pricing BOM Q2 2026 "
        "2peak 1055_1165 _final.xlsx\n"
        "[撥水] sku=1055 | CASPER 005 GA IDROREPELLEN+TAC | "
        "vendor=Toung Far Industry C | price=1.8528 EUR/M2"
    )

    layers: list[tuple[str, bool]] = [
        ("extractive_mode fires on fact-lookup",
         is_fact_lookup(user_q)),
        ("citation_guard flags evidence-less bad reply",
         not check_citation(bad_reply).ok),
        ("citation_guard passes evidence-cited good reply",
         check_citation(good_reply).ok),
        ("correction_detector fires on '我查過...沒有'",
         detect_correction(user_correction).is_correction),
        ("verify_claim rejects fabricated facts",
         verify_claim(["Sympatex", "Huafon"], real_evidence).startswith("❌")),
        ("verify_claim accepts real facts",
         verify_claim(["CASPER 005", "1.8528"], real_evidence).startswith("✅")),
    ]

    failures = [name for name, ok in layers if not ok]
    detail = "\n".join(
        f"  {'✅' if ok else '❌'} {name}" for name, ok in layers
    )
    if failures:
        detail = (
            f"REGRESSION — {len(failures)}/{len(layers)} 層失靈：\n{detail}\n"
            f"處理：見 docs/anti_hallucination.md「What to do when a "
            f"hallucination still slips through」。"
        )
    else:
        detail = f"All {len(layers)} defense layers engaged.\n{detail}"
    return SmokeResult("anti_hallucination_canary", not failures, detail)


def check_node() -> SmokeResult:
    # node_modules is gitignored and can vanish (manual `git clean -xfd`, disk
    # cleanup) — that, not a code regression, is what reds this check. Auto-heal
    # from the lockfile so a missing install doesn't fail the smoke. Deps are
    # puppeteer-core (no Chromium download), so PUPPETEER_SKIP_DOWNLOAD keeps it fast.
    if not (REPO_ROOT / "node_modules").is_dir():
        _run(
            ["npm", "ci"],
            timeout=180,
            env={**os.environ, "PUPPETEER_SKIP_DOWNLOAD": "1"},
        )
    ok, output = _run(["npm", "run", "smoke:node"], timeout=60)
    return SmokeResult("node_puppeteer", ok, output)


def _run_step(name: str, fn: Callable[[], SmokeResult]) -> SmokeResult:
    try:
        return fn()
    except Exception as exc:
        return SmokeResult(name, False, f"{type(exc).__name__}: {exc}")


def _print_human(results: list[SmokeResult]) -> None:
    print(f"RED post-deploy smoke @ {_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)
    for item in results:
        icon = "OK" if item.ok else "FAIL"
        print(f"[{icon}] {item.name}")
        if item.detail:
            for line in item.detail.splitlines()[:10]:
                print(f"  {line}")
        print()
    ok_count = sum(1 for item in results if item.ok)
    print(f"Summary: {ok_count}/{len(results)} checks passed")
    if ok_count < len(results):
        ctx = _system_load_context()
        if ctx:
            print()
            print("System context at failure (load / top CPU):")
            for line in ctx.splitlines():
                print(f"  {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run RED post-deploy smoke checks.")
    parser.add_argument("--skip-google", action="store_true", help="Skip Google Drive API ping.")
    parser.add_argument("--skip-telegram", action="store_true", help="Skip Telegram push ping.")
    parser.add_argument("--skip-node", action="store_true", help="Skip npm Puppeteer smoke.")
    parser.add_argument("--skip-tool-rpc", action="store_true", help="Skip tool_rpc socket/worker ping.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--no-log", action="store_true", help="Do not write a smoke run JSON record.")
    parser.add_argument("--no-failure-notify", action="store_true", help="Do not send Telegram alert on failure.")
    parser.add_argument(
        "--log-dir",
        default="",
        help="Directory for smoke run JSON records. Default: var/runs/post_deploy_smoke.",
    )
    parser.add_argument(
        "--telegram-message",
        default="",
        help="Custom Telegram smoke message. Default includes timestamp.",
    )
    args = parser.parse_args(argv)

    os.chdir(REPO_ROOT)
    os.environ.setdefault("AGENT_DAEMON_MODE", "1")
    # launchd daemon 全靠 plist 帶 RED_CHROMA_HTTP_URL（bin/inject-plist-env 注入）；
    # 從沒 export 的 shell（Claude session、手動終端）跑 smoke 時補上同一個值，
    # 否則 chroma_backend 防護會在共用 server 活著時拒開 PersistentClient，
    # health_check 誤紅（2026-06-12 誤報）。單一定義：chroma_backend.SHARED_SERVER_URL。
    from agent_core.chroma_backend import SHARED_SERVER_URL

    os.environ.setdefault("RED_CHROMA_HTTP_URL", SHARED_SERVER_URL)

    checks: list[tuple[str, Callable[[], SmokeResult]]] = [
        ("health_check", check_health),
        ("red_status_daemons", check_daemons),
        ("anti_hallucination_canary", check_anti_hallucination),
    ]
    if not args.skip_tool_rpc:
        checks.append(("tool_rpc", check_tool_rpc))
    if not args.skip_google:
        checks.append(("google_drive", check_google_drive))
    if not args.skip_telegram:
        message = args.telegram_message or (
            "RED post-deploy smoke OK @ "
            + _now().strftime("%Y-%m-%d %H:%M:%S")
        )
        checks.append(("telegram_push", lambda: check_telegram(message)))
    if not args.skip_node:
        checks.append(("node_puppeteer", check_node))

    results = [_run_step(name, fn) for name, fn in checks]
    ok = all(item.ok for item in results)
    log_path: Path | None = None
    if not args.no_log:
        try:
            log_path = _write_run_log(results, ok=ok, log_dir=args.log_dir or None)
        except Exception as exc:
            print(f"[post-deploy smoke] WARN: failed to write run log: {exc}", file=sys.stderr)
    notify_enabled = os.environ.get("RED_SMOKE_NOTIFY_FAILURE", "1").lower() not in {"0", "false", "no"}
    if not ok and notify_enabled and not args.no_failure_notify and not args.skip_telegram:
        try:
            notify_result = _notify_failure(results, log_path)
            print(f"[post-deploy smoke] failure notification: {notify_result}", file=sys.stderr)
        except Exception as exc:
            print(f"[post-deploy smoke] WARN: failed to send failure notification: {exc}", file=sys.stderr)
    if args.json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        _print_human(results)
        if log_path:
            print(f"Log: {log_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
