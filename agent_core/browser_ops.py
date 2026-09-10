import re

from agent_core.web_access_guard import assess_web_access, format_web_access_assessment


def _sanitize_browser_text(text: str, label: str = "browser-content") -> str:
    try:
        from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted
        return wrap_as_untrusted(sanitize_for_llm(text or ""), label=label)
    except Exception:
        return text or ""


def browser_err(action: str, error: Exception) -> str:
    return f"瀏覽器操作失敗（{action}）：{type(error).__name__}: {error}"


def browser_open(url: str, wait_for: str = "load", *, ensure_page) -> str:
    try:
        page = ensure_page()
        response = page.goto(url, wait_until=wait_for)
        raw_title = page.title()
        try:
            body = page.content()
            http_status = response.status if response is not None else None
            assessment = assess_web_access(
                url=url,
                final_url=page.url,
                http_status=http_status,
                title=raw_title,
                body=body,
            )
            if not assessment.can_extract:
                return format_web_access_assessment(assessment)
        except Exception:
            # Diagnostics must never break a normal browser open path.
            pass
        title = _sanitize_browser_text(raw_title[:80], label="browser-title")
        return f"✅ 已開啟：{page.url}（title: {title}）"
    except Exception as e:
        return browser_err("open", e)


def browser_status(*, browser_session_cls) -> str:
    try:
        if browser_session_cls._page is None or browser_session_cls._page.is_closed():
            return "瀏覽器目前未啟動。用 browser_open(url) 開始。"
        page = browser_session_cls._page
        title = _sanitize_browser_text(page.title(), label="browser-title")
        return f"URL: {page.url}\n標題: {title}\n視窗: {browser_session_cls._ctx.pages and len(browser_session_cls._ctx.pages)} 個分頁"
    except Exception as e:
        return browser_err("status", e)


def browser_read(max_chars: int = 4000, *, ensure_page) -> str:
    try:
        page = ensure_page()
        text = page.evaluate(
            "() => { const c = document.body.cloneNode(true);"
            " c.querySelectorAll('script,style,noscript').forEach(n=>n.remove());"
            " return c.innerText; }"
        )
        text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
        max_chars = max(500, min(int(max_chars), 20000))
        truncated = len(text) > max_chars
        out = text[:max_chars] + ("\n…（已截斷）" if truncated else "")
        return _sanitize_browser_text(out)
    except Exception as e:
        return browser_err("read", e)


def browser_click(selector: str, timeout_ms: int = 8000, *, ensure_page) -> str:
    try:
        page = ensure_page()
        page.locator(selector).first.click(timeout=int(timeout_ms))
        return f"✅ 已點擊：{selector}（頁面 URL 現在：{page.url[:100]}）"
    except Exception as e:
        return browser_err(f"click {selector}", e)


def browser_fill(selector: str, value: str, timeout_ms: int = 8000, *, ensure_page) -> str:
    try:
        page = ensure_page()
        page.locator(selector).first.fill(value, timeout=int(timeout_ms))
        return f"✅ 已填入 {selector}：{value[:50]}"
    except Exception as e:
        return browser_err(f"fill {selector}", e)


def browser_type(selector: str, text: str, delay_ms: int = 30, *, ensure_page) -> str:
    try:
        page = ensure_page()
        locator = page.locator(selector).first
        locator.click()
        locator.press_sequentially(text, delay=int(delay_ms))
        return f"✅ 已逐字輸入 {selector}：{text[:50]}"
    except Exception as e:
        return browser_err(f"type {selector}", e)


def browser_press(key: str, *, ensure_page) -> str:
    try:
        page = ensure_page()
        page.keyboard.press(key)
        return f"✅ 已按 {key}"
    except Exception as e:
        return browser_err(f"press {key}", e)


def browser_wait_for(selector: str, timeout_ms: int = 15000, *, ensure_page) -> str:
    try:
        page = ensure_page()
        page.locator(selector).first.wait_for(timeout=int(timeout_ms))
        return f"✅ 元素已出現：{selector}"
    except Exception as e:
        return browser_err(f"wait_for {selector}", e)
