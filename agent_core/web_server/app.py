"""Phase 4 employee web interface — FastAPI app.

Routes:
  GET  /                    → redirect to /dept/<color> or /auth/login
  GET  /auth/login          → Google OAuth redirect
  GET  /auth/callback       → OAuth callback, set session
  GET  /auth/logout         → clear session
  GET  /dept/<color>        → department chat page
  POST /api/dept/<color>/query    → query intent (read-only)
  POST /api/dept/<color>/command  → command intent (write, requires confirm)
  GET  /admin/employees           → list/add employees (大王 only)
  POST /admin/employees           → register employee
  GET  /admin/telegram            → Telegram audit + command policy
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import html as _html

_log = logging.getLogger(__name__)

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from agent_core.web_server.auth import build_login_url, exchange_code, get_secret_key, generate_state
from agent_core.web_server.employee_registry import get_employee, register_employee, list_employees

_HERE = os.path.dirname(__file__)


class _LazySessionMiddleware:
    """Resolve the session secret only when a request needs session support."""

    def __init__(self, app, *, max_age: int, https_only: bool):
        self.app = app
        self.max_age = max_age
        self.https_only = https_only
        self._inner = None
        self._lock = threading.Lock()
        self._secret_error_logged = False

    def _middleware(self):
        if self._inner is None:
            with self._lock:
                if self._inner is None:
                    try:
                        secret_key = get_secret_key()
                    except RuntimeError:
                        if not self._secret_error_logged:
                            _log.exception(
                                "Failed to initialize session middleware; set WEB_SECRET_KEY "
                                "to a random value with at least 32 characters if Keychain is unavailable."
                            )
                            self._secret_error_logged = True
                        raise
                    self._inner = SessionMiddleware(
                        self.app,
                        secret_key=secret_key,
                        max_age=self.max_age,
                        https_only=self.https_only,
                    )
        return self._inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if scope.get("type") == "http" and (
            path in {"/health", "/healthz", "/line/webhook"}
            or path.startswith("/api/edge/")
        ):
            await self.app(scope, receive, send)
            return
        try:
            middleware = self._middleware()
        except RuntimeError:
            response = PlainTextResponse(
                "Session configuration error: unable to load the signing key. "
                "Set WEB_SECRET_KEY to a random value with at least 32 characters.",
                status_code=500,
            )
            await response(scope, receive, send)
            return
        await middleware(scope, receive, send)


# https_only=True sets the Secure flag on session cookies (required for HTTPS deployments).
# Set WEB_HTTPS_ONLY=0 only for local HTTP development; always leave at 1 in production.
_HTTPS_ONLY: bool = os.environ.get("WEB_HTTPS_ONLY", "1").lower() not in ("0", "false", "no")

@asynccontextmanager
async def _app_lifespan(_app):
    """Startup: warn if OAUTH_REDIRECT_BASE looks unstable.

    Migrated from `@app.on_event("startup")` (FastAPI deprecated, scheduled
    for removal in 1.0). Behaviour is unchanged: warn on rotating Quick
    Tunnel hosts, info-log the local dev fallback, info-log a stable base.
    """
    base = _get_oauth_redirect_base()
    if "trycloudflare.com" in base:
        _log.warning(
            "⚠️  OAUTH_REDIRECT_BASE is a rotating Quick Tunnel URL "
            "(%s/auth/callback). Login will break as soon as the tunnel "
            "restarts. Set the OAUTH_REDIRECT_BASE env var to a stable "
            "domain, or switch to a Cloudflare Named Tunnel.",
            base,
        )
    elif base == "http://localhost:8080":
        _log.info(
            "OAuth redirect base: %s (local dev mode — set "
            "OAUTH_REDIRECT_BASE for production)",
            base,
        )
    else:
        _log.info("OAuth redirect base: %s", base)
    yield


app = FastAPI(
    title="小紅 Employee Portal",
    docs_url=None,
    redoc_url=None,
    lifespan=_app_lifespan,
)
app.add_middleware(
    _LazySessionMiddleware,
    max_age=86400 * 7,
    https_only=_HTTPS_ONLY,
)
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


@app.get("/health")
@app.get("/healthz")
async def healthz():
    return {"ok": True}


# 1 req/sec sustained, burst of 10 per source IP. Real LINE traffic is at
# most a handful per second across all users; this still leaves headroom
# for legitimate bursts (e.g. a follow-event storm from a campaign post)
# while capping a single-IP spoofed-signature spray. Cloud Armor in front
# of Cloud Run is the right answer for coordinated cross-IP attacks; this
# is the cheap first-tier guard.
_line_webhook_limiter = _LineWebhookRateLimiter = None  # populated lazily


def _get_line_webhook_limiter():
    global _line_webhook_limiter
    if _line_webhook_limiter is None:
        from agent_core.web_server.rate_limiter import IPRateLimiter
        _line_webhook_limiter = IPRateLimiter(rate=1.0, burst=10.0)
    return _line_webhook_limiter


@app.post("/line/webhook")
async def line_webhook(request: Request):
    from agent_core.web_server.rate_limiter import client_ip

    headers_list = [(k.encode(), v.encode()) for k, v in request.headers.items()]
    ip = client_ip(headers_list, fallback=request.client.host if request.client else "")
    if not _get_line_webhook_limiter().allow(ip):
        _log.warning("[line/webhook] rate-limited %s", ip)
        return JSONResponse({"error": "rate limited"}, status_code=429)

    body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    try:
        from starlette.concurrency import run_in_threadpool

        from agent_core.line_bot import LineWebhookError, handle_line_webhook
        # handle_line_webhook 內部是同步 requests.post（LINE reply API）——
        # 直接在 async handler 跑會卡死單一 event loop、凍住所有並發請求，
        # 跟 api_query/api_command 的 dispatch 一樣丟 threadpool。
        result = await run_in_threadpool(handle_line_webhook, body, signature)
    except LineWebhookError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    return JSONResponse(result)


def _push_command_notification(user: dict, color: str, intent: str, payload: dict) -> None:
    """Send a Telegram notification to 大王 when an employee executes a command.

    Runs in a daemon thread so it never blocks the HTTP response.
    Failures are logged but silently swallowed — the command already succeeded.
    """
    def _send() -> None:
        try:
            from agent_core.telegram import telegram_push
            dept = _DEPT_LABELS.get(color, color)
            ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            # Summarise payload: show keys only to avoid leaking sensitive values in chat
            payload_summary = ", ".join(payload.keys()) if payload else "（無）"
            msg = (
                f"🔔 員工操作通知\n"
                f"部門：{dept}\n"
                f"員工：{user.get('name', '')}（{user.get('email', '')}）\n"
                f"操作：{intent}\n"
                f"參數：{payload_summary}\n"
                f"時間：{ts}"
            )
            telegram_push(msg)
        except Exception as exc:
            _log.warning("Telegram command notification failed: %s", exc)

    try:
        threading.Thread(target=_send, daemon=True).start()
    except Exception as exc:
        _log.warning("Telegram notification thread could not start: %s", exc)


# ── Server-side command confirmation token store ──────────────────────
# Tokens live in process memory (not in the signed cookie / client-held session)
# so they cannot be replayed by presenting an old cookie.
# Each entry: token_str → {intent, payload_fp, expires}
_PENDING_COMMANDS: dict[str, dict] = {}
_CONFIRM_TTL = 300        # 5 minutes to click confirm
_MAX_PAYLOAD_BYTES = 8192  # guard against oversized payloads hitting the store
_MAX_PENDING_COMMANDS = 256  # hard cap on the in-memory token store（健檢 Low）


def _issue_confirm_token(intent: str, payload_fp: str) -> str:
    """Store a one-time challenge token server-side and return it."""
    now = time.monotonic()
    # Evict expired tokens to avoid unbounded growth
    for k in [k for k, v in _PENDING_COMMANDS.items() if v["expires"] < now]:
        _PENDING_COMMANDS.pop(k, None)
    # Hard cap on count（健檢 Low：原本只逐出過期，認證員工可在 5 分鐘窗口內無上限堆積）→
    # 逐出最舊的直到低於上限。
    while len(_PENDING_COMMANDS) >= _MAX_PENDING_COMMANDS:
        oldest = min(_PENDING_COMMANDS, key=lambda k: _PENDING_COMMANDS[k]["expires"])
        _PENDING_COMMANDS.pop(oldest, None)
    token = secrets.token_urlsafe(16)
    _PENDING_COMMANDS[token] = {"intent": intent, "payload_fp": payload_fp, "expires": now + _CONFIRM_TTL}
    return token


def _consume_confirm_token(token: str, intent: str, payload_fp: str) -> bool:
    """Validate and atomically consume a confirmation token.

    Returns True only if the token exists, is unexpired, and matches the
    intent + payload fingerprint.  pop() makes it single-use regardless of
    whether the client holds an old cookie.
    """
    pending = _PENDING_COMMANDS.pop(token, None)
    return bool(
        pending
        and pending["expires"] >= time.monotonic()
        and pending["intent"] == intent
        and pending["payload_fp"] == payload_fp
    )


def _payload_fp(payload: dict) -> str:
    """SHA-256 fingerprint of the payload (first 32 hex chars)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:32]


_DEPT_LABELS = {
    "red":    "總經理辦公室",
    "orange": "業務",
    "yellow": "採購",
    "green":  "樣品開發",
    "blue":   "船務",
    "indigo": "倉庫",
    "purple": "會計",
    "gray":   "生產管理",
    "black":  "出納",
    "white":  "法務",
}

def _get_oauth_redirect_base() -> str:
    """Return the OAuth redirect base URL used in both /auth/login and /auth/callback.

    The redirect_uri sent to Google must exactly match a URI registered in
    Google Cloud Console, so it must be stable (not derived from the incoming
    request host, which rotates every time Cloudflare Quick Tunnel restarts).

    Priority:
      1. OAUTH_REDIRECT_BASE env var (set this in launchd plist for a fixed domain)
      2. var/data/tunnel_url.txt written by cloudflare_tunnel.py on startup
      3. http://localhost:8080   (local dev fallback)

    NOTE: whenever the tunnel URL changes the operator must also update the
    Authorised Redirect URIs in Google Cloud Console to include
    <new_tunnel_url>/auth/callback.
    """
    env = os.environ.get("OAUTH_REDIRECT_BASE", "").rstrip("/")
    if env:
        return env
    try:
        from agent_core.logging_and_paths import DATA_DIR
        tunnel_file = os.path.join(DATA_DIR, "tunnel_url.txt")
        if os.path.exists(tunnel_file):
            with open(tunnel_file, encoding="utf-8") as f:
                url = f.read().strip().rstrip("/")
            if url:
                return url
    except Exception:
        pass
    return "http://localhost:8080"

# BOSS_EMAILS env var（初始大王）+ employee registry 裡 color=red 的都算總經理
_BOSS_EMAILS: set[str] = set(
    os.environ.get("BOSS_EMAILS", "").lower().split(",")
) - {""}


def _is_boss(email: str, employee: dict | None) -> bool:
    """總經理判斷：BOSS_EMAILS env 或 registry color=red。"""
    return email in _BOSS_EMAILS or (employee is not None and employee.get("color") == "red")


def _get_session_user(request: Request) -> dict[str, str] | None:
    return request.session.get("user")


def _safe_next_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return ""
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return ""
    if path.startswith("/auth/"):
        return ""
    return f"{path}?{parsed.query}" if parsed.query else path


def _request_path_with_query(request: Request) -> str:
    query = str(request.url.query or "")
    return f"{request.url.path}?{query}" if query else request.url.path


def _require_login(request: Request) -> dict[str, str]:
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="請先登入")
    # 每次請求對 registry 重驗 color/is_boss —— session cookie 的權限是登入當下
    # 的快照、可存活 7 天。不重驗的話，被降級/移除的員工在 cookie 過期前仍保有
    # 原部門與 admin 權限（無法即時撤權）。以 registry 現值覆寫，讓撤權即時生效。
    # get_employee 走 employee_registry mtime 快取，per-request 讀取成本很低。
    email = user.get("email", "")
    employee = get_employee(email)
    boss = _is_boss(email, employee)
    if not employee and not boss:
        request.session.clear()
        raise HTTPException(status_code=401, detail="帳號已不在員工名單，請重新登入。")
    fresh = dict(user)
    fresh["color"] = employee["color"] if employee else "red"
    fresh["is_boss"] = str(boss)
    request.session["user"] = fresh
    return fresh


def _session_gate_or_none(user: dict[str, str]):
    """Owner session 主控台 gate for web sessions（登記 + 暫停判斷）。

    每個部門 API 動作都 touch 一次 registry（session_id=`web:<email>`），讓大王
    能在 Telegram 用 list_sessions 綜覽並 pause/resume 網頁員工的對話。boss（大王／
    color=red）視為 owner、永不被擋（與 pause_session 拒絕暫停 owner 對稱）。

    回傳：session 被暫停 → 一個 423 JSONResponse（caller 直接 return）；否則 None。
    registry 出錯一律 fail-open（回 None），不擋正常查詢。
    """
    email = str(user.get("email") or "")
    if not email:
        return None
    is_boss = str(user.get("is_boss")) == "True"
    try:
        from agent_core import session_registry
        sid = f"web:{email}"
        sess = session_registry.touch_session(sid, channel="web", actor={
            "name": user.get("name") or "",
            "email": email,
            "color": user.get("color") or "",
            "is_owner": "true" if is_boss else "false",
        })
        if sess.get("reset_pending"):
            # web 部門查詢每 request 無狀態，reset 無上下文可清，消旗標即可。
            session_registry.consume_reset(sid)
        if sess.get("status") == "paused" and not is_boss:
            return JSONResponse(
                {"error": session_registry.paused_notice(sess.get("paused_reason") or "")},
                status_code=423,
            )
    except Exception:
        return None
    return None


_CSRF_SESSION_KEY = "csrf_token"


def _get_csrf_token(request: Request) -> str:
    """Return the per-session CSRF token, creating one on first read.

    Stored in the signed session cookie, so a cross-site attacker cannot read
    or forge it. Templates render it into forms and the JS chat client reads
    it from a <meta name="csrf-token"> tag and replays it in X-CSRF-Token.
    """
    token = request.session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


def _require_csrf(request: Request, supplied: str) -> None:
    expected = request.session.get(_CSRF_SESSION_KEY, "")
    if not expected or not hmac.compare_digest(expected, supplied or ""):
        raise HTTPException(status_code=403, detail="CSRF token 無效，請重新整理頁面後再試")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.exception_handler(HTTPException)
async def _friendly_http_exception_handler(request: Request, exc: HTTPException):
    if (
        exc.status_code == 401
        and str(exc.detail) == "請先登入"
        and request.method in {"GET", "HEAD"}
    ):
        next_path = quote(_request_path_with_query(request), safe="/")
        return RedirectResponse(f"/?next={next_path}", status_code=303)
    return await http_exception_handler(request, exc)


# ── Auth ──────────────────────────────────────────────────────────────

@app.get("/auth/login")
async def auth_login(request: Request, next: str = ""):
    state = generate_state()
    redirect_uri = f"{_get_oauth_redirect_base()}/auth/callback"
    # Store both values so /auth/callback reuses the exact same redirect_uri.
    # If the tunnel URL rotates between login and callback the session value
    # keeps the exchange consistent; Google requires an exact match.
    request.session["oauth_state"] = state
    request.session["oauth_redirect_uri"] = redirect_uri
    next_path = _safe_next_path(next)
    if next_path:
        request.session["oauth_next"] = next_path
    else:
        request.session.pop("oauth_next", None)
    return RedirectResponse(build_login_url(redirect_uri, state))


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", error: str = "", state: str = ""):
    if error or not code:
        safe_err = _html.escape(error or "未收到 code")
        return HTMLResponse(f"<h2>登入失敗：{safe_err}</h2>", status_code=400)
    # CSRF: verify state matches what we stored in session
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        return HTMLResponse("<h2>❌ OAuth state 驗證失敗（可能是 CSRF 攻擊）</h2>", status_code=400)
    # Reuse the redirect_uri that was sent to Google at login time — Google requires
    # the token-exchange URI to exactly match the authorisation-request URI.
    redirect_uri = request.session.pop("oauth_redirect_uri", f"{_get_oauth_redirect_base()}/auth/callback")
    try:
        info = await exchange_code(code, redirect_uri)
    except Exception as exc:
        safe_exc = _html.escape(str(exc))
        return HTMLResponse(f"<h2>OAuth 失敗：{safe_exc}</h2>", status_code=500)

    email = (info.get("email") or "").lower().strip()
    name = info.get("name") or email

    employee = get_employee(email)
    boss = _is_boss(email, employee)

    if not employee and not boss:
        return HTMLResponse(
            "<h2>❌ 此帳號尚未登記</h2>"
            "<p>請聯絡管理員（大王）將您加入員工名單。</p>",
            status_code=403,
        )

    color = employee["color"] if employee else "red"
    request.session["user"] = {
        "email": email,
        "name": name,
        "color": color,
        "is_boss": str(boss),
    }
    next_path = _safe_next_path(request.session.pop("oauth_next", "")) or "/"
    return RedirectResponse(next_path)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


# ── Root ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, next: str = ""):
    user = _get_session_user(request)
    if not user:
        next_path = _safe_next_path(next)
        login_url = "/auth/login"
        if next_path:
            login_url = f"{login_url}?next={quote(next_path, safe='')}"
        return templates.TemplateResponse(request, "login.html", {
            "login_url": login_url,
            "next_path": next_path,
        })
    color = user["color"]
    if color == "red":
        return templates.TemplateResponse(request, "boss_home.html", {
            "user": user, "depts": _DEPT_LABELS,
            "csrf_token": _get_csrf_token(request),
        })
    return RedirectResponse(f"/dept/{color}")


# ── Department pages ──────────────────────────────────────────────────

@app.get("/dept/{color}", response_class=HTMLResponse)
async def dept_page(request: Request, color: str):
    user = _require_login(request)
    if user["color"] != color and user.get("is_boss") != "True":
        raise HTTPException(status_code=403, detail="無權存取此部門")
    # 總經理辦公室 → boss_home（可查所有部門）
    if color == "red":
        return templates.TemplateResponse(request, "boss_home.html", {
            "user": user, "depts": {k: v for k, v in _DEPT_LABELS.items() if k != "red"},
            "csrf_token": _get_csrf_token(request),
        })
    label = _DEPT_LABELS.get(color, color)
    # Owner session 主控台：開頁時取出並清空這位員工的待讀廣播（broadcast_message
    # 對 web session 只排佇列、無伺服器推播）。drain 失敗不擋開頁（fail-open）。
    broadcasts: list = []
    try:
        from agent_core import session_registry
        broadcasts = session_registry.drain_broadcasts(f"web:{user.get('email', '')}")
    except Exception:
        broadcasts = []
    return templates.TemplateResponse(request, "dept_chat.html", {
        "user": user,
        "color": color,
        "label": label,
        "csrf_token": _get_csrf_token(request),
        "broadcasts": broadcasts,
    })


# ── API — query (read-only) ───────────────────────────────────────────

@app.post("/api/dept/{color}/query")
async def api_query(request: Request, color: str):
    user = _require_login(request)
    _require_csrf(request, request.headers.get("x-csrf-token", ""))
    _gate = _session_gate_or_none(user)
    if _gate is not None:
        return _gate
    if user["color"] != color and user.get("is_boss") != "True":
        raise HTTPException(status_code=403, detail="無權存取此部門")
    # red has no registered agent — boss users query other depts directly.
    if color == "red":
        return JSONResponse({"error": "總經理辦公室無專屬查詢端點，請選擇其他部門"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "請求 body 不是合法的 JSON"}, status_code=400)
    intent = str(body.get("intent", "")).strip()
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        return JSONResponse({"error": "payload 必須是 JSON 物件（{...}）"}, status_code=400)

    if not intent.startswith("query."):
        return JSONResponse({"error": "只允許 query.* intent"}, status_code=400)

    try:
        from starlette.concurrency import run_in_threadpool

        from agent_core.agents import AgentRequest, PermissionDenied
        from agent_core.agents.permission_matrix import Agent
        from agent_core.agents.wire import get_default_registry

        def _dispatch():
            # dispatch 跑阻塞的 Gmail/Drive/Gemini I/O（可達數分鐘）。offload 到
            # threadpool，否則一個慢查詢卡死整個單一 web event loop、凍住所有其他
            # 並發請求（css 健檢 Medium）。registry 用 process-wide 單例（#182），
            # 免每 request 重建 9 個部門 agent。
            registry, middleware = get_default_registry()
            return middleware.dispatch(AgentRequest(
                caller=Agent("red"),  # web portal 以大王身份代理員工查詢
                target=Agent(color), intent=intent, payload=payload,
            ))

        result = await run_in_threadpool(_dispatch)
        return JSONResponse({"ok": True, "result": result})
    except PermissionDenied as e:
        return JSONResponse({"error": f"🔒 {e}"}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"執行失敗：{e}"}, status_code=500)


# ── API — ask (自然語言查詢，唯讀) ────────────────────────────────────

_MAX_QUESTION_CHARS = 2000

# 員工 NL 查詢 per-user 限流（key=email 的 token bucket）。burst 應付一輪
# 密集追問，長期速率擋腳本灌爆；boss 不限（owner 永不被擋的慣例）。這是
# 防禦縱深 — 成本的真兜底是 cost_tracker 的月上限告警。
_ask_rate_limiter = None


def _get_ask_rate_limiter():
    global _ask_rate_limiter
    if _ask_rate_limiter is None:
        from agent_core.env_utils import env_float
        from agent_core.web_server.rate_limiter import IPRateLimiter
        _ask_rate_limiter = IPRateLimiter(
            rate=env_float("RED_EMPLOYEE_NLP_RATE_PER_S", 1.0 / 15.0),
            burst=env_float("RED_EMPLOYEE_NLP_BURST", 8.0),
        )
    return _ask_rate_limiter


@app.post("/api/dept/{color}/ask")
async def api_ask(request: Request, color: str):
    """員工自由文字查詢 → dept_nlp_query 引擎（只走唯讀 query.* + ACL RAG）。

    與 api_query 不同：dispatch 的 caller 是「頁面部門色」而非 red ——
    NL 路徑的工具由 LLM 規劃，必須用最小權限身分跑（QUERY_MATRIX +
    rag ACL 都以員工色把關）；boss 開部門頁時也以該部門身分查（least
    privilege，視角跟員工一致）。
    """
    user = _require_login(request)
    _require_csrf(request, request.headers.get("x-csrf-token", ""))
    _gate = _session_gate_or_none(user)
    if _gate is not None:
        return _gate
    if user["color"] != color and user.get("is_boss") != "True":
        raise HTTPException(status_code=403, detail="無權存取此部門")
    if color == "red":
        return JSONResponse({"error": "總經理辦公室請選擇一個部門後再查詢"}, status_code=400)
    if user.get("is_boss") != "True" and not _get_ask_rate_limiter().allow(
        str(user.get("email") or "")
    ):
        return JSONResponse(
            {"error": "查詢太頻繁，請稍候幾秒再問（限流保護）"}, status_code=429,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "請求 body 不是合法的 JSON"}, status_code=400)
    question = str(body.get("question", "")).strip()
    if not question:
        return JSONResponse({"error": "question 不能為空"}, status_code=400)
    if len(question) > _MAX_QUESTION_CHARS:
        return JSONResponse(
            {"error": f"問題太長（上限 {_MAX_QUESTION_CHARS} 字）"}, status_code=400,
        )

    try:
        from starlette.concurrency import run_in_threadpool

        from agent_core.dept_nlp_query import answer_dept_question

        def _ask():
            # NL pipeline 內含 2 次 Gemini + 數個查詢 I/O（可達分鐘級）——
            # 跟 api_query 一樣丟 threadpool，別凍住單一 event loop。
            return answer_dept_question(
                color,
                question,
                actor_name=str(user.get("name") or ""),
                channel="web",
            )

        answer = await run_in_threadpool(_ask)
        return JSONResponse({"ok": True, "result": {"text": answer}})
    except Exception as e:
        return JSONResponse({"error": f"執行失敗：{e}"}, status_code=500)


# ── API — command (write, requires confirm token) ─────────────────────

@app.post("/api/dept/{color}/command")
async def api_command(request: Request, color: str):
    user = _require_login(request)
    _require_csrf(request, request.headers.get("x-csrf-token", ""))
    _gate = _session_gate_or_none(user)
    if _gate is not None:
        return _gate
    if user["color"] != color and user.get("is_boss") != "True":
        raise HTTPException(status_code=403, detail="無權存取此部門")
    # red has no registered agent — boss users command other depts directly.
    if color == "red":
        return JSONResponse({"error": "總經理辦公室無專屬指令端點，請選擇其他部門"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "請求 body 不是合法的 JSON"}, status_code=400)
    intent = str(body.get("intent", "")).strip()
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        return JSONResponse({"error": "payload 必須是 JSON 物件（{...}）"}, status_code=400)

    if not intent.startswith("command."):
        return JSONResponse({"error": "此端點只處理 command.* intent"}, status_code=400)

    # Enforce payload size limit before touching the token store
    payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode())
    if payload_bytes > _MAX_PAYLOAD_BYTES:
        return JSONResponse(
            {"error": f"payload 超過大小限制（上限 {_MAX_PAYLOAD_BYTES} bytes）"},
            status_code=400,
        )

    confirm_token = str(body.get("confirm_token", "")).strip()
    fp = _payload_fp(payload)

    if not confirm_token:
        # First request: issue a one-time challenge token stored in process memory.
        # The client must echo it back with the same intent + payload to prove it
        # received the challenge.  A client that forges confirmed=true or replays
        # an old cookie gets a 400 — the token lives server-side, not in the cookie.
        token = _issue_confirm_token(intent, fp)
        return JSONResponse({
            "ok": False,
            "requires_confirm": True,
            "message": f"即將執行 `{intent}`，請確認後送出。",
            "confirm_token": token,
            "intent": intent,
            "payload": payload,
        })

    # Second request: validate and consume the server-side token.
    if not _consume_confirm_token(confirm_token, intent, fp):
        return JSONResponse({"error": "確認 token 無效或已過期，請重新操作"}, status_code=400)

    try:
        from starlette.concurrency import run_in_threadpool

        from agent_core.agents import AgentRequest, PermissionDenied
        from agent_core.agents.permission_matrix import Agent
        from agent_core.agents.wire import get_default_registry

        def _dispatch():
            # 阻塞的 Gmail/Drive/Gemini I/O（可達數分鐘）→ offload，否則凍住整個 web
            # event loop（css 健檢 Medium）。registry 用單例（#182）免每 request 重建。
            registry, middleware = get_default_registry()
            return middleware.dispatch(AgentRequest(
                caller=Agent("red"), target=Agent(color),
                intent=intent, payload=payload,
            ))

        result = await run_in_threadpool(_dispatch)
        # push 通知也走網路 I/O → 同樣 offload，別擋 loop
        await run_in_threadpool(_push_command_notification, user, color, intent, payload)
        return JSONResponse({"ok": True, "result": result})
    except PermissionDenied as e:
        return JSONResponse({"error": f"🔒 {e}"}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"執行失敗：{e}"}, status_code=500)


# ── API — Edge Agent devices ─────────────────────────────────────────

def _require_edge_api_token(request: Request) -> None:
    expected = os.environ.get("RED_EDGE_AGENT_TOKEN", "").strip()
    if not expected:
        if os.environ.get("RED_EDGE_AGENT_ALLOW_INSECURE", "").lower() in {"1", "true", "yes"}:
            return
        raise HTTPException(status_code=503, detail="Edge Agent API token not configured")
    provided = request.headers.get("x-edge-token", "").strip()
    auth = request.headers.get("authorization", "").strip()
    if not provided and auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid Edge Agent token")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="請求 body 不是合法的 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必須是 JSON object")
    return body


@app.post("/api/edge/register")
async def api_edge_register(request: Request):
    _require_edge_api_token(request)
    body = await _json_body(request)
    try:
        from agent_core import edge_tasks
        return JSONResponse(edge_tasks.register_device(
            device_id=str(body.get("device_id") or ""),
            department=str(body.get("department") or ""),
            employee_email=str(body.get("employee_email") or ""),
            device_name=str(body.get("device_name") or ""),
            capabilities=body.get("capabilities") or [],
        ))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"register failed: {exc}"}, status_code=500)


@app.post("/api/edge/poll")
async def api_edge_poll(request: Request):
    _require_edge_api_token(request)
    body = await _json_body(request)
    try:
        from agent_core import edge_tasks
        return JSONResponse(edge_tasks.claim_next_task(
            device_id=str(body.get("device_id") or ""),
            department=str(body.get("department") or ""),
            employee_email=str(body.get("employee_email") or ""),
        ))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"poll failed: {exc}"}, status_code=500)


@app.post("/api/edge/tasks/{task_id}/status")
async def api_edge_task_status(request: Request, task_id: str):
    _require_edge_api_token(request)
    body = await _json_body(request)
    try:
        from agent_core import edge_tasks
        return JSONResponse(edge_tasks.update_task_status(
            task_id=task_id,
            device_id=str(body.get("device_id") or ""),
            status=str(body.get("status") or ""),
            message=str(body.get("message") or ""),
            result=body.get("result") if isinstance(body.get("result"), dict) else None,
        ))
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"status update failed: {exc}"}, status_code=500)


# ── Admin — employee management ───────────────────────────────────────

@app.get("/admin/employees", response_class=HTMLResponse)
async def admin_employees_page(request: Request):
    user = _require_login(request)
    if user.get("is_boss") != "True":
        raise HTTPException(status_code=403, detail="僅限管理員")
    employees = list_employees()
    return templates.TemplateResponse(request, "admin_employees.html", {
        "user": user,
        "employees": employees,
        "depts": _DEPT_LABELS,
        "csrf_token": _get_csrf_token(request),
    })


@app.post("/admin/employees")
async def admin_add_employee(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    color: str = Form(...),
    line_user_id: str = Form(""),
    telegram_user_id: str = Form(""),
    csrf_token: str = Form(""),
):
    user = _require_login(request)
    _require_csrf(request, csrf_token)
    if user.get("is_boss") != "True":
        raise HTTPException(status_code=403, detail="僅限管理員")
    try:
        register_employee(
            email,
            name,
            color,
            line_user_id=line_user_id,
            telegram_user_id=telegram_user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse("/admin/employees", status_code=303)


@app.get("/admin/telegram", response_class=HTMLResponse)
async def admin_telegram_page(request: Request, limit: int = 50):
    user = _require_login(request)
    if user.get("is_boss") != "True":
        raise HTTPException(status_code=403, detail="僅限管理員")
    limit = max(1, min(int(limit or 50), 200))
    # 這頁的重活（audit 檔尾讀 / DB 查詢、binding 診斷的 registry/keyring I/O）
    # 都是同步阻塞呼叫 —— 跟 api_query/api_command 一樣丟 threadpool，別佔住
    # 單一 web event loop。
    from starlette.concurrency import run_in_threadpool
    try:
        from agent_core.telegram_audit import audit_file_path, read_recent_events
        audit_path = audit_file_path()
        audit_events = list(reversed(
            await run_in_threadpool(read_recent_events, limit=limit)
        ))
    except Exception as exc:
        audit_path = ""
        audit_events = []
        _log.warning("Failed to read Telegram audit log: %s", exc)
    try:
        from agent_core.telegram_policy import telegram_command_policy_rows
        policy_rows = await run_in_threadpool(telegram_command_policy_rows)
    except Exception as exc:
        policy_rows = []
        _log.warning("Failed to build Telegram policy rows: %s", exc)
    try:
        from agent_core.telegram import _get_telegram_chat_id
        from agent_core.telegram_agent_config import telegram_binding_diagnostics

        def _build_binding_diagnostics():
            return telegram_binding_diagnostics(_get_telegram_chat_id())

        binding_diagnostics = await run_in_threadpool(_build_binding_diagnostics)
    except Exception as exc:
        binding_diagnostics = {"summary": "", "bindings": [], "actors": [], "warnings": []}
        _log.warning("Failed to build Telegram binding diagnostics: %s", exc)
    return templates.TemplateResponse(request, "admin_telegram.html", {
        "user": user,
        "audit_events": audit_events,
        "audit_path": audit_path,
        "policy_rows": policy_rows,
        "binding_diagnostics": binding_diagnostics,
        "limit": limit,
        "csrf_token": _get_csrf_token(request),
    })
