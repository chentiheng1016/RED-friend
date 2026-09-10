"""Vision (Gemini multimodal) tools: image + screen analysis.

Split (phase 62) into three cohesive modules:
  - this module: analyze_image, analyze_screen + screenshot/focus helpers
  - agent_core.qc: QC photo-inspection (set_qc_master, qc_inspect, qc_batch_inspect)
  - agent_core.specs: spec-sheet parse/list/compare
"""
import os
import sys
import time
import tempfile
import mimetypes
import subprocess

from agent_core.file_ops import _clean_path
from agent_core.gemini_client import (
    GEMINI_MODEL,
    _get_genai_types,
    _gemini_generate,
    _prepare_image_for_gemini,
)
from agent_core.logging_and_paths import logger
from agent_core.system_ctl import _IS_MAC, _IS_WIN, _IS_LINUX, pyautogui
from agent_core.apps import _resolve_app_name

_MAX_IMAGE_SIZE_MB = 20
_SCREENSHOT_JPEG_QUALITY = 80

# analyze_uploaded_image 只收這些副檔名——文件（pdf/xlsx…）不在此工具射程，
# 也順便擋掉「猜大王上傳的文件檔名」的跨使用者讀取面（照片檔名是不可猜的
# file_unique_id，文件才保留原檔名）。
_UPLOAD_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic",
})


def analyze_image(image_path: str, prompt: str = "請詳細描述這張圖片的內容，並提取其中的所有文字。"):
    """用 Gemini Vision 分析一張本機圖片檔（PNG / JPG / WebP / TIFF / BMP 等）。prompt 指定要問什麼。"""
    clean_path = _clean_path(image_path)
    print(f"\n[系統日誌] 👁️ 正在解析圖片：{clean_path}...")
    try:
        if not os.path.exists(clean_path):
            return f"錯誤：找不到圖片檔案 {clean_path}"
        img_size_mb = os.path.getsize(clean_path) / (1024 * 1024)
        if img_size_mb > _MAX_IMAGE_SIZE_MB:
            return f"錯誤：圖片太大（{img_size_mb:.1f}MB），超過 {_MAX_IMAGE_SIZE_MB}MB 上限。"
        mime_type, _ = mimetypes.guess_type(clean_path)
        if not mime_type:
            mime_type = 'image/jpeg'
        with open(clean_path, 'rb') as f:
            image_data = f.read()
        # Gemini 拒收 tiff/bmp 等格式（400 Unsupported MIME）→ 先轉 JPEG。
        image_data, mime_type = _prepare_image_for_gemini(image_data, mime_type)
        response = _gemini_generate(
            model=GEMINI_MODEL,
            contents=[_get_genai_types().Part.from_bytes(data=image_data, mime_type=mime_type), prompt]
        )
        # Round 7: 圖片可由 attacker 控（email 附圖含 OCR-readable injection /
        # 內藏指令）。Vision 回傳的 text 流入 LLM context 前過 sanitize_for_llm。
        try:
            from agent_core.prompt_injection import sanitize_for_llm
            text = sanitize_for_llm(response.text or "")
        except Exception:
            text = response.text or ""
        return f"視覺解析結果：\n{text}"
    except Exception as e:
        return f"圖片解析失敗：{str(e)}"


def analyze_uploaded_image(image_path: str, question: str = "") -> str:
    """讀「Telegram 上傳」的圖片並回答問題（員工通道可用的受限看圖工具）。

    跟 analyze_image 差在路徑限縮：只接受 Telegram 上傳目錄
    （exchange_policy.telegram_upload_root()）底下的圖片檔——員工 freeform
    session 用它讀對話裡剛上傳的照片（上傳訊息「路徑:」欄的完整路徑），
    不能指向磁碟上其他檔案。

    起因 2026-07-29 UserA OZ18/OZ19 案：員工白名單沒有任何看圖工具，
    「依圖列出扣件總需求量」時 LLM 看不到圖，只能給公式＋腦補範例數量
    （真實款號 #8916447 配虛構的 10,000 雙；圖上明明是 12,000）。

    輸出以「逐字抄錄」為先：先把圖中表格/文字原樣抄出（含每一格數字），
    再答問題——使用者能對照原圖驗數字，判讀錯一眼可抓。
    """
    try:
        from agent_core.exchange_policy import telegram_upload_root
        root = os.path.realpath(telegram_upload_root())
    except Exception:
        return "錯誤：無法解析 Telegram 上傳目錄。"
    p = (image_path or "").strip().strip("'\"")
    if not p:
        return "錯誤：請帶上傳訊息「路徑:」欄的完整圖片路徑。"
    real = os.path.realpath(os.path.expanduser(p))
    if not real.startswith(root + os.sep):
        return (f"錯誤：此工具只能讀 Telegram 上傳目錄（{root}）裡的圖片。"
                "請用上傳訊息「路徑:」給的完整路徑。")
    ext = os.path.splitext(real)[1].lower()
    if ext not in _UPLOAD_IMAGE_EXTS:
        return (f"錯誤：「{ext or '（無副檔名）'}」不是支援的圖片格式"
                "（此工具只讀照片；文件請貼文字或請管理員處理）。")
    base = ("請先把圖片中的文字與表格**逐字逐格抄錄**（表格每一列完整列出、"
            "數字一字不差；看不清楚的格子標「?」，不得腦補或改寫圖中數字）。"
            "抄錄完再回答問題。")
    q = (question or "").strip()
    prompt = f"{base}\n\n問題：{q}" if q else base
    return analyze_image(real, prompt)


# ---------- Screen capture + focus helpers ----------

def _focus_app_window(app_name: str) -> str:
    app_name = _resolve_app_name(app_name)
    if not app_name.strip():
        return ""
    try:
        if _IS_MAC:
            return _mac_focus_app(app_name)
        if _IS_WIN:
            return _win_focus_app(app_name)
        if _IS_LINUX:
            return _linux_focus_app(app_name)
    except Exception as e:
        logger.debug("focus app 失敗：%s", e)
    return ""


def _mac_focus_app(app_name: str) -> str:
    safe = app_name.replace('"', '').replace('\\', '').replace("'", '').strip()
    script = f'''
    tell application "System Events"
        if not (exists process "{safe}") then return "NOT_RUNNING"
    end tell
    tell application "{safe}"
        activate
        try
            reopen
        end try
    end tell
    tell application "System Events"
        tell process "{safe}"
            set frontmost to true
            try
                repeat with w in windows
                    if value of attribute "AXMinimized" of w is true then
                        set value of attribute "AXMinimized" of w to false
                    end if
                end repeat
            end try
        end tell
    end tell
    delay 1.0
    return "FOCUSED"
    '''
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=8)
    return r.stdout.strip()


def _win_focus_app(app_name: str) -> str:
    try:
        import pygetwindow
    except ImportError:
        return ""
    try:
        wins = [w for w in pygetwindow.getAllWindows()
                if app_name.lower() in (w.title or "").lower()]
        if not wins:
            return "NOT_RUNNING"
        w = wins[0]
        if w.isMinimized:
            w.restore()
        w.activate()
        return "FOCUSED"
    except Exception:
        return ""


def _linux_focus_app(app_name: str) -> str:
    try:
        r = subprocess.run(['wmctrl', '-a', app_name], capture_output=True, timeout=3)
        return "FOCUSED" if r.returncode == 0 else "NOT_RUNNING"
    except FileNotFoundError:
        print("[focus] ⚠️ Linux 需安裝 wmctrl：sudo apt install wmctrl")
        return ""
    except Exception:
        return ""


def _get_logical_screen_size() -> tuple:
    try:
        if pyautogui is not None:
            w, h = pyautogui.size()
            return (int(w), int(h))
    except Exception:
        logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])
    return (0, 0)


def _take_screenshot(output_path: str) -> bool:
    try:
        import mss
        import mss.tools
    except ImportError:
        mss = None

    ok = False
    if mss is not None:
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                img = sct.grab(monitor)
                mss.tools.to_png(img.rgb, img.size, output=output_path)
            ok = True
        except Exception as e:
            logger.debug("mss 截圖失敗（%s），嘗試備援", e)

    if not ok:
        try:
            if _IS_MAC:
                r = subprocess.run(['screencapture', '-x', '-o', output_path], timeout=10)
                ok = (r.returncode == 0)
            elif _IS_WIN:
                try:
                    from PIL import ImageGrab
                    ImageGrab.grab().save(output_path)
                    ok = True
                except Exception as e:
                    logger.debug("PIL.ImageGrab 失敗：%s", e)
            elif _IS_LINUX:
                for cmd in (['scrot', output_path],
                            ['gnome-screenshot', '-f', output_path],
                            ['import', '-window', 'root', output_path]):
                    try:
                        r = subprocess.run(cmd, capture_output=True, timeout=10)
                        if r.returncode == 0:
                            ok = True
                            break
                    except FileNotFoundError:
                        continue
        except Exception as e:
            logger.debug("截圖備援全部失敗：%s", e)

    if not ok:
        return False

    try:
        from PIL import Image
        logical_w, logical_h = _get_logical_screen_size()
        img = Image.open(output_path)
        if logical_w > 0 and logical_h > 0 and (img.width != logical_w or img.height != logical_h):
            img = img.resize((logical_w, logical_h), Image.LANCZOS)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(output_path, format='JPEG', quality=_SCREENSHOT_JPEG_QUALITY, optimize=True)
        img.close()
    except Exception as e:
        logger.debug("截圖壓縮失敗: %s", e)
    return True


def analyze_screen(prompt: str = "請幫我分析這張螢幕截圖，告訴我畫面上的重點資訊。",
                   app_name: str = ""):
    """對目前螢幕截圖並用 Gemini Vision 分析。app_name 指定要先拉到最前的 App 名（例如 'Mail'）。"""
    temp_file = os.path.join(tempfile.gettempdir(), f"xiaohong_shot_{os.getpid()}.jpg")
    try:
        app_name = app_name.strip()
        if app_name:
            print(f"\n[系統日誌] 👁️ 正在把 {app_name} 拉到最前方並截圖分析...")
            focus_result = _focus_app_window(app_name)
            if focus_result == "NOT_RUNNING":
                return f"錯誤：{app_name} 目前沒有在執行。"
            time.sleep(1.0)
        else:
            print("\n[系統日誌] 👁️ 正在截取並分析螢幕畫面...")

        ok = _take_screenshot(temp_file)
        if not ok or not os.path.exists(temp_file):
            return "錯誤：螢幕截圖失敗。"
        with open(temp_file, 'rb') as f:
            image_data = f.read()

        logical_w, logical_h = _get_logical_screen_size()
        coord_hint = (
            f"【重要：螢幕座標系】這張截圖的尺寸是 {logical_w}×{logical_h}（邏輯點），"
            f"您報告的任何座標 (x, y) 可以【直接】傳給 click_screen(x, y) 工具點擊，"
            f"不需要做任何縮放或轉換。若要點某個元素，請使用 click_screen(x, y)。\n"
            if logical_w and logical_h else ""
        )
        full_prompt = coord_hint + prompt
        if app_name:
            full_prompt = coord_hint + f"（注意：畫面已切換到 {app_name} 的視窗最前方）\n{prompt}"

        response = _gemini_generate(
            model=GEMINI_MODEL,
            contents=[_get_genai_types().Part.from_bytes(data=image_data, mime_type='image/jpeg'), full_prompt]
        )
        # Round 7：螢幕中可能有攻擊者網頁 / 開著的可疑文件。流回 LLM 過 sanitize。
        try:
            from agent_core.prompt_injection import sanitize_for_llm
            text = sanitize_for_llm(response.text or "")
        except Exception:
            text = response.text or ""
        return f"螢幕畫面解析結果：\n{text}"
    except subprocess.TimeoutExpired:
        return "螢幕截圖逾時。"
    except Exception as e:
        return f"螢幕解析失敗：{str(e)}"
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)
