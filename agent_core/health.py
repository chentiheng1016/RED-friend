"""Automated health check + self-repair.

Queries launchd
daemon state, JSON state files, ChromaDB, Parquet lake, log sizes,
disk space, DNS resolution. Can auto-repair common failures
(reload daemon, kickstart KeepAlive, rotate log, restore from .bak,
clean video cache).

_get_memory_collection (ChromaDB singleton) still lives in agent.py
— the RAG memory subsystem is the biggest remaining block and is
high-risk to extract (cross-thread state, pid-safety). Lazy-imported
inside _check_data_stores.
"""
import os
import json
import platform
import shutil
import signal
import socket
import subprocess
import time
from datetime import datetime

from agent_core.email_classify import _EMAIL_CLASSIFY_CACHE_FILE
from agent_core.email_lake import (
    _EMAIL_LAKE_STATE_FILE,
    _EMAIL_LAKE_PARQUET,
)
from agent_core.erp import _ERP_VIDEO_CACHE_DIR
from agent_core.logging_and_paths import (
    logger,
    _LOG_DIR,
    MEMORY_FILE,
    MISTAKES_FILE,
    DAEMON_STATE_FILE,
)
from agent_core.memory import _get_memory_collection, vector_store_last_error

_HEALTH_CHECK_LOG = os.path.join(_LOG_DIR, "health_check.log") if _LOG_DIR else None

_HEALTH_RSS_LIMIT_MB = {
    "com.xiaohong.telegram": 2048,
    "com.xiaohong.telegram_black": 2048,
    "com.xiaohong.telegram_blue": 2048,
    "com.xiaohong.telegram_gray": 2048,
    "com.xiaohong.telegram_green": 2048,
    "com.xiaohong.telegram_indigo": 2048,
    "com.xiaohong.telegram_orange": 2048,
    "com.xiaohong.telegram_purple": 2048,
    "com.xiaohong.telegram_white": 2048,
    "com.xiaohong.telegram_yellow": 2048,
    "com.xiaohong.email_ingest": 2048,
    "com.xiaohong.dispatcher": 1024,
    "com.xiaohong.mailcheck": 1024,
    "com.xiaohong.ponder": 1024,
    "com.xiaohong.morning": 1024,
    "com.xiaohong.health_check": 512,
    # 系統層停滯看門狗（tick daemon）。納入監看 = health_check 每 30 分鐘確認
    # 看門狗自己有被 launchd 載入（who watches the watchman）。
    "com.xiaohong.watchdog": 512,
    # 告警管線 + 基礎設施（健檢 Medium：以前漏列 → alert_check 自己死掉沒人
    # 看、chroma/tool_rpc/web_server 掉載入也無感）。都列 optional：plist 沒
    # 部署的環境（dev clone / CI）不誤報。
    "com.xiaohong.alert_check": 512,
    # 共用 Chroma server — 全 fleet RAG/記憶都靠它；上限放寬（大 collection
    # cache 合法吃記憶體），主要監看「有沒有被載入 / 有沒有 PID」。
    # 2026-07-23/24 實測：rag_sync 連續同步下工作集 6.4–8.6 GB 屬常態
    # （32 GB 機器），6144 太緊造成每 30 分鐘告警+重啟風暴，調高到 10240。
    "com.xiaohong.chroma": 10240,
    "com.xiaohong.tool_rpc": 2048,
    "com.xiaohong.web_server": 1024,
}

# 記憶體超標「只告警、不自動重啟」的 daemon。chroma 是有狀態的核心服務：
# unload/load 會清冷整個 collection cache、斬斷 fleet 進行中的 upsert/query
# （單一 chroma op timeout 600s），夜跑高峰被每 30 分鐘重啟一次反而把
# rag_sync 拖到撞 wall-clock 上限（2026-07-23/24 重啟 16 次的教訓）。
# 超標交給告警管線通知即可；「沒 PID」仍走 kickstart 修復不受此限。
_HEALTH_MEM_WARN_ONLY = {"com.xiaohong.chroma"}

_HEALTH_EXPECTED_LAUNCHD = list(_HEALTH_RSS_LIMIT_MB.keys())
_HEALTH_LONG_RUNNING = {
    "com.xiaohong.telegram",
    "com.xiaohong.telegram_black",
    "com.xiaohong.telegram_blue",
    "com.xiaohong.telegram_gray",
    "com.xiaohong.telegram_green",
    "com.xiaohong.telegram_indigo",
    "com.xiaohong.telegram_orange",
    "com.xiaohong.telegram_purple",
    "com.xiaohong.telegram_white",
    "com.xiaohong.telegram_yellow",
    # KeepAlive 常駐服務 — 沒 PID 即異常（launchctl kickstart 修）。
    "com.xiaohong.chroma",
    "com.xiaohong.tool_rpc",
    "com.xiaohong.web_server",
}
_HEALTH_OPTIONAL_LAUNCHD = {
    "com.xiaohong.telegram_black",
    "com.xiaohong.telegram_blue",
    "com.xiaohong.telegram_gray",
    "com.xiaohong.telegram_green",
    "com.xiaohong.telegram_indigo",
    "com.xiaohong.telegram_orange",
    "com.xiaohong.telegram_purple",
    "com.xiaohong.telegram_white",
    "com.xiaohong.telegram_yellow",
    # 看門狗 plist 尚未部署的環境不該誤報「沒載入」——optional 只在 plist
    # 存在時才檢查（_expected_launchd_labels）。
    "com.xiaohong.watchdog",
    # 同理：告警管線 / chroma / tool_rpc / web_server 沒部署的環境不誤報，
    # 有部署（plist 存在）就納入監看。
    "com.xiaohong.alert_check",
    "com.xiaohong.chroma",
    "com.xiaohong.tool_rpc",
    "com.xiaohong.web_server",
}
_HEALTH_LOG_LIMIT_MB = 5.0
_HEALTH_DISK_WARN_GB = 5.0
_HEALTH_NETWORK_HOSTS = (
    "api.telegram.org",
    "generativelanguage.googleapis.com",
    "www.googleapis.com",
)
_HEALTH_DNS_RETRY_DELAY_SEC = 1.0
# 「daemon 沒載入」重採一次的間隔。要 > bin/redeploy-daemons 的 unload→load
# 空窗（_reload_daemon 中間睡 1 秒），否則健檢正好在那一秒取樣就會誤判。
_HEALTH_LAUNCHD_RECHECK_DELAY_SEC = 2.0
# 重啟 daemon 時，SIGTERM 之後給它多久自己收乾淨（telegram bot 要存 offset、
# 關 session）才 kickstart -k。
_HEALTH_RESTART_SIGTERM_GRACE_SEC = 3.0


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_cloud_runtime() -> bool:
    return (
        _truthy(os.environ.get("RED_CLOUD_MODE"))
        or bool(os.environ.get("K_SERVICE"))
        or bool(os.environ.get("CLOUD_RUN_JOB"))
    )


def _is_macos_edge_host() -> bool:
    return platform.system() == "Darwin" and not _is_cloud_runtime()


def _launchctl_cmd(*args: str) -> list[str]:
    launchctl = "/bin/launchctl" if os.path.exists("/bin/launchctl") else "launchctl"
    return [launchctl, *args]


def _launchagent_path(label: str) -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")


def _launchd_loaded_labels() -> dict | None:
    """`launchctl list` 裡的 com.xiaohong.* → PID（沒 PID 為 None）。

    launchctl 自己失敗回 None，跟「一個都沒載入」區分開 —— 呼叫端不該把
    launchctl 壞掉誤當成整個 fleet 掉載入。
    """
    try:
        r = subprocess.run(_launchctl_cmd("list"), capture_output=True,
                           text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    loaded: dict = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].startswith("com.xiaohong."):
            pid = parts[0]
            loaded[parts[2]] = pid if pid.isdigit() else None
    return loaded


def _launchd_label_loaded(label: str) -> bool:
    """單一 label 現在在不在 launchd 裡。"""
    loaded = _launchd_loaded_labels()
    return bool(loaded) and label in loaded


def _recheck_missing_labels(missing: list[str]) -> dict:
    """隔一下重採 launchctl list，回傳這批 label 裡「其實有載入」的 → PID。

    為什麼要重採（2026-08-18 紅 bot 靜默掛掉）：bin/redeploy-daemons 的
    unload→load 中間有約 1 秒空窗，健檢正好在那一秒取樣就會看到「沒載入」。
    那次誤判很貴 —— auto_repair 照著誤判去動 launchctl，把 1.6 秒前才被
    redeploy 拉起來的新程序 unload 掉，label 整個從 launchd 消失。
    真的沒載入的 daemon 不會因為多等兩秒就自己長回來，所以這層只擋得掉
    「部署中取樣」這類暫態誤判，擋不掉真故障。
    """
    if not missing:
        return {}
    time.sleep(_HEALTH_LAUNCHD_RECHECK_DELAY_SEC)
    loaded = _launchd_loaded_labels()
    if not loaded:
        return {}
    return {label: loaded[label] for label in missing if label in loaded}


def _expected_launchd_labels() -> list[str]:
    """Return required launchd labels, skipping optional bots before deploy."""
    labels: list[str] = []
    for label in _HEALTH_EXPECTED_LAUNCHD:
        if label in _HEALTH_OPTIONAL_LAUNCHD and not os.path.exists(_launchagent_path(label)):
            continue
        labels.append(label)
    return labels


def _health_log(line: str):
    """寫一行 health log。"""
    if not _HEALTH_CHECK_LOG:
        return
    try:
        with open(_HEALTH_CHECK_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
    except Exception as _e:
        logger.debug("health log 寫入失敗：%s", _e)


def _check_daemons_health() -> list:
    """檢查 6 個 launchd daemon 是否 loaded / 跑得正常。"""
    if not _is_macos_edge_host():
        return []

    issues = []
    try:
        r = subprocess.run(_launchctl_cmd("list"), capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            msg = f"launchctl list 失敗（exit {r.returncode}）"
            if detail:
                msg += f"：{detail[:160]}"
            return [{"severity": "error", "area": "launchctl", "msg": msg}]
        loaded = {}
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].startswith("com.xiaohong."):
                pid = parts[0]
                loaded[parts[2]] = pid if pid.isdigit() else None
    except Exception as e:
        return [{"severity": "error", "area": "launchctl", "msg": f"launchctl list 失敗：{e}"}]

    expected = _expected_launchd_labels()
    # 第一次取樣說「沒載入」的，重採一次再定罪（部署空窗誤判）。
    loaded.update(_recheck_missing_labels([lb for lb in expected if lb not in loaded]))

    for label in expected:
        if label not in loaded:
            issues.append({
                "severity": "error", "area": f"daemon/{label}",
                "msg": "daemon 沒被 launchd 載入（應該要載入）",
                "repair": "launchctl_load",
            })
            continue
        pid = loaded[label]
        if label in _HEALTH_LONG_RUNNING and not pid:
            issues.append({
                "severity": "error", "area": f"daemon/{label}",
                "msg": "常駐 daemon 沒 PID（可能剛崩潰，launchd 快重啟）",
                "repair": "launchctl_kickstart",
            })
            continue
        if pid:
            try:
                rss_out = subprocess.run(
                    ["ps", "-o", "rss=", "-p", pid],
                    capture_output=True, text=True, timeout=3,
                )
                rss_kb = int((rss_out.stdout or "0").strip() or "0")
                rss_mb = rss_kb / 1024
                limit = _HEALTH_RSS_LIMIT_MB.get(label, 2048)
                if rss_mb > limit:
                    issue = {
                        "severity": "warning", "area": f"daemon/{label}",
                        "msg": f"記憶體過高 {rss_mb:.0f} MB > {limit} MB 上限",
                    }
                    if label not in _HEALTH_MEM_WARN_ONLY:
                        issue["repair"] = "launchctl_restart"
                        issue["label"] = label
                    issues.append(issue)
            except Exception as _e:
                logger.debug("RSS 檢查失敗 %s：%s", label, _e)
    return issues


def _check_state_files() -> list:
    """檢查關鍵 JSON 檔是否可讀可解析。"""
    issues = []
    state_files = [
        (DAEMON_STATE_FILE, "daemon_state.json"),
        (_EMAIL_CLASSIFY_CACHE_FILE, "email_classifications.json"),
        (_EMAIL_LAKE_STATE_FILE, "email_lake/last_sync.json"),
        (MEMORY_FILE, "memory.json"),
        (MISTAKES_FILE, "mistakes.json"),
    ]
    for path, label in state_files:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except json.JSONDecodeError as e:
            issues.append({
                "severity": "error", "area": f"state/{label}",
                "msg": f"JSON 損毀：{e}",
                "repair": "restore_from_bak", "path": path,
            })
        except Exception as _e:
            logger.debug("檢查 %s 失敗：%s", label, _e)
    return issues


def _check_data_stores() -> list:
    """檢查 ChromaDB、Parquet 能讀。"""
    issues = []
    try:
        col = _get_memory_collection()
        if col is None:
            # 帶出 memory 記下的真實 init 失敗原因——chroma_backend 防護拒開
            # （env 沒帶）、server 連不上、其他，各自的例外字串都不同。
            # 舊文案「Gemini embed 失敗？」是錯的：get_or_create_collection
            # 根本不會呼叫 embed（2026-06-12 誤導過診斷）。
            # 400 字截斷：chroma_backend 防護的 RuntimeError 連同 persist_dir
            # 路徑約 360-390 字，修復提示（export RED_CHROMA_HTTP_URL=…）在
            # 訊息尾段，截短就失去可操作性。
            reason = vector_store_last_error() or "init 未留下錯誤原因"
            issues.append({
                "severity": "warning", "area": "chroma",
                "msg": f"ChromaDB 無法初始化：{reason[:400]}",
            })
        else:
            _ = col.count()
    except Exception as e:
        issues.append({
            "severity": "warning", "area": "chroma",
            "msg": f"ChromaDB 讀取錯誤：{type(e).__name__}: {str(e)[:100]}",
        })

    if os.path.exists(_EMAIL_LAKE_PARQUET):
        # 只讀 parquet footer 的 schema 驗「檔案讀得起來」（健檢 Low：以前
        # _lake_load_df() 每 30 分鐘把整個 email lake 載進 pandas 只為驗可讀，
        # 白吃數百 MB 記憶體 + IO）。footer 損毀 / 檔案截斷這裡照樣會炸。
        try:
            import pyarrow.parquet as _pq
            _pq.read_schema(_EMAIL_LAKE_PARQUET)
        except ImportError:
            issues.append({
                "severity": "warning", "area": "email_lake",
                "msg": "Parquet 讀取失敗（pandas/pyarrow 缺？）",
            })
        except Exception as e:
            issues.append({
                "severity": "warning", "area": "email_lake",
                "msg": f"Parquet 錯誤：{type(e).__name__}",
            })
    try:
        from agent_core.operational_health import health_issues

        issues.extend(health_issues(cloud_runtime=_is_cloud_runtime()))
    except Exception as e:
        issues.append({
            "severity": "warning",
            "area": "operational_db",
            "msg": f"Postgres operational DB 健檢失敗：{type(e).__name__}",
        })
    return issues


def _check_dispatcher_task_tools() -> list:
    """排程任務點名的工具還進得了背景工具集嗎（設定漂移）。

    這是補「測試沒人跑」的空窗：同樣的稽核有測試版
    （tests/test_dispatcher_task_tool_reachability.py），但 daemon_tasks.json 是
    gitignore 的 runtime state，CI 上不存在 ⇒ 那支測試在 CI 只能 skip，**唯一跑
    得到的地方是部署機、而且要有人手動 make test-quiet**。排程是用 add_scheduled_task
    線上加的，不經 PR、不經 CI，所以新任務點名沒開的工具時沒有任何自動機制會發現
    （#350 那 5 支就是這樣潛伏了幾十輪）。掛在每 30 分鐘的 health_check 上補起來。

    嚴重度一律 warning：這不是服務中斷，是設定要人改（加白名單 or 改 prompt），
    沒有 repair 可自動套。health_check 對 🟡 會通知且有 dedup，同一組問題只吵一次。
    """
    issues = []
    try:
        from agent_core.daemon_dispatcher import audit_task_tool_refs
        from agent_core.scheduler import _load_daemon_tasks
        from agent_core.tool_registry import tools_list

        tasks = _load_daemon_tasks().get("tasks", [])
        if not tasks:            # 沒排程（dev clone / 全新機器）不是問題
            return issues
        for problem in audit_task_tool_refs(tasks, tools_list):
            issues.append({
                "severity": "warning", "area": "dispatcher_tasks",
                "msg": f"排程任務工具不可達：{problem}",
            })
    except Exception as e:
        issues.append({
            "severity": "warning", "area": "dispatcher_tasks",
            "msg": f"排程任務工具稽核失敗：{type(e).__name__}",
        })
    return issues


def _check_logs() -> list:
    """檢查日誌檔大小。"""
    issues = []
    if not _LOG_DIR or not os.path.isdir(_LOG_DIR):
        return issues
    for f in os.listdir(_LOG_DIR):
        if not f.startswith("daemon-") or not f.endswith(".log"):
            continue
        full = os.path.join(_LOG_DIR, f)
        try:
            mb = os.path.getsize(full) / 1024 / 1024
            if mb > _HEALTH_LOG_LIMIT_MB:
                issues.append({
                    "severity": "info", "area": f"log/{f}",
                    "msg": f"日誌檔 {mb:.1f} MB（將自動輪替）",
                    "repair": "rotate_log", "task_name": f[len("daemon-"):-len(".log")],
                })
        except Exception as _e:
            logger.debug("log 檢查失敗 %s：%s", f, _e)
    return issues


def _check_disk() -> list:
    """檢查磁碟剩餘空間。"""
    if _is_cloud_runtime():
        return []

    try:
        s = shutil.disk_usage(os.path.dirname(_LOG_DIR) if _LOG_DIR else os.getcwd())
        free_gb = s.free / 1024 / 1024 / 1024
        if free_gb < _HEALTH_DISK_WARN_GB:
            return [{
                "severity": "warning", "area": "disk",
                "msg": f"磁碟剩餘僅 {free_gb:.1f} GB",
                "repair": "cleanup_caches",
            }]
    except Exception as _e:
        logger.debug("磁碟檢查失敗：%s", _e)
    return []


def _network_dns_failures(hosts) -> list[tuple[str, str]]:
    failures = []
    for h in hosts:
        try:
            socket.gethostbyname(h)
        except Exception as e:
            failures.append((h, str(e)))
    return failures


def _format_dns_failures(failures: list[tuple[str, str]]) -> str:
    return "；".join(f"{host}: {err}" for host, err in failures)


def _check_network() -> list:
    """檢查關鍵服務 DNS 是否能解析（快速、不發請求）。"""
    failures = _network_dns_failures(_HEALTH_NETWORK_HOSTS)
    if not failures:
        return []
    return [{
        "severity": "warning", "area": "network/dns",
        "msg": f"DNS 解析失敗：{_format_dns_failures(failures)}",
        "repair": "flush_dns_cache",
        "hosts": list(_HEALTH_NETWORK_HOSTS),
    }]


def _repair_launchctl_load(label: str) -> str:
    """把「應該載入卻不在 launchd 裡」的 daemon 載回來。

    只有 enable + bootstrap，**絕不 unload**。2026-08-18 紅 bot 靜默掛掉就是
    這裡原本的 unload + load 幹的：

      1. 健檢在 bin/redeploy-daemons 的 unload→load 空窗取樣，誤判
         com.xiaohong.telegram「沒被 launchd 載入」；
      2. 到這裡修的時候，redeploy 早就把它拉起來了（新 PID 才活 1.6 秒），
         unload 於是把這個健康的新程序 SIGTERM 掉；
      3. 啟動到一半的 bot 沒能在 5 秒內收掉 → `subprocess.run(timeout=5)`
         先丟 TimeoutExpired → **下面那行 load 根本沒機會跑**；
      4. launchd 自己的 5 秒寬限期同時到期 → SIGKILL → removing service
         → label 整個從 launchd 消失，紅 bot 掛了近 3 分鐘沒人知道。

    bootstrap 沒有這個問題：對「其實已經載入」只會回一個無害的錯誤，不會殺掉
    任何正在跑的東西 —— 誤判的代價從「殺掉一個健康 daemon」降成「一則沒用的
    錯誤訊息」。
    """
    plist = _launchagent_path(label)
    if not os.path.exists(plist):
        return f"plist 不存在：{plist}"
    try:
        # 舊的 unload 會把 label 寫進 launchd 的 disabled set，之後 bootstrap
        # 一律回 Input/output error；enable 撤銷它（idempotent，沒被 disable
        # 也無害）。
        subprocess.run(_launchctl_cmd("enable", f"gui/{os.getuid()}/{label}"),
                       capture_output=True, timeout=5)
        r = subprocess.run(
            _launchctl_cmd("bootstrap", f"gui/{os.getuid()}", plist),
            capture_output=True, timeout=10,
        )
    except Exception as e:
        return f"❌ {e}"

    # 不看 exit code：已經載入時 bootstrap 回非 0（"service already loaded"），
    # 那反而代表沒事。唯一算數的是它現在到底在不在 launchctl list 裡。
    if _launchd_label_loaded(label):
        return f"✅ 已載入 {label}"
    err = (r.stderr or b"").decode(errors="replace").strip()
    return f"❌ bootstrap 失敗：{err[:100]}"


def _repair_launchctl_restart(label: str) -> str:
    """重啟一個已載入的 daemon（記憶體超標等）。

    SIGTERM（讓 bot 自己存 offset、收 session）→ 寬限 → `kickstart -k`。
    不再用 unload + load：後者會把 label 短暫移出 launchd domain，中間任何一步
    失敗就變成「daemon 憑空消失」，而且 unload 對沒即時退出的程序會卡滿
    timeout、後面那行 load 根本跑不到（見 _repair_launchctl_load 的事故說明）。
    kickstart 全程不動 domain：kill 跟 respawn 是 launchd 內部一件事。
    """
    loaded = _launchd_loaded_labels()
    if loaded is None:
        return "❌ launchctl list 失敗，不動它比亂重啟安全"
    if label not in loaded:
        # 根本沒載入 —— 該做的是載回來，不是重啟。
        return _repair_launchctl_load(label)
    pid = loaded.get(label)
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
            time.sleep(_HEALTH_RESTART_SIGTERM_GRACE_SEC)
        except (ValueError, TypeError, ProcessLookupError, PermissionError):
            pass  # 已經死了 / 不是我們的 —— 下面 kickstart -k 收尾
    return _repair_launchctl_kickstart(label)


def _repair_launchctl_kickstart(label: str) -> str:
    """用 launchctl kickstart 重跑 daemon。"""
    try:
        r = subprocess.run(
            _launchctl_cmd("kickstart", "-k", f"gui/{os.getuid()}/{label}"),
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return f"✅ 已 kickstart {label}"
        return _repair_launchctl_load(label)
    except Exception:
        return _repair_launchctl_load(label)


def _repair_restore_bak(path: str) -> str:
    """從 .bak 還原損毀的 JSON。"""
    bak = path + ".bak"
    if not os.path.exists(bak):
        return f"❌ 沒有備份檔可還原：{bak}"
    try:
        shutil.copy2(bak, path)
        return f"✅ 從 {bak} 還原 {os.path.basename(path)}"
    except Exception as e:
        return f"❌ 還原失敗：{e}"


def _repair_rotate_log(task_name: str) -> str:
    """呼叫 daemon log 輪替（要 import agent_daemon 的函式會造成循環，用 in-place rotation）。"""
    log_path = os.path.join(_LOG_DIR, f"daemon-{task_name}.log")
    if not os.path.exists(log_path):
        return f"log 不存在：{log_path}"
    try:
        size_mb = os.path.getsize(log_path) / 1024 / 1024
        keep_bytes = 1024 * 1024
        with open(log_path, "rb") as f:
            f.seek(max(0, os.path.getsize(log_path) - keep_bytes))
            f.readline()
            tail = f.read()
        header = f"# [auto-rotate @ {datetime.now().isoformat(timespec='seconds')}] 原 {size_mb:.1f}MB → 保留後 {len(tail)/1024:.0f}KB\n".encode("utf-8")
        with open(log_path, "wb") as f:
            f.write(header)
            f.write(tail)
        return f"✅ 輪替 {task_name} 日誌（{size_mb:.1f}MB → {len(tail)/1024:.0f}KB）"
    except Exception as e:
        return f"❌ 輪替失敗：{e}"


def _repair_cleanup_caches() -> str:
    """清磁碟：video cache、舊 chroma-policy-test 之類。"""
    freed_mb = 0.0
    cleaned = []
    try:
        if os.path.isdir(_ERP_VIDEO_CACHE_DIR):
            for f in os.listdir(_ERP_VIDEO_CACHE_DIR):
                p = os.path.join(_ERP_VIDEO_CACHE_DIR, f)
                if os.path.isfile(p):
                    freed_mb += os.path.getsize(p) / 1024 / 1024
                    os.remove(p)
                    cleaned.append(f)
    except Exception as _e:
        logger.debug("清 ERP cache 失敗：%s", _e)
    if cleaned:
        return f"✅ 清 {len(cleaned)} 個 video cache（省 {freed_mb:.0f} MB）"
    return "無可清除的 cache"


def _run_dns_cache_command(cmd: list[str]):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return None
        detail = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        return detail[:160] if detail else f"exit {r.returncode}"
    except Exception as e:
        return str(e)[:160]


def _repair_flush_dns_cache(hosts=None) -> str:
    """保守修復 DNS：清本機 cache 後重試，不改網路設定。"""
    targets = list(hosts or _HEALTH_NETWORK_HOSTS)
    notes = []
    for cmd, label in (
        (["dscacheutil", "-flushcache"], "dscacheutil"),
        (["killall", "-HUP", "mDNSResponder"], "mDNSResponder"),
    ):
        err = _run_dns_cache_command(cmd)
        if err:
            notes.append(f"{label}: {err}")

    time.sleep(_HEALTH_DNS_RETRY_DELAY_SEC)
    failures = _network_dns_failures(targets)
    suffix = f"（{'；'.join(notes)}）" if notes else ""
    if not failures:
        return f"✅ 已清 DNS cache，重新解析成功{suffix}"
    if notes:
        return f"❌ 已嘗試清 DNS cache（{'；'.join(notes)}），但仍解析失敗：{_format_dns_failures(failures)}"
    return f"❌ 已清 DNS cache，但仍解析失敗：{_format_dns_failures(failures)}"


def _apply_repair(issue: dict) -> str:
    """對一個 issue 執行對應的自動修復。"""
    action = issue.get("repair")
    if not action:
        return "（無自動修復動作）"
    if action == "launchctl_load":
        return _repair_launchctl_load(issue["area"].split("/", 1)[1])
    if action == "launchctl_restart":
        return _repair_launchctl_restart(issue["label"])
    if action == "launchctl_kickstart":
        return _repair_launchctl_kickstart(issue["area"].split("/", 1)[1])
    if action == "restore_from_bak":
        return _repair_restore_bak(issue["path"])
    if action == "rotate_log":
        return _repair_rotate_log(issue["task_name"])
    if action == "cleanup_caches":
        return _repair_cleanup_caches()
    if action == "flush_dns_cache":
        return _repair_flush_dns_cache(issue.get("hosts"))
    return f"（未知修復動作：{action}）"


def health_check(auto_repair: bool = True):
    """系統全面健康檢查 + 自動修復。
    檢查項目：
      - 6 個 daemon 是否 loaded / 記憶體正常
      - 關鍵 JSON 檔（state / memory / mistakes / email cache）沒損毀
      - ChromaDB 可讀
      - Email Lake Parquet 可讀
      - 排程任務點名的工具還進得了背景工具集（設定漂移）
      - 日誌檔大小（> 5 MB 會自動輪替）
      - 磁碟剩餘空間（< 5 GB 會清 cache）
      - 網路 DNS（Gemini / Gmail / Telegram）
    auto_repair=True 時，偵測到問題自動嘗試修復。回傳完整報告。"""
    all_issues = []
    all_issues += _check_daemons_health()
    all_issues += _check_state_files()
    all_issues += _check_data_stores()
    all_issues += _check_dispatcher_task_tools()
    all_issues += _check_logs()
    all_issues += _check_disk()
    all_issues += _check_network()

    lines = [f"🩺 健康檢查 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    if not all_issues:
        lines.append("  ✅ 一切正常，沒有發現問題")
        _health_log("OK no issues")
        return "\n".join(lines)

    severity_emoji = {"error": "🔴", "warning": "🟡", "info": "🔵"}
    lines.append(f"  發現 {len(all_issues)} 個問題：\n")
    repaired = 0
    for i, issue in enumerate(all_issues, 1):
        emoji = severity_emoji.get(issue["severity"], "⚪")
        lines.append(f"  {i}. {emoji} [{issue['area']}] {issue['msg']}")
        _health_log(f"{issue['severity'].upper()} {issue['area']}: {issue['msg']}")
        if auto_repair and issue.get("repair"):
            repair_result = _apply_repair(issue)
            lines.append(f"     → {repair_result}")
            _health_log(f"  REPAIR: {repair_result}")
            if repair_result.startswith("✅"):
                repaired += 1

    if auto_repair:
        lines.append(f"\n📝 自動修復：{repaired}/{sum(1 for i in all_issues if i.get('repair'))} 成功")
    else:
        lines.append("\n（auto_repair=False，只報告不修）")
    return "\n".join(lines)
