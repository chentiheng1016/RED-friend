"""Human/data exchange policy for cloud deployments.

This module keeps the human-facing channel decision separate from the
Telegram bot implementation:

* Telegram is the only normal human-facing exchange window in cloud mode.
* Backend state, audit logs, and artifacts stay in Red-owned storage.
* Large files are staged to an object-store-compatible directory and only the
  link is sent through Telegram.
* Emergency admin remains a separate, explicit break-glass path.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {
        "1", "true", "yes", "y", "on", "telegram_only", "telegram-only",
    }


def exchange_mode() -> str:
    """Return the configured exchange mode.

    Local development remains unchanged unless one of the cloud/Telegram-only
    env flags is set.
    """
    explicit = (os.environ.get("RED_EXCHANGE_MODE") or "").strip().lower()
    if explicit:
        return explicit.replace("-", "_")
    if _truthy(os.environ.get("RED_TELEGRAM_ONLY")):
        return "telegram_only"
    if _truthy(os.environ.get("RED_CLOUD_MODE")):
        return "telegram_only"
    return "local"


def is_telegram_only_mode() -> bool:
    return exchange_mode() in {"telegram_only", "cloud_telegram"}


_TELEGRAM_ONLY_BLOCKED_TOOL_NAMES = frozenset({
    # Alternate human-facing surfaces.
    "open_dashboard_in_browser",
    "show_notification",
    # Local foreground/UI control is not a reliable cloud exchange surface.
    "open_application",
    "close_application",
    "control_mac_system",
    "set_system_volume",
    "read_mac_clipboard",
    "click_screen",
    "type_text",
    "press_keys",
    "scroll_screen",
    "analyze_screen",
    "ocr_screen_region",
    "find_on_screen_by_image",
    "find_on_screen_by_text",
    "ax_click",
    "ax_type_in",
    "ax_read_value",
    "ax_find_elements",
    "ax_describe_app",
    "ax_list_running_apps",
})


def filter_tools_for_exchange_mode(tools: list) -> list:
    """Hide non-cloud human-interface tools in Telegram-only mode."""
    if not is_telegram_only_mode():
        return list(tools)
    filtered = []
    for tool in tools:
        name = getattr(tool, "__name__", "")
        if name in _TELEGRAM_ONLY_BLOCKED_TOOL_NAMES:
            continue
        filtered.append(tool)
    return filtered


def artifact_root() -> str:
    """Directory representing Red's durable artifact/object storage.

    In production this can be a mounted bucket path. In local/dev mode the
    default stays under the user's home, not repo var/data, because path_safety
    intentionally blocks var/data from LLM file tools.
    """
    root = (
        os.environ.get("RED_OBJECT_STORAGE_DIR")
        or os.environ.get("RED_EXCHANGE_ARTIFACT_DIR")
        or os.path.join(os.path.expanduser("~"), "red-exchange", "artifacts")
    )
    return os.path.abspath(os.path.expanduser(root))


def telegram_upload_root() -> str:
    """Where Telegram inbound attachments should be written."""
    override = os.environ.get("RED_TELEGRAM_UPLOAD_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if is_telegram_only_mode():
        return os.path.join(artifact_root(), "incoming")
    return os.path.join(os.path.expanduser("~"), "Downloads", "小紅-uploads")


def object_base_url() -> str:
    return (os.environ.get("RED_OBJECT_BASE_URL") or "").strip().rstrip("/")


def emergency_admin_url() -> str:
    return (os.environ.get("RED_EMERGENCY_ADMIN_URL") or "").strip()


def _safe_filename(name: str) -> str:
    from agent_core.path_safety import safe_cjk_filename
    return safe_cjk_filename(name, max_len=180, fallback="artifact")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ExchangeArtifact:
    source_path: str
    stored_path: str
    public_url: str
    size_bytes: int
    sha256: str
    created_at: str
    purpose: str


def _public_url_for(stored_path: str) -> str:
    base = object_base_url()
    if not base:
        return ""
    root = os.path.realpath(artifact_root())
    stored = os.path.realpath(stored_path)
    if not (stored == root or stored.startswith(root + os.sep)):
        return ""
    rel = os.path.relpath(stored, root)
    return f"{base}/{quote(rel)}"


def stage_artifact_for_exchange(file_path: str, *, purpose: str = "outgoing") -> ExchangeArtifact:
    """Copy a local file into the durable exchange artifact store."""
    src = os.path.realpath(os.path.expanduser(str(file_path or "")))
    if not os.path.isfile(src):
        raise FileNotFoundError(src)

    purpose = _safe_filename(purpose or "outgoing")
    today = datetime.now().strftime("%Y-%m-%d")
    root = artifact_root()
    target_dir = os.path.join(root, purpose, today)
    os.makedirs(target_dir, exist_ok=True)

    safe_name = _safe_filename(os.path.basename(src))
    unique = uuid.uuid4().hex[:12]
    stored = os.path.join(target_dir, f"{unique}_{safe_name}")
    shutil.copy2(src, stored)

    artifact = ExchangeArtifact(
        source_path=src,
        stored_path=stored,
        public_url=_public_url_for(stored),
        size_bytes=os.path.getsize(stored),
        sha256=_sha256_file(stored),
        created_at=datetime.now().isoformat(timespec="seconds"),
        purpose=purpose,
    )
    _append_manifest(artifact)
    return artifact


def _append_manifest(artifact: ExchangeArtifact) -> None:
    manifest = os.path.join(artifact_root(), "_manifest.jsonl")
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    rec = {
        "ts": artifact.created_at,
        "purpose": artifact.purpose,
        "source_path": artifact.source_path,
        "stored_path": artifact.stored_path,
        "public_url": artifact.public_url,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }
    with open(manifest, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def exchange_system_instruction() -> str:
    """Additional Telegram prompt text when cloud Telegram-only mode is active."""
    if not is_telegram_only_mode():
        return ""
    public = object_base_url()
    emergency = emergency_admin_url()
    return (
        "\n\n【交換窗口政策：雲端 Telegram-only】\n"
        "  - Telegram 是唯一正常的人機交換窗口；不要要求大王去看雲端本機路徑、"
        "開瀏覽器視窗、看桌面通知或操作本機 UI。\n"
        "  - 後端狀態、audit log、任務佇列、長期檔案不存 Telegram；Telegram 只用來收指令、"
        "傳短回覆、傳小檔或傳物件儲存連結。\n"
        "  - 超過 Telegram 上限的大檔要先放 artifact/object storage，再把連結用 Telegram 回覆。"
        f"目前物件儲存公開 base URL：{public or '未設定 RED_OBJECT_BASE_URL，不能交付大檔連結'}。\n"
        f"  - 備援管理入口：{emergency or '未設定 RED_EMERGENCY_ADMIN_URL；只能用伺服器 CLI/日誌 break-glass'}。\n"
    )


def exchange_policy_status() -> str:
    """Return a human-readable status summary for the exchange-window policy."""
    mode = exchange_mode()
    public = object_base_url()
    emergency = emergency_admin_url()
    lines = [
        "🔁 小紅交換窗口政策",
        f"- mode: {mode}",
        f"- Telegram-only: {'yes' if is_telegram_only_mode() else 'no'}",
        f"- inbound Telegram uploads: {telegram_upload_root()}",
        f"- artifact/object root: {artifact_root()}",
        f"- object public URL: {public or '(未設定 RED_OBJECT_BASE_URL)'}",
        f"- emergency admin URL: {emergency or '(未設定 RED_EMERGENCY_ADMIN_URL)'}",
    ]
    if is_telegram_only_mode() and not public:
        lines.append("- warning: 大於 Telegram 上限的檔案只能暫存，無法用 Telegram 交付可點連結。")
    lines.append(f"- checked_at: {datetime.now().isoformat(timespec='seconds')}")
    return "\n".join(lines)


def should_use_emergency_admin_path() -> bool:
    """True when break-glass admin path is configured."""
    return bool(emergency_admin_url())
