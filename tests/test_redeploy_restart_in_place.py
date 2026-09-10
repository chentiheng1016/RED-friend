"""bin/redeploy-daemons：plist 沒變就別動 launchd domain。

2026-08-18 紅 bot 事故的殘留風險。改完 Python 要讓常駐 daemon 吃到新碼時，
plist 其實一個字都沒變，但 redeploy 仍走 unload → sleep 1 → load —— 中間有約
1.03 秒 label 完全不在 `launchctl list` 裡（主 checkout 實測：unload 落在
T0+0.1~0.5s、load 落在 T0+1.1~1.6s）。那一秒就是誤判的溫床：health_check 在那
一秒取樣就認定「daemon 沒被載入」，8/18 它照著誤判動手把剛起來的 bot 殺掉。

#430 讓觀察者不再有殺傷力（修復動作改非破壞性），這裡把空窗本身消滅：plist 沒
變且 daemon 正在跑 → `launchctl kill TERM` → 寬限 → `kickstart -k`，label 全程
留在 domain 裡。

launchd 語意實測（拋棄式 probe job，KeepAlive={SuccessfulExit:false,Crashed:true}
＋RunAtLoad，與 telegram 系同款）：
  * 乾淨退出後 label **留在** list 裡變成 `pid=-`，不會被移出 domain
    —— 這正是「就地」成立的前提；
  * `kickstart -k` 對 idle-but-loaded 的 job 回 rc=0 並給出新 PID；
  * ⚠️ `-k` 的 man page 寫「Kill the running instance」，**實際是先 SIGTERM、
    給 5 秒才 SIGKILL**（probe 的 TERM trap 有跑、乾淨 exit 0）。所以不需要、
    也不該在前面自己補一次 SIGTERM：#434 那樣寫過，實測那個 SIGTERM 根本沒
    遞送，只是讓每個 daemon 白等寬限秒數。

這裡釘的不變式：plist 沒變 → 不准 unload；plist 變了或 daemon 沒在跑 → 照舊走
unload/load（launchd 必須重讀 plist）。
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

LABEL = "com.xiaohong.telegram"


class RedeployRestartInPlaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redeploy_inplace_")
        self.runtime = os.path.join(self.tmp, "var")
        self.launchagents = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.launchagents, exist_ok=True)
        self.bin = os.path.join(REPO_ROOT, "bin", "redeploy-daemons")
        self.target = os.path.join(self.launchagents, f"{LABEL}.plist")

        self.rec = os.path.join(self.tmp, "calls.txt")
        self.pidf = os.path.join(self.tmp, "pid")
        self.bump = os.path.join(self.tmp, "kickstart_bumps_pid")
        self._write_pid("1000")
        open(self.bump, "w").close()   # 預設：kickstart 真的換掉 PID

        self.stub = os.path.join(self.tmp, "launchctl_stub.sh")
        with open(self.stub, "w") as f:
            f.write(
                "#!/bin/bash\n"
                f'echo "$@" >> "{self.rec}"\n'
                'case "$1" in\n'
                "  list)\n"
                f'    printf "\\t%s\\t0\\t{LABEL}\\n" "$(cat {self.pidf})"\n'
                "    ;;\n"
                "  kickstart)\n"
                f'    if [ -f "{self.bump}" ]; then\n'
                f'      cur=$(cat {self.pidf})\n'
                f'      case "$cur" in [0-9]*) echo $((cur + 1)) > {self.pidf};; esac\n'
                "    fi\n"
                "    ;;\n"
                "  load|bootstrap)\n"
                # 老路載入成功 → daemon 有 PID 了
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
        """把 template 照腳本的 sed 規則展開後放到 target，製造「plist 沒變」。"""
        tmpl = os.path.join(REPO_ROOT, "launchd", "templates", f"{LABEL}.plist")
        with open(tmpl, encoding="utf-8") as f:
            rendered = f.read()
        rendered = rendered.replace("@@REPO_ROOT@@", REPO_ROOT)
        rendered = rendered.replace("@@RED_OFFSITE_DIR@@", "")
        with open(self.target, "w", encoding="utf-8") as f:
            f.write(rendered)

    def _run(self, *args):
        env = os.environ.copy()
        env["RED_RUNTIME_DIR"] = self.runtime
        env["RED_LAUNCHCTL_BIN"] = self.stub
        env["RED_LAUNCHAGENTS_DIR"] = self.launchagents
        env["RED_SKIP_POST_DEPLOY_SMOKE"] = "1"
        env["RED_OFFSITE_DIR"] = ""          # 讓 render 結果可預測
        env["RED_REDEPLOY_VERIFY_SETTLE_S"] = "0"
        env["RED_REDEPLOY_VERIFY_RETRY_DELAY_S"] = "0"
        return subprocess.run(
            [self.bin, *args], env=env,
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

    def _calls(self) -> list[str]:
        if not os.path.exists(self.rec):
            return []
        with open(self.rec, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def test_unchanged_plist_restarts_in_place_without_unloading(self):
        """常駐 daemon + plist 沒變 → 絕不 unload，label 全程留在 domain。"""
        self._install_identical_plist()
        result = self._run("telegram", "--force")
        calls = self._calls()
        out = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, out)
        self.assertFalse(
            [c for c in calls if c.startswith("unload")],
            f"plist 沒變卻還是 unload 了，空窗又回來了：{calls}",
        )
        self.assertIn(f"kickstart -k gui/{os.getuid()}/{LABEL}", calls,
                      "KeepAlive.SuccessfulExit=false 下沒 kickstart 就起不來")
        self.assertFalse(
            [c for c in calls if c.startswith("kill ")],
            "kickstart -k 自己就是 SIGTERM + 5 秒寬限，前面再補一次 SIGTERM 是白等"
            f"（而且先斬後奏：kickstart 失敗時 daemon 已經被殺了）：{calls}",
        )
        self.assertIn("restarted in place", out)

    def test_idle_daemon_falls_back_to_unload_load(self):
        """沒在跑的 daemon 沒有「就地」可言 —— 照舊走 unload/load。"""
        self._install_identical_plist()
        self._write_pid("-")
        result = self._run("telegram", "--force")
        calls = self._calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue([c for c in calls if c.startswith("unload")], calls)
        self.assertTrue([c for c in calls if c.startswith("load")], calls)
        self.assertFalse([c for c in calls if c.startswith("kickstart")], calls)

    def test_changed_plist_still_uses_unload_load(self):
        """plist 真的變了 → launchd 必須重讀，就地重啟不夠。"""
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("<plist><dict><key>stale</key></dict></plist>\n")
        result = self._run("telegram", "--force")
        calls = self._calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue([c for c in calls if c.startswith("unload")],
                        f"plist 有差異卻沒重讀：{calls}")
        self.assertFalse([c for c in calls if c.startswith("kickstart -k")], calls)

    def test_falls_back_when_pid_did_not_change(self):
        """kickstart 後 PID 沒換 = 沒真的重啟 —— 不准當成功，要退回老路。"""
        self._install_identical_plist()
        os.remove(self.bump)          # kickstart 不換 PID
        result = self._run("telegram", "--force")
        calls = self._calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            [c for c in calls if c.startswith("unload")],
            f"就地重啟沒生效卻沒退回 unload/load：{calls}",
        )
        self.assertNotIn("restarted in place", result.stdout)


if __name__ == "__main__":
    unittest.main()
