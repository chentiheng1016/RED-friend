"""Playwright browser automation tools.

_BrowserSession is a
module-level singleton; Playwright is lazy-loaded on first use. Thin
wrappers delegate most behavior to browser_ops (sibling top-level module).
"""
import os
from datetime import datetime

from agent_core import browser_ops

from agent_core.logging_and_paths import logger

_BROWSER_PROFILE_DIR = os.path.expanduser("~/Documents/小紅瀏覽器/profile")


class _BrowserSession:
    """Playwright 單例：首次呼叫任何 browser_* 工具時才啟動；之後沿用同一個分頁。"""
    _pw = None
    _ctx = None
    _page = None
    _headless = False  # 預設開視窗，大王能看小紅在做什麼

    @classmethod
    def _ensure(cls):
        # 完整健康檢查：page、ctx、pw 三個任一壞掉就重建，避免幽靈狀態
        if (cls._page is not None and cls._ctx is not None and cls._pw is not None):
            try:
                if not cls._page.is_closed():
                    _ = cls._ctx.pages  # 用 pages 屬性觸發 ctx 有效性檢查
                    return cls._page
            except Exception as _e:
                logger.debug("browser 狀態檢查失敗（%s），將重建", _e)
        # 狀態不完整或已壞 — 先清乾淨再重建，避免雙重 start
        cls.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright 未安裝。執行：pip install playwright && python3 -m playwright install chromium")
        os.makedirs(_BROWSER_PROFILE_DIR, exist_ok=True)
        cls._pw = sync_playwright().start()
        cls._ctx = cls._pw.chromium.launch_persistent_context(
            user_data_dir=_BROWSER_PROFILE_DIR,
            headless=cls._headless,
            viewport={"width": 1280, "height": 900},
            locale="zh-TW",
            args=["--disable-blink-features=AutomationControlled"],
        )
        if cls._ctx.pages:
            cls._page = cls._ctx.pages[0]
        else:
            cls._page = cls._ctx.new_page()
        cls._page.set_default_timeout(15000)
        return cls._page

    @classmethod
    def close(cls):
        try:
            if cls._ctx is not None:
                cls._ctx.close()
        except Exception as _e:
            logger.debug("browser ctx close: %s", _e)
        try:
            if cls._pw is not None:
                cls._pw.stop()
        except Exception as _e:
            logger.debug("browser pw stop: %s", _e)
        cls._page = None
        cls._ctx = None
        cls._pw = None


def _browser_err(action: str, e: Exception) -> str:
    return browser_ops.browser_err(action, e)


def browser_open(url: str, wait_for: str = "load"):
    """開啟指定網址。wait_for: 'load'（預設, DOM 載完）、'networkidle'（連 AJAX 都靜止）、'domcontentloaded'。
    首次呼叫會啟動 Chromium（cookie 持久化在 ~/Documents/小紅瀏覽器/profile/）。"""
    return browser_ops.browser_open(url, wait_for=wait_for, ensure_page=_BrowserSession._ensure)


def browser_status():
    """看目前瀏覽器分頁的 URL / 標題；如未啟動就回報未啟動。"""
    return browser_ops.browser_status(browser_session_cls=_BrowserSession)


def browser_read(max_chars: int = 4000):
    """讀取目前頁面的純文字內容（去掉 script/style）。預設上限 4000 字，太長自動截斷。"""
    return browser_ops.browser_read(max_chars=max_chars, ensure_page=_BrowserSession._ensure)


def browser_click(selector: str, timeout_ms: int = 8000):
    """點擊元素。selector 可以是：
       - CSS：'button#submit'、'.btn-primary'、'a[href*=order]'
       - 可見文字：'text=送出訂單'、'text=/^登入/'（regex）
       - ARIA role：'role=button[name=\"Submit\"]'
       - XPath：'xpath=//button[contains(text(),\"確認\")]'
       找到第一個符合的元素就點。"""
    return browser_ops.browser_click(selector, timeout_ms=timeout_ms, ensure_page=_BrowserSession._ensure)


def browser_fill(selector: str, value: str, timeout_ms: int = 8000):
    """在輸入框填值（會先清空）。selector 範例：'input[name=username]'、'#email'、'textarea.comment'。"""
    return browser_ops.browser_fill(selector, value, timeout_ms=timeout_ms, ensure_page=_BrowserSession._ensure)


def browser_type(selector: str, text: str, delay_ms: int = 30):
    """逐字敲鍵（適合自動完成、搜尋建議需要模擬真人打字的場景）。"""
    return browser_ops.browser_type(selector, text, delay_ms=delay_ms, ensure_page=_BrowserSession._ensure)


def browser_press(key: str):
    """按單一鍵（在目前焦點元素上）。常用：'Enter'、'Tab'、'Escape'、'ArrowDown'、'Control+A'。"""
    return browser_ops.browser_press(key, ensure_page=_BrowserSession._ensure)


def browser_wait_for(selector: str, timeout_ms: int = 15000):
    """等某元素出現（頁面慢 / AJAX 載入時用）。"""
    return browser_ops.browser_wait_for(selector, timeout_ms=timeout_ms, ensure_page=_BrowserSession._ensure)


def browser_extract(selector: str, attribute: str = "text", limit: int = 20):
    """抽取多個元素的內容。
    attribute：'text'（可見文字）、'html'、'href'、'src'、'value' 或任何 HTML 屬性名。
    limit：最多抓幾個（預設 20）。常用於『把表格第一欄全部抓出來』、『把搜尋結果的標題+連結全部拿到』。"""
    try:
        page = _BrowserSession._ensure()
        loc = page.locator(selector)
        count = min(loc.count(), int(limit))
        if count == 0:
            return f"找不到任何符合 {selector} 的元素。"
        results = []
        from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted
        for i in range(count):
            item = loc.nth(i)
            attr = attribute.lower()
            try:
                if attr == "text":
                    v = item.inner_text(timeout=3000)
                elif attr == "html":
                    v = item.inner_html(timeout=3000)
                elif attr == "value":
                    v = item.input_value(timeout=3000)
                else:
                    v = item.get_attribute(attribute, timeout=3000)
            except Exception as _e:
                v = f"(取值失敗: {_e})"
            results.append(f"[{i+1}] {sanitize_for_llm(str(v))}")
        body = f"共 {count} 筆（attribute={attribute}）：\n" + "\n".join(results)
        return wrap_as_untrusted(body, label="browser-extract")
    except Exception as e:
        return _browser_err(f"extract {selector}", e)


def browser_screenshot(save_path: str = "", full_page: bool = False):
    """螢幕截圖。save_path 空字串 = 自動存到 ~/Downloads/xiaohong_browser_YYYYMMDD_HHMMSS.png。
    full_page=True 截整頁（含捲動區域），否則只截目前可見範圍。"""
    try:
        page = _BrowserSession._ensure()
        if not save_path:
            save_path = os.path.expanduser(
                f"~/Downloads/xiaohong_browser_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )
        else:
            from agent_core.path_safety import safe_path
            save_path = safe_path(save_path)
        page.screenshot(path=save_path, full_page=bool(full_page))
        size_kb = os.path.getsize(save_path) // 1024
        return f"✅ 截圖已存：{save_path}（{size_kb} KB）"
    except Exception as e:
        return _browser_err("screenshot", e)


def browser_scroll(direction: str = "down", pixels: int = 800):
    """捲動目前頁面。direction: up / down / top / bottom；pixels 只在 up/down 有效。"""
    try:
        page = _BrowserSession._ensure()
        d = direction.lower().strip()
        if d == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif d == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif d == "up":
            page.evaluate(f"window.scrollBy(0, -{int(pixels)})")
        else:
            page.evaluate(f"window.scrollBy(0, {int(pixels)})")
        return f"✅ 已捲動 {d}"
    except Exception as e:
        return _browser_err("scroll", e)


def browser_eval(js: str):
    """在目前頁面執行 JavaScript；回傳該 JS 的結果（序列化成字串）。
    ⚠️ 威力大，只在其他工具做不到時用。例如：
       browser_eval('document.querySelectorAll(\".price\").length')
       browser_eval('window.localStorage.getItem(\"auth_token\")')"""
    try:
        page = _BrowserSession._ensure()
        result = page.evaluate(f"() => {{ return ({js}); }}")
        from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted
        safe = sanitize_for_llm(str(result)[:1500])
        return "✅ JS 結果：\n" + wrap_as_untrusted(safe, label="browser-js-result")
    except Exception as e:
        return _browser_err("eval", e)


def browser_new_tab(url: str = ""):
    """開新分頁（可選跳到某 URL）。之後所有 browser_* 工具會作用在新分頁上。"""
    try:
        _BrowserSession._ensure()
        new_page = _BrowserSession._ctx.new_page()
        _BrowserSession._page = new_page
        if url:
            new_page.goto(url)
        return f"✅ 新分頁 #{len(_BrowserSession._ctx.pages)}：{new_page.url}"
    except Exception as e:
        return _browser_err("new_tab", e)


def browser_close():
    """完全關掉瀏覽器（釋放記憶體）。下次 browser_open 會重新啟動。"""
    try:
        _BrowserSession.close()
        return "✅ 瀏覽器已關閉。"
    except Exception as e:
        return _browser_err("close", e)
