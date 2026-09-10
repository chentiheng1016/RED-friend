# Drive 夜跑平行化設計

> 狀態：**設計草案，待批准**。針對 `agent_core/ingest/drive_sync.py` 的夜跑（`com.xiaohong.rag_sync_daily`，03:00，單輪 7–15h）。
> 這條路徑有多次卡死 / SIGSEGV 事故史，改動極敏感，故採「先無行為改變的結構重構、再 gated 平行化」的分階段策略，每階段獨立可上線、可回滾。

## 1. 目標與現狀

**目標**：把 Drive 夜跑從「完全序列」改為「下載+抽取平行、embed/upsert 單執行緒」，讓網路 I/O 重疊，縮短單輪 wall-clock（最樂觀省數小時）。

**現狀**（序列）：三個 orchestrator —`sync_folder`（[drive_sync.py:2826](../agent_core/ingest/drive_sync.py)）、`sync_shared_drive`（:2923）、`sync_all_drives`（:3026）— 都是 `for f in files: sync_file(...)` 純序列（:2851 / :2954 / :3061）。`sync_file`（:2396-2823）一體式：fast-skip → metadata GET → 下載 → 抽取（`_ExtractPool`）→ embed → upsert → delete_stale → mark_complete → skip-marker。每檔的網路 RTT 零重疊。

**範本**：`gmail_sync.sync_query`（[gmail_sync.py:277-437](../agent_core/ingest/gmail_sync.py)）已實作這個確切方向，drive 照搬即可。

## 2. 硬約束（深讀確認，違反就出事）

| # | 約束 | 證據 | 含意 |
|---|---|---|---|
| C1 | **ChromaDB 必須單一 writer** | vector_store.py:341-344（多 process/thread 直寫同 index → HNSW 腐壞 → upsert SIGSEGV）；多次事故史 | 所有 `upsert_batch`/`delete_stale_chunks`/`mark_doc_sync_complete`/`delete_by_doc_id`/`set_doc_metadata_fields` 必須回主執行緒序列；**embed 嵌在 upsert op 內**（`_GeminiEF`，vector_store.py:311-334），所以 embed 與 upsert 是同一件事、不能拆出來平行 |
| C2 | **共用 service 不能進 worker** | google_auth.py:281-284 親述「googleapiclient transport not thread-safe」；`_service_cache` 無鎖（:29） | worker 必須各自持有 service（thread-local builder） |
| C3 | **`_ExtractPool` 是單一子程序、`run()` 全程持鎖** | drive_sync.py:380-491（`self._lock` 包整個 `run`） | 多 worker 抽取會在這把鎖序列化排隊；純文字/Office 抽取快可接受，image/media（含 Gemini、600s timeout）會成瓶頸 |
| C4 | **單檔三步驟順序** | vector_store.py:1024-1030 / 1057-1060 | `upsert_batch → delete_stale_chunks → mark_doc_sync_complete` 必須同一 commit 單元、對同一 doc 不可與其他寫交錯（中途失敗靠 `sync_complete=False` 自癒） |
| C5 | **skip-state 已 thread-safe，但 defer context 要單一** | drive_sync.py:803-820（generation+雙鎖）、:854-874（`_defer_skip_state_writes`） | marker 寫本身安全；但 `_SKIP_STATE_DEFER_DEPTH` 是全域，defer context 必須只開在主執行緒最外層，worker 不可各開 |

**已 thread-safe、不必動**：skip-state 鎖、cost/quota latch、supported-mimes cache。

## 3. 設計：prepare / commit 拆分（對齊 gmail）

把 `sync_file` 劈成兩半：

```
_prepare_file(file_meta, prefetched, svc)   # worker 執行緒，可平行
    → fast-skip 判斷（純讀，prefetched metadata）
    → metadata GET（C2：用傳入的 per-worker svc）
    → junk/ignored/too_large 閘 → 回 skip-verdict（不寫！）
    → 下載 get_media + _ExtractPool 抽取（C3：抽取會排隊）
    → content-hash + _chunk_text + 組 chunk metadata
    → 回傳純資料 dict：{verdict, doc_id, ids, docs, metas, n_chunks, skip_reason, delete_doc_id}

_commit(prep)                               # 主執行緒，唯一 writer（C1）
    → 依 verdict：
        skip → _record_skip_marker / _clear_skip_marker
        delete → store.delete_by_doc_id + marker
        index → dedup 讀（見 §4 決策）
              → upsert_batch → delete_stale_chunks → mark_doc_sync_complete（C4）
              → _clear_skip_marker
    → results.append(...)
```

orchestrator 改用 gmail 的 `ThreadPoolExecutor + ex.map`（保序、lazy → fetch 與 commit 重疊）：

```python
with _defer_skip_state_writes():                      # C5：單一、主執行緒
    if service_builder and fetch_workers > 1:
        _tls = threading.local()
        def _worker(f):
            svc = getattr(_tls, "svc", None) or service_builder()
            _tls.svc = svc
            return _safe_prepare(f, prefetched, svc)  # 吞單檔錯誤→skip
        with ThreadPoolExecutor(max_workers=fetch_workers) as ex:
            for prep in ex.map(_worker, files):       # 主執行緒消費
                _commit_with_buffer(prep)             # 唯一 writer
    else:
        for f in files:                               # 序列退路
            _commit_with_buffer(_safe_prepare(f, prefetched, service))
    _flush()                                          # 收尾
```

**跨檔 embed buffer**（gmail `_EMBED_FLUSH_CHUNKS=96`，gmail_sync.py:184）：commit 時累積 ids/docs/metas，「整檔加完才檢查 `len >= 門檻`」→ 單檔 chunks 永不跨兩個 flush（delete_stale 正確性所需）。Drive 巨檔可能上千 chunk → 一檔自成一批（語意正確，batch 不均可接受）；門檻可調大。

## 4. 關鍵設計決策

**D1：dedup 讀（`find_duplicate_doc_id`，drive_sync.py:2726，讀 ChromaDB）放 prepare 還是 commit？**
- 放 **commit（序列）**：精確，但 dedup 讀拖慢序列段。
- 放 **prepare（平行）**：快，但兩個 byte-identical 副本同輪同時查不到對方 → 都 upsert（dedup 失效，多嵌一份；**不腐壞**）。
- **建議**：放 commit（正確性優先；dedup 是 indexed `content_hash` SQL fast-path，序列成本低）。若量測證明它拖慢序列，再考慮移 prepare 並接受罕見重複。

**D2：抽取要不要也平行？**
- Phase 1 **不動**：接受 `_ExtractPool` 單子程序序列化（C3）。多 worker 的收益落在「下載 + Drive RPC」重疊。
- 若量測證明抽取（尤其 image/media）是新瓶頸，Phase 2 再評估多子程序池——但 SIGSEGV 史（reference_chroma_*、解析器隔離）使這條**高風險**，預設不做。

**D3：per-worker service 身分（⚠️ 待確認，見 §7）**
- 走 SA：`build_account_service("drive","v3",...)`（google_auth.py:277）— SA creds 無 refresh 競爭，最適合平行。
- 走 OAuth：per-worker 各建 service，但 `_get_valid_creds` 的 refresh / `_service_cache.clear()` 無鎖（google_auth.py:213/222）→ 多 worker 同時 refresh 有 race，要加鎖或預先 refresh 一次再開 pool。

**D4：worker 數 + 記憶體**
- Drive 檔可能很大（`get_media` 下載整檔），N worker 同時下載 = 記憶體 N× 放大（gmail 抓小 JSON 無此問題）。
- 建議 `fetch_workers` 預設保守（**2–4**，低於 gmail 的 6），由 config / env 給；大檔可加 size-based semaphore 節流。

## 5. 分階段（每階段獨立 PR、可上線、可回滾）

**Phase 0 — 等價重構（無行為改變，最高優先）**
- 把 `sync_file` 劈成 `_prepare_file` + `_commit`，三個 orchestrator 改用它們，但**仍序列**（`fetch_workers` 不存在或預設走序列退路）。
- skip-marker 改成 prepare 回 verdict、commit 寫（對齊 gmail），但行為與輸出完全等價。
- **驗收**：既有 `tests/test_drive_sync_*`（content_hash / modified_time / shared_drive / extract_error_skip / binary_extractors / chunk_text）**全綠且無修改**（行為不變的鐵證）；部署觀察一輪夜跑正常。
- 風險：低（純結構）。價值：可讀性 + 為平行鋪路，即使不繼續也值得。

**Phase 1 — gated 平行（預設關）✅ 已實作（`RED_DRIVE_FETCH_WORKERS`，預設 1=序列）**
- `_process_file_list` 統一三個 orchestrator 的檔案迴圈；**序列退路走原 `_sync_file_with_optional_prefetch`（= sync_file），完全等價 Phase 0**（含既有測試的 sync_file mock 點）。
- 平行：`ThreadPoolExecutor` + `ex.map`（保序消費、fetch/抽取與 commit 重疊）；每 worker thread-local 一個 OAuth Drive service（`google_auth.build_drive_service_uncached`，transport 非 thread-safe）；`_safe_prepare_file` 吞單檔錯誤不炸整批；**所有 ChromaDB 寫（`_commit_file`）只在主執行緒 = 單一 writer**。
- **身分確認（前置 D3 已解）**：grep 確認 live(`chroma-shared-server`) 與 `main` 的 Drive RAG 都走 OAuth `get_service("drive","v3")`，**非 SA**（SA 只用在次要 Gmail；記憶 `RED_DRIVE_USE_SA` 與實際 code 不符）。
- **未納入、移 Phase 2**：跨檔 embed buffer（#2）——Phase 1 commit 仍每檔 `upsert_batch`，平行收益落在「下載+抽取重疊」。
- **驗收**：186 既有 `test_drive_sync_*` 綠未改（序列等價）+ 4 個平行單元測試（commit 全在主執行緒=單 writer / prepare 在 worker / 平行=序列保序等價 / worker 錯誤隔離）。
- ⚠️ **真正開平行（`RED_DRIVE_FETCH_WORKERS`>1）前仍需真實 Drive 環境驗證**：OAuth refresh race、N× 下載記憶體放大、`_ExtractPool` 單子程序抽取序列瓶頸（mock 測不出）。
- 回滾：`RED_DRIVE_FETCH_WORKERS=1`（或不設）即序列，**不需 revert code**。

**Phase 2 — 調優（量測驅動）**
- 跨檔 embed buffer（#2，gmail `_EMBED_FLUSH_CHUNKS` 模式，~16x 少 embed RPC）；依實測調 worker 數、加大檔 size 節流；評估 dedup 位置（D1）與抽取池（D2）。只在 Phase 1 真實環境穩定後做。

## 6. 驗證策略（無真實 Drive）

- **Phase 0**：既有 drive_sync 測試全綠（等價性鐵證）。
- **Phase 1**（mock service_builder + mock store，多 thread）：
  - **單 writer**：mock store 的寫方法記錄 `threading.current_thread()`，斷言**所有寫都在主執行緒**（C1）。
  - **順序語義**：斷言每 doc 的呼叫序為 `upsert → delete_stale → mark_complete`（C4）。
  - **單檔不跨 flush**：造多檔含一個多-chunk 檔，斷言其 chunks 同一個 upsert_batch（C4 + delete_stale 正確性）。
  - **錯誤隔離**：一個 worker prepare 拋例外 → 該檔變 skip、其餘檔照常（gmail `_safe_prepare` 模式）。
  - **平行=序列等價**：同一組 mock 檔，`fetch_workers=1` 與 `>1` 產生相同的 upsert 集合（順序可不同，集合相同）。
  - **skip-state defer**：斷言 defer context 只開一次（C5）。

## 7. 待確認（實作前必答）

1. **Drive 身分機制**：這分支 code 全走 `get_service("drive","v3")`（OAuth 共用），但記憶 `project_rag_drive_via_service_account` 說 live 已改 `RED_DRIVE_USE_SA=1`（SA）。**live 到底是哪個？** 決定 D3 的 per-worker service 怎麼建。需對齊 live（可能在 css 分支）。
2. **可接受的 worker 數 / 記憶體上限**：影響 D4。
3. **Phase 1 先在哪個 drive opt-in 測試**（挑小的）。

## 8. 回滾點

- Phase 0：純重構，出問題 `git revert` 單一 commit。
- Phase 1：`fetch_workers=1`（env/config）即時退回序列路徑，無需改碼；最壞 revert Phase 1 commit，Phase 0 仍在。
- 每階段獨立 PR，squash merge。
