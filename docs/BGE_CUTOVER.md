# bge-m3 本機 embedding cutover SOP

目標：RAG embedding 從 gemini-embedding-001（API、帳單稽核認定的最大 API 成本）
切到本機 bge-m3（M5 MPS、$0 API）。依據 2026-08-01 影子評測（project memory
`local-embedding-eval`）：品質可用（中文實體檢索穩）、fp16+batch64 實測 33.6 docs/s。

**前置條件（未滿足前不得翻旗）**：golden set 盲測（24 題、gemini vs bge 人工
裁決）結果不劣於 gemini。盲測表已生成待填。

## 元件（本 PR 交付，均已測試、未部署）

| 元件 | 路徑 | 說明 |
|---|---|---|
| backend 開關 | `agent_core/embedding_config.py` | `RED_EMBED_BACKEND=gemini\|bge`（預設 gemini、行為不變）；bge 固定 1024 維、collection 後綴 `_bge1024` |
| 共用 embed server | `agent_core/embed_server.py` | bge-m3 單一實體、loopback :8601、`/healthz` `/embed`、server 端 L2-normalize |
| 獨立 venv | `bin/embed-server-venv` | 建 `var/venvs/embed`（torch 2.8/st 5.1.2 釘版；重依賴不進主 .venv） |
| launchd | `launchd/templates/com.xiaohong.embed_server.plist` | KeepAlive daemon，照 chroma server 劇本 |
| 客戶端 | `agent_core/embed_http_client.py` | `RED_EMBED_HTTP_URL`；4xx/5xx raise、絕不墊零向量、np.ndarray 合約 |
| EF 分派 | `vector_store._BackendEF` / `memory._GeminiEmbeddingFunction` | call-time 解析 env；gemini 路徑 byte-for-byte 不變 |
| 背填 | `scripts/backfill_bge_collections.py` | gemini collection → `_bge1024`，冪等可續跑，預設 dry-run |

## 背填矩陣（live 規模，2026-08-10；quote-chain 去重後）

| logical | 源（_768）筆數 | 預估時間 @33.6/s |
|---|---|---|
| drive_docs | 1,263,316 | ~10.4h |
| gmail_threads | 1,146,135 | ~9.5h |
| xiaohong_memory | 25,502 | ~13min |
| google_chat_messages | 9,961 | ~5min |
| operation_sops | 1,313 | ~1min |
| xiaohong_reflections | 1,184 | ~1min |
| skill_cards | 1,157 | ~1min |
| **合計** | **~2.45M** | **~20h**（兩三個夜間窗口） |

⚠️ RED_EMBED_BACKEND 一翻是全域的：七個 collection 都要背填完成才可翻旗，
缺一個 = 該功能翻旗後查空庫（`--logical` 已涵蓋全部七個）。

## 部署順序（每步可獨立回退）

1. **建 venv + 起 server**（不影響現行系統）：
   ```
   ./bin/embed-server-venv
   ./bin/redeploy-daemons embed_server -f        # ⚠️ 必須從主 checkout 跑
   curl http://127.0.0.1:8601/healthz            # {"ok":true,"model":"bge-m3","dim":1024}
   ```
2. **小 collection 背填 smoke**（sops/reflections/skill_cards/chat/memory，合計 <40k、~20min）：
   ```
   cp -c var/data/chroma_db/chroma.sqlite3 /tmp/chroma_src_clone.sqlite3
   BGE_BACKFILL_SRC_DB=/tmp/chroma_src_clone.sqlite3 \
     .venv/bin/python scripts/backfill_bge_collections.py --logical operation_sops --execute
   ```
   （先 dry-run 看計畫；每輪背填前重新 clone 一次 sqlite 取最新快照）
3. **大 collection 背填**（drive/gmail 各 ~10h）：夜間窗口跑、避開 03:00 rag_sync
   高峰亦可並存（embed server 不碰 gemini quota；chroma 寫入由 server 串行化）。
   中斷隨時 Ctrl-C，重跑自動跳過已完成 id。
4. **驗收**：每個 collection `dst.count` ≈ 源 count（誤差 = 空文件跳過數）；
   抽 10 條真實查詢在 `_bge1024` 上肉眼驗 top5。
5. **翻旗**：各 daemon plist EnvironmentVariables 加 `RED_EMBED_BACKEND=bge`
   （或 fleet 共用 env 注入點）→ `./bin/redeploy-daemons`。
6. **觀察期 ≥2 週**：員工查詢品質回饋、embed server 記憶體/延遲
   （`var/logs/daemon-embed_server.log`）。cost.jsonl 的 embed 行應歸零。
7. **收尾（觀察期後、另開 PR）**：評估刪 gemini `_768` collections 釋磁碟。

## 回退

任何時刻：拿掉 `RED_EMBED_BACKEND`（或設回 gemini）→ `./bin/redeploy-daemons`。
gemini `_768` collections 全程唯讀未動，回退即刻生效（分鐘級）。
夜跑增量在 bge 期間寫進 `_bge1024`；回退後 gemini 空間缺的增量由 rag_sync
skip-marker 機制自然補齊（unchanged 檔不動、新檔重抽）。

## 增量與維運

- 夜跑增量：萬 chunks ≈ 5 分鐘（33.6 docs/s），對 7–15h 輪次無感。
- embed server 掛掉：EF raise（不墊零向量）→ 該批 defer，KeepAlive 自動拉回。
  長時間掛 = rag_sync 報 embed 錯誤，同現行 gemini 失敗語意。
- 新機器部署：`bin/embed-server-venv` 冪等重建；模型檔快取 ~/.cache/huggingface。
- 版本升級：先在 worktree 重跑影子評測（scratchpad 腳本見 memory）再動釘版。
