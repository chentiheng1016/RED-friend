"""Run history / audit trail：每次 skill 執行都留證據。

專業 RPA 的門檻之一就是「跑過的事有 audit trail」。這個模組提供：
  - @audited 裝飾器：把任何函式包起來，每次呼叫自動寫一筆 JSON record
  - 可選前後 screenshot（mss 擷圖，不會被 hotword mic 占用）
  - list_runs / show_run / rerun 工具讓小紅（或大王）事後查

儲存結構：
  runs/
    index.jsonl                     # append-only，每行一筆簡表
    runs/{id}.json                  # 完整紀錄
    runs/{id}_before.png            # 可選 screenshot
    runs/{id}_after.png

index.jsonl 欄位（for list_runs 快速掃）：
  {id, tool, started_at, ended_at, status, elapsed_sec, short_result}

runs/{id}.json 多了：args, kwargs, full result, traceback, screenshot paths.

安全：
  - kwargs 裡 key 含 password / token / api_key / secret 的值會被 redact
  - result 截斷到 4000 字
  - traceback 只在失敗時存

⚠️ @audited 故意不是 function decorator auto-applied —— 要手動掛在想審計的 tool 上。
（全部包會讓高頻小工具（open_url 之類）洗爆 runs/。）
"""
from __future__ import annotations

import functools
import json
import os
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agent_core.logging_and_paths import RUNS_DIR, logger

RUNS_INDEX = os.path.join(RUNS_DIR, "index.jsonl")
# 健檢 Low：cap index.jsonl，避免只增不減 + 監控每 tick readlines 全掃。便宜 size 閘、
# 超過才 trim 成最後 N 行（housekeeping 的 mtime 修剪刻意跳過此檔）。
_MAX_INDEX_LINES = 5000
_MAX_INDEX_BYTES = 2_000_000
SCREENSHOTS_DIR = os.path.join(RUNS_DIR, "screenshots")

# 保留幾天的 run 記錄（超過自動刪，避免無限長大）
DEFAULT_RETENTION_DAYS = 90
_PG_RUN_HISTORY_WARNING_UNTIL = 0.0

# kwargs / result 中含這些 token 的 key 會被 redact。
# 用「token list + word-boundary 比對」而不是純 substring，避免把
# `primary_key` / `keyword` / `monkey` 這類無辜 key 當祕密誤殺。
# 比對在 _is_secret 做：先 lowercase + 切 `[_\-\s]` 再對 token list 取交集。
_SECRET_KEY_TOKENS = frozenset({
    # 密碼系列
    "password", "passwd", "pwd", "passphrase",
    # Token / API key
    "token", "apikey",  # api_key / api-key 切完都會變這個
    "secret", "credential", "credentials",
    "auth", "authorization", "bearer",
    # OAuth
    "oauth", "client",  # 配合上下文，client_secret / client_id 都會帶 secret/id token
    # 簽章 / 鑰
    "key", "keys", "privatekey",  # private_key 切後變 privatekey
    "cookie", "session",
    # 雲服務常見
    "credentials", "serviceaccount",
})


def _ensure_dirs():
    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _warn_pg_run_history_fallback(exc: Exception) -> None:
    global _PG_RUN_HISTORY_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_RUN_HISTORY_WARNING_UNTIL:
        return
    _PG_RUN_HISTORY_WARNING_UNTIL = now + 30
    logger.warning("Postgres run_history failed; falling back to files: %s", exc)


def _pg_run_store():
    try:
        from agent_core import operational_run_history as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - audit must stay best-effort
        _warn_pg_run_history_fallback(exc)
    return None


def _new_run_id(tool_name: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    safe_tool = "".join(c if c.isalnum() or c == "_" else "_" for c in tool_name)[:40]
    return f"{ts}_{safe_tool}_{suffix}"


_SECRET_SPLIT_RE = None


def _is_secret(key: str) -> bool:
    r"""Word-boundary 比對：把 key 在 `_-.\s` 切片，看片段是否在 token list。

    這比純 substring 精準：
      "api_key"       → ["api", "key"]       → "key" 命中 → secret
      "primary_key"   → ["primary", "key"]   → "key" 命中 → secret（合理：DB primary key 也常是敏感）
      "keyword"       → ["keyword"]          → 都不命中 → 不 redact ✅
      "monkey"        → ["monkey"]           → 都不命中 → 不 redact ✅
      "client_secret" → ["client", "secret"] → "secret" 命中 → secret
      "Authorization" → ["authorization"]    → "authorization" → 命中（在 token list）
    """
    global _SECRET_SPLIT_RE
    if _SECRET_SPLIT_RE is None:
        import re as _re
        _SECRET_SPLIT_RE = _re.compile(r"[_\-\.\s]+")
    if not key:
        return False
    parts = _SECRET_SPLIT_RE.split(str(key).lower())
    return any(p in _SECRET_KEY_TOKENS for p in parts if p)


def _redact(obj: Any, depth: int = 0) -> Any:
    """遞迴 redact dict 中的敏感 value。只處理 dict/list/tuple；其他原樣。"""
    if depth > 5:
        return "(太深，跳過)"
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if _is_secret(k) else _redact(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(x, depth + 1) for x in obj]
    return obj


def _safe_stringify(obj: Any, limit: int = 4000) -> str:
    """讓任何物件都能存檔。對 JSON-serializable 的原樣，其他 stringify。"""
    try:
        return json.dumps(obj, ensure_ascii=False)[:limit]
    except Exception:
        s = str(obj)
        return s[:limit] + (f"...(truncated, 原長 {len(s)})" if len(s) > limit else "")


def _coerce_text_filter(value: Any, *, keys: tuple[str, ...] = ()) -> str:
    """Best-effort coercion for LLM-supplied filter args.

    Tool callers occasionally pass a list/dict for a scalar argument. Returning
    an empty filter is more useful than raising and making diagnostics fail.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return _coerce_text_filter(value.get(key))
        return ""
    if isinstance(value, (list, tuple, set)):
        for item in value:
            s = _coerce_text_filter(item)
            if s:
                return s
        return ""
    return str(value).strip()


def _coerce_positive_int(
    value: Any,
    default: int,
    *,
    min_value: int = 1,
    max_value: int = 1000,
    keys: tuple[str, ...] = (),
) -> int:
    """Coerce a tool-call numeric arg into a bounded positive int."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return _coerce_positive_int(
                    value.get(key),
                    default,
                    min_value=min_value,
                    max_value=max_value,
                )
        return default
    if isinstance(value, (list, tuple, set)):
        for item in value:
            n = _coerce_positive_int(
                item,
                default,
                min_value=min_value,
                max_value=max_value,
            )
            if n != default:
                return n
        return default
    try:
        n = int(float(str(value).strip()))
    except Exception:
        return default
    return max(min_value, min(max_value, n))


def _capture_screen(run_id: str, label: str) -> str | None:
    """擷圖回傳檔案路徑；失敗回 None（不 crash）。"""
    try:
        import mss
        _ensure_dirs()
        path = os.path.join(SCREENSHOTS_DIR, f"{run_id}_{label}.png")
        with mss.mss() as sct:
            # 取第一個 monitor（主螢幕）
            monitors = sct.monitors
            if len(monitors) < 2:
                return None
            img = sct.grab(monitors[1])
            mss.tools.to_png(img.rgb, img.size, output=path)
        return path
    except Exception as e:
        logger.debug("run_history 擷圖失敗：%s", e)
        return None


def _attach_structured_fields(record: dict, result) -> None:
    """偵測 result 是 ToolResult / 既有 string，把 ok / error_code 寫進 record。

    ToolResult instance：直接用其 attribute（最權威）
    既有 string：用 classify_string_result heuristic 猜
    """
    try:
        from agent_core.tool_result import (
            ToolResult, classify_string_result, ErrorCode,
        )
    except Exception:
        return
    # 1) ToolResult 直接讀
    if isinstance(result, ToolResult):
        record["ok"] = result.ok
        if not result.ok:
            record["status"] = "error"
            record["error_code"] = result.error_code
            record["recoverable"] = result.recoverable
            if result.suggested_fix:
                record["suggested_fix"] = result.suggested_fix
        # success 額外欄位（給 dashboard 用）
        if result.warnings:
            record["warnings"] = result.warnings
        if result.cost:
            record["cost_meta"] = result.cost
        if result.artifacts:
            record["artifacts"] = result.artifacts
        return
    # 2) plain string — heuristic
    if isinstance(result, str):
        ok, error_code = classify_string_result(result)
        record["ok"] = ok
        if not ok:
            record["status"] = "error"
            record["error_code"] = error_code
            record["recoverable"] = ErrorCode.is_recoverable(error_code)
        return
    # 3) 其他型別（dict / None / number）— 視為 success
    record["ok"] = True


def _write_record(record: dict):
    """把完整 record 寫成 {id}.json，並 append 一行到 index.jsonl。"""
    store = _pg_run_store()
    if store is not None:
        try:
            store.write_run_record(record)
            return
        except Exception as e:  # noqa: BLE001 - fall back to local audit files
            _warn_pg_run_history_fallback(e)

    _ensure_dirs()
    path = os.path.join(RUNS_DIR, f"{record['id']}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.warning("run_history 寫 %s 失敗：%s", path, e)
        return
    # append 簡表到 index（fast listing 用）
    idx = {
        "id": record["id"],
        "tool": record["tool"],
        "started_at": record["started_at"],
        "ended_at": record.get("ended_at", ""),
        "status": record["status"],
        "elapsed_sec": record.get("elapsed_sec", 0),
        "short_result": (record.get("result") or "")[:200],
    }
    # 新欄位（不打破舊 reader — 沒這欄會 missing）
    if "ok" in record:
        idx["ok"] = record["ok"]
    if "error_code" in record:
        idx["error_code"] = record["error_code"]
    if "recoverable" in record:
        idx["recoverable"] = record["recoverable"]
    try:
        # append + 超額修剪一律在 sibling .lock 的 fcntl 排他鎖下做（比照
        # state_io.locked_json 模式）：修剪的「readlines → 整檔重寫」若與
        # 另一行程的 append 交錯，重寫會把對方剛 append 的行吃掉。
        import fcntl
        lock_fd = open(RUNS_INDEX + ".lock", "w")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            with open(RUNS_INDEX, "a", encoding="utf-8") as f:
                f.write(json.dumps(idx, ensure_ascii=False) + "\n")
            if os.path.getsize(RUNS_INDEX) > _MAX_INDEX_BYTES:
                with open(RUNS_INDEX, encoding="utf-8") as f:
                    _lines = f.readlines()
                with open(RUNS_INDEX, "w", encoding="utf-8") as f:
                    f.writelines(_lines[-_MAX_INDEX_LINES:])
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fd.close()
    except Exception as e:
        logger.warning("run_history 寫 index 失敗：%s", e)


# ────────────────────────────────────────────────────────────────────
# @audited 裝飾器
def _redact_secrets(text: str) -> str:
    """Value-level secret scrub for anything written to runs/*.json /
    index.jsonl (both LLM-readable via show_run/find_past_actions). Lazy
    import keeps run_history import-light.

    Why it matters for args specifically: positional args were previously
    stored raw — a secret passed positionally (e.g. run_shell("mysql -p
    HUNTER2"), set_secret("gemini-api-key", "AIza…")) landed verbatim, since
    the key-based _redact only inspects dict keys.
    """
    try:
        from agent_core.log_redact import has_secret, redact_log_line
        # Fast-path: this runs 3x per audited call (args/kwargs/result) and
        # result strings can be large (full email bodies, file dumps). has_secret
        # is a search-only scan that short-circuits on first match and builds no
        # output; only pay the full ~35-pattern sub() pass when there's actually
        # something to redact. has_secret and redact_log_line share _PATTERNS,
        # so "no secret" ⟺ redact would be a no-op — output is identical.
        return redact_log_line(text) if has_secret(text) else text
    except Exception:
        return text


# ────────────────────────────────────────────────────────────────────
def audited(capture_screen: bool = False, redact_args: bool = True):
    """把函式包進 run_history。每次呼叫寫一筆紀錄。

    Args:
        capture_screen: True 時前後各擷一張螢幕截圖。預設 False（多數 tool 不需要）。
        redact_args: True（預設）會 redact kwargs 中含 password/token/secret 等字的值。
    """
    def deco(fn: Callable):
        import time

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            run_id = _new_run_id(fn.__name__)

            record = {
                "id": run_id,
                "tool": fn.__name__,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "args": _redact_secrets(_safe_stringify(args)),
                "kwargs": _redact_secrets(_safe_stringify(_redact(kwargs) if redact_args else kwargs)),
                "status": "running",
            }
            if capture_screen:
                path = _capture_screen(run_id, "before")
                if path:
                    record["screenshot_before"] = path

            t0 = time.time()
            try:
                result = fn(*args, **kwargs)
                record["status"] = "success"
                # X3 補丁（review round 5）：result 字串可能含 inline secret /
                # API key / 信用卡（tool 回傳值來自任意第三方資料源）。寫檔前
                # 過 log_redact，避免 runs/*.json + index.jsonl 變新洩密管道。
                record["result"] = _redact_secrets(_safe_stringify(result))
                # 結構化分類：偵測 ToolResult instance 或 retroactively 從字串猜
                _attach_structured_fields(record, result)
                return result
            except Exception as e:
                record["status"] = "error"
                # X3: error / traceback 也過 redact — traceback 可能含 secret
                # 變數值或 inline string，path 也可能含 sensitive dirname。
                err_text = f"{type(e).__name__}: {e}"
                tb_text = traceback.format_exc()[-2000:]
                try:
                    from agent_core.log_redact import redact_log_line as _log_redact
                    err_text = _log_redact(err_text)
                    tb_text = _log_redact(tb_text)
                except Exception:
                    pass
                record["error"] = err_text
                record["traceback"] = tb_text
                # 例外也分類到 error_code
                try:
                    from agent_core.tool_result import classify_exception
                    record["error_code"] = classify_exception(e)
                    record["recoverable"] = (
                        record["error_code"]
                        in {"rate_limited", "timeout", "network",
                            "locked_by_mutex", "retry_later", "budget_exhausted"}
                    )
                except Exception:
                    pass
                raise
            finally:
                record["ended_at"] = datetime.now().isoformat(timespec="seconds")
                record["elapsed_sec"] = round(time.time() - t0, 3)
                if capture_screen:
                    path = _capture_screen(run_id, "after")
                    if path:
                        record["screenshot_after"] = path
                _write_record(record)

        wrapper._audited = True  # 方便 introspect
        # 保留原 fn 上的其他 marker（skill / mcp / background_safe）
        for attr in ("_is_skill", "_is_mcp_tool", "_mcp_server",
                     "_mcp_tool", "background_safe"):
            if hasattr(fn, attr):
                setattr(wrapper, attr, getattr(fn, attr))
        return wrapper
    return deco


# ────────────────────────────────────────────────────────────────────
# 查詢 / 管理工具（暴露給小紅用）
# ────────────────────────────────────────────────────────────────────
def list_runs(tool_name: str = "", status: str = "",
              since_hours: int = 24, limit: int = 20) -> str:
    """列最近的 run history。

    Args:
        tool_name: 可選，只看某個 tool 的 run；空字串 = 全部。
        status: 可選 "success" / "error" / "running"；空字串 = 全部。
        since_hours: 看最近幾小時內的；預設 24。
        limit: 最多回傳幾筆（最新優先）；預設 20。

    Returns:
        表格式的 run 清單。
    """
    tool_name = _coerce_text_filter(tool_name, keys=("tool_name", "tool", "name"))
    status = _coerce_text_filter(status, keys=("status",))
    since_hours = _coerce_positive_int(
        since_hours, 24, min_value=1, max_value=24 * 365,
        keys=("since_hours", "hours"),
    )
    limit = _coerce_positive_int(
        limit, 20, min_value=1, max_value=200,
        keys=("limit", "n", "count"),
    )

    store = _pg_run_store()
    if store is not None:
        try:
            entries = store.list_entries(
                tool_name=tool_name,
                status=status,
                since_hours=since_hours,
                limit=limit,
            )
            if not entries:
                return f"🔍 最近 {since_hours} 小時沒有符合條件的 run"
            return _format_run_list(entries)
        except Exception as e:  # noqa: BLE001 - fall back to local audit files
            _warn_pg_run_history_fallback(e)

    if not os.path.isfile(RUNS_INDEX):
        return "（還沒有任何 run history）"

    cutoff = datetime.now() - timedelta(hours=since_hours)
    entries = []
    try:
        with open(RUNS_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if tool_name and e.get("tool") != tool_name:
                    continue
                if status and e.get("status") != status:
                    continue
                try:
                    t = datetime.fromisoformat(e.get("started_at", ""))
                    if t < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
                entries.append(e)
    except Exception as e:
        return f"❌ 讀 index 失敗: {e}"

    if not entries:
        return f"🔍 最近 {since_hours} 小時沒有符合條件的 run"

    # 最新在前
    entries = entries[-limit:][::-1]

    return _format_run_list(entries)


def _format_run_list(entries: list[dict[str, Any]]) -> str:
    lines = [f"📋 最近 {len(entries)} 筆 run（最新在前）"]
    lines.append("-" * 60)
    for e in entries:
        status_icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(e["status"], "?")
        lines.append(f"{status_icon} {e['id']}")
        lines.append(f"   tool: {e['tool']}  |  {e['started_at']}  |  {e['elapsed_sec']}s")
        if e.get("short_result"):
            snippet = e["short_result"][:120].replace("\n", " ")
            lines.append(f"   → {snippet}")
    return "\n".join(lines)


def find_past_actions(query: str, days: int = 7,
                       limit: int = 20) -> str:
    """搜尋過去 N 天的 audit log 內容找符合 query 的 action。

    跟 list_runs 不同：list_runs 只能 filter tool 名稱 / status / 時間，
    這個會掃 args / kwargs / result snippet 的文字內容找 substring 命中。
    用來回答「我上週對客戶 A 做了什麼」「上次跑 batch_extract 結果如何」。

    每筆 sensitive tool 執行都會自動進 audit log（tg_auth.wrap_sensitive_tool
    對 CONFIRM/DANGEROUS 自動掛 @audited），所以這 tool 等於是 LLM 看
    自己過去動作的視窗。

    Args:
        query: 搜尋字串（會在 tool 名 / args / kwargs / short_result
               裡找 case-insensitive substring 命中）。
        days:  最近 N 天，預設 7。
        limit: 最多回傳幾筆，預設 20（最新在前）。

    Returns:
        匹配 action 的清單，含 tool 名 / 時間 / 結果摘要 / run_id（可
        進一步 show_run）。
    """
    query = _coerce_text_filter(query, keys=("query", "q", "text"))
    days = _coerce_positive_int(days, 7, min_value=1, max_value=365,
                                keys=("days", "since_days"))
    limit = _coerce_positive_int(limit, 20, min_value=1, max_value=200,
                                 keys=("limit", "n", "count"))

    if not query or not query.strip():
        return "❌ query 不能是空字串（要找什麼？）"

    store = _pg_run_store()
    if store is not None:
        try:
            matches = store.find_entries(query=query.strip(), days=days, limit=limit)
            if not matches:
                return f"🔍 過去 {days} 天 audit log 沒找到含「{query}」的 action"
            return _format_past_actions(query, days, matches)
        except Exception as e:  # noqa: BLE001 - fall back to local audit files
            _warn_pg_run_history_fallback(e)

    if not os.path.isfile(RUNS_INDEX):
        return "（還沒有 audit log — 過去沒做過任何 sensitive action）"
    q_lower = query.strip().lower()
    cutoff = datetime.now() - timedelta(days=max(1, days))
    matches = []
    try:
        with open(RUNS_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (ValueError, TypeError):
                    continue
                # Time filter
                try:
                    t = datetime.fromisoformat(e.get("started_at", ""))
                    if t < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue
                # Content match — 掃 tool / args / kwargs / short_result
                haystack = " ".join([
                    str(e.get("tool", "")),
                    str(e.get("args", "")),
                    str(e.get("kwargs", "")),
                    str(e.get("short_result", "")),
                ]).lower()
                if q_lower in haystack:
                    matches.append(e)
    except Exception as exc:
        return f"❌ 讀 audit log 失敗：{exc}"

    if not matches:
        return f"🔍 過去 {days} 天 audit log 沒找到含「{query}」的 action"

    # 最新在前
    matches = matches[-limit:][::-1]
    return _format_past_actions(query, days, matches)


def _format_past_actions(query: str, days: int, matches: list[dict[str, Any]]) -> str:
    lines = [f"📚 過去 {days} 天 {len(matches)} 筆 action 含「{query}」（最新在前）",
             "-" * 60]
    for e in matches:
        status_icon = {"success": "✅", "error": "❌", "running": "⏳"}.get(
            e.get("status"), "?")
        lines.append(f"{status_icon} {e.get('id', '')}")
        lines.append(f"   {e.get('tool', '')}  |  {e.get('started_at', '')}")
        sr = (e.get("short_result") or "")[:120].replace("\n", " ")
        if sr:
            lines.append(f"   → {sr}")
        kw = (str(e.get("kwargs", "")) or "")[:100].replace("\n", " ")
        if kw and kw not in ("{}", "None"):
            lines.append(f"   kwargs: {kw}")
    lines.append("")
    lines.append("💡 用 show_run('<id>') 看單筆完整內容")
    return "\n".join(lines)


find_past_actions.background_safe = True


def show_run(run_id: str) -> str:
    """顯示某個 run 的完整詳情（input, output, error, screenshot 路徑）。

    Args:
        run_id: list_runs 給的 id，例如 "20260423_210000_excel_pivot_abc123"。
    Returns:
        格式化的詳細紀錄。
    """
    store = _pg_run_store()
    if store is not None:
        try:
            r = store.read_run(run_id)
            if r:
                return _format_run_detail(r)
        except Exception as e:  # noqa: BLE001 - fall back to local audit files
            _warn_pg_run_history_fallback(e)

    path = os.path.join(RUNS_DIR, f"{run_id}.json")
    if not os.path.isfile(path):
        return f"❌ 找不到 run: {run_id}（檔案 {path} 不存在）"

    try:
        with open(path, "r", encoding="utf-8") as f:
            r = json.load(f)
    except Exception as e:
        return f"❌ 讀 run 失敗: {e}"

    return _format_run_detail(r)


def _format_run_detail(r: dict[str, Any]) -> str:
    lines = [
        f"📋 Run {r['id']}",
        "=" * 60,
        f"tool:        {r['tool']}",
        f"status:      {r['status']}",
        f"started:     {r['started_at']}",
        f"ended:       {r.get('ended_at', '?')}",
        f"elapsed:     {r.get('elapsed_sec', '?')} s",
        "",
        f"args:        {r.get('args', '')}",
        f"kwargs:      {r.get('kwargs', '')}",
    ]
    if r.get("screenshot_before"):
        lines.append(f"screenshot_before: {r['screenshot_before']}")
    if r.get("screenshot_after"):
        lines.append(f"screenshot_after:  {r['screenshot_after']}")
    lines.append("")
    if r["status"] == "success":
        res = r.get("result", "")
        lines.append("result:")
        lines.append(res if len(res) < 2000 else res[:2000] + "\n...(truncated)")
    elif r["status"] == "error":
        lines.append(f"error: {r.get('error', '')}")
        if r.get("traceback"):
            lines.append("")
            lines.append("traceback:")
            lines.append(r["traceback"])
    return "\n".join(lines)


def run_history_stats() -> str:
    """統計目前的 run history：總筆數、成功率、最常用 tool、最常錯的 tool。"""
    store = _pg_run_store()
    if store is not None:
        try:
            return _format_run_stats(store.stats())
        except Exception as e:  # noqa: BLE001 - fall back to local audit files
            _warn_pg_run_history_fallback(e)

    if not os.path.isfile(RUNS_INDEX):
        return "（還沒有任何 run history）"

    from collections import Counter
    total = 0
    ok = 0
    err = 0
    tool_counts = Counter()
    error_tools = Counter()
    with open(RUNS_INDEX, "r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except (ValueError, TypeError):
                continue
            total += 1
            tool_counts[e["tool"]] += 1
            if e.get("status") == "success":
                ok += 1
            elif e.get("status") == "error":
                err += 1
                error_tools[e["tool"]] += 1

    return _format_run_stats({
        "total": total,
        "success": ok,
        "error": err,
        "tool_counts": list(tool_counts.most_common(10)),
        "error_tools": list(error_tools.most_common(5)),
    })


def _format_run_stats(stats: dict[str, Any]) -> str:
    total = int(stats.get("total") or 0)
    ok = int(stats.get("success") or 0)
    err = int(stats.get("error") or 0)
    pct_ok = (ok * 100 / total) if total else 0
    lines = [
        "📊 Run History 統計",
        f"總 run 數: {total}",
        f"成功: {ok}（{pct_ok:.1f}%）",
        f"失敗: {err}",
        "",
    ]
    tool_counts = list(stats.get("tool_counts") or [])
    error_tools = list(stats.get("error_tools") or [])
    if tool_counts:
        lines.append("🔝 最常用 tool：")
        for tool, n in tool_counts[:10]:
            lines.append(f"  {n:3d}x  {tool}")
    if error_tools:
        lines.append("")
        lines.append("💥 最常失敗的 tool：")
        for tool, n in error_tools[:5]:
            lines.append(f"  {n:3d}x  {tool}")
    return "\n".join(lines)


def prune_old_runs(days: int = DEFAULT_RETENTION_DAYS) -> str:
    """刪除超過 N 天的 run 記錄（含 json、screenshot）。

    Args:
        days: 保留最近幾天；預設 90 天。

    Returns:
        刪除統計訊息。
    """
    store = _pg_run_store()
    if store is not None:
        try:
            deleted = store.prune_old_runs(days)
            return (
                f"🧹 清理完成：刪除 {deleted} 筆 Postgres run history"
                f"（保留最近 {days} 天）。"
            )
        except Exception as e:  # noqa: BLE001 - fall back to local audit files
            _warn_pg_run_history_fallback(e)

    if not os.path.isdir(RUNS_DIR):
        return "（runs 目錄不存在）"
    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0
    kept = 0
    for fn in os.listdir(RUNS_DIR):
        path = os.path.join(RUNS_DIR, fn)
        if not os.path.isfile(path) or fn == "index.jsonl":
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        if mtime < cutoff:
            try:
                os.remove(path)
                deleted += 1
            except Exception:
                pass
        else:
            kept += 1
    # screenshot dir 同樣清
    if os.path.isdir(SCREENSHOTS_DIR):
        for fn in os.listdir(SCREENSHOTS_DIR):
            path = os.path.join(SCREENSHOTS_DIR, fn)
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if mtime < cutoff:
                try:
                    os.remove(path)
                    deleted += 1
                except Exception:
                    pass
    return f"🧹 清理完成：刪除 {deleted} 個過期檔（保留最近 {days} 天），目前留 {kept} 個 run。"
