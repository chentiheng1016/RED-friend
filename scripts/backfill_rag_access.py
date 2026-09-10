"""rag_access 規則 → 存量 chunk ACL metadata 一次性全量 backfill（CLI）。

核心邏輯已抽到 `agent_core.ingest.acl_reconcile.reconcile_acl`（夜跑也用同一份）；
本檔只是「無時間預算 + 啟動前擋 rag_sync 並行」的手動入口。

用法（從 repo root，指向 live runtime）：
  RED_RUNTIME_DIR=/Users/user/RED/var \
  RED_CHROMA_HTTP_URL=http://127.0.0.1:8000 RED_EMBED_DIM=768 \
  .venv/bin/python scripts/backfill_rag_access.py [--dry-run] [--skip-proc-check]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _abort_if_sync_running() -> None:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "rag_sync.py|drive_sync.py"],
            capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        return
    if out:
        raise SystemExit(
            f"⛔ 偵測到 rag_sync/drive_sync 在跑（pid {out}）— 請等夜跑結束再 backfill，"
            "或確認後帶 --skip-proc-check。"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-proc-check", action="store_true")
    args = parser.parse_args()

    if not args.skip_proc_check:
        _abort_if_sync_running()

    from agent_core.ingest.acl_reconcile import reconcile_acl

    summary = reconcile_acl(time_budget_s=None, dry_run=args.dry_run, log=print)
    print(
        f"\n完成：共掃 {summary['scanned']:,} chunk、更新 {summary['updated']:,}"
        f"{'（dry-run，未寫入）' if args.dry_run else ''}，耗時 {summary['elapsed_s']:,.0f}s"
    )


if __name__ == "__main__":
    main()
