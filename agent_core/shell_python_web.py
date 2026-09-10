"""Shell exec, Python sandbox, and web fetch/search tools.

Self-contained.
"""
import os
import io
import re
import sys
import json
import math
import time
import random
import shutil
import collections
import subprocess
import multiprocessing
from datetime import datetime

import numpy as np
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from agent_core.logging_and_paths import _LOG_DIR, logger
from agent_core.web_access_guard import (
    SSRFBlockedError,
    assess_web_access,
    format_web_access_assessment,
    guarded_requests_get,
)

_PYTHON_EXEC_TIMEOUT = 30


_ddgs_instance = None


def _get_ddgs():
    global _ddgs_instance
    if _ddgs_instance is None:
        _ddgs_instance = DDGS()
    return _ddgs_instance


def _python_exec_worker(code: str, queue):
    buf = io.StringIO()
    # Round 4 fix：subprocess 模式 sys.stdout/stderr 反正會跟著 spawn 重建，
    # 但若直接 in-process 呼叫（unit test）不可漏 restore，否則後續 print 全消失。
    _saved_stdout = sys.stdout
    _saved_stderr = sys.stderr
    try:
        sys.stdout = buf
        sys.stderr = buf

        # C7: 靜態檢查放最前面 — 即使後面 import 失敗也要先攔下惡意輸入
        # Round 4 補丁（C11/C12/M8/M9）：之前 C1/C8/C9 修了 excel_query 的
        # pandas I/O 攻擊面，但**忘了同步到 run_python_code** — pd.read_pickle
        # / pd.read_html / pd.io.formats.excel.ExcelFormatter 都還能玩。
        # 這裡 mirror 同款 deny + 加上 np.save / plt.savefig / allow_pickle=True。
        _PY_FORBIDDEN = [
            r"\b__\w+__\b",                    # dunder access (__import__/__class__/__builtins__/...)
            r"\bimport\b",                      # import / from ... import 都禁
            r"\bopen\s*\(",                     # 直接 open
            r"\bexec\s*\(", r"\beval\s*\(", r"\bcompile\s*\(",
            r"\bgetattr\s*\(", r"\bsetattr\s*\(", r"\bdelattr\s*\(",
            r"\bglobals\s*\(", r"\blocals\s*\(", r"\bvars\s*\(",
            r"\b(?:os|sys|subprocess|socket|shutil|pathlib|pickle|marshal|"
            r"importlib|requests|urllib|http|ftplib|smtplib|telnetlib)\b",
            # C11: pandas read_* — pickle 反序列化 RCE / SSRF 經 read_html
            r"\.read_\w+\b",
            # C11/C9: pandas to_X 寫檔
            r"\.to_(?:csv|excel|pickle|hdf|parquet|json|feather|orc|sql|"
            r"clipboard|xml|html|stata|latex|gbq|msgpack|sas|spss)\b",
            # C9 mirror: to_string(buf=) / to_markdown(buf=) 等帶 path 的 to_*
            r"\.to_\w+\s*\([^)]*\b(?:buf|path_or_buf|excel_writer|writer|fname|filepath)\s*=",
            # C8 + C12: pandas 構造器 + formatter — alias 也擋
            r"\b(?:HDFStore|ExcelWriter|ExcelFile|ExcelFormatter|"
            r"CSVFormatter|HTMLFormatter|StataWriter\w*|FrameFormatter)\b",
            # C12: pd.io.* submodule — 沒 Q&A 用途，整路擋
            r"\bpd\s*\.\s*io\b",
            # M8/M9: numpy / matplotlib 寫檔 + 危險 load
            r"\bnp\s*\.\s*(?:save|savez|savez_compressed|savetxt|memmap|"
            r"frombuffer|tofile|fromfile|ctypeslib|f2py)\b",
            r"\.savefig\b",                     # plt.savefig / fig.savefig
            r"\.tofile\b",                      # array.tofile
            r"\ballow_pickle\s*=\s*True\b",     # np.load(allow_pickle=True) — pickle RCE
            r"\.read_clipboard\b",
        ]
        bad_match = None
        for pat in _PY_FORBIDDEN:
            m = re.search(pat, code)
            if m:
                bad_match = (pat, m.group(0))
                break
        if bad_match:
            queue.put((
                "err",
                f"Python 拒絕執行：含禁用 token『{bad_match[1]}』（pattern={bad_match[0]}）。\n"
                "  data analysis 用 pd / np / math / re 等；要跑 shell 用 run_shell。\n"
                "  要畫圖表（甘特圖/長條圖/趨勢圖等）改叫 generate_chart —— 本沙箱禁 "
                ".savefig，存不出圖；generate_chart 會直接畫好並傳到 Telegram、且免確認。\n"
                f"  代碼前 200 字：{code[:200]}"
            ))
            return

        # matplotlib 視為 optional — 沒裝就跳過 plot 功能但其他能跑
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as _plt
            _plt.rcParams['font.sans-serif'] = ['Arial Unicode MS']
            _plt.rcParams['axes.unicode_minus'] = False
        except ImportError:
            _plt = None

        # C7 補丁（review LOW + SECURITY.md 列舉的「未沙箱」項目）：
        # 原本 globs 內含 os / sys / subprocess / shutil / requests — 跑在 subprocess
        # 雖然不會炸主程序，但可以 (1) 任意 shell escape via os.system (2) 讀
        # /etc/* 或 ~/.ssh/* (3) requests.post 把 secret exfil 到 attacker.com。
        # 移除危險模組；data analysis 仍可用 pd/np/sklearn/math/re 等。
        # 真要跑 shell 的請呼叫 run_shell（V2 dry-run + V4 確認門兜底）。
        globs = {
            "json": json,
            "math": math, "re": re, "datetime": datetime,
            "time": time, "random": random, "collections": collections,
            "np": np,
        }
        if _plt is not None:
            globs["plt"] = _plt
        try:
            import pandas as _pd
            globs["pd"] = _pd
        except ImportError:
            pass
        # 機器學習常用工具（銷售預測、客戶分群、定價模型都用得到）
        try:
            import sklearn as _sklearn
            from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge, Lasso
            from sklearn.cluster import KMeans, DBSCAN
            from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
            from sklearn.preprocessing import StandardScaler, MinMaxScaler
            from sklearn.model_selection import train_test_split, cross_val_score
            from sklearn.metrics import (
                mean_squared_error, mean_absolute_error, r2_score,
                accuracy_score, confusion_matrix, classification_report,
            )
            from sklearn.decomposition import PCA
            globs.update({
                "sklearn": _sklearn,
                "LinearRegression": LinearRegression,
                "LogisticRegression": LogisticRegression,
                "Ridge": Ridge, "Lasso": Lasso,
                "RandomForestRegressor": RandomForestRegressor,
                "RandomForestClassifier": RandomForestClassifier,
                "KMeans": KMeans, "DBSCAN": DBSCAN, "PCA": PCA,
                "StandardScaler": StandardScaler, "MinMaxScaler": MinMaxScaler,
                "train_test_split": train_test_split,
                "cross_val_score": cross_val_score,
                "mean_squared_error": mean_squared_error,
                "mean_absolute_error": mean_absolute_error,
                "r2_score": r2_score,
                "accuracy_score": accuracy_score,
                "confusion_matrix": confusion_matrix,
                "classification_report": classification_report,
            })
        except ImportError:
            pass
        # C7: sandbox builtins — 同 V1 配方。只給純運算用的 helper。
        _SAFE_BUILTIN_NAMES = (
            "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
            "int", "len", "list", "map", "max", "min", "range", "reversed",
            "round", "set", "slice", "sorted", "str", "sum", "tuple", "type",
            "zip", "True", "False", "None", "print", "isinstance", "hasattr",
            "callable", "bytes", "bytearray", "chr", "ord", "hex", "oct", "bin",
            "divmod", "id", "iter", "next", "pow", "repr",
        )
        import builtins as _builtins
        globs["__builtins__"] = {
            n: getattr(_builtins, n) for n in _SAFE_BUILTIN_NAMES
            if hasattr(_builtins, n)
        }

        exec(code, globs)  # noqa: S102 - sandboxed

        output_str = buf.getvalue()
        if len(output_str) > 10000:
            output_str = output_str[:10000] + "\n...[輸出過長已截斷，為保護記憶體僅顯示前1萬字]"

        # V13/C3 補強：output 過 redact_log_line — 連 print 出的 inline secret
        # 也擋（雖然 globs 裡沒給 os/requests 已經難洩，但 LLM 可能產生
        # 固定字串「我的 password is xxx」之類）
        try:
            from agent_core.log_redact import redact_log_line as _redact
            output_str = _redact(output_str)
        except Exception:
            pass

        queue.put(("ok", output_str or "執行成功（無輸出）"))
    except Exception as e:
        # 例外訊息也過 redact（traceback 可能洩 path / secret）
        try:
            from agent_core.log_redact import redact_log_line as _redact
            err_msg = _redact(f"Python 錯誤：{e}\n{buf.getvalue()}")
        except Exception:
            err_msg = f"Python 錯誤：{e}\n{buf.getvalue()}"
        queue.put(("err", err_msg))
    finally:
        # Round 4 fix：恢復原 stdout/stderr。subprocess 模式不必要但 in-process
        # 測試（unittest 直呼 worker）若不還原，後面 print 全部寫進孤兒 buf。
        sys.stdout = _saved_stdout
        sys.stderr = _saved_stderr


_SHELL_AUDIT_LOG = os.path.join(_LOG_DIR, "shell_audit.log") if _LOG_DIR else None
_SHELL_AUDIT_MAX_BYTES = 5 * 1024 * 1024  # V12: 5MB 後 rotate（避免 log 爆量）
_SHELL_AUDIT_KEEP_ROTATIONS = 3            # V12: 保留 .1 .2 .3，再老的丟掉
_SHELL_MAX_TIMEOUT = 300  # 最大 5 分鐘
_SHELL_OUT_LIMIT = 4000   # 輸出截斷上限（字元）

# 這些 pattern 匹配就**直接拒絕執行**，不可繞過
_SHELL_HARD_BLOCKS = [
    # 刪除整個系統 / 家目錄
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-f[a-zA-Z]*r|-rf|-fr)\s+(/|/\*|/\s|~[/\s]|~$|\$HOME\b|\*\s)",
        "rm -rf 作用在系統根或家目錄"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/Users|/System|/Library|/bin|/sbin|/etc|/var|/usr)\b",
        "rm 作用在系統關鍵目錄"),
    # 直接寫入磁碟裝置
    (r"\bdd\s+[^|]*of=/dev/", "dd 寫入磁碟裝置（可能抹除硬碟）"),
    (r"(^|[\s;|&])(mkfs|newfs|format)\b", "格式化指令"),
    (r"\bdiskutil\s+(eraseDisk|eraseVolume|secureErase)", "diskutil 抹除磁碟"),
    # Fork bomb
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;:", "fork bomb"),
    # sudo / su / 提權
    (r"(^|[\s;|&])sudo\b", "sudo 提權"),
    (r"(^|[\s;|&])su\s+-", "su 切換使用者"),
    # 從網路下載直接執行
    (r"\b(curl|wget|fetch)\s+[^|]*\|\s*(sh|bash|zsh|ksh|fish|python3?|ruby|perl|node)\b",
        "從網路下載後直接執行（典型攻擊手法）"),
    # 關機 / 重啟
    (r"\b(shutdown|reboot|halt|poweroff)\b", "關機/重啟指令"),
    (r"\binit\s+[06]\b", "init 0/6 關機/重啟"),
    # 改 system 關鍵檔權限
    (r"\bchmod\s+(?:-\w+\s+)?[0-7]*[67]?7[0-7]?\s+/(?:$|\s|\*|Users|System|Library|etc|bin|sbin|usr)",
        "對系統關鍵路徑改 777/666 權限"),
    # 殺 launchd / kernel_task
    (r"\bkill(all)?\s+.*\b(launchd|kernel_task)\b", "砍 launchd / kernel_task"),
    # 寫入系統 PATH 裡的執行檔
    (r"\b(mv|cp|tee|cat)\s+[^|;&]*>\s*/(?:bin|sbin|usr/bin|usr/sbin|etc)/",
        "寫入系統執行檔目錄"),
]


def _rotate_shell_audit_if_big():
    """V12: 超過 _SHELL_AUDIT_MAX_BYTES 就 rotate（.log → .log.1 → .log.2 → 丟）。"""
    if not _SHELL_AUDIT_LOG:
        return
    try:
        if not os.path.isfile(_SHELL_AUDIT_LOG):
            return
        if os.path.getsize(_SHELL_AUDIT_LOG) < _SHELL_AUDIT_MAX_BYTES:
            return
        # rotate
        for i in range(_SHELL_AUDIT_KEEP_ROTATIONS, 0, -1):
            src = f"{_SHELL_AUDIT_LOG}.{i}"
            dst = f"{_SHELL_AUDIT_LOG}.{i+1}"
            if os.path.isfile(src):
                if i == _SHELL_AUDIT_KEEP_ROTATIONS:
                    os.remove(src)  # oldest 丟掉
                else:
                    os.replace(src, dst)
        os.replace(_SHELL_AUDIT_LOG, f"{_SHELL_AUDIT_LOG}.1")
    except Exception as _e:
        logger.debug("shell audit rotate 失敗：%s", _e)


def _shell_audit(command: str, exit_code, cwd: str):
    """寫一行 shell 執行紀錄。V9: command 過 log_redact 防 inline secret 入檔。
    V12: 寫入前若檔案 > 5MB 自動 rotate，最多保 3 份歷史。"""
    if not _SHELL_AUDIT_LOG:
        return
    try:
        _rotate_shell_audit_if_big()
        from agent_core.log_redact import redact_log_line
        safe_cmd = redact_log_line(command)
        with open(_SHELL_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] "
                    f"cwd={cwd or '.'} exit={exit_code}\n  {safe_cmd}\n")
    except Exception as _e:
        logger.debug("shell audit 寫入失敗：%s", _e)


def run_shell(command: str, timeout_sec: int = 30, working_dir: str = ""):
    """執行一行 shell 指令並回傳輸出。
    - command：要執行的 shell 字串（bash 語法，可含 pipe / && / || / > 等）。
    - timeout_sec：上限秒數（預設 30，最多 300）。
    - working_dir：工作目錄（預設當前目錄；支援 '~'）。
    **禁止**：rm -rf 家目錄/系統、sudo/su、fork bomb、dd 寫磁碟、
            curl | sh、shutdown/reboot、對系統目錄 chmod 777 等。
    所有執行都會寫到 logs/shell_audit.log 供事後追蹤。
    ⚠️ 呼叫前務必跟大王確認指令是什麼；破壞性動作（rm / git reset --hard / git push --force / npm publish）
       必須取得大王明確同意才執行。"""
    if not command or not command.strip():
        return "錯誤：command 不能空。"
    cmd = command.strip()

    for pat, reason in _SHELL_HARD_BLOCKS:
        if re.search(pat, cmd, re.IGNORECASE):
            print(f"[系統日誌] 🛑 拒絕 shell：{reason}")
            _shell_audit(cmd, "BLOCKED", working_dir)
            return (f"🛑 拒絕執行：指令疑似危險（{reason}）。\n"
                    f"  指令：{cmd}\n"
                    f"  若確定安全，請大王自己到 Terminal 執行。")

    cwd = os.path.expanduser(working_dir.strip()) if working_dir else None
    if cwd and not os.path.isdir(cwd):
        return f"錯誤：working_dir 不存在：{cwd}"
    timeout = max(1, min(int(timeout_sec), _SHELL_MAX_TIMEOUT))

    # V9: redact 後再印（避免 launchd log 抓到 inline secret）
    from agent_core.log_redact import redact_log_line
    safe_cmd_for_print = redact_log_line(cmd)
    print(f"[系統日誌] 🐚 shell ({timeout}s): {safe_cmd_for_print[:120]}"
          f"{'...' if len(safe_cmd_for_print)>120 else ''}")

    try:
        proc = subprocess.run(
            cmd, shell=True,
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,  # 需要輸入的指令（sudo -S, 密碼 prompt 等）直接 EOF，不會 hang
            timeout=timeout,
            cwd=cwd,
            env={**os.environ, "PS1": "", "PS2": ""},
            errors="replace",  # 非 UTF-8 輸出不會爆
        )
    except subprocess.TimeoutExpired:
        _shell_audit(cmd, "TIMEOUT", working_dir)
        return f"⏱️ 指令超過 {timeout}s 逾時（已強制中止）。若需更長時間，加大 timeout_sec 參數（最多 {_SHELL_MAX_TIMEOUT}）。"
    except Exception as e:
        _shell_audit(cmd, f"ERROR:{e}", working_dir)
        return f"執行失敗：{type(e).__name__}: {e}"

    out = (proc.stdout or "").rstrip()
    err = (proc.stderr or "").rstrip()
    code = proc.returncode
    _shell_audit(cmd, code, working_dir)

    # C3 補丁（review 找到）：proc.stdout 流回 LLM context + run_history
    # 如果指令印出 secret（env / cat ~/.aws/creds 等），會無 redact 進 logs。
    # 這裡 redact 後才回。
    from agent_core.log_redact import redact_log_line as _redact
    out = _redact(out) if out else out
    err = _redact(err) if err else err

    parts = [f"exit={code}"]
    if out:
        truncated = len(out) > _SHELL_OUT_LIMIT
        parts.append("stdout:\n" + out[:_SHELL_OUT_LIMIT] + ("\n…（stdout 截斷）" if truncated else ""))
    if err:
        truncated = len(err) > 1500
        parts.append("stderr:\n" + err[:1500] + ("\n…（stderr 截斷）" if truncated else ""))
    if not out and not err:
        parts.append("(無輸出)")
    return "\n".join(parts)


def run_python_code(code: str):
    """在隔離子程序執行 Python 程式碼（有 timeout + sandbox 保護）。

    用途：臨時運算、資料分析、用 pandas / sklearn / numpy 跑統計。
    ⚠️ 要畫圖表（甘特圖/長條圖/堆疊圖/趨勢圖/圓餅圖）請改叫 generate_chart：本沙箱
       禁 .savefig / open，存不出圖；generate_chart 會直接畫好並自動傳 Telegram、免確認。

    ⚠️ C7 沙箱（自 V1-style 修補開始）：
      - **禁用 module**：os / sys / subprocess / socket / shutil / pathlib /
        pickle / marshal / importlib / requests / urllib / http / ftplib /
        smtplib / telnetlib — 含這些 token 直接拒絕。
      - **禁用 builtin**：__import__ / open / exec / eval / compile /
        getattr / setattr / globals / vars 等。
      - **可用**：pd / np / sklearn / plt / math / re / datetime / json /
        time / random / collections。
      - **要跑 shell** 改叫 run_shell（V2 dry-run + V4 確認門兜底）。
      - **要存檔** 改叫 manage_files / write_file（path_safety 把關）。
      - 輸出（stdout / 例外）會過 redact_log_line — 含 inline secret 的
        debug print 不會洩到 LLM context。"""
    print(f"\n[系統日誌] 🐍 執行 Python（上限 {_PYTHON_EXEC_TIMEOUT} 秒）...")
    try:
        ctx = multiprocessing.get_context("spawn")
    except ValueError:
        ctx = multiprocessing
    queue = ctx.Queue()
    proc = ctx.Process(target=_python_exec_worker, args=(code, queue), daemon=True)
    proc.start()
    proc.join(timeout=_PYTHON_EXEC_TIMEOUT)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
        return f"Python 執行逾時（超過 {_PYTHON_EXEC_TIMEOUT} 秒），已強制中止子程序。請檢查是否有無限迴圈。"
    try:
        status, payload = queue.get_nowait()
        return payload
    except Exception:
        return "執行完成（無輸出）"


def read_website_content(url: str):
    """抓一個靜態網頁的純文字（用 BeautifulSoup 解）。JS 重度渲染的網站請改用 browser_open + browser_read。"""
    print("\n[系統日誌] 📖 閱讀網頁...")
    try:
        # SSRF 防護：URL 可能來自被注入的內容，禁止連向內網/loopback/雲端 metadata。
        res = guarded_requests_get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        res.encoding = res.apparent_encoding or 'utf-8'
        content_type = res.headers.get('Content-Type', '')
        assessment = assess_web_access(
            url=url,
            final_url=res.url,
            http_status=res.status_code,
            body=res.text or "",
            content_type=content_type,
        )
        if not assessment.can_extract:
            return format_web_access_assessment(assessment)
        if 'text/html' not in content_type and 'text/plain' not in content_type:
            return f"此網址不是網頁（Content-Type: {content_type}），無法解析。"
        soup = BeautifulSoup(res.text, 'html.parser')
        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()
        raw_text = soup.get_text(separator='\n')
        clean_text = re.sub(r'\n{3,}', '\n\n', raw_text).strip()
        from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted
        safe_text = sanitize_for_llm(clean_text[:4000])
        return wrap_as_untrusted(safe_text, label="webpage-content")
    except SSRFBlockedError as e:
        return f"拒絕存取：{e}此工具只能讀公開網頁。"
    except Exception as e:
        return f"閱讀失敗：{e}"


def search_the_web(query: str):
    """用 DuckDuckGo 搜尋網路，回傳前 3 筆結果（標題 / 摘要 / 網址）。"""
    print(f"\n[系統日誌] 🌐 搜尋：{query}...")
    try:
        results = _get_ddgs().text(query, max_results=3)
        if not results:
            return "搜尋無結果，請換個關鍵字試試。"
        lines = []
        from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted
        for r in results:
            title = sanitize_for_llm(r.get('title', '（無標題）'))
            body = sanitize_for_llm(r.get('body', '無摘要'))
            href = r.get('href', '（無網址）')
            lines.append(f"標題: {title}\n摘要: {body}\n網址: {href}")
        return wrap_as_untrusted("\n".join(lines), label="search-results")
    except Exception as e:
        global _ddgs_instance
        _ddgs_instance = None
        return f"搜尋失敗：{e}"
