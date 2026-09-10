"""Terminal window management (bring-to-front).

Cross-platform best-effort;
silently degrades if the underlying tool is missing.
"""
import platform
import subprocess

from agent_core.logging_and_paths import logger

_OS = platform.system()
_IS_MAC = _OS == "Darwin"
_IS_WIN = _OS == "Windows"
_IS_LINUX = _OS == "Linux"


def bring_to_front():
    try:
        if _IS_MAC:
            _mac_bring_to_front()
        elif _IS_WIN:
            _win_bring_to_front()
        elif _IS_LINUX:
            _linux_bring_to_front()
    except Exception as e:
        logger.debug("bring_to_front 失敗（無礙）：%s", e)


def _mac_bring_to_front():
    script = '''
    tell application "System Events"
        set termApps to {"Terminal", "iTerm2", "iTerm", "Code", "Cursor", "Ghostty", "Warp", "Hyper", "Alacritty", "Tabby", "kitty", "Rio", "WezTerm", "Nova", "PyCharm", "PyCharm CE"}
        repeat with appName in termApps
            if (exists process appName) then
                set frontmost of process appName to true
                return
            end if
        end repeat
    end tell
    '''
    subprocess.run(['osascript', '-e', script], capture_output=True, timeout=5)


def _win_bring_to_front():
    try:
        import pygetwindow
    except ImportError:
        return
    candidates = ['Python', 'cmd', 'PowerShell', 'Windows Terminal', 'Command Prompt']
    for title_hint in candidates:
        try:
            wins = [w for w in pygetwindow.getAllWindows()
                    if title_hint.lower() in (w.title or "").lower()]
            if wins:
                w = wins[0]
                if w.isMinimized:
                    w.restore()
                w.activate()
                return
        except Exception:
            continue


def _linux_bring_to_front():
    candidates = ['gnome-terminal', 'konsole', 'xterm', 'Alacritty', 'kitty', 'Terminal']
    for name in candidates:
        try:
            r = subprocess.run(['wmctrl', '-a', name], capture_output=True, timeout=3)
            if r.returncode == 0:
                return
        except FileNotFoundError:
            return
        except Exception:
            continue
