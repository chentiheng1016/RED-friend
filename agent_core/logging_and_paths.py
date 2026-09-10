"""Logging system, file paths, and atomic write utilities.

Self-contained.

Module-load side effects:
  * creates _LOG_DIR
  * runs _cleanup_old_logs()
Deferred (caller must invoke): install_tee_and_configure_logging(). This is
deferred because Tee must wrap sys.stdout/stderr BEFORE logging.basicConfig
runs, and the choice to install Tee depends on whether agent.py is __main__.
"""
import os
import sys
import time
import logging
import tempfile
from datetime import datetime

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_DIR = os.path.dirname(_PKG_DIR)
REPO_ROOT = _SCRIPT_DIR
RUNTIME_ROOT = os.environ.get("RED_RUNTIME_DIR", os.path.join(REPO_ROOT, "var"))
_LOG_RETENTION_DAYS = 30


def _guard_tests_never_touch_live_runtime() -> None:
    """在測試環境裡指向 live `var/` 就當場停掉整個 process。

    這個 repo 同時就是部署本體，`var/` 裝的是真的 runtime 狀態。測試的隔離靠
    `tests/__init__.py` 搶在 agent_core 之前把 `RED_RUNTIME_DIR` 導向 tmp dir，
    但那有個前提：tests package 要**先**被匯入。

    `unittest discover -s tests`（**少了 `-t .`**）會把 tests/ 當 top_level_dir、
    測試模組以 top-level 名匯入 —— `tests/__init__.py` 於是完全不在載入路徑上，
    只有某支測試剛好寫 `from tests.x import ...` 時才被順帶拖進來，那時 agent_core
    早就用 live `var/` 把常數算好了。隔離靜默落空，而且擋不住：在 `tests/__init__`
    裡 raise 會被 unittest 包成一筆 `_FailedTest` 然後**繼續跑完全套**。

    2026-08-17 用 audit hook 普查主 checkout，量到實際流進線上的東西：

        var/data/rag_access_audit.jsonl   4,386 行裡 ≥1,901 行是測試殘留（≥43%，33 天）
        var/state/policy_decisions.jsonl  1,462 行裡 126 行是測試殘留（8.6%，14 天）

    （rag 那個 ≥ 是保守下界：只算「人類不可能這樣查」的 fixture 字串。另有 1,312
    行是 `保固`/`交期`/`報價` 這種真人也會問的查詢、分布形狀與已證實的那批一模一樣，
    很可能同樣是測試寫的，但無法證明所以不列入——真值介於 43% 與 73% 之間。）

    兩份都是稽核軌跡。同一個根還餵出過 2026-08-12 的 var/runs 40 筆假 run
    （`bin/red-status` 從 100 掉到 85），以及 age-based 告警被 live tick 消音。

    所以守門放在「做出這個決定的那一行」旁邊 —— 這裡是唯一必經之路，而且此刻
    還沒有任何測試跑過，停下來就是零污染。用 `os._exit` 是因為 unittest 的
    loader 會把例外吞成一筆失敗然後照跑（同 run_with_deadline 的 os._exit 慣例）。

    只在「跑測試」且「沒人顯式指定 runtime 根」時才作用；顯式設 RED_RUNTIME_DIR
    就是逃生門（`tests/test_deploy_lock.py` 等起子程序的測試靠它）。
    """
    if os.environ.get("RED_RUNTIME_DIR"):
        return                                    # 有人明講要用哪個根 → 尊重
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    runner = getattr(spec, "name", "") or ""
    if not runner.startswith(("unittest", "pytest")):
        return                                    # 不是測試 runner → 正常運作
    sys.stderr.write(
        "\n🚨 測試 runtime 隔離失效，已中止（沒有任何測試被執行）。\n\n"
        f"   RUNTIME_ROOT 會指向 {RUNTIME_ROOT}\n"
        "   ＝部署本體的 live var/：稽核軌跡、成本帳、session registry、告警 tick\n"
        "   都會被測試讀寫。實測污染過 rag_access_audit(≥43%)、policy_decisions(8.6%)。\n\n"
        "   原因：agent_core 比 tests/__init__ 更早被 import，RED_RUNTIME_DIR 重導\n"
        "   來不及生效。多半是 `unittest discover -s tests` 少了 `-t .`。\n\n"
        "   改用：make test-quiet\n"
        "         python -m unittest discover -s tests -t . -q\n"
        "         python -m unittest tests.test_x        （單一測試）\n\n"
        "   真的要對 live var/ 跑：自己顯式設 RED_RUNTIME_DIR，本守門就不管了。\n\n")
    sys.stderr.flush()
    os._exit(1)


_guard_tests_never_touch_live_runtime()


def _legacy_or_runtime_path(runtime_rel: str, legacy_rel: str) -> str:
    runtime_path = os.path.join(RUNTIME_ROOT, runtime_rel)
    legacy_path = os.path.join(REPO_ROOT, legacy_rel)
    if os.path.exists(runtime_path):
        return runtime_path
    if os.path.exists(legacy_path):
        return legacy_path
    return runtime_path


def _legacy_or_runtime_dir(runtime_rel: str, legacy_rel: str) -> str:
    return _legacy_or_runtime_path(runtime_rel, legacy_rel)


STATE_DIR = os.path.join(RUNTIME_ROOT, "state")
DATA_DIR = os.path.join(RUNTIME_ROOT, "data")
CACHE_DIR = os.path.join(RUNTIME_ROOT, "cache")
# Transient runtime artifacts (pid/lock files) that must NOT survive a reboot
# and have no legacy root-level equivalent. The deploy mutual-exclusion lock
# (agent_core/deploy_lock.py) lives here as var/run/deploy.lock; bin/redeploy-
# daemons recomputes this same path in bash, so keep the layout in sync.
RUN_DIR = os.path.join(RUNTIME_ROOT, "run")
_LOG_DIR = _legacy_or_runtime_dir("logs", "logs")

MEMORY_FILE = _legacy_or_runtime_path("state/memory.json", "memory.json")
MISTAKES_FILE = _legacy_or_runtime_path("state/mistakes.json", "mistakes.json")
DAEMON_STATE_FILE = _legacy_or_runtime_path("state/daemon_state.json", "daemon_state.json")
DAEMON_STATE_LOCK = DAEMON_STATE_FILE + ".lock"
DAEMON_STATE_BAK = DAEMON_STATE_FILE + ".bak"
HOTWORD_STATE_FILE = _legacy_or_runtime_path("state/hotword_state.json", "hotword_state.json")
EMAIL_CLASSIFY_CACHE_FILE = _legacy_or_runtime_path(
    "state/email_classifications.json",
    "email_classifications.json",
)
VAULT_ACCESS_LOG = _legacy_or_runtime_path("state/vault_access.log", "vault_access.log")
VOICEPRINT_FILE = _legacy_or_runtime_path("state/voiceprints.npz", "voiceprints.npz")
TOKEN_FILE = _legacy_or_runtime_path("state/google/token.json", "token.json")
CREDENTIALS_FILE = _legacy_or_runtime_path("state/google/credentials.json", "credentials.json")
CHROMA_DB_DIR = _legacy_or_runtime_dir("data/chroma_db", "chroma_db")
EMAIL_LAKE_DIR = _legacy_or_runtime_dir("data/data_lake", "data_lake")
INTERNAL_LAKE_DIR = _legacy_or_runtime_dir("data/data_lake_internal", "data_lake_internal")
QUOTE_HISTORY_DIR = _legacy_or_runtime_dir("data/quote_history", "quote_history")
BOM_HISTORY_DIR = _legacy_or_runtime_dir("data/bom_history", "bom_history")
INTERPRET_SESSIONS_DIR = _legacy_or_runtime_dir("data/interpret_sessions", "interpret_sessions")
# 產圖輸出夾刻意放 var/ 下（非 var/data）：var/data 被 path_safety 列受保護寫入區，工具
# (generate_image/edit_image/generate_product_concept 走 safe_path) 存進去會被擋。產圖是
# 工具輸出、非受保護狀態，放 var/generated_images 讓 safe_path 放行（修既有 generate_image 也中的雷）。
GENERATED_IMAGES_DIR = _legacy_or_runtime_dir("generated_images", "generated_images")
EXPORTS_DIR = _legacy_or_runtime_dir("data/exports", "exports")
RUNS_DIR = _legacy_or_runtime_dir("runs", "runs")
WORKFLOWS_DIR = _legacy_or_runtime_dir("workflows", "workflows")
EXIT_WORDS = ['退出', '結束', '掰掰', 'quit', 'exit', '再見']

_IS_DAEMON_MODE = os.environ.get("AGENT_DAEMON_MODE") == "1"


def startup_print(*args, **kwargs):
    """Print only when not running in daemon mode.

    Shared helper for module-import-time diagnostic messages. Suppressed
    in daemon mode so scheduled tasks' logs don't fill with startup
    chatter. Previously duplicated in 5 modules; consolidated phase 68.
    """
    if not _IS_DAEMON_MODE:
        print(*args, **kwargs)


def _safe_debug(msg, *args):
    try:
        logging.getLogger("agent").debug(msg, *args)
    except Exception:
        logger.debug("silent ignore in broad except")


try:
    os.makedirs(_LOG_DIR, exist_ok=True)
except Exception:
    _LOG_DIR = None


def _cleanup_old_logs():
    if not _LOG_DIR or not os.path.isdir(_LOG_DIR):
        return
    try:
        cutoff = time.time() - (_LOG_RETENTION_DAYS * 86400)
        for f in os.listdir(_LOG_DIR):
            if not f.startswith('agent-') or not f.endswith('.log'):
                continue
            full = os.path.join(_LOG_DIR, f)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
            except Exception:
                _safe_debug("silent ignore in broad except: %s", sys.exc_info()[1])
    except Exception:
        _safe_debug("silent ignore in broad except: %s", sys.exc_info()[1])


_cleanup_old_logs()

_today = datetime.now().strftime('%Y-%m-%d')
LOG_FILE = os.path.join(_LOG_DIR, f'agent-{_today}.log') if _LOG_DIR else None


class _TeeStream:
    def __init__(self, primary, log_path):
        self.primary = primary
        self.log_path = log_path
        try:
            self._logf = open(log_path, 'a', encoding='utf-8', buffering=1)
        except Exception:
            self._logf = None

    def write(self, s):
        self.primary.write(s)
        if self._logf and s:
            try:
                self._logf.write(s)
            except Exception:
                _safe_debug("silent ignore in broad except: %s", sys.exc_info()[1])

    def flush(self):
        try:
            self.primary.flush()
        except Exception:
            _safe_debug("silent ignore in broad except: %s", sys.exc_info()[1])
        if self._logf:
            try:
                self._logf.flush()
            except Exception:
                _safe_debug("silent ignore in broad except: %s", sys.exc_info()[1])

    def isatty(self):
        return getattr(self.primary, 'isatty', lambda: False)()

    def fileno(self):
        return self.primary.fileno()

    @property
    def encoding(self):
        return getattr(self.primary, 'encoding', 'utf-8')

    @property
    def errors(self):
        return getattr(self.primary, 'errors', 'strict')

    @property
    def closed(self):
        return getattr(self.primary, 'closed', False)

    def writable(self):
        return True

    def readable(self):
        return False


# Known-benign third-party SDK log lines suppressed as pure noise:
#  - google-genai: "non-text parts in the response" (multimodal responses).
#  - google-auth 2.55+: an unconditional Regional Access Boundary (RAB, formerly
#    "trust boundary") lookup fires against iamcredentials .../allowedLocations on
#    every service-account token refresh. RED's SA has no IAM permission for that
#    endpoint (and no regional boundary configured), so each lookup logs a 403
#    "Permission denied on the service account" WARNING then enters cooldown. The
#    failure is non-fatal — Drive/Gmail sync proceeds and content still ingests —
#    so the line is noise. There is no env opt-out in 2.55+
#    (GOOGLE_AUTH_TRUST_BOUNDARY_ENABLED is deprecated and ignored), so we drop it
#    at the logging layer. See agent_core/google_auth.install_sdk_log_filters call.
_NOISY_SDK_SUBSTRINGS = (
    "non-text parts in the response",
    "Regional Access Boundary HTTP request failed",
)


class _DropNoisySDKWarnings(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
            return not any(s in msg for s in _NOISY_SDK_SUBSTRINGS)
        except Exception:
            return True


def install_sdk_log_filters():
    """Attach _DropNoisySDKWarnings directly to noisy third-party SDK loggers.

    install_tee_and_configure_logging() adds the filter to the *root* handlers,
    which only catches records that reach a configured handler. Daemon
    entrypoints like launchd/scripts/rag_sync.py never call basicConfig, so
    google-auth's Regional Access Boundary WARNING falls through to
    logging.lastResort and prints raw (bare, un-timestamped). Attaching the
    filter to the emitting loggers themselves suppresses the record inside
    Logger.handle() before any handler (or lastResort) runs, so it works
    regardless of logging configuration. Idempotent.
    """
    _f = _DropNoisySDKWarnings()
    for _name in ("google.oauth2._client", "google.oauth2._client_async"):
        _lg = logging.getLogger(_name)
        if not any(isinstance(x, _DropNoisySDKWarnings) for x in _lg.filters):
            _lg.addFilter(_f)


def install_tee_and_configure_logging(install_tee: bool):
    """Install Tee (if requested) then configure root logger.

    Caller should pass install_tee=True when running agent.py as __main__
    so that log records emitted by the default StreamHandler (which binds to
    sys.stderr) are captured into LOG_FILE via Tee. Must be called before
    any logger.info/etc. is emitted.
    """
    if install_tee and LOG_FILE:
        sys.stdout = _TeeStream(sys.stdout, LOG_FILE)
        sys.stderr = _TeeStream(sys.stderr, LOG_FILE)
    # `%(name)s` 是 2026-08-12 加的：原格式只有 時間/等級/訊息，**沒有任何位置
    # 資訊**，所以「XX 失敗」這種訊息在 daemon log 裡根本追不回是哪個模組發的 ——
    # 排查時只能全 repo grep 訊息字串，訊息又常常撞名。加了 logger 名稱之後每一行
    # 都自帶來源，既有的幾百條 log 全部一次受惠，不必逐條去改訊息內容。
    # 用 name 不用 filename/lineno：logger 名稱就是模組路徑（各模組都是
    # `logging.getLogger(__name__)` 或共用的 agent logger），夠定位又不會讓每行
    # 暴長；lineno 還會因為改碼而漂移，對照舊 log 反而困擾。
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s | %(message)s',
        datefmt='%H:%M:%S',
        handlers=[logging.StreamHandler()],
        force=True,
    )
    for _handler in logging.getLogger().handlers:
        _handler.addFilter(_DropNoisySDKWarnings())
    install_sdk_log_filters()
    if LOG_FILE and not _IS_DAEMON_MODE:
        logging.getLogger("agent").info(f"日誌檔位置：{LOG_FILE}")


logger = logging.getLogger("agent")


def _atomic_write_bytes(path: str, write_fn):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, 'wb') as f:
            write_fn(f)
            # rename 前 fsync：不然斷電時 os.replace 的目標可能是空檔
            # （rename 的 metadata 先落盤、資料還在 page cache — 本機
            # 2026-07-07 真斷過電）。
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                logger.debug("silent ignore in broad except: %s", sys.exc_info()[1])
        raise


def _atomic_write_text(path: str, text: str, encoding='utf-8'):
    _atomic_write_bytes(path, lambda f: f.write(text.encode(encoding)))


def show_log_tail(lines: int = 50):
    """看今天 log 檔最後 N 行，大王說「剛剛小紅做了什麼」或 debug 時用。"""
    if not LOG_FILE:
        return "log 系統未啟用(無法建立 logs 目錄)。"
    if not os.path.exists(LOG_FILE):
        return f"今日 log 檔尚未建立:{LOG_FILE}"
    lines = max(1, min(500, int(lines)))
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        text = ''.join(tail).strip()
        return f"📜 {LOG_FILE}(最後 {len(tail)} 行,共 {len(all_lines)} 行):\n\n{text}"
    except Exception as e:
        return f"讀取 log 失敗:{e}"
