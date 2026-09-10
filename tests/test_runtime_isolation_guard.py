"""測試絕不能讀寫部署本體的 live var/ —— 守門在 logging_and_paths。

這個 repo 同時就是部署本體，`~/RED/var/` 裝的是真的 runtime 狀態（稽核軌跡、成本
帳、session registry、告警用的 tick 時間軸……）。隔離靠 `tests/__init__.py` 搶在
agent_core 之前把 `RED_RUNTIME_DIR` 導向 tmp dir，但 `unittest discover -s tests`
少了 `-t .` 時 tests package 根本不在載入路徑上，重導靜默落空。2026-08-17 普查主
checkout 量到 rag_access_audit ≥43%、policy_decisions 8.6% 是測試殘留，橫跨三個月。

守門因此放在 `agent_core.logging_and_paths`（算 RUNTIME_ROOT 的那一行旁邊）：偵測到
「在測試 runner 底下、又沒人顯式指定 runtime 根」就 os._exit。這裡釘三個方向 ——
該炸的要炸、正常入口要安靜、逃生門要留著。

⚠️ 兩個踩過的雷，改這支測試前先看：
  1. 子程序一定要把 `RED_RUNTIME_DIR` 從環境清掉。父程序（跑這支測試的這一輪）
     早就設好了，直接繼承會讓守門的第一個 early-return 就走掉，三個測試全變假綠。
  2. 守門用 `os._exit` 不是 raise —— 因為 unittest 的 loader 會把匯入期例外包成
     一筆 `_FailedTest` 然後**照跑完全套**（污染照樣發生）。所以斷言要看 returncode
     與 stderr，不要期待 traceback。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _run(argv: list[str], scrub_runtime_dir: bool = True,
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if scrub_runtime_dir:
        env.pop("RED_RUNTIME_DIR", None)          # 見 module docstring 的 ⚠️ 1
    env["AGENT_DAEMON_MODE"] = "1"
    env.update(extra_env or {})
    return subprocess.run([sys.executable, *argv], cwd=_REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=300)


class RuntimeIsolationGuardTests(unittest.TestCase):

    def test_bare_discover_is_killed_before_any_test_runs(self):
        """真正的犯案現場：`discover -s tests` 少了 `-t .`。

        用 -k 挑一支最輕的測試，重點不是它跑不跑得完，是**整個 process 應該在
        任何測試執行前就被停掉**。
        """
        proc = _run(["-m", "unittest", "discover", "-s", "tests", "-q",
                     "-k", "test_arrow_up"])
        self.assertEqual(proc.returncode, 1, "隔離失效卻沒被擋 = 靜默污染 live var/")
        self.assertIn("測試 runtime 隔離失效", proc.stderr)
        self.assertIn("-t .", proc.stderr)                  # 訊息要能直接照做
        self.assertNotIn("Ran ", proc.stderr, "不該有任何測試被執行")

    def test_official_entrypoint_still_works(self):
        """`discover -t .`（＝ make test-quiet / CI 用的形式）不受影響。"""
        proc = _run(["-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q",
                     "-k", "test_arrow_up"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertNotIn("隔離失效", proc.stderr)

    def test_package_form_single_module_still_works(self):
        """`python -m unittest tests.test_x`（CLAUDE.md 記的單測跑法）不受影響。"""
        proc = _run(["-m", "unittest", "tests.test_awake_clock", "-q"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertNotIn("隔離失效", proc.stderr)

    def test_explicit_runtime_dir_is_the_escape_hatch(self):
        """顯式設 RED_RUNTIME_DIR 就放行（test_deploy_lock 等子程序靠它）。"""
        proc = _run(["-m", "unittest", "discover", "-s", "tests", "-q",
                     "-k", "test_arrow_up"],
                    extra_env={"RED_RUNTIME_DIR": "/tmp/red-explicit-runtime-test"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertNotIn("隔離失效", proc.stderr)

    def test_production_import_is_untouched(self):
        """守門只在測試 runner 底下作用 —— daemon / 腳本直接 import 不能受影響。

        這條是這個 PR 的主要風險面：logging_and_paths 是所有 daemon 的必經之路，
        誤判就是整個艦隊起不來。
        """
        proc = _run(["-c",
                     "from agent_core.logging_and_paths import RUNTIME_ROOT\n"
                     "assert RUNTIME_ROOT.endswith('/var'), RUNTIME_ROOT\n"
                     "print('OK')\n"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertIn("OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
