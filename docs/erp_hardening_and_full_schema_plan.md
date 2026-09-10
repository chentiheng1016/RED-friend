# ERP「無悔第一步」執行方案：清毒收網 × 全 schema 擷取

> 狀態：**執行計畫，待老闆逐步授權**。本文只規劃、不動主機。凡標 🛑 的動作是破壞性/會改動 live 主機，**必須老闆在當下逐步確認後才做**；🟢 是唯讀盤點、無破壞。
>
> 主機：阿里雲 ECS `<ECS_INSTANCE_ID>`（FUCHUN-ERP、Win2012R2、中国香港區、公網 <ERP_HOST> / 私網 <ERP_PRIVATE_IP>）。DB：Oracle 10.2.0.5、schema 由 `BQ_SE_*` 偵測（`erp_schema_probe.detect_owner`）。
>
> ⚠️ 前提誠實話：記憶 `project_erp_server_security_blocker` 記載老闆 2026-07-07 口頭說「已掃毒、忽略」，但三個高危埠與 slpr.exe **未經 RED 獨立複驗**。本計畫的第一步就是把「口頭已處理」變成「有證據地確認或收斂」。

---

## A. 為什麼是「無悔」

不管日後 ERP 走 A（擴充現有）/ B（strangler 漸進替換）/ C（整套重寫）哪條路，這兩件事都用得到、都不會白做（見記憶 `project_erp_rebuild_abc_assessment`）：

1. **清毒收網**：把「DB/RDP 對全世界開 + 弱憑證 + 疑似惡意檔」收斂到最小攻擊面——這是任何後續動作的安全底線，愈晚做風險敞口愈久。
2. **全 schema 擷取**：把 DB 的完整地圖（表/欄/主外鍵/**PL/SQL 全文**/view 定義/trigger/index/synonym）抓下來，是 B/C 反推業務邏輯的唯一材料，也是 A 擴充時避免踩到隱藏約束的參考。

---

## B. 全 schema 擷取——現況、缺口、與這次補的 code

### B.1 已有（`erp_schema_probe.probe()`，14 schema 的 `-latest.json` 已落地）
表名 + 估列數 + 表註解、欄位 + 欄註解、主外鍵、view **名字**、邏輯落點 **COUNT**（all_source 按 type 數行數、trigger/sequence 數）。盤點量級：~1,538 表 / 23,325 欄 / 405 views / 25,324 行 PL/SQL / 199 triggers（SC00 供應鏈獨佔大宗）。

### B.2 這次補的 code（本 PR，唯讀、可測、授權後即用）
`erp_schema_probe` 新增 **PL/SQL 原始碼全文擷取**——這是 B.1 只有 COUNT、缺了「真正材料」的最高價值缺口（子代理盤點標 🔴）：

- `sql_source_stream(owner)` / `extract_source(owner)`：走 `ssh_sqlplus_stream` **全量**串流 all_source（單 schema 上萬行 > fetch_table 的 5000 cap，故必須 stream），按 `(type, name)` 依 line 重組 body。**故意不套 clean_text_col**（它連 tab 一起剝、毀縮排）：只把換行轉空白避免打散 stream 的「一行一列」，保留 tab 縮排，重組時 `join('\n')` 還原。
- `write_source_files(bodies, owner)`：每物件一個 `.sql`，依 type 分子目錄（`packages/` `package_bodies/` `procedures/` `functions/` `types/` `triggers/`…），檔名過 `_safe_filename` 擋路徑穿越。
- `probe_source(owner)`：偵測 owner → 擷取 → 落檔 → 回統計。**與 `probe()` 分開跑**（stream 量大、斷線風險高，不綁死結構探勘）。
- 測試：`tests/test_erp_schema_probe.py` 新增 7 測（stream SQL 過 guard、body 依 line 重組、tab 保留、畸形行跳過、type 分目錄、路徑穿越防護、預設 stream 綁真 client），全 mock、不碰主機。

### B.3 還缺、留待後續（需 LONG 欄專門處理或次要）
| 缺口 | 型別/難點 | 建議 SQL |
|---|---|---|
| 🔴 view SQL 定義 | `all_views.text` 是 **LONG** | `SELECT view_name, text FROM all_views WHERE owner=:o`；LONG 要走 stream + `SET LONG`/`LONGCHUNKSIZE` 拉大，不能 CHR concat |
| 🔴 trigger body + 觸發條件 | `all_triggers.trigger_body` 是 **LONG** | `SELECT trigger_name, table_name, triggering_event, when_clause, trigger_body FROM all_triggers WHERE owner=:o` |
| 🟡 索引（查詢計畫用，呼應夜跑教訓） | 普通多 row | `all_indexes` + `all_ind_columns`（`fetch_table` 可，量 < 5000） |
| 🟡 synonym（名稱解析鏈，APP 有 CREATE PUBLIC SYNONYM） | 普通 | `SELECT synonym_name, table_owner, table_name FROM all_synonyms WHERE owner IN (:o,'PUBLIC')` |
| 🟡 sequence 明細（目前只 COUNT） | 普通 | `all_sequences` min/max/increment/cache/last_number |
| 🟢 權限矩陣（換強唯讀帳號用） | 普通 | `all_tab_privs` / `user_tab_privs_recd` |

### B.4 執行前置（🛑 連 live 主機，需授權）
1. `RED_ERP_ORACLE_ENABLED=1`（預設關）；SSH key `~/.ssh/id_erp_jhdb` 就緒。
2. **斷線重試**：實測 ERP 大串流會偶發 SSH `Broken pipe`（07-18 erp_mirror 就有一張表這樣失敗）。25,000 行 all_source 是大串流——`extract_source` 遇斷會 raise（不吞半份）；執行端要按 schema 分段跑、失敗重試該 schema，別一次抽 14 個。
3. 建議先抽最高價值的 SC00（供應鏈，13,846 行 PL/SQL 主體）驗證流程，再擴其餘。

---

## C. 清毒收網——逐步計畫（🛑 破壞性動作逐步授權）

### C.1 只需 SSH，先做唯讀盤點（🟢 無破壞，授權連主機後可一次全跑）
在主機 PowerShell（經 `erp_oracle_client` 的 SSH 通道或直接 SSH）唯讀查：
1. `Get-Process` / `Get-Service` / `netstat -ano` / `Get-ScheduledTask` / `Get-CimInstance Win32_StartupCommand`——列出跑什麼、開什麼埠、什麼開機自啟。
2. `Get-Item C:\Users\Temp\Documents\slpr.exe`（若在）→ 算 `Get-FileHash SHA256`、看數位簽章、查有無對應執行中 process/服務/排程指向它。
3. 事件記錄查異常登入（呼應 06-14「常見位置登入」中危告警）。
> 產出：一份「主機現況證據」清單，把「口頭已掃毒」變成「有 hash、有埠表、有 process 表」的事實。

### C.2 隔離惡意檔（🛑 破壞性，需老闆逐步確認）
- 若 slpr.exe 仍在且有持久化（服務/排程/autorun）：**先停用持久化、再把檔案移到隔離夾**（改副檔名或搬 `C:\quarantine\`）。**不要直接刪**——保留樣本（阿里雲建議亦然），可還原。
- 回滾：記錄原路徑 + ACL，還原即復原。

### C.3 收斂公網埠（🛑 需阿里雲 console，RED/SSH 改不到；放寬類動作會被 Claude Code 安全閘擋）
安全群組 `sg-j6caf5o0prtc6vnar0d5` 目前三個高危埠來源全 `0.0.0.0/0`。**動前每個埠都先盤點「誰在用」**，再逐條收（風險遞增排序）：
1. **RDP 3389**（風險最低、先收）：來源改白名單或關閉。
2. **Oracle 1521**：理想是完全不對公網開，只走 SSH（RED 已是 key-only SSH bastion 姿態）或 VPC 內網。⚠️動前確認有無外部據點靠公網直連 1521。
3. **Forms/Reports 7778**：盤點誰在用再收。
- SSH 22 維持 key-only（收完上面三個後它是唯一入口，不需再放寬；mac 浮動 IP 故當初刻意不鎖來源）。
- 回滾：每條規則改前記原值，改回即復原。

### C.4 DB 帳號（🛑 最後做、最需 DBA 協作）
- ⚠️**不要改 APP 密碼**——APP 是 ERP 自身服務帳號，改了會弄停 ERP。
- 正解：另開一個 **`GRANT SELECT`-only 專用帳號**（需 DBA/SYS 權限，在主機 sqlplus 內做），寫進 keyring（`erp_oracle_user`/`erp_oracle_password`），再拿掉 `erp_oracle_client` 的 `or "APP"` fallback。這樣「DB 帳號層只給 SELECT」這道現在失效的防線就補回來（目前擋寫入只剩 Python `guard_select_only` 單點）。
- 回滾：`DROP USER` 新帳號，不影響 APP。

### C.5 建議順序（風險遞增）
C.1 唯讀盤點（今天、零風險） → 老闆確認 → C.3-1（RDP） → C.2（隔離 slpr） → C.3-2/3（1521/7778，先盤點來源） → C.4（換唯讀帳號）。

---

## D. 這次 PR 的邊界（做了什麼、沒做什麼）

- ✅ **做了**：`erp_schema_probe` 的 PL/SQL 全文擷取 code + 測試（本地無悔）；本執行方案文件。
- ❌ **沒做**（需老闆授權連/動 live 主機）：實際執行 schema 擷取（連主機跑 stream）、C 段任何清毒收網動作。
- 相關：記憶 `project_erp_server_security_blocker` / `project_erp_db_readonly` / `project_erp_rebuild_abc_assessment`；code `agent_core/erp_oracle_client.py`（唯讀守門）、`agent_core/erp_schema_probe.py`。
