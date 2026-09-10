"""add_scheduled_task 的 notify_emails 參數 —— 解析/驗證/儲存。

notify_emails 是給 daemon_dispatcher.notify_dispatcher_result 用的信任本地
清單（不是 LLM 輸出）：設定後改走「每個地址各自收到自己寄給自己的一封信」
（見 agent_core.actor_google_tools.send_gmail_as），取代預設單一 owner email。
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
import unittest


class AddScheduledTaskNotifyEmailsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmpdir, "daemon_tasks.json")

        import agent_core.scheduler as sched
        importlib.reload(sched)
        self.sched = sched
        self.sched.DAEMON_TASKS_FILE = self.tasks_file
        self.sched.DAEMON_TASKS_LOCK = self.tasks_file + ".lock"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_tasks(self):
        with open(self.tasks_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_no_notify_emails_stores_empty_list(self):
        result = self.sched.add_scheduled_task("t1", "查一下東西")
        self.assertIn("✅", result)
        task = self._read_tasks()["tasks"][0]
        self.assertEqual(task["notify_emails"], [])

    def test_single_email_parsed(self):
        self.sched.add_scheduled_task(
            "t1", "查一下東西", notify_emails="owner@company.example"
        )
        task = self._read_tasks()["tasks"][0]
        self.assertEqual(task["notify_emails"], ["owner@company.example"])

    def test_multiple_emails_comma_separated(self):
        self.sched.add_scheduled_task(
            "t1", "查一下東西",
            notify_emails="owner@company.example,gm@company.example,twsales@company.example",
        )
        task = self._read_tasks()["tasks"][0]
        self.assertEqual(
            task["notify_emails"],
            ["owner@company.example", "gm@company.example", "twsales@company.example"],
        )

    def test_chinese_separators_and_whitespace_tolerated(self):
        self.sched.add_scheduled_task(
            "t1", "查一下東西",
            notify_emails=" owner@company.example、gm@company.example ；production-mgr@company.example",
        )
        task = self._read_tasks()["tasks"][0]
        self.assertEqual(
            task["notify_emails"],
            ["owner@company.example", "gm@company.example", "production-mgr@company.example"],
        )

    def test_invalid_email_rejected_without_writing(self):
        result = self.sched.add_scheduled_task(
            "t1", "查一下東西", notify_emails="owner@company.example,not-an-email"
        )
        self.assertIn("錯誤", result)
        self.assertIn("not-an-email", result)
        self.assertFalse(os.path.exists(self.tasks_file))

    def test_success_message_lists_recipients(self):
        result = self.sched.add_scheduled_task(
            "t1", "查一下東西", notify_emails="owner@company.example,gm@company.example"
        )
        self.assertIn("owner@company.example", result)
        self.assertIn("gm@company.example", result)


if __name__ == "__main__":
    unittest.main()
