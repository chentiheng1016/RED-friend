"""bin/red-preflight —— 開工前檢查的行為契約。

這支腳本存在的理由是「別再靠記得」：一個晚上撞到三次別的 session 正在部署、
一次沒 fetch 就跟人重工。所以它的兩個關鍵行為必須釘死：

  1. 部署鎖被持有時要**明確擋下**（exit 3，與 deploy_lock.BUSY_EXIT_CODE 同碼），
     而且判斷要走 flock 而不是 ps 字串比對。
  2. 不能自己製造誤報 —— 第一版把 `tail -f daemon-erp_mirror_refresh.log`
     這種看 log 的視窗算成「長跑任務」，正是這支腳本要消滅的東西。

鎖的路徑透過 RED_RUNTIME_DIR 導到 tmp，所以測試不會碰到 live 的 deploy.lock
（碰了會讓真正的部署 fail-fast）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SCRIPT = os.path.join(_REPO_ROOT, "bin", "red-preflight")


def _run(runtime_dir: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["RED_RUNTIME_DIR"] = runtime_dir
    return subprocess.run(
        [_SCRIPT, "--no-fetch", *args],
        capture_output=True, text=True, env=env, timeout=180,
    )


@unittest.skipUnless(os.path.exists(_SCRIPT), "bin/red-preflight 不存在")
class RedPreflightTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime = self._tmp.name
        self.run_dir = os.path.join(self.runtime, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.lock_path = os.path.join(self.run_dir, "deploy.lock")

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK), "bin/red-preflight 要可執行")

    def test_clear_state_exits_zero_and_prints_all_sections(self):
        out = _run(self.runtime)
        self.assertEqual(out.returncode, 0, out.stderr)
        for marker in ("位置", "部署互斥鎖", "版本落差", "長跑任務", "時間窗"):
            self.assertIn(marker, out.stdout)
        self.assertIn("可以動", out.stdout)

    def test_held_lock_blocks_with_exit_3(self):
        """鎖被持有 = 有人在部署 → 明確擋下，並報出 holder pid。"""
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent("""
                import fcntl, os, sys, time
                fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, str(os.getpid()).encode())
                os.fsync(fd)
                sys.stdout.write("locked\\n")
                sys.stdout.flush()
                time.sleep(60)
            """), self.lock_path],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        # 等持有者確認拿到鎖，避免比賽條件（別用 sleep 賭時間）
        self.assertEqual(holder.stdout.readline().strip(), "locked")

        out = _run(self.runtime)
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("有人正在部署", out.stdout)
        self.assertIn(str(holder.pid), out.stdout)
        self.assertIn("先等部署跑完", out.stdout)

    def test_released_lock_does_not_block(self):
        """鎖檔存在但沒人持有（部署跑完後的常態）→ 不能誤判成忙碌。"""
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write("99999\n")          # 舊的 holder pid，早就結束了
        out = _run(self.runtime)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("沒有部署在進行", out.stdout)

    def test_log_watcher_is_not_reported_as_a_long_running_job(self):
        """`tail -f daemon-erp_mirror_refresh.log` 是在看 log，不是在跑跑批。

        第一版只比對關鍵字，這種視窗會被算成長跑任務 → 假警報。
        """
        log_path = os.path.join(self.runtime, "daemon-erp_mirror_refresh.log")
        open(log_path, "w").close()
        watcher = subprocess.Popen(["tail", "-f", log_path],
                                   stdout=subprocess.DEVNULL)
        self.addCleanup(watcher.wait)
        self.addCleanup(watcher.kill)
        time.sleep(0.3)                 # 讓 tail 真的出現在 process table

        out = _run(self.runtime)
        self.assertNotIn("daemon-erp_mirror_refresh.log", out.stdout)

    def test_unknown_flag_is_rejected(self):
        out = _run(self.runtime, "--bogus")
        self.assertEqual(out.returncode, 2)
        self.assertIn("未知參數", out.stderr)

    def test_help_exits_zero(self):
        env = dict(os.environ)
        env["RED_RUNTIME_DIR"] = self.runtime
        out = subprocess.run([_SCRIPT, "--help"], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0)
        self.assertIn("開工前檢查", out.stdout)


if __name__ == "__main__":
    unittest.main()
