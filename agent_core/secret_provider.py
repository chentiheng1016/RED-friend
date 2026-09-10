"""Runtime secret loading for local and Cloud Run deployments.

Priority:
  1. Explicit environment variables.
  2. Google Secret Manager, when configured.
  3. OS keyring, for local development compatibility.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class SecretLookup:
    value: str
    source: str


_CACHE: dict[str, SecretLookup] = {}
_CACHE_LOCK = threading.Lock()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def cloud_runtime_detected() -> bool:
    return bool(
        os.environ.get("K_SERVICE")
        or os.environ.get("K_REVISION")
        or _truthy(os.environ.get("RED_CLOUD_MODE"))
    )


def _env_suffix(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").upper()


def _secret_id(name: str) -> str:
    explicit = os.environ.get(f"RED_SECRET_{_env_suffix(name)}_ID", "").strip()
    if explicit:
        return explicit
    prefix = os.environ.get("RED_SECRET_PREFIX", "").strip()
    raw = f"{prefix}{name}"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-_")


def _secret_resource(name: str) -> str:
    suffix = _env_suffix(name)
    explicit = os.environ.get(f"RED_SECRET_{suffix}_RESOURCE", "").strip()
    if explicit:
        return explicit

    if not _truthy(os.environ.get("RED_SECRET_MANAGER_ENABLED")):
        return ""

    project = (
        os.environ.get("RED_SECRET_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
        or ""
    ).strip()
    if not project:
        return ""

    version = os.environ.get("RED_SECRET_VERSION", "latest").strip() or "latest"
    return f"projects/{project}/secrets/{_secret_id(name)}/versions/{version}"


def _read_secret_manager(name: str) -> SecretLookup:
    resource = _secret_resource(name)
    if not resource:
        return SecretLookup("", "missing")

    with _CACHE_LOCK:
        cached = _CACHE.get(resource)
    if cached is not None:
        return cached

    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": resource})
        value = response.payload.data.decode("utf-8")
        lookup = SecretLookup(value, f"secret-manager:{resource}")
    except Exception:
        lookup = SecretLookup("", "missing")

    if lookup.value:
        with _CACHE_LOCK:
            _CACHE[resource] = lookup
    return lookup


def _read_keyring(service: str, key: str) -> SecretLookup:
    try:
        import keyring

        value = (keyring.get_password(service, key) or "").strip()
    except Exception:
        return SecretLookup("", "missing")
    if not value:
        return SecretLookup("", "missing")
    return SecretLookup(value, f"keyring:{service}/{key}")


def get_secret(
    name: str,
    *,
    env_names: Iterable[str] = (),
    keyring_service: str = "xiaohong-agent",
    keyring_name: str | None = None,
    required: bool = False,
    strip: bool = True,
) -> SecretLookup:
    """Return a secret value without making cloud-only assumptions.

    Cloud Run's recommended path is to inject Secret Manager versions as
    environment variables. For deployments that prefer runtime Secret Manager
    access, set either:

      * RED_SECRET_<NAME>_RESOURCE=projects/.../secrets/.../versions/latest
      * or RED_SECRET_MANAGER_ENABLED=1 plus RED_SECRET_PROJECT=<project>
    """
    candidates = list(env_names)
    red_env = f"RED_SECRET_{_env_suffix(name)}"
    if red_env not in candidates:
        candidates.append(red_env)

    for env_name in candidates:
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        value = raw.strip() if strip else raw
        if value:
            return SecretLookup(value, f"env:{env_name}")

    lookup = _read_secret_manager(name)
    if lookup.value:
        value = lookup.value.strip() if strip else lookup.value
        return SecretLookup(value, lookup.source)

    lookup = _read_keyring(keyring_service, keyring_name or name)
    if lookup.value:
        value = lookup.value.strip() if strip else lookup.value
        return SecretLookup(value, lookup.source)

    if required:
        raise RuntimeError(f"Missing required secret: {name}")
    return SecretLookup("", "missing")


def clear_secret_cache() -> None:
    """Testing helper."""
    with _CACHE_LOCK:
        _CACHE.clear()
