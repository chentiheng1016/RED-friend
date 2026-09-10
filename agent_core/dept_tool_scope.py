"""員工自由對話的 per-color 工具白名單（Telegram employee freeform chat）。

背景：telegram_actor_scope 的區隔只分 owner / 非 owner，不分部門色 —— 直接放行
colored 員工進 Gemini session 會拿到跟 red GM 員工一樣的全公司唯讀工具面，且
RAG 以 Agent.RED（SUPER_ADMIN）視野查詢。本模組補上「色」這一層：

  1. 啟用開關 ``RED_TG_EMPLOYEE_FREEFORM``（預設空 = 全關）：
       "indigo,gray"       → 只開這些色
       "1" / "true" / "all" → 全色開
  2. 白名單 = _COMMON_TOOLS ∪ 本色 _HOME_TOOLS ∪（QUERY_MATRIX 可查各色的
     _HOME_TOOLS），再與 SAFE tier 交集。CONFIRM/DANGEROUS/LOCKED 永不進員工
     session —— +確認 token 不綁身分，漏一顆進去就能自我確認。
  3. wrap_tools_with_agent_caller：把每顆工具包進 middleware AgentRequest
     context（caller=該色），讓 rag_gateway.current_request_caller 在資料層
     套 per-color ACL —— 否則 RAG 預設 RED 視野＝資料層越權。

呼叫端（daemon_telegram.tg_build_chat）先跑 filter_tools_for_non_owner 再疊
本模組的色過濾（雙保險、皆為 build 期移除而非執行期拒絕）。
"""
from __future__ import annotations

import os
from typing import Any, Callable

# 各部門色的「本部門」工具（curated；只放 SAFE 唯讀查詢）。
# 未列出的色只拿 _COMMON_TOOLS —— 之後要開放哪個色，先來這裡補 curation。
_HOME_TOOLS: dict[str, frozenset[str]] = {
    "indigo": frozenset({
        "read_warehouse_stock",
        "read_box_shipping_marks",
        # ERP 鏡像確定性庫存查詢（同 yellow；零 LLM 入口 /dept indigo
        # query.erp_stock、/warehouse erp）。經矩陣繼承 green 也會拿到。
        "query_erp_stock",
        # ERP 採購單確定性查詢（收貨要對單；indigo 矩陣不含 yellow，
        # 不放這裡就拿不到）。零 LLM 入口 /warehouse erppo。
        "query_erp_purchase_orders",
        # 訂單×材料 齊套/缺料（需求/已到/在途/短少；含 item 聚焦＋舊短碼）。
        # 起因：2026-07-29 UserA SF24 案——員工拿不到齊套引擎，freeform 用
        # 舊單採購到貨腦補「短少 0」（採購配額綁單，舊單到貨≠新單有料）。
        "kitting_check",
        # ERP 庫存分配紀錄確定性查詢（哪天分配給哪張指令訂單；倉庫調撥
        # M01→0 批的上游依據）。起因見 yellow 同名條目（indigo 矩陣不含
        # yellow，不放這裡就拿不到）。零 LLM 入口 /warehouse alloc。
        "query_erp_allocation",
        # 進貨預告／庫存注意／組合簡報（2026-08-04 UserY 每日通知清單）：
        # 未到齊的生效採購單＋廠商付款/送貨條件＋該料現有庫存與倉別，是倉庫
        # 排驗貨人力與倉儲空間的日常查詢面。確定性 SQL、內容不含單價/金額，
        # 敏感度同 query_erp_purchase_orders（已評估可開）。
        "warehouse_arrival_plan",
        "warehouse_stock_watch",
        "warehouse_daily_brief",
        # ⚠️ warehouse_mail_digest / warehouse_todo_board 刻意**不進**任何色：
        # 它們直讀 warehouse-mgr@ / warehouse@ 兩個信箱的內容，放進員工 session 等於
        # 讓任何 indigo 員工讀主管信箱（理由同下方 read_dept_email_timeline）。
        # 這兩顆只給 dispatcher 排程與大王互動用。
    }),
    "gray": frozenset({
        "read_production_progress_sheet",
        "production_alert",
        "query_production_schedule",
        "production_dashboard",
        # 形體剩餘材料可做雙數（2026-08-05 生管主管經理需求）：接新單前自己查「這個形體
        # 的料還能開幾雙」，不必等 09:00/15:00 那封信。確定性 SQL、內容是用量與庫存
        # 雙數（無單價/金額），敏感度同 kitting_check。
        # ⚠️ chart_station_capacity 刻意不放：畫圖工具一律不進員工白名單（deliver
        # 會送到**大王**的 chat，不是問的人）；折線圖走排程信的附件。
        "material_capacity_by_style",
    }),
    "green": frozenset({
        "read_sample_status",
        "read_sample_bom",
        # ERP 樣品開發單確定性查詢（SR 樣品單反查／樣品 BOM 用料照表念）。
        # 起因：2026-07-29 UserC PU468 案——SP00 樣品域是 freeform 工具盲區，
        # LLM 憑備料採購單斷言「ERP 無獨立 SR 樣品單」（實際 4 張 SR 單在用）。
        # 經矩陣繼承 orange/indigo/purple/gray/black 也會拿到——內容是樣品
        # BOM 用料/供應商，敏感度同 query_erp_bom，已評估可開。
        "query_erp_sample_orders",
        "search_product_photos",
        # 鞋圖照片本體直傳 Telegram（2026-07-28 UserA 案：查迪卡儂款鞋圖，
        # freeform 只回得出文字＋Drive 連結）。照片經回覆附圖機制送回當前
        # 對話，不走出站推送閘。
        "fetch_shoe_photos",
        "list_tracked_samples",
        "check_sample_deadlines",
        # 客人樣品單(xlsx/pdf) 解析 → 每款的款號/顏色/逐部位材料。
        "parse_sample_order",
        # 樣品單「線稿驅動」生鞋圖（抽客人手繪線稿鎖形狀 + 規格上材質）。
        # 起因：2026-08-03 UserC 案——丟 Richter 8105-3272 樣品單問「自動生成
        # 鞋圖」，小紅答「系統尚無此功能」。這正是開發部的主場景，工具早就有
        # （PR #297），只是卡在 CONFIRM tier 進不了員工白名單；已改 SAFE +
        # 每日張數硬上限擋花費（見 tool_tiers 該條註解）。
        # ⚠️ 這是唯一會「花生圖錢」的員工工具，列進 _HOME_ONLY_TOOLS 不給矩陣
        # 繼承——別色能查樣品室資料是一回事，能替樣品室燒生圖預算是另一回事。
        "generate_from_order",
    }),
    "orange": frozenset({
        "read_customer_order_pos",
        "query_po_timeline",
        "query_customer_timeline",
        "list_customer_pos",
        "check_stale_pos",
        "check_overdue_promises",
        "customer_360",
        "list_active_customers",
        "customer_alerts",
        "query_quote_history",
        # Supremo（Lurchi）出貨文件追蹤（2026-08-20 大王需求）：排程在橙色 bot
        # 問 UserAng「文件寄了沒」，她在同一個 bot 回覆——所以確認/查狀態兩顆
        # 要進 freeform 白名單，不然回覆只會得到「這要請 Red 管理員處理」。
        # 兩顆都只回「這幾櫃的文件寄給兩位 ContactW 了沒」的窄事實（固定收件人、
        # 固定查詢），不開放任意讀信箱。confirm 會寫追蹤狀態檔＋觸發查核，
        # 列進 _HOME_ONLY_TOOLS 不給矩陣繼承——能查業務資料是一回事，能替
        # 業務「回覆已處理」是另一回事。
        "shipping_doc_status",
        "confirm_shipping_docs",
    }),
    "yellow": frozenset({
        "query_material_arrival",
        "material_arrival_overview",
        "check_material_readiness",
        "get_model_bom",
        "query_bom",
        "query_quote_history",
        # ERP 鏡像確定性庫存查詢（寫死參數化 SQL、非 text-to-SQL；同邏輯的
        # 零 LLM 入口是 /dept yellow query.erp_stock）。經 QUERY_MATRIX 繼承，
        # orange/blue/purple/gray 員工也會拿到——內容僅庫存結存，已評估可開。
        "query_erp_stock",
        # ERP 採購單確定性查詢（訂購/已收數量、單價/金額分欄照表念）。
        # 起因：2026-07-26 UserA 案——freeform 走 RAG 讀採購單 PDF，把金額
        # 當訂購數量。零 LLM 入口 /purchase erppo、/dept yellow query.erp_po。
        # 含單價/金額欄，繼承面同上（orange/blue/purple/gray/black），已評估可開。
        "query_erp_purchase_orders",
        # ERP 生效版 BOM 確定性查詢（料號→使用款反查／型體·款號→用料）。
        # 起因：2026-07-27 UserA 案——freeform 走 RAG 讀 Drive BOM 表（型體級），
        # 把同型體別的顏色（#8916447）也算成 G40 用料款；BOM 用料是顏色級。
        # 零 LLM 入口 /purchase bom、/dept yellow query.erp_bom。
        "query_erp_bom",
        # ERP 訂單實際需求確定性查詢（訂單材料追蹤照表念：逐生效訂單
        # 需求/領料/每雙攤提＝需求÷訂單雙數）。起因：2026-07-29 G407 案——
        # freeform 拿 Drive BOM 成本表回單一標準用量，ERP 實際攤提要看
        # 訂單材料追蹤。零 LLM 入口 /purchase demand、/dept yellow
        # query.erp_demand。
        "query_erp_order_demand",
        # ERP 應付請款確定性查詢（帳單總額＝NET_MONEY 含運費等非採購單費用）。
        # 起因：2026-07-27 三寶案——「帳單總金額」被拿採購單金額加總回答，
        # 漏掉不掛採購單的運輸費 5,140。零 LLM 入口 /purchase ap、
        # /dept yellow query.erp_ap。金額敏感度同 erp_po（已評估可開）。
        "query_erp_payables",
        # 訂單×材料 齊套/缺料（同 indigo；採購追料主場景，SF24 案）。
        "kitting_check",
        # ERP 庫存分配紀錄確定性查詢（庫存可用量分配 PO-POTF_240 照表念：
        # 分配日期/指令訂單/本次分配數/轉出批號）。起因：2026-07-30 UserA
        # G407 案五——「7/30 分配給哪張單」freeform 拿庫存批次＋訂單需求
        # 數字對得上就縫合成答案，把 07-23 的舊分配當成 07-30 回報。
        # 零 LLM 入口 /purchase alloc、/dept yellow query.erp_alloc。
        # 內容僅分配紀錄（無單價/金額），敏感度同 query_erp_stock。
        "query_erp_allocation",
        # 鞋圖照片本體直傳 Telegram（2026-07-28 UserA 案；yellow 矩陣不含
        # green，不放這裡採購就拿不到）。唯讀 Drive 圖檔、SAFE。
        "fetch_shoe_photos",
    }),
    # purple（會計）/ black（出納）不設 home：QUERY_MATRIX 已可查大半部門，
    # 對帳/付款所需的 PO/BOM/料況/庫存面全由矩陣繼承。
    # white（法務）矩陣為空（SoT 不主動查）→ 只拿共用查詢面；合約/測試報告
    # 走 search_drive_docs（RAG caller ACL 綁 white）。cash_* / specs 查詢是
    # /dept 子 agent 通道（telegram_command），不在 registry 工具面、不受此表影響。
    # ⚠️ read_dept_email_timeline 刻意不進任何色：dept 是自由參數（含「老闆」），
    # 員工 session 放進去=跨部門信件時間軸越權。
}

# 所有開放色都拿得到的共用查詢面。
_COMMON_TOOLS: frozenset[str] = frozenset({
    "search_drive_docs",
    "search_operation_sops",
    # Telegram 上傳圖片判讀（路徑限縮在上傳目錄、只收圖片副檔名）。
    # 起因：2026-07-29 UserA OZ18/OZ19 案——員工通道無看圖工具，「依圖列出
    # 總需求量」只能腦補數量（真實款號配虛構 10,000 雙）。每個部門都會
    # 傳照片問事，放共用面。
    "analyze_uploaded_image",
    # 產出 Excel / Word / PDF 檔並跟著回覆傳回當前對話。
    # 起因：2026-08-03 開發部 UserC 案——問「請生成 excel 表」只拿到 CSV 文字，
    # 小紅答「green 部門唯讀查詢，需 Red 管理員權限」。這顆**沒有自己的資料面**
    # （純渲染 LLM 手上已經合法查到的內容），開放不擴大任何查詢範圍；且每個
    # 部門都會要「這份給我一份 Excel」，所以放共用面而非單開 green。
    # 交付走 doc_export 的 [[TG_FILE:]] 回覆附檔（產到 exports/dept/<色> 分艙），
    # 不走 telegram_send_file —— 後者的 chat_id 閘只認大王 keyring chat。
    "export_report",
    # 上傳的 PDF（客人規格單 / 型錄）挖出產品圖 → 填進 export_report 的圖片
    # 儲存格。起因：2026-08-12 UserAng 案——15 份規格單 PDF 要做成開發追蹤表
    # （tracking log），客人原表「remarks」欄放的是產品圖，小紅只填得出
    # 「見 2001.pdf 第 1 頁」的文字對照，還回「無法把 PDF 裡的圖嵌進 Excel」。
    # 讀入限縮在上傳目錄、產出分艙 per-color，跟 analyze_uploaded_image 同
    # 邊界，各部門都會收到客人 PDF，放共用面。
    "extract_uploaded_pdf_images",
    # 同上，但來源是客人做好的 Excel 母表（圖貼在儲存格裡）。回傳帶「原本錨在
    # 哪一列 + 該列款號」，才對得回品項；UserAng 那份
    # 「8-11samples room AW27 RD Richter tracking log.xlsx」就是這型。
    "extract_uploaded_excel_images",
    # 上傳檔的**完整內容**（整張 Excel/CSV 的列與欄、PDF 逐頁原文）。
    # 起因：2026-08-17 UserAng Richter 案——員工通道從頭到尾沒有一顆「把這份
    # 檔讀完」的工具（抽圖那兩顆只看得到有圖的格子，parse_sample_order 是 LLM
    # 摘要成 3 欄），於是母表 19 列產出 18 列（掉的正是唯一沒有圖的那列）、
    # 12 欄剩 7 欄，彙總 15 份 PDF 時一份都沒重讀、改用被裁掉一半的對話記憶，
    # 還回「已將您上傳的 10 份規格單全部彙總」。
    # 讀取範圍與上面兩顆完全相同（同一道上傳目錄閘、同一批檔案），差別只在
    # 拿回的是全文而不是圖；純讀不寫、不花錢。
    "read_uploaded_table",
    "read_uploaded_pdf_text",
})

# 只給「本色」、不隨 QUERY_MATRIX 繼承出去的工具。
# 矩陣繼承的語意是「A 可以查 B 部門的資料」——對唯讀查詢成立，對**會花錢的
# 產生型工具**不成立（2026-08-03 UserC 案：generate_from_order 開給 green，
# 但 indigo/purple/gray/black 能查 green 不代表能燒 green 的生圖預算）。
_HOME_ONLY_TOOLS: frozenset[str] = frozenset({
    "generate_from_order",
    # 出貨文件的「業務確認」只有業務本人（orange）能做——別色經矩陣拿到等於
    # 能替業務答「已處理」。唯讀的 shipping_doc_status 照常繼承。
    "confirm_shipping_docs",
})

_ENABLE_ALL = frozenset({"1", "true", "all"})


# 個別部門色專屬的 system instruction 補充（接在共用那段後面）。
_COLOR_ADDENDA: dict[str, str] = {
    # 2026-08-20 大王需求：Supremo（Lurchi）出貨文件的提醒訊息由排程推到橙色
    # bot，UserAng 直接在同一個 bot 回「文件已寄出／處理好了」——LLM 要接住
    # 這句話去呼叫工具，否則整條「回覆→查核→結案」流程斷在中間。
    "orange": (
        "\n📄【出貨文件回報要記錄，別只口頭答謝】對方（業務）說某櫃的出貨文件"
        "「已寄出／已處理好了／已安排寄出」時，呼叫 confirm_shipping_docs("
        "ref=櫃號，如 \"LURCHI-CONT8\"；note=對方原話重點）——工具會記錄回覆並"
        "立刻查核 email 是否真的寄給兩位 ContactW，把工具回傳**照表念**給對方："
        "說結案就是結案；說查無寄件就照實轉達查無、請對方確認，**不可**自行"
        "宣布結案或替對方掛保證。被問「文件案子現在的狀態」用 "
        "shipping_doc_status 查了照念。"
    ),
    # 2026-08-03 UserC 案：丟樣品單 xlsx 問「依 Richter 8105-3272 自動生成鞋圖」，
    # 小紅回「系統目前尚無依 Excel 自動 AI 繪製鞋圖的功能、請設計師用 Photoshop
    # 畫」——工具其實早就有（PR #297 線稿驅動）。工具面補齊後還要在 prompt 講明，
    # 否則 LLM 仍會照「小紅是文字與資料管理助手」的自我認知推掉。
    "green": (
        "\n👟【樣品單→鞋圖辦得到，別再說沒這功能】對方上傳客人樣品單/訂單 "
        ".xlsx 並要「依這張單生成鞋圖／畫出來／渲染看看」時，直接呼叫 "
        "generate_from_order（order_file_path 帶訊息「路徑:」的完整路徑；"
        "指定配色就帶 colorway）——它會抽出單裡客人的手繪線稿鎖住鞋型、再依"
        "數字標記部位的材質顏色渲染成產品圖。**不要**回「小紅是文字助手／"
        "系統沒有繪圖功能／請設計師用 Photoshop 畫」。要先講解單子內容才用 "
        "parse_sample_order。工具回傳的 [[TG_PHOTO:...]] 標記行要原樣保留在"
        "回覆最後，系統才會把圖傳給對方（標記本身不會顯示）。產出是概念圖、"
        "非生產精準圖，回覆時要照實說明這點。"
        # 2026-09-07 大王兩條規則：①視角依樣品單（設計稿只畫側面就只出側面），
        # 對方明確要求才換角度；②對方指正上一版某部位錯了＝只修那裡，其他保留。
        # 聊天層曾自行加「45 度立體展示視角」進 extra_note 生出四視角組圖——
        # extra_note 是工具的耳朵，只能聽見使用者的原話。
        "對方指正上一版渲染圖某部位錯了（其他都對）時，走修圖模式：再呼叫 "
        "generate_from_order，previous_render_path 帶上一版的「圖檔路徑」、"
        "extra_note **一字不改照抄對方的指正原話**——不要自行改寫或添加對方"
        "沒說的內容（尤其視角/構圖/色系），視角一律依樣品單設計稿，對方明確"
        "要求換角度才提。"
    ),
}


def employee_freeform_colors() -> frozenset[str]:
    """回傳 RED_TG_EMPLOYEE_FREEFORM 啟用的色集合（無效色靜默略過）。"""
    raw = os.environ.get("RED_TG_EMPLOYEE_FREEFORM", "").strip().lower()
    if not raw:
        return frozenset()
    from agent_core.agents.permission_matrix import Agent
    all_colors = frozenset(a.value for a in Agent)
    if raw in _ENABLE_ALL:
        # red 走既有 GM 全工具路徑，不在 freeform 白名單制度內。
        return all_colors - {"red"}
    colors = set()
    for piece in raw.split(","):
        color = piece.strip()
        if color and color != "red" and color in all_colors:
            colors.add(color)
    return frozenset(colors)


def employee_freeform_enabled_for(color: str) -> bool:
    return str(color or "").strip().lower() in employee_freeform_colors()


def allowed_tool_names_for_color(color: str) -> frozenset[str]:
    """本色白名單：共用 ∪ 本色 home ∪ QUERY_MATRIX 可查各色的 home。

    繼承來的那份會扣掉 _HOME_ONLY_TOOLS（只屬於本色的工具不外流）。
    """
    normalized = str(color or "").strip().lower()
    try:
        from agent_core.agents.permission_matrix import Agent, QUERY_MATRIX
        agent = Agent(normalized)
    except Exception:
        return frozenset()
    allowed = set(_COMMON_TOOLS) | set(_HOME_TOOLS.get(agent.value, ()))
    for target in QUERY_MATRIX.get(agent, ()):
        allowed |= _HOME_TOOLS.get(target.value, frozenset()) - _HOME_ONLY_TOOLS
    return frozenset(allowed)


def _tool_name(fn: Any) -> str:
    return getattr(fn, "__name__", "") or ""


def filter_tools_for_color(
    tools: list[Any], color: str
) -> tuple[list[Any], list[Any]]:
    """(kept, removed)：白名單 ∩ SAFE tier，保序。tier 查不到＝fail-closed 移除。"""
    allowed = allowed_tool_names_for_color(color)
    kept: list[Any] = []
    removed: list[Any] = []
    for fn in tools:
        name = _tool_name(fn)
        if name not in allowed:
            removed.append(fn)
            continue
        try:
            from agent_core.tool_tiers import get_tier, TIER_SAFE
            is_safe = get_tier(name) == TIER_SAFE
        except Exception:
            is_safe = False
        (kept if is_safe else removed).append(fn)
    return kept, removed


def wrap_tools_with_agent_caller(tools: list[Any], color: str) -> list[Any]:
    """把每顆工具包進 AgentRequest context（caller=該色）。

    工具實際執行點在 genai AFC 的 worker thread 裡，contextvar 不會從主緒自動
    傳播 —— 所以包在工具本體上（執行當下才 set/reset），不管誰在哪條線程呼叫
    都保證生效。functools.wraps 保 __name__/__doc__/簽名，後續 mode/intent/
    tg_auth 各層 by-name 過濾不受影響。
    """
    import functools

    from agent_core.agents.middleware import AgentRequest, agent_request_context
    from agent_core.agents.permission_matrix import Agent

    try:
        agent = Agent(str(color or "").strip().lower())
    except Exception:
        return []

    wrapped: list[Any] = []
    for fn in tools:
        name = _tool_name(fn)

        def _make(inner: Callable, tool_name: str):
            @functools.wraps(inner)
            def scoped(*args, **kwargs):
                req = AgentRequest(
                    caller=agent,
                    target=agent,
                    intent=f"telegram.freeform.{tool_name}",
                    payload={},
                )
                with agent_request_context(req):
                    return inner(*args, **kwargs)
            scoped._dept_scoped_color = agent.value
            return scoped

        wrapped.append(_make(fn, name))
    return wrapped


def dept_scope_color() -> str:
    """這次呼叫是否在部門色 AgentRequest context 底下？回部門色或 ""。

    context 是 wrap_tools_with_agent_caller 包上去的（本模組是唯一產生者，所以
    判讀也放這裡）；大王 / REPL / daemon 路徑沒有 context（或 caller=red）→ 回
    ""，行為完全不變。工具端拿它決定「產出物落哪個分艙目錄」——doc_export 的
    exports/dept/<色>、doc_images 的 doc_images/dept/<色> 都靠這顆。
    """
    try:
        from agent_core.agents.middleware import current_agent_request
        req = current_agent_request()
    except Exception:
        return ""
    if req is None:
        return ""
    color = str(getattr(getattr(req, "caller", None), "value", "") or "").strip().lower()
    return "" if color in ("", "red") else color


def dept_scope_addendum(color: str) -> str:
    """附進 system instruction 的一小段部門範圍說明（給 LLM 對齊預期）。"""
    normalized = str(color or "").strip().lower()
    if not normalized:
        return ""
    return (
        f"\n\n【部門查詢範圍】你現在服務的是 {normalized} 部門員工：只有該部門"
        "與權限矩陣授權部門的唯讀查詢工具。不能寄信、不能寫入、不能執行系統"
        "操作；被問到時直說「這要請 Red 管理員處理」，不要假裝辦得到。"
        "\n📄【產檔案是可以的，別再說沒權限】對方說「生成 excel／給我 word／"
        "做成 pdf／做成檔案」時，直接呼叫 export_report 產出真檔案，**不要**"
        "改回 CSV 文字、也**不要**回「權限不足／需 Red 管理員」——這顆工具"
        "已對所有部門開放。formats 只給對方明確要的那一種（沒指明就 \"excel\"）。"
        "工具回傳的 [[TG_FILE:...]] 標記行要原樣保留在回覆最後，系統才會把檔案"
        "傳給對方（標記本身不會顯示）。"
        "\n🖼️【Excel 可以放圖，別再說做不到】對方要的表格有「圖／照片／"
        "remarks 放圖」那一欄時：規格單 PDF 用 extract_uploaded_pdf_images、"
        "客人已經做好的 Excel 母表（圖貼在格子裡）用 extract_uploaded_excel_images"
        "（都帶訊息「路徑:」的完整路徑），再把它**回傳的路徑**填進 export_report "
        "該格，寫成 {\"image\": \"<路徑>\"}——圖會嵌進儲存格。"
        "**不要**回「AI 無法自行抓圖／無法把 PDF 裡的繪圖與照片嵌入 Excel／"
        "只能做文字對照（Cross-Reference）」，也不要拿檔名頁碼交差。路徑一律用"
        "工具回傳的原字串、不可自己拼；某份抽不到圖就照實講那幾份沒圖。"
        "🛑 Excel 母表抽出來的圖要用工具回報的**該列款號**對回你的表，"
        "**不可照順序硬配**——配錯圖比沒圖更糟；對不上就明講對不上。"
        "\n【上傳圖片必先讀圖】訊息含「上傳檔案／種類: photo」時，先用"
        " analyze_uploaded_image（image_path 帶訊息「路徑:」的完整路徑）讀出"
        "實際內容再回答。「依圖」計算時：先逐列複誦圖中數字（款號→數量），"
        "再列式計算，並註明數字判讀自圖片、請對方複核；圖讀不到或格子看不清"
        "就明講。🛑 絕不可拿假設/範例數字配真實款號料號回答——寧可說讀不到，"
        "不可腦補數量。"
        "\n📊【上傳的表格／PDF 要「讀完」才算讀過】要照某份上傳檔做事（做成新表、"
        "比對、彙總）時：Excel/CSV 用 read_uploaded_table 把整張表讀出來、PDF 用 "
        "read_uploaded_pdf_text 讀原文（都帶訊息「路徑:」的完整路徑）。"
        "🛑 抽圖那兩顆（extract_uploaded_*_images）**只看得到有圖的儲存格**，"
        "拿它當清單會把「沒有圖的那幾列」整列漏掉；parse_sample_order 是 LLM "
        "摘要（只有款號/顏色/部位規格三欄、截前 9000 字），兩者都不等於讀完這份檔。"
        "\n🛑【彙總多份檔案：一份一份重讀，然後對帳】把 N 份上傳檔併成一張表時："
        "① **逐檔重新呼叫工具讀一次**，不可以拿對話記憶裡先前的摘要充數——"
        "歷史訊息會依字元預算被裁掉，被裁掉的那幾份會整批消失且完全看不出來；"
        "② 產出前對帳：來源幾份/幾列 vs 你表上幾列，對不上就在回覆裡列出漏了哪幾"
        "份/哪幾列與原因；③ 沒讀到的欄位就留空並註明「原檔未提供／尚未讀取」，"
        "**不可**填「Colorway Variant 1」「Standard Spec」這類看起來像資料的佔位字樣。"
        "🛑 不確定是否讀齊時，回覆要寫「已納入 N 項（來源 M 項，差異：…）」，"
        "不可以寫「已全部彙總」。"
        "\n🛑【員工指錯→重新查核（最高指導原則）】員工說你答錯、數字不對、"
        "要你再確認時：**必須用手上的查詢工具把該事實重查一次**，依重查結果"
        "回答——資料推翻原答就引用新資料更正並說明錯在哪；資料仍支持原答就"
        "維持並列出處；重查後仍無法確認就老實說「查不到能確認的資料」。"
        "禁止沒重查就道歉附和、禁止沒重查就堅持原答、禁止編造任何數字或單號。"
        + _COLOR_ADDENDA.get(normalized, "")
    )
