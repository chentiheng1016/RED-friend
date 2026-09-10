"""Google Chat backup/ingest tests — pure unittest, no network/Chroma.

conftest autouse fixtures are inert under `unittest discover`, so all
isolation lives in setUp/tearDown (temp cursor file, mocked API clients).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")


def _http_error(status: int):
    from googleapiclient.errors import HttpError

    resp = mock.Mock()
    resp.status = status
    resp.reason = "Forbidden" if status == 403 else "Error"
    return HttpError(resp, b"{}")


def _chat_service(spaces, messages_by_space=None):
    """Build a mock Chat client: spaces().list and spaces().messages().list."""
    messages_by_space = messages_by_space or {}
    svc = mock.MagicMock()
    spaces_obj = svc.spaces.return_value

    spaces_list_req = mock.MagicMock()
    spaces_list_req.execute.return_value = {"spaces": spaces}
    spaces_obj.list.return_value = spaces_list_req

    def messages_list(**kwargs):
        parent = kwargs.get("parent")
        req = mock.MagicMock()
        req.execute.return_value = {"messages": messages_by_space.get(parent, [])}
        return req

    spaces_obj.messages.return_value.list.side_effect = messages_list
    return svc


def _drive_service(existing_files=None, existing_json=None):
    drive = mock.MagicMock()
    files = drive.files.return_value

    list_req = mock.MagicMock()
    list_req.execute.return_value = {"files": existing_files or []}
    files.list.return_value = list_req

    media_req = mock.MagicMock()
    media_req.execute.return_value = (
        json.dumps(existing_json).encode("utf-8") if existing_json is not None else b"{}"
    )
    files.get_media.return_value = media_req

    files.update.return_value = mock.MagicMock()
    files.create.return_value = mock.MagicMock()
    return drive, files


def _msg(name, when, text="", formatted="", sender="Alice", attachments=None):
    m = {"name": name, "createTime": when, "sender": {"displayName": sender}}
    if text:
        m["text"] = text
    if formatted:
        m["formattedText"] = formatted
    if attachments:
        m["attachment"] = attachments
    return m


class MessageLineTests(unittest.TestCase):
    def test_formatted_text_preferred_and_attachment_suffix(self):
        from agent_core.ingest import chat_sync

        line = chat_sync._message_line(_msg(
            "spaces/A/messages/1", "2026-06-03T01:00:00Z",
            text="plain", formatted="*bold*",
            attachments=[{"contentName": "po.pdf"}],
        ))
        self.assertIn("*bold*", line)
        self.assertNotIn("plain", line)
        self.assertIn("po.pdf", line)
        self.assertIn("Alice", line)

    def test_empty_message_yields_empty_line(self):
        from agent_core.ingest import chat_sync
        self.assertEqual(
            chat_sync._message_line(_msg("spaces/A/messages/2", "t", sender="Bob")),
            "",
        )

    def test_sender_falls_back_to_resource_id(self):
        from agent_core.ingest import chat_sync
        line = chat_sync._message_line({
            "name": "m", "createTime": "t", "text": "hi",
            "sender": {"name": "users/999"},
        })
        self.assertIn("users/999", line)


class IngestTests(unittest.TestCase):
    def test_ingest_skips_when_no_text(self):
        from agent_core.ingest import chat_sync

        store = mock.MagicMock()
        with mock.patch.object(chat_sync, "get_store", return_value=store):
            n = chat_sync.ingest_space_messages(
                "spaces/A", "Team", "SPACE",
                [_msg("spaces/A/messages/1", "t", sender="Bob")],  # no text
            )
        self.assertEqual(n, 0)
        store.upsert_batch.assert_not_called()

    def test_ingest_upserts_with_space_doc_id(self):
        from agent_core.ingest import chat_sync

        store = mock.MagicMock()
        with mock.patch.object(chat_sync, "get_store", return_value=store):
            n = chat_sync.ingest_space_messages(
                "spaces/A", "Team Space", "SPACE",
                [_msg("spaces/A/messages/1", "2026-06-03T01:00:00Z", text="hello world")],
            )
        self.assertGreaterEqual(n, 1)
        store.upsert_batch.assert_called_once()
        ids, _docs, metas = store.upsert_batch.call_args.args
        self.assertTrue(all(i.startswith("spaces/A__") for i in ids))
        self.assertEqual(metas[0]["doc_id"], "spaces/A")
        self.assertEqual(metas[0]["space_type"], "SPACE")
        self.assertEqual(metas[0]["last_message_time"], "2026-06-03T01:00:00Z")
        # No rag_access.json rule for this space -> conservative default:
        # only Red can see it, not every department bot.
        self.assertTrue(metas[0]["access_red"])
        self.assertFalse(metas[0]["access_orange"])
        self.assertFalse(metas[0]["access_green"])

    def test_ingest_passes_space_and_display_name_to_access_gateway(self):
        from agent_core.ingest import chat_sync

        store = mock.MagicMock()
        with mock.patch.object(chat_sync, "get_store", return_value=store), \
             mock.patch.object(
                 chat_sync, "metadata_access_fields", return_value={"access_red": True},
             ) as fake_access:
            chat_sync.ingest_space_messages(
                "spaces/A", "Team Space", "SPACE",
                [_msg("spaces/A/messages/1", "2026-06-03T01:00:00Z", text="hello world")],
            )
        fake_access.assert_called_once_with(
            "chat", space_name="spaces/A", display_name="Team Space",
        )


class DriveBackupTests(unittest.TestCase):
    def test_creates_file_when_absent(self):
        from agent_core.ingest import chat_sync

        drive, files = _drive_service(existing_files=[])
        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        payload = chat_sync.backup_space_to_drive(
            drive, space,
            [_msg("spaces/A/messages/1", "2026-06-03T01:00:00Z", text="hi")],
            "folder123",
        )
        files.create.assert_called_once()
        files.update.assert_not_called()
        self.assertEqual(payload["message_count"], 1)

    def test_updates_and_merges_when_present(self):
        from agent_core.ingest import chat_sync

        existing = {
            "messages": [_msg("spaces/A/messages/1", "2026-06-03T01:00:00Z", text="old")],
        }
        drive, files = _drive_service(
            existing_files=[{"id": "file-1"}], existing_json=existing,
        )
        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        payload = chat_sync.backup_space_to_drive(
            drive, space,
            [_msg("spaces/A/messages/2", "2026-06-03T02:00:00Z", text="new")],
            "folder123",
        )
        files.update.assert_called_once()
        files.create.assert_not_called()
        # merged: old + new, deduped by message name, sorted by createTime
        self.assertEqual(payload["message_count"], 2)
        self.assertEqual(payload["messages"][0]["name"], "spaces/A/messages/1")
        self.assertEqual(payload["messages"][1]["name"], "spaces/A/messages/2")


class SyncUserTests(unittest.TestCase):
    def setUp(self):
        from agent_core.ingest import chat_sync
        self.drive, _ = _drive_service(existing_files=[])
        # sync_user 現在每個 space 成功就 _save_cursors → 必須隔離 cursor 檔，
        # 否則測試會覆寫真部署的 var/state/chat_sync_cursors.json。
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir, ignore_errors=True))
        self.cursor_file = os.path.join(self.tmpdir, "chat_sync_cursors.json")
        self._cursor_patch = mock.patch.object(chat_sync, "_CURSOR_FILE", self.cursor_file)
        self._cursor_patch.start()
        self.addCleanup(self._cursor_patch.stop)

    def _run(self, chat_svc, *, cursors=None, seen=None, store=None):
        from agent_core.ingest import chat_sync

        errors: list[str] = []
        cursors = cursors if cursors is not None else {}
        seen = seen if seen is not None else set()
        store = store or mock.MagicMock()
        with mock.patch.object(chat_sync, "get_service_for_account", return_value=chat_svc), \
             mock.patch.object(chat_sync, "get_store", return_value=store):
            stats = chat_sync.sync_user(
                "bob@example.com", self.drive,
                service_account_file="sa.json",
                drive_folder_id="folder123",
                seen_spaces=seen, cursors=cursors, errors=errors,
            )
        return stats, cursors, errors, store

    def test_cursor_used_as_message_filter(self):
        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        chat_svc = _chat_service(
            [space],
            {"spaces/A": [_msg("spaces/A/messages/9", "2026-06-03T05:00:00Z", text="new")]},
        )
        cursors = {"spaces/A": {"last_message_time": "2026-06-03T00:00:00Z"}}
        _stats, _cursors, _errors, _store = self._run(chat_svc, cursors=cursors)
        list_fn = chat_svc.spaces.return_value.messages.return_value.list
        self.assertEqual(
            list_fn.call_args.kwargs["filter"], 'createTime > "2026-06-03T00:00:00Z"',
        )

    def test_cursor_advances_only_on_success(self):
        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        chat_svc = _chat_service(
            [space],
            {"spaces/A": [_msg("spaces/A/messages/9", "2026-06-03T05:00:00Z", text="new")]},
        )
        stats, cursors, errors, _store = self._run(chat_svc)
        self.assertEqual(stats["spaces"], 1)
        self.assertEqual(cursors["spaces/A"]["last_message_time"], "2026-06-03T05:00:00Z")
        self.assertEqual(errors, [])

    def test_cursor_not_advanced_when_ingest_fails(self):
        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        chat_svc = _chat_service(
            [space],
            {"spaces/A": [_msg("spaces/A/messages/9", "2026-06-03T05:00:00Z", text="new")]},
        )
        store = mock.MagicMock()
        store.upsert_batch.side_effect = RuntimeError("chroma down")
        stats, cursors, errors, _store = self._run(chat_svc, store=store)
        self.assertEqual(stats["spaces"], 0)
        self.assertNotIn("spaces/A", cursors)
        self.assertTrue(any("spaces/A" in e for e in errors))

    def test_cursor_persisted_immediately_after_each_space(self):
        """每個 space 成功後 cursor 就要落盤 — 整輪跑數十分鐘，daemon 中途被砍
        不能丟掉已完成 space 的進度（之前只在 sync_domain_chat 結尾存一次）。"""
        space_a = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team A"}
        space_b = {"name": "spaces/B", "spaceType": "SPACE", "displayName": "Team B"}
        chat_svc = _chat_service(
            [space_a, space_b],
            {
                "spaces/A": [_msg("spaces/A/messages/1", "2026-06-03T05:00:00Z", text="a")],
                "spaces/B": [_msg("spaces/B/messages/1", "2026-06-03T06:00:00Z", text="b")],
            },
        )
        from agent_core.ingest import chat_sync

        snapshots: list[set] = []
        real_save = chat_sync._save_cursors

        def spy_save(cursors):
            real_save(cursors)
            with open(self.cursor_file, encoding="utf-8") as f:
                snapshots.append(set(json.load(f)))

        with mock.patch.object(chat_sync, "_save_cursors", side_effect=spy_save):
            stats, _cursors, errors, _store = self._run(chat_svc)

        self.assertEqual(stats["spaces"], 2)
        self.assertEqual(errors, [])
        # 兩個 space → 兩次即時落盤；第一次只含 A（B 還沒跑完就已持久化）。
        self.assertEqual(snapshots[0], {"spaces/A"})
        self.assertEqual(snapshots[1], {"spaces/A", "spaces/B"})

    def test_cursor_save_failure_does_not_fail_the_space(self):
        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        chat_svc = _chat_service(
            [space],
            {"spaces/A": [_msg("spaces/A/messages/9", "2026-06-03T05:00:00Z", text="new")]},
        )
        from agent_core.ingest import chat_sync
        with mock.patch.object(chat_sync, "_save_cursors", side_effect=OSError("disk full")):
            stats, cursors, errors, _store = self._run(chat_svc)
        # space 本身算成功（backup+ingest 都完成），存檔失敗只影響下輪重抓。
        self.assertEqual(stats["spaces"], 1)
        self.assertEqual(errors, [])
        self.assertEqual(cursors["spaces/A"]["last_message_time"], "2026-06-03T05:00:00Z")

    def test_dedup_skips_already_seen_space(self):
        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        chat_svc = _chat_service(
            [space],
            {"spaces/A": [_msg("spaces/A/messages/9", "2026-06-03T05:00:00Z", text="x")]},
        )
        seen = {"spaces/A"}  # another member already handled it
        stats, _cursors, _errors, store = self._run(chat_svc, seen=seen)
        self.assertEqual(stats["spaces"], 0)
        store.upsert_batch.assert_not_called()
        self.drive.files.return_value.create.assert_not_called()


class SyncDomainChatTests(unittest.TestCase):
    def setUp(self):
        from agent_core.ingest import chat_sync
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.cursor_file = os.path.join(self.tmpdir, "chat_sync_cursors.json")
        self._p = mock.patch.object(chat_sync, "_CURSOR_FILE", self.cursor_file)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.drive, _ = _drive_service(existing_files=[])

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_incomplete_config_returns_error(self):
        from agent_core.ingest import chat_sync
        out = chat_sync.sync_domain_chat(
            admin_subject="", service_account_file="sa.json", drive_folder_id="f",
        )
        self.assertEqual(out["users"], 0)
        self.assertTrue(out["errors"])

    def test_admin_403_aborts_with_hint(self):
        from agent_core.ingest import chat_sync
        with mock.patch.object(chat_sync, "list_domain_users", side_effect=_http_error(403)):
            out = chat_sync.sync_domain_chat(
                admin_subject="admin@example.com",
                service_account_file="sa.json", drive_folder_id="f",
            )
        self.assertEqual(out["users"], 0)
        self.assertTrue(any("admin.directory" in e for e in out["errors"]))

    def test_chat_403_short_circuits_remaining_users(self):
        from agent_core.ingest import chat_sync

        call_count = {"n": 0}

        def fail_first(*_a, **_k):
            call_count["n"] += 1
            raise _http_error(403)

        with mock.patch.object(
            chat_sync, "list_domain_users",
            return_value=["a@x.com", "b@x.com", "c@x.com"],
        ), mock.patch.object(chat_sync, "_drive_service", return_value=self.drive), \
             mock.patch.object(chat_sync, "sync_user", side_effect=fail_first):
            out = chat_sync.sync_domain_chat(
                admin_subject="admin@x.com",
                service_account_file="sa.json", drive_folder_id="f",
            )
        # short-circuited after the first 403, not all three users tried
        self.assertEqual(call_count["n"], 1)
        self.assertTrue(any("Chat API" in e for e in out["errors"]))

    def test_suspended_user_excluded(self):
        from agent_core.ingest import chat_sync

        directory = mock.MagicMock()
        users_req = mock.MagicMock()
        users_req.execute.return_value = {
            "users": [
                {"primaryEmail": "active@x.com", "suspended": False},
                {"primaryEmail": "gone@x.com", "suspended": True},
            ],
        }
        directory.users.return_value.list.return_value = users_req
        with mock.patch.object(chat_sync, "get_service_for_account", return_value=directory):
            emails = chat_sync.list_domain_users("admin@x.com", "sa.json")
        self.assertEqual(emails, ["active@x.com"])

    def test_cursors_persisted_after_run(self):
        from agent_core.ingest import chat_sync

        space = {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Team"}
        chat_svc = _chat_service(
            [space],
            {"spaces/A": [_msg("spaces/A/messages/9", "2026-06-03T05:00:00Z", text="hi")]},
        )
        store = mock.MagicMock()
        with mock.patch.object(chat_sync, "list_domain_users", return_value=["bob@x.com"]), \
             mock.patch.object(chat_sync, "_drive_service", return_value=self.drive), \
             mock.patch.object(chat_sync, "get_service_for_account", return_value=chat_svc), \
             mock.patch.object(chat_sync, "get_store", return_value=store):
            out = chat_sync.sync_domain_chat(
                admin_subject="admin@x.com",
                service_account_file="sa.json", drive_folder_id="folder123",
            )
        self.assertEqual(out["users"], 1)
        self.assertEqual(out["spaces"], 1)
        with open(self.cursor_file, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["spaces/A"]["last_message_time"], "2026-06-03T05:00:00Z")


class ConfigAndCollectionTests(unittest.TestCase):
    def test_chat_collection_is_valid(self):
        from agent_core.ingest import vector_store
        self.assertIn("google_chat_messages", vector_store._VALID_COLLECTIONS)

    def test_enable_disable_chat_backup_roundtrip(self):
        from agent_core.ingest import sync_config as sc

        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        path = os.path.join(tmpdir, "rag_sync_targets.json")
        with mock.patch.object(sc, "_CONFIG_FILE", path):
            sc.enable_chat_backup(
                "admin@x.com", "folder123", space_filter="", max_users=3,
            )
            cfg = sc.load_targets()["chat_backup"]
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["admin_subject"], "admin@x.com")
            self.assertEqual(cfg["drive_folder_id"], "folder123")
            self.assertEqual(cfg["max_users"], 3)

            sc.disable_chat_backup()
            self.assertFalse(sc.load_targets()["chat_backup"]["enabled"])

    def test_enable_chat_backup_requires_admin_and_folder(self):
        from agent_core.ingest import sync_config as sc
        with self.assertRaises(ValueError):
            sc.enable_chat_backup("", "folder123")
        with self.assertRaises(ValueError):
            sc.enable_chat_backup("admin@x.com", "")


if __name__ == "__main__":
    unittest.main()
