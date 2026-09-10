# RED 模組與功能明細

本文件整理目前 RED / 小紅專案的主要模組邊界與功能面。專案本體是一個以 Python 為主的個人工作助理與 macOS daemon，入口薄、核心邏輯集中在 `agent_core/`，外掛能力放在 `skills/`，背景排程由 `launchd/` 管理。

## 1. 系統入口

| 模組 | 角色 | 主要功能 |
| --- | --- | --- |
| `agent.py` | 互動式主入口 / compatibility facade | 初始化記憶、錯誤修正、Gemini chat、REPL；對外保留少量 public symbol，真正邏輯多數下放到 `agent_core/` |
| `agent_daemon.py` | 背景任務總入口 | 供 launchd 以 `--task <name>` 呼叫；路由 dispatcher、telegram bot、email ingest、ponder 等 daemon 任務 |
| `bin/agent` | 推薦啟動器 | 自動使用 repo 內 `.venv/bin/python` 啟動互動模式 |
| `setup.sh` | 安裝腳本 | 建立環境、安裝 lean/full/dev dependency bundle |
| `Makefile` | 開發與維運命令 | install、test、lint、housekeeping、runtime migration |

## 2. 核心架構模組

| 模組 | 角色 | 功能明細 |
| --- | --- | --- |
| `agent_core/gemini_client.py` | Gemini client singleton | API key 載入、client cache、模型呼叫包裝 |
| `agent_core/persona.py` | 系統提示與 chat builder | 建立小紅 persona、工具注入、Gemini chat 建構；含全時段生效的【量化分析素養】守則（經濟/微積分/統計：先公式後驗算、相關≠因果、缺料先標假設） |
| `agent_core/persona_profiles.py` | per-mode persona 補丁 | 依 work_mode 動態追加；`quant` = 量化深度模式（教學/推導/結構化分析報告，搭配 `set_work_mode('quant')`） |
| `agent_core/chat_session.py` | 對話狀態 | 保存 chat handle、turn counter、啟動記憶、context compact |
| `agent_core/tool_registry.py` | 工具組裝器 | 載入 built-in tools、skills、MCP tools；驗證 Gemini function schema；支援 `reload_skills` |
| `agent_core/tool_registry_catalog.py` | 內建工具清單 | 統一列出所有可被 LLM 呼叫的 built-in tools，並套用 audit / dry-run wrapper |
| `agent_core/skills.py` | skill loader | 掃描 `skills/*.py`、讀取 `SKILL_TOOLS`、註冊外掛函式 |
| `agent_core/mcp_bridge.py` | MCP bridge | 從 `mcp_servers.json` 載入 MCP server，轉成 Gemini function tool |
| `agent_core/logging_and_paths.py` | 日誌與路徑 | log 初始化、tee stream、runtime path、atomic write、log tail |

## 3. 記憶、RAG 與知識檢索

| 模組 | 功能明細 |
| --- | --- |
| `memory.py` | 長期記憶；ChromaDB vector RAG、BM25 hybrid search、行為準則學習與管理 |
| `embedding_config.py` | embedding backend／維度／collection 命名的單一事實源：`RED_EMBED_BACKEND=gemini\|bge`＋`RED_EMBED_DIM`，vector_store 與 memory 兩個 EF 共用、不得漂移 |
| `embed_server.py` | 共用本機 embedding HTTP server（bge-m3、loopback :8601）：fleet 單一模型實體，跑在獨立 torch venv（`bin/embed-server-venv` 建 `var/venvs/embed`）、launchd `com.xiaohong.embed_server` 常駐；cutover SOP 見 `docs/BGE_CUTOVER.md` |
| `embed_http_client.py` | embed server 客戶端（`RED_EMBED_HTTP_URL`）：連線錯誤退避重試、4xx/5xx 直接 raise、絕不墊零向量、回 np.ndarray（chromadb HttpClient 合約） |
| `memory_ops.py` | 記憶操作 helper；供較薄 wrapper 使用 |
| `memory_seed.py` | 版本控管的記憶種子；啟動時把 repo 根 `memory_seed.json` 的事實合併進執行期記憶 + 向量庫，讓更正跟著 repo 同步到每台機器 |
| `rerank.py` | LLM reranker；將 recall top-N 重新精排 |
| `query_expansion.py` | 查詢擴展；同義詞、翻譯、關鍵詞補強 |
| `time_aware.py` | 時間詞偵測與 date range 過濾 |
| `multihop.py` | 複雜問題拆解成多步 retrieval plan |
| `citation.py` | RAG 出處追蹤；從 thread id / parquet / Gmail 回查來源 |
| `eval_rag.py` | RAG 評測；golden set、recall@k、MRR、citation validity、歷史紀錄 |
| `entity_resolver.py` | 實體歸一化；客戶、品牌、別名表、canonical entity |

## 4. 郵件、公司知識與客戶情報

| 模組 | 功能明細 |
| --- | --- |
| `gmail.py` / `gmail_ops.py` | Gmail 搜尋、讀信、寄信、回覆、附件下載、inbox 摘要 |
| `email_classify.py` | 信件分類與急迫度判斷；支援 prioritized inbox |
| `email_lake.py` | Email data lake；append、query、統計、重建、回覆時間分析 |
| `email_timeline.py` | 依 PO / customer 查跨部門時間線 |
| `email_utils.py` | email parsing leaf helper |
| `internal_emails.py` | 內部信件知識庫入口；預覽、匯入、抽取、向量化、每日增量 |
| `internal_emails_preview.py` | 內部信件匯入預覽 |
| `internal_emails_extract.py` | Gmail thread 解析、prompt 建構、Gemini 抽取、row 產生 |
| `internal_emails_store.py` | Chroma / parquet 寫入、id set、ingest status |
| `internal_emails_orchestrator.py` | 內部信件 ingestion flow coordination |
| `dept_rules.py` | 部門、方向、品牌、排除規則判斷 |
| `payment_notice.py` | 客戶貨款到帳通知 → 台越會計（紫色 bot ＋ email）：掃 UserAng `twsales@` 與大王 `owner@` 兩個信箱，只認①銀行匯入款通知（第一銀行國外/國內匯入、國泰世華外匯匯入，逐欄解析匯款人／金額／生效日／付款明細）②客戶自己寄的付款通知（Supremo「Payment on …」、Decathlon「Notice of transfer」、外部來信明講「payment has been completed／已匯款」）。兩條都是 allowlist——實測寬鬆關鍵字 49 封候選裡 41 封是反向的（我方催款／申請付款／付供應商）。匯入款**不等於**客戶貨款：信保退費／退稅另列一區，匯款人照表念不替會計判斷。跨信箱以 RFC `Message-ID` 去重、每封只推一次（`var/state/payment_notice_seen.json`）。收件人：台灣會計 UserJ＋越南會計 UserL，每天 09:00 彙整一封（紫色 bot 推播＋兩人信箱各自寄一封，走 dispatcher 的 `deterministic_tool` 完全不經 LLM）；排程註冊 `scripts/register_payment_notice_task.py` |
| `customer_intel.py` | Customer 360；整合 Gmail、報價、樣品、RAG、spec sheet |
| `quote.py` | 從 Gmail 抽取報價歷史、建立 quote history、查詢 quote history |
| `quote_batch.py` | parquet-based 大量報價抽取，成本較低 |
| `quote_gen.py` | 產生 Excel 報價單，可選擇寄出 |
| `doc_export.py` | 把整理好的資料/報告輸出成 Excel / Word / PDF（彈性 document spec → 三種 renderer，PDF 嵌入 CJK 字型）並自動傳到 Telegram；`export_report` tool。表格儲存格可放**圖片本體**（`{"image": "<路徑>"}` / `[[IMG:...]]`，三種格式都會嵌，列高欄寬自動撐開）—— 來源路徑過白名單，部門員工收窄成 per-color 分艙目錄 |
| `doc_images.py` | 文件 → 產品圖，兩個來源：**PDF**（規格單/型錄）抽內嵌點陣圖（濾 logo/icon 與跨頁重複的頁首圖），純向量頁退回整頁 render + 裁白邊；**Excel**（客人做好的 master 表，圖貼在格子裡）兩種擺法都吃 —— 浮動圖走 openpyxl drawing anchor、新版 Excel「置於儲存格」的圖走 zip 解 richData 對應鏈（`metadata.xml` → `rdrichvalue` → `richValueRel` → media，**openpyxl 的 `ws._images` 看不到這型**，客人母表多半是這型）；每張圖連同「錨在哪一格 + 該列款號」一起回報（圖檔本身認不出款號，配對只能靠列標籤，禁止照順序硬配）。工具 `extract_uploaded_pdf_images` / `extract_uploaded_excel_images`（員工通道，讀入限縮 Telegram 上傳目錄、產出 `var/data/doc_images/dept/<色>/`）。搭 `doc_export` 的圖片儲存格＝「規格單/母表 → 有圖的 tracking log」。⚠️ 這顆**只看得到有圖的儲存格**，不是整張表：抽圖結果會一併回報「有資料但沒有圖」的列（`rows_without_images`，只看第一張到最後一張圖之間），因為 2026-08-17 UserAng 案就是拿抽圖結果當清單，母表 19 列產出 18 列（掉的正是唯一 remarks 放文字沒放圖那列）。要整張表請用 `uploaded_docs` |
| `uploaded_docs.py` | 上傳文件的**完整讀取**（員工通道）：`read_uploaded_table` 回整張 Excel/CSV（每列帶**原表列號**，才對得回抽圖工具回報的 `F11` 這種儲存格；「置於儲存格」的圖標成 🖼️ 而非 `#VALUE!`）、`read_uploaded_pdf_text` 回逐頁原文。兩顆都**不呼叫 LLM、不摘要**，且**截斷一定明講**（靜默截斷正是病灶）。路徑閘與 `doc_images.extract_uploaded_*` 共用（只讀 Telegram 上傳目錄、同一批檔案，差別只在拿回全文而非圖）。案由 2026-08-17 UserAng Richter 案：員工通道原本沒有任何「把這份檔讀完」的工具（抽圖只看得到有圖的格、`parse_sample_order` 是 LLM 摘要成 3 欄），於是母表 19 列→18 列、12 欄→7 欄，彙總 15 份 PDF 時一份都沒重讀、改用被裁掉一半的對話記憶 |
| `sample_tracker.py` | 樣品 / 訂單追蹤、狀態更新、逾期檢查、後續草稿 |
| `fx_rates.py` | 美金對台幣即時匯率資料層（每天 09:00＋14:00 推播紅/黃/紫/橘四色）：多來源交叉核對、合理區間閘（20–50）、與上次推播的漲跌、休市提醒；全確定性、走 dispatcher 的 `deterministic_tool` 完全不經 LLM。工具面 `skills/fx_rates.py`、排程註冊 `scripts/register_fx_rate_tasks.py`。⚠️ 不用台銀牌告：`rate.bot.com.tw` 已掛 JS bot 防護擋掉所有非瀏覽器 client |
| `purchasing_brief.py` | 採購每日簡報資料層：未讀信摘要、待辦追蹤（依回覆內容判是否結案）、出口越南福群明細（出貨通知信主旨解析＋Excel 附件，期間可給空＝本月／`2026-07`／`2026` 整年）、未完成付款（ERP `AP_APPLY_M` PAYMENT_ID 為空）、VN 交貨進度／下單狀況。收件人：UserA（台灣採購）09:00 一封、**只有兩段**（未讀信＋2026 出口文件 Excel，2026-08-06 她本人縮小範圍）；UserM（越南採購）09:00＋15:00 兩封、七段不變。全確定性，LLM 只寫人話；工具面在 `skills/purchasing_brief.py`、排程註冊在 `scripts/register_purchasing_brief_tasks.py` |

## 5. Google Suite 與行事曆

| 模組 | 功能明細 |
| --- | --- |
| `google_auth.py` | Google OAuth credentials 與 service cache |
| `google_suite.py` | Calendar event 查詢 / 建立 / 刪除；Drive 搜尋與上傳 |
| `drive_ops.py` | Drive 搜尋與上傳底層 helper |
| `briefing.py` | 會議 briefing；整合 Calendar、Gmail、報價歷史、RAG |
| `meeting_notes.py` | 會議紀錄保存到本機文件，並支援寄出 |

## 6. 本機操作、瀏覽器、RPA 與視覺

| 模組 | 功能明細 |
| --- | --- |
| `apps.py` | 開啟 URL、開關 App、跨平台 app alias |
| `system_ctl.py` | 音量、通知、剪貼簿、AppleScript、本機系統控制 |
| `input_devices.py` | 鍵盤、滑鼠、捲動操作 |
| `accessibility.py` | macOS Accessibility API；以語義元素點擊、輸入、讀值 |
| `browser.py` / `browser_ops.py` | Playwright browser automation；open/read/click/fill/type/press/extract/screenshot/eval/tab |
| `vision.py` | 圖片與螢幕分析 |
| `vision_ops.py` | OpenCV template matching、OCR image / screen region、以圖或文字定位畫面元素 |
| `qc.py` | QC 圖片檢查；master reference 管理與批次檢查 |
| `specs.py` | spec sheet 解析、版本列表、差異比較 |
| `erp.py` | 從教學影片或 Drive folder 學 ERP workflow；管理 workflow JSON |
| `erp_executor.py` | 以 Gemini Computer Use + Playwright 執行 ERP workflow |
| `auto_skill.py` | 從螢幕影片自動產出 Python skill |
| `demo_recording.py` | Playwright codegen 錄製 demo，整理成 reusable skill |
| `workflow.py` | 多步驟 workflow state machine；step、retry、resume、run history |

## 7. 自動化、背景任務與通知

| 模組 | 功能明細 |
| --- | --- |
| `scheduler.py` | daemon dispatcher 的排程任務 CRUD 與立即執行 |
| `daemon_helpers.py` | daemon 共用工具；state、notify、log rotate、timestamp |
| `daemon_dispatcher.py` | 判斷 scheduled task 是否到期、執行、記錄結果、通知 |
| `daemon_email_ingest.py` | email ingest daemon task |
| `daemon_ponder.py` | 從近期訊息抽 fresh insight 並寫入記憶 |
| `daemon_telegram.py` | Telegram bot message handling 與 chat 建構 |
| `telegram.py` | Telegram push notification |
| `telegram_actor_scope.py` | Telegram 多使用者區隔 — 非 owner actor 的 owner-only 工具封鎖與身分注入 |
| `dept_tool_scope.py` | 員工自由對話 per-色工具白名單 — `RED_TG_EMPLOYEE_FREEFORM`（現值 `all`，九色全開；僅私訊）放行員工進受限 Gemini 對話；工具面 = 共用查詢面 ∪ 本色 `_HOME_TOOLS` ∪ QUERY_MATRIX 繼承，再 ∩ SAFE tier；每顆工具綁 `AgentRequest(caller=色)` → rag_gateway per-color ACL。curation 明細見 `agent_core/agents/README.md` |
| `actor_google_tools.py` | 員工自有信箱 actor-scoped Gmail/行事曆工具（網域委派 impersonate 員工本人，回信/寄通知/排會議） |
| `tg_auth.py` | Telegram 敏感工具確認、鎖定、一次性授權 |
| `dept_nlp_query.py` | 員工自然語言查詢引擎（三通道共用：web `/api/dept/*/ask`、Telegram 色 bot 自由文字、LINE 自由文字）。兩段式 plan→execute→synthesize：flash 規劃唯讀呼叫（JSON mode）→ `query.*` 走 PermissionMiddleware（caller=員工色）＋ `rag_gateway` ACL 語意搜尋 → sanitize+wrap untrusted 後整合回答。只開 query.*、fail-closed；`RED_EMPLOYEE_NLP_DISABLED=1` kill switch。**語意搜尋（RAG + search_emails/search_docs）預設關**（`RED_EMPLOYEE_NLP_SEMANTIC_SEARCH=1` 才開）——chroma 的 boolean `access_<color> $eq True` metadata filter 有 server 端效能病態（連 8k collection 都 >35s），待修好再開；結構化 query.* 不受影響 |
| `ingest/acl_reconcile.py` | RAG ACL 對帳：按 `rag_access.json` 現值重標「存量」chunk 的 `access_<color>` 旗標（規則改動只影響新 ingest，存量靠這個補）。sqlite 唯讀 fastpath 撈 embedding id → 共用 HTTP server 分批 get/update（只改 mismatch、冪等）。共用入口：`scripts/backfill_rag_access.py`（一次性全量）＋ `rag_runner` 夜跑最後 phase（`time_budget_s` 分夜補 delta）。含請求 timeout + 逐批非致命（wedge 請求逾時跳過、下次補） |
| `session_registry.py` | Owner session 主控台：登記三通道活躍對話 session（`telegram:`/`line:`/`web:` 前綴，跨程序 locked_json）＋ owner-only 遠端控制工具 `list_sessions`/`pause_session`/`resume_session`/`reset_session`/`broadcast_message`。三個前台入口都在收訊時 touch 並套暫停 gate：Telegram（`daemon_telegram.tg_handle_message`，暫停短路不進 LLM）、LINE（`line_bot.build_line_employee_reply`，回暫停通知）、Web（`web_server/app._session_gate_or_none`，回 423）。`broadcast_message` 對活躍 session 群發（Telegram/LINE 直接 push、Web 排佇列於下次開頁顯示，路由以 session_id 前綴為準）。owner/boss 永不被暫停、也不收自己的廣播 |
| `tool_runner.py` / `tool_rpc_*` | Tool RPC client/server/protocol 與 fresh subprocess worker；隔離 tool 執行、timeout/cancel、source freshness |
| `health.py` | daemon、state、ChromaDB、parquet、log、磁碟、DNS 健康檢查與部分自修復 |

`launchd/scripts/` 目前提供：

- `morning.py`：早晨簡報
- `briefing_15min.py`：會議前簡報
- `mailcheck.py`：新信分類、急件與業務信提醒、回覆草稿
- `sample_check.py`：樣品期限檢查
- `health_check.py`：定期健康檢查
- `email_ingest.py`：信件資料湖匯入
- `tool_rpc.py`：Tool RPC Unix socket daemon，每次 tool call 再派 fresh worker subprocess
- `ponder.py`：近期訊息洞察沉澱
- `housekeeping.py`：清理舊 logs / workflow runs / migrations
- `backup_data_lake.sh`：資料湖備份

`launchd/templates/` 目前包含 morning、briefing、mailcheck、sample_check、health_check、dispatcher、telegram、email_ingest、internal_ingest_daily、vectorize_oneoff、housekeeping、ponder、backup_weekly 等 plist template。

## 8. 安全、審計、成本與執行紀錄

| 模組 | 功能明細 |
| --- | --- |
| `path_safety.py` | path traversal 與敏感路徑防護 |
| `prompt_injection.py` | untrusted text sanitization 與 prompt injection 包裝 |
| `dry_run.py` | 全域 dry-run 模式；破壞性工具可只回報預期行為 |
| `run_history.py` | skill 執行 audit trail，可選前後截圖 |
| `log_redact.py` | log secret redaction |
| `vault.py` | secret vault；統一 keyring 存取、audit trail、清理 |
| `cost_tracker.py` | Gemini API 成本記錄、每日 / 七日 / tool 維度統計與警示 |
| `mistake_ledger.py` | 事實糾正記錄與錯誤歷史（Telegram 文字糾錯學習，搭配 correction_detector） |

## 9. 外掛 Skills

`skills/` 每個 `.py` 檔可透過 `SKILL_TOOLS` 註冊為小紅工具，不用改 `agent.py`。目前有：

| Skill | 功能明細 |
| --- | --- |
| `briefing.py` | 每日狀態 briefing：把 `system_status()` 報告寄 email / 推 Telegram（有對外副作用，屬 sensitive） |
| `erp_oracle.py` | 飛越 ERP（Oracle / JHDB）唯讀查詢：訂單主檔 + 物料到料狀況，走 SSH 在主機上跑 sqlplus |
| `erp_warehouse.py` | 飛越 ERP 本地 DuckDB 鏡像 text-to-SQL：中文自然語言查全 ERP（1,538 表 + 20 個可讀視圖，唯讀多層防線）；另含 `query_erp_stock` 確定性庫存查詢（零 LLM、員工白名單可用，核心在 `agent_core/erp_stock_query.py`）與 `erp_delivery_risk_alert` 交期風險每日表（ERP 出交期/生效狀態 × 生管日報出已包裝/未完，逐列標來源；走 `deterministic_tool` 不經 LLM，排程註冊在 `scripts/register_erp_delivery_risk_task.py`） |
| `excel_ops.py` | Excel sheets/read/pivot/filter/write/query；`xlsx_extract_images` 抽貼在儲存格的圖（回報錨點儲存格＋該列款號，核心在 `agent_core/doc_images.py`） |
| `factory_warehouse.py` | 工廠數據倉 text-to-SQL：自然語言精確查生產數據（DuckDB 唯讀、deadline 看門狗） |
| `pdf_ops.py` | PDF info、抽文字、抽表格、搜尋、合併、拆分、轉圖（`pdf_to_images` 整頁截圖給 OCR）、`pdf_extract_images` 挖頁面裡的產品圖本體（核心在 `agent_core/doc_images.py`，給 `export_report` 的圖片儲存格用） |
| `image_gen.py` | Gemini image model 生成與編輯圖片 |
| `product_photo_search.py` | 產品照搜尋：款號/鞋型關鍵字或參考圖找相似舊款（macOS Vision 特徵指紋、本機免費） |
| `purchasing_brief.py` | 採購每日簡報六件套：信箱未讀摘要、待辦追蹤、出口越南福群明細（含 Excel，欄序＝客戶／供應商／LOT／數量／ETD／ETA (CAT LAI)／ETA (FU CHUN)／庫存編號）、未完成付款、VN 廠商交貨進度、近期下單（純讀、`background_safe`，核心在 `agent_core/purchasing_brief.py`） |
| `fx_rates.py` | 美金對台幣即時匯率：`usd_twd_rate_brief` 回傳可直接推播的完整訊息（純讀、`background_safe`，核心在 `agent_core/fx_rates.py`） |
| `taiwan_public.py` | 台灣公開 API：電子發票、央行匯率（⚠️ `bot_exchange_rates` 目前失效，台銀已擋自動化）、公司查詢、縣市代碼 |
| `taiwan_public_2.py` | 台灣公開 API 第二批：AQI、台股、氣象警報、郵遞區號 |
| `quant_tools.py` | 驗算過的經濟/財務/統計計算器：損益兩平、貢獻邊際、需求彈性、NPV/回收期、CAGR、敘述統計、簡單 OLS 迴歸（純函式、`background_safe`） |
| `erp_kitting.py` | 齊套/缺料上線預警（ERP 鏡像、確定性 SQL、SAFE 唯讀）：`kitting_check` 逐單齊套明細、`kitting_alert` 每日掃描（卡單料聚合／FCFS 共用料模擬／在途 ETA 三分類／催排清單）。另含**反向**問法 `material_capacity_by_style`（剩下的材料每個形體還能做幾雙＝接新單本錢：可用池 0批+M01、扣在手單需求算淨/毛，共用同一道需求門 `_counts_as_demand`）與生產管理每日簡報 `production_capacity_brief`（材料段＋產能折線圖，走 `deterministic_tool` 不經 LLM，排程註冊在 `scripts/register_production_brief_tasks.py`） |
| `warehouse_box_ocr.py` | 外箱手寫嘜頭 OCR（`read_box_shipping_marks`）：純本機 PP-OCRv6 + 旋轉投票 + lexicon 編輯距離校正，搭配 `record_box_ocr_correction` 糾正回饋 |

## 10. 資料與 runtime 目錄

| 路徑 | 用途 |
| --- | --- |
| `var/logs/` | runtime logs |
| `var/state/` | daemon / agent 狀態 |
| `var/data/` | data lake、向量庫或其他資料 |
| `var/runs/` | tool / workflow 執行紀錄 |
| `var/workflows/` | workflow runtime artifacts |
| `erp_workflows/` | 已學習 ERP workflow JSON 與 raw video extraction |
| `specs/` | customer spec sheet 結構化版本 |
| `recordings/` | demo recording runtime artifact |
| `logs/` | 仍存在的舊 log 位置或相容資料 |

## 11. 測試與品質門檻

目前 `tests/` 覆蓋：

- daemon smoke：`test_agent_daemon_smoke.py`
- lazy path / import safety：`test_agent_lazy_paths.py`
- daemon helpers：`test_daemon_helpers.py`
- internal email modules：`test_internal_email_modules.py`
- pure helper functions：`test_pure_helpers.py`
- RPA helpers：`test_rpa_helpers.py`
- security regressions：`test_security_regressions.py`
- smoke imports：`test_smoke_imports.py`

常用命令：

```bash
make test
make test-quiet
make lint
make housekeeping
make migrate-runtime
```

## 12. 目前模組分層摘要

```text
RED/
├── agent.py / agent_daemon.py     # 入口與 daemon router
├── agent_core/                    # 核心能力與工具實作
│   ├── Gemini / persona / chat / tool registry
│   ├── memory / RAG / eval / citation
│   ├── Gmail / Google Suite / email lake / customer intel
│   ├── browser / accessibility / vision / ERP / workflow
│   ├── daemon / scheduler / telegram
│   └── safety / audit / vault / cost tracking
├── skills/                        # 可熱重載的外掛工具
├── launchd/                       # macOS 背景任務 scripts + plist templates
├── erp_workflows/                 # 已學習 ERP 流程資料
├── specs/                         # spec sheet 結構化資料
├── var/                           # runtime state / data / logs / runs
└── tests/                         # unittest 品質門檻
```
