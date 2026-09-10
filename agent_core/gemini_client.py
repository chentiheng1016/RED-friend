"""Gemini client singleton + API key loader.

Kept self-contained:
no imports from agent.py, so agent.py can import from here without
circular dependency.
"""
from agent_core.logging_and_paths import startup_print
from agent_core.secret_provider import get_secret
import os
import re
import sys
import platform
import threading
import ssl

_OS = platform.system()
_IS_MAC = _OS == "Darwin"
_IS_WIN = _OS == "Windows"


_KEYRING_SERVICE = "xiaohong-agent"
_KEYRING_USER = "gemini-api-key"


def _platform_keystore_name() -> str:
    if _IS_MAC:
        return "macOS 鑰匙圈"
    if _IS_WIN:
        return "Windows 認證管理員"
    return "Linux Secret Service"


def api_key_fingerprint() -> str:
    """目前**已載入**的 Gemini API key 的短指紋（sha256 前 8 碼十六進位）。

    給 cost_tracker 記帳用：帳本要能按 key 拆帳（多把 key 分屬不同 GCP project，
    帳單也是照 project 出的），但**絕不能把 key 本身寫進 var/data/cost/cost.jsonl**
    —— 那是明文檔、會被 dashboard / red-web 讀。存不可逆的雜湊，既能分辨是哪一把、
    又不外洩任何金鑰材料。Gemini key 是 39 字元高熵字串，sha256 前像攻擊不可行；
    截到 8 碼（32 bit）對區分「幾把 key」綽綽有餘。

    ⚠️ 刻意**只讀已載入的全域**、不呼叫 `_get_gemini_api_key()`：那顆在金鑰缺失
    或格式錯時會 `sys.exit(1)`，而這裡是記帳路徑 —— 記帳絕不該有能終止程序的副作用。
    還沒載入就回空字串（實務上 record_call 只在成功呼叫之後發生，那時必然已載入）。
    """
    key = _gemini_api_key
    if not key:
        return ""
    import hashlib

    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:8]


def _validate_api_key(key: str) -> tuple[bool, str]:
    if not key:
        return False, "金鑰為空"
    if not key.isascii():
        return False, "金鑰包含非 ASCII 字元（看起來是佔位字，不是真金鑰）"
    if len(key) < 20:
        return False, f"金鑰長度僅 {len(key)}，遠短於 Gemini key 的標準長度"
    if not key.startswith("AIza"):
        return False, "Gemini API key 標準以 'AIza' 開頭，你存的格式不符"
    return True, ""


# Default model for 小紅 + all daemons. Override via RED_GEMINI_MODEL env var
# without touching code — e.g. switch the whole fleet to a steadier model when
# the preview tier starts returning 503 UNAVAILABLE under high demand
# (`RED_GEMINI_MODEL=gemini-flash-latest`). Set in the daemon plists'
# EnvironmentVariables, then `./bin/redeploy-daemons <name> --force`.
GEMINI_MODEL = os.environ.get("RED_GEMINI_MODEL", "").strip() or "gemini-3-flash-preview"

# Model for the RAG answer-synthesis step (multi-hop 彙整). Defaults to
# GEMINI_MODEL so nothing changes unless you opt in. Set RED_RAG_GEN_MODEL to
# give RAG answers their own model independent of general chat — e.g. keep the
# fleet on a cheap flash model (RED_GEMINI_MODEL=gemini-flash-latest) while RAG
# synthesis runs on a stronger one (RED_RAG_GEN_MODEL=gemini-2.5-pro). Routing
# is automatic: the synthesis code path reads this, normal chat reads
# GEMINI_MODEL. No manual switching — set once in the plist EnvironmentVariables.
RAG_GEN_MODEL = os.environ.get("RED_RAG_GEN_MODEL", "").strip() or GEMINI_MODEL
_genai_module = None
_genai_types = None
_gemini_client = None
_vertex_embed_client = None
_gemini_api_key = None
_gemini_key_src = None
_gemini_ready_checked = False
_gemini_lock = threading.Lock()
_gemini_call_lock = threading.Lock()
_USES_LIBRESSL = "LibreSSL" in ssl.OPENSSL_VERSION
_circuit_lock = threading.Lock()
_circuit_state: dict[str, dict[str, float | int | str]] = {}
_circuit_backend_warning_until = 0.0


class GeminiCircuitOpenError(RuntimeError):
    """Raised when recent Gemini failures make another immediate call wasteful."""


def _gemini_fallback_model(primary_model: str) -> str:
    """Optional fallback model for transient primary-model outages.

    This is read dynamically so launchd / Cloud Run env changes take effect on
    the next call after process restart, while tests can patch os.environ
    without reloading the module.
    """
    fallback = os.environ.get("RED_GEMINI_FALLBACK_MODEL", "").strip()
    primary = (primary_model or "").strip()
    if not fallback or fallback == primary:
        return ""
    return fallback


# ── thinking budget：兩代 Gemini 的旋鈕互斥，要按目標模型翻譯 ──────────
#
# 2026-08-05 實測（同一支 key、同一個 SDK 2.16.0）：
#   gemini-flash-latest（3.x）  thinking_level ✅   thinking_budget ❌ 400
#   gemini-2.5-flash-lite       thinking_level ❌ 400  thinking_budget ✅
#     └─ 400 訊息："Thinking level is not supported for this model."
#
# 這件事之所以危險：`_gemini_generate` 的 fallback 分支把**同一份 config**
# 原封不動傳給 fallback model，而 fleet 的組合正好跨代
# （RED_GEMINI_MODEL=gemini-flash-latest / RED_GEMINI_FALLBACK_MODEL=
# gemini-2.5-flash-lite）。任一個旋鈕寫死在 config 裡，primary 一出事、
# fallback 就會吃 400 INVALID_ARGUMENT —— 而 400 是**不可重試**的，等於把
# 「primary 掛掉但 fallback 頂著」變成「整條路徑一起死」。
#
# 所以呼叫端只表達意圖（"minimal" / "low"），由這裡按目標模型翻成該模型吃得下
# 的欄位；認不出世代就整個拿掉（＝退回模型預設，沒有省到但也絕不會壞）。
_THINKING_BUDGET_MODELS = ("gemini-1.5", "gemini-2.0", "gemini-2.5")
_THINKING_LEVEL_MODELS = ("gemini-3", "gemini-flash-latest", "gemini-pro-latest")
# 兩代的等價表述。budget 那欄有下限：實測 gemini-2.5-flash-lite 收 512、
# **拒收 128**（400 "The thinking budget 128 is invalid"），所以 low 用 512
# 而不是更小的數字。
_THINKING_INTENTS = {
    "minimal": {"level": "MINIMAL", "budget": 0},
    "low": {"level": "LOW", "budget": 512},
}


def _adapt_thinking_config(config, model: str):
    """把 config 裡的 thinking 意圖翻成 `model` 支援的欄位。

    config 用 `_red_thinking` 這個私有鍵表達意圖（"minimal" / "low"）；回傳新的
    config，私有鍵換成該模型的真實欄位。認不得的意圖或模型 → 直接移除，退回
    模型預設行為。非 dict 的 config（呼叫端自己給 GenerateContentConfig 物件）
    原樣返回，不去猜它的內部結構。
    """
    if not isinstance(config, dict) or "_red_thinking" not in config:
        return config
    out = {k: v for k, v in config.items() if k != "_red_thinking"}
    intent = _THINKING_INTENTS.get((config.get("_red_thinking") or "").strip().lower())
    if intent is None:
        return out  # 不認得的意圖一律當沒設
    m = (model or "").lower()
    if any(p in m for p in _THINKING_LEVEL_MODELS):
        out["thinking_config"] = {"thinking_level": intent["level"]}
    elif any(p in m for p in _THINKING_BUDGET_MODELS):
        out["thinking_config"] = {"thinking_budget": intent["budget"]}
    # else: 認不出世代 → 不帶 thinking_config（安全退回預設）
    return out


def _should_try_gemini_fallback(exc: BaseException) -> bool:
    if isinstance(exc, GeminiCircuitOpenError):
        return True
    msg = str(exc).lower()
    if _is_non_retryable_quota_error(msg):
        return False
    return _is_transient_error(msg)


def _monotonic() -> float:
    import time

    return time.monotonic()


def _epoch_time() -> float:
    import time

    return time.time()


def _warn_gemini_circuit_backend_failure(action: str, exc: Exception) -> None:
    global _circuit_backend_warning_until
    now = _monotonic()
    if now < _circuit_backend_warning_until:
        return
    _circuit_backend_warning_until = now + 60
    startup_print(
        "[gemini] ⚠️ Postgres Gemini circuit breaker "
        f"{action} failed; falling back to process-local state: {exc}"
    )


def _get_genai_module():
    global _genai_module
    if _genai_module is None:
        from google import genai as _genai
        _genai_module = _genai
        # 唯一 genai import 咽喉點 → 在這裡裝 AFC 未知工具護欄一次，覆蓋所有
        # Gemini 路徑（telegram daemon / REPL / 背景任務）。擋「模型呼叫被拔掉
        # 或幻覺的工具名 → KeyError 炸掉整輪」。詳見 genai_afc_guard。
        try:
            from agent_core.genai_afc_guard import install_afc_unknown_tool_guard
            if not install_afc_unknown_tool_guard():
                startup_print(
                    "[gemini] ⚠️ AFC 未知工具護欄未裝上（SDK 接縫可能變了），"
                    "未知工具呼叫仍可能炸整輪"
                )
        except Exception as exc:
            startup_print(f"[gemini] ⚠️ AFC 未知工具護欄安裝失敗（略過）：{exc}")
    return _genai_module


def _get_genai_types():
    global _genai_types
    if _genai_types is None:
        from google.genai import types as _types
        _genai_types = _types
    return _genai_types


# MIME types Gemini accepts directly as an inline image Part. Everything else
# (bmp/tiff/x-icon/psd/…) is rejected with 400 "Unsupported MIME type", so we
# PIL-convert it to JPEG first. Mirrors drive_sync's _CONVERTIBLE_IMAGE_MIMES
# routing for the RAG pipeline — same reason, different entry point (the local
# analyze_image / spec-parse / QC tools).
_GEMINI_NATIVE_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/jpg",
    "image/webp", "image/gif",
    "image/heic", "image/heif",
})


def _prepare_image_for_gemini(data: bytes, mime_type: str | None) -> tuple[bytes, str]:
    """Return (bytes, mime) safe to hand Gemini as an inline image Part.

    Gemini-native formats pass through untouched. Formats Gemini rejects
    (bmp/tiff/x-icon/psd/…) are converted to JPEG via Pillow so the call
    doesn't 400. On any conversion failure the input is returned unchanged
    (best effort — let Gemini decide)."""
    mt = (mime_type or "").lower()
    if mt in _GEMINI_NATIVE_IMAGE_MIMES:
        return data, mime_type
    try:
        import io
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            background = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, (mime_type or "image/jpeg")


def _get_gemini_api_key() -> str:
    global _gemini_api_key, _gemini_key_src, _gemini_ready_checked
    if _gemini_ready_checked:
        return _gemini_api_key or ""

    lookup = get_secret(
        _KEYRING_USER,
        env_names=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        keyring_service=_KEYRING_SERVICE,
        keyring_name=_KEYRING_USER,
    )
    key = lookup.value
    key_src = lookup.source if lookup.value else "未設定"
    ok, reason = _validate_api_key(key)

    if not ok:
        if not key:
            startup_print("❌ 找不到 GEMINI API 金鑰。請擇一設定：")
        else:
            startup_print(f"❌ GEMINI API 金鑰無效（來源：{key_src}）：{reason}")
            startup_print("   請重新設定為真正的金鑰，避免啟動後才在 API 呼叫時爆 ASCII 編碼錯誤。")
        startup_print(f"   【推薦】{_platform_keystore_name()}（跨平台最安全）:")
        startup_print(f"     python3 -c \"import keyring; keyring.set_password(f'{_KEYRING_SERVICE}',f'{_KEYRING_USER}','你的真實金鑰')\"")
        startup_print("   【備援】環境變數：export GEMINI_API_KEY='你的真實金鑰'")
        sys.exit(1)

    _gemini_api_key = key
    _gemini_key_src = key_src
    _gemini_ready_checked = True
    startup_print(f"[系統日誌] ✅ API 金鑰已載入（來源：{key_src}，平台：{_OS}）")
    return key


def _gemini_http_timeout_ms() -> int:
    """Per-request HTTP timeout for the shared genai client, in milliseconds.

    2026-06-13 incident: the SDK ships with NO default HTTP timeout, so when
    a Google-side 503 storm killed the underlying connections (left in
    CLOSE_WAIT), a chat.send_message call inside the Telegram inference
    thread blocked forever — the task never completed and the wedged thread
    kept the shared client unusable. Every request now gets a hard ceiling.

    Default 600s: far above the slowest legitimate single request (Files API
    chunked upload of a large video, pro-model long generation) yet low
    enough that a wedged call unblocks well inside the Telegram task
    deadline (default 15 min), letting the deadline gate finish the thread.
    """
    from agent_core.env_utils import env_int
    return env_int("RED_GEMINI_HTTP_TIMEOUT_S", 600, min_value=10, max_value=7200) * 1000


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    with _gemini_lock:
        if _gemini_client is None:
            _gemini_client = _get_genai_module().Client(
                api_key=_get_gemini_api_key(),
                # google-genai HttpOptions.timeout 單位是毫秒。
                http_options={"timeout": _gemini_http_timeout_ms()},
            )
    return _gemini_client


def _embed_use_vertex() -> bool:
    """是否把 embedding 改走 Vertex AI（讀 live env，預設關）。"""
    from agent_core.env_utils import env_bool
    return env_bool("RED_EMBED_USE_VERTEX", False)


def _build_vertex_embed_client():
    """建 Vertex AI client 給 embedding 用。

    走 service account 自身身分（cloud-platform scope、**不**做網域委派 subject），
    project 取 SA 金鑰裡的 project_id（RED_VERTEX_PROJECT 可覆寫）、location 預設
    us-central1。Vertex 用專屬配額，embedding 撞「high demand」503 遠少於公開 API；
    向量與公開 API 的 gemini-embedding-001 同空間（實測 cosine=1.0），故**不必重抽**。
    """
    import json
    import os
    from agent_core.google_auth import (
        get_service_account_credentials,
        _resolve_repo_path,
    )

    sa_file = os.environ.get(
        "RED_VERTEX_SA_FILE", "var/state/google/service_account.json"
    )
    location = os.environ.get("RED_VERTEX_LOCATION", "us-central1")
    project = os.environ.get("RED_VERTEX_PROJECT", "").strip()
    if not project:
        with open(_resolve_repo_path(sa_file), encoding="utf-8") as f:
            project = json.load(f).get("project_id", "")
    if not project:
        raise RuntimeError(
            "Vertex embed：無法決定 project（SA 無 project_id 且 RED_VERTEX_PROJECT 未設）"
        )
    creds = get_service_account_credentials(
        sa_file,
        subject="",
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return _get_genai_module().Client(
        vertexai=True,
        project=project,
        location=location,
        credentials=creds,
        http_options={"timeout": _gemini_http_timeout_ms()},
    )


def _get_embed_client():
    """Embedding 專用 client：`RED_EMBED_USE_VERTEX=1` → Vertex（專屬配額、少 503），
    否則公開 API。**只影響 embedding**；generation（2.5-pro / flash）仍走
    `_get_gemini_client`。Vertex 建立失敗 → 印警告並 fallback 公開 API，確保 ingest
    不因認證/設定問題整個中斷。"""
    if not _embed_use_vertex():
        return _get_gemini_client()
    global _vertex_embed_client
    if _vertex_embed_client is not None:
        return _vertex_embed_client
    with _gemini_lock:
        if _vertex_embed_client is None:
            try:
                _vertex_embed_client = _build_vertex_embed_client()
                print("[gemini] embedding 改走 Vertex AI", flush=True)
            except Exception as exc:  # noqa: BLE001 — 認證/設定失敗不可中斷 ingest
                print(
                    f"[gemini] Vertex embed client 建立失敗，fallback 公開 API：{exc}",
                    flush=True,
                )
                return _get_gemini_client()
    return _vertex_embed_client


def upload_file(client, path, *, mime_type=None):
    """Files API 上傳，強制帶 per-call HTTP timeout。

    ⚠️ client 建立時設的 http_options.timeout **不涵蓋 Files API 的上傳傳輸層**
    （resumable upload 走獨立路徑），所以不在 upload config 顯式帶 timeout 的話，
    大檔上傳一旦卡在 SSL read 會無限期掛住、沒有上限。2026-06-14：RAG 影片路徑
    上傳一支 178MB 影片卡死，整個 rag_sync 靜默 wedge 18 小時（log 凍住、watchdog
    只能告警無法自救）。全 repo 的 files.upload 一律改走這支，繼承同一個 timeout。

    收 client 參數（而非自己 _get_gemini_client）刻意為之：呼叫端用自己的綁定
    解析 client，既有測試的 mock 面（patch 各模組的 _get_gemini_client）才攔得到。
    """
    config = {"http_options": {"timeout": _gemini_http_timeout_ms()}}
    if mime_type:
        config["mime_type"] = mime_type
    return client.files.upload(file=path, config=config)


def _is_non_retryable_quota_error(message: str) -> bool:
    msg = (message or "").lower()
    return (
        "monthly spending cap" in msg
        or "spending cap" in msg
        or "project spend cap" in msg
        # 預付餘額耗盡（AI Studio Prepay）。重試 5 次也補不回餘額，只會拖慢、
        # 多燒一次 log；直接拋給上層讓使用者去 Buy credits / 開 auto-reload。
        or "prepayment credits are depleted" in msg
        or "prepayment credits" in msg
        # 帳務催收/付款被擋：Google 回 403 PERMISSION_DENIED
        # "Lightning dunning decision is deny for project: …"。dunning＝催收，
        # 是付款失效/逾期的硬性封鎖（2026-06-16 實際發生於 project 168037703156），
        # 同一輪重試補不回。歸到此處 → 失敗快收 + _classify_api_error 標成
        # quota_depleted，讓 billing 紅線抓得到、dashboard 告警直接提示「查 billing」。
        or "dunning" in msg
    )


# Transient = worth retrying with backoff (the opposite of the non-retryable
# quota errors above). Single source of truth so _gemini_generate's retry loop
# and daemon_telegram's chat.send_message retry loop can't drift apart — keeping
# those two policies consistent was the whole point of the quota fail-fast fix.
# Status codes matched on WORD BOUNDARIES — bare "500"/"503" substrings false-
# match digits embedded in an error's JSON body (retryDelay, token counts, doc
# URLs like ".../generate-content"). And the old "rate" keyword matched inside
# "generate" → any generate_content error looked transient（健檢 Low：硬 4xx 被當
# 暫時性、白重試數次 + 加幾秒延遲）。
_TRANSIENT_STATUS_RE = re.compile(r"\b(429|500|502|503|504)\b")
_TRANSIENT_ERROR_PHRASES = (
    "rate limit", "ratelimit", "quota", "timeout",
    "unavailable", "deadline", "high demand", "overloaded",
)


def _is_transient_error(message: str) -> bool:
    """True if `message` looks like a transient Gemini/network error a retry can
    plausibly fix (429 / 5xx / overload / timeout). Callers must still check
    _is_non_retryable_quota_error FIRST — a depleted-prepay 429 is NOT transient.
    """
    msg = (message or "").lower()
    return bool(_TRANSIENT_STATUS_RE.search(msg)) or any(
        p in msg for p in _TRANSIENT_ERROR_PHRASES
    )


# 「連 Gemini 都還沒連上」的本機網路失敗特徵字串。放在分類器最後一段（HTTP 狀態
# 碼、timeout、unavailable 都比對過之後）才判，免得把帶 5xx 的訊息搶走。
#
# 為什麼要獨立一類：這種錯誤**不是 Gemini 的問題**，是本機 DNS／網路斷了（最常見
# 的成因是睡眠喚醒的空窗）。全部歸進 "other" 的話，告警只會說「status: other×12」，
# 而 advice 給的三條（503 過載／429 配額／timeout 卡住）沒有一條對得上，看的人得
# 自己去翻 daemon log —— 2026-08-14 就實際發生過一次，翻了十分鐘才知道是 DNS。
_NETWORK_ERROR_PHRASES = (
    "nodename nor servname",          # macOS getaddrinfo
    "name or service not known",      # Linux getaddrinfo
    "temporary failure in name resolution",
    "nameresolutionerror",
    "failed to resolve",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "connection reset by peer",
)


def _classify_api_error(message: str) -> str:
    """把 Gemini/網路錯誤訊息歸成短狀態碼，給外部 API 錯誤率紅線分類統計。"""
    msg = (message or "").lower()
    if _is_non_retryable_quota_error(msg):
        return "quota_depleted"
    status = _TRANSIENT_STATUS_RE.search(msg)
    if status:
        return status.group(1)
    if "timeout" in msg or "deadline" in msg:
        return "timeout"
    if "unavailable" in msg or "high demand" in msg:
        return "unavailable"
    if any(p in msg for p in _NETWORK_ERROR_PHRASES):
        return "network_unreachable"
    return "other"


def _gemini_circuit_enabled() -> bool:
    from agent_core.env_utils import env_bool

    return env_bool("RED_GEMINI_CIRCUIT_BREAKER", True)


def _gemini_circuit_failure_threshold() -> int:
    from agent_core.env_utils import env_int

    return env_int("RED_GEMINI_CIRCUIT_FAILURES", 3, min_value=1, max_value=100)


def _gemini_circuit_window_s() -> int:
    from agent_core.env_utils import env_int

    return env_int("RED_GEMINI_CIRCUIT_WINDOW_S", 300, min_value=1, max_value=86400)


def _gemini_circuit_open_s() -> int:
    from agent_core.env_utils import env_int

    return env_int("RED_GEMINI_CIRCUIT_OPEN_S", 180, min_value=1, max_value=86400)


def _circuit_key(model: str) -> str:
    return (model or "?").strip() or "?"


def _reset_gemini_circuit_for_tests() -> None:
    global _circuit_backend_warning_until
    with _circuit_lock:
        _circuit_state.clear()
    _circuit_backend_warning_until = 0.0


def _gemini_circuit_store():
    try:
        from agent_core import operational_gemini_circuit as store
        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - circuit backend must not break chat
        _warn_gemini_circuit_backend_failure("load", exc)
    return None


def _raise_gemini_circuit_open(key: str, opened_until: float, now: float, reason: str) -> None:
    remaining = max(1, int(round(opened_until - now)))
    raise GeminiCircuitOpenError(
        f"Gemini circuit open for {key}; retry in {remaining}s ({reason})"
    )


def _check_gemini_circuit_local(model: str) -> None:
    now = _monotonic()
    key = _circuit_key(model)
    with _circuit_lock:
        state = _circuit_state.get(key)
        if not state:
            return
        opened_until = float(state.get("opened_until") or 0.0)
        if opened_until <= now:
            if opened_until:
                _circuit_state.pop(key, None)
            return
        reason = str(state.get("reason") or "recent Gemini failures")
    _raise_gemini_circuit_open(key, opened_until, now, reason)


def _check_gemini_circuit(model: str) -> None:
    if not _gemini_circuit_enabled():
        return
    key = _circuit_key(model)
    store = _gemini_circuit_store()
    if store is not None:
        now = _epoch_time()
        try:
            state = store.open_state(key, now=now)
        except Exception as exc:  # noqa: BLE001 - fall through to local guard
            _warn_gemini_circuit_backend_failure("check", exc)
        else:
            if state:
                _raise_gemini_circuit_open(
                    key,
                    float(state.get("opened_until") or 0.0),
                    now,
                    str(state.get("reason") or "recent Gemini failures"),
                )
            return
    _check_gemini_circuit_local(model)


def _record_gemini_circuit_success_local(model: str) -> None:
    with _circuit_lock:
        _circuit_state.pop(_circuit_key(model), None)


def _record_gemini_circuit_success(model: str) -> None:
    if not _gemini_circuit_enabled():
        return
    key = _circuit_key(model)
    store = _gemini_circuit_store()
    if store is not None:
        try:
            store.clear_state(key)
            return
        except Exception as exc:  # noqa: BLE001 - fall through to local clear
            _warn_gemini_circuit_backend_failure("clear", exc)
    _record_gemini_circuit_success_local(model)


def _record_gemini_circuit_failure_local(model: str, reason: str) -> None:
    now = _monotonic()
    key = _circuit_key(model)
    threshold = _gemini_circuit_failure_threshold()
    window_s = _gemini_circuit_window_s()
    open_s = _gemini_circuit_open_s()
    with _circuit_lock:
        state = _circuit_state.get(key) or {}
        last_failure_at = float(state.get("last_failure_at") or 0.0)
        failures = int(state.get("failures") or 0)
        if now - last_failure_at > window_s:
            failures = 0
        failures += 1
        state.update({
            "failures": failures,
            "last_failure_at": now,
            "reason": reason,
        })
        if failures >= threshold:
            state["opened_until"] = now + open_s
        else:
            state["opened_until"] = 0.0
        _circuit_state[key] = state


def _record_gemini_circuit_failure(model: str, status: str, message: str) -> None:
    if not _gemini_circuit_enabled():
        return
    key = _circuit_key(model)
    reason = status or _summarize_circuit_reason(message)
    threshold = _gemini_circuit_failure_threshold()
    window_s = _gemini_circuit_window_s()
    open_s = _gemini_circuit_open_s()
    store = _gemini_circuit_store()
    if store is not None:
        try:
            store.record_failure(
                key,
                reason=reason,
                now=_epoch_time(),
                threshold=threshold,
                window_s=window_s,
                open_s=open_s,
            )
            return
        except Exception as exc:  # noqa: BLE001 - fall through to local guard
            _warn_gemini_circuit_backend_failure("record", exc)
    _record_gemini_circuit_failure_local(model, reason)


def _summarize_circuit_reason(message: str, limit: int = 80) -> str:
    text = " ".join(str(message or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _gemini_generate_once(model: str, contents, max_attempts: int = 5, config=None,
                          caller: str = "", record_final_error: bool = True):
    """Gemini generate_content 加上指數退避重試。遇到 429/5xx/網路錯誤會自動重試。

    config: 可選 GenerateContentConfig（例如 safety_settings、response_mime_type）。
    呼叫者不傳就沿用 Gemini 預設值，維持舊行為。

    caller: 可選的成本歸戶標籤（例 "drive_sync._extract_media"）。不傳則由
    cost_tracker 從 call stack 推斷 — 但經過共用 helper 模組轉一手的呼叫，
    stack 推斷只會看到 helper 自己，預算斷路器（按 caller 字串對帳）就對不到
    帳，這時必須顯式傳。

    record_final_error: 最終失敗時要不要記 api_error。外層 _gemini_generate
    還有 fallback model 可試時傳 False —— 否則「primary 503、fallback 全救回」
    的任務也各記一筆錯誤，dashboard 錯誤率被推近 50% 觸發告警疲勞；此時
    改由外層在 fallback 也失敗時補記 primary 那筆。circuit breaker 記錄
    **不受此參數影響**（primary 的失敗要照記，fallback 才會持續被選中）。

    成本追蹤：每次成功 call 後把 usage_metadata 餵給 cost_tracker.record_call，
    寫 JSONL log 供 cost_today() / cost_by_tool() 查詢。失敗不記（沒 usage 資訊）。
    """
    import time as _t
    last_err = None
    # thinking 旋鈕依「這一次實際要打的 model」翻譯。放在這裡而不是呼叫端，是因為
    # 這是 config 碰到 SDK 的唯一咽喉點：primary 與 fallback 各自帶著自己的 model
    # 進來，翻譯自然就對；私有鍵 `_red_thinking` 也不可能從任何入口漏給 SDK。
    config = _adapt_thinking_config(config, model)
    _check_gemini_circuit(model)
    for attempt in range(max_attempts):
        try:
            # macOS CLT Python 3.9 ships with LibreSSL 2.8.3, which has shown
            # sporadic native crashes under concurrent HTTPS traffic on this host.
            # In that environment we serialize Gemini HTTPS calls to trade some
            # throughput for much better daemon stability.
            kwargs = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            t0 = _t.time()
            if _USES_LIBRESSL:
                with _gemini_call_lock:
                    resp = _get_gemini_client().models.generate_content(**kwargs)
            else:
                resp = _get_gemini_client().models.generate_content(**kwargs)
            # 成本追蹤（lazy import 避免 startup cycle）
            try:
                from agent_core import cost_tracker as _ct
                # 記帳用回應的 model_version 而非請求別名：`gemini-flash-latest`
                # 這類別名對不上定價表前綴、只能走家族 fallback，實際解析到的
                # 版本（例 gemini-2.5-flash）才對得準費率。
                _ct.record_call(
                    model=getattr(resp, "model_version", "") or model,
                    usage_metadata=getattr(resp, "usage_metadata", None),
                    duration_ms=(_t.time() - t0) * 1000,
                    caller=caller,
                )
                # 記下這個別名解析到誰，失敗那端才記得到同一個名字
                _remember_model_resolution(
                    model, getattr(resp, "model_version", "") or "")
            except Exception:
                pass  # cost tracker 不該 break 主流程
            _record_gemini_circuit_success(model)
            return resp
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            non_retryable = _is_non_retryable_quota_error(msg)
            transient = _is_transient_error(msg)
            will_retry = (not non_retryable) and transient and (attempt < max_attempts - 1)
            if not will_retry:
                # 只在最終放棄（不可重試 / retries 用盡）才記一筆 → 錯誤率＝真實
                # 任務失敗率，retry 後成功的暫時性 503 不計入（避免告警疲勞）。
                # record_final_error=False（外層還有 fallback 可試）時先不記，
                # 由外層在 fallback 也失敗時補記。best-effort，永不中斷主流程。
                status = _classify_api_error(msg)
                if record_final_error:
                    _record_gemini_api_error(model, status, detail=msg)
                if transient and not non_retryable:
                    _record_gemini_circuit_failure(model, status, msg)
                raise
            wait = min(2 ** attempt, 8)
            print(f"[系統日誌] ⚠️ Gemini 呼叫失敗（{e}），{wait}s 後重試...")
            _t.sleep(wait)
            continue
    raise last_err


# 請求別名 → 實際解析到的型號（gemini-flash-latest → gemini-3.7-flash）。
#
# 為什麼需要這張表：成功時記帳用 `resp.model_version`（**解析後**的具體型號），
# 失敗時沒有 response、只有請求時的**別名**，於是同一條路徑的成功與失敗被記在
# 兩個名字下。dashboard 的 by_model 因此長期顯示
#     gemini-flash-latest 12/12 (100%), gemini-3.7-flash 0/35 (0%)
# ——把讀的人指向一個完全健康的別名（實測 30 天 476 筆錯誤裡 236 筆掛在別名下、
# 而它的成功一筆都不在那個名字）。2026-08-14 那次 DNS 斷線就是這樣被誤讀成
# 「flash-latest 全掛」。
#
# process 內快取就夠：daemon 常駐，第一次成功呼叫就會填上；填不上時退回別名，
# 行為與修正前相同，不會更糟。
_MODEL_RESOLUTION: dict[str, str] = {}


def _remember_model_resolution(requested: str, resolved: str) -> None:
    """成功回應時記下「這個別名這次解析到誰」。

    ⚠️ 型別要嚴格檢查：`resolved` 來自 SDK 的 `resp.model_version`，欄位缺席時
    測試的 MagicMock 會自動生一個**真值但非字串**的物件。不擋的話它會被存進這張
    表，接著寫進 api_errors.jsonl 的 model 欄——全套測試當場抓到（單跑會過、
    合跑才炸，正是這種跨測試污染最難查的形狀）。
    """
    if (isinstance(requested, str) and isinstance(resolved, str)
            and requested and resolved and requested != resolved):
        _MODEL_RESOLUTION[requested] = resolved


def _record_gemini_api_error(model: str, status: str, detail: str = "") -> None:
    """記一筆 Gemini 最終失敗到 api_errors.jsonl。best-effort，永不 raise。

    `detail` 從 2026-08-14 起真的會傳 —— 在那之前這個參數不存在，寫進去的每一列
    detail 都是空的，於是 dashboard 的「外部 API 錯誤率」告警只能說「status:
    other×12」，看的人得自己去翻 daemon log 才知道發生什麼事（實際發生過：那 12
    筆其實是 `[Errno 8] nodename nor servname` ＝本機 DNS 斷線，跟 Gemini 無關，
    但面板完全看不出來）。

    ⚠️ 錯誤訊息可能夾帶 API key / URL 裡的 token，所以進檔前過 log_redact；
    `record_api_error` 那端另外截到 200 字。
    """
    try:
        from agent_core import cost_tracker as _ct
        safe = ""
        if detail:
            try:
                from agent_core.log_redact import has_secret, redact_log_line
                text = str(detail)
                safe = redact_log_line(text) if has_secret(text) else text
            except Exception:
                safe = ""          # redact 壞掉寧可不留 detail，也不要漏秘密
        # 別名 → 解析後型號，讓失敗與成功記在同一個名字下（見 _MODEL_RESOLUTION）。
        resolved = _MODEL_RESOLUTION.get(model or "", "")
        _ct.record_api_error(
            "gemini", status,
            model=resolved or model,
            requested_model=model if resolved else "",
            detail=safe,
        )
    except Exception:
        pass


def generate_content_tracked(model: str, contents, *, config=None,
                             caller: str = "", max_attempts: int = 3):
    """公開入口：`client.models.generate_content` 的**會記帳**版本。

    為什麼需要這顆（2026-08-04 查帳發現）：技能模組拿 `_get_gemini_client()` 之後
    直接呼 `client.models.generate_content(...)` 會整個繞過 `_gemini_generate_once`
    ——那層才有成本記帳、指數退避、circuit breaker。實測 cost.jsonl **全歷史 0 筆
    生圖**（nano-banana/imagen 一筆都沒有），不是沒生過圖，是根本沒記到；查帳時
    「帳本查無生圖」會被誤讀成「沒生圖」。

    刻意不走 `_gemini_generate`（那顆會在失敗時換 fallback model）：生圖/視覺模型
    換成文字 fallback 只會拿到不能用的回應。要 fallback 的呼叫端請直接用內部那顆。

    caller：成本歸戶標籤。經共用 helper 轉一手時 stack 推斷只看得到 helper，
    所以這裡**一律顯式傳**（例 "image_gen.generate_image"）。
    """
    return _gemini_generate_once(
        model=model,
        contents=contents,
        max_attempts=max_attempts,
        config=config,
        caller=caller,
    )


def _gemini_generate(model: str, contents, max_attempts: int = 5, config=None,
                     caller: str = ""):
    fallback_model = _gemini_fallback_model(model)
    try:
        # 有 fallback 可試時，primary 最終失敗先不記 api_error（fallback 全救
        # 回的任務不是「任務失敗」）；fallback 也失敗才由下面補記 primary 那筆。
        return _gemini_generate_once(
            model=model,
            contents=contents,
            max_attempts=max_attempts,
            config=config,
            caller=caller,
            record_final_error=not fallback_model,
        )
    except Exception as exc:
        primary_status = _classify_api_error(str(exc).lower())
        if not fallback_model:
            raise
        if not _should_try_gemini_fallback(exc):
            # 有 fallback 但這種錯誤不值得試（non-retryable 等）→ 內層被
            # 抑制的那筆在這裡補記，任務確實失敗了。
            _record_gemini_api_error(model, primary_status, detail=str(exc))
            raise
        startup_print(
            "[gemini] ⚠️ primary model failed; trying fallback "
            f"{model} -> {fallback_model}: {_summarize_circuit_reason(str(exc))}"
        )
        try:
            return _gemini_generate_once(
                model=fallback_model,
                contents=contents,
                max_attempts=max_attempts,
                # config 原樣傳下去即可：_gemini_generate_once 會依 fallback_model
                # 重新翻譯 thinking 旋鈕（跨代時旋鈕不同，見 _adapt_thinking_config）。
                config=config,
                caller=caller,
                record_final_error=True,
            )
        except Exception:
            # fallback 也失敗 → 任務真的失敗：補記 primary 那筆
            # （fallback 自己那筆已由內層記）。
            _record_gemini_api_error(model, primary_status, detail=str(exc))
            raise


# Gemini Files API polling timeouts (used by _wait_for_file_ready)
_FILE_POLL_TIMEOUT = 300         # 預設 5 分鐘（一般檔案夠）
_FILE_POLL_TIMEOUT_LARGE = 1500  # 大型影片（>500 MB）用 25 分鐘


def _wait_for_file_ready(uploaded_file, timeout_sec: int = None):
    """等 Gemini Files 處理到 ACTIVE。大型影片（> 500 MB）自動給更長超時。
    可顯式傳 timeout_sec 覆蓋預設值。"""
    import time as _t
    if timeout_sec is None:
        try:
            size_bytes = getattr(uploaded_file, "size_bytes", 0) or 0
            if size_bytes > 500 * 1024 * 1024:
                timeout_sec = _FILE_POLL_TIMEOUT_LARGE
            else:
                timeout_sec = _FILE_POLL_TIMEOUT
        except Exception:
            timeout_sec = _FILE_POLL_TIMEOUT
    elapsed = 0.0
    delay = 0.5
    while uploaded_file.state.name == "PROCESSING":
        if elapsed >= timeout_sec:
            raise TimeoutError(f"Gemini 檔案處理逾時（超過 {timeout_sec} 秒）")
        _t.sleep(delay)
        elapsed += delay
        delay = min(delay * 2, 8.0)
        uploaded_file = _get_gemini_client().files.get(name=uploaded_file.name)
    if uploaded_file.state.name == "FAILED":
        raise RuntimeError("Gemini 檔案處理失敗，請確認檔案格式是否正確。")
    return uploaded_file
