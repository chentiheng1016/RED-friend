"""Persisted chat history for the Telegram daemon.

Storage layout (per-chat JSON):
  var/state/tg_chat_history/<chat_id>.json
    {"updated_ts": float, "turns": [{"role": "user"|"model", "text": str}, ...]}

Why per-chat files (vs. a single tg_chat_history.json)
  - The legacy single file became a hot RMW point — every turn for every
    chat re-wrote the whole dict, and concurrent writes from different
    chats could lose each other's appends. Per-chat files mean each chat's
    write doesn't touch other chats' state.
  - A lazy one-time migration in `_migrate_legacy_entry_if_present`
    copies a chat's data out of the legacy file the first time the new
    code touches it, then removes the legacy entry only after the per-chat
    write succeeds (so a crash mid-migration can't lose data).

Compaction
  - In-memory chat is rebuilt every `_TG_MAX_TURNS_BEFORE_REBUILD` turns
    (or after 30 min idle, or on daemon restart). On rebuild we pre-seed
    the new chat with the persisted turns so 小紅 doesn't forget context.
  - Function-call / function-response parts are intentionally NOT persisted;
    they're fragile across SDK versions and user-facing text alone carries
    enough context. The model can re-call any tool if it needs to.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from agent_core.env_utils import env_int as _env_int
from agent_core.logging_and_paths import STATE_DIR as _STATE_DIR


_TG_CHAT_HISTORY_MAX_TURNS_PER_CHAT = _env_int(
    "RED_TG_CHAT_HISTORY_MAX_TURNS", 40, min_value=2, max_value=400
)
_TG_CHAT_HISTORY_TTL_S = _env_int(
    "RED_TG_CHAT_HISTORY_TTL_DAYS", 7, min_value=1, max_value=90
) * 86400

# 重建 session 時**餵回去**的歷史上限（字元）。
#
# 2026-08-04 查帳：telegram_chat.yellow 一天 $75.92／只有 17 次呼叫，單次 prompt
# 6.4萬→12.3萬 tokens。真兇不是工具面，是這裡：turn 數上限（40 對話×每則 8,000
# 字元）只管**則數**不管**份量**，ERP 查詢那種長表格答案幾則就把底盤墊起來——
# 實測 UserA 的歷史檔 71,504 字元 ≈ 3.6 萬 tokens，於是每次 rebuild 後光開場白
# 就 6.1 萬 tokens（×$47.8/M ≈ $2.9，還沒開始講話）。
#
# 改成「則數 ∩ 字元預算」：從最新往回收，收到預算滿為止（保留最近的對話，丟最舊
# 的）。磁碟上的紀錄不動——只在餵進 session 時裁，之後要調參或做別的用途都還在。
_TG_CHAT_HISTORY_MAX_CHARS = _env_int(
    "RED_TG_CHAT_HISTORY_MAX_CHARS", 24000, min_value=1000, max_value=2_000_000
)
# 部門員工再收一半：他們的對話幾乎都是「查一個數字」，不需要跨半天的上下文，
# 而每輪成本跟大王一樣貴。
_TG_CHAT_HISTORY_MAX_CHARS_EMPLOYEE = _env_int(
    "RED_TG_CHAT_HISTORY_MAX_CHARS_EMPLOYEE", 12000, min_value=1000, max_value=2_000_000
)
# 走 logging_and_paths.STATE_DIR（吃 RED_RUNTIME_DIR）而不是從 __file__ 往上
# 數三層拼 var/state：相對 traversal 繞過 runtime 根（測試會寫進 live state、
# Cloud Run 會寫進 image 內），層數也會在模組搬家時默默算錯。RED_RUNTIME_DIR
# 沒設時解析結果與舊值逐字相同，不需搬檔。
_TG_CHAT_HISTORY_DIR = os.path.join(_STATE_DIR, "tg_chat_history")
_TG_CHAT_HISTORY_FILE_LEGACY = os.path.join(_STATE_DIR, "tg_chat_history.json")

_tg_chat_history_lock = threading.Lock()
_tg_chat_history_legacy_cache: dict[str, Any] = {"mtime": 0.0, "data": {}}


def _chat_history_path(chat_id: str) -> str:
    """Map a chat_id to its per-chat history file path. Sanitizes to digits
    (plus a leading minus for group chats) so a malicious chat_id can't
    escape _TG_CHAT_HISTORY_DIR via traversal."""
    s_id = str(chat_id).strip()
    safe = "".join(
        ch for i, ch in enumerate(s_id)
        if ch.isdigit() or (i == 0 and ch == "-")
    ) or "unknown"
    return os.path.join(_TG_CHAT_HISTORY_DIR, f"{safe}.json")


def _atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON atomically via tmp + rename. Per-chat files are written
    serially under _tg_chat_history_lock, so a separate cross-process lock
    isn't needed here — but see state_io.locked_json for the general
    pattern when multiple processes share a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_tg_chat_history_entry(chat_id: str) -> dict[str, Any]:
    """Load one chat's persisted history entry.

    Shape: {"updated_ts": float, "turns": [{role, text}, ...]}
    Returns {} on missing file or any parse error — we treat corrupted state
    as "no memory" rather than crashing the daemon.
    """
    path = os.path.abspath(_chat_history_path(chat_id))
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[tg bot] ⚠️ chat history 載入失敗（用空記憶）：{exc}")
        return {}


def _load_tg_chat_histories_legacy() -> dict[str, dict[str, Any]]:
    """Read the legacy single-file history (cached by mtime). Used only to
    migrate per-chat entries out on first touch."""
    path = os.path.abspath(_TG_CHAT_HISTORY_FILE_LEGACY)
    if not os.path.isfile(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _tg_chat_history_legacy_cache
    if cached.get("mtime") == mtime and isinstance(cached.get("data"), dict):
        return cached["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = data if isinstance(data, dict) else {}
        _tg_chat_history_legacy_cache["mtime"] = mtime
        _tg_chat_history_legacy_cache["data"] = data
        return data
    except Exception:
        return {}


def _clear_tg_chat_history_legacy(chat_id: str) -> None:
    path = os.path.abspath(_TG_CHAT_HISTORY_FILE_LEGACY)
    if not os.path.isfile(path):
        return
    try:
        data = _load_tg_chat_histories_legacy()
        if not isinstance(data, dict) or chat_id not in data:
            return
        data.pop(chat_id, None)
        _atomic_write_json(path, data)
        _tg_chat_history_legacy_cache["mtime"] = 0.0
        _tg_chat_history_legacy_cache["data"] = {}
    except Exception as exc:
        print(f"[tg bot] ⚠️ legacy chat history 刪除失敗：{exc}")


def _migrate_legacy_entry_if_present(chat_id: str) -> dict[str, Any]:
    """If legacy history exists for this chat, persist it to per-chat file once.

    IMPORTANT: never delete the legacy entry unless the per-chat write succeeds.
    """
    legacy = _load_tg_chat_histories_legacy()
    entry = legacy.get(chat_id) if isinstance(legacy, dict) else None
    if not isinstance(entry, dict):
        return {}
    ok = _persist_tg_chat_history_entry(chat_id, entry)
    if ok:
        _clear_tg_chat_history_legacy(chat_id)
    return entry


def _persist_tg_chat_history_entry(chat_id: str, entry: dict[str, Any]) -> bool:
    path = os.path.abspath(_chat_history_path(chat_id))
    try:
        _atomic_write_json(path, entry)
        return True
    except Exception as exc:
        print(f"[tg bot] ⚠️ chat history 寫入失敗：{exc}")
        return False


def _record_tg_chat_turn(chat_id: str, user_text: str, model_reply: str) -> None:
    """Append a user+model turn pair to persisted history for this chat.

    Caps total turns per chat at _TG_CHAT_HISTORY_MAX_TURNS_PER_CHAT — older
    turns drop off. Empty replies are still recorded so a tool-call-only
    response doesn't break the user-bot turn pairing.
    """
    if not chat_id:
        return
    with _tg_chat_history_lock:
        entry = _load_tg_chat_history_entry(chat_id)
        if not entry:
            entry = _migrate_legacy_entry_if_present(chat_id) or {}
        turns = entry.get("turns") if isinstance(entry.get("turns"), list) else []
        turns.append({"role": "user", "text": str(user_text or "")[:8000]})
        turns.append({"role": "model", "text": str(model_reply or "")[:8000]})
        if len(turns) > 2 * _TG_CHAT_HISTORY_MAX_TURNS_PER_CHAT:
            turns = turns[-(2 * _TG_CHAT_HISTORY_MAX_TURNS_PER_CHAT):]
        entry = {"updated_ts": time.time(), "turns": turns}
        _persist_tg_chat_history_entry(chat_id, entry)


def _load_tg_chat_history_for_rebuild(
    chat_id: str, max_chars: int | None = None
) -> list[dict[str, Any]]:
    """Format persisted turns into the Gemini SDK's expected `history=` shape.

    Returns [] when there's no usable memory — caller passes that through
    to `chats.create` which interprets it as a fresh chat.

    max_chars：餵回去的總字元預算（None = `_TG_CHAT_HISTORY_MAX_CHARS`）。從最新
    往回收，超出預算的舊 turn 直接不餵（磁碟紀錄不動）。呼叫端對部門員工要傳
    `_TG_CHAT_HISTORY_MAX_CHARS_EMPLOYEE`。理由見該常數的註解。
    """
    if not chat_id:
        return []
    with _tg_chat_history_lock:
        entry = _load_tg_chat_history_entry(chat_id)
        if not entry:
            entry = _migrate_legacy_entry_if_present(chat_id) or {}
    if not entry:
        return []
    turns = entry.get("turns")
    if not isinstance(turns, list):
        return []
    try:
        ts = float(entry.get("updated_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts and time.time() - ts > _TG_CHAT_HISTORY_TTL_S:
        return []
    budget = _TG_CHAT_HISTORY_MAX_CHARS if max_chars is None else int(max_chars)
    kept: list[dict[str, Any]] = []
    used = 0
    dropped = 0
    # 由新到舊收：留「最近且連續」的一段，預算滿了就整段停手（不跳過大 turn 再撿
    # 小的——那會讓對話中間破洞，讀起來像跳針）。至少保留最新一則。
    usable = [t for t in turns
              if isinstance(t, dict)
              and t.get("role") in ("user", "model") and (t.get("text") or "")]
    for i, turn in enumerate(reversed(usable)):
        text = str(turn.get("text") or "")
        if kept and used + len(text) > budget:
            dropped = len(usable) - i
            break
        used += len(text)
        kept.append({"role": turn.get("role"), "parts": [{"text": text}]})
    kept.reverse()
    if dropped:
        print(f"[tg bot] ✂️ chat history 依 {budget:,} 字元預算裁掉最舊 {dropped} 則"
              f"（餵回 {len(kept)} 則 / {used:,} 字元）")
    return kept


def _clear_tg_chat_history(chat_id: str) -> None:
    """Drop persisted memory for one chat (called on /reset)."""
    if not chat_id:
        return
    with _tg_chat_history_lock:
        try:
            path = os.path.abspath(_chat_history_path(chat_id))
            if os.path.isfile(path):
                os.unlink(path)
        except Exception as exc:
            print(f"[tg bot] ⚠️ chat history 刪除失敗：{exc}")
        _clear_tg_chat_history_legacy(chat_id)
