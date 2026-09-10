"""Compliant web access diagnostics.

This module deliberately does not bypass WAFs, CAPTCHAs, login walls, or
rate limits. It gives the agent a single place to identify access-control
states and stop extraction with a useful next step.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger


_MAX_CLASSIFY_CHARS = 200_000
_MAX_RESPONSE_BYTES = 1_000_000
_DIAG_DIR = os.path.join(STATE_DIR, "web_access_diagnostics")
_DOMAIN_POLICY_FILE = os.path.join(STATE_DIR, "web_domain_policy.json")
_DEFAULT_USER_AGENT = (
    "RED-Agent/1.0 compliant-access-diagnostic; contact-site-owner-for-allowlist"
)
_DOMAIN_POLICIES = frozenset({
    "allowed",
    "needs_api",
    "requires_manual_login",
    "blocked",
})
_PG_WEB_POLICY_WARNING_UNTIL = 0.0


# ── SSRF 防護 ──────────────────────────────────────────────────────────────
# LLM/被注入內容給的 URL 不可用來打內網服務（127.0.0.1:8000 的共用 Chroma、
# 169.254.169.254 雲端 metadata=SA token、其他 RFC1918 內部服務）。所有「拿
# 不可信 URL 去 fetch」的工具都要先過 assert_url_is_public，並用 guarded_requests_get
# 逐跳重驗 redirect（公開 URL 可能 302 到內網）。
class SSRFBlockedError(Exception):
    """URL 指向內網/loopback/link-local/保留位址，拒絕連線。"""


def _ip_is_blocked(ip_str: str) -> bool:
    """該 IP 是否為不可對外 fetch 的內部/保留位址（含雲端 metadata 169.254.169.254）。"""
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])  # 去掉 IPv6 scope 後綴
    except ValueError:
        return True  # 無法解析 = fail closed
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped  # ::ffff:127.0.0.1 之類的 IPv4-mapped
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def assert_url_is_public(url: str) -> None:
    """URL 非 http/https 或（解析後）指向內部/保留位址 → raise SSRFBlockedError。

    對主機名做 getaddrinfo，逐一檢查每個解析出的 A/AAAA；任一落在內部位址就拒。
    （DNS rebinding 殘留風險：這裡與 requests 連線時各解析一次，屬已知次要風險。）
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError(f"只允許 http/https 網址（收到 scheme={parsed.scheme or '空'}）。")
    host = parsed.hostname
    if not host:
        raise SSRFBlockedError("URL 缺少主機名。")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"DNS 解析失敗（{host}）。") from exc
    for info in infos:
        ip_str = info[4][0]
        if _ip_is_blocked(ip_str):
            raise SSRFBlockedError(f"拒絕連向內網/保留位址（{host} → {ip_str}）。")


def guarded_requests_get(
    url: str,
    *,
    headers: dict | None = None,
    timeout: int = 10,
    stream: bool = False,
    max_redirects: int = 5,
) -> requests.Response:
    """requests.get 的 SSRF-safe 版：對初始 URL 與每個 redirect 目標都先過
    assert_url_is_public，再手動跟隨（allow_redirects=False），阻擋「公開 URL 302
    到內網」。回傳最終 Response（其 .url 即最終 URL）。可能 raise SSRFBlockedError。"""
    current = (url or "").strip()
    for _ in range(max_redirects + 1):
        assert_url_is_public(current)
        resp = requests.get(
            current, headers=headers, timeout=timeout,
            stream=stream, allow_redirects=False,
        )
        is_redirect = resp.is_redirect or resp.is_permanent_redirect
        location = resp.headers.get("Location") if is_redirect else None
        if not location:
            return resp
        resp.close()
        current = urljoin(current, location)
    raise SSRFBlockedError("重導次數過多（可能是重導迴圈）。")


@dataclass(frozen=True)
class WebAccessAssessment:
    """Structured result for a single web access attempt."""

    status: str
    reason: str
    next_action: str
    url: str = ""
    final_url: str = ""
    http_status: int | None = None
    title: str = ""
    content_type: str = ""
    signals: tuple[str, ...] = field(default_factory=tuple)
    can_extract: bool = False
    sample_truncated: bool = False
    domain_policy: str = ""
    policy_domain: str = ""
    policy_note: str = ""
    retry_after: str = ""
    retry_after_seconds: int | None = None
    retry_after_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "next_action": self.next_action,
            "url": self.url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "title": self.title,
            "content_type": self.content_type,
            "signals": list(self.signals),
            "can_extract": self.can_extract,
            "sample_truncated": self.sample_truncated,
            "domain_policy": self.domain_policy,
            "policy_domain": self.policy_domain,
            "policy_note": self.policy_note,
            "retry_after": self.retry_after,
            "retry_after_seconds": self.retry_after_seconds,
            "retry_after_at": self.retry_after_at,
        }


_WAF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cloudflare_title", re.compile(r"\b(cloudflare|just a moment)\b", re.I)),
    ("cloudflare_checking_browser", re.compile(r"checking your browser", re.I)),
    ("cloudflare_ray_id", re.compile(r"cloudflare ray id|cf-ray", re.I)),
    ("cloudflare_challenge_path", re.compile(r"cdn-cgi/challenge-platform|__cf_chl_", re.I)),
    ("cloudflare_error_details", re.compile(r"cf-error-details|attention required", re.I)),
    ("generic_waf", re.compile(r"\b(web application firewall|waf challenge)\b", re.I)),
)

_WAF_HEADER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cloudflare_ray_header", re.compile(r"(^|\n)cf-ray\s*:", re.I)),
    ("cloudflare_mitigated_header", re.compile(r"(^|\n)cf-mitigated\s*:", re.I)),
    ("cloudflare_challenge_header", re.compile(r"(^|\n)cf-chl", re.I)),
)

_CAPTCHA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("turnstile_widget", re.compile(r"cf-turnstile|challenges\.cloudflare\.com/turnstile", re.I)),
    ("recaptcha_widget", re.compile(r"g-recaptcha|www\.google\.com/recaptcha", re.I)),
    ("hcaptcha_widget", re.compile(r"h-captcha|hcaptcha\.com", re.I)),
    (
        "captcha_text",
        re.compile(
            r"\b(please (complete|solve) (the )?captcha|captcha challenge|"
            r"verify you are human|human verification)\b",
            re.I,
        ),
    ),
)

_LOGIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("password_input", re.compile(r"<input[^>]+type=[\"']?password", re.I)),
    ("login_title", re.compile(r"\b(sign in|log in|login|登入|登錄)\b", re.I)),
)


def _collect_signals(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(text)]


def _headers_to_text(headers: Mapping[str, str] | None) -> str:
    if not headers:
        return ""
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


def _header_value(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return str(v)
    return ""


def _extract_title(html: str) -> str:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        if soup.title and soup.title.string:
            return soup.title.string.strip()
    except Exception:
        pass
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _normalize_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        raw = urlparse(raw).netloc
    raw = raw.split("@")[-1].split(":")[0].strip(".")
    if raw.startswith("*."):
        suffix = raw[2:].strip(".")
        return f"*.{suffix}" if suffix else ""
    return raw


def _warn_pg_policy_fallback(exc: Exception) -> None:
    global _PG_WEB_POLICY_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_WEB_POLICY_WARNING_UNTIL:
        return
    _PG_WEB_POLICY_WARNING_UNTIL = now + 30
    logger.warning("Postgres web domain policy failed; falling back to JSON: %s", exc)


def _pg_domain_policy_store():
    try:
        from agent_core import operational_web_domain_policy as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - policy lookup should stay usable
        _warn_pg_policy_fallback(exc)
    return None


def _host_from_url(url: str) -> str:
    return _normalize_domain(urlparse(url or "").netloc)


def _load_domain_policies() -> dict[str, dict[str, str]]:
    store = _pg_domain_policy_store()
    if store is not None:
        try:
            return store.load_policies()
        except Exception as exc:  # noqa: BLE001 - fall back to local policy file
            _warn_pg_policy_fallback(exc)
    try:
        with open(_DOMAIN_POLICY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for domain, entry in raw.items():
        norm = _normalize_domain(str(domain))
        if not norm:
            continue
        if isinstance(entry, str):
            policy = entry
            note = ""
        elif isinstance(entry, dict):
            policy = str(entry.get("policy", ""))
            note = str(entry.get("note", ""))
        else:
            continue
        if policy in _DOMAIN_POLICIES:
            out[norm] = {"policy": policy, "note": note}
    return out


def _save_domain_policies(policies: dict[str, dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(_DOMAIN_POLICY_FILE), exist_ok=True)
    _atomic_write_text(
        _DOMAIN_POLICY_FILE,
        json.dumps(policies, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _domain_candidates(host: str) -> list[str]:
    host = _normalize_domain(host)
    if not host:
        return []
    parts = host.split(".")
    candidates = [host]
    for i in range(1, max(len(parts) - 1, 1)):
        suffix = ".".join(parts[i:])
        candidates.append(f"*.{suffix}")
        candidates.append(suffix)
    return list(dict.fromkeys(candidates))


def _lookup_domain_policy(url: str) -> dict[str, str]:
    policies = _load_domain_policies()
    host = _host_from_url(url)
    for candidate in _domain_candidates(host):
        entry = policies.get(candidate)
        if entry:
            return {
                "domain": candidate,
                "policy": entry.get("policy", ""),
                "note": entry.get("note", ""),
            }
    return {}


def _policy_block_assessment(url: str, entry: dict[str, str]) -> WebAccessAssessment | None:
    policy = entry.get("policy", "")
    if policy == "allowed" or not policy:
        return None
    domain = entry.get("domain", "")
    note = entry.get("note", "")
    if policy == "blocked":
        status = "blocked_by_policy"
        reason = f"網域策略將 {domain} 標記為 blocked。"
        next_action = note or "不要請求此網域；若需資料，先取得明確授權或改用站方資料出口。"
        signal = "domain_policy_blocked"
    elif policy == "needs_api":
        status = "needs_api"
        reason = f"網域策略將 {domain} 標記為 needs_api。"
        next_action = note or "使用官方 API、資料合作接口或站方提供的匯出機制。"
        signal = "domain_policy_needs_api"
    elif policy == "requires_manual_login":
        status = "manual_login_required"
        reason = f"網域策略將 {domain} 標記為 requires_manual_login。"
        next_action = note or "請使用者用合法帳號手動登入，或改用 OAuth/API/Access 憑證。"
        signal = "domain_policy_manual_login"
    else:
        return None
    return WebAccessAssessment(
        status=status,
        reason=reason,
        next_action=next_action,
        url=url,
        final_url=url,
        signals=(signal,),
        can_extract=False,
        domain_policy=policy,
        policy_domain=domain,
        policy_note=note,
    )


def _parse_retry_after(headers: Mapping[str, str] | None) -> tuple[str, int | None, str]:
    raw = _header_value(headers, "Retry-After").strip()
    if not raw:
        return "", None, ""
    now = datetime.now(timezone.utc)
    if raw.isdigit():
        seconds = max(0, int(raw))
        return raw, seconds, (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return raw, None, ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((dt - now).total_seconds()))
    return raw, seconds, dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def assess_web_access(
    *,
    url: str = "",
    final_url: str = "",
    http_status: int | None = None,
    title: str = "",
    body: str = "",
    content_type: str = "",
    headers: Mapping[str, str] | None = None,
    sample_truncated: bool = False,
) -> WebAccessAssessment:
    """Classify a web response without attempting to bypass access controls."""

    body = body or ""
    title = title or _extract_title(body)
    final_url = final_url or url
    policy_entry = _lookup_domain_policy(final_url or url)
    header_text = _headers_to_text(headers)
    text = "\n".join([title, final_url, header_text, body[:_MAX_CLASSIFY_CHARS]])

    waf_signals = _collect_signals(text, _WAF_PATTERNS)
    if http_status in (403, 503):
        waf_signals.extend(_collect_signals(header_text, _WAF_HEADER_PATTERNS))
    captcha_signals = _collect_signals(text, _CAPTCHA_PATTERNS)
    login_signals = _collect_signals(text, _LOGIN_PATTERNS)
    all_signals = tuple(dict.fromkeys(captcha_signals + waf_signals + login_signals))
    domain_policy = policy_entry.get("policy", "")
    policy_domain = policy_entry.get("domain", "")
    policy_note = policy_entry.get("note", "")

    if captcha_signals:
        return WebAccessAssessment(
            status="captcha_required",
            reason="頁面要求 CAPTCHA / Turnstile / human verification。",
            next_action=(
                "停止自動化；改用官方 API、已授權登入流程、Cloudflare Access "
                "service token，或請人工完成合法驗證。"
            ),
            url=url,
            final_url=final_url,
            http_status=http_status,
            title=title,
            content_type=content_type,
            signals=all_signals,
            can_extract=False,
            sample_truncated=sample_truncated,
            domain_policy=domain_policy,
            policy_domain=policy_domain,
            policy_note=policy_note,
        )

    if http_status == 429:
        retry_after, retry_after_seconds, retry_after_at = _parse_retry_after(headers)
        if retry_after_seconds is not None:
            next_action = (
                f"停止連續請求；Retry-After={retry_after}，約 {retry_after_seconds} 秒後再試。"
            )
        else:
            next_action = "停止連續請求，依 Retry-After 或站方規範退避，降低頻率後再試。"
        return WebAccessAssessment(
            status="rate_limited",
            reason="伺服器回覆 429 Too Many Requests。",
            next_action=next_action,
            url=url,
            final_url=final_url,
            http_status=http_status,
            title=title,
            content_type=content_type,
            signals=all_signals,
            can_extract=False,
            sample_truncated=sample_truncated,
            domain_policy=domain_policy,
            policy_domain=policy_domain,
            policy_note=policy_note,
            retry_after=retry_after,
            retry_after_seconds=retry_after_seconds,
            retry_after_at=retry_after_at,
        )

    if waf_signals:
        return WebAccessAssessment(
            status="blocked_by_waf",
            reason="偵測到 Cloudflare/WAF challenge 或阻擋頁。",
            next_action=(
                "不要重整或模擬繞防；確認授權，使用官方 API、IP allowlist、"
                "Cloudflare Access service token，或站方提供的資料出口。"
            ),
            url=url,
            final_url=final_url,
            http_status=http_status,
            title=title,
            content_type=content_type,
            signals=all_signals,
            can_extract=False,
            sample_truncated=sample_truncated,
            domain_policy=domain_policy,
            policy_domain=policy_domain,
            policy_note=policy_note,
        )

    if http_status in (401, 407) or login_signals:
        return WebAccessAssessment(
            status="auth_required",
            reason="頁面需要登入、代理授權或有效 session。",
            next_action="使用站方允許的登入、OAuth/API token、Access 憑證，或請使用者手動授權。",
            url=url,
            final_url=final_url,
            http_status=http_status,
            title=title,
            content_type=content_type,
            signals=all_signals,
            can_extract=False,
            sample_truncated=sample_truncated,
            domain_policy=domain_policy,
            policy_domain=policy_domain,
            policy_note=policy_note,
        )

    if http_status == 403:
        return WebAccessAssessment(
            status="forbidden",
            reason="伺服器回覆 403 Forbidden。",
            next_action="確認是否有資料授權；若是自有站點，請走 allowlist/API/Access 憑證。",
            url=url,
            final_url=final_url,
            http_status=http_status,
            title=title,
            content_type=content_type,
            signals=all_signals,
            can_extract=False,
            sample_truncated=sample_truncated,
            domain_policy=domain_policy,
            policy_domain=policy_domain,
            policy_note=policy_note,
        )

    if http_status is not None and http_status >= 500:
        return WebAccessAssessment(
            status="server_error",
            reason=f"伺服器回覆 {http_status}。",
            next_action="稍後重試；若是自有系統，查看站台錯誤日誌。",
            url=url,
            final_url=final_url,
            http_status=http_status,
            title=title,
            content_type=content_type,
            signals=all_signals,
            can_extract=False,
            sample_truncated=sample_truncated,
            domain_policy=domain_policy,
            policy_domain=policy_domain,
            policy_note=policy_note,
        )

    return WebAccessAssessment(
        status="ok",
        reason="未偵測到 WAF/CAPTCHA/登入牆阻擋。",
        next_action="可以依 robots.txt、站方條款與速率限制進行資料讀取。",
        url=url,
        final_url=final_url,
        http_status=http_status,
        title=title,
        content_type=content_type,
        signals=all_signals,
        can_extract=True,
        sample_truncated=sample_truncated,
        domain_policy=domain_policy,
        policy_domain=policy_domain,
        policy_note=policy_note,
    )


def format_web_access_assessment(
    assessment: WebAccessAssessment,
    *,
    artifact_dir: str = "",
) -> str:
    """Render a concise Traditional Chinese diagnostic message."""

    icon = "✅" if assessment.status == "ok" else "⚠️"
    lines = [
        f"{icon} Web 存取診斷：{assessment.status}",
        f"原因：{assessment.reason}",
    ]
    if assessment.http_status is not None:
        lines.append(f"HTTP：{assessment.http_status}")
    if assessment.final_url:
        lines.append(f"URL：{assessment.final_url}")
    if assessment.title:
        lines.append(f"Title：{assessment.title[:120]}")
    if assessment.signals:
        lines.append("Signals：" + ", ".join(assessment.signals))
    if assessment.domain_policy:
        policy = assessment.domain_policy
        domain = assessment.policy_domain or "(unknown)"
        note = f"；{assessment.policy_note}" if assessment.policy_note else ""
        lines.append(f"Domain policy：{domain} = {policy}{note}")
    if assessment.retry_after:
        retry = assessment.retry_after
        if assessment.retry_after_seconds is not None:
            retry += f"（約 {assessment.retry_after_seconds} 秒）"
        lines.append(f"Retry-After：{retry}")
    if assessment.sample_truncated:
        lines.append(f"內容樣本：已限制在 {_MAX_RESPONSE_BYTES // 1000}KB 內")
    lines.append(f"下一步：{assessment.next_action}")
    if artifact_dir:
        lines.append(f"診斷檔：{artifact_dir}")
    return "\n".join(lines)


def _artifact_slug(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or "unknown-host"
    path = re.sub(r"[^a-zA-Z0-9._-]+", "_", parsed.path.strip("/") or "root")
    host = re.sub(r"[^a-zA-Z0-9._-]+", "_", host)
    return f"{host}_{path}"[:120].strip("_") or "web"


def _save_diagnostic_artifacts(
    *,
    url: str,
    body: str,
    assessment: WebAccessAssessment,
) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(_DIAG_DIR, f"{ts}_{_artifact_slug(url)}")
    os.makedirs(out_dir, exist_ok=True)
    _atomic_write_text(
        os.path.join(out_dir, "assessment.json"),
        json.dumps(assessment.to_dict(), ensure_ascii=False, indent=2),
    )
    _atomic_write_text(os.path.join(out_dir, "response.html"), body or "")
    return out_dir


def _read_response_prefix(response: requests.Response, max_bytes: int = _MAX_RESPONSE_BYTES) -> tuple[str, bool]:
    """Read only the response prefix needed for diagnostics."""

    if not hasattr(response, "iter_content"):
        text = getattr(response, "text", "") or ""
        return text[:max_bytes], len(text.encode("utf-8", errors="ignore")) > max_bytes

    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in response.iter_content(chunk_size=65536, decode_unicode=False):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            keep = len(chunk) - (total - max_bytes)
            if keep > 0:
                chunks.append(chunk[:keep])
            truncated = True
            break
        chunks.append(chunk)
    encoding = (
        response.encoding
        or requests.utils.get_encoding_from_headers(response.headers)
        or "utf-8"
    )
    return b"".join(chunks).decode(encoding, errors="replace"), truncated


def _diagnose_url(
    url: str,
    *,
    save_artifacts: bool,
    timeout_sec: int,
) -> tuple[WebAccessAssessment, str]:
    """Run diagnostics and return a structured assessment plus artifact dir."""

    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return WebAccessAssessment(
            status="invalid_url",
            reason="URL 只支援完整的 http/https 網址。",
            next_action="請提供完整 http:// 或 https:// 網址。",
            url=url,
            final_url=url,
            can_extract=False,
        ), ""

    policy_assessment = _policy_block_assessment(url, _lookup_domain_policy(url))
    if policy_assessment is not None:
        return policy_assessment, ""

    timeout = max(3, min(int(timeout_sec), 60))
    try:
        response = guarded_requests_get(
            url,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=timeout,
            stream=True,
        )
    except SSRFBlockedError as exc:
        return WebAccessAssessment(
            status="blocked",
            reason=f"SSRF 阻擋：{exc}",
            next_action="此工具不可連向內網/loopback/雲端 metadata；請提供公開網址。",
            url=url,
            final_url=url,
            can_extract=False,
        ), ""
    except requests.Timeout:
        assessment = WebAccessAssessment(
            status="timeout",
            reason=f"連線逾時（>{timeout}s）。",
            next_action="稍後重試；若是自有站點，檢查網路、DNS、站台健康狀態。",
            url=url,
            final_url=url,
            can_extract=False,
        )
        return assessment, ""
    except requests.RequestException as exc:
        assessment = WebAccessAssessment(
            status="network_error",
            reason=f"{type(exc).__name__}: {exc}",
            next_action="檢查網址、DNS、網路連線，或改用站方正式資料出口。",
            url=url,
            final_url=url,
            can_extract=False,
        )
        return assessment, ""

    try:
        body, sample_truncated = _read_response_prefix(response, max_bytes=_MAX_RESPONSE_BYTES)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    content_type = response.headers.get("Content-Type", "")
    assessment = assess_web_access(
        url=url,
        final_url=response.url,
        http_status=response.status_code,
        title=_extract_title(body),
        body=body,
        content_type=content_type,
        headers=response.headers,
        sample_truncated=sample_truncated,
    )
    artifact_dir = ""
    if save_artifacts:
        artifact_dir = _save_diagnostic_artifacts(
            url=response.url,
            body=body,
            assessment=assessment,
        )
    return assessment, artifact_dir


def web_access_diagnose(
    url: str,
    save_artifacts: bool = False,
    timeout_sec: int = 15,
) -> str:
    """Diagnose a web URL without bypassing access controls.

    Args:
        url: HTTP/HTTPS URL to check.
        save_artifacts: Save response HTML + assessment JSON under var/state.
        timeout_sec: Request timeout, clamped to 3..60 seconds.
    """

    assessment, artifact_dir = _diagnose_url(
        url,
        save_artifacts=save_artifacts,
        timeout_sec=timeout_sec,
    )
    return format_web_access_assessment(assessment, artifact_dir=artifact_dir)


def web_access_diagnose_json(
    url: str,
    save_artifacts: bool = False,
    timeout_sec: int = 15,
) -> str:
    """Return web access diagnostics as JSON for agents and schedulers."""

    assessment, artifact_dir = _diagnose_url(
        url,
        save_artifacts=save_artifacts,
        timeout_sec=timeout_sec,
    )
    payload = assessment.to_dict()
    if artifact_dir:
        payload["artifact_dir"] = artifact_dir
    return json.dumps(payload, ensure_ascii=False, indent=2)


def web_domain_policy_set(domain: str, policy: str, note: str = "") -> str:
    """Set a compliant access policy for a domain or wildcard domain.

    policy: allowed / needs_api / requires_manual_login / blocked
    """

    norm = _normalize_domain(domain)
    policy = (policy or "").strip().lower()
    if not norm:
        return "❌ domain 不可為空。"
    if policy not in _DOMAIN_POLICIES:
        return "❌ policy 必須是 allowed / needs_api / requires_manual_login / blocked。"
    store = _pg_domain_policy_store()
    if store is not None:
        try:
            store.set_policy(norm, policy, (note or "").strip())
            return f"✅ 已設定 domain policy：{norm} = {policy}"
        except Exception as exc:  # noqa: BLE001 - fall back to local policy file
            _warn_pg_policy_fallback(exc)
    policies = _load_domain_policies()
    policies[norm] = {"policy": policy, "note": (note or "").strip()}
    _save_domain_policies(policies)
    return f"✅ 已設定 domain policy：{norm} = {policy}"


def web_domain_policy_clear(domain: str) -> str:
    """Remove a domain access policy entry."""

    norm = _normalize_domain(domain)
    if not norm:
        return "❌ domain 不可為空。"
    store = _pg_domain_policy_store()
    if store is not None:
        try:
            if not store.clear_policy(norm):
                return f"（沒有找到 {norm} 的 domain policy）"
            return f"✅ 已移除 domain policy：{norm}"
        except Exception as exc:  # noqa: BLE001 - fall back to local policy file
            _warn_pg_policy_fallback(exc)
    policies = _load_domain_policies()
    if norm not in policies:
        return f"（沒有找到 {norm} 的 domain policy）"
    policies.pop(norm, None)
    _save_domain_policies(policies)
    return f"✅ 已移除 domain policy：{norm}"


def web_domain_policy_list() -> str:
    """List configured domain access policies."""

    policies = _load_domain_policies()
    if not policies:
        return "（尚未設定 web domain policy）"
    lines = ["Web domain policy："]
    for domain in sorted(policies):
        entry = policies[domain]
        note = f" — {entry.get('note')}" if entry.get("note") else ""
        lines.append(f"- {domain}: {entry.get('policy')}{note}")
    return "\n".join(lines)
