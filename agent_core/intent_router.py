"""Intent router — 把使用者請求分類，再決定載入哪批 tool。

問題：現在每個 Telegram 訊息都把 292 個 tool 全丟給 Gemini context。
  - 浪費 token（每 turn 多 ~5KB schema）
  - LLM 容易挑錯工具（同名類似的 tool 太多）
  - 同一個 chat session 一下查資料一下刪檔，安全 surface 過大
  - 燒錢 / 慢

解法：先 classify intent → narrow tool catalog → 餵給 main LLM。
這樣 「幫我寄信給客戶 A」只會看到 send_gmail / search_gmail / 等 ~10 個
tool 而不是 292 個。LLM 思考更專注、context 更小、誤觸 dangerous tool 機率
也下降。

8 個 intent（從 大王 提案）：
  query_data         查資料 — RAG / search / read / list
  write_email        寫信 — gmail send / draft / reply
  schedule_meeting   安排會議 — calendar create / list / suggest
  lookup_customer    查客戶 — customer_360 / quote / PO timeline
  operate_computer   操作電腦 — click / type / shell / browser
  run_workflow       執行 workflow — ERP / scheduled / queue
  manage_memory      管理記憶 — recall / save / task_memory / behavior
  system_maintenance 系統維護 — dashboard / health / cost / logs
+ 後備 unknown / chat（純閒聊或無法分類）

Classification 策略（兩段式）：
  1. Heuristic（regex / keyword）— fast、deterministic、~70% 流量被它接住
     高 confidence (>0.85) 直接 dispatch
  2. LLM fallback（Gemini Flash 小 prompt）— 只在 heuristic 低 confidence 時
     才呼叫，省 API 額度

Tool bucket per intent：
  每個 intent 有「相關 tool 名稱集合」。
  filter_tools_by_intent(tools_list, intent) 回 tools_list 的 subset，
  caller（daemon_telegram）拿這個 subset 進 LLM。

整合點（caller 該怎麼用）：
  # 在 daemon_telegram.tg_handle_message：
  intent = classify_intent(user_text).intent
  filtered = filter_tools_by_intent(tools_list, intent)
  # 把 filtered 餵 Gemini chat session

  目前未強制接（避免一次破壞性變更），但 env `RED_INTENT_ROUTING=1` 啟用後
  daemon_telegram 會走這條路。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger


# ────────────────────────────────────────────────────────────────────
# Intent constants
# ────────────────────────────────────────────────────────────────────
INTENT_QUERY_DATA = "query_data"
INTENT_WRITE_EMAIL = "write_email"
INTENT_SCHEDULE_MEETING = "schedule_meeting"
INTENT_LOOKUP_CUSTOMER = "lookup_customer"
INTENT_OPERATE_COMPUTER = "operate_computer"
INTENT_RUN_WORKFLOW = "run_workflow"
INTENT_MANAGE_MEMORY = "manage_memory"
INTENT_SYSTEM_MAINTENANCE = "system_maintenance"
INTENT_MEDIA_PROCESSING = "media_processing"
INTENT_CHAT = "chat"
INTENT_UNKNOWN = "unknown"

_ALL_INTENTS = (
    INTENT_QUERY_DATA, INTENT_WRITE_EMAIL, INTENT_SCHEDULE_MEETING,
    INTENT_LOOKUP_CUSTOMER, INTENT_OPERATE_COMPUTER, INTENT_RUN_WORKFLOW,
    INTENT_MANAGE_MEMORY, INTENT_SYSTEM_MAINTENANCE,
    INTENT_MEDIA_PROCESSING,
    INTENT_CHAT, INTENT_UNKNOWN,
)

_INTENT_DESC = {
    INTENT_QUERY_DATA:        "查資料 — RAG / search / read / list",
    INTENT_WRITE_EMAIL:       "寫信 — gmail send / reply / draft",
    INTENT_SCHEDULE_MEETING:  "安排會議 — calendar create / suggest_time",
    INTENT_LOOKUP_CUSTOMER:   "查客戶 — customer_360 / quote / PO",
    INTENT_OPERATE_COMPUTER:  "操作電腦 — click / type / shell / browser",
    INTENT_RUN_WORKFLOW:      "執行 workflow — ERP / scheduled / queue",
    INTENT_MANAGE_MEMORY:     "管理記憶 — recall / save / task_memory",
    INTENT_SYSTEM_MAINTENANCE:"系統維護 — dashboard / health / cost",
    INTENT_MEDIA_PROCESSING:  "媒體處理 — download / pitch_shift / 降調 / 轉檔",
    INTENT_CHAT:              "閒聊（無 tool 需要）",
    INTENT_UNKNOWN:           "無法分類 — fallback 到全工具",
}


# ────────────────────────────────────────────────────────────────────
# IntentResult dataclass
# ────────────────────────────────────────────────────────────────────
@dataclass
class IntentResult:
    intent: str
    confidence: float        # 0.0 ~ 1.0
    method: str              # 'heuristic' / 'llm' / 'fallback'
    matched_keywords: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 2),
            "method": self.method,
            "matched_keywords": self.matched_keywords,
            "reason": self.reason,
        }


# ────────────────────────────────────────────────────────────────────
# Heuristic rules — pattern + 配重 score
# ────────────────────────────────────────────────────────────────────
# 每條 rule 是 (compiled_regex, intent, weight)。weight 越高越權威。
# 一個訊息可能多個 rule 中，sum weights → 取最高 intent。
_RULES: list[tuple[re.Pattern, str, float]] = [
    # ── write_email（高權威，因為動作關鍵字明確）──
    (re.compile(r"(寄信|寄出|寄給|發信|回信|回覆.*信|寫信|草擬.*信|草稿)"),
     INTENT_WRITE_EMAIL, 0.9),
    (re.compile(r"\b(send\s*(an\s*)?email|reply\s*to|draft\s*(an\s*)?email|compose)\b", re.I),
     INTENT_WRITE_EMAIL, 0.9),
    (re.compile(r"通知.{0,8}(供應商|客戶|大王|對方)"), INTENT_WRITE_EMAIL, 0.7),

    # ── schedule_meeting ──
    (re.compile(r"(會議|行事曆|預約)"), INTENT_SCHEDULE_MEETING, 0.85),
    (re.compile(r"(安排|約).{0,30}(會|開會|meeting|time)"),
     INTENT_SCHEDULE_MEETING, 0.85),
    (re.compile(r"\b(schedule|book|calendar|meeting|set\s*up\s*a\s*call)\b", re.I),
     INTENT_SCHEDULE_MEETING, 0.85),
    (re.compile(r"建.{0,3}(行事曆|事件|event)"), INTENT_SCHEDULE_MEETING, 0.85),
    (re.compile(r"\d{1,2}\s*點.{0,5}(會|開會|跟|與)"), INTENT_SCHEDULE_MEETING, 0.7),

    # ── lookup_customer ──
    (re.compile(r"(客戶|供應商|報價|PO|採購單|訂單|工廠).{0,15}(資料|歷史|清單|紀錄)?"),
     INTENT_LOOKUP_CUSTOMER, 0.7),
    (re.compile(r"\b(customer|supplier|quote|PO|purchase\s*order)\b", re.I),
     INTENT_LOOKUP_CUSTOMER, 0.7),
    (re.compile(r"(這個|那個|某).{0,5}客戶"), INTENT_LOOKUP_CUSTOMER, 0.85),

    # ── operate_computer ──
    (re.compile(r"(點擊|點開|點一下|按一下|打字|輸入|開啟.{0,3}(程式|app|應用|網頁)|關閉.{0,3}視窗|執行.{0,5}指令|跑.{0,3}(指令|shell|命令))"),
     INTENT_OPERATE_COMPUTER, 0.9),
    (re.compile(r"\b(click|type|press|run\s*shell|execute|open\s*chrome|browser)\b", re.I),
     INTENT_OPERATE_COMPUTER, 0.85),
    (re.compile(r"(終端機|terminal|命令列|command\s*line)"), INTENT_OPERATE_COMPUTER, 0.9),
    # 「點開 chrome」/ 「開 safari」 — app names
    (re.compile(r"(點開|打開|開啟|開).{0,3}(chrome|safari|firefox|finder|terminal|excel|word)", re.I),
     INTENT_OPERATE_COMPUTER, 0.9),

    # ── run_workflow ──
    (re.compile(r"(跑.{0,3}流程|執行.{0,3}workflow|啟動.{0,5}排程|觸發.{0,5}任務)"),
     INTENT_RUN_WORKFLOW, 0.9),
    (re.compile(r"\b(workflow|run\s*pipeline|execute\s*workflow|trigger\s*task)\b", re.I),
     INTENT_RUN_WORKFLOW, 0.9),
    (re.compile(r"(批次|處理.{0,5}發票|月結|每月)"), INTENT_RUN_WORKFLOW, 0.7),

    # ── manage_memory ──
    # 「提醒我」/ remind = 強訊號（避免被 lookup_customer 的 PO/客戶 score 蓋過 —
    # 「提醒我...回 PO」整句意圖是 manage memory 不是 lookup customer）
    (re.compile(r"(提醒我|別忘了|不要忘|don'?t\s*forget|remind\s*me)", re.I),
     INTENT_MANAGE_MEMORY, 1.6),
    (re.compile(r"(提醒.{0,3}(明天|後天|下週|\d{1,2}點|早上|下午))"),
     INTENT_MANAGE_MEMORY, 1.0),
    (re.compile(r"(記住|加.{0,3}待辦|加.{0,3}task|我.{0,5}答應|承諾|交代)"),
     INTENT_MANAGE_MEMORY, 0.9),
    (re.compile(r"\b(remember\s*that|add\s*todo|note\s*to\s*self)\b", re.I),
     INTENT_MANAGE_MEMORY, 1.0),
    (re.compile(r"(完成.{0,5}(那個|這個).{0,3}task|任務.{0,3}做完|改.{0,3}status)"),
     INTENT_MANAGE_MEMORY, 0.85),
    (re.compile(r"(忘記.{0,3}剛才|刪.{0,3}記憶)"), INTENT_MANAGE_MEMORY, 0.85),

    # ── system_maintenance ──
    (re.compile(r"(系統.{0,3}(狀態|健康|狀況)|目前.{0,3}狀況|dashboard|控制台|儀表板)"),
     INTENT_SYSTEM_MAINTENANCE, 0.95),
    (re.compile(r"\b(system\s*status|health\s*check|cost|usage|dashboard|metrics)\b", re.I),
     INTENT_SYSTEM_MAINTENANCE, 0.9),
    (re.compile(r"(成本|花了多少|API.{0,3}費用|今日.{0,3}消耗)"), INTENT_SYSTEM_MAINTENANCE, 0.85),
    (re.compile(r"(daemon|launchctl|背景.{0,3}(任務|程式)|排程)"), INTENT_SYSTEM_MAINTENANCE, 0.85),
    (re.compile(r"(錯誤.{0,3}log|最近.{0,3}失敗|error.{0,3}log)"), INTENT_SYSTEM_MAINTENANCE, 0.85),

    # ── query_data（最廣，較弱權重避免吃掉其他）──
    (re.compile(r"(查.{0,3}(看|一下|資料|信)|找.{0,5}(信|資料|文件|規格))"),
     INTENT_QUERY_DATA, 0.7),
    (re.compile(r"\b(search|find|look\s*up|show\s*me|what\s*is|tell\s*me\s*about)\b", re.I),
     INTENT_QUERY_DATA, 0.6),
    (re.compile(r"(信箱.{0,3}(摘要|未讀|新信)|今天.{0,3}(信|郵件))"), INTENT_QUERY_DATA, 0.85),
    (re.compile(r"(摘要|統計|清單|list|recall)"), INTENT_QUERY_DATA, 0.55),

    # ── media_processing — 音／影片下載 + 後處理 ──
    # High weight on 「降/升調」 verbs because they have no overlap with any
    # other intent and were previously stranded in `rest` (agent looped on
    # download instead of finding adjust_audio_pitch — see commit log).
    (re.compile(r"(降.{0,2}(全|半)音|降.{0,2}\d+\s*(個)?(全|半)?音|升.{0,2}(全|半)?音|升.{0,2}調|降.{0,2}調|轉.{0,2}調|變.{0,2}調|移.{0,2}調)"),
     INTENT_MEDIA_PROCESSING, 1.4),
    (re.compile(r"\b(pitch\s*shift|transpose|semitone|半音|whole\s*tone|全音)\b", re.I),
     INTENT_MEDIA_PROCESSING, 1.3),
    # Download verbs paired with media URLs/words. Lower weight than the
    # transpose rule so a mixed message like 「下載 + 降全音」 still picks
    # media_processing (pitch_shift's 1.4 outweighs download's 1.0).
    (re.compile(r"(下載.{0,15}(影片|音樂|歌|MV|video|audio|mp[34]|m4a|wav|flac)|"
                r"抓.{0,15}(影片|音樂|歌|video|audio))", re.I),
     INTENT_MEDIA_PROCESSING, 1.0),
    (re.compile(r"\b(download.{0,15}(youtube|tiktok|instagram|facebook|video|audio|song|music))\b", re.I),
     INTENT_MEDIA_PROCESSING, 1.0),

    # ── chat（無動作）──
    (re.compile(r"^(你好|嗨|hi|hello|早安|午安|晚安|哈囉)\W*$", re.I),
     INTENT_CHAT, 0.95),
    (re.compile(r"^(謝謝|thanks?|thank\s*you|辛苦了|麻煩了)\W*$", re.I),
     INTENT_CHAT, 0.9),
]


def classify_heuristic(text: str) -> IntentResult:
    """純 regex 分類，不打 API。"""
    if not text or not isinstance(text, str):
        return IntentResult(INTENT_UNKNOWN, 0.0, "heuristic",
                             reason="empty input")
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for pat, intent, w in _RULES:
        m = pat.search(text)
        if m:
            scores[intent] = scores.get(intent, 0.0) + w
            matched.setdefault(intent, []).append(m.group(0)[:30])
    if not scores:
        return IntentResult(INTENT_UNKNOWN, 0.0, "heuristic",
                             reason="no rule matched")
    # 取最高分
    intent, score = max(scores.items(), key=lambda kv: kv[1])
    # 將 raw score normalize 為 0-1（單一 rule 上限約 1.0；多 rule 加總會高）
    # 用 tanh-style 平滑：confidence = score / (score + 0.5)
    confidence = score / (score + 0.5)
    return IntentResult(intent, round(confidence, 2), "heuristic",
                         matched_keywords=matched.get(intent, [])[:3],
                         reason=f"raw_score={score:.2f}")


def classify_via_llm(text: str) -> IntentResult:
    """Gemini Flash 小 prompt 分類。heuristic 低 confidence 時才呼叫。

    失敗就回 UNKNOWN（不擋 caller）。
    """
    try:
        from agent_core.gemini_client import _gemini_generate
    except Exception as e:
        return IntentResult(INTENT_UNKNOWN, 0.0, "llm",
                             reason=f"gemini import failed: {e}")
    prompt = (
        "Classify this user message into ONE intent. Respond with JSON only.\n\n"
        f"User: {text[:500]}\n\n"
        "Choose ONE of: " + ", ".join(_ALL_INTENTS) + "\n"
        "Format: {\"intent\": \"...\", \"confidence\": 0.0-1.0, \"reason\": \"<short>\"}\n"
        "Examples:\n"
        '  「幫我寄信給客戶 A 確認交期」 → {"intent":"write_email","confidence":0.95}\n'
        '  「今天信箱有什麼重要的」 → {"intent":"query_data","confidence":0.9}\n'
        '  「執行月結 workflow」 → {"intent":"run_workflow","confidence":0.95}\n'
        '  「現在系統還好嗎」 → {"intent":"system_maintenance","confidence":0.85}\n'
        '  「下載這首歌，然後降一個全音」 → {"intent":"media_processing","confidence":0.95}\n'
        '  「幫我把這段 mp3 升半音」 → {"intent":"media_processing","confidence":0.95}\n'
        '  「下載 YouTube 影片」 → {"intent":"media_processing","confidence":0.9}\n'
    )
    try:
        # 用 flash model（便宜快），預設 model 太貴
        resp = _gemini_generate(model="gemini-2.0-flash", contents=[prompt])
        text_out = resp.text if hasattr(resp, "text") else str(resp)
        # 找 JSON
        m = re.search(r"\{[^{}]*\}", text_out)
        if not m:
            return IntentResult(INTENT_UNKNOWN, 0.0, "llm",
                                 reason=f"no json in response: {text_out[:80]}")
        data = json.loads(m.group(0))
        intent = data.get("intent", INTENT_UNKNOWN)
        if intent not in _ALL_INTENTS:
            intent = INTENT_UNKNOWN
        return IntentResult(
            intent=intent,
            confidence=float(data.get("confidence", 0.5)),
            method="llm",
            reason=str(data.get("reason", ""))[:120],
        )
    except Exception as e:
        return IntentResult(INTENT_UNKNOWN, 0.0, "llm",
                             reason=f"llm error: {type(e).__name__}: {str(e)[:60]}")


_HIGH_CONFIDENCE = 0.85


def classify(text: str, *, allow_llm: bool = True) -> IntentResult:
    """主分類 entry：先 heuristic，confidence 不夠才 LLM。

    Args:
        text: user message
        allow_llm: False 時即使 heuristic 不夠強也不打 API（給離線測試 / 省錢）
    """
    h = classify_heuristic(text)
    if h.confidence >= _HIGH_CONFIDENCE:
        _log_classification(text, h)
        return h
    if not allow_llm or h.confidence > 0.6:
        _log_classification(text, h)
        return h
    # LLM fallback
    llm = classify_via_llm(text)
    # 如果 LLM 也不太確定，回 heuristic 結果（heuristic 至少有 keyword 證據）
    if llm.confidence < h.confidence and h.confidence > 0:
        h.reason = (h.reason + "; llm: " + llm.reason)[:200]
        _log_classification(text, h)
        return h
    _log_classification(text, llm)
    return llm


# ────────────────────────────────────────────────────────────────────
# Tool buckets per intent — 每個 intent 對應「相關 tool 名稱」
# 重疊 OK（一個 tool 可在多個 bucket）；目標是「相關優先」非嚴格分區
# ────────────────────────────────────────────────────────────────────
_TOOL_BUCKETS: dict[str, frozenset[str]] = {
    INTENT_QUERY_DATA: frozenset({
        "recall", "recall_reranked", "multihop_query",
        "search_gmail", "read_gmail", "summarize_inbox",
        "query_email_lake", "email_lake_stats",
        "query_bom", "query_quote_history",
        "query_po_timeline", "query_customer_timeline", "list_customer_pos",
        "customer_360", "list_active_customers", "customer_alerts",
        "list_tracked_samples", "check_sample_deadlines", "check_stale_pos",
        "check_overdue_promises",
        "list_calendar_events", "list_specs", "compare_specs", "parse_spec_sheet",
        "search_drive_files", "search_drive_docs", "read_drive_file",
        "read_production_progress_sheet",
        "search_google_chat",
        "rag_coverage_report", "rag_gap_report",
        "show_log_tail", "list_recent_files",
        "read_website_content", "web_access_diagnose", "web_access_diagnose_json",
        "web_domain_policy_list", "search_the_web", "mcp_fetch_fetch",
        "list_tasks", "task_detail", "find_tasks_by",
        "tasks_due_today", "tasks_overdue", "tasks_for_email",
        "list_workflows", "list_erp_workflows", "show_erp_workflow",
        "list_workflow_runs", "show_workflow_run",
        "list_runs", "show_run",
        "analyze_email_reply_times",
        "tw_company_lookup", "tw_postal_code", "bot_exchange_rates",
        "einvoice_winning_numbers", "tw_stock_quote", "aqi_now",
        "list_drafts", "list_labels", "search_threads", "get_thread",
        "get_youtube_transcript",
    }),
    INTENT_WRITE_EMAIL: frozenset({
        "search_gmail", "read_gmail", "summarize_inbox",
        "send_gmail", "reply_gmail", "create_draft", "create_label",
        "send_briefing_email", "briefing_preview",
        "download_gmail_attachment",
        "analyze_email_reply_times",
        "recall",  # 寫信常需要查歷史 context
    }),
    INTENT_SCHEDULE_MEETING: frozenset({
        "list_calendar_events", "create_calendar_event",
        "delete_calendar_event", "update_calendar_event",
        "respond_to_event", "suggest_time",
        "create_event", "delete_event", "get_event", "update_event",
        "list_calendars", "list_events",
        "briefing_next_meeting", "meeting_briefing",
    }),
    INTENT_LOOKUP_CUSTOMER: frozenset({
        "customer_360", "list_active_customers", "customer_alerts",
        "query_po_timeline", "query_customer_timeline", "list_customer_pos",
        "query_bom", "query_quote_history", "query_email_lake",
        "list_tracked_samples", "check_sample_deadlines", "check_stale_pos",
        "check_overdue_promises",
        "multihop_query", "search_drive_docs", "read_drive_file",
        "read_production_progress_sheet",
        "resolve_entity", "list_entity_aliases", "build_alias_table",
        "entity_stats",
        "tw_company_lookup",
        "search_gmail", "summarize_inbox",  # 查客戶常順便查 email
        "recall",
        "generate_quote", "list_specs",
    }),
    INTENT_OPERATE_COMPUTER: frozenset({
        "click_screen", "type_text", "press_keys", "scroll_screen",
        "open_application", "close_application", "open_url",
        "control_mac_system", "set_system_volume", "show_notification",
        "read_mac_clipboard",
        "ax_click", "ax_type_in",
        "browser_open", "browser_close", "browser_status",
        "browser_read", "browser_click", "browser_fill", "browser_type",
        "browser_press", "browser_wait_for", "browser_extract",
        "browser_screenshot", "browser_scroll", "browser_eval",
        "browser_new_tab",
        "run_shell", "run_python_code",
        "manage_files", "read_file", "write_file",
        "analyze_screen",
    }),
    INTENT_RUN_WORKFLOW: frozenset({
        "list_workflows", "show_workflow_run", "list_workflow_runs",
        "process_monthly_invoices", "demo_flaky_workflow",
        "list_erp_workflows", "show_erp_workflow", "delete_erp_workflow",
        "merge_erp_workflows",
        "learn_erp_from_video", "learn_erp_from_drive_folder",
        "add_scheduled_task", "remove_scheduled_task",
        "run_scheduled_task_now", "list_scheduled_tasks",
        "submit_task", "cancel_task", "task_status", "list_queue_tasks",
        "dead_letter_status", "requeue_dead_letter",
        "batch_extract_quotes_from_parquet",
        "qc_inspect", "qc_batch_inspect",
    }),
    INTENT_MANAGE_MEMORY: frozenset({
        "recall", "save_memory", "remember", "forget_memory",
        "learn_behavior", "forget_behavior",
        "correct_mistake", "delete_correction",
        "add_task", "update_task_status", "complete_task",
        "link_to_email", "link_to_calendar", "set_task_reminder",
        "delete_task", "list_tasks", "task_detail", "find_tasks_by",
        "tasks_due_today", "tasks_overdue", "tasks_for_email",
        "set_qc_master", "list_qc_masters",
        "track_sample", "update_sample_status", "close_sample",
        "delete_tracked_sample",
    }),
    INTENT_SYSTEM_MAINTENANCE: frozenset({
        "system_status", "system_alerts", "open_dashboard_in_browser",
        "correlate_alert",
        "list_tools_by_tier", "tool_budget_status", "reset_budget",
        "cost_today", "cost_last_7_days", "cost_by_tool", "cost_by_key",
        "cost_alert", "cost_stats",
        "health_check", "show_log_tail",
        "list_runs", "show_run",
        "list_scheduled_tasks", "remove_scheduled_task",
        "dead_letter_status",
        "enable_dry_run_mode", "disable_dry_run_mode",
        "dry_run_status", "list_dry_run_log", "last_dry_run_log",
        "reload_skills", "list_skills",
        "classify_intent", "tools_for_intent", "intent_recent",
    }),
    INTENT_MEDIA_PROCESSING: frozenset({
        # Downloaders (also in operate_computer historically; promoted here
        # so 「下載 + 降全音」 ranks them above shell/click)
        "download_online_video", "download_youtube_audio", "download_youtube_video",
        "download_hls_with_ytdlp", "download_hls_with_ffmpeg_copy",
        "download_hls_with_n_m3u8dl",
        "package_hls_aes128",
        # Pitch / transpose — THE fix for the original loop
        "adjust_audio_pitch",
        # Telegram delivery — agent often needs to send the processed file
        # back; without this it falls back to filesystem write which the
        # daemon's post-agent scan picks up, but listing here is cleaner
        "telegram_send_file",
        # Filesystem helpers for naming output paths
        "read_file", "write_file",
    }),
    # CHAT / UNKNOWN：給最小工具集（避免 LLM 突然要做事但沒工具）
    INTENT_CHAT: frozenset({
        "system_status", "recall",
    }),
}


# UNKNOWN 沒 bucket → caller 取「全 tool」當 fallback（最寬容）


def tools_for_intent(intent: str) -> str:
    """🟢 列出某 intent 的相關工具名稱（給 LLM / 大王 introspect）。"""
    if intent not in _TOOL_BUCKETS:
        if intent == INTENT_UNKNOWN:
            return f"  ❓ intent={intent} — 不縮限工具，回全集"
        return f"  ❌ 未知 intent: {intent}（可選：{', '.join(_ALL_INTENTS)}）"
    bucket = sorted(_TOOL_BUCKETS[intent])
    out = [f"📋 intent={intent}  ({_INTENT_DESC.get(intent, '')})",
           f"   相關 tool 共 {len(bucket)} 個："]
    for n in bucket:
        out.append(f"     {n}")
    return "\n".join(out)


def filter_tools_by_intent(tools_list: list, intent: str) -> list:
    """🟢 把整份 tools_list 篩成 intent 相關的 subset。

    Args:
        tools_list: 原 tools_list（list of callables）
        intent: 9 個 intent 之一；UNKNOWN 不篩

    Returns:
        新 list — intent 相關 tool 排前，其餘照原順序保留在後面
                （safety net：避免 narrow 太多誤殺重要 tool）
    """
    if intent == INTENT_UNKNOWN or intent not in _TOOL_BUCKETS:
        return list(tools_list)
    bucket = _TOOL_BUCKETS[intent]
    relevant: list = []
    rest: list = []
    for fn in tools_list:
        name = getattr(fn, "__name__", "")
        if name in bucket:
            relevant.append(fn)
        else:
            rest.append(fn)
    # CHAT intent 真的縮 — 只回相關（沒 fallback 到 rest）
    if intent == INTENT_CHAT:
        return relevant
    return relevant + rest


# ────────────────────────────────────────────────────────────────────
# Classification log（給 dashboard 用）
# ────────────────────────────────────────────────────────────────────
_LOG_FILE = os.path.join(STATE_DIR, "intent_log.jsonl")
_LOG_LOCK = threading.Lock()
_LOG_MAX_LINES = 1000
# rotation 用便宜的 os.stat() size 閘門（比照 policy_engine._LOG_ROTATE_BYTES）：
# 舊版每則訊息 append 後都 readlines() 全檔數行數，多行程共用檔上白掃。
# entry 一行 ~150-200 bytes，256KB ≈ 1300+ 行，早超過 _LOG_MAX_LINES。
_LOG_ROTATE_BYTES = 256 * 1024
_PG_INTENT_WARNING_UNTIL = 0.0


def _warn_pg_intent_fallback(exc: Exception) -> None:
    global _PG_INTENT_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_INTENT_WARNING_UNTIL:
        return
    _PG_INTENT_WARNING_UNTIL = now + 30
    logger.warning("Postgres intent_router failed; falling back to JSONL: %s", exc)


def _pg_intent_store():
    try:
        from agent_core import operational_intent_router as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - intent logging should stay best-effort
        _warn_pg_intent_fallback(exc)
    return None


def _log_classification(text: str, result: IntentResult) -> None:
    """寫一筆分類紀錄（給 dashboard 算 distribution）。失敗 silent。"""
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "intent": result.intent,
        "confidence": round(result.confidence, 2),
        "method": result.method,
        "text_preview": text[:60] if text else "",
    }
    store = _pg_intent_store()
    if store is not None:
        try:
            store.write_classification(entry)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to local classification log
            _warn_pg_intent_fallback(exc)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _LOG_LOCK:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # rotate：先 os.stat 過 byte 門檻才 readlines（policy_engine 同款
            # 修法）— 每則訊息全檔 readlines 在共用檔上是 O(檔案大小) 白掃。
            try:
                if os.stat(_LOG_FILE).st_size > _LOG_ROTATE_BYTES:
                    with open(_LOG_FILE, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    if len(lines) > _LOG_MAX_LINES:
                        _atomic_write_text(_LOG_FILE,
                                           "".join(lines[-_LOG_MAX_LINES:]))
            except Exception:
                pass
    except Exception:
        pass


def intent_recent(hours: int = 24, limit: int = 20) -> str:
    """🟢 看最近 N 小時的 intent 分類紀錄 + 分布統計。"""
    records = _load_intent_records(hours=hours, limit=50000)
    if records is not None:
        if not records:
            return f"  （過去 {hours}h 沒有分類過）"
        return _format_intent_recent(records, hours=hours, limit=limit)
    if not os.path.isfile(_LOG_FILE):
        return "  （尚無 intent_log.jsonl — 還沒分類過任何訊息）"
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"  ❌ 讀 intent_log.jsonl 失敗：{e}"
    rows: list[dict] = []
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if (r.get("at") or "") < cutoff:
            break
        rows.append(r)
    if not rows:
        return f"  （過去 {hours}h 沒有分類過）"
    return _format_intent_recent(rows, hours=hours, limit=limit)


def _load_intent_records(hours: int = 24, limit: int = 50000) -> list[dict] | None:
    store = _pg_intent_store()
    if store is None:
        return None
    try:
        return store.load_classifications(hours=hours, limit=limit)
    except Exception as exc:  # noqa: BLE001 - fall back to local classification log
        _warn_pg_intent_fallback(exc)
        return None


def _format_intent_recent(rows: list[dict], *, hours: int, limit: int) -> str:
    counts: dict[str, int] = {}
    methods: dict[str, int] = {}
    for r in rows:
        counts[r.get("intent", "?")] = counts.get(r.get("intent", "?"), 0) + 1
        methods[r.get("method", "?")] = methods.get(r.get("method", "?"), 0) + 1
    out = [f"🔍 過去 {hours}h intent 分類 — 共 {len(rows)} 次"]
    out.append("─" * 60)
    out.append("  分布：")
    for intent, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        pct = n * 100 // len(rows)
        out.append(f"    {intent:25s} {n:4d} ({pct:2d}%)")
    out.append("")
    out.append("  分類方式：" + ", ".join(f"{m}={n}" for m, n in methods.items()))
    out.append("")
    out.append(f"  最近 {min(limit, len(rows))} 筆：")
    for r in rows[:limit]:
        out.append(f"    [{r.get('at', '')[:16]}] {r.get('intent', '?'):20s} "
                   f"({r.get('confidence', 0):.2f}, {r.get('method', '?')})  "
                   f"{r.get('text_preview', '')[:40]}")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Public tool — LLM 可用來自我 introspect
# ────────────────────────────────────────────────────────────────────
def intent_routing_status() -> str:
    """🟢 看 intent routing 是否已啟用 + 最近分類分布。

    顯示：
      - env RED_INTENT_ROUTING 啟用狀態
      - 過去 24h 分類分布
      - 命中（confidence ≥ 0.7）/ 漏判（fallback 全集）比例
    """
    enabled = os.environ.get("RED_INTENT_ROUTING") == "1"
    out = ["🛣️  Intent Routing 狀態"]
    out.append("─" * 60)
    if enabled:
        out.append("  ✅ 已啟用（RED_INTENT_ROUTING=1）— Telegram 訊息會 narrow tool catalog")
    else:
        out.append("  ⚠️ 未啟用 — Telegram 仍使用全 300+ tool catalog")
        out.append("     啟用：export RED_INTENT_ROUTING=1，重啟 telegram daemon")
    out.append("")

    rows = _load_intent_records(hours=24, limit=50000)
    s = _summary_from_intent_records(rows) if rows is not None else intent_summary(hours=24)
    total = s.get("total", 0)
    if total == 0:
        out.append("  （過去 24h 沒有分類紀錄）")
        return "\n".join(out)
    out.append(f"  過去 24h 分類 {total} 次：")
    by_intent = s.get("by_intent", {})
    high_conf = 0
    routable = 0
    if rows is None and not os.path.isfile(_LOG_FILE):
        out.append("    （無 log）")
        return "\n".join(out)
    if rows is not None:
        for r in rows:
            try:
                confidence = float(r.get("confidence", 0))
            except Exception:
                confidence = 0
            if confidence >= 0.7:
                high_conf += 1
            if r.get("intent") not in ("unknown", "chat"):
                routable += 1
    else:
        try:
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            with open(_LOG_FILE, "r", encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except (ValueError, TypeError):
                        continue
                    if (r.get("at") or "") < cutoff:
                        continue
                    if float(r.get("confidence", 0)) >= 0.7:
                        high_conf += 1
                    if r.get("intent") not in ("unknown", "chat"):
                        routable += 1
        except (ValueError, TypeError):
            pass
    routable_pct = (routable * 100 // total) if total else 0
    high_pct = (high_conf * 100 // total) if total else 0
    out.append(f"    可路由（非 chat/unknown）：{routable} ({routable_pct}%)")
    out.append(f"    confidence ≥ 0.7：{high_conf} ({high_pct}%)")
    out.append("")
    out.append("  分布：")
    for intent, n in sorted(by_intent.items(), key=lambda kv: -kv[1])[:8]:
        pct = n * 100 // total
        out.append(f"    {intent:25s} {n:4d} ({pct:2d}%)")
    methods = s.get("by_method", {})
    if methods:
        out.append("  分類方式：" + ", ".join(f"{m}={n}" for m, n in methods.items()))
    return "\n".join(out)


def classify_intent(text: str, allow_llm: bool = True) -> str:
    """🟢 把使用者訊息分類成 intent，回人類可讀文字。

    Args:
        text: 要分類的 user message
        allow_llm: True（預設）= heuristic 不夠時打 Gemini Flash 補強；
                   False = 純 regex 分類（離線、不燒 API）

    Returns:
        formatted text — 含 intent / confidence / method / matched keywords
    """
    if not text:
        return "❌ text 必填"
    r = classify(text, allow_llm=allow_llm)
    out = [
        f"🔍 intent: {r.intent}",
        f"   confidence: {r.confidence:.2f}",
        f"   method: {r.method}",
        f"   description: {_INTENT_DESC.get(r.intent, '')}",
    ]
    if r.matched_keywords:
        out.append(f"   matched: {', '.join(r.matched_keywords)}")
    if r.reason:
        out.append(f"   reason: {r.reason[:120]}")
    out.append("")
    out.append(f"   👉 用 tools_for_intent('{r.intent}') 看相關工具")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────
# Dashboard helper
# ────────────────────────────────────────────────────────────────────
def intent_summary(hours: int = 24) -> dict:
    """給 dashboard 的精簡分布。"""
    records = _load_intent_records(hours=hours, limit=50000)
    if records is not None:
        return _summary_from_intent_records(records)
    if not os.path.isfile(_LOG_FILE):
        return {"total": 0, "by_intent": {}, "by_method": {}}
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    counts: dict[str, int] = {}
    methods: dict[str, int] = {}
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except (ValueError, TypeError):
                    continue
                if (r.get("at") or "") < cutoff:
                    continue
                counts[r.get("intent", "?")] = counts.get(r.get("intent", "?"), 0) + 1
                methods[r.get("method", "?")] = methods.get(r.get("method", "?"), 0) + 1
    except (ValueError, TypeError):
        pass
    return {
        "total": sum(counts.values()),
        "by_intent": counts,
        "by_method": methods,
    }


def _summary_from_intent_records(records: list[dict]) -> dict:
    counts: dict[str, int] = {}
    methods: dict[str, int] = {}
    for r in records:
        counts[r.get("intent", "?")] = counts.get(r.get("intent", "?"), 0) + 1
        methods[r.get("method", "?")] = methods.get(r.get("method", "?"), 0) + 1
    return {
        "total": sum(counts.values()),
        "by_intent": counts,
        "by_method": methods,
    }
