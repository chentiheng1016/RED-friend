"""macOS Vision OCR 共用入口 — 本機、免費、無 API、無 503。

原本 ocrmac 只在 drive_sync._extract_image_vision 一處；影片關鍵幀 gating
（video_understanding）也要用，抽成葉模組讓兩邊共用同一個咽喉點。
跟 env_utils 同款鐵則：這裡是唯一直接呼叫 ocrmac 的地方，別再各自 import。

- 非 macOS / 沒裝 ocrmac / 解圖失敗 / 無文字 → 回 None，呼叫端自行 fallback
  （Cloud Run / Linux 走這條自然回 None）。
- ocrmac 其實會回逐行信心分數（annotation = (text, confidence, bbox)），
  這裡把平均值帶回來 — 下游分級（skill_cards confirmed/likely/uncertain）
  要用，別再丟掉。
"""
from __future__ import annotations

import io
import os
from typing import Any

_DEFAULT_LANGS = ("zh-Hant", "zh-Hans", "en-US", "vi-VT")


def _langs() -> list[str]:
    raw = os.environ.get("RAG_VISION_OCR_LANGS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return list(_DEFAULT_LANGS)


def _import_deps():
    """lazy import（PIL, ocrmac）；任一缺 → (None, None)。"""
    try:
        from ocrmac import ocrmac
        from PIL import Image

        return Image, ocrmac
    except Exception:  # noqa: BLE001 — 非 macOS / 沒裝 → 呼叫端 fallback
        return None, None


def available() -> bool:
    """本機 Vision OCR 依賴（PIL + ocrmac）齊備。Cloud Run（Linux）/ 沒裝 → False。"""
    return _import_deps()[0] is not None


def ocr_text_boxes(source: bytes | str) -> list:
    """逐行文字＋外框：[(text, (x, y, w, h))]；bbox 為 Vision 正規化座標
    （0–1、原點在**左下**）。不可用/失敗/無文字回 []（呼叫端自行 fallback）。

    用途（2026-09-08 sample_order 點綴色抽取）：把完稿上的文字標註塗掉再抽色
    ——需要框不只要字，ocr_image 的聚合輸出不夠用。
    """
    Image, ocrmac = _import_deps()
    if Image is None or ocrmac is None:
        return []
    try:
        if isinstance(source, (bytes, bytearray)):
            img = Image.open(io.BytesIO(source))
        else:
            img = Image.open(source)
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        annotations = ocrmac.OCR(
            img,
            language_preference=list(_langs()),
            recognition_level="accurate",
        ).recognize()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in annotations or []:
        try:
            text = str(a[0] or "")
            x, y, w, h = (float(v) for v in a[2])
            if text:
                out.append((text, (x, y, w, h)))
        except Exception:  # noqa: BLE001, S112 — 單行格式歪掉就跳過
            continue
    return out


def ocr_image(source: bytes | str, *, languages: list[str] | None = None
              ) -> dict[str, Any] | None:
    """對一張圖做本機 Vision OCR。source 給 bytes 或圖片檔路徑。

    回 {"text": 全部辨識文字（空白連接）, "confidence": 平均信心 0–1 或 None}；
    不可用/失敗/無文字一律回 None。
    """
    Image, ocrmac = _import_deps()
    if Image is None or ocrmac is None:
        return None
    try:
        if isinstance(source, (bytes, bytearray)):
            img = Image.open(io.BytesIO(source))
        else:
            img = Image.open(source)
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        annotations = ocrmac.OCR(
            img,
            language_preference=list(languages or _langs()),
            recognition_level="accurate",
        ).recognize()
    except Exception:  # noqa: BLE001 — 解圖/OCR 失敗 → 呼叫端 fallback
        return None
    texts: list[str] = []
    confs: list[float] = []
    for a in annotations or []:
        if not a or not a[0]:
            continue
        texts.append(str(a[0]))
        try:
            confs.append(float(a[1]))
        except (TypeError, ValueError, IndexError):
            pass
    text = " ".join(texts).strip()
    if not text:
        return None
    return {
        "text": text,
        "confidence": round(sum(confs) / len(confs), 4) if confs else None,
    }
