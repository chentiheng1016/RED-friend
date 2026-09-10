"""Shared Chroma client factory — single source of truth for the
PersistentClient-vs-HttpClient decision.

Why this exists: mixing an embedded `PersistentClient` with the `chroma run`
server on the *same* on-disk path lets two processes mutate the same SQLite +
HNSW index at once. ChromaDB's Rust core is not built for that — it corrupts
the index and SIGSEGVs in `chromadb_rust_bindings` (this is what crashed
`rag_sync_daily` repeatedly in 2026-06: three identical EXC_BAD_ACCESS reports,
all in chromadb_rust_bindings, while the standalone chroma server held the same
directory open).

The contract: when `RED_CHROMA_HTTP_URL` is set, EVERY subsystem that touches
Chroma (RAG `vector_store`, `memory`, …) must talk to that one server over HTTP
so only the server process owns the files. `memory.py` already did this; the RAG
`vector_store` did not — hence this shared helper, so the decision lives in one
place and can never drift between subsystems again.

The PersistentClient fallback (env unset) is guarded: before direct-opening we
probe the canonical shared-server address (the launchd template pins it to
127.0.0.1:8000). If anything answers there, we refuse with a RuntimeError
instead of silently opening the same index the server owns — ad-hoc scripts
that forget the env var were exactly how the 2026-06 corruption happened.
`RED_CHROMA_ALLOW_DIRECT=1` is the explicit escape hatch for offline
maintenance (server stopped) or scratch indexes. With no server detected,
single-process dev / REPL / CI / tests keep working unchanged.
"""
from __future__ import annotations

import logging
import os

from agent_core.env_utils import env_bool

logger = logging.getLogger(__name__)

# Canonical address of the shared server (launchd com.xiaohong.chroma pins
# --host 127.0.0.1 --port 8000). Probed only on the PersistentClient fallback
# path; daemons with RED_CHROMA_HTTP_URL set never reach it.
SHARED_SERVER_URL = "http://127.0.0.1:8000"
_HEARTBEAT_TIMEOUT_S = 1.0


def _shared_server_alive(
    base_url: str = SHARED_SERVER_URL, timeout: float = _HEARTBEAT_TIMEOUT_S
) -> bool:
    """Probe the shared chroma server's heartbeat. Fail-safe semantics:
    only a refused connection ("nobody listening") counts as *not* alive.
    Any response — including a non-chroma listener or a timeout — counts as
    alive, because direct-opening while a server holds the index corrupts it;
    a false "alive" merely makes the caller set an env var.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import ProxyHandler, build_opener

    url = base_url.rstrip("/") + "/api/v2/heartbeat"
    try:
        # ProxyHandler({}) 顯式停用 http_proxy/all_proxy：loopback 探測絕不能
        # 經過代理——proxy 回 502/504 會誤判「活著」（煩人），proxy 本身掛掉的
        # connection refused 更會誤判「沒人聽」而直開腐壞（致命）。
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=timeout):
            return True
    except HTTPError:
        # 有東西在聽，只是回了非 2xx——可能是舊版 chroma 或別的服務佔了
        # 8000。寧可誤判為活著（呼叫端再明確 opt-out），不可冒腐壞風險。
        return True
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return False  # 真的沒人在聽——唯一安全的 fallback 訊號
        return True
    except Exception:
        return True


def build_chroma_client(persist_dir: str):
    """Return a Chroma client. HttpClient when RED_CHROMA_HTTP_URL is set
    (shared server — multi-process safe). Otherwise a local PersistentClient,
    but only after probing that the shared server is NOT running; refuses
    with RuntimeError if it is (override: RED_CHROMA_ALLOW_DIRECT=1)."""
    import chromadb

    http_url = os.environ.get("RED_CHROMA_HTTP_URL", "").strip()
    if http_url:
        from urllib.parse import urlparse

        parsed = urlparse(http_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"RED_CHROMA_HTTP_URL 必須是 http(s):// URL，收到：{http_url!r}"
            )
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 8000)
        ssl = parsed.scheme == "https"
        logger.info("[chroma] HttpClient backend → %s:%d (ssl=%s)", host, port, ssl)
        return chromadb.HttpClient(host=host, port=port, ssl=ssl)

    allow_direct = env_bool("RED_CHROMA_ALLOW_DIRECT", False)
    if _shared_server_alive():
        if not allow_direct:
            raise RuntimeError(
                f"共用 Chroma server 似乎正在 {SHARED_SERVER_URL} 運行，"
                f"拒絕直接開 PersistentClient({persist_dir!r})。"
                "多個 process 同時持有同一份 index 會腐壞 HNSW 段並導致 "
                "SIGSEGV crash-loop（2026-06 rag_sync 事故）。請改走共用 server："
                f"export RED_CHROMA_HTTP_URL={SHARED_SERVER_URL}；"
                "若確定要直開（離線維運、或這份 persist_dir 不是 server 服務的"
                "那份 index），設 RED_CHROMA_ALLOW_DIRECT=1 明確跳過此防護。"
            )
        logger.warning(
            "[chroma] ⚠️ 共用 server 仍在 %s 運行，但 RED_CHROMA_ALLOW_DIRECT=1 → "
            "照樣直開 %s（請確認這不是 server 正在服務的 index）",
            SHARED_SERVER_URL,
            persist_dir,
        )

    os.makedirs(persist_dir, exist_ok=True)
    logger.info(
        "[chroma] PersistentClient backend → %s (⚠️ single-process)", persist_dir
    )
    return chromadb.PersistentClient(path=persist_dir)


def _heartbeat_ok(base_url: str, timeout: float = _HEARTBEAT_TIMEOUT_S) -> bool:
    """Strict heartbeat for MONITORING: True ONLY on a clean 2xx from the chroma
    server. Timeout / refused / non-2xx / wrong listener / bad URL all → False.

    This is the inverse of _shared_server_alive's fail-safe ("ambiguous = alive",
    which protects the direct-open fallback). For monitoring we WANT a hung or
    replaced server (timeout / non-2xx) to read as not-ok so the crit alert
    fires — otherwise the chroma red-line stays silent on the most likely
    failure mode (Codex / gemini-code-assist review on PR #136).
    """
    from urllib.request import ProxyHandler, build_opener

    url = base_url.rstrip("/") + "/api/v2/heartbeat"
    try:
        opener = build_opener(ProxyHandler({}))  # bypass proxy on loopback
        with opener.open(url, timeout=timeout) as resp:
            code = getattr(resp, "status", None)
            if code is None:
                code = resp.getcode()
            return 200 <= int(code) < 300
    except Exception:
        return False


def preflight() -> dict:
    """Probe the shared-Chroma contract WITHOUT building a client — for
    monitoring (dashboard_alerts) and eager daemon-startup checks.

    Never raises (monitoring must not break its caller). Returns:
      {mode, http_url, server_alive, allow_direct, ok, detail}
      - mode: "http" when RED_CHROMA_HTTP_URL is set (the production contract),
        else "direct" (the PersistentClient fallback path).
      - ok: in http mode, True iff the shared server answers heartbeat; in
        direct mode, False iff build_chroma_client WOULD refuse (server alive
        and RED_CHROMA_ALLOW_DIRECT unset) — the dangerous drift case.
    """
    try:
        http_url = os.environ.get("RED_CHROMA_HTTP_URL", "").strip()
        allow_direct = env_bool("RED_CHROMA_ALLOW_DIRECT", False)
        if http_url:
            from urllib.parse import urlparse
            if urlparse(http_url).scheme not in ("http", "https"):
                return {
                    "mode": "http",
                    "http_url": http_url,
                    "server_alive": False,
                    "allow_direct": allow_direct,
                    "ok": False,
                    "detail": f"RED_CHROMA_HTTP_URL 格式錯誤（須 http(s)://）：{http_url}",
                }
            alive = _heartbeat_ok(http_url)
            return {
                "mode": "http",
                "http_url": http_url,
                "server_alive": alive,
                "allow_direct": allow_direct,
                "ok": alive,
                "detail": ("共用 server heartbeat 正常" if alive
                           else f"RED_CHROMA_HTTP_URL={http_url} heartbeat 無回應或非 2xx"),
            }
        alive = _shared_server_alive()
        would_refuse = alive and not allow_direct
        return {
            "mode": "direct",
            "http_url": "",
            "server_alive": alive,
            "allow_direct": allow_direct,
            "ok": not would_refuse,
            "detail": (
                "未設 RED_CHROMA_HTTP_URL 但共用 server 在跑 → build_chroma_client "
                "會拒絕直開（防 HNSW 腐壞）" if would_refuse
                else ("離線維運直開（RED_CHROMA_ALLOW_DIRECT=1）" if allow_direct
                      else "無共用 server，single-process 直開")
            ),
        }
    except Exception as exc:
        # 監控用途，絕不可讓探測本身炸掉呼叫端（alert daemon）。
        return {
            "mode": "unknown",
            "http_url": "",
            "server_alive": None,
            "allow_direct": False,
            "ok": True,
            "detail": f"preflight 探測失敗：{exc}",
        }
