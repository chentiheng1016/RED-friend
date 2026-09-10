"""T4 視覺操作：OpenCV 模板匹配 + 本地 OCR（tesseract）。

補 Accessibility API 救不了的 App（Electron / Java Swing / 自家 ERP
常見的「畫面 iframe 裡面全部是畫上去的像素」）。

三種查法，按精準度排序：
  1. ax_click(app, title=...)       ← 首選，有 A11Y 就用（99% 穩）
  2. find_on_screen_by_text(text)   ← 沒 A11Y 時退這條（85% 穩）
  3. find_on_screen_by_image(tpl)   ← UI 改版就爛，但有時是唯一辦法（70% 穩）

OCR 語系支援：tesseract-lang 裝好後，小紅能讀中英日韓（chi_tra/chi_sim/eng/jpn/kor）。

全部 tool 回座標都是螢幕原生（含 Retina 實際像素）；搭配 `click_screen(x, y)`
即可完成 RPA 迴圈。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

from agent_core.logging_and_paths import logger

# OCR/CV 重套件延後載入：pytesseract 會連帶把 pandas 拉進來（實測 ~240ms），
# 而 fleet 啟動時沒有任何 daemon 需要 OCR。保留 module-level 名稱（None）讓
# 既有的 `if cv2 is None` 守衛仍成立，首次呼叫任一視覺函數時才真正 import。
cv2 = None
np = None
pytesseract = None
mss = None
_deps_loaded = False


def _ensure_deps() -> None:
    """首次用到才 import cv2 / numpy / pytesseract / mss（冪等、便宜）。

    `_deps_loaded` 在所有 import 之後才設 True：多執行緒首次併發呼叫最壞各跑一次
    import（sys.modules 已快取，無害），但都會正確設好 module-level 名稱。
    """
    global cv2, np, pytesseract, mss, _deps_loaded
    if _deps_loaded:
        return
    try:
        import cv2 as _cv2
        import numpy as _np
        cv2, np = _cv2, _np
    except ImportError:
        pass
    try:
        import pytesseract as _pt
        pytesseract = _pt
    except ImportError:
        pass
    try:
        import mss as _mss
        mss = _mss
    except ImportError:
        pass
    _deps_loaded = True


# ────────────────────────────────────────────────────────────────────
# 擷圖工具
# ────────────────────────────────────────────────────────────────────
def _capture_screen(region: dict | None = None):
    """擷取主螢幕或指定 region，回 numpy BGR ndarray。"""
    _ensure_deps()
    if mss is None:
        raise RuntimeError("mss 未裝：pip install mss")
    if np is None:
        raise RuntimeError("numpy / opencv 未裝")
    with mss.mss() as sct:
        monitor = region or sct.monitors[1]
        raw = sct.grab(monitor)
        img = np.array(raw)  # BGRA
        # 轉 BGR（OpenCV 格式）
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def _monitor_size() -> tuple[int, int]:
    """主螢幕（含 Retina 實際像素）尺寸。"""
    _ensure_deps()
    with mss.mss() as sct:
        m = sct.monitors[1]
        return m["width"], m["height"]


# ────────────────────────────────────────────────────────────────────
# OpenCV 模板匹配
# ────────────────────────────────────────────────────────────────────
def find_on_screen_by_image(template_path: str, threshold: float = 0.85,
                             region: str = "") -> str:
    """在螢幕上找一張範本圖（例如某個按鈕的截圖），回傳中心點座標。

    典型流程：
      1. 大王先截一小張按鈕圖存 `~/templates/save_btn.png`
      2. 小紅呼叫 find_on_screen_by_image("~/templates/save_btn.png")
      3. 拿到 (x, y) → click_screen(x, y)

    Args:
        template_path: 範本圖片路徑（PNG / JPG）。
        threshold: 匹配分數門檻 0~1；預設 0.85，太嚴可能找不到、太鬆會誤判。
        region: 可選，限定搜尋區域，格式 "x,y,width,height"（例如 "0,0,1920,500"）；
                空字串 = 全螢幕。
    Returns:
        找到 → "✅ 位置：(x=123, y=456) 信心 0.92"
        找不到 → "❌ 找不到" + 最佳信心分數
    """
    _ensure_deps()
    if cv2 is None:
        return "❌ OpenCV 未裝（pip install opencv-python-headless）"

    try:
        from agent_core.path_safety import safe_path
        tp = safe_path(template_path)
    except ValueError as e:
        return str(e)
    if not os.path.isfile(tp):
        return f"❌ 找不到範本：{tp}"

    try:
        template = cv2.imread(tp)
        if template is None:
            return f"❌ 讀範本失敗（檔案可能損毀）：{tp}"
    except Exception as e:
        return f"❌ 讀範本失敗：{e}"

    region_dict = None
    if region.strip():
        try:
            x, y, w, h = [int(v.strip()) for v in region.split(",")]
            region_dict = {"left": x, "top": y, "width": w, "height": h}
        except Exception:
            return f"❌ region 格式錯誤（要 'x,y,width,height'）：{region}"

    try:
        screen = _capture_screen(region_dict)
    except Exception as e:
        return f"❌ 擷螢幕失敗：{e}"

    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

    if max_val < threshold:
        return f"❌ 找不到（最佳信心 {max_val:.2f} < {threshold:.2f}）"

    th, tw = template.shape[:2]
    cx = max_loc[0] + tw // 2
    cy = max_loc[1] + th // 2

    if region_dict:
        cx += region_dict["left"]
        cy += region_dict["top"]

    # Retina 螢幕調整：mss 回實際像素，click_screen 用邏輯像素
    # 嘗試抓縮放比（cv2 matchTemplate 用實際像素；click_screen 要除 2）
    try:
        import Quartz
        scale = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width / _monitor_size()[0]
    except Exception:
        scale = 1.0

    logical_x = int(cx * scale)
    logical_y = int(cy * scale)

    return (f"✅ 找到範本，中心點座標：\n"
            f"  實際像素: ({cx}, {cy})\n"
            f"  邏輯像素 (給 click_screen 用): ({logical_x}, {logical_y})\n"
            f"  信心分數: {max_val:.3f}\n"
            f"  範本大小: {tw}x{th} px")


# ────────────────────────────────────────────────────────────────────
# tesseract OCR
# ────────────────────────────────────────────────────────────────────
def _default_lang() -> str:
    """多數台灣場景 = 中文繁體 + 英文。"""
    return "chi_tra+eng"


def ocr_image(image_path: str, lang: str = "") -> str:
    """對任何圖片檔跑 tesseract OCR，回純文字。

    Args:
        image_path: 圖片路徑（PNG / JPG / BMP / TIFF / PDF 的 page 圖）。
        lang: tesseract 語言代碼，例如：
              - "chi_tra+eng"（預設；繁中 + 英文）
              - "chi_sim+eng"（簡中 + 英文）
              - "jpn"（日文）
              - "kor"（韓文）
              - "eng"（純英）
              完整列表：`tesseract --list-langs`
    Returns:
        辨識出的純文字。
    """
    _ensure_deps()
    if pytesseract is None:
        return "❌ pytesseract 未裝（pip install pytesseract）"
    try:
        from agent_core.path_safety import safe_path
        p = safe_path(image_path)
    except ValueError as e:
        return str(e)
    if not os.path.isfile(p):
        return f"❌ 找不到圖片：{p}"
    try:
        text = pytesseract.image_to_string(p, lang=lang or _default_lang())
    except Exception as e:
        return f"❌ OCR 失敗：{type(e).__name__}: {e}"
    text = text.strip()
    if not text:
        return "⚠️ 辨識結果為空（圖片可能沒文字或太模糊）"
    # Round 8 M8-2：Tesseract OCR 結果是 attacker-controlled 文字（印刷品 / 螢幕
    # 內容含 `[INSTRUCTION]…[/INSTRUCTION]` 等 injection）。流入 LLM 前 sanitize。
    try:
        from agent_core.prompt_injection import sanitize_for_llm
        text = sanitize_for_llm(text)
    except Exception:
        pass
    return f"📝 OCR ({lang or _default_lang()})：\n{text}"


def ocr_screen_region(x: int, y: int, width: int, height: int, lang: str = "") -> str:
    """擷取螢幕指定區域並做 OCR（座標是邏輯像素）。

    Args:
        x, y: 左上角座標（邏輯像素）。
        width, height: 區域寬高。
        lang: tesseract 語言；空字串 = chi_tra+eng。
    """
    _ensure_deps()
    if mss is None:
        return "❌ mss 未裝"
    if pytesseract is None:
        return "❌ pytesseract 未裝"

    # Retina 縮放：mss 接實際像素
    try:
        import Quartz
        from Cocoa import NSScreen
        scale = NSScreen.mainScreen().backingScaleFactor()
    except Exception:
        scale = 1.0

    region = {
        "left": int(x * scale),
        "top": int(y * scale),
        "width": int(width * scale),
        "height": int(height * scale),
    }
    try:
        with mss.mss() as sct:
            raw = sct.grab(region)
            # 存到暫存 PNG（tesseract 最穩的 input 是檔案）
            import mss.tools as mss_tools
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                mss_tools.to_png(raw.rgb, raw.size, output=f.name)
                tmp_path = f.name
    except Exception as e:
        return f"❌ 擷區域失敗：{e}"

    try:
        text = pytesseract.image_to_string(tmp_path, lang=lang or _default_lang())
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    text = (text or "").strip()
    if not text:
        return f"⚠️ 區域 ({x},{y} {width}x{height}) OCR 結果為空"
    # Round 8 M8-2：螢幕區域 OCR 同樣可能含 attacker 網頁 injection 字。
    try:
        from agent_core.prompt_injection import sanitize_for_llm
        text = sanitize_for_llm(text)
    except Exception:
        pass
    return f"📝 區域 OCR：\n{text}"


def find_on_screen_by_text(text: str, lang: str = "",
                           region: str = "") -> str:
    """在螢幕上找指定文字，回傳位置（用 tesseract 做區域定位）。

    比 find_on_screen_by_image 更靈活（不用先截範本圖），但對小字 / 低對比
    準確度較差。UI 能找到用 ax_find_elements 最好。

    Args:
        text: 要找的文字（可以是部分字串）。
        lang: tesseract 語言；空字串 = chi_tra+eng。
        region: 可選，"x,y,width,height" 限定搜尋範圍；空 = 全螢幕。
    Returns:
        所有命中位置（邏輯像素座標，給 click_screen 用）。
    """
    _ensure_deps()
    if cv2 is None or pytesseract is None or mss is None:
        return "❌ 缺 opencv / pytesseract / mss（pip install 或 brew install tesseract）"

    if not text.strip():
        return "❌ text 不能空"

    region_dict = None
    if region.strip():
        try:
            rx, ry, rw, rh = [int(v.strip()) for v in region.split(",")]
            # 考慮 scale
            try:
                from Cocoa import NSScreen
                scale = NSScreen.mainScreen().backingScaleFactor()
            except Exception:
                scale = 1.0
            region_dict = {
                "left": int(rx * scale),
                "top": int(ry * scale),
                "width": int(rw * scale),
                "height": int(rh * scale),
            }
        except Exception:
            return f"❌ region 格式錯誤：{region}"

    try:
        with mss.mss() as sct:
            monitor = region_dict or sct.monitors[1]
            raw = sct.grab(monitor)
            import mss.tools as mss_tools
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                mss_tools.to_png(raw.rgb, raw.size, output=f.name)
                tmp_path = f.name
    except Exception as e:
        return f"❌ 擷螢幕失敗：{e}"

    try:
        data = pytesseract.image_to_data(
            tmp_path,
            lang=lang or _default_lang(),
            output_type=pytesseract.Output.DICT,
        )
    except Exception as e:
        return f"❌ OCR 失敗：{e}"
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    # 搜尋匹配的 word/block
    target = text.strip().lower()
    hits = []
    try:
        from Cocoa import NSScreen
        scale = NSScreen.mainScreen().backingScaleFactor()
    except Exception:
        scale = 1.0

    offset_x = region_dict["left"] if region_dict else 0
    offset_y = region_dict["top"] if region_dict else 0

    for i in range(len(data["text"])):
        word = (data["text"][i] or "").strip().lower()
        conf = int(data.get("conf", [])[i] or -1)
        if not word or conf < 40:
            continue
        if target in word or word in target:
            x = data["left"][i] + data["width"][i] // 2 + offset_x
            y = data["top"][i] + data["height"][i] // 2 + offset_y
            # 轉邏輯像素
            lx = int(x / scale)
            ly = int(y / scale)
            hits.append({
                "text": data["text"][i],
                "conf": conf,
                "logical_xy": (lx, ly),
                "actual_xy": (x, y),
            })

    if not hits:
        return f"🔍 螢幕上找不到 '{text}'（OCR lang={lang or _default_lang()}）"

    lines = [f"🔍 找到 {len(hits)} 處 '{text}'（邏輯像素座標，可直接給 click_screen）："]
    for i, h in enumerate(hits[:10], 1):
        lines.append(f"  {i}. ({h['logical_xy'][0]}, {h['logical_xy'][1]})  "
                     f"文字={h['text']!r}  信心={h['conf']}")
    if len(hits) > 10:
        lines.append(f"  ...（另有 {len(hits) - 10} 處）")
    return "\n".join(lines)


def list_ocr_languages() -> str:
    """列出系統已安裝的 tesseract 語言資料。"""
    try:
        out = subprocess.check_output(["tesseract", "--list-langs"], timeout=5,
                                       stderr=subprocess.STDOUT)
        out = out.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return "❌ tesseract 未安裝（brew install tesseract tesseract-lang）"
    except Exception as e:
        return f"❌ 列語言失敗：{e}"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    # 把常用的排前面
    preferred = ["chi_tra", "chi_sim", "eng", "jpn", "kor"]
    front = [line for line in lines if line in preferred]
    others = [line for line in lines if line not in preferred and "vert" not in line]
    total = len(lines)
    return (f"📚 tesseract 已裝語言（共 {total} 種）：\n"
            f"  常用：{', '.join(front)}\n"
            f"  其他：{', '.join(others[:30])}{'...' if len(others) > 30 else ''}")
