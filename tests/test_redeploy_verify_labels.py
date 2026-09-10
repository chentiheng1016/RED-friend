"""bin/redeploy-daemons 收尾驗證：重部署過的 label 真的還在 launchctl list。

2026-08-18 事故（紅 bot 靜默掛掉近 3 分鐘）：

  17:42:35  redeploy 的 launchctl unload 把 com.xiaohong.telegram 移出 domain
  17:42:35  health_check 依 30 分鐘 StartInterval 醒來，正好在那一秒取樣
            launchctl list → 誤判「daemon 沒被 launchd 載入」
  17:42:36  redeploy 的 load 把它拉起來（新 PID 6803）
  17:42:37  redeploy 隔 1 秒驗到 label 在 list 裡 → 印「↻ reloaded」→ exit 0
  17:42:37  health_check 的 auto-repair 照著誤判送 launchctl unload
            → 剛起來 1.6 秒的新程序被 SIGTERM
  17:42:42  launchd 5 秒寬限期到 → SIGKILL → "removing service"
            → label 從 launchd 整個消失，而 redeploy 早就 exit 0 了

關鍵在時間差：被 bootout 的 job 不會立刻從 `launchctl list` 消失，而是停在
SIGTERMed 狀態直到寬限期到。所以「load 完馬上驗一次」必然驗到一個已經被判
死刑的 job 是健康的 —— 整條失敗路徑完全靜默，只因為 post-merge hook 剛好跑
了 red-smoke 才被抓到。

這裡釘的不變式：整批部署完、等 launchd 沉澱之後要再驗一次；不在 list 裡就
bootstrap 救回（有次數上限），救不回就 exit 非 0 並指名是哪個 label。
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


class RedeployVerifyLabelsTests(unittest.TestCase):
    """跑真的 bash entrypoint，launchctl 換成 stub，不碰活的 fleet。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redeploy_verify_")
        self.runtime = os.path.join(self.tmp, "var")
        self.launchagents = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.launchagents, exist_ok=True)
        self.bin = os.path.join(REPO_ROOT, "bin", "redeploy-daemons")

        self.rec = os.path.join(self.tmp, "calls.txt")
        self.state = os.path.join(self.tmp, "state")       # loaded | gone
        self.arm = os.path.join(self.tmp, "arm_bootout")   # 存在＝第一次 list 後被 bootout
        self.heal = os.path.join(self.tmp, "heal")         # 存在＝bootstrap 救得回來
        with open(self.state, "w") as f:
            f.write("loaded\n")

        # Stub launchctl。所有狀態變更都排在 printf 之前：`grep -q` 一命中就
        # 關管線，之後的寫入可能收到 SIGPIPE，順序寫反會讓測試變成擲骰子。
        self.stub = os.path.join(self.tmp, "launchctl_stub.sh")
        with open(self.stub, "w") as f:
            f.write(
                "#!/bin/bash\n"
                f'echo "$@" >> "{self.rec}"\n'
                'case "$1" in\n'
                "  list)\n"
                f'    cur=$(cat "{self.state}" 2>/dev/null)\n'
                # 第一次 list ＝ redeploy load 完那次即時驗證。模擬「驗完之後
                # 才被別人 bootout」——正是 health_check auto-repair 幹的事。
                f'    if [ -f "{self.arm}" ]; then rm -f "{self.arm}"; echo gone > "{self.state}"; fi\n'
                '    if [ "$cur" = "loaded" ]; then\n'
                f'      printf "\\t12345\\t0\\t{LABEL}\\n"\n'
                "    fi\n"
                "    ;;\n"
                "  bootstrap)\n"
                f'    if [ -f "{self.heal}" ]; then echo loaded > "{self.state}"; fi\n'
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            )
        os.chmod(self.stub, 0o755)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _arm_bootout(self, *, heals: bool):
        """讓 label 在 redeploy 驗過之後從 launchd 消失。"""
        open(self.arm, "w").close()
        if heals:
            open(self.heal, "w").close()

    def _run(self, *args):
        env = os.environ.copy()
        env["RED_RUNTIME_DIR"] = self.runtime
        env["RED_LAUNCHCTL_BIN"] = self.stub
        env["RED_LAUNCHAGENTS_DIR"] = self.launchagents
        env["RED_SKIP_POST_DEPLOY_SMOKE"] = "1"
        # 驗證邏輯本身照跑，只是不真的等 launchd 沉澱。
        env["RED_REDEPLOY_VERIFY_SETTLE_S"] = "0"
        env["RED_REDEPLOY_VERIFY_RETRY_DELAY_S"] = "0"
        env["RED_REDEPLOY_VERIFY_RETRIES"] = "2"
        return subprocess.run(
            [self.bin, *args], env=env,
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

    def _calls(self) -> list[str]:
        if not os.path.exists(self.rec):
            return []
        with open(self.rec, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]

    def test_label_still_loaded_needs_no_rescue(self):
        """正常重部署：驗過就好，不該多打 bootstrap。"""
        result = self._run("telegram")
        self.assertEqual(result.returncode, 0,
                         f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertIn("收尾驗證", result.stdout)
        self.assertNotIn(
            "bootstrap", " ".join(self._calls()),
            "label 一直都在，不該觸發救援 bootstrap",
        )

    def test_bootout_after_deploy_is_caught_and_rebootstrapped(self):
        """部署後被別人 bootout → 收尾驗證要抓到並救回來（不能靜默）。"""
        self._arm_bootout(heals=True)
        result = self._run("telegram")
        out = result.stdout + result.stderr

        self.assertIn("bootstrap", " ".join(self._calls()),
                      "label 消失了卻沒重試 bootstrap")
        self.assertIn(LABEL, out)
        self.assertIn("救回", out, f"沒印出救援結果：\n{out}")
        self.assertEqual(result.returncode, 0,
                         f"救回來了就該 exit 0：\n{out}")

    def test_label_gone_for_good_exits_nonzero_naming_the_label(self):
        """救不回來 → exit 非 0，而且要講清楚是哪個 label（別再靜默）。"""
        self._arm_bootout(heals=False)
        result = self._run("telegram")
        out = result.stdout + result.stderr

        self.assertEqual(result.returncode, 1,
                         f"label 沒救回來卻 exit 0：\n{out}")
        self.assertIn(LABEL, out, "失敗訊息沒指名是哪個 label")
        bootstraps = [c for c in self._calls() if c.startswith("bootstrap")]
        self.assertEqual(
            len(bootstraps), 2,
            f"重試次數要照 RED_REDEPLOY_VERIFY_RETRIES 收斂，實際 {bootstraps}",
        )


if __name__ == "__main__":
    unittest.main()
