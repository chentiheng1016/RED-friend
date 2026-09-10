"""Built-in tool catalog for tool_registry."""

from agent_core.accessibility import (
    ax_check_permission, ax_list_running_apps, ax_describe_app,
    ax_find_elements, ax_click, ax_type_in, ax_read_value,
)
from agent_core.apps import open_application, close_application, open_url
from agent_core.auto_skill import learn_skill_from_video, list_learned_skills
from agent_core.briefing import meeting_briefing, briefing_next_meeting
from agent_core.citation import fetch_email_by_thread_id, fetch_emails_by_thread_ids
from agent_core.rerank import recall_reranked
from agent_core.query_expansion import preview_expansion
from agent_core.time_aware import preview_time_detection
from agent_core.multihop import multihop_query, preview_multihop_plan
from agent_core.eval_rag import (
    run_eval, eval_history, list_golden_set, add_to_golden_set,
)
from agent_core.entity_resolver import (
    build_alias_table, list_entity_aliases, entity_stats,
)
from agent_core.cost_tracker import (
    cost_today, cost_last_7_days, cost_by_tool, cost_by_key, cost_alert, cost_stats,
)
from agent_core.fresh_diagnostics import system_status, system_alerts
from agent_core.dashboard_web import open_dashboard_in_browser
from agent_core.tool_tiers import list_tools_by_tier
from agent_core.tool_budgets import tool_budget_status, reset_budget
from agent_core.tool_rpc_server import tool_rpc_status
from agent_core.tool_runner import tool_runner_status
from agent_core.task_queue import (
    submit_task, cancel_task, task_status, list_queue_tasks,
    dead_letter_status, requeue_dead_letter,
)
from agent_core.task_memory import (
    add_task, update_task_status, complete_task,
    link_to_email, link_to_calendar, set_task_reminder,
    delete_task,
    list_tasks, task_detail, find_tasks_by,
    tasks_due_today, tasks_overdue, tasks_for_email,
    set_recurring_reminder, clear_recurring_reminder,
    link_last_sent_email_to_task,
)
from agent_core.intent_router import (
    classify_intent, tools_for_intent, intent_recent, intent_routing_status,
)
from agent_core.mode_manager import (
    set_work_mode, exit_work_mode,
    work_mode_status, list_work_modes, mode_history,
)
from agent_core.vision_rpa_engine import (
    fill_form, list_rpa_runs, show_rpa_run,
)
from agent_core.idp import (
    read_document, extract_document_fields,
    fill_document, auto_fill_document,
)
from agent_core.alert_pusher import alert_push_status, push_alerts_now
from agent_core.metrics import metrics_overview, tool_health
from agent_core.status_center import overview_text, correlate_alert
from agent_core.dashboard_api import dashboard_json, health_json
# Owner session 主控台（遠端控制活躍對話 session；工具 owner-only）
from agent_core.session_registry import (
    list_sessions, pause_session, resume_session, reset_session,
    broadcast_message,
)
from agent_core.risk_guard import risk_assessment
from agent_core.policy_engine import (
    evaluate_policy_text, policy_recent, env_override_status,
)
from agent_core.progress_manager import (
    update_sample_progress, get_sample_progress, get_all_samples_progress,
    get_overall_progress, generate_progress_report,
)
# ⚠️ factory_* 系列當初讀的是 var/factory_data 自存 JSON（沒接 email lake /
# Drive 真實資料），模型拿「預測工具」回答交期/庫存問題會產出無證據數字，
# 違反事實準確守則，所以從未掛進工具目錄。其中 factory_brain /
# factory_data_integration / factory_predictive_analytics / factory_quality_control
# 已無任何生產路徑引用，整組刪除。僅 factory_supply_chain 保留 —
# yellow_procurement 與 indigo_warehouse 子 agent 仍直接 import 它。
from agent_core.browser import (
    browser_open, browser_status, browser_read, browser_click, browser_fill,
    browser_type, browser_press, browser_wait_for, browser_extract,
    browser_screenshot, browser_scroll, browser_eval, browser_new_tab,
    browser_close,
)
from agent_core.web_access_guard import (
    web_access_diagnose,
    web_access_diagnose_json,
    web_domain_policy_clear,
    web_domain_policy_list,
    web_domain_policy_set,
)
from agent_core.customer_intel import customer_360, list_active_customers, customer_alerts
from agent_core.demo_recording import (
    start_demo_recording, finalize_demo_recording,
    list_recorded_demos, delete_recorded_demo,
)
from agent_core.dry_run import (
    respects_dry_run as _respects_dry_run,
    get_dry_run_describer as _get_dry_run_describer,
    enable_dry_run_mode, disable_dry_run_mode,
    dry_run_status, last_dry_run_log,
)
from agent_core.exchange_policy import exchange_policy_status
from agent_core.email_classify import classify_email, prioritized_inbox
from agent_core.email_lake import (
    analyze_email_reply_times,
    query_email_lake, email_lake_stats, email_lake_rebuild,
)
from agent_core.email_pending_tracker import list_unanswered_company_threads
from agent_core.sent_reply_tracker import (
    list_unanswered_sent_threads, sent_reply_reminder,
)
from agent_core.payment_notice import scan_payment_notices, payment_notice_alert
from agent_core.shipping_doc_tracker import (
    track_shipping_docs, shipping_doc_status,
    confirm_shipping_docs, shipping_doc_check, shipping_doc_escalation,
)
from agent_core.sample_status import read_sample_status, read_sample_bom
from agent_core.lake_dept_timeline import read_dept_email_timeline
from agent_core.email_timeline import (
    query_po_timeline, query_customer_timeline, list_customer_pos, check_stale_pos,
    check_overdue_promises,
)
from agent_core.video_understanding import watch_teaching_video
from agent_core.erp import (
    learn_erp_from_video, learn_erp_from_drive_folder,
    list_erp_workflows, show_erp_workflow, delete_erp_workflow,
    merge_erp_workflows,
)
from agent_core.factory_warehouse_stock import read_warehouse_stock
from agent_core.warehouse_mail import warehouse_mail_digest, warehouse_todo_board
from agent_core.file_ops import manage_files, read_file, write_file
from agent_core.gmail import (
    search_gmail, read_gmail, send_gmail, reply_gmail,
    download_gmail_attachment, summarize_inbox,
)
from agent_core.google_suite import (
    list_calendar_events, create_calendar_event, update_calendar_event,
    delete_calendar_event,
    search_drive_files, upload_to_drive,
)
from agent_core.factory_production_report import (
    read_production_progress_sheet, chart_production_completion,
    chart_daily_output, chart_delivery_risk, chart_station_capacity,
    read_customer_order_pos, daily_production_report,
)
from agent_core.health import health_check
from agent_core.ingest.drive_search import search_drive_docs, read_drive_file
from agent_core.ingest.chat_search import search_google_chat
from agent_core.operation_sops import search_operation_sops
from agent_core.skill_cards import (
    list_pending_skill_cards, resolve_skill_card, search_skill_cards,
)
from agent_core.rag_sync_health import rag_sync_health
from agent_core.production_schedule import (
    production_alert, query_production_schedule, production_dashboard,
    production_overdue_bom,
)
from agent_core.factory_bom import get_model_bom, check_material_readiness
from agent_core.material_arrival import query_material_arrival, material_arrival_overview
from agent_core.rag_coverage import rag_coverage_report
from agent_core.rag_gap_detector import rag_gap_report
from agent_core.input_devices import type_text, press_keys, scroll_screen, click_screen
from agent_core.logging_and_paths import show_log_tail
from agent_core.mcp_bridge import list_mcp_servers
from agent_core.media_security import (
    inspect_iso_bmff,
    parse_pssh_box,
    generate_cenc_key_material,
    build_ffmpeg_cenc_command,
    simulate_license_challenge,
    media_security_blueprint,
    package_hls_aes128,
    analyze_drm_manifest,
    download_hls_with_n_m3u8dl,
    download_hls_with_ffmpeg_copy,
    download_hls_with_ytdlp,
    assess_drm_request_safety,
    drm_chain_of_trust_model,
    build_eme_player_template,
)
from agent_core.memory import (
    save_memory, load_memory,
    remember, recall, forget_memory, memory_stats,
    learn_behavior, list_behaviors, forget_behavior,
    memory_governance_report, resolve_conflict,
    remember_correction_rule, confirm_inferred_fact,
)
from agent_core.reflection import search_reflections, revoke_reflection
from agent_core.factory_world_model import factory_now
from agent_core.mistake_ledger import (
    correct_mistake,
    list_mistakes,
    delete_correction,
    recent_factual_corrections,
)
from agent_core.qc import qc_inspect, qc_batch_inspect, set_qc_master, list_qc_masters
from agent_core.quote import extract_quote_from_email, build_quote_history, query_quote_history
from agent_core.agents.orange_sales.bom import (
    build_bom_history,
    query_bom,
    sync_bom_from_drive,
)
from agent_core.citation_guard import verify_claim
from agent_core.quote_batch import batch_extract_quotes_from_parquet
from agent_core.quote_gen import generate_quote
from agent_core.doc_export import export_report
from agent_core.doc_images import (
    extract_uploaded_excel_images, extract_uploaded_pdf_images,
)
from agent_core.uploaded_docs import read_uploaded_pdf_text, read_uploaded_table
from agent_core.chart_export import generate_chart
from agent_core.run_history import (
    audited as _audited,
    list_runs, show_run, find_past_actions, run_history_stats, prune_old_runs,
)
from agent_core.sample_tracker import (
    track_sample, list_tracked_samples, update_sample_status, close_sample,
    delete_tracked_sample, check_sample_deadlines,
)
from agent_core.scheduler import (
    add_scheduled_task, list_scheduled_tasks,
    remove_scheduled_task, run_scheduled_task_now,
)
from agent_core.shell_python_web import run_python_code, run_shell, read_website_content, search_the_web
from agent_core.specs import parse_spec_sheet, list_specs, compare_specs
from agent_core.sub_agents import delegate_to_sub_agent, delegate_to_sub_agents_parallel
from agent_core.system_ctl import (
    control_mac_system, read_mac_clipboard,
    set_system_volume, show_notification,
)
from agent_core.telegram import (
    telegram_push,
    telegram_send_file, telegram_send_photo, telegram_send_attachment,
)
from agent_core.vault import (
    list_vault_secrets, set_vault_secret, delete_vault_secret,
    vault_access_log, prune_vault_log,
)
from agent_core.vision import analyze_image, analyze_screen, analyze_uploaded_image
from agent_core.vision_ops import (
    find_on_screen_by_image, find_on_screen_by_text,
    ocr_image, ocr_screen_region, list_ocr_languages,
)
from agent_core.workflow import list_workflows, list_workflow_runs, show_workflow_run, workflow_stats
from agent_core.youtube import (
    get_youtube_transcript,
    download_youtube_audio,
    download_youtube_video,
    download_online_video,
    adjust_audio_pitch,
)

BASE_BUILTIN_TOOLS = [
    system_status,
    system_alerts,
    rag_sync_health,
    list_unanswered_company_threads,
    list_unanswered_sent_threads, sent_reply_reminder,
    scan_payment_notices, payment_notice_alert,
    # Supremo（Lurchi）出貨文件追蹤：加櫃／看狀態／業務確認／排程查核
    track_shipping_docs, shipping_doc_status,
    confirm_shipping_docs, shipping_doc_check, shipping_doc_escalation,
    tool_rpc_status, tool_runner_status,
    exchange_policy_status,
    open_dashboard_in_browser,
    list_tools_by_tier,
    tool_budget_status, reset_budget,
    submit_task, cancel_task, task_status, list_queue_tasks,
    dead_letter_status, requeue_dead_letter,
    # task memory（承諾型記憶 — 與 task_queue 不同概念）
    add_task, update_task_status, complete_task,
    link_to_email, link_to_calendar, set_task_reminder,
    delete_task, list_tasks, task_detail, find_tasks_by,
    tasks_due_today, tasks_overdue, tasks_for_email,
    # task memory 強化：recurring reminder + auto-link
    set_recurring_reminder, clear_recurring_reminder,
    link_last_sent_email_to_task,
    # intent router（先分類再 narrow tool catalog）
    classify_intent, tools_for_intent, intent_recent, intent_routing_status,
    # work mode（會議 / 業務 / 開發 — 換 persona + narrow tools）
    set_work_mode, exit_work_mode,
    work_mode_status, list_work_modes, mode_history,
    # vision RPA engine — 自主 UI 填表（OBSERVE/THINK/ACT loop）
    fill_form, list_rpa_runs, show_rpa_run,
    # IDP — Word/PDF/Excel 文件理解 + 自動填寫
    read_document, extract_document_fields,
    fill_document, auto_fill_document,
    # alert pusher — daemon 死自動推 telegram + email fallback
    alert_push_status, push_alerts_now,
    # 觀測：metrics 聚合 + status_center + dashboard JSON API
    metrics_overview, tool_health,
    overview_text, correlate_alert,
    dashboard_json, health_json,
    # 權限：risk_guard content-level + policy_engine 中央決策
    risk_assessment,
    evaluate_policy_text, policy_recent, env_override_status,
    # Owner session 主控台：綜覽並遠端控制活躍對話 session（owner-only）
    list_sessions, pause_session, resume_session, reset_session,
    broadcast_message,
    # 工廠進度管理
    update_sample_progress, get_sample_progress, get_all_samples_progress,
    get_overall_progress, generate_progress_report,
    # （工廠AI大腦 / 數據集成 / 預測分析 / 供應鏈管理 / 品質控制 的 stub 工具
    #   已移除 — 見檔頭 import 區註解；接上真實資料源前不掛回）
    media_security_blueprint,
    inspect_iso_bmff, parse_pssh_box,
    generate_cenc_key_material, build_ffmpeg_cenc_command,
    simulate_license_challenge, package_hls_aes128,
    analyze_drm_manifest, download_hls_with_n_m3u8dl,
    download_hls_with_ffmpeg_copy, download_hls_with_ytdlp,
    assess_drm_request_safety,
    drm_chain_of_trust_model, build_eme_player_template,
    get_youtube_transcript, download_youtube_audio, download_youtube_video,
    download_online_video, adjust_audio_pitch, analyze_image, analyze_screen,
    analyze_uploaded_image,
    qc_inspect, qc_batch_inspect, set_qc_master, list_qc_masters,
    parse_spec_sheet, list_specs, compare_specs,
    control_mac_system, open_application, close_application, open_url, read_mac_clipboard,
    set_system_volume, show_notification, show_log_tail,
    type_text, press_keys, scroll_screen, click_screen,
    list_calendar_events, create_calendar_event, update_calendar_event,
    delete_calendar_event,
    meeting_briefing, briefing_next_meeting,
    search_drive_files, upload_to_drive,
    search_drive_docs, read_drive_file,
    search_google_chat,
    search_operation_sops,
    # 影片知識卡：三層信度分級＋影片時間點溯源的單條知識主張；uncertain 卡
    # 走 pending 佇列人工核可（resolve_skill_card = CONFIRM 級）
    search_skill_cards, list_pending_skill_cards, resolve_skill_card,
    # 生管日報：結構化讀 Drive『X月份生產日報進度表.xlsx』(每日×各客戶日產量=生產進度)
    read_production_progress_sheet, chart_production_completion,
    chart_daily_output, chart_delivery_risk,
    # 每日生產數量回報（daily_production_8am 走 deterministic_tool 直接寄它的回傳）
    daily_production_report,
    # 針車/射出/包裝 三站每日產能折線圖（月初自動往前接上個月，湊滿天數才畫）
    chart_station_capacity,
    # 生產排程（生管型體為核心）：落後示警/查詢/看板（建在生管日報上）
    production_alert, query_production_schedule, production_dashboard,
    # 落後單 × 料表：每張落後指令一鍵帶出有沒有 BOM（生產排程 × factory_bom）
    production_overdue_bom,
    # 型體 BOM 用料表（DECATHLON CBD / JALAS RFQ，接生管型體；附供應商→追採購）
    get_model_bom,
    # 採購備料進度：型體→BOM 供應商→採購信查到貨（生產排程的採購層）
    check_material_readiness,
    # 料到貨進度（LOT 視角，互補 check_material_readiness）：查某 LOT 料到沒＋掃未到貨料批
    query_material_arrival, material_arrival_overview,
    # 業務訂單夾：客戶訂單真實量(PO)權威來源，非生管日報
    read_customer_order_pos,
    # 倉庫庫存料表（00 Stock Data 料號庫存表 — 以各料分頁結餘為權威現有量）
    read_warehouse_stock,
    # 倉庫信箱（warehouse@ / warehouse-mgr@）未讀重點 + 待辦追蹤（每日 09:00/15:00 通知用）
    warehouse_mail_digest, warehouse_todo_board,
    rag_coverage_report,
    # Drive 漏抽偵測（doc_id 級集合差：有檔無索引；補 rag_coverage 只給數量缺口的空白）
    rag_gap_report,
    # 教學影片理解 — 旁白語意×畫面動作融合分析（人可讀教學整理）
    watch_teaching_video,
    learn_erp_from_video, learn_erp_from_drive_folder,
    list_erp_workflows, show_erp_workflow, delete_erp_workflow,
    merge_erp_workflows,
    search_gmail, read_gmail, send_gmail, reply_gmail, download_gmail_attachment, summarize_inbox,
    analyze_email_reply_times, classify_email, prioritized_inbox,
    query_email_lake, email_lake_stats, email_lake_rebuild,
    # 樣品室進度：從內部 lake 樣品信抽狀態時間軸（生管日報只有量產、沒有樣品）
    read_sample_status,
    # 樣品 BOM＋進度：樣品單號 → 信件時間軸 ＋ Drive 料表（逐料，複用 factory_bom 解析器）
    read_sample_bom,
    # 通用部門郵件時間軸：倉庫/會計/採購/船務 等「信多無工具」部門
    read_dept_email_timeline,
    query_po_timeline, query_customer_timeline, list_customer_pos, check_stale_pos,
    check_overdue_promises,
    extract_quote_from_email, build_quote_history, query_quote_history,
    build_bom_history, query_bom, sync_bom_from_drive,
    verify_claim,
    batch_extract_quotes_from_parquet,
    generate_quote,
    export_report,
    extract_uploaded_pdf_images, extract_uploaded_excel_images,
    read_uploaded_table, read_uploaded_pdf_text,
    generate_chart,
    track_sample, list_tracked_samples, update_sample_status, close_sample,
    delete_tracked_sample, check_sample_deadlines,
    customer_360, list_active_customers, customer_alerts,
    manage_files, read_file, write_file,
    run_python_code, run_shell, read_website_content,
    web_access_diagnose, web_access_diagnose_json,
    web_domain_policy_list, web_domain_policy_set, web_domain_policy_clear,
    search_the_web,
    save_memory, load_memory,
    remember, recall, forget_memory, memory_stats,
    fetch_email_by_thread_id, fetch_emails_by_thread_ids,
    recall_reranked,
    preview_expansion, preview_time_detection,
    multihop_query, preview_multihop_plan,
    run_eval, eval_history, list_golden_set, add_to_golden_set,
    build_alias_table, list_entity_aliases, entity_stats,
    cost_today, cost_last_7_days, cost_by_tool, cost_by_key, cost_alert, cost_stats,
    learn_behavior, list_behaviors, forget_behavior,
    memory_governance_report, resolve_conflict,
    remember_correction_rule, confirm_inferred_fact,
    search_reflections, revoke_reflection,
    factory_now,
    add_scheduled_task, list_scheduled_tasks, remove_scheduled_task, run_scheduled_task_now,
    health_check,
    browser_open, browser_status, browser_read, browser_click, browser_fill, browser_type,
    browser_press, browser_wait_for, browser_extract, browser_screenshot, browser_scroll,
    browser_eval, browser_new_tab, browser_close,
    telegram_push,
    telegram_send_file, telegram_send_photo, telegram_send_attachment,
    correct_mistake, list_mistakes, delete_correction, recent_factual_corrections,
    list_mcp_servers,
    delegate_to_sub_agent,
    delegate_to_sub_agents_parallel,
    learn_skill_from_video, list_learned_skills,
    list_runs, show_run, find_past_actions, run_history_stats, prune_old_runs,
    ax_check_permission, ax_list_running_apps, ax_describe_app,
    ax_find_elements, ax_click, ax_type_in, ax_read_value,
    list_workflows, list_workflow_runs, show_workflow_run, workflow_stats,
    find_on_screen_by_image, find_on_screen_by_text,
    ocr_image, ocr_screen_region, list_ocr_languages,
    list_vault_secrets, set_vault_secret, delete_vault_secret,
    vault_access_log, prune_vault_log,
    enable_dry_run_mode, disable_dry_run_mode,
    dry_run_status, last_dry_run_log,
    start_demo_recording, finalize_demo_recording,
    list_recorded_demos, delete_recorded_demo,
]

_AUDITED_TOOLS = {
    "send_gmail": {"capture_screen": False},
    "reply_gmail": {"capture_screen": False},
    "create_calendar_event": {"capture_screen": False},
    "update_calendar_event": {"capture_screen": False},
    "delete_calendar_event": {"capture_screen": False},
    "upload_to_drive": {"capture_screen": False},
    "generate_image": {"capture_screen": False},
    "edit_image": {"capture_screen": False},
    "generate_quote": {"capture_screen": False},
    "excel_write": {"capture_screen": False},
    "pdf_merge": {"capture_screen": False},
    "pdf_split": {"capture_screen": False},
    "delegate_to_sub_agent": {"capture_screen": False},
    "delegate_to_sub_agents_parallel": {"capture_screen": False},
    "learn_skill_from_video": {"capture_screen": False},
    "click_screen": {"capture_screen": True},
    "type_text": {"capture_screen": True},
    "press_keys": {"capture_screen": False},
    "open_application": {"capture_screen": True},
    "close_application": {"capture_screen": True},
    "ax_click": {"capture_screen": True},
    "ax_type_in": {"capture_screen": True},
    # Round 8 L8-1：之前 audit 漏列關鍵 tool — 特別是 dry-run toggle 本身。
    # 攻擊者過 +確認 後 disable_dry_run_mode → 跑破壞動作 → 重啟 enable，
    # 若 toggle 本身沒 audit，事後 forensic 看不到 pivot 點。
    "enable_dry_run_mode": {"capture_screen": False},
    "disable_dry_run_mode": {"capture_screen": False},
    "run_shell": {"capture_screen": False},
    "run_python_code": {"capture_screen": False},
    "set_vault_secret": {"capture_screen": False},
    "delete_vault_secret": {"capture_screen": False},
    "add_scheduled_task": {"capture_screen": False},
    "remove_scheduled_task": {"capture_screen": False},
    "run_scheduled_task_now": {"capture_screen": False},
    "correct_mistake": {"capture_screen": False},
    "learn_behavior": {"capture_screen": False},
    "save_memory": {"capture_screen": False},
    "remember": {"capture_screen": False},
    "manage_files": {"capture_screen": False},
    "write_file": {"capture_screen": False},
    "set_qc_master": {"capture_screen": False},
    "forget_memory": {"capture_screen": False},
    "forget_behavior": {"capture_screen": False},
    "resolve_conflict": {"capture_screen": False},
    "remember_correction_rule": {"capture_screen": False},
    "revoke_reflection": {"capture_screen": False},
    "confirm_inferred_fact": {"capture_screen": False},
    "delete_correction": {"capture_screen": False},
    # Network egress（C8-1）
    "read_website_content": {"capture_screen": False},
    "web_access_diagnose": {"capture_screen": False},
    "web_access_diagnose_json": {"capture_screen": False},
    "web_domain_policy_set": {"capture_screen": False},
    "web_domain_policy_clear": {"capture_screen": False},
    "search_the_web": {"capture_screen": False},
    "mcp_fetch_fetch": {"capture_screen": False},
    # MCP filesystem write
    "mcp_filesystem_write_file": {"capture_screen": False},
    "mcp_filesystem_edit_file": {"capture_screen": False},
    "mcp_filesystem_move_file": {"capture_screen": False},
    "mcp_filesystem_create_directory": {"capture_screen": False},
    # Briefing skill (見 skills/briefing.py)
    "send_briefing_email": {"capture_screen": False},
    "push_briefing_telegram": {"capture_screen": False},
}


def _tool_name(tool) -> str:
    name = getattr(tool, "__name__", "")
    return name if isinstance(name, str) else ""


def build_builtin_tools(extra_tools=None):
    """Return the wrapped built-in tool list used by tool_registry."""
    tools = list(BASE_BUILTIN_TOOLS)
    if extra_tools:
        tools.extend(extra_tools)
    audited_tools = []
    for tool in tools:
        tool_name = _tool_name(tool)
        if not tool_name:
            continue
        if tool_name in _AUDITED_TOOLS:
            tool = _audited(**_AUDITED_TOOLS[tool_name])(tool)
        audited_tools.append(tool)
    tools = audited_tools
    wrapped = []
    for tool in tools:
        tool_name = _tool_name(tool)
        describe = _get_dry_run_describer(tool_name)
        if describe and not getattr(tool, "_dry_run_wrapped", False):
            tool = _respects_dry_run(describe=describe)(tool)
        wrapped.append(tool)
    tools = wrapped
    return tools
