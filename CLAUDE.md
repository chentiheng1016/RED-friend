# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 這個 repo 是什麼

小紅（RED）— macOS 上的個人工作助理：Gmail / Drive / Calendar 整合、Gemini 對話與 RAG 記憶、launchd 背景 daemon 艦隊（含 10 色 Telegram bot）、製鞋廠營運分析工具組。**這個 repo 同時就是部署本體**：`~/RED` 主 checkout 的程式碼由 launchd 直接常駐執行，改完 Python 後 daemon 不會自動吃到新碼，要用 `bin/redeploy-daemons <name> --force` 重啟對應服務。

詳細文件：`README.md`（架構總覽）、`MODULES.md`（模組清單）、`SETUP.md`（安裝/部署）、`SECURITY.md`（威脅模型與敏感工具分級）、`agent_core/agents/README.md`（10 色權限矩陣）、`docs/`。

## 常用指令

```bash
make test-quiet          # 全套測試（unittest discover -q，~20 秒）
make lint                # ruff check
make security-audit      # detect-secrets + pip-audit（含 scoped CVE 豁免，見 Makefile 註解）
make install-hooks       # 裝 pre-commit + post-merge hooks

# 跑單一測試檔 / 單一測試
AGENT_DAEMON_MODE=1 .venv/bin/python -m unittest tests.test_drive_sync_content_hash -q
AGENT_DAEMON_MODE=1 .venv/bin/python -m unittest tests.test_chat_sync.ChatSyncTests.test_xxx
```

```bash
# 維運/觀測（都跑 repo 內 .venv，不用先 activate）
bin/red-preflight                           # 開工/部署前先跑：誰在部署(flock)、live 落後幾筆、有無長跑任務、離夜跑多久；exit 3 = 有人在部署
bin/red-status                              # 一行看 daemon 健康/成本/錯誤（= system_status() tool）
bin/red-tiers [locked|dangerous|...]        # 看工具 4 級權限分級
bin/red-web --serve                         # 本機 dashboard（bind 127.0.0.1、唯讀）
bin/redeploy-daemons <name> --force         # 改完 Python 後重啟對應 daemon，完會自動跑 red-smoke
bin/red-smoke --skip-telegram --skip-google # 不觸發外部副作用的 post-deploy smoke
```

- 測試跑的是 **unittest discover，不是 pytest**：`conftest.py` 的 autouse fixture 不會生效，測試隔離要寫在 `setUp`/`tearDown`。
- pre-commit hooks（commit 時自動跑）：merge-conflict 檢查、ruff（只挑 E9/F63/F7/F82）、detect-secrets、smoke tests。
- CI（GitHub Actions）順序：pre-commit → unittest → security-audit（audit 排在測試後、`if !cancelled()`，避免上游 CVE 蓋掉測試訊號）。合併慣例是 squash merge，commit message 用 zh-TW conventional commit（`fix(rag): …`）。

## 架構大圖

**入口**：`agent.py`（互動 REPL，compatibility facade）、`agent_daemon.py`（背景任務 router）、`launchd/templates/*.plist`（daemon 定義，`bin/inject-plist-env` 注入環境變數後部署）。

**工具系統**：`agent_core/tool_registry.py` 組裝工具；built-in 目錄在 `tool_registry_catalog.py`；外掛工具熱載自 `skills/*.py`（每檔 `SKILL_TOOLS`）。10 色部門 agent 框架在 `agent_core/agents/`（`registry.py` + `permission_matrix.py` + 每色一個子套件）。某個 tool call 能不能跑由下面〈權限/政策層〉裁決。

**權限/政策層**：工具分 4 級（`tool_tiers.py`：safe / confirm / dangerous / locked，再 × channel 矩陣）。`policy_engine.evaluate_policy` 是唯一「這個 tool call 能不能跑」的決策入口，判決順序（第一個會 return 的 layer 即定案，見 `evaluate_policy` docstring）：① `RED_BLOCK_TOOL` 封鎖（sysadmin kill switch、最高優先，連 REPL 也禁）→ ② `risk_guard` 內容偵測（args 含 `rm -rf` 之類樣式，score≥90 critical 即 refuse；**先於** LOCKED 判斷）→ ③ tier==LOCKED × channel 不允許即 refuse（voice 對 DANGEROUS 亦然）→ ④ tier×channel 矩陣決定 token / token+warn / allow → ⑤ `RED_FORCE_DRY_RUN_FOR`（強制 dry-run）、⑥ `RED_RAISE_TIER_TO_DANGEROUS`（升級 token+warn）——後兩個 env override 是最後才套、**不是**最高優先（只有 `RED_BLOCK_TOOL` 是）。`tg_auth.py` 發/驗 `+確認` token 並消費這份判決。`mode_manager.py` 管工作情境（normal/meeting/sales/dev/security/quant/cfo，與 channel/tier/intent 正交）。`bin/red-tiers` 看分級。

**前台通道 / Web**：人面前的窗口有三個 — Telegram（owner/admin 主通道，10 色 bot）、LINE（`line_bot.py`，員工窄通道、綁 employee registry）、Web portal（`agent_core/web_server/` FastAPI 員工入口：Google OAuth + 部門聊天 + admin）。`dashboard.py` / `fresh_diagnostics.system_status` 是純文字健康面板（`bin/red-status` 與 `bin/red-web` 同源、都過 `sanitize_for_llm` + `log_redact`）。

**RAG pipeline**（`agent_core/ingest/`）：`rag_runner.py` 是每日 03:00 `com.xiaohong.rag_sync_daily` 的 orchestrator，依序跑 `drive_sync` / `gmail_sync` / `chat_sync` → `vector_store.py` → ChromaDB。兩個大 Drive 共 5 萬+ 檔，單輪 7–15 小時。

**Gemini**：`gemini_client.py` 是唯一 client 入口。`cost_tracker` 用呼叫 stack 推斷 caller 來對帳預算 — Gemini 呼叫要留在擁有該預算的模組內（或顯式傳 caller 標籤）。模型分層：Telegram 前台 `gemini-2.5-pro`、背景 daemon `gemini-flash-latest`。

**runtime 狀態**：一律在 `var/`（`var/state/`、`var/data/`、`var/logs/`、`var/runs/`）。路徑由 `agent_core/logging_and_paths.py` 統一管（含 legacy→var 遷移 helper），不要自己拼路徑。

**部署雙模式**：同一份碼跑兩種型態 — 主力是 macOS launchd daemon 艦隊（`~/RED` 主 checkout 常駐，見本檔開頭那條 redeploy 鐵則）；同一個 image 也能上 Cloud Run，靠 `RED_CLOUD_MODE=1` + `RED_CLOUD_ROLE=web|telegram|task`（`cloud_run_entrypoint.py`、`exchange_policy.py`、`docs/CLOUD_RUN.md`）。所以別假設一定有 launchd 或本機路徑：runtime 走 `RED_RUNTIME_DIR`、員工名單可換 Firestore backend。

## 鐵則（踩過雷的）

- **ChromaDB 一律走共用 HTTP server**（launchd `com.xiaohong.chroma`、`RED_CHROMA_HTTP_URL`、`chroma_backend.build_chroma_client()`）。多個 process 直開同一份 index 目錄會讓 HNSW 段腐壞、之後每次 upsert SIGSEGV。env 沒設時 `build_chroma_client` 會先探測 server heartbeat，活著就拒開 PersistentClient（RuntimeError）；離線維運（server 已停）才用 `RED_CHROMA_ALLOW_DIRECT=1` 明確跳過。embedding function 必須回 `np.ndarray`（list 會炸 HttpClient 查詢）。診斷 daemon `exit -11` 看 `~/Library/Logs/DiagnosticReports/Python-*.ips`，別只信 daemon log 最後一行。
- **`agent_core/env_utils` 是唯一的 `env_int`/`env_float` 來源**（葉模組、只 import os）。不要再寫局部 `_env_int`。
- **任何會呼叫 `drive_sync.sync_all_drives` 的腳本要加 `if __name__ == "__main__"` 守衛**：抽取走 `_ExtractPool` spawn 子程序，會重新 import 主模組。
- **drive_sync 的 skip-marker 負快取**（`var/data/drive_sync_skip_state.json`，以 modifiedTime 為鍵）：無法處理的檔下載前就跳過；summary 數字不變 ≠ 有重抽。
- **郵件/行事曆餵 LLM 前**，每條讀取路徑都要套 `sanitize_for_llm` + `wrap_as_untrusted`（沒有單一咽喉點，別想塞進 extract_body 一處了事）。
- **測試裡不要 hardcode `/Users/user/RED/`**：從 `agent_core/path_safety._REPO_ROOT` 推導，worktree 裡才跑得動。
- **跑整套一定要 `make test-quiet`（＝`discover -s tests -t .`），不能裸 `discover -s tests`**：少了 `-t .` 會把 tests/ 當 top_level_dir、測試模組以 top-level 匯入 → `tests/__init__.py` 不在載入路徑上 → `RED_RUNTIME_DIR` 重導失效 → 整套測試改讀寫部署本體的 live `var/`（實測污染過 `rag_access_audit` ≥43%、`policy_decisions` 8.6%，橫跨三個月）。`agent_core/logging_and_paths._guard_tests_never_touch_live_runtime()` 現在會直接 `os._exit` 擋下來。單一測試用 package 形式 `python -m unittest tests.test_x`。
- **不要在 worker thread 內 mock.patch 全域**（兩個 thread 進出同一個 patch 會把 fake 永久漏進整個 suite）：在主執行緒 patch 一次，fake 用 `*args/**kwargs` 簽名。
- **detect-secrets baseline 以路徑為鍵**：git mv / 大改名後要 `detect-secrets scan --baseline .secrets.baseline` 重掃，否則 hook 紅。
- **launchd `KeepAlive.SuccessfulExit` 隱含 RunAtLoad**：redeploy 會立刻觸發該 daemon 跑一輪（rag_sync 已有守門擋全量夜跑誤觸發，新 daemon 要自己想到這件事）。
- **launchd 的狀態不是即時的，「動完馬上驗」是假的**（2026-08-18 紅 bot 靜默掛掉近 3 分鐘）：被 bootout 的 job 不會立刻從 `launchctl list` 消失 —— 它停在 SIGTERMed，直到程序**真的退出**才 `removing service`，而程序最多能拖到 launchd 的 5 秒寬限期滿被 SIGKILL（8/18 同一個 label 實測：乖乖退的那次延遲 0.67 秒、賴著不退的那次滿 5.02 秒 —— 是「最長 5 秒」不是固定 5 秒）。總之「load 完隔一秒驗一次」必然把一個已經被判死刑的 job 驗成健康。`bin/redeploy-daemons` 因此整批部署完會等 6 秒沉澱再驗一次（`RED_REDEPLOY_VERIFY_SETTLE_S`），不在 list 裡就 bootstrap 救回、救不回 exit 1 並指名 label；plist 沒變又還在跑的 daemon 則改走 `kickstart -k` 就地重啟，label 全程不離開 domain（輸出是 `↻ restarted in place`）。兩個實測、man page 不會講的事實：`kickstart -k` 是 **graceful** 的（先 SIGTERM、5 秒後才 SIGKILL，不是字面上的 kill），而乾淨退出 **不會** 讓 label 離開 domain（留成 `pid=-`）—— 所以「label 整個不見」＝一定有人明確 bootout，查兇手用 `/usr/bin/log show --predicate 'processID == 1'` 看 `booting out service: caller = ...` 的祖先鏈（⚠️ `log` 被 zsh function 蓋掉，要寫 `/usr/bin/log`）。
- **會讀 `launchctl list` 又回頭動 launchctl 的觀察者，判讀前一定要重採、修復只能非破壞性**：redeploy 的 unload→load 中間有約 1 秒空窗，健檢／看門狗在那一秒取樣就會誤判「daemon 沒被載入」。8/18 就是 `health_check` 的 auto-repair 照著誤判送 `unload`，把 redeploy 在 1.6 秒前才拉起來的健康 bot 殺掉（更慘的是 `subprocess(timeout=5)` 先炸 → 後面那行 `load` 根本沒跑到 → label 就這樣消失）。修復動作一律 `enable`+`bootstrap`（對「其實已經載入」只回無害錯誤，不會殺掉任何東西），**絕不 unload**；成敗看 `launchctl list`，不看 `load` 的 exit code —— 它印「Load failed」也照樣回 0。
- **daemon 的 SIGTERM handler 只設旗標不夠，每個阻塞點都要能被打斷 —— 而且手法隨阻塞的種類而不同**：launchd 只給 5 秒，但旗標只在主迴圈頂端被檢查，訊號打在 `getUpdates` 長輪詢（`RED_TG_LONGPOLL_TIMEOUT_S` 預設 8 秒）或網路退避 sleep 中途就得等它跑完 —— 實測 3 天 7 次被 SIGKILL、offset 沒存。兩個阻塞點治法不一樣（`agent_core/daemon_telegram.py`）：長輪詢靠 handler 丟 `_ShutdownInterrupt`，真訊號實測能把阻塞中的 socket read 當場打斷（#441）；`time.sleep` **不能**這樣治 —— PEP 475 下它被訊號中斷後會拿剩餘時間重睡，所以改成切片的 `_sleep_unless_shutdown()`、每 `_SHUTDOWN_POLL_SLICE_S` 檢查一次旗標（#442）。三個共通的坑：①旗標只圍住那一個呼叫，SIGTERM 打在處理訊息途中仍要讓那則做完（中途砍＝弄丟使用者的回覆）；②旗標用一元素 list 不用 `Event` —— signal handler 裡拿鎖可能跟被它中斷的同一條執行緒對撞；③打斷用的例外**必須繼承 `BaseException`**，迴圈底部的廣捕 `except Exception` 會把它當網路錯誤吞掉重試。
- **repo-wide 改寫腳本要排除 `.claude/worktrees/`**，否則會把其他 Claude session 的工作樹一起改掉。
- **`factory_*.py`（brain / supply_chain / quality_control / predictive_analytics / data_integration）已從工具目錄移除**：它們讀 `var/factory_data` 自存 JSON、沒接 email lake / Drive 真實資料，別當活的查詢工具掛回去。模組保留只因 `yellow_procurement` 子 agent 仍直接 import `factory_supply_chain`。
- **merge `origin/main` 進部署分支（css）後必跑 `make lint` + `make test-quiet`**：兩邊改過同一區塊時，merge 可能把新舊兩版並排保留（同名函式後者遮蔽前者 = 靜默還原，F811 才抓得到）。merge 自動產生的 commit 走 `pre-merge-commit` hook 而非 `pre-commit`，hook 要用 `make install-hooks` 裝齊兩個 stage。

## Claude Code worktree 注意

`.pre-commit-config.yaml` hardcode 了 `.venv/bin/python`：新 worktree 第一次 commit 前要先 `ln -s /Users/user/RED/.venv .venv`。
