#!/usr/bin/env python3
"""Standalone cron：每天 07:00 + 12:30 重建工廠數據倉（DuckDB）。

純確定性維護：抓 Drive 最新『生產日報進度表』→ fact_production_daily / fact_order_state，
讓小紅查到最新生產數據（不打 Gemini）。07:00 給開工的「截至昨天」完整快照、12:30 在
當天日報（生管多 8–11 點才寄）進來後再刷一次。設計見 docs/factory_warehouse_design.md。
"""
import sys
import time
import traceback
from pathlib import Path

# repo root = 向上 3 層（scripts/ → launchd/ → repo）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import rotate_log, run_with_deadline
from agent_core.env_utils import env_int
from agent_core.factory_warehouse import build_factory_warehouse

# backfill 時間預算的安全邊際：逐份檢查只擋「還沒開工的」，已開跑的最後一份最壞
# 可吃 2×150s Drive belt 超時 + 本地 OCR + Gemini 重試，邊際要蓋得住它，
# 否則仍會撞 run_with_deadline 的 os._exit(75)（2026-07-24 12:50 事故）。
_BACKFILL_SAFETY_MARGIN_S = 300


def _rebuild(deadline_s=None, started_ts=None):
    """重建一次倉並把結果印進 daemon log（成功靜默、失敗 exit≠0 讓告警看得到）。"""
    res = build_factory_warehouse()
    if res.get("error"):
        print(f"[factory_warehouse_rebuild] ⚠️ 重建失敗：{res['error']}")
        sys.exit(1)
    msg = (f"已重建：{res.get('daily_rows', 0)} 日產量 / {res.get('orders', 0)} 指令 / "
           f"{res.get('email_threads', 0)} 郵件 / {res.get('shipment_docs', 0)} 單據 / "
           f"{res.get('payment_rows', 0)} 付款（來源修改日 {res.get('report_modified') or '?'}）")
    if res.get("warnings"):
        msg += "；解析提醒：" + "；".join(res["warnings"])
    print(f"[factory_warehouse_rebuild] {msg}")
    # Phase 3b：每輪增量補抽一小批單據金額（預算上限、只 PDF），覆蓋率隨日漸增、抽完即 no-op。
    n = env_int("RED_WAREHOUSE_PAYMENT_BACKFILL_N", 40, min_value=0, max_value=500)
    if n:
        time_budget = None
        if deadline_s is not None and started_ts is not None:
            time_budget = deadline_s - (time.monotonic() - started_ts) - _BACKFILL_SAFETY_MARGIN_S
            if time_budget <= 0:
                print(f"[factory_warehouse_rebuild] 付款 backfill：主重建後剩餘時間不足"
                      f"安全邊際 {_BACKFILL_SAFETY_MARGIN_S}s，本輪跳過（下輪自動補）")
                return res
        try:
            from agent_core.factory_warehouse_extract import extract_payments_batch
            b = extract_payments_batch(max_docs=n, time_budget_s=time_budget)
            note = "（時間預算耗盡、提前收工）" if b.get("time_exhausted") else ""
            print(f"[factory_warehouse_rebuild] 付款 backfill：抽 {b.get('extracted', 0)} 份、"
                  f"剩 {b.get('candidates_remaining', '?')}、花費 ${b.get('spent_usd', 0)}{note}")
        except Exception as e:  # noqa: BLE001
            print(f"[factory_warehouse_rebuild] backfill 失敗（不影響倉）：{e}")
    return res


def main():
    rotate_log("factory_warehouse_rebuild")
    # 整輪 wall-clock 看門狗：抓 Drive 大表可能卡住，別堵住下個 calendar interval。
    # 預設 1200s，RED_WAREHOUSE_REBUILD_DEADLINE_S 可調。deadline 同時傳給 _rebuild，
    # 讓 backfill 拿「扣掉主重建耗時後的剩餘」當自己的時間預算、在看門狗撞牆前先收工。
    deadline_s = env_int("RED_WAREHOUSE_REBUILD_DEADLINE_S", 1200, min_value=60, max_value=7200)
    started_ts = time.monotonic()
    run_with_deadline(
        lambda: _rebuild(deadline_s=deadline_s, started_ts=started_ts),
        deadline_s,
        label="factory_warehouse_rebuild",
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[factory_warehouse_rebuild] 主程序失敗：{e}")
        traceback.print_exc()
        # exit 0 = 連續重建失敗完全沒有告警面（不像 erp 還有 manifest 新鮮度兜底）。
        sys.exit(1)
