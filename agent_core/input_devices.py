"""Keyboard + mouse automation (pyautogui wrapper).

Extracted from agent_core/system_ctl.py (phase 63). Four tools surfaced
to the Gemini tool list: type_text, press_keys, click_screen,
scroll_screen. type_text defaults to clipboard-paste mode so it bypasses
IME (注音/倉頡/拼音) garbling; press_keys has a hard-coded blocklist for
destructive hotkeys (cmd+shift+delete, alt+f4 etc.).

pyautogui is imported from system_ctl (single probe + FAILSAFE config
there). _IS_MAC also comes from system_ctl so Cmd vs Ctrl translation
is consistent with other platform code.
"""
import sys
import time

from agent_core.logging_and_paths import logger
from agent_core.system_ctl import _IS_MAC, pyautogui


_DANGEROUS_HOTKEYS_RAW = {
    # 登出 / 強制結束
    'cmd+shift+q', 'cmd+option+esc', 'alt+f4', 'ctrl+alt+delete',
    # 刪除 / 清垃圾桶
    'cmd+delete', 'shift+delete', 'cmd+shift+delete',
    'cmd+option+shift+delete',  # macOS 清空垃圾桶（無警告）
    # 關機 / 睡眠 / 重啟（macOS 電源鍵組合）
    'cmd+option+ctrl+eject', 'cmd+ctrl+eject', 'cmd+ctrl+power',
    'cmd+option+eject', 'cmd+option+power',
    # 系統登出
    'cmd+shift+option+q',
    # Windows 常見破壞性
    'ctrl+shift+delete',  # 清歷史
    'win+l',              # 鎖定
}


def _normalize_hotkey(seq: str) -> str:
    parts = [p.strip().lower() for p in seq.split('+') if p.strip()]
    if not parts:
        return ""
    synonyms = {
        'command': 'cmd', 'control': 'ctrl', 'option': 'alt',
        'esc': 'escape', 'del': 'delete', 'ret': 'enter', 'return': 'enter',
    }
    parts = [synonyms.get(p, p) for p in parts]
    modifiers = sorted(set(parts[:-1]))
    return '+'.join(modifiers + [parts[-1]])


_DANGEROUS_HOTKEYS = {_normalize_hotkey(k) for k in _DANGEROUS_HOTKEYS_RAW}


def _ensure_pyautogui():
    if pyautogui is None:
        return ("錯誤：pyautogui 未安裝。執行：pip install pyautogui\n"
                "macOS 使用者首次使用還需到「系統設定 → 隱私權與安全性 → 輔助使用」"
                "把 Terminal / Python 加入允許清單。")
    return ""


def type_text(text: str, interval: float = 0.02, force_keystroke: bool = False):
    """在目前游標位置輸入文字（預設走剪貼簿避開注音 IME 亂碼）。force_keystroke=True 改用逐字敲擊。"""
    err = _ensure_pyautogui()
    if err:
        return err
    if not text:
        return "錯誤：沒有文字可輸入。"
    if len(text) > 5000:
        return f"錯誤：一次輸入上限 5000 字元，收到 {len(text)} 字元。"

    print(f"\n[系統日誌] ⌨️ 輸入文字（{len(text)} 字元，{'鍵盤模擬' if force_keystroke else '剪貼簿貼上'}）...")

    if force_keystroke:
        has_non_ascii = any(ord(c) > 127 for c in text)
        if has_non_ascii:
            return "錯誤：force_keystroke 只支援純 ASCII；含中文請把 force_keystroke 設 False（預設）。"
        try:
            pyautogui.typewrite(text, interval=interval)
            return f"已輸入 {len(text)} 字元（鍵盤模擬模式）。"
        except Exception as e:
            return f"輸入失敗：{e}"

    try:
        import pyperclip
    except ImportError:
        pyperclip = None
    if pyperclip is None:
        return ("錯誤：貼上模式需要 pyperclip，請執行：pip install pyperclip。\n"
                "或可傳 force_keystroke=True 走鍵盤模擬（只支援 ASCII，且會被中文 IME 影響）。")
    original_clip = ""
    try:
        original_clip = pyperclip.paste() or ""
    except Exception:
        logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])
    try:
        pyperclip.copy(text)
        time.sleep(0.1)
        mod = 'command' if _IS_MAC else 'ctrl'
        pyautogui.hotkey(mod, 'v')
        time.sleep(0.2)
        return f"已輸入 {len(text)} 字元（剪貼簿貼上，繞過輸入法 IME）。"
    except Exception as e:
        return f"輸入失敗：{e}"
    finally:
        try:
            if original_clip:
                pyperclip.copy(original_clip)
        except Exception:
            logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])


def press_keys(keys: str):
    """按一組鍵盤組合鍵。keys 用 '+' 分隔，例如 'cmd+c'、'cmd+shift+t'、'enter'。危險組合會被擋下。"""
    err = _ensure_pyautogui()
    if err:
        return err
    keys = (keys or "").strip().lower()
    if not keys:
        return "錯誤：請指定要按的鍵。"

    print(f"\n[系統日誌] ⌨️ 按鍵：{keys}")
    sequences = [s.strip() for s in keys.split(',') if s.strip()]
    results = []
    for seq in sequences:
        norm = _normalize_hotkey(seq)
        if norm in _DANGEROUS_HOTKEYS:
            return f"🛑 安全防護：「{seq}」屬於毀滅性快捷鍵，已攔截。"
        parts = [p.strip().lower() for p in seq.split('+') if p.strip()]
        translated = []
        for p in parts:
            if p in ('cmd', 'command'):
                translated.append('command' if _IS_MAC else 'ctrl')
            elif p == 'option':
                translated.append('alt')
            elif p == 'esc':
                translated.append('escape')
            else:
                translated.append(p)

        try:
            if len(translated) == 1:
                pyautogui.press(translated[0])
            else:
                pyautogui.hotkey(*translated)
            results.append(seq)
        except Exception as e:
            return f"按鍵「{seq}」失敗：{e}"
    return f"已按下：{' → '.join(results)}"


def click_screen(x: int, y: int, button: str = "left", double: bool = False):
    """在螢幕像素座標 (x,y) 點一下。button: left/right/middle。double=True 是雙擊。⚠️ 座標變了就失靈，網頁用 browser_click 更穩。"""
    err = _ensure_pyautogui()
    if err:
        return err
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return "錯誤：x, y 必須是整數座標"

    try:
        max_x, max_y = pyautogui.size()
        if x < 0 or y < 0 or x > max_x or y > max_y:
            return (f"❌ 座標 ({x}, {y}) 超出螢幕範圍 {max_x}×{max_y}。\n"
                    f"很可能 Gemini 給的是物理像素座標（Retina 2x）。"
                    f"請重新呼叫 analyze_screen 再取得邏輯點座標。")
    except Exception:
        logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])

    print(f"\n[系統日誌] 🖱️ 點擊 ({x}, {y}) {button}{'×2' if double else ''}")
    try:
        pyautogui.moveTo(x, y, duration=0.1)
        time.sleep(0.08)
        if double:
            pyautogui.doubleClick(x, y, button=button)
        else:
            pyautogui.click(x, y, button=button)
        return f"已{'雙' if double else ''}點擊 ({x}, {y}) [{button} 鍵]"
    except Exception as e:
        return f"點擊失敗：{e}"


def scroll_screen(direction: str = "down", amount: int = 5):
    """捲動目前視窗畫面。direction: up/down/left/right；amount 是滾輪格數，5 約一小段。"""
    err = _ensure_pyautogui()
    if err:
        return err

    d = (direction or "").strip().lower()
    dir_map = {
        '上': 'up', 'up': 'up', '向上': 'up', '往上': 'up',
        '下': 'down', 'down': 'down', '向下': 'down', '往下': 'down',
        '左': 'left', 'left': 'left', '向左': 'left', '往左': 'left',
        '右': 'right', 'right': 'right', '向右': 'right', '往右': 'right',
    }
    d = dir_map.get(d, d)
    if d not in ('up', 'down', 'left', 'right'):
        return f"錯誤:方向必須是 up/down/left/right(或中文的上/下/左/右),收到「{direction}」。"

    try:
        a = max(1, min(50, int(amount)))
    except (TypeError, ValueError):
        a = 5

    print(f"\n[系統日誌] 🖱️ 滾動 {d} x{a}")
    try:
        clicks = a * 3
        if d == 'up':
            pyautogui.scroll(clicks)
        elif d == 'down':
            pyautogui.scroll(-clicks)
        elif d == 'right':
            if hasattr(pyautogui, 'hscroll'):
                pyautogui.hscroll(clicks)
            else:
                return "左右滾動此平台不支援(macOS 僅上下)。"
        elif d == 'left':
            if hasattr(pyautogui, 'hscroll'):
                pyautogui.hscroll(-clicks)
            else:
                return "左右滾動此平台不支援(macOS 僅上下)。"
        return f"已{direction}滾動 {a} 格。"
    except Exception as e:
        return f"滾動失敗:{e}"
