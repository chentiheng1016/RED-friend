from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock


class RagGatewayTests(unittest.TestCase):
    def test_metadata_access_fields_default_to_red_and_owner(self):
        from agent_core.rag_gateway import metadata_access_fields

        fields = metadata_access_fields(
            "drive",
            owner_color="green",
            department="樣品室",
            drive_id="drive-1",
        )

        self.assertEqual(fields["rag_source"], "drive")
        self.assertEqual(fields["owner_color"], "green")
        self.assertEqual(fields["department"], "樣品室")
        self.assertTrue(fields["access_red"])
        self.assertTrue(fields["access_green"])
        self.assertFalse(fields["access_orange"])

    def test_metadata_access_fields_uses_config_rules(self):
        from agent_core.rag_gateway import metadata_access_fields

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "rag_access.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "drive_sources": [{
                        "drive_id": "drive-sales",
                        "owner_color": "orange",
                        "department": "業務",
                        "allowed_colors": ["orange", "purple"],
                    }],
                    "gmail_mailboxes": [{
                        "mailbox_email": "accounting@example.com",
                        "owner_color": "purple",
                        "department": "會計",
                    }],
                }, handle)

            with mock.patch.dict(os.environ, {"RED_RAG_ACCESS_CONFIG": config_path}):
                drive = metadata_access_fields("drive", drive_id="drive-sales")
                gmail = metadata_access_fields(
                    "gmail",
                    mailbox_email="Accounting@Example.com",
                )

        self.assertEqual(drive["owner_color"], "orange")
        self.assertEqual(drive["department"], "業務")
        self.assertTrue(drive["access_orange"])
        self.assertTrue(drive["access_purple"])
        self.assertFalse(drive["access_green"])
        self.assertEqual(gmail["owner_color"], "purple")
        self.assertTrue(gmail["access_purple"])
        self.assertEqual(gmail["mailbox_email"], "accounting@example.com")

    def test_metadata_access_fields_chat_defaults_to_red_only(self):
        from agent_core.rag_gateway import metadata_access_fields

        fields = metadata_access_fields(
            "chat", space_name="spaces/unconfigured", display_name="Random Space",
        )

        self.assertEqual(fields["rag_source"], "chat")
        self.assertEqual(fields["owner_color"], "red")
        self.assertTrue(fields["access_red"])
        self.assertFalse(fields["access_orange"])
        self.assertFalse(fields["access_green"])
        self.assertFalse(fields["access_purple"])

    def test_metadata_access_fields_chat_uses_config_rule(self):
        from agent_core.rag_gateway import metadata_access_fields

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "rag_access.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "chat_spaces": [{
                        "space_name": "spaces/AAQAtjkOnQ4",
                        "display_name": "業務群組",
                        "owner_color": "orange",
                        "department": "業務",
                        "allowed_colors": ["orange", "purple"],
                    }],
                }, handle)

            with mock.patch.dict(os.environ, {"RED_RAG_ACCESS_CONFIG": config_path}):
                matched = metadata_access_fields(
                    "chat", space_name="spaces/AAQAtjkOnQ4", display_name="業務群組",
                )
                unmatched = metadata_access_fields(
                    "chat", space_name="spaces/other", display_name="其他群組",
                )

        self.assertEqual(matched["owner_color"], "orange")
        self.assertEqual(matched["department"], "業務")
        self.assertTrue(matched["access_orange"])
        self.assertTrue(matched["access_purple"])
        self.assertFalse(matched["access_green"])
        self.assertEqual(unmatched["owner_color"], "red")
        self.assertFalse(unmatched["access_orange"])

    def test_access_where_red_sees_all_department_gets_acl(self):
        """ACL 語意：大王沒有部門限制、部門色要有 access_<color>。

        access_where 同時是「排除小紅自產內容」的單一 chokepoint（三道檢索門
        都走這支），所以每個回傳值都會多帶一個 generated_by_red 條件——用
        include_generated=True 關掉才能單看 ACL 部分。
        """
        from agent_core.rag_gateway import access_where

        self.assertIsNone(access_where("red", include_generated=True))
        self.assertEqual(
            access_where("orange", include_generated=True),
            {"access_orange": {"$eq": True}},
        )
        self.assertEqual(
            access_where(
                "orange", base_where={"drive_id": {"$eq": "d1"}},
                include_generated=True,
            ),
            {
                "$and": [
                    {"drive_id": {"$eq": "d1"}},
                    {"access_orange": {"$eq": True}},
                ]
            },
        )

    def test_access_where_excludes_generated_by_default(self):
        """預設就要帶排除條件——drive_search / chat_search 直接叫這支，
        之前它們是敞開的（只有 semantic_search 自己疊）。"""
        from agent_core.rag_gateway import access_where

        self.assertEqual(
            access_where("red"), {"generated_by_red": {"$ne": True}},
        )
        self.assertEqual(
            access_where("orange"),
            {"$and": [
                {"generated_by_red": {"$ne": True}},
                {"access_orange": {"$eq": True}},
            ]},
        )

    def test_semantic_search_logs_audit(self):
        from agent_core.rag_gateway import semantic_search

        fake_store = mock.MagicMock()
        fake_store.query.return_value = [{"text": "hit", "metadata": {}, "distance": 0.1}]
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = os.path.join(tmpdir, "rag_audit.jsonl")
            with mock.patch.dict(os.environ, {"RED_RAG_ACCESS_AUDIT_FILE": audit_path}), \
                 mock.patch("agent_core.ingest.vector_store.get_store", return_value=fake_store):
                hits = semantic_search(
                    "gmail_threads",
                    "PAX quote",
                    caller="orange",
                    n_results=3,
                    trace_id="trace-1",
                )

            with open(audit_path, encoding="utf-8") as handle:
                row = json.loads(handle.readline())

        fake_store.query.assert_called_once_with(
            "PAX quote",
            n_results=3,
            where={"$and": [
                {"generated_by_red": {"$ne": True}},
                {"access_orange": {"$eq": True}},
            ]},
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(row["caller"], "orange")
        self.assertEqual(row["collection"], "gmail_threads")
        self.assertEqual(row["hit_count"], 1)
        self.assertEqual(row["trace_id"], "trace-1")


if __name__ == "__main__":
    unittest.main()
