"""SOP 新舊衝突自動複核 — 對每處衝突抽影片幀、讓模型讀畫面裁決哪版正確。

流程（每支影片）：
1. 從 conflicts.json（對比 workflow 產出）撈該影片的 value_conflicts。
2. 錨點定位：在新版 SOP（collection 現行文字）拆 token 找衝突值字樣，往回取
   最近的【mm:ss】時間戳；找不到再用舊版快照；再不行全片均勻抽幀兜底。
3. 下載影片一次；每個錨點抽 8 幀（t-10s 起、60 秒窗、寬≤1920），同錨點共用幀組。
4. gemini-pro-latest + 圖片 HIGH 解析讀值裁決：A(舊)/B(新)/other(附實際值)/unclear。
5. 全部裁決存 out.json（可中斷續跑；no_anchor/error 會重試）。

只讀不改（迴流另有腳本）。用法：
  RED_CHROMA_HTTP_URL=… RED_EMBED_DIM=768 .venv/bin/python \\
    scripts/adjudicate_sop_conflicts.py <conflicts.json> <snapshot_dir> <out.json> [video_id ...]
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

RESULTS = ""  # 由 argv 提供（見檔頭用法）
SNAP = ""
OUT = ""

_TS = re.compile(r"[\[【](\d{1,2}):(\d{2})(?::(\d{2}))?[\]】]")


def _ts_to_s(m) -> int:
    a, b, c = m.groups()
    return (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))


_CODE_TOK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-\.]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]{3,}")


def _needle_tokens(*raws: str) -> list[str]:
    """把對比 agent 寫的複合值（「P4TF_560（出櫃申請單建立）」）拆成可比對
    token：整串＋代碼型 token＋中文連續段，長的優先（最專一）。"""
    seen: list[str] = []
    for raw in raws:
        raw = (raw or "").strip()
        if not raw:
            continue
        cands = [raw] + _CODE_TOK.findall(raw) + _CJK_RUN.findall(raw)
        for c in cands:
            c = c.strip()
            if len(c) >= 3 and c not in seen:
                seen.append(c)
    seen.sort(key=len, reverse=True)
    return seen


def find_anchor(texts: list[str], needles: list[str]) -> int | None:
    """在多份文字裡找任一 needle token 的首次出現，回其前方最近時間戳的秒數。"""
    tokens = _needle_tokens(*needles)
    for text in texts:
        if not text:
            continue
        stamps = [(m.start(), _ts_to_s(m)) for m in _TS.finditer(text)]
        if not stamps:
            continue
        for needle in tokens:
            pos = text.find(needle)
            if pos < 0:
                continue
            prev = [s for p, s in stamps if p <= pos]
            return prev[-1] if prev else stamps[0][1]
    return None


def extract_frames(video: str, t: int | None, out_dir: str,
                   duration: float) -> list[str]:
    """t 給了抽 60 秒窗（8 幀）；t=None（找不到錨點）全片均勻抽 8 幀兜底 —
    功能代碼/視窗標題這類值整片都看得到，均勻抽也有得讀。"""
    if t is None:
        key, args = "full", ["-i", video, "-vf",
                             f"fps=8/{max(duration, 8):.1f},scale='min(1920,iw)':-2"]
    else:
        start = max(0.0, min(float(t) - 10.0, max(0.0, duration - 60.0)))
        key = f"{int(start):05d}"
        args = ["-ss", f"{start:.1f}", "-i", video, "-t", "60",
                "-vf", "fps=8/60,scale='min(1920,iw)':-2"]
    prefix = f"adj_{key}_"
    existing = sorted(f for f in os.listdir(out_dir) if f.startswith(prefix))
    if existing:
        return [os.path.join(out_dir, f) for f in existing]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *args, "-q:v", "3",
         os.path.join(out_dir, prefix + "%02d.png")],
        capture_output=True, timeout=600,
    )
    got = sorted(f for f in os.listdir(out_dir) if f.startswith(prefix))
    return [os.path.join(out_dir, f) for f in got]


def adjudicate(frames: list[str], item: str, old: str, new: str, t: int):
    from google.genai import types

    from agent_core.gemini_client import _gemini_generate

    contents = []
    for f in frames:
        with open(f, "rb") as fh:
            contents.append(types.Part.from_bytes(data=fh.read(),
                                                  mime_type="image/png"))
    where = (f"在全片約 {t // 60:02d}:{t % 60:02d} 前後" if t is not None
             else "全片均勻")
    contents.append(
        f"這些是同一支 ERP 教學影片{where}抽的畫面。"
        f"兩份筆記對「{item}」記載不一致：\n"
        f"A（舊版筆記）：{old}\n"
        f"B（新版筆記）：{new}\n"
        "請逐張放大細讀畫面上的視窗標題、欄位、按鈕、狀態值，判斷哪個與畫面實際相符。"
        "畫面裡看不到相關內容就老實回 unclear，不要猜。只輸出 JSON："
        '{"verdict":"A|B|other|unclear","actual":"畫面實際顯示的值（other 時必填）",'
        '"evidence":"你在畫面哪裡看到（一句話）"}'
    )
    resp = _gemini_generate(
        model="gemini-pro-latest",  # 別釘具體版本 — 2.5-pro 曾在批次中途被下架
        contents=contents,
        config=types.GenerateContentConfig(
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            response_mime_type="application/json"),
        caller="training_videos.adjudicate",
    )
    txt = (resp.text or "").strip()
    try:
        obj = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        obj = json.loads(m.group(0)) if m else {}
    verdict = str(obj.get("verdict", "unclear")).strip()
    if verdict not in ("A", "B", "other", "unclear"):
        verdict = "unclear"
    return {"verdict": verdict, "actual": str(obj.get("actual", ""))[:120],
            "evidence": str(obj.get("evidence", ""))[:200]}


def _persist_out(out: list) -> None:
    """out.json 原子寫（tmp+rename）— 這是可中斷續跑的斷點檔，中途被砍
    （harness timeout / Ctrl-C）不能留半截 JSON，否則下次續跑直接炸。"""
    from agent_core.logging_and_paths import _atomic_write_text

    _atomic_write_text(OUT, json.dumps(out, ensure_ascii=False, indent=1))


def main(only_ids: set) -> None:
    from agent_core.erp import _download_drive_file
    from agent_core.ingest.vector_store import get_store
    from agent_core.video_understanding import (
        _drive_file_meta, _ffprobe_duration_seconds, _suffix_for_mime,
    )

    results = json.load(open(RESULTS))
    store = get_store("operation_sops")
    col = store._col or store._open_collection()

    out: list[dict] = []
    if os.path.exists(OUT):
        out = json.load(open(OUT))
    # no_anchor / error 的紀錄丟掉重跑（錨點邏輯已改進、error 多為暫時性）
    out = [o for o in out if o.get("verdict") not in ("no_anchor", "error")]
    done = {(o["vid"], o["item"]) for o in out}

    for r in results:
        vid, name = r["vid"], r["name"]
        if only_ids and vid not in only_ids:
            continue
        conflicts = [c for c in r["value_conflicts"]
                     if (vid, c["item"]) not in done]
        if not conflicts:
            continue
        data = col.get(where={"video_id": {"$eq": vid}}, include=["documents", "metadatas"])
        new_text = "\n".join(d for _, d in sorted(
            zip(data["metadatas"], data["documents"]),
            key=lambda p: int(p[0]["chunk_index"])))
        old_path = os.path.join(SNAP, f"{vid}.txt")
        old_text = open(old_path, encoding="utf-8").read() if os.path.exists(old_path) else ""

        tmp = tempfile.mkdtemp(prefix="adj_")
        video = None
        t0 = time.time()
        try:
            meta = _drive_file_meta(vid)
            video = os.path.join(tmp, "v" + _suffix_for_mime(meta.get("mimeType", "video/mp4")))
            if not _download_drive_file(vid, video):
                print(f"[adjud] ✗ {name}: 下載失敗", flush=True)
                continue
            duration = _ffprobe_duration_seconds(video) or 0.0
            if duration <= 0:
                # ffprobe 讀不到片長：全片兜底抽幀的 fps 公式會退化成
                # 1fps 全片（幾千張幀塞單一 request）— 記 unclear 跳過。
                for c in conflicts:
                    out.append({"vid": vid, "name": name, **c,
                                "anchor_s": None, "verdict": "unclear",
                                "actual": "",
                                "evidence": "ffprobe 讀不到片長，跳過抽幀"})
                _persist_out(out)
                print(f"[adjud] ✗ {name}: ffprobe 讀不到片長，"
                      f"{len(conflicts)} 處記 unclear", flush=True)
                continue
            n_ok = 0
            for c in conflicts:
                anchor = find_anchor([new_text, old_text],
                                     [c.get("new"), c.get("old"), c.get("item")])
                rec = {"vid": vid, "name": name, **c, "anchor_s": anchor}
                frames = extract_frames(video, anchor, tmp, duration)
                if not frames:
                    rec.update({"verdict": "unclear", "actual": "",
                                "evidence": "抽不到幀"})
                else:
                    try:
                        rec.update(adjudicate(frames, c["item"],
                                              c["old"], c["new"], anchor))
                        n_ok += 1
                    except Exception as exc:
                        rec.update({"verdict": "error",
                                    "actual": "",
                                    "evidence": f"{type(exc).__name__}: {exc}"[:150]})
                out.append(rec)
                _persist_out(out)
            print(f"[adjud] ✓ {name} {len(conflicts)} 處裁決"
                  f"（模型讀值 {n_ok}）{(time.time() - t0) / 60:.1f}min", flush=True)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    tally: dict[str, int] = {}
    for o in out:
        tally[o["verdict"]] = tally.get(o["verdict"], 0) + 1
    print(f"[adjud] DONE {len(out)} 處 → {OUT} | {tally}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    RESULTS, SNAP, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
    main(set(sys.argv[4:]))
