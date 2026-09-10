"""run_history 寫入隔離 mixin —— 給任何會觸發 @audited 的測試共用。

為什麼需要：`tg_auth.wrap_sensitive_tool` 對 CONFIRM 以上的工具**自動掛
@audited**（見 tg_auth.py 的 auto-audit 段）。所以「只是驗 wrap 行為」的
測試也會在 runs/index.jsonl 留下真紀錄 —— tool=send_gmail / run_shell、
elapsed_sec≈0、short_result 是 fixture 字串。這些假 run 會被
`metrics._load_runs_in_window(24)` 撈進 `metrics_overview()`，dashboard
的「今日任務失敗率」跟著虛高（2026-08-12 事故：live var/runs 混進 40 筆
假 run，`bin/red-status` 從 100 掉到 85／success_pct 70%）。

tests/__init__.py 已把 `RED_RUNTIME_DIR` 導向一次性 tmp dir，但那層只在
tests 以 **package** 匯入時才生效（`unittest discover -s tests -t .`，即
`make test` / `make test-quiet`）。少了 `-t .`（top_level_dir 變成 tests/，
模組以 top-level 匯入）或直接 `python tests/test_x.py`，`__init__` 根本不
執行 → RUNS_DIR 還是指 live var/runs。2026-08-12 那 40 筆就是這樣進去的。

這個 mixin 是第二道防線：不管入口是哪一種，寫入都落在自己的 tmpdir。

用法（比照 test_security_regressions._IsolatedStateMixin 的形狀）::

    class FooTest(RunHistoryIsolationMixin, unittest.TestCase):
        def setUp(self):
            self._run_history_iso_setup()
        def tearDown(self):
            self._run_history_iso_teardown()

⚠️ 兩個踩過的雷：
  1. `RUNS_INDEX` / `SCREENSHOTS_DIR` 在 import time 就從 RUNS_DIR 算好，
     只 patch `logging_and_paths.RUNS_DIR` 沒用，要逐個 rebind。
  2. 紀錄是在**工具實際回傳的那個 thread** 裡寫的。工具若跑在背景 thread
     （task_queue 的軟逾時），tearDown 復原路徑後它才回傳＝照樣寫進 live。
     那種測試要先 join 到 thread 結束，再讓隔離拆掉。
"""
from __future__ import annotations

import os
import shutil
import tempfile

# run_history 在 import time 從 RUNS_DIR 派生的模組常數，全部要一起 rebind。
_PATCHED_ATTRS = ("RUNS_DIR", "RUNS_INDEX", "SCREENSHOTS_DIR")


class RunHistoryIsolationMixin:
    """把 run_history 的寫入路徑改指 per-test tmpdir。"""

    def _run_history_iso_setup(self) -> str:
        """重導 run_history 寫入路徑，回傳該 tmpdir（測試想斷言內容時可用）。"""
        import agent_core.run_history as rh

        self._rh_iso_tmp = tempfile.mkdtemp(prefix="rh_iso_")
        self._rh_iso_orig = {attr: getattr(rh, attr) for attr in _PATCHED_ATTRS}
        rh.RUNS_DIR = self._rh_iso_tmp
        rh.RUNS_INDEX = os.path.join(self._rh_iso_tmp, "index.jsonl")
        rh.SCREENSHOTS_DIR = os.path.join(self._rh_iso_tmp, "screenshots")
        return self._rh_iso_tmp

    def _run_history_iso_teardown(self) -> None:
        import agent_core.run_history as rh

        for attr, value in getattr(self, "_rh_iso_orig", {}).items():
            setattr(rh, attr, value)
        self._rh_iso_orig = {}
        tmp = getattr(self, "_rh_iso_tmp", "")
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            self._rh_iso_tmp = ""
