"""治本回歸：Google Chat backup 必須在 Drive sync 之前跑。

Chat 原本排夜跑最後一個階段，夜跑跑不完時被餓死、凍結 ~2 週（2026-06-28 查出）。
改成最先跑後，這個測試鎖住順序，防止有人不小心又移回最後。
"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_core.ingest import rag_runner


class ChatFirstOrderingTests(unittest.TestCase):
    def test_chat_backup_runs_before_drive_sync(self):
        order: list[str] = []
        targets = {
            "all_drives": True,
            "chat_backup": {
                "enabled": True, "admin_subject": "a@b.com",
                "service_account_file": "sa.json", "drive_folder_id": "fid",
            },
        }
        with mock.patch("agent_core.ingest.sync_config.load_targets", return_value=targets), \
             mock.patch("agent_core.ingest.drive_sync.sync_all_drives",
                        side_effect=lambda *a, **k: (order.append("drive"), {"synced": 0})[1]), \
             mock.patch.object(rag_runner, "_compact_sync_result", return_value={}), \
             mock.patch.object(rag_runner, "_sync_chat_backup",
                               side_effect=lambda *a, **k: (order.append("chat"), {"spaces": 0})[1]), \
             mock.patch.object(rag_runner, "_sync_gmail_accounts", return_value=[]):
            result = rag_runner._run_sync_locked()

        self.assertEqual(order, ["chat", "drive"], "chat 必須在 drive 之前")
        self.assertIn("chat", result)


if __name__ == "__main__":
    unittest.main()
