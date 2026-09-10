"""教學影片重學 CLI — 手動跑單支/多支/全部（watcher 的人工版）。

用法（在 repo 根、帶 fleet env）：
  RED_CHROMA_HTTP_URL=http://127.0.0.1:8000 RED_EMBED_DIM=768 \\
  RED_VIDEO_LOCAL_ASR=1 RED_VIDEO_LOCAL_OCR_GATE=1 \\
  .venv/bin/python scripts/relearn_training_videos.py <video_id> [...]
  .venv/bin/python scripts/relearn_training_videos.py --all   # operation_sops 既有全部

注意：deep 學習走付費 gemini-2.5-pro（大檔 ~$1.5/支、20+ 分鐘），--all 一輪
13 支約 $8-13、3-4 小時 — 建議 nohup … & disown 跑。
"""
import json
import sys


def _all_known() -> list[tuple[str, str, str]]:
    from agent_core.ingest.vector_store import get_store

    store = get_store("operation_sops")
    col = store._col or store._open_collection()
    data = col.get(include=["metadatas"])
    seen: dict[str, tuple[str, str, str]] = {}
    for m in data.get("metadatas") or []:
        vid = str(m.get("video_id") or "")
        if vid and vid not in seen:
            seen[vid] = (vid, str(m.get("video_name") or vid),
                         str(m.get("department") or ""))
    return list(seen.values())


def main(argv: list[str]) -> int:
    from agent_core.training_videos import relearn_video

    if not argv:
        print(__doc__)
        return 2
    if argv == ["--all"]:
        todo = _all_known()
    else:
        todo = [(vid, "", "") for vid in argv]
    print(f"[relearn] {len(todo)} 支待跑", flush=True)
    failures = 0
    for vid, name, dept in todo:
        r = relearn_video(vid, name, dept)
        mark = "✓" if r.get("ok") else "✗"
        print(f"[relearn] {mark} {json.dumps(r, ensure_ascii=False)}", flush=True)
        failures += 0 if r.get("ok") else 1
    print(f"[relearn] DONE {len(todo) - failures}/{len(todo)}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
