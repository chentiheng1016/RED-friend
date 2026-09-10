"""Local OS control: platform detection, volume, notifications, clipboard, AppleScript.

Phase 63 split the bigger original into three cohesive files:
  - this module: platform booleans, volume, notifications, AppleScript
    bridge, clipboard read, pyautogui probe (shared with input_devices
    + vision.py for pyautogui.size()).
  - agent_core.apps: open_application / close_application / open_url +
    app-name alias tables + installed-apps scanner
  - agent_core.input_devices: type_text / press_keys / click_screen /
    scroll_screen + hotkey blocklist

Keep platform booleans (_OS, _IS_*) and the pyautogui probe here —
they're the base everyone else imports from.
"""
import os
import re
import sys
import platform
import subprocess

from agent_core.logging_and_paths import logger

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
except ImportError:
    pyautogui = None

_OS = platform.system()
_IS_MAC = _OS == "Darwin"
_IS_WIN = _OS == "Windows"
_IS_LINUX = _OS == "Linux"

DANGEROUS_APPLESCRIPT_KEYWORDS = [
    'delete', 'remove', 'erase', 'rm ', 'trash', 'move to trash',
    'shutdown', 'restart', 'sleep computer', 'log out',
    'do shell script', 'run script', 'sudo', 'kill ',
    'keystroke', 'key code', 'key down',
    'format disk', 'eject', 'mount volume',
    '/system', '/library/launchdaemons', '/etc/',
    'quit application', 'force quit',
]

# Windows pycaw availability (audio control via COM)
_pycaw = None
if _IS_WIN:
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # noqa: F401
        import comtypes  # noqa: F401
        _pycaw = True
    except ImportError:
        _pycaw = False


# ---------- System volume ----------

def _win_get_volume_interface():
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, 7, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def _get_system_volume() -> int:
    try:
        if _IS_MAC:
            r = subprocess.run(
                ['osascript', '-e', 'output volume of (get volume settings)'],
                capture_output=True, text=True, timeout=3
            )
            return int(r.stdout.strip())
        if _IS_WIN and _pycaw:
            return int(round(_win_get_volume_interface().GetMasterVolumeLevelScalar() * 100))
        if _IS_LINUX:
            try:
                r = subprocess.run(['pactl', 'get-sink-volume', '@DEFAULT_SINK@'],
                                   capture_output=True, text=True, timeout=3)
                m = re.search(r'(\d+)%', r.stdout)
                if m:
                    return int(m.group(1))
            except FileNotFoundError:
                pass
            r = subprocess.run(['amixer', 'get', 'Master'],
                               capture_output=True, text=True, timeout=3)
            m = re.search(r'\[(\d+)%\]', r.stdout)
            if m:
                return int(m.group(1))
    except Exception:
        logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])
    return -1


def _set_system_volume(vol: int):
    vol = max(0, min(100, int(vol)))
    try:
        if _IS_MAC:
            subprocess.run(
                ['osascript', '-e', f'set volume output volume {vol}'],
                capture_output=True, timeout=3
            )
            return
        if _IS_WIN and _pycaw:
            _win_get_volume_interface().SetMasterVolumeLevelScalar(vol / 100.0, None)
            return
        if _IS_LINUX:
            try:
                subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{vol}%'],
                               capture_output=True, timeout=3)
                return
            except FileNotFoundError:
                pass
            subprocess.run(['amixer', '-D', 'pulse', 'sset', 'Master', f'{vol}%'],
                           capture_output=True, timeout=3)
    except Exception:
        logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])


def set_system_volume(volume_percent: int):
    """設定系統音量 0-100。跨平台（Mac 用 osascript / Win 用 pycaw / Linux 用 pactl/amixer）。"""
    try:
        v = int(volume_percent)
    except (TypeError, ValueError):
        return f"錯誤：音量必須是整數 0~100，收到「{volume_percent}」。"
    v = max(0, min(100, v))
    before = _get_system_volume()
    _set_system_volume(v)
    after = _get_system_volume()
    if after < 0:
        return f"音量已嘗試設為 {v}%，但目前平台無法回讀音量確認（{_OS}）。"
    return f"音量已設為 {after}%（原本 {before}%，目標 {v}%）。"


# ---------- OS notifications ----------

def show_notification(title: str, body: str = ""):
    """在作業系統通知中心顯示一條訊息。title 必填；body 副標題可留空。"""
    title = (title or "小紅").strip()
    body = (body or "").strip()
    try:
        if _IS_MAC:
            def _esc_as(s):
                return s.replace('\\', '\\\\').replace('"', '\\"')
            script = f'display notification f"{_esc_as(body)}" with title f"{_esc_as(title)}"'
            subprocess.run(['osascript', '-e', script],
                           capture_output=True, timeout=5)
            return f"已顯示 macOS 通知：{title} — {body}"
        if _IS_WIN:
            def _esc_ps(s):
                return s.replace("'", "''")
            ps = (
                f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                f"$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode(f'{_esc_ps(title)}')) > $null;"
                f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode(f'{_esc_ps(body)}')) > $null;"
                f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('XiaoHong').Show([Windows.UI.Notifications.ToastNotification]::new($t));"
            )
            subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                           capture_output=True, timeout=5)
            return f"已顯示 Windows 通知：{title} — {body}"
        if _IS_LINUX:
            try:
                subprocess.run(['notify-send', title, body],
                               capture_output=True, timeout=5)
                return f"已顯示 Linux 通知：{title} — {body}"
            except FileNotFoundError:
                return "⚠️ 請安裝 libnotify：sudo apt install libnotify-bin"
        return f"未支援的平台：{_OS}"
    except Exception as e:
        return f"通知顯示失敗：{e}"


# ---------- macOS AppleScript + clipboard ----------

def control_mac_system(applescript_code: str):
    """macOS-only：執行任意 AppleScript。用來做系統深度操控（暗色模式、彈 dock 等）。Windows/Linux 會回拒絕。"""
    if not _IS_MAC:
        return (f"⚠️ 這個工具僅 macOS 可用，目前平台是 {_OS}。"
                f"請改用跨平台工具：set_system_volume / show_notification / open_application 等。")
    print("\n[系統日誌] 💻 執行 Mac 系統控制指令...")
    lowered = applescript_code.lower()
    for kw in DANGEROUS_APPLESCRIPT_KEYWORDS:
        needle = kw.strip().lower()
        if not needle:
            continue
        if ' ' in needle or '/' in needle:
            if needle in lowered:
                return f"🛑 安全防護：偵測到潛在危險指令「{needle}」，已攔截。"
            continue
        if re.search(r'(?<![a-z0-9])' + re.escape(needle) + r'(?![a-z0-9])', lowered):
            return f"🛑 安全防護：偵測到潛在危險指令「{needle}」，已攔截。"
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return f"Mac 控制執行失敗：{result.stderr.strip()}"
        return f"Mac 控制執行成功。{('系統回傳：' + result.stdout.strip()) if result.stdout.strip() else ''}"
    except subprocess.TimeoutExpired:
        return "Mac 控制執行逾時。"
    except Exception as e:
        return f"發生未知錯誤：{str(e)}"


def read_mac_clipboard():
    """讀取目前系統剪貼簿內容（純文字）。大王說「看剪貼簿」/「幫我總結剛複製的」時用。"""
    try:
        import pyperclip
    except ImportError:
        pyperclip = None
    print("\n[系統日誌] 📋 讀取剪貼簿中...")
    try:
        if pyperclip is not None:
            return pyperclip.paste() or ""
        if _IS_MAC:
            env = {**os.environ, 'LANG': 'en_US.UTF-8'}
            return subprocess.check_output('pbpaste', env=env).decode('utf-8')
        if _IS_WIN:
            return subprocess.check_output(
                ['powershell', '-NoProfile', '-Command', 'Get-Clipboard'],
                text=True, timeout=5
            )
        if _IS_LINUX:
            try:
                return subprocess.check_output(['xclip', '-selection', 'clipboard', '-o'],
                                               text=True, timeout=5)
            except FileNotFoundError:
                return subprocess.check_output(['wl-paste'], text=True, timeout=5)
        return ""
    except Exception as e:
        return f"讀取剪貼簿失敗：{e}（建議 pip install pyperclip 以跨平台支援）"
