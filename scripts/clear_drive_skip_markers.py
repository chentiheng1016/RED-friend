"""一次性清除 drive_sync 的 skip marker — 按 reason 精確篩選及/或 file_id 白名單。

背景：skip marker 以 modifiedTime 為鍵，檔案沒重新上傳就永遠不會重抽。
當某類「永久失敗」後來有了解法（例：誤標 .xls 的 OOXML 檔，_extract_xls
已加 zip-magic fallback 改走 openpyxl），既有 marker 會擋住重抽，要用這支
把該 reason 的 marker 清掉，下一輪夜跑才會重試。

--ids-file 給 file_id 白名單（一行一個 id，# 開頭與空行忽略），適合「同
reason 只清其中一部分」的場景（例：empty_text 死 marker 清理只清已入庫
的那批、未入庫的留著）。與 --reason 並用時取交集。

預設 dry-run 只列出會清掉的檔；加 --apply 才真的寫回（原子寫）。
跑之前先確認 rag_sync 沒在跑（腳本直接改 state 檔，與 daemon 併寫會互蓋）。

用法：
  .venv/bin/python scripts/clear_drive_skip_markers.py \\
      --reason "extract_error: Excel xlsx file; not supported" [--apply]
  .venv/bin/python scripts/clear_drive_skip_markers.py \\
      --reason empty_text --ids-file /path/to/ids.txt [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text  # noqa: E402

SKIP_STATE_FILE = os.path.join(DATA_DIR, "drive_sync_skip_state.json")


def remove_markers_by_reason(
    state: dict, reasons: set[str] | None, ids: set[str] | None = None
) -> tuple[dict, list[tuple[str, dict]]]:
    """回傳 (清完的 state, 被移除的 [(file_id, marker), …])。不動原 dict。

    reasons 與 ids 至少要給一個（非空）；兩個都給時取交集——reason 對
    且 file_id 在白名單內才清。reasons=None 表示不按 reason 篩（只按 ids）。"""
    if not reasons and not ids:
        raise ValueError("reasons 與 ids 至少要給一個（非空）")
    files = state.get("files")
    if not isinstance(files, dict):
        return state, []
    removed = [
        (file_id, marker)
        for file_id, marker in files.items()
        if isinstance(marker, dict)
        and (reasons is None or marker.get("reason") in reasons)
        and (ids is None or file_id in ids)
    ]
    removed_ids = {file_id for file_id, _ in removed}
    cleaned = dict(state)
    cleaned["files"] = {
        file_id: marker for file_id, marker in files.items() if file_id not in removed_ids
    }
    return cleaned, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reason",
        action="append",
        help='要清除的 marker reason（可重複），例：'
        '"extract_error: Excel xlsx file; not supported"',
    )
    parser.add_argument(
        "--ids-file",
        help="file_id 白名單檔（一行一個 id，# 開頭與空行忽略）；"
        "與 --reason 並用時取交集",
    )
    parser.add_argument(
        "--state-file",
        default=SKIP_STATE_FILE,
        help=f"skip state 檔路徑（預設 {SKIP_STATE_FILE}）",
    )
    parser.add_argument("--apply", action="store_true", help="真的寫回（預設 dry-run）")
    args = parser.parse_args(argv)
    if not args.reason and not args.ids_file:
        parser.error("要給 --reason 或 --ids-file（可並用，並用時取交集）")

    ids: set[str] | None = None
    if args.ids_file:
        try:
            with open(args.ids_file, encoding="utf-8") as f:
                ids = {
                    line.strip()
                    for line in f
                    if line.strip() and not line.lstrip().startswith("#")
                }
        except FileNotFoundError:
            print(f"--ids-file 不存在：{args.ids_file}", file=sys.stderr)
            return 1
        if not ids:
            print(f"--ids-file 是空的：{args.ids_file}", file=sys.stderr)
            return 1

    try:
        with open(args.state_file, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        print(f"skip state 檔不存在：{args.state_file}", file=sys.stderr)
        return 1
    if not isinstance(state, dict):
        print(f"skip state 格式不對（非 dict）：{args.state_file}", file=sys.stderr)
        return 1

    cleaned, removed = remove_markers_by_reason(
        state, set(args.reason) if args.reason else None, ids
    )
    for file_id, marker in removed:
        print(f"{file_id}\t{marker.get('title', '')}\t{marker.get('reason', '')}")
    total = len(state.get("files", {})) if isinstance(state.get("files"), dict) else 0
    print(f"# 符合 {len(removed)}/{total} 檔", file=sys.stderr)

    if not removed:
        return 0
    if not args.apply:
        print("# dry-run，沒有寫回；加 --apply 才會清除", file=sys.stderr)
        return 0
    _atomic_write_text(args.state_file, json.dumps(cleaned, ensure_ascii=False, indent=2))
    print(f"# 已寫回 {args.state_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
