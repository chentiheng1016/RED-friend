"""飛越 ERP（Oracle 10.2.0.5）唯讀查詢 — 走 SSH + 主機自帶 sqlplus。

為什麼不用 python-oracledb：DB 是 10.2（10g），thin 模式只支援 12.1+，而 macOS 上
拿不到能連 10g 的 Instant Client（19c/23ai client 最低只連 11.2 server）。所以查詢
**在 ERP 主機上用它自己的 10g sqlplus 執行**（版本完全相符、本來就連得到 JHDB），
RED 透過 SSH 把 SELECT 餵過去、取回 tab 分隔的結果。實測見 docs/erp_db_readonly_design.md。

連線鏈（已驗證）：RED ──SSH(金鑰,只開22給RED IP)──▶ ERP 主機 ──本機 sqlplus──▶ Oracle(JHDB)

三道唯讀防線：① DB 帳號只給 SELECT ② 本檔 guard_select_only 程式守門 ③ 不提供寫入工具。
所有設定走 env / secret_provider，預設 RED_ERP_ORACLE_ENABLED=0（清毒/tunnel 前不啟用）。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading

from agent_core.env_utils import env_bool, env_int
from agent_core.secret_provider import get_secret

# 主機位址不放 repo（repo 曾公開曝露過）：env RED_ERP_SSH_HOST → keyring
# xiaohong-agent/erp_ssh_host，兩者皆空時 _cfg() 直接報錯。
_DEFAULT_USER = "Administrator"
_DEFAULT_TNS = "JHDB"          # 主機 tnsnames 別名（背後 service JHNEW）
_DEFAULT_KEY = "~/.ssh/id_erp_jhdb"


class ErpOracleError(RuntimeError):
    """ERP Oracle 操作的友善錯誤（訊息可直接轉述）。"""


class ErpConnectionError(ErpOracleError):
    """SSH 傳輸層失敗（ssh exit 255／輸出結束但 ssh 不退）——重試有意義。

    與 ORA-/SP2- 這類確定性錯誤（重跑必然再錯）區分：深夜 darkwake/idle sleep
    會切斷長時間串流（2026-07-25 兩張熱表實例），這類錯誤等一下重連通常就過。
    """


_SSH_EXIT_RC = 255   # ssh 本身失敗（連線斷/連不上）的退出碼；sqlplus 錯誤是其他非零值


# ── 唯讀守門 ──────────────────────────────────────────────────────────────
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|"
    r"commit|rollback|begin|declare|execute|call|lock|savepoint|set|comment|"
    r"flashback|purge|rename|analyze|audit)\b",
    re.IGNORECASE,
)
_STARTS_OK = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
# 網路 / OS / 動態執行類套件 —— 唯讀查訂單/料況根本用不到，但攻擊者一旦能把 SQL
# 注入本工具（prompt injection），utl_http.request(...) 可從 ERP 網內把資料外送、
# dbms_lock.sleep(...) 可癱瘓 session、dbms_* 可繞道執行。純加碼黑名單、與通道無關，
# 即使 daemon 通道 CONFIRM 自動放行也擋得住。10g 這些套件常 default grant 給 PUBLIC。
_DANGER_PKG = re.compile(
    r"\b(utl_http|utl_tcp|utl_smtp|utl_inaddr|utl_url|utl_file|httpuritype|"
    r"dbms_lock|dbms_pipe|dbms_scheduler|dbms_job|dbms_java|dbms_sql|"
    r"dbms_xmlgen|dbms_xslprocessor|dbms_ldap|dbms_advisor|dbms_sys_sql|"
    r"dbms_backup_restore|dbms_aq|dbms_aqadm)\b",
    re.IGNORECASE,
)
# Oracle 字串字面值（'' 為內嵌單引號）與雙引號識別碼。偵測「多語句 / 寫入關鍵字」
# 前先把這些挖掉，否則資料值會被誤判：IN ('INSERT','DELETE') 不是寫入、
# WHERE note = ';' 也不是多語句。挖掉後語句層級的 DML/DDL/FOR UPDATE 仍會被擋。
# 另挖掉註解（避免用 /* ; */ 或 -- 藏分號/關鍵字繞過檢查）與 Oracle q'[]' 替代引號。
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"", re.DOTALL)
_COMMENT_RE = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
_QQUOTE_RE = re.compile(
    r"[nN]?[qQ]'(?:\[.*?\]|\{.*?\}|\(.*?\)|<.*?>)'", re.DOTALL
)


def _strip_literals(sql: str) -> str:
    # 先挖註解與 q'[]' 替代引號，再挖一般字串/識別碼 —— 順序重要：q'[a'b]' 內含的
    # 單引號不能先被一般字串規則配對走，否則會錯位遮蔽掉後面的真實關鍵字。
    out = _COMMENT_RE.sub(" ", sql)
    out = _QQUOTE_RE.sub(" ", out)
    return _LITERAL_RE.sub(" ", out)


def guard_select_only(sql: str) -> str:
    """只放行單條 SELECT/WITH；否則 raise ErpOracleError。回傳清過尾分號的 sql。"""
    if not sql or not sql.strip():
        raise ErpOracleError("查詢是空的。")
    s = sql.strip().rstrip(";").strip()
    if not _STARTS_OK.match(s):
        raise ErpOracleError("唯讀工具只允許 SELECT / WITH 查詢。")
    # 只看程式碼骨架（挖掉字串/註解/識別碼）來判多語句與寫入關鍵字，避免誤殺資料值。
    bare = _strip_literals(s)
    if ";" in bare:
        raise ErpOracleError("一次只能跑一條查詢（偵測到分號分隔的多語句）。")
    hit = _FORBIDDEN.search(bare)
    if hit:
        raise ErpOracleError(f"查詢含不允許的關鍵字「{hit.group(0)}」（唯讀工具只能讀，不能改）。")
    pkg = _DANGER_PKG.search(bare)
    if pkg:
        raise ErpOracleError(f"查詢含不允許的套件「{pkg.group(0)}」（唯讀工具禁用網路/OS/動態執行類套件）。")
    return s


def validate_identifier(value: str, field: str = "識別碼") -> str:
    """驗證單號/編號這類要內嵌進 SQL 的識別碼（只允許英數 . _ -，≤40）。防注入。"""
    v = (value or "").strip()
    if not _IDENT_RE.match(v):
        raise ErpOracleError(f"{field}格式不合法（只允許英數與 . _ -，長度 1–40）。")
    return v


# ── 設定 ──────────────────────────────────────────────────────────────────
def is_enabled() -> bool:
    """是否已啟用 ERP 查詢（預設關，避免清毒/SSH 就緒前誤用）。"""
    return env_bool("RED_ERP_ORACLE_ENABLED", False)


def max_rows() -> int:
    return env_int("RED_ERP_ORACLE_MAX_ROWS", 200, min_value=1, max_value=5000)


def _timeout_s() -> int:
    return env_int("RED_ERP_ORACLE_TIMEOUT_S", 40, min_value=5, max_value=600)


def _cfg() -> dict:
    key = os.path.expanduser(
        os.environ.get("RED_ERP_SSH_KEY", "").strip() or _DEFAULT_KEY
    )
    host = (
        os.environ.get("RED_ERP_SSH_HOST", "").strip()
        or get_secret("ERP_SSH_HOST", keyring_name="erp_ssh_host").value
    )
    if not host:
        raise ErpOracleError(
            "找不到 ERP 主機位址（設 RED_ERP_SSH_HOST，或 keyring xiaohong-agent/erp_ssh_host）。"
        )
    return {
        "key": key,
        "user": os.environ.get("RED_ERP_SSH_USER", _DEFAULT_USER).strip() or _DEFAULT_USER,
        "host": host,
        "tns": os.environ.get("RED_ERP_ORACLE_TNS", _DEFAULT_TNS).strip() or _DEFAULT_TNS,
        "db_user": get_secret("ERP_ORACLE_USER", keyring_name="erp_oracle_user").value or "APP",
        "db_pass": get_secret("ERP_ORACLE_PASSWORD", keyring_name="erp_oracle_password").value or "APP",
    }


# sqlplus 輸出前置：關掉所有裝飾、tab 分隔、寬行
_SQLPLUS_PREAMBLE = (
    "SET PAGESIZE 0\nSET FEEDBACK OFF\nSET HEADING OFF\nSET VERIFY OFF\n"
    # DEFINE OFF：關掉 & / && 替代變數展開，否則查詢裡的 & 會觸發 sqlplus 向
    # stdin 索取變數值（吃掉 EXIT 而卡住），或被拿來當注入手段。唯讀查詢用不到。
    "SET DEFINE OFF\n"
    # TAB OFF：sqlplus 預設會把輸出裡的連續空白壓成 tab（右對齊數字的前導空白、
    # 自由文字欄內的長空白串都會中招）。fetch_table 靠 CHR(9) 切欄，資料被注入
    # tab = 欄位錯位、數值對錯欄——clean_text_col 只能剝資料「裡」的 tab，防不到
    # sqlplus 在輸出端自己轉出來的。實錘：count_rows 曾因前導 tab 誤讀（45ce55a6）。
    "SET TAB OFF\n"
    "SET TRIMSPOOL ON\nSET TRIMOUT ON\nSET LINESIZE 32767\nSET LONG 200000\n"
    "WHENEVER SQLERROR EXIT FAILURE\n"
)


def _ssh_sqlplus(script_body: str, timeout_s: int | None = None) -> str:
    """把 sqlplus 腳本(本文，不含 connect/preamble)透過 SSH 餵給主機 sqlplus，回 stdout。

    - 帳密用 CONNECT 寫在 stdin（不放進 argv，避免出現在主機 process 列表）。
    - NLS_LANG=AL32UTF8 讓中文以 UTF-8 回傳。
    - 偵測 ORA-/SP2- 錯誤 → raise ErpOracleError。
    - timeout_s：整體 wall-clock 上限；None 用 RED_ERP_ORACLE_TIMEOUT_S（預設 40s）。
      批次腳本（如鏡像預檢的多表 COUNT）要自帶較大的值。
    """
    if not is_enabled():
        raise ErpOracleError(
            "ERP 查詢尚未啟用（RED_ERP_ORACLE_ENABLED=0）。主機清毒 + SSH 就緒後再開。"
        )
    c = _cfg()
    if not os.path.exists(c["key"]):
        raise ErpOracleError(f"找不到 SSH 金鑰：{c['key']}（設 RED_ERP_SSH_KEY）。")

    effective_timeout = timeout_s or _timeout_s()
    stdin = (
        f"CONNECT {c['db_user']}/{c['db_pass']}@{c['tns']}\n"
        + _SQLPLUS_PREAMBLE
        + script_body
        + "\nEXIT\n"
    )
    remote_cmd = "set NLS_LANG=AMERICAN_AMERICA.AL32UTF8 && sqlplus -S -L /nolog"
    argv = [
        "ssh", "-i", c["key"],
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={min(20, effective_timeout)}",
        f"{c['user']}@{c['host']}",
        remote_cmd,
    ]
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=effective_timeout
        )
    except subprocess.TimeoutExpired as e:
        raise ErpOracleError(f"ERP 查詢逾時（>{effective_timeout}s）。") from e
    except Exception as e:
        raise ErpOracleError("SSH 執行失敗：" + str(e).splitlines()[0][:200]) from e

    out = proc.stdout or ""
    err = (proc.stderr or "").strip()
    # sqlplus 把錯誤印在 stdout；SP2-0267(SET LONG) 之類無害，但 ORA-/login 失敗要擋
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("ORA-") or s.startswith("SP2-0640") or "ERROR at line" in s:
            raise ErpOracleError("ERP 查詢錯誤：" + s[:200])
    if proc.returncode != 0:
        # 有部分輸出也要擋：ssh 中途斷線（rc=255）時 stdout 是被截斷的半份結果，
        # 靜默接受＝下游拿短資料當完整對帳（比報錯更危險）。
        cls = ErpConnectionError if proc.returncode == _SSH_EXIT_RC else ErpOracleError
        raise cls(
            f"ERP 查詢失敗（exit {proc.returncode}，輸出可能不完整）："
            + (err or "無 stderr")[:200]
        )
    # CONNECT 會回 "Connected." ─ 不是資料，濾掉
    return "\n".join(ln for ln in out.splitlines() if ln.strip() != "Connected.")


def clean_text_col(col: str) -> str:
    """包住會進 fetch_table CHR(9) 分隔輸出的自由文字欄，剝掉 CHR(9/10/13)。

    fetch_table 以「一物理行=一列、CHR(9) 分欄」解析。自由文字欄（品名/客戶名/供應商名…
    VARCHAR2 自由輸入）若含真換行(CHR10/13)或 tab(CHR9)，sqlplus 會把一筆印成多物理行
    或多切一欄 → 下游數值欄靜默對錯欄、給出錯的對帳數字（比報錯更危險）。在 SELECT 端把
    這些字元換成空白即根治（零 round-trip、零 runtime 成本；NULL 經 REPLACE 仍是 NULL）。
    高階工具組每個自由文字欄都應包這個。
    """
    return f"REPLACE(REPLACE(REPLACE({col},CHR(10),' '),CHR(13),' '),CHR(9),' ')"


def fetch_table(delimited_select: str, headers: list[str], limit: int | None = None) -> dict:
    """跑一條「每列產生一個 CHR(9) 分隔字串」的 SELECT，回 {columns, rows, truncated}。

    delimited_select 形如：SELECT a||CHR(9)||b||CHR(9)||c FROM t WHERE ...
    （由高階工具自己組欄位，故能用 tab 精準切欄。）
    自由文字欄要先過 clean_text_col()，否則欄內的換行/tab 會打散切列切欄。
    """
    guard_select_only(delimited_select)
    cap = limit if limit is not None else max_rows()
    capped = f"SELECT * FROM ({delimited_select}) WHERE ROWNUM <= {cap + 1}"
    out = _ssh_sqlplus(capped + ";")
    rows: list[list[str]] = []
    for ln in out.splitlines():
        if ln.strip() == "":
            continue
        cells = ln.split("\t")
        # 尾端欄位 NULL 時，TRIMOUT 會把行尾的分隔 tab 一起剪掉 → 短列。下游多處
        # dict(zip(columns, row)) 直接下標，短列會 KeyError 炸整個工具（LEFT_QTY、
        # 交廠日 NULL 是完全正常的業務狀態）。在咽喉點統一補齊空字串。
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        rows.append(cells)
    truncated = len(rows) > cap
    return {"columns": headers, "rows": rows[:cap], "truncated": truncated}


def run_raw_text(select_sql: str, limit: int | None = None) -> str:
    """ad-hoc 任意 SELECT：回 sqlplus 預設欄位排版的純文字（含表頭）。"""
    safe = guard_select_only(select_sql)
    cap = limit if limit is not None else max_rows()
    capped = f"SELECT * FROM ({safe}) WHERE ROWNUM <= {cap}"
    # ad-hoc 要表頭，覆寫 preamble 的 HEADING OFF / PAGESIZE 0
    body = "SET HEADING ON\nSET PAGESIZE 50000\n" + capped + ";"
    return _ssh_sqlplus(body).rstrip()


# 串流版 preamble：不設 ROWNUM cap（全量），ARRAYSIZE 拉高減少往返；其餘同 _SQLPLUS_PREAMBLE。
# TAB OFF 理由同上——鏡像走 CHR(1) 分隔不會錯位，但 cell 內的連續空白被轉成 tab
# 一樣是靜默改寫資料（實測鏡像裡已有 NAME_T/NOTE 帶 tab）。
_STREAM_PREAMBLE = (
    "SET PAGESIZE 0\nSET FEEDBACK OFF\nSET HEADING OFF\nSET VERIFY OFF\n"
    "SET DEFINE OFF\nSET TAB OFF\nSET TRIMSPOOL ON\nSET TRIMOUT ON\nSET LINESIZE 32767\n"
    "SET LONG 100000\nSET LONGCHUNKSIZE 100000\nSET ARRAYSIZE 2000\n"
    "WHENEVER SQLERROR EXIT FAILURE\n"
)


def ssh_sqlplus_stream(select_sql: str):
    """串流跑一條 SELECT，逐行 yield stdout（**不**把整表吃進記憶體）——供大表全量鏡像。

    與 _ssh_sqlplus（subprocess.run 整包 capture）不同：百萬列表若整包 capture 會 OOM，
    故這裡用 Popen 邊讀邊 yield。呼叫端負責在 SELECT 內自組欄位分隔（如 ||CHR(1)||）且
    確保自由文字欄已剝掉換行（CHR10/13），否則「一物理行=一列」會被打散。過 guard_select_only。
    偵測 ORA-/SP2-0640/login 失敗 → raise ErpOracleError。
    掛死偵測靠 SSH keepalive（ServerAliveInterval 30 × CountMax 3 ≈ 90s 無回應即斷、
    讀取端拋錯），故不設整體 wall-clock（大表串流本來就可能跑很久）。
    """
    safe = guard_select_only(select_sql)
    if not is_enabled():
        raise ErpOracleError(
            "ERP 查詢尚未啟用（RED_ERP_ORACLE_ENABLED=0）。主機清毒 + SSH 就緒後再開。"
        )
    c = _cfg()
    if not os.path.exists(c["key"]):
        raise ErpOracleError(f"找不到 SSH 金鑰：{c['key']}（設 RED_ERP_SSH_KEY）。")
    stdin = (
        f"CONNECT {c['db_user']}/{c['db_pass']}@{c['tns']}\n"
        + _STREAM_PREAMBLE + safe + ";\nEXIT\n"
    )
    remote_cmd = "set NLS_LANG=AMERICAN_AMERICA.AL32UTF8 && sqlplus -S -L /nolog"
    argv = [
        "ssh", "-i", c["key"],
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        f"{c['user']}@{c['host']}",
        remote_cmd,
    ]
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    # stderr 用背景執行緒邊讀邊丟（只留尾巴）：不讀的話 ssh 灌滿 64KB pipe 會死鎖，
    # 讀了尾巴還能在異常結束時給出「為什麼斷」的線索。
    stderr_tail: list[str] = []

    def _drain_stderr() -> None:
        try:
            for ln in proc.stderr:
                s = ln.strip()
                if s:
                    stderr_tail.append(s)
                    del stderr_tail[:-5]
        except Exception:  # noqa: BLE001 - drain 執行緒永不拖累主流程
            pass

    drain = threading.Thread(target=_drain_stderr, daemon=True)
    drain.start()
    try:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
            for raw in proc.stdout:
                line = raw.rstrip("\n").rstrip("\r")
                if not line or line == "Connected.":
                    continue
                # SP2-0027=輸入行過長(整條被忽略)、SP2-0640=未登入；連同 ORA-/ERROR 都當致命，
                # 否則像 SP2-0027 這種「SELECT 根本沒跑」會被誤當成正常空結果。
                if (line.startswith("ORA-") or line.startswith("SP2-0640")
                        or line.startswith("SP2-0027") or "ERROR at line" in line):
                    proc.kill()
                    raise ErpOracleError("ERP 串流查詢錯誤：" + line[:200])
                yield line
        except (OSError, UnicodeDecodeError) as ex:
            # rc 還沒讀到就死的傳輸層失敗也要歸類為可重試：ssh 在連線建立中陣亡
            # → stdin.write 拋 BrokenPipeError；串流在多位元組字元中間被切斷
            # → text 模式讀 stdout 拋 UnicodeDecodeError。不轉的話會漏過鏡像端
            # 的單表重試（Codex review P2, PR #286）。
            proc.kill()
            raise ErpConnectionError(
                f"ERP 串流傳輸中斷（{type(ex).__name__}）：{str(ex)[:200]}") from ex
        # stdout EOF ≠ 成功：ssh 中途斷線（EOF 短抽）跟正常跑完在讀取端長得一模一樣，
        # 唯一的確定性訊號是 returncode（ssh 斷=255、sqlplus SQLERROR=非零）。不檢查
        # 的話短抽只能靠下游統計門檻猜（2026-07-11 三張熱表短抽事故的根因）。
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise ErpConnectionError("ERP 串流輸出結束但 ssh 未退出（>30s）——視為連線異常。")
        drain.join(timeout=5)
        if rc != 0:
            tail = " / ".join(stderr_tail[-3:])
            cls = ErpConnectionError if rc == _SSH_EXIT_RC else ErpOracleError
            raise cls(
                f"ERP 串流異常結束（exit {rc}，輸出可能短少）"
                + (f"：{tail[:200]}" if tail else "")
            )
    finally:
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
