#!/usr/bin/env python3
"""Standalone cron：每日 02:00 刷新飛越 ERP 本地鏡像的營運熱表（訂單/BOM/採購MRP/庫存/生產/樣品）。

全量鏡像(1538 表/2198萬列)是一次性、~12-15h；這支只重刷會天天變的營運熱表(agent_core.erp_mirror
HOT_TABLES，~22 張、~1-1.5h)，讓小紅查到當天最新。歷史/備份大表不重刷。刷新後可讀視圖(v_*)自動反映。

⚠️ 需 RED_ERP_ORACLE_ENABLED=1 + SSH 金鑰(走 SSH+10g sqlplus 連 ERP 主機)——由 plist env 提供。
⚠️ 刷新期間會短暫寫鎖 DuckDB(小紅該時段查會 locked)——排在 02:00 低用量時段。
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import rotate_log, run_with_deadline
from agent_core.env_utils import env_int
from agent_core.erp_mirror import refresh_hot
from agent_core.erp_oracle_client import is_enabled
from agent_core.erp_security_patrol import run_patrol_safe


def _refresh():
    if not is_enabled():
        print("[erp_mirror_refresh] ⚠️ RED_ERP_ORACLE_ENABLED=0，跳過（plist 未帶 env？）")
        return {"skipped": True}
    res = refresh_hot(log=lambda m: print(f"[erp_mirror_refresh] {m}"))
    print(f"[erp_mirror_refresh] 完成：done={res.get('done')} 對帳不符={res.get('count_mismatch')} "
          f"失敗={res.get('errors')} / 共 {res.get('total')} 熱表")
    # 順手 RDP 安全巡檢（同一條 SSH 通道統計 Security 4625/4624 → 寫報告 + 旗標，
    # dashboard_alerts._check_erp_rdp_security 讀旗標推 Telegram）。run_patrol_safe
    # 永不 raise，這層再包一次保險——巡檢任何失敗都只記 log，不碰鏡像結果與
    # exit code（exit≠0 是留給鏡像失敗的告警訊號）。
    try:
        run_patrol_safe(log=lambda m: print(f"[erp_mirror_refresh] {m}"))
    except Exception as e:  # noqa: BLE001
        print(f"[erp_mirror_refresh] ⚠️ RDP 安全巡檢異常（忽略）：{e}")
    return res


def main():
    rotate_log("erp_mirror_refresh")
    # 整輪 wall-clock 看門狗：ERP 主機/SSH 可能卡，別堵住隔天。預設 7200s(2h)。
    res = run_with_deadline(
        _refresh,
        env_int("RED_ERP_REFRESH_DEADLINE_S", 7200, min_value=300, max_value=21600),
        label="erp_mirror_refresh",
    )
    # 失敗要讓 launchctl 看得到（exit≠0 → dashboard_alerts._check_daemon_health 會報）。
    # 以前所有失敗路徑都 exit 0 = 靜默；搭配 _check_erp_mirror_stale 新鮮度檢查雙保險。
    if res.get("skipped") or res.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[erp_mirror_refresh] 主程序失敗：{e}")
        traceback.print_exc()
        # 吞例外後 exit 0 會讓「exit≠0 → daemon_fail 告警」失明（refresh_hot 拋
        # SSH/DuckDB 例外正走這裡），與 main() 內特地 sys.exit(1) 的設計自相矛盾。
        sys.exit(1)
