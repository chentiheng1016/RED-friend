"""飛越 ERP 全量資料鏡像 → 本地 DuckDB（唯讀擷取，走 erp_oracle_client 串流）。

目標：把整套 ERP（14 業務 schema、~1,538 表）的**資料**（非只 schema）拉進本地一個
DuckDB 檔，讓小紅/大王能直接下 SQL 分析，不必每次戳那台主機。

做法（受限於「只有 SSH+10g sqlplus 文字管線」這條路）：
  1. 讀 var/data/erp_schema_probe/erp-schema-<owner>-latest.json（前一步全 schema 擷取的產物）
     拿每張表的欄位/型別。
  2. 為每表組一條 SELECT：欄位用 CHR(1)(SOH，資料裡不會出現)分隔；自由文字欄剝掉
     CHR(10)/CHR(13)（否則換行打散「一行=一列」）並 SUBSTR 上限 TEXT_CAP 字（bound 行寬，
     免超過 sqlplus LINESIZE 32767 被 wrap）；DATE/TIMESTAMP → TO_CHAR；LOB/LONG/RAW 跳過。
  3. 串流（Popen 逐行，不整包吃記憶體）寫成 .csv.gz，再用 DuckDB read_csv(all_varchar) 建表。
  4. 每表寫 manifest（可續跑：status==done 就跳過）；db 列數 vs 串流列數 vs 載入列數對帳，
     不一致就標記（wide row 被 sqlplus wrap 會在此現形）。

全部 VARCHAR 載入（穩、不會因髒資料 cast 失敗）；型別在 schema json 裡，需要再於查詢時 cast。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterator

from agent_core import erp_oracle_client as erp
from agent_core.env_utils import env_int
from agent_core.erp_oracle_client import (
    ErpConnectionError,
    ErpOracleError,
    fetch_table,
    ssh_sqlplus_stream,
)
from agent_core.logging_and_paths import DATA_DIR

_DIR = os.path.join(DATA_DIR, "erp_mirror")
_DB = os.path.join(_DIR, "erp_full.duckdb")
_MANIFEST = os.path.join(_DIR, "manifest.json")
_RAW_DIR = os.path.join(_DIR, "raw")
_SCHEMA_DIR = os.path.join(DATA_DIR, "erp_schema_probe")

# 大表優先序（有價值的核心 schema 先鏡像，之後才是匯入副本/系統）。
_SCHEMA_ORDER = ["SC00", "MK00", "SP00", "GL00", "APP", "EP00",
                 "SY00", "FY00", "PDA", "GL_IMP", "EP_IMP", "SHIRLEY", "TIPTOP", "BTW"]

# 這些型別在文字管線裡無法可靠傳（LOB 需 DBMS_LOB＝被守門擋、LONG/RAW 會亂），一律跳過。
_SKIP_TYPES = {"CLOB", "NCLOB", "BLOB", "BFILE", "LONG", "LONG RAW", "RAW",
               "ROWID", "UROWID", "XMLTYPE", "ANYDATA"}
_TEXT_TYPES = {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "VARCHAR", "NVARCHAR"}
_TEXT_CAP = 2000                      # 自由文字欄截斷上限（bound sqlplus 行寬）
_LINESIZE = 32767                     # sqlplus 物理行上限；估行寬超過就標 wide_risk
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,29}$")


def _qi(name: str) -> str:
    """驗證 + 雙引號包 Oracle 識別碼（來源是自撈 schema，仍防注入/防怪名）。"""
    if not _IDENT.match(name or ""):
        raise ErpOracleError(f"不合法識別碼：{name!r}")
    return f'"{name}"'


def duck_table(owner: str, table: str) -> str:
    """DuckDB 表名：OWNER__TABLE（保留 schema 區隔、合法識別碼）。"""
    return f"{owner}__{table}"


def build_select(owner: str, table: str, columns: list[dict]) -> tuple[str | None, list[str], list[str], int]:
    """組全量鏡像 SELECT。回 (sql, kept_names, skipped_names, est_row_width)。

    est_row_width：各欄估最大顯示寬度加總（含分隔），用來判斷是否可能超過 LINESIZE 被 wrap。
    sql=None 代表沒有任何可鏡像欄位（整表都是 LOB 之類）。
    """
    exprs: list[str] = []
    kept: list[str] = []
    skipped: list[str] = []
    width = 0
    for c in columns:
        t = (c.get("type") or "").upper()
        nm = c.get("name") or ""
        if t in _SKIP_TYPES or ("TIMESTAMP" in t and "ZONE" in t):
            skipped.append(nm)
            continue
        try:
            q = _qi(nm)
        except ErpOracleError:
            skipped.append(nm)
            continue
        if t == "DATE" or t.startswith("TIMESTAMP"):
            exprs.append(f"TO_CHAR({q},'YYYY-MM-DD HH24:MI:SS')")
            width += 20
        elif t in _TEXT_TYPES:
            # 剝換行 + 截斷，bound 行寬；CHR(1) 分隔故不必剝 tab。
            exprs.append(f"SUBSTR(REPLACE(REPLACE({q},CHR(10),' '),CHR(13),' '),1,{_TEXT_CAP})")
            declared = 0
            try:
                declared = int(c.get("length") or 0)
            except (ValueError, TypeError):
                declared = 0
            width += min(declared or _TEXT_CAP, _TEXT_CAP)
        else:  # NUMBER / FLOAT / BINARY_* → 直接串（|| 會轉全精度字串）
            exprs.append(q)
            width += 41
        kept.append(nm)
        width += 1  # 分隔
    if not exprs:
        return None, [], skipped, 0
    # sqlplus 輸入**單行**上限 2499 字元（SP2-0027），寬表的長串接會爆掉整條被忽略。
    # 把運算式每 ~1400 字插一個換行拆成多物理行——sqlplus 會把換行的續行接成同一條 SQL。
    sep = "||CHR(1)||"
    chunks: list[str] = []
    line_len = 0
    for i, ex in enumerate(exprs):
        piece = (sep if i else "") + ex
        if i and line_len + len(piece) > 1400:
            chunks.append("\n")
            line_len = 0
        chunks.append(piece)
        line_len += len(piece)
    sql = "SELECT " + "".join(chunks) + f"\nFROM {_qi(owner)}.{_qi(table)}"
    return sql, kept, skipped, width


def _parse_count(rows: list) -> int:
    """從 COUNT(*) 回列取數字。sqlplus 有時在數值前帶 tab/空白 → CHR(9) 切欄會多切出
    空欄，故掃整列找第一個純數字欄，別死抓 rows[0][0]（否則空欄 int('') 會誤回 0）。"""
    if not rows or not rows[0]:
        return 0
    for cell in rows[0]:
        s = str(cell).strip()
        if s.isdigit():
            return int(s)
    return 0


def _should_retry(db_rows: int, loaded: int) -> bool:
    """串流是否明顯短於 COUNT（→ 值得重抽一次）。門檻：短少 > max(20, 0.2%)，
    避開幾列 live churn 造成的良性差異（那種重抽也補不齊、徒增負擔）。"""
    return db_rows - loaded > max(20, db_rows // 500)


def count_rows(owner: str, table: str) -> int:
    res = fetch_table(f"SELECT COUNT(*) FROM {_qi(owner)}.{_qi(table)}", ["N"], limit=1)
    return _parse_count(res.get("rows") or [])


# ── 夜刷變更預檢 ─────────────────────────────────────────────────────────
# 每晚熱表全量重抓 ~68 分鐘，但多數表在沒新單的日子根本沒變（SE_BOM_SIZE 一張就
# 吃掉 1/3）。預檢用**一發 SSH** 對每張熱表跑 COUNT(*)+<變更訊號> 輕查詢，與
# manifest 上次「真正全量刷」的記錄相同 → 跳過重刷。保守邊界（全部 fail-open 到
# 「照常全量重刷」）：拿不到安全訊號、manifest 沒記過訊號（升級首晚）、上次
# status != done、距上次真刷超過兜底天數、預檢本身失敗。順帶縮小 SSH EOF 短抽的
# 暴露面（跑得越少、被劣化窗口打中的機率越低）。
_PRECHECK_TIMEOUT_S = 600
_LAST_DATE_COL = "LAST_DATE"


def _precheck_rowscn_enabled() -> bool:
    from agent_core.env_utils import env_bool
    return env_bool("RED_ERP_PRECHECK_ROWSCN", False)


def _precheck_max_skip_days() -> float:
    """距上次「真正全量刷」超過這麼多天 → 強制重刷（不管訊號說沒變）。
    收斂任何訊號盲點的資料滯留上限；0 = 關閉兜底。"""
    from agent_core.env_utils import env_float
    return env_float("RED_ERP_PRECHECK_MAX_SKIP_DAYS", 7.0, min_value=0.0)


def _precheck_signal(columns: list[dict]) -> tuple[str, str] | None:
    """回 (manifest 存放鍵, SQL MAX 運算式) —— 這張表用什麼「變更訊號」判有沒有
    被改；沒有安全訊號可用時回 None（→ 該表照常全量重刷）。

    優先序：
      1. LAST_DATE（ERP 的列級最後修改時戳）—— 任何 DML 都會把它推進，
         MAX(LAST_DATE) 是最可靠的「不變⟹沒被改」判據（proven，預設路徑）。
      2. RED_ERP_PRECHECK_ROWSCN 開啟時用 ORA_ROWSCN —— Oracle 的區塊級 commit
         SCN，任何**已提交** DML 都會推進、只會**高報**（改到同區塊鄰列也算變、
         頂多多刷一次）、永不**漏報**，故「MAX 不變⟹沒被改」對無 LAST_DATE 欄的
         表（SE_BOM_SIZE 33 分那張等）也安全成立。**預設關**：需先在 live 維運
         窗口驗證這台 Oracle 10.2 的 ORA_ROWSCN 行為（見 PR 的驗證步驟）才打開。

    刻意**不**退回其他日期欄（CR_REQDATE / ITEM_DATE / PLAN_DATE …）：那些是
    **業務日期**、不是修改時戳。原地改一列的數量而不動日期時 MAX(業務日期) 不變
    → 把「已改」誤判成「沒改」→ 靜默送舊資料，正是這次健檢在滅的那種錯。寧可
    全量重刷，不賭業務日期。
    """
    names = {(c.get("name") or "").upper() for c in columns}
    if _LAST_DATE_COL in names:
        return ("last_date",
                f"NVL(TO_CHAR(MAX({_qi(_LAST_DATE_COL)}),'YYYY-MM-DD HH24:MI:SS'),'-')")
    if _precheck_rowscn_enabled():
        return ("row_scn", "NVL(TO_CHAR(MAX(ORA_ROWSCN)),'-')")
    return None


def _precheck_script(entries: list[tuple[str, str, str, str]]) -> str:
    """組多表 COUNT+<訊號> 的 sqlplus 腳本；每表輸出一行 KEY<TAB>count<TAB>sig。
    entries: (key, owner, table, max_expr)。訊號全 NULL 時為 '-'。"""
    lines = []
    for key, owner, table, max_expr in entries:
        lines.append(
            f"SELECT '{key}'||CHR(9)||COUNT(*)||CHR(9)||{max_expr} "
            f"FROM {_qi(owner)}.{_qi(table)};"
        )
    return "\n".join(lines)


def _parse_precheck(out_lines: list[str]) -> dict[str, tuple[int, str]]:
    """預檢輸出 → {key: (count, sig)}。格式不對的行直接略過
    （該表拿不到預檢值 = 不會被判 unchanged = 照常重刷，安全側）。"""
    res: dict[str, tuple[int, str]] = {}
    for ln in out_lines:
        parts = ln.split("\t")
        if len(parts) != 3:
            continue
        key, cnt, sig = (p.strip() for p in parts)
        if not cnt.isdigit():
            continue
        res[key] = (int(cnt), sig)
    return res


def _parse_manifest_ts(s) -> float | None:
    """manifest 的 '%Y-%m-%d %H:%M:%S' 字串 → epoch 秒；解析不了回 None。"""
    try:
        return time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def precheck_hot_unchanged(targets: list[tuple], man: dict,
                           log: Callable[[str], None] = print) -> tuple[set[str], dict]:
    """回 (可跳過的 key 集合, {key:(count, sig, sigkey)})。

    只有同時滿足才判 unchanged：上次 status==done、db_rows==本次 COUNT、manifest
    記過該訊號且與本次相同、且距上次「真正全量刷」（full_ts）未超過兜底天數。任何
    一項拿不到（表無安全訊號、預檢失敗、首晚沒記錄、full_ts 過期或缺）都照常重刷。"""
    entries = []
    sigkey_of: dict[str, str] = {}
    for owner, table, columns, _est in targets:
        sig = _precheck_signal(columns)
        if sig is None:
            continue
        sigkey, max_expr = sig
        key = f"{owner}.{table}"
        entries.append((key, owner, table, max_expr))
        sigkey_of[key] = sigkey
    if not entries:
        return set(), {}
    try:
        out = erp._ssh_sqlplus(_precheck_script(entries), timeout_s=_PRECHECK_TIMEOUT_S)
    except ErpOracleError as ex:
        log(f"precheck 失敗（{str(ex)[:120]}）→ 全表照常重刷")
        return set(), {}
    raw = _parse_precheck(out.splitlines())
    max_skip_days = _precheck_max_skip_days()
    now = time.time()
    unchanged: set[str] = set()
    stats: dict[str, tuple[int, str, str]] = {}
    for key, (cnt, sig) in raw.items():
        sigkey = sigkey_of.get(key, "last_date")
        stats[key] = (cnt, sig, sigkey)
        m = man.get(key) or {}
        try:
            same_rows = int(m.get("db_rows")) == cnt
        except (TypeError, ValueError):
            same_rows = False
        # 週兜底：距上次真刷超過 N 天就強制重刷（即使訊號說沒變）——收斂訊號盲點的
        # 滯留上限；full_ts 缺（新碼首晚 / 從沒真刷過）視為過期 → 重刷（安全側）。
        fresh_enough = True
        if max_skip_days > 0:
            full_ts = _parse_manifest_ts(m.get("full_ts"))
            fresh_enough = full_ts is not None and (now - full_ts) <= max_skip_days * 86400
        if (m.get("status") == "done" and same_rows and fresh_enough
                and m.get(sigkey) and m.get(sigkey) == sig):
            unchanged.add(key)
    return unchanged, stats


def stream_to_gz(sql: str, path: str) -> int:
    """串流跑 sql，逐行寫進 gz；回寫出的行（列）數。"""
    n = 0
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fh:
        for line in ssh_sqlplus_stream(sql):
            fh.write(line)
            fh.write("\n")
            n += 1
    return n


def _names_sql(names: list[str]) -> str:
    return "[" + ", ".join("'" + n.replace("'", "''") + "'" for n in names) + "]"


def load_gz_into_duckdb(con, duck: str, gz_path: str, names: list[str],
                        expected_rows: int | None = None) -> int:
    """用 read_csv(all_varchar) 把 gz 建成 DuckDB 表；回載入列數。

    先載進 staging 表、通過列數檢查才取代正式表——「寧舊勿缺」：短載（串流短抽
    漏網、或 read_csv ignore_errors 掉列）時保留昨日完整舊表，不讓短表上線服務
    查詢（2026-07-11 事故：SF_TRANS_HEADER 只載 34% 照樣蓋掉好表、exit 0）。
    expected_rows=None 時跳過檢查（初次鏡像無舊表可保）。
    """
    p = gz_path.replace("'", "''")
    stage = f"{duck}__stage"
    con.execute(
        f'CREATE OR REPLACE TABLE "{stage}" AS SELECT * FROM read_csv('
        f"'{p}', delim=chr(1), header=false, quote='', escape='', "
        f"all_varchar=true, null_padding=true, ignore_errors=true, "
        f"max_line_size=41943040, names={_names_sql(names)})"
    )
    loaded = con.execute(f'SELECT COUNT(*) FROM "{stage}"').fetchone()[0]
    if expected_rows is not None and _should_retry(expected_rows, loaded):
        con.execute(f'DROP TABLE IF EXISTS "{stage}"')
        raise ErpOracleError(
            f"載入明顯短少（db={expected_rows} loaded={loaded}）——保留舊表不覆蓋")
    con.execute(f'DROP TABLE IF EXISTS "{duck}"')
    con.execute(f'ALTER TABLE "{stage}" RENAME TO "{duck}"')
    return loaded


# ── 目標盤點（讀 schema json）─────────────────────────────────────────────
def _schema_json(owner: str) -> dict | None:
    path = os.path.join(_SCHEMA_DIR, f"erp-schema-{owner}-latest.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def iter_targets(schemas: list[str] | None = None) -> Iterator[tuple[str, str, list[dict], int]]:
    """依 schema 優先序 yield (owner, table, columns, est_rows)；schemas=None 用預設全業務 schema。"""
    owners = schemas or _SCHEMA_ORDER
    for owner in owners:
        rep = _schema_json(owner)
        if not rep:
            continue
        for t in rep.get("tables", []):
            yield owner, t["name"], t.get("columns", []), (t.get("est_rows") or 0)


def _is_history_backup(table: str) -> bool:
    """歷史/備份表（通常最肥、分析價值低）→ 排到最後跑。"""
    u = (table or "").upper()
    return any(tok in u for tok in ("_HIS", "_BAK", "_BK", "_BACKUP"))


def _order_targets(targets: list[tuple], schemas: list[str] | None) -> list[tuple]:
    """排序：現行表先於歷史/備份；其次照 schema 優先序；再其次小表先（快速廣覆蓋）。"""
    order = schemas or _SCHEMA_ORDER
    idx = {o: i for i, o in enumerate(order)}
    return sorted(targets, key=lambda t: (
        _is_history_backup(t[1]), idx.get(t[0], 99), t[3]))


def load_manifest(path: str | None = None) -> dict:
    path = path or _MANIFEST   # 呼叫時才解析，尊重測試/維運對模組全域的 patch
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_manifest(man: dict, path: str | None = None) -> None:
    path = path or _MANIFEST   # 呼叫時才解析，尊重測試/維運對模組全域的 patch
    Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _build_catalog(con, man: dict) -> None:
    con.execute("CREATE OR REPLACE TABLE _erp_catalog "
                "(owner VARCHAR, tbl VARCHAR, duck_table VARCHAR, "
                "db_rows BIGINT, loaded_rows BIGINT, cols INTEGER, status VARCHAR)")
    for key, m in sorted(man.items()):
        # "_" 開頭是偽條目（如 _item_alias 對照同步狀態），非 OWNER.TABLE，
        # 進 split(".") 解包會炸；catalog 只收真表。
        if key.startswith("_") or m.get("status") not in ("done", "count_mismatch"):
            continue
        owner, tbl = key.split(".", 1)
        con.execute("INSERT INTO _erp_catalog VALUES (?,?,?,?,?,?,?)",
                    [owner, tbl, m.get("duck"), m.get("db_rows"), m.get("loaded"),
                     m.get("cols"), m.get("status")])


# 每日刷新的營運「熱表」：會天天變、且分析最常用（訂單/當前BOM/採購MRP/庫存/生產/樣品）。
# 歷史/備份大表(_HIS/_BAK，如 SE_BOM_HIS_SIZE 458萬)不在此列——它們幾乎不動、且最肥，
# 靠一次性全量鏡像即可；真要重刷歷史再手動全量跑。
HOT_TABLES = [
    "SC00.SE_ORD_M", "SC00.SE_ORD_ITEM", "SC00.SE_ITEMSCHE_M",
    "SC00.SE_BOM_M", "SC00.SE_BOM_PART", "SC00.SE_BOM_SIZE",
    "SC00.PO_ORDER_M", "SC00.PO_ORDER_D",
    # MRP：v_mrp 實際讀 PO_MRP_ITEM/PO_MRP_M（初版誤放 PO_MRP_ORD/ORDITEM/ORDS——
    # 無任何視圖使用，害 v_mrp 凍在初鏡快照）。PO_MRP_PO=需求→實際下單、PO_RCPT_M/D=收貨，
    # 是「已下單未到貨」缺口分析的來源。
    "SC00.PO_MRP_ITEM", "SC00.PO_MRP_M", "SC00.PO_MRP_PO",
    "SC00.PO_RCPT_M", "SC00.PO_RCPT_D", "SC00.PO_VENDER_M",
    # 委外加工單（DJ*）：PO_MRP_PO 每日刷但 PROC 凍結時，最新委外單在配額表裡
    # 成孤兒（實測凍在 07-07 → 缺 18 張 DJ 單、齊套在途鏈 88 列配額對不到單據）。
    "SC00.PO_PROC_M", "SC00.PO_PROC_D",
    "SC00.IV_TRANS_M", "SC00.IV_TRANS_D", "SC00.IV_STOC_M", "SC00.IV_STOC_D",
    # 庫存可用量分配（PO-POTF_240 畫面底表；~4.8 萬列、有 LAST_DATE 可吃
    # precheck）：2026-07-30 UserA G407 案——「哪天分配給哪張指令訂單」的
    # 唯一權威源，凍在初鏡快照會讓 erp_allocation_lookup 永遠查不到新分配。
    "SC00.PO_ITEM_SELOT",
    "MK00.WK_ORD_M", "MK00.WK_DISPATCH_M", "MK00.SF_TRANS_HEADER", "MK00.SF_TRANS_WK",
    "SP00.SP_PROD_SP", "SP00.SP_ITEM_MIDDLE",
    # 維度/屬性表：本身小（秒級），但視圖 join 全靠它們解碼——不在熱表名單時
    # 新客戶/新型體/新樣品的「客戶名/品牌/BOM」永遠解成 NULL 且不自癒（v_mrp
    # 凍結同型殘留；實測 2026-07 的 15 筆新樣品 0 筆解得出客戶）。RD_BOM_ITEM
    # ~15.5 萬列（~5 分鐘）是「新單要備什麼料」的來源，值得每晚重刷。
    "SC00.SE_CUST", "SC00.RD_ITEM", "SP00.SP_EXCEL_M",
    "SC00.RD_BOM_M", "SC00.RD_BOM_ITEM",
    # 樣品 BOM（43.6 萬列、全量 ~14 分鐘）：原「暫不進名單」，2026-07-29 UserC
    # PU468 案上線 erp_sample_lookup 反查後翻轉——快照凍結會讓新版次/新樣品單
    # 的用料永遠查無（實測 SR2511001 主檔已 v29、快照停在 07-08 的 v28）。
    # 表有 LAST_DATE，precheck 無變動的晚上會整表跳過，實際成本只在有改動日。
    "SP00.SP_PROD_SPBOM",
    # 樣品域治本四表（2026-07-30 sysdba GRANT SELECT TO APP 後可讀）：
    # SP_ITEM_RDITEM=開發料↔量產料對照、SRC_SPNO=「來源樣品單號」唯一權威源
    # （開發料品畫面右下欄；PU468→SR2511002 已實證）；SP_ITEM=料品主檔
    # （O_ITEMNO 庫存編號可直讀，PHOTO BLOB 由 _SKIP_TYPES 自動跳過）；
    # SP_SP_M=樣品單主檔（客戶/交期/狀態）；SP_SPITEM_PART=樣品逐部位用料。
    # ⚠️ 若 DBA 撤授權，這些表會回到 probe 不可見、自然退出鏡像——工具端要查無不炸。
    "SP00.SP_ITEM_RDITEM", "SP00.SP_ITEM", "SP00.SP_SP_M",
    "SP00.SP_SPITEM_PART",
    # 應付/付款域（2026-08-04 UserA/UserM 每日簡報「未完成付款」上線後加入）：
    # AP_APPLY_M 是付款請示單主檔、PAYMENT_ID 為空＝ERP 還沒付款，是「未完成付款」
    # 的唯一確定性判準（purchasing_brief.open_payment_requests）。原本只在初鏡
    # 全量快照裡 → 實測凍在 07-07、比當下晚了近一個月，未付清單會永遠停在上個月
    # 的樣子（Drive 已有 JFPA2607018 而鏡像最新只到 JFPA2607008）。四表都小
    # （APPLY_M 1,510 列、PAY_M 1,016 列，秒級）且有 LAST_DATE 可吃 precheck，
    # 沒改動的晚上整表跳過。
    "GL00.AP_APPLY_M", "GL00.AP_APPLY_D", "GL00.AP_PAY_M", "GL00.AP_PAY_D",
    # 總帳/損益域（2026-09-07 財務長 Phase 1 月損益上線後加入）：傳票主/明細是
    # 月損益唯一計算源（finance_statements.py），原本只在初鏡快照 → 實測凍在
    # 07-07、傳票只到 2026-05，關帳偵測（VCH_TYPE='99' 結轉傳票）永遠停在 04 月。
    # 七表都小（最大 GL_VOUCH_D 9,190 列，秒級）且有 LAST_DATE 可吃 precheck。
    # GL_BALANCES_M 不是計算源，但留著當口徑對帳的第二來源（傳票加總 vs ERP 自己
    # 的餘額表）。
    "GL00.GL_VOUCH_M", "GL00.GL_VOUCH_D", "GL00.GL_BALANCES_M",
    "GL00.GL_ACCT_M", "GL00.GL_ACCT_TYPE", "GL00.GL_BOOK", "GL00.GL_EXCHANGE",
    # 應付對帳/付款排程（2026-09-07 財務長 Phase 2 資金展望上線後加入）：
    # AP_DUE_D.PL_PAY_DATE（計畫付款日，98.6% 有填）＋ PAY_NO 空＝未付，是
    # cash_outlook 逐週付款排程的唯一來源；凍在初鏡快照會讓「未來 N 週要付
    # 多少」永遠停在 7 月的樣子。兩表都有 LAST_DATE 可吃 precheck。
    "GL00.AP_DUE_M", "GL00.AP_DUE_D",
    # 標準價（2026-09-07 財務長 Phase 3 買貴清單上線後加入）：料號×供應商×幣別
    # 的議定價，是 overpriced_purchases「PO 價 vs 標準價」與材料幣別反查的來源；
    # 凍住會讓新談的價永遠比不到。表小（~3K 列），無 LAST_DATE 就整表重刷也秒級。
    "GL00.AP_STDPRICE_ITEM",
]


# ── 庫存編號（舊碼）對照同步 ──────────────────────────────────────────────
# ERP 畫面（如 SE-SETF_440 訂單材料追蹤）的「庫存編號」（SF24/CL14.1 這類倉庫與
# 台灣採購慣用的舊短碼）存在 SP00.SP_ITEM.O_ITEMNO——該表對 APP 唯讀帳號**無
# SELECT 授權**（schema probe 看不到、不在 1,538 張鏡像表內），但 GG_1001.
# GF_ITEM_O_ITEMNO(org, item_no) 函式有 PUBLIC 執行權，可逐料解碼。這裡把鏡像內
# 用到的料號全集（DISTINCT ~3.2K 個）一發查詢解完，落成 _item_alias 表＋
# v_item_alias 視圖。2026-07-28 UserA SF24 案：查「庫存編號 SF24 短少數量」
# 全部確定性工具查無、LLM 憑空答「BOM未選用此材料」（實際 JA1055 BLACK/
# JA1065 BK-RED 生效版 BOM 都在用 SFIXI0500T003600400-A010=SF24）。
_ALIAS_SRC_TABLES = ["SE_ITEMSCHE_M", "IV_STOC_M", "RD_BOM_ITEM",
                     "SE_BOM_PART", "PO_ORDER_D", "PO_MRP_ITEM"]


def sync_item_alias(db_path: str = _DB, *,
                    log: Callable[[str], None] = print) -> dict:
    """把 料號→庫存編號(SP_ITEM.O_ITEMNO 舊碼) 對照同步進 DuckDB。

    治本路徑（2026-07-30 sysdba GRANT SELECT ON SP00.SP_ITEM TO APP 後）：
    直讀 SP_ITEM 全量對照，一發查詢、涵蓋所有建過庫存編號的料品（不再限
    營運熱表出現過的料）。授權被撤（ORA-00942/01031）時自動退回舊法——
    GG_1001.GF_ITEM_O_ITEMNO PUBLIC 函式對熱表 DISTINCT 料號逐料解碼。
    失敗/解出 0 筆回 status!=done 且**保留舊對照表**不覆蓋——對照是加值
    資料，不 raise、不擋鏡像主流程。
    """
    import duckdb
    union = " UNION ".join(f"SELECT ITEM_NO FROM {t}" for t in _ALIAS_SRC_TABLES)
    plans = [
        ("direct", "SELECT ITEM_NO || CHR(1) || O_ITEMNO FROM SP00.SP_ITEM "
                   "WHERE O_ITEMNO IS NOT NULL AND ITEM_NO IS NOT NULL"),
        ("legacy", "SELECT ITEM_NO || CHR(1) || GG_1001.GF_ITEM_O_ITEMNO(1, ITEM_NO) "
                   f"FROM (SELECT DISTINCT ITEM_NO FROM ({union}) "
                   "WHERE ITEM_NO IS NOT NULL)"),
    ]
    pairs: list[tuple[str, str]] = []
    source = ""
    for label, sql in plans:
        pairs = []
        try:
            for line in ssh_sqlplus_stream(sql):
                item_no, _, alias = line.partition("\x01")
                item_no, alias = item_no.strip(), alias.strip()
                if item_no and alias:
                    pairs.append((item_no, alias))
        except ErpOracleError as e:
            if label == "direct":
                log(f"sync_item_alias：SP_ITEM 直讀失敗（{str(e)[:120]}）"
                    "→ 退回 GG_1001 函式解碼")
                continue
            log(f"sync_item_alias：抓取失敗（保留舊對照）：{e}")
            return {"status": "error", "error": str(e)[:200]}
        if pairs:
            source = label
            break
        if label == "direct":
            log("sync_item_alias：直讀 0 筆——退回 GG_1001 函式解碼")
    if not pairs:
        log("sync_item_alias：解出 0 筆對照——異常，保留舊表不覆蓋")
        return {"status": "empty"}
    con = duckdb.connect(db_path)
    try:
        con.execute('CREATE OR REPLACE TABLE "_item_alias__stage" '
                    "(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)")
        con.executemany('INSERT INTO "_item_alias__stage" VALUES (?, ?)', pairs)
        con.execute('DROP TABLE IF EXISTS "_item_alias"')
        con.execute('ALTER TABLE "_item_alias__stage" RENAME TO "_item_alias"')
        con.execute('CREATE OR REPLACE VIEW v_item_alias AS SELECT '
                    'ITEM_NO AS "料號", O_ITEMNO AS "庫存編號" FROM "_item_alias"')
    finally:
        con.close()
    log(f"sync_item_alias：{len(pairs)} 筆 料號→庫存編號 對照已更新"
        f"（{'SP_ITEM 直讀' if source == 'direct' else 'GG_1001 函式'}）")
    return {"status": "done", "rows": len(pairs), "source": source}


def _record_alias_manifest(res: dict, manifest_path: str) -> None:
    """把 sync_item_alias 的結果記進 manifest 的 "_item_alias" 偽條目。

    sync_item_alias 失敗不影響 refresh exit code（設計如此：對照是加值資料、
    不擋鏡像主流程）——但連續失敗多晚沒有任何持久訊號，SF24 這類舊短碼查詢
    會悄悄退化成查無。這裡每晚落帳：status/ts 記本次嘗試；last_done_ts 只在
    成功時前進、失敗時保留；first_bad_ts 在「轉入失敗」那晚定錨、連續失敗不
    重設——dashboard_alerts._check_erp_item_alias 拿兩者算連續失敗多久。
    偽條目 key 帶 "_" 前綴，_build_catalog / staleness 檢查都據此跳過。
    """
    man = load_manifest(manifest_path)
    prior = man.get("_item_alias") or {}
    entry = {"status": res.get("status"), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if res.get("status") == "done":
        entry["rows"] = res.get("rows")
        entry["last_done_ts"] = entry["ts"]
    else:
        if prior.get("last_done_ts"):
            entry["last_done_ts"] = prior["last_done_ts"]
        entry["first_bad_ts"] = (prior.get("first_bad_ts")
                                 if prior.get("status") not in (None, "done")
                                 else None) or entry["ts"]
    man["_item_alias"] = entry
    save_manifest(man, manifest_path)


def refresh_hot(*, log: Callable[[str], None] = print, atomic: bool = True) -> dict:
    """每日刷新營運熱表（force 重灌）。給 launchd 每日 cron 用。

    atomic=True（預設）：先 clone live DB → tmp（APFS clonefile，瞬間零空間），對 tmp
    刷新後 os.replace(tmp, live) 原子換。**刷新期間查詢端永遠打得開 live**（舊 inode），
    避免每晚 ~95 分鐘寫鎖讓小紅查不了（比照 factory_warehouse temp+replace 慣例）。
    刷新失敗則 live 原封不動（tmp 丟棄），不留半成品。atomic=False 走直接寫（測試用）。
    """
    from agent_core.env_utils import env_bool
    skip_unchanged = env_bool("RED_ERP_REFRESH_SKIP_UNCHANGED", True)
    log(f"refresh_hot：重刷 {len(HOT_TABLES)} 張營運熱表"
        f"（atomic={atomic}, skip_unchanged={skip_unchanged}）")
    if not atomic or not os.path.exists(_DB):
        res = mirror(only_tables=HOT_TABLES, force=True,
                     skip_unchanged=skip_unchanged, log=log)
        res["item_alias"] = sync_item_alias(log=log)
        _record_alias_manifest(res["item_alias"], _MANIFEST)
        return res

    import shutil
    import subprocess
    tmp = _DB + ".refresh.tmp"
    man_tmp = _MANIFEST + ".refresh.tmp"
    for stale in (tmp, tmp + ".wal", man_tmp):
        if os.path.exists(stale):
            os.remove(stale)
    try:  # APFS clonefile（cp -c）瞬間零額外空間；非 APFS 退一般複製
        subprocess.run(["cp", "-c", _DB, tmp], check=True, capture_output=True)
    except Exception:  # noqa: BLE001
        shutil.copy2(_DB, tmp)
    # manifest 跟資料一樣走 tmp→swap：不然刷 tmp 途中每表都寫全域 manifest，中途
    # 失敗（deadline exit 75、swap 前例外）tmp 被丟棄、live 還是舊資料，但 manifest
    # ts 已是今天 → staleness 告警被自己的帳本騙過、人工對帳 db_rows/loaded 全失真。
    if os.path.exists(_MANIFEST):
        shutil.copy2(_MANIFEST, man_tmp)
    try:
        res = mirror(only_tables=HOT_TABLES, force=True, db_path=tmp,
                     manifest_path=man_tmp, skip_unchanged=skip_unchanged, log=log)
        # 庫存編號對照跟熱表同一份 tmp、swap 前寫入——live 檔永遠只被 read_only 開。
        res["item_alias"] = sync_item_alias(tmp, log=log)
        _record_alias_manifest(res["item_alias"], man_tmp)   # 隨 manifest 一起 swap 生效
        if os.path.exists(tmp + ".wal"):  # 正常 close 會 checkpoint，保險再清
            os.remove(tmp + ".wal")
        os.replace(tmp, _DB)             # 原子換：讀者舊 fd 續用舊 inode、新連線拿新檔
        if os.path.exists(man_tmp):      # 資料換成功才讓 manifest 生效
            os.replace(man_tmp, _MANIFEST)
        return res
    finally:
        for leftover in (tmp, tmp + ".wal", man_tmp):  # 失敗時清 tmp、live 保持原樣
            if os.path.exists(leftover):
                os.remove(leftover)


def mirror(schemas: list[str] | None = None, *, db_path: str = _DB,
           keep_raw: bool = False, limit_tables: int | None = None,
           only_tables: list[str] | None = None, force: bool = False,
           manifest_path: str | None = None, skip_unchanged: bool = False,
           log: Callable[[str], None] = print) -> dict:
    """鏡像進 DuckDB。可續跑（manifest status==done 跳過，force=True 則不跳、強制重灌）。

    skip_unchanged=True：先跑一發 SSH 預檢（COUNT+變更訊號，見 _precheck_signal），
    與上次真刷相同的表跳過重刷（manifest ts 照樣打點=「今天驗過沒變」，staleness
    告警不誤響）。回 summary。"""
    import duckdb  # lazy：只有真跑鏡像才需要

    manifest_path = manifest_path or _MANIFEST
    Path(_DIR).mkdir(parents=True, exist_ok=True)
    Path(_RAW_DIR).mkdir(parents=True, exist_ok=True)
    man = load_manifest(manifest_path)
    con = duckdb.connect(db_path)
    targets = _order_targets(list(iter_targets(schemas)), schemas)
    if only_tables:
        want = set(only_tables)
        targets = [t for t in targets if f"{t[0]}.{t[1]}" in want or t[1] in want]
    if limit_tables:
        targets = targets[:limit_tables]
    total = len(targets)
    done = errors = mism = skipped_tbl = 0
    # SSH 傳輸層斷線（ErpConnectionError）的單表重試：深夜 darkwake/idle sleep 會切斷
    # 長串流（2026-07-25 兩張熱表實例，窗口實測約 2 分鐘），等一下重連通常就過。
    # ORA-/SP2- 確定性錯誤不在此列（重跑必然再錯，浪費 wall-clock deadline）。
    ssh_retries = env_int("RED_ERP_SSH_RETRY", 1, min_value=0, max_value=3)
    ssh_retry_wait = env_int("RED_ERP_SSH_RETRY_WAIT_S", 60, min_value=0, max_value=600)

    precheck_stats: dict[str, tuple[int, str, str]] = {}
    skipped_fresh = 0
    if skip_unchanged and targets:
        unchanged, precheck_stats = precheck_hot_unchanged(targets, man, log)
        if unchanged:
            now_s = time.strftime("%Y-%m-%d %H:%M:%S")
            for owner, table, _cols, _est in targets:
                key = f"{owner}.{table}"
                if key in unchanged:
                    m = man.get(key) or {}
                    m["ts"] = now_s          # 「今天驗過沒變」也是新鮮
                    m["precheck"] = "unchanged"
                    man[key] = m             # 保留 full_ts（不是真刷，別更新）
            save_manifest(man, manifest_path)
            targets = [t for t in targets if f"{t[0]}.{t[1]}" not in unchanged]
            skipped_fresh = len(unchanged)
            log(f"precheck：{skipped_fresh}/{total} 張熱表 COUNT+變更訊號 "
                f"與上次相同 → 跳過重刷")

    try:
        for i, (owner, table, columns, _est) in enumerate(targets, 1):
            key = f"{owner}.{table}"
            if not force and man.get(key, {}).get("status") == "done":
                done += 1
                continue
            duck = duck_table(owner, table)
            gz = os.path.join(_RAW_DIR, f"{duck}.csv.gz")
            t0 = time.time()
            try:
                sql, kept, skip_cols, width = build_select(owner, table, columns)
                if sql is None:
                    man[key] = {"status": "no_columns", "skipped_cols": skip_cols,
                                "duck": duck, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                    skipped_tbl += 1
                    save_manifest(man, manifest_path)
                    log(f"[{i}/{total}] {key}: 跳過（無可鏡像欄位）")
                    continue
                conn_breaks = 0
                while True:
                    try:
                        db_rows = count_rows(owner, table)
                        # SSH 串流偶爾中途 EOF（ssh_sqlplus_stream 已改為 returncode 非零即
                        # raise，這裡的統計門檻是對「rc=0 但仍短少」的第二道防線）。串流明顯
                        # 少於 COUNT（非幾列 live churn）就自動重抽一次；重抽仍短 → 放棄本表
                        # （raise → 保留舊表），寧舊勿缺。對帳在「載入之前」做，短資料不落 DB。
                        # 2026-07-11 實例：SF_TRANS_HEADER 192098→只串 66085，短表照樣上線 7.5h。
                        streamed = stream_to_gz(sql, gz)
                        retried = False
                        if _should_retry(db_rows, streamed):
                            log(f"[{i}/{total}] {key}: 串流短少（db={db_rows} streamed={streamed}）→ 重抽一次")
                            streamed = stream_to_gz(sql, gz)
                            retried = True
                        if _should_retry(db_rows, streamed):
                            raise ErpOracleError(
                                f"串流兩次皆明顯短少（db={db_rows} streamed={streamed}）——保留舊表不覆蓋")
                        loaded = load_gz_into_duckdb(con, duck, gz, kept, expected_rows=db_rows)
                        break
                    except ErpConnectionError as cex:
                        conn_breaks += 1
                        if conn_breaks > ssh_retries:
                            raise
                        log(f"[{i}/{total}] {key}: SSH 連線中斷（{str(cex)[:120]}）"
                            f"→ 等 {ssh_retry_wait}s 後重試（{conn_breaks}/{ssh_retries}）")
                        time.sleep(ssh_retry_wait)
                status = "done" if (db_rows == streamed == loaded) else "count_mismatch"
                if status == "count_mismatch":
                    mism += 1
                now_s = time.strftime("%Y-%m-%d %H:%M:%S")
                man[key] = {
                    "status": status, "db_rows": db_rows, "streamed": streamed,
                    "loaded": loaded, "cols": len(kept), "skipped_cols": skip_cols,
                    "wide_risk": width > _LINESIZE, "duck": duck, "retried": retried,
                    "secs": round(time.time() - t0, 1), "ts": now_s,
                    # full_ts=這次真的全量刷了；預檢跳過只更新 ts、不動 full_ts，
                    # 週兜底靠 full_ts 判「距上次真刷多久」。
                    "full_ts": now_s,
                }
                if conn_breaks:
                    man[key]["ssh_retries"] = conn_breaks   # 可觀測：這表靠重連救回
                if key in precheck_stats:
                    # 供下晚預檢比對。值取自「串流之前」的預檢查詢——若中間有新
                    # 寫入，記錄偏舊 → 下晚判 changed 多刷一次（安全側）。sigkey 決定
                    # 存 last_date 還是 row_scn（隨這張表用哪個變更訊號）。
                    _cnt, _sig, _sigkey = precheck_stats[key]
                    man[key][_sigkey] = _sig
                done += 1 if status == "done" else 0
                flag = "" if status == "done" else f"  ⚠️{status}(db={db_rows} load={loaded})"
                wr = "  🟠wide" if width > _LINESIZE else ""
                log(f"[{i}/{total}] {key}: {loaded:,} 列 / {len(kept)} 欄  "
                    f"{man[key]['secs']}s{flag}{wr}")
                if not keep_raw:
                    try:
                        os.remove(gz)
                    except OSError:
                        pass
            except Exception as ex:  # noqa: BLE001 - 單表失敗不該中斷全庫
                errors += 1
                man[key] = {"status": "error", "error": str(ex)[:300], "duck": duck,
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                log(f"[{i}/{total}] {key}: ERROR {str(ex)[:160]}")
                if not keep_raw:  # 失敗路徑也清 gz，免得 raw/ 越積越肥
                    try:
                        os.remove(gz)
                    except OSError:
                        pass
            save_manifest(man, manifest_path)
        _build_catalog(con, man)
    finally:
        con.close()

    summary = {"total": total, "done": done, "errors": errors,
               "count_mismatch": mism, "no_columns": skipped_tbl,
               "skipped_fresh": skipped_fresh, "db_path": db_path}
    fresh_note = f"；{skipped_fresh} 預檢無變化跳過" if skipped_fresh else ""
    log(f"完成：{done}/{total - skipped_fresh} 表 OK；{mism} 對帳不符；{errors} 失敗；"
        f"{skipped_tbl} 無欄位跳過{fresh_note}。")
    return summary
