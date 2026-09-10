"""Red-owned RAG access gateway.

Central Email/Drive RAG is useful only if department agents cannot bypass the
data boundary. This module owns the two low-level pieces needed for that:

* metadata tags stamped during ingest (``access_orange=True`` etc.)
* query-time filters + local JSONL audit records

Unclassified legacy chunks are intentionally visible only to Red because they
do not carry explicit access metadata.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from agent_core.agents.permission_matrix import Agent
from agent_core.logging_and_paths import DATA_DIR
from agent_core.provenance import METADATA_FIELD as PROVENANCE_FIELD

_AUDIT_LOCK = threading.Lock()
_AGENT_VALUES = {agent.value for agent in Agent}

# rag_access.json is read once per file during nightly RAG sweeps (50k+ files).
# Cache it keyed by mtime so each sweep does one open()+json.load() instead of
# 50k; a mid-sweep edit still takes effect because the key includes mtime.
_ACCESS_CONFIG_LOCK = threading.Lock()
_ACCESS_CONFIG_CACHE: dict[str, tuple[int, Mapping[str, Any]]] = {}


def rag_access_config_path() -> str:
    return os.environ.get("RED_RAG_ACCESS_CONFIG") or os.path.join(
        DATA_DIR,
        "rag_access.json",
    )


def rag_access_audit_path() -> str:
    return os.environ.get("RED_RAG_ACCESS_AUDIT_FILE") or os.path.join(
        DATA_DIR,
        "rag_access_audit.jsonl",
    )


def _coerce_agent(value: Agent | str | None, default: Agent = Agent.RED) -> Agent:
    if isinstance(value, Agent):
        return value
    try:
        return Agent(str(value or "").strip().lower())
    except Exception:
        return default


def _load_access_config() -> Mapping[str, Any]:
    """回唯讀的 rag_access 設定（mtime 快取）。

    唯一呼叫路徑（metadata_access_fields → _match_*_rule）只讀不改，故回快取的
    canonical 物件本身、不再 per-檔 deepcopy（夜跑 5 萬+ 檔，deepcopy ~51µs/檔
    純屬浪費）。用 MappingProxyType 包一層唯讀視角，仍防呼叫端意外的頂層 mutate
    污染快取。
    """
    path = rag_access_config_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    with _ACCESS_CONFIG_LOCK:
        cached = _ACCESS_CONFIG_CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    proxy = MappingProxyType(data if isinstance(data, dict) else {})
    with _ACCESS_CONFIG_LOCK:
        _ACCESS_CONFIG_CACHE[path] = (mtime, proxy)
    return proxy


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [value]


def _normalize_allowed_colors(
    values: Any,
    *,
    owner_color: str = "",
) -> set[str]:
    colors = {"red"}
    owner = str(owner_color or "").strip().lower()
    if owner in _AGENT_VALUES:
        colors.add(owner)
    for item in _as_list(values):
        color = str(item or "").strip().lower()
        if color in _AGENT_VALUES:
            colors.add(color)
    return colors


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _match_drive_rule(
    config: Mapping[str, Any],
    *,
    file_id: str = "",
    drive_id: str = "",
    folder_id: str = "",
) -> Mapping[str, Any]:
    rules = config.get("drive_sources") or config.get("drive") or []
    if not isinstance(rules, list):
        return {}
    best: Mapping[str, Any] = {}
    best_score = 0
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        score = 0
        if rule.get("file_id"):
            if str(rule.get("file_id")) != str(file_id):
                continue
            score += 4
        if rule.get("folder_id"):
            if str(rule.get("folder_id")) != str(folder_id):
                continue
            score += 2
        if rule.get("drive_id"):
            if str(rule.get("drive_id")) != str(drive_id):
                continue
            score += 1
        if score > best_score:
            best = rule
            best_score = score
    return best


def _match_gmail_rule(
    config: Mapping[str, Any],
    *,
    mailbox_email: str = "",
) -> Mapping[str, Any]:
    rules = config.get("gmail_mailboxes") or config.get("gmail") or []
    if not isinstance(rules, list):
        return {}
    mailbox = str(mailbox_email or "").strip().lower()
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        rule_mailbox = str(rule.get("mailbox_email") or rule.get("email") or "").strip().lower()
        if rule_mailbox and rule_mailbox == mailbox:
            return rule
    return {}


def _match_chat_rule(
    config: Mapping[str, Any],
    *,
    space_name: str = "",
    display_name: str = "",
) -> Mapping[str, Any]:
    rules = config.get("chat_spaces") or config.get("chat") or []
    if not isinstance(rules, list):
        return {}
    best: Mapping[str, Any] = {}
    best_score = 0
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        score = 0
        if rule.get("space_name"):
            if str(rule.get("space_name")) != str(space_name):
                continue
            score += 2
        if rule.get("display_name"):
            if str(rule.get("display_name")) != str(display_name):
                continue
            score += 1
        if score > best_score:
            best = rule
            best_score = score
    return best


def metadata_access_fields(
    source_kind: str,
    *,
    owner_color: str = "",
    department: str = "",
    allowed_colors: Any = None,
    mailbox_email: str = "",
    drive_id: str = "",
    folder_id: str = "",
    file_id: str = "",
    space_name: str = "",
    display_name: str = "",
) -> dict[str, Any]:
    """Return Chroma-safe access metadata for one indexed chunk.

    Config file shape is intentionally small and hand-editable:

    {
      "gmail_mailboxes": [
        {"mailbox_email": "sales@example.com", "owner_color": "orange",
         "department": "業務", "allowed_colors": ["orange", "purple"]}
      ],
      "drive_sources": [
        {"drive_id": "0A...", "owner_color": "green",
         "department": "樣品室", "allowed_colors": ["green", "orange"]}
      ],
      "chat_spaces": [
        {"space_name": "spaces/AAQAtjkOnQ4", "display_name": "業務群組",
         "owner_color": "orange", "department": "業務",
         "allowed_colors": ["orange"]}
      ]
    }

    Unmatched sources (no rule found for the kind, or kind unrecognized) fall
    back to owner_color=red / allowed_colors=[red] — visible only to Red until
    someone adds an explicit rule. This matters most for chat_spaces: new
    spaces show up here with no config entry until a human audits them, so
    the safe default must NOT be "visible to everyone."
    """
    kind = str(source_kind or "").strip().lower()
    config = _load_access_config()
    rule: Mapping[str, Any] = {}
    if kind == "gmail":
        rule = _match_gmail_rule(config, mailbox_email=mailbox_email)
    elif kind == "drive":
        rule = _match_drive_rule(
            config,
            file_id=file_id,
            drive_id=drive_id,
            folder_id=folder_id,
        )
    elif kind == "chat":
        rule = _match_chat_rule(
            config,
            space_name=space_name,
            display_name=display_name,
        )

    rule_owner = _first_text(rule.get("owner_color"), rule.get("color"))
    owner = _coerce_agent(rule_owner or owner_color, default=Agent.RED).value
    dept = _first_text(rule.get("department"), rule.get("dept"), department)
    raw_allowed = rule.get("allowed_colors") if rule else allowed_colors
    if raw_allowed in (None, ""):
        raw_allowed = allowed_colors
    allowed = _normalize_allowed_colors(raw_allowed, owner_color=owner)

    fields: dict[str, Any] = {
        "rag_source": kind,
        "owner_color": owner,
        "department": dept,
        "access_red": True,
    }
    if mailbox_email:
        fields["mailbox_email"] = str(mailbox_email).strip().lower()
    for color in sorted(_AGENT_VALUES):
        fields[f"access_{color}"] = color in allowed
    return fields


def metadata_access_matches(
    metadata: Mapping[str, Any] | None,
    expected: Mapping[str, Any],
) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    keys = [
        "rag_source",
        "owner_color",
        "department",
        "mailbox_email",
        *(f"access_{agent.value}" for agent in Agent),
    ]
    for key in keys:
        if key in expected and metadata.get(key) != expected.get(key):
            return False
    return True


def _combine_where(
    left: Mapping[str, Any] | None,
    right: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if left and right:
        return {"$and": [dict(left), dict(right)]}
    if left:
        return dict(left)
    if right:
        return dict(right)
    return None


# 小紅自產內容（排程報表）在向量庫裡照存不誤——「上週寄了什麼報表」還是要查
# 得到——但語意檢索預設要濾掉：報表是從 lake / Drive 算出來的衍生品，讓它跟
# 原始資料同分競爭，等於把自己的輸出當成新證據，錯的數字會一輪一輪被覆述。
#
# 用 $ne 而不是 $eq False，是為了存量安全：15 萬+ 既有 chunk 沒有這個欄位，
# 而 vector_store._eval_term 的 $ne 對「欄位不存在」是放行的（$eq 則會全滅）。
# 另外布林 term 會被 _split_where_pushdown 攔下來在 Python 端評估（附帶
# over-fetch），所以不會退化成 Chroma 的全表掃描，也不吃 Chroma 自己那套
# 「缺欄位不匹配」的語意。
_EXCLUDE_RED_GENERATED: dict[str, Any] = {PROVENANCE_FIELD: {"$ne": True}}


def access_where(
    caller: Agent | str | None,
    *,
    base_where: Mapping[str, Any] | None = None,
    include_generated: bool = False,
) -> dict[str, Any] | None:
    """Build the query filter. Red sees all; departments need explicit ACL.

    同時套用「排除小紅自產內容」的預設過濾（見 _EXCLUDE_RED_GENERATED）。
    這件事放在這裡而不是各呼叫端，是因為檢索有三道門——semantic_search、
    drive_search、chat_search——三道都得走這支拿 filter。之前只有
    semantic_search 自己疊了排除條件，另外兩道是敞開的；把它收斂成單一
    chokepoint，新增檢索路徑就不會又漏一道。

    include_generated=True 才會納入自產內容（例：「上週寄了哪些報表」）。
    """
    actor = _coerce_agent(caller)
    combined = base_where
    if not include_generated:
        combined = _combine_where(base_where, _EXCLUDE_RED_GENERATED)
    if actor is Agent.RED:
        return dict(combined) if combined else None
    acl_filter = {f"access_{actor.value}": {"$eq": True}}
    return _combine_where(combined, acl_filter)


def _redact_preview(value: Any, *, limit: int = 300) -> str:
    text = str(value or "")
    try:
        from agent_core.log_redact import redact_log_line
        text = redact_log_line(text)
    except Exception:
        pass
    return text.replace("\r", "\\r").replace("\n", "\\n")[:limit]


def log_rag_access_event(
    *,
    caller: Agent | str | None,
    collection: str,
    query: str,
    status: str,
    n_results: int = 0,
    hit_count: int = 0,
    where: Mapping[str, Any] | None = None,
    trace_id: str = "",
    reason: str = "",
) -> None:
    try:
        actor = _coerce_agent(caller)
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "caller": actor.value,
            "collection": str(collection or ""),
            "query_preview": _redact_preview(query),
            "status": str(status or ""),
            "n_results": int(n_results),
            "hit_count": int(hit_count),
            "where": where or {},
            "trace_id": str(trace_id or ""),
            "reason": _redact_preview(reason, limit=160),
        }
        path = rag_access_audit_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        return


def current_request_caller(default: Agent = Agent.RED) -> Agent:
    try:
        from agent_core.agents.middleware import current_agent_request
        req = current_agent_request()
    except Exception:
        req = None
    caller = getattr(req, "caller", None)
    return _coerce_agent(caller, default=default)


def current_request_trace_id() -> str:
    try:
        from agent_core.agents.middleware import current_agent_request
        req = current_agent_request()
    except Exception:
        req = None
    return str(getattr(req, "trace_id", "") or "")


def semantic_search(
    collection: str,
    query: str,
    *,
    caller: Agent | str | None = None,
    n_results: int = 5,
    base_where: Mapping[str, Any] | None = None,
    trace_id: str = "",
    include_generated: bool = False,
) -> list[dict[str, Any]]:
    """語意檢索（含 ACL）。

    include_generated=True 才會把小紅自產的內容（排程報表等，帶
    generated_by_red 標記）納入結果——預設濾掉，見 _EXCLUDE_RED_GENERATED。
    要回答「我們上週寄了哪些報表」這類問題時才打開。
    """
    q = str(query or "").strip()
    actor = _coerce_agent(caller, default=current_request_caller())
    where = access_where(
        actor, base_where=base_where, include_generated=include_generated
    )
    n = max(1, min(20, int(n_results)))
    if not q:
        log_rag_access_event(
            caller=actor,
            collection=collection,
            query=q,
            status="invalid",
            n_results=n,
            where=where,
            trace_id=trace_id or current_request_trace_id(),
            reason="empty_query",
        )
        return []

    from agent_core.ingest.vector_store import get_store

    hits = get_store(collection).query(q, n_results=n, where=where)
    log_rag_access_event(
        caller=actor,
        collection=collection,
        query=q,
        status="ok",
        n_results=n,
        hit_count=len(hits),
        where=where,
        trace_id=trace_id or current_request_trace_id(),
    )
    return hits
