"""Owner session 主控台 — 活躍對話 session 的登記本 + 遠端控制。

大王在任何一個 chat 都能綜覽「目前有哪些人正在跟小紅對話」，並對任一
session 下三種遠端指令：暫停（擋掉對方新訊息）、恢復、重置（清掉對方的
對話上下文，下一則從全新開始）。這就是「Remote Control for all sessions」
方案 2：不是放寬既有工具的頻道權限，而是新增一層 owner-only 的 session
管理面，順著現有 actor 身分系統走、不動任何安全防線。

設計要點：
  - **跨程序安全**：registry 是 `var/state/session_registry.json`，讀改寫
    一律走 `state_io.locked_json`（fcntl 排他鎖），因為 Telegram daemon、
    Web server、REPL 可能同時 touch。daemon 每收一則訊息就 touch 一次，
    owner 的控制工具在另一個程序寫旗標，兩邊靠同一個檔對得起來。
  - **owner 永不被暫停**：pause_session 拒絕暫停 owner 自己的 session；
    daemon 端 gate 也額外檢查 is_owner，雙保險避免把大王鎖在門外。
  - **暫停 gate 有牙齒**：daemon 在 tg_handle_message 早期就短路 paused
    的非 owner 訊息，根本不進 LLM（省 token、也真的擋住）。
  - **owner-only 由既有機制把關**：list_sessions 掛進
    telegram_actor_scope.OWNER_PRIVATE_READ_TOOLS（非 owner session build
    時整顆移除、唯讀不用 +確認）；pause/resume/reset 掛進
    tg_auth._SENSITIVE_TOOLS（非 owner 移除 + 大王要 +確認）。

刻意 **不用** `from __future__ import annotations`：進 tools_list 的工具若
帶字串 annotation，google-genai 帶參數派發會 TypeError（見
tests/test_tool_string_annotations.py）。這裡工具參數全用具體型別。

env 旋鈕：
  RED_SESSION_REGISTRY_FILE — 覆寫 registry 檔路徑（測試 / 特殊部署用）。
"""
import json
import os
import time
from typing import Any, Mapping, Optional

from agent_core.logging_and_paths import STATE_DIR, logger
from agent_core.state_io import locked_json

_STATUS_ACTIVE = "active"
_STATUS_PAUSED = "paused"

# 暫停理由存進 registry 給大王的主控台看，但**不會**回給被擋的員工（避免
# 洩漏內部緣由），所以長度上限寬鬆即可。
_REASON_MAX = 200

# 廣播內容長度上限（Telegram 單則 4096，留餘裕給前綴）。
_BROADCAST_MAX = 4000
# 每個 web session 最多留幾則未讀廣播（防無限成長；超過丟最舊）。
_BROADCAST_QUEUE_MAX = 20
# 送達通道用的已知通道名（target 可用來過濾）。
_CHANNELS = ("telegram", "line", "web")


def _registry_path() -> str:
    """Registry JSON 檔的絕對路徑。env 覆寫優先（測試把它指到 tmp 檔）。"""
    override = os.environ.get("RED_SESSION_REGISTRY_FILE")
    if override:
        return override
    return os.path.join(STATE_DIR, "session_registry.json")


def _now() -> float:
    """包一層方便測試 monkeypatch（也可由 caller 顯式傳 now=）。"""
    return time.time()


def _read_all() -> dict:
    """唯讀載入整份 registry（寫入是 atomic，唯讀不必上鎖）。壞檔回 {}。"""
    path = _registry_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[session_registry] %s 解析失敗（%s），當空處理", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _actor_fields(actor: Optional[Mapping[str, Any]]) -> tuple[str, str, bool]:
    """從 telegram actor 取 (可讀標籤, 部門色, 是否 owner)。"""
    if not isinstance(actor, Mapping):
        return ("未知", "", False)
    try:
        from agent_core.telegram_actor_scope import actor_label
        label = actor_label(actor)
    except Exception:
        label = str(actor.get("name") or actor.get("email") or "未知").strip() or "未知"
    color = str(actor.get("color") or "").strip()
    is_owner = str(actor.get("is_owner") or "").strip().lower() == "true"
    return (label, color, is_owner)


def _channel_of(rec: Mapping[str, Any]) -> str:
    """路由用的權威通道：以 session_id 前綴（`telegram:`/`line:`/`web:`）為準，
    退回 rec['channel']。egress（廣播）靠這個決定送哪個管道，不受 channel 欄位
    是否被寫對影響。"""
    sid = str(rec.get("session_id") or "")
    if ":" in sid:
        return sid.split(":", 1)[0]
    return str(rec.get("channel") or "")


def _resolve_in(reg: Mapping[str, Any], given: str) -> str:
    """把使用者給的 id 對到 registry 內真正的 key。

    容許只給 chat_id（省略 `telegram:` 前綴）：先試完全相同，再試補上
    telegram 前綴，都沒有就原樣回（讓上層產生「找不到」訊息）。
    """
    if given in reg:
        return given
    prefixed = f"telegram:{given}"
    if prefixed in reg:
        return prefixed
    return given


# ────────────────────────────────────────────────────────────────────
# daemon 端 API（每則 inbound 訊息會呼叫）
# ────────────────────────────────────────────────────────────────────
def touch_session(
    session_id: str,
    *,
    channel: str = "telegram",
    actor: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
) -> dict:
    """登記 / 更新一個活躍 session，回傳更新後紀錄的快照。

    每收到一則訊息呼叫一次：更新 last_seen、turn_count 與 actor 標籤，
    但**保留** status / paused_* / reset_pending（那些由 owner 控制工具管，
    touch 不得覆蓋，否則暫停會被對方下一則訊息自動解除）。
    """
    ts = _now() if now is None else now
    label, color, is_owner = _actor_fields(actor)
    with locked_json(_registry_path(), default={}) as reg:
        rec = reg.get(session_id)
        if not isinstance(rec, dict):
            rec = {
                "session_id": session_id,
                "channel": channel or "telegram",
                "first_seen": ts,
                "turn_count": 0,
                "status": _STATUS_ACTIVE,
                "paused_reason": "",
                "paused_at": 0.0,
                "reset_pending": False,
            }
            reg[session_id] = rec
        rec["channel"] = channel or rec.get("channel") or "telegram"
        rec["actor_label"] = label
        rec["color"] = color
        rec["is_owner"] = is_owner
        rec["last_seen"] = ts
        rec["turn_count"] = int(rec.get("turn_count") or 0) + 1
        snapshot = dict(rec)
    return snapshot


def get_session(session_id: str) -> Optional[dict]:
    rec = _read_all().get(session_id)
    return rec if isinstance(rec, dict) else None


def is_paused(session_id: str) -> bool:
    rec = get_session(session_id)
    return bool(rec and rec.get("status") == _STATUS_PAUSED)


def consume_reset(session_id: str) -> bool:
    """若該 session 有待處理的 reset 旗標：清掉並回 True（one-shot）；否則 False。

    daemon 在 gate 收到 reset_pending 後，會清空該 chat 的對話 session
    並呼叫此函式消費旗標，確保只重置一次。
    """
    with locked_json(_registry_path(), default={}) as reg:
        rec = reg.get(session_id)
        if isinstance(rec, dict) and rec.get("reset_pending"):
            rec["reset_pending"] = False
            return True
    return False


def active_sessions(target: str = "all") -> list:
    """回符合 target 的活躍、非 owner session 紀錄（給 broadcast 用）。

    target：'all'/'' → 全部；'telegram'/'line'/'web' → 該通道；其餘 → 當部門色過濾。
    暫停中的 session 不納入（正被刻意擋著，不該收廣播）。
    """
    tgt = str(target or "all").strip().lower()
    out = []
    for rec in _read_all().values():
        if not isinstance(rec, dict):
            continue
        if rec.get("is_owner") or rec.get("status") != _STATUS_ACTIVE:
            continue
        if tgt in ("", "all"):
            out.append(rec)
        elif tgt in _CHANNELS:
            if _channel_of(rec) == tgt:
                out.append(rec)
        elif str(rec.get("color") or "").strip().lower() == tgt:
            out.append(rec)
    return out


def queue_broadcast(session_id: str, text: str) -> bool:
    """把一則廣播排進某 session 的待讀佇列（web 專用：無伺服器推播，下次開頁顯示）。"""
    with locked_json(_registry_path(), default={}) as reg:
        rec = reg.get(session_id)
        if not isinstance(rec, dict):
            return False
        q = rec.get("pending_broadcasts")
        if not isinstance(q, list):
            q = []
        q.append(str(text))
        rec["pending_broadcasts"] = q[-_BROADCAST_QUEUE_MAX:]
        return True


def drain_broadcasts(session_id: str) -> list:
    """取出並清空某 session 的待讀廣播（one-shot；web 開頁時呼叫）。"""
    drained: list = []
    with locked_json(_registry_path(), default={}) as reg:
        rec = reg.get(session_id)
        if isinstance(rec, dict):
            q = rec.get("pending_broadcasts")
            if isinstance(q, list) and q:
                drained = [str(x) for x in q]
                rec["pending_broadcasts"] = []
    return drained


def paused_notice(reason: str = "") -> str:
    """回給「被暫停的非 owner」的訊息。刻意不透露內部暫停理由。"""
    return (
        "⏸️ 這個對話目前已由管理員暫停，暫時無法處理新的請求。\n"
        "若需要恢復，請直接聯絡大王。"
    )


# ────────────────────────────────────────────────────────────────────
# 顯示 helper
# ────────────────────────────────────────────────────────────────────
def _ago(ts: float, *, now: Optional[float] = None) -> str:
    try:
        delta = (_now() if now is None else now) - float(ts)
    except (TypeError, ValueError):
        return "?"
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分鐘前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小時前"
    return f"{int(delta // 86400)} 天前"


def _fmt_line(rec: Mapping[str, Any]) -> str:
    icon = "⏸️" if rec.get("status") == _STATUS_PAUSED else "🟢"
    owner_tag = " 👑大王" if rec.get("is_owner") else ""
    color = str(rec.get("color") or "").strip()
    color_tag = f"｜{color}" if color else ""
    label = str(rec.get("actor_label") or "未知")
    line = (
        f"{icon} `{rec.get('session_id')}` — {label}{owner_tag}{color_tag}\n"
        f"     最後活動 {_ago(rec.get('last_seen') or 0)}"
        f"｜共 {int(rec.get('turn_count') or 0)} 則"
    )
    if rec.get("status") == _STATUS_PAUSED:
        reason = str(rec.get("paused_reason") or "").strip()
        line += f"｜⏸️ 已暫停{('：' + reason) if reason else ''}"
    if rec.get("reset_pending"):
        line += "｜♻️ 待重置"
    return line


# ────────────────────────────────────────────────────────────────────
# Owner-only 工具（進 tools_list）
# ────────────────────────────────────────────────────────────────────
def list_sessions(include_owner: bool = False) -> str:
    """🖥️【Owner 主控台】列出目前所有活躍對話 session（誰正在跟小紅講話）。

    涵蓋三個前台通道，session_id 前綴標明來源：`telegram:<chat>`（大王/員工
    bot）、`line:<userId>`（LINE 員工）、`web:<email>`（網頁部門聊天員工）。
    顯示每個 session 的 id、對象、部門色、最後活動時間、累計訊息數，以及
    是否已被暫停 / 待重置。拿到 session_id 後可用 pause_session /
    resume_session / reset_session 遠端控制。

    Args:
        include_owner: 是否一併列出大王自己的 session（預設 False，只看員工/其他人）。
    """
    reg = _read_all()
    items = [r for r in reg.values() if isinstance(r, dict)]
    if not items:
        return "（目前沒有任何活躍 session 記錄。等有人跟小紅對話後就會出現。）"
    owner_items = [r for r in items if r.get("is_owner")]
    shown = items if include_owner else [r for r in items if not r.get("is_owner")]
    shown.sort(key=lambda r: float(r.get("last_seen") or 0), reverse=True)
    if not shown:
        return (
            f"（目前只有大王自己的 {len(owner_items)} 個 session，沒有其他人在對話。"
            f"想連自己一起看可用 include_owner=True。）"
        )
    lines = [f"🖥️ Owner session 主控台 — 共 {len(shown)} 個活躍 session"]
    lines.append("─" * 48)
    lines.extend(_fmt_line(r) for r in shown)
    if not include_owner and owner_items:
        lines.append("─" * 48)
        lines.append(f"（另有大王自己的 {len(owner_items)} 個 session 未列，include_owner=True 可顯示）")
    lines.append("")
    lines.append("控制：pause_session('<id>') 暫停｜resume_session('<id>') 恢復｜reset_session('<id>') 清空對話")
    lines.append("廣播：broadcast_message('訊息', target='all'|部門色|通道) 對活躍 session 群發")
    return "\n".join(lines)


def pause_session(session_id: str, reason: str = "") -> str:
    """⏸️【Owner 主控台】暫停指定 session：擋掉該對象接下來的訊息，不進 LLM。

    被暫停的對象送訊息時只會收到「已由管理員暫停」的通知，直到你
    resume_session。大王本人的 session 禁止暫停（避免把自己鎖在門外）。

    Args:
        session_id: 目標 session id（如 'telegram:12345'，可省略前綴只給 chat_id）。
        reason: 暫停理由，只記在主控台給你自己看，不會透露給被暫停的人。
    """
    given = str(session_id or "").strip()
    if not given:
        return "⚠️ 請提供 session_id（先用 list_sessions 看清單）。"
    with locked_json(_registry_path(), default={}) as reg:
        sid = _resolve_in(reg, given)
        rec = reg.get(sid)
        if not isinstance(rec, dict):
            return f"⚠️ 找不到 session「{given}」。先用 list_sessions 查看目前有哪些。"
        if rec.get("is_owner"):
            return f"🚫 「{sid}」是大王本人的 session，禁止暫停（避免把自己鎖在門外）。"
        label = str(rec.get("actor_label") or sid)
        already = rec.get("status") == _STATUS_PAUSED
        rec["status"] = _STATUS_PAUSED
        rec["paused_reason"] = str(reason or "").strip()[:_REASON_MAX]
        rec["paused_at"] = _now()
    prefix = "（原本就已暫停，更新理由）" if already else ""
    return (
        f"⏸️ {prefix}已暫停 session「{sid}」（{label}）。\n"
        f"該對象的新訊息會被擋下、不進 LLM，直到你 resume_session('{sid}')。"
    )


def resume_session(session_id: str) -> str:
    """▶️【Owner 主控台】恢復先前暫停的 session，對方即可繼續對話。

    Args:
        session_id: 目標 session id（可省略前綴只給 chat_id）。
    """
    given = str(session_id or "").strip()
    if not given:
        return "⚠️ 請提供 session_id（先用 list_sessions 看清單）。"
    with locked_json(_registry_path(), default={}) as reg:
        sid = _resolve_in(reg, given)
        rec = reg.get(sid)
        if not isinstance(rec, dict):
            return f"⚠️ 找不到 session「{given}」。先用 list_sessions 查看目前有哪些。"
        label = str(rec.get("actor_label") or sid)
        was_paused = rec.get("status") == _STATUS_PAUSED
        rec["status"] = _STATUS_ACTIVE
        rec["paused_reason"] = ""
        rec["paused_at"] = 0.0
    if not was_paused:
        return f"ℹ️ session「{sid}」（{label}）本來就不是暫停狀態，無需恢復。"
    return f"▶️ 已恢復 session「{sid}」（{label}），對方可以繼續對話了。"


def reset_session(session_id: str) -> str:
    """♻️【Owner 主控台】重置指定 session：對方下一則訊息會以全新對話開始。

    清掉該 session 累積的對話上下文（小紅會「忘記」先前這串對話），常用在
    對方把話題帶偏、或你要接管重來。不影響長期記憶 / RAG，只清這串即時對話。

    Args:
        session_id: 目標 session id（可省略前綴只給 chat_id）。
    """
    given = str(session_id or "").strip()
    if not given:
        return "⚠️ 請提供 session_id（先用 list_sessions 看清單）。"
    with locked_json(_registry_path(), default={}) as reg:
        sid = _resolve_in(reg, given)
        rec = reg.get(sid)
        if not isinstance(rec, dict):
            return f"⚠️ 找不到 session「{given}」。先用 list_sessions 查看目前有哪些。"
        label = str(rec.get("actor_label") or sid)
        rec["reset_pending"] = True
    return (
        f"♻️ 已排定重置 session「{sid}」（{label}）：\n"
        f"對方下一則訊息會以全新對話開始（先前上下文清空，長期記憶不受影響）。"
    )


def broadcast_message(text: str, target: str = "all") -> str:
    """📢【Owner 主控台】對活躍對話 session 廣播一則訊息（跨 Telegram/LINE/Web）。

    把同一則訊息發給目前正在跟小紅對話的人。Telegram / LINE 直接推播；Web 因無
    伺服器推播，改排進佇列、對方下次開部門聊天頁時顯示。已暫停的 session 不會
    收到（正被刻意擋著）；大王/boss 自己也不會收到。只發給登記在案的活躍 session，
    不能對任意 id 發送。

    Args:
        text: 廣播內容（上限 4000 字）。
        target: 'all'（預設，所有活躍非 owner session）｜部門色（如 'blue' 只發船務）
                ｜通道名（'telegram'/'line'/'web' 只發該通道）。
    """
    msg = str(text or "").strip()
    if not msg:
        return "⚠️ 廣播內容不能為空。"
    if len(msg) > _BROADCAST_MAX:
        return f"⚠️ 廣播內容過長（{len(msg)} 字，上限 {_BROADCAST_MAX}）。"
    sessions = active_sessions(target)
    if not sessions:
        return f"（沒有符合 target=「{target}」的活躍 session，未送出任何廣播。）"

    body = f"📢 管理員廣播：\n{msg}"
    tg_ok = tg_fail = line_ok = line_fail = web_queued = 0

    # 只在有對應目標時才 lazy 載入送達管道（避免無謂 secret 讀取 / 重 import）。
    tg_token, tg_send_fn, line_push_fn = "", None, None
    if any(_channel_of(s) == "telegram" for s in sessions):
        try:
            from agent_core.daemon_telegram import tg_get_token_and_chat, tg_send
            tg_token, _ = tg_get_token_and_chat()
            tg_send_fn = tg_send
        except Exception as exc:
            logger.warning("[broadcast] 取 Telegram 送達管道失敗：%s", exc)
    if any(_channel_of(s) == "line" for s in sessions):
        try:
            from agent_core.line_bot import line_push
            line_push_fn = line_push
        except Exception as exc:
            logger.warning("[broadcast] 取 LINE 送達管道失敗：%s", exc)

    for rec in sessions:
        sid = str(rec.get("session_id") or "")
        channel = _channel_of(rec)
        ident = sid.split(":", 1)[1] if ":" in sid else ""
        if channel == "telegram":
            ok = False
            if tg_send_fn and tg_token and ident:
                try:
                    ok = bool(tg_send_fn(tg_token, ident, body))
                except Exception as exc:
                    logger.warning("[broadcast] Telegram %s 送達失敗：%s", sid, exc)
            tg_ok, tg_fail = (tg_ok + 1, tg_fail) if ok else (tg_ok, tg_fail + 1)
        elif channel == "line":
            ok = False
            if line_push_fn and ident:
                try:
                    ok, _ = line_push_fn(ident, body)
                except Exception as exc:
                    logger.warning("[broadcast] LINE %s 送達失敗：%s", sid, exc)
            line_ok, line_fail = (line_ok + 1, line_fail) if ok else (line_ok, line_fail + 1)
        elif channel == "web":
            if queue_broadcast(sid, msg):
                web_queued += 1

    preview = msg[:30] + ("…" if len(msg) > 30 else "")
    out = [f"📢 已廣播「{preview}」（target={target}，共 {len(sessions)} 個 session）："]
    out.append(f"   Telegram 送達 {tg_ok}" + (f"、失敗 {tg_fail}" if tg_fail else ""))
    out.append(f"   LINE 送達 {line_ok}" + (f"、失敗 {line_fail}" if line_fail else ""))
    out.append(f"   Web 佇列 {web_queued}（對方下次開部門聊天頁時顯示）")
    return "\n".join(out)
