# Contextual Retrieval 設計（chunk 前綴：靜態標題 → LLM 文件級脈絡）

> 狀態：**設計草案，待批准**。針對 `agent_core/ingest/` 三個 sync（drive / gmail / chat）共用的 chunk→embed 路徑。
> 動機：三個 sync 的 chunk 現在是 `"[標題] " + 正文視窗`，一段「保固期 12 個月」在 embedding 層失去它屬於「ABC合約_2026Q1.docx」的連結，傷害客戶名 / spec 名 / 料號類查詢的召回。Anthropic 的 Contextual Retrieval 做法是把這段前綴從「只有標題」升級成「LLM 生成的一段文件脈絡」，再連同正文一起 embed。
> 核心原則：**文件級 context 為主、chunk 級為輔**（成本差兩個數量級）；**天然可逆**（env flag 一關退回舊前綴）；**不加重夜跑**（context 生成是額外 LLM 呼叫，故 gated + 離峰 backfill 為主，仿 [pdf_ocr_fallback_design.md](pdf_ocr_fallback_design.md)）。

這是知識管理進階路線圖的 **Phase 1（能力①）**；後續 ⑤記憶分層 / ④階層反思 / ⑥Self-RAG / ②GraphRAG / ③Temporal KG 另有設計。本 doc 只涵蓋 Contextual Retrieval。

## 1. 目標與現狀

**目標**：讓每個 chunk 的 embedding 攜帶「這段話出自哪份文件、什麼脈絡」，提升 `drive_docs` / `gmail_threads` / `google_chat_messages` 的檢索召回，且**不長期加重已經跑不完的夜跑**（見夜跑 503 瓶頸診斷、[drive_sync_parallelization_design.md](drive_sync_parallelization_design.md)）。

**現狀**（chunk→embed 路徑，三來源鏡像）：

- Chunk 切分是**純字元滑動視窗**（非 token、非語意）：`CHUNK_SIZE=600` / `CHUNK_OVERLAP=80`。
  - Drive：`_chunk_text(text, title="")`（[drive_sync.py:1196](../agent_core/ingest/drive_sync.py)），常數 [:78-79](../agent_core/ingest/drive_sync.py)，呼叫點 [:3384](../agent_core/ingest/drive_sync.py) `_chunk_text(text, title=meta.get("name", ""))`。
  - Gmail：`_chunk_text(text, subject="")`（[gmail_sync.py:101](../agent_core/ingest/gmail_sync.py)）。
  - Chat：`_chunk_text(text, space_label="")`（[chat_sync.py:383](../agent_core/ingest/chat_sync.py)）。
- 每個 chunk 的 text = `prefix + 視窗原文`，`prefix = f"[{safe_title}] "`，且 `payload_size = max(1, CHUNK_SIZE - len(prefix))`（[drive_sync.py:1211-1213](../agent_core/ingest/drive_sync.py)）——**前綴會吃掉 payload 空間**。docstring 明講此舉是「讓 embedding 攜帶 document-level context」（[:1197-1206](../agent_core/ingest/drive_sync.py)）：也就是說**架構已經預留了這個接口，我們只是把前綴內容變聰明**。
- 寫入 ChromaDB：`store.upsert_batch(ids, docs, metas)`（[vector_store.py:521](../agent_core/ingest/vector_store.py)），**embedding 在 `col.upsert(documents=)` 內部由 `_GeminiEF` 自動算**（[vector_store.py:347-370](../agent_core/ingest/vector_store.py)），模型 `gemini-embedding-001`（[:57](../agent_core/ingest/vector_store.py)），維度由 `RED_EMBED_DIM` 決定（768 / 3072，[embedding_config.py:48-59](../agent_core/embedding_config.py)），768 模式 collection 加 `_768` 後綴（[:85-91](../agent_core/embedding_config.py)）。
- chunk_id：Drive `f"{file_id}__c{i}"`（[drive_sync.py:3408](../agent_core/ingest/drive_sync.py)）、Gmail `f"{thread_id}__c{i}"`、Chat `f"{space_name}__{anchor}__c{i}"`。
- 增量 gate（決定要不要重嵌）：Drive 兩道——modifiedTime gate（下載前，[:3032-3045](../agent_core/ingest/drive_sync.py)）+ content-hash gate（抽取後，[:3306-3337](../agent_core/ingest/drive_sync.py)）；**`content_hash = sha256(text)[:16]` 是對「原始抽取文字」算的、不含 chunk 前綴**（[:3317](../agent_core/ingest/drive_sync.py)）。Gmail 比 `history_id`（[gmail_sync.py:270](../agent_core/ingest/gmail_sync.py)）；Chat append-only 無 hash。
- eval 基建：`run_eval(methods, category)`（[eval_rag.py:271](../agent_core/eval_rag.py)），golden set 在 `var/data/eval/golden_set.json`（[:44-45](../agent_core/eval_rag.py)），算 recall@5/10/20 + MRR（[:142-151](../agent_core/eval_rag.py)）。

## 2. 硬約束（違反就出事 / 白做工）

| # | 約束 | 證據 | 含意 |
|---|---|---|---|
| C1 | **加 context 前綴不會觸發重嵌** | `content_hash` 對原文算、不含前綴（[drive_sync.py:3317](../agent_core/ingest/drive_sync.py)）；title 相符也 skip（[:3321](../agent_core/ingest/drive_sync.py)） | 光把前綴變聰明，兩道 gate 都判「沒變」→ 舊檔永遠不重嵌。**必須新增一個版本維度（`ctx_ver`）主動令 fast-skip 失效**，仿 PDF OCR 的 `_skip_marker_matches` 重試閘（[:1052-1056](../agent_core/ingest/drive_sync.py)） |
| C2 | **context 生成 = 額外 LLM 呼叫，會加成本 + 加夜跑負擔** | embed 已是夜跑瓶頸（503 累積）；記憶 `project_rag_nightly_bottleneck_20260627` | context 生成掛在抽取後、embed 前。夜跑 inline 開 = 每個新/改檔多一次 LLM。**故 inline 預設關、主力走離峰 backfill**（同 PDF OCR 的 C1） |
| C3 | **ChromaDB 單一 writer** | 多 process 直寫 → HNSW 腐壞 SIGSEGV，多次事故；記憶 `reference_chroma_server_backend` | 重嵌 backfill **不可與夜跑同時跑**（搶 writer）。沿用「先確認夜跑結束、writer 空出」原則（同 PDF OCR 的 C2） |
| C4 | **前綴吃 payload、且改前綴會動 chunk 邊界** | `payload_size = CHUNK_SIZE - len(prefix)`（[:1213](../agent_core/ingest/drive_sync.py)） | LLM context（~100-150 字）遠長於標題。若沿用「前綴計入 CHUNK_SIZE」→ payload 被擠爆、chunk 數暴增。**設計改為 context 外掛、payload 維持 600**（見 §3.2），embed input budget 夠（gemini-embedding-001 吃 2048 token，150+600 remaining 遠低於上限） |
| C5 | **768 維 + `_768` collection + L2 normalize 要沿用** | [embedding_config.py:62-91](../agent_core/embedding_config.py) | context 不影響維度路徑；backfill 重嵌一樣過 `maybe_normalize`。手動 backfill 環境**必帶 `RED_EMBED_DIM=768`**（否則寫進 3072 collection，記憶 `project_rag_misnamed_xls_fallback` 教訓） |
| C6 | **沿用 `sync_file` 全套、`_ExtractPool` spawn 守衛** | [drive_sync.py:33](../agent_core/ingest/drive_sync.py)；記憶 `feedback_extract_pool_spawn_main` | backfill 走 `sync_file` 不自刻 upsert（承接 skip/purge/dedup/single-writer）；腳本要 `if __name__=="__main__"` 守衛 |

## 3. 設計

分三部分：**(A) context 生成 helper** ＋ **(B) chunk 前綴升級（外掛式）** ＋ **(C) `ctx_ver` 版本閘 + 離峰 backfill**。

### 3.1 (A) 文件級 context 生成 helper

新增 `agent_core/ingest/contextualize.py`（葉模組，三來源共用）：

```
def gen_doc_context(full_text, title, mime, source) -> str:
    1. 未啟用（RAG_CONTEXTUAL_RETRIEVAL False）→ 回 ""（呼叫端 fallback 回 [title] 舊前綴）
    2. full_text 截到 RAG_CTX_MAX_DOC_CHARS（預設 12000，防巨檔爆 token/成本）
    3. flash-lite 一次呼叫（RAG_CTX_MODEL，預設 gemini-2.5-flash-lite）：
       prompt =「用 1-2 句(≤80 字)描述這份文件是什麼、屬於誰/哪個客戶或專案、
                 涵蓋什麼——只根據內文，不要臆測。回純敘述，不要開場白。」
    4. 失敗 / 空 / 逾時 → 回 ""（degrade gracefully，絕不阻塞夜跑；同熱路徑鐵則）
    5. 成本記帳走 cost_tracker（caller 標籤 "rag_contextualize"）
```

- **為何文件級（每檔一次）而非 chunk 級（每 chunk 一次）**：見 §4 成本表——文件級全庫 ~$25、chunk 級無 prompt caching 不可行。文件級拿到八成好處、成本低兩個數量級。
- **chunk 級留給高價值子集**：合約 / spec sheet / 報價單等（未來 gated by `RAG_CTX_LEVEL=chunk` + 部門白名單 + prompt caching 攤平文件重送成本），本 Phase 不實作。
- **context 快取**：生成結果隨檔存一次（見 §3.3 `ctx_text` 入 skip-state / metadata），同檔多 chunk 共用、重嵌時不重生成。

### 3.2 (B) chunk 前綴升級（外掛式，payload 不縮）

改三個 `_chunk_text` 簽名為 `_chunk_text(text, title="", context="")`：

```
prefix = f"{context}\n\n" if context else (f"[{safe_title}] " if safe_title else "")
# 關鍵改動：payload_size 不再扣 len(prefix)，固定用 CHUNK_SIZE
#   → chunk 邊界只由正文決定，context 有無/長短都不動 chunk 切法（C4）
#   → 同一檔重嵌時 chunk_id __c{i} 對齊，upsert 直接覆蓋、delete_stale_chunks 乾淨
payload_size = CHUNK_SIZE
```

- context 有值就取代 `[title]`；為空則**完全沿用今日行為**（flag 關 = 零差異）。
- embed input = context(~150) + payload(≤600) ≈ 750 字 ≪ 2048 token 上限（C4）。

### 3.3 (C) `ctx_ver` 版本閘 + 離峰 backfill

**metadata / skip-marker 新欄位**：chunk metadata 與 skip-marker 都加 `ctx_ver`（int，模組常數 `CONTEXTUAL_VER`，初版 = 1；未來改 prompt / 改 level 就 bump）。

**令 fast-skip 對「舊版 context」失效**（繞過 C1 的兩道 gate）：

```
# content-hash gate（drive_sync.py:3306-3337）加一條：
if existing.get("ctx_ver", 0) < CONTEXTUAL_VER and RAG_CONTEXTUAL_RETRIEVAL:
    → 不判 content_unchanged，照常重抽→重嵌（此時正文 hash 沒變、但要換 context 前綴）
# modifiedTime gate（:3032-3045）同理加 ctx_ver 檢查，否則下載前就被擋掉
```

**離峰一次性 backfill 腳本** `tools/backfill_contextual.py`（不進夜跑熱路徑、手動離峰跑，仿 PDF OCR 3.3）：

```
前置：確認無夜跑在跑（rag_sync.lock PID 已死 / status!=running）→ 不搶 writer（C3）
環境：RAG_CONTEXTUAL_RETRIEVAL=1 RED_EMBED_DIM=768（C5）
候選：對指定 drive_id / 部門列檔，挑 metadata ctx_ver < CONTEXTUAL_VER 的 doc
逐檔：sync_file(file_id, drive_id=…) → 內部走 gen_doc_context → 新前綴 chunk → 重 embed → upsert
特性：可續跑（已 backfill 者 ctx_ver 達標、下輪 fast-skip）、限速、每 N 檔印進度、
      會計/採購/合約優先（查詢價值最高）；if __name__=="__main__" 守衛（C6）
```

### 3.4 (D) eval：補上量測 drive_docs 的能力（目前的真空）

現有 `run_eval` **只評測 gmail thread / `xiaohong_memory`，量不到 `drive_docs`**（method 都打 `memory.recall`，`memory.py:44` `_VECTOR_COLLECTION="xiaohong_memory"`；命中判定用 `thread_id` regex、golden 欄位 `expected_thread_ids`）。所以要驗證 Contextual Retrieval 對 Drive 的提升，需二選一：

- **路徑 A（快、先驗概念）**：先把 contextual 前綴套在 **gmail_threads** 路徑，用現成 golden set 跑 `run_eval` A/B（`ctx_ver=0` vs `1`），拿到 recall/MRR 前後對比。gmail 是現成量測管道，證明「context 前綴確實提升召回」最省事。
- **路徑 B（完整、Drive 才是主場）**：為 `drive_docs` 補一組 golden queries（部門代表性問句 + `expected_doc_id`）+ 一個打 `drive_search.py`（[:26](../agent_core/ingest/drive_search.py) `_MAX_CHUNK_CHARS=600`）的 eval method。這是把 eval 從「只懂 gmail」擴到「懂 Drive」的一次性投資，⑥Self-RAG 之後也用得到。

建議：**Phase 2 先做路徑 A 驗證概念**，Phase 3 backfill 前補路徑 B 做部門級 A/B。

## 4. 效率與成本（誠實版）

| 項 | 影響 |
|---|---|
| context 生成（文件級） | flash-lite $0.025/$0.10 per M token。全庫 input ≈ 所有正文 ~10 億 token → **~$25 一次性**；output 5 萬檔×80 字 ≈ $0.5。增量每檔 ~$0.0005，可忽略 |
| context 生成（chunk 級，本 Phase 不做） | 無 prompt caching 需每 chunk 重送全文 → 數百美元起跳，故**留給高價值子集 + caching** |
| **重嵌 embedding（真正大頭）** | backfill 全庫 = 580 萬 chunk 重新過 `gemini-embedding-001`，這是 embedding API 成本 + 與夜跑搶 503 throughput。**故離峰、分批、部門優先，不一次全庫** |
| CPU / 夜跑 | inline 開 = 每檔多一次 LLM 往返，夜跑更慢；**預設關就零影響** |
| 對人查找 | 客戶名 / spec / 料號類查詢召回提升——**對人的效率正向** |

結論：**context 生成本身便宜（~$25），真正的成本與風險在「重嵌整庫」**。正確順序＝先在 gmail 用現成 eval 驗概念 → 再補 drive eval → 再離峰分部門 backfill，別為了「全庫一次到位」去撞夜跑 embed 瓶頸。

## 5. 設定旗標（全部 env、預設保守）

| Flag | 預設 | 作用 |
|---|---|---|
| `RAG_CONTEXTUAL_RETRIEVAL` | `0` | 總開關（inline 生成 + `ctx_ver` 重試閘）。backfill 腳本內部自設 1 |
| `RAG_CTX_MODEL` | `gemini-2.5-flash-lite` | context 生成模型 |
| `RAG_CTX_MAX_DOC_CHARS` | `12000` | 讀進生成的正文上限（防巨檔爆 token） |
| `RAG_CTX_LEVEL` | `doc` | `doc`（每檔一次）/ `chunk`（每 chunk，未實作，需 caching + 白名單） |
| 沿用 `RED_EMBED_DIM` | `768`(live) | 重嵌維度，手動 backfill 必帶（C5） |

模組常數 `CONTEXTUAL_VER=1`（改 prompt / 改 level 就 bump → 觸發下輪 backfill 重嵌）。

## 6. 分階段上線

1. **Phase 1**（本設計）：`contextualize.gen_doc_context` helper + 三 `_chunk_text` 加 `context` 參數（外掛式、payload 不縮）+ `ctx_ver` 進 metadata/skip-marker + 兩道 gate 的 `ctx_ver` 重試閘 + flag（**預設關**）。單元測試：flag 關時 chunk 完全不變（逐字比對舊行為）、flag 開時前綴替換且 payload 維持 600、context 生成失敗 fallback 回 `[title]`、`ctx_ver` 閘對「舊版 vs 達標」的 skip 判斷、chunk_id 對齊。**對 live 零影響**（預設關）。
2. **Phase 2**：gmail 路徑套 contextual + `run_eval` A/B（`ctx_ver` 0 vs 1），拿 recall@k / MRR 數字證明召回提升；不夠好就回頭調 prompt（bump `CONTEXTUAL_VER`）。
3. **Phase 3**：補 `drive_docs` golden set + drive eval method（§3.4 路徑 B）；實作 `tools/backfill_contextual.py`，**離峰**對會計（`0AFFZvSO`）先跑一批，量部門級提升與耗時/成本。
4. **Phase 4**：視提升與 embed throughput，決定是否 inline 常開 + 排程分部門全庫 backfill。

## 7. 風險與待決

- **context 幻覺**：LLM 可能替文件編出不存在的脈絡 → prompt 限「只根據內文、不臆測」，且 context 只進 embedding 影響排序、**不改變原文呈現**（citation 仍回原 chunk）。Phase 2 抽驗。
- **召回不升反降**：若 context 太泛（「這是一份表格」）反而稀釋語意 → 用 eval A/B 把關，`CONTEXTUAL_VER` 可隨時 bump 換 prompt 重跑。
- **重嵌撞夜跑**：backfill 搶 writer → 沿用 C3 的 lock 協調，離峰跑。
- **成本失控**：context 生成走 cost_tracker + 月 cap；`RAG_CTX_MAX_DOC_CHARS` 擋巨檔。
- **待決**：inline 是否永久常開？取決於 embed 503 throughput 是否先解（同 PDF OCR 的待決，見 `project_rag_nightly_bottleneck_20260627`）。chunk 級 context 何時值得對哪些部門開？待 Phase 2/3 的 A/B 數字定奪。

## 8. Phase 2 gmail A/B eval 實測結果（2026-07-19）

> 結論先講：**gmail 上 contextual 前綴相對現行 `[subject]` 前綴，召回增益可忽略**（recall 完全相同、MRR +0.007）。**不建議在 gmail 開 flag / backfill**；contextual 的價值待在 **drive_docs** 上驗（§3.4 路徑 B，另需建 drive golden set）。Phase 1 code 保持 flag 關（零風險）是對的。

**為何不走設計原案的 `run_eval` A/B**：實測發現 `var/data/eval/golden_set.json` **根本不存在**、且沒有 `bootstrap_golden_set`；`run_eval` 預設打的是 `memory.recall`（`xiaohong_memory`）。所以「用現成 golden set 跑 A/B」的前提不成立。改用**離線自建 A/B**（不依賴人工 golden set、不碰 chroma writer、不與夜跑搶 single-writer）。

**方法（離線簡化 A/B，隔離唯一變數＝前綴文字）**：
- 從 live `gmail_threads_768` 抽 N 個 thread，撈其 chunks（現況都是舊 `[subject]` 前綴），剝掉前綴得 payload。
- A/B 用**完全相同的 payload**，只換前綴：A = `[subject] payload`（現況）；B = `<LLM context>\n\n payload`。chunk 一一對應、payload 逐字相同。
- 每個 thread 用 LLM 產一個搜尋式 query（expected = 該 thread）；兩組各自 embed（gemini-embedding-001, 768, L2 正規化），純記憶體 cosine top-k → thread，算 recall@5/10/20 + MRR。
- 腳本：`scripts/eval_contextual_ab_gmail.py`（唯讀、不碰 chroma writer；未來 drive eval 可基於同一套方法泛化）。用法：`.venv/bin/python scripts/eval_contextual_ab_gmail.py [POOL] [NQ]`。

**兩輪結果**：

| 輪 | 設定 | recall@5 (A/B) | MRR (A/B) |
|---|---|---|---|
| 1 | 39 threads、含單號的精確 query | 1.000 / 1.000 | 0.987 / 1.000 |
| 2 | 195 threads pool、**去單號的語意 query**（「迪卡儂布重超重」而非「LOT211 PU467」） | 1.000 / 1.000 | 0.882 / 0.889 |

**判讀**：
- 兩輪 recall 都撞天花板且 A==B。放大 pool、把 query 從「精確單號」改成「只記情境的語意查詢」都沒讓兩組分離。
- 根因＝**gmail 的 subject 本身就是強力文件級脈絡**（含客戶名/主題/單號）。靜態 `[subject]` 前綴已把最有定位價值的資訊帶進每個 chunk 的 embedding，LLM context 能加的邊際極小。這正是 §3.4 說「gmail 只是現成量測代理、contextual 主場是 drive」的實證。
- MRR<1.0 證明檢索確實在排序（非 trivial 全命中 bug）；B 只是偶爾把 rank-2 拉到 rank-1。

**本實驗的限制（結論僅為方向性信號）**：① 天花板效應——gmail thread 主題太獨特、pool 再大仍易區分，真要離開天花板需數千 pool + 同客戶同主題的強競爭 thread；② query 為 LLM 自動產、非真實用戶 query；③ 簡化 A/B 只隔離「前綴文字」，未測 B 組「payload 不縮」的額外增益（該增益也主要在長文件＝drive，gmail thread 通常僅 1–3 chunk）。

**修正後的下一步（取代原 Phase 3 的排序）**：
1. contextual 的價值要在 **drive_docs** 上驗才有意義——但那需要先建 **drive golden set**（部門代表問句 + `expected_doc_id`）+ 一個打 `drive_search` 的 eval method（§3.4 路徑 B）。這是獨立且較大的工作（golden set 需領域知識或另設自動產）。
2. 在有 drive 正面證據前，**Phase 1 flag 保持關**，不做任何 backfill（415 萬 gmail chunk 重嵌成本大、增益近零）。
3. 若日後在 drive 上證實有效，backfill **只跑 drive、離峰、分部門**，gmail 可跳過。
