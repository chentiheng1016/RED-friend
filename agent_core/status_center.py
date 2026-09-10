"""Status center — 把全系統各 module 的 summary dict 聚合成單一 overview。

問題：
  各 module 自己有 summary helper（task_queue.queue_summary、
  task_memory.task_summary、tool_budgets._load_today、intent_router.intent_summary
  等等），dashboard.py 各 section 自己呼叫對應的。但「我現在只想拿一個 dict
  含全部摘要」沒地方拿 — 比如要傳給 web UI / 推給 Slack / 寫 daily report。

設計：
  純聚合層 — 不算指標、不寫狀態。每個 module 各自 import + try/except 包起來
  （單一 module 壞掉不該擋掉整份 overview）。

  output shape：
    {
        "at": ISO 時間戳,
        "alerts": {...},      # dashboard_alerts.check_alerts()
        "metrics": {...},     # metrics.metrics_summary()
        "queue": {...},       # task_queue.queue_summary()
        "task_memory": {...}, # task_memory.task_summary()
        "intent": {...},      # intent_router.intent_summary()
        "cost": {...},        # cost_tracker.cost_today + 7d/30d
        "rag": {...},         # ChromaDB doc count
        "daemons": [...],     # launchctl list 解析
        "errors": (counts by error_code, last 24h)
    }

  caller 範例：
    from agent_core.status_center import system_overview
    o = system_overview()
    if o['alerts']['crit_count'] > 0:
        notify_team(o)
"""
from __future__ import annotations

import subprocess
from datetime import datetime

from agent_core.daemon_launchd_state import (
    is_benign_stopped_daemon,
    is_known_daemon_recovering,
    short_launchd_label,
)


def _safe(fn, default=None):
    """run fn() — 失敗回 default。每個 module summary 套這個避免一壞全壞。"""
    try:
        return fn()
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:80]}",
                "_default": default}


def _alerts_summary(alerts: list | None = None) -> dict:
    # alerts 可傳入已算好的 check_alerts() 結果（system_status 內共用一次
    # alert 電池，避免同一輪重跑 launchctl / log 掃描 / chroma heartbeat）。
    if alerts is None:
        from agent_core.dashboard_alerts import check_alerts
        alerts = check_alerts()
    crit = sum(1 for a in alerts if a.get("level") == "crit")
    warn = sum(1 for a in alerts if a.get("level") == "warn")
    return {
        "crit_count": crit,
        "warn_count": warn,
        "alerts": [{"id": a.get("id"), "level": a.get("level"),
                    "title": a.get("title"), "detail": a.get("detail", "")[:120]}
                   for a in alerts[:10]],
    }


def _metrics_summary() -> dict:
    from agent_core.metrics import metrics_summary
    return metrics_summary(hours=24)


def _queue_summary() -> dict:
    from agent_core.task_queue import queue_summary
    return queue_summary()


def _task_memory_summary() -> dict:
    from agent_core.task_memory import task_summary
    return task_summary()


def _intent_summary() -> dict:
    from agent_core.intent_router import intent_summary
    return intent_summary(hours=24)


def _cost_summary() -> dict:
    from agent_core.cost_tracker import cost_today, cost_last_7_days
    today = cost_today()  # returns formatted string in this codebase
    last7 = cost_last_7_days()
    return {"today": today, "last_7_days": last7}


def _budgets_summary() -> dict:
    from agent_core.tool_budgets import _load_today, _DEFAULT_BUDGETS
    today = _load_today()
    used = sum(int(v.get("daily", 0)) for v in today.values())
    near_limit = []
    for name, rec in today.items():
        if name not in _DEFAULT_BUDGETS:
            continue
        d_max = _DEFAULT_BUDGETS[name].get("daily")
        if d_max and rec.get("daily", 0) >= d_max * 0.8:
            near_limit.append({"tool": name,
                               "used": rec["daily"], "max": d_max})
    return {
        "tools_active_today": len(today),
        "total_uses": used,
        "near_limit": near_limit,
    }


def _rag_summary() -> dict:
    try:
        # memory.py 的正名是 _get_memory_collection（舊名 _get_collection
        # 不存在 → ImportError → 這裡永遠回 -1）。
        from agent_core.memory import _get_memory_collection
        col = _get_memory_collection()
        return {"chroma_doc_count": col.count() if col else 0}
    except Exception:
        return {"chroma_doc_count": -1}


def _daemons_summary() -> dict:
    """launchctl list | grep com.xiaohong — 簡化結果。"""
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True,
                               text=True, timeout=5)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            msg = f"launchctl exit {proc.returncode}"
            if detail:
                msg += f": {detail[:120]}"
            return {"_error": msg}
        running = []
        idle_ok = []
        last_err = []
        for line in (proc.stdout or "").splitlines():
            if "com.xiaohong." not in line:
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            pid, exit_code_s, label = parts[0], parts[1], parts[2]
            short = short_launchd_label(label)
            try:
                exit_code = int(exit_code_s)
            except ValueError:
                exit_code = -1
            if pid != "-":
                running.append(short)
            elif is_known_daemon_recovering(short):
                running.append(short)
            elif is_benign_stopped_daemon(short, exit_code):
                idle_ok.append(short)
            elif exit_code == 0:
                idle_ok.append(short)
            else:
                last_err.append({"label": short, "exit_code": exit_code_s})
        return {
            "total": len(running) + len(idle_ok) + len(last_err),
            "running": running,
            "idle_ok_count": len(idle_ok),
            "last_exit_nonzero": last_err,
        }
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:80]}"}


def _errors_summary() -> dict:
    """過去 24h 錯誤 by code（同 dashboard 的）。"""
    from agent_core.metrics import metrics_summary
    s = metrics_summary(hours=24)
    return {
        "total_24h": s.get("total_error", 0),
        "by_code": s.get("error_codes", {}),
    }


# ────────────────────────────────────────────────────────────────────
# Main entry
# ────────────────────────────────────────────────────────────────────
def system_overview(alerts: list | None = None) -> dict:
    """🟢 全系統健康 snapshot — 一個 dict 含全部摘要。

    用途：
      - web UI / 外部 monitoring 拿結構化資料
      - 寫 daily report
      - alert pipeline 一次抓全狀態判斷

    failure-safe：單一 module 壞掉不會擋下其他 — 那塊 sub-dict 會有 `_error`。

    alerts: 可傳入已算好的 dashboard_alerts.check_alerts() 結果，避免呼叫端
    （dashboard.system_status 的 health + alerts 段）同一輪重跑 alert 電池。
    """
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "alerts": _safe(lambda: _alerts_summary(alerts),
                        {"crit_count": 0, "warn_count": 0}),
        "metrics": _safe(_metrics_summary, {}),
        "queue": _safe(_queue_summary, {}),
        "task_memory": _safe(_task_memory_summary, {}),
        "intent": _safe(_intent_summary, {}),
        "cost": _safe(_cost_summary, {}),
        "budgets": _safe(_budgets_summary, {}),
        "rag": _safe(_rag_summary, {}),
        "daemons": _safe(_daemons_summary, {}),
        "errors": _safe(_errors_summary, {}),
    }


def health_score(overview: dict | None = None) -> dict:
    """🟢 系統健康分數（0-100）— 給 alert / dashboard 一眼看。

    overview: 可傳入已算好的 system_overview() 結果，避免呼叫端（dashboard_api.
    get_health / overview_text / system_status）把整套 alert 電池重跑 2-3 次
    （健檢 Low）。不傳則自己算（行為不變）。

    扣分規則（粗略，能改）：
      crit alerts        每個 -25
      warn alerts        每個 -5
      daemon non-zero    每個 -10
      success_pct < 90   -10
      success_pct < 70   -25
      DLQ > 0            -10
      task overdue > 0   -5
    """
    o = overview if overview is not None else system_overview()
    score = 100
    reasons: list[str] = []

    a = o.get("alerts") or {}
    crit = a.get("crit_count", 0)
    warn = a.get("warn_count", 0)
    if crit:
        score -= 25 * crit
        reasons.append(f"-{25*crit} ({crit} crit alerts)")
    if warn:
        score -= 5 * warn
        reasons.append(f"-{5*warn} ({warn} warn alerts)")

    d = o.get("daemons") or {}
    bad_daemons = len(d.get("last_exit_nonzero") or [])
    if bad_daemons:
        score -= 10 * bad_daemons
        reasons.append(f"-{10*bad_daemons} ({bad_daemons} daemon last_exit ≠ 0)")

    m = o.get("metrics") or {}
    sp = m.get("success_pct", 100)
    if _should_penalize_success_pct(m):
        if sp < 70:
            score -= 25
            reasons.append(f"-25 (success_pct {sp}% < 70)")
        elif sp < 90:
            score -= 10
            reasons.append(f"-10 (success_pct {sp}% < 90)")

    q = o.get("queue") or {}
    if q.get("dlq", 0) > 0:
        score -= 10
        reasons.append(f"-10 (DLQ has {q['dlq']} item)")

    t = o.get("task_memory") or {}
    if t.get("overdue", 0) > 0:
        score -= 5
        reasons.append(f"-5 ({t['overdue']} overdue task)")

    score = max(0, min(100, score))
    if score >= 90:
        status = "🟢 healthy"
    elif score >= 70:
        status = "🟡 ok with issues"
    elif score >= 40:
        status = "🟠 degraded"
    else:
        status = "🔴 critical"

    return {
        "score": score,
        "status": status,
        "reasons": reasons,
    }


def _should_penalize_success_pct(metrics: dict) -> bool:
    """Only let success_pct hurt health when the sample reflects current risk.

    The 24h metrics window can be dragged down by two old manual-tool failures
    long after today's alert/runs trend has gone quiet. That makes the health
    score feel worse while the system is actually stable. Prefer today's run
    sample when available; fall back to 24h only when it is large enough to be
    meaningful.
    """
    try:
        from agent_core.dashboard_trends import runs_trend
        today = (runs_trend() or {}).get("today") or {}
        today_total = int(today.get("total", 0) or 0)
        if today_total >= 5:
            return True
        if today_total == 0:
            return False
    except (ValueError, TypeError):
        pass
    return int(metrics.get("total_calls", 0) or 0) >= 20


# ────────────────────────────────────────────────────────────────────
# Alert correlation (P3) — 把「alert 觸發 → tail logs → grep error type
# → 對 daemon last_exit → 串 git log」這套手動流程自動化成一條呼叫。
# ────────────────────────────────────────────────────────────────────
import os
import re
import time
from collections import Counter
from datetime import date


# Same pattern object as dashboard_trends.errors_trend. Importing it avoids
# drift such as counting structured summaries like failed=0 as RCA errors.
from agent_core.dashboard_trends import _ERR_PATTERN  # noqa: E402


def _normalize_error_line(line: str) -> str:
    """Replace varying tokens (timestamps, IPs, hex addrs, digits) so retry-loop
    spam buckets together. Mirrors the manual `sed` we ran when diagnosing the
    cloudflared retry storm.

    Tradeoff: aggressive (every digit → <N>) on purpose. For RCA the goal is
    "do all the variants of THIS message line collapse into one count?" — so
    we'd rather over-merge similar lines than fragment a real retry loop into
    ~one-bucket-per-attempt.
    """
    s = line.strip()
    if not s:
        return ""
    # ISO date / datetime: match "2026-05-05" and "2026-05-05T12:46:28Z" both.
    s = re.sub(r"20\d{2}-\d{2}-\d{2}(?:T[\d:.]+Z?)?", "<TS>", s)
    # IPv4 (before generic digits, else octets each become <N>).
    s = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<IP>", s)
    # Hex addresses
    s = re.sub(r"\b0x[0-9a-fA-F]+\b", "<HEX>", s)
    # All other digit runs
    s = re.sub(r"\b\d+\b", "<N>", s)
    return s[:200]  # cap line length


# Match a YYYY-MM-DD positioned at the LEADING TIMESTAMP slot of a log
# line — i.e. preceded only by a short, non-digit prefix (brackets, tags,
# whitespace) such as `[cf] ` or `[ERROR] `. Used to *scope* lines to
# today when the log format carries a date; intentionally rejects ISO
# dates that appear deep inside the message body.
#
# Codex P1: the trailing `\b` was requiring a non-word char after the day,
# but the conventional ISO datetime form `2026-05-04T23:59:59Z` puts a
# `T` (word char) right after — so `\b` failed and yesterday's full-ISO
# lines bypassed the today filter, polluting RCA buckets with non-today
# data. `(?!\d)` instead matches the date when followed by anything that
# isn't a digit while still rejecting a 7+ digit run.
#
# Codex P2 round 3: previously this used `re.search` with no positional
# constraint, so a message body like
#   `12:00:00 [ERROR] failed import for order date 2026-01-01`
# would capture the trailing `2026-01-01` as the line's timestamp and
# wrongly drop a today error. The new regex uses `^[^\d\n]{0,20}` as the
# allowed prefix — a HH:MM:SS-led line starts with a digit so it can't
# match (the rollback detector handles those instead), while a bracketed
# prefix like `[cf] ` (≤20 non-digit chars) still matches the date that
# follows. Real log timestamps live at the start of the line; embedded
# business dates do not.
_ISO_DATE_PATTERN = re.compile(r"^[^\d\n]{0,20}(20\d{2}-\d{2}-\d{2})(?!\d)")

# Match a HH:MM:SS at line start (the standard Python logger format
# `%(asctime)s` with `datefmt='%H:%M:%S'`). Used for day-rollover
# detection in undated daemon logs (Codex P2 round 2).
_HHMMSS_LINE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})")


def _scope_undated_lines_to_today(lines: list[str]) -> list[bool]:
    """Return per-line `is_today` booleans for time-only (`%H:%M:%S`) logs.

    Codex P2 round 2: long-running daemons (dispatcher, mailcheck, etc.)
    write to a SINGLE persistent file with the standard Python logger's
    time-only format. If the file accumulates over days and we get a
    fresh-mtime tail, the previous fallback "trust mtime → all lines
    today" wrongly counted yesterday's errors as today's.

    Strategy: walk the tail BACKWARD. The file's mtime represents the
    last write — by definition that's "today" (the file_recency_hours
    gate already excluded older files). Each prior line's HH should be
    ≤ the next line's HH (going earlier in time means hours decrease).
    The first time we see HH INCREASE while walking backward, we've
    crossed midnight in reverse — every line from there back is on a
    PREVIOUS day, not today.

    Continuation lines without HH:MM:SS at start (e.g. multi-line
    Tracebacks) inherit the day from the next line we already classified.

    Returns: list[bool] same length as `lines`. True = today, False =
    older. If no HH:MM:SS line is found anywhere, returns all-True
    (caller's fallback "trust mtime" semantics — best we can do).
    """
    n = len(lines)
    is_today = [True] * n
    found_any_hhmmss = False
    last_hh: int | None = None
    crossed_midnight = False
    # Continuation lines that come AFTER their owner in file order, so we
    # encounter them BEFORE the owner when walking backward. Buffer their
    # indices and assign once we hit the owning HH:MM:SS line.
    pending_continuations: list[int] = []

    for i in range(n - 1, -1, -1):
        m = _HHMMSS_LINE_RE.match(lines[i])
        if m:
            found_any_hhmmss = True
            hh = int(m.group(1))
            if last_hh is not None and hh > last_hh:
                # Going backward, HH should monotonically decrease (or stay).
                # An increase = we crossed midnight in reverse.
                crossed_midnight = True
            # Classify owner + its trailing continuations identically.
            owner_today = not crossed_midnight
            is_today[i] = owner_today
            for cont_idx in pending_continuations:
                is_today[cont_idx] = owner_today
            pending_continuations = []
            last_hh = hh
        else:
            # Continuation: defer until we find the owner. Until then,
            # leave default True; if the owner turns out to be on a
            # previous day, we re-classify here.
            pending_continuations.append(i)

    # pending_continuations remaining after the loop → continuations at
    # the very start of the tail with no owner BEFORE them in the file.
    # They belong to a HH:MM:SS line we couldn't see (cut off by 256KB
    # window). Most permissive interpretation: trust mtime → today.
    # No action needed (default is_today[i] already True).

    if not found_any_hhmmss:
        # Pure non-time-format content — caller's mtime trust is the
        # best we can do.
        return is_today
    return is_today


# Match dated-filename forms like `agent-2026-05-05.log`, `daemon-X-2026-05-05.log`,
# `2026-05-05.log`. Captures the date so we can drop yesterday's per-day file
# even when the lines inside have only `%H:%M:%S` and the file mtime is still
# within the recency window (e.g. last-touched at 23:59:59 of the previous day).
_DATED_FILENAME_PATTERN = re.compile(r"-?(20\d{2}-\d{2}-\d{2})\.log$")


def _bucket_recent_errors(
    log_dir: str | None = None,
    today_only: bool = True,
    top_n: int = 5,
    max_bytes_per_file: int = 262144,
    file_recency_hours: float = 26.0,
) -> list[tuple[int, str, str]]:
    """Tail every var/logs/*.log, keep only error-pattern lines, normalize
    whitespace + numbers + timestamps, and bucket. Returns list of
    (count, normalized_line, top_source_log) sorted by count desc.

    log_dir=None → use _LOG_DIR (production path). Pass an explicit dir for
    tests so they don't depend on global state.

    today_only filtering (Codex P1 fix):
      The repo's default logger format is `%H:%M:%S [LEVEL] msg` (see
      `logging_and_paths.py` — datefmt is time-only by design, since each
      file is rotated per-day and the date lives in the filename). The
      original line-substring check `today_str not in line` therefore
      excluded **every** line from those logs and `correlate_alert()`
      reported "no errors" during real incidents. Fixed via two-tier:

        1. Skip files whose mtime is older than `file_recency_hours` —
           they're definitely not "today's" content. Default 26h gives a
           little slop so a log just-rotated past midnight isn't dropped.
        2. For each remaining line:
             • If the line begins with a `YYYY-MM-DD` (allowing a short
               non-digit prefix like `[cf] `), accept iff today. Catches
               multi-day non-rotated logs like daemon-cloudflare; bare ISO
               dates buried in the message body — e.g.
               `12:00:00 [ERROR] failed import for order date 2026-01-01`
               — are intentionally NOT treated as the line's timestamp,
               because the line begins with a digit (the HH:MM:SS slot)
               so the leading-prefix anchor refuses to match. Codex P2 r3.
             • Otherwise (time-only `%H:%M:%S` line), classify via the
               HH-rollback detector that walks the tail backward from the
               most-recent line and flips to "previous day" the moment HH
               increases (we crossed midnight in reverse).

    """
    if log_dir is None:
        from agent_core.logging_and_paths import _LOG_DIR
        log_dir = _LOG_DIR
    if not log_dir or not os.path.isdir(log_dir):
        return []

    today_str = date.today().isoformat()
    bucket: Counter = Counter()
    sources: dict[str, Counter] = {}  # normalized_line → Counter[filename]
    now = time.time()
    cutoff_seconds = file_recency_hours * 3600.0

    for fn in os.listdir(log_dir):
        if not fn.endswith(".log"):
            continue
        fp = os.path.join(log_dir, fn)
        if not os.path.isfile(fp):
            continue

        # Codex P2 fix: dated-filename gate (runs BEFORE the mtime check).
        # The repo's logger writes `agent-YYYY-MM-DD.log` and lines inside
        # carry only `%H:%M:%S`. Just after midnight, yesterday's file is
        # still within the 26h mtime window AND has no per-line date
        # markers, so the (mtime + line-date) two-tier scope alone would
        # count it as today's. Parse the date out of the filename first
        # and skip files explicitly stamped with a non-today date.
        if today_only:
            fn_date_match = _DATED_FILENAME_PATTERN.search(fn)
            if fn_date_match and fn_date_match.group(1) != today_str:
                continue

        # Second-tier: file mtime — skip files that haven't been touched
        # recently. Cheap stat call avoids reading 256KB just to discard
        # it. Default 26h gives a little slop for logs rotated past midnight.
        if today_only:
            try:
                if (now - os.path.getmtime(fp)) > cutoff_seconds:
                    continue
            except OSError:
                continue
        try:
            with open(fp, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes_per_file))
                tail = f.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        lines = tail.splitlines()

        # Codex P2 fix: long-running processes with `%H:%M:%S` format may
        # accumulate multi-day content in one file (mtime says today, but
        # the tail's beginning could be from yesterday or earlier). Trusting
        # file mtime alone polluted today's RCA buckets with stale lines.
        #
        # Strategy: scope each line's date by an ISO date in the LEADING
        # timestamp slot of the line (allowing a short non-digit prefix
        # such as `[cf] `). Codex P2 r3 narrowed this from `re.search`
        # because that scanned the whole line — message bodies regularly
        # contain bare business dates like
        # `failed import for order date 2026-01-01` and the previous
        # search misread them as the line's log timestamp. The new
        # leading-anchor `re.match` rejects message-body dates because
        # HH:MM:SS-led lines start with a digit. Forward-fill then
        # backward-fill, so an undated line gets the date of the closest
        # dated neighbor — most daemon logs print a date marker on startup
        # or rotation, giving us at least one anchor per session.
        line_date: list[str | None] = [None] * len(lines)
        # is_today_by_hhmmss: only consulted when line_date[i] is None
        # (i.e. no ISO marker in this tail). Codex P2 round 2: undated
        # multi-day logs need rollback detection rather than trust-mtime.
        is_today_by_hhmmss: list[bool] = [True] * len(lines)
        if today_only:
            for i, ln in enumerate(lines):
                m = _ISO_DATE_PATTERN.match(ln)
                if m:
                    line_date[i] = m.group(1)
            # Forward fill (carry the last seen marker forward).
            for i in range(1, len(lines)):
                if line_date[i] is None:
                    line_date[i] = line_date[i - 1]
            # Backward fill for lines BEFORE the first marker.
            for i in range(len(lines) - 2, -1, -1):
                if line_date[i] is None:
                    line_date[i] = line_date[i + 1]
            # If line_date is still None for everything, the tail had no
            # ISO markers — but it MAY still have HH:MM:SS line prefixes
            # (the Python logger's default datefmt). Walk backward
            # detecting midnight rollover so we don't lump yesterday's
            # errors with today's.
            if all(d is None for d in line_date):
                is_today_by_hhmmss = _scope_undated_lines_to_today(lines)

        # Codex P1: a matched error line could embed a token / password / PII
        # (e.g. a stack trace echoing a payload, a "Bearer ..." API call that
        # 4xx'd, a `password=...` query string from a noisy URL log). The
        # downstream report goes to console / Telegram / LLM during incident
        # triage, so we must run lines through the same redactor every other
        # log-display path uses BEFORE bucketing — keys leak via the bucket
        # text itself otherwise. Lazy import keeps this file's import graph
        # light.
        try:
            from agent_core.log_redact import redact_log_line as _redact
        except Exception:
            def _redact(s: str) -> str:
                return s

        for i, line in enumerate(lines):
            if not _ERR_PATTERN.search(line):
                continue
            if today_only:
                d = line_date[i]
                if d is not None:
                    # ISO marker present (directly or filled from neighbor).
                    if d != today_str:
                        continue
                else:
                    # No ISO marker anywhere in tail — check the HH:MM:SS
                    # rollback classifier. False means line is on a
                    # previous day relative to file mtime.
                    if not is_today_by_hhmmss[i]:
                        continue
            # Redact BEFORE normalize/bucketing so secrets never become part
            # of the bucket key (which the report text comes directly from).
            safe_line = _redact(line)
            norm = _normalize_error_line(safe_line)
            if not norm:
                continue
            bucket[norm] += 1
            sources.setdefault(norm, Counter())[fn] += 1

    out: list[tuple[int, str, str]] = []
    for norm, count in bucket.most_common(top_n):
        top_src = sources[norm].most_common(1)[0][0]
        out.append((count, norm, top_src))
    return out


def correlate_alert(alert_id: str) -> str:
    """🟢 給定一個 alert_id，回 root-cause 分析報告（人類可讀）。

    自動化「alert 觸發 → 我手動 tail / grep / sort | uniq -c → 把錯誤桶起來」
    的流程。對 errors_log_* / errors_ratio_* / daemon_multi_fail 等常見 alert
    有特化策略；其他 alert 走 generic（top buckets + daemon state）。

    Args:
        alert_id: dashboard_alerts 回傳的 id，例如 "errors_log_crit"。也接受
                  尚未觸發但歷史出現過的 id（rationale：方便回看「上次為什麼會
                  炸」）。

    Returns:
        Plain text report，分段以分隔線標示。可直接丟 Telegram 或 console。

    Codex P1: 這個 function 註冊為 LLM 可呼叫 tool（intent_router 的
    system_maintenance bucket）。原本 signature 含 `log_dir` 參數會被
    LLM-supplied，prompt-injection 後可指向任意 logs（系統 log、其他
    project log 等），繞過 path_safety / safe_path 的保護。現在公開
    signature 只有 alert_id；測試走內部的 _correlate_alert_for_test。
    """
    return _correlate_alert_for_test(alert_id, log_dir=None)


def _correlate_alert_for_test(
    alert_id: str, log_dir: str | None = None,
) -> str:
    """Internal entry — accepts custom log_dir for unit tests only.

    The leading underscore + `for_test` suffix is the contract: anyone
    grepping for a public-API rename can spot this isn't part of it.
    Tests that need to plant synthetic log files in a tmpdir call THIS
    function directly. Production / LLM tool dispatch goes through
    `correlate_alert(alert_id)` which forces log_dir=None → _LOG_DIR.
    """
    lines: list[str] = []
    lines.append(f"🔍 Alert RCA — {alert_id}")
    lines.append("─" * 60)

    # Section 1 ── current alert status
    try:
        from agent_core.dashboard_alerts import check_alerts
        alerts = check_alerts() or []
        match = next((a for a in alerts if a.get("id") == alert_id), None)
        if match:
            lines.append(
                f"  狀態: 🔴 {match.get('level', 'unknown')} — {match.get('title', '')}"
            )
            if match.get("detail"):
                lines.append(f"  細節: {match['detail']}")
            if match.get("advice"):
                lines.append(f"  建議: {match['advice']}")
        else:
            lines.append("  狀態: 🟢 目前未觸發（可能已恢復；繼續分析最近 logs）")
    except Exception as exc:
        lines.append(f"  ⚠️ 無法讀 check_alerts: {type(exc).__name__}: {exc}")

    # Section 2 ── error-pattern buckets (today)
    lines.append("")
    lines.append("📋 今日 error-pattern 分桶 (top 5)：")
    buckets = _bucket_recent_errors(log_dir=log_dir, top_n=5)
    if not buckets:
        lines.append("  (今日沒符合 error pattern 的 log 行)")
    else:
        for count, norm, src in buckets:
            short = norm[:80] + ("…" if len(norm) > 80 else "")
            lines.append(f"  {count:5}× [{src}]  {short}")

    # Section 3 ── daemon last_exit_nonzero (almost always relevant)
    #
    # Codex P2: _daemons_summary() returns {"_error": "..."} when launchctl
    # is unavailable (CI / non-macOS / the binary missing). Treating that as
    # an empty list and printing "✅ 無" claimed daemon health was fine when
    # we actually never collected the data — that misleads incident triage.
    # Distinguish three states explicitly: error → unknown → "✅ 無".
    lines.append("")
    lines.append("🤖 Daemon 上次 exit ≠ 0：")
    d = _safe(_daemons_summary, {}) or {}
    daemon_data_unavailable = bool(d.get("_error"))
    if daemon_data_unavailable:
        lines.append(f"  ⚠️  daemon 狀態無法取得（{d['_error']}）")
        lines.append(
            "      （非 macOS / launchctl 不可用 / 權限問題；此次 RCA "
            "未能納入 daemon 維度，請手動確認）"
        )
        bad: list = []
    else:
        bad = d.get("last_exit_nonzero") or []
        if not bad:
            lines.append("  ✅ 無")
        else:
            for entry in bad[:8]:
                lines.append(
                    f"  ⚠️  {entry.get('label', '?'):20} "
                    f"exit={entry.get('exit_code', '?')}"
                )

    # Section 4 ── alert-id-specific advice
    lines.append("")
    lines.append("💡 推測：")
    lines.append(_correlate_advice(
        alert_id, buckets, bad,
        daemon_data_unavailable=daemon_data_unavailable,
    ))

    return "\n".join(lines)


# Cache: launchd label → log-file basename. Built lazily from plist templates
# the first time we need to correlate a daemon failure with a log spam source.
_LABEL_TO_LOGNAME: dict[str, str] | None = None


def _build_label_to_logname() -> dict[str, str]:
    """Parse launchd plist templates to map `<label>` → log-basename.

    Codex P2 (rag_sync_daily case): the launchd label and the log filename
    aren't always identical — `com.xiaohong.rag_sync_daily` writes to
    `daemon-rag_sync.log`, so the previous "strip 'daemon-' / '.log'"
    heuristic produced "rag_sync" but bad_daemons contained "rag_sync_daily".
    The advice "X is in last_exit≠0 + top spam source" was suppressed.

    Strategy: read each plist's <Label> and <StandardOutPath>, build a map
    from short-label (after `com.xiaohong.` strip) to log basename. Fall
    back to the strip heuristic for any daemon whose plist isn't here.

    The map is cached process-wide because plist filenames don't change at
    runtime — daemons are deployed by `redeploy-daemons` which restarts the
    Python process anyway.
    """
    import xml.etree.ElementTree as ET
    from agent_core.logging_and_paths import _SCRIPT_DIR
    out: dict[str, str] = {}
    templates_dir = os.path.join(_SCRIPT_DIR, "launchd", "templates")
    if not os.path.isdir(templates_dir):
        return out
    for fn in os.listdir(templates_dir):
        if not fn.endswith(".plist"):
            continue
        try:
            tree = ET.parse(os.path.join(templates_dir, fn))
            root = tree.getroot()
            # plist <dict> is the first child of <plist>
            d = root[0] if len(root) else None
            if d is None or d.tag != "dict":
                continue
            # Walk key/value pairs
            label = log_path = None
            children = list(d)
            for i in range(0, len(children) - 1, 2):
                key, val = children[i], children[i + 1]
                if key.tag != "key":
                    continue
                if key.text == "Label" and val.tag == "string":
                    label = (val.text or "").strip()
                elif key.text == "StandardOutPath" and val.tag == "string":
                    log_path = (val.text or "").strip()
            if label and log_path and label.startswith("com.xiaohong."):
                short = label[len("com.xiaohong."):]
                # Use only the basename so paths like
                # "@@REPO_ROOT@@/var/logs/daemon-rag_sync.log" → "daemon-rag_sync.log"
                out[short] = os.path.basename(log_path)
        except Exception:
            continue
    return out


def _label_for_log(src: str, bad_labels: set[str]) -> str | None:
    """Given a log filename `src` (e.g. "daemon-rag_sync.log"), return the
    matching short launchd label if any in `bad_labels` corresponds — using
    the plist alias map first, then a default strip-heuristic as fallback."""
    global _LABEL_TO_LOGNAME
    if _LABEL_TO_LOGNAME is None:
        _LABEL_TO_LOGNAME = _build_label_to_logname()
    # Reverse-lookup via alias map: any label whose log basename equals src.
    for short_label, logname in _LABEL_TO_LOGNAME.items():
        if logname == src and short_label in bad_labels:
            return short_label
    # Fallback heuristic — works for daemons whose label and log filename
    # do match (e.g. com.xiaohong.dispatcher → daemon-dispatcher.log).
    fallback = src.replace("daemon-", "").replace(".log", "")
    if fallback in bad_labels:
        return fallback
    return None


def _correlate_advice(
    alert_id: str,
    buckets: list[tuple[int, str, str]],
    bad_daemons: list[dict],
    daemon_data_unavailable: bool = False,
) -> str:
    """Cheap heuristics — turn the bucket + daemon view into a one-line pointer
    at where to look first. Not magic; matches the kind of pattern matching the
    dev would do staring at top buckets.

    daemon_data_unavailable: True when _daemons_summary() returned an _error
    payload (e.g. launchctl missing). In that case bad_daemons is forced to
    [] in the caller to keep the rest of the heuristics simple, but the
    "alert 可能已恢復" hint would be misleading (we don't actually know),
    so we soften the empty-state message.
    """
    if not buckets and not bad_daemons:
        if daemon_data_unavailable:
            return ("  log 端看不到 error 行；但 daemon 狀態本次未收集到，"
                    "無法判斷是否真的全部恢復。")
        return "  系統目前看不到 error 行；alert 可能已恢復。"
    lines: list[str] = []

    # If a daemon is in last_exit_nonzero AND its log is the top bucket source.
    # Use _label_for_log to resolve label↔log aliases (rag_sync_daily etc.)
    # rather than just stripping "daemon-" / ".log" off the filename.
    if buckets and bad_daemons:
        bad_labels = {b["label"] for b in bad_daemons}
        for count, _, src in buckets[:3]:
            label = _label_for_log(src, bad_labels)
            if label is not None:
                lines.append(
                    f"  - {label} 同時是「last_exit≠0」+「今日 log spam top-source ({count} 條)」"
                    f" → 八成是 root cause"
                )

    # Heavy concentration in one bucket is usually a retry loop:
    if buckets and buckets[0][0] >= 100:
        count, _, src = buckets[0]
        lines.append(
            f"  - 單一 pattern {count} 條集中在 {src} → 像 retry 死循環 / 連線抖動"
        )

    # errors_log alert（新單一 id）/ 舊 errors_log_* id 但 buckets 都很小
    # → broad noise (許多不同錯誤)
    if alert_id.startswith("errors_log") and buckets and buckets[0][0] < 20:
        lines.append(
            "  - 沒有單一主因；今日有多種不同 error pattern → 看 top 5 各自一條去追"
        )

    if not lines:
        lines.append("  - （沒抓到明顯 pattern；用 top 5 + daemon 列表手動串一下）")
    return "\n".join(lines)


def overview_text() -> str:
    """🟢 system_overview 的人類可讀版（給 LLM / 大王看）。"""
    o = system_overview()
    h = health_score(o)
    out = [
        f"🩺 系統健康 — {h['status']}  (score: {h['score']}/100)",
        f"   @ {o['at']}",
        "─" * 60,
    ]
    if h["reasons"]:
        out.append("  扣分原因：")
        for r in h["reasons"]:
            out.append(f"    {r}")
        out.append("")

    a = o["alerts"]
    out.append(f"  alerts        : {a.get('crit_count', 0)} crit / {a.get('warn_count', 0)} warn")

    m = o["metrics"]
    if m.get("total_calls", 0) > 0:
        out.append(f"  calls (24h)   : {m['total_calls']} / "
                   f"success {m['success_pct']}% / "
                   f"avg {m['avg_latency_sec']:.2f}s")
    else:
        out.append("  calls (24h)   : 0")

    q = o["queue"]
    out.append(f"  task_queue    : pending={q.get('pending', 0)} / "
               f"running={q.get('running', 0)} / DLQ={q.get('dlq', 0)}")

    t = o["task_memory"]
    overdue = t.get("overdue", 0)
    out.append(f"  task_memory   : {t.get('total', 0)} 個 / "
               f"逾期={overdue} / 今日截止={t.get('due_today', 0)}")

    d = o["daemons"]
    bad = d.get("last_exit_nonzero") or []
    out.append(f"  daemons       : running={len(d.get('running', []))} / "
               f"last_exit_nonzero={len(bad)}")
    if bad:
        names = ", ".join(x["label"] for x in bad[:5])
        out.append(f"                  ⚠️  {names}")

    c = o["cost"]
    if c.get("today"):
        # cost_today returns text；just take first 80 chars
        today_str = str(c["today"]).split("\n")[0][:80]
        out.append(f"  cost (today)  : {today_str}")

    e = o["errors"]
    if e.get("total_24h", 0) > 0:
        codes = e.get("by_code", {})
        top = ", ".join(f"{k}={v}" for k, v in
                        sorted(codes.items(), key=lambda kv: -kv[1])[:3])
        out.append(f"  errors (24h)  : {e['total_24h']}  ({top})")

    return "\n".join(out)
