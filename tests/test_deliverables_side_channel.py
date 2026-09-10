"""排程附件的側通道 —— LLM 漏抄 [[MAIL_FILE:]] 標記時仍寄得出附件。

原本附件**只**靠標記傳遞：工具把 `[[MAIL_FILE:路徑]]` 放進回傳字串，指望 LLM 把
那行原樣抄進最終回覆。實戰 8/8 都抄對了，但機制上是脆的 —— 模型哪次沒抄，信照樣
寄出、只是沒有附件，沒有例外、沒有錯誤、收件人也不知道原本該有附件。

本檔守三條：
  1. 標記漏抄時，側通道要補得上（這就是整件事的目的）
  2. 側通道**不能**繞過路徑白名單（有一條路沒被檢查＝沒有檢查）
  3. 每支任務開跑前要清空，否則前一支的檔會夾進下一封信 —— 那是把 A 的附件
     寄給 B，比沒附件更糟
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        from agent_core import deliverables
        self.d = deliverables
        self.d.drain()          # 每條測試從乾淨狀態開始

    tearDown = setUp

    def test_register_then_drain(self):
        self.d.register("/x/a.xlsx")
        self.assertEqual(self.d.drain(), ["/x/a.xlsx"])

    def test_drain_empties(self):
        self.d.register("/x/a.xlsx")
        self.d.drain()
        self.assertEqual(self.d.drain(), [])

    def test_duplicates_collapse(self):
        self.d.register("/x/a.xlsx")
        self.d.register("/x/a.xlsx")
        self.assertEqual(self.d.drain(), ["/x/a.xlsx"])

    def test_blank_ignored(self):
        for junk in ("", "   ", None):
            self.d.register(junk)
        self.assertEqual(self.d.drain(), [])

    def test_registration_from_another_thread_is_visible(self):
        """dispatcher 用 run_task_with_deadline 把任務丟到工作執行緒 —— 若用
        thread-local 就會在主執行緒 drain 到空的。"""
        import threading
        t = threading.Thread(target=self.d.register, args=("/x/from_thread.xlsx",))
        t.start()
        t.join()
        self.assertEqual(self.d.drain(), ["/x/from_thread.xlsx"])


class ExtractWithSideChannelTests(unittest.TestCase):
    def setUp(self):
        from agent_core import daemon_dispatcher, deliverables
        self.dd = daemon_dispatcher
        deliverables.drain()
        self.root = os.path.realpath(os.path.join(_REPO_ROOT, "var", "data", "exports"))
        os.makedirs(self.root, exist_ok=True)
        self.good = os.path.join(self.root, "_side_channel_test.xlsx")
        with open(self.good, "w", encoding="utf-8") as fh:
            fh.write("x")

    def tearDown(self):
        try:
            os.remove(self.good)
        except OSError:
            pass

    def _extract(self, text, extra=None):
        with mock.patch.object(self.dd, "_attachment_allow_root", return_value=self.root):
            return self.dd.extract_mail_attachments(text, extra=extra)

    def test_side_channel_recovers_dropped_marker(self):
        """LLM 完全沒抄標記 —— 這正是要救的情形。"""
        body, paths = self._extract("報表內文，模型忘了抄標記", extra=[self.good])
        self.assertEqual(paths, [self.good])
        self.assertIn("報表內文", body)

    def test_marker_and_side_channel_do_not_double_attach(self):
        _, paths = self._extract(f"內文\n[[MAIL_FILE:{self.good}]]", extra=[self.good])
        self.assertEqual(paths, [self.good])

    def test_side_channel_still_obeys_the_allowlist(self):
        """側通道不經 LLM，但一樣不給繞過白名單的特權。"""
        _, paths = self._extract("內文", extra=["/etc/passwd"])
        self.assertEqual(paths, [])

    def test_side_channel_missing_file_dropped(self):
        _, paths = self._extract("內文", extra=[os.path.join(self.root, "nope.xlsx")])
        self.assertEqual(paths, [])

    def test_backward_compatible_without_extra(self):
        """既有呼叫端沒傳 extra 時行為不變。"""
        _, paths = self.dd.extract_mail_attachments("純文字")
        self.assertEqual(paths, [])


class TaskIsolationTests(unittest.TestCase):
    """每支任務開跑前清空 —— 否則 A 的附件會寄給 B。"""

    def setUp(self):
        from agent_core import daemon_dispatcher, deliverables
        self.dd = daemon_dispatcher
        self.dl = deliverables
        self.dl.drain()

    tearDown = setUp

    def test_run_one_task_clears_leftovers(self):
        self.dl.register("/x/上一支任務留下的.xlsx")

        types = mock.MagicMock()
        with mock.patch.object(self.dd, "run_deterministic_task", return_value="ok"):
            self.dd.run_one_dispatcher_task(
                {"name": "t", "prompt": "p", "deterministic_tool": "some_tool"},
                tools_list=[], gemini_model="m",
                agent_client_factory=lambda: mock.MagicMock(),
                agent_types_factory=lambda: types,
            )
        self.assertEqual(self.dl.peek(), [],
                         "deterministic 早退路徑也必須清空，否則殘留檔會夾進下一封信")


if __name__ == "__main__":
    unittest.main()
