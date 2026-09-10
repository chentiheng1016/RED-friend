# RED 直讀飛越 ERP Oracle DB — 唯讀查詢設計（研究稿）

> 狀態：研究/設計，尚未動工。目標是讓 RED 能直接 SELECT 飛越 ERP 的 Oracle
> 資料庫（訂單/BOM/生產排程），繞過 Oracle Forms applet 不能自動化、BOM 不能
> 匯出的牆（見 ERP 自動化計畫）。
>
> 去識別化：repo 曾公開曝露，本檔與 erp_hardening / erp_schema_map 的
> `<ERP_HOST>` / `<ERP_PRIVATE_IP>` / `<ECS_INSTANCE_ID>` / `<MY_MAC_IP>`
> 均為佔位符；主機實際位址在 keyring `xiaohong-agent/erp_ssh_host`
>（runtime 由 `erp_oracle_client._cfg()` 讀取，env `RED_ERP_SSH_HOST` 可覆寫）。

## 0. 硬前提：先過安全阻塞

那台 ERP 主機（阿里雲 FUCHUN-ERP / 香港 / `<ERP_HOST>`）目前是
**已被植入惡意檔案 + 1521/3389/7778 對 `0.0.0.0/0` 全開** 的狀態
（詳見記憶 `project_erp_server_security_blocker`）。

**在主機清乾淨、1521 收掉公網之前，不接 RED 上去。** 本文設計成立的前提是：
網路走 tunnel（內網），不是直連那個裸露的公網 1521。

---

## 1. 連線層 — `python-oracledb`（thin 模式優先）

- 驅動用 **`python-oracledb`**（cx_Oracle 的後繼，官方維護）。
- **thin 模式**：純 Python，**不需要安裝 Oracle Instant Client**，最乾淨，符合
  RED「葉依賴、少系統相依」的調性。
- ✅ **版本已實測（2026-06-28）：Oracle 10.2.0.5（10g R2）。** 用 mac 從公網 1521
  打 listener 的 refuse 封包解 VSNNUM=0x0A200500 確認。
  - **thin 模式直接出局**（thin 只支援 12.1+）。
  - **必走 thick 模式**：python-oracledb `init_oracle_client()` + **Oracle Instant
    Client 11.2**（新版 19c/21c client 不支援連 10.2；11.2 client ↔ 10.2 server 才在
    互通矩陣內）。系統相依變重，且要釘到舊版 IC，部署時要打包進 image / launchd 環境。
  - 連線參數（實測，見 `docs/erp_schema_map.md`）：tnsnames 別名 `jhdb` → 真實
    **service `JHNEW`**、內部主機名 `fast`、port 1521；帳號 `APP`/`APP`（弱、待換）。
  - ⚠️ 公網 <ERP_HOST>:1521 listener 活著但 JHNEW **未對外註冊**（外部連 12514），
    所以**一定走本機/tunnel**，別假設 mac 直連得到。

DSN（連線字串）格式（Easy Connect）：

```
host:port/service_name        # 例：127.0.0.1:1521/ORCLPDB1（走 tunnel 後是 localhost）
```

連線 helper（草圖）：

```python
# agent_core/erp_oracle_client.py（葉模組，唯讀守門 + 連線池）
import oracledb
from agent_core.secret_provider import get_secret

def _dsn() -> str:
    # 走 tunnel 後 host 是 127.0.0.1；service_name 由研究待辦 #2 補
    host = get_secret("ERP_ORACLE_HOST").value or "127.0.0.1"
    port = get_secret("ERP_ORACLE_PORT").value or "1521"
    svc  = get_secret("ERP_ORACLE_SERVICE", required=True).value
    return f"{host}:{port}/{svc}"

def connect():
    return oracledb.connect(
        user=get_secret("ERP_ORACLE_USER", required=True).value,
        password=get_secret("ERP_ORACLE_PASSWORD", required=True).value,
        dsn=_dsn(),
        # thin 模式是預設；若需 thick：先 oracledb.init_oracle_client(...)
    )
```

secrets 一律走既有 `agent_core/secret_provider.get_secret`
（env → Secret Manager → keyring `xiaohong-agent`），**不寫進碼、不進 git**。

---

## 2. 網路層 — 一律走 tunnel，不碰公網 1521

兩種接法，擇一：

| 方式 | 做法 | 適用 |
|---|---|---|
| **SSH local forward** | `ssh -L 1521:127.0.0.1:1521 user@erp-host`，RED 連 `127.0.0.1:1521` | 主機有開 SSH（Windows 要裝 OpenSSH Server）；最輕 |
| **VPN / 阿里雲 VPC** | RED 端進 VPC 內網，直連私網 `<ERP_PRIVATE_IP>:1521` | 較正規、長期；要建 VPN |

- 收掉 1521 公網規則後，tunnel 是唯一入口，攻擊面大幅縮小。
- RED 那端把 host 設 `127.0.0.1`（SSH forward）或私網 IP（VPN）。
- **研究待辦 #3：主機要不要開 OpenSSH？還是建 VPN？** 決定 tunnel 形狀。

---

## 3. 認證 — 跟飛越要「唯讀帳號」

- 跟 ERP 廠商（飛越）要一組 **read-only Oracle 帳號**：只給對應 schema 的
  `SELECT` 權限，不給任何 `INSERT/UPDATE/DELETE/DDL`。
- 若廠商只肯給一般帳號 → RED 端再用 SELECT-only 守門（見 §5）兜底，但
  「帳號層就唯讀」永遠優先。
- **研究待辦 #4：帳號 + 它能看到的 schema 名。**

---

## 4. Schema 探索 — 廠商不給文件就反推

台灣 ERP 廠商常把表結構鎖死。沒有 schema 文件時，用 Oracle 資料字典自己摸：

```sql
-- 4.1 這個帳號看得到哪些表（含註解，飛越若有中文表註解這裡就現形）
SELECT t.table_name, c.comments
FROM   all_tables t
LEFT   JOIN all_tab_comments c ON c.table_name = t.table_name
WHERE  t.owner = :schema
ORDER  BY t.table_name;

-- 4.2 某張表的欄位 + 欄位註解
SELECT column_name, data_type, data_length, nullable
FROM   all_tab_columns
WHERE  owner = :schema AND table_name = :tbl
ORDER  BY column_id;

SELECT column_name, comments
FROM   all_col_comments
WHERE  owner = :schema AND table_name = :tbl;

-- 4.3 用關鍵字撈「訂單/BOM/排程」候選表（中英都試）
SELECT table_name FROM all_tables
WHERE  owner = :schema
AND   (UPPER(table_name) LIKE '%ORDER%' OR UPPER(table_name) LIKE '%BOM%'
       OR UPPER(table_name) LIKE '%SCHEDUL%' OR UPPER(table_name) LIKE '%PROD%');

-- 4.4 主鍵/外鍵（拼關聯）
SELECT ac.table_name, ac.constraint_type, acc.column_name
FROM   all_constraints ac
JOIN   all_cons_columns acc ON acc.constraint_name = ac.constraint_name
WHERE  ac.owner = :schema AND ac.constraint_type IN ('P','R');
```

策略：先用 4.1/4.3 拿候選表 → 4.2 看欄位 → 對單一已知訂單號 `SELECT … WHERE
rownum <= 5` 抽樣比對真實值（用權威來源驗：業務訂單夾、twsales@/owner@ 信，
見記憶 `reference_authoritative_order_source`）→ 確認對上才寫進工具。

→ 把摸出來的對照表沉澱成 repo 內一份 schema map（`docs/erp_schema_map.md`）。

---

## 5. RED 整合 — 一個 SAFE 唯讀 skill

新 skill `skills/erp_oracle.py`，照 repo 慣例（純函式 + `SKILL_TOOLS`、字串參數、
友善繁中錯誤字串、回傳字串）。

**SELECT-only 守門（雙保險，帳號唯讀之外再擋一層）**：

```python
import re
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|"
    r"revoke|commit|rollback|begin|declare|execute|call)\b", re.I)

def _guard_select_only(sql: str) -> str:
    s = sql.strip().rstrip(";")
    if ";" in s:                       # 擋多語句注入
        raise ValueError("一次只能跑一條查詢")
    if not re.match(r"^\s*(select|with)\b", s, re.I):
        raise ValueError("只允許 SELECT / WITH 查詢")
    if _FORBIDDEN.search(s):
        raise ValueError("查詢含不允許的關鍵字（唯讀工具只能 SELECT）")
    return s
```

每次查詢都套：
- **statement timeout**（連線 `call_timeout`，借鏡 RAG 那邊 sqlite 全掃卡死的教訓，
  見 `run_with_deadline` / `RAG_CHROMA_OP_TIMEOUT_S` 慣例）。
- **row cap**（外層硬加 `FETCH FIRST N ROWS ONLY` 或 fetchmany 上限），防一條
  query 拖垮。
- 回傳前對自由文字欄位套 `sanitize_for_llm` + `wrap_as_untrusted`
  （ERP 字串也是外部資料，照郵件那套淨化慣例，見 `reference_email_llm_sanitization`）。

工具面（初版）：
- `query_erp_orders(...)`、`query_erp_bom(...)`、`query_erp_production_schedule(...)`
  — 包好參數化 SQL 的高階查詢（不讓 LLM 直接拼 SQL）。
- （進階，可選）`run_erp_readonly_sql(sql)` — 給 SELECT，過 §5 守門。tier 仍 SAFE
  但要不要開放給 Telegram 前台要再想（text-to-SQL 會吃 token、也可能撞 schema 雷）。

**tier**：全部 `TIER_SAFE`（查詢/讀取，無確認需要）。**絕不**讓這個 skill 觸碰
寫入；任何 DML/DDL 在帳號層 + §5 守門 + 不提供寫入工具 三層都擋死。

依賴：`python-oracledb` 加進 **新的 `requirements-erp.txt`**（或併 core，視部署），
不污染現有 requirements 分層。

---

## 6. 還缺什麼（gated inputs）— 2026-06-28 更新

| # | 項目 | 狀態 |
|---|---|---|
| 1 | Oracle 版本 | ✅ 10.2.0.5 → thick + IC 11.2（§1） |
| 2 | service / 連線參數 | ✅ service `JHNEW`、host `fast`、1521、`APP`/`APP`（§1） |
| 3 | 兩張核心表 schema | ✅ 見 `docs/erp_schema_map.md`（ORDITEM 49 欄 / ITEMSCHE 23 欄、join key SE_ID） |
| 4 | tunnel 形狀（SSH or VPN） | ⬜ 你/IT 決定（§2）；公網未對外註冊 JHNEW，**必走 tunnel** |
| 5 | 主機清毒 + 1521 收公網 | ⬜ 前提（§0），未過不接 RED |
| 6 | 換強的唯讀帳號 | ⬜ APP/APP 太弱，跟飛越要 read-only 專用帳號 |
| 7 | col comments / SE_STATUS 代碼表 / 主鍵索引 | ⬜ 建工具前補（§5 row cap、查詢 hint） |

①②③已在 ERP 主機本機用 PL/SQL Developer 實測拿到；④⑤⑥⑦待辦。

---

## 7. 分階段落地

- ✅ **Phase 0**：研究稿 + gated inputs。
- ✅ **Phase 1**：2026-06-28 在 ERP 主機本機用 PL/SQL Developer 探 schema，產
  `docs/erp_schema_map.md`（ORDITEM 49 欄 / ITEMSCHE 23 欄）。
- ✅ **Phase 2（已寫，預設關閉）**：
  - `agent_core/erp_oracle_client.py` — thick(IC11.2) 連線 + `guard_select_only`
    （只放行單條 SELECT/WITH）+ call_timeout + row cap；oracledb lazy import；
    `RED_ERP_ORACLE_ENABLED` 預設 0。
  - `skills/erp_oracle.py` — `query_erp_order` / `query_erp_order_materials` /
    `run_erp_readonly_sql`（SAFE、字串參數、bind、sanitize_for_llm、code-block 表）。
  - `requirements-erp.txt`（oracledb，選用，艦隊不裝）。
  - `tests/test_erp_oracle.py` — 15 測全綠（守門、預設關、格式化），不需 DB/oracledb。
  - ⚠️ **尚未對真實 DB 驗證**：要等 tunnel 就緒 + 設 IC 11.2 + 開
    `RED_ERP_ORACLE_ENABLED=1` 才能真連；目前只證明「不連也不會炸、守門對」。
- ⬜ **Phase 3**：tunnel 就緒後真連驗證 → 補 col comments/狀態碼 → smoke → redeploy。

---

## 8. 一句話風險守則

唯讀工具的三道防線缺一不可：**帳號層 SELECT-only → §5 程式守門 → 不提供寫入
工具**；網路只走 tunnel；ERP 回來的字串當外部不可信資料淨化。主機沒清乾淨前，
整個計畫停在 Phase 0。

---

## 9. 實作定案（2026-06-28，取代前述 thick/IC 規劃）

**為什麼不用 python-oracledb**：DB 是 10.2（10g）。thin 只支援 12.1+；macOS 只有
19c/23ai 版 Instant Client（最低連 11.2 server），**沒有任何 mac client 連得到 10g**。
→ 改成 **查詢在 ERP 主機上用它自己的 10g sqlplus 執行，RED 每次查詢開一條 SSH 把
SELECT 餵過去、取回 tab 分隔結果**。不是常駐 port-forward tunnel，**不需 autossh**。

連線鏈（已端到端實測）：
```
RED ──ssh -i ~/.ssh/id_erp_jhdb Administrator@<ERP_HOST> "sqlplus -S -L /nolog"──▶
     主機 sqlplus ──CONNECT APP/APP@JHDB（走 stdin，不入 argv）──▶ Oracle 10g
```

**已就緒**：
- 主機裝 Win32-OpenSSH（sshd Running、開機自啟）。
- 金鑰 `~/.ssh/id_erp_jhdb`（ed25519、無密碼，給 daemon 用）；公鑰在主機
  `C:\ProgramData\ssh\administrators_authorized_keys`（ACL 僅 SYSTEM+Admins）。
- 安全群組加 22 入方向、來源限 `<MY_MAC_IP>/32`（mac，浮動 IP 變了要改）。
- `NLS_LANG=AL32UTF8` → 中文以 UTF-8 回傳（實測料號/品名/供應商全正常）。

**程式（worktree，預設關閉）**：
- `agent_core/erp_oracle_client.py`：`guard_select_only`（單條 SELECT/WITH）、
  `validate_identifier`（單號防注入）、`fetch_table`/`run_raw_text`、row cap、timeout、
  `RED_ERP_ORACLE_ENABLED` 預設 0；env：`RED_ERP_SSH_KEY/USER/HOST`、
  `RED_ERP_ORACLE_TNS=JHDB`、DB 帳密走 secret_provider（預設 APP/APP）。
- `skills/erp_oracle.py`：`query_erp_order` / `query_erp_order_materials` /
  `run_erp_readonly_sql`（SAFE；`_cell` 用 `sanitize_untrusted_text` 防注入，**不用**
  `sanitize_for_llm`——後者 IBAN 偵測會把料號遮成 `[REDACTED:IBAN]`）。
- `tests/test_erp_oracle.py`：16 測（守門/防注入/預設關/解析/格式化），不需主機。
- ✅ 實測 JFC26160：訂單 DECATHLON 368 雙；物料 63 筆中文完美。

**部署待辦**：主機清毒（slpr.exe）+ 收公網 1521/3389；換強的唯讀帳號；
`RED_ERP_ORACLE_ENABLED=1` + redeploy 艦隊（金鑰已在 mac = fleet host）；
補 col comments / SE_STATUS 代碼表；抽樣對業務訂單夾驗數字。
