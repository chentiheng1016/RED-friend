# 小紅 Cloud Run 程式部署筆記

本文件只描述程式側約定；實際搬雲端時再依專案、區域、網域與 IAM 建立資源。

## Container role

同一個 image 可以跑三種角色：

```bash
RED_CLOUD_ROLE=web        # FastAPI employee portal，讀 Cloud Run 的 PORT
RED_CLOUD_ROLE=telegram   # Telegram long polling worker
RED_CLOUD_ROLE=task       # 跑 agent_daemon.py --task <name>
RED_DAEMON_TASK=rag_sync  # role=task 時必填，例如 rag_sync/email_ingest/health_check
```

預設值：

```bash
RED_CLOUD_MODE=1
RED_EXCHANGE_MODE=telegram_only
AGENT_DAEMON_MODE=1
RED_RUNTIME_DIR=/var/lib/red
RED_EMPLOYEE_REGISTRY_BACKEND=firestore
RED_FIRESTORE_PROJECT=<project>
RED_EMPLOYEE_REGISTRY_FILE=/mnt/red-artifacts/data/employee_registry.json
RED_OPERATIONAL_DB_URL=postgresql://red:<password>@<cloud-sql-host>:5432/red
# Cloud SQL connector / Unix socket form:
RED_OPERATIONAL_DB_URL=postgresql://red:<password>@/red?host=/cloudsql/<project>:<region>:<instance>
RED_OPERATIONAL_DB_POOL=1
RED_OPERATIONAL_DB_POOL_MAX_SIZE=4
RED_TASK_QUEUE_BACKEND=postgres
RED_TELEGRAM_APPROVALS_BACKEND=postgres
RED_TELEGRAM_AUTH_BACKEND=postgres
RED_TOOL_BUDGETS_BACKEND=postgres
RED_COST_TRACKER_BACKEND=postgres
RED_RUN_HISTORY_BACKEND=postgres
RED_TASK_MEMORY_BACKEND=postgres
RED_EDGE_TASKS_BACKEND=postgres
RED_POLICY_ENGINE_BACKEND=postgres
RED_WORK_MODE_BACKEND=postgres
RED_DRY_RUN_BACKEND=postgres
RED_WEB_DOMAIN_POLICY_BACKEND=postgres
RED_INTENT_ROUTER_BACKEND=postgres
RED_ALERT_PUSH_BACKEND=postgres
RED_GEMINI_CIRCUIT_BACKEND=postgres
RED_GEMINI_CIRCUIT_BREAKER=1
RED_GEMINI_CIRCUIT_FAILURES=3
RED_GEMINI_CIRCUIT_WINDOW_S=300
RED_GEMINI_CIRCUIT_OPEN_S=180
```

`RED_EMPLOYEE_REGISTRY_FILE` 仍保留作 Cloud Storage 掛載備援；正式雲端建議以 Firestore 為員工名單主資料庫。
多人 / 多 agent 的任務、審計、授權與配額狀態應逐步放進 Postgres operational DB；
目前會把 Telegram audit 事件同步寫入 Postgres，且可用 `RED_TASK_QUEUE_BACKEND=postgres`
把背景 task queue 切到 Postgres。設 `RED_TELEGRAM_APPROVALS_BACKEND=postgres` 後，
Telegram 私聊加入申請與核准名單也會走 Postgres。設 `RED_TELEGRAM_AUTH_BACKEND=postgres`
後，Telegram 確認窗、確認 rate-limit 與訊息 rate-limit 也會集中到 Postgres。
設 `RED_TOOL_BUDGETS_BACKEND=postgres` 後，敏感工具 daily/hourly budget 會集中計算，
避免多個 instance 各自擁有一份額度。
設 `RED_COST_TRACKER_BACKEND=postgres` 後，Gemini cost 與外部 API 最終失敗事件會
集中統計，讓 cost dashboard、月度上限預警與 API error-rate alert 跨 instance 一致。
設 `RED_RUN_HISTORY_BACKEND=postgres` 後，敏感工具 run history、過去 action 搜尋與
失敗統計會集中，避免多個 agent 各自只有自己的 `index.jsonl`；metrics/dashboard
也會從這份集中 run history 聚合。
設 `RED_TASK_MEMORY_BACKEND=postgres` 後，大王/客戶交代的 task、提醒與完成狀態會
集中，避免不同 agent 各自持有不同 `task_memory.json`。如需切分環境可設
`RED_TASK_MEMORY_NAMESPACE`，預設 `default` 代表同一份共享承諾記憶。
設 `RED_EDGE_TASKS_BACKEND=postgres` 後，Edge Agent 裝置註冊、poll/claim 與 ERP
RPA task 狀態會集中，避免多個 Cloud Run web instance 各有自己的 `edge_tasks.json`。
如需切分環境可設 `RED_EDGE_TASKS_NAMESPACE`，預設 `default`。
設 `RED_POLICY_ENGINE_BACKEND=postgres` 後，tool policy allow/refuse 決策會集中，
讓多個 agent / instance 的權限拒絕、風險擋下與 dry-run 強制紀錄能一起稽核。
設 `RED_WORK_MODE_BACKEND=postgres` 後，目前 work mode 與切換歷史會集中，
避免不同 agent 對「現在是 dev/security/meeting mode」有不同認知。
設 `RED_DRY_RUN_BACKEND=postgres` 後，全域 dry-run 保護開關與演練紀錄會集中，
避免某個 instance 以為在演練、另一個 instance 卻實際執行破壞性工具。
設 `RED_WEB_DOMAIN_POLICY_BACKEND=postgres` 後，web access domain policy 會集中，
避免某個 agent 已標記網域需 API/登入/禁止，其他 instance 卻仍按本機空白 policy 抓取。
設 `RED_INTENT_ROUTER_BACKEND=postgres` 後，intent 分類紀錄與 routing dashboard
會集中，方便觀察多 agent / 多 instance 的工具縮表品質與漏判情況。
設 `RED_ALERT_PUSH_BACKEND=postgres` 後，alert push 去重、6 小時節流與恢復通知
狀態會集中，避免多個 instance 同時把同一個 alert 重複推給 Telegram/email。
設 `RED_GEMINI_CIRCUIT_BACKEND=postgres` 後，Gemini 503/504 或高需求風暴的
短暫熔斷狀態會集中，避免多個 Cloud Run instance 在同一段故障期各自重試。
JSONL / JSON 檔仍保留為本機 fallback。

Gemini API 503/504 或高需求風暴時，`RED_GEMINI_CIRCUIT_BREAKER=1` 會對同一
模型做短暫熔斷：預設 300 秒視窗內 3 次最終 transient 失敗後，停止打該模型
180 秒，再自動半開重試。這同時覆蓋 `_gemini_generate` 與 Telegram
`chat.send_message` 路徑。若 `RED_GEMINI_CIRCUIT_BACKEND=postgres` 啟用，這個
狀態會放在 operational DB 讓多 instance 共享；否則會退回同一 process 內的
保護。這不會把「熔斷中」寫成新的 API 失敗事件，因此 error-rate alert 仍反映
真實外部 API 呼叫結果；正式雲端若流量大，可把 `RED_GEMINI_CIRCUIT_OPEN_S`
調高，降低故障期間的排隊與重試壓力。若設定 `RED_GEMINI_FALLBACK_MODEL`，
主模型遇到 transient 最終失敗或 circuit open 時會改打備援模型；billing quota /
prepayment depleted 這類不可重試錯誤不會切備援。Telegram path 只會在原 call
已明確失敗時切備援；soft timeout 不切，避免原背景 thread 仍在執行工具時造成
重複副作用。本機 launchd templates 目前以 `gemini-flash-latest` 為主模型、
`gemini-2.5-flash-lite` 為備援。

### Operational DB 覆蓋範圍

目前雲端多人 / 多 agent 共用狀態以 Postgres operational DB 為主，不需要再增加
第二個關聯式 DB。各類資料庫角色如下：

| Store | 功能 | 雲端建議 |
| --- | --- | --- |
| Postgres operational DB | task queue、audit、Telegram approval/auth、tool budget、cost、run history、task memory、edge task、policy、work mode、dry-run、domain policy、intent log、alert push state、Gemini circuit state | Cloud SQL / managed Postgres；所有 Cloud Run instance 共用 |
| Firestore | 員工名單、部門、LINE/Telegram 綁定欄位 | 保留為 employee registry 主資料庫 |
| Chroma / vector store | RAG / 長期語意知識檢索 | 使用 shared HTTP Chroma 或其他向量服務，不放進 Cloud Run 本機磁碟 |
| Cloud Storage / artifact bucket | 附件、產出檔、備份、匯入檔 | 使用 bucket + soft delete，不依賴 container filesystem |
| 本機 JSON/JSONL | 開發機 fallback / 單機 launchd 相容 | Cloud Run 僅當 fallback，不作正式共享狀態 |

仍刻意保留本機或外部系統的狀態：

- `tg_chat_history`：目前是聊天上下文快取。若同一個 Telegram chat 會被多個 worker 同時處理，下一步可搬 Postgres 或 Redis；若維持單一 telegram worker，可暫留本機。
- `drive_sync_skip_state`、部分 ingest marker：建議先用單一 Cloud Run Job / singleton worker 跑同步；若未來多 ingest worker 併發，再集中到 Postgres。
- `memory_seed` marker、launchd heartbeat/state：這些主要是本機 Mac edge/開發機用途；Cloud Run 用健康檢查、Cloud Logging 和 job execution history 取代。
- Chroma vector data 與員工 registry 已分別走 vector service / Firestore，不應重複搬進 operational DB。

### Cloud SQL / pooling

Cloud Run instance 會水平擴張，DB 連線數要保守。建議：

```bash
RED_OPERATIONAL_DB_POOL=1
RED_OPERATIONAL_DB_POOL_MIN_SIZE=0
RED_OPERATIONAL_DB_POOL_MAX_SIZE=4
RED_OPERATIONAL_DB_CONNECT_TIMEOUT_SEC=2
```

`requirements-core.txt` 會安裝 `psycopg-pool`；若某個精簡映像沒有此套件，程式會自動回到直連，不影響本機。
Cloud SQL 建議使用 private IP、Cloud SQL connector 或 PgBouncer/連線池代理；高併發前先用
`agent_core.operational_health.format_backend_status()` 或 `health_check(auto_repair=False)`
確認 schema version、pool 狀態與每個 backend switch 都正確啟用。

若使用 Cloud SQL connector / Unix socket，Cloud Run deploy 也要掛上同一個 instance：

```bash
scripts/gcp_preflight.sh \
  --project <project> \
  --region asia-east1 \
  --cloud-sql-instance <project>:asia-east1:<instance> \
  --require-operational-db

scripts/gcp_deploy_cloud_run.sh \
  --project <project> \
  --region asia-east1 \
  --cloud-sql-instance <project>:asia-east1:<instance> \
  --require-operational-db
```

runtime service account 需要 `roles/cloudsql.client`；`scripts/gcp_bootstrap.sh`
會授權這個角色。

## Secrets

程式現在支援三層 secret 來源：

1. Cloud Run 直接注入的環境變數。
2. Google Secret Manager runtime lookup。
3. 本機 OS keyring，保留開發機相容性。

建議 Cloud Run 直接用 `--set-secrets` 注入這些環境變數：

```text
GEMINI_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
WEB_SECRET_KEY
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
GOOGLE_OAUTH_TOKEN_JSON
```

部門 Agent 的 Telegram 入口可用員工名單 `telegram_user_id` 欄位管理；若要用
env 先綁固定部門 chat，可加：

```text
RED_TELEGRAM_AGENT_CHATS=green:123456789,orange:223456789
RED_TELEGRAM_BOT_USERNAME=your_bot_username
RED_TELEGRAM_AUDIT_FILE=/tmp/telegram_audit.jsonl
```

上線前可先在私人對話或部門群組送 `/whoami`，用回覆中的 `chat.id` 確認要填
進 `RED_TELEGRAM_AGENT_CHATS` 或員工名單的 ID。群組普通訊息會被忽略，只處理
明確指令或 `@BotName` mention。員工名單的 `telegram_user_id` 只接受數字 ID，
且不能重複綁定。Telegram audit 預設寫到 `var/data/telegram_audit.jsonl`；
Cloud Run 若沒有掛載持久磁碟，可先寫到 `/tmp`，正式環境再接 Cloud Logging
或掛載儲存層。管理後台 `/admin/telegram` 可查看綁定診斷、最近 audit 與
Telegram 指令白名單；若 env 與員工名單重複綁同一個 `chat.id`，診斷區會
標示實際生效的 actor。

若要部署獨立部門 bot（例如 Blue / Green / Orange / Yellow），用另一個 Cloud Run service 或另一組
worker env，改用部門 bot token secret 並開啟 private-chat default actor：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=blue-telegram-bot-token
RED_TELEGRAM_CHAT_ID_SECRET_NAME=blue-telegram-chat-id
RED_TELEGRAM_BOT_USERNAME=example_blue_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=blue
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=blue
```

Green 對應改成：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=green-telegram-bot-token
RED_TELEGRAM_CHAT_ID_SECRET_NAME=green-telegram-chat-id
RED_TELEGRAM_BOT_USERNAME=example_green_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=green
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=green
```

Orange 對應改成：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=orange-telegram-bot-token
RED_TELEGRAM_CHAT_ID_SECRET_NAME=orange-telegram-chat-id
RED_TELEGRAM_BOT_USERNAME=example_orange_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=orange
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=orange
```

Yellow 對應改成：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=yellow-telegram-bot-token
RED_TELEGRAM_CHAT_ID_SECRET_NAME=yellow-telegram-chat-id
RED_TELEGRAM_BOT_USERNAME=example_yellow_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=yellow
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=yellow
```

這種模式不需要先收集每個員工的 `chat.id`；私訊該 bot 會直接進對應部門
查詢入口。群組仍維持顯式綁定，避免把群組普通聊天誤接進 agent。

部署前建議在同一組 env/secret 下跑：

```bash
python3 scripts/telegram_preflight.py --check-api
```

若只想檢查本機設定、不呼叫 Telegram API，拿掉 `--check-api`。

LINE 員工入口是 optional。若要啟用 `/line/webhook`，再加：

```text
LINE_CHANNEL_SECRET
LINE_CHANNEL_ACCESS_TOKEN
```

若要由程式 runtime 讀 Secret Manager，設定：

```bash
RED_SECRET_MANAGER_ENABLED=1
RED_SECRET_PROJECT=<gcp-project-id>
```

預設 secret id 會使用既有名稱，例如：

```text
gemini-api-key
telegram-bot-token
telegram-chat-id
web-secret-key
google-oauth-client-id
google-oauth-client-secret
google-oauth-token-json
line-channel-secret
line-channel-access-token
```

也可以逐一指定 resource：

```bash
RED_SECRET_GEMINI_API_KEY_RESOURCE=projects/<project>/secrets/gemini-api-key/versions/latest
```

## Bootstrap

先登入並選定 project：

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project <project>
```

建立必要 API、Artifact Registry、runtime service account、artifact bucket：

```bash
scripts/gcp_bootstrap.sh --project <project> --region asia-east1
```

可先跑 preflight 看目前缺什麼：

```bash
scripts/gcp_preflight.sh --project <project> --region asia-east1
```

把既有本機員工名單匯入 Firestore：

```bash
scripts/gcp_import_employee_registry.py --project <project> --dry-run
scripts/gcp_import_employee_registry.py --project <project>
```

不上雲也可以先用本機臨時 Postgres 預演 schema / backfill。這會在 `/tmp` 建 disposable
cluster，跑完自動關閉並刪掉：

```bash
scripts/rehearse_operational_db.py --write-limit 5000
```

需要存成機器可讀摘要時加 `--json`：

```bash
scripts/rehearse_operational_db.py --write-limit 5000 --json > opdb-rehearsal.json
```

CI/schema smoke 只做 disposable Postgres migration、health、dry-run backfill counts，
不碰雲端、不寫正式 DB：

```bash
make operational-db-smoke

# CI runner 已安裝 Postgres 時可要求不可 skip
.venv/bin/python scripts/ci_operational_db_smoke.py --require-postgres --dry-run-limit 100
```

初始化 Postgres operational DB schema：

```bash
RED_OPERATIONAL_DB_URL=postgresql://red:<password>@<host>:5432/red \
  scripts/migrate_operational_db.py
```

把本機既有 operational state 搬進 Postgres。正式寫入前先 dry-run：

```bash
RED_OPERATIONAL_DB_URL=postgresql://red:<password>@<host>:5432/red \
  scripts/backfill_operational_db.py --dry-run

RED_OPERATIONAL_DB_URL=postgresql://red:<password>@<host>:5432/red \
  scripts/backfill_operational_db.py
```

需要留正式回填紀錄時可加 `--json`：

```bash
RED_OPERATIONAL_DB_URL=postgresql://red:<password>@<host>:5432/red \
  scripts/backfill_operational_db.py --json > opdb-backfill.json
```

`cost_events`、`api_errors`、Telegram audit、policy/intent log、work mode history
這類 append-only JSONL 來源會寫入 `red_backfill_events` ledger；同一份檔案重跑時，
已搬過的行會計入 `skipped`，不會再重複插入目標表。

如果 JSONL 很大，可以先搬最近一段：

```bash
scripts/backfill_operational_db.py --only cost_events,api_errors,run_history --limit 50000
```

retention 由 prune 腳本集中管理；預設 dry-run，只有 `--execute` 會真的刪資料：

```bash
RED_OPERATIONAL_DB_URL=postgresql://red:<password>@<host>:5432/red \
  scripts/prune_operational_db.py --dry-run

RED_OPERATIONAL_DB_URL=postgresql://red:<password>@<host>:5432/red \
  scripts/prune_operational_db.py --execute
```

預設保留策略：cost 365 天、run history / audit / policy / work mode history 180 天、
intent classification 90 天、API error 180 天、dead letter 30 天、已完成 queue 14 天、
tool budget 45 天。task memory、edge tasks、domain policy、dry-run state、alert state 是
目前狀態，不做時間型自動刪除。

建立 Secret Manager secrets。建議 secret id 固定如下：

```bash
printf '%s' '<gemini-key>' | gcloud secrets create gemini-api-key --data-file=-
printf '%s' '<telegram-token>' | gcloud secrets create telegram-bot-token --data-file=-
printf '%s' '<telegram-chat-id>' | gcloud secrets create telegram-chat-id --data-file=-
openssl rand -hex 32 | gcloud secrets create web-secret-key --data-file=-
printf '%s' '<oauth-client-id>' | gcloud secrets create google-oauth-client-id --data-file=-
printf '%s' '<oauth-client-secret>' | gcloud secrets create google-oauth-client-secret --data-file=-
printf '%s' '<token-json>' | gcloud secrets create google-oauth-token-json --data-file=-

# operational-db-url 二選一：
printf '%s' 'postgresql://red:<password>@<host>:5432/red' | gcloud secrets create operational-db-url --data-file=-
# 或 Cloud SQL connector / Unix socket form:
printf '%s' 'postgresql://red:<password>@/red?host=/cloudsql/<project>:asia-east1:<instance>' | gcloud secrets create operational-db-url --data-file=-
```

`scripts/gcp_deploy_cloud_run.sh` 看到 `operational-db-url` 後會自動把
`RED_OPERATIONAL_DB_URL` 綁到 Secret Manager，並啟用 task queue、audit、
Telegram approval/auth、tool budget、cost、run history、task memory、edge task、
policy、work mode、dry-run、domain policy、intent log、alert push 與 Gemini circuit
的 Postgres backend。若部署時一定要確認 operational DB 已接上，加
`--require-operational-db`；缺 secret 時部署會直接停下來，而不是退回本機 fallback。

若要讓員工從 LINE 找小紅，先在 LINE Developers 建 Messaging API channel，取得 channel secret 與 long-lived channel access token：

```bash
printf '%s' '<line-channel-secret>' | gcloud secrets create line-channel-secret --data-file=-
printf '%s' '<line-channel-access-token>' | gcloud secrets create line-channel-access-token --data-file=-
```

或用設定腳本避免手動建立 secret id：

```bash
LINE_CHANNEL_SECRET='<line-channel-secret>' \
LINE_CHANNEL_ACCESS_TOKEN='<line-channel-access-token>' \
  scripts/gcp_setup_line.sh --project <project> --apply
```

若 secret 已存在，用 `gcloud secrets versions add <secret> --data-file=-` 加新版。

若本機 keyring / `credentials.json` / `token.json` 已經有完整設定，可以先 dry-run：

```bash
scripts/gcp_sync_secrets.py --project <project>
```

確認 `local-ok` 後再寫入 Secret Manager：

```bash
scripts/gcp_sync_secrets.py --project <project> --apply --generate-web-secret
```

## Build / deploy

```bash
scripts/gcp_deploy_cloud_run.sh \
  --project <project> \
  --region asia-east1 \
  --cloud-sql-instance <project>:asia-east1:<instance> \
  --require-operational-db \
  --oauth-redirect-base https://<red-web-domain>
```

這會用 Cloud Build 建 image，不需要本機 Docker。

部署後，LINE webhook URL 設為：

```text
https://<red-web-domain>/line/webhook
```

員工第一次對 LINE bot 說話時，若尚未綁定，小紅會回覆 LINE userId。管理員到 `/admin/employees` 把該 userId 填到員工的 `LINE userId` 欄位後，該員工就能使用 LINE 員工入口。

## Deploy shape

第一階段建議：

```text
red-web       Cloud Run service, RED_CLOUD_ROLE=web
red-telegram  Cloud Run worker pool or always-on service, RED_CLOUD_ROLE=telegram
red-jobs      Cloud Run jobs, RED_CLOUD_ROLE=task + RED_DAEMON_TASK=<task>
```

排程用 Cloud Scheduler 觸發 jobs：

```bash
scripts/gcp_schedule_jobs.sh --project <project> --region asia-east1
```

長任務如 `rag_sync`、`email_ingest` 優先放 Cloud Run Jobs，不放在 web request 裡。桌面控制與本機 UI automation 先留在 Mac edge，不上 Cloud Run。

## Production hardening

建議正式環境至少打開這些保護：

```bash
gcloud storage buckets update gs://<project>-red-artifacts --soft-delete-duration=30d
gcloud firestore databases update --database="(default)" --project <project> --delete-protection --enable-pitr
```

建立 `/health` uptime check 後，再建立 Monitoring alert policy 並接 email notification channel。Email channel 建立後通常需要到信箱完成 Google 驗證，否則告警不一定會寄出。

設定專案每月預算警戒：

```bash
scripts/gcp_setup_budget.sh \
  --project <project> \
  --amount 1000TWD \
  --notification-channel projects/<project>/notificationChannels/<channel-id>
```

`--amount` 必須用 billing account 的幣別，例如 `1000TWD` 或 `50USD`。Budget 只提醒、不會自動停機或限制花費。

建立自訂網域 mapping：

```bash
scripts/gcp_map_domain.sh \
  --project <project> \
  --region asia-east1 \
  --domain red.example.com
```

腳本會印出 DNS records。DNS 生效後，用自訂網域重新部署 OAuth base，並在 Google OAuth client 補上 redirect URI：

```bash
scripts/gcp_deploy_cloud_run.sh \
  --project <project> \
  --region asia-east1 \
  --oauth-redirect-base https://red.example.com
```

```text
https://red.example.com/auth/callback
```

## References

- Cloud Run service / job / worker pool model: <https://docs.cloud.google.com/run/docs/overview/what-is-cloud-run>
- Cloud Storage volume mounts: <https://cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts>
- Cloud Run Jobs: <https://cloud.google.com/run/docs/create-jobs>
- Worker pools: <https://cloud.google.com/run/docs/deploy-worker-pools>
- Cloud Scheduler HTTP targets: <https://cloud.google.com/scheduler/docs/creating>
