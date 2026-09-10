#!/usr/bin/env python
"""外箱嘜頭 OCR runner — PP-OCRv6 medium（onnxruntime 後端）、純本機、無 LLM。

跑在 **專用 venv**（var/venvs/box_ocr，見 bin/setup-box-ocr）裡，不是主 .venv：
paddlex 會降版 numpy、且其 opencv-contrib-python 與主 venv 的
opencv-python-headless 互踩。本檔只 import stdlib + paddleocr/PIL，別加 repo 內
import（專用 venv 沒有 repo 的依賴）。

協定（skills/warehouse_box_ocr.py 是唯一 caller）：
    <box_ocr_venv>/bin/python scripts/box_ocr_runner.py IMG1 [IMG2 ...]
stdout 最後一行為：
    RESULT_JSON:{"ok": true, "results": [{"file": ..., "rotation": deg,
        "lines": [{"text": str, "score": float, "box": [x0,y0,x1,y1]|null}, ...]
        | "error": str}, ...]}
（paddle/paddlex 會往 stdout 噴進度 log，所以用行首 marker 定位結果。）

旋轉投票：倉庫拍箱照常上下顛倒/橫拍（2026-07 實測：倒拍照片轉正 180° 後
圈內件數與膠帶字立刻從全滅變 0.95+），每張圖跑 0/90/180/270 四個方向、
取高信心行總分最高的方向。RED_BOX_OCR_ROTATIONS 可覆寫（例 "0" 關閉投票）。
det 參數刻意維持預設 — A/B 實測 limit_side_len 拉高反而讓真箱照漏檢。

一個 process 辨識多張：模型只載一次（載入 ~1s、每張×4 方向 ~2s）。模型檔
自動下載快取在 ~/.paddlex/official_models/（共 ~133MB，bin/setup-box-ocr 會
預熱，daemon 執行期不需要外網）。
"""
import json
import os
import sys
import tempfile

RESULT_MARKER = "RESULT_JSON:"
_ROTATIONS_ENV = "RED_BOX_OCR_ROTATIONS"
_VOTE_MIN_SCORE = 0.5


def _rotations() -> list:
    """要嘗試的旋轉角（度）。預設 0/90/180/270；env 覆寫、壞值靜默略過。"""
    raw = os.environ.get(_ROTATIONS_ENV, "0,90,180,270")
    degs = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece.lstrip("-").isdigit():
            deg = int(piece) % 360
            if deg not in degs:
                degs.append(deg)
    return degs or [0]


def _rotation_score(lines: list) -> float:
    """旋轉投票評分：高信心行分數總和。

    印刷字（膠帶/尺寸）也計分 — 方向對了印刷字才讀得順，是很強的方向訊號；
    後續欄位對映自會把印刷雜訊濾掉。
    """
    return sum(ln["score"] for ln in lines if ln["score"] >= _VOTE_MIN_SCORE)


def _lines_from_result(res) -> list:
    """把 paddleocr predict 單頁結果整成 [{text, score, box}]。"""
    j = res.json if isinstance(res.json, dict) else json.loads(res.json)
    r = j.get("res", j)
    texts = r.get("rec_texts") or []
    scores = r.get("rec_scores") or []
    polys = r.get("rec_polys") or r.get("dt_polys") or []
    lines = []
    for i, text in enumerate(texts):
        box = None
        # res.json 已轉純 list；空 poly 直接略過，避免 min() 對空序列炸掉該檔
        if i < len(polys) and polys[i]:
            xs = [float(p[0]) for p in polys[i]]
            ys = [float(p[1]) for p in polys[i]]
            box = [min(xs), min(ys), max(xs), max(ys)]
        lines.append({
            "text": str(text),
            "score": float(scores[i]) if i < len(scores) else 0.0,
            "box": box,
        })
    return lines


def _predict_lines(ocr, path: str) -> list:
    lines = []
    for res in ocr.predict(path):
        lines.extend(_lines_from_result(res))
    return lines


def _exif_orientation(path: str) -> int:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int((im.getexif() or {}).get(0x0112, 1) or 1)
    except Exception:
        return 1


def _candidate_image(path: str, deg: int, tmpdir: str) -> str:
    """產生指定旋轉角的候選圖檔路徑。

    0° 也要過 exif_transpose — 手機 JPEG 常帶 EXIF 方向標籤，cv2/paddle
    直讀原檔不會理它，等於漏掉「轉正後」的 0° 候選；無 EXIF 方向時 0°
    直接用原檔（省一次重編碼）。
    """
    if deg == 0 and _exif_orientation(path) == 1:
        return path
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if deg:
        im = im.rotate(deg, expand=True, fillcolor=(255, 255, 255))
    p = os.path.join(tmpdir, f"rot{deg}_{os.path.basename(path)}.jpg")
    im.save(p, quality=95)
    return p


def _ocr_with_rotation_vote(ocr, path: str, tmpdir: str) -> tuple:
    """對一張圖跑各旋轉方向，回 (best_lines, best_deg)。"""
    best_lines, best_deg, best_score = None, 0, -1.0
    for deg in _rotations():
        lines = _predict_lines(ocr, _candidate_image(path, deg, tmpdir))
        score = _rotation_score(lines)
        if score > best_score:
            best_lines, best_deg, best_score = lines, deg, score
    return best_lines or [], best_deg


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(RESULT_MARKER + json.dumps({"ok": False, "error": "no image paths"}))
        return 2
    try:
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(
            engine="onnxruntime",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except Exception as e:  # noqa: BLE001 — 引擎起不來要整包回報
        print(RESULT_MARKER + json.dumps(
            {"ok": False, "error": f"engine init failed: {type(e).__name__}: {e}"},
            ensure_ascii=False))
        return 1

    results = []
    with tempfile.TemporaryDirectory(prefix="box_ocr_rot_") as tmpdir:
        for path in paths:
            entry = {"file": path}
            try:
                lines, deg = _ocr_with_rotation_vote(ocr, path, tmpdir)
                entry["lines"] = lines
                entry["rotation"] = deg
            except Exception as e:  # noqa: BLE001 — 單張失敗不拖垮整批
                entry["error"] = f"{type(e).__name__}: {e}"
            results.append(entry)

    print(RESULT_MARKER + json.dumps({"ok": True, "results": results},
                                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
