"""bin/redeploy-daemons：排程型 job 的守門 —— 不殺跑到一半的任務、不做無意義重載。

2026-08-29 凌晨事故：rag_sync_daily 01:00 照排程開始夜跑，01:29 全艦隊
`redeploy-daemons --force` 輪到它 —— 有 PID → 走就地路徑 → `kickstart -k` 把跑了
29 分鐘的夜跑 SIGTERM 掉（exit -15）；launchd 又因 ThrottleInterval=1800 拒絕
重生（log 連刷 "cannot spawn: service is throttled"），kickstart CLI 就地阻塞
24 分鐘，整條 redeploy 停在原地直到人工 Ctrl-C。

而這一切毫無必要：排程型 job（plist 有 StartCalendarInterval / StartInterval）
每輪 spawn 都是新 process，「重啟讓它吃新碼」對它們是假需求 —— 下輪自然就是新碼。

這裡釘的不變式：
  1. 排程型 + 跑到一半 → 完全不碰（無論 plist 有沒有變），deferred 記在統計裡。
  2. 排程型 + idle + plist 沒變（--force）→ 不重載。reload 對這類 plist 多半
     隱含 RunAtLoad（KeepAlive.SuccessfulExit）、會誤觸發一輪。
  3. 排程型 + idle + plist 真的變了 → 照舊 unload/load（launchd 必須重讀）。
  4. 常駐 daemon 的 kickstart 有阻塞上限（RED_KICKSTART_TIMEOUT_S）：卡住（throttle）
     時殺掉 CLI 退回 unload/load，redeploy 不再整條掛住。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.path_safety import _REPO_ROOT as REPO_ROOT  # noqa: E402

SCHED_LABEL = "com.xiaohong.rag_sync_daily"   # StartCalendarInterval + KA SuccessfulExit:false
RESIDENT_LABEL = "com.xiaohong.telegram"      # 常駐、無排程鍵


class _RedeployHarness(unittest.TestCase):
    """與 test_redeploy_restart_in_place 同款 stub harness，多一個 kickstart 卡死開關。"""

    label = SCHED_LABEL

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redeploy_sched_")
        self.runtime = os.path.join(self.tmp, "var")
        self.launchagents = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.launchagents, exist_ok=True)
        self.bin = os.path.join(REPO_ROOT, "bin", "redeploy-daemons")
        self.target = os.path.join(self.launchagents, f"{self.label}.plist")

        self.rec = os.path.join(self.tmp, "calls.txt")
        self.pidf = os.path.join(self.tmp, "pid")
        self.hang = os.path.join(self.tmp, "kickstart_hangs")   # 存在 → kickstart 卡住
        self._write_pid("1000")

        self.stub = os.path.join(self.tmp, "launchctl_stub.sh")
        with open(self.stub, "w") as f:
            f.write(
                "#!/bin/bash\n"
                f'echo "$@" >> "{self.rec}"\n'
                'case "$1" in\n'
                "  list)\n"
                f'    printf "\\t%s\\t0\\t{self.label}\\n" "$(cat {self.pidf})"\n'
                "    ;;\n"
                "  kickstart)\n"
                f'    if [ -f "{self.hang}" ]; then sleep 300; fi\n'
                f'    cur=$(cat {self.pidf})\n'
                f'    case "$cur" in [0-9]*) echo $((cur + 1)) > {self.pidf};; esac\n'
                "    ;;\n"
                "  load|bootstrap)\n"
                f'    echo 7777 > {self.pidf}\n'
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            )
        os.chmod(self.stub, 0o755)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_pid(self, pid: str):
        with open(self.pidf, "w") as f:
            f.write(pid + "\n")

    def _install_identical_plist(self):
        tmpl = os.path.join(REPO_ROOT, "launchd", "templates", f"{self.label}.plist")
        with open(tmpl, encoding="utf-8") as f:
            rendered = f.read()
        rendered = rendered.replace("@@REPO_ROOT@@", REPO_ROOT)
        rendered = rendered.replace("@@RED_OFFSITE_DIR@@", "")
        with open(self.target, "w", encoding="utf-8") as f:
            f.write(rendered)

    def _install_stale_plist(self):
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("<plist><dict><key>stale</key></dict></plist>\n")

    def _run(self, *args, extra_env: dict | None = None):
        env = os.environ.copy()
        env["RED_RUNTIME_DIR"] = self.runtime
        env["RED_LAUNCHCTL_BIN"] = self.stub
        env["RED_LAUNCHAGENTS_DIR"] = self.launchagents
        env["RED_SKIP_POST_DEPLOY_SMOKE"] = "1"
        env["RED_OFFSITE_DIR"] = ""
        env["RED_REDEPLOY_VERIFY_SETTLE_S"] = "0"
        env["RED_REDEPLOY_VERIFY_RETRY_DELAY_S"] = "0"
        env.update(extra_env or {})
        return subprocess.run(
            [self.bin, *args], env=env,
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
        )

    def _calls(self) -> list[str]:
        if not os.path.exists(self.rec):
            return []
        with open(self.rec, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def _destructive_calls(self) -> list[str]:
        return [c for c in self._calls()
                if c.startswith(("unload", "kickstart", "load", "bootstrap", "kill "))]


class ScheduledMidRunTests(_RedeployHarness):
    def test_midrun_unchanged_plist_is_left_alone(self):
        """夜跑進行中 + plist 沒變（--force）→ 一根手指都不准碰。"""
        self._install_identical_plist()
        result = self._run("rag_sync_daily", "--force")
        out = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(
            self._destructive_calls(), [],
            f"對跑到一半的排程任務動手了：{self._calls()}",
        )
        self.assertIn("跑到一半", out)
        self.assertIn("不殺", out)
        self.assertIn("deferred", out)

    def test_midrun_changed_plist_is_deferred_with_instructions(self):
        """plist 真的變了但任務跑到一半 → 一樣不動，明講怎麼補。"""
        self._install_stale_plist()
        result = self._run("rag_sync_daily", "--force")
        out = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(self._destructive_calls(), [], self._calls())
        self.assertIn("本輪不動它", out)
        self.assertIn("redeploy-daemons rag_sync_daily", out)


class ScheduledIdleTests(_RedeployHarness):
    def test_idle_unchanged_plist_is_not_reloaded(self):
        """idle + plist 沒變（--force）→ 不重載。

        reload 對這類 plist（KeepAlive.SuccessfulExit）隱含 RunAtLoad、會誤觸發
        一輪夜跑；而新碼本來就會在下輪 spawn 生效，reload 零收益。
        """
        self._install_identical_plist()
        self._write_pid("-")
        result = self._run("rag_sync_daily", "--force")
        out = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(self._destructive_calls(), [], self._calls())
        self.assertIn("下輪 spawn 自然吃新碼", out)
        self.assertIn("sched-skip", out)

    def test_idle_changed_plist_still_reloads(self):
        """plist 真的變了且沒在跑 → launchd 必須重讀，照舊 unload/load。"""
        self._install_stale_plist()
        self._write_pid("-")
        result = self._run("rag_sync_daily", "--force")
        calls = self._calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue([c for c in calls if c.startswith("unload")], calls)
        self.assertTrue([c for c in calls if c.startswith("load")], calls)
        self.assertFalse([c for c in calls if c.startswith("kickstart")], calls)


class KickstartTimeoutTests(_RedeployHarness):
    label = RESIDENT_LABEL

    def test_hung_kickstart_times_out_and_falls_back(self):
        """常駐 daemon 的 kickstart 卡住（launchd throttle）→ 限時放棄、退回老路。

        2026-08-29 實測：kickstart CLI 對 throttled service 會阻塞到窗口過去
        （當晚 24 分鐘）。逾時後必須殺掉 CLI、退回 unload/load，redeploy 才走得完。
        """
        self._install_identical_plist()
        open(self.hang, "w").close()
        result = self._run("telegram", "--force",
                           extra_env={"RED_KICKSTART_TIMEOUT_S": "1"})
        calls = self._calls()
        out = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, out)
        self.assertNotIn("restarted in place", out)
        self.assertIn("kickstart 卡超過", out)
        self.assertTrue([c for c in calls if c.startswith("unload")],
                        f"逾時後沒退回 unload/load：{calls}")
        self.assertTrue([c for c in calls if c.startswith("load")], calls)

    def test_resident_daemon_unchanged_still_restarts_in_place(self):
        """回歸：常駐 daemon（無排程鍵）不受守門影響，就地重啟照舊。"""
        self._install_identical_plist()
        result = self._run("telegram", "--force")
        calls = self._calls()
        out = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, out)
        self.assertIn(f"kickstart -k gui/{os.getuid()}/{RESIDENT_LABEL}", calls)
        self.assertFalse([c for c in calls if c.startswith("unload")], calls)
        self.assertIn("restarted in place", out)


if __name__ == "__main__":
    unittest.main()
