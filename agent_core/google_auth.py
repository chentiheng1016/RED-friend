"""Google OAuth credentials + service discovery cache.

Self-contained: imports
only stdlib and google-auth libraries (lazily). Paths are computed
relative to the package parent so agent.py and agent_core/ agree on
token.json / credentials.json location.
"""
import os
import sys
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from agent_core.logging_and_paths import (
    TOKEN_FILE,
    CREDENTIALS_FILE,
    _atomic_write_text,
    install_sdk_log_filters,
)
from agent_core.secret_provider import get_secret
from agent_core.env_utils import env_int

# google-auth 2.55+ fires an unconditional Regional Access Boundary lookup on
# every service-account token refresh; RED's SA can't read its own
# allowedLocations, so each refresh logs a benign 403 WARNING (see
# install_sdk_log_filters). This module is the single chokepoint for every
# Google credential, so installing the suppression filter at import time puts it
# in place before the first refresh across the whole daemon fleet.
install_sdk_log_filters()

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PKG_DIR)
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/calendar'
]

def _detect_daemon_mode() -> bool:
    """是否 daemon/headless：禁互動式 OAuth（在無 TTY 的 launchd 下 run_local_server
    會卡死到看門狗 exit 75，如 mailcheck）。顯式 AGENT_DAEMON_MODE=1 優先；否則以
    『stdin 非 TTY』判定 headless —— 涵蓋 plist 漏設 AGENT_DAEMON_MODE 的 cron daemon。
    互動 REPL（agent.py）stdin 是 TTY → False，仍可前景授權。"""
    if os.environ.get("AGENT_DAEMON_MODE") == "1":
        return True
    try:
        return not sys.stdin.isatty()
    except Exception:  # noqa: BLE001  無 stdin（典型 daemon）→ 視為 headless
        return True


_IS_DAEMON_MODE = _detect_daemon_mode()

_service_cache = {}
_cached_creds = None
_CREDS_REFRESH_BUFFER = timedelta(minutes=5)
_token_lock = threading.Lock()
# Module-level latch so a single daemon run doesn't spam Telegram on every
# drive/Gmail listing call that hits the daemon-mode OAuth wall.
_daemon_oauth_alert_sent = False

_Credentials = None
_InstalledAppFlow = None
_google_build = None
_MediaFileUpload = None
_AuthorizedHttp = None

# Hard wall-clock timeout per Google API RPC. Without this, httplib2.Http()
# defaults to socket timeout=None, so a half-closed TCP socket (CLOSE_WAIT)
# can wedge a daemon for hours — observed in rag_sync hanging 1h+ on
# Drive list calls with 0% CPU. 120s is well above p99 for any single
# Drive/Gmail/Calendar RPC (<5s typical) but bounds the worst case.
_GOOGLE_API_TIMEOUT_S = 120


def _get_google_oauth_classes():
    global _Credentials, _InstalledAppFlow
    if _Credentials is None:
        from google.oauth2.credentials import Credentials as _Creds
        _Credentials = _Creds
    if _InstalledAppFlow is None:
        from google_auth_oauthlib.flow import InstalledAppFlow as _Flow
        _InstalledAppFlow = _Flow
    return _Credentials, _InstalledAppFlow


def _get_google_build():
    global _google_build
    if _google_build is None:
        from googleapiclient.discovery import build as _build
        _google_build = _build
    return _google_build


def _get_media_file_upload():
    global _MediaFileUpload
    if _MediaFileUpload is None:
        from googleapiclient.http import MediaFileUpload as _Upload
        _MediaFileUpload = _Upload
    return _MediaFileUpload


def _get_authorized_http():
    global _AuthorizedHttp
    if _AuthorizedHttp is None:
        from google_auth_httplib2 import AuthorizedHttp as _Auth
        _AuthorizedHttp = _Auth
    return _AuthorizedHttp


def _notify_daemon_oauth_blocked() -> None:
    """Push a Telegram alert (with email fallback) when daemon mode hits the
    interactive-OAuth wall — otherwise the 03:00 daily silently no-ops for
    every drive + Gmail and the user has no idea until they check logs.

    Latched per process so a single daemon run doesn't spam every drive.
    Failures here are swallowed: we still want the RuntimeError to propagate.
    """
    global _daemon_oauth_alert_sent
    if _daemon_oauth_alert_sent:
        return
    _daemon_oauth_alert_sent = True

    daemon_name = os.environ.get("XPC_SERVICE_NAME") or os.path.basename(
        os.environ.get("_", "") or "rag_sync"
    )
    msg = (
        "🚨 *Google OAuth 失效* — daemon 模式無法跳互動式授權\n"
        f"daemon: `{daemon_name}`\n"
        f"token: `{TOKEN_FILE}`\n"
        "影響: Drive 列表/同步、Gmail 抓取全部 fail，daily 變 no-op\n"
        "修復: 前景跑 `python -c 'from agent_core.google_auth import "
        "get_google_credentials; get_google_credentials()'` 完成 OAuth"
    )
    telegram_err = ""
    try:
        from agent_core.telegram import telegram_push
        result = telegram_push(msg)
        if "✅" in str(result):
            return
        telegram_err = str(result)[:200]
    except Exception as e:
        telegram_err = f"{type(e).__name__}: {e}"

    # Email fallback — same chain as alert_pusher.
    try:
        from agent_core.daemon_helpers import notify
        notify(
            subject="🚨 Google OAuth 失效 (daemon mode blocked)",
            body=msg,
            task_name="google_auth",
        )
    except Exception as e:
        # Last resort: just write to stderr so it shows in daemon log.
        print(
            "[google_auth] ⚠️ daemon-OAuth alert dispatch failed: "
            f"telegram={telegram_err}; email={type(e).__name__}: {e}; msg={msg}"
        )


# OAuth token refresh 重試：吸收暫時性故障（網路 blip / token 端點 5xx）。一次 refresh
# 失敗就丟掉 creds，會讓 headless daemon 落到 fail-fast 守衛 → 推一則「請重新授權」假告警
# + 本輪失敗；但暫時性 blip 下一秒就好。故先重試數次再放棄。真正失效的 refresh token
# （invalid_grant）重試無益 → 立刻放棄。
_REFRESH_MAX_ATTEMPTS = 3
_REFRESH_RETRY_BACKOFF_S = 2.0

# TOTAL wall-clock bound on one token refresh. requests' timeout= (like
# httplib2's — see _GOOGLE_API_TIMEOUT_S) bounds each recv(), NOT the whole
# request: a trickling / half-dead token endpoint keeps every recv under the
# read timeout while the overall refresh stalls for many minutes at ~0% CPU.
# The refresh is the ONE Google call get_service() makes on the caller thread
# with no request-level backstop — Drive/Gmail/Chat .execute() all route through
# a wall-clock guard (_DRIVE_RPC_TIMEOUT_S / run_rpc_with_timeout), but the
# refresh in _execute_drive_request's request_factory() runs unwrapped on the
# MAIN thread. rag_sync 2026-07-06 wedged 30min+ there. 60s is far above a
# healthy refresh (<5s); reuses #221's shared run_rpc_with_timeout guard.
_REFRESH_ATTEMPT_TIMEOUT_S = env_int("RED_OAUTH_REFRESH_TIMEOUT_S", 60, min_value=0)


# OAuth 2.0（RFC 6749 §5.2）錯誤碼裡代表「憑證/用戶端本身永久失效」的那幾個。
# 只有這些才構成永久失效的證據 —— 見 _refresh_failure_is_permanent 對預設值的說明。
_PERMANENT_REFRESH_MARKS = (
    "invalid_grant",       # refresh token 被撤銷/過期 —— 真失效裡最常見的一種
    "invalid_client",      # client id/secret 錯，或 OAuth client 被刪
    "unauthorized_client",
    "invalid_scope",
    "access_denied",
)


class GoogleAuthTransientError(RuntimeError):
    """Token refresh 失敗，但成因是**暫時性**的（DNS/連線/5xx/涓流逾時），憑證本身沒失效。

    為什麼需要自己的型別：這兩件事以前都被摺疊成「refresh 回 False」，呼叫端一律
    當成「需要人重新互動授權」→ headless daemon 推一則假的「請重新登入」告警、整輪
    失敗。同一個誤判已經發生兩次：2026-07-15 training_video_watch 卡到隔天（當時只
    在那一支 daemon 貼字串比對的 OK 繃，根因沒動）、2026-08-27 04:00 一次 DNS 斷線
    讓 internal_ingest_daily exit 1，mailcheck / alert_check / email_ingest 同時噴
    數百行「重新進行 OAuth 授權」，而 token 從頭到尾都是好的。

    繼承 RuntimeError：既有 `except RuntimeError` / `except Exception` 的呼叫端行為
    不變。訊息內含原始例外全文，故 daemon_helpers.is_transient_network_error() 對它
    同樣回 True —— 被 retry_on_transient_network() 包住的呼叫會自動退避重試。
    """


def _refresh_failure_is_permanent(exc: BaseException) -> bool:
    """這次 refresh 失敗是不是「憑證真的失效、非得人來重新授權不可」？

    **預設回 False（＝暫時性）**，只有拿到明確證據才回 True。這個方向是刻意選的，
    因為兩種誤判的代價差很多：

      誤判成永久 → 推一則假的「請重新登入」告警 + 丟掉一份好 creds + 整輪失敗。
                   2026-07-15（training_video_watch 卡到隔天）、2026-08-27 04:00
                   （一次 DNS 斷線讓 internal_ingest_daily exit 1、三支 daemon 噴
                   數百行「重新進行 OAuth 授權」）都是這樣來的。
      誤判成暫時 → 本輪照樣失敗、照樣觸發 daemon 既有的「排程任務失敗」告警，
                   只是不會冤枉憑證。不會靜默。

    判定順序：
      1. 訊息帶 RFC 6749 的永久錯誤碼 → 永久（最強的證據）。
      2. RefreshError：端點有回應而且拒絕了這份憑證 → 是憑證問題的證據，可以推翻
         預設；除非它同時長得像伺服端暫時故障（5xx / 逾時）。
      3. 其餘一切（TransportError＝根本沒跟端點講到話、DNS、逾時、涓流、不認得的
         例外）→ 維持暫時性預設。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(mark in text for mark in _PERMANENT_REFRESH_MARKS):
        return True
    try:
        from google.auth.exceptions import RefreshError
    except Exception:  # noqa: BLE001  google-auth 缺席（精簡環境）→ 維持暫時性預設
        return False
    if isinstance(exc, RefreshError):
        return not _looks_like_transient_network(exc)
    return False


def _looks_like_transient_network(exc: BaseException) -> bool:
    """借用 daemon_helpers 那份唯一的網路暫時性分類器（型別 + 訊息 + HTTP 狀態）。

    lazy import：google_auth 是艦隊每支 daemon 都會載的模組，不在 import 時把
    daemon_helpers（faulthandler/fcntl）拖進最精簡的那幾支。
    """
    if isinstance(exc, RpcWallClockTimeout):
        # 涓流/半死連線：是網路病不是憑證失效（2026-07-06 rag_sync 卡死那一種）。
        return True
    try:
        from agent_core.daemon_helpers import is_transient_network_error
    except Exception:  # noqa: BLE001
        return False
    return is_transient_network_error(exc)


def _refresh_creds_with_retry(creds) -> bool:
    """就地 refresh creds，吸收暫時性失敗。

    結局有**三種**（不是兩種 —— 摺疊成兩種正是上面那兩次假告警的成因）：

      - 成功        → 回 True
      - 暫時性失敗  → raise GoogleAuthTransientError（DNS/連線/5xx/涓流逾時）
      - 永久性失效  → 回 False；呼叫端丟棄 creds → fail-fast / 互動授權

    暫時性走 raise 而不是三值回傳，是因為呼叫端寫的是
    ``if not _refresh_creds_with_retry(creds)`` —— 改回字串的話 "transient" 是
    truthy，會靜默走進「憑證還好」的分支。這正是本輪在抓的 bug 家族，不要自己再種一個。
    """
    # 唯一 use-site：延後到實際刷 token 才 import（連帶拉 requests/cryptography
    # ~27MB RSS），讓只用 get_service() 而不刷 token 的精簡 daemon 不付這份常駐。
    from google.auth.transport.requests import Request
    last_exc = None
    for attempt in range(_REFRESH_MAX_ATTEMPTS):
        try:
            # 整體 wall-clock 上限：涓流/半死連線讓 per-recv timeout 永不觸發，這裡
            # 兜住整個 refresh（見上方常數註解）。復用 #221 的共用護欄——逾時會清 service
            # cache（半開連線不再被沿用）並 raise RpcWallClockTimeout。
            run_rpc_with_timeout(
                _REFRESH_ATTEMPT_TIMEOUT_S,
                "oauth_refresh",
                lambda: creds.refresh(Request()),
            )
            return True
        except RpcWallClockTimeout as exc:
            # 整體逾時＝被遺棄的 worker thread 仍在 mutate 這個 creds；再起一個會與它
            # 撞非 thread-safe 的 creds/requests（同 drive_sync 不重試 wall-clock 逾時
            # 的理由）→ 本輪放棄、讓呼叫端 fail-fast。60s 逾時只會被真正的多分鐘涓流
            # 觸發，短暫 blip 早就完成、不會走到這。
            last_exc = exc
            print(
                f"[系統日誌] ⚠️ Token 刷新逾時（>{_REFRESH_ATTEMPT_TIMEOUT_S}s，疑似 token"
                f" 端點涓流/半死連線），放棄本輪 refresh",
                flush=True,
            )
            break
        except Exception as exc:  # noqa: BLE001  暫時性錯誤可安全重試、永久失效立刻放棄
            last_exc = exc
            if _refresh_failure_is_permanent(exc):
                break  # refresh token 已撤銷/過期或 client 設定錯，重試無益
            if attempt < _REFRESH_MAX_ATTEMPTS - 1:
                # 重試前留一行 — 先前整段靜默，害 2026-07-06 卡死事後難定位。
                print(
                    f"[系統日誌] Token 刷新第 {attempt + 1}/{_REFRESH_MAX_ATTEMPTS} 次未成功"
                    f"，{_REFRESH_RETRY_BACKOFF_S}s 後重試"
                    f"（{type(exc).__name__}: {str(exc)[:140]}）",
                    flush=True,
                )
                time.sleep(_REFRESH_RETRY_BACKOFF_S)
    if last_exc is not None and not _refresh_failure_is_permanent(last_exc):
        # 暫時性：**不是**憑證失效。不回 False（那會讓呼叫端丟掉好 creds、推假告警），
        # 改拋專屬例外讓本輪誠實失敗、下輪自然重試。
        print(
            f"[系統日誌] ⚠️ Token 刷新遇暫時性網路故障（{_REFRESH_MAX_ATTEMPTS} 次嘗試內未成功），"
            f"本輪放棄、下輪再試；憑證本身有效、**不需**重新授權："
            f"{type(last_exc).__name__}: {str(last_exc)[:200]}",
            flush=True,
        )
        raise GoogleAuthTransientError(
            f"Google OAuth token 刷新遇暫時性網路故障（{_REFRESH_MAX_ATTEMPTS} 次嘗試內未成功）："
            f"{type(last_exc).__name__}: {last_exc}"
        ) from last_exc
    print(
        f"[系統日誌] ⚠️ Token 刷新失敗（憑證已失效、重試無益），"
        f"需重新進行 OAuth 授權：{last_exc}",
        flush=True,
    )
    return False


def get_google_credentials():
    # daemon 模式撞到互動式 OAuth 牆時，要在「放掉 _token_lock 之後」才通知 + raise：
    # _notify_daemon_oauth_blocked() 的 email fallback 會 send_gmail → get_service →
    # 再進 get_google_credentials()，若還握著這把非重入鎖就會自我死鎖（同一 thread 等
    # 自己手上的鎖），整輪卡到 run_with_deadline 看門狗 1200s 才被砍（exit 75）。
    daemon_oauth_blocked = False
    with _token_lock:
        Credentials, InstalledAppFlow = _get_google_oauth_classes()
        creds = None
        token_json = get_secret(
            "google-oauth-token-json",
            env_names=("GOOGLE_OAUTH_TOKEN_JSON", "RED_GOOGLE_OAUTH_TOKEN_JSON"),
            keyring_service="xiaohong-agent",
            keyring_name="google-oauth-token-json",
            strip=False,
        ).value
        if token_json:
            try:
                creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
            except Exception as exc:
                raise RuntimeError("GOOGLE_OAUTH_TOKEN_JSON 格式錯誤，無法載入 Google OAuth token") from exc
        elif os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except Exception as exc:  # noqa: BLE001 - torn/損毀 token 檔
                # token.json 損毀（例如舊版非 atomic 直寫被斷電/併發撞爛）：
                # 當作無 token，往下走 refresh / 互動授權流程重建，而不是
                # 讓整艦隊每次啟動都在這裡炸 JSONDecodeError。
                print(f"[google_auth] ⚠️ token.json 無法解析（{exc}），"
                      "視為無 token 重新授權")
                creds = None
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                # 回 False ＝憑證**永久失效**（撤銷/過期/client 設定錯）才丟掉 creds。
                # 暫時性網路故障不會走到這裡：_refresh_creds_with_retry 直接拋
                # GoogleAuthTransientError 往上，避免一次 DNS blip 就被當成「請重新
                # 登入」而推假告警（2026-07-15 / 2026-08-27 兩次事故）。
                if not _refresh_creds_with_retry(creds):
                    creds = None
            if creds is None:
                if not os.path.exists(CREDENTIALS_FILE):
                    raise FileNotFoundError(
                        f"找不到 OAuth client 設定檔：{CREDENTIALS_FILE}"
                    )
                if _IS_DAEMON_MODE:
                    # 延到鎖外再 notify + raise（見函式頂端註解）。
                    daemon_oauth_blocked = True
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                    creds = flow.run_local_server(port=0)
            if not daemon_oauth_blocked:
                try:
                    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
                    # atomic write：open('w') 直寫會先 truncate，多 daemon 同時
                    # refresh 撞寫 → torn token → 整艦隊 OAuth 假死。
                    _atomic_write_text(TOKEN_FILE, creds.to_json())
                except Exception as exc:
                    if not token_json:
                        raise
                    print(f"[google_auth] ⚠️ token refresh 後無法寫回本機檔案：{exc}")
        if not daemon_oauth_blocked:
            return creds
    # ── _token_lock 已釋放 ──
    # daemon 撞 OAuth 牆：在鎖外通知（email fallback 重入 get_google_credentials()
    # 此時能拿到鎖；_daemon_oauth_alert_sent latch 擋掉重複通知）後再失敗本輪。
    _notify_daemon_oauth_blocked()
    raise RuntimeError("daemon 模式下無法啟動互動式 OAuth 授權，請先在前景完成登入")


def _get_valid_creds():
    global _cached_creds
    if _cached_creds and _cached_creds.valid:
        exp = _cached_creds.expiry
        # google-auth 回傳的是 naive UTC datetime，補上 tzinfo 才能跟 aware now 相比
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp and exp > datetime.now(timezone.utc) + _CREDS_REFRESH_BUFFER:
            return _cached_creds
    new_creds = get_google_credentials()
    if _cached_creds is not None and (not _cached_creds.valid or _cached_creds.token != new_creds.token):
        _service_cache.clear()
    _cached_creds = new_creds
    return _cached_creds


def get_service(api_name, version):
    key = f"{api_name}_{version}"
    creds = _get_valid_creds()
    if key not in _service_cache:
        import httplib2
        http = _get_authorized_http()(creds, http=httplib2.Http(timeout=_GOOGLE_API_TIMEOUT_S))
        _service_cache[key] = _get_google_build()(api_name, version, http=http)
    return _service_cache[key]


# ── Service-account (domain-wide delegation) ─────────────────────────────
# Secondary mailboxes (e.g. a different company's Workspace where Red is admin)
# are read via a service account with domain-wide delegation rather than a
# second interactive OAuth token. Service accounts have no refresh token to
# expire, so the daemon never hits the interactive-OAuth wall for these
# accounts — see _notify_daemon_oauth_blocked above for why that matters.
_DEFAULT_SA_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
_sa_service_cache: dict[tuple, Any] = {}
_sa_cache_lock = threading.Lock()


def _resolve_repo_path(path: str) -> str:
    """Resolve a config-supplied path: absolute as-is, relative to repo root."""
    if os.path.isabs(path):
        return path
    return os.path.join(_PROJECT_ROOT, path)


def get_service_account_credentials(service_account_file, subject, scopes=None):
    """Build delegated credentials that impersonate ``subject``.

    ``service_account_file`` is a path to the downloaded JSON key (absolute, or
    relative to the repo root). ``subject`` is the mailbox to impersonate; the
    service account's client ID must be authorised for ``scopes`` in that
    mailbox's Workspace admin console (domain-wide delegation).
    """
    from google.oauth2 import service_account

    resolved = _resolve_repo_path(str(service_account_file))
    if not os.path.exists(resolved):
        raise FileNotFoundError(f"找不到 service account 金鑰：{resolved}")
    creds = service_account.Credentials.from_service_account_file(
        resolved, scopes=list(scopes or _DEFAULT_SA_SCOPES)
    )
    subject = str(subject or "").strip()
    if subject:
        creds = creds.with_subject(subject)
    return creds


def build_account_service(
    api_name, version, *, service_account_file, subject, scopes=None
):
    """Build a FRESH, uncached delegated discovery client (own creds + own
    httplib2.Http). googleapiclient's transport is not thread-safe, so parallel
    fetchers must each hold their own service — this is the per-worker builder.
    Independent creds per service also avoids cross-thread token-refresh races.
    """
    import httplib2

    creds = get_service_account_credentials(service_account_file, subject, scopes)
    http = _get_authorized_http()(
        creds, http=httplib2.Http(timeout=_GOOGLE_API_TIMEOUT_S)
    )
    return _get_google_build()(api_name, version, http=http)


def build_drive_service_uncached():
    """Build a FRESH, uncached Drive (v3) discovery client (own creds + own
    httplib2.Http) for **parallel** RAG fetch. googleapiclient's transport is not
    thread-safe, so each worker thread must hold its own service — this is the
    per-worker builder, mirroring build_account_service but for the interactive
    OAuth identity (Drive RAG reads via OAuth, not a service account — unlike
    secondary Gmail). get_google_credentials() guards refresh with _token_lock,
    so concurrent builders are safe; each gets an independent Http to avoid
    cross-thread transport races.
    """
    import httplib2

    creds = get_google_credentials()
    http = _get_authorized_http()(
        creds, http=httplib2.Http(timeout=_GOOGLE_API_TIMEOUT_S)
    )
    return _get_google_build()("drive", "v3", http=http)


def get_service_for_account(
    account_key, api_name, version, *, service_account_file, subject, scopes=None
):
    """Return a discovery client authenticated as a delegated ``subject``.

    Cached per (account_key, api, version). The AuthorizedHttp wrapper refreshes
    the short-lived service-account token transparently, mirroring get_service.
    Use build_account_service for the per-thread, uncached variant.
    """
    cache_key = (str(account_key), api_name, version)
    with _sa_cache_lock:
        svc = _sa_service_cache.get(cache_key)
        if svc is None:
            svc = build_account_service(
                api_name, version,
                service_account_file=service_account_file,
                subject=subject, scopes=scopes,
            )
            _sa_service_cache[cache_key] = svc
        return svc


# ── RPC wall-clock guard ─────────────────────────────────────────────────
# _GOOGLE_API_TIMEOUT_S bounds each individual httplib2 recv(), NOT the whole
# request. A server that dribbles bytes (or a transport sub-path that never gets
# an effective socket timeout) keeps every recv under the socket timeout while
# the overall .execute() stalls for many minutes at ~0% CPU. rag_sync
# 2026-07-06 wedged ~32min on a Google SSL response-header read exactly this way
# (httplib2 is built on stdlib http.client, so the C-stack was _buffered_readline
# → _ssl__SSLSocket_read_impl → poll). drive_sync already guards its RPCs with a
# wall-clock ceiling (_DRIVE_RPC_TIMEOUT_S); gmail_sync / chat_sync had no such
# backstop and relied on the per-recv socket timeout alone. This is the shared
# guard those two now route through.


class RpcWallClockTimeout(TimeoutError):
    """A single Google API RPC exceeded its wall-clock budget.

    Distinct from httplib2's per-recv socket timeout — see run_rpc_with_timeout.
    The worker thread that ran the request may STILL be alive (blocked in the
    wedged read) when this is raised, so callers must not reuse the service that
    issued it; run_rpc_with_timeout clears the caches for exactly that reason.
    Ordinary Exception subclass (via TimeoutError) so per-item ``except
    Exception`` handlers in the sync loops record the failure and move on.
    """


def clear_service_caches() -> None:
    """Drop every cached discovery client (OAuth + service-account).

    Called after an RPC wall-clock timeout: the abandoned worker thread may
    still hold the wedged httplib2 connection, so the cached service (and its
    half-open socket) must not be reused — the next get_service* rebuilds a
    fresh Http/connection. Mirrors drive_sync._clear_google_service_cache but
    also clears the delegated service-account cache (chat_sync uses both).
    """
    _service_cache.clear()
    with _sa_cache_lock:
        _sa_service_cache.clear()


def run_rpc_with_timeout(timeout_s: int, label: str, fn):
    """Run a single Google API request (``fn`` = ``request.execute``) under a
    wall-clock ceiling.

    httplib2's socket timeout bounds each recv() but not the whole request; a
    slow/dribbling response can stall one .execute() far past it with 0% CPU,
    wedging a daemon for hours (rag_sync 2026-07-06). This bounds the whole
    call: on overshoot we abandon the worker thread (Python can't kill it),
    clear the service caches so its wedged connection isn't reused, and raise
    RpcWallClockTimeout so the caller records the failure and moves on. Mirrors
    drive_sync._run_with_timeout. ``timeout_s`` <= 0 disables (runs fn inline).
    """
    if timeout_s <= 0:
        return fn()
    holder: dict[str, Any] = {}

    def target() -> None:
        try:
            holder["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — carry across to caller
            holder["exc"] = exc

    worker = threading.Thread(target=target, name=f"rpc:{label}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        clear_service_caches()
        raise RpcWallClockTimeout(f"{label} exceeded {timeout_s}s")
    if "exc" in holder:
        raise holder["exc"]
    return holder.get("value")
