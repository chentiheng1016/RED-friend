"""awake_clock tick 檔隔離 mixin —— 給任何吃 age-based 告警判準的測試共用。

為什麼需要：`dashboard_alerts` 那批「距離上次 X 過了幾小時」的判準（email ingest
heartbeat、ERP 鏡像 manifest、庫存編號對照、RDP 巡檢報告）一律走
`_observed_age_h()` → `awake_clock.observed_age_hours()`，會**扣掉機器睡著、沒人
在線觀測的那段**。扣多少完全取決於 `var/state/awake_ticks.json` 這條 tick 時間軸。

那份檔是 **live 執行狀態**，而且測試自己會餵它：`dashboard_alerts.check_alerts()`
進來就打一顆 tick，而 test_alert_correlation / test_security_regressions 等好幾支
測試都真的呼叫 `check_alerts()`。於是跨「輪次」就長出空窗：

    上一輪測試（或 alert_check daemon）在 T 打了一批 tick
    這一輪在 T+6h 才跑 → 兩批之間 6 小時沒 tick
    → awake_clock 判定「那 6 小時沒在觀測」
    → 30h 的 raw age 只剩 24h 觀測時數 → 26h 門檻不觸發
    → `test_stale_warn_then_crit` 這種「本該報」的測試收到空清單而紅

**單獨跑該模組會過、整套 discover 會紅**，差別就在這裡：`python -m unittest
tests.test_x` 會先匯入 tests package，`tests/__init__.py` 把 `RED_RUNTIME_DIR`
導向一次性 tmp dir，`STATE_DIR`（進而 tick 檔）跟著乾淨；但 `unittest discover
-s tests` 少了 `-t .` 時，`agent_core.logging_and_paths` 比 `tests/__init__` **更早**
被匯入，`STATE_DIR` 早就用 live `var/` 算好了 —— 同 tests/run_history_isolation.py
記的那個雷，那邊是假 run 寫進 live var/runs，這邊是 live tick 洩進 age 判準。

這個 mixin 是第二道防線：把 tick 檔指到 per-test tmpdir。空檔＝沒有歷史＝
awake_clock 自己的降級路徑「視為全程在線」（見該模組 docstring「降級行為」），
age 於是回到單純的 wall-clock 差，不管入口是哪一種都一樣。順帶讓這些測試期間
的 `record_tick()` 落在 tmpdir，不再往 live 時間軸灌測試用的 tick。

用法（比照 tests/run_history_isolation.py 的形狀）::

    class FooTest(AwakeClockIsolationMixin, unittest.TestCase):
        def setUp(self):
            self._awake_clock_iso_setup()
            self.addCleanup(self._awake_clock_iso_teardown)

⚠️ `_TICKS_FILE` 在 import time 就從 `STATE_DIR` 算好 —— 只 patch
`logging_and_paths.STATE_DIR` 沒用，要直接 rebind 這個模組常數（同
tests/test_awake_clock.py TickPersistenceTests 的手法）。
"""
from __future__ import annotations

import os
import shutil
import tempfile


class AwakeClockIsolationMixin:
    """把 awake_clock 的 tick 檔改指 per-test tmpdir（空的＝視為全程在線）。"""

    def _awake_clock_iso_setup(self) -> str:
        """重導 tick 檔路徑，回傳該 tmpdir（測試想自己鋪 tick 時間軸時可用）。"""
        from agent_core import awake_clock

        self._awake_iso_tmp = tempfile.mkdtemp(prefix="awake_iso_")
        self._awake_iso_orig = awake_clock._TICKS_FILE
        awake_clock._TICKS_FILE = os.path.join(self._awake_iso_tmp, "awake_ticks.json")
        return self._awake_iso_tmp

    def _awake_clock_iso_teardown(self) -> None:
        from agent_core import awake_clock

        if hasattr(self, "_awake_iso_orig"):
            awake_clock._TICKS_FILE = self._awake_iso_orig
            del self._awake_iso_orig
        tmp = getattr(self, "_awake_iso_tmp", "")
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            self._awake_iso_tmp = ""
