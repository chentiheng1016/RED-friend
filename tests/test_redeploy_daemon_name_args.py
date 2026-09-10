"""bin/redeploy-daemons 的 daemon 名稱參數：多個名字要全部生效。

2026-08-26 實際踩到：argparse 的 `*)` 分支是 `ONLY_DAEMON="$arg"` 純量，多給
幾個名字時後面的覆蓋前面的，只剩**最後一個**生效，而且完全不吭聲。那次一條
13 個名字的 redeploy 只重啟了最後那個 `tool_rpc`；`web_server` 等 12 個原地不動
繼續跑合併前的碼，操作者卻以為整批都部署完了。唯一的線索是標題那行
「Redeploy 1 daemon plist(s)」—— 而輸出尾巴是 smoke 的 7/7 PASS，看起來一切正常。

（自動路徑沒中招：`scripts/post_merge_redeploy.py` 是一個名字呼叫一次。中招的
只有手打 CLI 的人。）

這裡釘的不變式：
  * 給 N 個名字就處理 N 個，順序照 template 目錄。
  * 名字打錯 → **整輪拒絕**，不「對的照部署、錯的靜默跳過」。
  * 不給名字 → 全部（原行為不變）。
  * 解析結果要印出來，讓操作者一眼看得出名字有沒有被收到。

用 --dry-run 跑：不碰 launchd、不寫 LaunchAgents，純驗參數解析與選檔。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.path_safety import _REPO_ROOT as REPO_ROOT  # noqa: E402

# 腳本輸出帶顏色，比對前先剝掉 ANSI escape。
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class RedeployDaemonNameArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redeploy_names_")
        self.bin = os.path.join(REPO_ROOT, "bin", "redeploy-daemons")
        self.launchagents = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.launchagents, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        env = os.environ.copy()
        env["RED_RUNTIME_DIR"] = os.path.join(self.tmp, "var")
        env["RED_LAUNCHAGENTS_DIR"] = self.launchagents
        env["RED_SKIP_POST_DEPLOY_SMOKE"] = "1"
        env["RED_OFFSITE_DIR"] = ""
        return subprocess.run(
            [self.bin, *args], env=env,
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

    def _handled(self, out: str) -> list[str]:
        """輸出裡每個被處理到的 daemon short name（🔄 有差異 / 🔥 force / ✅ 沒變）。"""
        names = []
        for line in out.splitlines():
            stripped = _ANSI.sub("", line).strip()
            for marker in ("🔄", "🔥", "✅"):
                if stripped.startswith(marker):
                    rest = stripped[len(marker):].strip()
                    names.append(rest.split()[0])
                    break
        return names

    # ── 多名字 ────────────────────────────────────────────────
    def test_every_name_is_processed_not_just_the_last(self):
        """這是本案的核心：4 個名字進去要出來 4 個，不是只剩最後一個。"""
        result = self._run("telegram", "telegram_black", "web_server",
                           "tool_rpc", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        handled = self._handled(result.stdout)
        self.assertEqual(sorted(handled),
                         sorted(["telegram", "telegram_black", "web_server", "tool_rpc"]),
                         f"少處理了 daemon —— 這正是 web_server 跑舊碼的原因：{handled}")
        self.assertIn("Redeploy 4 daemon plist(s)", result.stdout)

    def test_resolved_names_are_echoed_back(self):
        """解析結果要印出來 —— 上次唯一的線索是標題那個數字，太容易漏看。"""
        result = self._run("telegram", "web_server", "--dry-run")
        self.assertIn("指定：", result.stdout)
        self.assertIn("telegram", result.stdout)
        self.assertIn("web_server", result.stdout)

    def test_single_name_still_works(self):
        result = self._run("tool_rpc", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._handled(result.stdout), ["tool_rpc"])

    def test_flag_before_names_is_also_accepted(self):
        result = self._run("--dry-run", "telegram", "web_server")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(self._handled(result.stdout)),
                         ["telegram", "web_server"])

    # ── 打錯名字 → 整輪拒絕 ────────────────────────────────────
    def test_unknown_name_rejects_the_whole_run(self):
        """一個名字打錯，其他正確的也不准部署。

        「對的照做、錯的跳過」在部署工具上是最貴的失效模式：你以為 N 個都上了，
        其實少一個還在跑舊碼，而且不會有人告訴你。
        """
        result = self._run("telegram", "nosuchdaemon", "web_server", "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nosuchdaemon", result.stderr)
        combined = result.stdout + result.stderr
        self.assertNotIn("Redeploy", result.stdout,
                         f"打錯名字卻還是開始部署了：{combined}")

    def test_unknown_name_lists_the_available_ones(self):
        result = self._run("nosuchdaemon", "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tool_rpc", result.stderr)

    def test_all_unknown_names_are_reported_not_just_the_first(self):
        result = self._run("nope_one", "nope_two", "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nope_one", result.stderr)
        self.assertIn("nope_two", result.stderr)

    # ── 不給名字 → 全部（原行為） ──────────────────────────────
    def test_no_names_still_means_every_daemon(self):
        templates = [
            f for f in os.listdir(os.path.join(REPO_ROOT, "launchd", "templates"))
            if f.startswith("com.xiaohong.") and f.endswith(".plist")
        ]
        result = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Redeploy {len(templates)} daemon plist(s)", result.stdout)
        self.assertNotIn("指定：", result.stdout)


if __name__ == "__main__":
    unittest.main()
