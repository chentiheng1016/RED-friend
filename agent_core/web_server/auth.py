"""Google OAuth 2.0 for the employee web interface.

Flow:
  /auth/login  → redirect to Google consent screen
  /auth/callback → exchange code → verify email → set session

Session cookie stores: {"email": "...", "name": "...", "color": "..."}
Signed with SECRET_KEY (from keyring or env var WEB_SECRET_KEY).
"""
from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

import httpx

from agent_core.secret_provider import cloud_runtime_detected, get_secret

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

_SCOPES = "openid email profile"


def _get_oauth_config() -> tuple[str, str]:
    """Return (client_id, client_secret) from credentials.json."""
    client_id = get_secret(
        "google-oauth-client-id",
        env_names=("GOOGLE_OAUTH_CLIENT_ID", "RED_GOOGLE_OAUTH_CLIENT_ID"),
        keyring_name="google-oauth-client-id",
    ).value
    client_secret = get_secret(
        "google-oauth-client-secret",
        env_names=("GOOGLE_OAUTH_CLIENT_SECRET", "RED_GOOGLE_OAUTH_CLIENT_SECRET"),
        keyring_name="google-oauth-client-secret",
    ).value
    if client_id and client_secret:
        return client_id, client_secret

    client_json = get_secret(
        "google-oauth-client-json",
        env_names=("GOOGLE_OAUTH_CLIENT_JSON", "RED_GOOGLE_OAUTH_CLIENT_JSON"),
        keyring_name="google-oauth-client-json",
        strip=False,
    ).value
    if client_json:
        data = json.loads(client_json)
        web = data.get("web") or data.get("installed") or {}
        client_id = web.get("client_id", "")
        client_secret = web.get("client_secret", "")
        if client_id and client_secret:
            return client_id, client_secret

    from agent_core.logging_and_paths import CREDENTIALS_FILE
    creds_path = CREDENTIALS_FILE
    if not os.path.exists(creds_path):
        raise RuntimeError(f"credentials.json 不存在：{creds_path}")
    with open(creds_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    web = data.get("web") or data.get("installed") or {}
    client_id = web.get("client_id", "")
    client_secret = web.get("client_secret", "")
    if not client_id or not client_secret:
        raise RuntimeError("credentials.json 格式錯誤，找不到 client_id / client_secret")
    return client_id, client_secret


def get_secret_key() -> str:
    """Return session signing key — fail closed if no high-entropy secret exists.

    Priority:
      1. WEB_SECRET_KEY env var
      2. keyring "xiaohong-web" / "secret_key"
      3. Auto-generate + persist to keyring (first-run bootstrap)
    Raises RuntimeError rather than returning a predictable fallback, so a
    misconfigured deployment fails loudly instead of silently using a forgeable key.
    """
    import secrets as _secrets

    lookup = get_secret(
        "web-secret-key",
        env_names=("WEB_SECRET_KEY", "RED_WEB_SECRET_KEY"),
        keyring_service="xiaohong-web",
        keyring_name="secret_key",
    )
    if lookup.value and len(lookup.value) >= 32:
        return lookup.value

    if cloud_runtime_detected():
        raise RuntimeError(
            "WEB_SECRET_KEY is missing or shorter than 32 characters. "
            "Set it from Secret Manager or an environment variable before deploying."
        )

    try:
        import keyring
        val = keyring.get_password("xiaohong-web", "secret_key")
        if val and len(val) >= 32:
            return val
        # First run: generate and persist
        new_key = _secrets.token_hex(32)
        keyring.set_password("xiaohong-web", "secret_key", new_key)
        print("[web_server] 已自動產生 session secret key 並存入 macOS Keychain。")
        return new_key
    except Exception as exc:
        raise RuntimeError(
            f"無法取得 session secret key（keyring 失敗：{exc}）。\n"
            "請設定環境變數 WEB_SECRET_KEY=<至少 32 字元的隨機字串>。"
        ) from exc


def generate_state() -> str:
    """Generate a cryptographically random CSRF state token."""
    import secrets
    return secrets.token_urlsafe(32)


def build_login_url(redirect_uri: str, state: str) -> str:
    """Build Google OAuth URL. `state` must be a random token stored in session."""
    client_id, _ = _get_oauth_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "online",
        "state": state,
    }
    return f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange auth code for user info dict {email, name, picture}.
    Async to avoid blocking the FastAPI event loop during OAuth HTTP calls.
    """
    client_id, client_secret = _get_oauth_config()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        tokens = resp.json()
        access_token = tokens.get("access_token", "")
        info_resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info_resp.raise_for_status()
        return info_resp.json()
