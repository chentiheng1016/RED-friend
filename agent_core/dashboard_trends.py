"""Trend / time-series helpers for dashboard.

把「今天 vs 昨天 vs 7 天平均」之類的對比指標算出來，給 system_status
的 cost / runs / errors 段加 ↑/↓ 箭頭 + delta %。

讓 大王 一看就知道：
  - 今天 cost 是不是異常飆高？
  - 任務失敗率是不是比昨天差？
  - 錯誤 log 量是不是突然爆增？

唯讀（讀現有 cost.jsonl / runs/index.jsonl / logs/*.log），不寫 state。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

from agent_core.logging_and_paths import _LOG_DIR, RUNS_DIR


def _arrow(today: float, baseline: float, threshold_pct: float = 10) -> str:
    """根據 today vs baseline 給 ↑/↓/➡ + delta% 箭頭文字。"""
    if baseline <= 0:
        return "(無 baseline)"
    delta_pct = (today - baseline) / baseline * 100
    if abs(delta_pct) < threshold_pct:
        return f"➡ {delta_pct:+.0f}%"
    if delta_pct > 0:
        return f"📈 {delta_pct:+.0f}%"
    return f"📉 {delta_pct:+.0f}%"


# ────────────────────────────────────────────────────────────────────
# Cost trend
# ────────────────────────────────────────────────────────────────────
# #328（2026-08-01）依 GCP 帳單校正 _PRICING（舊表低估 ~100×）並補記漏掉的
# embedding 呼叫。cutover 之前寫進 cost.jsonl 的列是舊費率算出來的，跟之後的列
# 不同刻度——混進 7d/30d 均值會把基準線壓低，讓 cost_ratio 一路假警報：
# 2026-08-04 實測今日 $347 vs 被舊列壓低的均值 $118 = 2.9× → warn，但 8/01 起
# 的真實均值是 $276、比值只有 1.26×（同量級、根本不該響）。
# 均值只取 cutover 當天（含）之後的日子；設成空字串可關掉這個過濾。
# 🚨 2026-08-05 再推一次：08-01 那批列是**新台幣**刻度（見 cost_tracker._PRICING
# 的幣別註解），跟今天的真美元列差 ~32 倍，混進均值一樣會製造假警報 —— 只是這次
# 方向相反（舊列偏高，會把均值墊高、讓真正的暴衝反而不響）。
#
# ✅ 2026-08-05（本 PR）起預設關閉：cost_tracker._normalize_entry_costs 會在讀取
# 時把每一列都重算成當前紀元的口徑，歷史列不再有「刻度不同」這回事，這個日期過濾
# 就沒有存在意義了。留著反而有害 —— 每次改牌價都要把 cutover 往後推，而推一次就
# 讓 7d/30d 均值失去所有基準日：實測推到 08-05 之後 avg7 = avg30 = 0.0，
# `if avg7 > 0.01` 那道守門直接讓整條 cost_ratio 紅線靜默失效。
#
# 機制本身保留（env RED_COST_PRICING_CUTOVER_DATE 仍可設日期），萬一將來出現
# 正規化補不回來的列——例如缺 raw token 的紀錄——還可以臨時擋掉那段。
# 開這個 PR 時實測 31 天視窗 36,904 列 raw token 覆蓋率 100%，沒有這種列。
_PRICING_CUTOVER_DEFAULT = ""


def _pricing_cutover() -> date | None:
    """均值基準線的起算日；回 None = 不過濾（env 設空或格式壞）。"""
    raw = (os.getenv("RED_COST_PRICING_CUTOVER_DATE", _PRICING_CUTOVER_DEFAULT) or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def cost_trend() -> dict:
    """從 cost.jsonl 算 today / yesterday / 7d-avg / 30d-avg。

    Returns dict：
      {
        "today_usd": 0.25, "today_calls": 103,
        "yesterday_usd": 0.57, "yesterday_calls": 145,
        "7d_avg_usd": 0.40,
        "30d_avg_usd": 0.32,
        "vs_yesterday": "📉 -56%",
        "vs_7d_avg":    "📉 -38%",
        "vs_30d_avg":   "📉 -22%",
      }
    """
    try:
        from agent_core.cost_tracker import (
            _COST_LOG, _is_embedding_entry, _load_jsonl_window,
            _normalize_entry_costs,
        )
    except Exception:
        return {}
    if not os.path.isfile(_COST_LOG):
        return {}

    today_d = date.today()
    yesterday_d = today_d - timedelta(days=1)
    daily: dict[str, dict[str, float]] = {}  # "YYYY-MM-DD" -> {"usd": x, "calls": n}

    try:
        # 反向讀 + 30 天早停（健檢 Medium：以前前向掃全檔 — cost.jsonl 25MB/
        # 89K 行，而 alert daemon 每 5 分鐘經 _check_cost 跑到這裡）。31 天視窗
        # 保證涵蓋下面 `days > 30` 的日期過濾。
        # ⚠️ 這裡讀的是 _load_jsonl_window（低階、不是 _load_entries），所以要自己
        # 套一次幣別/口徑正規化 —— 否則 _check_cost 的 today_usd 會是混紀元的和，
        # 正是 2026-08-05 假告警的來源。
        for r in _normalize_entry_costs(_load_jsonl_window(_COST_LOG, hours=31 * 24)):
            ts = (r.get("ts") or "")[:10]
            if not ts:
                continue
            try:
                d = datetime.fromisoformat(ts).date()
            except (ValueError, TypeError):
                continue
            if (today_d - d).days > 30:
                continue  # 只看 30 天內
            key = d.isoformat()
            cell = daily.setdefault(key, {"usd": 0.0, "calls": 0, "emb_usd": 0.0})
            usd = float(r.get("cost_usd") or r.get("usd") or 0.0)
            cell["usd"] += usd
            cell["calls"] += 1
            # embedding 分開記：背填/重建的日子它會從個位數 % 跳到七成以上，
            # 是 cost_ratio 告警最常見的「非異常」成因（見 _check_cost 註解）。
            if _is_embedding_entry(r):
                cell["emb_usd"] += usd
    except Exception:
        return {}

    _EMPTY_CELL = {"usd": 0.0, "calls": 0, "emb_usd": 0.0}
    today_cell = daily.get(today_d.isoformat(), _EMPTY_CELL)
    yesterday_cell = daily.get(yesterday_d.isoformat(), _EMPTY_CELL)

    # 7d/30d avg — 不含今天，避免 today 被 self-compare
    pricing_cutover = _pricing_cutover()

    def _avg(days: int) -> float:
        cutoff = today_d - timedelta(days=days)
        usd_sum = 0.0
        n = 0
        for k, cell in daily.items():
            try:
                d = datetime.fromisoformat(k).date()
            except (ValueError, TypeError):
                continue
            if d == today_d:
                continue
            if d < cutoff:
                continue
            if pricing_cutover is not None and d < pricing_cutover:
                continue  # 舊費率列，刻度不同（見 _PRICING_CUTOVER_DEFAULT）
            usd_sum += cell["usd"]
            n += 1
        return usd_sum / max(1, n) if n > 0 else 0.0

    avg7 = _avg(7)
    avg30 = _avg(30)

    return {
        "today_usd": today_cell["usd"],
        "today_calls": today_cell["calls"],
        "today_embedding_usd": today_cell.get("emb_usd", 0.0),
        "yesterday_usd": yesterday_cell["usd"],
        "yesterday_calls": yesterday_cell["calls"],
        "7d_avg_usd": avg7,
        "30d_avg_usd": avg30,
        "vs_yesterday": _arrow(today_cell["usd"], yesterday_cell["usd"], 15),
        "vs_7d_avg": _arrow(today_cell["usd"], avg7, 15),
        "vs_30d_avg": _arrow(today_cell["usd"], avg30, 15),
        "daily_usd": {k: v["usd"] for k, v in sorted(daily.items())[-14:]},  # 最近 14 天 series
    }


# ────────────────────────────────────────────────────────────────────
# Audited runs trend
# ────────────────────────────────────────────────────────────────────
def runs_trend() -> dict:
    """從 runs/index.jsonl 算 today / yesterday 的 success / error count。"""
    index_path = os.path.join(RUNS_DIR, "index.jsonl")
    if not os.path.isfile(index_path):
        return {}

    today_d = date.today()
    yesterday_d = today_d - timedelta(days=1)
    daily: dict[str, dict[str, int]] = {}

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                except (ValueError, TypeError):
                    continue
                started = r.get("started_at") or ""
                if not started:
                    continue
                try:
                    d = datetime.fromisoformat(started.replace("Z", "")).date()
                except (ValueError, TypeError):
                    continue
                if (today_d - d).days > 30:
                    continue
                key = d.isoformat()
                cell = daily.setdefault(key, {"ok": 0, "err": 0, "running": 0, "total": 0})
                st = r.get("status", "?")
                if st == "success":
                    cell["ok"] += 1
                elif st == "error":
                    cell["err"] += 1
                elif st == "running":
                    cell["running"] += 1
                cell["total"] += 1
    except Exception:
        return {}

    today_cell = daily.get(today_d.isoformat(), {"ok": 0, "err": 0, "total": 0})
    yesterday_cell = daily.get(yesterday_d.isoformat(), {"ok": 0, "err": 0, "total": 0})

    def _err_pct(c: dict) -> float:
        if c["total"] <= 0:
            return 0.0
        return c["err"] / c["total"] * 100

    return {
        "today": today_cell,
        "yesterday": yesterday_cell,
        "today_err_pct": _err_pct(today_cell),
        "yesterday_err_pct": _err_pct(yesterday_cell),
        "vs_yesterday_total": _arrow(today_cell["total"], yesterday_cell["total"]),
        "vs_yesterday_err": _arrow(today_cell["err"], yesterday_cell["err"]),
    }


# ────────────────────────────────────────────────────────────────────
# Error log trend
# ────────────────────────────────────────────────────────────────────
# False-positive avoidance: routine daemon summary counters are not error
# entries. Examples we have seen in production:
#   `[alert_pusher] ... failed=0`
#   `[email_ingest] 統計：... / 失敗 0`
#   `[daemon_watchdog] {"findings": 0, ..., "failed": [], ...}`（健康心跳 JSON；
#   #452 幫每行加上時間戳後才開始被計入「今日」，一天 ~467 條直接撞破 crit 500）
# Without the lookaheads, those informational lines alone can trip
# `errors_log_*` and pollute RCA buckets. Keep real narrative failures like
# `failed to ...`, `failed:` and `Sync 失敗，請重試`.
_ERR_PATTERN = re.compile(
    r"(?:ERROR|Traceback|failed(?!=|['\"]\s*:)|失敗(?!\s*(?:[:：=])?\s*\d+\b)"
    r"|❌|Exception:|Error:)",
    re.IGNORECASE,
)


def errors_trend() -> dict:
    """掃 var/logs/*.log 算今天 vs 昨天 vs 過去 7 天的 ERROR / Traceback 行數。

    這是粗略估計（看 log 檔 mtime + 內容，不每行解 timestamp）。
    對「**有沒有突然爆量**」訊號夠用。
    """
    if not _LOG_DIR or not os.path.isdir(_LOG_DIR):
        return {}

    today_d = date.today()
    yesterday_d = today_d - timedelta(days=1)
    today_count = yesterday_count = 0
    week_total = 0
    timestamp_re = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")

    for fn in os.listdir(_LOG_DIR):
        if not fn.endswith(".log"):
            continue
        fp = os.path.join(_LOG_DIR, fn)
        if not os.path.isfile(fp):
            continue
        try:
            # 只讀最後 256KB，避免讀爆
            with open(fp, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 262144))
                tail = f.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        for line in tail.splitlines():
            if not _ERR_PATTERN.search(line):
                continue
            m = timestamp_re.search(line)
            if not m:
                continue
            try:
                ld = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except (ValueError, TypeError):
                continue
            if ld == today_d:
                today_count += 1
            elif ld == yesterday_d:
                yesterday_count += 1
            if 0 < (today_d - ld).days <= 7:
                week_total += 1

    week_avg = week_total / 7
    return {
        "today": today_count,
        "yesterday": yesterday_count,
        "7d_avg": week_avg,
        "vs_yesterday": _arrow(today_count, yesterday_count),
        "vs_7d_avg": _arrow(today_count, week_avg),
    }


# ────────────────────────────────────────────────────────────────────
# Format helpers — 給 dashboard.py 用
# ────────────────────────────────────────────────────────────────────
def format_cost_trend_lines(trend: dict) -> list[str]:
    if not trend:
        return ["  （無 cost trend 資料）"]
    return [
        f"  今日：{trend['today_calls']} 次  US${trend['today_usd']:.4f}  "
        f"vs 昨 {trend['vs_yesterday']}  vs 7d-avg {trend['vs_7d_avg']}",
        f"  昨日：{trend['yesterday_calls']} 次  US${trend['yesterday_usd']:.4f}  "
        f"|  7d 平均：US${trend['7d_avg_usd']:.4f}  |  30d 平均：US${trend['30d_avg_usd']:.4f}",
    ]


def format_runs_trend_lines(trend: dict) -> list[str]:
    if not trend:
        return ["  （無 runs trend 資料）"]
    t = trend["today"]
    y = trend["yesterday"]
    return [
        f"  今日：{t['ok']} 成功 / {t['err']} 失敗 / {t['total']} 總數  "
        f"err% = {trend['today_err_pct']:.1f}%",
        f"  昨日：{y['ok']} 成功 / {y['err']} 失敗 / {y['total']} 總數  "
        f"err% = {trend['yesterday_err_pct']:.1f}%  "
        f"vs 昨總 {trend['vs_yesterday_total']}  vs 昨錯 {trend['vs_yesterday_err']}",
    ]


def format_errors_trend_lines(trend: dict) -> list[str]:
    if not trend:
        return ["  （無 errors trend 資料）"]
    return [
        f"  今日 {trend['today']} 條  vs 昨 {trend['vs_yesterday']}  "
        f"vs 7d-avg ({trend['7d_avg']:.1f}/天) {trend['vs_7d_avg']}",
    ]
