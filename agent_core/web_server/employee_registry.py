"""Employee registry — maps Google email → department color + display name.

Backends:
  - file: var/data/employee_registry.json by default, or RED_EMPLOYEE_REGISTRY_FILE.
  - firestore: RED_EMPLOYEE_REGISTRY_BACKEND=firestore.
Schema:
  {
    "alice@company.com": {
      "name": "Alice", "color": "green",
      "line_user_id": "...", "telegram_user_id": "..."
    },
    "bob@company.com":   {"name": "Bob",   "color": "orange"}
  }

Colors must match Agent enum values: green, orange, white, yellow,
blue, gray, black, purple, indigo. "red" is reserved for 大王 only.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from agent_core.logging_and_paths import DATA_DIR
from agent_core.state_io import locked_json
# chat-id 驗證 regex 與 telegram_agent_config 共用單一定義（避免兩份漂移）。
from agent_core.telegram_agent_config import _TELEGRAM_CHAT_ID_RE


_OPTIONAL_STRING_FIELDS = ("line_user_id", "telegram_user_id", "signature")


def _normalize_record(email: str, info: dict[str, Any]) -> dict[str, str]:
    normalized_email = email.lower().strip()
    record = {
        "name": str(info.get("name") or normalized_email),
        "color": str(info.get("color") or ""),
    }
    for field in _OPTIONAL_STRING_FIELDS:
        value = str(info.get(field) or "").strip()
        if value:
            record[field] = value
    return record


def _registry_file() -> str:
    return os.environ.get("RED_EMPLOYEE_REGISTRY_FILE") or os.path.join(DATA_DIR, "employee_registry.json")


def _backend() -> str:
    return os.environ.get("RED_EMPLOYEE_REGISTRY_BACKEND", "file").strip().lower()


def _firestore_collection():
    from google.cloud import firestore

    project = os.environ.get("RED_FIRESTORE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or None
    database = os.environ.get("RED_FIRESTORE_DATABASE") or "(default)"
    collection = os.environ.get("RED_EMPLOYEE_REGISTRY_COLLECTION") or "red_employee_registry"
    client = firestore.Client(project=project, database=database)
    return client.collection(collection)


def _load_file_registry() -> dict[str, dict[str, str]]:
    path = _registry_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return {
        email.lower().strip(): _normalize_record(email, info)
        for email, info in data.items()
        if isinstance(info, dict)
    }


def _load_firestore_registry() -> dict[str, dict[str, str]]:
    registry: dict[str, dict[str, str]] = {}
    for doc in _firestore_collection().stream():
        data = doc.to_dict() or {}
        email = (data.get("email") or doc.id).lower().strip()
        if email and data.get("color"):
            registry[email] = _normalize_record(email, data)
    return registry


# ── file backend 快取（mtime+size gate）────────────────────────────────
# load_registry() 被每則 Telegram 訊息（重建 actor 表）、每個 web portal
# request、每則 LINE 訊息各自呼叫，原本每次都 open()+json.load() 整份再線性
# 掃。以 (mtime_ns, size) 為戳記快取解析結果 + line/telegram 反查索引；
# register_employee / set_employee_signature 走 locked_json 改檔 → mtime 變 →
# 下次自動失效（同一檔跨 process 也安全）。
_REGISTRY_CACHE_LOCK = threading.Lock()
_reg_cache: dict[str, Any] = {"stamp": None, "registry": None,
                              "by_line": None, "by_telegram": None}


def _file_registry_snapshot() -> tuple[dict, dict, dict]:
    """回 (registry, by_line_index, by_telegram_index)，file backend mtime 快取。

    回傳的是共享快取物件，呼叫端只可讀；對外的 load_registry / get_employee*
    都會 copy 後才交出，故快取不會被污染。索引按 email 排序建立，與舊
    list_employees() 線性掃的「字母序第一個命中」語義一致。
    """
    path = _registry_file()
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None  # 檔案不存在 → 空 registry
    with _REGISTRY_CACHE_LOCK:
        if _reg_cache["registry"] is None or _reg_cache["stamp"] != stamp:
            registry = _load_file_registry()
            by_line: dict[str, dict] = {}
            by_telegram: dict[str, dict] = {}
            for email, rec in sorted(registry.items()):
                full = {"email": email, **rec}
                lid = str(rec.get("line_user_id") or "").strip()
                tid = str(rec.get("telegram_user_id") or "").strip()
                if lid:
                    by_line.setdefault(lid, full)
                if tid:
                    by_telegram.setdefault(tid, full)
            _reg_cache.update(stamp=stamp, registry=registry,
                              by_line=by_line, by_telegram=by_telegram)
        return _reg_cache["registry"], _reg_cache["by_line"], _reg_cache["by_telegram"]


def load_registry() -> dict[str, dict[str, str]]:
    if _backend() == "firestore":
        return _load_firestore_registry()
    registry, _, _ = _file_registry_snapshot()
    # 回淺層 copy：呼叫端 mutate 不污染快取（record 值皆為 str，淺 copy 即足）。
    return {email: dict(rec) for email, rec in registry.items()}


def _save_file_registry(data: dict[str, dict[str, str]]) -> None:
    """Overwrite the whole registry file. Use only when you don't need RMW
    semantics (e.g., a top-level bulk save); for partial updates use
    `register_employee` which holds the cross-process lock via locked_json."""
    path = _registry_file()
    with locked_json(path, default={}) as current:
        current.clear()
        current.update(data)


def _save_firestore_registry(data: dict[str, dict[str, str]]) -> None:
    collection = _firestore_collection()
    for email, info in data.items():
        normalized_email = email.lower().strip()
        collection.document(normalized_email).set(
            {
                "email": normalized_email,
                **_normalize_record(normalized_email, info),
            },
        )


def save_registry(data: dict[str, dict[str, str]]) -> None:
    if _backend() == "firestore":
        _save_firestore_registry(data)
        return
    _save_file_registry(data)


def is_valid_telegram_user_id(value: str) -> bool:
    """Telegram chat/user IDs are signed decimal integers."""
    raw = str(value or "").strip()
    if not raw:
        return True
    if not _TELEGRAM_CHAT_ID_RE.fullmatch(raw):
        return False
    try:
        return int(raw) != 0
    except ValueError:
        return False


def _validate_telegram_user_id_format(telegram_user_id: str) -> str:
    """Return the trimmed chat_id, or raise ValueError on bad format. Empty is ok."""
    chat_id = str(telegram_user_id or "").strip()
    if not chat_id:
        return ""
    if not is_valid_telegram_user_id(chat_id):
        raise ValueError(
            "telegram_user_id 格式錯誤：請填 Telegram 的數字 chat/user ID；"
            "群組通常是負數 chat.id。可先對 bot 送 /whoami 查詢。"
        )
    return chat_id


def _check_telegram_user_id_unique(
    registry: dict[str, dict[str, str]], chat_id: str, exclude_email: str
) -> None:
    """Raise if chat_id is already bound to a different employee in `registry`.

    Takes the registry as an argument (not list_employees()) so the caller
    can hold the cross-process lock and do uniqueness-check + write atomically.
    Without that, two admins binding the same chat_id concurrently both pass
    the check and the second write wipes the first.
    """
    if not chat_id:
        return
    for email, info in registry.items():
        if email == exclude_email:
            continue
        if str((info or {}).get("telegram_user_id") or "").strip() == chat_id:
            owner = email or (info or {}).get("name") or "another employee"
            raise ValueError(f"telegram_user_id 已被 {owner} 使用，不能重複綁定。")


def get_employee(email: str) -> dict[str, str] | None:
    """Return employee record or None if not registered."""
    normalized_email = email.lower().strip()
    if _backend() == "firestore":
        doc = _firestore_collection().document(normalized_email).get()
        if not doc.exists:
            return None
        rec = _normalize_record(normalized_email, doc.to_dict() or {})
    else:
        # #182：load_registry 內部走 _file_registry_snapshot 快取 + 回 copy（避免
        # 每訊息重讀檔）。
        rec = load_registry().get(normalized_email)
    # Fail-closed：沒有有效 color 的記錄不算「已註冊」。否則一筆 color-less 的（多半是
    # 手動寫進 Firestore 的）doc 會 truthy 通過 auth_callback 的註冊閘 → 變成空 color 的
    # 壞死登入（健檢 Low：Firestore _load 有濾、檔案後端沒濾，兩邊在此對齊收口）。
    if not rec or not str(rec.get("color") or "").strip():
        return None
    return rec


def register_employee(
    email: str,
    name: str,
    color: str,
    *,
    line_user_id: str = "",
    telegram_user_id: str = "",
) -> None:
    """Add or update an employee. color must be a valid Agent value (including red for GMs).

    Optional bindings (line_user_id, telegram_user_id) preserve the existing value
    when passed as empty string, so a re-add through the admin "新增" form does not
    silently wipe a previously-registered LINE/Telegram userId.
    """
    from agent_core.agents.permission_matrix import Agent
    Agent(color)  # raises ValueError if invalid
    normalized_email = email.lower().strip()
    chat_id = _validate_telegram_user_id_format(telegram_user_id)

    if _backend() == "firestore":
        existing = get_employee(normalized_email) or {}
        # Firestore has its own concurrency control; no cross-process lock needed.
        # Uniqueness check still hits a stale snapshot, but the same window existed
        # before this refactor — out of scope for the lost-update fix.
        if chat_id:
            for emp in list_employees():
                if emp.get("email") == normalized_email:
                    continue
                if str(emp.get("telegram_user_id") or "").strip() == chat_id:
                    owner = emp.get("email") or emp.get("name") or "another employee"
                    raise ValueError(f"telegram_user_id 已被 {owner} 使用，不能重複綁定。")
        info = _normalize_record(
            normalized_email,
            {
                "name": name,
                "color": color,
                "line_user_id": line_user_id or existing.get("line_user_id", ""),
                "telegram_user_id": chat_id or existing.get("telegram_user_id", ""),
                # 簽名檔由員工自助設定（set_employee_signature）；re-add 時保留，
                # 別被 admin「新增」表單靜默清掉。
                "signature": existing.get("signature", ""),
            },
        )
        _firestore_collection().document(normalized_email).set({"email": normalized_email, **info})
        return

    # File backend: hold one cross-process lock across read → uniqueness-check
    # → write. The lock is what stops two admins (web + REPL + daemon) from
    # both binding the same telegram_user_id, or one update silently dropping
    # the other's add via lost-update.
    path = _registry_file()
    with locked_json(path, default={}) as registry:
        existing_raw = registry.get(normalized_email) or {}
        existing = _normalize_record(normalized_email, existing_raw) if existing_raw else {}
        _check_telegram_user_id_unique(registry, chat_id, exclude_email=normalized_email)
        info = _normalize_record(
            normalized_email,
            {
                "name": name,
                "color": color,
                "line_user_id": line_user_id or existing.get("line_user_id", ""),
                "telegram_user_id": chat_id or existing.get("telegram_user_id", ""),
                # 簽名檔由員工自助設定（set_employee_signature）；re-add 時保留，
                # 別被 admin「新增」表單靜默清掉。
                "signature": existing.get("signature", ""),
            },
        )
        registry[normalized_email] = info


def set_employee_signature(email: str, signature: str) -> None:
    """更新單一員工的寄信簽名檔（只改 signature 欄，保留其餘）。

    供員工自助設定自己的簽名（actor-scoped set_my_signature 工具）。空字串＝
    清掉自訂簽名（之後寄信走最小 fallback）。File backend 走 locked_json 的
    讀-改-寫，避免和 register_employee 並發時 lost-update。員工須已存在。
    """
    normalized_email = email.lower().strip()
    sig = str(signature or "").strip()
    if not normalized_email:
        raise ValueError("email 不可為空")

    if _backend() == "firestore":
        existing = get_employee(normalized_email)
        if existing is None:
            raise ValueError(f"員工不存在：{normalized_email}")
        _firestore_collection().document(normalized_email).set(
            {"signature": sig}, merge=True
        )
        return

    path = _registry_file()
    with locked_json(path, default={}) as registry:
        record = registry.get(normalized_email)
        if not isinstance(record, dict):
            raise ValueError(f"員工不存在：{normalized_email}")
        if sig:
            record["signature"] = sig
        else:
            record.pop("signature", None)
        registry[normalized_email] = record


def get_employee_by_line_user_id(line_user_id: str) -> dict[str, str] | None:
    """Return employee record by LINE userId, including email, or None."""
    needle = str(line_user_id or "").strip()
    if not needle:
        return None
    if _backend() == "firestore":
        for employee in list_employees():
            if str(employee.get("line_user_id") or "").strip() == needle:
                return employee
        return None
    _, by_line, _ = _file_registry_snapshot()
    rec = by_line.get(needle)
    return dict(rec) if rec is not None else None


def get_employee_by_telegram_user_id(telegram_user_id: str) -> dict[str, str] | None:
    """Return employee record by Telegram chat/user ID, including email, or None."""
    needle = str(telegram_user_id or "").strip()
    if not needle:
        return None
    if _backend() == "firestore":
        for employee in list_employees():
            if str(employee.get("telegram_user_id") or "").strip() == needle:
                return employee
        return None
    _, _, by_telegram = _file_registry_snapshot()
    rec = by_telegram.get(needle)
    return dict(rec) if rec is not None else None


def list_employees() -> list[dict[str, Any]]:
    return [
        {"email": email, **info}
        for email, info in sorted(load_registry().items())
    ]
