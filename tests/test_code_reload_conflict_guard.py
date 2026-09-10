"""熱重載 self-exec 衝突標記 guard（fix: 並行 merge 半成品撞熱重載 → crash-loop）。

2026-06-15：另一個 session 在共用部署分支 `git merge` 時，衝突檔短暫帶
`<<<<<<< HEAD` 標記；telegram 熱重載偵測到 mtime 變動就 self-exec 進這棵壞樹，
import SyntaxError → launchd 重啟 → 再 exec，10 色艦隊 crash-loop 約 2 分鐘。
修法：self-exec 前先掃衝突標記，有就暫緩、續跑舊碼，待樹收斂再載入。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from agent_core import daemon_telegram as dt  # noqa: E402

_CONFLICTED = (
    "def f(x):\n"
    "    if x:\n"
    "<<<<<<< HEAD\n"
    "        return 1\n"
    "=======\n"
    "        return 2\n"
    ">>>>>>> origin/main\n"
)


class ConflictMarkerDetectionTests(unittest.TestCase):
    def test_detects_only_conflicted_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "good.py"), "w") as f:
                f.write("x = 1\ny = '=' * 7  # not a marker\n")
            with open(os.path.join(d, "bad.py"), "w") as f:
                f.write(_CONFLICTED)
            found = dt._conflict_marked_reload_files([d])
        self.assertEqual([os.path.basename(p) for p in found], ["bad.py"])

    def test_clean_tree_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w") as f:
                f.write("import os\n\n\ndef g():\n    return os.getpid()\n")
            self.assertEqual(dt._conflict_marked_reload_files([d]), [])

    def test_diff3_base_marker_detected(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "c.py"), "w") as f:
                f.write("a=1\n<<<<<<< ours\nb=2\n||||||| base\nb=0\n=======\nb=3\n>>>>>>> theirs\n")
            found = dt._conflict_marked_reload_files([d])
        self.assertEqual([os.path.basename(p) for p in found], ["c.py"])


class ExecGuardTests(unittest.TestCase):
    def test_skips_execv_when_conflict_present(self):
        with mock.patch.object(dt, "_conflict_marked_reload_files",
                               return_value=["agent_core/agents/white_legal/specs.py"]), \
             mock.patch("os.execv") as execv:
            dt._exec_self_for_code_reload()
        execv.assert_not_called()

    def test_execv_proceeds_when_tree_clean(self):
        with mock.patch.object(dt, "_conflict_marked_reload_files", return_value=[]), \
             mock.patch("os.execv") as execv:
            dt._exec_self_for_code_reload()
        execv.assert_called_once()


if __name__ == "__main__":
    unittest.main()
