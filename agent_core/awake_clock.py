"""「這段時間我們實際盯著多久」—— 給 age-based 告警扣掉沒在觀測的時間。

## 要解決什麼

`dashboard_alerts` 有一整批「距離上次 X 過了幾小時」的判準：email ingest 的
heartbeat、ERP 鏡像的 manifest、庫存編號對照、RDP 巡檢報告……。這台是 MacBook，
**闔上蓋子睡 10 小時，這些 age 全部同時超標**，醒來瞬間噴一整排告警，而系統從頭
到尾都是好的（2026-07-07 就實際發生過一次，結論是喚醒自癒、資料無損）。

假警報的代價不是那一則通知，是**人會學會忽略這個面板**。所以這裡不調門檻（門檻
是對的：ERP 鏡像真的 26 小時沒刷就是有事），而是把問題問對：

    ❌ 「距離上次刷新過了幾小時？」
    ✅ 「距離上次刷新，我們**實際在線觀測**了幾小時？」

機器睡著的那段時間，daemon 本來就不會跑、也本來就不該刷 —— 那段不算數。

## 怎麼量

`check_alerts()` 每次跑就打一個 tick（alert_check daemon 的 StartInterval=300s）。
tick 之間的間隔遠大於預期＝那段時間沒人在線，記為「未觀測」。

**刻意誠實的語意**：這量的不是「機器有沒有睡」，是「我們有沒有在看」。alert_check
自己掛掉造成的空窗也會被算成未觀測 —— 那是對的，那段時間我們確實沒有資格聲稱看到
了什麼。（而且它掛著的時候本來也不會有任何告警發出。）

## 為什麼不解析 pmset

`pmset -g log` 可以不用 sudo 讀到真正的睡眠/喚醒事件，但：**單次呼叫 ~1.0 秒、
30,000 行輸出**，而這個判斷每 5 分鐘要用一次；而且 macOS 專屬 —— 這個 repo 同一
份碼也跑 Cloud Run（`RED_CLOUD_MODE`），那邊根本沒有 pmset。tick 檔便宜、可攜、
自我 bootstrap。

## 降級行為

沒有歷史（第一次部署、或 tick 檔壞掉）一律回「完全在線」＝維持現行行為。這條路
**寧可誤報也不漏報**：我們不確定的時候，不該幫告警消音。
"""
from __future__ import annotations

import json
import os
import time

from agent_core.env_utils import env_int
from agent_core.logging_and_paths import STATE_DIR, logger

_TICKS_FILE = os.path.join(STATE_DIR, "awake_ticks.json")

# alert_check 的 StartInterval（launchd/templates/com.xiaohong.alert_check.plist）。
_EXPECTED_TICK_S = 300

# 間隔超過這個倍數才算「沒在觀測」。3× 是為了容忍 daemon 排程抖動、機器忙碌時
# 遲到的那種正常延遲 —— 那些不是空窗。
_GAP_FACTOR = 3

# 保留多久的 tick。最長的判準窗是 RDP 報告的 48h，留 7 天綽綽有餘；
# 5 分鐘一顆 → 7 天約 2016 顆。
_RETENTION_S = 7 * 86400
_MAX_TICKS = 4000


def _expected_tick_s() -> int:
    return env_int("RED_AWAKE_TICK_INTERVAL_S", _EXPECTED_TICK_S,
                   min_value=30, max_value=3600)


def _gap_threshold_s() -> float:
    return _expected_tick_s() * env_int("RED_AWAKE_GAP_FACTOR", _GAP_FACTOR,
                                        min_value=2, max_value=20)


def _read_ticks() -> list[float]:
    try:
        with open(_TICKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 - 沒檔 / 壞檔一律當沒有歷史
        return []
    ticks = data.get("ticks") if isinstance(data, dict) else None
    if not isinstance(ticks, list):
        return []
    out = []
    for t in ticks:
        try:
            out.append(float(t))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def record_tick(now: float | None = None) -> None:
    """記一顆「此刻我們在線」。best-effort，永不 raise。"""
    now = time.time() if now is None else float(now)
    ticks = [t for t in _read_ticks() if now - t <= _RETENTION_S]
    ticks.append(now)
    if len(ticks) > _MAX_TICKS:
        ticks = ticks[-_MAX_TICKS:]
    try:
        os.makedirs(os.path.dirname(_TICKS_FILE), exist_ok=True)
        tmp = _TICKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ticks": ticks}, fh)
        os.replace(tmp, _TICKS_FILE)
    except Exception as e:  # noqa: BLE001 - 觀測用的簿記壞掉不該影響告警本身
        logger.debug("awake_clock 寫 tick 失敗：%s", e)


def unobserved_seconds_since(since_ts: float, now: float | None = None,
                             ticks: list[float] | None = None) -> float:
    """`since_ts` 到現在，有多少秒是沒人在線觀測的。

    純函式（ticks 可由 caller 傳）方便單元測試。沒有歷史時回 0.0
    ＝視為全程在線，維持未加這層之前的行為。
    """
    now = time.time() if now is None else float(now)
    ticks = _read_ticks() if ticks is None else sorted(float(t) for t in ticks)
    if not ticks or now <= since_ts:
        return 0.0
    gap_s = _gap_threshold_s()
    expected = _expected_tick_s()

    # 🚨 比最舊的 tick 更早的那段是**未知**，不是「沒觀測」—— 可能只是 tick 紀錄
    # 本身才剛開始（新部署、檔案剛壞掉重建）。把未知當成沒觀測的話，剛部署的頭
    # 幾小時會把所有 age-based 告警一起靜音，那比誤報更糟。未知一律當成有觀測。
    #
    # 真正的睡眠不受影響：它會落在「睡前最後一顆」與「醒後第一顆」**兩顆 tick
    # 之間**，照樣抓得到。
    start = max(since_ts, ticks[0])
    if now <= start:
        return 0.0

    # 最後一顆 tick 到現在也可能是空窗（例如剛從睡眠醒來、這輪還沒跑完）。
    points = [start] + [t for t in ticks if start < t <= now] + [now]
    total = 0.0
    for prev, cur in zip(points, points[1:]):
        gap = cur - prev
        if gap > gap_s:
            # 扣掉一個正常間隔：那段是「本來就該等」的，不是空窗。
            total += gap - expected
    return max(0.0, total)


def observed_age_hours(since_ts: float, now: float | None = None,
                       ticks: list[float] | None = None) -> float:
    """從 `since_ts` 到現在的「實際觀測時數」＝ 總時數 − 未觀測時數。"""
    now = time.time() if now is None else float(now)
    raw = max(0.0, now - since_ts)
    return max(0.0, raw - unobserved_seconds_since(since_ts, now, ticks)) / 3600.0
