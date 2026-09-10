"""Unified status dashboard for RED.

把散落各處的 health/cost/RAG/scheduled/audit 訊號彙總成一份可讀文字。
讓 大王 一個指令（或 Telegram 一句「目前狀況？」）就看到整個系統健康度。

Sections:
  1. Daemon 活著與否（launchctl list | grep com.xiaohong）
  2. 最近審計過的工具呼叫（runs/index.jsonl 最後 N 筆 + 成功/失敗）
  3. Gmail ingest 狀態（lake parquet size + 最後寫入時間）
  4. RAG 向量庫狀態（chroma 條目數 + BM25 索引狀態 + entity alias 數）
  5. 今日 Gemini 成本（cost.jsonl 計算當日 USD）
  6. 最近錯誤 log（var/logs/*.log 掃 ERROR / Traceback / 失敗）
  7. Scheduled tasks 清單（daemon_tasks.json）

設計原則：
  - **唯讀**：dashboard 只開檔讀，不寫不改任何 state
  - **失敗安全**：任何 section 拋例外不會吃掉整份報告，會印「(讀取失敗：reason)」
  - **redact**：error log 那段尤其要過 log_redact，避免歷史錯誤訊息含 inline secret
  - **體積**：輸出目標 ~2KB（Telegram 1 則訊息可放）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, date, timedelta
from typing import Any

from agent_core.logging_and_paths import (
    _LOG_DIR,
    RUNS_DIR,
    INTERNAL_LAKE_DIR,
)
from agent_core.daemon_launchd_state import (
    is_benign_stopped_daemon,
    is_known_daemon_recovering,
    short_launchd_label,
)


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
def _safe_section(fn, title: str) -> str:
    """讓任一 section 失敗都不會炸整份 dashboard。"""
    try:
        return fn()
    except Exception as e:
        return f"  ⚠️ ({title} 讀取失敗：{type(e).__name__}: {str(e)[:80]})"


def _human_age(ts: float) -> str:
    """Unix ts → 人類可讀「X 分鐘前」。"""
    now = datetime.now().timestamp()
    delta = now - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if isinstance(n, float) else f"{n}{unit}"
        n = n / 1024 if isinstance(n, float) else n // 1024
    return f"{n}TB"


# ────────────────────────────────────────────────────────────────────
# 1. Daemon 健康
# ────────────────────────────────────────────────────────────────────
def _section_daemons() -> str:
    """跑 `launchctl list | grep com.xiaohong` 解析每個 daemon 狀態。

    launchctl list 回傳格式：PID  LastExitCode  Label
      PID = `-` 表 not running（cron 或剛跑完）
      LastExitCode 0 = 上次跑成功；非 0 = 失敗
    """
    try:
        out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        return f"  ❌ launchctl 無法執行：{e}"
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()
        suffix = f"：{detail[:160]}" if detail else ""
        return f"  ❌ launchctl 回傳非 0（exit {out.returncode}）{suffix}"

    lines = []
    daemons = []
    for raw in out.stdout.splitlines():
        if "com.xiaohong" not in raw:
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        pid_s, exit_s, label = parts[0], parts[1], parts[2]
        try:
            exit_code = int(exit_s)
        except ValueError:
            exit_code = -1
        running = pid_s != "-"
        short = short_launchd_label(label)
        recovering = (not running and is_known_daemon_recovering(short))
        benign_done = (not running and is_benign_stopped_daemon(short, exit_code))
        daemons.append((label, running, exit_code, pid_s, recovering, benign_done))

    if not daemons:
        return "  （未找到任何 com.xiaohong.* daemon — launchd 沒裝？）"

    # 摘要：alive count / currently-down failure count. A running daemon can
    # retain a historical non-zero last_exit; that is not actionable here.
    alive = sum(1 for _, r, _, _, recovering, _ in daemons if r or recovering)
    failed = sum(1 for _, r, e, _, recovering, benign_done in daemons
                 if not r and e != 0 and not recovering and not benign_done)
    lines.append(f"  共 {len(daemons)} 個 daemon："
                 f"alive {alive} / down+last_exit非0 {failed}")
    # 失敗的優先顯示
    daemons.sort(key=lambda x: (
        x[2] == 0 or x[4] or x[5],
        not x[1] and not x[4] and not x[5],
    ))
    for label, running, exit_code, pid, recovering, benign_done in daemons[:13]:
        if running:
            status = f"🟢 running (pid={pid})"
        elif recovering:
            status = f"🟡 active now (launchd last exit {exit_code})"
        elif benign_done:
            status = f"⚪ completed one-shot (exit {exit_code})"
        elif exit_code == 0:
            status = "⚪ idle (last exit 0)"
        else:
            status = f"🔴 last exit {exit_code}"
        short = short_launchd_label(label)
        lines.append(f"    {short:25s} {status}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 2. 最近審計工具呼叫
# ────────────────────────────────────────────────────────────────────
def _section_tiers() -> str:
    """權限分級摘要（4 tier 個別 count + LOCKED/DANGEROUS 細目）。"""
    try:
        from agent_core.tool_tiers import (
            all_tools_by_tier, _TIER_ICON, _TIER_DESC,
            TIER_SAFE, TIER_CONFIRM, TIER_DANGEROUS, TIER_LOCKED,
        )
    except Exception as e:
        return f"  ⚠️ tier 模組讀取失敗：{e}"
    buckets = all_tools_by_tier()
    if not buckets:
        return "  （tools_list 不可用）"
    lines = []
    total = sum(len(v) for v in buckets.values())
    for t in (TIER_SAFE, TIER_CONFIRM, TIER_DANGEROUS, TIER_LOCKED):
        names = buckets[t]
        icon = _TIER_ICON[t]
        lines.append(f"  {icon} {t:10s} {len(names):4d} 個   ({len(names)*100//max(1,total)}%)")
    # 列 LOCKED 細目（最少最重要）
    locked = buckets.get(TIER_LOCKED) or []
    if locked:
        lines.append(f"  🔒 LOCKED list（{len(locked)} 個 — Telegram/voice 拒絕）：")
        for n in locked[:8]:
            lines.append(f"        {n}")
        if len(locked) > 8:
            lines.append(f"        ... 還有 {len(locked) - 8} 個")
    return "\n".join(lines)


def _section_alerts(alerts: list | None = None) -> str:
    """🚨 系統警示（如有）— 從 dashboard_alerts.check_alerts() 拉。

    alerts: 已算好的 check_alerts() 結果（system_status 內與 health 段共用）。
    """
    if alerts is None:
        from agent_core.dashboard_alerts import check_alerts
        alerts = check_alerts()
    else:
        alerts = list(alerts)  # 下面會就地 sort，別動 caller 的共用 list
    if not alerts:
        return "  ✅ 目前無警示"
    crit = sum(1 for a in alerts if a.get("level") == "crit")
    warn = sum(1 for a in alerts if a.get("level") == "warn")
    out = [f"  🚨 {crit} crit / {warn} warn"]
    alerts.sort(key=lambda a: (a.get("level") != "crit", a.get("id")))
    for a in alerts[:5]:
        icon = "🔴" if a.get("level") == "crit" else "🟡"
        out.append(f"    {icon} {a['title']}")
        out.append(f"        {a['detail'][:220]}")
    return "\n".join(out)


def _section_recent_runs(limit: int = 8) -> str:
    """runs/index.jsonl 最後 N 筆 — 每行是 short_result + status。"""
    index_path = os.path.join(RUNS_DIR, "index.jsonl")
    if not os.path.isfile(index_path):
        return "  （尚無 runs/index.jsonl — 沒有任何 audited tool 跑過）"
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except Exception as e:
        return f"  ⚠️ 讀 index.jsonl 失敗：{e}"
    if not lines:
        return "  （index.jsonl 空）"

    # parse 最後 200 筆統計
    recent = lines[-200:]
    ok = err = run = 0
    parsed = []
    for ln in recent:
        try:
            r = json.loads(ln)
        except (ValueError, TypeError):
            continue
        st = r.get("status", "?")
        if st == "success":
            ok += 1
        elif st == "error":
            err += 1
        elif st == "running":
            run += 1
        parsed.append(r)

    # Trend lines（vs 昨日）放最前面
    out = []
    try:
        from agent_core.dashboard_trends import runs_trend, format_runs_trend_lines
        out.extend(format_runs_trend_lines(runs_trend()))
    except Exception:
        pass
    out.append(f"  累計 {len(parsed)} 次審計（成功 {ok} / 失敗 {err} / 跑中 {run}）")
    # 顯示 failure 優先 + 最新 limit 筆
    failures = [r for r in parsed if r.get("status") == "error"][-3:]
    most_recent = parsed[-limit:]
    seen_ids = set()
    rows = []
    for r in failures + most_recent:
        rid = r.get("id")
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        rows.append(r)
    rows = rows[-limit:]
    for r in rows:
        st = r.get("status", "?")
        icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(st, "?")
        when = (r.get("started_at") or "")[5:16]  # MM-DD HH:MM
        tool = r.get("tool", "?")[:24]
        elapsed = r.get("elapsed_sec", 0)
        out.append(f"    {icon} {when}  {tool:24s} ({elapsed}s)")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 3. Gmail ingest 狀態
# ────────────────────────────────────────────────────────────────────
def _section_gmail_ingest() -> str:
    """email lake parquet 存在嗎？多大、多少列、最後一封信日期？"""
    parquet = os.path.join(INTERNAL_LAKE_DIR, "emails.parquet")
    if not os.path.isfile(parquet):
        # try alternate
        alt = os.path.join(os.path.dirname(INTERNAL_LAKE_DIR),
                           "data_lake_internal", "emails_master.parquet")
        parquet = alt if os.path.isfile(alt) else parquet
    if not os.path.isfile(parquet):
        return "  （未找到 lake parquet — Gmail ingest 沒跑過或路徑改了）"

    size = os.path.getsize(parquet)
    mtime = os.path.getmtime(parquet)
    out = [f"  parquet：{_human_bytes(size)}（最後寫入 {_human_age(mtime)}）"]

    # row count + latest date — 用 pandas 讀但只取 metadata
    try:
        import pandas as pd
        df = pd.read_parquet(parquet)
        n_rows = len(df)
        out.append(f"  共 {n_rows:,} 封信")
        if "date" in df.columns and n_rows > 0:
            try:
                latest = pd.to_datetime(df["date"], errors="coerce").max()
                if str(latest) != "NaT":
                    out.append(f"  最新一封：{latest.strftime('%Y-%m-%d %H:%M')}")
            except Exception:
                pass
        # 已分類率
        if "primary_dept" in df.columns:
            tagged = df["primary_dept"].notna().sum()
            pct = (tagged * 100 // max(1, n_rows))
            out.append(f"  已分部門：{tagged}/{n_rows}（{pct}%）")
    except ImportError:
        out.append("  （pandas 未裝，無法讀 row count）")
    except Exception as e:
        out.append(f"  ⚠️ 讀 parquet 失敗：{type(e).__name__}: {str(e)[:60]}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 4. RAG 向量庫狀態
# ────────────────────────────────────────────────────────────────────
def _section_rag() -> str:
    """ChromaDB 條目數 + entity alias 表狀態 + BM25 cache 狀態。"""
    out_lines = []
    # ChromaDB
    try:
        from agent_core.memory import (
            _get_memory_collection,
            _bm25_cache,
            vector_store_last_error,
        )
        col = _get_memory_collection()
        if col is None:
            # 帶出 memory 記下的真實 init 失敗原因（與 health._check_data_stores
            # 同契約）。舊文案「API key？路徑？」是陳年猜測：col is None 最常見
            # 主因是 #112 防護拒開 PersistentClient（RED_CHROMA_HTTP_URL 沒帶），
            # 與 API key / 路徑無關，2026-06-12 曾誤導診斷。
            reason = vector_store_last_error() or "init 未留下錯誤原因"
            out_lines.append(f"  ChromaDB：❌ 不可用：{reason[:400]}")
        else:
            try:
                total = col.count()
                out_lines.append(f"  ChromaDB：{total:,} 個 doc")
            except Exception as e:
                out_lines.append(f"  ChromaDB：⚠️ count 失敗：{str(e)[:60]}")
        # BM25 cache
        bm = _bm25_cache.get("count", -1)
        if bm > 0:
            out_lines.append(f"  BM25 索引：已建（{bm:,} 個 doc，記憶體中）")
        else:
            out_lines.append("  BM25 索引：未建（首次 hybrid recall 時會 build）")
    except Exception as e:
        out_lines.append(f"  ⚠️ memory 模組讀取失敗：{type(e).__name__}: {str(e)[:60]}")

    # Entity aliases
    try:
        alias_path = os.path.join(INTERNAL_LAKE_DIR, "entity_aliases.json")
        if os.path.isfile(alias_path):
            with open(alias_path, "r", encoding="utf-8") as f:
                aliases = json.load(f)
            n_groups = sum(len(g) for g in aliases.values())
            out_lines.append(f"  Entity alias：{n_groups} 組（含 customers / suppliers / products）")
        else:
            out_lines.append("  Entity alias：（未建 — 跑 build_alias_table() 一次）")
    except Exception as e:
        out_lines.append(f"  Entity alias：⚠️ {str(e)[:60]}")
    return "\n".join(out_lines)


# ────────────────────────────────────────────────────────────────────
# 5. 今日 Gemini 成本
# ────────────────────────────────────────────────────────────────────
def _section_cost() -> str:
    """掃 cost.jsonl 算今日 + 過去 7 天每天 USD + 趨勢箭頭（vs 昨/7d/30d avg）。"""
    from agent_core.dashboard_trends import cost_trend, format_cost_trend_lines
    trend = cost_trend()
    if not trend:
        return "  （cost.jsonl 不存在或讀取失敗）"
    out = format_cost_trend_lines(trend)
    # 模型分佈（從 cost_tracker 二次掃，輕量）— 反向讀 + 今日視窗早停
    # （健檢 Medium：以前前向掃全檔 25MB/89K 行只為算「今日」分佈）。
    try:
        from agent_core.cost_tracker import _COST_LOG, _load_jsonl_window
        today_str = date.today().isoformat()
        models: dict[str, int] = {}
        for r in _load_jsonl_window(_COST_LOG, hours=25):
            if (r.get("ts") or "")[:10] != today_str:
                continue
            m = r.get("model", "?")
            models[m] = models.get(m, 0) + 1
        if models:
            top = sorted(models.items(), key=lambda kv: -kv[1])[:3]
            out.append("  今日模型分佈：" + " / ".join(f"{m}={n}" for m, n in top))
    except Exception:
        pass
    # 14 天 sparkline（日序）
    daily = trend.get("daily_usd") or {}
    if daily:
        line = "  最近 14 天："
        for d, c in sorted(daily.items())[-14:]:
            line += f" {d[5:]}${c:.2f}"
        out.append(line[:200])
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 6. 最近錯誤 log
# ────────────────────────────────────────────────────────────────────
# Codex P2: import the canonical matcher from dashboard_trends so this
# section's "recent errors" view applies the same `failed(?!=)`
# negative-lookahead as the trend / alert counter. Otherwise daemon
# summaries containing `failed=N` (e.g. alert_pusher's hourly stats)
# would be excluded from the trend but still surface here as
# false-positive "recent errors", confusing users staring at
# system_status output.
from agent_core.dashboard_trends import _ERR_PATTERN  # noqa: E402


def _section_recent_errors(max_lines: int = 5) -> str:
    """掃 var/logs/*.log 最後 N 行找含 ERROR / Traceback / 失敗 的。"""
    if not _LOG_DIR or not os.path.isdir(_LOG_DIR):
        return "  （var/logs/ 不存在）"
    files = sorted(
        [os.path.join(_LOG_DIR, f) for f in os.listdir(_LOG_DIR)
         if f.endswith(".log")],
        key=lambda p: -os.path.getmtime(p) if os.path.isfile(p) else 0,
    )[:6]  # 最近改動的 6 個 log

    hits: list[tuple[str, str, str]] = []  # (mtime, file, line)
    for fp in files:
        try:
            with open(fp, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # 讀最後 64KB
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        for line in tail.splitlines()[-200:]:
            if _ERR_PATTERN.search(line):
                hits.append((
                    datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%m-%d %H:%M"),
                    os.path.basename(fp),
                    line.strip()[:140],
                ))

    # Trend line（今日 vs 昨日 / 7d-avg）放最前面
    out = []
    try:
        from agent_core.dashboard_trends import errors_trend, format_errors_trend_lines
        out.extend(format_errors_trend_lines(errors_trend()))
    except Exception:
        pass

    if not hits:
        out.append("  （所有 log 中沒抓到 ERROR / Traceback / 失敗 字眼）")
        return "\n".join(out)

    # 取最新 max_lines 條，過 redact 確保歷史 secret 不外漏
    hits = hits[-max_lines:]
    try:
        from agent_core.log_redact import redact_log_line
    except Exception:
        redact_log_line = lambda x: x  # noqa
    out.append(f"  最近錯誤（最後 {len(hits)} 條，過 redact）：")
    for when, fn, line in hits:
        safe = redact_log_line(line)
        out.append(f"    [{when}] {fn[:24]:24s} {safe[:80]}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 7. Scheduled tasks
# ────────────────────────────────────────────────────────────────────
def _section_deps() -> str:
    """venv 實裝版本 vs requirements 釘版 —— 抓「改了釘版但沒 pip install」。

    2026-08-12 一天內踩到兩次（pypdf 以為升了其實沒升、ruff 因為 requirements.txt
    不含 -dev 而一直是舊版），而沒有任何東西在檢查。判準只抓「已安裝但版本不符」，
    沒安裝的不算（gui/market/oracle/box-ocr 是選配）。見 agent_core.deps_check。
    """
    try:
        from agent_core.deps_check import check_dependency_drift, format_drift
        return format_drift(check_dependency_drift())
    except Exception as e:  # noqa: BLE001 — 面板任何一區都不該讓整份 status 掛掉
        return f"  ⚠️ 相依檢查失敗：{e}"


def _section_scheduled_tasks() -> str:
    """daemon_tasks.json 的 tasks 列表 —— 圖示是**健康狀態**，不是啟用旗標。

    2026-08-06 修正：這裡的 ✅ 本來是 `enabled`，所以一支連續失敗一週的任務長得
    跟正常的一模一樣（實測採購晨報那批共 7 支、一半直接寄信給同事）。改用
    scheduler.task_health()，並把有問題的排到最前面 —— 面板只列前 10 支，壞掉的
    那支排在第 11 位就等於看不到。
    """
    try:
        from agent_core.scheduler import (
            _load_daemon_tasks, task_health, task_health_icon,
        )
        data = _load_daemon_tasks()
    except Exception as e:
        return f"  ⚠️ 讀 daemon_tasks.json 失敗：{e}"
    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    if not tasks:
        return "  （沒有排程任務）"

    health = {id(t): task_health(t) for t in tasks}
    bad_states = ("error", "stalled", "never")
    # 壞的優先、其次未知、最後正常/停用；同組維持原順序。
    ranked = sorted(
        tasks,
        key=lambda t: (0 if health[id(t)]["state"] in bad_states else
                       1 if health[id(t)]["state"] == "unknown" else 2),
    )
    n_bad = sum(1 for t in tasks if health[id(t)]["state"] in bad_states)
    head = f"  共 {len(tasks)} 個排程任務"
    head += f"；⚠️ 其中 {n_bad} 支異常（已排到最前）：" if n_bad else "（全部正常）："
    out = [head]
    for t in ranked[:10]:
        h = health[id(t)]
        name = str(t.get("name", "?"))[:24]
        interval = t.get("interval_minutes", "?")
        last_run = t.get("last_run_at") or "從未跑"
        if last_run != "從未跑":
            last_run = last_run[5:16]
        runs = t.get("run_count", 0)
        icon = task_health_icon(h["state"])
        out.append(f"    {icon} {name:24s} 每 {interval}min  跑過 {runs} 次  最後 {last_run}")
        if h["state"] in bad_states:
            # 壞掉時把原因頂上來取代 prompt 摘要 —— 那 50 字 prompt 對排錯沒用。
            out.append(f"        ⤷ {h['state']}：{h['detail']}")
        else:
            out.append(f"        〔{(t.get('prompt', '') or '')[:50]}〕")
    if len(tasks) > 10:
        out.append(f"    ...（還有 {len(tasks) - 10} 個未顯示）")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 9. DANGEROUS-tier executions in last 24h (audit)
# ────────────────────────────────────────────────────────────────────
def _section_dangerous_audit(hours: int = 24) -> str:
    """掃 runs/index.jsonl，列最近 N 小時內 DANGEROUS-tier 工具的執行。

    這是實打實的「真有人在跑這個」紀錄；和 _section_tiers 的「分級總覽」互補
    （那邊是 static classification，這裡是 dynamic execution）。

    格式：
      共 N 次（M 個不同工具）
        ❌ 04-25 12:08  delete_erp_workflow  (5.0s)  status=error
        ✅ 04-25 11:42  run_shell             (0.2s)
    """
    try:
        from agent_core.tool_tiers import get_tier, TIER_DANGEROUS
    except Exception as e:
        return f"  ⚠️ tier 模組讀取失敗：{e}"
    index_path = os.path.join(RUNS_DIR, "index.jsonl")
    if not os.path.isfile(index_path):
        return "  （尚無 runs/index.jsonl — 沒有 audited 紀錄）"
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except Exception as e:
        return f"  ⚠️ 讀 index.jsonl 失敗：{e}"
    # 由後往前掃，遇到 started_at < cutoff 即停（檔尾為最新）
    danger_records: list[dict] = []
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except (ValueError, TypeError):
            continue
        started = r.get("started_at") or ""
        if started and started < cutoff:
            break
        tool = r.get("tool", "") or ""
        if not tool:
            continue
        try:
            if get_tier(tool) == TIER_DANGEROUS:
                danger_records.append(r)
        except Exception:
            continue
    if not danger_records:
        return f"  ✅ 過去 {hours}h 沒有 DANGEROUS-tier 工具被執行"
    danger_records.reverse()  # 最舊→最新
    tools = sorted({r.get("tool", "") for r in danger_records})
    out = [f"  共 {len(danger_records)} 次  ({len(tools)} 個不同工具)  最近 {hours}h"]
    for r in danger_records[-12:]:
        st = r.get("status", "?")
        icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(st, "?")
        when = (r.get("started_at") or "")[5:16]  # MM-DD HH:MM
        tool = (r.get("tool", "?") or "?")[:30]
        elapsed = r.get("elapsed_sec", 0)
        suffix = f"status={st}" if st != "success" else ""
        out.append(f"    {icon} {when}  {tool:30s} ({elapsed}s) {suffix}")
    if len(danger_records) > 12:
        out.append(f"    ... 還有 {len(danger_records) - 12} 筆未顯示")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 10. Tool budgets — daily / hourly counters
# ────────────────────────────────────────────────────────────────────
def _section_budgets() -> str:
    """今日各 tool budget 用量摘要（高用量在前 + 接近上限的 highlight）。"""
    try:
        from agent_core.tool_budgets import (
            _DEFAULT_BUDGETS, _load_today, get_budget,
        )
    except Exception as e:
        return f"  ⚠️ budget 模組讀取失敗：{e}"
    state = _load_today()
    if not state:
        return "  （今日尚無 tool 觸發 budget — 全部歸 0）"
    rows = []
    for name, rec in state.items():
        budget = get_budget(name)
        d_max = budget.get("daily")
        if d_max is None:
            continue
        used = int(rec.get("daily", 0))
        if used == 0:
            continue
        ratio = used / d_max if d_max else 0
        rows.append((ratio, used, d_max, name, rec.get("hour_count", 0),
                     budget.get("hourly", 0)))
    if not rows:
        return "  （今日各 tool 用量都 0）"
    rows.sort(reverse=True)  # 高用量在前
    out = [f"  共 {len(rows)} 個 tool 今日有用到 budget"]
    for ratio, used, d_max, name, h_used, h_max in rows[:10]:
        if ratio >= 1.0:
            mark = "🔴"
        elif ratio >= 0.8:
            mark = "🟡"
        else:
            mark = "🟢"
        out.append(
            f"    {mark} {name:36s} {used:3d}/{d_max:<3} 日   "
            f"({h_used}/{h_max} 時)"
        )
    if len(rows) > 10:
        out.append(f"    ... 還有 {len(rows) - 10} 個未顯示")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Policy engine — 過去 24h 政策決策統計
# ────────────────────────────────────────────────────────────────────
def _section_policy() -> str:
    """policy_engine 過去 24h 擋下多少 / 哪一層最常擋。"""
    try:
        from agent_core.policy_engine import policy_summary
    except Exception as e:
        return f"  ⚠️ policy_engine 讀取失敗：{e}"
    s = policy_summary(hours=24)
    total = s.get("total", 0)
    if total == 0:
        return "  （過去 24h 沒有 policy 決策紀錄）"
    out = []
    out.append(f"  共 {total} 次決策  allow={s.get('allowed', 0)}  "
               f"refuse={s.get('refused', 0)}")
    by_layer = s.get("by_layer") or {}
    if by_layer:
        out.append("  refuse 來源（哪一層擋的）：")
        for layer, n in sorted(by_layer.items(), key=lambda kv: -kv[1]):
            out.append(f"    {layer:20s} {n}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Tool metrics — top failure / slow / usage 排行榜
# ────────────────────────────────────────────────────────────────────
def _section_metrics() -> str:
    """全系統 metrics 摘要 + tool 排行榜（從 metrics.py 拉）。"""
    try:
        from agent_core.metrics import metrics_overview
    except Exception as e:
        return f"  ⚠️ metrics 模組讀取失敗：{e}"
    body = metrics_overview(hours=24)
    # metrics_overview 自帶 header，這裡只縮排內容（去掉它自己的 separator）
    lines = []
    skip = True
    for ln in body.splitlines():
        if skip and ("─" in ln or ln.startswith("📊")):
            skip = False if "─" in ln else skip
            continue
        lines.append(f"  {ln}")
    return "\n".join(lines) if lines else "  （metrics 沒輸出）"


def _section_health_score(alerts: list | None = None) -> str:
    """系統健康分數（從 status_center.health_score）。給 dashboard 開頭一眼看。

    alerts: 已算好的 check_alerts() 結果 — system_status 內與 alerts 段共用，
    避免同一輪把 alert 電池（launchctl / log 掃描 / chroma heartbeat）跑兩次。
    """
    try:
        from agent_core.status_center import health_score, system_overview
    except Exception as e:
        return f"  ⚠️ status_center 讀取失敗：{e}"
    h = health_score(system_overview(alerts=alerts))
    out = [f"  {h['status']}  score: {h['score']}/100"]
    if h["reasons"]:
        out.append("  扣分原因：")
        for r in h["reasons"][:8]:
            out.append(f"    {r}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Work mode — 當前模式 + 切換歷史
# ────────────────────────────────────────────────────────────────────
def _section_work_mode() -> str:
    """目前 work mode + 剩餘時間 + 過去 24h 切換次數。"""
    try:
        from agent_core.mode_manager import mode_summary, mode_history
        from agent_core.mode_policy import get_mode_rules
    except Exception as e:
        return f"  ⚠️ mode_manager 讀取失敗：{e}"
    s = mode_summary()
    mode = s.get("current", "normal")
    rules = get_mode_rules(mode)
    out = []
    icon = {"normal": "⚪", "meeting": "🤝", "sales": "💼", "dev": "💻"}.get(mode, "?")
    out.append(f"  {icon} 當前: {mode}  {rules.get('description', '')}")
    if s.get("expires_at"):
        rem = s.get("remaining_minutes", 0)
        if rem > 0:
            out.append(f"     剩餘 {rem} 分鐘自動回 normal")
    if s.get("set_by"):
        out.append(f"     切換者: {s['set_by']}  @ {(s.get('set_at') or '')[:16]}")
    # 過去 24h 切換次數（簡略 — mode_history 已 formatted 太長）
    import os as _os
    from agent_core.mode_manager import _HISTORY_FILE
    if _os.path.isfile(_HISTORY_FILE):
        try:
            from datetime import datetime as _dt, timedelta as _td
            cutoff = (_dt.now() - _td(hours=24)).isoformat()
            n = 0
            with open(_HISTORY_FILE) as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                        if (r.get("at") or "") >= cutoff:
                            n += 1
                    except (ValueError, TypeError):
                        pass
            if n > 0:
                out.append(f"  過去 24h 切換 {n} 次（用 mode_history() 看詳情）")
        except (ValueError, TypeError):
            pass
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Intent router — 過去 24h 訊息分類分布
# ────────────────────────────────────────────────────────────────────
def _section_intent_distribution() -> str:
    """看 intent_router 過去 24h 接到什麼類型的請求最多。"""
    try:
        from agent_core.intent_router import intent_summary, _INTENT_DESC
    except Exception as e:
        return f"  ⚠️ intent_router 讀取失敗：{e}"
    s = intent_summary(hours=24)
    total = s.get("total", 0)
    if total == 0:
        return "  （過去 24h 沒有 intent 分類紀錄）"
    out = [f"  共 {total} 次分類"]
    by_intent = s.get("by_intent", {})
    for intent, n in sorted(by_intent.items(), key=lambda kv: -kv[1])[:8]:
        pct = n * 100 // total
        desc = _INTENT_DESC.get(intent, "")[:30]
        out.append(f"    {intent:25s} {n:4d} ({pct:2d}%)  {desc}")
    methods = s.get("by_method", {})
    if methods:
        out.append("  分類方式：" + ", ".join(f"{m}={n}" for m, n in methods.items()))
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Task memory — 大王交代 / 客戶承諾的 commitment 記憶
# ────────────────────────────────────────────────────────────────────
def _section_task_memory() -> str:
    """task_memory 摘要：逾期 / 今日截止 / 開放中 + 帶 reminder 的數量。"""
    try:
        from agent_core.task_memory import task_summary
    except Exception as e:
        return f"  ⚠️ task_memory 讀取失敗：{e}"
    s = task_summary()
    total = s.get("total", 0)
    by_status = s.get("by_status", {})
    overdue = s.get("overdue", 0)
    due_today = s.get("due_today", 0)
    has_reminder = s.get("with_reminder", 0)

    if total == 0:
        return "  ✅ task_memory 空 — 沒有承諾記錄"
    out = []
    pending = by_status.get("pending", 0)
    in_progress = by_status.get("in_progress", 0)
    blocked = by_status.get("blocked", 0)
    done = by_status.get("done", 0)
    out.append(f"  📋 共 {total} 個 task — pending={pending} / in_progress={in_progress} "
               f"/ blocked={blocked} / done={done}")
    if overdue > 0:
        out.append(f"  🔴 逾期：{overdue} 個（用 tasks_overdue() 看清單）")
    if due_today > 0:
        out.append(f"  ⏰ 今日截止：{due_today} 個（用 tasks_due_today()）")
    if has_reminder > 0:
        out.append(f"  🔔 已設提醒：{has_reminder} 個（tick daemon 到期會自動 push）")
    if overdue == 0 and due_today == 0:
        out.append("  ✅ 沒有近期到期 / 逾期 task")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 1️⃣1️⃣ - 1  Errors by code — last 24h aggregated
# ────────────────────────────────────────────────────────────────────
def _section_errors_summary(hours: int = 24) -> str:
    """掃 runs/index.jsonl，按 error_code 聚合最近 N 小時的失敗。

    這是 ToolResult 統一錯誤格式的觀測面：dashboard 一眼看到「過去 24h
    哪種錯最多」— 是 BUDGET_EXHAUSTED（要提高額度）/ NEEDS_CONFIRMATION
    （LLM 沒等大王 +確認）/ TIMEOUT（網路問題）/ NOT_FOUND（資料對不上）。
    """
    index_path = os.path.join(RUNS_DIR, "index.jsonl")
    if not os.path.isfile(index_path):
        return "  （尚無 runs/index.jsonl）"
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except Exception as e:
        return f"  ⚠️ 讀 index.jsonl 失敗：{e}"
    by_code: dict[str, int] = {}
    by_tool: dict[str, dict[str, int]] = {}
    total_err = 0
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if (r.get("started_at") or "") < cutoff:
            break
        # 只看 error / ok=False
        if r.get("status") != "error" and r.get("ok", True):
            continue
        code = r.get("error_code") or "(unclassified)"
        tool = r.get("tool", "?")
        by_code[code] = by_code.get(code, 0) + 1
        by_tool.setdefault(tool, {}).setdefault(code, 0)
        by_tool[tool][code] += 1
        total_err += 1
    if total_err == 0:
        return f"  ✅ 過去 {hours}h 沒有錯誤"
    out = [f"  共 {total_err} 個錯誤（過去 {hours}h），按 error_code 分類："]
    icons = {
        "rate_limited": "⏸", "budget_exhausted": "💰",
        "timeout": "⌛", "network": "🌐",
        "needs_confirmation": "🔐", "locked_tier": "🔒",
        "invalid_input": "✏️", "not_found": "🔍",
        "permission_denied": "🚫", "internal": "💥",
        "(unclassified)": "❓",
    }
    rows = sorted(by_code.items(), key=lambda kv: -kv[1])
    for code, n in rows[:10]:
        ic = icons.get(code, "❌")
        out.append(f"    {ic} {code:24s} {n:3d} 次")
    # 最頻繁出錯的 top-3 工具
    out.append("")
    top_tools = sorted(by_tool.items(),
                       key=lambda kv: -sum(kv[1].values()))[:3]
    if top_tools:
        out.append("  ⚠️ 出錯最多的工具：")
        for tool, codes in top_tools:
            n = sum(codes.values())
            top_code = max(codes.items(), key=lambda kv: kv[1])[0]
            out.append(f"      {tool:30s} {n:3d} 次  ({top_code} 為主)")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# 1️⃣1️⃣  Task queue
# ────────────────────────────────────────────────────────────────────
def _section_task_queue() -> str:
    """背景 task queue 摘要（pending/running/dlq + 持鎖中的 mutex group）。"""
    try:
        from agent_core.task_queue import queue_summary
    except Exception as e:
        return f"  ⚠️ queue 模組讀取失敗：{e}"
    s = queue_summary()
    out = []
    pending = s.get("pending", 0)
    running = s.get("running", 0)
    dlq = s.get("dlq", 0)
    if pending == 0 and running == 0 and dlq == 0:
        out.append("  ✅ queue 空 — 沒有待處理 / 進行中 / DLQ task")
        return "\n".join(out)
    out.append(f"  pending={pending}  running={running}  done={s.get('done', 0)}  "
               f"failed={s.get('failed', 0)}  cancelled={s.get('cancelled', 0)}")
    if dlq > 0:
        out.append(f"  💀 dead-letter queue：{dlq} 個（用 dead_letter_status() 看詳情）")
    holders = s.get("mutex_holders") or {}
    if holders:
        out.append("  🔒 持鎖中的 mutex group：")
        for group, tid in holders.items():
            out.append(f"      {group:25s} held by {tid}")
    if dlq >= 5:
        out.append(f"  ⚠️ DLQ 累積過多（{dlq}）— 建議大王盡快審查並修復根因")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Operational DB — Postgres shared state readiness
# ────────────────────────────────────────────────────────────────────
def _section_operational_db() -> str:
    """Postgres operational DB status without printing connection strings."""
    try:
        from agent_core import operational_health
    except Exception as e:
        return f"  ⚠️ operational_health 讀取失敗：{e}"

    report = operational_health.health_report()
    db = report.get("operational_db") or {}
    enabled_backends = report.get("enabled_backends") or []
    configured_backends = report.get("configured_backends") or []
    fallback_backends = report.get("local_fallback_backends") or []
    rows = report.get("backends") or []
    lines: list[str] = []

    if not db.get("enabled"):
        lines.append("  ⚪ Postgres operational DB：未啟用（目前使用本機 JSON/JSONL fallback）")
        lines.append(f"  fallback backends：{len(fallback_backends)} 個")
        if fallback_backends:
            lines.append("    " + ", ".join(fallback_backends[:8]))
            if len(fallback_backends) > 8:
                lines.append(f"    ... 還有 {len(fallback_backends) - 8} 個")
        return "\n".join(lines)

    if db.get("ok"):
        icon = "🟢" if report.get("schema_ok") else "🟡"
        lines.append(
            f"  {icon} Postgres operational DB：enabled / ok="
            f"{bool(db.get('ok'))}"
        )
    else:
        try:
            from agent_core.log_redact import redact_log_line
        except Exception:
            redact_log_line = lambda x: x  # noqa: E731
        error = redact_log_line(str(db.get("error") or "unknown"))
        lines.append("  🔴 Postgres operational DB：enabled / ok=False")
        lines.append(f"     error: {error[:180]}")

    lines.append(
        f"     schema: {db.get('schema_version')}/{db.get('schema_expected')} "
        f"schema_ok={bool(report.get('schema_ok'))}"
    )
    lines.append(
        f"     pool: enabled={bool(db.get('pool_enabled'))} "
        f"active={bool(db.get('pool_active'))}"
    )
    lines.append(
        f"  backends：enabled={len(enabled_backends)} / "
        f"configured={len(configured_backends)} / fallback={len(fallback_backends)}"
    )
    if enabled_backends:
        lines.append("    enabled: " + ", ".join(enabled_backends[:10]))
        if len(enabled_backends) > 10:
            lines.append(f"    ... 還有 {len(enabled_backends) - 10} 個")

    inactive = [
        item for item in rows
        if item.get("configured") and not item.get("enabled")
    ]
    if inactive:
        lines.append("  ⚠️ configured 但未啟用：")
        for item in inactive[:5]:
            lines.append(f"    {item.get('name')}: {item.get('reason') or 'unknown'}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# Main entry
# ────────────────────────────────────────────────────────────────────
def _ensure_chroma_endpoint() -> None:
    """讓人工執行的 red-status 跟 daemon 一樣連到共用 chroma server + 同一維度。

    daemon 由 launchd 注入 RED_CHROMA_HTTP_URL；但人從乾淨 shell 跑 red-status
    時通常沒 export，#112 防護會拒開 PersistentClient → _section_rag 誤報向量庫
    「不可用」（其實 server 活著、22k+ doc 在線）。這裡在未顯式設定、且共用 server
    確實活著時，才把 endpoint 預設到共用 server（單一定義 chroma_backend.
    SHARED_SERVER_URL），讓診斷反映 daemon 看到的真實狀態。

    - 已設 RED_CHROMA_HTTP_URL：尊重既有值，不覆寫（daemon 即走這條 → no-op）。
    - ALLOW_DIRECT=1（離線維運、server 已停、要直讀本機 index）：不介入。
    - server 沒活著（dev / CI / server 已停）：不介入，維持 direct 模式。否則會把
      存取模式切成 http 卻連不到 server，讓 dashboard_alerts._check_chroma_mode_
      consistency 誤報 crit「共用 Chroma server 無回應」（CI 沒 server，會紅
      system_alerts 那類診斷煙霧測試）。server 真掛時 daemon 健康 / alerts 自有別
      條紅線接手，不需在這裡硬切模式。

    同一坑的第二個維度：全艦隊 plist 都注入 `RED_EMBED_DIM=768`（見
    project_chroma_dim_migration_768），但 embedding_config.embed_dim() 的
    「未設」預設值仍是舊的 3072（刻意保留，rollback 安全網，不可改）。人工跑
    red-status 若不設這個 env，_section_rag 會去開早已停用、幾乎是空的舊
    `xiaohong_memory`（3072）collection，誤報「0 個 doc」。同上，只在**未顯式
    設定**時才補；已設（daemon 走這條 = no-op；有人明確要測 3072 行為）一律尊重。
    """
    from agent_core import chroma_backend
    from agent_core.env_utils import env_bool

    if not os.environ.get("RED_CHROMA_HTTP_URL", "").strip():
        if env_bool("RED_CHROMA_ALLOW_DIRECT", False):
            pass
        # 只在 server 真的活著時改指過去——與上面那條 crit 的觸發條件（http 模式 +
        # server_alive=False）互斥，故本函式的 env-set 永遠不會引發那條 crit。
        elif chroma_backend._shared_server_alive():
            os.environ["RED_CHROMA_HTTP_URL"] = chroma_backend.SHARED_SERVER_URL

    if not os.environ.get("RED_EMBED_DIM", "").strip():
        os.environ["RED_EMBED_DIM"] = "768"


def system_status(sections: str = "") -> str:
    """🖥️ RED 統一控制台：一覽系統健康。

    Args:
        sections: 逗號分隔的 section name；空字串=全部。
                  可選：daemons, runs, gmail, rag, cost, errors, scheduled

    Returns:
        formatted dashboard 文字（~2KB），適合 Telegram / REPL。
    """
    # 在跑任何 section 前先確保 chroma endpoint 對齊 daemon（避免 _section_rag /
    # health_score 從乾淨 shell 跑時誤報向量庫不可用）。process 級、只設一次。
    _ensure_chroma_endpoint()

    # LLM tool calls don't always honour the str signature — Gemini sometimes
    # passes a list (`["daemons", "cost"]`) or None when it infers
    # "comma-separated string" should be a list. Coerce defensively so the
    # tool returns a useful dashboard instead of an AttributeError that the
    # LLM then surfaces to the user as "system_status broken".
    if isinstance(sections, dict):
        for key in ("sections", "section", "filter"):
            if key in sections:
                sections = sections.get(key)
                break
        else:
            sections = ""

    if isinstance(sections, list):
        sections = ",".join(str(s) for s in sections)
    elif sections is None:
        sections = ""
    elif not isinstance(sections, str):
        sections = str(sections)

    requested = {s.strip().lower() for s in sections.split(",") if s.strip()} if sections else None
    parts = [
        f"🖥️  RED 系統狀態  @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "═" * 60,
    ]
    # health 段（health_score → system_overview → check_alerts）與 alerts 段
    # 各自跑 check_alerts() 會把整套 alert 電池（launchctl / log 掃描 / chroma
    # heartbeat / cost 視窗讀）在同一輪 system_status 內重跑兩次（健檢 Low）。
    # 這裡 lazy 算一次、兩段共用；失敗不吞 — 交給 _safe_section 照舊處理。
    _shared_alerts: dict[str, list] = {}

    def _alerts_once() -> list:
        if "v" not in _shared_alerts:
            from agent_core.dashboard_alerts import check_alerts
            _shared_alerts["v"] = check_alerts()
        return _shared_alerts["v"]

    sections_to_run = [
        ("health",    "🩺  系統健康分數",
                      lambda: _section_health_score(alerts=_alerts_once())),
        ("alerts",    "🚨  Alerts（threshold-based）",
                      lambda: _section_alerts(alerts=_alerts_once())),
        ("daemons",   "1️⃣  Daemon 健康（launchd）",     _section_daemons),
        ("runs",      "2️⃣  最近審計工具呼叫",            _section_recent_runs),
        ("gmail",     "3️⃣  Gmail ingest 狀態",          _section_gmail_ingest),
        ("rag",       "4️⃣  RAG 向量庫",                  _section_rag),
        ("cost",      "5️⃣  Gemini 成本（含 trend）",    _section_cost),
        ("errors",    "6️⃣  最近錯誤 log",                _section_recent_errors),
        ("scheduled", "7️⃣  Scheduled tasks",            _section_scheduled_tasks),
        ("tiers",     "8️⃣  工具權限分級（safe/confirm/dangerous/locked）",
                                                          _section_tiers),
        ("audit",     "9️⃣  最近 24h DANGEROUS 動作 audit",
                                                          _section_dangerous_audit),
        ("budgets",   "🔟  Tool budgets — 今日用量",       _section_budgets),
        ("queue",     "1️⃣1️⃣  背景 task queue（pending/running/DLQ）",
                                                          _section_task_queue),
        ("operational", "1️⃣2️⃣  Operational DB — Postgres shared state",
                                                          _section_operational_db),
        ("errors_by_code", "1️⃣3️⃣  錯誤分類（過去 24h 按 error_code）",
                                                          _section_errors_summary),
        ("task_memory", "1️⃣4️⃣  Task memory — 大王 / 客戶 commitment",
                                                          _section_task_memory),
        ("intent",    "1️⃣5️⃣  Intent 分布（過去 24h）",
                                                          _section_intent_distribution),
        ("metrics",   "1️⃣6️⃣  Tool 排行榜 — 失敗 / 慢 / 使用 top-N",
                                                          _section_metrics),
        ("policy",    "1️⃣7️⃣  Policy engine — 決策統計（過去 24h）",
                                                          _section_policy),
        ("work_mode", "1️⃣8️⃣  Work mode — 當前情境",
                                                          _section_work_mode),
        ("deps",      "1️⃣9️⃣  相依版本 — venv vs requirements",
                                                          _section_deps),
    ]
    for key, title, fn in sections_to_run:
        if requested and key not in requested:
            continue
        parts.append("")
        parts.append(title)
        parts.append("─" * 60)
        parts.append(_safe_section(fn, key))
    parts.append("")
    parts.append("═" * 60)
    parts.append("💡 用 `system_status('daemons,cost')` 只看部份 section")
    return "\n".join(parts)
