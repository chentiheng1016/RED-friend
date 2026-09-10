"""Dispatcher helpers extracted from agent_daemon."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import threading
from datetime import datetime
from typing import Any, Callable

from agent_core.daemon_helpers import run_task_with_deadline
from agent_core.env_utils import env_int as _env_int
from agent_core.report_trail import append_source_footer, strip_source_footer


# Background dispatcher tasks legitimately run their entire automatic-function-
# calling chain (multi-tool reads + synthesis) inside ONE send_message, so they
# need a far more generous wall-clock bound than the interactive-chat default
# (_GEMINI_INFERENCE_TIMEOUT_S = 180s). Clamped 180s–1h: never shorter than the
# chat default, never unbounded, and a bad env config falls back to 600s.
_DISPATCHER_SEND_TIMEOUT_S = _env_int(
    "RED_DISPATCHER_SEND_TIMEOUT_S", 600, min_value=180, max_value=3600
)

# Per-task wall-clock ceiling for the dispatcher loop — belt-and-suspenders OVER
# the inner _send_message_with_timeout. The inner helper bounds chat.send_message
# via a thread-join, but it does NOT cover the per-task setup that runs first
# (client build / api-key fetch / chats.create), and a future code path could
# bypass it entirely — exactly the failure mode where a protection living at one
# choke point got dodged by a new dynamic path. So the dispatcher loop also wraps
# the WHOLE per-task execution in a wall-clock deadline: a wedged task is
# abandoned + marked failed (DLQ via the existing failure path) while the rest of
# the batch keeps running. Unlike the single-task crons we must NOT os._exit here
# (that would abort the other due tasks), so this uses a thread-join abandon
# (run_task_with_deadline), not the process-fatal run_with_deadline.
#
# Default = inner send timeout + 300s margin so the graceful inner timeout (cost
# tracking + abandon monitor) fires first; floored at inner+60s so a bad env
# can't invert the two bounds, capped at 2h so it stays << the multi-hour hangs
# this guards against.
_DISPATCHER_TASK_DEADLINE_S = _env_int(
    "RED_DISPATCHER_TASK_DEADLINE_S",
    _DISPATCHER_SEND_TIMEOUT_S + 300,
    min_value=_DISPATCHER_SEND_TIMEOUT_S + 60,
    max_value=7200,
)

# 網路探測的 wall-clock 上限。事故裡的 DNS 失敗是「立刻拋 Errno 8」，但解析
# 卡住不回也算網路不通（半醒的 mDNSResponder），所以探測要有自己的 deadline。
_DISPATCHER_NET_PROBE_TIMEOUT_S = _env_int(
    "RED_DISPATCHER_NET_PROBE_TIMEOUT_S", 10, min_value=2, max_value=60
)

# 兩個代表性主機＝dispatcher 通知的兩條實際出口（Telegram 推播、Gmail 寄信）。
# 任一解析成功就算網路活著 —— 探測的目的只是分辨「整條 DNS 斷了」，單一域名
# 解析失敗要讓任務照跑、走既有失敗路徑留下真錯誤。
_NET_PROBE_HOSTS = ("api.telegram.org", "gmail.googleapis.com")


def dispatcher_network_is_up() -> bool:
    """DNS 探測：_NET_PROBE_HOSTS 任一解析成功＝網路活著。

    2026-09-07 事故：闔蓋睡眠中的 DarkWake（每次只醒 ~45 秒）撞上 dispatcher
    的 catch-up 輪，Wi-Fi/DNS 還沒起來 → 四個任務同一秒拋 [Errno 8]，
    mark_dispatcher_task_failed 蓋掉 last_run_at → 每日任務整天不再重試（漏發
    一天）＋失敗告警假警報。這支給 task_dispatcher 當前置閘門：網路不通就整輪
    延後、完全不動任務狀態，下一輪（~5 分鐘後或喚醒後）網路回來自然補跑。

    解析跑在 daemon thread、主線程只等 _DISPATCHER_NET_PROBE_TIMEOUT_S：卡死
    視同斷網、放棄 worker 即可（不用 run_task_with_deadline —— 它超時會
    faulthandler.dump_traceback 全線程堆疊，長時間斷網時每 5 分鐘洗一次版）。
    """
    resolved: list[bool] = []
    done = threading.Event()

    def _resolves(host: str) -> bool:
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            return True
        except Exception:
            return False

    def _resolve_any() -> None:
        if any(_resolves(host) for host in _NET_PROBE_HOSTS):
            resolved.append(True)
        done.set()

    worker = threading.Thread(
        target=_resolve_any, name="dispatcher:net-probe", daemon=True
    )
    worker.start()
    if not done.wait(timeout=_DISPATCHER_NET_PROBE_TIMEOUT_S):
        return False
    return bool(resolved)

# ⚠️ 這份名單是 dispatcher 的**唯一**閘門：背景任務不過 policy_engine / tg_auth
# （工具也沒被 tg_auth 包裝），名字一進來就是無 +確認、不看 tier 直接可呼叫。
# 所以只放真正該給背景任務的唯讀工具，加名字前先想清楚那顆工具的 tier。
#
# 名字必須對得上真工具：比不中的字串不會報錯、只是靜默不生效，看名單的人卻會
# 以為那個能力有開（前科 f6f1994f）。tests/test_dispatcher_task_tool_reachability.py
# 有一支測試會擋住死名字，別再把不存在的名字寫進來。
_SAFE_TOOL_NAMES = {
    # Gmail（讀取）— read_gmail 是真名；query_email_lake 是乾淨的內部信箱查詢
    "search_gmail", "read_gmail",
    "query_email_lake", "email_lake_stats",
    # Drive（讀取）— 真實工具名。舊白名單寫的 list_drive_files/search_drive/
    # read_drive_text 都不存在 → 背景任務一直讀不到 Drive(只能退回 email)。
    # Google Docs/Sheets/Slides 一律走 read_drive_file 這個統一入口。
    "search_drive_files", "search_drive_docs", "read_drive_file",
    "read_production_progress_sheet",
    # 每日生產數量回報（daily_production_8am 的 deterministic_tool）。整封信＝這顆
    # 工具的回傳，run_deterministic_task 也是從 safe_tools 解析名字，沒列在這裡
    # 排程每輪都會 raise。builtin（agent_core/factory_production_report.py），
    # 拿不到 skills 那邊的 background_safe 旗標。
    "daily_production_report",
    "browser_read",
    "recall", "list_calendar_events",
    "query_quote_history", "list_specs",
    "list_mistakes", "get_youtube_transcript",
    "list_scheduled_tasks",
    "rag_sync_health",
    # ERP（唯讀查詢，erp_oracle.py 內部已用 guard_select_only 擋寫入/多語句）—
    # 生產回報/落後警示/email 追蹤任務要拿 ERP 交期/到料/交叉比對當第二資料源。
    # 只開查詢類，不開 prepare_order_card/verify_order_entry/parse_supremo_order
    # 這類建單流程專用、run_erp_readonly_sql 這種任意 SQL 面（背景任務不需要）。
    "query_erp_order", "query_erp_order_materials",
    "cross_reference_order", "reconcile_order",
    # 全公司 email 未完成事項追蹤（email_pending_tracker 排程任務用）。
    "list_unanswered_company_threads",
    # 倉庫信箱未讀重點 + 待辦追蹤（warehouse_brief_0900 / warehouse_todo_1500 用）。
    # 兩顆都是 agent_core 內建工具（不是 skills/*.py），拿不到 _is_skill 旗標。
    "warehouse_mail_digest", "warehouse_todo_board",
    # 生管日報衍生的唯讀分析（daily_production_alert 排程任務用）。這兩支是
    # builtin（agent_core/production_schedule.py），不是 skill，所以只有列在
    # 這裡才進得了背景工具集。
    "production_alert", "production_overdue_bom",
    # 倉庫庫存料表（daily_warehouse_report 的「庫存注意」段落用）。同上，
    # builtin（agent_core/factory_warehouse_stock.py）。
    "read_warehouse_stock",
    # Gemini 成本彙總（唯讀）。2026-08-12 補：在此之前 **cost 工具一個都不在
    # 背景工具集**，等於「排個每週成本報表」這種需求根本做不到 —— 而且照舊會
    # 踩靜默失效（prompt 點名 cost_by_tool，Gemini 看不到那顆工具就照退路走，
    # 任務照樣「成功」寄信、只是少一整段，見 audit_task_tool_refs 的註解）。
    # 只開 cost_by_tool 這顆彙總視圖：純讀 var/data/cost/cost.jsonl 做加總，
    # 不含金鑰、不含信件內容，分級與已在名單的 read_gmail / rag_sync_health 同級。
    # cost_by_key 刻意不開 —— 它會列出 key 指紋，背景任務沒有需要。
    "cost_by_tool",
    #
    # ── 2026-08-05 清掉的 19 個死名字（都比不中任何真工具、一直是靜默無效）──
    # 純冗餘（能力已被上面現役工具覆蓋，刪掉零損失）：
    #   read_email / search_internal_emails / list_internal_emails
    #     → read_gmail、search_gmail、query_email_lake
    #   search_internal_docs → search_drive_docs
    #   read_google_doc / read_google_sheet / read_google_slides → read_drive_file
    #   get_quote_history → query_quote_history      search_specs → list_specs
    #   query_calendar → list_calendar_events
    #   list_open_orders / get_order_status → query_erp_order 等 ERP 查詢類
    #
    # 刻意不開（真工具是 confirm 級，補進來等於給背景任務無閘的外網存取）：
    #   read_webpage / fetch_url_text → read_website_content（confirm）
    #   browser_search               → search_the_web（confirm）
    #   ※ browser_read 雖在名單且是 safe，但它讀的是「目前頁面」，背景任務沒有
    #     互動 browser session ⇒ 背景任務實際上讀不了任意網址。要開得先做權限決策。
    #
    # 想開但當初名字寫錯所以沒開成（真工具都在、都是 safe 級，等有任務需要再補）：
    #   get_customer_info → customer_360 / query_customer_timeline
    #   list_customers    → list_active_customers
    #   get_next_meeting  → briefing_next_meeting
    #   read_thread_full  → fetch_email_by_thread_id
}

_DISPATCHER_SYSTEM_INSTRUCTION = (
    "你是小紅的「背景版」，現在正在執行大王預先排定的背景任務。\n"
    # 這裡曾寫「閱讀網頁」，但背景工具集從來就沒有可用的網頁抓取工具
    # （read_website_content / search_the_web 都是 confirm 級、不在白名單）。
    # 對模型宣告一個它拿不到的能力只會誘發幻覺，照實列現有的。
    "限制：你只能用『讀取類』工具（搜尋、讀信、查 Drive 檔案、查記憶、查行事曆），\n"
    "      絕對不要嘗試寄信、打字、操控桌面或修改資料。dispatcher 會根據你最終的純文字\n"
    "      回答去寄 Gmail 給大王。\n"
    "規則：\n"
    "  1. 按大王的 prompt 執行，盡量具體、條列、精簡。\n"
    "  2. 如果這次結果跟上次一樣（例如 eBay 沒新上架），直接回覆「(無新發現)」五個字，\n"
    "     dispatcher 就會安靜、不寄信打擾大王。\n"
    "     ——但任務內容自己講明「這是每日固定報表、一律完整輸出」時，以任務內容為準，\n"
    "       照常完整輸出（收件人是靠這封信開始一天的工作，安靜等於今天沒報表）。\n"
    "  3. 有實質新資訊才回覆。回覆用繁體中文。\n"
)


def safe_tools(tools_list: list[Any]) -> list[Any]:
    """Only allow read-only tools for background dispatcher jobs.

    Two ways in, by tool kind:
      * builtin（agent_core/ 內、經 tool_registry_catalog 註冊）→ 列進
        `_SAFE_TOOL_NAMES`。名單顯式可 grep、審查時一眼看得完。
      * skill（`skills/*.py` 熱載）→ 在模組尾巴標 `background_safe = True`。
        skill 不在 agent_core 裡，寫死名單會過期，所以走自我宣告。

    以前這裡的第二條是 `_is_skill AND background_safe`，等於 builtin 標了
    `background_safe` 也**靜默無效**——而 `list_unanswered_company_threads`
    與 `find_past_actions` 兩支 builtin 都標了、作者都以為有效（前者剛好也
    在名單裡才沒出事，後者一直是死的）。條件放寬成「名單 OR background_safe」，
    讓這個 marker 名副其實；builtin 仍建議走名單。
    """
    safe = []
    for tool in tools_list:
        name = getattr(tool, "__name__", "")
        if name in _SAFE_TOOL_NAMES or getattr(tool, "background_safe", False):
            safe.append(tool)
    return safe


# ── 排程設定漂移稽核 ────────────────────────────────────────────────
# prompt 點名一顆被 safe_tools 濾掉的工具時**不會報錯**：Gemini 看不到那顆
# 工具，就照 prompt 的退路走（「工具回錯誤就略過此段」），任務照常成功、照常
# 寄信，只是少一整段內容。實際踩過 5 支（#350），齊套/交期風險兩支每日警示
# 因此上線至今一封沒發過。這裡的稽核給兩個消費端共用：
#   - tests/test_dispatcher_task_tool_reachability.py（CI + 部署機測試）
#   - agent_core/health.py（每 30 分鐘的 health_check，抓「測試沒人跑」的空窗）

# 「foo_bar(」——明確寫成呼叫形式。半形/全形括號都算（prompt 中文混寫）。
_CALL_FORM = re.compile(r"\b([a-z][a-z0-9_]{3,})\s*[（(]")
# 裸識別字（prompt 常寫「再用 search_gmail 查…」不帶括號）。
_BARE_TOKEN = re.compile(r"[a-z][a-z0-9_]{3,}")
# 任務**設定欄位名**，不是工具。prompt 常拿它們自我說明（「本任務走
# deterministic_tool（…）」），呼叫形式的偵測會誤判成「查無此工具」。
_TASK_CONFIG_KEYS = frozenset({
    "deterministic_tool", "notify_channel", "notify_agent_color", "notify_emails",
    "interval_minutes", "start_hour", "end_hour", "dedup_hashes", "next_force_run",
    "last_run_at", "last_error", "run_count", "weekdays",
})


def extract_tool_refs(prompt: str, real_tool_names: set[str]) -> set[str]:
    """抓出 prompt 裡點名的工具。

    兩路聯集：
      1. 呼叫形式 `name(` —— 一律算「意圖呼叫」，就算這名字根本沒這支工具也算
         （那正是 f6f1994f 那類「白名單寫了不存在的工具名」的鏡像 bug）。
      2. 裸識別字，且**確實是**現存工具名 —— 中文散文不會誤命中。

    扣掉 `_TASK_CONFIG_KEYS`：那些是設定欄位名，prompt 提到它們是在說明自己
    怎麼被排程的，不是要呼叫工具。
    """
    refs = set(_CALL_FORM.findall(prompt))
    refs |= {w for w in _BARE_TOKEN.findall(prompt) if w in real_tool_names}
    return refs - _TASK_CONFIG_KEYS


def audit_task_tool_refs(tasks: list[dict], tools_list: list[Any]) -> list[str]:
    """稽核排程任務點名的工具是否都進得了背景工具集，回傳人看得懂的問題清單。

    空 list = 沒問題。只看 enabled 的任務。

    兩種任務分開處理：
      - 帶 `deterministic_tool`：完全不經 LLM（run_one_dispatcher_task 開頭就轉去
        run_deterministic_task），prompt 只是寫給人看的說明 ⇒ 不掃 prompt，改查
        設定值本身。這條路徑找不到工具會 raise（記進 last_error），不是靜默，
        但錯字要等排程時間才炸、而且每天固定同一時間炸。
      - 一般任務：掃 prompt 點名的工具。
    """
    real = {getattr(t, "__name__", "") for t in tools_list}
    reachable = {getattr(t, "__name__", "") for t in safe_tools(tools_list)}
    problems: list[str] = []
    for task in tasks:
        if not task.get("enabled", True):
            continue
        name = task.get("name", "?")
        det = str(task.get("deterministic_tool") or "").strip()
        if det:
            if det not in reachable:
                problems.append(f"{name}：deterministic_tool「{det}」不在背景工具集，每輪都會失敗")
            continue
        for ref in sorted(extract_tool_refs(task.get("prompt", ""), real)):
            if ref not in real:
                problems.append(f"{name} → {ref}()：查無此工具")
            elif ref not in reachable:
                problems.append(f"{name} → {ref}()：存在但被 safe_tools 濾掉（會靜默少一段）")
    return problems


def run_one_dispatcher_task(
    task: dict,
    *,
    tools_list: list[Any],
    gemini_model: str,
    agent_client_factory: Callable[[], Any],
    agent_types_factory: Callable[[], Any],
) -> str:
    """Run one dispatcher task in an isolated Gemini chat session.

    例外：task 帶 ``deterministic_tool`` 時完全不經 Gemini，見
    run_deterministic_task。
    """
    # 開跑前清空產出檔側通道（agent_core.deliverables）：上一支任務若產了檔卻因為
    # 結果重複／空白而沒發通知，那個檔會留在累積器裡被下一支的信夾走 —— 收件人
    # 拿到不屬於自己的附件。放在 deterministic_tool 早退**之前**，兩條路都涵蓋。
    from agent_core.deliverables import drain as _drain_deliverables
    _drain_deliverables()

    tool_name = str(task.get("deterministic_tool") or "").strip()
    if tool_name:
        return run_deterministic_task(tool_name, tools_list=tools_list)

    types = agent_types_factory()
    # genai SDK 的 AFC 預設上限是 10 次 remote call，超過就**安靜地**停手回半成品。
    # 多段式報表（採購每日簡報一封信要 7 段、每段一顆工具）踩得到：一次探索性
    # 呼叫或一次重試就爆表，收到的人只會看到少了兩段、不會有任何錯誤訊息。
    # 拉到 25 與 Telegram 那條路徑（daemon_telegram）一致；真正的止血點是外層的
    # send/task deadline，不是這個計數。
    chat = agent_client_factory().chats.create(
        model=gemini_model,
        config=types.GenerateContentConfig(
            tools=safe_tools(tools_list),
            system_instruction=_DISPATCHER_SYSTEM_INSTRUCTION,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=25),
        ),
    )
    header = (
        f"[排程任務：{task['name']}]\n"
        # Inject wall-clock now so date/time-sensitive background tasks (e.g. the
        # daily production report deciding "is yesterday's report late?") can
        # reason about today/yesterday/weekday without guessing the date.
        f"[現在時間：{datetime.now().strftime('%Y-%m-%d (%A) %H:%M')}]\n"
        f"[設定間隔：{task.get('interval_minutes')} 分；時段 {task.get('start_hour')}-{task.get('end_hour')}]\n\n"
        f"任務內容：\n{task['prompt']}"
    )
    # Wrap with the same bounded-timeout helper the Telegram path uses: the
    # Gemini SDK's send_message can hang indefinitely, and launchd's
    # StartInterval will NOT spawn a new dispatcher while the old one is still
    # alive — so one wedged call would silently freeze every scheduled
    # background task. On timeout this raises (caught by the dispatch loop,
    # which marks the task failed and moves on). Use the dispatcher-specific
    # (longer) timeout so a legit multi-tool background task isn't killed at the
    # interactive-chat 180s mark, and attribute the spend to the dispatcher
    # rather than telegram_chat.
    from agent_core.daemon_telegram import _send_message_with_timeout
    resp = _send_message_with_timeout(
        chat, header, timeout_s=_DISPATCHER_SEND_TIMEOUT_S, caller="dispatcher"
    )
    # 附上這一輪實際查過的工具 —— 數字錯的時候，收到的人要分得出是「資料本來
    # 就錯」還是「模型自己編的」。足跡取自 SDK 記的 AFC history，不是模型自述。
    return append_source_footer((resp.text or "").strip(), resp)


def run_deterministic_task(tool_name: str, *, tools_list: list[Any]) -> str:
    """呼叫單一唯讀工具、把回傳原樣當成任務結果 —— 完全不經 LLM。

    給「訊息內容就是某顆工具的輸出」那種排程用（匯率推播、固定數字報表）。走
    Gemini 只是多一層可能改寫數字的風險，還多燒一次 quota；〈員工零幻覺〉在這種
    任務上最好的做法就是不要讓 LLM 碰。

    工具名一律從 safe_tools(tools_list) 解析 —— daemon_tasks.json 雖然是受保護的
    本地檔（M7-2），但「排程設定能指名任何工具」不該是這條路徑的性質：能跑的仍然
    只有背景唯讀工具集裡的那些。找不到就 raise，由 dispatch loop 記成 last_error。
    """
    for tool in safe_tools(tools_list):
        if getattr(tool, "__name__", "") == tool_name:
            return str(tool() or "").strip()
    raise ValueError(
        f"deterministic_tool「{tool_name}」不在背景唯讀工具集裡（safe_tools），"
        "無法執行。工具要嘛列進 _SAFE_TOOL_NAMES、要嘛標 background_safe = True。"
    )


def mark_dispatcher_task_failed(task: dict, err: Exception, now: datetime):
    task["last_error"] = str(err)
    task["last_run_at"] = now.isoformat(timespec="seconds")
    # A force-run (run_scheduled_task_now → next_force_run=True) is a ONE-SHOT
    # manual trigger, not retry-until-success. Clear it on failure too — mirroring
    # mark_dispatcher_task_succeeded (L139). Otherwise a deterministically-failing
    # forced task re-fires every dispatcher tick (~5min), 24h/day, ignoring the
    # interval/working-hours gates (should_run_task returns True on the flag first),
    # burning Gemini quota until someone notices（健檢 Medium）。正常 interval 排程接手。
    task.pop("next_force_run", None)


def mark_dispatcher_task_succeeded(task: dict, now: datetime):
    task["last_error"] = None
    task["last_run_at"] = now.isoformat(timespec="seconds")
    task["run_count"] = int(task.get("run_count", 0)) + 1
    task.pop("next_force_run", None)


def dispatcher_result_is_empty(result: str) -> bool:
    skip_markers = ("(無新發現)", "（無新發現）", "(無)", "（無）")
    return (not result) or any(marker in result[:20] for marker in skip_markers)


def remember_dispatcher_result(task: dict, result: str) -> bool:
    """Store result hashes and suppress recent duplicates.

    雜湊算在**拆掉資料來源足跡之後**的正文上：足跡帶工具參數、參數常含日期，
    否則「今天沒有變化」的同一份報表每天雜湊都不同，dedup 失效變成天天重寄。
    """
    result_hash = hashlib.sha1(
        strip_source_footer(result).encode("utf-8")
    ).hexdigest()[:16]
    dedup = task.setdefault("dedup_hashes", [])
    if result_hash in dedup:
        return False
    dedup.append(result_hash)
    task["dedup_hashes"] = dedup[-20:]
    return True


# 排程結果裡的附件標記 —— 由工具產生（例：purchasing_brief 的出口明細 Excel），
# LLM 只負責把整行原樣留在回覆裡。同 doc_export 的 [[TG_FILE:]] 機制（PR #341）。
_MAIL_FILE_RE = re.compile(r"^[ \t]*\[\[MAIL_FILE:([^\]\n]+)\]\][ \t]*$", re.M)


def _attachment_allow_root() -> str:
    """附件唯一允許的根目錄 = 小紅自己的匯出區。

    標記行雖然是工具產的，但它走過 LLM 的手才回到這裡 —— 一句
    「[[MAIL_FILE:/Users/…/.ssh/id_rsa]]」若照單全收就是外寄任意檔案。因此只認
    EXPORTS_DIR 底下的檔（小紅自己寫出去的產物），其餘一律丟棄並記 log。
    """
    from agent_core.logging_and_paths import EXPORTS_DIR
    return os.path.realpath(EXPORTS_DIR)


def extract_mail_attachments(result: str,
                             extra: list[str] | None = None) -> tuple[str, list[str]]:
    """抽出 [[MAIL_FILE:…]] 標記，回 (清掉標記的正文, 通過白名單的絕對路徑)。

    extra：工具直接註冊的產出檔（agent_core.deliverables 的側通道）。標記那條
    路要 LLM 把整行原樣抄進回覆才成立 —— 抄漏了信照樣寄出、只是沒附件，沒有
    例外也沒有錯誤。側通道不經 LLM 之手，兩邊取聯集當保險。

    ⚠️ 兩個來源都走**同一道白名單**（只認 EXPORTS_DIR 底下的檔）。側通道雖然
    不經 LLM，也不給它繞過檢查的特權 —— 有一條路沒被檢查就等於沒有檢查。

    被拒的路徑只記 log、不中斷通知 —— 報表正文比附件重要，附件掉了要看得見
    但不該讓整封信發不出去。
    """
    paths: list[str] = []
    root = None
    for raw in list(_MAIL_FILE_RE.findall(result or "")) + list(extra or []):
        candidate = raw.strip().strip("'\"")
        if not candidate:
            continue
        if root is None:
            try:
                root = _attachment_allow_root()
            except Exception as exc:  # noqa: BLE001
                print(f"[dispatcher] 附件白名單取得失敗（{exc}），本輪不夾附件。")
                root = ""
        real = os.path.realpath(os.path.expanduser(candidate))
        # gmail_ops.split_paths 以 [,;] 切路徑 —— 檔名含這兩個字元會被切成兩段
        # 誤成不存在的路徑，直接擋掉比夾一個壞附件好。
        if not root or not (real == root or real.startswith(root + os.sep)):
            print(f"[dispatcher] ⚠️ 附件不在匯出區、已丟棄：{candidate[:200]}")
            continue
        if any(ch in real for ch in ",;") or not os.path.isfile(real):
            print(f"[dispatcher] ⚠️ 附件不存在或檔名含分隔符、已丟棄：{candidate[:200]}")
            continue
        if real not in paths:
            paths.append(real)
    return _MAIL_FILE_RE.sub("", result or "").rstrip(), paths


def dispatcher_agent_colors(task: dict) -> list[str]:
    """這個 task 的 Telegram 推播目標色（去重、保留設定順序）。

    ``notify_agent_colors``（清單，多色）優先；沒設就退回單色的
    ``notify_agent_color``（既有欄位，byte-identical 舊行為），兩個都沒有就是 red。
    """
    raw = task.get("notify_agent_colors")
    if isinstance(raw, str):
        raw = [raw]
    colors = [str(c).strip().lower() for c in (raw or []) if str(c).strip()]
    if not colors:
        colors = [str(task.get("notify_agent_color") or "red").strip().lower()]
    seen: set[str] = set()
    return [c for c in colors if not (c in seen or seen.add(c))]


def dispatcher_email_subject(task: dict, now: datetime | None = None) -> str:
    """這封排程信的主旨。

    預設沿用舊行為 ``【任務名】`` —— 對維運面（大王自己的信箱）夠用，但收件人是同事
    時，一行英文任務名說不出這是什麼信。任務設 ``email_subject`` 就用它，可帶
    ``{date}``／``{time}`` 佔位（例：「生產管理每日簡報 {date}（早）」）。
    佔位符打錯只回原字串，不讓一封報表因為主旨格式化失敗而寄不出去。
    """
    raw = str(task.get("email_subject") or "").strip()
    if not raw:
        return f"【{task.get('name', '?')}】"
    now = now or datetime.now()
    try:
        return raw.format(date=now.strftime("%m-%d"), time=now.strftime("%H:%M"))
    except (KeyError, IndexError, ValueError):
        return raw


# send_gmail_as 不丟例外就回字串：成功是「已寄出給 x」，失敗是「❌ …」/「發信
# 失敗：…」/ scope 提示。只用來決定「Telegram 這一路掛了要不要再補一封給大王」，
# 所以認不出來時偏向「當成沒送到」——多一封重複信，比整份到帳通知悄悄消失便宜。
_SEND_FAILURE_MARKERS = ("❌", "⚠️", "發信失敗", "失敗", "錯誤", "尚未開通")


def _send_looks_failed(result: Any) -> bool:
    text = str(result or "")
    return (not text.strip()) or any(m in text for m in _SEND_FAILURE_MARKERS)


def notify_dispatcher_result(
    task: dict,
    result: str,
    *,
    notify: Callable[..., Any],
    telegram_push_agent: Callable[..., Any] | None = None,
    send_gmail_as: Callable[..., Any] | None = None,
):
    name = task.get("name", "?")
    # 附件來源取「LLM 抄回來的標記」∪「工具直接註冊的側通道」——
    # 前者會因為模型漏抄而靜默掉檔，後者不經 LLM 之手。兩邊都過同一道白名單。
    from agent_core.deliverables import drain as _drain_deliverables
    result, attachments = extract_mail_attachments(result, extra=_drain_deliverables())
    if task.get("notify_plain"):
        # 員工面的推播（部門色 bot）：不要包「任務 xxx 的最新結果」與「取消排程」
        # 那層外殼 —— 那是大王的維運介面，對收到的員工只是雜訊，還會邀請他們去下
        # 一個自己沒權限的指令。訊息本身已經是完整成品時就原樣送。
        body = result
    else:
        body = (
            f"🔔 任務「{name}」的最新結果：\n\n{result}\n\n---\n"
            f"（移除：跟小紅說「取消排程 {name}」/ 關掉：「暫停排程 {name}」）"
        )
    if attachments:
        # Telegram / owner-email 這兩條路走不了附件（notify 沒有附件面），至少把
        # 檔案位置寫進正文，別讓產好的 Excel 靜靜消失。
        body += "\n📎 附件：" + "、".join(os.path.basename(p) for p in attachments)
    # Per-task delivery channels. notify_emails 與 notify_channel=="telegram"
    # 是**可以並存**的（2026-08-12 貨款到帳通知：同一份內容要同時進紫色 bot 與
    # 台越會計的信箱）；兩個都沒設才走預設的 owner email。
    #   1. notify_emails —— 信任的本地清單（不是 LLM 輸出，跟 notify_agent_color
    #      同等級的 injection-safety 模型）。清單裡每個地址各自收到「自己寄給
    #      自己」的一封信（網域委派冒充該地址寄信，見 actor_google_tools.
    #      send_gmail_as），不是大王代寄的群發信。單一地址失敗只記 log、不影響
    #      其他地址，也不 fallback 到 owner-email（避免大王的信箱被塞進一堆
    #      「這封其實是要給別人的」通知）。
    #   2. notify_channel=="telegram" —— fans the body out to the task's
    #      agent-colour chats — resolved from trusted local config (employee
    #      registry / owner), NOT from LLM output, so it's injection-safe —
    #      and only falls back to email when Telegram didn't fully succeed,
    #      so a scheduled report is never silently lost.
    #   3. 預設 email 給 owner —— byte-identical 既有行為。
    notify_emails = [str(a).strip() for a in (task.get("notify_emails") or []) if str(a).strip()]
    wants_telegram = (task.get("notify_channel") == "telegram"
                      and telegram_push_agent is not None)
    email_delivered = False
    if notify_emails and send_gmail_as is not None:
        for addr in notify_emails:
            try:
                # markdown_html：Gemini 產的結果常帶 markdown 表格（生產日報），
                # 純文字信會把 `| :---: |` 原樣露出、欄位沒對齊 → 多帶 HTML 版。
                send_result = send_gmail_as(
                    addr, addr, dispatcher_email_subject(task), body,
                    attachments=";".join(attachments), markdown_html=True,
                    # 出處標記：這封是小紅產的排程報表，不是這位同事寫的信。
                    # 少了它，隔天 rag_sync 會把報表當公司原始資料吃回向量庫。
                    generated_by=f"dispatcher:{name}",
                )
                if not _send_looks_failed(send_result):
                    email_delivered = True
                print(f"[dispatcher] {name} 自寄 email → {addr}：{str(send_result)[:120]}")
            except Exception as exc:
                print(
                    f"[dispatcher] {name} 自寄 email → {addr} 例外"
                    f"（{type(exc).__name__}: {exc}）"
                )
        if not wants_telegram:
            return
    if wants_telegram:
        colors = dispatcher_agent_colors(task)
        # 一份結果要發給多色時（例：匯率推播同時發紅/黃/紫/橘），每色各推一次。
        # 任何一色沒完全成功就一起走 email fallback —— 少一個部門收到跟完全沒收到
        # 一樣是「報表悄悄掉了」，寧可大王的信箱多一封也要看得見。
        failed: list[str] = []
        for color in colors:
            try:
                tg_result = telegram_push_agent(color, body)
                if "✅" not in str(tg_result):
                    failed.append(f"{color}: {str(tg_result)[:80]}")
            except Exception as exc:
                failed.append(f"{color}: {type(exc).__name__}: {exc}")
        if not failed:
            print(f"[dispatcher] {name} 已推送 Telegram：{'、'.join(colors)}")
            return
        print(
            f"[dispatcher] {name} Telegram 未完全成功"
            f"（{len(failed)}/{len(colors)} 色失敗：{'; '.join(failed)[:200]}）。"
        )
        if email_delivered:
            # 同一份內容已經由 notify_emails 送到收件人手上，Telegram 這一路失敗
            # 只記 log —— 再補一封給大王只是重複，也違反 notify_emails 那條
            # 「不往 owner 信箱倒別人的通知」的取捨。
            print(f"[dispatcher] {name} 內容已由 notify_emails 送達，不再 fallback。")
            return
        print(f"[dispatcher] {name} 改寄 email。")
        # fall through to email fallback below
    notify(
        subject=f"【排程: {name}】",
        body=body,
        task_name=f"dispatcher:{name}",
        markdown_html=True,
    )


def should_run_task(task: dict, now: datetime) -> bool:
    if not task.get("enabled", True):
        return False
    if task.get("next_force_run"):
        return True
    # 週排程閘門（ISO：1=週一…7=週日）。沒設 weekdays = 每天可跑（既有行為）。
    # 解析器住在 scheduler.task_weekdays —— 停擺門檻 expected_gap_minutes 也用
    # 同一份，兩邊各解析一次遲早分家（週一才跑的任務被按日門檻天天誤報 stalled）。
    from agent_core.scheduler import task_weekdays
    weekdays = task_weekdays(task)
    if weekdays and now.isoweekday() not in weekdays:
        return False
    start_hour = int(task.get("start_hour", 0))
    end_hour = int(task.get("end_hour", 24))
    if not (start_hour <= now.hour < end_hour):
        return False
    last = task.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    delta_min = (now - last_dt).total_seconds() / 60
    return delta_min >= int(task.get("interval_minutes", 60))


def task_dispatcher(
    *,
    load_daemon_tasks: Callable[[], dict],
    save_daemon_tasks: Callable[[dict], bool],
    should_run_task_fn: Callable[[dict, datetime], bool],
    run_one_dispatcher_task_fn: Callable[[dict], str],
    mark_dispatcher_task_failed_fn: Callable[[dict, Exception, datetime], None],
    mark_dispatcher_task_succeeded_fn: Callable[[dict, datetime], None],
    dispatcher_result_is_empty_fn: Callable[[str], bool],
    remember_dispatcher_result_fn: Callable[[dict, str], bool],
    notify_dispatcher_result_fn: Callable[[dict, str], None],
    network_is_up_fn: Callable[[], bool],
) -> None:
    data = load_daemon_tasks()
    tasks = data.get("tasks") or []
    if not tasks:
        print("[dispatcher] daemon_tasks.json 沒有任何任務，結束。")
        return

    now = datetime.now()
    due = [task for task in tasks if should_run_task_fn(task, now)]

    # 網路閘門（見 dispatcher_network_is_up docstring 的 2026-09-07 事故）：
    # 有任務到期才探測；不通就整輪延後 —— 不 mark failed/succeeded、不動
    # last_run_at / next_force_run、不存檔，任務保持「到期」等下一輪。斷網時
    # 失敗告警反正也推不出去，延後到網路恢復直接補跑成功，嚴格優於記失敗。
    # 單一域名解析失敗不會觸發這裡（探測任一主機成功即放行），那種要讓任務
    # 照跑、留下真錯誤。
    if due and not network_is_up_fn():
        names = "、".join(task.get("name", "?") for task in due)
        print(
            f"[dispatcher] 🌐 網路不通（DNS 解析失敗），"
            f"{len(due)} 個到期任務延後到下一輪、不記失敗：{names}"
        )
        return

    ran = 0
    state_dirty = False
    save_failures = 0

    def _persist(reason: str) -> None:
        # 每個任務 mark_succeeded/failed（含 dedup hash）後**立即**存檔，且一律
        # 在 notify 之前（健檢 Medium：以前整批跑完才存、通知又先於存檔 →
        # redeploy / 崩潰打斷後，已通知過的任務因 last_run_at 沒落盤而重跑重寄）。
        # save_daemon_tasks（scheduler._save_daemon_tasks）是無鎖但原子的
        # tmp+os.replace 整檔寫入，per-task 重複呼叫安全；與並行 mutator 的
        # 覆寫競態是既有性質，逐任務存檔反而縮短陳舊視窗。失敗只記 log（下次
        # 存檔 / 下輪 dispatcher 會再試）。
        nonlocal save_failures
        if not save_daemon_tasks(data):
            save_failures += 1
            print(f"[dispatcher] ⚠️ state 寫入失敗（{reason}）")

    for task in due:
        name = task.get("name", "?")
        print(f"[dispatcher] ▶️ 執行 {name}")
        try:
            # Per-task wall-clock ceiling. A wedged task is isolated HERE:
            # run_task_with_deadline abandons just THIS task on timeout (raises
            # TaskDeadlineExceeded, a TimeoutError subclass caught below) so the
            # rest of the batch keeps running — unlike the process-fatal
            # run_with_deadline the single-task crons use. Lambda is consumed
            # synchronously before the next iteration, so it binds this `task`.
            result = run_task_with_deadline(
                lambda: run_one_dispatcher_task_fn(task),
                _DISPATCHER_TASK_DEADLINE_S,
                label=f"dispatcher:{name}",
            )
        except Exception as exc:
            mark_dispatcher_task_failed_fn(task, exc, now)
            state_dirty = True
            _persist(f"{name} failed")
            print(f"[dispatcher] {name} 執行失敗：{exc}")
            continue

        mark_dispatcher_task_succeeded_fn(task, now)
        state_dirty = True

        if dispatcher_result_is_empty_fn(result):
            _persist(f"{name} empty")
            print(f"[dispatcher] {name} 無新內容，跳過通知。")
            ran += 1
            continue

        should_notify = remember_dispatcher_result_fn(task, result)
        # dedup hash 也要在 notify 之前落盤，否則「通知後、存檔前」被打斷 →
        # 重跑時 dedup 不認得 → 同一份結果重寄。
        _persist(f"{name} pre-notify")

        if not should_notify:
            print(f"[dispatcher] {name} 結果跟前幾次重複，不重發。")
            ran += 1
            continue

        notify_dispatcher_result_fn(task, result)
        ran += 1

    if state_dirty:
        if save_failures == 0:
            print(f"[dispatcher] 本輪跑了 {ran} 個任務，state 已逐任務存檔。")
        else:
            print(f"[dispatcher] ⚠️ 本輪跑了 {ran} 個任務，但有 {save_failures} 次 state 寫入失敗。")
    else:
        print("[dispatcher] 本輪沒有任務到期。")
