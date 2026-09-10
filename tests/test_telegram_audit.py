from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock


class TelegramAuditTests(unittest.TestCase):
    def test_log_telegram_event_writes_redacted_jsonl(self):
        from agent_core import telegram_audit

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = os.path.join(tmpdir, "telegram_audit.jsonl")
            with mock.patch.dict(os.environ, {"RED_TELEGRAM_AUDIT_FILE": audit_path}), \
                    mock.patch("agent_core.operational_db.write_audit_event", return_value=True) as db_write:
                telegram_audit.log_telegram_event(
                    event="telegram_update",
                    status="ok",
                    chat_id="12345",
                    text="/dept green query.profile sk-proj-abcdefghijklmnopqrstuvwxyz123456",
                    reply="done",
                    actor={"color": "green", "source": "employee_registry", "name": "Alice"},
                    message={
                        "message_id": 7,
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 12345, "username": "alice"},
                    },
                    update_id=99,
                )

                recent = telegram_audit.read_recent_events()

        db_write.assert_called_once()
        self.assertEqual(len(recent), 1)
        record = recent[0]
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["chat_id"], "12345")
        self.assertEqual(record["actor_color"], "green")
        self.assertEqual(record["chat_type"], "private")
        self.assertEqual(record["from_username"], "alice")
        self.assertEqual(record["command"], "/dept")
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz123456", json.dumps(record))

    def test_classify_command_marks_freeform(self):
        from agent_core.telegram_audit import classify_command

        self.assertEqual(classify_command("/whoami"), "/whoami")
        self.assertEqual(classify_command("hello"), "freeform")
        self.assertEqual(classify_command(""), "")

    def test_read_recent_events_prefers_operational_db(self):
        from agent_core import telegram_audit

        db_rows = [{"event": "telegram_update", "status": "ok"}]
        with mock.patch("agent_core.operational_db.read_recent_audit_events", return_value=db_rows):
            self.assertEqual(telegram_audit.read_recent_events(), db_rows)


class TailReadTests(unittest.TestCase):
    """健檢 Low：read_recent_events 改檔尾反向讀塊，不整檔 readlines()。

    行為必須與舊版一致：取最後 N 行、壞 JSON 行跳過、檔不存在回 []。
    """

    def _write_lines(self, path, n, *, trailing_newline=True):
        with open(path, "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({"i": i, "status": "ok"}))
                if i < n - 1 or trailing_newline:
                    f.write("\n")

    def _events(self, path, limit):
        from agent_core import telegram_audit
        with mock.patch.dict(os.environ, {"RED_TELEGRAM_AUDIT_FILE": path}), \
                mock.patch("agent_core.operational_db.read_recent_audit_events",
                           return_value=[]):
            return telegram_audit.read_recent_events(limit=limit)

    def test_returns_last_n_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            self._write_lines(path, 500)
            events = self._events(path, 50)
        self.assertEqual(len(events), 50)
        self.assertEqual([e["i"] for e in events], list(range(450, 500)))

    def test_no_trailing_newline_last_line_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            self._write_lines(path, 10, trailing_newline=False)
            events = self._events(path, 5)
        self.assertEqual([e["i"] for e in events], [5, 6, 7, 8, 9])

    def test_file_shorter_than_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            self._write_lines(path, 3)
            events = self._events(path, 50)
        self.assertEqual([e["i"] for e in events], [0, 1, 2])

    def test_bad_json_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"i": 0}) + "\n")
                f.write("not json{{{\n")
                f.write(json.dumps({"i": 2}) + "\n")
            events = self._events(path, 50)
        self.assertEqual([e["i"] for e in events], [0, 2])

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = self._events(os.path.join(tmp, "nope.jsonl"), 50)
        self.assertEqual(events, [])

    def test_tail_crosses_block_boundary(self):
        # 每行灌大 payload，逼 tail 讀多個 64KB block 仍取對行
        from agent_core import telegram_audit
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for i in range(200):
                    f.write(json.dumps({"i": i, "pad": "x" * 2000}) + "\n")
            lines = telegram_audit._tail_lines(path, 60, block_size=4096)
        self.assertEqual(len(lines), 60)
        self.assertEqual(json.loads(lines[0])["i"], 140)
        self.assertEqual(json.loads(lines[-1])["i"], 199)

    def test_does_not_load_whole_file(self):
        # 直接鎖實作：readlines 不得出現在 read_recent_events 的檔案路徑
        import inspect
        from agent_core import telegram_audit
        src = inspect.getsource(telegram_audit.read_recent_events)
        self.assertNotIn("readlines", src)
        self.assertIn("_tail_lines", src)


if __name__ == "__main__":
    unittest.main()
