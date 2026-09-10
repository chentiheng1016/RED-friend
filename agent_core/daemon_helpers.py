"""Shared helpers for daemon tasks (launchd/scripts/*.py + agent_daemon.py).

Single source of truth for:
  - ts(): timestamp string
  - get_my_email(): recipient lookup from memory.json
  - notify(): send_gmail wrapper with task header
  - update_state(), load_state(): fcntl-locked daemon_state.json ops
  - rotate_log(): truncate oversized launchd logs

Callers just need `from agent_core.daemon_helpers import ...`.
"""
import faulthandler
import fcntl
import json
import os
import shutil
import sys
import threading
from datetime import datetime

from agent_core.logging_and_paths import (
    RUNTIME_ROOT,
    DAEMON_STATE_FILE,
    DAEMON_STATE_LOCK,
    DAEMON_STATE_BAK,
    MEMORY_FILE,
    _LOG_DIR,
)

STATE_FILE = DAEMON_STATE_FILE
STATE_LOCK = DAEMON_STATE_LOCK
STATE_BAK = DAEMON_STATE_BAK
# _LOG_DIR 只在 makedirs 失敗時是 None；退路一樣要吃 RED_RUNTIME_DIR，
# 別自己拼 REPO_ROOT/var（理由同 cost_tracker._get_cost_log_path）。
LOG_DIR = _LOG_DIR or os.path.join(RUNTIME_ROOT, "logs")
FALLBACK_EMAIL = "owner@company.example"


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_my_email() -> str:
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            mem = json.load(f)
        addr = (mem.get("user_email") or "").strip()
        if addr and "@" in addr:
            return addr
    except Exception:
        pass
    return FALLBACK_EMAIL


def _read_state_only() -> dict:
    """Read state without locking. Tries STATE_FILE then STATE_BAK."""
    for candidate in (STATE_FILE, STATE_BAK):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def update_state(mutate_fn):
    """Read-modify-write daemon_state.json under fcntl lock.

    mutate_fn(state_dict) mutates in place; return value is ignored.

    重要：以前 lock 拿不到 / 寫入失敗就 return {}，會把 dedup state 默默蓋
    成空（mailcheck / ponder / dispatcher 全部一輪重複通知）。現在改成：
      - 開鎖 / 拿鎖失敗 → 至少從 disk 讀一次，避免 caller 拿到 {}
      - 寫入失敗 → 仍把 in-memory state 回給 caller（並 log）
    這樣最壞情況是 state 沒寫到 disk（下次 tick 還會試），不會把整批 dedup
    清掉。
    """
    lock_fd = None
    try:
        lock_fd = open(STATE_LOCK, "w")
    except Exception as e:
        print(f"[state lock] 開鎖檔失敗：{e}（fallback: 純讀取）")
        return _read_state_only()
    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            print(f"[state lock] 拿鎖失敗：{e}（fallback: 純讀取）")
            return _read_state_only()
        try:
            state = _read_state_only()
            mutate_fn(state)
            if os.path.exists(STATE_FILE):
                try:
                    shutil.copy2(STATE_FILE, STATE_BAK)
                except Exception:
                    pass
            try:
                tmp = STATE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STATE_FILE)
            except Exception as e:
                # 寫入失敗 — 但 in-memory state 已經 mutate 過。回傳 mutated
                # state 給 caller（不要回 {} 把 dedup 蓋空）。下次 tick 會
                # 重試 write。
                print(f"[state lock] 寫入失敗（state 留在記憶體，下 tick 重試）：{e}")
            return state
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        try:
            lock_fd.close()
        except Exception:
            pass


def load_state() -> dict:
    """純讀 daemon_state.json（不上鎖、不寫檔）。

    以前寫成 `update_state(lambda _s: None)` — 每次「讀」都做 .bak 全檔複製 +
    全檔重寫 + 獨佔鎖（健檢 Low）。寫入端走 os.replace 原子換檔，所以 lock-free
    讀不會看到半寫檔；損毀時 _read_state_only 自動退 .bak，再不行回 {}。
    沒有任何呼叫端依賴「讀取會建檔/修復檔案」的副作用（建檔由 update_state
    第一次寫入時做）。
    """
    return _read_state_only()


def notify(subject: str, body: str, task_name: str, *, markdown_html: bool = False) -> bool:
    """Send a notification email via Gmail. 回傳是否寄送成功（失敗不 raise）。

    send_gmail 失敗時不 raise、回 "發信失敗：..." 字串 —— 以前這裡整個吞掉，
    caller（mailcheck / ponder / alert_pusher email fallback）以為送到了，
    急件通知一次失敗即永久漏掉（健檢 Medium）。現在把結果轉成 bool 讓
    caller 決定要不要記 dedup / 換 channel；維持「絕不 raise」的契約。

    markdown_html=True：body 是 markdown（dispatcher 排程結果）時多帶 HTML
    alternative，表格在 Mail 客戶端才會對齊；純文字 body 行為不變。
    """
    # Lazy import so callers that only need rotate_log/state don't pay the
    # agent_core.gmail import cost.
    from agent_core.gmail import send_gmail_internal

    body_with_header = f"[daemon task: {task_name}] [{ts()}]\n\n{body}"
    try:
        # notify() 送出的一律是 daemon / 排程自己產的內容，所以永遠帶
        # X-RED-Generated —— 否則這封信隔天就會被 RAG 當成公司原始信吃回去。
        res = send_gmail_internal(
            to=get_my_email(), subject=subject, body=body_with_header,
            markdown_html=markdown_html, generated_by=f"daemon:{task_name}",
        )
        print(f"[daemon/{task_name}] 通知信結果：{res}")
        # gmail_ops.send_gmail 的失敗訊號是回傳字串（不 raise）
        return not str(res).startswith("發信失敗")
    except Exception as e:
        print(f"[daemon/{task_name}] 寄信失敗：{e}")
        return False


class _TimestampStream:
    """行首時間戳包裝（daemon stdout/stderr 專用）。

    為什麼需要：launchd 把 daemon 的 print 直接導進 var/logs/daemon-<name>.log，
    行內**沒有任何時間資訊**（只有少數 `▶️ 開始 @` 標記帶日期）。2026-08-29 的
    除錯實測連續三次被這件事絆倒：①四月的化石錯誤被當成活訊號追了半天；
    ②「OAuth 噪音是否在修復部署之後」無法從 log 判定；③日期切片掃描器對大半
    log 是盲的。8/18 的 launchd 鑑識也是因此只能仰賴 /usr/bin/log show。

    語意：只在「邏輯行的行首」加 `[YYYY-MM-DD HH:MM:SS] `，跨多次 write 的
    partial line 不會被切斷（狀態機記著上一次有沒有以 \n 收尾）；空白行不加
    （保持段落分隔乾淨）。委派面照抄 _TeeStream（logging_and_paths）的合約：
    fileno/encoding/errors/closed/isatty 全部透傳 —— subprocess(stdout=sys.stdout)
    走 fileno 拿到**原始 fd**，子程序輸出不會（也不該）被包到。
    """

    def __init__(self, primary):
        self.primary = primary
        self._lock = threading.Lock()
        self._at_line_start = True

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")

    def write(self, s):
        if not s:
            return 0
        buf = []
        with self._lock:
            parts = str(s).split("\n")
            last_i = len(parts) - 1
            for i, piece in enumerate(parts):
                if piece and self._at_line_start:
                    buf.append(self._stamp())
                    self._at_line_start = False
                buf.append(piece)
                if i != last_i:
                    buf.append("\n")
                    self._at_line_start = True
            self.primary.write("".join(buf))
        return len(s)

    def flush(self):
        try:
            self.primary.flush()
        except Exception:
            pass

    def isatty(self):
        return getattr(self.primary, "isatty", lambda: False)()

    def fileno(self):
        return self.primary.fileno()

    @property
    def encoding(self):
        return getattr(self.primary, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self.primary, "errors", "strict")

    @property
    def closed(self):
        return getattr(self.primary, "closed", False)

    def writable(self):
        return True

    def readable(self):
        return False


def install_stdout_timestamps() -> bool:
    """把 daemon 的 stdout/stderr 包上行首時間戳（冪等；回傳是否已生效）。

    三道安全閘，缺一不可：
      * `RED_LOG_TIMESTAMPS=0` — kill switch。
      * 只包「還是原始 process stream」的 stdout/stderr（`sys.__stdout__`）：
        已被 redirect_stdout / StringIO / _TeeStream 換掉的一律不碰 —— 測試
        redirect 之後呼叫到這裡，不會把時間戳寫進被斷言的 buffer。
      * tty 不包 —— 人在終端機手動跑 launchd/scripts/*.py 時輸出保持乾淨；
        launchd 導到 log 檔的是 pipe/file，不是 tty。
    """
    if os.environ.get("RED_LOG_TIMESTAMPS", "1").strip() == "0":
        return False
    installed = False
    for attr, orig in (("stdout", sys.__stdout__), ("stderr", sys.__stderr__)):
        cur = getattr(sys, attr, None)
        if isinstance(cur, _TimestampStream):
            installed = True
            continue
        if cur is None or orig is None or cur is not orig:
            continue
        try:
            if cur.isatty():
                continue
        except Exception:
            continue
        setattr(sys, attr, _TimestampStream(cur))
        installed = True
    return installed


def rotate_log(task_name: str, max_mb: int = 2):
    """Truncate the launchd log tail when it grows past max_mb.

    Log path convention: logs/daemon-<task_name>.log
    Safe to call every run; returns silently if log is small enough.

    順手掛上行首時間戳（install_stdout_timestamps）：rotate_log 是每個 daemon
    入口的第一句（16 個 entrypoint，含 agent_daemon._task_wrapper 每輪開場），
    是艦隊唯一的共同咽喉點 —— 在這裡裝，全艦隊 log 一次長出時間戳，不必改
    幾百個 print 站點。沒走 rotate_log 的 module main（alert_pusher / watchdog /
    embed_server）各自顯式呼叫。
    """
    install_stdout_timestamps()
    log_path = os.path.join(LOG_DIR, f"daemon-{task_name}.log")
    try:
        if not os.path.exists(log_path):
            return
        size_bytes = os.path.getsize(log_path)
        size_mb = size_bytes / 1024 / 1024
        if size_mb < max_mb:
            return
        keep_bytes = 1024 * 1024
        with open(log_path, "rb") as f:
            f.seek(max(0, size_bytes - keep_bytes))
            f.readline()  # skip partial line
            tail = f.read()
        header = f"# [rotate @ {ts()}] 原檔 {size_mb:.1f}MB，保留後 {len(tail)/1024:.0f}KB\n".encode("utf-8")
        with open(log_path, "wb") as f:
            f.write(header + tail)
    except Exception as e:
        print(f"[rotate_log] 失敗：{e}")


_hang_dump_installed = False


def install_hang_dump_signal() -> None:
    """Wire SIGUSR1 → dump every thread's Python traceback (non-fatal).

    Lets an operator pinpoint a wedged daemon WITHOUT killing it:

        kill -USR1 <pid>

    prints all-thread Python stacks to stderr (→ the launchd daemon log), so an
    intermittent hang — e.g. a Google read whose socket timeout never bites
    (rag_sync 2026-07-06) — can be located at the exact Python frame on the NEXT
    occurrence. `sample` only yields C frames, and SIGUSR1's default disposition
    is *terminate*, so blindly signalling a process with no handler installed
    would kill it (which is why the 2026-07-06 wedge couldn't be probed live).

    all_threads=True → the wedged worker AND the main thread both dump.
    chain=False → we own SIGUSR1. Attempted once per process; best-effort so a
    platform without SIGUSR1 (Windows) or a stderr with no real fileno (captured
    under a test harness) simply skips it instead of breaking the run.
    """
    global _hang_dump_installed
    if _hang_dump_installed:
        return
    _hang_dump_installed = True  # attempt exactly once, success or not
    try:
        import signal
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception as e:  # noqa: BLE001 — diagnostics must never break a run
        print(f"[daemon] SIGUSR1 hang-dump 未安裝（略過）：{e}")


def _deadline_exceeded(label: str, deadline_s: int) -> None:
    """看門狗到期動作：dump 全線程堆疊定位卡點，再強制退出整個 process。

    用 os._exit（非 sys.exit / raise）是刻意的——主線程可能卡在不理會 Python
    signal 的 C 擴充網路呼叫裡，只有獨立背景線程的強退才保證脫困。退碼 75=
    EX_TEMPFAIL，告訴 launchd「暫時失敗」，下個 StartInterval 重排即可。
    """
    sys.stderr.write(
        f"\n[{label}] ❌ 超過 {deadline_s}s wall-clock 硬上限，疑似網路呼叫卡死"
        "（per-request timeout 被半關閉 TCP / 傳輸層空隙漏接）。"
        "dump 全線程堆疊後強制退出，讓 launchd 下一輪重跑：\n"
    )
    try:
        faulthandler.dump_traceback()  # 預設寫 sys.stderr → launchd log
    except Exception:
        pass
    sys.stderr.flush()
    os._exit(75)


def run_with_deadline(fn, deadline_s: int, *, label: str = "task"):
    """在 wall-clock 硬上限內跑 fn()；超時用背景看門狗強制退出整個 process。

    給單發 cron daemon（launchd StartInterval）用。即使每個 request 都已帶
    per-request timeout（Gmail httplib2 / Gemini http_options），半關閉 TCP
    (CLOSE_WAIT) 或傳輸層空隙仍可能讓整輪卡死數小時——而 StartInterval 不會在
    前一輪還在跑時起新的，於是整條 ingest 停擺到有人手動介入（2026-06-15
    email_ingest 卡 poll() 5.4h、郵件停止進 lake 事故）。看門狗給整輪一個天花板：
    到期 dump 堆疊（定位卡點）+ os._exit，launchd 下個 interval 自然重跑。

    回傳 fn() 的回傳值；fn 正常完成或拋例外都會取消看門狗（finally）。
    """
    # 順帶把 SIGUSR1 接上 all-thread traceback dump——單發 cron 都經這裡，
    # 下次卡死可 `kill -USR1 <pid>` 不殺程序就定位確切 Python 行（見函式註解）。
    install_hang_dump_signal()
    timer = threading.Timer(deadline_s, _deadline_exceeded, args=(label, deadline_s))
    timer.daemon = True
    timer.start()
    try:
        return fn()
    finally:
        timer.cancel()


# ── 暫時性網路錯誤的退避重試（給 daemon 內「唯讀、可安全重跑」的呼叫）──────
# 起因：2026-08-03 08:30 morning 那輪在**第一個** Google API 呼叫就死——
# httplib2 解不到 www.googleapis.com，整份早安簡報沒送出（該 log 93 次成功
# 對 4 次 DNS 失敗，約 4%）。當時那步既沒重試也沒降級，一擊必殺。
#
# ⚠️ 型別判定救不了這顆：httplib2.error.ServerNotFoundError 的繼承鏈是
#    HttpLib2Error → Exception，**不是** ConnectionError / OSError / TimeoutError
#    的子類（drive_sync._is_retryable_drive_rpc_error 只認型別，所以認不出它）。
#    因此這裡型別 + 訊息雙管，並附上 googleapiclient 的 5xx/429 狀態碼。
_TRANSIENT_NETWORK_TOKENS = (
    "unable to find the server",            # httplib2 ServerNotFoundError（DNS）
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname",                # macOS getaddrinfo
    "nameresolutionerror",                  # urllib3 包裝後的 DNS 失敗
    "failed to resolve",                    # urllib3 NameResolutionError 內文
    "max retries exceeded",                 # urllib3 MaxRetryError（連線層才會有）
    "network is unreachable",
    "no route to host",
    "connection reset",
    "connection refused",
    "connection aborted",
    "server disconnected",
    "broken pipe",
    "timed out",
    "timeout",
    "eof occurred",                         # SSL 半關閉
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)
_TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


def is_transient_network_error(exc: BaseException) -> bool:
    """這個例外像不像「等一下再試就會好」的網路問題？"""
    import socket
    if isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        if int(status) in _TRANSIENT_HTTP_STATUS:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(exc).lower()
    return any(token in msg for token in _TRANSIENT_NETWORK_TOKENS)


def retry_on_transient_network(
    fn,
    *,
    attempts: int = 3,
    base_delay_s: float = 2.0,
    label: str = "",
    sleep=None,
):
    """跑 fn()；遇到暫時性網路錯誤就指數退避重試，其餘例外原樣往上拋。

    ⚠️ **只能包冪等操作**（唯讀 GET / list）。寄信、送訊息這類有副作用的呼叫
    不可以用——重試會重複送出。

    退避：base_delay_s × 2^(n-1)，預設 3 次 = 最多多花 2+4=6 秒，遠低於
    run_with_deadline 給 morning 的 1200s 上限。耗盡重試後拋最後一次的例外，
    讓呼叫端自己決定要降級還是失敗。
    """
    import time
    if sleep is None:
        sleep = time.sleep
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not is_transient_network_error(exc):
                raise
            delay = base_delay_s * (2 ** (attempt - 1))
            print(
                f"[{label or 'retry'}] ⚠️ 暫時性網路錯誤（{type(exc).__name__}: "
                f"{str(exc)[:120]}），{delay:.0f}s 後重試（{attempt}/{attempts - 1}）",
                flush=True,
            )
            sleep(delay)


class TaskDeadlineExceeded(TimeoutError):
    """單一工作項超過 per-task wall-clock 上限（可逐項中止的場景）。

    刻意繼承 TimeoutError（→ OSError → Exception）：caller 既有的
    `except Exception` 失敗處理會自動接住，走原本的「記失敗 + 換下一項」路徑，
    不必為它特別 catch。
    """


def run_task_with_deadline(fn, deadline_s: float, *, label: str = "task"):
    """在 wall-clock 上限內跑 fn()；超時放棄「這一項」、但**不殺整個 process**。

    與 run_with_deadline 的分工（兩支是一對）：
      - run_with_deadline → 到期 os._exit(75) 整個 process 強退。給「一輪只跑
        一個任務」的單發 cron（email_ingest / mailcheck / morning / ponder）：
        卡死就讓 launchd 下個 StartInterval 重跑整輪。
      - run_task_with_deadline → 到期 raise TaskDeadlineExceeded，由 caller 自行
        記失敗、換下一項。給「一輪跑多個異質任務」的 dispatcher：一個慢任務不
        該 hard-kill 整批，os._exit 會連同批其他正常任務一起打斷。

    機制：fn 跑在 daemon worker thread，主線程 `Event.wait(deadline_s)`。到期主
    線程不再等、回 caller 換下一項；卡死的 worker 被放棄（Python 殺不了 thread）。
    dispatcher 是單發 cron（StartInterval 跑完即退），leaked daemon thread 隨
    process 結束自然回收、不累積。到期會 `faulthandler.dump_traceback()` 印**全
    線程**堆疊（含 leaked worker）定位卡點 —— 與 run_with_deadline 同樣「卡死自動
    留證」，差別只在不強退。

    ⚠️ 前提：fn 必須 idempotent / 可安全放棄 —— 我們不真的中止 worker，它可能
    在背景把活兒跑完（含副作用）。dispatcher task 走唯讀 safe_tools、最終 notify
    寄信在主迴圈而非 worker 內，天然滿足；別拿這支去包會寫資料的工作。

    回傳 fn() 的回傳值；fn 自身拋的例外原樣 re-raise（含內層 timeout，讓 caller
    照常記失敗）；只有「到期仍沒完成」才拋 TaskDeadlineExceeded。
    """
    holder: dict = {}
    done = threading.Event()

    def target() -> None:
        try:
            holder["value"] = fn()
        except BaseException as exc:  # 原樣帶回主線程 re-raise（含內層 timeout）
            holder["exc"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=target, name=f"deadline:{label}", daemon=True)
    worker.start()
    if not done.wait(timeout=deadline_s):
        sys.stderr.write(
            f"\n[{label}] ⏱️ 超過 {deadline_s}s per-task wall-clock 上限，疑似網路/"
            "Gemini 呼叫卡死（per-request timeout 被半關閉 TCP / 傳輸層空隙漏接，或"
            "繞過內層 timeout 的路徑）。放棄此項、不殺 process，同批其他任務照跑。"
            "dump 全線程堆疊定位卡點：\n"
        )
        try:
            faulthandler.dump_traceback()  # 預設寫 sys.stderr → launchd log
        except Exception:
            pass
        sys.stderr.flush()
        raise TaskDeadlineExceeded(
            f"[{label}] 超過 {deadline_s}s per-task wall-clock 上限，已放棄此項"
        )
    if "exc" in holder:
        raise holder["exc"]
    return holder["value"]
