# RED

小紅（RED）是一個以 Python 為主的個人工作助理 / daemon 專案，整合了：

- Gmail / Google Drive / Google Calendar
- Gemini 對話、摘要、分類、RAG 記憶
- launchd 背景任務
- OCR / GUI 自動化 / MCP 擴充

這個 repo 現在已經有：

- 可重複安裝的 `setup.sh`
- `var/` runtime 目錄收斂
- `Makefile`
- `pre-commit` / `post-merge` hooks
- GitHub Actions CI

## Quick Start

```bash
git clone https://github.com/chentiheng1016/RED-friend.git ~/RED
cd ~/RED
./setup.sh
./bin/agent
```

如果你是開發者：

```bash
./setup.sh --dev
make install-hooks
make test
```

如果你需要完整 heavy 功能（GUI / OCR）：

```bash
./setup.sh --full
```

## Project Layout

```text
RED/
├── agent.py              # 互動入口
├── agent_daemon.py       # 背景任務主入口
├── agent_core/           # 核心模組
├── skills/               # 額外技能 / 工具
├── launchd/              # plist templates + scripts
├── bin/                  # 啟動 / 維運 helper
├── tests/                # unittest
├── var/                  # runtime state / data / logs / runs / workflows
└── .github/workflows/    # CI
```

`var/` 目前是標準 runtime 位置：

- `var/logs/`
- `var/state/`
- `var/data/`
- `var/runs/`
- `var/workflows/`

## Architecture

目前程式結構的主軸是：

- [`agent.py`](./agent.py)：互動入口 + compatibility facade。對外保留穩定 public surface，主要邏輯已盡量下放到 `agent_core/`
- [`agent_daemon.py`](./agent_daemon.py)：背景任務入口 / task router。實際 task 邏輯已拆到較小模組
- [`agent_core/tool_registry.py`](./agent_core/tool_registry.py)：工具組裝器；built-in tool catalog 與包裝規則已拆到 [`agent_core/tool_registry_catalog.py`](./agent_core/tool_registry_catalog.py)
- [`agent_core/internal_emails.py`](./agent_core/internal_emails.py)：內部郵件子系統入口；細節已拆成 preview / extract / store / orchestrator

幾個這輪整理後最重要的模組邊界：

- daemon Telegram：
  [`agent_core/daemon_telegram.py`](./agent_core/daemon_telegram.py)
- daemon dispatcher：
  [`agent_core/daemon_dispatcher.py`](./agent_core/daemon_dispatcher.py)
- daemon email ingest：
  [`agent_core/daemon_email_ingest.py`](./agent_core/daemon_email_ingest.py)
- daemon ponder：
  [`agent_core/daemon_ponder.py`](./agent_core/daemon_ponder.py)
- internal email preview：
  [`agent_core/internal_emails_preview.py`](./agent_core/internal_emails_preview.py)
- internal email extraction：
  [`agent_core/internal_emails_extract.py`](./agent_core/internal_emails_extract.py)
- internal email storage：
  [`agent_core/internal_emails_store.py`](./agent_core/internal_emails_store.py)
- internal email orchestration：
  [`agent_core/internal_emails_orchestrator.py`](./agent_core/internal_emails_orchestrator.py)
- cloud exchange policy：
  [`agent_core/exchange_policy.py`](./agent_core/exchange_policy.py) keeps Telegram as the normal human-facing window in cloud mode while artifacts live in Red-owned storage.

如果之後要重構，優先原則是：

- 入口檔保持薄，只做 wiring / facade / dispatch
- 真正的流程邏輯放進 `agent_core/` 下的專責模組
- 相容層名稱先保留，再慢慢縮小 public surface

如果你是從舊版升級，可以把 root 舊資料搬進 `var/`：

```bash
./bin/migrate-runtime
./bin/migrate-runtime --apply
```

## Common Commands

```bash
make install        # lean default bundles
make install-full   # all optional bundles
make install-dev    # runtime + dev tools
make install-hooks  # install pre-commit + post-merge hooks
make test
make test-quiet
make lint
make node-test         # Node/Puppeteer import smoke
npm run smoke:node:launch
npm ci
npm test              # Node/Puppeteer dependency smoke
./bin/red-smoke       # post-deploy smoke, sends one Telegram ping
./bin/red-smoke --skip-telegram --skip-google
make red-smoke-local  # smoke without Google/Telegram side effects
RED_SKIP_POST_DEPLOY_SMOKE=1 ./bin/redeploy-daemons --force  # redeploy without auto smoke
./bin/redeploy-daemons --force --strict-smoke                # fail redeploy on smoke failure
RED_DEPLOY_ENV=production ./bin/redeploy-daemons --force     # production defaults to strict smoke
./bin/redeploy-daemons --rollback-last                       # restore latest plist backups + smoke
make migrate-runtime
make housekeeping
```

`./bin/red-smoke` writes each run to `var/runs/post_deploy_smoke/` as JSON.
Housekeeping prunes those smoke records after `RED_SMOKE_RETENTION_DAYS` days
(default: 60). Smoke failures send a Telegram alert unless
`RED_SMOKE_NOTIFY_FAILURE=0` or `--no-failure-notify` is used.

## Quality Gates

本地：

- `pre-commit`
- `ruff`
- `pip-audit`
- `detect-secrets`
- `unittest`

雲端：

- GitHub Actions 會跑 `pre-commit`
- GitHub Actions 會跑 Python dependency audit / secrets scan
- GitHub Actions 會跑 Node/Puppeteer launch smoke
- GitHub Actions 會跑整包 `unittest`

手動驗證：

```bash
./.venv/bin/pre-commit run --all-files
make security-audit
make node-launch-smoke
AGENT_DAEMON_MODE=1 ./.venv/bin/python -m unittest discover -s tests -q
```

## Docs

- 完整安裝與維運說明：[`SETUP.md`](./SETUP.md)
- Cloud Run 程式部署約定：[`docs/CLOUD_RUN.md`](./docs/CLOUD_RUN.md)
- 防幻覺防線設計（5 層、debug 步驟、新增 incident SOP）：[`docs/anti_hallucination.md`](./docs/anti_hallucination.md)
- CI：[`/.github/workflows/ci.yml`](./.github/workflows/ci.yml)
- pre-commit：[`/.pre-commit-config.yaml`](./.pre-commit-config.yaml)

## Notes

- 目標環境目前是 macOS。
- Google OAuth、Gemini API key、Telegram token 等敏感設定不會由 repo 自動提供。
- 有些功能依賴較重，預設不會在 `setup.sh` 安裝完整 bundle，請依需求加 `--full`。
