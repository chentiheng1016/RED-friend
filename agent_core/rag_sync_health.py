"""rag_sync_health：Drive/Gmail 夜間整理（rag_sync）的健康摘要。

給「每日早上自動推播」與大王隨時查詢用。**只讀** `rag_sync_last_run.json`（夜跑
狀態）與 `daemon-rag_sync.log`（Gemini 503 / 連線失敗 / 各硬碟結果統計），**完全不碰
ChromaDB**——因為夜跑期間資料庫正在 upsert，連它的 system_status 會卡（get_tenant），
而健康摘要本身不該卡。SAFE tier、background_safe（背景 dispatcher 可用）。
"""
from __future__ import annotations

import os
import re
import time


def _rag_log_path() -> str:
    # 用 logging_and_paths 的統一 log 目錄（含 RED_RUNTIME_DIR override + legacy 遷移），
    # 才對齊 live daemon 實際寫的位置。
    try:
        from agent_core.logging_and_paths import _LOG_DIR
        if _LOG_DIR:
            return os.path.join(_LOG_DIR, "daemon-rag_sync.log")
    except Exception:
        pass
    try:
        from agent_core.logging_and_paths import RUNTIME_ROOT
        return os.path.join(RUNTIME_ROOT, "logs", "daemon-rag_sync.log")
    except Exception:
        return os.path.expanduser("~/RED/var/logs/daemon-rag_sync.log")


def _tail_text(path: str, max_bytes: int = 600_000) -> str:
    """讀檔尾 ~max_bytes（夠涵蓋最近一輪），不把整個多 MB log 載進記憶體。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _fmt_ago(ts: float, now: float) -> str:
    try:
        sec = now - float(ts)
    except (TypeError, ValueError):
        return "?"
    if sec < 0:
        return "剛剛"
    h = sec / 3600
    if h >= 1:
        return f"{h:.1f} 小時前"
    return f"{int(sec / 60)} 分鐘前"


def rag_sync_health() -> str:
    """查最近一輪 Drive/Gmail 夜間整理（rag_sync）的健康摘要：上次跑完沒、卡不卡、
    新整理幾檔、Gemini 過載（503）幾次。

    給每日早上自動推播或大王隨時查用。只讀狀態檔與 log、**不碰資料庫**，所以即使夜跑
    正在跑、ChromaDB 忙碌也不會卡。回人類可讀摘要。
    """
    now = time.time()
    out: list[str] = ["📊 夜間整理（RAG）健康"]

    # ── 1. 上次整理狀態（rag_sync_last_run.json）──
    try:
        from agent_core.ingest.sync_guard import read_last_run
        last = read_last_run() or {}
    except Exception:
        last = {}
    status = str(last.get("status") or "?")
    status_zh = {
        "success": "✅ 上次成功跑完",
        "running": "🔄 目前正在跑",
        "failed": "❌ 上次失敗",
    }.get(status, f"狀態：{status}")
    started, finished = last.get("started_at"), last.get("finished_at")
    if status == "running" and started:
        out.append(f"  {status_zh}（{_fmt_ago(started, now)}開始）")
    elif finished:
        out.append(f"  {status_zh}（{_fmt_ago(finished, now)}）")
    else:
        out.append(f"  {status_zh}")

    # ── 2. log 統計（最近一輪窗口；只讀檔尾）──
    log = _tail_text(_rag_log_path())
    n503 = log.count("503 UNAVAILABLE")
    n_conn = log.count("Unable to find the server")
    n_quota = log.count("RESOURCE_EXHAUSTED") + log.count("prepayment")

    if n503:
        out.append(f"  ⚠️ Gemini 過載（503）：近期約 {n503} 次（向量化卡在這、會拖慢整輪）")
    else:
        out.append("  Gemini：近期無 503 過載 👍")
    if n_conn:
        out.append(f"  ⚠️ 連 Google 失敗：{n_conn} 次（網路不穩）")
    if n_quota:
        out.append(f"  ⚠️ 配額/預付不足：{n_quota} 次")

    # 最近幾個硬碟的整理結果（synced/skipped/listing_complete）
    results = re.findall(
        r"SharedDrive (\S+): \{[^}]*?'synced': (\d+), 'skipped': (\d+)"
        r"[^}]*?'listing_complete': (True|False)",
        log,
    )
    if results:
        recent = results[-3:]
        done = sum(1 for r in recent if r[3] == "True")
        out.append(f"  最近處理：{done}/{len(recent)} 個硬碟完整掃完")
        for drive_id, synced, skipped, complete in recent:
            mark = "✅" if complete == "True" else "⏳"
            out.append(f"    {mark} {drive_id[:14]}…：新整理 {synced} 檔、跳過 {skipped}")
    else:
        out.append("  （近期 log 沒有硬碟整理結果，可能還在抓檔或剛起步）")

    # ── 3. 一句話結論 ──
    over_15h = (
        status == "running" and started
        and (now - float(started)) > 15 * 3600
    )
    if over_15h:
        verdict = "🔴 已跑超過 15 小時還沒結束 —— 可能卡住，建議查 log"
    elif status == "failed":
        verdict = "🔴 上次整理失敗 —— 建議查 log"
    elif n503 > 200:
        verdict = "🟡 有在跑，但 Gemini 過載嚴重拖慢（503 很多）—— 多半是 Google 尖峰，非系統故障"
    elif status == "success":
        verdict = "🟢 上次順利跑完，無大礙"
    else:
        verdict = "🟡 整理進行中或狀態不明，下一輪再看"
    out.append(f"  結論：{verdict}")

    return "\n".join(out)
