"""health_check 的 launchd 自動修復不准把健康的 daemon 弄死。

2026-08-18 事故：health_check 在 bin/redeploy-daemons 的 unload→load 空窗
（約 1 秒）取樣 launchctl list，誤判 com.xiaohong.telegram「沒被 launchd
載入」，然後 auto_repair 照著誤判做了 unload + load：

  * unload 把 redeploy 在 1.6 秒前才拉起來的新程序 SIGTERM 掉；
  * 啟動到一半的 bot 沒在 5 秒內收掉 → subprocess.run(timeout=5) 先炸
    → **後面那行 load 根本沒跑到**；
  * launchd 自己的 5 秒寬限期同時到期 → SIGKILL → removing service
    → label 整個從 launchd 消失。

紅 bot 就這樣靜默掛了近 3 分鐘。這裡釘兩件事：
  1. 判讀要先重採一次再定罪（暫態誤判不算數）；
  2. 修復動作一律非破壞性 —— 誤判的代價最多是一則沒用的錯誤訊息，
     不能是「殺掉一個跑得好好的 daemon」。
"""
from __future__ import annotations

import os
import signal
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import health  # noqa: E402

LABEL = "com.xiaohong.telegram"


def _list_stdout(labels_with_pids: dict) -> str:
    return "".join(f"{pid}\t0\t{label}\n" for label, pid in labels_with_pids.items())


class _FakeLaunchctl:
    """記錄每一條 launchctl/ps 呼叫；`list` 依序吐出預先排好的樣本。

    簽名用 *args/**kwargs：patch 過的 fake 被不同呼叫點以不同關鍵字參數呼叫
    （text=True 與否都有），寫死簽名會在別的測試裡爆掉。
    """

    def __init__(self, list_samples: list[dict], kickstart_rc: int = 0):
        self.samples = list(list_samples)
        self.kickstart_rc = kickstart_rc
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.cmds.append(list(cmd))
        result = mock.Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = b""
        if cmd[0] == "ps":
            result.stdout = "10240\n"          # 10 MB，遠低於任何上限
            return result
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "list":
            sample = self.samples.pop(0) if self.samples else {}
            result.stdout = _list_stdout(sample)
        elif sub == "kickstart":
            result.returncode = self.kickstart_rc
            result.stdout = ""
            result.stderr = ""
        elif sub == "bootstrap":
            # 真 launchctl 對「已經載入」也回非 0；修復成敗只看 list。
            result.returncode = 1
            result.stderr = b"Bootstrap failed: 37: Operation already in progress"
        return result

    @property
    def subcommands(self) -> list[str]:
        return [c[1] for c in self.cmds if c and c[0] != "ps" and len(c) > 1]


class LaunchdRepairIsNonDestructiveTests(unittest.TestCase):
    """修復動作不准 unload —— 那是 2026-08-18 殺掉紅 bot 的那一刀。"""

    def test_load_repair_never_unloads_and_bootstraps_instead(self):
        fake = _FakeLaunchctl([{LABEL: "6803"}])
        with mock.patch.object(health.os.path, "exists", return_value=True), \
                mock.patch.object(health.subprocess, "run", side_effect=fake):
            msg = health._repair_launchctl_load(LABEL)

        self.assertNotIn("unload", fake.subcommands,
                         "修復不得 unload：判讀可能已經過期，unload 會殺掉健康的 daemon")
        self.assertIn("bootstrap", fake.subcommands)
        self.assertIn("enable", fake.subcommands,
                      "舊 unload 會把 label 留在 disabled set，bootstrap 前要先 enable")
        self.assertTrue(msg.startswith("✅"), msg)

    def test_load_repair_trusts_launchctl_list_not_bootstrap_exit_code(self):
        """bootstrap 對「已經載入」回非 0 —— 那代表沒事，不是失敗。"""
        fake = _FakeLaunchctl([{LABEL: "6803"}])
        with mock.patch.object(health.os.path, "exists", return_value=True), \
                mock.patch.object(health.subprocess, "run", side_effect=fake):
            msg = health._repair_launchctl_load(LABEL)
        self.assertTrue(msg.startswith("✅"), msg)

    def test_load_repair_reports_failure_when_label_still_missing(self):
        fake = _FakeLaunchctl([{}])       # bootstrap 完還是沒進 list
        with mock.patch.object(health.os.path, "exists", return_value=True), \
                mock.patch.object(health.subprocess, "run", side_effect=fake):
            msg = health._repair_launchctl_load(LABEL)
        self.assertTrue(msg.startswith("❌"), msg)

    def test_restart_repair_sigterms_then_kickstarts_without_unload(self):
        """記憶體超標的重啟：保留 graceful SIGTERM，但全程不動 launchd domain。"""
        fake = _FakeLaunchctl([{LABEL: "6803"}])
        with mock.patch.object(health.subprocess, "run", side_effect=fake), \
                mock.patch.object(health, "_HEALTH_RESTART_SIGTERM_GRACE_SEC", 0), \
                mock.patch.object(health.os, "kill") as fake_kill:
            msg = health._repair_launchctl_restart(LABEL)

        fake_kill.assert_called_once_with(6803, signal.SIGTERM)
        self.assertIn("kickstart", fake.subcommands)
        self.assertNotIn("unload", fake.subcommands)
        self.assertNotIn("load", fake.subcommands)
        self.assertTrue(msg.startswith("✅"), msg)


class TransientMissingLabelTests(unittest.TestCase):
    """部署空窗取樣到的「沒載入」是暫態誤判，重採一次就該消失。"""

    def _issues(self, samples: list[dict]) -> list:
        fake = _FakeLaunchctl(samples)
        with mock.patch.object(health, "_is_macos_edge_host", return_value=True), \
                mock.patch.object(health, "_expected_launchd_labels",
                                  return_value=[LABEL]), \
                mock.patch.object(health, "_HEALTH_LAUNCHD_RECHECK_DELAY_SEC", 0), \
                mock.patch.object(health.subprocess, "run", side_effect=fake):
            return health._check_daemons_health()

    def test_missing_only_in_first_sample_is_not_reported(self):
        """redeploy 的 unload→load 空窗：第一次沒看到，第二次看到了。"""
        issues = self._issues([{}, {LABEL: "6803"}])
        self.assertEqual(
            issues, [],
            "部署空窗的暫態取樣不該報成故障（更不該觸發 auto-repair）",
        )

    def test_missing_in_both_samples_is_still_reported(self):
        """真的沒載入不會因為多等兩秒就長回來 —— 這種必須照報。"""
        issues = self._issues([{}, {}])
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("沒被 launchd 載入", issues[0]["msg"])
        self.assertEqual(issues[0]["repair"], "launchctl_load")


if __name__ == "__main__":
    unittest.main()
