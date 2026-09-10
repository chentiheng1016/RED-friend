"""Per-mode 工具 / 權限 policy — 每個 work mode 規定哪些 tool 可用、哪些 tier 禁用。

跟其他 policy 層的關係：
  tool_tiers.py        基礎 4 tier（safe / confirm / dangerous / locked）
  tg_auth.py           channel × tier 確認門
  policy_engine.py     env override + content-level risk
  intent_router.py     依使用者請求 narrow tool catalog
  mode_policy.py（本）  依大王當前 work mode narrow tool catalog + block tier

層次順序（由寬到嚴）：
  全 300 個 tool
   ↓ intent_filter（依訊息語意 narrow，~30 個）
   ↓ mode_filter（依工作模式 narrow，再交集，~15 個）
   ↓ tier × channel 矩陣
   ↓ +確認 / +雙確認
   ↓ risk_guard content-level
   ↓ tool_budgets

Mode 只控制**前段 narrow** + **block_tier**，不取代後面的安全檢查。
"""
from __future__ import annotations


# ────────────────────────────────────────────────────────────────────
# Per-mode rules
# ────────────────────────────────────────────────────────────────────
# 規則：
#   tool_names: frozenset 或 None（None = 全 tool 集，不 narrow）
#   blocked_tiers: 該 mode 拒絕的 tier 清單
#   max_response_chars: LLM 回應字數上限（0 = 無限）— 提示 LLM 用，非硬限
#
# meeting mode：tool_names 嚴格列舉（會議中只該用 read + 記筆記）
# sales mode：tool_names 嚴格列舉（業務只該動客戶 / 寄信）
# dev mode：tool_names = None（全集），但提示 LLM 主動用 shell
# security mode：安全研究 subset；允許受確認門控的 shell/python，但不包含寄信/RPA/記憶寫入等無關外送或持久化工具
# normal mode：tool_names = None（全集），無限制
_MODE_RULES: dict[str, dict] = {
    "normal": {
        "tool_names": None,
        "blocked_tiers": [],
        "max_response_chars": 0,
        "description": "預設 — 全 tool 集，無限制",
    },

    "meeting": {
        # 會議中只允許 read 類 + 記筆記（add_task）
        "tool_names": frozenset({
            # Read / query
            "recall", "search_gmail", "read_gmail", "summarize_inbox",
            "query_email_lake", "list_calendar_events",
            "briefing_next_meeting", "meeting_briefing", "briefing_preview",
            "list_specs", "compare_specs", "list_recent_files",
            "show_log_tail",
            # Customer lookup（會議中常需要查客戶背景）
            "customer_360", "list_active_customers",
            "query_po_timeline", "query_customer_timeline",
            # Task memory — 會議中「記下來」是核心
            "add_task", "update_task_status", "complete_task",
            "set_task_reminder", "task_status", "task_detail",
            "list_tasks", "find_tasks_by",
            "tasks_due_today", "tasks_overdue", "tasks_for_email",
            # System awareness
            "system_status", "system_alerts",
            # Time / date
            "list_calendar_events",
        }),
        "blocked_tiers": ["dangerous"],  # 會議中誤觸破壞性動作風險高
        "max_response_chars": 200,
        "description": "會議中 — 只 read + 記筆記，禁 DANGEROUS",
    },

    "sales": {
        # 業務模式：客戶 / 報價 / 寄信
        "tool_names": frozenset({
            # Customer lookup（核心）
            "customer_360", "list_active_customers", "customer_alerts",
            "query_po_timeline", "query_customer_timeline", "list_customer_pos",
            "resolve_entity", "list_entity_aliases",
            "tw_company_lookup",
            # Email — 業務常寄信
            "search_gmail", "read_gmail", "summarize_inbox",
            "send_gmail", "reply_gmail", "create_draft",
            "download_gmail_attachment",
            "analyze_email_reply_times",
            # Quote / spec
            "generate_quote", "list_specs", "compare_specs", "parse_spec_sheet",
            # Calendar — 業務常約見客戶
            "list_calendar_events", "create_calendar_event",
            "update_calendar_event", "respond_to_event", "suggest_time",
            # Recall + task tracking（持續 follow-up）
            "recall",
            "add_task", "list_tasks", "find_tasks_by",
            "tasks_for_email", "tasks_due_today", "tasks_overdue",
            "set_task_reminder", "complete_task",
            "link_to_email", "link_to_calendar", "link_last_sent_email_to_task",
            # System
            "system_status",
        }),
        "blocked_tiers": [],
        "max_response_chars": 0,
        "description": "業務模式 — 客戶 / 報價 / 寄信 follow-up",
    },

    "dev": {
        # 開發模式：全 tool 集（包括 shell / python / files）
        # 不 narrow tool — dev 隨時可能要查任何東西
        "tool_names": None,
        "blocked_tiers": [],
        "max_response_chars": 0,
        "description": "開發模式 — 全 tool，技術用語，主動用 shell",
    },

    "security": {
        # 安全研究模式：保留診斷、稽核、讀取、合法媒體安全研究、
        # shell/python（仍是 DANGEROUS tier，後段確認門不變）。
        "tool_names": frozenset({
            # System / audit / policy posture
            "system_status", "system_alerts", "overview_text",
            "correlate_alert", "health_check", "show_log_tail",
            "list_runs", "show_run", "find_past_actions",
            "run_history_stats", "list_tools_by_tier",
            "tool_budget_status", "risk_assessment",
            "evaluate_policy_text", "policy_recent",
            "env_override_status", "dry_run_status",
            "last_dry_run_log",
            # Local read / analysis
            "read_file", "search_drive_docs", "recall",
            "recall_reranked", "preview_expansion",
            "multihop_query", "preview_multihop_plan",
            "read_website_content", "web_access_diagnose",
            "web_access_diagnose_json", "web_domain_policy_list",
            "search_the_web",
            # Controlled code execution for authorized lab diagnostics
            "run_shell", "run_python_code",
            # Browser inspection without UI side effects beyond the browser
            "browser_open", "browser_status", "browser_read",
            "browser_wait_for", "browser_extract",
            "browser_screenshot",
            "browser_new_tab", "browser_close",
            # Media/DRM safety research already constrained in media_security.py
            "media_security_blueprint", "inspect_iso_bmff",
            "parse_pssh_box", "generate_cenc_key_material",
            "build_ffmpeg_cenc_command", "simulate_license_challenge",
            "package_hls_aes128", "analyze_drm_manifest",
            "download_hls_with_n_m3u8dl", "download_hls_with_ffmpeg_copy",
            "download_hls_with_ytdlp", "assess_drm_request_safety",
            "drm_chain_of_trust_model", "build_eme_player_template",
        }),
        "blocked_tiers": [],
        "max_response_chars": 0,
        "description": "安全研究 — 架構/稽核/實驗室 PoC，保留確認門",
    },

    "quant": {
        # 量化深度模式：不 narrow tool — 分析常要 run_python_code / search_the_web /
        # recall / query_* / read_drive_file 任意組合，跟 dev 一樣放全集。
        "tool_names": None,
        "blocked_tiers": [],
        "max_response_chars": 0,
        "description": "量化深度 — 經濟/微積分/統計，step-by-step + 驗算",
    },

    "cfo": {
        # 財務長模式：不 narrow tool — 財務問答常要 finance_* 三件套之外再交叉
        # email/Drive/ERP 查證（例：這筆未付請示對應哪封信），放全集、行為差異
        # 全靠 persona addendum。財務工具本身是 owner-only（不進員工白名單），
        # 不需要在 mode 層再擋。
        "tool_names": None,
        "blocked_tiers": [],
        "max_response_chars": 0,
        "description": "財務長 — 損益/資金/成本三本帳，數字照表唸",
    },
}


# ────────────────────────────────────────────────────────────────────
# Public helpers
# ────────────────────────────────────────────────────────────────────
def get_mode_rules(mode: str) -> dict:
    """取得 mode 對應的 rules dict。未知 mode 回 normal 的 rules。"""
    return _MODE_RULES.get(mode, _MODE_RULES["normal"])


def known_modes() -> list[str]:
    return list(_MODE_RULES.keys())


def filter_tools_by_mode(tools_list: list, mode: str) -> list:
    """🟢 把 tools_list 按 mode 篩選。

    跟 intent_router.filter_tools_by_intent 邏輯類似，但根據 mode：
      - tool_names = None → 不篩，回原 list
      - tool_names = frozenset → 只保留 names 在內 + 過濾 blocked_tiers

    Args:
        tools_list: 完整 tool list
        mode: 'normal' / 'meeting' / 'sales' / 'dev' / 'security' / 'quant' / 'cfo'

    Returns:
        篩過的新 list（subset）
    """
    rules = get_mode_rules(mode)
    allowed_names = rules.get("tool_names")
    blocked_tiers = set(rules.get("blocked_tiers") or [])

    # 先處理 blocked_tiers
    if blocked_tiers:
        try:
            from agent_core.tool_tiers import get_tier
            tools_list = [
                fn for fn in tools_list
                if get_tier(getattr(fn, "__name__", "")) not in blocked_tiers
            ]
        except Exception:
            pass

    # 再處理 allowed_names
    if allowed_names is None:
        return tools_list  # 不 narrow（dev / normal）
    return [
        fn for fn in tools_list
        if getattr(fn, "__name__", "") in allowed_names
    ]


def mode_blocks_tier(mode: str, tier: str) -> bool:
    """檢查 mode 是否 block 某 tier。"""
    rules = get_mode_rules(mode)
    return tier in (rules.get("blocked_tiers") or [])


def mode_max_response_chars(mode: str) -> int:
    """取 mode 的回應字數上限（0 = 無限）。"""
    rules = get_mode_rules(mode)
    return int(rules.get("max_response_chars", 0))
