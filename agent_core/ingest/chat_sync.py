"""Google Chat → Drive JSON backup + ChromaDB RAG ingest (domain-wide).

Mirrors the Gmail/Drive RAG sync pattern (service-account domain-wide
delegation) but for Google Chat. For every active Workspace user we impersonate
them, list the spaces they belong to, and for each space:

  1. back up its messages to a JSON file in a Red-owned Drive folder
     (create-or-update, append-merge — the Drive file stays the full archive);
  2. ingest the *new* messages into the `google_chat_messages` ChromaDB
     collection so the assistant can search/answer over chat history.

A space shared by N users is seen N times across the user loop; a process-wide
`seen_spaces` set guarantees we back up / ingest it exactly once per run.

Incremental:
  Per-space cursor (last seen `createTime`) lives in
  `var/state/chat_sync_cursors.json` (cross-process locked). We only fetch
  messages with `createTime > cursor`, and only advance the cursor after BOTH
  the Drive backup and the RAG ingest succeed — so a partial failure re-fetches
  next run instead of leaving a gap.

Quota note:
  RAG ingest embeds only the *new* messages each run (chunk ids are namespaced
  by the batch's anchor message), never the whole space — re-embedding a 2000+
  message space daily would burn Gemini embedding quota. Drive backup keeps the
  complete history via append-merge.

Entry points:
  sync_domain_chat(...)  — enumerate users and sync every space (used by rag_runner)
  sync_status()          — chunk count in the collection
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator

from agent_core.env_utils import env_bool, env_int
from agent_core.google_auth import (
    get_service,
    get_service_for_account,
    run_rpc_with_timeout,
)
from agent_core.ingest.vector_store import get_store
from agent_core.logging_and_paths import STATE_DIR
from agent_core.rag_gateway import metadata_access_fields
from agent_core.state_io import locked_json

_log = logging.getLogger(__name__)

CHUNK_SIZE = 600
CHUNK_OVERLAP = 80
_COLLECTION = "google_chat_messages"

_CHAT_SCOPES = (
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
)
_ADMIN_SCOPES = ("https://www.googleapis.com/auth/admin.directory.user.readonly",)

_CURSOR_FILE = os.path.join(STATE_DIR, "chat_sync_cursors.json")

# Bounded retry for transient Chat/Directory/Drive errors (429 rate limit,
# 5xx). Linear-ish backoff is plenty; the daily daemon can afford to wait.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 4
_RETRY_BASE_S = 2.0

# Wall-clock ceiling per Chat/Directory/Drive RPC. httplib2's socket timeout
# bounds each recv, but a dribbling response can stall one .execute() for many
# minutes at 0% CPU (rag_sync 2026-07-06 wedge). Mirrors drive_sync's
# RAG_DRIVE_RPC_TIMEOUT_S; chat had no such backstop. 0 disables.
_CHAT_RPC_TIMEOUT_S = env_int("RAG_CHAT_RPC_TIMEOUT_S", 150, min_value=0)

# ── image-attachment OCR (Phase 1: macOS Vision only, free) ──────────
# Chat 訊息常夾 ERP 截圖 / 報表圖。預設只存檔名佔位字、內容搜不到。開了這個旗標
# 就下載圖片 bytes → 走 drive_sync 的 macOS Vision OCR（本機、免費、不打 Gemini）
# → OCR 文字併進該則訊息一起 embed，變成可搜尋。預設關（不改既有行為、不加負載）。
_CHAT_IMAGE_OCR = env_bool("RAG_CHAT_IMAGE_OCR", False)
# 沿用 drive 的圖片大小門檻（同 env 名，行為一致）：太小是 icon、太大跳過。
_CHAT_IMG_MIN_BYTES = env_int("RAG_IMAGE_MIN_BYTES", 5 * 1024, min_value=0)
_CHAT_IMG_MAX_BYTES = env_int("RAG_IMAGE_MAX_BYTES", 10 * 1024 * 1024, min_value=0)
# 單一 space 單輪最多 OCR 幾張，避免某張圖很多的 space 把夜跑拖爆。
_CHAT_IMG_OCR_PER_SPACE_CAP = env_int("RAG_CHAT_IMAGE_OCR_CAP", 200, min_value=0)
# 單張 OCR 文字進 chunk 的上限，避免一張長截圖灌爆 embedding。
_CHAT_OCR_TEXT_CAP = 4000

# ── image-attachment Gemini caption fallback（Phase 2：OCR 無字才打） ──
# 純照片（鞋樣照/現場照）Vision OCR 抽不出字 → 整張圖在索引裡只剩檔名。開了這個
# 旗標，OCR 無字的圖改打 Gemini 產生一兩句描述，跟 OCR 文字一樣併進訊息行 embed，
# 讓純照片也能被語意搜尋。只在 OCR 空手時才呼叫（歷史普查無字圖僅 ~5%，日增 <1
# 張），掛在 OCR 迴圈內、需 RAG_CHAT_IMAGE_OCR 同時開。預設關。
_CHAT_IMAGE_CAPTION = env_bool("RAG_CHAT_IMAGE_CAPTION", False)
# 單一 space 單輪最多打幾張 Gemini（成本保險絲；正常增量遠低於此）。
_CHAT_IMG_CAPTION_PER_SPACE_CAP = env_int("RAG_CHAT_IMAGE_CAPTION_CAP", 20, min_value=0)
# 單張描述進 chunk 的上限。
_CHAT_CAPTION_TEXT_CAP = 1000
_CHAT_CAPTION_PROMPT = (
    "用繁體中文一到兩句描述這張圖片，聚焦可搜尋的具體資訊："
    "物品種類、顏色、材質、型號/編號、場景。不要開場白，直接給描述。"
)


# ── retry / pagination helpers ───────────────────────────────────────

def _execute(request) -> dict:
    """Execute a Google API request with bounded backoff on 429/5xx."""
    from googleapiclient.errors import HttpError

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            # Wall-clock bound each attempt (see _CHAT_RPC_TIMEOUT_S). A
            # RpcWallClockTimeout is not an HttpError, so it isn't retried here —
            # it propagates to the per-space/per-user handler, which records the
            # failure and moves on (the service caches were cleared, so a later
            # RPC rebuilds a fresh connection).
            return run_rpc_with_timeout(
                _CHAT_RPC_TIMEOUT_S, "chat_rpc", request.execute
            )
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = None
            if status not in _RETRY_STATUSES or attempt == _MAX_RETRIES - 1:
                raise
            last_exc = exc
            time.sleep(_RETRY_BASE_S * (attempt + 1))
    if last_exc:
        raise last_exc
    return {}


def _is_auth_or_setup_error(exc: Exception) -> bool:
    """True for the 'you didn't finish the console setup' class of failures.

    A 403 on the very first call usually means the Chat/Admin API isn't enabled
    or the SA client ID isn't authorised for the new scopes — distinct from a
    per-user permission blip, and worth surfacing loudly + short-circuiting.
    """
    from googleapiclient.errors import HttpError

    if not isinstance(exc, HttpError):
        return False
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) in (401, 403)
    except (TypeError, ValueError):
        return False


# ── cursor state ─────────────────────────────────────────────────────

def _load_cursors() -> dict[str, dict[str, Any]]:
    """Read the per-space cursor map (lock-free: writes are atomic)."""
    try:
        with open(_CURSOR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _save_cursors(cursors: dict[str, dict[str, Any]]) -> None:
    """Persist the cursor map atomically under a cross-process lock."""
    with locked_json(_CURSOR_FILE, default={}) as current:
        current.clear()
        current.update(cursors)


# ── enumeration ──────────────────────────────────────────────────────

def list_domain_users(
    admin_subject: str,
    service_account_file: str,
    *,
    customer: str = "my_customer",
    scopes: Any = _ADMIN_SCOPES,
) -> list[str]:
    """Return sorted primaryEmail of all active users via the Admin SDK.

    Impersonates ``admin_subject`` (must be a Workspace admin with Users:Read).
    Raises on the first call if the subject isn't an admin / scope missing —
    the caller turns that into an actionable, phase-aborting error.
    """
    directory = get_service_for_account(
        "chat_admin", "admin", "directory_v1",
        service_account_file=service_account_file,
        subject=admin_subject,
        scopes=list(scopes),
    )
    emails: list[str] = []
    page_token = None
    while True:
        kwargs: dict[str, Any] = dict(
            customer=customer,
            query="isSuspended=false",
            maxResults=500,
            orderBy="email",
            projection="basic",
        )
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _execute(directory.users().list(**kwargs))
        for u in resp.get("users", []):
            if u.get("suspended"):
                continue
            email = str(u.get("primaryEmail") or "").strip().lower()
            if email:
                emails.append(email)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return sorted(set(emails))


def list_user_spaces(chat_service, space_filter: str = "") -> list[dict[str, str]]:
    """List spaces the impersonated user belongs to (paged)."""
    spaces: list[dict[str, str]] = []
    page_token = None
    while True:
        kwargs: dict[str, Any] = dict(pageSize=1000)
        if space_filter:
            kwargs["filter"] = space_filter
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _execute(chat_service.spaces().list(**kwargs))
        for sp in resp.get("spaces", []):
            name = str(sp.get("name") or "").strip()
            if name:
                spaces.append({
                    "name": name,
                    "spaceType": str(sp.get("spaceType") or ""),
                    "displayName": str(sp.get("displayName") or ""),
                })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return spaces


def list_space_messages(
    chat_service,
    space_name: str,
    *,
    after_rfc3339: str = "",
    page_cap: int = 2000,
) -> list[dict[str, Any]]:
    """Return up to ``page_cap`` messages in ``space_name``, oldest first.

    When ``after_rfc3339`` is set, only messages strictly newer than it are
    returned (incremental). Oldest-first paging means a capped first run drains
    backlog over successive days without leaving holes.
    """
    messages: list[dict[str, Any]] = []
    page_token = None
    filter_expr = f'createTime > "{after_rfc3339}"' if after_rfc3339 else ""
    while len(messages) < page_cap:
        kwargs: dict[str, Any] = dict(
            parent=space_name,
            pageSize=min(1000, page_cap - len(messages)),
            orderBy="createTime asc",
        )
        if filter_expr:
            kwargs["filter"] = filter_expr
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _execute(chat_service.spaces().messages().list(**kwargs))
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages[:page_cap]


# ── text extraction + chunking ───────────────────────────────────────

def _space_label(display_name: str, space_name: str) -> str:
    """Stable human label; DMs usually lack displayName so derive from id."""
    label = (display_name or "").strip()
    if label:
        return label
    return space_name.replace("spaces/", "space ")


def _sender_label(msg: dict[str, Any]) -> str:
    sender = msg.get("sender") or {}
    name = str(sender.get("displayName") or "").strip()
    if name:
        return name
    # Fall back to the opaque resource id (e.g. "users/12345") — no per-sender
    # Directory lookup, that would multiply quota across every message.
    return str(sender.get("name") or "unknown").strip()


def _message_line(msg: dict[str, Any]) -> str:
    when = str(msg.get("createTime") or "").strip()
    who = _sender_label(msg)
    body = str(msg.get("formattedText") or msg.get("text") or "").strip()
    suffix = ""
    attachments = msg.get("attachment") or []
    names = [
        str(a.get("contentName") or "").strip()
        for a in attachments
        if isinstance(a, dict) and a.get("contentName")
    ]
    if names:
        suffix = " 「附件：" + "、".join(names) + "」"
    # 圖片附件若已被 enrich_messages_with_image_ocr 補上 OCR 文字，併進這行一起
    # embed → 截圖/報表圖的內容變得可搜尋（檔名後面接「附件OCR：…」）。
    ocr_parts = [
        f"{(a.get('contentName') or '圖片')}→{a['_ocr_text']}"
        for a in attachments
        if isinstance(a, dict) and str(a.get("_ocr_text") or "").strip()
    ]
    if ocr_parts:
        suffix += " 〔附件OCR：" + " ｜ ".join(ocr_parts) + "〕"
    # OCR 無字的純照片若有 Gemini 描述（enrich 的 caption 退路），同樣併進本行。
    caption_parts = [
        f"{(a.get('contentName') or '圖片')}→{a['_caption_text']}"
        for a in attachments
        if isinstance(a, dict) and str(a.get("_caption_text") or "").strip()
    ]
    if caption_parts:
        suffix += " 〔附件描述：" + " ｜ ".join(caption_parts) + "〕"
    if not body and not suffix:
        return ""
    return f"[{when}] {who}: {body}{suffix}".strip()


# ── image-attachment OCR enrichment (Phase 1) ────────────────────────

def _download_attachment_bytes(chat_service, resource_name: str, max_bytes: int) -> bytes:
    """Best-effort download of a Chat attachment's media bytes (size-capped).

    Uses the impersonated user's chat service (same one that listed the space —
    so it can see the attachment). Returns b"" if it grows past max_bytes.
    """
    import io

    from googleapiclient.http import MediaIoBaseDownload

    req = chat_service.media().download_media(resourceName=resource_name)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req, chunksize=1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
        if buf.tell() > max_bytes:
            return b""
    return buf.getvalue()


def caption_image_bytes(data: bytes, mime_type: str) -> str:
    """Gemini 圖片描述（OCR 無字的純照片退路）。失敗回 ""、絕不 raise。

    走 gemini_client 唯一入口；模型吃 RED_GEMINI_MODEL（daemon＝flash）。描述文字
    跟 _ocr_text 同一條 embed 路徑入索引，retrieval 端讀取時照既有慣例過 sanitize。
    """
    try:
        from agent_core.gemini_client import (
            GEMINI_MODEL,
            _gemini_generate,
            _get_genai_types,
            _prepare_image_for_gemini,
        )

        img, mime = _prepare_image_for_gemini(data, mime_type)
        response = _gemini_generate(
            model=GEMINI_MODEL,
            contents=[
                _get_genai_types().Part.from_bytes(data=img, mime_type=mime),
                _CHAT_CAPTION_PROMPT,
            ],
        )
        return str(getattr(response, "text", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — 描述失敗只損失一張圖的可搜性
        _log.warning("[chat_sync] 附件描述失敗: %s", exc)
        return ""


def enrich_messages_with_image_ocr(chat_service, messages: list[dict[str, Any]]) -> int:
    """In-place: OCR image attachments into ``att['_ocr_text']``. Returns count
    of enriched attachments (OCR text or Gemini caption).

    Gated by RAG_CHAT_IMAGE_OCR (default off). OCR is macOS Vision only — free,
    no Gemini. 若再開 RAG_CHAT_IMAGE_CAPTION，OCR 無字的圖改打 Gemini 產生描述
    （``att['_caption_text']``，另有 per-space 成本上限）。Best-effort: any
    per-attachment download/OCR failure is logged and skipped, never aborts the
    space. Bounded per call by a per-space cap and the shared image size limits.
    Runs BEFORE the Drive backup so the OCR/caption text is also persisted in
    the archive (future re-ingest needs no re-download).
    """
    if not _CHAT_IMAGE_OCR:
        return 0
    # Vision OCR 在 drive_sync（本機 ocrmac，免費）。延後 import 避免無謂載入。
    from agent_core.ingest.drive_sync import _extract_image_vision

    done = 0
    caption_attempts = 0  # 保險絲數「嘗試」不數「成功」——Gemini 故障時才不會打好打滿
    for m in messages:
        for a in m.get("attachment") or []:
            if done >= _CHAT_IMG_OCR_PER_SPACE_CAP:
                return done
            if not isinstance(a, dict):
                continue
            ct = str(a.get("contentType") or "")
            if not ct.startswith("image/"):
                continue
            res = (a.get("attachmentDataRef") or {}).get("resourceName")
            if not res:
                continue
            try:
                data = _download_attachment_bytes(chat_service, res, _CHAT_IMG_MAX_BYTES)
            except Exception as exc:  # noqa: BLE001 — 一張壞圖不該拖垮整個 space
                _log.warning("[chat_sync] 附件下載失敗 %s: %s", a.get("contentName"), exc)
                continue
            if not data or len(data) < _CHAT_IMG_MIN_BYTES:
                continue
            try:
                text = _extract_image_vision(data, ct)
            except Exception as exc:  # noqa: BLE001
                _log.warning("[chat_sync] 附件 OCR 失敗 %s: %s", a.get("contentName"), exc)
                continue
            if text and text.strip():
                a["_ocr_text"] = text.strip()[:_CHAT_OCR_TEXT_CAP]
                done += 1
            elif _CHAT_IMAGE_CAPTION and caption_attempts < _CHAT_IMG_CAPTION_PER_SPACE_CAP:
                caption_attempts += 1
                try:
                    caption = caption_image_bytes(data, ct)
                except Exception as exc:  # noqa: BLE001 — 防禦：函式自身已吞錯
                    _log.warning("[chat_sync] 附件描述例外 %s: %s", a.get("contentName"), exc)
                    caption = ""
                if caption:
                    a["_caption_text"] = caption[:_CHAT_CAPTION_TEXT_CAP]
                    done += 1
    return done


def _chunk_text(text: str, space_label: str = "", context: str = "") -> list[str]:
    """Split into ~CHUNK_SIZE windows, each prefixed with the space label.

    The prefix mirrors gmail_sync's subject prefix: it keeps every chunk tied
    to its space at the embedding level, helping retrieval on space/topic
    queries. `context` (Contextual Retrieval) is accepted for a uniform
    signature with drive/gmail, but chat ingest is an append-only message
    stream (not a document) so callers leave it empty — behaviour is unchanged.
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []
    if context:
        prefix = f"{context}\n\n"
        payload_size = CHUNK_SIZE
    else:
        safe_label = (space_label or "")[:80]
        prefix = f"[{safe_label}] " if safe_label else ""
        payload_size = max(1, CHUNK_SIZE - len(prefix))

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + payload_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(prefix + chunk)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


# ── RAG ingest (new messages only) ───────────────────────────────────

def _sanitize_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def ingest_space_messages(
    space_name: str,
    display_name: str,
    space_type: str,
    new_messages: list[dict[str, Any]],
    *,
    synced_at: str = "",
) -> int:
    """Ingest the given (new) messages as appended chunks. Returns chunk count.

    doc_id is the space resource name so retrieval groups by space. Chunk ids
    are namespaced by the batch's anchor message id, so re-running an
    un-advanced cursor is idempotent and old messages are never re-embedded.
    We intentionally do NOT call delete_stale_chunks here (chat is append-only
    in this design; deletions persist, matching the Drive append-merge archive).
    """
    label = _space_label(display_name, space_name)
    lines = [ln for ln in (_message_line(m) for m in new_messages) if ln]
    if not lines:
        return 0
    chunks = _chunk_text("\n".join(lines), space_label=label)
    if not chunks:
        return 0

    anchor = _sanitize_id(str(new_messages[0].get("name") or "batch"))
    now = synced_at or datetime.now(timezone.utc).isoformat()
    last_time = max(
        (str(m.get("createTime") or "") for m in new_messages), default=""
    )
    access_fields = metadata_access_fields(
        "chat", space_name=space_name, display_name=display_name,
    )

    ids, docs, metas = [], [], []
    for i, chunk in enumerate(chunks):
        ids.append(f"{space_name}__{anchor}__c{i}")
        docs.append(chunk)
        metas.append({
            "doc_id": space_name,
            "space_name": space_name,
            "display_name": label,
            "space_type": space_type,
            "chunk_index": i,
            "synced_at": now,
            "last_message_time": last_time,
            **access_fields,
        })
    get_store(_COLLECTION).upsert_batch(ids, docs, metas)
    return len(chunks)


# ── Drive JSON backup (create-or-update, append-merge) ───────────────

def _drive_service():
    return get_service("drive", "v3")


def _backup_filename(space_name: str) -> str:
    return f"chat_{_sanitize_id(space_name)}.json"


def _find_drive_file(drive, name: str, folder_id: str) -> str:
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    safe_folder = folder_id.replace("\\", "\\\\").replace("'", "\\'")
    resp = _execute(drive.files().list(
        q=f"name = '{safe_name}' and '{safe_folder}' in parents and trashed = false",
        fields="files(id)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ))
    files = resp.get("files", [])
    return str(files[0]["id"]) if files else ""


def _download_drive_json(drive, file_id: str) -> dict[str, Any]:
    try:
        raw = _execute(drive.files().get_media(fileId=file_id))
    except Exception as exc:  # noqa: BLE001 — corrupt/inaccessible backup must not abort
        _log.warning("[chat_sync] 既有備份讀取失敗 %s: %s", file_id, exc)
        return {}
    if isinstance(raw, bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
    return {}


def backup_space_to_drive(
    drive,
    space: dict[str, str],
    new_messages: list[dict[str, Any]],
    folder_id: str,
) -> dict[str, Any]:
    """Append-merge ``new_messages`` into the space's Drive JSON archive.

    Idempotent: existing file is updated in place (deduped by message name),
    a new file is created if absent. Returns the merged payload that was
    written so callers can inspect message counts.
    """
    from googleapiclient.http import MediaInMemoryUpload

    space_name = space["name"]
    filename = _backup_filename(space_name)
    file_id = _find_drive_file(drive, filename, folder_id)

    existing = _download_drive_json(drive, file_id) if file_id else {}
    prior_msgs = existing.get("messages") if isinstance(existing, dict) else None
    merged: dict[str, dict[str, Any]] = {}
    for m in (prior_msgs or []):
        key = str(m.get("name") or "")
        if key:
            merged[key] = m
    for m in new_messages:
        key = str(m.get("name") or "")
        if key:
            merged[key] = m

    ordered = sorted(merged.values(), key=lambda m: str(m.get("createTime") or ""))
    payload = {
        "space_name": space_name,
        "space_type": space.get("spaceType", ""),
        "display_name": _space_label(space.get("displayName", ""), space_name),
        "message_count": len(ordered),
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "messages": ordered,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaInMemoryUpload(body, mimetype="application/json", resumable=False)

    if file_id:
        _execute(drive.files().update(
            fileId=file_id, media_body=media, supportsAllDrives=True,
        ))
    else:
        _execute(drive.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ))
    return payload


# ── orchestration ────────────────────────────────────────────────────

def sync_user(
    user_email: str,
    drive,
    *,
    service_account_file: str,
    drive_folder_id: str,
    seen_spaces: set[str],
    cursors: dict[str, dict[str, Any]],
    errors: list[str],
    space_filter: str = "",
    per_space_cap: int = 2000,
) -> dict[str, int]:
    """Back up + ingest every not-yet-seen space the user belongs to."""
    chat = get_service_for_account(
        f"chat:{user_email}", "chat", "v1",
        service_account_file=service_account_file,
        subject=user_email,
        scopes=list(_CHAT_SCOPES),
    )
    spaces = list_user_spaces(chat, space_filter)

    stats = {"spaces": 0, "messages": 0}
    for space in spaces:
        space_name = space["name"]
        if space_name in seen_spaces:
            continue
        seen_spaces.add(space_name)
        try:
            cursor = str((cursors.get(space_name) or {}).get("last_message_time") or "")
            new_messages = list_space_messages(
                chat, space_name, after_rfc3339=cursor, page_cap=per_space_cap,
            )
            if not new_messages:
                continue

            # OCR 圖片附件（gated, macOS Vision 免費）→ 寫進 message 的
            # attachment._ocr_text，讓後面 backup 與 ingest 都帶到。整段 best-effort，
            # 失敗只記 log 不擋同步。
            if _CHAT_IMAGE_OCR:
                try:
                    n_ocr = enrich_messages_with_image_ocr(chat, new_messages)
                    if n_ocr:
                        _log.info("[chat_sync] %s: OCR %d 張圖片附件", space_name, n_ocr)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("[chat_sync] %s 圖片 OCR 階段失敗: %s", space_name, exc)

            # Drive backup + RAG ingest must BOTH succeed before the cursor
            # advances, so a partial failure re-fetches next run (no gap).
            backup_space_to_drive(drive, space, new_messages, drive_folder_id)
            chunks = ingest_space_messages(
                space_name, space.get("displayName", ""),
                space.get("spaceType", ""), new_messages,
            )

            latest = max(str(m.get("createTime") or "") for m in new_messages)
            cursors[space_name] = {
                "last_message_time": latest or cursor,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "msg_count": int((cursors.get(space_name) or {}).get("msg_count", 0))
                + len(new_messages),
            }
            # 每個 space 成功就即時落盤（_save_cursors 自帶跨程序鎖 + 原子寫）。
            # 只在整輪結束才存的話，daemon 中途被砍/當機會丟掉本輪所有已完成
            # space 的 cursor → 下輪整批重抓重 embed。存檔失敗只記警告：頂多
            # 下輪重抓，別讓它毀掉這個 space 的成功。
            try:
                _save_cursors(cursors)
            except Exception as exc:  # noqa: BLE001
                _log.warning("[chat_sync] %s cursor 落盤失敗: %s", space_name, exc)
            stats["spaces"] += 1
            stats["messages"] += len(new_messages)
            _log.info(
                "[chat_sync] %s: +%d msgs, %d chunks",
                space_name, len(new_messages), chunks,
            )
        except Exception as exc:  # noqa: BLE001 — one space must not sink the rest
            errors.append(f"Chat[{user_email} {space_name}]: {exc}")
            _log.warning("[chat_sync] %s %s 失敗: %s", user_email, space_name, exc)
    return stats


def sync_domain_chat(
    *,
    admin_subject: str,
    service_account_file: str,
    drive_folder_id: str,
    space_filter: str = "",
    per_space_cap: int = 2000,
    max_users: int = 0,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Enumerate active users and back up / ingest every space once.

    Per-user failures are non-fatal. A first-call auth/setup failure (admin
    not authorised, API disabled, scope missing) aborts the whole phase with an
    actionable error rather than silently syncing nothing.
    """
    errors = errors if errors is not None else []
    if not (admin_subject and service_account_file and drive_folder_id):
        errors.append("Chat: 設定不完整 (需 admin_subject/service_account_file/drive_folder_id)")
        return {"users": 0, "spaces": 0, "messages": 0, "errors": errors}

    try:
        users = list_domain_users(admin_subject, service_account_file)
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if _is_auth_or_setup_error(exc):
            hint = (
                " — 請確認 admin_subject 是 Workspace 管理員，且 service account 的 "
                "Client ID 已在網域委派加上 admin.directory.user.readonly scope，"
                "並已啟用 Admin SDK API"
            )
        errors.append(f"Chat: 無法列出網域使用者{hint}: {exc}")
        return {"users": 0, "spaces": 0, "messages": 0, "errors": errors}

    if max_users and max_users > 0:
        users = users[:max_users]

    drive = _drive_service()
    seen_spaces: set[str] = set()
    cursors = _load_cursors()

    total_spaces = 0
    total_messages = 0
    chat_setup_failed = False
    for user_email in users:
        if chat_setup_failed:
            break
        try:
            stats = sync_user(
                user_email, drive,
                service_account_file=service_account_file,
                drive_folder_id=drive_folder_id,
                seen_spaces=seen_spaces,
                cursors=cursors,
                errors=errors,
                space_filter=space_filter,
                per_space_cap=per_space_cap,
            )
            total_spaces += stats["spaces"]
            total_messages += stats["messages"]
        except Exception as exc:  # noqa: BLE001
            # A first-user 403 on spaces.list = Chat API not enabled / SA not
            # authorised for chat scopes → short-circuit the rest of the domain.
            if _is_auth_or_setup_error(exc):
                chat_setup_failed = True
                errors.append(
                    "Chat: spaces 讀取被拒，疑似 Chat API 未啟用或 SA 未授權 "
                    f"chat.*.readonly scope，已中止本輪: {exc}"
                )
            else:
                errors.append(f"Chat[{user_email}]: {exc}")

    _save_cursors(cursors)
    return {
        "users": len(users),
        "spaces": total_spaces,
        "messages": total_messages,
        "errors": errors,
    }


def sync_status() -> dict[str, Any]:
    return {"collection": _COLLECTION, "total_chunks": get_store(_COLLECTION).count()}
