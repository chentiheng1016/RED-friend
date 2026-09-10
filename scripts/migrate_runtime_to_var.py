#!/usr/bin/env python3
"""Migrate legacy runtime files/directories from repo root into var/.

Safe defaults:
  - dry-run unless --apply is passed
  - skips destinations that already exist
  - records a manifest under var/migrations/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_ROOT = os.path.join(REPO_ROOT, "var")


@dataclass(frozen=True)
class MoveItem:
    src_rel: str
    dst_rel: str
    kind: str


MOVE_ITEMS = [
    MoveItem("memory.json", "state/memory.json", "file"),
    MoveItem("mistakes.json", "state/mistakes.json", "file"),
    MoveItem("daemon_state.json", "state/daemon_state.json", "file"),
    MoveItem("daemon_state.json.bak", "state/daemon_state.json.bak", "file"),
    MoveItem("daemon_state.json.lock", "state/daemon_state.json.lock", "file"),
    MoveItem("hotword_state.json", "state/hotword_state.json", "file"),
    MoveItem("email_classifications.json", "state/email_classifications.json", "file"),
    MoveItem("vault_access.log", "state/vault_access.log", "file"),
    MoveItem("voiceprints.npz", "state/voiceprints.npz", "file"),
    MoveItem("token.json", "state/google/token.json", "file"),
    MoveItem("credentials.json", "state/google/credentials.json", "file"),
    MoveItem("logs", "logs", "dir"),
    MoveItem("chroma_db", "data/chroma_db", "dir"),
    MoveItem("data_lake", "data/data_lake", "dir"),
    MoveItem("data_lake_internal", "data/data_lake_internal", "dir"),
    MoveItem("quote_history", "data/quote_history", "dir"),
    MoveItem("generated_images", "data/generated_images", "dir"),
    MoveItem("interpret_sessions", "data/interpret_sessions", "dir"),
    MoveItem("runs", "runs", "dir"),
    MoveItem("workflows", "workflows", "dir"),
]


def _abs_repo(rel_path: str) -> str:
    return os.path.join(REPO_ROOT, rel_path)


def _abs_runtime(rel_path: str) -> str:
    return os.path.join(RUNTIME_ROOT, rel_path)


def _describe_size(path: str) -> str:
    if not os.path.exists(path):
        return "-"
    if os.path.isfile(path):
        return f"{os.path.getsize(path)} B"
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    return f"{total} B"


def _plan():
    rows = []
    for item in MOVE_ITEMS:
        src = _abs_repo(item.src_rel)
        dst = _abs_runtime(item.dst_rel)
        rows.append(
            {
                "src_rel": item.src_rel,
                "dst_rel": item.dst_rel,
                "src": src,
                "dst": dst,
                "kind": item.kind,
                "exists": os.path.exists(src),
                "dest_exists": os.path.exists(dst),
                "size": _describe_size(src),
            }
        )
    return rows


def _ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _move(row: dict) -> str:
    src = row["src"]
    dst = row["dst"]
    if not row["exists"]:
        return "missing"
    if row["dest_exists"]:
        return "skipped_dest_exists"
    _ensure_parent(dst)
    shutil.move(src, dst)
    return "moved"


def _write_manifest(rows: list[dict], mode: str) -> str:
    manifest_dir = _abs_runtime("migrations")
    os.makedirs(manifest_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(manifest_dir, f"{ts}_{mode}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "repo_root": REPO_ROOT,
                "runtime_root": RUNTIME_ROOT,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "mode": mode,
                "rows": rows,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate RED runtime files into var/")
    parser.add_argument("--apply", action="store_true", help="perform the moves")
    args = parser.parse_args()

    rows = _plan()
    printable = []
    for row in rows:
        status = "would-move"
        if not row["exists"]:
            status = "missing"
        elif row["dest_exists"]:
            status = "skipped-dest-exists"
        printable.append(f"{status:20s} {row['src_rel']:24s} -> var/{row['dst_rel']} ({row['size']})")

    print("\n".join(printable))

    if not args.apply:
        manifest = _write_manifest(rows, "dry_run")
        print(f"\nDry run only. Manifest: {manifest}")
        return 0

    results = []
    for row in rows:
        result = _move(row)
        row = dict(row)
        row["result"] = result
        results.append(row)

    manifest = _write_manifest(results, "apply")
    moved = sum(1 for row in results if row["result"] == "moved")
    skipped = sum(1 for row in results if row["result"] == "skipped_dest_exists")
    missing = sum(1 for row in results if row["result"] == "missing")
    print(
        f"\nApplied. moved={moved} skipped={skipped} missing={missing}\n"
        f"Manifest: {manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
