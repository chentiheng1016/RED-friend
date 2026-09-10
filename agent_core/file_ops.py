"""Local file operations + shared path helper.

Self-contained; pure
stdlib (os, shutil).
"""
import os
import re
import shutil

_MAX_READ_FILE_MB = 10

# Characters illegal on most filesystems (Windows is the strictest).
# Callers needing a path-safe slug (spaces too) pass strip_whitespace=True.
_FILENAME_BAD_CHARS = re.compile(r"[\\/:*?\"<>|]")
_SLUG_BAD_CHARS = re.compile(r"[\\/:*?\"<>|\s]")


def _clean_path(p):
    """Strip → expanduser → V7 path-safety guard。被擋會 raise ValueError。

    (V7) 多數 caller 已經 try/except 包住 — 但若 caller 沒接，
    err message 會浮到使用者面前，這也算合理。
    """
    if not p:
        return ""
    cleaned = p.strip().strip("'").strip('"')
    if not cleaned:
        return ""
    from agent_core.path_safety import safe_path
    return safe_path(cleaned)


def _sanitize_filename(name: str, max_len: int = 80, strip_whitespace: bool = False) -> str:
    """Replace characters illegal on common filesystems with '_'.

    strip_whitespace=True also collapses spaces (useful for URL-like slugs
    or customer/model directory names where whitespace is undesirable).
    """
    pattern = _SLUG_BAD_CHARS if strip_whitespace else _FILENAME_BAD_CHARS
    return pattern.sub("_", (name or "").strip())[:max_len]


def manage_files(action: str, target: str, destination: str = None):
    """本機檔案操作。action：mkdir / delete / rename / move / copy / list。target 是主路徑，destination 用在 rename/move/copy。"""
    print(f"\n[系統日誌] 📁 檔案操作：{action}")
    try:
        clean_target = _clean_path(target)  # V7: may raise ValueError
        clean_dest = _clean_path(destination) if destination else None
        if action == 'mkdir':
            os.makedirs(clean_target, exist_ok=True)
            return f"已建立 {clean_target}"
        if action == 'delete':
            if not os.path.exists(clean_target):
                return f"錯誤：找不到 {clean_target}，無法刪除。"
            if os.path.isdir(clean_target):
                shutil.rmtree(clean_target)
            else:
                os.remove(clean_target)
            return f"已刪除 {clean_target}"
        if action == 'move':
            if not clean_dest:
                return "錯誤：移動操作需要提供目的地路徑。"
            if not os.path.exists(clean_target):
                return f"錯誤：找不到 {clean_target}，無法移動。"
            dest_parent = os.path.dirname(clean_dest)
            if dest_parent:
                os.makedirs(dest_parent, exist_ok=True)
            shutil.move(clean_target, clean_dest)
            return f"已移動至 {clean_dest}"
        if action == 'copy':
            if not clean_dest:
                return "錯誤：複製操作需要提供目的地路徑。"
            if not os.path.exists(clean_target):
                return f"錯誤：找不到 {clean_target}，無法複製。"
            dest_parent = os.path.dirname(clean_dest)
            if dest_parent:
                os.makedirs(dest_parent, exist_ok=True)
            if os.path.isdir(clean_target):
                shutil.copytree(clean_target, clean_dest)
            else:
                shutil.copy2(clean_target, clean_dest)
            return f"已複製至 {clean_dest}"
        return f"未知操作：{action}（支援 mkdir / delete / move / copy）"
    except ValueError as e:
        # V7 path-safety 擋下：直接回 user
        return str(e)
    except Exception as e:
        return f"操作失敗：{e}"


def read_file(filename: str):
    """讀本機文字檔內容（支援 ~ 展開、絕對/相對路徑）。大檔有大小上限保護。"""
    try:
        clean_name = _clean_path(filename)  # V7: may raise ValueError
        if os.path.exists(clean_name):
            size_mb = os.path.getsize(clean_name) / (1024 * 1024)
            if size_mb > _MAX_READ_FILE_MB:
                return f"錯誤：{clean_name} 太大（{size_mb:.1f}MB），超過 {_MAX_READ_FILE_MB}MB 上限。"
        for encoding in ('utf-8-sig', 'cp950'):
            try:
                with open(clean_name, 'r', encoding=encoding) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        with open(clean_name, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except FileNotFoundError:
        return f"錯誤：找不到檔案 {clean_name}"
    except ValueError as e:
        # V7 path-safety 擋下：直接回 user 看得懂的訊息
        return str(e)
    except Exception as e:
        return f"讀取失敗: {e}"


def write_file(filename: str, content: str):
    """寫內容到本機檔案（覆蓋模式）。會自動建上層目錄。"""
    try:
        clean_file = _clean_path(filename)
        parent_dir = os.path.dirname(clean_file)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(clean_file, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"{clean_file} 寫入成功"
    except Exception as e:
        return f"寫入失敗: {e}"
