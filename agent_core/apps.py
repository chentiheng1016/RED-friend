"""App launcher + URL opener.

Extracted from agent_core/system_ctl.py (phase 63). Owns:
  - App-name alias tables (English ↔ Chinese ↔ actual launcher path)
    for all three platforms, and `_resolve_app_name` which normalises
    user speech into the canonical launcher name.
  - `open_application` / `close_application` with fuzzy "did you mean"
    fallback via the platform's installed-apps scanner.
  - `open_url` — a browser-launching convenience that routes through
    the platform default browser (distinct from agent_core.browser
    which drives a Chromium via Playwright).

Platform booleans (_IS_MAC etc.) live in system_ctl; we import from
there rather than re-probe.
"""
import os
import re
import subprocess

from agent_core.logging_and_paths import logger
from agent_core.mistake_ledger import _log_mistake
from agent_core.system_ctl import _OS, _IS_MAC, _IS_WIN, _IS_LINUX


_APP_ALIASES_MAC = {
    "apple music": "Music", "蘋果音樂": "Music", "音樂": "Music", "itunes": "Music",
    "podcast": "Podcasts", "播客": "Podcasts", "tv": "TV", "apple tv": "TV", "電視": "TV",
    "備忘錄": "Notes", "notes": "Notes", "行事曆": "Calendar", "日曆": "Calendar", "calendar": "Calendar",
    "郵件": "Mail", "mail": "Mail", "訊息": "Messages", "imessage": "Messages", "messages": "Messages",
    "提醒事項": "Reminders", "reminders": "Reminders", "通訊錄": "Contacts", "聯絡人": "Contacts", "contacts": "Contacts",
    "系統偏好設定": "System Settings", "系統設定": "System Settings", "system preferences": "System Settings",
    "preferences": "System Settings", "設定": "System Settings", "終端機": "Terminal", "terminal": "Terminal",
    "活動監視器": "Activity Monitor", "磁碟工具程式": "Disk Utility", "字體簿": "Font Book",
    "地圖": "Maps", "maps": "Maps", "照片": "Photos", "photos": "Photos", "預覽程式": "Preview", "preview": "Preview",
    "計算機": "Calculator", "時鐘": "Clock", "天氣": "Weather", "股市": "Stocks", "書籍": "Books", "ibooks": "Books",
    "尋找": "Find My", "find my": "Find My", "捷徑": "Shortcuts", "shortcuts": "Shortcuts", "語音備忘錄": "Voice Memos",
    "螢幕共享": "Screen Sharing", "無邊記": "Freeform",
    "瀏覽器": "Safari", "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "vs code": "Visual Studio Code", "vscode": "Visual Studio Code",
}

_APP_ALIASES_WIN = {
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge", "firefox": "firefox",
    "記事本": "notepad", "notepad": "notepad", "小畫家": "mspaint", "paint": "mspaint",
    "計算機": "calc", "calculator": "calc", "檔案總管": "explorer", "explorer": "explorer",
    "終端機": "wt", "命令提示字元": "cmd", "cmd": "cmd", "powershell": "powershell",
    "設定": "ms-settings:", "系統設定": "ms-settings:",
    "音樂": "mswindowsmusic:", "媒體播放器": "mswindowsmediaplayer:", "相片": "ms-photos:", "photos": "ms-photos:",
    "郵件": "outlookmail:", "行事曆": "outlookcal:", "vs code": "code", "vscode": "code",
}

_APP_ALIASES_LINUX = {
    "chrome": "google-chrome", "google chrome": "google-chrome", "firefox": "firefox", "edge": "microsoft-edge",
    "檔案總管": "nautilus", "文字編輯器": "gedit", "計算機": "gnome-calculator", "calculator": "gnome-calculator",
    "終端機": "gnome-terminal", "terminal": "gnome-terminal", "系統設定": "gnome-control-center",
    "音樂": "rhythmbox", "影片": "totem", "vs code": "code", "vscode": "code",
}

if _IS_MAC:
    _APP_ALIASES = _APP_ALIASES_MAC
elif _IS_WIN:
    _APP_ALIASES = _APP_ALIASES_WIN
elif _IS_LINUX:
    _APP_ALIASES = _APP_ALIASES_LINUX
else:
    _APP_ALIASES = {}


def _resolve_app_name(name: str) -> str:
    if not name:
        return name
    key = name.strip().lower()
    resolved = _APP_ALIASES.get(key)
    if resolved and resolved != name.strip():
        print(f"[App 別名] 🏷️ 「{name.strip()}」→「{resolved}」(平台：{_OS})")
        return resolved
    return name.strip()


def open_url(url: str, browser: str = ""):
    """在大王系統預設瀏覽器（Safari/Chrome 等）開一個網址。browser 可指定特定瀏覽器名。
    ⚠️ 只是讓大王自己看；要能讀內容/填表請用 browser_open 系列工具。"""
    url = (url or "").strip()
    if not url:
        return "錯誤：網址不可為空。"
    if not re.match(r'^https?://', url, re.IGNORECASE):
        if '.' in url and ' ' not in url:
            url = 'https://' + url
        else:
            return f"錯誤：「{url}」看起來不是網址。"
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return f"錯誤：只允許 http(s) 網址，拒絕「{url[:30]}」。"

    browser = (browser or "").strip()
    print(f"\n[系統日誌] 🌐 打開網址：{url}" + (f"（{browser}）" if browser else ""))
    try:
        if _IS_MAC:
            if browser:
                r = subprocess.run(['open', '-a', browser, url],
                                   capture_output=True, text=True, timeout=8)
            else:
                r = subprocess.run(['open', url],
                                   capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                return f"已在{browser or '預設瀏覽器'}打開：{url}"
            return f"開啟失敗：{r.stderr.strip() or '未知錯誤'}"
        if _IS_WIN:
            if browser:
                r = subprocess.run(['cmd', '/c', 'start', '', browser, url],
                                   capture_output=True, text=True, timeout=8)
            else:
                r = subprocess.run(['cmd', '/c', 'start', '', url],
                                   capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                return f"已打開：{url}"
            os.startfile(url)
            return f"已打開：{url}"
        if _IS_LINUX:
            try:
                subprocess.Popen(['xdg-open', url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return f"已打開：{url}"
            except FileNotFoundError:
                return "⚠️ 找不到 xdg-open，請安裝：sudo apt install xdg-utils"
        return f"未支援的平台：{_OS}"
    except subprocess.TimeoutExpired:
        return f"開啟逾時：{url}"
    except Exception as e:
        return f"開啟失敗：{e}"


def _find_installed_apps_matching(query: str, limit: int = 5) -> list:
    if _IS_MAC:
        return _mac_find_apps(query, limit)
    if _IS_WIN:
        return _win_find_apps(query, limit)
    if _IS_LINUX:
        return _linux_find_apps(query, limit)
    return []


def _mac_find_apps(query: str, limit: int) -> list:
    candidates = []
    query_low = query.lower()
    for base in ('/Applications', '/System/Applications', os.path.expanduser('~/Applications')):
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                if not name.endswith('.app'):
                    continue
                app_display = name[:-4]
                if query_low in app_display.lower() or app_display.lower().startswith(query_low[:2]):
                    candidates.append(app_display)
        except Exception:
            continue
    return _dedupe_limit(candidates, limit)


def _win_find_apps(query: str, limit: int) -> list:
    candidates = []
    query_low = query.lower()
    roots = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _, files in os.walk(root):
                for f in files:
                    if not f.lower().endswith('.lnk'):
                        continue
                    name = f[:-4]
                    if query_low in name.lower() or name.lower().startswith(query_low[:2]):
                        candidates.append(name)
        except Exception:
            continue
    return _dedupe_limit(candidates, limit)


def _linux_find_apps(query: str, limit: int) -> list:
    candidates = []
    query_low = query.lower()
    roots = [
        '/usr/share/applications',
        '/usr/local/share/applications',
        os.path.expanduser('~/.local/share/applications'),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for f in os.listdir(root):
                if not f.endswith('.desktop'):
                    continue
                name = f[:-8]
                if query_low in name.lower() or name.lower().startswith(query_low[:2]):
                    candidates.append(name)
        except Exception:
            continue
    return _dedupe_limit(candidates, limit)


def _dedupe_limit(items: list, limit: int) -> list:
    seen = set()
    uniq = []
    for c in items:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
        if len(uniq) >= limit:
            break
    return uniq


def _mac_open_app(safe_name: str) -> tuple:
    script = f'''
    tell application "{safe_name}"
        activate
        if (count of windows) is 0 then
            reopen
            activate
        end if
    end tell
    '''
    r = subprocess.run(['osascript', '-e', script],
                       capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        return (True, "")
    r2 = subprocess.run(['open', '-a', safe_name],
                        capture_output=True, text=True, timeout=5)
    if r2.returncode == 0:
        return (True, "")
    return (False, r2.stderr.strip() or r.stderr.strip())


def _win_open_app(safe_name: str) -> tuple:
    try:
        if safe_name.endswith(':') or ':/' in safe_name:
            os.startfile(safe_name)
            return (True, "")
        r = subprocess.run(['cmd', '/c', 'start', '', safe_name],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return (True, "")
        os.startfile(safe_name)
        return (True, "")
    except Exception as e:
        return (False, str(e))


def _linux_open_app(safe_name: str) -> tuple:
    try:
        subprocess.Popen([safe_name], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return (True, "")
    except FileNotFoundError:
        pass
    except Exception as e:
        return (False, str(e))
    try:
        r = subprocess.run(['gtk-launch', safe_name.replace('.desktop', '')],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return (True, "")
        return (False, r.stderr.strip())
    except FileNotFoundError:
        return (False, "gtk-launch / 執行檔皆找不到")


def open_application(app_name: str):
    """開啟本機 App（跨平台 Mac/Win/Linux）。app_name 用顯示名，例如 'Safari'、'Mail'、'Spotify'。"""
    print(f"\n[系統日誌] 🚀 開啟應用程式：{app_name}（平台：{_OS}）...")
    app_name = _resolve_app_name(app_name)
    safe_name = app_name.replace('"', '').replace('\\', '').replace("'", '').strip()
    if not safe_name:
        return "錯誤：應用程式名稱無效。"

    def _try_open() -> tuple:
        try:
            if _IS_MAC:
                return _mac_open_app(safe_name)
            if _IS_WIN:
                return _win_open_app(safe_name)
            if _IS_LINUX:
                return _linux_open_app(safe_name)
            return (False, f"未支援的平台：{_OS}")
        except subprocess.TimeoutExpired:
            return (False, "timeout")
        except Exception as e:
            return (False, str(e))

    try:
        success, err = _try_open()
        if success:
            return f"已開啟 {safe_name}"

        candidates = _find_installed_apps_matching(safe_name)
        hint = ""
        if candidates:
            hint = f" 您是不是想開：{' / '.join(candidates)}？"
        _log_mistake(
            mistake_type="app_not_found",
            user_said=safe_name,
            detail=f"系統找不到 App「{safe_name}」({err})",
            resolution=(candidates[0] if candidates else ""),
        )
        return (f"錯誤：找不到名為「{safe_name}」的應用程式。{hint}"
                f"如果這是語音誤聽，請告訴小紅正確的 App 名稱，並說「記住這個錯」"
                f"以後就不會再犯。")
    except Exception as e:
        _log_mistake(
            mistake_type="app_open_error",
            user_said=safe_name,
            detail=str(e),
        )
        return f"開啟失敗：{e}"


def close_application(app_name: str):
    """【跨平台】關閉/結束應用程式。大王說「關閉郵件」、「退出 Safari」時呼叫此工具。"""
    print(f"\n[系統日誌] 🛑 關閉應用程式：{app_name}（平台：{_OS}）...")
    app_name = _resolve_app_name(app_name)
    safe_name = app_name.replace('"', '').replace('\\', '').replace("'", '').strip()
    if not safe_name:
        return "錯誤：應用程式名稱無效。"

    try:
        if _IS_MAC:
            script = f'tell application "{safe_name}" to quit'
            r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return f"已成功關閉 {safe_name}。"
            return f"關閉失敗：{r.stderr.strip()}"
        if _IS_WIN:
            try:
                import pygetwindow
            except ImportError:
                pygetwindow = None
            if pygetwindow:
                wins = [w for w in pygetwindow.getAllWindows() if safe_name.lower() in (w.title or "").lower()]
                if not wins:
                    return "找不到執行中的該程式。"
                for w in wins:
                    w.close()
                return f"已關閉 {safe_name}。"
            else:
                os.system(f'taskkill /IM {safe_name}.exe /F')
                return f"已嘗試強制關閉 {safe_name}。"
        if _IS_LINUX:
            subprocess.run(['killall', safe_name], capture_output=True, timeout=3)
            return f"已嘗試關閉 {safe_name}。"
        return f"未支援的平台：{_OS}"
    except Exception as e:
        return f"關閉 {safe_name} 時發生錯誤：{e}"
