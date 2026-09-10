"""ERP 主機 RDP 安全巡檢 — 隨每日 02:00 erp_mirror_refresh 順跑。

背景（2026-07 判讀）：FUCHUN-ERP（阿里雲 Windows Server）3389 曝露公網、被殭屍網路
分散式暴力破解。RdpCoreTS event 140 只記「帳號不存在」的嘗試；猜中真帳號名的失敗只在
Security log 4625（SubStatus 區分帳號存不存在），登入成功是 4624（RDP 互動=LogonType 10）。

這支經 erp_oracle_client 同一條 SSH 通道（金鑰/user/host 同組 env），在主機上跑
powershell 統計近 24h：
  - 4625 失敗登入：來源 IP Top N、被猜帳號 Top N、「真實存在帳號」的失敗
    （SubStatus=0xC000006A 密碼錯 等）per 帳號與 per (帳號,IP)
  - 4624 LogonType 10（RDP 互動登入）成功清單（XPath 直濾，不受 4625 洪水排擠）

引號逸出：遠端命令要過 cmd.exe → powershell 兩層 shell。這裡用
`powershell -Command -` 把腳本本體從 stdin 餵進去 — 命令列上零腳本內容，
兩層逸出問題整個消失，也不用擔心 cmd 對引號/^/% 的各種咬法。
⚠️別改回 `-EncodedCommand <base64>`：那是駭客常用手法，阿里雲雲安全中心會把巡檢自己
判成「蠕蟲病毒命令」serious 告警（2026-07-22～07-26 每晚誤報一次）。加白等於把這條對
真攻擊有效的偵測鈍化，這台又是確認長期被入侵過的主機——寧可換寫法、留著偵測。
stdin 走法要求腳本每個 try/catch 自成一行（見 _build_ps_script），逐行解讀也不會炸。

結果寫 var/data/erp_security/rdp_audit_latest.json（+ 歷史 jsonl），並在報告內預判兩面旗
（白名單/門檻在巡檢端裁決，policy env 集中在 erp_mirror_refresh plist 一處）：
  - 4624 type10 來自非白名單來源 IP → crit（疑似已淪陷）
  - 真實帳號被密集猜密碼（單帳號 24h 失敗 ≥ 門檻）→ warn/crit
dashboard_alerts._check_erp_rdp_security 讀旗標 → alert_pusher → Telegram（不經 Gemini）。

安全邊界：
  - 遠端輸出全是攻擊者可控字串（被猜的帳號名可以是任意 payload）——寫檔前逐欄
    截長 + 過 sanitize_for_llm，之後任何 LLM 路徑（system_alerts、報告查詢）拿到的
    都已是淨化版。
  - 本模組不註冊為 LLM 工具；遠端命令是固定腳本 + env 整數參數，無注入面。
  - 巡檢失敗不可拖垮鏡像主流程：入口 run_patrol_safe 永不 raise，失敗只記 log。

env 旋鈕（都吃 erp_mirror_refresh plist / shell env）：
  RED_ERP_SEC_AUDIT_ENABLED=1        巡檢總開關（另需 RED_ERP_ORACLE_ENABLED=1）
  RED_ERP_RDP_ALLOWED_IPS            4624 type10 白名單來源 IP（逗號分隔，**加在**內建
                                     127.0.0.1/::1 之上；預設空 = 只認 SSH tunnel 內的 RDP）
  RED_ERP_RDP_REAL_FAIL_WARN=5       單一真實帳號 24h 失敗次數 warn 門檻
  RED_ERP_RDP_REAL_FAIL_CRIT=50      同上 crit 門檻
  RED_ERP_SEC_AUDIT_WINDOW_H=24      統計視窗（小時）
  RED_ERP_SEC_AUDIT_MAX_EVENTS=50000 4625 掃描上限（暴力破解時 24h 可能爆量）
  RED_ERP_SEC_AUDIT_TOP_N=20         Top N
  RED_ERP_SEC_AUDIT_TIMEOUT_S=300    SSH + 遠端統計 wall-clock 上限
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

from agent_core.env_utils import env_bool, env_int
from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text
from agent_core.prompt_injection import sanitize_for_llm


class ErpSecurityPatrolError(RuntimeError):
    """巡檢失敗的友善錯誤（訊息可直接進 daemon log）。"""


_REPORT_DIR = os.path.join(DATA_DIR, "erp_security")
REPORT_PATH = os.path.join(_REPORT_DIR, "rdp_audit_latest.json")
_HISTORY_PATH = os.path.join(_REPORT_DIR, "rdp_audit_history.jsonl")

# 4625 SubStatus → 「帳號真實存在」的失敗碼（0xC0000064=帳號不存在，刻意不在列）：
# 006A 密碼錯 / 006F 時段限制 / 0070 不允許此機 / 0071 密碼過期 / 0072 帳號停用 /
# 0193 帳號過期 / 0224 須改密碼 / 0234 帳號鎖定。攻擊者打到這些碼＝已摸到正確帳號名。
_REAL_ACCOUNT_SUBSTATUS = (
    "0xC000006A", "0xC000006F", "0xC0000070", "0xC0000071",
    "0xC0000072", "0xC0000193", "0xC0000224", "0xC0000234",
)

# 內建永遠放行的 4624 type10 來源：SSH tunnel 內轉發的 RDP 看起來是 loopback，
# 那正是被授權的存取路徑。RED_ERP_RDP_ALLOWED_IPS 只做「加白」不做覆蓋——
# 否則大王設了辦公室 IP 反而把 tunnel 路徑誤標成入侵。
_BUILTIN_ALLOWED_IPS = frozenset({"127.0.0.1", "::1"})

_MAX_FIELD_LEN = 200      # 攻擊者可控字串（帳號名等）一律截長，防 payload 塞爆報告
_MAX_ROWS_PER_LIST = 200  # 每張清單上限（rdp_logons 遠端已截 500，這裡再保險）


# ── 遠端 PowerShell 統計腳本 ──────────────────────────────────────────────
def _build_ps_script(window_h: int, top_n: int, max_events: int) -> str:
    """組遠端統計腳本。參數強制 int（呼叫端來自 env_int）——腳本本體是固定字串，
    唯三可變處都是整數字面值，沒有字串插值注入面。"""
    w, n, m = int(window_h), int(top_n), int(max_events)
    subs = "','".join(_REAL_ACCOUNT_SUBSTATUS)
    return (
        "$ErrorActionPreference='SilentlyContinue'\n"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
        f"$w={w}\n"
        f"$n={n}\n"
        f"$m={m}\n"
        "$since=(Get-Date).AddHours(-$w)\n"
        "$fErr=''\n"
        "$sErr=''\n"
        # 4625 用 FilterHashtable + MaxEvents 上限（暴力破解時 24h 可能幾十萬條）。
        # 兩個查詢都走 -ErrorAction Stop + try/catch：查詢失敗（權限不足/壞 XPath）
        # 必須讓巡檢紅（ok=0 → Python 端 raise 進 daemon log），不能被吞成
        # 「0 筆=乾淨」的假陰性；唯獨 NoMatchingEventsFound（查無事件）是合法空結果
        # （Get-WinEvent 對零筆命中是丟 error 而非回空，得特判）。
        # try/catch 必須擠在同一行：腳本走 stdin 餵給 `powershell -Command -`，
        # 若 catch 落到下一行，逐行解讀的模式會把它當孤兒 catch 而 parse error。
        "try{$f=@(Get-WinEvent -FilterHashtable @{LogName='Security';Id=4625;StartTime=$since}"
        " -MaxEvents $m -ErrorAction Stop)}"
        "catch{$f=@();if($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*')"
        "{$fErr=[string]$_}}\n"
        # 4624 直接 XPath 濾 LogonType=10：真 RDP 登入一天沒幾筆，卻可能被服務/網路
        # 登入的 4624 洪水擠出 MaxEvents 視窗——server 端先濾就沒這問題。
        "$x='*[System[EventID=4624 and TimeCreated[timediff(@SystemTime)<='"
        "+[string]($w*3600000)+']] and EventData[Data[@Name=\"LogonType\"]=\"10\"]]'\n"
        "try{$s=@(Get-WinEvent -LogName Security -FilterXPath $x -MaxEvents 500"
        " -ErrorAction Stop)}"
        "catch{$s=@();if($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*')"
        "{$sErr=[string]$_}}\n"
        # 4625 Properties 佈局：TargetUserName=5、SubStatus=9、IpAddress=19
        "$fr=@($f|ForEach-Object{$p=$_.Properties;New-Object PSObject -Property"
        " @{u=[string]$p[5].Value;ip=[string]$p[19].Value;"
        "sub=('0x{0:X8}' -f [int64]$p[9].Value)}})\n"
        f"$rs=@('{subs}')\n"
        "$rr=@($fr|Where-Object{$rs -contains $_.sub})\n"
        # 4624 Properties 佈局：TargetUserName=5、TargetDomainName=6、IpAddress=18
        "$lg=@($s|ForEach-Object{$p=$_.Properties;New-Object PSObject -Property"
        " @{t=$_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss');"
        "u=[string]$p[5].Value;d=[string]$p[6].Value;ip=[string]$p[18].Value}})\n"
        "$ti=@($fr|Group-Object ip|Sort-Object Count -Descending|Select-Object -First $n"
        "|ForEach-Object{@{ip=$_.Name;n=$_.Count}})\n"
        "$tu=@($fr|Group-Object u|Sort-Object Count -Descending|Select-Object -First $n"
        "|ForEach-Object{@{u=$_.Name;n=$_.Count}})\n"
        "$ru=@($rr|Group-Object u|Sort-Object Count -Descending|Select-Object -First $n"
        "|ForEach-Object{@{u=$_.Name;n=$_.Count}})\n"
        "$rp=@($rr|Group-Object u,ip|Sort-Object Count -Descending|Select-Object -First $n"
        "|ForEach-Object{$g=$_.Group[0];@{u=$g.u;ip=$g.ip;n=$_.Count}})\n"
        "$err=((@($fErr,$sErr)|Where-Object{$_}) -join ' | ')\n"
        "$ok=1\n"
        "if($err){$ok=0}\n"
        "@{ok=$ok;err=[string]$err;host=[string]$env:COMPUTERNAME;window_h=$w;"
        "fail_total=$fr.Count;fail_truncated=[bool]($f.Count -ge $m);"
        "real_fail_total=$rr.Count;success_scan_truncated=[bool]($s.Count -ge 500);"
        "top_src_ips=$ti;top_accounts=$tu;real_accounts=$ru;real_account_pairs=$rp;"
        "rdp_logons=$lg}|ConvertTo-Json -Depth 4 -Compress\n"
    )


# ── SSH 執行 ──────────────────────────────────────────────────────────────
def _ssh_cfg() -> dict:
    """同 erp_oracle_client._cfg 的 SSH 三元組（key/user/host），但不碰 DB 帳密
    （巡檢用不到，也免得測試/離線環境戳 keyring）。"""
    from agent_core import erp_oracle_client as eoc
    key = os.path.expanduser(os.environ.get("RED_ERP_SSH_KEY", "").strip() or eoc._DEFAULT_KEY)
    return {
        "key": key,
        "user": os.environ.get("RED_ERP_SSH_USER", eoc._DEFAULT_USER).strip() or eoc._DEFAULT_USER,
        "host": os.environ.get("RED_ERP_SSH_HOST", eoc._DEFAULT_HOST).strip() or eoc._DEFAULT_HOST,
    }


def _run_remote_powershell(script: str, timeout_s: int) -> str:
    """把統計腳本經 stdin 丟過 SSH 執行，回 stdout。失敗 raise。

    腳本本體走 stdin（`-Command -`）而非命令列：命令列上不留任何腳本內容，既免掉
    cmd→powershell 兩層逸出，也不會像 -EncodedCommand 那樣被 EDR 判成蠕蟲（見模組
    docstring）。ssh 不能加 -n（那會把 stdin 導向 /dev/null，腳本就送不過去）。
    """
    c = _ssh_cfg()
    if not os.path.exists(c["key"]):
        raise ErpSecurityPatrolError(f"找不到 SSH 金鑰：{c['key']}（設 RED_ERP_SSH_KEY）。")
    argv = [
        "ssh", "-i", c["key"],
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={min(20, timeout_s)}",
        f"{c['user']}@{c['host']}",
        "powershell -NoProfile -NonInteractive -NoLogo -ExecutionPolicy Bypass -Command -",
    ]
    try:
        proc = subprocess.run(
            argv, input=script, capture_output=True, timeout=timeout_s,
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise ErpSecurityPatrolError(f"遠端安全統計逾時（>{timeout_s}s）。") from e
    except Exception as e:
        err_lines = str(e).splitlines()
        msg = err_lines[0][:200] if err_lines else type(e).__name__
        raise ErpSecurityPatrolError(f"SSH 執行失敗：{msg}") from e
    if proc.returncode != 0:
        err_lines = (proc.stderr or "").strip().splitlines()
        raise ErpSecurityPatrolError(
            f"遠端安全統計失敗（exit {proc.returncode}）："
            + (err_lines[-1][:200] if err_lines else "無 stderr"))
    return proc.stdout or ""


# ── 解析 + 淨化 ───────────────────────────────────────────────────────────
def _clean_str(value) -> str:
    """攻擊者可控字串一律截長 + sanitize_for_llm（injection redact + PII redact），
    這份報告之後會進 system_alerts 等 LLM-facing 路徑。"""
    s = str(value if value is not None else "")
    if len(s) > _MAX_FIELD_LEN:
        s = s[:_MAX_FIELD_LEN] + "…"
    return sanitize_for_llm(s)


def _as_list(value) -> list:
    """ConvertTo-Json 對單元素集合可能不包 array（PS 版本差）——統一成 list。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _parse_report(stdout: str) -> dict:
    """從遠端 stdout 撈出 JSON 統計行，正規化 + 逐欄淨化。"""
    line = ""
    for ln in stdout.splitlines():
        s = ln.strip()
        if s.startswith("{") and s.endswith("}"):
            line = s  # 取最後一個 JSON 物件行（前面可能有 shell 雜訊）
    if not line:
        raise ErpSecurityPatrolError(
            "遠端輸出找不到 JSON 統計（PowerShell 太舊或腳本失敗）：" + stdout.strip()[:200])
    try:
        raw = json.loads(line)
    except Exception as e:
        raise ErpSecurityPatrolError(f"遠端 JSON 解析失敗：{e}") from e
    if not isinstance(raw, dict) or raw.get("ok") != 1:
        # ok=0 = 遠端事件查詢失敗（權限不足/壞 XPath 等），err 帶 PowerShell 錯誤訊息。
        # 一定要 raise 讓巡檢紅在 daemon log——吞掉會變「0 筆=乾淨」假陰性（Codex P1）。
        err = str(raw.get("err") or "")[:300] if isinstance(raw, dict) else ""
        raise ErpSecurityPatrolError(
            f"遠端事件查詢失敗：{err}" if err else "遠端統計格式不符（缺 ok=1）。")

    def _rows(key: str, fields: dict) -> list[dict]:
        out = []
        for item in _as_list(raw.get(key))[:_MAX_ROWS_PER_LIST]:
            if not isinstance(item, dict):
                continue
            row = {}
            for dst, src in fields.items():
                if dst == "n":
                    try:
                        row["n"] = int(item.get(src) or 0)
                    except (TypeError, ValueError):
                        row["n"] = 0
                else:
                    row[dst] = _clean_str(item.get(src))
            out.append(row)
        return out

    def _int(key: str) -> int:
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "host": _clean_str(raw.get("host")),
        "window_hours": _int("window_h"),
        "fail_total": _int("fail_total"),
        "fail_truncated": bool(raw.get("fail_truncated")),
        "real_fail_total": _int("real_fail_total"),
        "success_scan_truncated": bool(raw.get("success_scan_truncated")),
        "top_src_ips": _rows("top_src_ips", {"ip": "ip", "n": "n"}),
        "top_accounts": _rows("top_accounts", {"account": "u", "n": "n"}),
        "real_accounts": _rows("real_accounts", {"account": "u", "n": "n"}),
        "real_account_pairs": _rows("real_account_pairs", {"account": "u", "ip": "ip", "n": "n"}),
        "rdp_logons": _rows("rdp_logons", {"time": "t", "account": "u", "domain": "d", "ip": "ip"}),
    }


# ── 旗標裁決 ──────────────────────────────────────────────────────────────
def _allowed_ips() -> frozenset:
    raw = os.environ.get("RED_ERP_RDP_ALLOWED_IPS", "")
    extra = {p.strip() for p in raw.split(",") if p.strip()}
    return _BUILTIN_ALLOWED_IPS | frozenset(extra)


def _evaluate(report: dict) -> dict:
    """在巡檢端（erp_mirror_refresh plist env 在手）預判旗標；dashboard_alerts
    只讀旗標轉 alert——policy 設定不用再散到 alert_check plist。"""
    allowed = _allowed_ips()
    unknown = []
    for entry in report.get("rdp_logons", []):
        ip = (entry.get("ip") or "").strip()
        if ip in ("", "-"):
            continue  # 無網路來源（console / session 重連）——不是外連 RDP 跡象
        if ip not in allowed:
            unknown.append(entry)
    warn_thr = env_int("RED_ERP_RDP_REAL_FAIL_WARN", 5, min_value=1)
    crit_thr = env_int("RED_ERP_RDP_REAL_FAIL_CRIT", 50, min_value=1)
    dense = [a for a in report.get("real_accounts", [])
             if int(a.get("n") or 0) >= warn_thr]
    level_bruteforce = ""
    if dense:
        level_bruteforce = ("crit" if any(int(a.get("n") or 0) >= crit_thr for a in dense)
                            else "warn")
    return {
        "success_unknown_ip": unknown[:20],
        "real_account_bruteforce": dense[:20],
        "level_success": "crit" if unknown else "",
        "level_bruteforce": level_bruteforce,
        "allowed_ips": sorted(allowed),
        "real_fail_warn": warn_thr,
        "real_fail_crit": crit_thr,
    }


# ── 落盤 ──────────────────────────────────────────────────────────────────
def _write_report(report: dict) -> None:
    os.makedirs(_REPORT_DIR, exist_ok=True)
    _atomic_write_text(REPORT_PATH, json.dumps(report, ensure_ascii=False, indent=2))
    try:
        with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 歷史檔寫失敗不致命——告警只看 latest


# ── 入口 ──────────────────────────────────────────────────────────────────
def run_patrol(log=print) -> dict:
    """跑一輪巡檢：遠端統計 → 淨化 → 旗標 → 落盤。失敗 raise（包裝入口見下）。"""
    from agent_core.erp_oracle_client import is_enabled
    if not env_bool("RED_ERP_SEC_AUDIT_ENABLED", True):
        return {"skipped": "RED_ERP_SEC_AUDIT_ENABLED=0"}
    if not is_enabled():
        return {"skipped": "RED_ERP_ORACLE_ENABLED=0"}
    window_h = env_int("RED_ERP_SEC_AUDIT_WINDOW_H", 24, min_value=1, max_value=168)
    top_n = env_int("RED_ERP_SEC_AUDIT_TOP_N", 20, min_value=5, max_value=50)
    max_events = env_int("RED_ERP_SEC_AUDIT_MAX_EVENTS", 50000,
                         min_value=1000, max_value=500000)
    timeout_s = env_int("RED_ERP_SEC_AUDIT_TIMEOUT_S", 300, min_value=30, max_value=1800)
    stdout = _run_remote_powershell(_build_ps_script(window_h, top_n, max_events), timeout_s)
    report = _parse_report(stdout)
    report["flags"] = _evaluate(report)
    _write_report(report)
    flags = report["flags"]
    log(f"RDP 巡檢：{report['window_hours']}h 內 4625 失敗 {report['fail_total']}"
        f"（真實帳號 {report['real_fail_total']}）、4624 type10 成功 {len(report['rdp_logons'])}；"
        f"旗標：非白名單成功 {len(flags['success_unknown_ip'])}、"
        f"真實帳號密集嘗試 {len(flags['real_account_bruteforce'])} → {REPORT_PATH}")
    return report


def run_patrol_safe(log=print) -> dict:
    """巡檢入口 — 永不 raise。順跑在 erp_mirror_refresh 主流程之後，
    任何失敗只記 log，不碰鏡像結果與 exit code。"""
    try:
        return run_patrol(log=log)
    except Exception as e:  # noqa: BLE001 - 巡檢失敗不可拖垮鏡像主流程
        try:
            log(f"⚠️ RDP 安全巡檢失敗（不影響鏡像刷新）：{e}")
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:300]}
