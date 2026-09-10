# 小紅 (RED) 安裝指南

給第一次拿到這份 repo 的使用者。macOS 專用。

## 快速開始（3 個指令）

```bash
git clone https://github.com/chentiheng1016/RED-friend.git ~/RED
cd ~/RED
./setup.sh
```

完成後：

```bash
./bin/agent
```

如果要一起裝測試 / lint 工具：

```bash
./setup.sh --dev
```

如果要把所有 optional heavy bundles 也一起裝上：

```bash
./setup.sh --full
```

就這樣。如果要懂裡面做了什麼，往下讀。

---

## 系統需求

| 項目 | 版本 | 說明 |
|---|---|---|
| macOS | 12+ | 用到 `osascript` / launchd / `SwitchAudioSource` |
| Python | 3.12+ | **不要** 用系統內建 3.9（已 EOL，`ssl` 模組會用 LibreSSL 崩 native code） |
| Node.js | 18+ | 可選；Puppeteer / MCP npm servers 用 |
| 硬碟 | ~1.2 GB 起 | 預設安裝為 core + rag + docs；`--full` 會再裝 GUI 等重套件 |

**推薦裝 Python 3.12**：

```bash
brew install python@3.12
```

## 前置依賴（Homebrew）

```bash
brew install python@3.12 ffmpeg tesseract tesseract-lang
```

- `ffmpeg`：YouTube / 影音轉檔

如果要啟用 Node/Puppeteer 輔助能力：

```bash
brew install node
npm ci
npm run smoke:node
```

`puppeteer-core` 不會下載 Chromium；它會使用本機 Chrome/Chromium。只驗證依賴載入時跑
`npm run smoke:node`；要真的啟動瀏覽器做 smoke test：

```bash
npm run smoke:node:launch
```

如果 Chrome 不在常見 macOS 路徑，設定 `PUPPETEER_EXECUTABLE_PATH` 或 `CHROME_PATH`。

目前 `puppeteer-extra-plugin-stealth` 的 transitive dependency 仍帶
`rimraf@3` / `glob@7` / `inflight` 的 deprecated warning，且 2026-07 起
`npm audit` 會報 `brace-expansion` DoS（GHSA-mh99-v99m-4gvg）——CI 走
`make node-audit`（`scripts/node/npm_audit_gate.cjs`）做 scoped 豁免，理由見
Makefile `NODE_AUDIT_IGNORE` 註解。不要用 `overrides` 硬升這條鏈：
`rimraf` 已實測會破壞舊 plugin 關閉瀏覽器時清暫存 profile 的行為；
`brace-expansion@5.0.8` 改 named exports，會讓 `minimatch@3` 所有 brace
pattern 直接 TypeError（2026-07-25 實測）。

## Post-deploy smoke

改完 daemon / launchd / Google / Telegram / Node 相關設定後，可跑：

```bash
./bin/red-smoke
```

它會依序檢查：

- `health_check(auto_repair=False)`
- `./bin/red-status daemons`
- `tool_rpc_status()`
- Google Drive API list ping
- Telegram push ping（會送一則短測試訊息）
- `npm run smoke:node`

如果只想跑不會對外送訊息的版本：

```bash
./bin/red-smoke --skip-telegram
```

沒有 Google token 或只是做本機快速檢查時：

```bash
./bin/red-smoke --skip-telegram --skip-google
```

## setup.sh 做的事

1. **檢查 Python 版本**（≥ 3.12 推薦）
2. **建 `.venv/`** — repo 內部的虛擬環境，跟系統 Python 隔離
3. **安裝預設 bundles**（`requirements-core.txt` + `requirements-rag.txt` + `requirements-docs.txt`），包含：
   - Gemini API（google-genai）
   - Gmail / Drive / Calendar API
   - ChromaDB / BM25 記憶檢索
   - Excel / PDF / Docx 文件處理
4. **渲染 + 部署 launchd plist**：把 `launchd/templates/*.plist` 裡的 `@@REPO_ROOT@@` 替換成你 repo 的真實位置，複製到 `~/Library/LaunchAgents/`
5. **`launchctl load`** 所有 daemon（telegram bot、email 抓信、每日增量 ingest 等）

`--full` 另外會安裝：
- `requirements-gui.txt`：`mss`、`pyautogui`、`pygetwindow`、`playwright`、OCR / OpenCV、macOS Accessibility
- `requirements-market.txt`：`yfinance`

如果有加 `--dev`，還會另外安裝：
- `pytest`
- `pytest-timeout`
- `ruff`

方便本地做測試與靜態檢查，但不影響正式執行環境。

## 開發者常用指令

```bash
make install-dev
make install-hooks
make test
make lint
```

如果要安裝完整依賴集合：

```bash
make install-full
```

把舊版 root runtime 檔案收斂到 `var/`：

```bash
./bin/migrate-runtime        # 先 dry-run
./bin/migrate-runtime --apply
```

手動做一次 housekeeping：

```bash
make housekeeping
```

## 目前結構

如果你是接手維護的人，現在 repo 可以先這樣理解：

- `agent.py`
  互動入口 + compatibility facade。外部若有 `import agent` 依賴，優先把它當穩定介面，不要直接把內部 wiring 當公共 API。
- `agent_daemon.py`
  背景任務入口。主要 task 邏輯已拆到 `agent_core/daemon_*.py`，這個檔現在以 dispatch / wrapper 為主。
- `agent_core/tool_registry.py`
  工具清單組裝器。built-in tool catalog 與 audit/dry-run 包裝規則在 `agent_core/tool_registry_catalog.py`。
- `agent_core/internal_emails.py`
  內部郵件系統入口。preview / extract / store / orchestration 已拆成獨立模組，主檔以 compat + wiring 為主。

這個方向的原則是：

- 大檔先拆成「入口」和「流程」兩層
- 入口檔盡量只保留 public surface / dispatch
- 真正的流程與 I/O 細節移到 `agent_core/` 的專責模組

如果你想讓每次 git commit 前自動跑 lint + smoke tests：

```bash
make install-hooks
```

如果只想安靜地確認測試是否通過：

```bash
make test-quiet
```

## 需要自己提供的秘密

`setup.sh` **不會**幫你設定這些，必須手動做：

### 1. Gemini API key

存到 macOS 鑰匙圈（最安全）：

```bash
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'gemini-api-key', '你的AIza...金鑰')"
```

或走環境變數（不推薦）：`export GEMINI_API_KEY="AIza..."`

### 2. Google OAuth credentials（Gmail/Drive/Calendar）

去 [Google Cloud Console](https://console.cloud.google.com/) 建 OAuth 2.0 Client ID（desktop app），下載 JSON 存成：

```
~/RED/credentials.json
```

第一次執行 agent 會彈瀏覽器讓你授權，產生 `~/RED/token.json`。

### 3. Telegram Bot Token（可選）

如果要透過 Telegram 聊天：

```bash
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'telegram-bot-token', '你的token')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'telegram-chat-id', '你的chat_id')"
```

沒設定 Telegram bot 會自動關掉，不影響其他功能。

#### 3a. 部門 Agent Telegram chat 綁定（可選）

預設只有 `telegram-chat-id`（Red / 管理者）能進 Telegram daemon。若要先準備
各部門 Agent 的 Telegram 入口，可以二選一：

1. 在 `/admin/employees` 員工管理頁填 `Telegram chat/user ID`；系統會用員工
   的 `color` 決定 Telegram 入口權限。這個欄位只接受 Telegram 的數字
   chat/user ID，且同一個 ID 不能重複綁給多位員工。
2. 用環境變數直接綁部門 chat：

```bash
export RED_TELEGRAM_AGENT_CHATS="green:123456789,orange:223456789,white:-1001234567890"
```

多個 chat 可用 `|` 分隔：`green:111|222`。非 Red chat 目前只走部門
`/dept <color> query.*` 查詢入口，不會進全工具 Gemini 對話；寫入、同步、
寄信與檔案操作仍保留給 Red 管理者。

實際綁定前，可在 Telegram 對 bot 送：

```text
/whoami
```

回覆會顯示 `chat.id`、`from.id`、群組型態與目前綁到的 Agent color。私人
對話通常綁 `chat.id` 即可；群組請綁負數的群組 `chat.id`。如果要把 bot 放進
群組，建議也設定 bot username，讓 `/dept@BotName ...` 和 `@BotName` mention
可被正確辨識：

```bash
export RED_TELEGRAM_BOT_USERNAME="your_bot_username"
```

群組內的普通聊天會被忽略；只有明確指令（例如 `/dept ...`、`/dev ...`、
`/sales ...`、`/whoami`）或 tag bot 的訊息會被處理。部門程式若主動推播，
會優先送到該 color 的 Telegram chat；沒有設定時會 fallback 到 Red owner。

#### 3a-2. 獨立部門 Telegram Bot（例如 Green / Orange / Gray / Purple）

如果某個部門要像 Red 一樣有自己的 Telegram 帳號，不用群組，也不用把
`chat.id` 先寫進 `RED_TELEGRAM_AGENT_CHATS`。做法是替該部門在 BotFather
建立一支獨立 bot，token 存 keyring，daemon 以該部門身分處理私人對話。

Black / Blue / Green / Gray / Indigo / Orange / Purple / White / Yellow 採同一個命名規則：

```bash
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'black-telegram-bot-token', '<black bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'blue-telegram-bot-token', '<blue bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'green-telegram-bot-token', '<green bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'gray-telegram-bot-token', '<gray bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'indigo-telegram-bot-token', '<indigo bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'orange-telegram-bot-token', '<orange bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'purple-telegram-bot-token', '<purple bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'white-telegram-bot-token', '<white bot token>')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'yellow-telegram-bot-token', '<yellow bot token>')"
```

launchd 模板：

- `launchd/templates/com.xiaohong.telegram_black.plist`
- `launchd/templates/com.xiaohong.telegram_orange.plist`
- `launchd/templates/com.xiaohong.telegram_blue.plist`
- `launchd/templates/com.xiaohong.telegram_gray.plist`
- `launchd/templates/com.xiaohong.telegram_green.plist`
- `launchd/templates/com.xiaohong.telegram_indigo.plist`
- `launchd/templates/com.xiaohong.telegram_purple.plist`
- `launchd/templates/com.xiaohong.telegram_white.plist`
- `launchd/templates/com.xiaohong.telegram_yellow.plist`

Black 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=black-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_black_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=black
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=black
```

Blue 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=blue-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_blue_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=blue
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=blue
```

Green 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=green-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_green_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=green
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=green
```

Gray 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=gray-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_gray_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=gray
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=gray
```

Orange 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=orange-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_orange_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=orange
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=orange
```

Indigo 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=indigo-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_indigo_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=indigo
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=indigo
```

Purple 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=purple-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_purple_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=purple
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=purple
```

Yellow 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=yellow-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_yellow_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=yellow
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=yellow
```

White 模板會讀：

```text
RED_TELEGRAM_BOT_TOKEN_SECRET_NAME=white-telegram-bot-token
RED_TELEGRAM_BOT_USERNAME=example_white_bot
RED_TELEGRAM_DEFAULT_ACTOR_COLOR=white
RED_TELEGRAM_ALLOW_DEFAULT_ACTOR_PRIVATE_CHATS=1
RED_TELEGRAM_STATE_SUFFIX=white
```

意思是：任何人私訊部門 bot，都會被視為該 color 的部門入口；群組仍然
不自動開放，必須另外綁 chat ID。部門入口不走 Red 的全工具 Gemini，
預設只開放部門查詢類指令，例如：

```text
/sales customer PAX
/sales active 90 2
/dev profile
/dev samples open
/dev sample S-2026-01
/production status
/production history
/production report {"order_id":"P-001","product":"鞋底 A","customer":"PAX","original_ecd":"2026-05-30","reason":"機台故障","severity":"medium"} +確認
/cashier summary
/cashier payments Fulltide
/cashier receipts 第一銀行
/legal specs
/legal spec Richter 5001-4292
/legal search 合約 PFAS 保固
/dept blue query.profile
/dept black query.profile
/dept gray query.profile
/dept white query.profile
/dept orange query.customer_360 {"customer":"PAX","days":60}
/dept green query.profile
/dept yellow query.profile
/dept purple query.accounting_summary {"days_back":30}
/purchase pos delayed
/purchase suppliers
/purchase eta PO001
/accounting summary
/accounting invoices 中華電信
/whoami
```

部署 / 重啟部門 bot：

```bash
cd ~/RED
./bin/redeploy-daemons telegram_black --force
./bin/redeploy-daemons telegram_blue --force
./bin/redeploy-daemons telegram_gray --force
./bin/redeploy-daemons telegram_green --force
./bin/redeploy-daemons telegram_indigo --force
./bin/redeploy-daemons telegram_orange --force
./bin/redeploy-daemons telegram_purple --force
./bin/redeploy-daemons telegram_white --force
./bin/redeploy-daemons telegram_yellow --force
tail -f ~/RED/var/logs/daemon-telegram-black.log
tail -f ~/RED/var/logs/daemon-telegram-blue.log
tail -f ~/RED/var/logs/daemon-telegram-gray.log
tail -f ~/RED/var/logs/daemon-telegram-green.log
tail -f ~/RED/var/logs/daemon-telegram-indigo.log
tail -f ~/RED/var/logs/daemon-telegram-orange.log
tail -f ~/RED/var/logs/daemon-telegram-purple.log
tail -f ~/RED/var/logs/daemon-telegram-white.log
tail -f ~/RED/var/logs/daemon-telegram-yellow.log
```

Telegram daemon 會把授權失敗、群組忽略、rate-limit、以及實際處理結果寫入
本機 JSONL audit：

```bash
var/data/telegram_audit.jsonl
```

可用 `RED_TELEGRAM_AUDIT_FILE=/path/to/telegram_audit.jsonl` 改位置。
管理後台 `/admin/telegram` 會顯示綁定診斷、最近 audit，以及目前 Telegram
指令白名單。若 `RED_TELEGRAM_AGENT_CHATS` 與員工名單重複綁定同一個
`chat.id`，或格式不是 Telegram 數字 ID，診斷區會直接標示 warning。

部署或改綁定前，可先跑 Telegram preflight：

```bash
python3 scripts/telegram_preflight.py
```

預設只檢查本機設定與檔案路徑，不打 Telegram API。若要驗證 bot token 是否
真的能連到 Telegram，再加：

```bash
python3 scripts/telegram_preflight.py --check-api
```

#### 3b. 雲端 Telegram-only 交換窗口（建議雲端部署）

小紅上雲端後，建議把 Telegram 設成唯一正常的人機交換窗口；後端狀態、
audit log、任務佇列和長期檔案仍放在小紅自己的儲存層，不放 Telegram。

Cloud Run 的程式入口、container role、Secret Manager / env secret 約定集中在
[`docs/CLOUD_RUN.md`](./docs/CLOUD_RUN.md)。本機開發仍可用 macOS keyring；
雲端建議用 Cloud Run `--set-secrets` 注入環境變數。

```bash
export RED_EXCHANGE_MODE=telegram_only

# 可以是本機目錄、mounted bucket、或物件儲存同步目錄。
# 不要放在 ~/RED/var/data，該區被 path_safety 保護，避免 RAG poisoning。
export RED_EXCHANGE_ARTIFACT_DIR="$HOME/red-exchange/artifacts"

# 若要讓超過 Telegram 50MB 的檔案用 Telegram 傳下載連結，必填。
# 可填 CDN / R2 / S3 / GCS / 私有簽名網址 gateway 的 base URL。
export RED_OBJECT_BASE_URL="https://files.example.com/red"

# break-glass，只做緊急管理/健康檢查；日常資料交換仍走 Telegram。
export RED_EMERGENCY_ADMIN_URL="https://admin.example.com/red"
```

行為：
- Telegram 收到的附件在 Telegram-only 模式會存到
  `$RED_EXCHANGE_ARTIFACT_DIR/incoming/<日期>/`。
- `telegram_send_file()` 遇到超過 50MB 的檔案時，會暫存到 artifact store，
  再把 `$RED_OBJECT_BASE_URL/...` 連結發回 Telegram。
- Telegram session 會隱藏本機瀏覽器、桌面通知等替代
  人機窗口工具，避免雲端環境把資料導到看不到的本機 UI。
- 若沒設定 `RED_OBJECT_BASE_URL`，小紅會明確回報「已暫存但無法交付可點連結」，
  不會假裝已送出。
- 在 Telegram 問「交換窗口狀態」時可用 `exchange_policy_status()` 檢查目前模式。

#### 3c. LINE 員工入口（可選）

LINE 適合作為公司員工入口；Telegram 仍保留給大王/管理者使用。LINE webhook
會驗證 `X-Line-Signature`，再用 LINE `userId` 對應員工名單。

本機可先把 LINE secrets 放 keyring：

```bash
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'line-channel-secret', '你的LINE channel secret')"
python3 -c "import keyring; keyring.set_password('xiaohong-agent', 'line-channel-access-token', '你的LINE channel access token')"
```

雲端部署請建立 Secret Manager secrets：

```bash
printf '%s' '<line-channel-secret>' | gcloud secrets create line-channel-secret --data-file=-
printf '%s' '<line-channel-access-token>' | gcloud secrets create line-channel-access-token --data-file=-
```

也可以用腳本處理：

```bash
LINE_CHANNEL_SECRET='<line-channel-secret>' \
LINE_CHANNEL_ACCESS_TOKEN='<line-channel-access-token>' \
  scripts/gcp_setup_line.sh --project <gcp-project-id> --apply
```

LINE Developers 的 webhook URL 設成：

```text
https://<小紅網域>/line/webhook
```

員工第一次傳訊息會收到自己的 LINE userId；管理員到 `/admin/employees` 把
這串填進 `LINE userId` 欄位後，該員工就能使用 LINE 員工入口。

### 4. MCP Servers（可選，但強烈建議）

MCP (Model Context Protocol) 讓小紅一鍵接上 Anthropic 生態的數百個工具 — 檔案、GitHub、Notion、Slack、Postgres 等等。每加一個 server = 小紅多 10-30 個新能力。

#### 前置：裝 node + uv

```bash
brew install node uv   # node 給 npx 用、uv 給 uvx 用
```

#### 設定 config

```bash
cp mcp_servers.json.example mcp_servers.json
# 編輯 mcp_servers.json，拿掉 _開頭_ 的註解 key，留下要用的 server
```

起步建議兩個沒風險的：

```json
{
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem",
             "/Users/你/RED", "/Users/你/Downloads"]
  },
  "fetch": {
    "command": "uvx",
    "args": ["mcp-server-fetch"]
  }
}
```

- **filesystem**：給小紅 read/write/edit/search 指定目錄的檔案（14 個 tool）
- **fetch**：HTTP 抓網頁轉 Markdown（比 `read_website_content` 輕量）

重啟 agent，小紅啟動時會自動連線、列工具，log 會看到：

```
[MCP] ✅ 載入 filesystem（14 個 tool）
[tools] 44 skill + 14 MCP = 外掛工具 58 個
```

對小紅說「列一下 MCP servers」→ 她會呼叫 `list_mcp_servers()` 報告目前有哪些。

#### 要加更多 server

MCP 官方 index: https://github.com/modelcontextprotocol/servers
社群收集: https://glama.ai/mcp/servers

大部分 npm 的 server 直接加 config 就能用（第一次會下載約 20MB）。含 API key 的（GitHub / Slack / Notion）在 `env` 欄位填；`mcp_servers.json` 已加進 `.gitignore` 不會被 commit。

**注意**：MCP tool 是帶前綴的工具名（`mcp_filesystem_read_text_file`），跟小紅內建工具分開。大王跟她講話時不用記前綴 — 她自己會挑對的 tool 叫。

### 5a-quad. Dry-run 全覆蓋（T7）

想先看小紅「要做什麼」但不要真的做？打開 dry-run：

```
對小紅：「進入 dry-run 模式」  → enable_dry_run_mode()
   現在寄信、點擊、刪事件、生圖、起子代理...全部只會演練

對小紅：「列一下 dry-run 演練了什麼」 → last_dry_run_log()
對小紅：「關掉 dry-run」 → disable_dry_run_mode()
```

覆蓋的 destructive tool（17 個）：
- 寄信 / 回信：`send_gmail` / `reply_gmail`
- 日曆 / Drive：`create_calendar_event` / `delete_calendar_event` / `upload_to_drive`
- 生成類：`generate_image` / `edit_image`
- GUI 操作：`click_screen` / `type_text` / `press_keys` / `open_application` / `close_application`
- Accessibility：`ax_click` / `ax_type_in`
- 代理 / 學習：`delegate_to_sub_agent` / `delegate_to_sub_agents_parallel` / `learn_skill_from_video`

**設計**：Dry-run 包裝在 `@audited` 之後，演練也會進 `run_history`（result 帶 `🧪 [DRY RUN]` 前綴，方便日後 audit 查當時在演練模式）。

### 5a-tri. Credential Vault（T6）

把散落在各 skill 的 `keyring.get_password(...)` 統一成一個有 audit 的介面：

```python
from agent_core.vault import get_secret, set_secret

api_key = get_secret("moenv-api-key")    # ← 自動寫 audit log
set_secret("moenv-api-key", "xxx")       # ← 只記 name、不記 value
```

對大王用：
- 對小紅說「看一下我有哪些 secret 設了」→ `list_vault_secrets()`
- 「幫我設 moenv-api-key 為 abc123」→ `set_vault_secret(name, value)`
- 「誰最近讀過 gemini-api-key？」→ `vault_access_log(name="gemini-api-key")`

已知 secrets（`vault.py _KNOWN_SECRETS`）：
- `gemini-api-key`（核心，沒設 agent 直接 exit）
- `telegram-bot-token` / `telegram-chat-id`（Telegram bot）
- `einvoice-appid`（電子發票中獎查詢）
- `moenv-api-key`（空氣品質）
- `cwa-api-key`（中央氣象署警報）

**安全設計**：
- `vault_access.log` 只記 `{ts, op, name, caller, ok}` — **永遠不寫 value**
- `set_vault_secret / delete_vault_secret` 故意**不**走 `@audited` — 雙重 audit 會讓 value 落地到 `runs/` 風險增加
- Vault 的 access log 已 `.gitignore`

### 5a-bis. Workflow State Machine（T2）

把 skill 從「一條函式」升級成「多 step 有狀態、可 retry、失敗可查」。是小紅走向**真正 RPA** 的關鍵架構。

#### 寫 workflow 的語法

```python
from agent_core.workflow import workflow, step

@workflow(name="月度發票整理", description="...")
def process_invoices(month: str):
    files = step("下載發票", download, month, retry=3, backoff="exponential")
    records = step("OCR", ocr_all, files, retry=2)
    return step("寫 Excel", write_report, records,
                on_fail=lambda e, ctx: notify_human(str(e)))
```

每個 step 都自動：
- 記錄 input / output / elapsed / status 到 `workflows/{run_id}/state.json`
- retry N 次，backoff 策略可選 `"none" / "linear" / "exponential"`
- `on_fail` callback 最後防線（通常是通知人工介入）
- 整條 workflow 也進 `run_history`

#### 查詢工具

- `list_workflows()` — 有哪些 workflow 註冊了
- `list_workflow_runs(workflow_name, status, since_hours)` — 最近跑過的
- `show_workflow_run(run_id)` — 每個 step 的 retry 次數 / 結果 / 錯誤
- `workflow_stats(since_hours)` — 成功率、最常失敗的 step

⚠️ `workflows/` 已 `.gitignore`（含 intermediate data）。

### 5a. RPA 升級（Accessibility API + Run History）

**這個跟一般 LLM agent 不一樣 —— 要做真正的 RPA（像 UiPath）必備的兩個基礎都上線了**。

#### T1：macOS Accessibility API（`ax_*` 系列）

把 `click_screen(300, 412)` 升級成 `ax_click(app="Mail", title="送出")`。
視窗位置／解析度／動畫延遲變動都不怕。

**要給 Accessibility 權限**（必要，一次性）：
1. `System Settings → Privacy & Security → Accessibility`
2. 加入 `/Users/你/RED/.venv/bin/python`（可用 `.venv/bin/python` 執行 `ax_check_permission(prompt=True)` 彈視窗）
3. 勾選後重啟 agent 就能用

工具：
- `ax_check_permission()` — 看有沒有權
- `ax_list_running_apps()` — 列執行中的 App
- `ax_describe_app(app_name)` — dump 元素樹（找目標用）
- `ax_find_elements(app, title="", role="")` — 查符合的元素
- `ax_click(app, title, role="")` — 語義化點擊
- `ax_type_in(app, field_title, text)` — 直接設文字欄位值
- `ax_read_value(app, element_title)` — 讀 AXValue

#### T3：Run History（Audit Trail）

凡是被 `@audited` 裝飾的 tool，每次呼叫自動寫紀錄到 `runs/`：
- input / output / elapsed / status
- 失敗時完整 traceback
- GUI 操作類（`click_screen`、`ax_click` 等）前後各 1 張 screenshot

查詢工具：
- `list_runs(tool_name, status, since_hours=24)` — 最近執行
- `show_run(run_id)` — 單筆詳情（含 screenshot 路徑）
- `run_history_stats()` — 成功率 / 最常用 / 最常錯 tool
- `prune_old_runs(days=90)` — 刪舊記錄

⚠️ `runs/` 已 `.gitignore`（screenshot 可能含敏感畫面）。

### 5b. 文件處理 skill（Excel / PDF / 圖片生成）

`requirements.txt` 包含這 3 個 skill 需要的所有 lib（setup.sh 會一起裝）。小紅能直接做：

| 類別 | 工具 | 範例指令 |
|---|---|---|
| Excel | `excel_sheets` / `excel_read` / `excel_pivot` / `excel_filter` / `excel_write` / `excel_query` | 「幫我讀 ~/Downloads/訂單.xlsx 前 10 列」／「按客戶做 pivot 看總金額」／「這 Excel 裡誰是第一大客戶」 |
| PDF | `pdf_info` / `pdf_extract_text` / `pdf_extract_tables` / `pdf_search` / `pdf_merge` / `pdf_split` / `pdf_to_images` | 「合併這 3 份報價成一份」／「這份合約裡哪裡提到 PFAS」／「把掃描版發票每頁轉成圖（之後可 OCR）」 |
| 圖片 | `generate_image` / `edit_image` | 「幫我畫一款黑色皮革工作鞋的側面示意圖」／「把這張圖片的背景改成純白」 |

生成的圖預設存 `generated_images/`（已 gitignored）。用 Gemini 2.5 Flash Image / Nano Banana 模型，每張幾秒就好。

## 日常使用

```bash
./bin/agent              # 進互動 REPL 模式
./bin/agent < input.txt  # 批次處理
```

想把 `agent` 加到 PATH：

```bash
echo 'export PATH="$HOME/RED/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
agent
```

## 日常維運

檢查 daemon 狀態：

```bash
launchctl list | grep xiaohong
```

看 log：

```bash
ls ~/RED/var/logs/
tail -f ~/RED/var/logs/daemon-telegram.log
```

重啟某個 daemon（例如 telegram）：

```bash
launchctl kickstart -k gui/$(id -u)/com.xiaohong.telegram
```

### Tool RPC 執行隔離

Telegram daemon 會把部分無狀態診斷工具交給 `com.xiaohong.tool_rpc`，每次 tool call 再開一個 fresh worker subprocess。這樣可以把大型 import、native code hang、記憶體洩漏和程式碼熱更新隔離在單次呼叫裡。

常用設定：

| 變數 | 預設 | 說明 |
|---|---:|---|
| `RED_TOOL_RPC_TELEGRAM` | `1` | Telegram 是否啟用 Tool RPC proxy |
| `RED_TOOL_RPC_PROXY_MODE` | `diagnostics` | `diagnostics` / `safe` / `nonlocked` / `all`；預設只代理診斷工具 |
| `RED_TOOL_RPC_FALLBACK_DIRECT` | `1` | RPC socket 不可用時，client 是否直接開 worker subprocess |
| `RED_TOOL_RPC_DEFAULT_TIMEOUT_S` | `300` | RPC server 預設 tool timeout |
| `RED_TOOL_RPC_TOOL_TIMEOUT_S` | `300` | Telegram proxy 傳給單次 tool call 的 timeout |
| `RED_TOOL_WORKER_TIMEOUT_S` | `300` | direct worker 預設 timeout |
| `RED_TOOL_WORKER_CANCEL_TERM_GRACE_S` | `2` | 使用者取消時，`SIGTERM` 後等待幾秒再升級 |
| `RED_TOOL_WORKER_KILL_GRACE_S` | `2` | `SIGKILL` 後等待 worker 收尾/回收的秒數 |
| `RED_HLS_MAX_VARIANT_CHECKS` | `128` | HLS 下載前最多安全掃描多少個 variant playlist；超過會拒絕下載 |

健康檢查：

```bash
tail -f ~/RED/var/logs/daemon-tool_rpc.log
launchctl kickstart -k gui/$(id -u)/com.xiaohong.tool_rpc
```

## 備份

自動週備份（預設每週日 03:00）：

- 腳本：`launchd/scripts/backup_data_lake.sh`
- plist：`com.xiaohong.backup_weekly`
- 目標：`~/RED_backups/`（保留最近 12 份）

Runtime housekeeping（預設每週日 04:30）：

- 腳本：`launchd/scripts/housekeeping.py`
- plist：`com.xiaohong.housekeeping`
- 內容：清舊 `var/logs`、`var/runs`、`var/workflows`、`var/migrations`

手動備份：

```bash
cd ~/RED
./launchd/scripts/backup_data_lake.sh
```

## 故障排除

### 「找不到 venv Python」
→ 沒跑 setup.sh，或 `.venv/` 被刪了。重跑 `./setup.sh`。

### 「No module named 'scipy' / 'torch'」
→ `pip install` 沒跑完。`cd ~/RED && make install-full`

### launchd daemon 一直 crash
→ 看 `~/RED/var/logs/daemon-*.log`。常見原因：API key 沒設、token.json 過期。

### 系統 Python 3.9 被撞到
→ **永遠用 `./bin/agent`**，不要直接 `python3 agent.py`（會撿到系統 3.9）。

## 檔案結構概覽

```
~/RED/
├── agent.py                 # 主程式 (REPL 入口)
├── agent_daemon.py          # launchd 背景任務 dispatcher
├── bin/agent                # 啟動器（→ 用 .venv/bin/python 跑 agent.py）
├── setup.sh                 # 一鍵安裝器（建 venv + 部署 plist）
├── agent_core/              # 所有核心邏輯（gmail, memory, gemini, 等）
├── launchd/
│   ├── templates/           # plist 範本（裡面寫 @@REPO_ROOT@@ placeholder）
│   └── scripts/             # launchd 呼叫的 Python 腳本
├── .venv/                   # Python 虛擬環境（setup.sh 建）
├── var/
│   ├── logs/                # 所有 daemon 的 log
│   ├── state/               # token / memory / daemon_state / cache
│   ├── data/                # chroma_db / data_lake / quote_history 等
│   ├── runs/                # audit trail + screenshots
│   └── workflows/           # workflow checkpoints
└── skills/                  # 自訂 skill（Excel / PDF / 圖片生成等）
```

如果你是從舊版專案升級，root 下原本的 `memory.json`、`logs/`、`chroma_db/` 等可以用上面的 migration 指令搬進 `var/`。

## 解安裝

```bash
launchctl unload ~/Library/LaunchAgents/com.xiaohong.*.plist
rm ~/Library/LaunchAgents/com.xiaohong.*.plist
rm -rf ~/RED ~/RED_backups
```
