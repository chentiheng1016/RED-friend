#!/usr/bin/env python3
"""Sync local RED secrets into Google Secret Manager.

Default mode is a dry run: it reports which values are available locally and
which Secret Manager secrets exist, without printing secret values or writing
anything. Use --apply to create missing secrets or add new versions.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_core.logging_and_paths import CREDENTIALS_FILE, TOKEN_FILE  # noqa: E402


@dataclass(frozen=True)
class SecretSpec:
    secret_id: str
    required: bool
    source_hint: str


SPECS = [
    SecretSpec("gemini-api-key", True, "env GEMINI_API_KEY or keyring xiaohong-agent/gemini-api-key"),
    SecretSpec("telegram-bot-token", True, "env TELEGRAM_BOT_TOKEN or keyring xiaohong-agent/telegram-bot-token"),
    SecretSpec("telegram-chat-id", True, "env TELEGRAM_CHAT_ID or keyring xiaohong-agent/telegram-chat-id"),
    SecretSpec("web-secret-key", True, "env WEB_SECRET_KEY or keyring xiaohong-web/secret_key; can be generated"),
    SecretSpec("google-oauth-client-id", True, "env GOOGLE_OAUTH_CLIENT_ID or credentials.json"),
    SecretSpec("google-oauth-client-secret", True, "env GOOGLE_OAUTH_CLIENT_SECRET or credentials.json"),
    SecretSpec("google-oauth-token-json", True, "env GOOGLE_OAUTH_TOKEN_JSON or token.json"),
    SecretSpec("line-channel-secret", False, "env LINE_CHANNEL_SECRET or keyring xiaohong-agent/line-channel-secret"),
    SecretSpec(
        "line-channel-access-token",
        False,
        "env LINE_CHANNEL_ACCESS_TOKEN or keyring xiaohong-agent/line-channel-access-token",
    ),
]


def _run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=None if input_text is None else input_text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _gcloud_ok(project: str) -> tuple[bool, str]:
    try:
        proc = _run(["gcloud", "config", "get-value", "project"], check=False)
    except FileNotFoundError:
        return False, "gcloud not installed"
    if proc.returncode != 0:
        return False, proc.stderr.decode("utf-8", "replace").strip()
    configured = proc.stdout.decode("utf-8", "replace").strip()
    if project and configured and configured != project:
        return True, f"configured project is {configured}; using --project {project}"
    return True, "gcloud available"


def _keyring_get(service: str, name: str) -> str:
    try:
        import keyring

        return (keyring.get_password(service, name) or "").strip()
    except Exception:
        return ""


def _load_json_file(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _oauth_client_config() -> tuple[str, str]:
    client_id = (
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        or os.environ.get("RED_GOOGLE_OAUTH_CLIENT_ID")
        or _keyring_get("xiaohong-agent", "google-oauth-client-id")
    ).strip()
    client_secret = (
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        or os.environ.get("RED_GOOGLE_OAUTH_CLIENT_SECRET")
        or _keyring_get("xiaohong-agent", "google-oauth-client-secret")
    ).strip()
    if client_id and client_secret:
        return client_id, client_secret

    client_json = (
        os.environ.get("GOOGLE_OAUTH_CLIENT_JSON")
        or os.environ.get("RED_GOOGLE_OAUTH_CLIENT_JSON")
        or _keyring_get("xiaohong-agent", "google-oauth-client-json")
    )
    data = {}
    if client_json:
        try:
            data = json.loads(client_json)
        except Exception:
            data = {}
    if not data:
        data = _load_json_file(CREDENTIALS_FILE)
    block = data.get("web") or data.get("installed") or {}
    return client_id or block.get("client_id", ""), client_secret or block.get("client_secret", "")


def _secret_values(generate_web_secret: bool) -> dict[str, str]:
    values: dict[str, str] = {}
    values["gemini-api-key"] = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or _keyring_get("xiaohong-agent", "gemini-api-key")
    ).strip()
    values["telegram-bot-token"] = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("RED_TELEGRAM_BOT_TOKEN")
        or _keyring_get("xiaohong-agent", "telegram-bot-token")
    ).strip()
    values["telegram-chat-id"] = (
        os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("RED_TELEGRAM_CHAT_ID")
        or _keyring_get("xiaohong-agent", "telegram-chat-id")
    ).strip()
    values["web-secret-key"] = (
        os.environ.get("WEB_SECRET_KEY")
        or os.environ.get("RED_WEB_SECRET_KEY")
        or _keyring_get("xiaohong-web", "secret_key")
    ).strip()
    if not values["web-secret-key"] and generate_web_secret:
        values["web-secret-key"] = secrets.token_hex(32)

    client_id, client_secret = _oauth_client_config()
    values["google-oauth-client-id"] = client_id.strip()
    values["google-oauth-client-secret"] = client_secret.strip()

    token_json = (
        os.environ.get("GOOGLE_OAUTH_TOKEN_JSON")
        or os.environ.get("RED_GOOGLE_OAUTH_TOKEN_JSON")
        or _keyring_get("xiaohong-agent", "google-oauth-token-json")
    )
    if not token_json and os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as handle:
            token_json = handle.read()
    values["google-oauth-token-json"] = token_json.strip() if token_json else ""
    values["line-channel-secret"] = (
        os.environ.get("LINE_CHANNEL_SECRET")
        or os.environ.get("RED_LINE_CHANNEL_SECRET")
        or _keyring_get("xiaohong-agent", "line-channel-secret")
    ).strip()
    values["line-channel-access-token"] = (
        os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
        or os.environ.get("RED_LINE_CHANNEL_ACCESS_TOKEN")
        or _keyring_get("xiaohong-agent", "line-channel-access-token")
    ).strip()
    return values


def _secret_exists(project: str, secret_id: str) -> bool:
    proc = _run(
        ["gcloud", "secrets", "describe", secret_id, "--project", project],
        check=False,
    )
    return proc.returncode == 0


def _write_secret(project: str, secret_id: str, value: str, *, exists: bool) -> None:
    if exists:
        args = ["gcloud", "secrets", "versions", "add", secret_id, "--project", project, "--data-file=-"]
    else:
        args = ["gcloud", "secrets", "create", secret_id, "--project", project, "--data-file=-"]
    _run(args, input_text=value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Google Cloud project id")
    parser.add_argument("--apply", action="store_true", help="Write secrets to Secret Manager")
    parser.add_argument(
        "--generate-web-secret",
        action="store_true",
        help="Generate web-secret-key if no local value exists",
    )
    args = parser.parse_args()

    ok, msg = _gcloud_ok(args.project)
    if not ok:
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1
    print(f"gcloud: {msg}")

    values = _secret_values(generate_web_secret=args.generate_web_secret)
    missing_local: list[str] = []
    existence: dict[str, bool] = {}

    # 第一輪：只收集狀態、印總覽，不寫入任何東西。
    for spec in SPECS:
        value = values.get(spec.secret_id, "")
        local_status = "local-ok" if value else "local-missing"
        if not value and spec.required:
            missing_local.append(spec.secret_id)
        exists = _secret_exists(args.project, spec.secret_id)
        existence[spec.secret_id] = exists
        cloud_status = "cloud-exists" if exists else "cloud-missing"
        print(f"{spec.secret_id:28s} {local_status:14s} {cloud_status:14s} source: {spec.source_hint}")

    # 有必填缺值就提早收手 — 避免 --apply 部分寫入後才報錯（殘留半套 + 重跑多版本）。
    if missing_local:
        print("\nMissing local values:", ", ".join(missing_local), file=sys.stderr)
        print("Run without --apply until these are available, or provide env vars.", file=sys.stderr)
        return 1

    if not args.apply:
        print("\nDry run only. Add --apply to write Secret Manager secrets.")
        return 0

    # 第二輪：確認必填全齊後才寫入（existence 沿用第一輪結果，免重複呼叫 gcloud）。
    for spec in SPECS:
        value = values.get(spec.secret_id, "")
        if value:
            exists = existence[spec.secret_id]
            _write_secret(args.project, spec.secret_id, value, exists=exists)
            action = "added new version" if exists else "created"
            print(f"  -> {spec.secret_id}: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
