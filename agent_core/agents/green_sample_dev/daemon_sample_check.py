"""Sample deadline daemon helper — 共用實作。

每日 09:00 跑一次，掃所有 open / delayed 樣品，逾期的：
  - 自動建 Gmail draft 催信（auto_draft_followup=True）
  - 推 Telegram 提醒大王
  - 全部 OK 就安靜退出（不打擾）
"""
from __future__ import annotations

import traceback
from typing import Any, Callable


def task_sample_check(
    *,
    check_sample_deadlines_fn: Callable[..., str],
    notify: Callable[..., Any],
    telegram_push_fn: Callable[[str], Any],
    ts_fn: Callable[[], str],
) -> None:
    """掃樣品截止日，逾期催信 + push Telegram。"""
    try:
        result = check_sample_deadlines_fn(
            auto_draft_followup=True, push_telegram=False,
        )
    except Exception as e:
        print(f"[sample_check] 失敗：{e}")
        notify(
            subject="【小紅】樣品檢查失敗",
            body=f"錯誤：{e}\n\n{traceback.format_exc()}",
            task_name="sample_check",
        )
        return

    print(f"[sample_check] 檢查結果：\n{result}")

    if "✅ 所有樣品" in result:
        print("[sample_check] 無異常，安靜退出")
        return

    header = f"📦 每日樣品追蹤檢查 @ {ts_fn()[:16]}\n{'=' * 40}\n\n"
    try:
        telegram_push_fn((header + result)[:3900])
    except Exception as e:
        print(f"[sample_check] Telegram push 失敗：{e}")
