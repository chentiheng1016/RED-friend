#!/usr/bin/env python3
"""飛越 ERP 全量資料鏡像 → 本地 DuckDB（唯讀，走 SSH+10g sqlplus 串流）。

前置：先跑過 scripts/erp_schema_probe.py（要 var/data/erp_schema_probe/*-latest.json）。
用法：
    RED_ERP_ORACLE_ENABLED=1 RED_ERP_ORACLE_TIMEOUT_S=1200 \\
        .venv/bin/python scripts/erp_mirror.py                    # 全部業務 schema
    ... scripts/erp_mirror.py --schemas SC00,MK00                 # 只鏡像指定 schema
    ... scripts/erp_mirror.py --only SC00.SE_BOM_SIZE --keep-raw  # 單表（驗證用，保留 gz）
    ... scripts/erp_mirror.py --limit 20                          # 前 20 表（試跑）
可續跑：manifest 記 status==done 的表會跳過。輸出 DuckDB 在 var/data/erp_mirror/erp_full.duckdb。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.erp_mirror import mirror, sync_item_alias  # noqa: E402
from agent_core.erp_oracle_client import is_enabled  # noqa: E402


def _split(v: str | None) -> list[str] | None:
    if not v:
        return None
    return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="飛越 ERP 全量資料鏡像 → DuckDB（唯讀）。")
    p.add_argument("--schemas", default=None, help="逗號分隔 schema（預設全業務 schema）。")
    p.add_argument("--only", default=None, help="逗號分隔 owner.table 或 table（只鏡像這些，驗證用）。")
    p.add_argument("--limit", type=int, default=None, help="只鏡像前 N 張表（試跑）。")
    p.add_argument("--keep-raw", action="store_true", help="保留中繼 .csv.gz（預設載入後刪）。")
    p.add_argument("--alias-only", action="store_true",
                   help="只同步 料號→庫存編號(O_ITEMNO 舊碼) 對照（_item_alias/v_item_alias）。")
    args = p.parse_args(argv)

    if not is_enabled():
        print("ERP 查詢尚未啟用（RED_ERP_ORACLE_ENABLED=0）。設 =1 再跑。", file=sys.stderr)
        return 3

    if args.alias_only:
        res = sync_item_alias()
        print(f"sync_item_alias：{res}")
        return 0 if res.get("status") == "done" else 1

    summary = mirror(
        schemas=_split(args.schemas),
        only_tables=_split(args.only),
        limit_tables=args.limit,
        keep_raw=args.keep_raw,
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
