"""macOS Accessibility API 整合 — 專業 RPA 的基礎（T1）。

把小紅的 GUI 操作從「座標硬寫」升級成「元素語義查找」：
    舊：click_screen(300, 412)           ← 視窗一動就爛
    新：ax_click(app="Mail", title="送出")  ← 只要那個 button 在，就找得到

底層：pyobjc 的 AXUIElement API（等同於 Windows 的 UIAutomation）。
Apple 原生 App / 多數第三方 App / 網頁（經 Chrome）都支援 accessibility tree。

⚠️ 需要 macOS Accessibility 權限：
    System Settings → Privacy & Security → Accessibility
    → 加入 Terminal / iTerm / agent.py 在跑的 host process
    第一次呼叫任何 ax_* 會偵測權限，沒給會回清楚訊息。

常用流程：
    1. ax_describe_app("Mail") 看元素樹、找目標
    2. ax_click / ax_type / ax_get_value
    3. 失敗時 ax_describe_app 再看一次（畫面狀態可能變了）
"""
from __future__ import annotations

import json
from typing import Any

try:
    from ApplicationServices import (
        AXIsProcessTrustedWithOptions,
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
        kAXErrorSuccess,
        kAXErrorNoValue,
        kAXErrorAttributeUnsupported,
        kAXTrustedCheckOptionPrompt,
    )
    _AX_AVAILABLE = True
    _AX_IMPORT_ERROR: Exception | None = None
except ModuleNotFoundError as exc:
    _AX_AVAILABLE = False
    _AX_IMPORT_ERROR = exc
    kAXErrorSuccess = 0
    kAXErrorNoValue = -25212
    kAXErrorAttributeUnsupported = -25205
    kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"

    def AXIsProcessTrustedWithOptions(_options):
        return False

    def AXUIElementCreateApplication(_pid):
        return None

    def AXUIElementCopyAttributeValue(_element, _attr, _unused):
        return kAXErrorNoValue, None

    def AXUIElementPerformAction(_element, _action):
        return kAXErrorAttributeUnsupported

    def AXUIElementSetAttributeValue(_element, _attr, _value):
        return kAXErrorAttributeUnsupported

# 常用 attribute / action 常數名稱（字串）。pyobjc 的 AX 很多常數在 k* 裡
# 但直接用字串也 work，code 比較清楚。
_ROLE_ATTR = "AXRole"
_SUBROLE_ATTR = "AXSubrole"
_TITLE_ATTR = "AXTitle"
_VALUE_ATTR = "AXValue"
_DESCRIPTION_ATTR = "AXDescription"
_CHILDREN_ATTR = "AXChildren"
_ENABLED_ATTR = "AXEnabled"

_PRESS_ACTION = "AXPress"


# ────────────────────────────────────────────────────────────────────
# 權限檢查
# ────────────────────────────────────────────────────────────────────
def ax_check_permission(prompt: bool = False) -> str:
    """檢查目前 Python 進程有沒有 macOS Accessibility 權限。

    Args:
        prompt: True 時若沒權限會**彈系統視窗**請使用者開啟（只會彈一次）。
                預設 False（不打擾）。
    Returns:
        True / False 狀態 + 沒權限時的明確指引。
    """
    if not _AX_AVAILABLE:
        return (
            "❌ macOS Accessibility API 不可用。這些 ax_* GUI 工具只能在 macOS "
            f"本機執行；目前 runtime 沒有 ApplicationServices（{_AX_IMPORT_ERROR}）。"
        )
    has = AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt})
    if has:
        return "✅ Accessibility 權限 OK，可用 ax_* 工具。"
    import sys
    host = sys.executable
    return (
        "❌ 沒有 Accessibility 權限。請：\n"
        "  1. 開啟 System Settings → Privacy & Security → Accessibility\n"
        f"  2. 加入：{host}\n"
        "     （或加入執行 agent 的 Terminal / iTerm）\n"
        "  3. 勾選後重新執行 agent（ax_* 工具就會可用）\n\n"
        "  或者：呼叫 ax_check_permission(prompt=True) 讓系統自動彈授權視窗一次。"
    )


# ────────────────────────────────────────────────────────────────────
# 內部 helpers
# ────────────────────────────────────────────────────────────────────
def _get_attr(element, attr: str):
    """讀一個 attribute；失敗回 None。"""
    try:
        err, val = AXUIElementCopyAttributeValue(element, attr, None)
        if err == kAXErrorSuccess:
            return val
    except Exception:
        pass
    return None


def _get_pid_for_app(app_name: str) -> int | None:
    """透過 NSWorkspace 找 app 的 PID（要 app 正在跑）。大小寫不敏感。"""
    from Cocoa import NSWorkspace
    target = (app_name or "").strip().lower()
    if not target:
        return None
    ws = NSWorkspace.sharedWorkspace()
    for app in ws.runningApplications():
        name = (app.localizedName() or "").lower()
        if name == target or target in name:
            return app.processIdentifier()
    return None


def _get_app_element(app_name: str):
    """從 app 名取得 AXUIElement，失敗回 (None, error_msg)。"""
    pid = _get_pid_for_app(app_name)
    if pid is None:
        return None, f"找不到執行中的 App '{app_name}'。先確認它有打開（可用 open_application）。"
    element = AXUIElementCreateApplication(pid)
    if element is None:
        return None, f"AXUIElementCreateApplication 失敗（pid={pid}）"
    return element, None


def _describe_node(element, depth: int = 0, max_depth: int = 3,
                    max_children: int = 20) -> dict:
    """遞迴抽 element 的關鍵 attributes + 子節點。"""
    role = _get_attr(element, _ROLE_ATTR)
    title = _get_attr(element, _TITLE_ATTR)
    value = _get_attr(element, _VALUE_ATTR)
    desc = _get_attr(element, _DESCRIPTION_ATTR)
    subrole = _get_attr(element, _SUBROLE_ATTR)

    node: dict[str, Any] = {"role": str(role) if role else "?"}
    if subrole:
        node["subrole"] = str(subrole)
    if title:
        node["title"] = str(title)
    if value is not None:
        # value 可能很長（整個文字欄內容）；截斷
        v = str(value)
        node["value"] = v[:200] + ("…" if len(v) > 200 else "")
    if desc:
        node["description"] = str(desc)[:100]

    if depth < max_depth:
        children = _get_attr(element, _CHILDREN_ATTR) or []
        if children:
            kids = []
            for i, c in enumerate(children[:max_children]):
                kids.append(_describe_node(c, depth + 1, max_depth, max_children))
            if len(children) > max_children:
                kids.append({"...": f"（另有 {len(children) - max_children} 個子節點未顯示）"})
            if kids:
                node["children"] = kids
    return node


def _find_matching(element, predicate, max_depth: int = 10,
                    results: list | None = None) -> list:
    """BFS 找所有符合 predicate(element)->bool 的節點。"""
    if results is None:
        results = []
    try:
        if predicate(element):
            results.append(element)
    except Exception:
        pass
    if max_depth <= 0:
        return results
    children = _get_attr(element, _CHILDREN_ATTR) or []
    for c in children:
        _find_matching(c, predicate, max_depth - 1, results)
    return results


# ────────────────────────────────────────────────────────────────────
# Tools — 給小紅 agent 用
# ────────────────────────────────────────────────────────────────────
def ax_describe_app(app_name: str, depth: int = 3, max_children: int = 20) -> str:
    """Dump 指定 App 的 Accessibility 元素樹（結構化 JSON）。

    找目標元素前先用這個看看能點什麼。建議從 depth=2 開始（夠多數畫面），
    需要找深層元素再加 depth。

    Args:
        app_name: App 名稱，例如 "Mail", "Safari", "Finder", "Calendar"。
                  大小寫不敏感、可用部分字串（"mail" 會匹配 "Mail"）。
        depth: 遞迴深度，預設 3。要看更深可到 5-6，但畫面會很長。
        max_children: 每層最多顯示幾個子節點（避免無限列）。預設 20。

    Returns:
        JSON 格式的元素樹，前 4000 字。關鍵欄位：role（元素類型）、
        title（顯示文字）、value（輸入值）、children（子元素）。
    """
    if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}):
        return ax_check_permission()
    element, err = _get_app_element(app_name)
    if err:
        return f"❌ {err}"
    tree = _describe_node(element, depth=0, max_depth=depth, max_children=max_children)
    out = json.dumps(tree, ensure_ascii=False, indent=2)
    if len(out) > 4000:
        out = out[:4000] + "\n...\n(tree 被截斷，深度調小或 max_children 調小可看完整)"
    return f"📐 {app_name} Accessibility tree:\n{out}"


def ax_find_elements(app_name: str, title: str = "", role: str = "",
                     value_contains: str = "") -> str:
    """在 App 的元素樹中找符合條件的所有元素，回它們的位置與資訊。

    Args:
        app_name: 目標 App。
        title: 元素的 AXTitle（顯示標籤），支援完全匹配或子字串；空字串 = 任意。
        role: 元素 AXRole，例如 "AXButton", "AXTextField", "AXMenuItem"。
        value_contains: 元素當前值含這個子字串；空字串 = 任意。

    至少要給 title / role / value_contains 其中一個。
    """
    if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}):
        return ax_check_permission()
    if not any([title, role, value_contains]):
        return "❌ 至少要給 title / role / value_contains 其中一個"
    element, err = _get_app_element(app_name)
    if err:
        return f"❌ {err}"

    t_lo = (title or "").lower()
    v_lo = (value_contains or "").lower()

    def pred(node):
        if role:
            r = _get_attr(node, _ROLE_ATTR)
            if str(r or "") != role:
                return False
        if t_lo:
            n_title = str(_get_attr(node, _TITLE_ATTR) or "")
            if t_lo not in n_title.lower():
                return False
        if v_lo:
            n_val = str(_get_attr(node, _VALUE_ATTR) or "")
            if v_lo not in n_val.lower():
                return False
        return True

    matches = _find_matching(element, pred, max_depth=15)
    if not matches:
        return f"🔍 {app_name} 裡找不到符合條件的元素（title='{title}', role='{role}'）"

    lines = [f"🔍 {app_name} 找到 {len(matches)} 個符合："]
    for i, m in enumerate(matches[:15], 1):
        r = _get_attr(m, _ROLE_ATTR)
        t = _get_attr(m, _TITLE_ATTR)
        v = _get_attr(m, _VALUE_ATTR)
        enabled = _get_attr(m, _ENABLED_ATTR)
        lines.append(
            f"  {i}. role={r} title={t!r}"
            + (f" value={str(v)[:60]!r}" if v else "")
            + (f" enabled={enabled}" if enabled is not None else "")
        )
    if len(matches) > 15:
        lines.append(f"  ...（還有 {len(matches) - 15} 個）")
    return "\n".join(lines)


def ax_click(app_name: str, title: str, role: str = "",
             at_index: int = 0) -> str:
    """用 Accessibility 點擊 App 裡的某個元素（比座標點擊穩 95%）。

    Args:
        app_name: 目標 App。
        title: 要點的元素顯示文字（button 名、menu item 名等）。子字串匹配。
        role: 可選，例如 "AXButton" 限定按鈕；空字串 = 任意 role。
        at_index: 如果有多個符合，用哪一個（0-based）；預設 0。

    Returns:
        成功 / 失敗訊息。失敗時建議用 ax_describe_app 或 ax_find_elements 先探。
    """
    if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}):
        return ax_check_permission()
    if not title:
        return "❌ title 不能空（要給要點的元素名稱）"
    element, err = _get_app_element(app_name)
    if err:
        return f"❌ {err}"

    t_lo = title.lower()

    def pred(node):
        n_title = str(_get_attr(node, _TITLE_ATTR) or "")
        if t_lo not in n_title.lower():
            return False
        if role:
            r = _get_attr(node, _ROLE_ATTR)
            if str(r or "") != role:
                return False
        return True

    matches = _find_matching(element, pred, max_depth=15)
    if not matches:
        return (f"❌ {app_name} 裡找不到 title 含 '{title}' 的元素"
                + (f"（role={role}）" if role else "")
                + "。用 ax_describe_app 看元素樹。")
    if at_index >= len(matches):
        return f"❌ 只有 {len(matches)} 個符合，at_index={at_index} 超出範圍"

    target = matches[at_index]
    err_code = AXUIElementPerformAction(target, _PRESS_ACTION)
    if err_code == kAXErrorSuccess:
        return f"✅ 已點擊 {app_name}.{title}（{len(matches)} 個符合中的第 {at_index}）"
    return f"❌ Press action 失敗，錯誤碼: {err_code}"


def ax_type_in(app_name: str, field_title: str, text: str) -> str:
    """把 text 設到指定文字欄位的值（不是模擬鍵盤，而是直接設 AXValue）。

    適合輸入完整字串；比 `type_text` 精準（不會因為焦點跑掉打錯欄位）。

    Args:
        app_name: 目標 App。
        field_title: 文字欄位的 AXTitle 或 placeholder；子字串匹配。
        text: 要輸入的完整字串。
    """
    if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}):
        return ax_check_permission()
    element, err = _get_app_element(app_name)
    if err:
        return f"❌ {err}"

    t_lo = (field_title or "").lower()
    def pred(node):
        r = _get_attr(node, _ROLE_ATTR)
        if r not in ("AXTextField", "AXTextArea", "AXSearchField"):
            return False
        n_title = str(_get_attr(node, _TITLE_ATTR) or _get_attr(node, _DESCRIPTION_ATTR) or "")
        return t_lo in n_title.lower() if t_lo else True

    matches = _find_matching(element, pred, max_depth=15)
    if not matches:
        return f"❌ 找不到 {app_name} 裡 title 含 '{field_title}' 的文字欄位"

    target = matches[0]
    try:
        err_code = AXUIElementSetAttributeValue(target, _VALUE_ATTR, text)
    except Exception as e:
        return f"❌ 設值失敗: {type(e).__name__}: {e}"
    if err_code == kAXErrorSuccess:
        return f"✅ 已在 {app_name} 的 '{field_title}' 填入 {len(text)} 字"
    return f"❌ SetAttributeValue 錯誤碼: {err_code}"


def ax_read_value(app_name: str, element_title: str) -> str:
    """讀指定元素的 AXValue（例如文字欄位裡的內容、label 的文字、checkbox 狀態）。

    Args:
        app_name: 目標 App。
        element_title: 元素的 title。子字串匹配。
    """
    if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}):
        return ax_check_permission()
    element, err = _get_app_element(app_name)
    if err:
        return f"❌ {err}"

    t_lo = (element_title or "").lower()
    def pred(node):
        n_title = str(_get_attr(node, _TITLE_ATTR) or "")
        return t_lo in n_title.lower() if t_lo else False

    matches = _find_matching(element, pred, max_depth=15)
    if not matches:
        return f"❌ 找不到 {app_name} 裡 title 含 '{element_title}' 的元素"

    target = matches[0]
    value = _get_attr(target, _VALUE_ATTR)
    if value is None:
        return "⚠️ 元素存在但 AXValue 是 None（可能是純顯示 label 沒值）"
    return f"{value}"


def ax_list_running_apps() -> str:
    """列出所有正在跑的 App，含 PID（給 ax_describe_app 用）。"""
    if not _AX_AVAILABLE:
        return ax_check_permission()
    from Cocoa import NSWorkspace
    ws = NSWorkspace.sharedWorkspace()
    apps = []
    for app in ws.runningApplications():
        name = app.localizedName()
        if not name:
            continue
        pid = app.processIdentifier()
        policy = app.activationPolicy()  # 0=regular, 1=accessory, 2=prohibited
        if policy != 0:  # 只列使用者看得到的 app
            continue
        apps.append((name, pid))
    apps.sort(key=lambda x: x[0].lower())

    lines = [f"📱 執行中的 GUI Apps（共 {len(apps)} 個）："]
    for name, pid in apps:
        lines.append(f"  • {name}  (pid={pid})")
    return "\n".join(lines)
