"""Daemon 存活 / 停滯看門狗（系統層 belt-and-suspenders）。

背景（2026-06-12/13 兩起事故）：
  1. 紅 bot（com.xiaohong.telegram）的推理 worker 因 Google 連線無超時
     （CLOSE_WAIT）永久卡死，process 還活著、launchd 看得到 PID，但任務
     「永遠不會完成」，**靠大王半夜人工發現**才重啟。PR #116 已在應用層補了
     整體 deadline + genai HTTP timeout；本模組是「即使那些 in-process 機制
     全失效」也能兜底的系統層看門狗。
  2. rag_sync 是 7–15h 夜跑，中途 wedge 也沒人會發現。

設計（兩個關鍵防誤報，別跳過）：
  - **telegram long-poll 主線程平常就「安靜」是正常的**（sample 堆疊長期在
    poll()）。所以絕不能因為「process 安靜」就重啟。我們改看 per-bot 的
    「任務心跳」檔（var/state/telegram_heartbeat_<key>.json）：daemon_telegram
    在任務執行期間 begin()→active、過程 pulse()、結束 idle()。看門狗只在
    **state==active 且 ts 停滯超過門檻** 才判定卡死；idle 不論多舊都不算。
    參考既有 var/state/email_ingest_heartbeat.json 的模式。
  - **rag_sync 進度 log 很稀疏**（每 50 檔才印一次，圖片 OCR 一張 ~29s →
    50 張可能 ~24 分鐘才跳一行）。所以 rag 看「log mtime 多久沒寫入」配上較寬
    的門檻（預設 30 分鐘），而且只有 is_sync_process_active 為真（真的有一輪
    在跑）時才檢查。rag **不自動重啟**（夜跑重啟損失進度，且 content-hash
    dedup 會在當天續做；只告警）。
  - **手動補跑不寫 daemon log**（2026-07-04 誤報事故）：SOP 的 RAG_SYNC_FORCE=1
    手動補跑（sync_guard 的逃生口）輸出慣例重導向 var/logs/rag_sync_manual*.log，
    期間 daemon-rag_sync.log 必然停滯 → 只盯 daemon log 會每次補跑都誤報一輪、
    跑完又自動恢復。所以停滯判定取「所有已知 rag log（daemon + manual glob）中
    **最新**的 mtime」：手動 run 活躍 → manual log 新鮮 → 不告警；真卡死 → 全部
    停滯 → 照樣告警。取 max(mtime) 也讓幾天前補跑殘留的舊 manual log 無法反過來
    遮掉 daemon 停滯。

動作：
  - telegram bot 卡死 → 可選自動 SIGTERM + `launchctl kickstart -k`。
    ⚠️ telegram plist 是 KeepAlive={Crashed:true, SuccessfulExit:false}：乾淨
    退出（exit 0）launchd 不會自動拉起，所以**一定要跟一句 kickstart**，不能
    只 SIGTERM。而且真正 wedge 的 process 多半收不到 SIGTERM（主迴圈卡住沒
    回到頂端檢查旗標），所以 `kickstart -k`（必要時 SIGKILL）才是有牙的那招。
  - 同一個 label 有 restart 冷卻（預設 15 分鐘），避免啟動即崩的 respawn 風暴。

告警走兩條互補路徑（都**不經 Gemini**）：
  - dashboard_alerts._check_daemon_stalls() → 現有 alert_check daemon
    （alert_pusher，每 5 分鐘）會把停滯推到 Telegram，自帶 dedup / 6h 節流 /
    恢復通知。涵蓋「自動重啟關閉 / 失敗」與 rag。
  - 本看門狗 daemon **只有實際執行重啟動作時**才自己推一則即時通知（受冷卻
    天然節流），確保「自動修好了」這件事大王也會即時知道。

門檻 / 開關全走 agent_core.env_utils（唯一 env_int/env_float 來源）：
  RED_WATCHDOG_ENABLE              master switch（預設 on）
  RED_WATCHDOG_TG_STALL_S          telegram active heartbeat 停滯門檻（預設 1200）
  RED_WATCHDOG_RAG_STALL_S         rag log mtime 停滯門檻（預設 1800）
  RED_WATCHDOG_TG_AUTORESTART      telegram 自動重啟（預設 on）
  RED_WATCHDOG_RESTART_COOLDOWN_S  同 label 兩次重啟最小間隔（預設 900）
  RED_WATCHDOG_TG_GRACE_S          SIGTERM 後等多久再 kickstart（預設 3）
"""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime

from agent_core.env_utils import env_bool, env_int
from agent_core.logging_and_paths import STATE_DIR, _LOG_DIR

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────
TELEGRAM_LABEL_PREFIX = "com.xiaohong.telegram"
RAG_LABEL = "com.xiaohong.rag_sync_daily"
_TELEGRAM_HB_PREFIX = "telegram_heartbeat_"
_RAG_LOG_BASENAME = "daemon-rag_sync.log"
# SOP 手動補跑（RAG_SYNC_FORCE=1）的輸出命名慣例；改慣例要同步改這個 glob，
# 否則手動補跑期間的假卡死告警會回來（2026-07-04 事故）。
_RAG_MANUAL_LOG_GLOB = "rag_sync_manual*.log"
_WATCHDOG_STATE_BASENAME = "daemon_watchdog_state.json"
# rag_sync 每處理一檔就 touch 的心跳檔（drive_sync._pulse_rag_heartbeat 寫）。
# progress log 每 50 檔才印一行，圖片 OCR / 大檔抽取可讓相鄰兩行間隔 >30 分鐘
# → 只看 log mtime 會把「慢但健康」的 batch 誤判成 wedge（2026-07-15 誤報）。
# 心跳每檔跳、內部 throttle；detect_stalls 取 log 與心跳中較新者，慢 batch 不再假告警。
_RAG_HEARTBEAT_BASENAME = "rag_sync_heartbeat.json"


# ────────────────────────────────────────────────────────────────────
# Env-configurable thresholds (single source of truth: env_utils)
# ────────────────────────────────────────────────────────────────────
def watchdog_enabled() -> bool:
    return env_bool("RED_WATCHDOG_ENABLE", True)


def telegram_stall_threshold_s() -> int:
    # 1200s = 20min, comfortably above the 900s (RED_TG_TASK_DEADLINE_S) overall
    # task deadline — so a normal long task either finishes / gets abandoned by
    # the app-layer deadline before this fires. Only a genuinely wedged handler
    # (active, no pulse, never returns) crosses it.
    return env_int("RED_WATCHDOG_TG_STALL_S", 1200, min_value=120)


def rag_stall_threshold_s() -> int:
    # 1800s = 30min. drive_sync only prints "processing i/N" every 50 files and
    # image OCR is ~29s/image → up to ~24min between log lines on an all-image
    # stretch. 30min keeps a safe margin so a slow-but-healthy run isn't flagged.
    return env_int("RED_WATCHDOG_RAG_STALL_S", 1800, min_value=300)


def telegram_autorestart_enabled() -> bool:
    return env_bool("RED_WATCHDOG_TG_AUTORESTART", True)


def restart_cooldown_s() -> int:
    return env_int("RED_WATCHDOG_RESTART_COOLDOWN_S", 900, min_value=60)


def sigterm_grace_s() -> int:
    return env_int("RED_WATCHDOG_TG_GRACE_S", 3, min_value=0, max_value=60)


# ────────────────────────────────────────────────────────────────────
# Per-bot heartbeat key ↔ launchd label
# ────────────────────────────────────────────────────────────────────
def _normalize_key(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw or "")).strip("-_.").lower()


def telegram_heartbeat_key() -> str:
    """Heartbeat key for THIS bot process, derived from its env.

    Mirrors the colour suffix each telegram plist sets via
    RED_TELEGRAM_STATE_SUFFIX (green/blue/…). The red bot has no suffix → "red".
    The watchdog derives the same key from the launchd label, so the writer
    (daemon_telegram) and reader (this module) always agree.
    """
    suffix = _normalize_key(os.environ.get("RED_TELEGRAM_STATE_SUFFIX", ""))
    return suffix or "red"


def _label_to_key(label: str) -> str:
    """com.xiaohong.telegram → 'red'; com.xiaohong.telegram_green → 'green'."""
    rest = label[len(TELEGRAM_LABEL_PREFIX):]
    if rest.startswith("_"):
        rest = rest[1:]
    return _normalize_key(rest) or "red"


def _key_to_label(key: str) -> str:
    key = _normalize_key(key) or "red"
    return TELEGRAM_LABEL_PREFIX if key == "red" else f"{TELEGRAM_LABEL_PREFIX}_{key}"


def _is_telegram_label(label: str) -> bool:
    return label == TELEGRAM_LABEL_PREFIX or label.startswith(TELEGRAM_LABEL_PREFIX + "_")


def telegram_heartbeat_path(key: str, *, state_dir: str | None = None) -> str:
    state_dir = state_dir or STATE_DIR
    return os.path.join(state_dir, f"{_TELEGRAM_HB_PREFIX}{_normalize_key(key) or 'red'}.json")


# ────────────────────────────────────────────────────────────────────
# Heartbeat writer (used by daemon_telegram during task execution)
# ────────────────────────────────────────────────────────────────────
def write_telegram_heartbeat(
    key: str,
    *,
    state: str,
    task: str = "",
    label: str = "",
    state_dir: str | None = None,
    now: float | None = None,
) -> None:
    """Atomically write a per-bot heartbeat. Best-effort — never raises
    (a heartbeat write failure must not break the bot's main loop)."""
    state_dir = state_dir or STATE_DIR
    path = telegram_heartbeat_path(key, state_dir=state_dir)
    payload = {
        "state": state,
        "at": datetime.now().isoformat(timespec="seconds"),
        "ts": float(now) if now is not None else time.time(),
        "task": str(task)[:80],
        "label": label or _key_to_label(key),
        "pid": os.getpid(),
    }
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


class TelegramHeartbeat:
    """Tracks a telegram bot's task-execution state in a heartbeat file.

    Lifecycle per handled message:
        hb.begin(task)   # state → active, ts = now
        hb.pulse()       # called from heartbeat_touch during long ops
                         # (throttled; only refreshes ts while active)
        hb.idle()        # state → idle, ts = now (in a finally)

    The watchdog only flags state==active heartbeats whose ts has stalled past
    the threshold, so an idle bot — however long it has been quiet — is never
    flagged. The throttle keeps the ~per-chunk heartbeat_touch calls from
    hammering var/state with writes.
    """

    def __init__(
        self,
        *,
        key: str | None = None,
        label: str = "",
        state_dir: str | None = None,
        min_pulse_interval_s: float = 5.0,
    ) -> None:
        self.key = _normalize_key(key) or telegram_heartbeat_key()
        self.label = label or _key_to_label(self.key)
        self._state_dir = state_dir
        self._min = float(min_pulse_interval_s)
        self._state = "idle"
        self._task = ""
        self._last_mono: float | None = None

    def begin(self, task: str = "") -> None:
        self._state = "active"
        self._task = str(task)[:80]
        self._flush()

    def idle(self) -> None:
        self._state = "idle"
        self._task = ""
        self._flush()

    def pulse(self) -> None:
        if self._state != "active":
            return
        now = time.monotonic()
        if self._last_mono is not None and (now - self._last_mono) < self._min:
            return
        self._flush()

    def _flush(self) -> None:
        self._last_mono = time.monotonic()
        write_telegram_heartbeat(
            self.key,
            state=self._state,
            task=self._task,
            label=self.label,
            state_dir=self._state_dir,
        )


# ────────────────────────────────────────────────────────────────────
# Detection
# ────────────────────────────────────────────────────────────────────
def _current_pids() -> dict[str, str | None] | None:
    """Return {label: pid_str or None} for loaded com.xiaohong.* daemons，
    **讀取失敗時回 None**。

    pid is None when launchctl shows "-" (loaded but not currently running).

    為什麼要跟「一個都沒載入」分開：偵測端把空 map 當「沒東西要檢查」是安全的
    （不動作），但**動作端**不行 —— 拿一次 launchctl 失敗當成「這個 label 不見
    了」而去動它，正是 2026-08-18 誤殺紅 bot 那個形狀。health.py 早就為自己的
    helper 做了這個區分（見該檔 _loaded_pids 註解），這裡跟上。
    """
    try:
        r = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out: dict[str, str | None] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].startswith("com.xiaohong."):
            pid = parts[0].strip()
            out[parts[2]] = pid if pid.isdigit() else None
    return out


def _launchctl_list() -> dict[str, str | None]:
    """偵測端用的版本：讀取失敗回 {}（＝本輪沒東西可判，不動作，安全）。"""
    return _current_pids() or {}


def _read_heartbeat(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _rag_sync_active() -> bool:
    try:
        from agent_core.ingest.rag_runner import is_sync_process_active
        return bool(is_sync_process_active())
    except Exception:
        return False


def _freshest_rag_log(log_dir: str) -> tuple[str, float] | None:
    """回所有已知 rag_sync 輸出 log 中最新的 (path, mtime)；一個都沒有回 None。

    排程 daemon 寫 daemon-rag_sync.log（launchd StandardOutPath）；SOP 手動
    RAG_SYNC_FORCE=1 補跑慣例重導向 rag_sync_manual*.log（含日期後綴變體）。
    兩種 run 共用同一把 rag_sync.lock，is_sync_process_active 分不出是誰在跑，
    所以停滯判定必須看全部候選中最新的 mtime。
    """
    candidates = [os.path.join(log_dir, _RAG_LOG_BASENAME)]
    candidates.extend(
        glob.glob(os.path.join(glob.escape(log_dir), _RAG_MANUAL_LOG_GLOB))
    )
    best: tuple[str, float] | None = None
    for path in candidates:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if best is None or mtime > best[1]:
            best = (path, mtime)
    return best


def rag_heartbeat_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or STATE_DIR, _RAG_HEARTBEAT_BASENAME)


def write_rag_heartbeat(state_dir: str | None = None, now: float | None = None) -> None:
    """rag_sync「我還在逐檔推進」的心跳（atomic、best-effort，永不 raise）。

    由 drive_sync._pulse_rag_heartbeat 在每處理一檔時呼叫（呼叫端 throttle）。排程
    daemon 與 SOP 手動補跑跑同一份 code、寫同一個檔 → 天然免疫「補跑期間 daemon
    log 停滯」的假告警，不必像 log 那樣 glob 多個候選。detect_stalls 只看這個檔的
    mtime（os.replace 每次更新），不解析內容；ts / pid 僅供人工 debug。心跳寫失敗
    絕不能中斷 sync，故整段吞例外。"""
    path = rag_heartbeat_path(state_dir)
    payload = {
        "ts": float(now) if now is not None else time.time(),
        "at": datetime.now().isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _rag_heartbeat_mtime(state_dir: str | None = None) -> float | None:
    """rag 心跳檔最後寫入時刻（epoch 秒）；檔不存在回 None。"""
    try:
        return os.path.getmtime(rag_heartbeat_path(state_dir))
    except OSError:
        return None


def detect_stalls(
    *,
    now: float | None = None,
    state_dir: str | None = None,
    log_dir: str | None = None,
    launchctl_list_fn=None,
    rag_active_fn=None,
) -> list[dict]:
    """Return a list of stall findings. Pure / read-only.

    Each finding is a dict:
      telegram: {kind:"telegram", label, pid:int, key, age_s, threshold_s, task}
      rag:      {kind:"rag", label, age_s, threshold_s, log_path}

    The seam parameters (launchctl_list_fn / rag_active_fn / now / *_dir) keep
    this fully unit-testable without touching the real launchd / filesystem.
    """
    now = time.time() if now is None else now
    state_dir = state_dir or STATE_DIR
    log_dir = log_dir or _LOG_DIR
    list_fn = launchctl_list_fn or _launchctl_list
    rag_active = rag_active_fn or _rag_sync_active

    findings: list[dict] = []

    # ── Telegram bots: alive PID + active heartbeat that stalled ──
    loaded = list_fn()
    tg_threshold = telegram_stall_threshold_s()
    for label, pid in loaded.items():
        if not _is_telegram_label(label):
            continue
        # No numeric pid → not currently running (just crashed / between
        # respawns). That's the existing _check_daemons_health()'s job, not a
        # "alive but stuck" stall. Skip.
        if not pid or not str(pid).isdigit():
            continue
        key = _label_to_key(label)
        hb = _read_heartbeat(telegram_heartbeat_path(key, state_dir=state_dir))
        if not hb or hb.get("state") != "active":
            continue  # idle / never-ran → never a stall (anti-false-positive)
        # 殘檔防誤殺：heartbeat 帶寫入者 pid（write_telegram_heartbeat）。若與
        # launchctl 現任 pid 不符，這份 active 是**上一個** process 中途被殺留下
        # 的殘檔 —— 新 process 還沒寫過心跳，不能當「活著卻卡死」的證據，否則
        # 看門狗會每輪誤殺剛重啟的健康 bot（直到下一則訊息覆寫 heartbeat）。
        # 沒帶 pid 的舊格式檔無從比對，維持原判定。
        hb_pid = hb.get("pid")
        if hb_pid is not None:
            try:
                if int(hb_pid) != int(pid):
                    continue
            except (TypeError, ValueError):
                pass  # pid 欄位壞掉 → 無從比對，維持原判定
        try:
            ts = float(hb.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        age = now - ts
        if age > tg_threshold:
            findings.append({
                "kind": "telegram",
                "label": label,
                "pid": int(pid),
                "key": key,
                "age_s": age,
                "threshold_s": tg_threshold,
                "task": str(hb.get("task") or ""),
            })

    # ── rag_sync: a run is active but stopped advancing ──
    # 活躍的 run 可能是排程 daemon，也可能是 SOP 手動補跑（寫自己的 manual log）。
    # 「還在推進」的證據取兩路中較新者：
    #   - progress log mtime（_freshest_rag_log：daemon + manual glob 取最新）—
    #     涵蓋 gmail / chat 等每步都印一行的階段；
    #   - rag 心跳檔 mtime（drive_sync 每處理一檔就 touch）— 涵蓋 drive 逐檔階段，
    #     progress log 每 50 檔才印、大檔 OCR / 一批 skip 可讓相鄰兩行間隔 >門檻，
    #     只看 log 會把慢但健康的 batch 誤判成 wedge（2026-07-15 誤報）。
    # 任一新鮮 → 還在推進 → 不報；兩者都停滯過門檻才是真 wedge。
    if rag_active():
        rag_threshold = rag_stall_threshold_s()
        freshest = _freshest_rag_log(log_dir) if log_dir else None
        hb_mtime = _rag_heartbeat_mtime(state_dir)
        newest_mtime: float | None = None
        log_path: str | None = None
        if freshest is not None:
            log_path, newest_mtime = freshest
        if hb_mtime is not None and (newest_mtime is None or hb_mtime > newest_mtime):
            newest_mtime = hb_mtime
            # 報告仍優先指向 log（人要 tail 的是 log 不是心跳檔）；
            # 完全沒有任何 rag log 時才退而指向心跳檔。
            if log_path is None:
                log_path = rag_heartbeat_path(state_dir)
        if newest_mtime is not None:
            age = now - newest_mtime
            if age > rag_threshold:
                findings.append({
                    "kind": "rag",
                    "label": RAG_LABEL,
                    "age_s": age,
                    "threshold_s": rag_threshold,
                    "log_path": log_path,
                })

    return findings


# ────────────────────────────────────────────────────────────────────
# Restart action (telegram only)
# ────────────────────────────────────────────────────────────────────
def _launchctl(*args: str) -> list[str]:
    launchctl = "/bin/launchctl" if os.path.exists("/bin/launchctl") else "launchctl"
    return [launchctl, *args]


def restart_telegram_bot(
    label: str,
    pid: int | None = None,
    *,
    grace_s: int | None = None,
) -> tuple[bool, str]:
    """SIGTERM (graceful, best-effort) then `launchctl kickstart -k`.

    The kickstart is non-negotiable: telegram plists have
    KeepAlive={SuccessfulExit:false}, so a clean exit would NOT be respawned by
    launchd. `-k` also SIGKILLs a process that ignored SIGTERM (the wedged
    case), guaranteeing the bot actually comes back.
    """
    grace_s = sigterm_grace_s() if grace_s is None else grace_s
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ValueError, TypeError, OSError):
            # OSError 是關鍵：已死的 pid 丟的是 ProcessLookupError、別人的
            # process 丟 PermissionError，兩者都是 OSError 子類 —— 舊版只接
            # (ValueError, TypeError)，於是這行會往上炸穿 run_watchdog（那裡沒有
            # try），整個 tick 當場結束，同一輪其他真的卡死的 bot 一個都不會被救。
            # 註解本來就寫著要處理「already dead」，只是型別列錯了。
            pass  # already dead / not ours — kickstart -k handles it
    if grace_s:
        time.sleep(grace_s)
    try:
        r = subprocess.run(
            _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{label}"),
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:160]
    if r.returncode == 0:
        return True, f"kickstart -k {label}"
    return False, (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:160]


# ────────────────────────────────────────────────────────────────────
# Cooldown state
# ────────────────────────────────────────────────────────────────────
def _watchdog_state_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or STATE_DIR, _WATCHDOG_STATE_BASENAME)


def _load_state(state_dir: str | None = None) -> dict:
    try:
        with open(_watchdog_state_path(state_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict, state_dir: str | None = None) -> None:
    path = _watchdog_state_path(state_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# Notification (best-effort, Telegram only — never via Gemini)
# ────────────────────────────────────────────────────────────────────
def _notify(message: str) -> bool:
    try:
        from agent_core.telegram import telegram_push
        return "✅" in str(telegram_push(message))
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────
def run_watchdog(
    *,
    now: float | None = None,
    state_dir: str | None = None,
    detect_fn=None,
    restart_fn=None,
    notify_fn=None,
    current_pids_fn=None,
) -> dict:
    """One watchdog tick: detect → (auto-restart telegram, with cooldown) →
    notify on action. rag stalls and autorestart-off telegram stalls are left
    to dashboard_alerts/alert_pusher (this only ACTS + notifies on action).

    🚨 動手前一定重採（``current_pids_fn``）：``findings`` 是 detect() 那一瞬間的
    快照，而每個 restart 內含 ``grace_s`` 秒的 SIGTERM 寬限，多個 finding 時最後
    一個可能在快照後數十秒才輪到。這段期間該 bot 可能已經自己換了 process
    （crash→respawn、redeploy、上一輪 kickstart），對舊 pid 送 SIGTERM 輕則打空、
    重則 pid 被回收給無關的 process；接著 kickstart -k 又會把剛起來的健康 bot 砍掉
    重來 —— 2026-08-18 health_check 誤殺紅 bot 就是這個形狀。
    """
    if not watchdog_enabled():
        return {"enabled": False, "findings": 0, "restarted": [], "skipped": [],
                "failed": [], "stale": []}

    now = time.time() if now is None else now
    state_dir = state_dir or STATE_DIR
    detect = detect_fn or (lambda: detect_stalls(now=now, state_dir=state_dir))
    restart = restart_fn or restart_telegram_bot
    notify = notify_fn or _notify
    current_pids = current_pids_fn or _current_pids

    findings = detect()
    restarted: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    stale: list[str] = []

    if not findings:
        return {"enabled": True, "findings": 0, "restarted": [],
                "skipped": [], "failed": [], "alerted_only": [], "stale": []}

    autorestart = telegram_autorestart_enabled()
    cooldown = restart_cooldown_s()
    wd_state = _load_state(state_dir)
    state_dirty = False
    alerted_only: list[str] = []

    for f in findings:
        label = f["label"]
        mins = f["age_s"] / 60.0
        if f["kind"] != "telegram" or not autorestart:
            # rag, or telegram with autorestart off → dashboard_alerts notifies.
            alerted_only.append(label)
            continue

        prev = wd_state.get(label, {})
        last = float(prev.get("last_restart_ts") or 0.0)
        if now - last < cooldown:
            skipped.append(label)
            continue

        # ── 動手前重採（見函式 docstring）──────────────────────────────
        live = current_pids()
        if live is None:
            # launchctl 讀不到 ≠ 這個 label 不見了。讀不出現況就別動手 —— 停滯本身
            # 另有 dashboard_alerts 那條線會告警，不會因此變成靜默。
            stale.append(label)
            continue
        if label not in live:
            # 有人明確 bootout 了它（查兇手：/usr/bin/log show --predicate
            # 'processID == 1'）。不是看門狗該用 kickstart 蓋過去的狀況。
            stale.append(label)
            continue
        flagged_pid = f.get("pid")
        now_pid = live[label]
        if now_pid is not None and str(now_pid) != str(flagged_pid):
            # 已經換了一個 process：我們判定卡死的那個早就不在了，現在這個還沒有
            # 卡死的證據（heartbeat 也還沒輪到它寫）。動它＝誤殺健康 bot。
            stale.append(label)
            continue
        # now_pid is None＝label 還在但沒有 process 在跑：不送 SIGTERM（那個 pid
        # 已經是舊的），只靠 kickstart 把它拉回來。telegram plist 是
        # KeepAlive={SuccessfulExit:false}，乾淨退出 launchd 不會自己補。
        ok, detail = restart(label, flagged_pid if now_pid is not None else None)
        wd_state[label] = {
            "last_restart_ts": now,
            "last_restart_at": datetime.now().isoformat(timespec="seconds"),
            "count": int(prev.get("count") or 0) + 1,
            "last_detail": detail,
            "last_ok": bool(ok),
        }
        state_dirty = True
        task = (f.get("task") or "")[:40]
        if ok:
            restarted.append(label)
            notify(
                f"🔁 看門狗：{label} 偵測到任務停滯"
                f"（heartbeat 已 {mins:.0f} 分鐘沒前進"
                f"{('；任務「' + task + '」') if task else ''}），已自動重啟。"
            )
        else:
            failed.append(label)
            notify(
                f"⚠️ 看門狗：{label} 任務停滯 {mins:.0f} 分鐘但自動重啟失敗：{detail}\n"
                f"  請手動：launchctl kickstart -k gui/$(id -u)/{label}"
            )

    if state_dirty:
        _save_state(wd_state, state_dir)

    return {
        "enabled": True,
        "findings": len(findings),
        "restarted": restarted,
        "skipped": skipped,
        "failed": failed,
        "alerted_only": alerted_only,
        "stale": stale,
    }


def main() -> int:
    """launchd entry point (com.xiaohong.daemon_watchdog, StartInterval tick)."""
    # 沒走 rotate_log 的入口要自己掛行首時間戳（見 daemon_helpers.rotate_log docstring）
    from agent_core.daemon_helpers import install_stdout_timestamps
    install_stdout_timestamps()
    result = run_watchdog()
    print(f"[daemon_watchdog] {json.dumps(result, ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
