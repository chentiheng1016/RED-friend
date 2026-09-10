# 掃描 PDF OCR 退路設計（PDF 空文字層 → Vision OCR）

> 狀態：**設計草案，待批准**。針對 `agent_core/ingest/drive_sync.py` 的 PDF 抽取路徑。
> 動機：2026-06-27 會計/採購覆蓋率診斷發現會計 Drive 約 4–5 成的缺口是**掃描型 PDF**（合約 / 財報 / 匯款單），`_extract_pdf` 只抽文字層、抽到空就 `empty_text` 跳過、永不重試。生管部門單一硬碟最近一輪也有 **1,572 個 `empty_text`**，全 fleet 規模可觀。
> 核心原則：**OCR 是涵蓋率強化，不是夜跑加速**——OCR 本身免費本地（ocrmac），但 OCR 出的文字仍要 embed，會加重現有 Gemini 503 瓶頸。故設計以「離峰一次性 backfill 為主、夜跑 inline 退路為輔且預設關」。

## 1. 目標與現狀

**目標**：讓掃描型 PDF 的內容變成可被 RAG 搜尋，且**不長期加重已經跑不完的夜跑**（見 [drive_sync_parallelization_design.md](drive_sync_parallelization_design.md) 與夜跑 503 瓶頸診斷）。

**現狀**（PDF 抽取路徑）：

- `_extract_pdf(data)`（[drive_sync.py:1154](../agent_core/ingest/drive_sync.py)）只用 `pypdf` 逐頁 `page.extract_text()`，**無 OCR 退路**。純掃描 PDF（無文字層）→ 回空字串。
- 空字串 → `_prepare_file` 記 `empty_text` skip（[:2821](../agent_core/ingest/drive_sync.py)）並**寫 skip-marker**（:2824）。
- `_skip_marker_matches` 對 `empty_text` 是 `pass`（[:966](../agent_core/ingest/drive_sync.py)）→ 只要檔案 `modifiedTime` 沒變就**永遠跳過**、不重抽。

**已存在、可直接重用的 OCR 基礎**（目前只接圖片 mime、沒接 PDF）：

- `_extract_image_vision(data, mime_type)`（[:1823](../agent_core/ingest/drive_sync.py)）：丟圖片 bytes → macOS `ocrmac` OCR → 回文字（< 門檻或非 macOS 回 `None`）。
- `_VISION_OCR_ENABLED`（預設 True）、`_VISION_OCR_MIN_CHARS=24`、`_VISION_OCR_LANGS=("zh-Hant","zh-Hans","en-US","vi-VT")`（[:265-267](../agent_core/ingest/drive_sync.py)）——語言正好涵蓋台越雙廠。
- 證據：圖片 backlog 曾用 ocrmac **免費清完**（見記憶 `project_rag_image_ocr_vision`，PoC「完勝」）。

**相依現況**（已查 live `.venv`）：`ocrmac` ✅、`pypdfium2==5.10.1` ✅（已釘在 requirements-docs.txt，與 `_extract_pdf` 用的 `pypdf` 同檔）、`Pillow` ✅。→ **「PDF→圖」用現成 pypdfium2，Phase 1 零新增相依**。

## 2. 硬約束（違反就出事 / 白做工）

| # | 約束 | 證據 | 含意 |
|---|---|---|---|
| C1 | **embed = 夜跑真瓶頸**，OCR 會放大它 | 夜跑 log 累積 Gemini `503` 1,500+ 次卡 embed；記憶 `project_rag_nightly_bottleneck_20260627` | OCR 出的文字仍要向量化。把 ~5,000 份掃描檔丟進夜跑 = 多 5,000 份 embed → 503 更嚴重、夜跑更長。**故 inline 退路預設關、主力走離峰 backfill** |
| C2 | **ChromaDB 單一 writer** | vector_store.py（多 process 直寫 → HNSW 腐壞 SIGSEGV）；多次事故 | backfill 腳本**不可與夜跑同時跑**（搶 writer）。要跟現行 backfill 一樣先確認夜跑結束、writer 空出 |
| C3 | **`_ExtractPool.run()` 全程持鎖、單一子程序** | drive_sync.py:380-491 | OCR（rasterize+逐頁 ocrmac）跑在抽取子程序、會與其他檔抽取**序列排隊**。多頁掃描檔很慢 → 必須**限頁數 + 限逐檔 timeout**，否則一份 200 頁掃描檔卡死整條抽取 |
| C4 | **既有大小/分塊上限要沿用** | `_DRIVE_EXTRACT_MAX_BYTES=30MB`（:249）、`_DRIVE_MAX_CHUNKS_PER_FILE=500`（:251） | OCR 文字一樣過 chunk 上限；rasterize 前先擋超大 PDF |
| C5 | **現有 5,000 份已被 `empty_text` skip-marker 擋住** | :966 `pass` + :2824 寫 marker | 光加 OCR 不會自動重抽它們。要嘛 backfill 繞過 marker、要嘛在 flag 開時讓 `_skip_marker_matches` 對「empty_text + PDF mime」回 `False` 重試 |

## 3. 設計

分兩部分：**(A) gated inline 退路**（接住未來新增掃描 PDF）＋ **(B) 離峰一次性 backfill**（清現有 ~5,000）。共用同一條 rasterize+OCR helper。

### 3.1 共用：PDF → 逐頁 OCR helper

新增 `_extract_pdf_ocr(data: bytes) -> str`（與 `_extract_pdf` 同檔、同樣跑在 `_ExtractPool` 子程序內）：

```
1. 若未啟用（_PDF_OCR_ENABLED False）→ 回 ""（行為同今日）
2. rasterize（_rasterize_pdf）：pypdfium2 PdfDocument(data) → 逐頁 page.render(scale=DPI/72).to_pil()
   → PNG bytes。頁數上限 _PDF_OCR_MAX_PAGES（預設 15），超過記 log（不靜默截斷）。
   沒裝 pypdfium2 / 壞檔 → 回 [] → 上層維持 empty_text。
3. 逐頁 _extract_image_vision(png, "image/png")（ocrmac、免費本地；非 macOS 回 None）
   - 累計字數達 _PDF_OCR_MAX_CHARS（預設 20 萬）即停；整體 wall-clock 由 _ExtractPool 既有 timeout 兜底
4. 併頁文字回傳；全空 → 回 ""（維持 empty_text，代表真的 OCR 不出東西＝照片/手寫/糊）
```

- **rasterizer 用 pypdfium2（已是相依，零新增）**：in-process、無外部子程序；不選 pdf2image（要 poppler 子程序、較慢）、不選 PyMuPDF（多一個 AGPL 相依）。
- **逐檔時間**：不另開 wall-clock 計時器，靠「頁數上限 + 字數上限 + `_ExtractPool.run` 既有 timeout」三層兜底，簡單且不重造。
- **不接 Gemini Vision 退路**（ocrmac 不夠時直接放棄該頁）：避免每頁打 Gemini 的成本/503。財務掃描檔 ocrmac 已足夠（PoC 完勝）。未來要更兇再 gated 開。

### 3.2 (A) 夜跑 inline 退路（gated、預設關）

- 在 `_extract_binary_text` 的 PDF 分支（`_extract_pdf` 之後）：若 `_extract_pdf` 回空且 `_PDF_OCR_ENABLED` → 改呼叫 `_extract_pdf_ocr(data)`。
- skip-marker 重試閘：`_skip_marker_matches`（:966）改成
  `if reason == "empty_text": return False if (_PDF_OCR_ENABLED and marker.mime==_PDF_MIME) else (沿用 modifiedTime 檢查)`，
  讓「開 flag 後」自然把舊掃描 PDF 視為待重試。
- **預設 `RAG_PDF_OCR=0`**：因 C1，平時不要讓夜跑扛 OCR+embed。等 503 throughput 改善後再評估常開。

### 3.3 (B) 離峰一次性 backfill（主力）

獨立腳本 `tools/backfill_pdf_ocr.py`（不進夜跑熱路徑、手動離峰跑）：

```
前置：確認無夜跑在跑（rag_sync.lock PID 已死 / status!=running）→ 不搶 writer（C2）
候選清單（兩種來源，取快者）：
  (a) 讀 drive_sync_skip_state.json，挑 reason==empty_text 且 mime==application/pdf 的條目
      → 這就是負快取裡現成的「掃描 PDF 清單」，免重新列檔
  (b) 或對指定 drive_id 列 PDF、比對 chroma 缺席集合
逐檔：RAG_PDF_OCR=1 環境 → sync_file(file_id, drive_id=…)（繞過 empty_text marker 重抽）
  → 內部走 3.1 OCR → embed → upsert（正常單 writer 路徑）
特性：可續跑（已成功者下輪 fast-skip）、限速、每 N 檔印進度、會計優先（掃描密度最高）
```

- **為何走 `sync_file` 而非自刻 upsert**：沿用 skip/purge/dedup/single-writer 全套（C2/C4），不重造輪子。腳本要有 `if __name__=="__main__"` 守衛（`_ExtractPool` spawn 重 import）。

## 4. 效率與成本（誠實版）

| 項 | 影響 |
|---|---|
| OCR 計算 | **免費 + 本地**（ocrmac），不碰 Gemini、不碰 503、零 API 費 |
| CPU | rasterize + 逐頁 OCR 吃 CPU，受 `_PDF_OCR_MAX_PAGES` 限；離峰跑可接受 |
| embed | **唯一真成本**：~5,000 份 × 數 chunk 的向量化，加 503 競爭 → 故離峰、分批、會計優先 |
| 對夜跑速度 | inline 開了會**更慢**（多 embed）；預設關就無影響 |
| 對人查找 | 5,000 份合約/財報/匯款單變可搜尋——**對人的效率是正向**，對系統夜跑是負擔 |

結論：**B 是涵蓋率強化，不是夜跑優化**。正確順序＝先收純文字漏抽（免 OCR）→ 先解 503 throughput → 再離峰跑 B。

## 5. 設定旗標（全部 env、預設保守）

| Flag | 預設 | 作用 |
|---|---|---|
| `RAG_PDF_OCR` | `0` | 總開關（inline 退路 + 重試 empty_text marker）。backfill 腳本內部自行設 1 |
| `RAG_PDF_OCR_MAX_PAGES` | `15` | 每份 PDF 最多 OCR 頁數，超過記 log |
| `RAG_PDF_OCR_DPI` | `200` | rasterize 解析度（OCR 品質 vs 速度平衡） |
| `RAG_PDF_OCR_MAX_CHARS` | `200000` | 單份累計 OCR 字數上限，到頂即停（防病態巨檔） |
| 沿用 `RAG_VISION_OCR` / `_MIN_CHARS` / `_LANGS` | — | 共用既有 ocrmac 設定 |

## 6. 分階段上線

1. **Phase 1**（✅ 本次實作）：`_rasterize_pdf`（pypdfium2）+ `_extract_pdf_ocr` helper + `_extract_binary_text` PDF 分支 inline 退路（flag 預設關）+ `_skip_marker_matches` 重試閘 + empty_text marker 補存 mime_type。**零新增相依**（pypdfium2 已在）。單元測試：含文字層 PDF 不變、純掃描 PDF 開 flag 後 OCR 出字、頁數上限截斷、未啟用/沒裝 pypdfium2/非 macOS 回退、skip-marker 閘對 PDF vs 非 PDF。**對 live 零影響**（預設關）。
2. **Phase 2**：實作 3.3 backfill 腳本，**離峰**對會計（0AFFZvSO）先跑一批驗證品質與耗時，再採購/其他。
3. **Phase 3**：視 503 throughput 改善情形，決定 inline 退路是否常開（帶頁數上限）。

## 7. 風險與待決

- **OCR 品質**：掃描財報密集表格、紅章、手寫批註 OCR 不完美；但數字/抬頭/日期可搜尋，足夠 RAG 用途。Phase 2 抽驗。
- **rasterize 記憶體**：大尺寸高 DPI pixmap 吃記憶體；`_DRIVE_EXTRACT_MAX_BYTES` 先擋超大檔 + 頁數上限雙保險。
- **skip-marker 語義**：開 flag 重試 empty_text 後，若 OCR 仍空，要確保**重寫 marker**避免每輪重抽（維持負快取效益）。
- **待決**：inline 是否永久常開？取決於 503/embed throughput 是否先解（見 `project_rag_nightly_bottleneck_20260627`）。
