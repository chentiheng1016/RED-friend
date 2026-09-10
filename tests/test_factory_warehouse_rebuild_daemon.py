"""factory_warehouse_rebuild cron daemon：runner 包裝 + plist 結構驗證（免網路）。"""
import importlib.util
import plistlib
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_core.path_safety import _REPO_ROOT

_REPO = Path(_REPO_ROOT)
_RUNNER = _REPO / "launchd" / "scripts" / "factory_warehouse_rebuild.py"
_PLIST = _REPO / "launchd" / "templates" / "com.xiaohong.factory_warehouse_rebuild.plist"


def _load_runner():
    spec = importlib.util.spec_from_file_location("_fw_rebuild_runner", _RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # 跑 top-level import（不跑 main，受 __name__ 守衛）
    return mod


class RunnerTests(unittest.TestCase):
    def test_rebuild_success_returns_summary(self):
        mod = _load_runner()
        # mock 掉 build 與 backfill，別碰真 var/Drive（[live var 洩漏進測試] 鐵則）
        with mock.patch.object(mod, "build_factory_warehouse", return_value={
            "daily_rows": 5, "orders": 3, "report_modified": "2026-06-17", "warnings": [],
        }), mock.patch(
            "agent_core.factory_warehouse_extract.extract_payments_batch",
            return_value={"extracted": 0, "candidates_remaining": 0, "spent_usd": 0.0},
        ):
            res = mod._rebuild()
        self.assertEqual(res["daily_rows"], 5)
        self.assertEqual(res["orders"], 3)

    def test_rebuild_error_path_exits_nonzero(self):
        # 重建失敗要 exit≠0（launchctl 才看得到、daemon_fail 告警才會響）；
        # 以前 return res 落到 exit 0 = 連續失敗完全靜默。
        mod = _load_runner()
        with mock.patch.object(mod, "build_factory_warehouse",
                               return_value={"error": "找不到生產日報"}):
            with self.assertRaises(SystemExit) as ctx:
                mod._rebuild()
        self.assertEqual(ctx.exception.code, 1)


class BackfillTimeBudgetTests(unittest.TestCase):
    """2026-07-24 12:50 exit 75 根因：backfill 無時間上限、撞整輪 run_with_deadline。
    _rebuild 要把「deadline 扣掉主重建耗時、再扣安全邊際」的剩餘傳給 backfill。"""

    _BUILD_OK = {"daily_rows": 1, "orders": 0, "report_modified": "2026-07-24",
                 "warnings": []}

    def test_backfill_receives_remaining_time_budget(self):
        mod = _load_runner()
        captured = {}

        def fake_batch(max_docs, time_budget_s=None):
            captured["budget"] = time_budget_s
            return {"extracted": 0, "candidates_remaining": 0, "spent_usd": 0.0}

        with mock.patch.object(mod, "build_factory_warehouse",
                               return_value=dict(self._BUILD_OK)), \
             mock.patch("agent_core.factory_warehouse_extract.extract_payments_batch",
                        side_effect=fake_batch):
            mod._rebuild(deadline_s=1200, started_ts=time.monotonic())
        self.assertIsNotNone(captured.get("budget"))
        # 1200 - 主重建耗時（mock、≈0）- 安全邊際 300 → 略低於 900
        self.assertLessEqual(captured["budget"], 1200 - mod._BACKFILL_SAFETY_MARGIN_S)
        self.assertGreater(captured["budget"], 0)

    def test_backfill_skipped_when_no_time_left(self):
        # 主重建吃掉太多時間 → backfill 整個跳過（別開工一半被 os._exit 砍）
        mod = _load_runner()
        called = []
        with mock.patch.object(mod, "build_factory_warehouse",
                               return_value=dict(self._BUILD_OK)), \
             mock.patch("agent_core.factory_warehouse_extract.extract_payments_batch",
                        side_effect=lambda **kw: called.append(kw) or {}):
            res = mod._rebuild(deadline_s=1200,
                               started_ts=time.monotonic() - 1100)
        self.assertEqual(called, [])          # 沒被呼叫
        self.assertEqual(res["daily_rows"], 1)  # 倉本體結果照常回傳

    def test_no_deadline_info_means_no_budget(self):
        # 舊呼叫慣例（不帶 deadline）不受影響：time_budget_s=None
        mod = _load_runner()
        captured = {}

        def fake_batch(max_docs, time_budget_s="sentinel"):
            captured["budget"] = time_budget_s
            return {"extracted": 0, "candidates_remaining": 0, "spent_usd": 0.0}

        with mock.patch.object(mod, "build_factory_warehouse",
                               return_value=dict(self._BUILD_OK)), \
             mock.patch("agent_core.factory_warehouse_extract.extract_payments_batch",
                        side_effect=fake_batch):
            mod._rebuild()
        self.assertIsNone(captured["budget"])


class PlistTests(unittest.TestCase):
    def setUp(self):
        self.data = plistlib.loads(_PLIST.read_bytes())

    def test_label_and_program(self):
        self.assertEqual(self.data["Label"], "com.xiaohong.factory_warehouse_rebuild")
        self.assertIn("factory_warehouse_rebuild.py", self.data["ProgramArguments"][1])

    def test_two_calendar_times_0700_and_1230(self):
        times = self.data["StartCalendarInterval"]
        self.assertIsInstance(times, list)   # 陣列 = 多觸發時間
        self.assertEqual({(t["Hour"], t["Minute"]) for t in times}, {(7, 0), (12, 30)})

    def test_no_run_at_load(self):
        # bootstrap 時不可立刻跑一輪（RunAtLoad 隱含啟動觸發的坑）
        self.assertFalse(self.data["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
