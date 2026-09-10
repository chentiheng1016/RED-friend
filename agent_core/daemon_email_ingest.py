"""Email ingest helpers extracted from agent_daemon."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable

LAKE_BACKFILL_DAYS = 730
LAKE_BATCH_PER_RUN = 20
LAKE_NEW_WINDOW_HOURS = 24

# Heartbeat：每跑必寫，即使新信 0。讓 alert 能區分「daemon 沒跑」vs「沒新信」
# 之前 alert 看 parquet mtime，空跑時不更新 → 看起來像 stale 但其實 daemon 健康。
EMAIL_INGEST_HEARTBEAT_FILE = "email_ingest_heartbeat.json"


def _write_heartbeat(stats: dict | None = None) -> None:
    """寫 heartbeat 到 var/state/email_ingest_heartbeat.json。失敗 silent。"""
    try:
        from agent_core.logging_and_paths import STATE_DIR
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, EMAIL_INGEST_HEARTBEAT_FILE)
        payload = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "stats": stats or {},
        }
        # atomic write 避免半寫
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass  # heartbeat 寫不了不該擋住 daemon 主流程


def task_email_ingest(
    *,
    email_lake_dir: str,
    lake_load_df: Callable[[], Any],
    lake_append: Callable[[list[dict]], int],
    classify_email_for_lake: Callable[[str], dict[str, Any] | None],
    get_service: Callable[[str, str], Any],
) -> None:
    """Incrementally ingest Gmail into the email data lake."""
    os.makedirs(email_lake_dir, exist_ok=True)
    df = lake_load_df()
    existing_ids = set(df["message_id"].tolist()) if df is not None and not df.empty else set()
    print(f"[email_ingest] 目前 lake 內有 {len(existing_ids)} 筆")

    service = get_service("gmail", "v1")
    budget = LAKE_BATCH_PER_RUN
    new_rows = []
    stats = {"new_processed": 0, "backfill_processed": 0, "skipped": 0, "fail": 0}
    phase_errors: list[str] = []
    phase1_ok = False
    phase2_ok = False

    try:
        q_new = f"newer_than:{LAKE_NEW_WINDOW_HOURS}h"
        resp = service.users().messages().list(userId="me", q=q_new, maxResults=50).execute()
        new_ids = [msg["id"] for msg in (resp.get("messages") or []) if msg["id"] not in existing_ids]
        for mid in new_ids[:budget]:
            row = classify_email_for_lake(mid)
            if row is None:
                stats["fail"] += 1
            elif row.get("skipped"):
                stats["skipped"] += 1
                existing_ids.add(mid)
            else:
                new_rows.append(row)
                stats["new_processed"] += 1
                existing_ids.add(mid)
            budget -= 1
            if budget <= 0:
                break
        phase1_ok = True
    except Exception as exc:
        msg = f"phase1 (新信) 失敗：{exc}"
        print(f"[email_ingest] {msg}")
        phase_errors.append(msg)

    if budget > 0:
        try:
            q_old = f"newer_than:{LAKE_BACKFILL_DAYS}d older_than:{LAKE_NEW_WINDOW_HOURS}h"
            resp = service.users().messages().list(userId="me", q=q_old, maxResults=100).execute()
            old_ids = [msg["id"] for msg in (resp.get("messages") or []) if msg["id"] not in existing_ids]
            for mid in old_ids[:budget]:
                row = classify_email_for_lake(mid)
                if row is None:
                    stats["fail"] += 1
                elif row.get("skipped"):
                    stats["skipped"] += 1
                    existing_ids.add(mid)
                else:
                    new_rows.append(row)
                    stats["backfill_processed"] += 1
                    existing_ids.add(mid)
            phase2_ok = True
        except Exception as exc:
            msg = f"phase2 (回填) 失敗：{exc}"
            print(f"[email_ingest] {msg}")
            phase_errors.append(msg)
    else:
        phase2_ok = True  # 沒跑等於 ok（不該因為 budget 用完視為失敗）

    if new_rows:
        total = lake_append(new_rows)
        print(f"[email_ingest] 寫入 {len(new_rows)} 筆，lake 共 {total} 筆")
    print(
        "[email_ingest] 統計："
        f"新信 {stats['new_processed']} / "
        f"回填 {stats['backfill_processed']} / "
        f"敏感過濾 {stats['skipped']} / "
        f"失敗 {stats['fail']}"
    )
    # Heartbeat：必須至少一個 phase 成功才寫 — 不然 dashboard 看「fresh
    # heartbeat」誤判 daemon 健康，但實際 Gmail 已連 N 小時沒抓到信。
    # 兩個 phase 都失敗 → 不寫 heartbeat，stale 警告才會正確觸發。
    if phase1_ok or phase2_ok:
        stats_with_errors = dict(stats)
        if phase_errors:
            stats_with_errors["partial_errors"] = phase_errors
        _write_heartbeat(stats_with_errors)
    else:
        print("[email_ingest] ⚠️ 兩個 phase 全失敗，不寫 heartbeat（讓 stale alert 觸發）")
