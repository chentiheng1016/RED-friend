"""Telegram auth hardening (V4).

問題：
  daemon_telegram.py 只認 chat_id 一個因子。SIM swap、Telegram 帳號被盜、
  手機被借走幾分鐘 → 攻擊者拿到對話權限後就能叫小紅做 264 個 tool 的任何
  一個，包括：
    - run_shell（任意指令）
    - send_gmail（用大王身分寄信給供應商 / 客戶 / 銀行）
    - manage_files delete（刪本機檔）
    - click_screen / type_text（操控 Mac 螢幕）
    - generate_image（燒錢）

修補：對「敏感 tool」加 session-scope 確認門。
  - 大王要在 90 秒內發過確認訊息（含 "+確認" / "/confirm" / "確認執行"）
    sensitive tool 才會真執行。
  - 沒確認 → tool 直接 return「需要確認」訊息，**不會**呼叫真函式。
  - Read-only tool（recall / fetch / list / get）不受影響，問問題日常順暢。

設計權衡：
  - 為什麼是 90s 不是更短？大王跟小紅連續對話常見 30-60s 一回，太短會
    每三句要打一次「+確認」很煩。90s 夠用一次確認跑 1-2 個 sensitive op。
  - 為什麼是 token 不是 6 位數 OTP？大王不想為每個動作收一封 SMS。
    "+確認" token 對 SIM-swap / 帳號盜用攻擊者有效（他不知要打這個），
    對 shoulder-surfing 短期借手機攻擊也有效（他看不到歷史訊息）。
  - 這是 defense-in-depth，不是 silver bullet — 真正高風險動作還是要靠 dry-run
    + audit trail + 大王自己警覺。

未來可加（沒做）：
  - HMAC 簽章每則訊息（要大王 mac 跟 phone 共享 secret，過度工程）
  - 額外綁時段（深夜訊息一律拒絕）
  - 異常偵測（短時間連續高風險 op）
"""
from __future__ import annotations

import functools
import os
import re
import threading
import time
from typing import Callable

from agent_core.state_io import locked_json


# 90 秒內的確認算數。再短大王嫌煩，再長安全度降。
_CONFIRM_WINDOW_SEC = 90
# M5 補丁：每次確認只能用一次 sensitive op（one-shot）。後續 op 要再確認。
# 這擋掉「大王確認後 LLM 串連多個 sensitive op」的情境（review M5）。
_ONE_SHOT = True
# Rate-limit（C6 — 即使 one-shot，若帳號被劫持也可能有人打字飛快發 N 個 +確認）
# sliding window：5 分鐘內最多 N 次確認，超過鎖定 10 分鐘。
_RATE_WINDOW_SEC = 300
_RATE_MAX_CONFIRMS = 5
_RATE_LOCKOUT_SEC = 600


# 大王打哪些字會被當「我授權接下來的 sensitive 動作」？要用各種大王打字習慣涵蓋。
# 用 regex 做 word-boundary，避免 "+確認交期" 也誤判（不過這樣其實 OK，意圖
# 也是確認，所以放過也合理）。
_CONFIRM_TOKENS = re.compile(
    r"(?:\+確認|/confirm|確認執行|執行確認|yes\s*執行|go\s*ahead|確定執行|執行吧)",
    re.IGNORECASE,
)

# DANGEROUS-tier 二次確認 — 一般 +確認 不夠（攻擊者可能騙大王連續確認多次）。
# DANGEROUS（刪除 / 批次改 / code-exec）要大王再多打一個明顯不同的 token。
# 這擋掉「攻擊者把 prompt-injection 包成『請大王確認』騙得逞」的情境 — 因為
# 大王看到 +雙確認 / EXEC 這種非日常字眼 才會警覺「不對，我剛才不是只是要寄信？」。
_DANGEROUS_CONFIRM_TOKENS = re.compile(
    r"(?:\+雙確認|\+\+確認|/exec|EXEC\b|危險執行|確認危險動作)",
    re.IGNORECASE,
)

# 短碼 `c` / `cc` aliases — Codex P1: 必須是「整則訊息只有 c / cc」才算。
# 之前用 \bc\b 太鬆，"write a C++ script" / "use column C" / "option C"
# 等等內容裡的單字 c 都會誤觸 → 任意 sensitive tool 在使用者沒授權意圖的
# 情況下被 grant。
#
# 為什麼用 fullmatch 而不是 text.strip().lower() == "c":
#   實際打字常帶結尾標點（c. / c! / c?）跟前後空白／換行；不接受這些
#   就違反 Telegram 直覺輸入。fullmatch + 容許 0..N 個尾標點是中庸方案。
_C_ALIAS_RE = re.compile(r"\s*c\s*[.!?。！？]?\s*", re.IGNORECASE)
_CC_ALIAS_RE = re.compile(r"\s*cc\s*[.!?。！？]?\s*", re.IGNORECASE)
# Combined：一條訊息同時授權 +確認 + +雙確認，用「c cc」/「cc c」（空白分隔）。
_COMBINED_ALIAS_RE = re.compile(
    r"\s*(?:c\s+cc|cc\s+c)\s*[.!?。！？]?\s*",
    re.IGNORECASE,
)


def _is_c_alias(text: str) -> bool:
    """True iff message is JUST `c` (whole message, optional space + trailing
    punctuation). Whole-message anchoring stops "use column C" / "C++ script"
    from accidentally granting confirmation."""
    if not text:
        return False
    return bool(_C_ALIAS_RE.fullmatch(text) or _COMBINED_ALIAS_RE.fullmatch(text))


def _is_cc_alias(text: str, chat_id: str = "") -> bool:
    """True iff message grants DANGEROUS confirm via the `cc` short-code.

    Codex P2: bare `cc` alone (without a regular `c` already in window)
    must NOT pre-arm the DANGEROUS half of the gate. Otherwise:

      Turn 1: user types `cc` for any benign reason / under prompt-injection
              → DANGEROUS timestamp armed
      Turn 2: user types `c` to authorise some ordinary CONFIRM action
              → regular timestamp armed
      → both timestamps now valid simultaneously
      → ANY DANGEROUS tool the LLM picks in turn 2 runs without the
        user ever seeing the DANGEROUS prompt.

    The intent of `+雙確認` was specifically the cognitive friction —
    user has to type a non-daily token AFTER seeing the prompt and pause
    "wait, why am I typing this?". A bare `cc` that works without that
    prompt would erase the friction.

    Three cases now:
      • `c cc` / `cc c` combined        → grant unconditionally (the same
                                          message arms both, which means
                                          the user is explicitly opting in)
      • bare `cc` AFTER regular `c`     → grant (natural flow: c then cc)
      • bare `cc` with no prior `c`     → REJECT (Codex case)

    `check_confirmed` is referenced at call time (defined later in this
    module). Lazy resolution avoids the forward-decl ordering issue.
    """
    if not text:
        return False
    if _COMBINED_ALIAS_RE.fullmatch(text):
        return True
    if not _CC_ALIAS_RE.fullmatch(text):
        return False
    if not chat_id:
        return False
    confirmed, _elapsed = check_confirmed(chat_id)
    return confirmed
# DANGEROUS 確認的有效期 — 比一般 +確認 短（30s vs 90s）。LLM 串連攻擊更難
# 在 30s 內讓大王連打兩個不同的確認。
_DANGEROUS_WINDOW_SEC = 30


# 哪些 tool 算 sensitive（需要確認）。盡量列全。
# 原則：副作用會「飛出去」（寄信、寫外部檔、操控 UI、刪資料、燒錢） → sensitive
#       唯讀/查詢（recall / fetch / list / count / search） → 不需確認
#
# C2 補丁（adversarial review 找到 30+ tool 漏列）：
#   原本只列 17 個。新版加上 vault / scheduled task / ERP / dry-run toggle /
#   browser / 各種 forget / 樣品追蹤 / QC master / 系統音量 等。重點：
#   enable_dry_run_mode / disable_dry_run_mode 沒列等於攻擊者拿到帳號可以
#   先把 dry-run 關掉再叫敏感動作。
_SENSITIVE_TOOLS = frozenset({
    # 對外通訊（不可逆）
    "send_gmail", "reply_gmail", "telegram_push",
    "download_gmail_attachment",  # 拉檔案到 Mac 本機
    # Calendar / Drive 寫入
    "create_calendar_event", "delete_calendar_event", "update_calendar_event",
    "respond_to_event",
    "upload_to_drive",
    "create_event", "delete_event", "update_event",  # MCP variants
    "create_draft", "create_label",                  # MCP gmail
    # Shell / Python / 任意 code-exec
    "run_shell", "run_python_code",
    # 桌面操控（macOS Accessibility）
    "click_screen", "type_text", "press_keys",
    "open_application", "close_application",
    "ax_click", "ax_type_in",
    "set_system_volume", "control_mac_system",
    "read_mac_clipboard",  # 剪貼簿常含密碼管理員剛貼的密碼，當 sensitive
    # 檔案系統
    "manage_files",  # 整個函式都危險：mkdir/delete/move/copy 都能搞砸
    "write_file",
    "excel_write",
    "pdf_merge", "pdf_split",  # 寫新檔到磁碟
    "generate_quote",  # 寫報價檔
    # AI 燒錢動作
    "generate_image", "edit_image",
    # Vault / 機密管理（直接動 keychain）
    "set_vault_secret", "delete_vault_secret", "prune_vault_log",
    # 排程任務（背景執行任意動作）
    "add_scheduled_task", "remove_scheduled_task", "run_scheduled_task_now",
    # ERP workflow（學完直接跑）
    "learn_erp_from_video", "learn_erp_from_drive_folder",
    "merge_erp_workflows", "delete_erp_workflow",
    # ERP 任意唯讀 SQL — 雖唯讀，但直打生產 ERP：被 prompt-injection 導去跑
    # utl_http 外送/dbms_lock DoS/笛卡兒積 DoS 的風險高，且 guard 只擋寫入不擋
    # 一切讀。列為 sensitive → 每次 ad-hoc 查詢先 +確認（guard 黑名單另擋套件）。
    "run_erp_readonly_sql",
    # 樣品追蹤（公司營運資料寫入）
    "track_sample", "update_sample_status",
    "delete_tracked_sample", "close_sample",
    # QC / corrections / demo recording
    "set_qc_master", "correct_mistake", "delete_recorded_demo",
    # Memory / behavior 篡改
    "forget_memory", "forget_behavior", "delete_correction",
    # 聲紋（影響後續 auth flow）
    # Email lake 重建（耗時且寫入）
    "email_lake_rebuild", "prune_old_runs",
    # ⚠️ Dry-run toggle — 沒列等於攻擊者可以先關安全才動真格
    "enable_dry_run_mode", "disable_dry_run_mode",
    # Browser 自動化（執行 JS / 點按鈕 / 填表 = 任意網頁副作用）
    "browser_eval", "browser_click", "browser_fill", "browser_type",
    # 子代理（會展開使用所有 tool）
    "delegate_to_sub_agent", "delegate_to_sub_agents_parallel",
    # 學新 skill（會寫 .draft 到 skills/）
    "learn_skill_from_video",
    # C10 補丁（review round 3）：email_meeting_notes 內部直接 import & call
    # send_gmail，繞過 wrap_sensitive_tool（wrapper 只攔 tools_list 進入點）。
    # 把 email_meeting_notes 自身列為 sensitive — caller (LLM) 必須先 +確認。
    # C2 補丁 round 3（review 又發現一批）：
    # 政策放行（大王 2026-06-09 決定）：唯讀的「開檔 + 讀取以摘要」不再需要 +確認，故
    # open_url / browser_open / browser_read / browser_extract 移出敏感清單。取捨：
    # 它們仍可能被 prompt-injection 用來讀已登入分頁 / 開任意 URL，接受此風險以換取
    # 日常開檔摘要免確認。其餘瀏覽器動作維持 gate（new_tab / screenshot / eval /
    # click / fill / type / press / scroll）。
    "browser_new_tab",
    "browser_screenshot",              # 可拍開著的密碼管理員視窗
    "browser_press", "browser_scroll",
    "save_memory", "remember",         # 記憶寫入 — injected LLM 持久化惡意指令（self-replicating prompt）
    "learn_behavior",                  # 注入永久 persona rule
    "resolve_conflict",                # 手動 archive/revive behavior_policy 規則，同 learn_behavior 級
    "remember_correction_rule",        # 糾正固化（窄化版 learn_behavior）— CONFIRM 走 +確認；子代理一律濾除
    "record_box_ocr_correction",       # 嘜頭 OCR 糾正 — 寫 lexicon 影響後續辨識；CONFIRM；員工/子代理濾除
    "revoke_reflection",               # 刪反思洞察 — CONFIRM；子代理一律濾除
    "confirm_inferred_fact",           # 確認推論事實（窄化版 remember）— CONFIRM；子代理一律濾除
    "meeting_briefing",                # 直接寄 briefing email
    "batch_extract_quotes_from_parquet",  # 大量檔案處理 + 寫入
    "read_file",                       # 讀檔（path_safety 把關，但仍是 sensitive 動作）
    # Codex P2 (PR #18): correlate_alert tails var/logs/*.log directly
    # (bypassing read_file → bypassing path_safety's protected-dir gate)
    # and returns normalized error-line snippets to the LLM. Without
    # confirmation, prompt-injection can request an RCA and exfiltrate
    # log content (potentially PII / debug values that escape the
    # built-in redactor). Same tier as read_file — sensitive-but-not-
    # DANGEROUS, so a single +確認 / c covers it.
    "correlate_alert",
    # qc_inspect / qc_batch_inspect 內部 import + call send_gmail（C10 同型 bypass）
    "qc_inspect", "qc_batch_inspect",
    # 開麥克風 + screen 錄影
    "start_demo_recording",
    # 動態載入 skills/ — 若攻擊者可寫到 skills/ 即可 reload 後執行
    "reload_skills",
    # workflow / 評估狀態 mutation
    "process_monthly_invoices",
    "add_to_golden_set",
    # MCP filesystem 寫入入口（即使 server 自帶 allowed-dirs，仍是 sensitive）
    "mcp_filesystem_write_file",
    "mcp_filesystem_edit_file",
    "mcp_filesystem_create_directory",
    "mcp_filesystem_move_file",
    # Round 8 C8-1：對外 HTTP egress（**任何 URL 在 path 中即攜出 secret**，
    # response 是否 sanitize 與此無關 — outbound request 已洩）
    "read_website_content",        # bs4 抓網頁，URL 可塞 secret
    "web_access_diagnose",         # 任意 URL 診斷，同樣屬 outbound egress
    "web_access_diagnose_json",    # 任意 URL 診斷，同樣屬 outbound egress
    "web_domain_policy_set",       # 持久化改變網域存取策略
    "web_domain_policy_clear",
    "search_the_web",              # DuckDuckGo query 可塞 secret
    "mcp_fetch_fetch",             # 任意 URL 抓取
    # Briefing skill — 內部 import + call send_gmail / telegram_push（C10 同型）
    "send_briefing_email",
    "push_briefing_telegram",
    # Budget reset — LOCKED in tool_tiers（若 LLM 被 inject 可呼叫此把 budget
    # 歸零繞過上限），但漏列在這裡：sub_agents.py 的 deny-by-default 直接檢查
    # _SENSITIVE_TOOLS 成員，不經過 is_sensitive() 的 tier fallback，漏列會讓
    # 子代理委派濾不掉這個工具。
    "reset_budget",
    # Task queue — submit/cancel/requeue 都會觸發背景動作
    "submit_task",
    "cancel_task",
    "requeue_dead_letter",
    # Task memory — 寫類動作（含 delete）會修改大王 commitment 記憶
    "add_task",
    "update_task_status",
    "complete_task",
    "link_to_email",
    "link_to_calendar",
    "set_task_reminder",
    "delete_task",
    "set_recurring_reminder",
    "clear_recurring_reminder",
    "link_last_sent_email_to_task",
    # Work mode 切換 — 改持久化 state，要 +確認
    "set_work_mode",
    "exit_work_mode",
    # Telegram 檔案傳送 — 對外 egress
    "telegram_send_file",
    "telegram_send_photo",
    "telegram_send_attachment",
    # Vision RPA engine — 自主 UI 控制
    "fill_form",
    # IDP — fill_document / auto_fill 寫新 .docx
    "fill_document",
    "auto_fill_document",
    # Owner session 主控台 — 改別人 session 的控制狀態（暫停/恢復/重置）。
    # 列入 sensitive → 非 owner session build 時整顆移除（owner-only），
    # owner 在 Telegram 需 +確認（tool_tiers 定為 CONFIRM）。list_sessions
    # 是唯讀，改走 telegram_actor_scope.OWNER_PRIVATE_READ_TOOLS（免確認）。
    "pause_session",
    "resume_session",
    "reset_session",
    # broadcast 對外發訊（egress）— owner-only + +確認。
    "broadcast_message",
})


# Per-chat 最後一次確認時間戳。多個 chat 並行（不該發生但保險）也不會混。
# threading.Lock 守 read/write 防 race。
_state_lock = threading.Lock()
_confirm_state: dict[str, float] = {}  # chat_id → ts
# DANGEROUS 二次確認的時間戳（獨立於一般 confirm；同樣 one-shot）
_dangerous_confirm_state: dict[str, float] = {}  # chat_id → ts


# Round 6 Y9/Y12：chat_id 接受任何值是壞衛生 — Telegram chat_id 是 int
# （正/負，最多 ~19 位數）。空白 / path-traversal 樣式 / 超長字串都該拒絕。
def _is_valid_chat_id(chat_id) -> bool:
    """嚴格驗 chat_id 格式：純數字（含前綴 -），長度 1..20。"""
    if chat_id is None:
        return False
    if isinstance(chat_id, bool):  # True/False 是 int 子類，先排除
        return False
    if isinstance(chat_id, int):
        # int 0 也算可疑
        return chat_id != 0 and len(str(chat_id)) <= 20
    if isinstance(chat_id, str):
        s = chat_id.strip()
        if not s or s == "0":
            return False
        if len(s) > 20:
            return False
        # 允許：正數 / 負數 (group chat) / 純數字串
        if s.startswith("-"):
            return s[1:].isdigit()
        return s.isdigit()
    return False


def _confirm_scope_key(chat_id) -> str | None:
    """+確認 窗口的 scope key：可為單一 chat_id（"-100111"），或 per-user 複合鍵
    "<chat_id>:<from_id>"（綁定群裡以發訊者 from.id 區隔，見 daemon_telegram
    _confirm_scope_for）。每一段都要過 _is_valid_chat_id；合法回正規化字串，否則回
    None。複合鍵只當 dict/DB 的不透明鍵用，不會再被當真 chat_id 傳去送訊息。
    向後相容：原本傳純 chat_id 的呼叫者行為不變（單段＝原鍵）。"""
    s = str(chat_id or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) == 1:
        return s if _is_valid_chat_id(s) else None
    if len(parts) == 2 and _is_valid_chat_id(parts[0]) and _is_valid_chat_id(parts[1]):
        return s
    return None

# C6 rate-limit state：每個 chat 的最近確認時戳清單 + 鎖定到期時戳
_confirm_history: dict[str, list[float]] = {}  # chat_id → list[ts]（最多 _RATE_MAX_CONFIRMS+1 個）
_lockout_until: dict[str, float] = {}          # chat_id → 鎖定到期 ts
_gc_call_counter = 0                            # mark 呼叫計數，每 16 次觸發一次 _gc_state
# Round 4 LOW（review）：dict 大小 cap，避免暴露於多 chat_id 時長期累積記憶體
_MAX_TRACKED_CHATS = 1024


# ─────────────────────────────────────────────────────────────────────
# 一般訊息（非確認）rate-limit — 防 chat_id 被劫持後刷爆 Gemini 配額
# ─────────────────────────────────────────────────────────────────────
# 跟 +確認 rate-limit 不同：這個是擋日常 message stream 的暴量。每個被授權
# 的 chat_id（理論上只有大王）一分鐘最多 _MESSAGE_MAX_PER_WINDOW 條。超過
# 不 lockout（避免大王自己手抖暴擊就被鎖 10 分鐘），只**拒這一條**並回
# rate-limit 訊息；下一秒就有 quota 滾動釋出。
#
# Threshold rationale：
#   一分鐘 20 條已經是「大王打字飛快 + 連環追問」的上限，正常用法很少超過 5 條。
#   超過 20 條十之八九是被 prompt-injection 開無限對話、自動腳本灌訊息、或
#   chat_id 被盜後攻擊者刷費。
#
# Codex P1: history MUST persist across daemon restarts. The Telegram daemon
# self-exits at _TG_RESTART_AFTER_MSGS=30 (msg counter increments on every
# inbound message, including rate-limited rejections). So a flooder could:
#   • send 20 → all granted (Gemini-burning)
#   • send 10 rejected → still increments counter
#   • daemon restarts → in-memory history wiped → fresh 20 quota immediately
# Persisting to disk on every accept means the new process loads the
# already-full window and continues rejecting until oldest entry expires.
_MESSAGE_WINDOW_SEC = 60
_MESSAGE_MAX_PER_WINDOW = 20
_PG_AUTH_WARNING_UNTIL = 0.0


def _warn_pg_auth_fallback(exc: Exception) -> None:
    global _PG_AUTH_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_AUTH_WARNING_UNTIL:
        return
    _PG_AUTH_WARNING_UNTIL = now + 30
    try:
        from agent_core.logging_and_paths import logger

        logger.warning("Telegram auth Postgres state failed; using local fallback: %s", exc)
    except Exception:
        pass


def _pg_auth_state():
    try:
        from agent_core import operational_tg_auth_state
        if not operational_tg_auth_state.enabled():
            return None
        return operational_tg_auth_state
    except Exception as exc:  # noqa: BLE001 - optional cloud backend
        _warn_pg_auth_fallback(exc)
        return None


def _message_history_path() -> str:
    """Lazy resolution — keeps STATE_DIR import out of module init so
    test fixtures that patch logging_and_paths still work."""
    from agent_core.logging_and_paths import STATE_DIR
    return os.path.join(STATE_DIR, "tg_message_history.json")


def _load_message_history_from_disk() -> dict[str, list[float]]:
    """Restore the rate-limit window from disk on daemon start.

    GC-stale entries on load (past 2× window size). Tolerates corrupt /
    missing file by returning {} — fail open here is fine because the
    worst outcome is one full window of fresh quota for the attacker,
    which is the same as a clean install.
    """
    import json
    path = _message_history_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        cutoff = _MESSAGE_WINDOW_SEC * 2
        out: dict[str, list[float]] = {}
        for cid, history in raw.items():
            if not isinstance(history, list):
                continue
            recent = [float(t) for t in history
                      if isinstance(t, (int, float)) and (now - float(t)) <= cutoff]
            if recent:
                out[str(cid)] = recent
        return out
    except Exception:
        return {}


def _persist_message_history() -> None:
    """Merge this process's _message_history view onto disk under fcntl lock.

    Pre-flock, this did a plain atomic overwrite — fine for a single daemon,
    broken for 11 telegram color daemons. Each daemon holds its own copy of
    _message_history (loaded from disk at module import), and each only sees
    messages for ITS bot's chat_ids. If daemon A overwrites the whole file
    with its view, daemon B's chat_id entries vanish on disk. After daemon B
    restarts, it loads from disk → missing entries → flooder gets a fresh
    20-message quota (the exact bug Codex P1 was trying to prevent).

    Merge strategy: read disk fresh under lock, overlay this process's
    chat_id entries on top, write back. Other daemons' chat_id entries are
    untouched.

    Lock order: caller already holds _state_lock (threading). locked_json
    acquires the fcntl lock inside. _state_lock is always taken first, then
    locked_json — consistent everywhere, no deadlock.
    """
    try:
        with locked_json(_message_history_path(), default={}) as disk:
            for cid, hist in _message_history.items():
                disk[cid] = hist
    except Exception:
        # Persistence failure must never block dispatch — fail open.
        # In-memory history still works for the current process.
        pass


_message_history: dict[str, list[float]] = _load_message_history_from_disk()


def is_sensitive(tool_name: str) -> bool:
    """這個 tool 算 sensitive 嗎？

    後相容：sensitive = tier 至少是 CONFIRM（即非 SAFE）。
    現役 callers（sub_agents.py, dashboard.py 等）用此 API 時行為跟以前一樣。
    """
    if tool_name in _SENSITIVE_TOOLS:
        return True
    # tool_tiers 模組可能回 LOCKED / DANGEROUS 即使沒在 _SENSITIVE_TOOLS（很少見）
    try:
        from agent_core.tool_tiers import get_tier, TIER_SAFE
        return get_tier(tool_name) != TIER_SAFE
    except Exception:
        return False


def message_grants_confirmation(text: str) -> bool:
    """這條 user message 含確認 token 嗎？

    短碼 `c` 必須是整則訊息獨立出現（whole-message fullmatch），不能在
    句子裡混入 — 否則「write a C++ script」/「use column C」這種輸入
    會誤觸 confirmation。長碼（+確認 / /confirm / 確認執行 等）仍走
    sub-string search 因為它們不會出現在無辜訊息裡。
    """
    if not text:
        return False
    if _is_c_alias(text):
        return True
    return bool(_CONFIRM_TOKENS.search(text))


def message_grants_dangerous_confirmation(text: str, chat_id: str = "") -> bool:
    """這條 user message 含 DANGEROUS 二次確認 token 嗎？

    要跟一般 +確認 完全分開的 token：大王看到 `+雙確認` / `EXEC` 這種非日常
    字眼才會警覺「我為什麼在打這個」— 阻擋 prompt-injection 騙得逞。
    短碼 `cc` 同樣只認 whole-message fullmatch（防 "occurs"/"according"/
    "successful" 等內含 cc 的字誤觸）。

    Codex P2: bare `cc` short-code now requires a regular confirmation to
    already be active for the chat (passed via chat_id). Without it,
    a benign `cc` could pre-arm the DANGEROUS half before any +確認
    actually exists. Long-form tokens (+雙確認 / EXEC) keep working
    standalone because they're noisy enough to require deliberate intent.
    """
    if not text:
        return False
    if _is_cc_alias(text, chat_id=chat_id):
        return True
    return bool(_DANGEROUS_CONFIRM_TOKENS.search(text))


def _gc_state(now: float) -> None:
    """Round 4 LOW + round 5 X5：把過期的 history / lockout / confirm-state
    entry 清掉（避免多 chat_id 累積）。呼叫端必須已持有 _state_lock。"""
    # 清過期 lockout
    for cid in [k for k, v in _lockout_until.items() if v <= now]:
        _lockout_until.pop(cid, None)
    # 清空 history（沒有最近確認）
    for cid in [k for k, v in _confirm_history.items()
                if not v or (now - max(v)) > _RATE_WINDOW_SEC * 2]:
        _confirm_history.pop(cid, None)
    # X5：_confirm_state 過期（>2× window 沒再確認）也清掉
    for cid in [k for k, v in _confirm_state.items()
                if (now - v) > _RATE_WINDOW_SEC * 2]:
        _confirm_state.pop(cid, None)
    # DANGEROUS 確認過期（_DANGEROUS_WINDOW_SEC × 4 沒再確認）也清
    for cid in [k for k, v in _dangerous_confirm_state.items()
                if (now - v) > _DANGEROUS_WINDOW_SEC * 4]:
        _dangerous_confirm_state.pop(cid, None)
    # 一般訊息 rate-limit 的 history（>2× window 沒再發訊息）也清
    for cid in [k for k, v in _message_history.items()
                if not v or (now - max(v)) > _MESSAGE_WINDOW_SEC * 2]:
        _message_history.pop(cid, None)
    # 上限保護：超過 _MAX_TRACKED_CHATS 砍最舊一半
    if len(_confirm_history) > _MAX_TRACKED_CHATS:
        sorted_cids = sorted(_confirm_history.items(),
                             key=lambda kv: max(kv[1]) if kv[1] else 0)
        for cid, _ in sorted_cids[:len(sorted_cids) // 2]:
            _confirm_history.pop(cid, None)
            _confirm_state.pop(cid, None)
    if len(_message_history) > _MAX_TRACKED_CHATS:
        sorted_cids = sorted(_message_history.items(),
                             key=lambda kv: max(kv[1]) if kv[1] else 0)
        for cid, _ in sorted_cids[:len(sorted_cids) // 2]:
            _message_history.pop(cid, None)


def mark_confirmed(chat_id) -> bool:
    """大王發了確認訊息：記下時間。

    C6 rate-limit：同一 chat 在 _RATE_WINDOW_SEC 內超過 _RATE_MAX_CONFIRMS
    次確認 → 進入 _RATE_LOCKOUT_SEC 鎖定期。鎖定期內這個 call 不會更新
    _confirm_state，呼叫者收到 False 應印警告（但不要回 secret 資訊）。

    Round 6 Y9/Y12：chat_id 必須是合法 Telegram 格式（int 或全數字 str，
    含負號用於 group chat）。怪值（path-traversal、超長字串、空白等）一律拒。

    Returns:
        True：確認被接受
        False：被 rate-limit / lockout / 格式驗證擋下，未更新 state
    """
    cid = _confirm_scope_key(chat_id)
    if cid is None:
        return False
    now = time.time()
    store = _pg_auth_state()
    if store:
        try:
            return store.mark_confirmed(
                cid,
                now=now,
                rate_window_sec=_RATE_WINDOW_SEC,
                rate_max_confirms=_RATE_MAX_CONFIRMS,
                rate_lockout_sec=_RATE_LOCKOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 - fail open to local state
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        # Round 4 LOW: 偶爾 GC 過期 entry（每 16 次 mark 跑一次，攤平成本）。
        # 用呼叫計數器，不用 id(cid)&0xF — 後者因 CPython 物件對齊在 macOS 上幾乎恆為 0
        # （等於每次 mark 都全量 GC，失去攤平意義）。
        global _gc_call_counter
        _gc_call_counter = (_gc_call_counter + 1) & 0xF
        if _gc_call_counter == 0:
            _gc_state(now)
        # 仍在鎖定期？
        lock_until = _lockout_until.get(cid, 0.0)
        if lock_until > now:
            return False
        # 滾動清理 history（>_RATE_WINDOW_SEC 的丟掉）
        hist = [t for t in _confirm_history.get(cid, []) if (now - t) <= _RATE_WINDOW_SEC]
        # 超出 quota？進入 lockout
        if len(hist) >= _RATE_MAX_CONFIRMS:
            _lockout_until[cid] = now + _RATE_LOCKOUT_SEC
            _confirm_history[cid] = hist  # 留下歷史
            return False
        # 接受
        hist.append(now)
        _confirm_history[cid] = hist
        _confirm_state[cid] = now
        return True


def is_locked_out(chat_id) -> tuple[bool, float]:
    """這 chat 還在 rate-limit 鎖定期嗎？回 (locked, 剩餘秒數)。"""
    cid = _confirm_scope_key(chat_id)
    if cid is None:
        return False, 0.0
    now = time.time()
    store = _pg_auth_state()
    if store:
        try:
            return store.is_locked_out(cid, now=now)
        except Exception as exc:  # noqa: BLE001
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        lock_until = _lockout_until.get(cid, 0.0)
    remaining = lock_until - now
    return remaining > 0, max(0.0, remaining)


def check_message_rate_limit(chat_id) -> tuple[bool, float, int]:
    """Per-chat sliding-window rate limit for inbound Telegram messages.

    Defends against chat-account compromise: an attacker (or a prompt-injection
    loop) could otherwise spam the bot with arbitrary text and burn through
    the daily Gemini quota / generate cost — even without ever touching a
    sensitive tool, because a single Gemini chat turn already costs money.

    Different from the +確認 rate-limit (mark_confirmed):
      • Confirmation rate-limit triggers a 10-minute lockout because each
        confirmation is a security-critical event.
      • Message rate-limit only rejects the **current** over-quota message
        and lets the window roll forward. No lockout — that would be too
        annoying when the user himself just types fast.

    Returns:
      (allowed, retry_after_sec, count_in_window)

      allowed=True   — message under quota, caller may proceed; retry_after=0.
      allowed=False  — message over quota for this chat; caller should NOT
                       call Gemini and should return a friendly "slow down"
                       message. retry_after is "seconds until oldest msg in
                       window expires" so the caller can tell the user when
                       they can try again.

    A bad chat_id (None / empty / wrong shape) returns (False, 0, 0) — defends
    against an unauthenticated path slipping past upstream auth checks.
    """
    if not _is_valid_chat_id(chat_id):
        return False, 0.0, 0
    now = time.time()
    cid = str(chat_id).strip()
    store = _pg_auth_state()
    if store:
        try:
            return store.check_message_rate_limit(
                cid,
                now=now,
                window_sec=_MESSAGE_WINDOW_SEC,
                max_per_window=_MESSAGE_MAX_PER_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        # Same GC strategy as mark_confirmed (cheap, amortised every ~16 calls).
        # 用呼叫計數器，不用 id(cid)&0xF —— 後者因 CPython macOS 物件對齊幾乎恆為 0、
        # 等於每則訊息都在 _state_lock 內全量 GC（健檢 Low；mark_confirmed 已修、這裡漏）。
        global _gc_call_counter
        _gc_call_counter = (_gc_call_counter + 1) & 0xF
        if _gc_call_counter == 0:
            _gc_state(now)
        # Roll forward the window.
        hist = [t for t in _message_history.get(cid, []) if (now - t) <= _MESSAGE_WINDOW_SEC]
        count = len(hist)
        if count >= _MESSAGE_MAX_PER_WINDOW:
            # Over quota: don't append (so we don't keep extending the wait).
            # Tell caller how long until the OLDEST entry in the window
            # expires — that's when one slot opens up.
            retry_after = max(0.0, _MESSAGE_WINDOW_SEC - (now - hist[0]))
            _message_history[cid] = hist
            # No persistence here — over-quota rejections don't change
            # the persisted window meaningfully (we already have the entries
            # they're trying to add to from previous accepts).
            return False, retry_after, count
        # Accept and record.
        hist.append(now)
        _message_history[cid] = hist
        # Codex P1: persist on every accept so daemon restart can't reset
        # the window. _TG_RESTART_AFTER_MSGS exits the daemon every 30
        # processed messages; without persistence a flooder gets a fresh
        # 20-message Gemini quota right after the restart.
        _persist_message_history()
        return True, 0.0, count + 1


def check_confirmed(chat_id, *, window_sec: int = _CONFIRM_WINDOW_SEC) -> tuple[bool, float]:
    """這個 chat 在 window 內有 confirm 嗎？回 (yes/no, 距上次秒數)。"""
    cid = _confirm_scope_key(chat_id)
    if cid is None:
        return False, -1.0
    now = time.time()
    store = _pg_auth_state()
    if store:
        try:
            return store.check_confirmed(
                cid,
                now=now,
                window_sec=window_sec,
            )
        except Exception as exc:  # noqa: BLE001
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        ts = _confirm_state.get(cid, 0.0)
    if ts <= 0:
        return False, -1.0
    elapsed = now - ts
    return elapsed <= window_sec, elapsed


def revoke_after_use(chat_id: str) -> None:
    """One-shot：執行完 sensitive op 立刻 revoke，下個 op 要再確認。

    M5 補丁（review）：原本 window-based — 大王確認後 90 秒內想做幾個
    sensitive op 都行。但這代表 prompt-injection 觸發 LLM 在大王確認後
    串連多個 sensitive op 也會被允許。one-shot 模式擋掉這個。
    """
    if not chat_id:
        return
    cid = _confirm_scope_key(chat_id)
    if cid is None:
        return
    store = _pg_auth_state()
    if store:
        try:
            store.revoke_after_use(cid)
        except Exception as exc:  # noqa: BLE001
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        _confirm_state.pop(cid, None)
        # DANGEROUS confirm 也是 one-shot：跑完一個 DANGEROUS 動作就過期
        _dangerous_confirm_state.pop(cid, None)


def consume_confirmation(chat_id, *, window_sec: int = _CONFIRM_WINDOW_SEC) -> tuple[bool, float]:
    """原子消費 one-shot +確認：在 _state_lock 內 check+pop 一次完成。

    健檢 LOW（check→revoke 非原子）：wrap_sensitive_tool 原本
    check_confirmed()（讀）→ 執行 → finally revoke_after_use()（寫）之間有
    窗口 —— 軟超時轉背景的舊推理線程與新對話輪可在窗口內共用同一枚 one-shot
    確認（一枚確認放行兩個 sensitive op）。這裡把 check 與 revoke 合併在同
    一把鎖內，兩個並發呼叫只有一個拿得到 True。

    消費語意與舊「finally 必 revoke」等價（工具執行失敗不退還確認）；成功
    消費時連 DANGEROUS 二次確認一併撤銷（同 revoke_after_use 的 one-shot
    語意）。群組複合 scope（"<chat_id>:<from_id>"）與私聊單段 scope 走同一
    路（都經 _confirm_scope_key 正規化）。

    Returns:
        (consumed, elapsed)：consumed=True 表示 window 內有確認且本呼叫已把
        它用掉；False 時 elapsed=-1.0（沒確認或已被別人用掉）或 >window_sec
        （已過期）—— 對齊 check_confirmed 的回傳語意。
    """
    cid = _confirm_scope_key(chat_id)
    if cid is None:
        return False, -1.0
    now = time.time()
    store = _pg_auth_state()
    if store:
        try:
            # PG 後端沒有原子 consume API（operational_tg_auth_state 不在本次
            # 修改範圍）：退回 check→revoke 兩步。跨 process 的窗口仍在，但不
            # 比修前更差；單 process 內的主要 race（軟超時背景線程 vs 新對話
            # 輪）由下面本地路徑補上。
            ok, elapsed = store.check_confirmed(cid, now=now, window_sec=window_sec)
            if ok:
                store.revoke_after_use(cid)
            return ok, elapsed
        except Exception as exc:  # noqa: BLE001 - fail open to local state
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        ts = _confirm_state.get(cid, 0.0)
        if ts <= 0:
            return False, -1.0
        elapsed = now - ts
        if elapsed > window_sec:
            return False, elapsed
        _confirm_state.pop(cid, None)
        # DANGEROUS confirm 也是 one-shot：跟 revoke_after_use 一致
        _dangerous_confirm_state.pop(cid, None)
        return True, elapsed


def mark_dangerous_confirmed(chat_id) -> bool:
    """大王發了 DANGEROUS 二次確認 token：記下時間。

    跟 mark_confirmed 不互斥 — 大王要先打 +確認 再打 +雙確認 兩個 token，
    DANGEROUS tool 才會 fire。

    Returns:
        True：接受
        False：chat_id 格式錯 / lockout（複用 mark_confirmed 的 rate-limit 狀態，
              因為一般 confirm 已限速；這裡不再加額外 quota）
    """
    cid = _confirm_scope_key(chat_id)
    if cid is None:
        return False
    now = time.time()
    store = _pg_auth_state()
    if store:
        try:
            return store.mark_dangerous_confirmed(cid, now=now)
        except Exception as exc:  # noqa: BLE001
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        # 共用 lockout（被 rate-limit 鎖住時連 dangerous-confirm 也擋）
        if _lockout_until.get(cid, 0.0) > now:
            return False
        _dangerous_confirm_state[cid] = now
        return True


def check_dangerous_confirmed(chat_id, *, window_sec: int = _DANGEROUS_WINDOW_SEC) -> tuple[bool, float]:
    """這 chat 在 DANGEROUS window 內有打過二次確認嗎？回 (yes/no, 距上次秒數)。"""
    cid = _confirm_scope_key(chat_id)
    if cid is None:
        return False, -1.0
    now = time.time()
    store = _pg_auth_state()
    if store:
        try:
            return store.check_dangerous_confirmed(
                cid,
                now=now,
                window_sec=window_sec,
            )
        except Exception as exc:  # noqa: BLE001
            _warn_pg_auth_fallback(exc)
    with _state_lock:
        ts = _dangerous_confirm_state.get(cid, 0.0)
    if ts <= 0:
        return False, -1.0
    elapsed = now - ts
    return elapsed <= window_sec, elapsed


def wrap_sensitive_tool(fn: Callable, *, get_chat_id: Callable[[], str],
                         channel: str = "telegram") -> Callable:
    """把一個 sensitive tool 包成 tier-aware「需要確認才執行」版本。

    新版（permission tier system）：
      - SAFE      → pass-through（理論上 SAFE 不會被丟進來，外層會跳過）
      - CONFIRM   → 90s `+確認` token gate（M5 one-shot revoke）
      - DANGEROUS → token gate + 警示前綴（回應前加 🔴 標）
      - LOCKED    → 直接拒絕，給 mac REPL 替代方案

    Args:
        fn: 原 tool function。
        get_chat_id: callable，每次 call 回傳當前 chat_id。
        channel: 'telegram' / 'voice' / 'daemon' / 'repl'。預設 telegram。

    Returns:
        wrapped callable，依 channel × tier matrix 決定行為。
    """
    from agent_core.tool_tiers import (
        get_tier, get_check_method, refusal_message,
        TIER_DANGEROUS, TIER_CONFIRM, _TIER_ICON,
    )

    # Auto-audit：對 CONFIRM 以上的 sensitive tool，自動掛 @audited，這樣
    # dashboard runs_trend 才看得到真實樣本（之前 @audited 覆蓋率 < 5%，
    # 失敗率統計失真）。SAFE tier 的 read-only tool 不掛，避免 jsonl 爆量。
    _wrapped_fn = fn
    try:
        _tier_for_audit = get_tier(fn.__name__)
        if (_tier_for_audit in (TIER_CONFIRM, TIER_DANGEROUS)
                and not getattr(fn, "_audited", False)):
            from agent_core.run_history import audited as _run_audited
            _wrapped_fn = _run_audited()(fn)
    except Exception:
        pass  # audit hook 失敗不該擋住 sensitive tool 本身

    # fn 的 signature 固定，wrap 期解析一次即可；這個 wrapper 在每個敏感工具
    # 呼叫的熱路徑上，之前每次呼叫都重跑 inspect.signature(fn) 純屬浪費。
    try:
        import inspect as _inspect
        _fn_sig = _inspect.signature(fn)
    except (TypeError, ValueError):
        _fn_sig = None

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        chat_id = ""
        try:
            chat_id = get_chat_id() or ""
        except Exception:
            chat_id = ""

        tier = get_tier(fn.__name__)
        method = get_check_method(channel, tier)

        # ── policy_engine 中央決策（含 env override + content-level risk）──
        # 為什麼放在 method 判斷前面：env RED_BLOCK_TOOL / risk_score >= 90
        # 比 channel × tier 矩陣優先，REPL 也擋。
        # 把 positional args 透過 signature 轉成 named，risk_guard 才掃得到
        # （e.g. run_shell("rm -rf /") 是 positional，要轉成 {cmd: "rm -rf /"}）
        merged_kwargs = dict(kwargs)
        if _fn_sig is not None:
            try:
                bound = _fn_sig.bind_partial(*args, **kwargs)
                merged_kwargs = dict(bound.arguments)
            except (TypeError, ValueError):
                pass  # bind 失敗就退回只看 kwargs

        # 守底防線：policy_engine 模組壞掉時，env RED_BLOCK_TOOL（系統管理員
        # 緊急 kill switch）也要繼續生效。inline 一份檢查，不依賴 policy_engine
        # 能 import 起來。
        try:
            _blk = os.environ.get("RED_BLOCK_TOOL", "").strip()
            if _blk:
                import fnmatch
                _blocked = {x.strip() for x in _blk.split(",") if x.strip()}
                # glob 比對，對齊 policy_engine 主路徑 + 文件範例 RED_BLOCK_TOOL=run_shell,delete_*
                # （健檢 Low：原本精確比對 → policy_engine 掛掉的降級態下 delete_* 靜默失效）。
                if any(fnmatch.fnmatch(fn.__name__, _p) for _p in _blocked):
                    from agent_core.tool_result import ToolResult, ErrorCode
                    return ToolResult.failure(
                        f"⛔ 工具 `{fn.__name__}` 被 env RED_BLOCK_TOOL 擋掉",
                        error_code=ErrorCode.PERMISSION_DENIED,
                        recoverable=False,
                        suggested_fix="若要解除：unset RED_BLOCK_TOOL 後重啟 daemon",
                    )
        except Exception:
            pass  # env 讀取本身失敗只能放過，繼續走 policy_engine

        try:
            from agent_core.policy_engine import evaluate_policy
            from agent_core.tool_result import ToolResult, ErrorCode
            policy = evaluate_policy(fn.__name__, channel=channel,
                                      kwargs=merged_kwargs)
            if not policy.allow:
                # 區分 layer → 對應 ErrorCode
                if policy.reason_layer == "env_override":
                    code = ErrorCode.PERMISSION_DENIED
                elif policy.reason_layer == "risk_guard":
                    code = ErrorCode.PERMISSION_DENIED
                else:
                    code = ErrorCode.LOCKED_TIER
                return ToolResult.failure(
                    policy.reason, error_code=code, recoverable=False,
                    suggested_fix=policy.suggested_action,
                )
        except Exception:
            policy = None  # 政策模組壞掉 → tier 仍會擋，env_override 已在上面 inline 過

        # RED_FORCE_DRY_RUN_FOR：管理員要求這個 tool 只「模擬、不真正執行」。
        # policy_engine 算出了 forced_dry_run 但過去沒人消費 —— 設了等於沒效果。
        # 放在確認門之前：既然不會有副作用，就不需要 +確認，直接回模擬描述。
        if policy is not None and getattr(policy, "forced_dry_run", False):
            try:
                from agent_core.dry_run import get_dry_run_describer, _record_simulated
                describer = get_dry_run_describer(fn.__name__)
                if describer is not None:
                    try:
                        # Call with the signature-bound merged_kwargs (same as
                        # _record_simulated below), NOT raw *args/**kwargs: the
                        # describers take named params + **_, so a positional
                        # invocation would otherwise raise TypeError and degrade
                        # to the "（describe fn 出錯）" placeholder.
                        desc = describer(**merged_kwargs)
                    except Exception as e:
                        desc = f"（describe fn 出錯：{e}）"
                else:
                    desc = f"會呼叫 {fn.__name__}（被 RED_FORCE_DRY_RUN_FOR 攔為模擬）"
                try:
                    _record_simulated(fn.__name__, desc, merged_kwargs)
                except Exception:
                    pass
                return f"🧪 [DRY RUN｜RED_FORCE_DRY_RUN_FOR] {fn.__name__}\n  {desc}"
            except Exception:
                pass  # dry_run 模組壞掉 → 退回正常流程（理論上不會發生）

        if method == "allow":
            return _wrapped_fn(*args, **kwargs)

        if method == "refuse":
            from agent_core.tool_result import ToolResult, ErrorCode
            msg = refusal_message(channel, fn.__name__)
            return ToolResult.failure(
                msg, error_code=ErrorCode.LOCKED_TIER,
                recoverable=False,
                suggested_fix=f"用其他 channel（REPL）執行 `{fn.__name__}`，"
                              f"或確認此工具是否真該為 LOCKED tier",
            )

        # method ∈ {"token", "token+warn"}
        ok, elapsed = check_confirmed(chat_id)
        # DANGEROUS 額外要求二次確認 token（+雙確認 / EXEC）
        if ok and method == "token+warn":
            d_ok, d_elapsed = check_dangerous_confirmed(chat_id)
            if not d_ok:
                from agent_core.tool_result import ToolResult, ErrorCode
                icon = _TIER_ICON[tier]
                msg = (
                    f"{icon} `{fn.__name__}` 為 DANGEROUS 級 — 已收到 +確認，"
                    f"還需要二次確認才會真的跑。\n"
                    f"   ↑ 這道門擋的是「prompt-injection 騙大王連續確認」攻擊：\n"
                    f"   攻擊者就算騙到一次 +確認，也猜不到下個 token。"
                )
                return ToolResult.failure(
                    msg, error_code=ErrorCode.NEEDS_CONFIRMATION,
                    recoverable=True,
                    suggested_fix=f"在 {_DANGEROUS_WINDOW_SEC}s 內回覆含 `+雙確認` 或 `EXEC` 的訊息",
                )
        if ok:
            # Budget check（在 fn() 之前）
            try:
                from agent_core.tool_budgets import check_budget, record_use
                budget_ok, budget_msg = check_budget(fn.__name__)
            except Exception:
                budget_ok, budget_msg = True, ""
                record_use = None  # type: ignore
            if not budget_ok:
                # 不消耗 confirm token — consume_confirmation 在 budget 檢查
                # 之後才發生（攻擊者不能用「故意觸 budget 上限」當作 DOS 把
                # confirm 用掉）
                from agent_core.tool_result import ToolResult, ErrorCode
                return ToolResult.failure(
                    budget_msg, error_code=ErrorCode.BUDGET_EXHAUSTED,
                    recoverable=True,
                    suggested_fix=f"等隔天 00:00 額度重置，或 env "
                                  f"`RED_BUDGET_{fn.__name__.upper()}_DAILY=N` 提高",
                )
            # 健檢 LOW（check→revoke 非原子）：舊流程 check_confirmed（上面）
            # → 執行 → finally revoke_after_use 之間有窗口，軟超時轉背景的舊
            # 推理線程與新對話輪可共用同一枚 one-shot 確認。改成執行前在鎖內
            # 原子消費（check+pop 一次完成）；輸掉 race 的呼叫落到下面
            # NEEDS_CONFIRMATION 路徑。消費語意跟舊「finally 必 revoke」一致：
            # 執行前已消費 = 工具執行失敗（含 raise）不退還確認。
            if _ONE_SHOT:
                ok, elapsed = consume_confirmation(chat_id)
        if ok:
            result = _wrapped_fn(*args, **kwargs)
            # 成功才 record（失敗的 op 不消耗 budget）——「成功」以
            # ToolResult.ok 為準：工具回 ToolResult.failure（沒 raise）
            # 一樣是失敗，不該扣 budget。plain string 結果視為成功。
            if record_use is not None and getattr(result, "ok", True):
                try:
                    record_use(fn.__name__)
                except Exception:
                    pass
            if method == "token+warn":
                # 危險動作多加警示頭，提醒大王 / LLM 看清楚做了什麼
                icon = _TIER_ICON[tier]
                warn_prefix = (
                    f"{icon} 已執行 DANGEROUS 動作 `{fn.__name__}` "
                    f"— 請確認結果無誤；audit 紀錄已寫入 runs/。\n"
                    f"{'-' * 50}\n"
                )
                # 不能 `warn_prefix + str(result)` 壓平：ToolResult 是 str
                # 子類，壓平會把 .ok/.error_code/.data/.artifacts 全丟掉
                # （下游 daemon retry 判斷 / dashboard 分類就瞎了）。
                # 用同一組 metadata 重建、只換文字表面。
                from agent_core.tool_result import ToolResult
                if isinstance(result, ToolResult):
                    return ToolResult(
                        warn_prefix + str(result),
                        ok=result.ok,
                        summary=result.summary,
                        error_code=result.error_code,
                        message=result.message,
                        recoverable=result.recoverable,
                        suggested_fix=result.suggested_fix,
                        data=result.data,
                        warnings=result.warnings,
                        cost=result.cost,
                        artifacts=result.artifacts,
                    )
                return warn_prefix + str(result)
            return result

        # 沒 confirm — 回 user-readable refusal
        from agent_core.tool_result import ToolResult, ErrorCode
        if elapsed < 0:
            timing = "本次 chat 還沒確認過（或上次確認已用掉）"
        else:
            timing = f"上次確認在 {int(elapsed)} 秒前（已超過 {_CONFIRM_WINDOW_SEC} 秒窗）"
        icon = _TIER_ICON[tier]
        warn_extra = ""
        if method == "token+warn":
            warn_extra = (
                "\n   ⚠️ 此工具為 DANGEROUS 級（刪除/批次/code-exec）— "
                "需要 **兩道** 確認（+確認、再打 +雙確認 或 EXEC）。\n"
                "   請務必確認 LLM 描述的動作完全是大王要的（防 prompt-injection）。"
            )
        msg = (
            f"{icon} 需要大王確認才能執行 `{fn.__name__}`（tier={tier}）。\n"
            f"   {timing}。\n"
            f"   （one-shot：一次確認只通一個動作）"
            + warn_extra
        )
        return ToolResult.failure(
            msg, error_code=ErrorCode.NEEDS_CONFIRMATION, recoverable=True,
            suggested_fix=("在訊息中含「+確認」或「確認執行」再叫我做"
                          + ("；DANGEROUS 還需 +雙確認" if method == "token+warn" else "")),
        )

    # 保留 markers
    for attr in ("_is_skill", "_is_mcp_tool", "_mcp_server", "_mcp_tool",
                 "background_safe", "_audited", "_dry_run_wrapped"):
        if hasattr(fn, attr):
            setattr(wrapper, attr, getattr(fn, attr))
    wrapper._tg_auth_wrapped = True
    wrapper._tg_auth_channel = channel
    wrapper._tg_auth_tier = get_tier(fn.__name__)
    return wrapper


def filter_tools_for_telegram(tools_list: list, *, get_chat_id: Callable[[], str]) -> list:
    """把整份 tools_list 篩過：sensitive 的包確認門，其他原樣。

    Args:
        tools_list: agent.py 全域 tools_list（264 個）。
        get_chat_id: thunk 回傳當前 chat_id。

    Returns:
        新 list（同樣 264 個），sensitive 的都換成 wrapped 版本。
    """
    out = []
    for fn in tools_list:
        name = getattr(fn, "__name__", "")
        if is_sensitive(name) and not getattr(fn, "_tg_auth_wrapped", False):
            out.append(wrap_sensitive_tool(fn, get_chat_id=get_chat_id,
                                            channel="telegram"))
        else:
            out.append(fn)
    return out


def filter_tools_for_voice(tools_list: list, *, get_chat_id: Callable[[], str]) -> list:
    """同 filter_tools_for_telegram，但 channel='voice'。

    voice 頻道差別（見 tool_tiers._CHANNEL_RULES）：
      - DANGEROUS 一律 refuse（Whisper 誤聽風險太高）
      - LOCKED 一律 refuse
      - SAFE/CONFIRM 跟 telegram 一樣（CONFIRM 走 token gate）

    呼叫時機：daemon_telegram.py 收到語音訊息（voice note），轉文字後
    要叫 LLM 用此版本（不是 telegram 版）— 這樣即使 Whisper 把雜音聽成
    「請刪掉所有檔案」，DANGEROUS 那層 refuse 會擋下。
    """
    out = []
    for fn in tools_list:
        name = getattr(fn, "__name__", "")
        if is_sensitive(name) and not getattr(fn, "_tg_auth_wrapped", False):
            out.append(wrap_sensitive_tool(fn, get_chat_id=get_chat_id,
                                            channel="voice"))
        else:
            out.append(fn)
    return out
