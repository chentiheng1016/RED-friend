"""daemon_helpers._TimestampStream / install_stdout_timestamps — daemon log 行首時間戳。

為什麼存在：launchd 把 daemon 的 print 直接導進 var/logs/daemon-<name>.log，行內
沒有任何時間資訊。2026-08-29 的除錯實測連續三次被絆倒：四月的化石錯誤被當活訊號
追、「OAuth 噪音是否在修復部署後」無法從 log 判定、日期切片掃描器對大半 log 全盲。

合約（測試釘住的）：
  1. 只在邏輯行行首加 `[YYYY-MM-DD HH:MM:SS] `；跨多次 write 的 partial line
     不會被切斷；空白行不加。
  2. install 的三道閘：RED_LOG_TIMESTAMPS=0 停用；已被 redirect 的 stream 不碰
     （redirect_stdout 之後呼叫不會污染被斷言的 buffer）；tty 不包（人手動跑
     保持乾淨）。
  3. rotate_log（16 個 daemon 入口的共同開場白）會自動安裝。
  4. fileno 透傳原始 fd —— subprocess(stdout=sys.stdout) 的子程序輸出不被包。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.daemon_helpers import _TimestampStream, install_stdout_timestamps  # noqa: E402

_STAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


class TimestampStreamUnitTests(unittest.TestCase):
    def setUp(self):
        self.buf = io.StringIO()
        self.ts = _TimestampStream(self.buf)

    def _lines(self):
        return self.buf.getvalue().split("\n")

    def test_each_logical_line_gets_one_stamp(self):
        self.ts.write("hello\nworld\n")
        lines = self._lines()
        self.assertRegex(lines[0], _STAMP_RE)
        self.assertTrue(lines[0].endswith("hello"))
        self.assertRegex(lines[1], _STAMP_RE)
        self.assertTrue(lines[1].endswith("world"))

    def test_partial_writes_do_not_split_a_line(self):
        """print() 會拆成 write("body") + write("\\n")；一行只准一個時間戳。"""
        self.ts.write("progress ")
        self.ts.write("42/100")
        self.ts.write("\n")
        line = self._lines()[0]
        self.assertRegex(line, _STAMP_RE)
        self.assertTrue(line.endswith("progress 42/100"))
        self.assertEqual(line.count("["), 1, line)

    def test_blank_lines_stay_blank(self):
        self.ts.write("a\n\nb\n")
        lines = self._lines()
        self.assertEqual(lines[1], "", lines)

    def test_write_returns_input_length(self):
        self.assertEqual(self.ts.write("abc\n"), 4)
        self.assertEqual(self.ts.write(""), 0)

    def test_delegation_contract(self):
        """fileno/encoding/errors 透傳 —— subprocess(stdout=sys.stdout) 要拿得到原始 fd。"""
        class _Fake:
            encoding = "utf-8"
            errors = "strict"
            def write(self, s): pass
            def fileno(self): return 42
            def isatty(self): return False
        ts = _TimestampStream(_Fake())
        self.assertEqual(ts.fileno(), 42)
        self.assertEqual(ts.encoding, "utf-8")
        self.assertFalse(ts.isatty())
        self.assertTrue(ts.writable())
        self.assertFalse(ts.readable())


class InstallGateTests(unittest.TestCase):
    def test_redirected_stdout_is_left_alone(self):
        """redirect_stdout / StringIO 之後呼叫 install：不碰 —— 否則測試斷言的
        buffer 會被塞進時間戳。"""
        orig = sys.stdout
        try:
            sys.stdout = io.StringIO()
            install_stdout_timestamps()
            self.assertNotIsInstance(sys.stdout, _TimestampStream)
        finally:
            sys.stdout = orig

    def test_env_kill_switch(self):
        orig = sys.stdout
        try:
            sys.stdout = io.StringIO()
            os.environ["RED_LOG_TIMESTAMPS"] = "0"
            self.assertFalse(install_stdout_timestamps())
        finally:
            os.environ.pop("RED_LOG_TIMESTAMPS", None)
            sys.stdout = orig


def _run_snippet(code: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """在乾淨的子直譯器裡跑（stdout=PIPE ⇒ 不是 tty ⇒ install 會生效）。"""
    env = os.environ.copy()
    env["RED_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ts_test_var_")
    env.pop("RED_LOG_TIMESTAMPS", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", code], env=env,
        capture_output=True, text=True, cwd=_REPO_ROOT, timeout=60,
    )


class SubprocessEndToEndTests(unittest.TestCase):
    def test_install_stamps_stdout_and_stderr(self):
        r = _run_snippet(
            "from agent_core.daemon_helpers import install_stdout_timestamps as i;"
            "import sys; i(); print('to-out'); print('to-err', file=sys.stderr)"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.splitlines()[0], _STAMP_RE)
        self.assertIn("to-out", r.stdout)
        err_lines = [ln for ln in r.stderr.splitlines() if "to-err" in ln]
        self.assertTrue(err_lines and _STAMP_RE.match(err_lines[0]), r.stderr)

    def test_kill_switch_in_subprocess(self):
        r = _run_snippet(
            "from agent_core.daemon_helpers import install_stdout_timestamps as i;"
            "i(); print('plain')",
            extra_env={"RED_LOG_TIMESTAMPS": "0"},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("plain"), repr(r.stdout))

    def test_rotate_log_installs_automatically(self):
        """rotate_log 是 16 個 daemon 入口的共同開場白 —— 呼叫它就要長出時間戳，
        連 log 檔還不存在（早 return）的那條路也一樣。"""
        r = _run_snippet(
            "from agent_core.daemon_helpers import rotate_log;"
            "rotate_log('ts_test_nonexistent'); print('after-rotate')"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.splitlines()[0], _STAMP_RE)
        self.assertIn("after-rotate", r.stdout)

    def test_double_install_single_stamp(self):
        r = _run_snippet(
            "from agent_core.daemon_helpers import install_stdout_timestamps as i;"
            "i(); i(); print('once')"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        line = r.stdout.splitlines()[0]
        self.assertEqual(line.count("["), 1, line)


if __name__ == "__main__":
    unittest.main()
