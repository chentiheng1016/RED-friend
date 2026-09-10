#!/usr/bin/env python3
"""飛越 ERP 全 schema 擷取 + 邏輯落點量測（唯讀，走 SSH+10g sqlplus）。

這是「反推重寫 ERP」評估的『無悔第一步』②③。**主機清毒 + SSH 就緒前跑不起來**
（RED_ERP_ORACLE_ENABLED=0 會直接擋下，物理上碰不到主機）。

用法（主機清乾淨、設好 RED_ERP_ORACLE_ENABLED=1 之後）：
    # 大字典查詢較慢，建議放寬逾時
    RED_ERP_ORACLE_ENABLED=1 RED_ERP_ORACLE_TIMEOUT_S=300 \\
        .venv/bin/python scripts/erp_schema_probe.py            # 自動偵測 APP schema
    ... scripts/erp_schema_probe.py --list-owners               # 先看有哪些 owner
    ... scripts/erp_schema_probe.py --owner APP                 # 指定 schema
輸出：var/data/erp_schema_probe/erp-schema-<owner>-*.md / .json（含 latest 固定路徑）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.erp_schema_probe import (  # noqa: E402
    list_owners,
    probe,
    render_markdown,
    write_report,
)
from agent_core.erp_oracle_client import ErpOracleError, is_enabled  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="飛越 ERP 全 schema 擷取 + 邏輯落點量測（唯讀）。")
    p.add_argument("--owner", default=None, help="schema owner（省略則自動偵測飛越 BQ_SE_* 所在 schema）。")
    p.add_argument("--list-owners", action="store_true", help="只列出可見的 owner + 表數，不做完整探勘。")
    p.add_argument("--out-dir", default=None, help="輸出目錄（預設 var/data/erp_schema_probe）。")
    p.add_argument("--stdout", action="store_true", help="把 markdown 印到 stdout（不寫檔）。")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not is_enabled():
        print(
            "ERP 查詢尚未啟用（RED_ERP_ORACLE_ENABLED=0）。\n"
            "主機清毒 + 收公網埠 + SSH 就緒後，設 RED_ERP_ORACLE_ENABLED=1 再跑。\n"
            "（這是刻意的閘門：未啟用時本探勘物理上不會連上那台主機。）",
            file=sys.stderr,
        )
        return 3
    try:
        if args.list_owners:
            for owner, n in list_owners():
                print(f"{owner}\t{n}")
            return 0
        report = probe(args.owner)
    except ErpOracleError as exc:
        print(f"schema 探勘失敗：{exc}", file=sys.stderr)
        return 2

    if args.stdout:
        print(render_markdown(report))
        return 0
    md_path, json_path = write_report(report, out_dir=args.out_dir)
    s = report["summary"]
    print(f"schema `{report['owner']}`：{s['tables']} 表 / {s['columns']} 欄 / {s['views']} views；"
          f"邏輯 {s['logic_objects']} 物件 · {s['logic_lines']} 行 · {s['triggers']} triggers。")
    print(f"→ {md_path}")
    print(f"→ {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
